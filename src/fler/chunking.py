"""Suddivisione del testo in frammenti citabili.

Due accorgimenti che nella documentazione amministrativa contano piu' della
dimensione del chunk:

1. Si taglia preferibilmente sui confini logici dell'atto (Art. 3, Allegato B,
   punto 4.2), non a caso ogni N caratteri: cosi' una disposizione non si
   spezza a meta' e la citazione resta leggibile.
2. Ogni frammento porta in testa una intestazione con titolo del documento,
   sezione e data. Serve sia al recupero (una domanda su "accreditamento 2024"
   trova il frammento anche se quelle parole stanno solo nel titolo) sia al
   modello, che vede subito di che atto sta parlando.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .extract import Block

# Confini tipici degli atti amministrativi e normativi italiani
BOUNDARY = re.compile(
    r"^\s*("
    r"art(?:icolo)?\.?\s*\d+[\.\)]?"
    r"|allegato\s+[A-Z0-9IVX]+"
    r"|capo\s+[IVXLC]+"
    r"|titolo\s+[IVXLC]+"
    r"|sezione\s+[A-Z0-9IVX]+"
    r"|punto\s+\d+(?:\.\d+)*"
    r"|\d+(?:\.\d+){1,3}[\.\)]?\s+[A-ZÀÈÉÌÒÙ]"
    r"|premess[ao]"
    r"|visto\b|vista\b|considerato\b|delibera\b|determina\b|decreta\b"
    r")",
    re.I | re.M,
)


def _hard_split(text: str, limit: int) -> list[str]:
    """Spezza per forza un testo troppo lungo, preferendo i confini di frase.

    Serve come ultima difesa: un allegato PDF estratto senza righe vuote
    produceva un unico frammento da oltre 14.000 caratteri, che l'embedding
    riduce a una media indistinta e che da solo occupava un quarto del contesto.
    """
    out: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(". "), window.rfind(".\n"), window.rfind("; "))
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut < limit // 3:
            cut = limit
        out.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        out.append(rest)
    return [p for p in out if p]


def _split_paragraphs(text: str, limit: int) -> list[str]:
    parts = re.split(r"\n\s*\n", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= limit:
            out.append(p)
            continue
        # paragrafo enorme: prima si tenta sui confini normativi...
        cur = ""
        pieces: list[str] = []
        for piece in BOUNDARY.split(p):
            if not piece:
                continue
            if len(cur) + len(piece) > limit and cur:
                pieces.append(cur.strip())
                cur = piece
            else:
                cur += piece
        if cur.strip():
            pieces.append(cur.strip())
        # ...e se il testo non ha confini riconoscibili, si taglia comunque
        for piece in pieces:
            out.extend(_hard_split(piece, limit) if len(piece) > limit else [piece])
    return out


def _header(doc: dict[str, Any]) -> str:
    bits = [f"Documento: {doc.get('title') or doc.get('filename') or 'senza titolo'}"]
    if doc.get("portal_type") and doc["portal_type"] not in ("Document", "File"):
        bits.append(f"Tipo: {doc['portal_type']}")
    if doc.get("breadcrumb"):
        bits.append(f"Sezione: {doc['breadcrumb']}")
    date = doc.get("site_effective") or doc.get("site_modified")
    if date:
        bits.append(f"Data: {str(date)[:10]}")
    if doc.get("meta", {}).get("fonte"):
        bits.append(f"Fonte: {doc['meta']['fonte']}")
    return "[" + " | ".join(bits) + "]"


def chunk_document(
    doc: dict[str, Any],
    blocks: list[Block],
    target: int = 1800,
    overlap: int = 250,
) -> list[dict[str, Any]]:
    header = _header(doc)
    # L'intestazione consuma budget come il resto: non contarla portava i
    # frammenti al 17-24% di boilerplate identico per tutto il documento,
    # con l'effetto di avvicinare fra loro tutti i vettori dello stesso atto.
    head_len = len(header) + 2
    body_budget = max(target - head_len, 600)
    hard_limit = int(body_budget * 1.6)

    chunks: list[dict[str, Any]] = []
    cur: list[str] = []
    cur_len = 0
    cur_page: int | None = None
    cur_heading: str | None = None
    carry: list[str] = []  # testo troppo breve per stare da solo

    def flush(force: bool = False) -> None:
        nonlocal cur, cur_len, carry
        if not cur:
            return
        body = "\n\n".join(cur).strip()
        if len(body) < 40 and not force:
            # Non si butta: si porta avanti. Le rubriche d'articolo
            # ("Art. 12 Ore di stage") stanno spesso da sole e sono
            # esattamente il testo che discrimina una risposta; scartarle
            # rendeva l'articolo irrecuperabile da una ricerca per rubrica.
            carry = cur[:]
            cur, cur_len = [], 0
            return
        text = f"{header}\n\n{body}"
        cid = hashlib.sha1(f"{doc['doc_id']}|{len(chunks)}|{body[:200]}".encode()).hexdigest()[:24]
        chunks.append({
            "chunk_id": f"{doc['doc_id']}#{len(chunks)}:{cid[:8]}",
            "ord": len(chunks),
            "page": cur_page,
            "heading": cur_heading,
            "text": text,
        })
        if overlap > 0 and len(body) > overlap:
            tail = body[-overlap:]
            cut = tail.find(" ")
            cur = [tail[cut + 1:] if cut > 0 else tail]
            cur_len = len(cur[0])
        else:
            cur, cur_len = [], 0

    for b in blocks:
        if b.page is not None and b.page != cur_page and cur_len > body_budget * 0.6:
            flush()
        if b.page is not None:
            cur_page = b.page
        if b.heading:
            cur_heading = b.heading

        for para in _split_paragraphs(b.text, hard_limit):
            m = BOUNDARY.match(para)
            starts_section = bool(m)
            if starts_section:
                # la rubrica riconosciuta diventa l'intestazione del frammento,
                # cosi' resta associata al testo anche quando il blocco di
                # partenza non portava alcun heading
                cur_heading = para[:90].strip() if len(para) < 120 else m.group(1).strip()
            if cur_len and (
                cur_len + len(para) > body_budget
                or (starts_section and cur_len > body_budget * 0.45)
            ):
                flush()
            if carry:
                cur = carry + cur
                cur_len += sum(len(c) + 2 for c in carry)
                carry = []
            cur.append(para)
            cur_len += len(para) + 2

    if carry and not cur:
        cur = carry
        cur_len = sum(len(c) + 2 for c in carry)
        carry = []
    elif carry:
        cur = carry + cur
        carry = []
    # l'ultimo residuo va emesso anche se breve: e' testo del documento
    flush(force=True)
    return chunks
