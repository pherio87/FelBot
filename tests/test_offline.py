"""Verifiche che non richiedono ne' rete ne' chiavi API (servono le dipendenze Python leggere:
`pip install pydantic-settings pyyaml httpx tenacity lxml`).

Sono scritte in chiave AVVERSARIA: ogni caso in questo file e' un tentativo di
far passare un'affermazione infondata, o di far perdere un documento al
crawler. I test che si limitano a confermare cio' che il codice fa non servono
a niente — questi devono fallire se una garanzia si indebolisce.

    python3 tests/test_offline.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

OK, KO = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
fails: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  {OK} {label}")
    else:
        fails.append(label)
        print(f"  {KO} {label}  {extra}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ══════════════════════════════════════════ riferimenti agli atti
section("Riconoscimento degli estremi degli atti")
from fler.retrieve import ActRef, extract_refs, fts_query, ref_query

for q, atteso in [
    ("Cosa prevede la DGR 1298/2015?", ("1298", "2015")),
    ("determinazione dirigenziale n. 16133/2025", ("16133", "2025")),
    ("ai sensi della L.R. 17/2005", ("17", "2005")),
    ("nota PG.2017.333374", ("333374", "2017")),
    ("determina 456 del 2018 allegato 3", ("456", "2018")),
]:
    refs = extract_refs(q)
    check(f"{q[:44]!r} → {[str(r) for r in refs]}",
          any(r.tokens == atteso for r in refs), f"atteso {atteso}")

# Questi sono i falsi positivi che portavano in cima frammenti a caso
# marcandoli come "riferimento esatto"
for q in [
    "requisiti di accreditamento aggiornati al 2024",
    "un corso da 600 ore e' finanziabile?",
    "il contributo massimo e' di 15.000 euro?",
    "FSE+ 2021-2027: quali priorita'?",
    "percentuale minima 100% di frequenza",
]:
    check(f"nessun falso riferimento in {q[:44]!r}", extract_refs(q) == [],
          str([str(r) for r in extract_refs(q)]))

check("due atti citati insieme sono in OR, non in AND",
      " OR " in ref_query(extract_refs("confronta DGR 1298/2015 e PG.2017.333374")))
check("gli estremi si cercano in prossimita', non sparsi",
      "NEAR(" in ref_query([ActRef("DGR", "1298", "2015")]))

section("Query full-text: sicurezza e conservazione degli identificativi")
for q in ['accreditamento " OR 1=1 --', "NEAR(a b)", "corso* AND (x", "^inizio", "???", ""]:
    fq = fts_query(q)
    safe = all(t.startswith('"') and t.endswith('"') for t in fq.split(" OR ") if t)
    check(f"{q[:30]!r} → {fq[:46]!r}", safe, "sintassi non neutralizzata")

# scartare i token brevi cancellava proprio cio' per cui esiste il BM25
check('il "17" di L.R. 17/2005 sopravvive', '"17"' in fts_query("cosa dice la L.R. 17/2005"))
check('il "pg" di PG.2017.333374 sopravvive', '"pg"' in fts_query("la nota PG.2017.333374"))
check('il "3" di "allegato 3" sopravvive', '"3"' in fts_query("vedi allegato 3"))
check("le stop-word accentate sono riconosciute",
      '"perche"' not in fts_query("perche' serve") and '"più"' not in fts_query("più ore"))
check("una domanda reale non si svuota", fts_query("quali requisiti per l'accreditamento") != "")

# ══════════════════════════════════════════ chunking
section("Suddivisione in frammenti")
from fler.chunking import chunk_document
from fler.extract import Block

doc = {
    "doc_id": "test:1", "title": "Delibera di Giunta regionale 1298/2015 recante approvazione",
    "portal_type": "File", "breadcrumb": "leggi-atti-bandi/atti-amministrativi",
    "site_effective": "2015-09-14T00:00:00", "filename": "dgr1298.pdf",
    "meta": {"fonte": "Formazione e Lavoro — RER"},
}

c = chunk_document(doc, [Block(text="Art. 12 Ore di stage\n\n" + "Le ore di stage sono 400. " * 90)])
check("la rubrica d'articolo non viene scartata", any("Art. 12" in x["text"] for x in c))
check("la rubrica diventa intestazione del frammento",
      any((x["heading"] or "").startswith("Art. 12") for x in c))

mono = chunk_document(doc, [Block(text="Il presente allegato disciplina i parametri. " * 330)])
check(f"un blocco monolitico viene spezzato ({len(mono)} frammenti, max "
      f"{max(len(x['text']) for x in mono)} car.)",
      max(len(x["text"]) for x in mono) < 3200 and len(mono) > 4)

minimo = chunk_document(doc, [Block(text="Art. 12"), Block(text="Le ore di stage sono 400.")])
check("un documento brevissimo non si perde", len(minimo) >= 1 and "400" in minimo[0]["text"])

norm = chunk_document(doc, [
    Block(text="Art. 1 Oggetto\n\n" + "Disciplina l'accreditamento. " * 45, page=1),
    Block(text="Art. 2 Requisiti\n\n" + "Requisiti degli organismi. " * 45, page=2),
    Block(text="Allegato A\n\n" + "Tabella dei parametri orari. " * 35, page=3),
])
check("le pagine sono conservate", all(x["page"] in (1, 2, 3) for x in norm))
check("gli identificativi sono univoci", len({x["chunk_id"] for x in norm}) == len(norm))
check("l'intestazione contestuale e' presente su ogni frammento",
      all(x["text"].startswith("[Documento: Delibera") for x in norm))
check("l'intestazione e' conteggiata nel budget",
      max(len(x["text"]) for x in norm) < 2400, f"max {max(len(x['text']) for x in norm)}")

# ══════════════════════════════════════════ verifica anti-allucinazione
section("Verifica della risposta — scenari avversari")
from fler.answer import REFUSAL, Source, verify


def src(n: int, text: str, **kw) -> Source:
    return Source(n=n, title=f"Doc {n}", url=f"http://x/{n}", download_url=None, page=None,
                  date="2024-01-01", portal_type="File", section=None, source_label="RER",
                  score=0.5, snippet=text[:180], text=text, **kw)


def fonti(**kw) -> list[Source]:
    return [
        src(1, "Il contributo massimo per progetto e' pari a 50.000 euro come da allegato A.", **kw),
        src(2, "Le domande sono presentate entro il 30 giugno 2024 secondo le modalita' indicate."),
        src(3, "Gli organismi accreditati trasmettono la comunicazione entro trenta giorni."),
    ]


S = fonti()
t, w, conf = verify(
    "Il contributo massimo e' pari a 50.000 euro [1]. Le domande si presentano entro "
    "il 30 giugno 2024 [2]. La comunicazione va trasmessa entro trenta giorni [3].", S)
check("una risposta corretta e ben citata NON viene punita", conf == "alta" and not w, str(w))
check("le fonti citate risultano marcate", all(s.cited for s in S))

S = fonti()
t, w, conf = verify(
    "Il contributo massimo per progetto e' pari a 250.000 euro e la scadenza e' "
    "il 31 dicembre 2026 [1][2][3].", S)
check("citazione valida ma DATI ALTERATI → affidabilita' bassa", conf == "bassa", conf)
check("i valori non confermati sono elencati all'utente",
      any("non trovati negli estratti" in x for x in w), str(w))

S = fonti()
t, w, conf = verify(
    "Il contributo e' di 250.000 euro e l'anticipo e' pari all'80%. "
    "Gli enti devono avere un fatturato minimo. "
    "Non risulta dai documenti indicizzati. quale sia il termine di rendicontazione.", S)
check("la frase di rifiuto in coda NON disattiva la verifica", conf != "rifiuto", conf)
check("le affermazioni infondate restano segnalate", bool(w))

S = fonti()
t, w, conf = verify(
    "Gli organismi devono essere accreditati [1]. Il termine e' trenta giorni [3]. "
    "Devono possedere un patrimonio netto non inferiore a 500.000 euro [7].", S)
check("un riferimento inesistente resta VISIBILE nel testo",
      "RIFERIMENTO INESISTENTE" in t)
check("un riferimento inesistente forza l'affidabilita' bassa", conf == "bassa", conf)

S = fonti()
t, w, conf = verify(
    "Gli organismi trasmettono la comunicazione entro trenta giorni [3].\n"
    "Importo massimo: 250.000 euro.\nScadenza: 31 dicembre 2025.\nAnticipo: 80%.", S)
check("le righe brevi non citate sono controllate", conf == "bassa" and bool(w), conf)

S = fonti()
t, w, conf = verify(
    "Requisiti richiesti [1]:\n- sede idonea\n- fatturato minimo di 500.000 euro\n"
    "- certificazione ISO 9001", S)
check("un elenco con una sola citazione viene segnalato", conf == "bassa", conf)

S = fonti()
t, w, conf = verify("Il contributo e' 50.000 euro [1, 2]. Il termine e' trenta giorni [1-3].", S)
check("le forme [1, 2] e [1-3] sono riconosciute e non declassate",
      conf in ("alta", "media") and all(s.cited for s in S), conf)

S = fonti()
t, w, conf = verify(REFUSAL + " Conviene consultare il BURERT.", S)
check("un rifiuto pulito e' riconosciuto come tale", conf == "rifiuto", conf)

S = fonti(expired=True, stale_years=4.0)
S[1].via_ocr = True
t, w, conf = verify("Il contributo e' 50.000 euro [1] e le domande entro il 30 giugno 2024 [2].", S)
check("una fonte scaduta genera un avviso automatico",
      any("scaduto" in x.lower() for x in w), str(w))
check("una fonte da OCR genera un avviso automatico", any("OCR" in x for x in w))
check("la riga sulla vigenza e' aggiunta dal codice, non dal modello",
      "Verificare che gli atti citati" in t)
check("le fonti datate o scadute non ottengono affidabilita' alta", conf != "alta", conf)

# ══════════════════════════════════════════ astensione
section("Soglia di astensione")
from fler.config import settings
from fler.retrieve import Hit, Retriever, SearchResult


def hit(sim, lex=None, exact=False):
    return Hit(chunk_id=f"c{id(sim)}{lex}", score=0.1, lexical_rank=lex,
               exact_ref=exact, data={"similarita": sim, "doc_id": "d", "ord": 0})


class FakeR(Retriever):
    def __init__(self):  # nessun catalogo, nessuna rete
        pass


r = FakeR()
soglia = settings.min_relevance

res = SearchResult(hits=[hit(0.10, 0), hit(0.09, 1), hit(0.08, 2), hit(0.07, 3), hit(0.06, 4)])
check("solo concordanza lessicale NON basta piu' per rispondere",
      r.is_confident(res)[0] is False, str(r.is_confident(res)))

res = SearchResult(hits=[hit(soglia + 0.2, 0), hit(0.10, 1), hit(0.09, 2)])
check("un unico frammento pertinente non basta (serve quorum)",
      r.is_confident(res)[0] is False, str(r.is_confident(res)))

res = SearchResult(hits=[hit(soglia + 0.2, 0), hit(soglia + 0.05, 1), hit(0.10, 2)])
check("due frammenti sopra soglia fanno rispondere", r.is_confident(res)[0] is True)

res = SearchResult(hits=[hit(0.05, 0, exact=True), hit(0.04, 1)],
                   refs=[ActRef("DGR", "1298", "2015")])
check("un riferimento d'atto validato basta da solo", r.is_confident(res)[0] is True)

res = SearchResult(hits=[hit(None, 0), hit(None, 1), hit(None, 2), hit(None, 3)],
                   semantic_ok=False, degraded=["semantica giu'"])
ok, motivo = r.is_confident(res)
check("senza ramo semantico si risponde solo con forte accordo testuale",
      ok is True and "testuale" in motivo, motivo)

check("un solo frammento non basta mai", r.is_confident(SearchResult(hits=[hit(0.9, 0)]))[0] is False)
check("nessun frammento → astensione", r.is_confident(SearchResult())[0] is False)

# ══════════════════════════════════════════ estrazione
section("Estrazione del testo")
from fler.extract import clean, extract_csv, extract_txt, sniff

check("sillabazione di fine riga ricomposta", clean("accredita-\nmento") == "accreditamento")
check("PDF riconosciuto dalla firma", sniff(b"%PDF-1.7 ...", None, None) == "pdf")
check("DOCX riconosciuto da estensione", sniff(b"PK\x03\x04", None, "a.docx") == "docx")
check("PPTX riconosciuto", sniff(b"PK\x03\x04", None, "slide.pptx") == "pptx")
# un contenitore ZIP trattato come docx faceva perdere in silenzio la
# modulistica allegata ai bandi
check("uno ZIP NON viene confuso con un docx",
      sniff(b"PK\x03\x04" + b"\x00" * 100, None, "modulistica.zip") == "zip")
check("uno ZIP senza estensione non diventa docx",
      sniff(b"PK\x03\x04" + b"\x00" * 4000, None, None) == "zip")
check(".doc binario riconosciuto", sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", None, "a.doc") == "doc")
check("testo semplice estratto", extract_txt(b"Determinazione n. 123").n_chars > 10)
check("CSV tabulato estratto", "Bologna" in extract_csv(b"ente,sede\nOficina,Bologna\n").text)

# ══════════════════════════════════════════ catalogo
section("Catalogo, incrementale e indice full-text")
from fler.catalog import Catalog

with tempfile.TemporaryDirectory() as td:
    cat = Catalog(Path(td) / "c.sqlite3")
    d = {"doc_id": "s:1", "source_id": "s", "url": "http://x/1", "title": "Atto uno",
         "portal_type": "File", "site_modified": "2024-01-01T10:00:00",
         "meta": {"fonte": "RER"}, "breadcrumb": "atti", "filename": "a.pdf"}

    check("primo avvistamento → da scaricare", cat.upsert_discovered(d) is True)
    check("finche' non e' stato scaricato resta in coda", cat.upsert_discovered(d) is True)

    cat.replace_chunks("s:1", [
        {"chunk_id": "s:1#0", "ord": 0, "page": 1, "heading": None,
         "text": "L'accreditamento ai sensi della DGR 1298/2015 del 14 settembre."},
        {"chunk_id": "s:1#1", "ord": 1, "page": 2, "heading": "Art. 2",
         "text": "I requisiti sono definiti nell'allegato A."},
    ])
    cat.mark("s:1", status="indexed", last_fetched=__import__("time").time())
    check("indicizzato e data invariata → non riscaricato",
          cat.upsert_discovered(d, revalidate_after_days=30) is False)

    # il portale sostituisce allegati senza toccare la data di modifica
    cat.mark("s:1", last_fetched=__import__("time").time() - 60 * 86400)
    check("dopo la finestra di rivalidazione → riscaricato",
          cat.upsert_discovered(d, revalidate_after_days=30) is True)

    cat.mark("s:1", status="indexed", last_fetched=__import__("time").time())
    check("data di modifica cambiata → riscaricato",
          cat.upsert_discovered(dict(d, site_modified="2024-06-01T10:00:00")) is True)

    rows = cat.conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", ('"1298"',)).fetchall()
    check("l'indice full-text trova l'estremo dell'atto", len(rows) == 1, str(rows))

    cat.replace_chunks("s:1", [
        {"chunk_id": "s:1#0b", "ord": 0, "page": 1, "heading": None, "text": "Testo aggiornato."}])
    n = cat.conn.execute("SELECT COUNT(*) n FROM chunks_fts").fetchone()["n"]
    check("la reindicizzazione non lascia orfani nel full-text", n == 1, f"trovati {n}")
    check("la cancellazione full-text avviene per rowid",
          cat.conn.execute("SELECT fts_rowid FROM chunks").fetchone()["fts_rowid"] is not None)

    check("i frammenti contigui sono recuperabili",
          cat.neighbours("s:1", 0) == ["s:1#0b"])

    cat.mark_embedded(["s:1#0b"])
    check("nessun frammento in attesa dopo la vettorizzazione", len(cat.unembedded()) == 0)

    # un documento dato per rimosso e poi ricomparso non deve restare invisibile
    cat.mark("s:1", gone=1, status="indexed", indexed_hash="abc")
    n = cat.purge_gone()
    row = cat.doc("s:1")
    check("dopo la rimozione il documento torna in coda, non resta 'indicizzato'",
          row["status"] == "discovered" and row["indexed_hash"] is None, row["status"])
    check("i frammenti dei documenti ritirati non sono piu' recuperabili",
          cat.hydrate(["s:1#0b"]) == {})

    cat.log_query("domanda di prova", True, 3, 1.2)
    check("le domande vecchie si possono cancellare", cat.purge_queries(keep_days=0) == 1)

# ══════════════════════════════════════════ sorgenti e configurazione
section("Sorgenti e configurazione")
import yaml

cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "src/fler/sources.yml").read_text())
attive = [s for s in cfg["sources"] if s.get("enabled")]
check("almeno una sorgente attiva", len(attive) >= 1)
check("la fonte primaria e' il portale Formazione e Lavoro",
      any(s["id"] == "formazionelavoro" for s in attive))
check("ogni sorgente dichiara tipo e url",
      all(s.get("kind") and s.get("base_url") for s in cfg["sources"]))
check("la soglia di pertinenza non e' sotto il rumore del corpus",
      settings.min_relevance >= 0.35, str(settings.min_relevance))

from fler.retrieve import REF_WEIGHT, RRF_K
check("RRF_K e' calibrata sul pool reale, non sul valore da letteratura",
      10 <= RRF_K <= 30, str(RRF_K))
check("il peso dei riferimenti non schiaccia gli altri segnali",
      1.0 < REF_WEIGHT <= 1.6, str(REF_WEIGHT))


# ══════════════════════════════════════════ v1.2: falle trovate nella revisione
section("v1.2 — verifica dei valori: falle della 1.1")
from fler.valori import parola_a_numero, valori_fonte, valori_risposta

s50 = src(1, "Il contributo massimo per progetto e' pari a 50.000 euro.")
s30 = src(2, "Le domande vanno presentate entro 30 giorni dalla pubblicazione.")
s250 = src(3, "Il cofinanziamento complessivo del programma e' di 250.000 euro.")

_, w, c = verify("Il contributo massimo per progetto e' di 250.000 euro [1].\n"
                 "Le domande entro 30 giorni [2].\nVedi anche il programma [3].", [s50, s30, s250])
check("importo preso da un ALTRO estratto citato → declassato", c == "bassa", f"{c} {w}")
check("…e l'avviso parla di attribuzione errata", any("attribuiti" in x for x in w), str(w))

_, w, c = verify("Il contributo massimo e' di 80.000 euro [1].", [s50, s30],
                 question="E' vero che il contributo massimo e' 80.000 euro?")
check("numero suggerito dalla domanda, non nella fonte → declassato", c == "bassa", f"{c} {w}")

_, w, c = verify("Le domande vanno presentate entro 5 giorni dalla pubblicazione [2].", [s50, s30])
check("cifra singola con unita' alterata (5 giorni invece di 30) → declassata", c == "bassa", f"{c} {w}")

_, w, c = verify("La quota di cofinanziamento e' del 3% [1].",
                 [src(1, "La quota di cofinanziamento e' pari al 5% del costo."), s30])
check("percentuale a una cifra alterata → declassata", c == "bassa", f"{c} {w}")

_, w, c = verify("Le domande vanno presentate entro sessanta giorni [2].", [s50, s30])
check("valore in lettere alterato → declassato", c == "bassa", f"{c} {w}")

_, w, c = verify("La scadenza e' il 18/09/2026 [1].",
                 [src(1, "La scadenza per la presentazione e' fissata al 15/03/2026."), s30])
check("data con giorno e mese alterati → segnalata la data intera",
      c == "bassa" and any("18/09/2026" in x for x in w), f"{c} {w}")

_, w, c = verify("Non risulta dai documenti indicizzati. Comunque il contributo e' di 999.000 euro.",
                 [s50, s30])
check("rifiuto con contenuto inventato in coda → NON classificato come rifiuto",
      c != "rifiuto", f"{c} {w}")

section("v1.2 — verifica dei valori: nessun falso allarme")
tab = src(1, "Tabella parametri\nOre corso 300 400 600\nCosto ora 12,50 15,00")
_, w, c = verify("Il percorso da 400 ore [1] ha un costo orario di 15,00 euro [1].", [tab, s30])
check("valore vero in una tabella appiattita NON segnalato", not any("Valori" in x for x in w), str(w))

_, w, c = verify("Gli organismi trasmettono la comunicazione entro 30 giorni [1].",
                 [src(1, "trasmettono la comunicazione entro trenta giorni."), s30])
check("'30' in cifre confermato da 'trenta' in lettere", not any("Valori" in x for x in w), str(w))

_, w, c = verify("La scadenza e' il 15 marzo 2026 [1].",
                 [src(1, "La scadenza e' fissata al 15/03/2026."), s30])
check("'15 marzo 2026' confermato da '15/03/2026'", not any("Valori" in x for x in w), str(w))

_, w, c = verify("Il contributo e' di € 50.000,00 [1].", [s50, s30])
check("'€ 50.000,00' confermato da '50.000 euro'", not any("Valori" in x for x in w), str(w))

_, w, c = verify("Lo prevede il protocollo PG.2017.333374 [1].",
                 [src(1, "Nota prot. PG.2017.333374 del servizio regionale."), s30])
check("numero di protocollo riportato esattamente non segnalato",
      not any("Valori" in x for x in w), str(w))

_, w, c = verify("La DGR n. 1298/2015 disciplina l'accreditamento [1].",
                 [src(1, "Delibera di Giunta regionale n. 1298 del 2015 sull'accreditamento."), s30])
check("estremi d'atto in forma diversa ma stessi valori non segnalati",
      not any("Valori" in x for x in w), str(w))

check("parole-numero: trenta=30, centoventi=120, duemilacinquecento=2500",
      (parola_a_numero("trenta"), parola_a_numero("centoventi"),
       parola_a_numero("duemilacinquecento")) == (30, 120, 2500))
check("parole-numero: articoli e parole comuni non sono numeri",
      all(parola_a_numero(x) is None for x in ("una", "uno", "settimane", "novembre", "milano")))
check("'le tre condizioni' non e' un valore da verificare (manca l'unita')",
      not valori_risposta("Valgono le tre condizioni previste"))

section("v1.2 — crawler: rimozioni solo se verificate")
import asyncio
import types
try:
    import selectolax.parser  # noqa: F401
except ImportError:  # serve solo al crawler HTML, non a queste verifiche
    sys.modules["selectolax.parser"] = types.SimpleNamespace(HTMLParser=object)
from fler.pipeline import Pipeline
from fler.fetcher import FetchResult


class FintoPortale:
    """Catalogo da 1000 oggetti; `nascosti` non compaiono in enumerazione,
    `ritirati` non esistono piu' davvero (404)."""
    def __init__(self, nascosti=(), ritirati=()):
        self.nascosti, self.ritirati = set(nascosti), set(ritirati)
    async def get_json(self, url, params):
        b = params["b_start"]
        items = [{"@id": f"https://x/doc{i}", "UID": f"u{i}", "@type": "Document",
                  "modified": "2025-01-01"}
                 for i in range(b, min(b + 100, 1000))
                 if i not in self.nascosti and i not in self.ritirati]
        return {"items_total": 1000 - len(self.ritirati), "items": items}
    async def get(self, url, **kw):
        i = int(url.rsplit("doc", 1)[1])
        return FetchResult(404 if i in self.ritirati else 200, b"{}", None, {}, url)


