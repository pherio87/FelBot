"""Estrazione e normalizzazione dei VALORI da confrontare con le fonti.

Separato da `answer.py` perche' e' la parte piu' delicata del sistema e merita
test propri. La revisione della 1.1 ha mostrato che il controllo dei valori si
aggirava in cinque modi, tutti riprodotti in `tests/test_offline.py`:

1. un numero giusto attribuito all'estratto sbagliato passava, perche' bastava
   che comparisse in QUALUNQUE estratto citato nella risposta;
2. un numero suggerito dalla domanda ("e' vero che sono 80.000 euro?") passava
   sempre, anche se nessuna fonte lo riporta;
3. le cifre singole erano ignorate: "entro 5 giorni" al posto di "entro 30
   giorni" non veniva visto, e nemmeno "6 mesi" o "3%";
4. i valori in lettere ("sessanta giorni") non erano controllati affatto,
   benche' gli atti scrivano spesso "30 (trenta) giorni";
5. le date erano spezzate in numeri sciolti, e giorno e mese (cifre singole o
   con zero iniziale) sfuggivano: "18/09/2026" al posto di "15/03/2026"
   produceva al massimo un avviso su "18".

E un difetto opposto: nelle tabelle appiattite dall'estrazione ("Ore corso
300 400 600") i numeri separati da spazio venivano fusi in "300400600", e un
valore vero veniva segnalato come inventato. Un controllo che grida "al lupo"
viene presto ignorato, quindi anche questo conta.

Il principio: per le FONTI si raccoglie un insieme generoso (ogni lettura
plausibile di ogni numero); per la RISPOSTA si richiede che ogni valore
affermato abbia almeno una lettura interamente presente nella fonte attribuita.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------------------------------------------------------- unita'
# Un numero seguito da una di queste e' un VALORE da verificare anche se ha
# una sola cifra: "5 giorni", "6 mesi", "3%" sono esattamente cio' che conta.
UNITA = (
    r"(?:giorn[oi](?:\s+(?:lavorativ[oi]|natural[ei]|consecutiv[ie]|solar[ei]))?|gg\.?|"
    r"mes[ei]|ann[oi]|settiman[ae]|or[ae]|h\b|minut[oi]|"
    r"euro|eur\b|€|%|per\s*cento|percento|"
    r"credit[oi]|cfu|punt[oi]|allievi|partecipanti|post[oi]|destinatari|"
    r"edizion[ei]|modul[oi]|annualit[aà]|rate|mensilit[aà])"
)

# --------------------------------------------------------------- date
MESI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
_MESE_RE = "|".join(MESI)
DATA_NUM = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})\b")
DATA_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATA_TESTO = re.compile(
    rf"\b(\d{{1,2}})\s*°?\s+({_MESE_RE})(?:\s+(\d{{4}}))?\b", re.I)


def _data_token(g: int, m: int, a: int | None) -> list[str]:
    if not (1 <= g <= 31 and 1 <= m <= 12):
        return []
    out = [f"DM:{m:02d}-{g:02d}"]
    if a is not None:
        if a < 100:
            a += 2000 if a < 70 else 1900
        out.append(f"D:{a:04d}-{m:02d}-{g:02d}")
    return out


def _date(text: str) -> list[tuple[int, int, list[str]]]:
    """(inizio, fine, token) per ogni data trovata."""
    out: list[tuple[int, int, list[str]]] = []
    for m in DATA_ISO.finditer(text):
        out.append((m.start(), m.end(), _data_token(int(m[3]), int(m[2]), int(m[1]))))
    for m in DATA_NUM.finditer(text):
        out.append((m.start(), m.end(), _data_token(int(m[1]), int(m[2]), int(m[3]))))
    for m in DATA_TESTO.finditer(text):
        a = int(m[3]) if m[3] else None
        out.append((m.start(), m.end(), _data_token(int(m[1]), MESI[m[2].lower()], a)))
    # togli le sovrapposizioni, tenendo la piu' lunga
    out.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    finale: list[tuple[int, int, list[str]]] = []
    fine = -1
    for s, e, tok in out:
        if s >= fine and tok:
            finale.append((s, e, tok))
            fine = e
    return finale


# --------------------------------------------------------- numeri in lettere
_UNITA_P = {
    "zero": 0, "uno": 1, "un": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
    "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12,
    "tredici": 13, "quattordici": 14, "quindici": 15, "sedici": 16, "diciassette": 17,
    "diciotto": 18, "diciannove": 19,
}
_DECINE = {
    "venti": 20, "trenta": 30, "quaranta": 40, "cinquanta": 50, "sessanta": 60,
    "settanta": 70, "ottanta": 80, "novanta": 90,
}


def _sotto_mille(w: str) -> int | None:
    if not w:
        return 0
    if w.startswith("cento"):
        r = _sotto_mille(w[5:])
        return None if r is None else 100 + r
    for k, v in _UNITA_P.items():
        if k in ("un", "una", "zero"):
            continue
        if w.startswith(k + "cento"):
            r = _sotto_mille(w[len(k) + 5:])
            return None if r is None or v < 2 else v * 100 + r
    if w in _UNITA_P and w not in ("un", "una"):
        return _UNITA_P[w]
    for k, v in _DECINE.items():
        stem = k[:-1]                       # "trenta" -> "trent" (trentuno, trentotto)
        for pref in (k, stem):
            if w.startswith(pref):
                resto = w[len(pref):]
                if resto == "":
                    return v
                if resto == "tre" or resto == "tré":
                    return v + 3
                if resto in _UNITA_P and resto not in ("un", "una", "zero") and _UNITA_P[resto] < 10:
                    return v + _UNITA_P[resto]
    return None


def parola_a_numero(w: str) -> int | None:
    """'trenta' -> 30, 'centoventi' -> 120, 'duemilacinquecento' -> 2500.

    Volutamente esclude 'un', 'uno', 'una' isolati: sono quasi sempre articoli.
    """
    w = w.lower().strip()
    if w in ("un", "uno", "una") or len(w) < 3:
        return None
    if w == "mille":
        return 1000
    if w.startswith("mille"):
        r = _sotto_mille(w[5:])
        return None if r is None else 1000 + r
    if "mila" in w:
        testa, _, coda = w.partition("mila")
        t = _sotto_mille(testa)
        c = _sotto_mille(coda)
        if t is None or c is None or t < 2:
            return None
        return t * 1000 + c
    return _sotto_mille(w)


PAROLA_NUM = re.compile(r"\b([a-zà-ù]{3,30})\b", re.I)

# --------------------------------------------------------------- numeri
# 1) migliaia raggruppate con punto: 250.000 / 1.250.000,50
NUM_PUNTO = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?"
# 2) migliaia raggruppate con spazio (anche non separabile): 250 000 — ambiguo
#    con una riga di tabella "300 400 600"
NUM_SPAZIO = r"\d{1,3}(?:[   ]\d{3})+(?:,\d+)?"
# 3) numero semplice, eventualmente decimale: 30 / 2,5 / 4.2
NUM_SEMPLICE = r"\d+(?:[.,]\d+)?"
NUMERO = re.compile(rf"(?<!\d)(?<!\d[.,])(?:{NUM_PUNTO}|{NUM_SPAZIO}|{NUM_SEMPLICE})(?![\w])")
DOPO_UNITA = re.compile(rf"^\s*(?:{UNITA})(?![a-zà-ù])", re.I)
PRIMA_EURO = re.compile(r"(?:€|euro|eur)\s*$", re.I)


def _norm(s: str) -> str:
    s = s.replace(" ", "").replace(" ", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f.is_integer() else repr(round(f, 6)).rstrip("0").rstrip(".")


def _letture(tok: str) -> list[set[str]]:
    """Possibili letture di un numero: '300 400' e' 300400 oppure 300 e 400."""
    if re.search(r"[   ]", tok):
        parti = re.split(r"[   ]", tok)
        return [{_norm(tok)}, {_norm(p) for p in parti}]
    return [{_norm(tok)}]


