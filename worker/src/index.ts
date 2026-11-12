/**
 * FelBot — Worker.
 *
 *   /bot, /bot.html     pubblico: pagina informativa sul crawler
 *   POST /api/contatto  pubblico: modulo di contatto (Turnstile + limiti)
 *   tutto il resto      riservato: identita' verificata da Cloudflare Access
 *     GET  /            chat
 *     GET  /api/stato   stato dell'indice e uso personale
 *     POST /api/chat    domanda -> risposta in streaming (SSE)
 *     GET  /admin       solo AMMINISTRATORI: pagina del crawler
 *     /api/crawler/...  solo AMMINISTRATORI: stato, attivazione, lotto di prova
 *
 * Attivita' programmate (UTC): ogni 10 minuti il crawler prosegue il giro in
 * corso; alle 03:30 (04:30 o 05:30 in Italia) ne apre uno nuovo, completo la
 * domenica. Il crawler parte solo dopo l'attivazione esplicita (kv
 * crawler_attivo), cosi' il primo giro e' sempre una prova controllata.
 */
import { getContainer } from "@cloudflare/containers";
import { verifyAccessJwt } from "./access";
import type { Crawler } from "./crawler";
import { gestisciInterno } from "./interno";
import { gestisciContatto, type ContattoEnv } from "./contatto";
import { complete, stream, type LlmEnv } from "./llm";
import { isConfident, Retriever, type RetrieveEnv } from "./retrieve";
import { buildContext, NO_CONTEXT, publicSource, REWRITE_SYSTEM, SYSTEM, verify } from "./verify";

export interface Env extends RetrieveEnv, LlmEnv, ContattoEnv {
  ASSETS: Fetcher;
  ACCESS_TEAM: string;
  ACCESS_AUD: string;
  LLM_MODEL: string;
  LLM_MODEL_RIFORMULA: string;
  LIMITE_GIORNALIERO_UTENTE: string;
  LIMITE_GIORNALIERO_TOTALE: string;
  TURNSTILE_SITEKEY: string;
  CRAWLER: DurableObjectNamespace<Crawler>;
  /** email separate da virgola: possono avviare e fermare il crawler (segreto) */
  AMMINISTRATORI?: string;
  /** solo sviluppo locale (.dev.vars): utente fittizio quando Access non c'e' */
  DEV_UTENTE?: string;
}

export { Crawler } from "./crawler";
// necessario all'intercettazione delle richieste in uscita dal container
export { ContainerProxy } from "@cloudflare/containers";

const CRON_NOTTE = "30 3 * * *";

const SICUREZZA: Record<string, string> = {
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
  "strict-transport-security": "max-age=31536000",
};

function conIntestazioni(r: Response, extra: Record<string, string> = {}): Response {
  const out = new Response(r.body, r);
  for (const [k, v] of Object.entries({ ...SICUREZZA, ...extra })) out.headers.set(k, v);
  return out;
}

const jsonResp = (data: unknown, status = 200) =>
  conIntestazioni(new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  }));

const testo = (status: number, t: string) =>
  conIntestazioni(new Response(t, { status, headers: { "content-type": "text/plain; charset=utf-8" } }));

async function asset(env: Env, request: Request, path: string): Promise<Response> {
  return env.ASSETS.fetch(new Request(new URL(path, request.url), request));
}

/** Email dell'utente, verificata. Senza configurazione di Access il servizio
 *  resta CHIUSO, salvo l'utente fittizio dello sviluppo locale. */
async function utente(request: Request, env: Env): Promise<string | null> {
  if (!env.ACCESS_AUD || !env.ACCESS_TEAM) return env.DEV_UTENTE || null;
  return verifyAccessJwt(request.headers.get("cf-access-jwt-assertion"), { team: env.ACCESS_TEAM, aud: env.ACCESS_AUD });
}

const oggi = () => new Date().toISOString().slice(0, 10);

