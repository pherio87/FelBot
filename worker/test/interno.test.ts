/**
 * Regola di riscaricamento del catalogo: gli stessi casi di
 * catalog.upsert_discovered nei test Python.
 */
import { describe, expect, it } from "vitest";
import { daRiscaricare, sha1hex, type Esistente } from "../src/interno";

const adesso = 1_800_000_000;
const giorno = 86400;
const doc = (o: Partial<Esistente> = {}): Esistente => ({
  site_modified: "2024-01-01", status: "indexed", gone: 0, last_fetched: adesso - giorno, ha_frammenti: 1, ...o,
});

describe("quando riscaricare un documento visto in enumerazione", () => {
  it("indicizzato, invariato, verificato di recente → no", () =>
    expect(daRiscaricare(doc(), "2024-01-01", 30, adesso)).toBe(false));
  it("data di modifica cambiata → si'", () =>
    expect(daRiscaricare(doc(), "2025-02-02", 30, adesso)).toBe(true));
  it("mai scaricato o in errore → si'", () => {
    expect(daRiscaricare(doc({ status: "discovered" }), "2024-01-01", 30, adesso)).toBe(true);
    expect(daRiscaricare(doc({ status: "error" }), "2024-01-01", 30, adesso)).toBe(true);
  });
  it("ricomparso dopo essere stato ritirato → si'", () =>
    expect(daRiscaricare(doc({ gone: 1 }), "2024-01-01", 30, adesso)).toBe(true));
  it("dichiarato indicizzato ma senza frammenti → si'", () =>
    expect(daRiscaricare(doc({ ha_frammenti: null }), "2024-01-01", 30, adesso)).toBe(true));
  it("senza testo o escluso dai robots: NON a ogni scansione (difetto della 1.1)", () => {
    expect(daRiscaricare(doc({ status: "empty", ha_frammenti: null }), "2024-01-01", 30, adesso)).toBe(false);
    expect(daRiscaricare(doc({ status: "skipped", ha_frammenti: null }), "2024-01-01", 30, adesso)).toBe(false);
  });
  it("non riverificato da oltre 30 giorni → si', salvo gli esclusi per dati personali", () => {
    expect(daRiscaricare(doc({ last_fetched: adesso - 40 * giorno }), "2024-01-01", 30, adesso)).toBe(true);
    expect(daRiscaricare(doc({ status: "excluded", ha_frammenti: null, last_fetched: adesso - 400 * giorno }),
      "2024-01-01", 30, adesso)).toBe(false);
  });
  it("gia' preso in carico da un lotto → non si tocca", () =>
    expect(daRiscaricare(doc({ status: "in_lavorazione" }), "2099-01-01", 30, adesso)).toBe(false));
});

describe("id dei vettori", () => {
  it("SHA-1 esadecimale: 40 caratteri, sotto il limite di 64 byte di Vectorize", async () => {
    const lungo = "formazionelavoro:0123456789abcdef0123#1234:deadbeef" + "x".repeat(40);
    const v = await sha1hex(lungo);
    expect(v).toMatch(/^[0-9a-f]{40}$/);
    expect(await sha1hex(lungo)).toBe(v);
  });
  it("stesso algoritmo di Python (hashlib.sha1)", async () =>
    expect(await sha1hex("abc")).toBe("a9993e364706816aba3e25717850c26c9cd0d89d"));
});
