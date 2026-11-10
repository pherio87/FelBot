/**
 * Modulo di contatto della pagina /bot: chi gestisce un sito che vede FelBot
 * nei log puo' scrivere senza che l'indirizzo del gestore sia esposto.
 *
 * Difese, in ordine: campo trappola, validazione, Turnstile, limite per
 * indirizzo (5 l'ora, impronta SHA-256 dell'IP, cancellata dopo un giorno).
 * L'email parte con Email Routing verso CONTATTO_DESTINATARIO (segreto).
 */

export interface ContattoEnv {
  DB: D1Database;
  EMAIL: SendEmail;
  TURNSTILE_SECRET: string;
  CONTATTO_MITTENTE: string;
  CONTATTO_DESTINATARIO: string;
}

// niente spazi, a capo, virgolette, parentesi angolari: un indirizzo valido e
// nient'altro, perche' finisce nell'intestazione Reply-To
const EMAIL_RE = /^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$/;

export interface Messaggio {
  email: string;
  ente: string;
  messaggio: string;
}

export function valida(form: FormData): Messaggio | { errore: string; trappola?: boolean } {
  if (String(form.get("sito_web") ?? "").trim()) return { errore: "trappola", trappola: true };
  const email = String(form.get("email") ?? "").trim();
  const ente = String(form.get("ente") ?? "").trim().slice(0, 200);
  const messaggio = String(form.get("messaggio") ?? "").trim();
  if (!EMAIL_RE.test(email)) return { errore: "indirizzo email non valido" };
  if (messaggio.length < 10 || messaggio.length > 4000) return { errore: "messaggio troppo breve o troppo lungo" };
  return { email, ente, messaggio };
}

function b64(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/** Messaggio RFC 5322 in text/plain UTF-8, corpo in base64 a righe di 76. */
export function componiEmail(m: Messaggio, da: string, a: string, quando = new Date()): string {
  const oggetto = `FelBot — messaggio dal modulo di contatto${m.ente ? ` (${m.ente.replace(/[\r\n]+/g, " ").slice(0, 80)})` : ""}`;
  const corpo =
    `Messaggio inviato dalla pagina https://felbot.pherio.net/bot\n\n` +
    `Da: ${m.email}\nEnte o sito: ${m.ente || "—"}\nData: ${quando.toISOString()}\n\n` +
    `${m.messaggio}\n\n— \nRispondendo a questa email si risponde al mittente.`;
  const dominio = da.split("@")[1] || "localhost";
  return [
    `From: FelBot <${da}>`,
    `To: <${a}>`,
    `Reply-To: <${m.email}>`,
    `Subject: =?UTF-8?B?${b64(oggetto)}?=`,
    `Message-ID: <${crypto.randomUUID()}@${dominio}>`,
    `Date: ${quando.toUTCString().replace("GMT", "+0000")}`,
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=UTF-8",
    "Content-Transfer-Encoding: base64",
    "",
    (b64(corpo).match(/.{1,76}/g) ?? []).join("\r\n"),
    "",
  ].join("\r\n");
}

async function sha256(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const risposta = (status: number, testo: string) =>
  new Response(testo, { status, headers: { "content-type": "text/plain; charset=utf-8" } });

export async function gestisciContatto(request: Request, env: ContattoEnv): Promise<Response> {
  if (!env.TURNSTILE_SECRET || !env.CONTATTO_DESTINATARIO)
    return risposta(503, "Modulo di contatto non ancora configurato.");

  const form = await request.formData();
  const v = valida(form);
  // al bot che ha compilato la trappola si risponde "ok": non deve imparare nulla
  if ("errore" in v) return v.trappola ? risposta(200, "ok") : risposta(400, v.errore);

  const ip = request.headers.get("cf-connecting-ip") ?? "";
  const ts = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    body: new URLSearchParams({
      secret: env.TURNSTILE_SECRET,
      response: String(form.get("cf-turnstile-response") ?? ""),
      remoteip: ip,
    }),
  }).then((r) => r.json() as Promise<{ success: boolean }>).catch(() => ({ success: false }));
  if (!ts.success) return risposta(403, "verifica anti-bot non superata");

  const impronta = await sha256(`felbot-contatto|${ip}`);
  const ora = Date.now() / 1000;
  const [, conteggio] = await env.DB.batch([
    env.DB.prepare("DELETE FROM contatti WHERE ts < ?1").bind(ora - 86400),
    env.DB.prepare("SELECT COUNT(*) AS n FROM contatti WHERE ip_hash = ?1 AND ts > ?2").bind(impronta, ora - 3600),
  ]);
  if (((conteggio.results[0] as { n: number })?.n ?? 0) >= 5) return risposta(429, "troppi messaggi, riprovare fra un'ora");
  await env.DB.prepare("INSERT INTO contatti (ip_hash, ts) VALUES (?1, ?2)").bind(impronta, ora).run();

  const { EmailMessage } = await import("cloudflare:email");
  const raw = componiEmail(v, env.CONTATTO_MITTENTE, env.CONTATTO_DESTINATARIO);
  try {
    await env.EMAIL.send(new EmailMessage(env.CONTATTO_MITTENTE, env.CONTATTO_DESTINATARIO, raw));
  } catch (e) {
    console.error("invio email del modulo di contatto fallito", String(e));
    return risposta(502, "invio non riuscito");
  }
  return risposta(200, "ok");
}
