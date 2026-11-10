/**
 * Recupero ibrido: lessicale (FTS5 su D1) + semantico (Vectorize), fusi con
 * RRF. Porting di retrieve.py; le parti pure (fusione, astensione, scelta del
 * contesto) sono separate dall'accesso ai dati per poterle verificare nei test.
 */
import { asDate, type ChunkData } from "./verify";
import { extractRefs, ftsQuery, refQuery, refToString, type ActRef } from "./refs";

// Costante RRF calibrata sul pool reale (80 candidati), non il 60 da letteratura
export const RRF_K = 20;
// Peso dei riscontri su riferimento d'atto validato: sopra gli altri, non tale
// da schiacciarli
export const REF_WEIGHT = 1.4;

export interface Hit {
  chunk_id: string;
  score: number;
  lexical_rank: number | null;
  vector_rank: number | null;
  exact_ref: boolean;
  neighbour: boolean;
  data: ChunkData;
}

export interface SearchResult {
  hits: Hit[];
  refs: ActRef[];
  semantic_ok: boolean;
  lexical_ok: boolean;
  degraded: string[];
}

export interface RetrieveEnv {
  DB: D1Database;
  VECTORS: VectorizeIndex;
  AI: Ai;
  EMBED_MODEL: string;
  TOP_K: string;
  CONTEXT_CHUNKS: string;
  MIN_RELEVANCE: string;
}

// ------------------------------------------------------------ parti pure
export function fuse(
  lex: [string, number][],
  sem: [string, number][],
  refHits: [string, number][],
  limit: number,
) {
  const scores = new Map<string, number>();
  const lexRank = new Map<string, number>();
  const vecRank = new Map<string, number>();
  const semSim = new Map<string, number>();
  const exact = new Set<string>();
  const add = (cid: string, v: number) => scores.set(cid, (scores.get(cid) ?? 0) + v);

  lex.forEach(([cid], i) => { add(cid, 1 / (RRF_K + i + 1)); lexRank.set(cid, i); });
  sem.forEach(([cid, sim], i) => { add(cid, 1 / (RRF_K + i + 1)); vecRank.set(cid, i); semSim.set(cid, sim); });
  refHits.forEach(([cid], i) => { add(cid, REF_WEIGHT / (RRF_K + i + 1)); exact.add(cid); });

  const ranked = [...scores.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit);
  return { ranked, lexRank, vecRank, semSim, exact };
}

/** Correttivo leggero su stato e attualita': non decide la vigenza, ma evita
 *  che a parita' di pertinenza una delibera di dieci anni fa prevalga. */
export function temporalFactor(d: ChunkData, oggi: Date = new Date()): number {
  let f = 1;
  const exp = asDate(d.expires);
  if (exp && exp.getTime() < oggi.getTime()) f *= 0.75;
  if ((d.review_state || "published") !== "published") f *= 0.85;
  const dt = asDate(d.site_effective) || asDate(d.site_modified);
  if (dt) {
    const anni = (oggi.getTime() - dt.getTime()) / 86400000 / 365.25;
    if (anni <= 3) f *= 1.12;
    else if (anni > 12) f *= 0.92;
  }
  return f;
}

/** Il recupero e' abbastanza solido da tentare una risposta? Condizioni
 *  CONGIUNTIVE: soglia di similarita' E quorum, oppure riferimento esatto. */
export function isConfident(res: SearchResult, minRelevance: number): [boolean, string] {
  const hits = res.hits;
  if (!hits.length) return [false, "nessun frammento trovato"];
  if (hits.length < 2) return [false, "un solo frammento recuperato"];

  const sims = hits.slice(0, 8).map((h) => h.data.similarita).filter((s): s is number => s != null);
  const best = sims.length ? Math.max(...sims) : null;
  const sopra = sims.filter((s) => s >= minRelevance).length;

  if (res.refs.length && hits.slice(0, 5).some((h) => h.exact_ref))
    return [true, `riscontro esatto su ${refToString(res.refs[0])}`];

  if (!res.semantic_ok) {
    const forti = hits.slice(0, 5).filter((h) => h.lexical_rank !== null && h.lexical_rank < 10).length;
    return [forti >= 3, "solo corrispondenza testuale"];
  }
  if (best === null) return [false, "nessun punteggio di similarita' disponibile"];
  if (best < minRelevance)
    return [false, `pertinenza massima ${best.toFixed(2)} sotto la soglia ${minRelevance}`];
  if (sopra < 2) return [false, "un solo frammento sopra la soglia di pertinenza"];
  return [true, `pertinenza ${best.toFixed(2)} su ${sopra} frammenti`];
}