async def _due_giri(portale):
    cat = Catalog(Path(tempfile.mkdtemp()) / "c.sqlite3")
    await Pipeline(catalog=cat).discover(FintoPortale(), [{"id": "fl", "kind": "plone",
                                                          "base_url": "https://x"}], since=None)
    import time as _t; _t.sleep(0.01)
    p = Pipeline(catalog=cat)
    await p.discover(portale, [{"id": "fl", "kind": "plone", "base_url": "https://x"}], since=None)
    return cat, p

cat, p = asyncio.run(_due_giri(FintoPortale(nascosti=range(500, 515))))
check("15 documenti saltati dall'enumerazione ma ancora pubblicati NON vengono ritirati",
      cat.count_active("fl") == 1000, f"attivi={cat.count_active('fl')} {p.stats}")
cat, p = asyncio.run(_due_giri(FintoPortale(ritirati=range(10, 20))))
check("10 documenti davvero rimossi (404) vengono ritirati",
      cat.count_active("fl") == 990, f"attivi={cat.count_active('fl')} {p.stats}")

section("v1.2 — catalogo: rivalidazione e download inutili")
import time as _time
cat = Catalog(Path(tempfile.mkdtemp()) / "c.sqlite3")
d = {"doc_id": "x:1", "source_id": "x", "url": "http://x/1", "site_modified": "2024-01-01"}
cat.upsert_discovered(d)
cat.mark("x:1", status="empty", last_fetched=_time.time())
check("un documento senza testo NON viene riscaricato a ogni scansione completa",
      cat.upsert_discovered(d, revalidate_after_days=30) is False)
