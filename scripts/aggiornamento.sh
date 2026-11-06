#!/usr/bin/env bash
# Aggiornamento incrementale da crontab.
#   30 4 * * *  /opt/fler/scripts/aggiornamento.sh >> /var/log/fler.log 2>&1
#
# ATTENZIONE: se si usa questo script, DISATTIVARE il servizio "scheduler" nel
# docker-compose.yml (commentarlo), altrimenti gli aggiornamenti sono due. Non
# si corrompe nulla — le ingestioni sono serializzate da un lock — ma il
# secondo esce subito e il traffico verso la Regione raddoppia inutilmente.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose run --rm web fler ingest
docker compose run --rm web fler stato
# il web tiene l'indice vettoriale aperto: dopo la ricostruzione va riletto
docker compose restart web