/** Tetto adattivo per documento: di norma 3, ma se un atto domina il recupero
 *  gli si concede fino a meta' del contesto. */
export function chooseContext(hits: Hit[], n: number): Hit[] {
  if (!hits.length) return [];
  const totale = hits.reduce((a, h) => a + h.score, 0) || 1;
  const perDocScore = new Map<string, number>();
  for (const h of hits) perDocScore.set(h.data.doc_id, (perDocScore.get(h.data.doc_id) ?? 0) + h.score);
  const [dominante, sDom] = [...perDocScore.entries()].sort((a, b) => b[1] - a[1])[0];
  const capDominante = sDom / totale > 0.4 ? Math.max(3, Math.floor(n / 2)) : 3;

  const perDoc = new Map<string, number>();
  const chosen: Hit[] = [];
  for (const h of hits) {
    const d = h.data.doc_id;
    const cap = d === dominante ? capDominante : 3;
    if ((perDoc.get(d) ?? 0) >= cap) continue;
    perDoc.set(d, (perDoc.get(d) ?? 0) + 1);
    chosen.push(h);
    if (chosen.length >= n) break;
  }
  return chosen;
}

// ------------------------------------------------------------ accesso ai dati
const HYDRATE_SQL = `
  SELECT c.chunk_id, c.text, c.page, c.heading, c.ord,
         d.doc_id, d.title, d.url, d.download_url, d.filename, d.portal_type,
         d.breadcrumb, d.site_modified, d.site_effective, d.expires, d.review_state,
         d.source_id, d.meta_json, d.via_ocr, d.truncated
  FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
  WHERE c.chunk_id IN (SELECT value FROM json_each(?1))
    AND d.gone = 0 AND d.status != 'excluded'`;

const FTS_SQL = `
  SELECT c.chunk_id AS chunk_id, bm25(chunks_fts) AS s
  FROM chunks_fts f JOIN chunks c ON c.fts_rowid = f.rowid
  JOIN documents d ON d.doc_id = c.doc_id
  WHERE chunks_fts MATCH ?1 AND d.gone = 0 AND d.status != 'excluded'
  ORDER BY s LIMIT ?2`;

export class Retriever {
  constructor(private env: RetrieveEnv) {}

  get topK() { return Number(this.env.TOP_K) || 40; }
  get contextChunks() { return Number(this.env.CONTEXT_CHUNKS) || 14; }
  get minRelevance() { return Number(this.env.MIN_RELEVANCE) || 0.5; }

  private async fts(query: string, k: number): Promise<[string, number][]> {
    if (!query) return [];
    const { results } = await this.env.DB.prepare(FTS_SQL).bind(query, k).all<{ chunk_id: string; s: number }>();
    return results.map((r) => [r.chunk_id, -r.s]);
  }

  async embed(texts: string[]): Promise<number[][]> {
    const out = (await this.env.AI.run(this.env.EMBED_MODEL as any, { text: texts })) as unknown as { data: number[][] };
    return out.data;
  }

  private async semantic(q: string, k: number): Promise<[string, number][]> {
    const [vec] = await this.embed([q]);
    const res = await this.env.VECTORS.query(vec, { topK: Math.min(k, 50), returnMetadata: "none" });
    if (!res.matches.length) return [];
    // gli id dei vettori sono SHA-1 dei chunk_id (limite di 64 byte di Vectorize)
    const { results } = await this.env.DB.prepare(
      "SELECT vec_id, chunk_id FROM chunks WHERE vec_id IN (SELECT value FROM json_each(?1))",
    ).bind(JSON.stringify(res.matches.map((m) => m.id))).all<{ vec_id: string; chunk_id: string }>();
    const map = new Map(results.map((r) => [r.vec_id, r.chunk_id]));
    // un vettore senza frammento e' un orfano: si salta
    return res.matches.filter((m) => map.has(m.id)).map((m) => [map.get(m.id)!, m.score]);
  }

