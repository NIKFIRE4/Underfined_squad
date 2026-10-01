"""Адаптер под контракт web-service (ветка web-service-mvp, web-service/INTEGRATION.md).

В web-service/integrations/enricher.py достаточно:

    import sys; sys.path.insert(0, "..")          # корень репозитория, если пакет не установлен
    from enrichment.webservice import READY, enrich

Настройки через переменные окружения:
  ENRICHMENT_DB        URL базы обогащения (по умолчанию sqlite:///data/enrichment.db)
  ENRICH_LIVE=1        дозапрашивать карточки, которых нет в БД (по умолчанию выключено)
  ENRICH_LIVE_TIMEOUT  общий лимит живого дозапроса на лот, секунды (по умолчанию 15)
  ENRICH_NEW_MAX       сколько новых компаний добавлять к лоту (по умолчанию 10)
"""

import asyncio
import logging
import os
from datetime import timezone
from functools import lru_cache
from typing import Any

from sqlalchemy import select

from . import storage
from .card import build_company
from .discovery import classify_role, find_new_companies
from .http import Http
from .inn import is_valid_inn
from .models import now_utc
from .pipeline import fetch_all

log = logging.getLogger("enrichment")

READY = True

NEW_SCORE_CAP = 75.0  # новые компании видны в выдаче, но ниже сильных кандидатов с историей
SOURCE_LABELS = {
    "egrul": "ЕГРЮЛ/ЕГРИП", "pb": "ФНС «Прозрачный бизнес»", "rmsp": "Реестр МСП",
    "bo": "ГИР БО (бухотчётность)", "rnp": "Реестр недобросовестных поставщиков ЕИС",
    "fns_rsmp": "Реестр МСП (открытые данные ФНС)", "fns_sshr2019": "Численность (открытые данные ФНС)",
    "fns_paytax": "Уплаченные налоги (открытые данные ФНС)", "fns_debtam": "Недоимки (открытые данные ФНС)",
    "fns_taxoffence": "Налоговые правонарушения (открытые данные ФНС)",
    "reg_gisp": "Реестр промышленной продукции (ПП 719)", "reg_software": "Реестр российского ПО",
    "history": "История закупок АИС ГЗ и ЭМ (выгрузка организаторов)",
    "contacts": "Контракты ЕИС (контакты поставщика)",
}
# поля, которые показываем в sources: то, на чём основаны фильтры, роль, статус и причины
SOURCE_FIELDS = ("status", "reg_date", "okved_main", "is_smp", "smp_category", "revenue", "employees",
                 "in_rnp", "in_gisp", "in_software_registry", "tax_arrears_total", "hist_okpd2_codes")
ROLE_LABELS = {"manufacturer": "Производитель", "distributor": "Дистрибьютор",
               "supplier": "Поставщик-исполнитель", "unknown": "Не определена"}
SMP_NAMES = {1: "микропредприятие", 2: "малое предприятие", 3: "среднее предприятие"}


def _url(source: str, inn: str, card: dict) -> str:
    if source == "bo" and (bo_id := (card.get("bo_id") or {}).get("value")):
        return f"https://bo.nalog.gov.ru/organizations-card/{bo_id}"
    return {
        "egrul": "https://egrul.nalog.ru/",
        "pb": f"https://pb.nalog.ru/search.html#quick-result?queryAll={inn}",
        "rmsp": f"https://rmsp.nalog.ru/search.html?query={inn}",
        "bo": f"https://bo.nalog.gov.ru/search?query={inn}",
        "rnp": f"https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html?searchString={inn}",
        "reg_gisp": "https://gisp.gov.ru/pp719v2/pub/prod/",
        "reg_software": "https://reestr.digital.gov.ru/reestr/",
        "history": "https://zakupki.gov.ru/",
        "contacts": card.get("contact_contract_url", {}).get("value") or "https://zakupki.gov.ru/epz/contract/search/results.html",
    }.get(source) or f"https://www.nalog.gov.ru/opendata/7707329152-{source.removeprefix('fns_')}/"


@lru_cache(maxsize=1)
def _engine():
    return storage.connect(os.environ.get("ENRICHMENT_DB", "sqlite:///data/enrichment.db"))


@lru_cache(maxsize=1)
def _known_inns() -> frozenset[str]:
    """ИНН с историей закупок: все, по кому грузились выгрузки ФНС (= ИНН из файла поставщиков)."""
    with _engine().connect() as conn:
        rows = conn.execute(select(storage.enrichment_runs.c.inn)
                            .where(storage.enrichment_runs.c.source == "fns_sshr2019").distinct())
        return frozenset(r[0] for r in rows)


def _cards(inns: list[str]) -> dict[str, tuple[dict, dict]]:
    facts, runs = storage.load_all(_engine(), set(inns))
    return {i: build_company(i, facts[i], runs.get(i, {})) for i in inns if i in facts}


def _live_fetch(inns: list[str]) -> None:
    """Дозапрос карточек с общим таймаутом; не успевшие останутся «Не обогащено»."""
    timeout = float(os.environ.get("ENRICH_LIVE_TIMEOUT", "15"))

    async def run():
        http = Http()
        try:
            async def one(inn):
                results = await fetch_all(http, inn, ["rmsp", "bo", "rnp", "egrul"])
                storage.save_results(_engine(), results)
            await asyncio.wait_for(asyncio.gather(*(one(i) for i in inns), return_exceptions=True), timeout)
        except asyncio.TimeoutError:
            log.warning("live enrich: таймаут %ss, часть карточек не догружена", timeout)
        finally:
            await http.aclose()

    asyncio.run(run())


