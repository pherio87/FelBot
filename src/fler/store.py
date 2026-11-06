"""Indice vettoriale su LanceDB: file su disco, nessun servizio da gestire,
adatto a un VPS piccolo e a qualche centinaio di migliaia di frammenti.

Tre dettagli che decidono se la ricerca semantica funziona davvero:

- `checkout_latest()` prima di ogni ricerca. Il processo web tiene la tabella
  aperta in memoria; lo scheduler, a fine ingestione, ricostruisce l'indice e
  compatta i file. Senza rilettura del manifest il processo web cerca in file
  che non esistono piu' e la ricerca vettoriale muore in silenzio.

- `nprobes` e `refine_factor` espliciti. L'indice IVF_PQ comprime 1536
  dimensioni in 48 byte (128:1) e con i valori di default sonda il 4% dello
  spazio senza ri-punteggiare sui vettori veri: su un corpus omogeneo come
  questo l'errore di quantizzazione e' dello stesso ordine dei divari fra
  candidati, e l'ordine dei primi risultati diventa in buona parte casuale.

- L'indice ANN si costruisce solo oltre le 100.000 righe. Sotto quella soglia
  la scansione piatta e' piu' PRECISA e ancora rapida: costruire l'indice
  troppo presto peggiora la qualita' senza guadagno percepibile.
"""
from __future__ import annotations

import logging
from pathlib import Path

import lancedb
import pyarrow as pa

from .config import settings

log = logging.getLogger(__name__)

TABLE = "chunks"
ANN_MIN_ROWS = 100_000


class VectorStore:
    def __init__(self, path: Path | None = None, dim: int | None = None):
        self.dim = dim or settings.embed_dim
        self.db = lancedb.connect(str(path or settings.lance_dir))
        self.schema = pa.schema([
            pa.field("chunk_id", pa.string()),
            pa.field("doc_id", pa.string()),
            pa.field("source_id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), self.dim)),
        ])
        if TABLE not in self.db.table_names():
            self.tbl = self.db.create_table(TABLE, schema=self.schema)
        else:
            self.tbl = self.db.open_table(TABLE)
        self._has_ann = False

    # ----------------------------------------------------------------- scrittura
    def upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.delete([r["chunk_id"] for r in rows])
        self.tbl.add(rows)

    def delete(self, chunk_ids: list[str]) -> None:
        """Cancella per chunk_id. Un errore qui NON va ignorato.

        Se la cancellazione fallisce e si aggiunge comunque, si ottengono
        vettori duplicati: lo stesso estratto compare tre volte nel contesto e
        satura i posti disponibili. Meglio fermarsi e farlo sapere.
        """
        if not chunk_ids:
            return
        for i in range(0, len(chunk_ids), 400):
            batch = chunk_ids[i:i + 400]
            quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in batch)
            try:
                self.tbl.delete(f"chunk_id IN ({quoted})")
            except Exception as exc:
                log.error("cancellazione vettori fallita (%s frammenti): %s", len(batch), exc)
                raise

    # ------------------------------------------------------------------ ricerca
    def refresh(self) -> None:
        """Rilegge il manifest: indispensabile dopo un'ingestione in altro processo."""
        try:
            self.tbl.checkout_latest()
        except Exception:
            try:
                self.tbl = self.db.open_table(TABLE)
            except Exception as exc:
                log.error("riapertura della tabella vettoriale fallita: %s", exc)
                raise

    def search(self, vector: list[float], k: int = 40, source_ids: list[str] | None = None):
        self.refresh()
        q = self.tbl.search(vector).metric("cosine").limit(k)
        if self._ann_present():
            # sonda piu' partizioni e ri-punteggia i candidati sui vettori
            # veri, non sulla loro versione quantizzata
            q = q.nprobes(40).refine_factor(10)
        if source_ids:
            vals = ", ".join("'" + s.replace("'", "''") + "'" for s in source_ids)
            q = q.where(f"source_id IN ({vals})", prefilter=True)
        return q.to_list()

    def _ann_present(self) -> bool:
        if self._has_ann:
            return True
        try:
            self._has_ann = bool(self.tbl.list_indices())
        except Exception:
            self._has_ann = False
        return self._has_ann

    # -------------------------------------------------------------- manutenzione
    def count(self) -> int:
        try:
            return self.tbl.count_rows()
        except Exception:
            return 0

    def optimize(self) -> None:
        """Da lanciare dopo un'ingestione massiva."""
        n = self.count()
        try:
            self.tbl.create_scalar_index("source_id", replace=True)
        except Exception as exc:
            log.debug("indice scalare su source_id non creato: %s", exc)

        if n >= ANN_MIN_ROWS:
            try:
                self.tbl.create_index(
                    metric="cosine",
                    num_partitions=max(1, min(512, int(n ** 0.5))),
                    num_sub_vectors=max(8, self.dim // 32),
                    replace=True,
                )
                self._has_ann = True
                log.info("indice ANN ricostruito su %s vettori", n)
            except Exception as exc:
                log.warning("create_index fallito (si procede con scansione lineare): %s", exc)
        else:
            log.info("%s vettori: sotto la soglia ANN, si resta sulla scansione esatta", n)

        for op in ("compact_files", "cleanup_old_versions"):
            try:
                getattr(self.tbl, op)()
            except Exception as exc:
                log.debug("%s non eseguito: %s", op, exc)