  async hydrate(ids: string[]): Promise<Map<string, ChunkData>> {
    if (!ids.length) return new Map();
    const { results } = await this.env.DB.prepare(HYDRATE_SQL).bind(JSON.stringify(ids)).all<ChunkData>();
    return new Map(results.map((r) => [r.chunk_id, r]));
  }

  async search(q: string): Promise<SearchResult> {
    const k = this.topK;
    const pool = Math.max(k * 2, 60);
    const res: SearchResult = { hits: [], refs: extractRefs(q), semantic_ok: true, lexical_ok: true, degraded: [] };

    const [lex, refHits, sem] = await Promise.all([
      this.fts(ftsQuery(q), pool).catch((e) => {
        console.error("ricerca lessicale fallita", String(e));
        res.lexical_ok = false;
        res.degraded.push("ricerca testuale non disponibile");
        return [] as [string, number][];
      }),
      res.refs.length ? this.fts(refQuery(res.refs), 25).catch(() => [] as [string, number][]) : Promise.resolve([]),
      this.semantic(q, pool).catch((e) => {
        // un ramo semantico spento in silenzio degradava il sistema a solo
        // BM25 per settimane senza che nulla lo segnalasse
        console.error("ricerca semantica non disponibile", String(e));
        res.semantic_ok = false;
        res.degraded.push("ricerca semantica non disponibile: risultati basati solo sulla corrispondenza testuale");
        return [] as [string, number][];
      }),
    ]);

    const f = fuse(lex, sem, refHits, k * 2);
    const data = await this.hydrate(f.ranked.map(([c]) => c));
    const oggi = new Date();
    const hits: Hit[] = [];
    for (const [cid, sc] of f.ranked) {
      const d = data.get(cid);
      if (!d) continue;
      d.similarita = f.semSim.get(cid) ?? null;
      hits.push({
        chunk_id: cid, score: sc * temporalFactor(d, oggi),
        lexical_rank: f.lexRank.get(cid) ?? null, vector_rank: f.vecRank.get(cid) ?? null,
        exact_ref: f.exact.has(cid), neighbour: false, data: d,
      });
    }
    hits.sort((a, b) => b.score - a.score);
    res.hits = hits.slice(0, k);
    return res;
  }

  /** Sceglie i frammenti per il modello e li estende ai contigui, perche' tre
   *  pezzi sconnessi di una procedura sono peggio di due consecutivi. */
  async selectContext(res: SearchResult): Promise<Hit[]> {
    const n = this.contextChunks;
    const chosen = chooseContext(res.hits, n);
    const spazio = Math.max(0, n - chosen.length) * 2;
    if (!chosen.length || !spazio) return ordina(chosen);

    const have = new Set(chosen.map((h) => h.chunk_id));
    const { results } = await this.env.DB.prepare(
      `SELECT c.chunk_id, j.key AS parent FROM json_each(?1) j
       JOIN chunks c ON c.doc_id = json_extract(j.value, '$.d')
        AND c.ord BETWEEN json_extract(j.value, '$.o') - 1 AND json_extract(j.value, '$.o') + 1
       ORDER BY j.key, c.ord`,
    ).bind(JSON.stringify(chosen.map((h) => ({ d: h.data.doc_id, o: h.data.ord })))).all<{ chunk_id: string; parent: number }>();
    const want = results.filter((r) => !have.has(r.chunk_id)).slice(0, spazio);
    const extra = await this.hydrate(want.map((w) => w.chunk_id));

    const out = [...chosen];
    for (const w of want) {
      if (out.length >= n + 4) break;
      const d = extra.get(w.chunk_id);
      if (!d || have.has(w.chunk_id)) continue;
      have.add(w.chunk_id);
      out.push({ chunk_id: w.chunk_id, score: chosen[w.parent].score * 0.5, lexical_rank: null,
        vector_rank: null, exact_ref: false, neighbour: true, data: d });
    }
    return ordina(out);
  }
}

// il modello legge una sequenza: per documento e posizione
const ordina = (hs: Hit[]) =>
  [...hs].sort((a, b) => a.data.doc_id.localeCompare(b.data.doc_id) || a.data.ord - b.data.ord);
