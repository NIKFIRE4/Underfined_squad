#!/usr/bin/env bash
# Загрузка дампа обогащения в PostgreSQL стенда. Таблицы с тем же именем заменяются.
#   docker compose up -d db && scripts/db_restore.sh data/enrichment.dump
set -euo pipefail
cd "$(dirname "$0")/.."
IN="${1:-data/enrichment.dump}"
[ -f "$IN" ] || { echo "нет файла $IN"; exit 1; }
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < "$IN"
docker compose run --rm enrichment stats
