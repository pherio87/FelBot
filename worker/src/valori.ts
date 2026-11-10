/**
 * Estrazione e normalizzazione dei VALORI da confrontare con le fonti.
 *
 * Porting fedele di src/fler/valori.py: stessa logica, stessi test
 * (test/valori.test.ts e test/verify.test.ts riproducono quelli Python).
 *
 * Il principio: per le FONTI si raccoglie un insieme generoso (ogni lettura
 * plausibile di ogni numero); per la RISPOSTA si richiede che ogni valore
 * affermato abbia almeno una lettura interamente presente nella fonte
 * attribuita.
 *
 * Nota sul porting: in JavaScript `\b` e `\w` riconoscono solo caratteri
 * ASCII anche con il flag `u`, mentre in Python sono Unicode. Al loro posto
 * si usano confini espliciti (INIZIO/FINE) su lettere e cifre Unicode: con
 * `\b`, "è30" o "à 5" si comporterebbero diversamente dall'originale.
 */

const LN = "[\\p{L}\\p{N}_]";
const INIZIO = `(?<!${LN})`;
const FINE = `(?!${LN})`;

// Un numero seguito da una di queste e' un VALORE da verificare anche se ha
// una sola cifra: "5 giorni", "6 mesi", "3%" sono esattamente cio' che conta.
const UNITA =
  "(?:giorn[oi](?:\\s+(?:lavorativ[oi]|natural[ei]|consecutiv[ie]|solar[ei]))?|gg\\.?|" +
  "mes[ei]|ann[oi]|settiman[ae]|or[ae]|h\\b|minut[oi]|" +
  "euro|eur\\b|€|%|per\\s*cento|percento|" +
  "credit[oi]|cfu|punt[oi]|allievi|partecipanti|post[oi]|destinatari|" +
  "edizion[ei]|modul[oi]|annualit[aà]|rate|mensilit[aà])";

// ------------------------------------------------------------------ date
const MESI: Record<string, number> = {
  gennaio: 1, febbraio: 2, marzo: 3, aprile: 4, maggio: 5, giugno: 6,
  luglio: 7, agosto: 8, settembre: 9, ottobre: 10, novembre: 11, dicembre: 12,
};
const MESE_RE = Object.keys(MESI).join("|");
const DATA_NUM = new RegExp(`${INIZIO}(\\d{1,2})[/.\\-](\\d{1,2})[/.\\-](\\d{4}|\\d{2})${FINE}`, "gu");
const DATA_ISO = new RegExp(`${INIZIO}(\\d{4})-(\\d{2})-(\\d{2})${FINE}`, "gu");
const DATA_TESTO = new RegExp(
  `${INIZIO}(\\d{1,2})\\s*°?\\s+(${MESE_RE})(?:\\s+(\\d{4}))?${FINE}`, "giu");

const p2 = (n: number) => String(n).padStart(2, "0");

function dataToken(g: number, m: number, a: number | null): string[] {
  if (!(g >= 1 && g <= 31 && m >= 1 && m <= 12)) return [];
  const out = [`DM:${p2(m)}-${p2(g)}`];
  if (a !== null) {
    if (a < 100) a += a < 70 ? 2000 : 1900;
    out.push(`D:${String(a).padStart(4, "0")}-${p2(m)}-${p2(g)}`);
  }
  return out;
}

type Span = { s: number; e: number; tok: string[] };

function trovaDate(text: string): Span[] {
  const out: Span[] = [];
  for (const m of text.matchAll(DATA_ISO))
    out.push({ s: m.index!, e: m.index! + m[0].length, tok: dataToken(+m[3], +m[2], +m[1]) });
  for (const m of text.matchAll(DATA_NUM))
    out.push({ s: m.index!, e: m.index! + m[0].length, tok: dataToken(+m[1], +m[2], +m[3]) });
  for (const m of text.matchAll(DATA_TESTO))
    out.push({
      s: m.index!, e: m.index! + m[0].length,
      tok: dataToken(+m[1], MESI[m[2].toLowerCase()], m[3] ? +m[3] : null),
    });
  // togli le sovrapposizioni, tenendo la piu' lunga
  out.sort((a, b) => a.s - b.s || (b.e - b.s) - (a.e - a.s));
  const finale: Span[] = [];
  let fine = -1;
  for (const x of out) {
    if (x.s >= fine && x.tok.length) {
      finale.push(x);
      fine = x.e;
    }
  }
  return finale;
}

