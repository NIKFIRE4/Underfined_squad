#!/usr/bin/env bash
# Полный прогон обогащения. Каждый шаг можно перезапускать: повторный запуск продолжает с места остановки.
# Падение шага не останавливает следующие: в конце печатается сводка.
#
#   docker compose run --rm --entrypoint scripts/enrich_all.sh enrichment
#   ENRICH_LIMIT=500 scripts/enrich_all.sh          # быстрый прогон на 500 самых активных поставщиках
#
# Переменные: ENRICH_LIMIT (по умолчанию все ИНН), ENRICH_SKIP (шаги через пробел: download fns registries batch)
set -u
cd "$(dirname "$0")/.."
mkdir -p data
PY="${PYTHON:-python}"
LIMIT_ARGS=()
[ -n "${ENRICH_LIMIT:-}" ] && LIMIT_ARGS=(--limit "$ENRICH_LIMIT")
SKIP=" ${ENRICH_SKIP:-} "
FAILED=()

step() {  # step <имя> <команда...>
  local name=$1; shift
  if [[ "$SKIP" == *" $name "* ]]; then echo "== $name: пропущен"; return; fi
  echo "== $name: $*"
  "$@" || { echo "!! $name: ошибка, продолжаю"; FAILED+=("$name"); }
}

if [ ! -f dataset/Поставщики_24-25.csv ]; then
  echo "!! нет dataset/Поставщики_24-25.csv — положите выгрузку организаторов в dataset/"; exit 1
fi

# 1. Выгрузки ФНС: скачивание с докачкой и проверкой CRC (~2,5 ГБ)
step download "$PY" -m enrichment fns-download
# 2. Признаки из выгрузок + пул МСП СПб/ЛО для новых компаний (минуты, без сети)
step fns "$PY" -m enrichment fns-load
# 3. РРПП и реестр ПО из файлов в dataset/ (если их нет — шаг предупредит и пропустит)
step registries "$PY" -m enrichment registries-load
# 4. Поштучные источники параллельно, каждый своим процессом: медленный не тормозит быстрые.
#    ГИР БО и РНП — без капчи (~1 ИНН/с); ЕГРЮЛ и ПБ — с капчей, сами замедляются.
if [[ "$SKIP" != *" batch "* ]]; then
  echo "== batch: bo, rnp, egrul параллельно"
  pids=()
  for src in bo rnp egrul; do
    "$PY" -m enrichment batch --sources "$src" --concurrency 2 ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} > "data/batch_$src.log" 2>&1 &
    pids+=($!)
  done
  for i in "${!pids[@]}"; do wait "${pids[$i]}" || FAILED+=("batch_${i}"); done
  step rebuild "$PY" -m enrichment rebuild
fi

"$PY" -m enrichment stats
if [ ${#FAILED[@]} -gt 0 ]; then echo "!! шаги с ошибками: ${FAILED[*]} — логи в data/batch_*.log"; exit 1; fi
echo "== готово"
