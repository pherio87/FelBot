"""Crawler a lotti per Cloudflare Containers.

Stesse regole della pipeline su VPS (pipeline.py), organizzate per un
container che vive minuti e non giorni:

- un GIRO (completo o incrementale) dura molti lotti; il suo stato sta in D1
  (kv 'giro_corrente'), non nella memoria del container;
- l'enumerazione si fa una volta per giro, nel primo lotto;
- ogni lotto prende in carico N documenti dalla coda con una scadenza: se il
  container si ferma a meta', alla scadenza tornano in coda da soli;
- quando la coda e' vuota il giro si chiude, e il punto di ripartenza
  dell'incrementale avanza SOLO se il giro e' riuscito per intero.

I documenti si elaborano esattamente come nella v1.2: esclusione degli elenchi
nominativi prima e dopo lo scaricamento, OCR per pagina, frazionamento sui
confini degli atti. L'embedding lo fa il Worker (Workers AI, bge-m3).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .chunking import chunk_document
from .config import settings
from .extract import extract
from .fetcher import Fetcher
from .pipeline import EXTRACT_WORKERS, MAX_GONE_RATIO, load_sources
from .privacy import escluso_per_contenuto, escluso_per_titolo
from .remoto import CatalogoRemoto
from .sources import REGISTRY
from .sources.plone import EnumerationIncomplete

log = logging.getLogger(__name__)

LOTTO = 200                 # documenti presi in carico per volta
DURATA_MASSIMA = 45 * 60    # oltre, il lotto in corso finisce e il resto aspetta il prossimo avvio
BLOCCO_SCOPERTI = 100       # documenti per chiamata durante l'enumerazione


class Lotto:
    def __init__(self, remoto: CatalogoRemoto, fetcher: Fetcher, sources: list[dict] | None = None):
        self.r = remoto
        self.fetcher = fetcher
        self.sources = sources if sources is not None else load_sources()
        self.stats: dict[str, int] = {}
        self.errori: list[str] = []

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    async def _flush(self, **kw: Any) -> None:
        """Riporta al giro le statistiche accumulate e le azzera."""
        if self.stats or self.errori or kw:
            await self.r.aggiorna_giro(self.stats, self.errori, **kw)
        self.stats, self.errori = {}, []

    # ------------------------------------------------------------ giro
    async def esegui(self, modo: str, limite: int | None = None) -> dict[str, Any]:
        """modo: 'prova' (manuale, `limite` documenti), 'continua' (ogni 10
        minuti), 'notte' (apre il giro del giorno)."""
        t0 = time.time()
        stato = await self.r.stato()
        giro = stato.get("giro")

        if giro is None:
            mai_completato = not stato.get("last_run_finished")
            if modo == "notte" or modo == "prova" or mai_completato:
                giro = await self._apri(stato, modo)
            elif stato.get("discovered", 0) == 0:
                return {"esito": "niente da fare"}

        if giro is not None and not giro.get("enumerazione_fatta"):
            await self._enumera(giro)

        elaborati = 0
        while True:
            if limite is not None and elaborati >= limite:
                break
            if time.time() - t0 > DURATA_MASSIMA:
                log.info("tempo del lotto esaurito: il resto al prossimo avvio")
                break
            n = min(LOTTO, limite - elaborati) if limite is not None else LOTTO
            presi = await self.r.coda(n)
            if not presi:
                break
            await self._elabora(presi)
            elaborati += len(presi)
            await self._flush()
            log.info("elaborati %s documenti in questo avvio", elaborati)

        risultato: dict[str, Any] = {"modo": modo, "elaborati": elaborati,
                                     "durata_secondi": int(time.time() - t0)}
        if modo != "prova":
            chiusura = await self.r.chiudi_giro()
            risultato["giro"] = chiusura
        return risultato

    async def _apri(self, stato: dict[str, Any], modo: str) -> dict[str, Any]:
        ultimo = stato.get("last_run_finished")
        domenica = datetime.now(ZoneInfo("Europe/Rome")).weekday() == 6
        if not ultimo or domenica or modo == "prova":
            kind, since = "completa", None
        else:
            # margine di 3 giorni: pubblicazioni retrodatate e fusi orari
            kind = "incrementale"
            since = (datetime.fromisoformat(ultimo) - timedelta(days=3)).strftime("%Y-%m-%d")
        r = await self.r.apri_giro(kind, since)
        log.info("giro %s aperto%s", kind, f" (da {since})" if since else "")
        return r["giro"]

    # ------------------------------------------------------------ enumerazione
    async def _enumera(self, giro: dict[str, Any]) -> None:
        since = giro.get("since")
        tutte_complete = True
        for cfg in self.sources:
            cls = REGISTRY.get(cfg["kind"])
            if not cls:
                self.errori.append(f"tipo di sorgente sconosciuto: {cfg['kind']}")
                tutte_complete = False
                continue
            src = cls(cfg, self.fetcher)
            # il confronto con last_seen avviene sul clock del Worker: margine
            # di un minuto per eventuali differenze di orologio
            t0 = time.time() - 60
            n, completa, blocco = 0, False, []
            try:
                async for doc in src.enumerate(since=since):
                    n += 1
                    blocco.append(doc)
                    if len(blocco) >= BLOCCO_SCOPERTI:
                        self._bump("da_scaricare", await self.r.scoperti(blocco, settings.revalidate_days))
                        blocco = []
                if blocco:
                    self._bump("da_scaricare", await self.r.scoperti(blocco, settings.revalidate_days))
                completa = True
            except EnumerationIncomplete as exc:
                log.error("%s", exc)
                self.errori.append(str(exc))
                tutte_complete = False
            except Exception as exc:
                log.error("[%s] enumerazione interrotta: %s", src.id, exc)
                self.errori.append(f"[{src.id}] {exc}")
                tutte_complete = False
            self._bump("censiti", n)
            log.info("[%s] enumerazione %s: %s oggetti", src.id, "completa" if completa else "INTERROTTA", n)
            if completa and not since and n > 0:
                await self._rimozioni(src.id, t0)

        if since and settings.revalidate_days > 0:
            n = await self.r.rivalida(settings.revalidate_days)
            if n:
                self._bump("in_rivalidazione", n)
        await self._flush(enumerazione_completa=tutte_complete)

    async def _rimozioni(self, source_id: str, prima: float) -> None:
        """Ritira solo cio' che il portale conferma assente: vedi pipeline.py."""
        attivi, candidati = await self.r.candidati_rimozione(source_id, prima)
        if not candidati:
            return
        quota = len(candidati) / max(attivi, 1)
        if quota > MAX_GONE_RATIO:
            msg = (f"[{source_id}] {len(candidati)} documenti risultano scomparsi ({quota:.0%}): "
                   "quota implausibile, NESSUNA rimozione applicata")
            log.error(msg)
            self.errori.append(msg)
            self._bump("rimozioni_sospese", len(candidati))
            return
        ritirati, presenti, dubbi = [], [], 0
        for row in candidati:
            probe = row.get("api_url") or row["url"]
            try:
                res = await self.fetcher.get(probe, binary=True)
            except Exception as exc:
                log.warning("verifica di %s non riuscita: %s", probe, exc)
                dubbi += 1
                continue
            if res.status in (404, 410, 401, 403):
                ritirati.append(row["doc_id"])
            elif 200 <= res.status < 400:
                presenti.append(row["doc_id"])
            else:
                dubbi += 1
        await self.r.esito_rimozioni(presenti, ritirati)
        if ritirati:
            self._bump("rimossi", len(ritirati))
        if presenti:
            self._bump("sfuggiti_all_enumerazione", len(presenti))
        if dubbi:
            self._bump("rimozioni_non_verificate", dubbi)

    # ------------------------------------------------------------ documenti
    async def _elabora(self, righe: list[dict[str, Any]]) -> None:
        cpu = asyncio.Semaphore(EXTRACT_WORKERS)
        esiti: list[dict[str, Any]] = []
        risultati = await asyncio.gather(*(self._uno(r, cpu, esiti) for r in righe), return_exceptions=True)
        for row, res in zip(righe, risultati):
            if isinstance(res, Exception):
                log.error("documento %s: %s", row.get("url"), res)
                esiti.append({"doc_id": row["doc_id"], "campi": {
                    "status": "error", "error": str(res)[:500], "last_fetched": time.time()}})
                self._bump("errori_imprevisti")
        await self.r.esiti(esiti)

    async def _uno(self, row: dict[str, Any], cpu: asyncio.Semaphore, esiti: list[dict[str, Any]]) -> None:
        doc_id = row["doc_id"]
        url = row.get("download_url") or row["url"]

        def esito(svuota: bool = False, **campi: Any) -> None:
            campi.setdefault("last_fetched", time.time())
            esiti.append({"doc_id": doc_id, "campi": campi, "svuota": svuota})

        motivo = escluso_per_titolo(row.get("title"), row.get("filename"))
        if motivo:
            esito(svuota=True, status="excluded", error=motivo[:500])
            self._bump("esclusi_dati_personali")
            return
        try:
            res = await self.fetcher.get(url, etag=row.get("http_etag"),
                                         last_modified=row.get("http_last_mod"), binary=True)
        except Exception as exc:
            esito(status="error", error=f"fetch: {exc}"[:500])
            self._bump("errori_rete")
            return

        if res.not_modified:
            if row.get("ha_frammenti"):
                esito(status="indexed")
                self._bump("invariati")
            else:
                esito(status="empty", error="304 dal server ma nessun frammento a indice")
                self._bump("senza_testo")
            return
        if res.status == 999:
            esito(status="skipped", error="escluso da robots.txt")
            self._bump("esclusi_robots")
            return
        if res.status == 413:
            esito(status="skipped", error=f"file oltre {settings.max_file_mb} MB")
            self._bump("troppo_grandi")
            return
        if res.status >= 400 or res.content is None:
            esito(status="error", error=f"HTTP {res.status}")
            self._bump("errori_http")
            return

        content_hash = res.content_hash or ""
        etag, lastmod = res.headers.get("etag"), res.headers.get("last-modified")
        if content_hash and content_hash == row.get("indexed_hash") and row.get("ha_frammenti"):
            esito(status="indexed", http_etag=etag, http_last_mod=lastmod)
            self._bump("invariati")
            return

        mime = res.headers.get("content-type", row.get("mime") or "").split(";")[0].strip()
        async with cpu:
            ext = await asyncio.to_thread(extract, res.content, mime or row.get("mime"),
                                          row.get("filename") or url)

        if ext.n_chars < 60:
            esito(svuota=True, status="empty", text_chars=ext.n_chars, content_hash=content_hash,
                  error="; ".join(ext.warnings)[:500] or "nessun testo estraibile",
                  bytes=len(res.content), extract_method=ext.method)
            self._bump("senza_testo")
            return

        motivo = escluso_per_contenuto(ext.text)
        if motivo:
            esito(svuota=True, status="excluded", error=motivo[:500], content_hash=content_hash)
            self._bump("esclusi_dati_personali")
            return

        doc = dict(row)
        doc["meta"] = json.loads(row.get("meta_json") or "{}")
        chunks = chunk_document(doc, ext.blocks, target=settings.chunk_target_chars,
                                overlap=settings.chunk_overlap_chars)
        campi = dict(
            last_fetched=time.time(), content_hash=content_hash, bytes=len(res.content),
            text_chars=ext.n_chars, http_etag=etag, http_last_mod=lastmod, extract_method=ext.method,
            via_ocr=int(ext.via_ocr), truncated=int(ext.truncated),
            error="; ".join(ext.warnings)[:500] or None,
        )
        # frammenti, vettori e stato in un'unica chiamata: se l'embedding
        # fallisce, il documento resta com'era e finisce in errore
        await self.r.frammenti(doc_id, [
            {k: ch.get(k) for k in ("chunk_id", "ord", "page", "heading", "text")} for ch in chunks
        ], campi)
        self._bump("documenti_estratti")
        self._bump("chunk_creati", len(chunks))
        if ext.via_ocr:
            self._bump("documenti_da_ocr")


async def esegui_lotto(modo: str, limite: int | None = None) -> dict[str, Any]:
    # FLER_SOURCES: file di fonti alternativo (prove locali con un finto portale)
    alt = os.environ.get("FLER_SOURCES")
    sources = load_sources(Path(alt)) if alt else None
    remoto = CatalogoRemoto()
    fetcher = Fetcher()
    try:
        return await Lotto(remoto, fetcher, sources).esegui(modo, limite)
    finally:
        await fetcher.aclose()
        await remoto.aclose()
