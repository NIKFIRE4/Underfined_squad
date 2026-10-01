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

from . import discovery, fns_dumps, history, registries, storage
from .card import build_company
from .card import links as card_links
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
    if args.missing_status:
        with engine.connect() as conn:
            unknown = {r[0] for r in conn.execute(
                select(storage.companies.c.inn).where(storage.companies.c.is_active.is_(None)))}
        inns = [i for i in inns if i in unknown]
    if not args.force:
        done = storage.done_inns(engine, args.sources)
        inns = [i for i in inns if i not in done]
    if args.limit:
        inns = inns[: args.limit]
    log.info("batch: %d INN, sources=%s, concurrency=%d", len(inns), args.sources, args.concurrency)

    reqnums, names = {}, {}
    if "contacts" in args.sources:  # подсказки для поиска контракта: номера закупок и название
        reqnums = history.won_reqnums(args.suppliers)
        with engine.connect() as conn:
            names = {i: n1 or n2 for i, n1, n2 in conn.execute(
                select(storage.companies.c.inn, storage.companies.c.name_short, storage.companies.c.name_full))}
        log.info("contacts: номера закупок для %d ИНН, названия для %d", len(reqnums), len(names))
        inns.sort(key=lambda i: i not in reqnums)  # стабильно: внутри групп порядок по активности

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
                hints = {"reqnums": reqnums.get(inn), "name": names.get(inn)}
                row = await enrich(http, engine, inn, args.sources, hints)
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
        if name == "rsmp":
            storage.clear_pool(engine)
            results = fns_dumps.load_rsmp(targets, lambda c, k: storage.insert_pool_chunk(engine, c, k))
        else:
            results = fns_dumps.load(fns_dumps.DATASETS[name], targets)
        storage.save_results(engine, results)
        log.info("%s: сохранено за %.0f с", name, time.monotonic() - t0)
    rebuild_all(engine, targets)


def cmd_registries_load(args) -> None:
    engine = _engine(args.db)
    targets = set(args.inns or supplier_inns(args.suppliers))
    paths = {"gisp": args.gisp, "software": args.software}
    for name in registries.REGISTRIES:
        results, items = registries.load(name, targets, paths[name])
        if not items:
            continue
        storage.replace_registry(engine, name, items)
        storage.save_results(engine, results)
        log.info("%s: %d позиций по ОКПД2, совпало с поставщиками %d",
                 name, len(items), sum(1 for r in results if any(f.value is True for f in r.facts)))
    rebuild_all(engine, targets)


def cmd_discover(args) -> None:
    engine = _engine(args.db)
    exclude = set() if args.include_known else set(supplier_inns(args.suppliers))
    found = discovery.find_new_companies(
        engine, args.okpd2, regions=set(args.regions) if args.regions else None,
        exclude=exclude, only_smp=args.only_smp, limit=args.limit)
    for c in found:
        print(f"{c.score:5.1f}  {c.inn:<12} {c.region_code or '':<3} {(c.name or '')[:50]:<50} "
              f"{c.evidence[0]['text'][:90]}")
    log.info("найдено %d новых компаний", len(found))


def cmd_history_load(args) -> None:
    engine = _engine(args.db)
    results = history.load(suppliers=args.suppliers)
    storage.save_results(engine, results)
    rebuild_all(engine, {r.inn for r in results})


EXPORT_EXTRA = ["okved_main_name", "reg_year", "smp_since", "employees_as_of", "taxes_paid_as_of", "revenue_tax",
                "hist_lots", "hist_wins", "hist_customers", "hist_okpd2_codes", "hist_okpd2_classes",
                "hist_last_date", "hist_class_codes", "finance_by_year",
                "contacts_found", "contact_phones", "contact_emails", "contact_postal_address",
                "contact_contract_url", "website", "links"]


