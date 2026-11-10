-- FelBot — schema D1.
--
-- E' lo stesso catalogo del crawler Python (src/fler/catalog.py), portato su
-- D1: il crawler nel container lo scrive, il Worker lo legge. Differenze:
--  - niente PRAGMA (D1 li gestisce da se');
--  - chunks.vec_id: Vectorize accetta id di al massimo 64 byte, e un chunk_id
--    ("formazionelavoro:<uid>#<n>:<hash>") puo' superarli. vec_id e' lo SHA-1
--    esadecimale del chunk_id: 40 caratteri, sempre.
--  - uso e contatti: limiti per persona e protezione del modulo di contatto.

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
    bytes           INTEGER,
    text_chars      INTEGER,
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
    vec_id      TEXT NOT NULL UNIQUE,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    page        INTEGER,
    heading     TEXT,
    text        TEXT NOT NULL,
    n_chars     INTEGER NOT NULL,
    embedded    INTEGER NOT NULL DEFAULT 0,
    fts_rowid   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id, ord);
CREATE INDEX IF NOT EXISTS idx_chunk_emb ON chunks(embedded);

-- Indice lessicale: indispensabile per gli estremi degli atti
-- ("DGR 1298/2015", "PG.2017.333374"), che l'embedding sfuma.
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

-- vettori da togliere da Vectorize (frammenti sostituiti o documenti esclusi)
CREATE TABLE IF NOT EXISTS vector_deletes (
    vec_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- Domande per persona e per giorno: limite d'uso, e base per un eventuale
-- abbonamento. Non si registra il testo delle domande.
CREATE TABLE IF NOT EXISTS uso (
    email    TEXT NOT NULL,
    giorno   TEXT NOT NULL,
    domande  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (email, giorno)
);

-- Invii del modulo di contatto, per limitarne la frequenza. L'indirizzo IP
-- non e' conservato: solo una sua impronta (SHA-256), cancellata dopo un giorno.
CREATE TABLE IF NOT EXISTS contatti (
    ip_hash  TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contatti ON contatti(ip_hash, ts);