def _money(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.1f} млрд ₽"
    if v >= 1e6:
        return f"{v / 1e6:.1f} млн ₽"
    return f"{v / 1e3:.0f} тыс. ₽"


def _reasons(row: dict, role: dict) -> list[str]:
    out = []
    if role["value"] in ("manufacturer", "distributor") and len(role["evidence"]) > 1:
        out.append(role["evidence"][1])
    if row.get("revenue"):
        out.append(f"Выручка {_money(row['revenue'])} за {row.get('finance_year') or 'последний'} г.")
    if row.get("is_smp") and row.get("smp_category"):
        out.append(f"В реестре МСП: {SMP_NAMES.get(row['smp_category'], 'МСП')}")
    if row.get("age_years") and row["age_years"] >= 3:
        out.append(f"Работает {int(row['age_years'])} лет")
    return out


def _sources(inn: str, card: dict) -> list[dict]:
    out = []
    for f in SOURCE_FIELDS:
        item = card.get(f)
        if item is None:
            continue
        src = item["source"]
        out.append({"field": f, "source": SOURCE_LABELS.get(src, src), "url": _url(src, inn, card),
                    "checked_at": item["fetched_at"] if "+" in item["fetched_at"]
                    else item["fetched_at"] + "+00:00"})
    return out


def _status(row: dict, current: str | None) -> tuple[str, list[str]]:
    flags = [f["text"] for f in row.get("risk_flags") or []]
    if flags:
        return "Требует проверки", flags[:2]
    return current or "Активный участник", []


def _apply(c: Any, row: dict, card: dict, lot_okpd2: list[str]) -> None:
    role = classify_role(row | {k: v["value"] for k, v in card.items()}, lot_okpd2)
    # модель отдаёт ИНН вместо названия (сервер не принимает пустое имя) — заменяем на название из карточки
    own_name = c.supplier_name if c.supplier_name != c.supplier_inn else ""
    c.supplier_name = own_name or row.get("name_short") or row.get("name_full") or c.supplier_inn
    c.supplier_kpp = c.supplier_kpp or row.get("kpp") or ""
    c.region = c.region or row.get("region_code") or ""
    if row.get("is_smp") is not None:
        c.is_smp = bool(row["is_smp"])
    c.role = role["label"]
    status, flag_texts = _status(row, None if c.status == "Требует проверки" else c.status)
    c.status = status
    c.reasons = list(dict.fromkeys(list(c.reasons) + flag_texts + _reasons(row, role)))[:4]
    c.sources = list(c.sources) + _sources(c.supplier_inn, card)
    c.enrichment_status = "Обогащено" if row.get("enrichment_status") == "full" else "Обогащено частично"


def _lot_constraints(lot) -> tuple[list[str], bool]:
    codes = sorted({(i.get("okpd2_code") or "").strip() for i in lot.items if i.get("okpd2_code")})
    is_smp = str(lot.notice.get("is_smp", "")).strip().lower() in ("true", "1", "да")
    return codes, is_smp


def enrich(lot, candidates: list) -> list:
    """Контракт web-service: обогатить кандидатов модели, отфильтровать, добавить новых."""
    okpd2, only_smp = _lot_constraints(lot)
    inns = [c.supplier_inn for c in candidates if is_valid_inn(c.supplier_inn or "")]
    cards = _cards(inns)
    missing = [i for i in inns if i not in cards]
    if missing and os.environ.get("ENRICH_LIVE") == "1":
        _live_fetch(missing)
        cards |= _cards(missing)

    out = []
    for c in candidates:
        got = cards.get(c.supplier_inn)
        if got is None:
            c.status = "Требует проверки"
            c.enrichment_status = "Не обогащено"
            out.append(c)
            continue
        row, card = got
        if row.get("is_active") is False or row.get("in_rnp"):
            continue  # жёсткий фильтр ФТ-08
        if only_smp and row.get("is_smp") is False:
            continue
        _apply(c, row, card, okpd2)
        out.append(c)

    # новые компании (ФТ-06)
    cls = type(candidates[0]) if candidates else _candidate_cls()
    limit = int(os.environ.get("ENRICH_NEW_MAX", "10"))
    if okpd2 and limit and cls is not None:
        exclude = set(_known_inns()) | {c.supplier_inn for c in candidates}
        for n in find_new_companies(_engine(), okpd2, regions={"78", "47"}, exclude=exclude,
                                    only_smp=only_smp, limit=limit):
            new = cls(supplier_name=n.name or n.inn, supplier_inn=n.inn)
            new.score = round(min(n.score, 100.0) * NEW_SCORE_CAP / 100.0, 1)
            new.is_new = True
            new.status = "Новый в пуле"
            new.region = n.region_code or ""
            new.is_smp = n.is_smp
            role = classify_role(n.as_company(), okpd2)
            new.role = role["label"]
            new.reasons = [e["text"] for e in n.evidence[:3]]
            new.sources = [{"field": "match", "source": SOURCE_LABELS.get(e["source"], e["source"]),
                            "url": _url(e["source"], n.inn, {}), "checked_at": _iso(e["fetched_at"])}
                           for e in n.evidence[:3]]
            new.enrichment_status = "Обогащено частично"
            out.append(new)
    return out


def _iso(ts) -> str:
    if ts is None:
        return now_utc().isoformat()
    if ts.tzinfo is None:  # SQLite возвращает naive datetime, хотя пишем UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _candidate_cls():
    try:
        from models import Candidate  # модуль web-service
        return Candidate
    except ImportError:
        return None
