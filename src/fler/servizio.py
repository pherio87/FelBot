"""Servizio HTTP del container: il Worker lo sveglia e gli affida i lotti.

  GET  /ping    pronto (usato da Cloudflare Containers all'avvio)
  GET  /stato   sta lavorando? cosa ha fatto l'ultima volta?
  POST /lotto   {"modo": "prova"|"continua"|"notte", "limite": N}

Un solo lotto alla volta. La richiesta risponde subito: il lavoro prosegue in
background, perche' un lotto puo' durare piu' di quanto un'attivita'
programmata del Worker possa aspettare. Il Worker chiede /stato prima di
spegnere il container (crawler.ts, onActivityExpired).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from .lotto import esegui_lotto

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fler.servizio")

app = FastAPI(title="FelBot crawler", docs_url=None, redoc_url=None, openapi_url=None)

_stato: dict[str, Any] = {"occupato": False, "modo": None, "avviato_alle": None, "ultimo": None}
_compito: asyncio.Task | None = None


class Richiesta(BaseModel):
    modo: str = "continua"
    limite: int | None = None


@app.get("/ping")
async def ping() -> dict[str, bool]:
    return {"ok": True}


@app.get("/stato")
async def stato() -> dict[str, Any]:
    return _stato


async def _esegui(modo: str, limite: int | None) -> None:
    t0 = time.time()
    try:
        risultato = await esegui_lotto(modo, limite)
        _stato["ultimo"] = {"esito": "ok", **risultato}
        log.info("lotto concluso: %s", risultato)
    except Exception as exc:  # il servizio deve restare in piedi per il lotto successivo
        log.exception("lotto fallito")
        _stato["ultimo"] = {"esito": "errore", "errore": str(exc)[:500], "durata_secondi": int(time.time() - t0)}
    finally:
        _stato.update(occupato=False, modo=None)


@app.post("/lotto")
async def lotto(req: Richiesta) -> dict[str, Any]:
    global _compito
    if req.modo not in ("prova", "continua", "notte"):
        return {"avviato": False, "motivo": f"modo sconosciuto: {req.modo}"}
    if _stato["occupato"]:
        return {"avviato": False, "motivo": "lotto gia' in corso", **_stato}
    _stato.update(occupato=True, modo=req.modo, avviato_alle=time.time())
    _compito = asyncio.create_task(_esegui(req.modo, req.limite))
    return {"avviato": True, "modo": req.modo, "limite": req.limite}
