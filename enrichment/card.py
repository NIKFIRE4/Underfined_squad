"""Сборка карточки компании из фактов: выбор значения по приоритету источников,
расхождения между источниками и флаги «Требует проверки» (ТЗ, раздел 6.5)."""

from datetime import date
from typing import Any

from .inn import inn_kind
from .models import Fact, now_utc

# Чем левее источник, тем выше доверие к его значению поля.
SOURCE_PRIORITY = ["rmsp", "fns_rsmp", "bo", "pb", "fns_sshr2019", "fns_paytax",
                   "fns_debtam", "fns_taxoffence", "reg_gisp", "reg_software", "history", "egrul", "rnp", "contacts"]

# Стартовые пороги, уточняем по распределениям на данных.
YOUNG_YEARS = 1.0
REVENUE_DROP = 0.5
MASS_DIRECTOR_COMPANIES = 5
TAX_ARREARS_MIN = 1000.0  # как у ФНС в признаке задолженности
NO_TAXES_REVENUE = 10_000_000  # выручка, при которой нулевые налоги подозрительны

ACTIVE_STATUSES = {"действующая организация", "действующий"}

# Сведения за годы раньше этого в карточку не попадают: на 2026 год они уже не описывают компанию.
MIN_INFO_YEAR = 2024
FINANCE_FIELDS = ("revenue", "revenue_prev", "net_profit", "equity", "assets", "finance_year")
# поле (или группа полей) из выгрузки ФНС → поле с датой, на которую выгрузка составлена
DATED_FIELDS = {("employees",): "employees_as_of", ("taxes_paid", "taxes_paid_detail"): "taxes_paid_as_of",
                ("tax_arrears_total", "tax_arrears"): "tax_arrears_as_of", ("tax_fines_total",): "tax_fines_as_of"}

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


def ogrn_year(ogrn: str | None) -> int | None:
    """Год присвоения ОГРН: 2-я и 3-я цифры (ОГРН юрлица — 13 цифр, ОГРНИП — 15).
    Для компаний старше 2002 года это год перерегистрации, то есть нижняя граница возраста."""
    s = (ogrn or "").strip()
    if not s.isdigit() or len(s) not in (13, 15):
        return None
    yy = int(s[1:3])
    year = 2000 + yy if yy <= date.today().year % 100 else 1900 + yy
    return year if year >= 2002 else None


def _prev_tax_revenue(by_year: dict | None, last_year: Any) -> float | None:
    if not by_year or last_year is None:
        return None
    return (by_year.get(str(int(last_year) - 1)) or {}).get("revenue")


def links(inn: str, c: dict) -> list[dict]:
    """Ссылки на компанию на площадках-источниках и каналы связи: [{title, url, kind}]."""
    from urllib.parse import quote
    out = []

    def add(title, url, kind):
        out.append({"title": title, "url": url, "kind": kind})

    if c.get("website"):
        site = c["website"] if str(c["website"]).startswith("http") else f"https://{c['website']}"
        add("Сайт компании (из реестра ПО)", site, "contact")
    if c.get("contact_contract_url"):
        add("Контракт ЕИС с контактами поставщика", c["contact_contract_url"], "contact")
    # по ИНН, а не по названию: у ИП название — ФИО, поиск находит однофамильцев
    add("Контракты 44-ФЗ компании в ЕИС (все годы и регионы)",
        f"https://zakupki.gov.ru/epz/contract/search/results.html?fz44=on&searchString={quote(inn)}", "profile")
    if c.get("bo_id"):
        add("Бухотчётность (ГИР БО)", f"https://bo.nalog.gov.ru/organizations-card/{c['bo_id']}", "profile")
    add("Реестр МСП", f"https://rmsp.nalog.ru/search.html?query={inn}", "profile")
    add("ФНС «Прозрачный бизнес»", f"https://pb.nalog.ru/search.html#mode=search-all&queryAll={inn}&page=1&pageSize=10", "profile")
    add("Реестр недобросовестных поставщиков", f"https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html?searchString={inn}", "check")
    return out


def risk_flags(c: dict) -> list[dict]:
    flags = []

    def add(code: str, text: str) -> None:
        flags.append({"code": code, "text": text})

    if c.get("age_years") is not None and c["age_years"] < YOUNG_YEARS:
        since = c.get("reg_date") or f"{c.get('reg_year')} г., оценка по ОГРН"
        add("young", f"Компания моложе года (с {since})")
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


def _year(v: Any) -> int | None:
    try:
        return int(str(v)[:4])
    except (TypeError, ValueError):
        return None


def drop_stale(c: dict) -> None:
    """Убирает сведения за годы до MIN_INFO_YEAR: отчётность, выгрузки ФНС, годы в finance_by_year."""
    if isinstance(by_year := c.get("finance_by_year"), dict):
        c["finance_by_year"] = {y: v for y, v in by_year.items() if (_year(y) or 0) >= MIN_INFO_YEAR} or None
    fy = _year(c.get("finance_year"))
    if fy is not None and fy < MIN_INFO_YEAR:
        for k in FINANCE_FIELDS:
            c[k] = None
    elif fy == MIN_INFO_YEAR:
        c["revenue_prev"] = None  # это выручка за предыдущий, слишком старый год
    ty = _year(c.get("finance_tax_year"))
    if ty is not None and ty < MIN_INFO_YEAR:
        c["revenue_tax"] = c["finance_tax_year"] = c["finance_tax_by_year"] = None
    for fields, as_of in DATED_FIELDS.items():
        y = _year(c.get(as_of))
        if y is not None and y < MIN_INFO_YEAR:
            for k in (*fields, as_of):
                c[k] = None


def build_company(inn: str, facts: list[Fact], runs: dict[str, str]) -> tuple[dict, dict]:
    """Возвращает (строка витрины companies, карточка с источниками по полям)."""
    card = resolve(facts)
    c: dict[str, Any] = {k: v["value"] for k, v in card.items()}
    c["inn"] = inn
    c["kind"] = inn_kind(inn)
    drop_stale(c)
    for k in [k for k in card if c.get(k) is None and card[k]["value"] is not None]:
        del card[k]  # устаревшее не показываем и в разборе по источникам
    if "finance_by_year" in card:
        card["finance_by_year"]["value"] = c["finance_by_year"]

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
    elif c.get("is_smp") and c.get("smp_as_of"):
        # реестр МСП включает только действующие компании: есть в свежем снимке — значит, действует
        c["is_active"] = not closed
    else:
        c["is_active"] = None
    c["age_years"] = _years_since(c.get("reg_date"))
    c["age_source"] = "reg_date" if c["age_years"] is not None else None
    if c["age_years"] is None and (year := ogrn_year(c.get("ogrn"))):
        # точной даты нет (ЕГРЮЛ/ПБ не запрашивались) — год из ОГРН, считаем от середины года
        c["reg_year"] = year
        c["age_years"] = _years_since(f"{year}-07-01")
        c["age_source"] = "ogrn"

    flags = risk_flags(c)
    c["risk_flags"] = flags
    c["needs_review"] = bool(flags)

    ok = sorted(s for s, st in runs.items() if st == "ok")
    c["sources_ok"] = ok
    c["enrichment_status"] = "full" if ok and len(ok) == len(runs) else ("partial" if ok else "failed")
    c["updated_at"] = now_utc()
    return c, card
