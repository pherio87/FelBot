/**
 * Riferimenti agli atti e query full-text sicure. Porting di retrieve.py.
 *
 * - Un riferimento d'atto e' una COPPIA (numero, anno), non un numero sciolto:
 *   "aggiornati al 2024" non deve cercare "2024", che compare nel 17% dei
 *   frammenti.
 * - I due numeri di un atto si cercano in prossimita' (NEAR), non in AND
 *   sparso: "17" AND "2005" selezionava il 3,8% del corpus.
 * - La query dell'utente non arriva mai a FTS5 come sintassi: ogni termine
 *   e' racchiuso fra virgolette.
 */

const LN = "[\\p{L}\\p{N}_]";
const I = `(?<!${LN})`; // equivalente Unicode di \b in apertura
const F = `(?!${LN})`;  // ... e in chiusura

export interface ActRef {
  kind: string;
  num: string;
  year: string;
}
export const refToString = (r: ActRef) => `${r.kind} ${r.num}/${r.year}`;

const REF_PATTERNS: [string, RegExp][] = [
  ["DGR", new RegExp(`${I}(?:d\\.?\\s?g\\.?\\s?r\\.?|delibera(?:zione)?(?:\\s+di\\s+giunta)?)\\s*` +
    `(?:n\\.?\\s*)?(\\d{1,5})\\s*(?:/|\\s+del\\s+|\\s+dell['’]\\s*)\\s*(\\d{2,4})`, "giu")],
  ["DD", new RegExp(`${I}(?:d\\.?\\s?d\\.?|determina(?:zione)?(?:\\s+dirigenziale)?)\\s*` +
    `(?:n\\.?\\s*)?(\\d{1,6})\\s*(?:/|\\s+del\\s+)\\s*(\\d{2,4})`, "giu")],
  ["LR", new RegExp(`${I}(?:l\\.?\\s?r\\.?|legge\\s+regionale)\\s*` +
    `(?:n\\.?\\s*)?(\\d{1,3})\\s*(?:/|\\s+del\\s+)\\s*(\\d{2,4})`, "giu")],
  ["PG", new RegExp(`${I}PG\\.?\\s*(\\d{4})\\.?(\\d{4,8})${F}`, "giu")],
];
// protocolli senza anno: "prot. n. 1096197"
const PROT = new RegExp(`${I}(?:prot(?:ocollo)?\\.?|nota)\\s*(?:n\\.?\\s*)?(\\d{5,10})${F}`, "giu");
// numero d'atto nudo ma introdotto da un marcatore: "la delibera 1298"
const BARE_ACT = new RegExp(
  `${I}(?:dgr|dd|l\\.?r\\.?|delibera(?:zione)?|determina(?:zione)?|atto|avviso|bando)\\s*` +
  `(?:n\\.?\\s*)?(\\d{3,7})${F}`, "giu");
const ANNO = /^(19|20)\d{2}$/;
// un numero seguito da un'unita' di misura NON e' un estremo d'atto
const UNITA = new RegExp(`${I}\\d{2,7}\\s*(?:ore|or[ae]|euro|€|%|allievi|giorni|mesi|posti|crediti|cfu)${F}`, "iu");

export function extractRefs(q: string): ActRef[] {
  const refs: ActRef[] = [];
  const seen = new Set<string>();
  const add = (kind: string, num: string, year: string) => {
    if (year.length === 2) year = (+year > 50 ? "19" : "20") + year;
    const key = `${num.replace(/^0+/, "") || num}|${year}`;
    if (!seen.has(key)) {
      seen.add(key);
      refs.push({ kind, num, year });
    }
  };
  for (const [kind, pat] of REF_PATTERNS) {
    for (const m of q.matchAll(pat)) {
      if (kind === "PG") add(kind, m[2], m[1]); // PG.<anno>.<numero>
      else add(kind, m[1], m[2]);
    }
  }
  for (const m of q.matchAll(PROT)) {
    const key = `${m[1]}|`;
    if (!seen.has(key)) {
      seen.add(key);
      refs.push({ kind: "PROT", num: m[1], year: "" });
    }
  }
  if (!refs.length) {
    for (const m of q.matchAll(BARE_ACT)) {
      const n = m[1];
      if (ANNO.test(n) || UNITA.test(m[0])) continue;
      const key = `${n}|`;
      if (!seen.has(key)) {
        seen.add(key);
        refs.push({ kind: "ATTO", num: n, year: "" });
      }
    }
  }
  return refs.slice(0, 6);
}

const STOP = new Set(
  ("il lo la i gli le un uno una di a da in con su per tra fra del dello della dei degli " +
    "delle al allo alla ai agli alle dal dalla dalle dagli nel nella nelle negli nei sul sulla sulle " +
    "e ed o oppure che chi cosa come quando dove quale quali quanto quanta quante quanti se non piu " +
    "si sono essere avere posso puo possono devo deve devono va vanno ci vi ne mi ti questo questa " +
    "questi queste qual anche ogni presso circa solo sempre mai gia ancora poi cioe ovvero perche " +
    "quindi mentre invece pero dunque affinche nonche essa esso loro suo sua mio mia nostro " +
    "nostra vostro vostra tutto tutti tutte tutta").split(" "),
);
const SIGLE = new Set(["pg", "dd", "lr", "ue", "fp"]);
const senzaAccenti = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "");

/** Query FTS5 sicura: niente sintassi iniettabile dall'utente. I numeri e le
 *  sigle brevi si conservano: sono cio' per cui esiste il ramo lessicale. */
export function ftsQuery(q: string): string {
  const tokens = q.toLowerCase().match(/[0-9A-Za-zÀ-ÿ]+/g) ?? [];
  let terms: string[] = [];
  for (const t of tokens) {
    const base = senzaAccenti(t);
    if (STOP.has(base)) continue;
    if (t.length > 2 || /^\d+$/.test(t) || SIGLE.has(base)) terms.push(t);
  }
  if (!terms.length) terms = tokens.filter((t) => t.length > 1);
  if (!terms.length) return "";
  return terms.slice(0, 24).map((t) => `"${t}"`).join(" OR ");
}

/** Prossimita' dentro l'atto, OR fra atti diversi. */
export function refQuery(refs: ActRef[]): string {
  return refs.map((r) => (r.year ? `NEAR("${r.num}" "${r.year}", 8)` : `"${r.num}"`)).join(" OR ");
}
