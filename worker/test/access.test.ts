import { beforeEach, describe, expect, it } from "vitest";
import { _resetCache, verifyAccessJwt } from "../src/access";
import { componiEmail, gestisciContatto, valida } from "../src/contatto";

const enc = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
const cfg = { team: "pherio", aud: "aud-felbot" };
const now = 1_800_000_000;

async function chiave() {
  const kp = (await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true, ["sign", "verify"],
  )) as CryptoKeyPair;
  const jwk = { ...(await crypto.subtle.exportKey("jwk", kp.publicKey) as JsonWebKey), kid: "k1" } as any;
  return { kp, jwk };
}

async function firma(kp: CryptoKeyPair, payload: object, kid = "k1") {
  const h = enc({ alg: "RS256", kid, typ: "JWT" });
  const p = enc(payload);
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", kp.privateKey, new TextEncoder().encode(`${h}.${p}`));
  return `${h}.${p}.${Buffer.from(sig).toString("base64url")}`;
}

const buono = { aud: ["aud-felbot"], iss: "https://pherio.cloudflareaccess.com", exp: now + 600,
  iat: now, email: "Chris@Example.org" };

describe("Cloudflare Access", () => {
  beforeEach(() => _resetCache());

  it("accetta un token valido e restituisce l'email in minuscolo", async () => {
    const { kp, jwk } = await chiave();
    const t = await firma(kp, buono);
    expect(await verifyAccessJwt(t, cfg, async () => ({ keys: [jwk] }), now)).toBe("chris@example.org");
  });

  it("rifiuta: audience sbagliata, emittente sbagliato, scaduto, firma altrui, assente", async () => {
    const { kp, jwk } = await chiave();
    const altra = await chiave();
    const f = async () => ({ keys: [jwk] });
    expect(await verifyAccessJwt(await firma(kp, { ...buono, aud: ["altra-app"] }), cfg, f, now)).toBeNull();
    expect(await verifyAccessJwt(await firma(kp, { ...buono, iss: "https://evil.cloudflareaccess.com" }), cfg, f, now)).toBeNull();
    expect(await verifyAccessJwt(await firma(kp, { ...buono, exp: now - 1 }), cfg, f, now)).toBeNull();
    expect(await verifyAccessJwt(await firma(altra.kp, buono), cfg, f, now)).toBeNull();
    expect(await verifyAccessJwt(null, cfg, f, now)).toBeNull();
    expect(await verifyAccessJwt("a.b.c", cfg, f, now)).toBeNull();
  });

  it("rifiuta un token non firmato (alg none)", async () => {
    const t = `${enc({ alg: "none", kid: "k1" })}.${enc(buono)}.`;
    expect(await verifyAccessJwt(t, cfg, async () => ({ keys: [] }), now)).toBeNull();
  });

  it("senza configurazione di Access il servizio resta chiuso", async () => {
    const { kp, jwk } = await chiave();
    expect(await verifyAccessJwt(await firma(kp, buono), { team: "", aud: "" }, async () => ({ keys: [jwk] }), now))
      .toBeNull();
  });
});

describe("modulo di contatto", () => {
  const form = (o: Record<string, string>) => {
    const f = new FormData();
    for (const [k, v] of Object.entries(o)) f.set(k, v);
    return f;
  };
  it("campo trappola compilato → scartato in silenzio", () =>
    expect(valida(form({ email: "a@b.it", messaggio: "messaggio di prova", sito_web: "x" }))).toMatchObject({ trappola: true }));
  it("intestazioni iniettate nell'indirizzo → rifiutato", () =>
    expect("errore" in valida(form({ email: "a@b.it\r\nBcc: x@y.it", messaggio: "messaggio di prova" }))).toBe(true));
  it("messaggio valido → email ben formata, mittente in Reply-To", () => {
    const m = valida(form({ email: "admin@regione.example", ente: "Servizio\r\nX", messaggio: "Rallentate il crawler, grazie." }));
    expect("errore" in m).toBe(false);
    const raw = componiEmail(m as any, "felbot@pherio.net", "dest@example.org", new Date(Date.UTC(2026, 10, 10)));
    expect(raw).toContain("Reply-To: <admin@regione.example>");
    expect(raw).toContain("Content-Transfer-Encoding: base64");
    const [testa, corpo] = raw.split("\r\n\r\n");
    expect(testa.split("\r\n").every((l) => /^[A-Za-z-]+: /.test(l))).toBe(true);
    expect(Buffer.from(corpo.replace(/\r\n/g, ""), "base64").toString("utf8")).toContain("Rallentate il crawler");
  });
  it("corpo che non e' un modulo → 400, non un'eccezione", async () => {
    const env = { TURNSTILE_SECRET: "s", CONTATTO_DESTINATARIO: "dest@example.org" } as any;
    const req = new Request("https://felbot.pherio.net/api/contatto", {
      method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    expect((await gestisciContatto(req, env)).status).toBe(400);
  });
});
