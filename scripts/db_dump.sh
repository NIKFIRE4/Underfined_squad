#!/usr/bin/env bash
# Выгрузка таблиц обогащения из PostgreSQL стенда в файл для переноса на другую машину.
#   scripts/db_dump.sh                   → data/enrichment.dump
#   NO_RAW=1 scripts/db_dump.sh          → без сырых ответов (raw_responses), файл в разы меньше
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-data/enrichment.dump}"
TABLES=(company_facts enrichment_runs companies pool_companies pool_codes registry_items rnp_registry)
[ -z "${NO_RAW:-}" ] && TABLES+=(raw_responses)
ARGS=(); for t in "${TABLES[@]}"; do ARGS+=(-t "$t"); done
mkdir -p "$(dirname "$OUT")"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner '"${ARGS[*]}" > "$OUT"
echo "готово: $OUT ($(du -h "$OUT" | cut -f1)), таблицы: ${TABLES[*]}"
