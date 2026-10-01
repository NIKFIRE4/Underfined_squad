"""Сборка карточки компании из фактов: выбор значения по приоритету источников,
расхождения между источниками и флаги «Требует проверки» (ТЗ, раздел 6.5)."""

from datetime import date
from typing import Any

from .inn import inn_kind
from .models import Fact, now_utc

# Чем левее источник, тем выше доверие к его значению поля.
SOURCE_PRIORITY = ["rmsp", "fns_rsmp", "bo", "pb", "fns_sshr2019", "fns_paytax",
                   "fns_debtam", "fns_taxoffence", "egrul", "rnp"]

# Стартовые пороги, уточняем по распределениям на данных.
YOUNG_YEARS = 1.0
REVENUE_DROP = 0.5
MASS_DIRECTOR_COMPANIES = 5
TAX_ARREARS_MIN = 1000.0  # как у ФНС в признаке задолженности
NO_TAXES_REVENUE = 10_000_000  # выручка, при которой нулевые налоги подозрительны

ACTIVE_STATUSES = {"действующая организация", "действующий"}

# Поля, расхождение которых между источниками стоит показать в карточке.
COMPARABLE = {"ogrn", "kpp", "reg_date", "okved_main", "smp_category", "status", "region_code"}


def _rank(source: str) -> int:
    return SOURCE_PRIORITY.index(source) if source in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def resolve(facts: list[Fact]) -> dict[str, dict]:
    """{field: {value, source, fetched_at, conflicts: [{value, source}]}}"""
    by_field: dict[str, list[Fact]] = {}
    for f in facts:
        by_field.setdefault(f.field, []).append(f)
    out = {}
    for name, fs in by_field.items():
        fs.sort(key=lambda f: _rank(f.source))
        best = fs[0]
        item = {"value": best.value, "source": best.source, "fetched_at": best.fetched_at.isoformat()}
        if name in COMPARABLE:
            others = [{"value": f.value, "source": f.source} for f in fs[1:]
                      if str(f.value).strip().lower() != str(best.value).strip().lower()]
            if others:
                item["conflicts"] = others
        out[name] = item
    return out


def _years_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return round((date.today() - date.fromisoformat(iso)).days / 365.25, 2)
    except ValueError:
        return None


def _prev_tax_revenue(by_year: dict | None, last_year: Any) -> float | None:
    if not by_year or last_year is None:
        return None
    return (by_year.get(str(int(last_year) - 1)) or {}).get("revenue")


def risk_flags(c: dict) -> list[dict]:
    flags = []

    def add(code: str, text: str) -> None:
        flags.append({"code": code, "text": text})

    if c.get("age_years") is not None and c["age_years"] < YOUNG_YEARS:
        add("young", f"Компания моложе года (с {c['reg_date']})")
    rev, prev = c.get("revenue"), c.get("revenue_prev")
    if rev is not None and prev and prev > 0 and rev < prev * (1 - REVENUE_DROP):
        add("revenue_drop", f"Выручка упала на {round((1 - rev / prev) * 100)}% к прошлому году")
    if c.get("equity") is not None and c["equity"] < 0:
        add("negative_equity", "Отрицательный капитал")
    if c.get("is_invalid_info"):
        add("invalid_info", "В ЕГРЮЛ есть отметка о недостоверности сведений")
    if c.get("is_director_invalid") or c.get("is_founder_invalid"):
        add("invalid_person", "Недостоверные сведения о руководителе или учредителе")
    arrears = c.get("tax_arrears_total")
    if (arrears or 0) > TAX_ARREARS_MIN:
        add("tax_debt", f"Недоимка по налогам {arrears:,.0f} ₽".replace(",", " "))
    elif c.get("has_tax_debt"):
        add("tax_debt", "Задолженность по налогам более 1000 ₽")
    if c.get("taxes_paid") == 0 and (c.get("revenue") or 0) > NO_TAXES_REVENUE:
        add("no_taxes", "Нулевые уплаченные налоги при выручке более 10 млн ₽")
    if c.get("no_tax_reporting"):
        add("no_reporting", "Не сдаёт налоговую отчётность более года")
    if c.get("is_mass_address"):
        add("mass_address", "Адрес массовой регистрации")
    if (n := c.get("director_companies_max")) and n >= MASS_DIRECTOR_COMPANIES:
        add("mass_director", f"Руководитель возглавляет {n} компаний")
    if c.get("rnp_ever") and not c.get("in_rnp"):
        add("rnp_history", "Ранее был в РНП")
    return flags


def build_company(inn: str, facts: list[Fact], runs: dict[str, str]) -> tuple[dict, dict]:
    """Возвращает (строка витрины companies, карточка с источниками по полям)."""
    card = resolve(facts)
    c: dict[str, Any] = {k: v["value"] for k, v in card.items()}
    c["inn"] = inn
    c["kind"] = inn_kind(inn)

    # фолбэки между источниками
    if c.get("employees") is None:
        c["employees"] = c.get("employees_rmsp")
    if c.get("is_smp") is None and c.get("smp_category") is not None:
        c["is_smp"] = c["smp_category"] > 0
    if c.get("revenue") is None and c.get("revenue_tax") is not None:
        c["revenue"] = c["revenue_tax"]
        c["revenue_prev"] = _prev_tax_revenue(c.get("finance_tax_by_year"), c.get("finance_tax_year"))
        c["finance_year"] = c.get("finance_tax_year")
    if not c.get("region_code"):
        # КПП отражает текущую постановку на учёт (Ростелеком: ИНН 77, КПП 78), ИНН — регион первой регистрации
        kpp = c.get("kpp") or ""
        c["region_code"] = kpp[:2] if kpp[:2].isdigit() else inn[:2]

    # у ИП в ПБ нет текстового статуса, только признак прекращения
    status = (c.get("status") or "").strip().lower()
    closed = bool(c.get("is_liquidated") or c.get("liquidation_date"))
    if status:
        c["is_active"] = status in ACTIVE_STATUSES and not closed
    elif c.get("is_liquidated") is not None or c.get("found_egrul"):
        c["is_active"] = not closed
    else:
        c["is_active"] = None
    c["age_years"] = _years_since(c.get("reg_date"))

    flags = risk_flags(c)
    c["risk_flags"] = flags
    c["needs_review"] = bool(flags)

    ok = sorted(s for s, st in runs.items() if st == "ok")
    c["sources_ok"] = ok
    c["enrichment_status"] = "full" if ok and len(ok) == len(runs) else ("partial" if ok else "failed")
    c["updated_at"] = now_utc()
    return c, card
