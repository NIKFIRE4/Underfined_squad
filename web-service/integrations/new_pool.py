"""Новые компании для вкладки «Непроверенные»: пул из открытых реестров, которых нет в истории закупок.

Данные собирает data/external/scripts (README там же): new_counterparties.parquet — карточка компании,
new_counterparties_groups.parquet — связки «компания × группа ОКПД2» с уровнем (A/B/C), приоритетом и доказательством.
Блок для лота: компании, у которых группа ОКПД2 позиции лота среди их групп. Порядок — сила доказательства
именно по этой группе (уровень A/B/C описывает компанию, а не совпадение с лотом): сначала сильное доказательство
из СПб и ЛО, затем сильное из других регионов, затем только ассоциация по ОКВЭД; внутри — приоритет команды.
Сильное доказательство: госконтракты в группе, реестр производителей или лицензия, основной ОКВЭД равен группе. Моделью не оцениваются, score не выше NEW_SCORE_CAP — ниже кандидатов с историей.

NEW_POOL_DIR — папка с parquet (по умолчанию data/external в корне репозитория); пул нужен pandas и pyarrow,
как и модели. Нет файлов — блок пуст, подбор моделью работает.
"""
import logging
import os
import re
import threading
from collections import Counter
from pathlib import Path

from models import Candidate, Lot

POOL_DIR = Path(os.environ.get("NEW_POOL_DIR", Path(__file__).resolve().parents[2] / "data" / "external"))
PER_GROUP = 300        # сколько лучших компаний держать в памяти на группу ОКПД2
PER_LOT = 30           # сколько отдавать на лот; сервер обрежет до top_k
NEW_SCORE_CAP = 75.0
CORE_REGIONS = {"78", "47"}
TIER_TEXT = {"A": "уровень A: госконтракты, СПб/ЛО", "B": "уровень B: госконтракты или реестр производителей/лицензий",
             "C": "уровень C: СПб/ЛО, профильный основной ОКВЭД"}
REGISTRY_URLS = [("Росздравнадзор", "https://roszdravnadzor.gov.ru/services/licenses"),
                 ("719", "https://gisp.gov.ru/pp719v2/pub/prod/"), ("ГИСП", "https://gisp.gov.ru/pp719v2/pub/prod/"),
                 ("ПО", "https://reestr.digital.gov.ru/reestr/")]

_lock = threading.Lock()
_pool = None  # (блоки по группам: DataFrame, карточки: DataFrame) или False, если данных нет


ORDER = ["rank_key", "priority"]


def _load():
    import pandas as pd
    groups = pd.read_parquet(POOL_DIR / "new_counterparties_groups.parquet")
    core = groups.inn.str[:2].isin(CORE_REGIONS)
    ev = groups.evidence.fillna("")
    same_okved = pd.Series([f"ОКВЭД {g} (осн.)" in e for g, e in zip(groups.okpd2_group, ev)], index=groups.index)
    groups["strong"] = ev.str.contains("контракт") | ev.str.contains("лиценз|реестр|ГИСП|Минпромторг|РУ ") | same_okved
    # 2 — сильное доказательство из СПб/ЛО, 1 — сильное из других регионов, 0 — только ассоциация по ОКВЭД
    groups["rank_key"] = (groups.strong.astype("int8") * (1 + core.astype("int8"))).astype("int8")
    groups = (groups.sort_values(["okpd2_group", *ORDER], ascending=[True, False, False])
                    .groupby("okpd2_group", sort=False).head(PER_GROUP).reset_index(drop=True))
    cols = ["inn", "name", "name_short", "region", "is_msp", "role", "role_evidence", "tier",
            "mos_source", "registry_sources", "registry_source_date", "msp_source", "msp_source_date", "retrieved_at"]
    companies = pd.read_parquet(POOL_DIR / "new_counterparties.parquet", columns=cols)
    companies = companies[companies.inn.isin(set(groups.inn))].set_index("inn")
    return groups, companies


def pool():
    """Пул загружается один раз (~5 с) и живёт в памяти процесса."""
    global _pool
    with _lock:
        if _pool is None:
            try:
                _pool = _load()
                logging.info("Пул новых компаний: %d связок, %d компаний", len(_pool[0]), len(_pool[1]))
            except (OSError, ImportError, ValueError) as e:
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
        elif part:
            out.append(part[0].upper() + part[1:])
    return out


def _sources(inn: str, c) -> list[dict]:
    out = []
    if c.mos_source and (d := _date(c.retrieved_at)):
        out.append({"field": "Госконтракты", "source": c.mos_source, "url": "https://zakupki.mos.ru/", "checked_at": d})
    if c.registry_sources and (d := _date(c.registry_source_date) or _date(c.retrieved_at)):
        url = next((u for key, u in REGISTRY_URLS if key in c.registry_sources), None)
        if url:
            out.append({"field": "Реестр", "source": c.registry_sources, "url": url, "checked_at": d})
    if c.msp_source and (d := _date(c.msp_source_date) or _date(c.retrieved_at)):
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
        block = block[block.inn.map(companies.is_msp).fillna(0).astype(bool)]
    block = block.assign(items=block.okpd2_group.map(lot_groups))
    block = block.sort_values(["rank_key", "items", "priority"], ascending=False).drop_duplicates("inn").head(PER_LOT)
    out = []
    for rank, row in enumerate(block.itertuples(index=False)):
        c = companies.loc[row.inn]
        role = str(c.role or "не определена").split(" (")[0]
        reasons = _evidence(row.evidence)[:2] + [f"Компания из открытых источников, {TIER_TEXT.get(row.tier, row.tier)}"]
        out.append(Candidate(
            supplier_name=c.name_short or c.name or row.inn,
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
