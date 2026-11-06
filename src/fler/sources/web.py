"""Sorgenti generiche per i portali regionali che non espongono una API:
una che segue le sitemap XML, una che fa crawling HTML entro prefissi consentiti.

Servono per le fonti secondarie (BURERT, Agenzia Lavoro, SIFER pubblico).
"""
from __future__ import annotations

import gzip
import hashlib
import logging
import re
from collections import deque
from typing import Any, AsyncIterator
from urllib.parse import urldefrag, urljoin, urlparse

from lxml import etree
from selectolax.parser import HTMLParser

from ..fetcher import Fetcher

log = logging.getLogger(__name__)

DOC_EXT = re.compile(
    r"\.(pdf|docx?|odt|ods|odp|xlsx?|pptx?|rtf|txt|csv)(\?|#|$)", re.I
)
SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|zip|rar|7z|mp4|mp3|avi|mov|woff2?|ttf|eot)(\?|#|$)",
    re.I,
)


def _mime_for(url: str) -> str:
    m = DOC_EXT.search(url)
    if not m:
        return "text/html"
    return {
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "odt": "application/vnd.oasis.opendocument.text",
        "ods": "application/vnd.oasis.opendocument.spreadsheet",
        "odp": "application/vnd.oasis.opendocument.presentation",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt": "application/vnd.ms-powerpoint",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "rtf": "application/rtf",
        "txt": "text/plain",
        "csv": "text/csv",
    }.get(m.group(1).lower(), "application/octet-stream")


class _WebBase:
    def __init__(self, cfg: dict[str, Any], fetcher: Fetcher):
        self.cfg = cfg
        self.id = cfg["id"]
        self.base = cfg["base_url"].rstrip("/")
        self.fetcher = fetcher
        self.label = cfg.get("label", self.id)
        self.authority = cfg.get("authority", "secondaria")
        self.allow = cfg.get("allow_prefixes") or []
        self.host = urlparse(self.base).netloc

    def _ok(self, url: str) -> bool:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or u.netloc != self.host:
            return False
        if SKIP_EXT.search(url):
            return False
        if self.allow and not any(u.path.startswith(p) for p in self.allow):
            return False
        return True

    def _doc(self, url: str, title: str = "", lastmod: str | None = None) -> dict[str, Any]:
        parts = [s for s in urlparse(url).path.split("/") if s]
        mime = _mime_for(url)
        return {
            "doc_id": f"{self.id}:{hashlib.sha1(url.encode()).hexdigest()[:20]}",
            "source_id": self.id,
            "url": url,
            "api_url": None,
            "download_url": url if mime != "text/html" else None,
            "portal_type": "File" if mime != "text/html" else "Document",
            "title": title,
            "description": "",
            "mime": mime,
            "filename": parts[-1] if parts else None,
            "section": parts[0] if parts else "",
            "breadcrumb": " / ".join(parts[:-1]),
            "site_modified": lastmod,
            "site_effective": None,
            "expires": None,
            "review_state": None,
            "meta": {"fonte": self.label, "autorevolezza": self.authority},
        }


class SitemapSource(_WebBase):
    kind = "sitemap"

    async def enumerate(self, since: str | None = None) -> AsyncIterator[dict[str, Any]]:
        queue = deque(self.cfg.get("sitemaps") or [
            f"{self.base}/sitemap.xml",
            f"{self.base}/sitemap_index.xml",
            f"{self.base}/sitemap.xml.gz",
        ])
        seen_maps: set[str] = set()
        seen_urls: set[str] = set()

        while queue:
            sm = queue.popleft()
            if sm in seen_maps:
                continue
            seen_maps.add(sm)
            try:
                res = await self.fetcher.get(sm, binary=True)
            except Exception as exc:
                log.warning("[%s] sitemap %s non raggiungibile: %s", self.id, sm, exc)
                continue
            if res.status != 200 or not res.content:
                continue
            raw = res.content
            if sm.endswith(".gz") or raw[:2] == b"\x1f\x8b":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    continue
            try:
                root = etree.fromstring(raw)
            except Exception:
                continue

            ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for node in root.findall(".//s:sitemap/s:loc", ns):
                if node.text:
                    queue.append(node.text.strip())
            for node in root.findall(".//s:url", ns):
                loc = node.findtext("s:loc", namespaces=ns)
                if not loc:
                    continue
                loc = loc.strip()
                lastmod = node.findtext("s:lastmod", namespaces=ns)
                if loc in seen_urls or not self._ok(loc):
                    continue
                if since and lastmod and lastmod < since:
                    continue
                seen_urls.add(loc)
                yield self._doc(loc, lastmod=lastmod)


class CrawlSource(_WebBase):
    kind = "crawl"
    max_pages = 20000

    async def enumerate(self, since: str | None = None) -> AsyncIterator[dict[str, Any]]:
        start = self.cfg.get("start_urls") or [self.base]
        queue: deque[str] = deque(start)
        seen: set[str] = set(start)
        n = 0

        while queue and n < self.max_pages:
            url = queue.popleft()
            n += 1
            mime = _mime_for(url)
            if mime != "text/html":
                yield self._doc(url)
                continue

            try:
                res = await self.fetcher.get(url)
            except Exception:
                continue
            if res.status != 200 or not res.text:
                continue

            tree = HTMLParser(res.text)
            t = tree.css_first("title")
            yield self._doc(url, title=t.text(strip=True) if t else "")

            for a in tree.css("a[href]"):
                href = a.attributes.get("href") or ""
                if href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                nxt = urldefrag(urljoin(url, href))[0]
                if nxt not in seen and self._ok(nxt):
                    seen.add(nxt)
                    queue.append(nxt)
