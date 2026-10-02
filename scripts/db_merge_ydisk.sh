#!/usr/bin/env bash
# Обновление базы стенда из выгрузки команды (Яндекс Диск, папка hack) без потери локальных данных.
#   scripts/db_merge_ydisk.sh [data/ydisk/enrichment.dump] [data/ydisk/unverified_suppliers.csv]
#
# 1. Бэкап всей базы → data/backup/squad_<дата>_before_ydisk.dump (если ещё нет).
# 2. Текущие таблицы обогащения переезжают в схему prev, дамп восстанавливается в public.
# 3. Из prev доливаются факты и статусы источников, которых нет в дампе или которые свежее (в рабочей базе
#    есть локальное обогащение: ГИР БО, контакты, РНП за ночь 1–2.10).
# 4. Непроверенные поставщики из CSV → таблица unverified_suppliers (уровень пула, налоги и численность за 2025).
# 5. Витрина companies пересобирается из фактов новым кодом: сведения раньше 2024 года отбрасываются (card.drop_stale).
# Откат: pg_restore --clean бэкапа из шага 1. Схема prev остаётся до проверки: drop schema prev cascade.
set -euo pipefail
cd "$(dirname "$0")/.."
DUMP="${1:-data/ydisk/enrichment.dump}"
CSV="${2:-data/ydisk/unverified_suppliers.csv}"
BACKUP="data/backup/squad_$(date +%Y%m%d)_before_ydisk.dump"
TABLES=(companies company_facts enrichment_runs pool_companies pool_codes registry_items rnp_registry)
psql() { docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q "$@"' sh "$@"; }

[ -f "$DUMP" ] && [ -f "$CSV" ] || { echo "нет $DUMP или $CSV"; exit 1; }
mkdir -p data/backup
if [ ! -s "$BACKUP" ]; then
  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner' > "$BACKUP"
fi
echo "бэкап: $BACKUP ($(du -h "$BACKUP" | cut -f1))"

docker compose stop enrichment-api

{ echo "begin; create schema prev;"; for t in "${TABLES[@]}"; do echo "alter table public.$t set schema prev;"; done; echo "commit;"; } | psql
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < "$DUMP"
echo "дамп восстановлен"

psql <<'SQL'
insert into public.company_facts (inn, field, source, value, fetched_at)
select inn, field, source, value, fetched_at from prev.company_facts
on conflict (inn, field, source) do update set value = excluded.value, fetched_at = excluded.fetched_at
where public.company_facts.fetched_at < excluded.fetched_at;

insert into public.enrichment_runs (inn, source, status, error, updated_at)
select inn, source, status, error, updated_at from prev.enrichment_runs
on conflict (inn, source) do update set status = excluded.status, error = excluded.error, updated_at = excluded.updated_at
where public.enrichment_runs.updated_at < excluded.updated_at;

drop table if exists unverified_suppliers;
create table unverified_suppliers (
  inn varchar(12) primary key, kind text, ogrn text, name_full text, name_short text, region_code text, locality text,
  smp_category integer, smp_since varchar(10), employees double precision, employees_2025 double precision,
  taxes_paid_2025 double precision, okved_main text, okved_main_name text, okved_extra jsonb, products jsonb,
  licenses_count integer, pool_tier text, pool_reason text, as_of varchar(10), source text, status text);
comment on table unverified_suppliers is 'Новые поставщики СПб и ЛО из реестра МСП (снимок 10.09.2026), которых нет в истории закупок';
SQL
psql -c "\copy unverified_suppliers from stdin with (format csv, delimiter ';', header true)" < "$CSV"
echo "непроверенные поставщики загружены"

docker compose build enrichment enrichment-api
docker compose run --rm enrichment rebuild
docker compose up -d enrichment-api

psql -At <<'SQL'
select 'companies', count(*) from companies
union all select 'company_facts', count(*) from company_facts
union all select 'enrichment_runs', count(*) from enrichment_runs
union all select 'pool_companies', count(*) from pool_companies
union all select 'unverified_suppliers', count(*) from unverified_suppliers
union all select 'финансы старше 2024', count(*) from companies where finance_year < 2024;
SQL
echo "проверено — убрать старые копии: drop schema prev cascade; drop database ydisk_check;"
