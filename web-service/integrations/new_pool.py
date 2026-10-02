"""Новые компании для вкладки «Непроверенные»: поставщики СПб и ЛО из реестра МСП, которых нет в истории закупок.

Источник — база стенда (PostgreSQL): таблица unverified_suppliers (выгрузка команды, снимок реестра МСП 10.09.2026)
и собранные по ней scripts/build_unverified_pool.py таблицы:
  unverified_pool        — карточка компании: реквизиты, роль, уровень пула, налоги и численность за 2025 год, источники;
  unverified_pool_groups — связки «компания × группа ОКПД2» с доказательством и приоритетом.
Блок для лота: компании, у которых группа ОКПД2 позиции лота среди их групп. Порядок — сила доказательства
именно по этой группе: сначала сильное (госконтракты в группе, реестр производителей или лицензия, основной ОКВЭД
или продукция совпадает с группой), затем только ассоциация по ОКВЭД; внутри — приоритет пула (уровень, налоги,
численность). Моделью не оцениваются, score не выше NEW_SCORE_CAP — ниже кандидатов с историей.

NEW_POOL_DB — строка подключения (по умолчанию POSTGRES_* из .env в корне репозитория, хост 127.0.0.1).
Нужны pandas и psycopg2. База недоступна — блок пуст, подбор моделью работает.
"""
import logging
import os
import re
import threading
from collections import Counter
from pathlib import Path

from models import Candidate, Lot

ROOT = Path(__file__).resolve().parents[2]
PER_GROUP = 300        # сколько лучших компаний держать в памяти на группу ОКПД2
PER_LOT = 30           # сколько отдавать на лот; сервер обрежет до top_k
NEW_SCORE_CAP = 75.0
CORE_REGIONS = {"78", "47"}
TIER_TEXT = {"strong": "действующая компания: налоги за 2025 г. и от 5 сотрудников",
             "active": "небольшая действующая компания: налоги или сотрудники за 2025 г.",
             "signal": "ИП с лицензией или продукцией в реестре МСП"}
REGISTRY_URLS = [("Росздравнадзор", "https://roszdravnadzor.gov.ru/services/licenses"),
                 ("719", "https://gisp.gov.ru/pp719v2/pub/prod/"), ("ГИСП", "https://gisp.gov.ru/pp719v2/pub/prod/"),
                 ("ПО", "https://reestr.digital.gov.ru/reestr/")]
CARD_COLS = ["inn", "name", "name_short", "region", "is_msp", "role", "role_evidence", "tier", "pool_reason",
             "mos_source", "registry_sources", "registry_source_date", "msp_source", "msp_source_date"]

_lock = threading.Lock()
_pool = None  # (блоки по группам: DataFrame, карточки: DataFrame) или False, если данных нет


def db_url() -> str:
    if url := os.environ.get("NEW_POOL_DB"):
        return url
    env = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except OSError:
        pass
    get = lambda k, d: os.environ.get(k) or env.get(k) or d  # noqa: E731
    return (f"postgresql://{get('POSTGRES_USER', 'squad')}:{get('POSTGRES_PASSWORD', 'squad')}"
            f"@{get('POSTGRES_HOST', '127.0.0.1')}:{get('POSTGRES_PORT', '5432')}/{get('POSTGRES_DB', 'squad')}")


def _tables():
    """(связки, карточки) из базы: связки — по PER_GROUP лучших на группу."""
    import warnings
    import pandas as pd
    import psycopg2
    with psycopg2.connect(db_url(), connect_timeout=5) as conn, warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # pandas предпочитает SQLAlchemy, psycopg2 тоже работает
        groups = pd.read_sql("select inn, okpd2_group, evidence, tier, priority from unverified_pool_groups", conn)
        companies = pd.read_sql(f"select {', '.join(CARD_COLS)} from unverified_pool", conn)
    return groups, companies


def _load():
    import pandas as pd
    groups, companies = _tables()
    ev = groups.evidence.fillna("")
    same_group = pd.Series([bool(re.search(rf"ОКВЭД {re.escape(g)} \(осн\.\)|продукция {re.escape(g)}", e)) for g, e in zip(groups.okpd2_group, ev)], index=groups.index)
    # 1 — сильное доказательство по группе, 0 — только ассоциация по ОКВЭД
    groups["rank_key"] = (ev.str.contains("контракт|лиценз|реестр|ГИСП|Минпромторг|РУ ") | same_group).astype("int8")
    groups = (groups.sort_values(["okpd2_group", "rank_key", "priority"], ascending=[True, False, False])
                    .groupby("okpd2_group", sort=False).head(PER_GROUP).reset_index(drop=True))
    # специализированный застройщик (214-ФЗ) вправе вести только свой проект строительства — не подрядчик госзакупок
    spv = companies.name.fillna("").str.contains("СПЕЦИАЛИЗИРОВАННЫЙ ЗАСТРОЙЩИК", case=False) | \
          companies.name_short.fillna("").str.contains(r"СПЕЦИАЛИЗИРОВАННЫЙ ЗАСТРОЙЩИК|^ООО\s+\"?СЗ\b", case=False, regex=True)
    companies = companies[~spv]
    groups = groups[groups.inn.isin(set(companies.inn))]
    companies = companies[companies.inn.isin(set(groups.inn))].set_index("inn")
    return groups, companies


