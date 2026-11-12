/**
 * Catalogo per il crawler: l'unico modo in cui il container tocca i dati.
 *
 * Il crawler (Python, nel container) chiama http://felbot.interno/... e la
 * classe Crawler intercetta quelle richieste in uscita e le passa qui, dentro
 * il Worker, con accesso diretto a D1, Vectorize e Workers AI. Non esiste una
 * rotta pubblica: niente token, niente eccezioni in Access.
 *
 * Le regole sono quelle di src/fler/catalog.py, portate in SQL a blocchi:
 * - un documento si riscarica se e' cambiato, mai scaricato, in errore,
 *   ricomparso, indicizzato senza frammenti, o non riverificato da N giorni;
 * - i documenti senza testo ed esclusi NON si riscaricano a ogni scansione;
 * - un documento si ritira solo se il portale lo conferma (vedi lotto.py);
 * - i vettori di frammenti sostituiti si cancellano subito, e se la
 *   cancellazione fallisce restano in vector_deletes per il giro successivo.
 */

export interface InternoEnv {
  DB: D1Database;
  VECTORS?: VectorizeIndex;
  AI?: Ai;
  EMBED_MODEL: string;
}

const ora = () => Date.now() / 1000;
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

// campi del documento che arrivano dall'enumerazione
const CAMPI_SCOPERTA = [
  "source_id", "url", "api_url", "download_url", "portal_type", "title", "description", "mime",
  "filename", "section", "breadcrumb", "site_modified", "site_effective", "expires", "review_state",
  "meta_json",
] as const;

// campi che il crawler puo' aggiornare dopo lo scaricamento: nient'altro
const CAMPI_ESITO = new Set([
  "status", "error", "last_fetched", "http_etag", "http_last_mod", "content_hash", "bytes",
  "text_chars", "extract_method", "via_ocr", "truncated",
]);
const STATI = new Set(["discovered", "error", "empty", "skipped", "excluded", "indexed"]);

export async function sha1hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export interface Esistente {
  site_modified: string | null;
  status: string;
  gone: number;
  last_fetched: number | null;
  ha_frammenti: number | null;
}

/** Il documento visto in enumerazione va (ri)scaricato? Stessa regola di
 *  catalog.upsert_discovered, verificata dagli stessi test. */
export function daRiscaricare(
  cur: Esistente, siteModified: string | null, revalidateDays: number, adesso = ora(),
): boolean {
  if (cur.status === "in_lavorazione") return false; // e' gia' in mano a un lotto
  const cambiato = (cur.site_modified || "") !== (siteModified || "");
  const scaduto = revalidateDays > 0 && cur.status !== "excluded" &&
    (cur.last_fetched || 0) < adesso - revalidateDays * 86400;
  const anomalo = !cur.ha_frammenti && (cur.status === "indexed" || cur.status === "chunked");
  return cambiato || cur.status === "discovered" || cur.status === "error" || !!cur.gone || anomalo || scaduto;
}

// ------------------------------------------------------------------ embedding
async function embed(env: InternoEnv, testi: string[]): Promise<number[][]> {
  if (!env.AI) {
    // solo sviluppo locale, dove Workers AI non c'e': vettori fittizi stabili
    return Promise.all(testi.map(async (t) => {
      const h = await sha1hex(t);
      return Array.from({ length: 1024 }, (_, i) => (parseInt(h[i % 40], 16) - 7.5) / 8);
    }));
  }
  const out: number[][] = [];
  for (let i = 0; i < testi.length; i += 20) {
    const r = (await env.AI.run(env.EMBED_MODEL as any, { text: testi.slice(i, i + 20) })) as unknown as
      { data: number[][] };
    out.push(...r.data);
  }
  return out;
}

async function cancellaVettori(env: InternoEnv, ids: string[]): Promise<void> {
  if (!ids.length || !env.VECTORS) return;
  try {
    for (let i = 0; i < ids.length; i += 500) await env.VECTORS.deleteByIds(ids.slice(i, i + 500));
  } catch (e) {
    console.error("cancellazione vettori rimandata", String(e));
    await env.DB.batch(ids.map((v) => env.DB.prepare("INSERT OR IGNORE INTO vector_deletes (vec_id) VALUES (?1)").bind(v)));
  }
}