cat.mark("x:1", status="indexed", last_fetched=_time.time() - 40 * 86400)
check("un documento non riverificato da 40 giorni torna in coda anche nell'incrementale",
      cat.queue_stale(30, 100) == 1 and cat.doc("x:1")["status"] == "discovered")
cat.mark("x:1", status="indexed", last_fetched=_time.time())
check("…ma non se e' stato riverificato di recente", cat.queue_stale(30, 100) == 0)


section("v1.2 — dati personali: elenchi nominativi esclusi")
from fler.privacy import escluso_per_contenuto, escluso_per_titolo
for t in ("Graduatoria finale corso OSS 2025", "Elenco degli ammessi alla selezione",
          "Esito della selezione - tirocinanti", "Elenco candidati idonei",
          "Assegnatari voucher formativi"):
    check(f"escluso per titolo: «{t}»", escluso_per_titolo(t) is not None)
for t in ("DGR 1298/2015 - Disposizioni per l'accreditamento",
          "Modulo di domanda di iscrizione", "Manuale di rendicontazione IeFP",
          "Calendario degli esami di qualifica"):
    check(f"NON escluso: «{t}»", escluso_per_titolo(t) is None)
elenco = "\n".join(f"{i}. ROSSI MARIO RSSMRA80A01A944{chr(65 + i)} nato a Bologna il 01/01/1980"
                   for i in range(5))