// ------------------------------------------------------ numeri in lettere
const UNITA_P: Record<string, number> = {
  zero: 0, uno: 1, un: 1, una: 1, due: 2, tre: 3, quattro: 4, cinque: 5,
  sei: 6, sette: 7, otto: 8, nove: 9, dieci: 10, undici: 11, dodici: 12,
  tredici: 13, quattordici: 14, quindici: 15, sedici: 16, diciassette: 17,
  diciotto: 18, diciannove: 19,
};
const DECINE: Record<string, number> = {
  venti: 20, trenta: 30, quaranta: 40, cinquanta: 50, sessanta: 60,
  settanta: 70, ottanta: 80, novanta: 90,
};
const ARTICOLI = new Set(["un", "una", "zero"]);

function sottoMille(w: string): number | null {
  if (!w) return 0;
  if (w.startsWith("cento")) {
    const r = sottoMille(w.slice(5));
    return r === null ? null : 100 + r;
  }
  for (const [k, v] of Object.entries(UNITA_P)) {
    if (ARTICOLI.has(k)) continue;
    if (w.startsWith(k + "cento")) {
      const r = sottoMille(w.slice(k.length + 5));
      return r === null || v < 2 ? null : v * 100 + r;
    }
  }
  if (w in UNITA_P && w !== "un" && w !== "una") return UNITA_P[w];
  for (const [k, v] of Object.entries(DECINE)) {
    const stem = k.slice(0, -1); // "trenta" -> "trent" (trentuno, trentotto)
    for (const pref of [k, stem]) {
      if (w.startsWith(pref)) {
        const resto = w.slice(pref.length);
        if (resto === "") return v;
        if (resto === "tre" || resto === "tré") return v + 3;
        if (resto in UNITA_P && !ARTICOLI.has(resto) && UNITA_P[resto] < 10) return v + UNITA_P[resto];
      }
    }
  }
  return null;
}

/** 'trenta' -> 30, 'centoventi' -> 120, 'duemilacinquecento' -> 2500.
 *  Esclude 'un', 'uno', 'una' isolati: sono quasi sempre articoli. */
export function parolaANumero(word: string): number | null {
  const w = word.toLowerCase().trim();
  if (w === "un" || w === "uno" || w === "una" || w.length < 3) return null;
  if (w === "mille") return 1000;
  if (w.startsWith("mille")) {
    const r = sottoMille(w.slice(5));
    return r === null ? null : 1000 + r;
  }
  const i = w.indexOf("mila");
  if (i >= 0) {
    const t = sottoMille(w.slice(0, i));
    const c = sottoMille(w.slice(i + 4));
    if (t === null || c === null || t < 2) return null;
    return t * 1000 + c;
  }
  return sottoMille(w);
}

const PAROLA_NUM = new RegExp(`${INIZIO}([a-zà-ù]{3,30})${FINE}`, "giu");

// ------------------------------------------------------------------ numeri
// 1) migliaia con punto: 250.000 / 1.250.000,50
const NUM_PUNTO = "\\d{1,3}(?:\\.\\d{3})+(?:,\\d+)?";
// 2) migliaia con spazio (anche non separabile): 250 000 — ambiguo con una
//    riga di tabella "300 400 600"
const NUM_SPAZIO = "\\d{1,3}(?:[ \\u00a0\\u202f]\\d{3})+(?:,\\d+)?";
// 3) numero semplice, eventualmente decimale: 30 / 2,5 / 4.2
const NUM_SEMPLICE = "\\d+(?:[.,]\\d+)?";
const NUMERO = new RegExp(`(?<!\\d)(?<!\\d[.,])(?:${NUM_PUNTO}|${NUM_SPAZIO}|${NUM_SEMPLICE})${FINE}`, "gu");
const DOPO_UNITA = new RegExp(`^\\s*(?:${UNITA})(?![a-zà-ù])`, "iu");
const PRIMA_EURO = /(?:€|euro|eur)\s*$/iu;
const SPAZI = /[   ]/;

