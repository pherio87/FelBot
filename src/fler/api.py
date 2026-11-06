"""Servizio web: chat interna + API REST.

Il perimetro e' volutamente stretto: password obbligatoria, limiti di frequenza
su login e chat, corpi di richiesta limitati, documentazione automatica
disattivata. Non e' paranoia: il servizio tiene in ambiente una chiave API a
consumo, e la differenza fra un applicativo interno e una chiave in pubblico
sta esattamente in queste poche decine di righe.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .answer import Assistant
from .catalog import Catalog
from .config import settings
from .retrieve import Retriever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("fler.api")

# Una password vuota non deve poter significare "servizio aperto a internet":
# era un interruttore che disattivava l'autenticazione in silenzio, senza che
# l'interfaccia mostrasse alcun sintomo.
if settings.require_password and not settings.access_password:
    raise RuntimeError(
        "FLER_ACCESS_PASSWORD non impostata. Il servizio espone un indice documentale "
        "e consuma una chiave API a pagamento: impostare una password di almeno 12 "
        "caratteri in .env. Solo per uso in locale, non raggiungibile da internet, si "
        "puo' disattivare il controllo con FLER_REQUIRE_PASSWORD=false."
    )
if settings.access_password and len(settings.access_password) < 12:
    log.warning("la password d'accesso e' piu' corta di 12 caratteri: poco prudente")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Assistente documentale Formazione e Lavoro — Regione Emilia-Romagna",
    version="1.2.0",
    # la documentazione automatica consegnerebbe a un estraneo la mappa esatta
    # degli endpoint: non serve a nessuno qui
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_catalog: Catalog | None = None
_assistant: Assistant | None = None
WEB = Path(__file__).parent / "web"


def catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog(settings.catalog_path)
    return _catalog


def assistant() -> Assistant:
    global _assistant
    if _assistant is None:
        _assistant = Assistant(Retriever(catalog()))
    return _assistant


def allowed_sources() -> set[str]:
    from .pipeline import load_sources
    try:
        return {s["id"] for s in load_sources()}
    except Exception:
        return set()


# ------------------------------------------------------------------- accesso
COOKIE = "fler_session"
_sessions: dict[str, float] = {}


def _eq(a: str, b: str) -> bool:
    # il confronto va fatto su bytes: su stringhe con caratteri non ASCII
    # (e una password italiana li ha) compare_digest solleva TypeError, e il
    # login restituiva 500 invece di 401, rendendo il sistema inaccessibile
    # senza alcun messaggio comprensibile
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _prune() -> None:
    now = time.time()
    for tok in [t for t, exp in _sessions.items() if exp < now]:
        _sessions.pop(tok, None)


def require_auth(request: Request) -> None:
    if not settings.access_password:
        return
    _prune()
    tok = request.cookies.get(COOKIE)
    if tok and _sessions.get(tok, 0) > time.time():
        return
    # Nella 1.1 qui si accettava anche la password in un'intestazione
    # X-Access-Password, su qualunque rotta: /api/stato non ha limite di
    # frequenza, quindi il limite sul login si aggirava e la password si poteva
    # provare a raffica. L'unica porta d'ingresso e' /api/login.
    raise HTTPException(status_code=401, detail="Accesso non autorizzato")


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/login")
@limiter.limit(settings.rate_login)
def login(request: Request, body: LoginIn, response: Response):
    if not settings.access_password:
        return {"ok": True}
    if not _eq(body.password, settings.access_password):
        log.warning("tentativo di accesso fallito da %s", get_remote_address(request))
        raise HTTPException(status_code=401, detail="Password errata")
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + settings.session_days * 86400
    response.set_cookie(
        COOKIE, tok, httponly=True, samesite="lax",
        secure=settings.cookie_secure, max_age=settings.session_days * 86400,
    )
    return {"ok": True}


# --------------------------------------------------------------------- modelli
class Msg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=6000)


class ChatIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    # senza tetto, un corpo da centinaia di MB veniva interamente
    # materializzato da Pydantic e faceva esaurire la memoria del VPS
    history: list[Msg] = Field(default_factory=list, max_length=12)
    sources: list[str] | None = Field(default=None, max_length=10)


class SearchIn(BaseModel):
    q: str = Field(min_length=2, max_length=500)
    k: int = Field(default=15, ge=1, le=50)


class FeedbackIn(BaseModel):
    query_id: int = Field(ge=1)
    voto: str = Field(pattern="^(utile|inutile|errata)$")
    nota: str = Field(default="", max_length=1000)


def _sources_filter(requested: list[str] | None) -> list[str] | None:
    if not requested:
        return None
    ok = allowed_sources()
    return [s for s in requested if s in ok] or None


# ---------------------------------------------------------------------- rotte
@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/session")
def session(request: Request):
    if not settings.access_password:
        return {"auth": False, "ok": True}
    _prune()
    tok = request.cookies.get(COOKIE)
    return {"auth": True, "ok": bool(tok and _sessions.get(tok, 0) > time.time())}


@app.get("/api/stato")
def stato(_: None = Depends(require_auth)) -> dict[str, Any]:
    st = catalog().stats()
    # lo stato della ricerca vettoriale va esposto: un ramo semantico spento
    # non ha alcun sintomo visibile nell'uso normale
    try:
        from .store import VectorStore
        vs = VectorStore()
        vs.refresh()
        st["vettori"] = vs.count()
        st["ricerca_semantica"] = "attiva" if st["vettori"] else "indice vuoto"
    except Exception as exc:
        st["vettori"] = None
        st["ricerca_semantica"] = f"non disponibile: {exc}"[:200]
    st["modello"] = settings.llm_model
    st["fonti_attive"] = sorted(allowed_sources())
    return st


@app.post("/api/chat")
@limiter.limit(settings.rate_chat)
def chat(request: Request, body: ChatIn, _: None = Depends(require_auth)):
    a = assistant().ask(
        body.question, [m.model_dump() for m in body.history],
        source_ids=_sources_filter(body.sources),
    )
    qid = catalog().log_query(body.question, a.answered, len(a.sources), a.latency)
    return {
        "query_id": qid,
        "risposta": a.text,
        "fonti": [s.public() for s in a.sources],
        "ha_risposto": a.answered,
        "affidabilita": a.confidence,
        "avvisi": a.warnings,
        "motivo": a.reason,
        "domanda_usata": a.question_used,
        "secondi": round(a.latency, 2),
    }


@app.post("/api/chat/stream")
@limiter.limit(settings.rate_chat)
def chat_stream(request: Request, body: ChatIn, _: None = Depends(require_auth)):
    def gen():
        n_src = 0
        answered = True
        latency = 0.0
        try:
            for kind, payload in assistant().ask_stream(
                body.question, [m.model_dump() for m in body.history],
                source_ids=_sources_filter(body.sources),
            ):
                if kind == "sources":
                    n_src = len(payload)
                if kind == "done":
                    answered = payload.get("answered", True)
                    latency = payload.get("latency", 0.0)
                yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as exc:
            log.exception("errore nello streaming")
            yield ("event: done\ndata: "
                   + json.dumps({"answered": False, "confidence": "nessuna",
                                 "warnings": [f"errore interno: {exc}"[:200]], "cited": [],
                                 "latency": 0}, ensure_ascii=False) + "\n\n")
        try:
            qid = catalog().log_query(body.question, answered, n_src, latency)
            yield f"event: qid\ndata: {json.dumps({'query_id': qid})}\n\n"
        except Exception:
            pass

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/cerca")
@limiter.limit("60/hour")
def cerca(request: Request, body: SearchIn, _: None = Depends(require_auth)):
    """Ricerca pura, senza generazione: serve a capire cosa c'e' davvero a indice."""
    res = Retriever(catalog()).search(body.q, k=body.k)
    return {
        "riferimenti_riconosciuti": [str(r) for r in res.refs],
        "degrado": res.degraded,
        "risultati": [
            {
                "titolo": (h.data or {}).get("title"),
                "url": (h.data or {}).get("url"),
                "pagina": (h.data or {}).get("page"),
                "data": ((h.data or {}).get("site_effective")
                         or (h.data or {}).get("site_modified") or "")[:10],
                "punteggio": round(h.score, 4),
                "similarita": (h.data or {}).get("similarita"),
                "riferimento_esatto": h.exact_ref,
                "da_ocr": bool((h.data or {}).get("via_ocr")),
                "estratto": ((h.data or {}).get("text") or "")[:400],
            }
            for h in res.hits
        ],
    }


@app.post("/api/feedback")
@limiter.limit("60/hour")
def feedback(request: Request, body: FeedbackIn, _: None = Depends(require_auth)):
    with catalog().tx() as c:
        c.execute("UPDATE queries SET feedback=? WHERE id=?",
                  (f"{body.voto}: {body.nota}"[:500], body.query_id))
    return {"ok": True}


@app.get("/salute")
def salute():
    return JSONResponse({"ok": True})