@dataclass
class Valore:
    """Un valore affermato nella risposta, con le sue letture alternative."""
    testo: str
    letture: list[set[str]]

    def confermato(self, pool: set[str]) -> bool:
        return any(l <= pool for l in self.letture)


def valori_fonte(text: str) -> set[str]:
    """Tutto cio' che un estratto puo' confermare: insieme generoso."""
    out: set[str] = set()
    for _, _, toks in _date(text):
        out.update(toks)
    for m in NUMERO.finditer(text):
        for l in _letture(m.group(0)):
            out.update(l)
        # le parti di una data o di un numero composto valgono anche da sole
        for p in re.findall(r"\d+", m.group(0)):
            out.add(_norm(p))
    for m in PAROLA_NUM.finditer(text):
        n = parola_a_numero(m.group(1))
        if n is not None:
            out.add(str(n))
    return out


def valori_risposta(text: str) -> list[Valore]:
    """I valori che la risposta AFFERMA e che vanno quindi trovati in una fonte."""
    out: list[Valore] = []
    # le date si controllano intere, poi si tolgono dal testo
    pezzi: list[str] = []
    ultimo = 0
    for s, e, toks in _date(text):
        piena = [t for t in toks if t.startswith("D:")] or toks
        out.append(Valore(text[s:e], [{piena[0]}]))
        pezzi.append(text[ultimo:s])
        pezzi.append(" § ")
        ultimo = e
    pezzi.append(text[ultimo:])
    resto = "".join(pezzi)

    for m in NUMERO.finditer(resto):
        tok = m.group(0)
        significative = len(re.sub(r"\D", "", tok).lstrip("0"))
        con_unita = bool(DOPO_UNITA.match(resto[m.end():m.end() + 30])) or bool(
            PRIMA_EURO.search(resto[max(0, m.start() - 6):m.start()]))
        if significative >= 2 or con_unita:
            out.append(Valore(tok, _letture(tok)))

    for m in PAROLA_NUM.finditer(resto):
        n = parola_a_numero(m.group(1))
        if n is None:
            continue
        # un numero in lettere e' un valore solo se seguito da un'unita':
        # "tre giorni" si', "le tre condizioni" no
        if DOPO_UNITA.match(resto[m.end():m.end() + 30]):
            out.append(Valore(m.group(1), [{str(n)}]))
    return out
