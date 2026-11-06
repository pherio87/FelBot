"""Esclusione dei documenti con dati personali di persone fisiche.

Il portale pubblica graduatorie, elenchi di ammessi, esiti di selezione con
nomi e cognomi. All'assistente non servono (non rispondono a domande sulla
normativa), e indicizzarli significherebbe copiarli, conservarli e
ripresentarli a chi interroga il servizio: un trattamento diverso dalla
pubblicazione originale, di cui risponde chi gestisce il servizio.

Due livelli:
- sul TITOLO, prima di scaricare: niente traffico, niente copia;
- sul TESTO estratto, dopo: per gli allegati con titolo neutro ("Allegato 1")
  che contengono in realta' un elenco nominativo.

Il criterio sul testo e' volutamente prudente: la modulistica in bianco
("Cognome ____ Nome ____") e' utile e non va esclusa.
"""
from __future__ import annotations

import re

# Titoli che indicano elenchi di persone. "ammessi"/"esclusi" possono
# riferirsi anche a progetti: la perdita e' accettabile, quegli elenchi non
# rispondono comunque a domande sulla normativa.
TITOLO = re.compile(
    r"\b(?:graduatori[ae]|ammess[io]\b|non\s+ammess|idone[io]\b|esclus[io]\b|"
    r"vincitor[ei]|nominativ[io]|assegnatari|"
    r"esit[oi]\s+(?:della\s+|delle\s+)?(?:selezion|prov[ae]|colloqui|valutazion)|"
    r"elenc[oh]i?\s+(?:dei\s+|degli\s+|delle\s+)?"
    r"(?:partecipanti|candidat[ie]|allievi|iscritti|corsisti|tirocinanti|qualificati))",
    re.I,
)

# Codice fiscale di persona fisica (quello delle societa' e' di 11 cifre)
CF = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b")
NASCITA = re.compile(r"\bnat[oa]\s+(?:a|il)\s+[A-Z0-9]", re.I)
SEGNAPOSTO = re.compile(r"_{4,}|\.{6,}")


def escluso_per_titolo(*campi: str | None) -> str | None:
    for c in campi:
        if c and TITOLO.search(c):
            return f"titolo da elenco nominativo: «{c[:80]}»"
    return None


def escluso_per_contenuto(text: str) -> str | None:
    cf = set(CF.findall(text))
    if len(cf) >= 3:
        return f"contiene {len(cf)} codici fiscali di persone fisiche"
    nascite = len(NASCITA.findall(text))
    if nascite >= 3 and len(SEGNAPOSTO.findall(text)) < nascite:
        return f"contiene {nascite} dati anagrafici di nascita"
    return None
