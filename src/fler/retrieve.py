"""Recupero ibrido: lessicale (BM25) + semantico (vettori), fusi con RRF.

Perche' ibrido. Nel linguaggio amministrativo meta' delle domande contengono
un identificativo esatto — "DGR 1298/2015", "PG.2017.333374", "L.R. 17/2005",
"allegato 3" — che gli embedding trattano come rumore numerico. Il BM25 quegli
identificativi li becca al primo colpo. Viceversa "chi puo' firmare gli
attestati di un corso IeFP" e' una domanda concettuale, dove il BM25 annaspa e
l'embedding va a bersaglio. Servono entrambi.

Tre correzioni rispetto all'impianto ingenuo, tutte misurate su corpus:

- Un riferimento d'atto e' una COPPIA (numero, anno), non un numero sciolto.
  Trattare ogni numero di tre cifre come estremo d'atto faceva si' che
  "aggiornati al 2024" cercasse "2024", che compare nel 17% dei frammenti, e
  che quei frammenti arrivassero in cima marcati come "riferimento esatto".

- I due numeri di un atto si cercano in prossimita' (NEAR), non in AND sparso
  sul frammento: "17" AND "2005" da L.R. 17/2005 selezionava il 3,8% del
  corpus, perche' "17" e' anche un articolo, un comma, un giorno.

- RRF_K vale 20, non 60. Su un pool di 80 candidati la costante da letteratura
  (tarata su collezioni TREC) appiattisce tutto: fra il primo e l'ottantesimo
  correvano meno di 2,5 volte, e un risultato presente in entrambe le liste a
  qualunque rango batteva il primo assoluto di una sola lista — cioe' proprio
  il match BM25 esatto sul numero di protocollo.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .catalog import Catalog
from .config import settings

log = logging.getLogger(__name__)

# Costante della fusione RRF, calibrata sulla dimensione reale del pool
RRF_K = 20.0
# Peso dei riscontri su riferimento d'atto validato: sopra gli altri, ma non
# tale da schiacciarli (a 2.0 anche l'ultimo ref-hit superava ogni altro).
REF_WEIGHT = 1.4

ANNO = re.compile(r"^(19|20)\d{2}$")


@dataclass(frozen=True)
class ActRef:
    """Estremi di un atto: numero e anno insieme, mai separati."""
    kind: str
    num: str
    year: str

    @property
    def tokens(self) -> tuple[str, str]:
        return self.num, self.year

    def __str__(self) -> str:
        return f"{self.kind} {self.num}/{self.year}"


# Riferimenti normativi e protocollari riconosciuti
REF_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("DGR", re.compile(r"\b(?:d\.?\s?g\.?\s?r\.?|delibera(?:zione)?(?:\s+di\s+giunta)?)\s*"
                       r"(?:n\.?\s*)?(\d{1,5})\s*(?:/|\s+del\s+|\s+dell['’]\s*)\s*(\d{2,4})", re.I)),
    ("DD", re.compile(r"\b(?:d\.?\s?d\.?|determina(?:zione)?(?:\s+dirigenziale)?)\s*"
                      r"(?:n\.?\s*)?(\d{1,6})\s*(?:/|\s+del\s+)\s*(\d{2,4})", re.I)),
    ("LR", re.compile(r"\b(?:l\.?\s?r\.?|legge\s+regionale)\s*"
                      r"(?:n\.?\s*)?(\d{1,3})\s*(?:/|\s+del\s+)\s*(\d{2,4})", re.I)),
    ("PG", re.compile(r"\bPG\.?\s*(\d{4})\.?(\d{4,8})\b", re.I)),
]
# protocolli senza anno: "prot. n. 1096197"
PROT = re.compile(r"\b(?:prot(?:ocollo)?\.?|nota)\s*(?:n\.?\s*)?(\d{5,10})\b", re.I)
# numero d'atto nudo ma introdotto da un marcatore: "la delibera 1298"
BARE_ACT = re.compile(
    r"\b(?:dgr|dd|l\.?r\.?|delibera(?:zione)?|determina(?:zione)?|atto|avviso|bando)\s*"
    r"(?:n\.?\s*)?(\d{3,7})\b", re.I)

# Unita' di misura: un numero seguito da queste NON e' un estremo d'atto
UNITA = re.compile(r"\b\d{2,7}\s*(?:ore|or[ae]|euro|€|%|allievi|giorni|mesi|posti|crediti|cfu)\b", re.I)

STOP = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "a", "da", "in",
    "con", "su", "per", "tra", "fra", "del", "dello", "della", "dei", "degli",
    "delle", "al", "allo", "alla", "ai", "agli", "alle", "dal", "dalla", "dalle",
    "dagli", "nel", "nella", "nelle", "negli", "nei", "sul", "sulla", "sulle",
    "e", "ed", "o", "oppure", "che", "chi", "cosa", "come", "quando", "dove",
    "quale", "quali", "quanto", "quanta", "quante", "quanti", "se", "non", "piu",
    "si", "sono", "essere", "avere", "posso", "puo", "possono", "devo", "deve",
    "devono", "va", "vanno", "ci", "vi", "ne", "mi", "ti", "questo", "questa",
    "questi", "queste", "qual", "anche", "ogni", "presso", "circa", "solo",
    "sempre", "mai", "gia", "ancora", "poi", "cioe", "ovvero", "perche",
    "quindi", "mentre", "invece", "pero", "dunque", "affinche", "nonche",
    "ovvero", "essa", "esso", "loro", "suo", "sua", "mio", "mia", "nostro",
    "nostra", "vostro", "vostra", "tutto", "tutti", "tutte", "tutta",
}
# Nota: il confronto avviene sulla forma senza accenti (vedi _senza_accenti),
# quindi "perche'", "perché" e "perche" cadono tutti nella stessa voce. Scrivere
# la lista senza accenti e confrontare la forma accentata era il difetto: "più"
# e "può" sopravvivevano come termini di ricerca.


def _senza_accenti(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


@dataclass
class Hit:
    chunk_id: str
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None
    exact_ref: bool = False
    neighbour: bool = False
    data: dict[str, Any] | None = None


@dataclass
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    refs: list[ActRef] = field(default_factory=list)
    semantic_ok: bool = True
    lexical_ok: bool = True
    degraded: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- riferimenti
def extract_refs(q: str) -> list[ActRef]:
    """Estrae gli estremi degli atti citati, come coppie numero/anno.

    Non restituisce numeri sciolti: un anno o una durata in ore non sono
    riferimenti, e trattarli come tali portava in cima frammenti a caso.
    """
    refs: list[ActRef] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, num: str, year: str) -> None:
        if len(year) == 2:
            year = ("19" if int(year) > 50 else "20") + year
        key = (num.lstrip("0") or num, year)
        if key not in seen:
            seen.add(key)
            refs.append(ActRef(kind, num, year))

    for kind, pat in REF_PATTERNS:
        for m in pat.finditer(q):
            a, b = m.group(1), m.group(2)
            if kind == "PG":
                add(kind, b, a)  # PG.<anno>.<numero>
            else:
                add(kind, a, b)

    for m in PROT.finditer(q):
        n = m.group(1)
        if (n, "") not in seen:
            seen.add((n, ""))
            refs.append(ActRef("PROT", n, ""))

    if not refs:
        # numero d'atto senza anno ma introdotto da un marcatore esplicito
        for m in BARE_ACT.finditer(q):
            n = m.group(1)
            if ANNO.match(n) or UNITA.search(m.group(0)):
                continue
            if (n, "") not in seen:
                seen.add((n, ""))
                refs.append(ActRef("ATTO", n, ""))

    return refs[:6]


def fts_query(q: str) -> str:
    """Costruisce una query FTS5 sicura: niente sintassi iniettabile dall'utente.

    I numeri e le sigle brevi si conservano: scartare i token di due caratteri
    cancellava il "17" di L.R. 17/2005, il "pg" di PG.2017.333374 e il "3" di
    "allegato 3" — cioe' proprio cio' per cui esiste il ramo lessicale.
    """
    tokens = re.findall(r"[0-9A-Za-zÀ-ÿ]+", q.lower())
    terms: list[str] = []
    for t in tokens:
        base = _senza_accenti(t)
        if base in STOP:
            continue
        if len(t) > 2 or t.isdigit() or base in {"pg", "dd", "lr", "ue", "fp"}:
            terms.append(t)
    if not terms:
        terms = [t for t in tokens if len(t) > 1]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms[:24])


def ref_query(refs: list[ActRef]) -> str:
    """Query mirata sugli estremi: prossimita' dentro l'atto, OR fra atti diversi.

    L'AND globale su tutti i numeri estratti azzerava il risultato quando la
    domanda citava due atti insieme, e perdeva precisione quando il numero era
    di due cifre.
    """
    parts: list[str] = []
    for r in refs:
        if r.year:
            parts.append(f'NEAR("{r.num}" "{r.year}", 8)')
        else:
            parts.append(f'"{r.num}"')
    return " OR ".join(parts)


class Retriever:
    def __init__(self, catalog: Catalog | None = None):
        self.cat = catalog or Catalog(settings.catalog_path)
        self._embedder = None
        self._store = None

    @property
    def embedder(self):
        if self._embedder is None:
            from .embed import Embedder
            self._embedder = Embedder()
        return self._embedder

    @property
    def store(self):
        if self._store is None:
            from .store import VectorStore
            self._store = VectorStore()
        return self._store

    # ------------------------------------------------------------- lessicale
    def _fts(self, query: str, k: int, source_ids: list[str] | None,
             weight_doc_title: bool = False) -> list[tuple[str, float]]:
        if not query:
            return []
        # Il filtro per fonte va applicato anche qui: prima valeva solo sul
        # ramo semantico, quindi una ricerca circoscritta a una fonte
        # restituiva comunque frammenti di tutte le altre.
        if source_ids:
            ph = ",".join("?" for _ in source_ids)
            sql = (
                f"SELECT c.chunk_id AS chunk_id, bm25(chunks_fts) AS s "
                f"FROM chunks_fts f JOIN chunks c ON c.fts_rowid = f.rowid "
                f"JOIN documents d ON d.doc_id = c.doc_id "
                f"WHERE chunks_fts MATCH ? AND d.gone=0 AND d.source_id IN ({ph}) "
                f"ORDER BY s LIMIT ?"
            )
            args: list[Any] = [query, *source_ids, k]
        else:
            sql = (
                "SELECT c.chunk_id AS chunk_id, bm25(chunks_fts) AS s "
                "FROM chunks_fts f JOIN chunks c ON c.fts_rowid = f.rowid "
                "JOIN documents d ON d.doc_id = c.doc_id "
                "WHERE chunks_fts MATCH ? AND d.gone=0 ORDER BY s LIMIT ?"
            )
            args = [query, k]
        try:
            rows = self.cat.conn.execute(sql, args).fetchall()
        except Exception as exc:
            log.warning("ricerca lessicale fallita (%s): %s", query[:80], exc)
            raise
        return [(r["chunk_id"], -float(r["s"])) for r in rows]

    def lexical(self, question: str, k: int, source_ids=None) -> list[tuple[str, float]]:
        return self._fts(fts_query(question), k, source_ids)

    def by_reference(self, refs: list[ActRef], k: int = 25, source_ids=None):
        if not refs:
            return []
        return self._fts(ref_query(refs), k, source_ids)

    # -------------------------------------------------------------- semantico
    def semantic(self, question: str, k: int, source_ids: list[str] | None = None):
        vec = self.embedder.encode_query(question)
        rows = self.store.search(vec, k=k, source_ids=source_ids)
        return [(r["chunk_id"], 1.0 - float(r.get("_distance", 1.0))) for r in rows]

    # ------------------------------------------------------------------ fusione
    def search(
        self, question: str, k: int | None = None, source_ids: list[str] | None = None
    ) -> SearchResult:
        k = k or settings.top_k
        pool = max(k * 2, 60)
        res = SearchResult()
        res.refs = extract_refs(question)

        try:
            lex = self.lexical(question, pool, source_ids)
        except Exception:
            lex, res.lexical_ok = [], False
            res.degraded.append("ricerca testuale non disponibile")

        try:
            ref_hits = self.by_reference(res.refs, 25, source_ids) if res.refs else []
        except Exception:
            ref_hits = []

        try:
            sem = self.semantic(question, pool, source_ids)
        except Exception as exc:
            sem, res.semantic_ok = [], False
            # Un ramo semantico spento in silenzio degradava il sistema a solo
            # BM25 per settimane senza che nulla lo segnalasse.
            log.error("ricerca vettoriale non disponibile: %s", exc)
            res.degraded.append(
                "ricerca semantica non disponibile: risultati basati solo sulla "
                "corrispondenza testuale"
            )

        scores: dict[str, float] = {}
        lex_rank: dict[str, int] = {}
        vec_rank: dict[str, int] = {}
        sem_sim: dict[str, float] = {}
        exact: set[str] = set()

        for i, (cid, _) in enumerate(lex):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + i + 1)
            lex_rank[cid] = i
        for i, (cid, sim) in enumerate(sem):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + i + 1)
            vec_rank[cid] = i
            sem_sim[cid] = sim
        for i, (cid, _) in enumerate(ref_hits):
            scores[cid] = scores.get(cid, 0.0) + REF_WEIGHT / (RRF_K + i + 1)
            exact.add(cid)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[: k * 2]
        hydrated = self.cat.hydrate([c for c, _ in ranked])

        hits: list[Hit] = []
        for cid, sc in ranked:
            data = hydrated.get(cid)
            if not data:
                continue
            data["similarita"] = sem_sim.get(cid)
            hits.append(Hit(
                chunk_id=cid, score=sc * _temporal_factor(data),
                lexical_rank=lex_rank.get(cid), vector_rank=vec_rank.get(cid),
                exact_ref=cid in exact, data=data,
            ))
        hits.sort(key=lambda h: h.score, reverse=True)
        res.hits = hits[:k]
        return res

    # ------------------------------------------------------- selezione contesto
    def select_context(self, result: SearchResult, n: int | None = None) -> list[Hit]:
        """Sceglie i frammenti da passare al modello.

        Tetto adattivo per documento: di norma 3, ma se un solo atto domina il
        recupero (un manuale di rendicontazione che contiene tutta la risposta)
        gli si concede fino a meta' del contesto, invece di diluire con dieci
        documenti che non c'entrano. I frammenti scelti vengono poi estesi ai
        contigui, perche' tre pezzi sconnessi di una procedura sono peggio di
        due pezzi consecutivi.
        """
        n = n or settings.context_chunks
        hits = result.hits
        if not hits:
            return []

        totale = sum(h.score for h in hits) or 1.0
        per_doc_score: dict[str, float] = {}
        for h in hits:
            if h.data:
                per_doc_score[h.data["doc_id"]] = per_doc_score.get(h.data["doc_id"], 0.0) + h.score
        dominante = max(per_doc_score, key=lambda d: per_doc_score[d])
        cap_dominante = max(3, n // 2) if per_doc_score[dominante] / totale > 0.4 else 3

        per_doc: dict[str, int] = {}
        chosen: list[Hit] = []
        for h in hits:
            if not h.data:
                continue
            d = h.data["doc_id"]
            cap = cap_dominante if d == dominante else 3
            if per_doc.get(d, 0) >= cap:
                continue
            per_doc[d] = per_doc.get(d, 0) + 1
            chosen.append(h)
            if len(chosen) >= n:
                break

        return self._expand(chosen, n)

    def _expand(self, chosen: list[Hit], n: int) -> list[Hit]:
        """Aggiunge i frammenti contigui a quelli scelti, se resta spazio."""
        have = {h.chunk_id for h in chosen}
        want: list[tuple[Hit, str]] = []
        for h in chosen:
            if not h.data:
                continue
            for cid in self.cat.neighbours(h.data["doc_id"], h.data.get("ord", 0)):
                if cid not in have:
                    want.append((h, cid))
        if not want:
            return chosen

        extra = self.cat.hydrate([cid for _, cid in want[: max(0, n - len(chosen)) * 2]])
        out = list(chosen)
        for parent, cid in want:
            if len(out) >= n + 4:
                break
            d = extra.get(cid)
            if not d or cid in have:
                continue
            have.add(cid)
            out.append(Hit(chunk_id=cid, score=parent.score * 0.5,
                           neighbour=True, data=d))
        # ordina per documento e posizione: il modello legge una sequenza
        out.sort(key=lambda h: ((h.data or {}).get("doc_id", ""), (h.data or {}).get("ord", 0)))
        return out

    # ---------------------------------------------------------------- fiducia
    def is_confident(self, result: SearchResult) -> tuple[bool, str]:
        """Il recupero e' abbastanza solido da tentare una risposta?

        Le condizioni sono CONGIUNTIVE, non alternative. Nella versione
        precedente bastava che tre dei primi cinque risultati avessero un rango
        lessicale — cosa che accade praticamente sempre, perche' la query
        lessicale e' un OR di tutti i termini su 21.000 documenti. Il risultato
        era che il sistema non si asteneva mai e consegnava al modello 14
        estratti non pertinenti con l'ordine di rispondere.
        """
        hits = result.hits
        if not hits:
            return False, "nessun frammento trovato"
        if len(hits) < 2:
            return False, "un solo frammento recuperato"

        sims = [(h.data or {}).get("similarita") for h in hits[:8]]
        sims = [s for s in sims if s is not None]
        best = max(sims) if sims else None
        sopra_soglia = sum(1 for s in sims if s >= settings.min_relevance)

        # Un riferimento d'atto validato vale da solo: e' una corrispondenza
        # esatta su numero e anno in prossimita', non un numero a caso.
        if any(h.exact_ref for h in hits[:5]) and result.refs:
            return True, f"riscontro esatto su {result.refs[0]}"

        if not result.semantic_ok:
            # senza il ramo semantico si risponde solo con un forte accordo
            # lessicale, e la risposta portera' comunque l'avviso di degrado
            forti = sum(1 for h in hits[:5] if h.lexical_rank is not None and h.lexical_rank < 10)
            return (forti >= 3), "solo corrispondenza testuale"

        if best is None:
            return False, "nessun punteggio di similarita' disponibile"
        if best < settings.min_relevance:
            return False, f"pertinenza massima {best:.2f} sotto la soglia {settings.min_relevance}"
        # quorum: un solo frammento fortunato non basta
        if sopra_soglia < 2:
            return False, "un solo frammento sopra la soglia di pertinenza"
        return True, f"pertinenza {best:.2f} su {sopra_soglia} frammenti"


def _temporal_factor(d: dict[str, Any]) -> float:
    """Correttivo leggero su stato e attualita' del documento.

    Non decide nulla al posto dell'utente — la vigenza resta una verifica
    umana — ma evita che, a parita' di pertinenza, una delibera superata da
    dieci anni prevalga a caso sulla sua versione aggiornata.
    """
    f = 1.0
    oggi = date.today()

    exp = _as_date(d.get("expires"))
    if exp and exp < oggi:
        f *= 0.75                        # bando chiuso: retrocede, non sparisce
    if (d.get("review_state") or "published") not in ("published", "", None):
        f *= 0.85

    dt = _as_date(d.get("site_effective")) or _as_date(d.get("site_modified"))
    if dt:
        anni = (oggi - dt).days / 365.25
        if anni <= 3:
            f *= 1.12
        elif anni > 12:
            f *= 0.92
    return f


def _as_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)[:19].replace("Z", "")).date()
    except Exception:
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
        except Exception:
            return None
