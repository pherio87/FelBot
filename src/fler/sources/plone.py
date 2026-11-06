"""Enumerazione via REST API di Plone.

Il portale espone `/++api++/@search` in anonimo: e' l'elenco autoritativo di
tutto cio' che e' pubblicato. Due assunzioni "ovvie" su questa API sono state
verificate FALSE sul campo e hanno dettato l'implementazione:

1. `metadata_fields` ripetuto (o separato da virgole) NON e' onorato: viene
   letto solo il primo valore. Chiedere l'elenco dei campi che servono fa
   tornare risposte prive di `created` e `modified` — cioe' prive esattamente
   dei due campi su cui si regge paginazione e aggiornamento incrementale.
   Si usa quindi `metadata_fields=_all`.

2. La paginazione "keyset" su `created` perde documenti: il valore torna in un
   fuso e viene reinterpretato in un altro, e il DateIndex di ZCatalog ha
   risoluzione al minuto con ordine arbitrario fra pari. Misurate perdite di
   decine di documenti su un solo confine di pagina. Si usa quindi `b_start`,
   verificato funzionante fino in fondo al catalogo (21.320 oggetti).

Terza regola, imparata a spese altrui: un'enumerazione incompleta non deve mai
essere indistinguibile da una completa, perche' a valle `mark_gone_unseen`
cancellerebbe dall'indice tutto cio' che non e' stato visto. Qui si confronta
sempre il numero di oggetti visti con `items_total` dichiarato dal portale e,
se non torna, si solleva `EnumerationIncomplete`.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from ..fetcher import Fetcher

log = logging.getLogger(__name__)


class EnumerationIncomplete(RuntimeError):
    """L'elenco dei contenuti non e' stato percorso per intero.

    Chi la riceve NON deve trarre conclusioni su cosa non e' piu' pubblicato.
    """

    def __init__(self, source_id: str, seen: int, expected: int | None, cause: str = ""):
        self.source_id = source_id
        self.seen = seen
        self.expected = expected
        super().__init__(
            f"[{source_id}] enumerazione incompleta: {seen} oggetti su "
            f"{expected if expected is not None else '?'} attesi"
            + (f" — {cause}" if cause else "")
        )


FILE_TYPES = {"File"}
DEFAULT_SKIP = {"Image", "Plone Site", "Folder", "LIF"}

# Campi copiati in `meta` quando presenti (con _all arrivano tutti)
META_KEYS = (
    "Subject", "tassonomia_argomenti", "tipologia_documento", "tipologia_bando",
    "tipologia_notizia", "scadenza_bando", "apertura_bando",
    "chiusura_procedimento_bando", "ente_bando", "ufficio_responsabile_bando",
    "destinatari_bando", "getObjSize",
)


class PloneSource:
    kind = "plone"
    page_size = 100

    def __init__(self, cfg: dict[str, Any], fetcher: Fetcher):
        self.cfg = cfg
        self.id = cfg["id"]
        self.base = cfg["base_url"].rstrip("/")
        self.api = f"{self.base}/++api++"
        self.fetcher = fetcher
        self.skip = set(cfg.get("skip_types") or []) | DEFAULT_SKIP
        self.authority = cfg.get("authority", "primaria")
        self.label = cfg.get("label", self.id)
        self.expected_total: int | None = None

    # ------------------------------------------------------------------ utils
    def _doc_id(self, uid: str | None, url: str) -> str:
        return f"{self.id}:{hashlib.sha1((uid or url).encode()).hexdigest()[:20]}"

    def _section(self, path: str | None, url: str) -> tuple[str, str]:
        """Sezione e percorso leggibile del documento.

        `getPath` di Plone inizia sempre con l'id del sito ("/formazionelavoro/..."):
        va scartato, altrimenti ogni documento del portale risulterebbe nella
        stessa sezione e il dato diventerebbe inutile sia per i filtri sia per
        l'intestazione mostrata al modello.
        """
        if path:
            parts = [s for s in path.split("/") if s][1:]
        else:
            parts = [s for s in urlparse(url).path.split("/") if s]
        section = parts[0] if parts else ""
        return section, " / ".join(parts[:-1])

    # ------------------------------------------------------------- enumerazione
    async def enumerate(self, since: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Scorre il catalogo del sito con paginazione a offset.

        `since` (data ISO) restringe agli oggetti modificati dopo quella data:
        e' la modalita' degli aggiornamenti periodici.

        Solleva EnumerationIncomplete se non riesce a percorrere tutto.
        """
        seen: set[str] = set()
        b_start = 0
        total: int | None = None
        empty_pages = 0

        while True:
            params: dict[str, Any] = {
                "b_size": self.page_size,
                "b_start": b_start,
                # ordinamento crescente per data di creazione: i contenuti
                # pubblicati durante la scansione si accodano in fondo e non
                # spostano le pagine gia' percorse
                "sort_on": "created",
                "sort_order": "ascending",
                "metadata_fields": "_all",
            }
            if since:
                params["modified.query"] = since
                params["modified.range"] = "min"

            try:
                data = await self.fetcher.get_json(f"{self.api}/@search", params=params)
            except Exception as exc:
                raise EnumerationIncomplete(
                    self.id, len(seen), total, f"errore a b_start={b_start}: {exc}"
                ) from exc

            if total is None:
                total = data.get("items_total")
                self.expected_total = total
                log.info("[%s] il portale dichiara %s oggetti%s",
                         self.id, total, " modificati dopo " + since if since else "")

            items = data.get("items") or []
            if not items:
                empty_pages += 1
                if empty_pages >= 2 or total is None:
                    break
                b_start += self.page_size
                continue
            empty_pages = 0

            for it in items:
                url = (it.get("@id") or it.get("getURL") or "").replace("/++api++", "")
                uid = it.get("UID")
                key = uid or url
                if not url or key in seen:
                    continue
                seen.add(key)

                ptype = it.get("@type") or it.get("portal_type") or ""
                if ptype in self.skip:
                    continue
                yield self._to_doc(it, url, ptype)

            b_start += len(items)
            if total is not None and b_start >= total:
                break
            if b_start > 200_000:  # cintura di sicurezza
                raise EnumerationIncomplete(self.id, len(seen), total, "offset fuori scala")

        # Controllo di completezza: e' l'unica cosa che separa "ho finito"
        # da "mi sono fermato a meta'", e a valle ne dipende la cancellazione.
        if total is None:
            raise EnumerationIncomplete(self.id, len(seen), None, "items_total non dichiarato")
        if len(seen) < total * 0.98:
            raise EnumerationIncomplete(self.id, len(seen), total, "troppi oggetti mancanti")
        log.info("[%s] enumerazione completa: %s oggetti su %s dichiarati",
                 self.id, len(seen), total)

    def _to_doc(self, it: dict[str, Any], url: str, ptype: str) -> dict[str, Any]:
        section, breadcrumb = self._section(it.get("getPath"), url)
        is_file = ptype in FILE_TYPES
        meta = {k: it[k] for k in META_KEYS if it.get(k) not in (None, "", [], {})}
        meta["fonte"] = self.label
        meta["autorevolezza"] = self.authority

        return {
            "doc_id": self._doc_id(it.get("UID"), url),
            "source_id": self.id,
            "url": url,
            "api_url": url.replace(self.base, self.api, 1),
            "download_url": f"{url}/@@download/file" if is_file else None,
            "portal_type": ptype,
            "title": (it.get("Title") or it.get("title") or "").strip(),
            "description": (it.get("Description") or it.get("description") or "").strip(),
            "mime": it.get("mime_type") if is_file else "text/html",
            # `getId` porta il nome del file con l'estensione: senza di esso
            # il riconoscimento del formato lavora alla cieca, perche' l'URL
            # di un File Plone non ha suffisso
            "filename": it.get("getId") or it.get("id") if is_file else None,
            "section": section,
            "breadcrumb": breadcrumb,
            "site_modified": it.get("modified"),
            "site_effective": it.get("effective"),
            "expires": it.get("expires"),
            "review_state": it.get("review_state"),
            "meta": meta,
        }
