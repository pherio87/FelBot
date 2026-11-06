"""Client HTTP educato: rate limit globale, rispetto di robots.txt,
richieste condizionali (ETag / If-Modified-Since) e ritentativi.

Il portale della Regione e' un servizio pubblico: il crawler si identifica,
si limita e non lo martella mai.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import settings

log = logging.getLogger(__name__)

ROBOTS_TTL = 24 * 3600          # robots.txt rivalutato una volta al giorno
DENY_ALL = object()             # sentinella: non interrogare questo host


class RateLimiter:
    """Token bucket semplice, condiviso da tutte le coroutine."""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.05)
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self.interval


@dataclass
class FetchResult:
    status: int
    content: bytes | None
    text: str | None
    headers: dict[str, str]
    url: str
    not_modified: bool = False

    @property
    def content_hash(self) -> str | None:
        if self.content is None:
            return None
        return hashlib.sha256(self.content).hexdigest()


class Fetcher:
    def __init__(self) -> None:
        self.limiter = RateLimiter(settings.requests_per_second)
        self.sem = asyncio.Semaphore(settings.max_concurrency)
        self._robots: dict[str, tuple[Any, float]] = {}
        self._robots_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "it-IT,it;q=0.9",
                # From e' riservata a un indirizzo email: si invia solo se c'e'
                **({"From": settings.contact_email} if settings.contact_email else {}),
            },
            timeout=httpx.Timeout(settings.http_timeout),
            follow_redirects=True,
            http2=True,
            limits=httpx.Limits(max_connections=settings.max_concurrency + 2),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    # ----------------------------------------------------------------- robots
    async def _robots_for(self, origin: str):
        """robots.txt con cache a scadenza e accesso serializzato.

        Un robots.txt che risponde 5xx o va in timeout viene trattato come
        DIVIETO, non come via libera: e' quanto prescrive la RFC 9309, ed e'
        anche il comportamento prudente se il portale e' in manutenzione.
        Un 404 invece significa davvero "nessuna restrizione".
        """
        async with self._robots_lock:
            entry = self._robots.get(origin)
            if entry and entry[1] > time.monotonic():
                return entry[0]

            rp: robotparser.RobotFileParser | None
            try:
                await self.limiter.wait()
                r = await self.client.get(urljoin(origin, "/robots.txt"))
                if r.status_code == 200:
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
                    delay = rp.crawl_delay(settings.user_agent) or rp.crawl_delay("*")
                    if delay:
                        # se il gestore chiede di rallentare, si rallenta
                        wanted = 1.0 / float(delay)
                        if wanted < settings.requests_per_second:
                            log.info("robots.txt di %s chiede Crawl-delay %ss: adeguo il ritmo",
                                     origin, delay)
                            self.limiter.interval = max(self.limiter.interval, float(delay))
                elif r.status_code == 404:
                    rp = None          # nessun robots.txt: nessun divieto
                else:
                    rp = DENY_ALL      # 5xx, 403, 429: prudenza
                    log.warning("robots.txt di %s ha risposto %s: mi astengo",
                                origin, r.status_code)
            except Exception as exc:
                rp = DENY_ALL
                log.warning("robots.txt di %s irraggiungibile (%s): mi astengo", origin, exc)

            self._robots[origin] = (rp, time.monotonic() + ROBOTS_TTL)
            return rp

    async def allowed(self, url: str) -> bool:
        if not settings.respect_robots:
            return True
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        rp = await self._robots_for(origin)
        if rp is None:
            return True
        if rp is DENY_ALL:
            return False
        return rp.can_fetch(settings.user_agent, url)

    # ------------------------------------------------------------------ fetch
    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=45),
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.HTTPStatusError, httpx.ReadTimeout)
        ),
    )
    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        async with self.sem:
            await self.limiter.wait()
            r = await self.client.request(method, url, **kw)
        # 429 / 5xx: vale la pena riprovare, il resto no
        if r.status_code == 429 or 500 <= r.status_code < 600:
            retry_after = r.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                await asyncio.sleep(min(int(retry_after), 120))
            r.raise_for_status()
        return r

    async def get_json(self, url: str, params: dict | None = None) -> dict:
        # anche l'enumerazione passa dal controllo robots.txt: escluderla
        # sarebbe incoerente con il resto e fragile se il portale un giorno
        # aggiungesse un divieto su /++api++
        if not await self.allowed(url):
            raise PermissionError(f"robots.txt vieta l'accesso a {url}")
        r = await self._request("GET", url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    async def get(
        self, url: str, etag: str | None = None, last_modified: str | None = None,
        binary: bool = False,
    ) -> FetchResult:
        if not await self.allowed(url):
            return FetchResult(999, None, None, {}, url)

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        r = await self._request("GET", url, headers=headers)
        h = {k.lower(): v for k, v in r.headers.items()}

        if r.status_code == 304:
            return FetchResult(304, None, None, h, str(r.url), not_modified=True)
        if r.status_code >= 400:
            return FetchResult(r.status_code, None, None, h, str(r.url))

        limit = settings.max_file_mb * 1024 * 1024
        content = r.content
        if len(content) > limit:
            # file enorme: lo si registra ma non lo si processa
            return FetchResult(413, None, None, h, str(r.url))

        return FetchResult(
            r.status_code,
            content,
            None if binary else r.text,
            h,
            str(r.url),
        )

    async def download_to(self, url: str, dest: Path, etag=None, last_modified=None) -> FetchResult:
        res = await self.get(url, etag=etag, last_modified=last_modified, binary=True)
        if res.content is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(res.content)
        return res