/** Toglie frammenti, indice full-text e vettori di un insieme di documenti. */
async function svuota(env: InternoEnv, docIds: string[]): Promise<number> {
  if (!docIds.length) return 0;
  const ids = JSON.stringify(docIds);
  const { results } = await env.DB.prepare(
    "SELECT vec_id FROM chunks WHERE doc_id IN (SELECT value FROM json_each(?1))",
  ).bind(ids).all<{ vec_id: string }>();
  await env.DB.batch([
    env.DB.prepare(`DELETE FROM chunks_fts WHERE rowid IN (SELECT fts_rowid FROM chunks
      WHERE doc_id IN (SELECT value FROM json_each(?1)) AND fts_rowid IS NOT NULL)`).bind(ids),
    env.DB.prepare("DELETE FROM chunks WHERE doc_id IN (SELECT value FROM json_each(?1))").bind(ids),
  ]);
  await cancellaVettori(env, results.map((r) => r.vec_id));
  return results.length;
}

// ------------------------------------------------------------------ giro
interface Giro {
  kind: "completa" | "incrementale";
  since: string | null;
  started: number;
  enumerazione_completa: boolean;
  enumerazione_fatta: boolean;
  stats: Record<string, number>;
  errori: string[];
}

async function kvGet(env: InternoEnv, k: string): Promise<string | null> {
  const r = await env.DB.prepare("SELECT v FROM kv WHERE k = ?1").bind(k).first<{ v: string }>();
  return r?.v ?? null;
}
const kvSet = (env: InternoEnv, k: string, v: string) =>
  env.DB.prepare("INSERT INTO kv (k, v) VALUES (?1, ?2) ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(k, v);

async function giroCorrente(env: InternoEnv): Promise<Giro | null> {
  const v = await kvGet(env, "giro_corrente");
  return v ? (JSON.parse(v) as Giro) : null;
}

async function inCoda(env: InternoEnv): Promise<{ discovered: number; in_lavorazione: number }> {
  const r = await env.DB.prepare(`SELECT
      SUM(CASE WHEN status = 'discovered' THEN 1 ELSE 0 END) AS discovered,
      SUM(CASE WHEN status = 'in_lavorazione' THEN 1 ELSE 0 END) AS in_lavorazione
    FROM documents WHERE gone = 0`).first<{ discovered: number | null; in_lavorazione: number | null }>();
  return { discovered: r?.discovered ?? 0, in_lavorazione: r?.in_lavorazione ?? 0 };
}

// ------------------------------------------------------------------ rotte
type Corpo = Record<string, any>;

export async function gestisciInterno(request: Request, env: InternoEnv): Promise<Response> {
  const url = new URL(request.url);
  const p = url.pathname.replace(/^\/__interno/, "");
  const corpo: Corpo = request.method === "POST" ? ((await request.json().catch(() => ({}))) as Corpo) : {};
  try {
    switch (p) {
      case "/stato": return json(await stato(env));
      case "/giro/apri": return json(await apriGiro(env, corpo));
      case "/giro/aggiorna": return json(await aggiornaGiro(env, corpo));
      case "/giro/chiudi": return json(await chiudiGiro(env));
      case "/scoperti": return json(await scoperti(env, corpo));
      case "/rimozioni/candidati": return json(await candidati(env, corpo));
      case "/rimozioni/esito": return json(await esitoRimozioni(env, corpo));
      case "/rivalida": return json(await rivalida(env, corpo));
      case "/coda": return json(await coda(env, corpo));
      case "/esiti": return json(await esiti(env, corpo));
      case "/frammenti": return json(await frammenti(env, corpo));
      default: return json({ errore: `rotta sconosciuta ${p}` }, 404);
    }
  } catch (e) {
    console.error("interno", p, String(e));
    return json({ errore: String(e) }, 500);
  }
}

async function stato(env: InternoEnv) {
  const [giro, ultimo, coda, attivi] = await Promise.all([
    giroCorrente(env), kvGet(env, "last_run_finished"), inCoda(env),
    env.DB.prepare("SELECT COUNT(*) AS n FROM documents WHERE gone = 0").first<{ n: number }>(),
  ]);
  return { giro, last_run_finished: ultimo, ...coda, attivi: attivi?.n ?? 0 };
}

async function apriGiro(env: InternoEnv, c: Corpo) {
  const esistente = await giroCorrente(env);
  if (esistente) return { giro: esistente, nuovo: false };
  const giro: Giro = {
    kind: c.kind === "completa" ? "completa" : "incrementale",
    since: typeof c.since === "string" ? c.since : null,
    started: ora(), enumerazione_completa: false, enumerazione_fatta: false, stats: {}, errori: [],
  };
  await kvSet(env, "giro_corrente", JSON.stringify(giro)).run();
  return { giro, nuovo: true };
}

async function aggiornaGiro(env: InternoEnv, c: Corpo) {
  const giro = await giroCorrente(env);
  if (!giro) return { giro: null };
  for (const [k, v] of Object.entries((c.stats ?? {}) as Record<string, number>))
    if (typeof v === "number") giro.stats[k] = (giro.stats[k] ?? 0) + v;
  for (const e of (c.errori ?? []) as string[]) if (giro.errori.length < 50) giro.errori.push(String(e).slice(0, 500));
  if (typeof c.enumerazione_completa === "boolean") {
    giro.enumerazione_fatta = true;
    giro.enumerazione_completa = c.enumerazione_completa;
  }
  await kvSet(env, "giro_corrente", JSON.stringify(giro)).run();
  return { giro };
}

/** Chiude il giro se la coda e' vuota. Il punto di ripartenza dell'incrementale
 *  avanza SOLO se il giro e' riuscito per intero. */
async function chiudiGiro(env: InternoEnv) {
  const giro = await giroCorrente(env);
  if (!giro) return { chiuso: false, motivo: "nessun giro aperto" };
  const coda = await inCoda(env);
  if (coda.discovered + coda.in_lavorazione > 0) return { chiuso: false, motivo: "coda non vuota", ...coda };

  // vettori la cui cancellazione era fallita: nuovo tentativo
  const { results: orfani } = await env.DB.prepare("SELECT vec_id FROM vector_deletes LIMIT 2000").all<{ vec_id: string }>();
  if (orfani.length && env.VECTORS) {
    try {
      await env.VECTORS.deleteByIds(orfani.map((o) => o.vec_id));
      await env.DB.prepare("DELETE FROM vector_deletes WHERE vec_id IN (SELECT value FROM json_each(?1))")
        .bind(JSON.stringify(orfani.map((o) => o.vec_id))).run();
    } catch (e) {
      giro.errori.push(`pulizia vettori orfani: ${e}`);
    }
  }

  const ok = giro.enumerazione_completa && giro.errori.length === 0;
  const fine = ora();
  const stats = { ...giro.stats, esito: ok ? "ok" : "parziale", durata_secondi: Math.round(fine - giro.started),
    ...(giro.errori.length ? { problemi: giro.errori.slice(0, 10) } : {}) };
  const stmt = [
    env.DB.prepare("INSERT INTO runs (started, finished, kind, outcome, stats_json) VALUES (?1, ?2, ?3, ?4, ?5)")
      .bind(giro.started, fine, giro.kind, ok ? "ok" : "parziale", JSON.stringify(stats)),
    kvSet(env, "last_run_outcome", ok ? "ok" : "parziale"),
    env.DB.prepare("DELETE FROM kv WHERE k = 'giro_corrente'"),
  ];
  if (ok) stmt.push(kvSet(env, "last_run_finished", new Date(fine * 1000).toISOString().replace(/\.\d+Z$/, "+00:00")));
  else console.warn("giro incompleto: il punto di ripartenza NON viene avanzato");
  await env.DB.batch(stmt);
  return { chiuso: true, esito: ok ? "ok" : "parziale", stats };
}

async function scoperti(env: InternoEnv, c: Corpo) {
  const docs = ((c.docs ?? []) as Corpo[]).slice(0, 200);
  const giorni = Number(c.revalidate_days ?? 30);
  if (!docs.length) return { da_scaricare: 0 };
  const adesso = ora();
  const { results } = await env.DB.prepare(`SELECT d.doc_id, d.site_modified, d.status, d.gone, d.last_fetched,
      (SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id LIMIT 1) AS ha_frammenti
    FROM documents d WHERE d.doc_id IN (SELECT value FROM json_each(?1))`)
    .bind(JSON.stringify(docs.map((d) => d.doc_id))).all<Esistente & { doc_id: string }>();
  const esistenti = new Map(results.map((r) => [r.doc_id, r]));

  const valori = (d: Corpo) => CAMPI_SCOPERTA.map((k) =>
    k === "meta_json" ? JSON.stringify(d.meta ?? {}) : (d[k] ?? null));
  const stmts: D1PreparedStatement[] = [];
  let n = 0;
  for (const d of docs) {
    const cur = esistenti.get(d.doc_id);
    if (!cur) {
      n++;
      stmts.push(env.DB.prepare(
        `INSERT INTO documents (doc_id, ${CAMPI_SCOPERTA.join(", ")}, status, first_seen, last_seen, gone)
         VALUES (?1, ${CAMPI_SCOPERTA.map((_, i) => `?${i + 2}`).join(", ")}, 'discovered', ?${CAMPI_SCOPERTA.length + 2}, ?${CAMPI_SCOPERTA.length + 2}, 0)`,
      ).bind(d.doc_id, ...valori(d), adesso));
      continue;
    }
    const riscarica = daRiscaricare(cur, d.site_modified ?? null, giorni, adesso);
    if (riscarica) n++;
    stmts.push(env.DB.prepare(
      `UPDATE documents SET ${CAMPI_SCOPERTA.map((k, i) => `${k} = ?${i + 2}`).join(", ")},
         last_seen = ?${CAMPI_SCOPERTA.length + 2}, gone = 0
         ${riscarica ? ", status = 'discovered'" : ""}
       WHERE doc_id = ?1`,
    ).bind(d.doc_id, ...valori(d), adesso));
  }
  await env.DB.batch(stmts);
  return { da_scaricare: n };
}

async function candidati(env: InternoEnv, c: Corpo) {
  const [attivi, { results }] = await Promise.all([
    env.DB.prepare("SELECT COUNT(*) AS n FROM documents WHERE source_id = ?1 AND gone = 0").bind(c.source_id).first<{ n: number }>(),
    env.DB.prepare(`SELECT doc_id, url, api_url FROM documents
      WHERE source_id = ?1 AND last_seen < ?2 AND gone = 0 LIMIT 5000`).bind(c.source_id, Number(c.prima)).all(),
  ]);
  return { attivi: attivi?.n ?? 0, candidati: results };
}

async function esitoRimozioni(env: InternoEnv, c: Corpo) {
  const presenti = (c.ancora_presenti ?? []) as string[];
  const ritirati = (c.ritirati ?? []) as string[];
  if (presenti.length)
    await env.DB.prepare("UPDATE documents SET last_seen = ?2 WHERE doc_id IN (SELECT value FROM json_each(?1))")
      .bind(JSON.stringify(presenti), ora()).run();
  let frammenti = 0;
  for (let i = 0; i < ritirati.length; i += 100) {
    const parte = ritirati.slice(i, i + 100);
    frammenti += await svuota(env, parte);
    // se un giorno ricompare va riscaricato da capo, non lasciato "indicizzato" a vuoto
    await env.DB.prepare(`UPDATE documents SET gone = 1, status = 'discovered', indexed_hash = NULL,
        content_hash = NULL, http_etag = NULL, http_last_mod = NULL
      WHERE doc_id IN (SELECT value FROM json_each(?1))`).bind(JSON.stringify(parte)).run();
  }
  return { ritirati: ritirati.length, frammenti_rimossi: frammenti };
}

async function rivalida(env: InternoEnv, c: Corpo) {
  const giorni = Number(c.days ?? 30);
  if (giorni <= 0) return { in_coda: 0 };
  const attivi = await env.DB.prepare("SELECT COUNT(*) AS n FROM documents WHERE gone = 0").first<{ n: number }>();
  const quota = Math.floor((attivi?.n ?? 0) / giorni) + 20;
  const r = await env.DB.prepare(`UPDATE documents SET status = 'discovered' WHERE doc_id IN (
      SELECT doc_id FROM documents WHERE gone = 0 AND status IN ('indexed', 'empty', 'skipped')
      AND COALESCE(last_fetched, 0) < ?1 ORDER BY COALESCE(last_fetched, 0) ASC LIMIT ?2)`)
    .bind(ora() - giorni * 86400, quota).run();
  return { in_coda: r.meta.changes ?? 0 };
}

async function coda(env: InternoEnv, c: Corpo) {
  const limite = Math.min(Number(c.limite ?? 100), 500);
  const lease = Math.min(Number(c.lease_s ?? 7200), 6 * 3600);
  const adesso = ora();
  // prese in carico scadute: il lotto che le aveva e' morto, tornano in coda
  await env.DB.prepare("UPDATE documents SET status = 'discovered', lease_until = NULL WHERE status = 'in_lavorazione' AND lease_until < ?1")
    .bind(adesso).run();
  const { results } = await env.DB.prepare(`SELECT d.*,
      (SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id LIMIT 1) AS ha_frammenti
    FROM documents d WHERE d.status = 'discovered' AND d.gone = 0
    ORDER BY d.first_seen, d.doc_id LIMIT ?1`).bind(limite).all<Corpo>();
  if (results.length)
    await env.DB.prepare(`UPDATE documents SET status = 'in_lavorazione', lease_until = ?2
      WHERE doc_id IN (SELECT value FROM json_each(?1))`).bind(JSON.stringify(results.map((r) => r.doc_id)), adesso + lease).run();
  return { documenti: results };
}

function setCampi(campi: Corpo): [string, unknown[]] {
  const cols: string[] = [];
  const vals: unknown[] = [];
  for (const [k, v] of Object.entries(campi ?? {})) {
    if (!CAMPI_ESITO.has(k)) continue;
    if (k === "status" && !STATI.has(String(v))) continue;
    cols.push(k);
    vals.push(v ?? null);
  }
  return [cols.map((k, i) => `${k} = ?${i + 2}`).join(", "), vals];
}

async function esiti(env: InternoEnv, c: Corpo) {
  const lista = ((c.esiti ?? []) as { doc_id: string; campi: Corpo; svuota?: boolean }[]).slice(0, 200);
  const daSvuotare = lista.filter((e) => e.svuota).map((e) => e.doc_id);
  if (daSvuotare.length) await svuota(env, daSvuotare);
  const stmts = lista.map((e) => {
    const [set, vals] = setCampi(e.campi);
    return env.DB.prepare(`UPDATE documents SET ${set ? set + ", " : ""}lease_until = NULL WHERE doc_id = ?1`)
      .bind(e.doc_id, ...vals);
  });
  if (stmts.length) await env.DB.batch(stmts);
  return { aggiornati: stmts.length };
}

/** Sostituisce i frammenti di un documento: embedding, vettori, D1, indice
 *  full-text, stato. L'embedding viene PRIMA di toccare D1: se fallisce il
 *  documento resta com'era e il crawler lo segna in errore. */
async function frammenti(env: InternoEnv, c: Corpo) {
  const docId = String(c.doc_id);
  const chunks = ((c.chunks ?? []) as Corpo[]).slice(0, 1500);
  const nuovi: Corpo[] = await Promise.all(chunks.map(async (ch) => ({ ...ch, vec_id: await sha1hex(String(ch.chunk_id)) })));
  const vettori = await embed(env, nuovi.map((ch) => String(ch.text)));

  const { results: vecchi } = await env.DB.prepare("SELECT vec_id FROM chunks WHERE doc_id = ?1").bind(docId).all<{ vec_id: string }>();
  if (env.VECTORS && nuovi.length) {
    const v = nuovi.map((ch, i) => ({ id: ch.vec_id, values: vettori[i] }));
    for (let i = 0; i < v.length; i += 500) await env.VECTORS.upsert(v.slice(i, i + 500));
  }

  const [set, vals] = setCampi({ ...(c.campi ?? {}), status: "indexed" });
  const adesso = ora();
  await env.DB.batch([
    env.DB.prepare(`DELETE FROM chunks_fts WHERE rowid IN (SELECT fts_rowid FROM chunks
      WHERE doc_id = ?1 AND fts_rowid IS NOT NULL)`).bind(docId),
    env.DB.prepare("DELETE FROM chunks WHERE doc_id = ?1").bind(docId),
    ...nuovi.map((ch) => env.DB.prepare(`INSERT INTO chunks (chunk_id, vec_id, doc_id, ord, page, heading, text, n_chars, embedded)
      VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1)`).bind(ch.chunk_id, ch.vec_id, docId, ch.ord, ch.page ?? null,
      ch.heading ?? null, ch.text, String(ch.text).length)),
    env.DB.prepare("UPDATE chunks SET fts_rowid = id WHERE doc_id = ?1").bind(docId),
    env.DB.prepare("INSERT INTO chunks_fts (rowid, text) SELECT id, text FROM chunks WHERE doc_id = ?1").bind(docId),
    // in un UPDATE di SQLite i valori a destra sono quelli VECCHI della riga:
    // "indexed_hash = content_hash" avrebbe salvato l'hash precedente
    env.DB.prepare(`UPDATE documents SET ${set}, lease_until = NULL, last_indexed = ?${vals.length + 2},
      indexed_hash = COALESCE(?${vals.length + 3}, content_hash) WHERE doc_id = ?1`)
      .bind(docId, ...vals, adesso, c.campi?.content_hash ?? null),
  ]);

  const tenuti = new Set(nuovi.map((ch) => ch.vec_id));
  await cancellaVettori(env, vecchi.map((v) => v.vec_id).filter((v) => !tenuti.has(v)));
  return { frammenti: nuovi.length };
}