export default {
  async fetch(request, env, ctx): Promise<Response> {
    const url = new URL(request.url);
    const p = url.pathname;

    // ------------------------------------------------------------ pubblico
    if ((p === "/bot" || p === "/bot/" || p === "/bot.html") && request.method === "GET") {
      const r = await asset(env, request, "/bot.html");
      const html = (await r.text()).replace("__TURNSTILE_SITEKEY__", env.TURNSTILE_SITEKEY || "");
      return conIntestazioni(new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } }));
    }
    if (p === "/api/contatto" && request.method === "POST") {
      return conIntestazioni(await gestisciContatto(request, env));
    }
    if (p === "/robots.txt") return testo(200, "User-agent: *\nDisallow: /\nAllow: /bot\n");

    // solo sviluppo locale: il catalogo del crawler esposto per le prove
    // end-to-end senza container. In produzione ACCESS_AUD e' impostato e
    // questa rotta non esiste.
    if (p.startsWith("/__interno/") && env.DEV_UTENTE && !env.ACCESS_AUD) return gestisciInterno(request, env);

    // ------------------------------------------------------------ riservato
    const email = await utente(request, env);
    if (!email) return testo(403, "Accesso riservato.");

    if (p === "/" || p === "/index.html") return conIntestazioni(await asset(env, request, "/index.html"),
      { "cache-control": "no-store" });
    if (p === "/api/stato" && request.method === "GET") return stato(env, email);
    if (p === "/api/chat" && request.method === "POST") return chat(request, env, ctx, email);
    if (p.startsWith("/api/crawler/")) return amministrazione(request, env, email, p);
    if (p === "/admin" && request.method === "GET") {
      if (!amministratori(env).includes(email)) return testo(403, "Riservato all'amministratore.");
      return conIntestazioni(await asset(env, request, "/admin.html"), { "cache-control": "no-store" });
    }
    return testo(404, "Non trovato.");
  },
  async scheduled(event, env, ctx) {
    const attivo = await env.DB.prepare("SELECT v FROM kv WHERE k = 'crawler_attivo'").first<{ v: string }>();
    if (attivo?.v !== "1") return;
    const modo = event.cron === CRON_NOTTE ? "notte" : "continua";
    ctx.waitUntil(lotto(env, { modo }).then((r) => console.log(`crawler (${modo}):`, r)));
  },
} satisfies ExportedHandler<Env>;

// ------------------------------------------------------------------ crawler
const amministratori = (env: Env) =>
  (env.AMMINISTRATORI || env.DEV_UTENTE || "").toLowerCase().split(",").map((x) => x.trim()).filter(Boolean);

async function lotto(env: Env, corpo: Record<string, unknown>): Promise<string> {
  try {
    const r = await getContainer(env.CRAWLER, "unico").fetch(new Request("http://container/lotto", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(corpo),
    }));
    return await r.text();
  } catch (e) {
    console.error("crawler non raggiungibile", String(e));
    return JSON.stringify({ errore: String(e) });
  }
}

