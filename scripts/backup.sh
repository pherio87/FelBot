#!/usr/bin/env bash
# Backup su rotazione settimanale di catalogo e indice vettoriale.
#   30 5 * * *  /opt/fler/scripts/backup.sh >> /var/log/fler-backup.log 2>&1
#
# Perdere /data non e' irreversibile — si rigenera dal portale — ma costa
# alcune ore di crawling e qualche euro di embedding, da rifare a riga di
# comando: esattamente cio' che non si vuole dover fare di fretta.
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${FLER_BACKUP_DIR:-./backup}"
GIORNO=$(date +%u)          # 1..7, rotazione settimanale
mkdir -p "$DEST"

# .backup di sqlite3 e' sicuro a database aperto, cp non lo e' (WAL)
if command -v sqlite3 >/dev/null; then
    sqlite3 data/catalog.sqlite3 ".backup '$DEST/catalog-$GIORNO.sqlite3'"
else
    docker compose run --rm --no-deps web \
        python -c "import sqlite3,sys; s=sqlite3.connect('/data/catalog.sqlite3'); \
d=sqlite3.connect('/data/backup-tmp.sqlite3'); s.backup(d); d.close(); s.close()"
    mv data/backup-tmp.sqlite3 "$DEST/catalog-$GIORNO.sqlite3"
fi

tar czf "$DEST/lancedb-$GIORNO.tgz" -C data lancedb
echo "[$(date -Is)] backup completato in $DEST (giorno $GIORNO)"
ls -lh "$DEST"
