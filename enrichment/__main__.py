"""CLI.

  python -m enrichment enrich 7707049388 7605016030          # обогатить ИНН и показать карточки
  python -m enrichment batch --limit 500 --concurrency 6      # пакетный прогон по выгрузке поставщиков
  python -m enrichment show 7707049388                       # карточка из БД с источниками полей
  python -m enrichment stats                                 # прогресс пакетного прогона
  python -m enrichment fns-download && python -m enrichment fns-load   # массовые выгрузки ФНС

БД: --db (по умолчанию sqlite:///data/enrichment.db) или переменная ENRICHMENT_DB.
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from . import fns_dumps, storage
from .card import build_company
from .http import Http
from .inn import is_valid_inn
from .pipeline import ALL_SOURCES, enrich, rebuild_company

DEFAULT_DB = os.environ.get("ENRICHMENT_DB", "sqlite:///data/enrichment.db")
DEFAULT_SUPPLIERS = "dataset/Поставщики_24-25.csv"

log = logging.getLogger("enrichment")


def _engine(url: str):
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return storage.connect(url)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def supplier_inns(path: str) -> list[str]:
    """Уникальные валидные ИНН поставщиков, самые активные первыми."""
    cnt: Counter[str] = Counter()
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            cnt[row["supplier_inn"].strip()] += 1
    return [inn for inn, _ in cnt.most_common() if is_valid_inn(inn)]


async def cmd_enrich(args) -> None:
    engine, http = _engine(args.db), Http()
    try:
        for inn in args.inns:
            await enrich(http, engine, inn, args.sources)
            row, card = rebuild_company(engine, inn)
            print(_dump({"company": row, "fields": card} if args.full else row))
    finally:
        await http.aclose()


async def cmd_batch(args) -> None:
    engine, http = _engine(args.db), Http()
    inns = args.inns or supplier_inns(args.suppliers)
    if not args.force:
        done = storage.done_inns(engine, args.sources)
        inns = [i for i in inns if i not in done]
    if args.limit:
        inns = inns[: args.limit]
    log.info("batch: %d INN, sources=%s, concurrency=%d", len(inns), args.sources, args.concurrency)

    queue: asyncio.Queue[str] = asyncio.Queue()
    for i in inns:
        queue.put_nowait(i)
    stats = Counter()
    t0 = time.monotonic()

    async def worker() -> None:
        while True:
            try:
                inn = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                row = await enrich(http, engine, inn, args.sources)
                stats[row["enrichment_status"]] += 1
            except Exception as e:  # noqa: BLE001
                stats["crash"] += 1
                log.error("%s: %s", inn, e)
            n = sum(stats.values())
            if n % 25 == 0 or n == len(inns):
                rate = n / (time.monotonic() - t0)
                log.info("%d/%d %s · %.2f ИНН/с · осталось ~%.0f мин",
                         n, len(inns), dict(stats), rate, (len(inns) - n) / rate / 60 if rate else 0)

    try:
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    finally:
        await http.aclose()
    log.info("done: %s", dict(stats))


def cmd_show(args) -> None:
    engine = _engine(args.db)
    for inn in args.inns:
        row, card = rebuild_company(engine, inn)
        print(_dump({"company": row, "fields": card}))


def cmd_fns_download(args) -> None:
    for name in args.datasets:
        fns_dumps.download(fns_dumps.DATASETS[name])


def cmd_fns_load(args) -> None:
    engine = _engine(args.db)
    targets = set(args.inns or supplier_inns(args.suppliers))
    for name in args.datasets:
        t0 = time.monotonic()
        storage.save_results(engine, fns_dumps.load(fns_dumps.DATASETS[name], targets))
        log.info("%s: сохранено за %.0f с", name, time.monotonic() - t0)
    rebuild_all(engine, targets)


def rebuild_all(engine, inns: set[str] | None = None) -> None:
    facts, runs = storage.load_all(engine, inns)
    rows = [build_company(inn, facts.get(inn, []), runs.get(inn, {}))[0] for inn in runs]
    storage.save_companies(engine, rows)
    log.info("витрина companies: пересобрано %d ИНН", len(rows))


def cmd_stats(args) -> None:
    engine = _engine(args.db)
    with engine.connect() as conn:
        runs = conn.execute(
            select(storage.enrichment_runs.c.source, storage.enrichment_runs.c.status, func.count())
            .group_by(storage.enrichment_runs.c.source, storage.enrichment_runs.c.status)
        ).all()
        comp = conn.execute(
            select(storage.companies.c.enrichment_status, func.count())
            .group_by(storage.companies.c.enrichment_status)
        ).all()
    print("sources:", _dump({f"{s}:{st}": n for s, st, n in runs}))
    print("companies:", _dump(dict(comp)))


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m enrichment")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def sources_arg(sp):
        sp.add_argument("--sources", type=lambda s: s.split(","), default=ALL_SOURCES,
                        help=f"через запятую, по умолчанию {','.join(ALL_SOURCES)}")

    e = sub.add_parser("enrich")
    e.add_argument("inns", nargs="+")
    e.add_argument("--full", action="store_true", help="показать источник и дату каждого поля")
    sources_arg(e)

    b = sub.add_parser("batch")
    b.add_argument("inns", nargs="*", help="по умолчанию — все ИНН из выгрузки поставщиков")
    b.add_argument("--suppliers", default=DEFAULT_SUPPLIERS)
    b.add_argument("--limit", type=int)
    b.add_argument("--concurrency", type=int, default=6)
    b.add_argument("--force", action="store_true", help="перезапросить уже обогащённые")
    sources_arg(b)

    datasets = list(fns_dumps.DATASETS)
    fd = sub.add_parser("fns-download", help="скачать выгрузки ФНС в data/fns")
    fd.add_argument("--datasets", type=lambda s: s.split(","), default=datasets)
    fl = sub.add_parser("fns-load", help="загрузить признаки из выгрузок ФНС для ИНН поставщиков")
    fl.add_argument("inns", nargs="*")
    fl.add_argument("--suppliers", default=DEFAULT_SUPPLIERS)
    fl.add_argument("--datasets", type=lambda s: s.split(","), default=datasets)
    sub.add_parser("rebuild", help="пересобрать витрину companies из фактов")

    s = sub.add_parser("show")
    s.add_argument("inns", nargs="+")
    sub.add_parser("stats")

    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "enrich":
        asyncio.run(cmd_enrich(args))
    elif args.cmd == "batch":
        asyncio.run(cmd_batch(args))
    elif args.cmd == "show":
        cmd_show(args)
    elif args.cmd == "fns-download":
        cmd_fns_download(args)
    elif args.cmd == "fns-load":
        cmd_fns_load(args)
    elif args.cmd == "rebuild":
        rebuild_all(_engine(args.db))
    else:
        cmd_stats(args)


if __name__ == "__main__":
    main()