async function amministrazione(request: Request, env: Env, email: string, p: string): Promise<Response> {
  if (!amministratori(env).includes(email)) return testo(403, "Riservato all'amministratore.");

  if (p === "/api/crawler/stato" && request.method === "GET") {
    const [kv, perStato, giri] = await env.DB.batch([
      env.DB.prepare("SELECT k, v FROM kv"),
      env.DB.prepare("SELECT status, COUNT(*) AS n FROM documents WHERE gone = 0 GROUP BY status"),
      env.DB.prepare("SELECT started, finished, kind, outcome, stats_json FROM runs ORDER BY run_id DESC LIMIT 5"),
    ]);
    let container: unknown = null;
    if (new URL(request.url).searchParams.has("container")) {
      try {
        const r = await getContainer(env.CRAWLER, "unico").fetch(new Request("http://container/stato"));
        container = await r.json();
      } catch (e) { container = { errore: String(e) }; }
    }
    return jsonResp({
      kv: Object.fromEntries((kv.results as { k: string; v: string }[]).map((r) => [r.k, r.v])),
      documenti_per_stato: Object.fromEntries((perStato.results as { status: string; n: number }[]).map((r) => [r.status, r.n])),
      ultimi_giri: giri.results, container,
    });
  }
  if (request.method !== "POST") return testo(405, "Metodo non ammesso.");
  const corpo = (await request.json().catch(() => ({}))) as Record<string, unknown>;

  if (p === "/api/crawler/attiva") {
    const attivo = corpo.attivo === true ? "1" : "0";
    await env.DB.prepare("INSERT INTO kv (k, v) VALUES ('crawler_attivo', ?1) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
      .bind(attivo).run();
    return jsonResp({ crawler_attivo: attivo === "1" });
  }
  if (p === "/api/crawler/lotto") {
    // lotto manuale, anche a crawler disattivato: e' cosi' che si fa la prova
    const limite = Math.min(Math.max(Number(corpo.limite) || 200, 1), 2000);
    return jsonResp(JSON.parse(await lotto(env, { modo: "prova", limite })));
  }
  return testo(404, "Non trovato.");
}

// ------------------------------------------------------------------ stato
async function stato(env: Env, email: string): Promise<Response> {
  const [docs, chunks, esclusi, run, uso] = await env.DB.batch([
    env.DB.prepare("SELECT COUNT(*) AS n FROM documents WHERE gone = 0 AND status = 'indexed'"),
    env.DB.prepare("SELECT COUNT(*) AS n FROM chunks"),
    env.DB.prepare("SELECT COUNT(*) AS n FROM documents WHERE status = 'excluded'"),
    env.DB.prepare("SELECT finished, outcome FROM runs WHERE finished IS NOT NULL AND outcome = 'ok' ORDER BY finished DESC LIMIT 1"),
    env.DB.prepare("SELECT domande FROM uso WHERE email = ?1 AND giorno = ?2").bind(email, oggi()),
  ]);
  const n = (r: D1Result) => (r.results[0] as { n: number } | undefined)?.n ?? 0;
  const ultimo = run.results[0] as { finished: number } | undefined;
  return jsonResp({
    utente: email,
    indicizzati: n(docs),
    chunk: n(chunks),
    esclusi_dati_personali: n(esclusi),
    ultimo_aggiornamento: ultimo ? new Date(ultimo.finished * 1000).toISOString() : null,
    domande_oggi: (uso.results[0] as { domande: number } | undefined)?.domande ?? 0,
    limite_giornaliero: Number(env.LIMITE_GIORNALIERO_UTENTE) || 60,
    modello: env.LLM_MODEL,
  });
}

// ------------------------------------------------------------------- chat
interface Msg { role: "user" | "assistant"; content: string }

function leggiRichiesta(b: unknown): { question: string; history: Msg[] } | null {
  if (!b || typeof b !== "object") return null;
  const o = b as { question?: unknown; history?: unknown };
  if (typeof o.question !== "string") return null;
  const question = o.question.trim().slice(0, 2000);
  if (question.length < 3) return null;
  const history = (Array.isArray(o.history) ? o.history : [])
    .slice(-6)
    .filter((m): m is Msg => !!m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
    .map((m) => ({ role: m.role, content: m.content.slice(0, 2000) }));
  return { question, history };
}

/** Conta la domanda e applica i limiti: per persona, e totale come tetto di spesa. */
async function contaDomanda(env: Env, email: string): Promise<string | null> {
  const giorno = oggi();
  const [mie, tutte] = await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO uso (email, giorno, domande) VALUES (?1, ?2, 1)
       ON CONFLICT (email, giorno) DO UPDATE SET domande = domande + 1
       RETURNING domande`,
    ).bind(email, giorno),
    env.DB.prepare("SELECT COALESCE(SUM(domande), 0) AS n FROM uso WHERE giorno = ?1").bind(giorno),
  ]);
  const n = (mie.results[0] as { domande: number }).domande;
  const tot = (tutte.results[0] as { n: number }).n;
  if (n > (Number(env.LIMITE_GIORNALIERO_UTENTE) || 60))
    return "Hai raggiunto il limite di domande per oggi. Il conteggio riparte a mezzanotte (UTC).";
  if (tot > (Number(env.LIMITE_GIORNALIERO_TOTALE) || 300))
    return "Il servizio ha raggiunto il limite giornaliero complessivo. Riprova domani.";
  return null;
}

async function riformula(env: Env, question: string, history: Msg[]): Promise<string> {
  if (!history.length) return question;
  const convo = history
    .map((m) => `${m.role === "user" ? "Utente" : "Assistente"}: ${m.content.slice(0, 600)}`)
    .join("\n");
  try {
    const out = await complete(env, {
      model: env.LLM_MODEL_RIFORMULA || env.LLM_MODEL,
      system: REWRITE_SYSTEM,
      user: `${convo}\n\nUltima domanda dell'utente: ${question}\n\nDomanda riscritta:`,
      maxTokens: 200,
    });
    return out.trim().split("\n")[0].slice(0, 500) || question;
  } catch (e) {
    console.error("riformulazione fallita", String(e));
    return question;
  }
}

