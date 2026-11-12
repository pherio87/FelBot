# Prova end-to-end in locale: crawler Python + Worker, senza Cloudflare

Fa girare il crawler vero contro un finto portale Plone e il Worker in
`wrangler dev` con D1 locale. Workers AI e Vectorize non ci sono: l'embedding
e' sostituito da vettori fittizi e la ricerca semantica risulta "non
disponibile" (e il servizio lo dichiara).

```bash
# 1. finto portale (50 documenti: delibere, una graduatoria, un allegato con
#    codici fiscali, un file vuoto, un download rotto)
cd tests/e2e_locale
python3 crea_stato.py stato.json
python3 portale.py stato.json hits.log &

# 2. Worker locale con il catalogo esposto su /__interno (solo se DEV_UTENTE
#    e' impostato e ACCESS_AUD e' vuoto: in produzione la rotta non esiste)
cd ../../worker
#   configurazione locale: wrangler.jsonc senza ai, vectorize, containers,
#   durable_objects, migrations dei DO, routes; ACCESS_AUD vuoto
printf 'DEV_UTENTE=io@locale.test\nANTHROPIC_API_KEY=finta\n' > .dev.vars
npx wrangler d1 migrations apply felbot --local -c wrangler.locale.jsonc
npx wrangler dev -c wrangler.locale.jsonc --port 8799 &

# 3. lotti
cd ../tests/e2e_locale
export NO_PROXY=127.0.0.1,localhost
python3 giro.py ../../src prova 5      # apre il giro completo, censisce, elabora 5
python3 giro.py ../../src continua     # elabora il resto e chiude il giro
```

Cosa controllare (con `npx wrangler d1 execute felbot --local ...`):
- la graduatoria e' `excluded` e non compare in `hits.log` (mai scaricata);
- l'allegato con tre codici fiscali e' `excluded` dopo l'estrazione;
- `chunks` e `chunks_fts` hanno lo stesso numero di righe;
- dopo aver tolto un documento dall'elenco lasciandolo pubblicato e averne
  cancellato un altro (modificando `stato.json`), un giro completo ritira solo
  il secondo.