function norm(tok: string): string {
  let s = tok.replace(/[   ]/g, "");
  if (s.includes(",")) s = s.replace(/\./g, "").replace(/,/g, ".");
  else if (/^\d{1,3}(?:\.\d{3})+$/.test(s)) s = s.replace(/\./g, "");
  const f = Number(s);
  if (!Number.isFinite(f) || s === "") return s;
  if (Number.isInteger(f)) return BigInt(Math.trunc(f)).toString();
  return String(Math.round(f * 1e6) / 1e6);
}

function letture(tok: string): Set<string>[] {
  if (SPAZI.test(tok)) {
    const parti = tok.split(/[   ]/);
    return [new Set([norm(tok)]), new Set(parti.map(norm))];
  }
  return [new Set([norm(tok)])];
}

/** Un valore affermato nella risposta, con le sue letture alternative. */
export class Valore {
  constructor(public testo: string, public letture: Set<string>[]) {}
  confermato(pool: Set<string>): boolean {
    return this.letture.some((l) => [...l].every((x) => pool.has(x)));
  }
}

/** Tutto cio' che un estratto puo' confermare: insieme generoso. */
export function valoriFonte(text: string): Set<string> {
  const out = new Set<string>();
  for (const d of trovaDate(text)) d.tok.forEach((t) => out.add(t));
  for (const m of text.matchAll(NUMERO)) {
    for (const l of letture(m[0])) l.forEach((t) => out.add(t));
    // le parti di una data o di un numero composto valgono anche da sole
    for (const p of m[0].match(/\d+/g) ?? []) out.add(norm(p));
  }
  for (const m of text.matchAll(PAROLA_NUM)) {
    const n = parolaANumero(m[1]);
    if (n !== null) out.add(String(n));
  }
  return out;
}

/** I valori che la risposta AFFERMA e che vanno quindi trovati in una fonte. */
export function valoriRisposta(text: string): Valore[] {
  const out: Valore[] = [];
  // le date si controllano intere, poi si tolgono dal testo
  let resto = "";
  let ultimo = 0;
  for (const d of trovaDate(text)) {
    const piena = d.tok.filter((t) => t.startsWith("D:"));
    out.push(new Valore(text.slice(d.s, d.e), [new Set([(piena.length ? piena : d.tok)[0]])]));
    resto += text.slice(ultimo, d.s) + " § ";
    ultimo = d.e;
  }
  resto += text.slice(ultimo);

  for (const m of resto.matchAll(NUMERO)) {
    const tok = m[0];
    const fine = m.index! + tok.length;
    const significative = tok.replace(/\D/g, "").replace(/^0+/, "").length;
    const conUnita =
      DOPO_UNITA.test(resto.slice(fine, fine + 30)) ||
      PRIMA_EURO.test(resto.slice(Math.max(0, m.index! - 6), m.index!));
    if (significative >= 2 || conUnita) out.push(new Valore(tok, letture(tok)));
  }

  for (const m of resto.matchAll(PAROLA_NUM)) {
    const n = parolaANumero(m[1]);
    if (n === null) continue;
    // un numero in lettere e' un valore solo se seguito da un'unita':
    // "tre giorni" si', "le tre condizioni" no
    const fine = m.index! + m[0].length;
    if (DOPO_UNITA.test(resto.slice(fine, fine + 30))) out.push(new Valore(m[1], [new Set([String(n)])]));
  }
  return out;
}
