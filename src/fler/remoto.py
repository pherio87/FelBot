"""Catalogo remoto: il crawler nel container parla con D1 attraverso il Worker.

Le richieste vanno a http://felbot.interno/... : un nome che non esiste su
internet. La classe Crawler del Worker (worker/src/crawler.ts) le intercetta e
le esegue con accesso diretto a D1, Vectorize e Workers AI. Nessun token,
nessuna rotta pubblica.

Ogni chiamata e' ritentata con attesa crescente: un errore transitorio del
Worker non deve far perdere un documento gia' scaricato ed estratto.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from tenacity import (AsyncRetrying, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

log = logging.getLogger(__name__)

BASE = os.environ.get("FLER_INTERNO_URL", "http://felbot.interno")


class ErroreRemoto(RuntimeError):
    pass


class CatalogoRemoto:
    def __init__(self, base: str = BASE, client: httpx.AsyncClient | None = None):
        self.base = base.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        async for tentativo in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=2, max=60),
            retry=retry_if_exception_type((httpx.TransportError, ErroreRemoto)),
            reraise=True,
        ):
            with tentativo:
                r = await self.client.post(f"{self.base}{path}", json=body or {})
                if r.status_code >= 500:
                    raise ErroreRemoto(f"{path}: HTTP {r.status_code} {r.text[:300]}")
                if r.status_code >= 400:
                    # errore della richiesta: ritentare non serve
                    raise ValueError(f"{path}: HTTP {r.status_code} {r.text[:300]}")
                return r.json()
        raise ErroreRemoto(path)  # non raggiungibile

    # ------------------------------------------------------------ giro
    async def stato(self) -> dict[str, Any]:
        return await self._post("/stato")

    async def apri_giro(self, kind: str, since: str | None) -> dict[str, Any]:
        return await self._post("/giro/apri", {"kind": kind, "since": since})

    async def aggiorna_giro(self, stats: dict[str, int] | None = None, errori: list[str] | None = None,
                            enumerazione_completa: bool | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"stats": stats or {}, "errori": errori or []}
        if enumerazione_completa is not None:
            body["enumerazione_completa"] = enumerazione_completa
        return await self._post("/giro/aggiorna", body)

    async def chiudi_giro(self) -> dict[str, Any]:
        return await self._post("/giro/chiudi")

    # ------------------------------------------------------------ enumerazione
    async def scoperti(self, docs: list[dict[str, Any]], revalidate_days: int) -> int:
        r = await self._post("/scoperti", {"docs": docs, "revalidate_days": revalidate_days})
        return int(r.get("da_scaricare", 0))

    async def candidati_rimozione(self, source_id: str, prima: float) -> tuple[int, list[dict[str, Any]]]:
        r = await self._post("/rimozioni/candidati", {"source_id": source_id, "prima": prima})
        return int(r.get("attivi", 0)), list(r.get("candidati", []))

    async def esito_rimozioni(self, ancora_presenti: list[str], ritirati: list[str]) -> dict[str, Any]:
        return await self._post("/rimozioni/esito",
                                {"ancora_presenti": ancora_presenti, "ritirati": ritirati})

    async def rivalida(self, days: int) -> int:
        return int((await self._post("/rivalida", {"days": days})).get("in_coda", 0))

    # ------------------------------------------------------------ lavorazione
    async def coda(self, limite: int, lease_s: int = 7200) -> list[dict[str, Any]]:
        return list((await self._post("/coda", {"limite": limite, "lease_s": lease_s})).get("documenti", []))

    async def esiti(self, esiti: list[dict[str, Any]]) -> None:
        for i in range(0, len(esiti), 100):
            await self._post("/esiti", {"esiti": esiti[i:i + 100]})

    async def frammenti(self, doc_id: str, chunks: list[dict[str, Any]], campi: dict[str, Any]) -> int:
        r = await self._post("/frammenti", {"doc_id": doc_id, "chunks": chunks, "campi": campi})
        return int(r.get("frammenti", 0))
