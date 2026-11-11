# FelBot — Worker Cloudflare

La parte online di FelBot: chat, ricerca, chiamata a Claude, verifica delle
risposte, pagina pubblica `/bot` con modulo di contatto. Il crawler (Python,
nella cartella principale del repository) riempie il database; questo Worker
lo legge.

```
                     Cloudflare Access (solo email autorizzate)
                                   │
 browser ──► felbot.pherio.net ──► Worker ──► D1 (catalogo + FTS5)
                                     │   ├──► Vectorize (vettori bge-m3)
              /bot e /api/contatto   │   ├──► Workers AI (embedding della domanda)
              pubblici, con Turnstile│   └──► Claude (riformulazione + risposta)
                                     └──► Email Routing (modulo di contatto)
```

| File | Cosa fa |
|---|---|
| `src/index.ts` | rotte, limiti d'uso, risposta in streaming |
| `src/retrieve.ts` | recupero ibrido FTS5 + Vectorize, fusione RRF, astensione, contesto |
| `src/refs.ts` | estremi degli atti (DGR, DD, L.R., PG) e query FTS5 sicure |
| `src/verify.ts` | prompt, fascicolo di estratti, **verifica della risposta** |
| `src/valori.ts` | importi, date, durate, numeri in lettere: estrazione e confronto |
| `src/access.ts` | verifica del token di Cloudflare Access |
| `src/contatto.ts` | modulo di contatto: trappola, Turnstile, limite, email |
| `migrations/` | schema D1, condiviso con il crawler |

`valori.ts` e `verify.ts` sono il porting della versione Python; i test
(`test/`) riproducono gli stessi scenari avversari e un confronto diretto ha
dato risultati identici fra le due implementazioni.

```bash
npm install
npm test          # 58 verifiche, nessuna rete
npm run check     # controllo dei tipi
```

---

## Primo deploy

Da fare una volta sola, nell'ordine. I comandi `npx wrangler` vanno lanciati
dalla cartella `worker/` dopo `npx wrangler login`.

**1. Database e indice vettoriale**

```bash
npx wrangler d1 create felbot
# copiare il database_id restituito in wrangler.jsonc
npx wrangler d1 migrations apply felbot --remote
npx wrangler vectorize create felbot-chunks --dimensions=1024 --metric=cosine
```

**2. Cloudflare Access** — Zero Trust → Access → Applications

- *Add an application* → Self-hosted, dominio `felbot.pherio.net`, policy
  **Allow** con le email autorizzate, metodo di accesso *One-time PIN*.
- Una seconda applicazione Self-hosted con due hostname, `felbot.pherio.net`
  con percorso `bot` e `felbot.pherio.net` con percorso `api/contatto`, e
  policy **Bypass** su *Everyone*. Sono la pagina pubblica sul crawler e il
  suo modulo; il percorso più specifico prevale sull'applicazione principale.
- Dall'applicazione principale copiare l'*Application Audience (AUD) Tag* in
  `ACCESS_AUD`; il nome del team (la parte prima di `.cloudflareaccess.com`)
  in `ACCESS_TEAM`.

Senza `ACCESS_AUD` e `ACCESS_TEAM` il Worker **rifiuta tutte le richieste**:
resta chiuso, non aperto.

**3. Turnstile** — crea un widget per `felbot.pherio.net`; la *site key* va in
`TURNSTILE_SITEKEY`, la *secret key* nel segreto `TURNSTILE_SECRET`.

**4. Email Routing** su pherio.net — attivarlo e verificare la casella di
destinazione. Il mittente `felbot@pherio.net` non deve esistere come casella.

**5. Segreti**

```bash
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put TURNSTILE_SECRET
npx wrangler secret put CONTATTO_DESTINATARIO   # la casella verificata al punto 4
```

**6. Workers Builds** — Workers & Pages → `felbot` → Settings → Builds →
collegare `pherio87/felbot`, con:

| | |
|---|---|
| Root directory | `worker` |
| Build command | `npm ci && npm test` |
| Deploy command | `npx wrangler d1 migrations apply felbot --remote && npx wrangler deploy` |

Da qui ogni push su `main` esegue i test e, se passano, pubblica.

**7. Tetto di spesa** — sulla console Anthropic un limite mensile. In più,
facoltativo: un gateway in AI Gateway con limite di spesa, e il suo URL
(`https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/anthropic`) in
`ANTHROPIC_BASE_URL`.

---

## Limiti d'uso

`LIMITE_GIORNALIERO_UTENTE` (60) per persona e `LIMITE_GIORNALIERO_TOTALE`
(300) per tutto il servizio, contati in D1 per email e giorno (UTC). Il totale
è un tetto di spesa: ~300 domande sono qualche euro al giorno nel caso peggiore.
La tabella `uso` è anche la base per un eventuale abbonamento con Stripe.

Il testo delle domande **non** viene conservato.

## Soglia di astensione

`MIN_RELEVANCE` vale 0,50: è una **stima** per bge-m3, che dà similarità più
alte del modello OpenAI usato nella versione su VPS (lì era 0,42). Va tarata
con lo stesso metodo del README principale (§5): domande con risposta nota e
domande sicuramente fuori corpus, si alza la soglia finché il sistema non
risponde più a nessuna di quelle fuori corpus.

## Modelli

`LLM_MODEL` (risposta) e `LLM_MODEL_RIFORMULA` (riscrittura dei follow-up,
più economico) sono variabili: si cambiano senza toccare il codice. La
temperatura è 0; se un modello non accetta il parametro, il Worker se ne
accorge alla prima richiesta e lo omette.

## Cosa manca

Il **crawler su Cloudflare Containers**: oggi il crawler Python scrive su
SQLite e LanceDB in locale. Va adattato per scrivere in D1 e Vectorize (lo
schema è già quello in `migrations/`, con `vec_id` = SHA-1 del `chunk_id`
perché Vectorize accetta id di massimo 64 byte) e lanciato a lotti da un
trigger programmato. Finché non c'è, l'indice è vuoto e ogni domanda
produce un'astensione.
