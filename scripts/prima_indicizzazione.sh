#!/usr/bin/env bash
# Prima indicizzazione completa.
#
# Dura 12-25 ore: l'OCR dei PDF scansionati è il collo di bottiglia.
# Lanciare dentro screen o tmux. Il processo è ripartibile: se cade,
# un 'fler ingest' riprende dai documenti rimasti.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "manca .env: copiare da .env.example e compilarlo"; exit 1; }
grep -q '^FLER_ACCESS_PASSWORD=.\+' .env || {
    echo "FLER_ACCESS_PASSWORD non impostata in .env. Il servizio non partirebbe."; exit 1; }

echo "== 1/3 prova su 200 documenti =="
docker compose run --rm web fler ingest --full --limit 200
docker compose run --rm web fler stato

cat <<'NOTA'

Cosa guardare prima di proseguire:
  · "indicizzati" deve essere vicino a 200, non a zero
  · "senza_testo" alto significa che l'estrazione non funziona (OCR? formati?)
  · "in_errore" alto significa problemi di rete o di robots.txt
  · provare una ricerca:  docker compose run --rm web fler cerca "accreditamento"

NOTA

read -r -p "I risultati sono sensati? Procedere con tutto il portale (12-25 ore)? [s/N] " ok
[[ "$ok" == "s" || "$ok" == "S" ]] || { echo "Interrotto. Nessun dato perso."; exit 0; }

echo "== 2/3 indicizzazione completa =="
docker compose run --rm web fler ingest --full || {
    echo
    echo "Il giro si è chiuso come PARZIALE. Non è un disastro: il punto di"
    echo "ripartenza non è stato avanzato, basta rilanciare 'fler ingest'."
    echo "Controllare prima i log per capire cosa si è interrotto."
}

echo "== 3/3 stato finale =="
docker compose run --rm web fler stato
echo
echo "Ora: docker compose up -d    e poi calibrare FLER_MIN_RELEVANCE (README §5)."