check("allegato con titolo neutro ma codici fiscali → escluso",
      escluso_per_contenuto(elenco) is not None)
modulo = ("Cognome ________ Nome ________\nnato a ________ il ________\n"
          "nato a ________ (genitore)\nnato a ________ (tutore)")
check("modulistica in bianco NON esclusa", escluso_per_contenuto(modulo) is None)

cat = Catalog(Path(tempfile.mkdtemp()) / "c.sqlite3")
cat.upsert_discovered({"doc_id": "d:1", "source_id": "x", "url": "http://x/1"})
cat.replace_chunks("d:1", [{"chunk_id": "d:1#0:aaaa", "ord": 0, "text": "vecchio testo"}])
cat.mark_embedded(["d:1#0:aaaa"])
cat.replace_chunks("d:1", [{"chunk_id": "d:1#0:bbbb", "ord": 0, "text": "testo nuovo"}])
check("un frammento sostituito lascia il suo vettore in coda di cancellazione",
      cat.pending_vector_deletes() == ["d:1#0:aaaa"], str(cat.pending_vector_deletes()))
cat.clear_vector_deletes(["d:1#0:aaaa"])
cat.mark_embedded(["d:1#0:bbbb"])
cat.replace_chunks("d:1", [])
check("un documento escluso dopo l'indicizzazione toglie anche i vettori",
      cat.pending_vector_deletes() == ["d:1#0:bbbb"] and not cat.has_chunks("d:1"))

print("\n" + "─" * 66)
if fails:
    print(f"\033[31m{len(fails)} VERIFICHE FALLITE\033[0m")
    for f in fails:
        print(f"  · {f}")
else:
    print("\033[32mTUTTE LE VERIFICHE SUPERATE\033[0m")
sys.exit(1 if fails else 0)
