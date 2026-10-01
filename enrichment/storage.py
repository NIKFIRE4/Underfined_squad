"""Хранилище: SQLite по умолчанию, PostgreSQL по URL (`postgresql+psycopg://...`).

raw_responses   — сырые ответы источников, чтобы перепарсить без сети
company_facts   — поле × источник со значением и датой получения (ФТ-07)
enrichment_runs — статус каждого источника по ИНН: ok / error / captcha (для догрузки)
companies       — плоская витрина для ранкера и UI, собирается из company_facts
"""

from typing import Any, Iterable

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Index, Integer, MetaData, String, Table, Text,
    create_engine, inspect, select, text,
)
from sqlalchemy.engine import Engine

from .models import Fact, SourceResult, now_utc

metadata = MetaData()

raw_responses = Table(
    "raw_responses", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("inn", String(12), index=True, nullable=False),
    Column("source", String(32), nullable=False),
    Column("request_key", String(128), nullable=False),
    Column("status_code", Integer),
    Column("body", JSON),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
)

company_facts = Table(
    "company_facts", metadata,
    Column("inn", String(12), primary_key=True),
    Column("field", String(64), primary_key=True),
    Column("source", String(32), primary_key=True),
    Column("value", JSON),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
)

enrichment_runs = Table(
    "enrichment_runs", metadata,
    Column("inn", String(12), primary_key=True),
    Column("source", String(32), primary_key=True),
    Column("status", String(16), nullable=False),  # ok | error | captcha
    Column("error", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

companies = Table(
    "companies", metadata,
    Column("inn", String(12), primary_key=True),
    Column("kind", String(2)),  # ul | ip
    Column("ogrn", String(15)),
    Column("kpp", String(9)),
    Column("name_full", Text),
    Column("name_short", Text),
    Column("region_code", String(2)),
    Column("address", Text),
    Column("status", Text),
    Column("is_active", Boolean),
    Column("reg_date", String(10)),
    Column("liquidation_date", String(10)),
    Column("age_years", Float),
    Column("director", JSON),
    Column("okved_main", String(16)),
    Column("okved_main_name", Text),
    Column("okved_extra", JSON),
    Column("charter_capital", Float),
    Column("is_smp", Boolean),
    Column("smp_category", Integer),
    Column("employees", Float),
    Column("tax_regimes", JSON),
    Column("revenue", Float),
    Column("revenue_prev", Float),
    Column("net_profit", Float),
    Column("equity", Float),
    Column("assets", Float),
    Column("finance_year", Integer),
    Column("revenue_tax", Float),
    Column("taxes_paid", Float),
    Column("tax_arrears_total", Float),
    Column("tax_fines_total", Float),
    Column("has_tax_debt", Boolean),
    Column("no_tax_reporting", Boolean),
    Column("is_invalid_info", Boolean),
    Column("is_mass_address", Boolean),
    Column("director_companies_max", Integer),
    Column("in_rnp", Boolean),
    Column("in_gisp", Boolean),
    Column("in_software_registry", Boolean),
    Column("rnp_ever", Boolean),
    Column("risk_flags", JSON),  # [{"code", "text"}] — основания статуса «Требует проверки»
    Column("needs_review", Boolean),
    Column("enrichment_status", String(16)),  # full | partial | failed
    Column("sources_ok", JSON),
    Column("updated_at", DateTime(timezone=True)),
)

# Пул компаний из реестра МСП для поиска новых поставщиков (ФТ-06). Снимок пересоздаётся целиком.
pool_companies = Table(
    "pool_companies", metadata,
    Column("inn", String(12), primary_key=True),
    Column("kind", String(2)),
    Column("ogrn", String(15)),
    Column("name_full", Text),
    Column("name_short", Text),
    Column("region_code", String(2), index=True),
    Column("locality", Text),
    Column("smp_category", Integer),
    Column("smp_since", String(10)),
    Column("employees", Float),
    Column("okved_main", String(16), index=True),
    Column("okved_main_name", Text),
    Column("okved_extra", JSON),
    Column("products", JSON),  # ОКПД2 производимой продукции из реестра МСП
    Column("licenses_count", Integer),
    Column("as_of", String(10)),
    Column("source", String(32), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
)

# ОКВЭД и ОКПД2 продукции пула построчно, для поиска по префиксу кода с индексом
pool_codes = Table(
    "pool_codes", metadata,
    Column("inn", String(12), primary_key=True),
    Column("code", String(32), primary_key=True),
    Column("kind", String(8), primary_key=True),  # okved_main | okved | product
    Index("ix_pool_codes_code", "code"),
)

# Позиции реестров производителей и правообладателей по ОКПД2 (РРПП, реестр ПО)
registry_items = Table(
    "registry_items", metadata,
    Column("inn", String(12), primary_key=True),
    Column("registry", String(16), primary_key=True),
    Column("okpd2", String(32), primary_key=True),
    Column("items_count", Integer),
    Column("sample", Text),
    Column("org_name", Text),
    Column("source", String(32), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Index("ix_registry_items_okpd2", "okpd2"),
)


def connect(url: str) -> Engine:
    engine = create_engine(url, future=True)
    metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Мини-миграция для хакатона: новые колонки витрины добавляются к существующей таблице."""
    insp = inspect(engine)
    for table in metadata.sorted_tables:
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                ddl = col.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))


def _upsert(engine: Engine, table: Table, rows: list[dict], keys: list[str]) -> None:
    if not rows:
        return
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(table)
    update = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in keys}
    with engine.begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=keys, set_=update), rows)


def save_results(engine: Engine, results: Iterable[SourceResult]) -> None:
    raws, facts, runs = [], {}, []
    for res in results:
        raws += [r.__dict__ for r in res.raws]
        for f in res.facts:  # при повторе поля внутри источника побеждает последнее значение
            facts[(f.inn, f.field, f.source)] = f.__dict__
        status = "ok" if res.ok else ("captcha" if res.error == "captcha" else "error")
        runs.append({"inn": res.inn, "source": res.source, "status": status,
                     "error": res.error, "updated_at": now_utc()})
    if raws:
        with engine.begin() as conn:
            conn.execute(raw_responses.insert(), raws)
    facts_rows = list(facts.values())
    for i in range(0, len(facts_rows), 5000):
        _upsert(engine, company_facts, facts_rows[i : i + 5000], ["inn", "field", "source"])
    for i in range(0, len(runs), 5000):
        _upsert(engine, enrichment_runs, runs[i : i + 5000], ["inn", "source"])


def load_facts(engine: Engine, inn: str) -> list[Fact]:
    with engine.connect() as conn:
        rows = conn.execute(select(company_facts).where(company_facts.c.inn == inn)).mappings()
        return [Fact(**r) for r in rows]


def load_all(engine: Engine, inns: set[str] | None = None) -> tuple[dict, dict]:
    """Все факты и статусы источников одним проходом: для пересборки витрины по 44 тыс. ИНН."""
    facts: dict[str, list[Fact]] = {}
    runs: dict[str, dict[str, str]] = {}
    with engine.connect() as conn:
        for r in conn.execute(select(company_facts)).mappings():
            if inns is None or r["inn"] in inns:
                facts.setdefault(r["inn"], []).append(Fact(**r))
        for r in conn.execute(select(enrichment_runs)).mappings():
            if inns is None or r["inn"] in inns:
                runs.setdefault(r["inn"], {})[r["source"]] = r["status"]
    return facts, runs


def load_runs(engine: Engine, inn: str) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(select(enrichment_runs).where(enrichment_runs.c.inn == inn)).mappings()
        return {r["source"]: r["status"] for r in rows}


def done_inns(engine: Engine, sources: list[str]) -> set[str]:
    """ИНН, у которых все указанные источники уже отработали успешно."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(enrichment_runs.c.inn, enrichment_runs.c.source)
            .where(enrichment_runs.c.status == "ok")
        ).all()
    by_inn: dict[str, set[str]] = {}
    for inn, src in rows:
        by_inn.setdefault(inn, set()).add(src)
    need = set(sources)
    return {inn for inn, s in by_inn.items() if need <= s}


def save_companies(engine: Engine, rows: list[dict[str, Any]], chunk: int = 2000) -> None:
    cols = {c.name for c in companies.columns}
    rows = [{k: r.get(k) for k in cols} for r in rows]
    for i in range(0, len(rows), chunk):
        _upsert(engine, companies, rows[i : i + chunk], ["inn"])


def save_company(engine: Engine, row: dict[str, Any]) -> None:
    save_companies(engine, [row])


def get_company(engine: Engine, inn: str) -> dict | None:
    with engine.connect() as conn:
        r = conn.execute(select(companies).where(companies.c.inn == inn)).mappings().first()
        return dict(r) if r else None


def clear_pool(engine: Engine) -> None:
    """Снимок РМСП заменяется целиком."""
    with engine.begin() as conn:
        conn.execute(pool_codes.delete())
        conn.execute(pool_companies.delete())


def insert_pool_chunk(engine: Engine, comps: list[dict], codes: list[dict]) -> None:
    with engine.begin() as conn:
        if comps:
            conn.execute(pool_companies.insert(), comps)
        if codes:
            conn.execute(pool_codes.insert(), codes)


def replace_registry(engine: Engine, registry: str, items: list[dict], chunk: int = 10000) -> None:
    with engine.begin() as conn:
        conn.execute(registry_items.delete().where(registry_items.c.registry == registry))
    for i in range(0, len(items), chunk):
        with engine.begin() as conn:
            conn.execute(registry_items.insert(), items[i : i + chunk])
