/**
 * Verifica della risposta: gli stessi scenari avversari di tests/test_offline.py.
 * Se il porting in TypeScript si discosta dal Python, e' qui che si vede.
 */
import { describe, expect, it } from "vitest";
import { parolaANumero, valoriRisposta } from "../src/valori";
import { REFUSAL, verify, type Source } from "../src/verify";

function src(n: number, text: string, extra: Partial<Source> = {}): Source {
  return {
    n, title: `Doc ${n}`, url: `http://x/${n}`, download_url: null, page: null, date: "2024-01-01",
    portal_type: "File", section: null, source_label: "RER", score: 0.5, snippet: text.slice(0, 180),
    text, expired: false, unpublished: false, via_ocr: false, stale_years: 0, cited: false, ...extra,
  };
}
const fonti = (extra: Partial<Source> = {}) => [
  src(1, "Il contributo massimo per progetto e' pari a 50.000 euro come da allegato A.", extra),
  src(2, "Le domande sono presentate entro il 30 giugno 2024 secondo le modalita' indicate."),
  src(3, "Gli organismi accreditati trasmettono la comunicazione entro trenta giorni."),
];
const valori = (w: string[]) => w.some((x) => x.includes("Valori"));

describe("verifica — scenari della 1.1", () => {
  it("una risposta corretta e ben citata NON viene punita", () => {
    const S = fonti();
    const r = verify("Il contributo massimo e' pari a 50.000 euro [1]. Le domande si presentano entro " +
      "il 30 giugno 2024 [2]. La comunicazione va trasmessa entro trenta giorni [3].", S);
    expect(r.confidence).toBe("alta");
    expect(r.warnings).toEqual([]);
    expect(S.every((s) => s.cited)).toBe(true);
  });

  it("citazione valida ma dati alterati → bassa, con i valori elencati", () => {
    const r = verify("Il contributo massimo per progetto e' pari a 250.000 euro e la scadenza e' " +
      "il 31 dicembre 2026 [1][2][3].", fonti());
    expect(r.confidence).toBe("bassa");
    expect(r.warnings.some((x) => x.includes("non trovati negli estratti"))).toBe(true);
  });

  it("la frase di rifiuto in coda NON disattiva la verifica", () => {
    const r = verify("Il contributo e' di 250.000 euro e l'anticipo e' pari all'80%. " +
      "Gli enti devono avere un fatturato minimo. " +
      "Non risulta dai documenti indicizzati. quale sia il termine di rendicontazione.", fonti());
    expect(r.confidence).not.toBe("rifiuto");
    expect(r.warnings.length).toBeGreaterThan(0);
  });

  it("un riferimento inesistente resta visibile e forza bassa", () => {
    const r = verify("Gli organismi devono essere accreditati [1]. Il termine e' trenta giorni [3]. " +
      "Devono possedere un patrimonio netto non inferiore a 500.000 euro [7].", fonti());
    expect(r.text).toContain("RIFERIMENTO INESISTENTE");
    expect(r.confidence).toBe("bassa");
  });

  it("le righe brevi non citate sono controllate", () => {
    const r = verify("Gli organismi trasmettono la comunicazione entro trenta giorni [3].\n" +
      "Importo massimo: 250.000 euro.\nScadenza: 31 dicembre 2025.\nAnticipo: 80%.", fonti());
    expect(r.confidence).toBe("bassa");
  });

  it("un elenco con una sola citazione viene segnalato", () => {
    const r = verify("Requisiti richiesti [1]:\n- sede idonea\n- fatturato minimo di 500.000 euro\n" +
      "- certificazione ISO 9001", fonti());
    expect(r.confidence).toBe("bassa");
  });

  it("le forme [1, 2] e [1-3] sono riconosciute e non declassate", () => {
    const S = fonti();
    const r = verify("Il contributo e' 50.000 euro [1, 2]. Il termine e' trenta giorni [1-3].", S);
    expect(["alta", "media"]).toContain(r.confidence);
    expect(S.every((s) => s.cited)).toBe(true);
  });

  it("un rifiuto pulito e' riconosciuto", () => {
    expect(verify(REFUSAL + " Conviene consultare il BURERT.", fonti()).confidence).toBe("rifiuto");
  });

  it("scaduto, OCR e vigenza: avvisi calcolati dal codice", () => {
    const S = fonti({ expired: true, stale_years: 4 });
    S[1].via_ocr = true;
    const r = verify("Il contributo e' 50.000 euro [1] e le domande entro il 30 giugno 2024 [2].", S);
    expect(r.warnings.some((x) => x.toLowerCase().includes("scaduto"))).toBe(true);
    expect(r.warnings.some((x) => x.includes("OCR"))).toBe(true);
    expect(r.text).toContain("Verificare che gli atti citati");
    expect(r.confidence).not.toBe("alta");
  });
});