def pool():
    """Пул загружается один раз (секунды) и живёт в памяти процесса."""
    global _pool
    with _lock:
        if _pool is None:
            try:
                _pool = _load()
                logging.info("Пул новых компаний: %d связок, %d компаний", len(_pool[0]), len(_pool[1]))
            except Exception as e:  # нет базы, драйвера или таблиц — вкладка пуста, остальное работает
                logging.warning("Пул новых компаний недоступен (%s): вкладка «Непроверенные» будет пустой", e)
                _pool = False
        return _pool


def _date(v):
    try:
        return v.strftime("%Y-%m-%dT00:00:00+03:00") if v is not None and v == v else None
    except (AttributeError, ValueError):
        return None


def _evidence(text: str) -> list[str]:
    """«контракты 28.93 (26); ОКВЭД 47.79» → понятные причины."""
    out = []
    for part in (p.strip() for p in str(text or "").split(";")):
        if m := re.fullmatch(r"контракты ([\d.]+) \((\d+)\)", part):
            n = int(m[2])
            word = "госконтракт" if n % 10 == 1 and n % 100 != 11 else "госконтракта" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "госконтрактов"
            out.append(f"{n} {word} по группе ОКПД2 {m[1]} на Портале поставщиков")
        elif m := re.fullmatch(r"ОКВЭД ([\d.]+)( \(осн\.\))?", part):
            out.append(f"{'Основной' if m[2] else 'Дополнительный'} вид деятельности по ОКВЭД {m[1]} связан с группой ОКПД2 лота")
        elif m := re.fullmatch(r"продукция ([\d.]+)", part):
            out.append(f"Продукция группы ОКПД2 {m[1]} заявлена в реестре МСП")
        elif part:
            out.append(part[0].upper() + part[1:])
    return out


def _sources(inn: str, c) -> list[dict]:
    out = []
    if c.mos_source and (d := _date(c.msp_source_date)):
        out.append({"field": "Госконтракты", "source": c.mos_source, "url": "https://zakupki.mos.ru/", "checked_at": d})
    if c.registry_sources and (d := _date(c.registry_source_date) or _date(c.msp_source_date)):
        url = next((u for key, u in REGISTRY_URLS if key in c.registry_sources), None)
        if url:
            out.append({"field": "Реестр", "source": c.registry_sources, "url": url, "checked_at": d})
    if c.msp_source and (d := _date(c.msp_source_date)):
        out.append({"field": "МСП", "source": c.msp_source, "url": f"https://rmsp.nalog.ru/search.html?query={inn}", "checked_at": d})
    return out


def find(lot: Lot, regions: dict, exclude: set[str]) -> list[Candidate]:
    """Новые компании для лота по группам ОКПД2 его позиций."""
    codes = [str(i.get("okpd2_code") or "").strip() for i in lot.items]
    # группа → число позиций лота: при нескольких группах выше компании основной группы лота
    lot_groups = Counter(c[:5] for c in codes if re.match(r"^\d{2}\.\d{2}", c))
    p = pool() if lot_groups else None
    if not p:
        return []
    groups, companies = p
    block = groups[groups.okpd2_group.isin(lot_groups) & ~groups.inn.isin(exclude)]
    if str(lot.notice.get("is_smp", "")).strip().lower() in ("true", "1", "да"):
        block = block[block.inn.map(companies.is_msp).fillna(False).astype(bool)]
    block = block.assign(items=block.okpd2_group.map(lot_groups))
    block = block.sort_values(["rank_key", "items", "priority"], ascending=False).drop_duplicates("inn").head(PER_LOT)
    out = []
    for rank, row in enumerate(block.itertuples(index=False)):
        c = companies.loc[row.inn]
        role = str(c.role or "не определена").split(" (")[0]
        reasons = _evidence(row.evidence)[:2]
        if c.pool_reason and row.tier != "signal":  # у signal причина совпадает с описанием уровня
            reasons.append(c.pool_reason[0].upper() + c.pool_reason[1:])
        reasons.append(f"Нет в истории закупок; {TIER_TEXT.get(row.tier, row.tier)}")
        out.append(Candidate(
            supplier_name=c["name_short"] or c["name"] or row.inn,  # c.name — метка строки (ИНН), не колонка
            supplier_inn=row.inn,
            score=round(NEW_SCORE_CAP * (1 - rank / max(len(block), 1)), 1),
            role=role[0].upper() + role[1:],
            status="Новый в пуле",
            region=regions.get(str(c.region)) or (f"Регион {c.region}" if c.region else ""),
            is_smp=bool(c.is_msp) if c.is_msp == c.is_msp else None,
            is_new=True,
            reasons=reasons,
            sources=_sources(row.inn, c),
            enrichment_status="Найдена в открытых реестрах",
        ))
    return out
