#!/usr/bin/env bash
# Ripristino da backup. La meta' della procedura che si dimentica sempre.
#   ./scripts/ripristino.sh 3      # ripristina il backup del giorno 3
set -euo pipefail
cd "$(dirname "$0")/.."
GIORNO="${1:?indicare il giorno del backup, 1-7 (ls backup/)}"
DEST="${FLER_BACKUP_DIR:-./backup}"

[[ -f "$DEST/catalog-$GIORNO.sqlite3" ]] || { echo "backup non trovato"; exit 1; }

echo "Fermo i servizi..."
docker compose down

echo "Metto da parte i dati attuali in data.prima-del-ripristino/"
rm -rf data.prima-del-ripristino
mv data data.prima-del-ripristino

mkdir -p data
cp "$DEST/catalog-$GIORNO.sqlite3" data/catalog.sqlite3
tar xzf "$DEST/lancedb-$GIORNO.tgz" -C data

echo "Riavvio..."
docker compose up -d
sleep 5
docker compose run --rm web fler stato
echo
echo "Se tutto torna, si puo' cancellare data.prima-del-ripristino/"