describe("verifica — falle della 1.1 chiuse nella 1.2", () => {
  const s50 = src(1, "Il contributo massimo per progetto e' pari a 50.000 euro.");
  const s30 = src(2, "Le domande vanno presentate entro 30 giorni dalla pubblicazione.");
  const s250 = src(3, "Il cofinanziamento complessivo del programma e' di 250.000 euro.");

  it("importo preso da un ALTRO estratto citato → bassa, attribuzione errata", () => {
    const r = verify("Il contributo massimo per progetto e' di 250.000 euro [1].\n" +
      "Le domande entro 30 giorni [2].\nVedi anche il programma [3].", [s50, s30, s250]);
    expect(r.confidence).toBe("bassa");
    expect(r.warnings.some((x) => x.includes("attribuiti"))).toBe(true);
  });

  it("numero suggerito dalla domanda, non nella fonte → bassa", () => {
    const r = verify("Il contributo massimo e' di 80.000 euro [1].", [s50, s30],
      "E' vero che il contributo massimo e' 80.000 euro?");
    expect(r.confidence).toBe("bassa");
  });

  it("cifra singola con unita' alterata (5 giorni invece di 30) → bassa", () => {
    expect(verify("Le domande vanno presentate entro 5 giorni dalla pubblicazione [2].", [s50, s30]).confidence)
      .toBe("bassa");
  });

  it("percentuale a una cifra alterata → bassa", () => {
    expect(verify("La quota di cofinanziamento e' del 3% [1].",
      [src(1, "La quota di cofinanziamento e' pari al 5% del costo."), s30]).confidence).toBe("bassa");
  });

  it("valore in lettere alterato → bassa", () => {
    expect(verify("Le domande vanno presentate entro sessanta giorni [2].", [s50, s30]).confidence).toBe("bassa");
  });

  it("data con giorno e mese alterati → segnalata intera", () => {
    const r = verify("La scadenza e' il 18/09/2026 [1].",
      [src(1, "La scadenza per la presentazione e' fissata al 15/03/2026."), s30]);
    expect(r.confidence).toBe("bassa");
    expect(r.warnings.some((x) => x.includes("18/09/2026"))).toBe(true);
  });

  it("rifiuto con contenuto inventato in coda → non e' un rifiuto", () => {
    expect(verify("Non risulta dai documenti indicizzati. Comunque il contributo e' di 999.000 euro.",
      [s50, s30]).confidence).not.toBe("rifiuto");
  });
});

describe("verifica — nessun falso allarme", () => {
  const s30 = src(2, "Le domande vanno presentate entro 30 giorni dalla pubblicazione.");
  const cases: [string, string, string][] = [
    ["tabella appiattita", "Tabella parametri\nOre corso 300 400 600\nCosto ora 12,50 15,00",
      "Il percorso da 400 ore [1] ha un costo orario di 15,00 euro [1]."],
    ["30 in cifre confermato da trenta", "trasmettono la comunicazione entro trenta giorni.",
      "Gli organismi trasmettono la comunicazione entro 30 giorni [1]."],
    ["15 marzo 2026 = 15/03/2026", "La scadenza e' fissata al 15/03/2026.", "La scadenza e' il 15 marzo 2026 [1]."],
    ["€ 50.000,00 = 50.000 euro", "Il contributo massimo per progetto e' pari a 50.000 euro.",
      "Il contributo e' di € 50.000,00 [1]."],
    ["protocollo", "Nota prot. PG.2017.333374 del servizio regionale.",
      "Lo prevede il protocollo PG.2017.333374 [1]."],
    ["estremi d'atto in forma diversa", "Delibera di Giunta regionale n. 1298 del 2015 sull'accreditamento.",
      "La DGR n. 1298/2015 disciplina l'accreditamento [1]."],
  ];
  for (const [nome, fonte, risposta] of cases) {
    it(nome, () => expect(valori(verify(risposta, [src(1, fonte), s30]).warnings)).toBe(false));
  }

  it("parole-numero", () => {
    expect([parolaANumero("trenta"), parolaANumero("centoventi"), parolaANumero("duemilacinquecento")])
      .toEqual([30, 120, 2500]);
    for (const x of ["una", "uno", "settimane", "novembre", "milano"]) expect(parolaANumero(x)).toBeNull();
    expect(valoriRisposta("Valgono le tre condizioni previste")).toEqual([]);
  });

  it("lettere accentate attorno ai numeri non rompono i confini (porting da Python)", () => {
    expect(valoriRisposta("è 30 giorni").map((v) => v.testo)).toEqual(["30"]);
    expect(valoriRisposta("annualità 2 rate").length).toBe(1);
  });
});
