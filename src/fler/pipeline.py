"""Orchestrazione dell'ingestione: enumera -> scarica -> estrae -> spezza -> vettorizza.

Ogni fase e' ripartibile: se il processo cade a meta', al giro successivo
riprende dai documenti rimasti indietro, perche' lo stato sta nel catalogo
SQLite e non in memoria.

Il principio che governa questo modulo e' uno: **un'enumerazione incompleta non
deve mai produrre cancellazioni**. Nella prima versione un 502 del portale a
meta' scansione faceva marcare come "non piu' pubblicati" diciassettemila
documenti ancora presenti, e la fase successiva ne cancellava i frammenti da
SQLite e da LanceDB. Qui la rimozione avviene solo dopo un'enumerazione
dichiarata completa dal portale stesso (confronto con `items_total`) e solo se
la quota di documenti scomparsi resta sotto una soglia di plausibilita'.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from .catalog import Catalog
from .chunking import chunk_document
from .config import settings
from .extract import extract
from .fetcher import Fetcher
from .privacy import escluso_per_contenuto, escluso_per_titolo
from .sources import REGISTRY
from .sources.plone import EnumerationIncomplete

log = logging.getLogger(__name__)

# Oltre questa quota di documenti scomparsi in un solo giro non si cancella
# nulla: e' molto piu' probabile un guasto del portale che una potatura vera.
MAX_GONE_RATIO = 0.05
# Estrazioni in parallelo: sono CPU-bound, non ha senso superare i core utili
EXTRACT_WORKERS = 3


def load_sources(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or Path(__file__).with_name("sources.yml")
    cfg = yaml.safe_load(p.read_text("utf-8"))
    return [s for s in cfg.get("sources", []) if s.get("enabled")]


@contextmanager
def ingest_lock(data_dir: Path) -> Iterator[bool]:
    """Lock esclusivo fra ingestioni.

    Lo scheduler, il cron suggerito nel README e il comando manuale possono
    partire insieme. Due enumerazioni sovrapposte si marcano a vicenda i
    documenti come scomparsi, oltre a raddoppiare il traffico verso la Regione
    violando il limite dichiarato.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "ingest.lock"
    fh = open(path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        fh.write(f"{time.time()}\n")
        fh.flush()
        yield True
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


class Pipeline:
    def __init__(self, catalog: Catalog | None = None):
        settings.ensure_dirs()
        self.cat = catalog or Catalog(settings.catalog_path)
        self.stats: dict[str, Any] = {}
        self.errors: list[str] = []

    def _bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    # ------------------------------------------------------------ 1. enumera
    async def discover(self, fetcher: Fetcher, sources: list[dict], since: str | None) -> bool:
        """Enumera le sorgenti. Ritorna True se TUTTE sono state percorse per intero."""
        tutte_complete = True

        for cfg in sources:
            cls = REGISTRY.get(cfg["kind"])
            if not cls:
                self.errors.append(f"tipo di sorgente sconosciuto: {cfg['kind']}")
                tutte_complete = False
                continue
            src = cls(cfg, fetcher)
            t0 = time.time()
            n = 0
            completa = False
            log.info("[%s] enumerazione avviata%s", src.id, f" (da {since})" if since else "")
            try:
                async for doc in src.enumerate(since=since):
                    n += 1
                    if self.cat.upsert_discovered(doc):
                        self._bump("da_scaricare")
                    if n % 1000 == 0:
                        log.info("[%s] %s oggetti censiti", src.id, n)
                completa = True
            except EnumerationIncomplete as exc:
                log.error("%s", exc)
                self.errors.append(str(exc))
                tutte_complete = False
            except Exception as exc:
                log.error("[%s] enumerazione interrotta: %s", src.id, exc)
                self.errors.append(f"[{src.id}] {exc}")
                tutte_complete = False

            self._bump("censiti", n)
            log.info("[%s] enumerazione %s: %s oggetti in %.0fs", src.id,
                     "completa" if completa else "INTERROTTA", n, time.time() - t0)

            # Il rilevamento delle rimozioni ha senso solo su una scansione
            # completa, non filtrata per data, e con una quota plausibile.
            if completa and not since and n > 0:
                await self._apply_removals(fetcher, src.id, t0)

        return tutte_complete

    async def _apply_removals(self, fetcher: Fetcher, source_id: str, before: float) -> None:
        """Ritira i documenti non piu' pubblicati, ma solo dopo averlo VERIFICATO.

        Nella 1.1 "non visto in enumerazione" valeva come "rimosso". Ma
        l'enumerazione e' dichiarata completa con una tolleranza del 2%
        (la paginazione a offset su un catalogo che cambia durante la scansione
        salta qualche oggetto), e la soglia di sicurezza scattava solo oltre il
        5%: fino a ~400 documenti ancora pubblicati potevano essere cancellati
        dall'indice in silenzio, con esito "ok". Ora ogni candidato viene
        interrogato singolarmente: si ritira solo cio' che il portale dichiara
        assente (404/410) o non piu' pubblico (401/403).
        """
        attivi = self.cat.count_active(source_id)
        candidati = self.cat.unseen(source_id, before)
        if not candidati:
            return
        quota = len(candidati) / max(attivi, 1)
        if quota > MAX_GONE_RATIO:
            msg = (f"[{source_id}] {len(candidati)} documenti risultano scomparsi "
                   f"({quota:.0%} del totale): quota implausibile, NESSUNA rimozione "
                   f"applicata. Verificare il portale e rilanciare 'fler ingest --full'.")
            log.error(msg)
            self.errors.append(msg)
            self._bump("rimozioni_sospese", len(candidati))
            return

        log.info("[%s] %s documenti non visti in enumerazione: verifica uno per uno",
                 source_id, len(candidati))
        ritirati: list[str] = []
        ancora_presenti: list[str] = []
        non_verificati = 0
        for row in candidati:
            probe = row["api_url"] or row["url"]
            try:
                res = await fetcher.get(probe, binary=True)
            except Exception as exc:
                log.warning("verifica di %s non riuscita: %s", probe, exc)
                non_verificati += 1
                continue
            if res.status in (404, 410, 401, 403):
                ritirati.append(row["doc_id"])
            elif 200 <= res.status < 400:
                ancora_presenti.append(row["doc_id"])
            else:
                # 999 (robots), 5xx esauriti i ritentativi, altro: nel dubbio
                # il documento resta dov'e'
                non_verificati += 1

        self.cat.touch_seen(ancora_presenti)
        gone = self.cat.mark_gone(ritirati)
        if gone:
            log.info("[%s] %s documenti non piu' pubblicati", source_id, gone)
            self._bump("rimossi", gone)
        if ancora_presenti:
            log.info("[%s] %s documenti sfuggiti all'enumerazione ma ancora pubblicati",
                     source_id, len(ancora_presenti))
            self._bump("sfuggiti_all_enumerazione", len(ancora_presenti))
        if non_verificati:
            self._bump("rimozioni_non_verificate", non_verificati)

    # -------------------------------------------------- 2. scarica + 3. estrai
    async def _process_one(self, fetcher: Fetcher, row, cpu: asyncio.Semaphore) -> None:
        doc_id = row["doc_id"]
        url = row["download_url"] or row["url"]

        # Elenchi nominativi: esclusi prima di scaricarli (vedi privacy.py)
        motivo = escluso_per_titolo(row["title"], row["filename"])
        if motivo:
            self._escludi(doc_id, motivo)
            return
        try:
            res = await fetcher.get(
                url, etag=row["http_etag"], last_modified=row["http_last_mod"], binary=True
            )
        except Exception as exc:
            self.cat.mark(doc_id, status="error", error=f"fetch: {exc}"[:500],
                          last_fetched=time.time())
            self._bump("errori_rete")
            return

        if res.not_modified:
            # Un 304 non basta a dichiarare il documento indicizzato: se
            # l'estrazione era fallita, resterebbe promosso a "indicizzato"
            # con zero frammenti, invisibile a ogni ricerca e a ogni diagnosi.
            if self.cat.has_chunks(doc_id):
                self.cat.mark(doc_id, status="indexed", last_fetched=time.time())
                self._bump("invariati")
            else:
                self.cat.mark(doc_id, status="empty", last_fetched=time.time(),
                              error="304 dal server ma nessun frammento a indice")
                self._bump("senza_testo")
            return
        if res.status == 999:
            self.cat.mark(doc_id, status="skipped", error="escluso da robots.txt",
                          last_fetched=time.time())
            self._bump("esclusi_robots")
            return
        if res.status == 413:
            self.cat.mark(doc_id, status="skipped", last_fetched=time.time(),
                          error=f"file oltre {settings.max_file_mb} MB")
            self._bump("troppo_grandi")
            return
        if res.status >= 400 or res.content is None:
            self.cat.mark(doc_id, status="error", error=f"HTTP {res.status}",
                          last_fetched=time.time())
            self._bump("errori_http")
            return

        content_hash = res.content_hash or ""
        if content_hash and content_hash == row["indexed_hash"] and self.cat.has_chunks(doc_id):
            self.cat.mark(doc_id, status="indexed", last_fetched=time.time(),
                          http_etag=res.headers.get("etag"),
                          http_last_mod=res.headers.get("last-modified"))
            self._bump("invariati")
            return

        mime = res.headers.get("content-type", row["mime"] or "").split(";")[0].strip()
        # L'estrazione e' codice sincrono CPU-bound (PyMuPDF, OCR, LibreOffice):
        # eseguirla sull'event loop bloccava tutte le altre coroutine e affamava
        # il rate limiter, rendendo l'ingestione seriale invece che sovrapposta.
        async with cpu:
            ext = await asyncio.to_thread(
                extract, res.content, mime or row["mime"], row["filename"] or url
            )

        if ext.n_chars < 60:
            self.cat.mark(
                doc_id, status="empty", last_fetched=time.time(), text_chars=ext.n_chars,
                error="; ".join(ext.warnings)[:500] or "nessun testo estraibile",
                content_hash=content_hash, bytes=len(res.content),
                extract_method=ext.method,
            )
            self._bump("senza_testo")
            return

        motivo = escluso_per_contenuto(ext.text)
        if motivo:
            self._escludi(doc_id, motivo, content_hash=content_hash)
            return

        doc = dict(row)
        doc["meta"] = json.loads(row["meta_json"] or "{}")
        chunks = chunk_document(
            doc, ext.blocks,
            target=settings.chunk_target_chars, overlap=settings.chunk_overlap_chars,
        )
        self.cat.replace_chunks(doc_id, chunks)
        self.cat.mark(
            doc_id, status="chunked", last_fetched=time.time(),
            content_hash=content_hash, bytes=len(res.content), text_chars=ext.n_chars,
            http_etag=res.headers.get("etag"),
            http_last_mod=res.headers.get("last-modified"),
            extract_method=ext.method,
            via_ocr=int(ext.via_ocr), truncated=int(ext.truncated),
            error="; ".join(ext.warnings)[:500] or None,
        )
        self._bump("documenti_estratti")
        if ext.via_ocr:
            self._bump("documenti_da_ocr")
        self._bump("chunk_creati", len(chunks))

    def _escludi(self, doc_id: str, motivo: str, **extra) -> None:
        # se il documento era gia' a indice (regole cambiate), i suoi frammenti
        # e i suoi vettori se ne vanno
        self.cat.replace_chunks(doc_id, [])
        self.cat.mark(doc_id, status="excluded", error=motivo[:500],
                      last_fetched=time.time(), **extra)
        self._bump("esclusi_dati_personali")

    async def fetch_and_extract(self, fetcher: Fetcher, limit: int | None = None) -> None:
        cpu = asyncio.Semaphore(EXTRACT_WORKERS)
        processed = 0
        stuck: set[str] = set()
        while True:
            batch = [r for r in self.cat.pending("discovered", limit=200)
                     if r["doc_id"] not in stuck]
            if not batch:
                break
            results = await asyncio.gather(
                *(self._process_one(fetcher, r, cpu) for r in batch), return_exceptions=True
            )
            for row, res in zip(batch, results):
                if isinstance(res, Exception):
                    log.error("documento %s: %s", row["url"], res)
                    self.cat.mark(row["doc_id"], status="error", error=str(res)[:500],
                                  last_fetched=time.time())
                    self._bump("errori_imprevisti")
                still = self.cat.doc(row["doc_id"])
                if still is not None and still["status"] == "discovered":
                    stuck.add(row["doc_id"])
            processed += len(batch)
            log.info("elaborati %s documenti (frammenti: %s, in coda: %s)", processed,
                     self.stats.get("chunk_creati", 0), self.cat.count_pending())
            if limit and processed >= limit:
                break

    # --------------------------------------------------------- 4. vettorizza
    def embed_pending(self) -> bool:
        from .embed import Embedder
        from .store import VectorStore

        emb = Embedder()
        store = VectorStore()
        total = 0
        ok = True
        while True:
            rows = self.cat.unembedded(limit=settings.embed_batch)
            if not rows:
                break
            ids = [r["chunk_id"] for r in rows]
            try:
                vectors = emb.encode([r["text"] for r in rows])
            except Exception as exc:
                log.error("vettorizzazione interrotta: %s", exc)
                self.errors.append(f"embedding: {exc}")
                ok = False
                break
            meta = self.cat.source_of(ids)
            try:
                store.upsert([
                    {"chunk_id": cid, "doc_id": cid.split("#")[0],
                     "source_id": meta.get(cid, ""), "vector": vec}
                    for cid, vec in zip(ids, vectors)
                ])
            except Exception as exc:
                log.error("scrittura dell'indice vettoriale interrotta: %s", exc)
                self.errors.append(f"indice vettoriale: {exc}")
                ok = False
                break
            self.cat.mark_embedded(ids)
            total += len(ids)
            if total % 960 == 0:
                log.info("vettorizzati %s frammenti", total)
        self._bump("chunk_vettorizzati", total)

        with self.cat.tx() as c:
            c.execute("""
                UPDATE documents SET status='indexed', last_indexed=?, indexed_hash=content_hash
                WHERE status='chunked' AND doc_id NOT IN (
                    SELECT DISTINCT doc_id FROM chunks WHERE embedded=0)
            """, (time.time(),))

        # La pulizia dei documenti ritirati avviene solo se la cancellazione
        # dei vettori e' andata a buon fine: cancellare da SQLite lasciando i
        # vettori orfani produce risultati di ricerca che non si possono
        # idratare, cioe' buchi silenziosi nel contesto.
        # vettori di frammenti sostituiti o esclusi
        while True:
            orfani = self.cat.pending_vector_deletes()
            if not orfani:
                break
            try:
                store.delete(orfani)
            except Exception as exc:
                log.error("vettori orfani non cancellati: %s", exc)
                self.errors.append(f"pulizia vettori orfani: {exc}")
                ok = False
                break
            self.cat.clear_vector_deletes(orfani)
            self._bump("vettori_orfani_rimossi", len(orfani))

        removed = self.cat.chunks_of_removed()
        if removed:
            try:
                store.delete(removed)
            except Exception as exc:
                log.error("frammenti ritirati non cancellati dall'indice vettoriale: %s", exc)
                self.errors.append(f"pulizia vettori: {exc}")
                ok = False
            else:
                n = self.cat.purge_gone()
                log.info("rimossi %s frammenti di documenti non piu' pubblicati", n)
                self._bump("chunk_rimossi", n)

        try:
            store.optimize()
        except Exception as exc:
            log.warning("ottimizzazione dell'indice non completata: %s", exc)
        return ok

    # ------------------------------------------------------------------- run
    async def run(self, mode: str = "incremental", sources: list[dict] | None = None,
                  limit: int | None = None, skip_embed: bool = False) -> dict:
        srcs = sources or load_sources()
        started = time.time()

        with ingest_lock(settings.data_dir) as acquired:
            if not acquired:
                log.error("un'altra ingestione e' gia' in corso: mi fermo")
                return {"esito": "gia_in_corso"}

            since: str | None = None
            if mode == "incremental":
                last = self.cat.get_state("last_run_finished")
                if last:
                    # margine di 3 giorni: copre pubblicazioni retrodatate e fusi orari
                    since = (datetime.fromisoformat(last) - timedelta(days=3)).strftime("%Y-%m-%d")
                else:
                    log.info("nessuna scansione precedente completata: modalita' completa")

            fetcher = Fetcher()
            try:
                complete = await self.discover(fetcher, srcs, since)
                if since and settings.revalidate_days > 0:
                    # Nella 1.1 la rivalidazione a 30 giorni dipendeva
                    # dall'enumerazione, ma l'aggiornamento notturno enumera
                    # solo cio' che e' cambiato: un documento con data
                    # invariata non veniva mai riverificato, cioe' proprio il
                    # caso (allegato sostituito senza toccare la data) per cui
                    # la rivalidazione esiste. Ora ogni notte se ne rimette in
                    # coda una quota, cosi' che l'intero indice ruoti nel
                    # periodo dichiarato.
                    quota = self.cat.count_active_all() // settings.revalidate_days + 20
                    n = self.cat.queue_stale(settings.revalidate_days, quota)
                    if n:
                        log.info("rivalidazione periodica: %s documenti rimessi in coda", n)
                        self._bump("in_rivalidazione", n)
                await self.fetch_and_extract(fetcher, limit=limit)
            finally:
                await fetcher.aclose()

            embed_ok = True if skip_embed else self.embed_pending()
            self.cat.purge_queries(keep_days=90)

            in_coda = self.cat.count_pending()
            success = complete and embed_ok and not self.errors and (limit is not None or in_coda == 0)
            outcome = "ok" if success else "parziale"
            self.stats["esito"] = outcome
            self.stats["durata_secondi"] = int(time.time() - started)
            if self.errors:
                self.stats["problemi"] = self.errors[:10]
            if in_coda:
                self.stats["ancora_in_coda"] = in_coda

            # Il punto di ripartenza dell'incrementale avanza SOLO se il giro e'
            # andato a buon fine. Avanzarlo dopo una scansione interrotta
            # dichiarava coperta una finestra temporale che nessuno aveva
            # guardato: quei documenti non sarebbero stati enumerati mai piu'.
            if success:
                finished = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                self.cat.set_state("last_run_finished", finished)
            else:
                log.warning("giro incompleto: il punto di ripartenza NON viene avanzato")
            self.cat.set_state("last_run_outcome", outcome)

            with self.cat.tx() as c:
                c.execute(
                    "INSERT INTO runs (started, finished, kind, outcome, stats_json) "
                    "VALUES (?,?,?,?,?)",
                    (started, time.time(), mode, outcome,
                     json.dumps(self.stats, ensure_ascii=False)),
                )
            log.info("scansione conclusa (%s): %s", outcome,
                     json.dumps(self.stats, ensure_ascii=False))
            return self.stats
