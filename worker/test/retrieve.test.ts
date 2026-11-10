import { describe, expect, it } from "vitest";
import { extractRefs, ftsQuery, refQuery, refToString } from "../src/refs";
import { chooseContext, fuse, isConfident, REF_WEIGHT, RRF_K, type Hit, type SearchResult } from "../src/retrieve";
import type { ChunkData } from "../src/verify";

describe("riferimenti agli atti", () => {
  const casi: [string, [string, string]][] = [
    ["Cosa prevede la DGR 1298/2015?", ["1298", "2015"]],
    ["determinazione dirigenziale n. 16133/2025", ["16133", "2025"]],
    ["ai sensi della L.R. 17/2005", ["17", "2005"]],
    ["nota PG.2017.333374", ["333374", "2017"]],
    ["determina 456 del 2018 allegato 3", ["456", "2018"]],
  ];
  for (const [q, [num, year]] of casi) {
    it(q, () => expect(extractRefs(q).some((r) => r.num === num && r.year === year)).toBe(true));
  }
  for (const q of [
    "requisiti di accreditamento aggiornati al 2024",
    "un corso da 600 ore e' finanziabile?",
    "il contributo massimo e' di 15.000 euro?",
    "FSE+ 2021-2027: quali priorita'?",
    "percentuale minima 100% di frequenza",
  ]) {
    it(`nessun falso riferimento: ${q}`, () => expect(extractRefs(q).map(refToString)).toEqual([]));
  }
  it("due atti in OR, estremi in prossimita'", () => {
    expect(refQuery(extractRefs("confronta DGR 1298/2015 e PG.2017.333374"))).toContain(" OR ");
    expect(refQuery([{ kind: "DGR", num: "1298", year: "2015" }])).toContain("NEAR(");
  });
});

describe("query full-text", () => {
  for (const q of ['accreditamento " OR 1=1 --', "NEAR(a b)", "corso* AND (x", "^inizio", "???", ""]) {
    it(`sintassi neutralizzata: ${q}`, () => {
      const fq = ftsQuery(q);
      for (const t of fq.split(" OR ").filter(Boolean)) expect(t.startsWith('"') && t.endsWith('"')).toBe(true);
    });
  }
  it("conserva gli identificativi brevi e scarta le stop-word accentate", () => {
    expect(ftsQuery("cosa dice la L.R. 17/2005")).toContain('"17"');
    expect(ftsQuery("la nota PG.2017.333374")).toContain('"pg"');
    expect(ftsQuery("vedi allegato 3")).toContain('"3"');
    expect(ftsQuery("perche' serve")).not.toContain('"perche"');
    expect(ftsQuery("più ore")).not.toContain('"più"');
    expect(ftsQuery("quali requisiti per l'accreditamento")).not.toBe("");
  });
});

const dati = (o: Partial<ChunkData> = {}): ChunkData => ({
  chunk_id: "c", text: "", page: null, heading: null, ord: 0, doc_id: "d", title: null, url: null,
  download_url: null, filename: null, portal_type: null, breadcrumb: null, site_modified: null,
  site_effective: null, expires: null, review_state: null, source_id: null, meta_json: null,
  via_ocr: 0, truncated: 0, ...o,
});
let seq = 0;
const hit = (sim: number | null, lex: number | null = null, exact = false, doc = "d"): Hit => ({
  chunk_id: `c${seq++}`, score: 0.1, lexical_rank: lex, vector_rank: null, exact_ref: exact,
  neighbour: false, data: dati({ similarita: sim, doc_id: doc }),
});
const res = (hits: Hit[], o: Partial<SearchResult> = {}): SearchResult =>
  ({ hits, refs: [], semantic_ok: true, lexical_ok: true, degraded: [], ...o });

describe("astensione", () => {
  const S = 0.5;
  it("solo concordanza lessicale non basta", () =>
    expect(isConfident(res([hit(0.1, 0), hit(0.09, 1), hit(0.08, 2), hit(0.07, 3)]), S)[0]).toBe(false));
  it("un unico frammento pertinente non basta (quorum)", () =>
    expect(isConfident(res([hit(S + 0.2, 0), hit(0.1, 1), hit(0.09, 2)]), S)[0]).toBe(false));
  it("due frammenti sopra soglia fanno rispondere", () =>
    expect(isConfident(res([hit(S + 0.2, 0), hit(S + 0.05, 1), hit(0.1, 2)]), S)[0]).toBe(true));
  it("un riferimento d'atto validato basta da solo", () =>
    expect(isConfident(res([hit(0.05, 0, true), hit(0.04, 1)],
      { refs: [{ kind: "DGR", num: "1298", year: "2015" }] }), S)[0]).toBe(true));
  it("senza ramo semantico serve forte accordo testuale", () => {
    const [ok, motivo] = isConfident(res([hit(null, 0), hit(null, 1), hit(null, 2), hit(null, 3)],
      { semantic_ok: false }), S);
    expect(ok).toBe(true);
    expect(motivo).toContain("testuale");
  });
  it("un frammento solo o nessuno → astensione", () => {
    expect(isConfident(res([hit(0.9, 0)]), S)[0]).toBe(false);
    expect(isConfident(res([]), S)[0]).toBe(false);
  });
});

describe("fusione e contesto", () => {
  it("costanti calibrate", () => {
    expect(RRF_K).toBeGreaterThanOrEqual(10);
    expect(RRF_K).toBeLessThanOrEqual(30);
    expect(REF_WEIGHT).toBeGreaterThan(1);
    expect(REF_WEIGHT).toBeLessThanOrEqual(1.6);
  });
  it("il primo assoluto di una lista batte un risultato tardivo presente in entrambe", () => {
    const lex: [string, number][] = [["esatto", 9], ...Array.from({ length: 40 }, (_, i) => [`l${i}`, 1] as [string, number])];
    const sem: [string, number][] = [...Array.from({ length: 40 }, (_, i) => [`s${i}`, 0.5] as [string, number])];
    lex.push(["comune", 1]);
    sem.push(["comune", 0.4]);
    const { ranked } = fuse(lex, sem, [], 80);
    const pos = (id: string) => ranked.findIndex(([c]) => c === id);
    expect(pos("esatto")).toBeLessThan(pos("comune"));
  });
  it("tetto di 3 frammenti per documento, salvo documento dominante", () => {
    const sparsi = [...Array.from({ length: 6 }, () => hit(0.6, 0, false, "A")),
      ...Array.from({ length: 6 }, (_, i) => hit(0.6, 0, false, `B${i}`))];
    sparsi.forEach((h, i) => (h.score = h.data.doc_id === "A" ? 0.01 : 0.2 - i * 0.001));
    expect(chooseContext(sparsi, 14).filter((h) => h.data.doc_id === "A").length).toBe(3);

    const dominante = [...Array.from({ length: 10 }, () => hit(0.6, 0, false, "A")), hit(0.6, 0, false, "B")];
    dominante.forEach((h) => (h.score = h.data.doc_id === "A" ? 0.5 : 0.1));
    expect(chooseContext(dominante, 14).filter((h) => h.data.doc_id === "A").length).toBe(7);
  });
});