def _csv_value(v):
    """Как в выгрузках организаторов: true/false; списки и словари — JSON; None — пустая ячейка."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def cmd_export_csv(args) -> None:
    """Плоская таблица по всем ИНН для pandas/polars: join с исходными данными по inn."""
    engine = _engine(args.db)
    facts, runs = storage.load_all(engine)
    cols = list(dict.fromkeys([c.name for c in storage.companies.columns if c.name != "updated_at"] + EXPORT_EXTRA + [
        "role", "role_label", "role_confidence", "role_evidence", "risk_flag_codes"]))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for inn in sorted(i for i in facts if is_valid_inn(i)):
        row, card = build_company(inn, facts[inn], runs.get(inn, {}))
        full = row | {k: v["value"] for k, v in card.items() if k not in row}
        full["links"] = card_links(inn, full)
        role = discovery.classify_role(full)
        full |= {"role": role["value"], "role_label": role["label"], "role_confidence": role["confidence"],
                 "role_evidence": " | ".join(role["evidence"]),
                 "risk_flag_codes": ",".join(fl["code"] for fl in row.get("risk_flags") or [])}
        rows.append({k: _csv_value(v) for k, v in full.items() if k in cols})
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log.info("CSV: %s, %d строк, %d колонок", args.out, len(rows), len(cols))
    if not args.no_xlsx:
        _write_xlsx(Path(args.out).with_suffix(".xlsx"), cols, rows)


TEXT_COLS = {"inn", "ogrn", "kpp", "region_code", "okved_main"}  # в Excel — только текстом, иначе 1,01E+10


def _write_xlsx(path: Path, cols: list[str], rows: list[dict]) -> None:
    """Для людей: Excel открывает CSV не в UTF-8 и превращает ИНН в числа — XLSX этих проблем не имеет."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Компании")
    ws.freeze_panes = "B2"
    bold = Font(bold=True)
    head = []
    for c in cols:
        cell = WriteOnlyCell(ws, value=c)
        cell.font = bold
        head.append(cell)
    ws.append(head)

    def typed(col, v):
        if v is None or v == "":
            return None
        if col in TEXT_COLS or not isinstance(v, (int, float)):
            if v in ("true", "false"):
                return v == "true"
            return str(v)[:32000]  # лимит ячейки Excel
        return v

    for r in rows:
        ws.append([typed(c, r.get(c)) for c in cols])
    wb.save(path)
    log.info("XLSX: %s", path)


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
    b.add_argument("--missing-status", action="store_true",
                   help="только ИНН, у которых статус «действующая» неизвестен (для --sources egrul)")
    sources_arg(b)

    datasets = list(fns_dumps.DATASETS)
    fd = sub.add_parser("fns-download", help="скачать выгрузки ФНС в data/fns")
    fd.add_argument("--datasets", type=lambda s: s.split(","), default=datasets)
    fl = sub.add_parser("fns-load", help="загрузить признаки из выгрузок ФНС для ИНН поставщиков")
    fl.add_argument("inns", nargs="*")
    fl.add_argument("--suppliers", default=DEFAULT_SUPPLIERS)
    fl.add_argument("--datasets", type=lambda s: s.split(","), default=datasets)
    rl = sub.add_parser("registries-load", help="РРПП (ПП 719) и реестр ПО из файлов в dataset/")
    rl.add_argument("inns", nargs="*")
    rl.add_argument("--suppliers", default=DEFAULT_SUPPLIERS)
    rl.add_argument("--gisp", help=f"по умолчанию {registries.GISP_PATH}")
    rl.add_argument("--software", help=f"по умолчанию {registries.SOFTWARE_GLOB}")
    hl = sub.add_parser("history-load", help="признаки из истории закупок (роль «дистрибьютор»)")
    hl.add_argument("--suppliers", default=DEFAULT_SUPPLIERS)
    dc = sub.add_parser("discover", help="новые компании по ОКПД2 лота (ФТ-06)")
    dc.add_argument("okpd2", nargs="+")
    dc.add_argument("--regions", type=lambda s: s.split(","), default=["78", "47"])
    dc.add_argument("--limit", type=int, default=20)
    dc.add_argument("--only-smp", action="store_true")
    dc.add_argument("--include-known", action="store_true", help="не исключать поставщиков из истории")
    dc.add_argument("--suppliers", default=DEFAULT_SUPPLIERS)
    ex = sub.add_parser("export-csv", help="выгрузить обогащённые компании в CSV (разделитель ;)")
    ex.add_argument("--out", default="data/companies_enriched.csv")
    ex.add_argument("--no-xlsx", action="store_true", help="не писать копию .xlsx для Excel")
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
    elif args.cmd == "registries-load":
        cmd_registries_load(args)
    elif args.cmd == "history-load":
        cmd_history_load(args)
    elif args.cmd == "discover":
        cmd_discover(args)
    elif args.cmd == "export-csv":
        cmd_export_csv(args)
    elif args.cmd == "rebuild":
        rebuild_all(_engine(args.db))
    else:
        cmd_stats(args)


if __name__ == "__main__":
    main()
