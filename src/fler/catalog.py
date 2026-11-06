"""Catalogo SQLite: stato del crawl, documenti, frammenti e indice full-text.

E' la sorgente di verita' per gli aggiornamenti incrementali: ogni documento
porta con se' etag / last-modified / hash del contenuto, cosi' ad ogni giro si
riscarica e si re-indicizza solo cio' che e' davvero cambiato.

Due scelte non ovvie, entrambe frutto di difetti misurati:

- La tabella full-text conserva il `rowid` in `chunks.fts_rowid`. Cancellare
  per `chunk_id` su una colonna UNINDEXED costa una scansione integrale: a
  120.000 righe erano ~96 ms per frammento, cioe' ore di soli DELETE per una
  re-indicizzazione. Per rowid sono 0,9 ms costanti.

- Il titolo del documento NON e' una colonna separata dell'indice full-text.
  Stava in tre posti (colonna `title`, intestazione del frammento, e quindi
  colonna `text`) con peso 2,5 su un campo cortissimo: bastava un titolo
  parlante per portare in cima frammenti il cui corpo non trattava
  l'argomento. Ora il titolo vive solo nell'intestazione del frammento.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    api_url         TEXT,
    download_url    TEXT,
    portal_type     TEXT,
    title           TEXT,
    description     TEXT,
    mime            TEXT,
    filename        TEXT,
    section         TEXT,
    breadcrumb      TEXT,
    site_modified   TEXT,
    site_effective  TEXT,
    expires         TEXT,
    review_state    TEXT,
    meta_json       TEXT,

    http_etag       TEXT,
    http_last_mod   TEXT,
    content_hash    TEXT,
    blob_path       TEXT,
    bytes           INTEGER,
    text_chars      INTEGER,

    -- provenienza dell'estrazione: serve a marcare gli estratti inaffidabili
    extract_method  TEXT,
    via_ocr         INTEGER NOT NULL DEFAULT 0,
    truncated       INTEGER NOT NULL DEFAULT 0,

    status          TEXT NOT NULL DEFAULT 'discovered',
    error           TEXT,
    first_seen      REAL,
    last_seen       REAL,
    last_fetched    REAL,
    last_indexed    REAL,
    indexed_hash    TEXT,
    gone            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_doc_status  ON documents(status);
CREATE INDEX IF NOT EXISTS idx_doc_source  ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_doc_seen    ON documents(last_seen);
CREATE INDEX IF NOT EXISTS idx_doc_fetched ON documents(last_fetched);
CREATE INDEX IF NOT EXISTS idx_doc_stale   ON documents(gone, status);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id    TEXT NOT NULL UNIQUE,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    page        INTEGER,
    heading     TEXT,
    text        TEXT NOT NULL,
    n_chars     INTEGER NOT NULL,
    embedded    INTEGER NOT NULL DEFAULT 0,
    fts_rowid   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc  ON chunks(doc_id, ord);
CREATE INDEX IF NOT EXISTS idx_chunk_emb  ON chunks(embedded);

-- Indice lessicale: indispensabile per gli estremi degli atti
-- ("DGR 1298/2015", "PG.2017.333374", "L.R. 17/2005"), che l'embedding sfuma.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started     REAL,
    finished    REAL,
    kind        TEXT,
    outcome     TEXT,
    stats_json  TEXT
);

-- Vettori da togliere dall'indice semantico. Il chunk_id contiene un hash del
-- testo: quando un documento cambia, i frammenti nuovi hanno id nuovi e quelli
-- vecchi restavano in LanceDB per sempre. Non comparivano nelle risposte (la
-- idratazione li scarta) ma occupavano posti fra i risultati semantici, che
-- si assottigliavano a ogni aggiornamento. Qui si accodano per la rimozione.
CREATE TABLE IF NOT EXISTS vector_deletes (
    chunk_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS queries (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL,
    question  TEXT,
    answered  INTEGER,
    n_sources INTEGER,
    latency   REAL,
    feedback  TEXT
);
CREATE INDEX IF NOT EXISTS idx_queries_ts ON queries(ts);
"""

