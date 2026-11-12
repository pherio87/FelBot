-- Crawler a lotti su Cloudflare Containers.
--
-- lease_until: un documento preso in carico da un lotto passa a
-- 'in_lavorazione' fino a questa scadenza. Se il container si ferma a meta'
-- (riavvio, limite di tempo), alla scadenza il documento torna in coda da solo
-- invece di restare bloccato per sempre.
ALTER TABLE documents ADD COLUMN lease_until REAL;
CREATE INDEX IF NOT EXISTS idx_doc_coda ON documents(status, gone, first_seen);