async function chat(request: Request, env: Env, ctx: ExecutionContext, email: string): Promise<Response> {
  let body: unknown;
  try { body = await request.json(); } catch { return testo(400, "Richiesta non valida."); }
  const req = leggiRichiesta(body);
  if (!req) return testo(400, "Domanda mancante o troppo breve.");
  const limite = await contaDomanda(env, email);
  if (limite) return testo(429, limite);

  const { readable, writable } = new TransformStream();
  const w = writable.getWriter();
  const enc = new TextEncoder();
  const invia = (event: string, data: unknown) =>
    w.write(enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));

  const lavoro = (async () => {
    const t0 = Date.now();
    const lat = () => Math.round((Date.now() - t0) / 100) / 10;
    try {
      const r = new Retriever(env);
      const q = await riformula(env, req.question, req.history);
      const res = await r.search(q);
      const [ok, reason] = isConfident(res, r.minRelevance);
      if (!ok) {
        console.log("astensione:", reason);
        await invia("sources", []);
        await invia("delta", NO_CONTEXT);
        await invia("done", { answered: false, confidence: "nessuna", warnings: res.degraded, cited: [],
          reason, latency: lat() });
        return;
      }
      const hits = await r.selectContext(res);
      const { context, sources } = buildContext(hits);
      await invia("sources", sources.map(publicSource));

      // al modello va la domanda RISCRITTA: con quella grezza ("E la
      // scadenza?") doveva indovinare il soggetto dagli estratti
      const user =
        `${context}\n\nDOMANDA: ${q}\n` +
        (q !== req.question ? `(formulazione originale dell'utente: «${req.question}»)\n` : "") +
        "\nRispondi seguendo alla lettera le regole ricevute: solo cio' che risulta dagli estratti, " +
        "con il riferimento [n] su ogni affermazione.";

      let raw = "";
      let interrotta = false;
      try {
        for await (const t of stream(env, { model: env.LLM_MODEL, system: SYSTEM, user })) {
          raw += t;
          await invia("delta", t);
        }
      } catch (e) {
        console.error("generazione interrotta", String(e));
        interrotta = true;
      }

      if (interrotta && !raw.trim()) {
        // nessun testo: si mostrano gli estratti trovati, senza fingere una risposta
        const msg = "Il servizio di generazione non e' al momento raggiungibile. " +
          "Gli estratti pertinenti trovati sono elencati a lato.";
        await invia("final", msg);
        await invia("done", { answered: false, confidence: "nessuna", warnings: res.degraded, cited: [],
          reason, latency: lat(), question_used: q });
        return;
      }

      // il testo definitivo e' quello passato dalla verifica
      const v = verify(raw, sources, q);
      let confidence = v.confidence;
      const warnings = [...res.degraded, ...v.warnings];
      if (res.degraded.length && confidence === "alta") confidence = "bassa";
      if (interrotta) {
        warnings.unshift("Generazione interrotta: la risposta e' incompleta.");
        if (confidence !== "rifiuto") confidence = "bassa";
      }
      await invia("final", v.text);
      await invia("done", { answered: confidence !== "rifiuto", confidence, warnings,
        cited: sources.filter((s) => s.cited).map((s) => s.n), reason, latency: lat(), question_used: q });
    } catch (e) {
      console.error("errore nella risposta", String(e));
      await invia("delta", "\n\nErrore interno: la risposta non e' stata completata.").catch(() => {});
      await invia("done", { answered: false, confidence: "nessuna", warnings: ["Errore interno del servizio."],
        cited: [], latency: lat() }).catch(() => {});
    } finally {
      await w.close().catch(() => {});
    }
  })();
  ctx.waitUntil(lavoro);

  return conIntestazioni(new Response(readable, {
    headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-store" },
  }));
}
