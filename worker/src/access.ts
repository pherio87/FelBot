/**
 * Identita' da Cloudflare Access.
 *
 * Access sta davanti al Worker e lascia passare solo chi ha fatto il login;
 * ad ogni richiesta aggiunge un JWT firmato (Cf-Access-Jwt-Assertion). Il
 * Worker lo VERIFICA comunque — firma, audience, emittente, scadenza — per
 * due ragioni: se un giorno la rotta fosse esposta senza Access (un indirizzo
 * workers.dev riacceso, una regola cambiata) il servizio resta chiuso invece
 * che aperto; e serve l'email verificata per i limiti d'uso per persona.
 */

export interface AccessConfig {
  team: string; // "<team>" di <team>.cloudflareaccess.com
  aud: string;  // Application Audience (AUD) Tag
}

interface Jwk extends JsonWebKey { kid: string }
type JwksFetcher = (url: string) => Promise<{ keys: Jwk[] }>;

const b64url = (s: string) => {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
};
const json = (s: string) => JSON.parse(new TextDecoder().decode(b64url(s)));

let cache: { url: string; keys: Jwk[]; until: number } | null = null;

const defaultFetcher: JwksFetcher = async (url) => {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`JWKS ${r.status}`);
  return r.json();
};

async function chiavi(url: string, fetcher: JwksFetcher, forza = false): Promise<Jwk[]> {
  if (!forza && cache && cache.url === url && cache.until > Date.now()) return cache.keys;
  const { keys } = await fetcher(url);
  cache = { url, keys, until: Date.now() + 3600_000 };
  return keys;
}

/** Restituisce l'email verificata, oppure null se il token non e' valido. */
export async function verifyAccessJwt(
  token: string | null,
  cfg: AccessConfig,
  fetcher: JwksFetcher = defaultFetcher,
  now: number = Date.now() / 1000,
): Promise<string | null> {
  if (!token || !cfg.team || !cfg.aud) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  let header: any, payload: any;
  try {
    header = json(parts[0]);
    payload = json(parts[1]);
  } catch {
    return null;
  }
  if (header.alg !== "RS256" || !header.kid) return null;

  const issuer = `https://${cfg.team}.cloudflareaccess.com`;
  const url = `${issuer}/cdn-cgi/access/certs`;
  let keys = await chiavi(url, fetcher);
  let jwk = keys.find((k) => k.kid === header.kid);
  if (!jwk) {
    // chiavi ruotate da Access: si ricaricano una volta
    keys = await chiavi(url, fetcher, true);
    jwk = keys.find((k) => k.kid === header.kid);
    if (!jwk) return null;
  }
  const key = await crypto.subtle.importKey(
    "jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"],
  );
  const ok = await crypto.subtle.verify(
    "RSASSA-PKCS1-v1_5", key, b64url(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
  );
  if (!ok) return null;

  const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!aud.includes(cfg.aud)) return null;
  if (payload.iss !== issuer) return null;
  if (typeof payload.exp !== "number" || payload.exp < now) return null;
  if (typeof payload.nbf === "number" && payload.nbf > now + 60) return null;
  if (typeof payload.email !== "string" || !payload.email.includes("@")) return null;
  return payload.email.toLowerCase();
}

/** Solo per i test: svuota la cache delle chiavi. */
export const _resetCache = () => { cache = null; };