# Migrazioni per database creati da versioni precedenti
MIGRATIONS = [
    ("documents", "extract_method", "ALTER TABLE documents ADD COLUMN extract_method TEXT"),
    ("documents", "via_ocr", "ALTER TABLE documents ADD COLUMN via_ocr INTEGER NOT NULL DEFAULT 0"),
    ("documents", "truncated", "ALTER TABLE documents ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0"),
    ("chunks", "fts_rowid", "ALTER TABLE chunks ADD COLUMN fts_rowid INTEGER"),
    ("runs", "outcome", "ALTER TABLE runs ADD COLUMN outcome TEXT"),
]


class Catalog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=120, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, ddl in MIGRATIONS:
            try:
                cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if cols and column not in cols:
                try:
                    self.conn.execute(ddl)
                    self.conn.commit()
                except sqlite3.Error:
                    pass

    # ------------------------------------------------------------------ utils
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )

    # -------------------------------------------------------------- documenti
    def upsert_discovered(self, doc: dict[str, Any], revalidate_after_days: int | None = None) -> bool:
        """Registra/aggiorna un documento visto in enumerazione.

        Ritorna True se il documento va (ri)scaricato. Oltre al caso ovvio
        (data di modifica cambiata) sono da riscaricare:
        - i documenti mai scaricati o in errore;
        - quelli tornati disponibili dopo essere stati dati per rimossi;
        - quelli senza frammenti, che risulterebbero 'indicizzati' a vuoto;
        - quelli non rivalidati da oltre `revalidate_after_days` giorni, perche'
          il portale sostituisce allegati senza toccare la data di modifica: senza
          questa rivalidazione periodica un PDF cambiato non verrebbe mai ripreso.
        """
        if revalidate_after_days is None:
            from .config import settings as _s
            revalidate_after_days = _s.revalidate_days
        now = time.time()
        cur = self.conn.execute(
            "SELECT site_modified, status, content_hash, gone, last_fetched, doc_id "
            "FROM documents WHERE doc_id=?",
            (doc["doc_id"],),
        ).fetchone()

        fields = dict(
            source_id=doc["source_id"],
            url=doc["url"],
            api_url=doc.get("api_url"),
            download_url=doc.get("download_url"),
            portal_type=doc.get("portal_type"),
            title=doc.get("title"),
            description=doc.get("description"),
            mime=doc.get("mime"),
            filename=doc.get("filename"),
            section=doc.get("section"),
            breadcrumb=doc.get("breadcrumb"),
            site_modified=doc.get("site_modified"),
            site_effective=doc.get("site_effective"),
            expires=doc.get("expires"),
            review_state=doc.get("review_state"),
            meta_json=json.dumps(doc.get("meta", {}), ensure_ascii=False),
        )

        if cur is None:
            cols = ", ".join(fields)
            ph = ", ".join("?" for _ in fields)
            with self.tx() as c:
                c.execute(
                    f"INSERT INTO documents (doc_id, {cols}, status, first_seen, last_seen, gone) "
                    f"VALUES (?, {ph}, 'discovered', ?, ?, 0)",
                    (doc["doc_id"], *fields.values(), now, now),
                )
            return True

        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE documents SET {sets}, last_seen=?, gone=0 WHERE doc_id=?",
                (*fields.values(), now, doc["doc_id"]),
            )

        has_chunks = self.conn.execute(
            "SELECT 1 FROM chunks WHERE doc_id=? LIMIT 1", (doc["doc_id"],)
        ).fetchone() is not None

        changed = (cur["site_modified"] or "") != (doc.get("site_modified") or "")
        stale = (
            revalidate_after_days > 0
            and cur["status"] != "excluded"
            and (cur["last_fetched"] or 0) < now - revalidate_after_days * 86400
        )
        # "Senza frammenti" e' un'anomalia solo per un documento che si dichiara
        # indicizzato. Nella 1.1 la condizione valeva per tutti, quindi ogni
        # scansione completa riscaricava da capo i documenti senza testo e
        # quelli esclusi (robots, troppo grandi): centinaia di download inutili
        # a ogni giro verso il server della Regione.
        anomalo = not has_chunks and cur["status"] in ("indexed", "chunked")
        needs_fetch = (
            changed
            or cur["status"] in ("discovered", "error")
            or bool(cur["gone"])
            or anomalo
            or stale
        )
        if needs_fetch:
            with self.tx() as c:
                c.execute(
                    "UPDATE documents SET status='discovered' WHERE doc_id=?", (doc["doc_id"],)
                )
        return needs_fetch

    def pending(self, status: str, limit: int = 500, source_id: str | None = None):
        q = "SELECT * FROM documents WHERE status=? AND gone=0"
        args: list[Any] = [status]
        if source_id:
            q += " AND source_id=?"
            args.append(source_id)
        q += " ORDER BY last_seen DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(q, args).fetchall()

    def count_pending(self, status: str = "discovered") -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM documents WHERE status=? AND gone=0", (status,)
        ).fetchone()["n"]

    def mark(self, doc_id: str, **kw: Any) -> None:
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        with self.tx() as c:
            c.execute(f"UPDATE documents SET {sets} WHERE doc_id=?", (*kw.values(), doc_id))

    def has_chunks(self, doc_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM chunks WHERE doc_id=? LIMIT 1", (doc_id,)
        ).fetchone() is not None

    def count_active(self, source_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM documents WHERE source_id=? AND gone=0", (source_id,)
        ).fetchone()["n"]

    def unseen_count(self, source_id: str, before: float) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM documents WHERE source_id=? AND last_seen < ? AND gone=0",
            (source_id, before),
        ).fetchone()["n"]

    def count_active_all(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM documents WHERE gone=0"
        ).fetchone()["n"]

    def unseen(self, source_id: str, before: float, limit: int = 5000):
        return self.conn.execute(
            "SELECT doc_id, url, api_url FROM documents "
            "WHERE source_id=? AND last_seen < ? AND gone=0 LIMIT ?",
            (source_id, before, limit),
        ).fetchall()

    def touch_seen(self, doc_ids: list[str]) -> None:
        if not doc_ids:
            return
        now = time.time()
        with self.tx() as c:
            c.executemany("UPDATE documents SET last_seen=? WHERE doc_id=?",
                          [(now, d) for d in doc_ids])

    def mark_gone(self, doc_ids: list[str]) -> int:
        """Ritira documenti la cui assenza e' stata VERIFICATA uno per uno."""
        if not doc_ids:
            return 0
        with self.tx() as c:
            c.executemany("UPDATE documents SET gone=1 WHERE doc_id=? AND gone=0",
                          [(d,) for d in doc_ids])
        return len(doc_ids)

    def queue_stale(self, days: int, limit: int) -> int:
        """Rimette in coda i documenti non riverificati da piu' di `days` giorni.

        I piu' vecchi per primi, a quote: cosi' l'intero indice ruota nel
        periodo dichiarato senza concentrare il traffico in una sola notte.
        """
        soglia = time.time() - days * 86400
        with self.tx() as c:
            cur = c.execute(
                "UPDATE documents SET status='discovered' WHERE doc_id IN ("
                "  SELECT doc_id FROM documents WHERE gone=0 "
                "  AND status IN ('indexed','empty','skipped') "
                "  AND COALESCE(last_fetched,0) < ? "
                "  ORDER BY COALESCE(last_fetched,0) ASC LIMIT ?)",
                (soglia, limit),
            )
        return cur.rowcount

    def mark_gone_unseen(self, source_id: str, before: float) -> int:
        """Marca come rimossi i documenti non piu' visti.

        Da chiamare SOLO dopo un'enumerazione completa e dopo aver verificato
        che la quota di rimozioni sia plausibile: qui non c'e' nessuna rete di
        sicurezza, la decisione sta a monte (vedi pipeline.discover).
        """
        with self.tx() as c:
            cur = c.execute(
                "UPDATE documents SET gone=1 WHERE source_id=? AND last_seen < ? AND gone=0",
                (source_id, before),
            )
        return cur.rowcount

    def doc(self, doc_id: str):
        return self.conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()

    # ----------------------------------------------------------------- chunks
    def replace_chunks(self, doc_id: str, chunks: list[dict[str, Any]]) -> None:
        """Sostituisce i frammenti di un documento.

        La cancellazione dall'indice full-text avviene per rowid: e' l'unica
        forma che non degrada con la dimensione dell'indice.
        """
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO vector_deletes(chunk_id) "
                "SELECT chunk_id FROM chunks WHERE doc_id=? AND embedded=1", (doc_id,))
            if chunks:
                ph = ",".join("?" for _ in chunks)
                c.execute(f"DELETE FROM vector_deletes WHERE chunk_id IN ({ph})",
                          [ch["chunk_id"] for ch in chunks])
            old = c.execute(
                "SELECT fts_rowid FROM chunks WHERE doc_id=? AND fts_rowid IS NOT NULL",
                (doc_id,),
            ).fetchall()
            if old:
                c.executemany(
                    "DELETE FROM chunks_fts WHERE rowid=?", [(r["fts_rowid"],) for r in old]
                )
            c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

            for ch in chunks:
                cur = c.execute("INSERT INTO chunks_fts (text) VALUES (?)", (ch["text"],))
                c.execute(
                    "INSERT INTO chunks (chunk_id, doc_id, ord, page, heading, text, "
                    "n_chars, embedded, fts_rowid) VALUES (?,?,?,?,?,?,?,0,?)",
                    (ch["chunk_id"], doc_id, ch["ord"], ch.get("page"), ch.get("heading"),
                     ch["text"], len(ch["text"]), cur.lastrowid),
                )

    def pending_vector_deletes(self, limit: int = 2000) -> list[str]:
        return [r["chunk_id"] for r in self.conn.execute(
            "SELECT chunk_id FROM vector_deletes LIMIT ?", (limit,))]

    def clear_vector_deletes(self, ids: list[str]) -> None:
        with self.tx() as c:
            c.executemany("DELETE FROM vector_deletes WHERE chunk_id=?", [(i,) for i in ids])

    def unembedded(self, limit: int = 256):
        return self.conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE embedded=0 LIMIT ?", (limit,)
        ).fetchall()

    def mark_embedded(self, chunk_ids: Iterable[str]) -> None:
        with self.tx() as c:
            c.executemany("UPDATE chunks SET embedded=1 WHERE chunk_id=?", [(i,) for i in chunk_ids])

    def chunks_of_removed(self) -> list[str]:
        return [
            r["chunk_id"]
            for r in self.conn.execute(
                "SELECT c.chunk_id FROM chunks c JOIN documents d USING(doc_id) WHERE d.gone=1"
            )
        ]

    def purge_gone(self) -> int:
        """Cancella i frammenti dei documenti non piu' pubblicati.

        Riporta anche il documento a 'discovered' azzerando gli hash: se un
        giorno ricompare (o se era stato dato per rimosso per errore) deve
        essere riscaricato e rifrazionato, non restare invisibile per sempre
        con lo stato 'indicizzato' e zero frammenti.
        """
        rows = self.conn.execute(
            "SELECT c.fts_rowid FROM chunks c JOIN documents d USING(doc_id) "
            "WHERE d.gone=1 AND c.fts_rowid IS NOT NULL"
        ).fetchall()
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM chunks c JOIN documents d USING(doc_id) WHERE d.gone=1"
        ).fetchone()["n"]
        with self.tx() as c:
            c.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(r["fts_rowid"],) for r in rows])
            c.execute(
                "DELETE FROM chunks WHERE doc_id IN (SELECT doc_id FROM documents WHERE gone=1)"
            )
            c.execute(
                "UPDATE documents SET status='discovered', indexed_hash=NULL, content_hash=NULL, "
                "http_etag=NULL, http_last_mod=NULL WHERE gone=1"
            )
        return n

    def hydrate(self, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Recupera frammenti + metadati del documento, per costruire il contesto.

        Esclude i documenti ritirati: fra due esecuzioni della pipeline i loro
        frammenti sarebbero ancora recuperabili e finirebbero in una risposta.
        """
        if not chunk_ids:
            return {}
        ph = ",".join("?" for _ in chunk_ids)
        rows = self.conn.execute(
            f"""SELECT c.chunk_id, c.text, c.page, c.heading, c.ord,
                       d.doc_id, d.title, d.url, d.download_url, d.filename, d.mime,
                       d.portal_type, d.section, d.breadcrumb, d.site_modified,
                       d.site_effective, d.expires, d.review_state, d.source_id,
                       d.meta_json, d.via_ocr, d.truncated, d.extract_method
                FROM chunks c JOIN documents d USING(doc_id)
                WHERE c.chunk_id IN ({ph}) AND d.gone=0""",
            chunk_ids,
        ).fetchall()
        return {r["chunk_id"]: dict(r) for r in rows}

    def neighbours(self, doc_id: str, ord_: int, span: int = 1) -> list[str]:
        """Frammenti contigui a quello indicato, per non spezzare una procedura."""
        return [
            r["chunk_id"]
            for r in self.conn.execute(
                "SELECT chunk_id FROM chunks WHERE doc_id=? AND ord BETWEEN ? AND ? ORDER BY ord",
                (doc_id, ord_ - span, ord_ + span),
            )
        ]

    def source_of(self, chunk_ids: list[str]) -> dict[str, str]:
        if not chunk_ids:
            return {}
        ph = ",".join("?" for _ in chunk_ids)
        return {
            r["chunk_id"]: r["source_id"]
            for r in self.conn.execute(
                f"SELECT c.chunk_id, d.source_id FROM chunks c JOIN documents d USING(doc_id) "
                f"WHERE c.chunk_id IN ({ph})",
                chunk_ids,
            )
        }

    # ------------------------------------------------------------------ stato
    def stats(self) -> dict[str, Any]:
        q = self.conn.execute
        out = {
            "documenti": q("SELECT COUNT(*) n FROM documents WHERE gone=0").fetchone()["n"],
            "indicizzati": q("SELECT COUNT(*) n FROM documents WHERE status='indexed' AND gone=0").fetchone()["n"],
            "in_errore": q("SELECT COUNT(*) n FROM documents WHERE status='error'").fetchone()["n"],
            "senza_testo": q("SELECT COUNT(*) n FROM documents WHERE status='empty'").fetchone()["n"],
            "da_ocr": q("SELECT COUNT(*) n FROM documents WHERE via_ocr=1 AND gone=0").fetchone()["n"],
            "rimossi": q("SELECT COUNT(*) n FROM documents WHERE gone=1").fetchone()["n"],
            "esclusi_dati_personali": q("SELECT COUNT(*) n FROM documents WHERE status='excluded'").fetchone()["n"],
            "chunk": q("SELECT COUNT(*) n FROM chunks").fetchone()["n"],
            "chunk_da_vettorizzare": q("SELECT COUNT(*) n FROM chunks WHERE embedded=0").fetchone()["n"],
            "ultimo_aggiornamento": self.get_state("last_run_finished"),
            "ultimo_esito": self.get_state("last_run_outcome"),
        }
        out["per_tipo"] = {
            r["portal_type"] or "?": r["n"]
            for r in q("SELECT portal_type, COUNT(*) n FROM documents WHERE gone=0 "
                       "GROUP BY portal_type ORDER BY n DESC").fetchall()
        }
        return out

    def log_query(self, question: str, answered: bool, n_sources: int, latency: float) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO queries (ts, question, answered, n_sources, latency) VALUES (?,?,?,?,?)",
                (time.time(), question, int(answered), n_sources, latency),
            )
        return int(cur.lastrowid or 0)

    def purge_queries(self, keep_days: int = 90) -> int:
        """Le domande possono contenere nomi di allievi: non si conservano per sempre."""
        with self.tx() as c:
            cur = c.execute("DELETE FROM queries WHERE ts < ?", (time.time() - keep_days * 86400,))
        return cur.rowcount
