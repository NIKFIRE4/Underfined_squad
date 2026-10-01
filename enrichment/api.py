"""HTTP API обогащения: ИНН → всё, что известно о компании из открытых источников.

  GET  /api/suppliers/{inn}            карточка (путь из ТЗ, раздел 8); ?refresh=true — перезапросить источники
  POST /api/suppliers/batch            {"inns": [...], "refresh": false} — до 50 ИНН за запрос
  GET  /api/health                     состояние сервиса и базы

Логика «один раз заполнить»: база заполняется заранее (scripts/enrich_all.sh или дамп), а эндпоинт
ходит в интернет только за источниками, которые по этому ИНН ещё ни разу не запрашивались,
или по refresh=true. Капчу ФНС не ждём: что не успело за таймаут, помечается в sources_status.

Запуск: uvicorn enrichment.api:app --host 0.0.0.0 --port 8010   (в docker compose — сервис enrichment-api)
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from functools import lru_cache

from . import history, storage
from .card import build_company, links
from .discovery import classify_role
from .http import Http
from .inn import inn_kind, is_valid_inn
from .pipeline import fetch_all
from .webservice import SOURCE_LABELS, _url

log = logging.getLogger("enrichment")

LIVE_SOURCES = ["pb", "bo", "rmsp", "rnp", "egrul", "contacts"]
SOURCE_TIMEOUT = float(os.environ.get("ENRICH_API_SOURCE_TIMEOUT", "15"))
BATCH_MAX = 50
BATCH_CONCURRENCY = 4

state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["engine"] = storage.connect(os.environ.get("ENRICHMENT_DB", "sqlite:///data/enrichment.db"))
    state["http"] = Http()
    yield
    await state["http"].aclose()


DESCRIPTION = """
По ИНН собирает всё, что известно о компании из **бесплатных открытых источников** (без нейросетей):

| Источник | Что даёт |
|---|---|
| ЕГРЮЛ/ЕГРИП (egrul.nalog.ru) | ОГРН, КПП, руководитель, даты регистрации и прекращения |
| ФНС «Прозрачный бизнес» (pb.nalog.ru) | статус, ОКВЭД, адрес, уставный капитал, спецрежимы, налоги, недостоверность, массовость |
| Реестр МСП (rmsp.nalog.ru + выгрузка) | категория МСП, ОКВЭД, численность, лицензии, продукция |
| ГИР БО (bo.nalog.gov.ru) | выручка, прибыль, капитал, активы по годам |
| РНП ЕИС (zakupki.gov.ru) | записи в реестре недобросовестных поставщиков |
| Открытые данные ФНС | численность, уплаченные налоги, недоимки, налоговые штрафы |
| РРПП (ПП 719), реестр ПО | производитель / правообладатель с ОКПД2 продукции |
| История закупок (выгрузка организаторов) | участия, победы, ассортимент ОКПД2, заказчики |

**Один раз заполнить:** база заполняется заранее (`scripts/enrich_all.sh` или дамп). Эндпоинт ходит в интернет
только за источниками, которые по этому ИНН ещё не запрашивались, или при `refresh=true`.
Капчу ФНС не ждём: ответ приходит за секунды, недоступный источник помечается в `sources_status`.

Подробности — `docs/enrichment/README.md`.
"""

app = FastAPI(title="Обогащение контрагентов", version="1.0", lifespan=lifespan, description=DESCRIPTION,
              openapi_tags=[{"name": "Контрагенты", "description": "Карточка компании по ИНН"},
                            {"name": "Служебное"}])

from .progress import router as progress_router  # noqa: E402

app.include_router(progress_router)

EXAMPLE = json.loads((Path(__file__).parent / "api_example.json").read_text(encoding="utf-8"))


class Role(BaseModel):
    value: Literal["manufacturer", "distributor", "supplier", "unknown"]
    label: str = Field(description="Производитель / Правообладатель / Дистрибьютор / Поставщик-исполнитель / Не определена")
    confidence: Literal["high", "medium", "low"] = Field(
        description="high — совпали правила по ОКВЭД и по реестру/истории; medium — одно правило; low — только история")
    evidence: list[str] = Field(description="Доказательства роли, готовые фразы для карточки")


class RiskFlag(BaseModel):
    code: str = Field(description="young, revenue_drop, negative_equity, invalid_info, tax_debt, no_taxes, "
                                  "no_reporting, mass_address, mass_director, rnp_history, …")
    text: str = Field(description="Причина статуса «Требует проверки» человеческим языком")


class FieldValue(BaseModel):
    value: Any
    source: str = Field(description="Код источника: pb, egrul, rmsp, bo, rnp, fns_*, reg_*, history")
    fetched_at: str = Field(description="Когда значение получено из источника, ISO 8601")
    conflicts: list[dict] | None = Field(None, description="Другие значения того же поля из других источников")


class SourceStatus(BaseModel):
    source: str
    name: str
    status: Literal["ok", "error", "captcha"]
    url: str = Field(description="Где проверить вручную")


class SupplierCard(BaseModel):
    inn: str
    kind: Literal["ul", "ip"] = Field(description="ul — юрлицо, ip — ИП")
    cached: bool = Field(description="true — ответ целиком из базы, в интернет не ходили")
    fetched_now: list[str] = Field(description="Источники, запрошенные в этом вызове")
    company: dict[str, Any] = Field(description=(
        "Плоская карточка (витрина companies + все поля): name_full, name_short, ogrn, kpp, region_code, address, "
        "status, is_active, reg_date, age_years, director, okved_main, okved_extra, is_smp, smp_category, employees, "
        "revenue, revenue_prev, net_profit, equity, finance_by_year, taxes_paid, tax_arrears_total, tax_regimes, "
        "in_rnp, rnp_entries, in_gisp, gisp_okpd2, in_software_registry, hist_lots, hist_wins, hist_okpd2_codes, "
        "hist_class_codes, needs_review, enrichment_status, …"))
    contacts: dict[str, Any] | None = Field(None, description=(
        "Контакты из карточки контракта ЕИС: phones, emails, postal_address, contract_url (источник), "
        "found_by (как найден контракт), found. null — ещё не искали"))
    links: list[dict[str, str]] = Field(default_factory=list, description=(
        "Ссылки на компанию: сайт и контракт с контактами (kind=contact), карточки на площадках (profile), "
        "проверка РНП (check)"))
    role: Role
    risk_flags: list[RiskFlag]
    fields: dict[str, FieldValue] = Field(description="Каждое поле с источником и датой получения (ТЗ, ФТ-07)")
    sources_status: list[SourceStatus]

    model_config = {"json_schema_extra": {"example": EXAMPLE}}


class BatchError(BaseModel):
    inn: str
    error: str


class BatchResponse(BaseModel):
    items: list[SupplierCard | BatchError]


class Health(BaseModel):
    status: str
    companies_in_db: int
    live_sources: list[str]
    source_timeout_s: float


ERRORS = {
    400: {"description": "Некорректный ИНН (длина или контрольная сумма)",
          "content": {"application/json": {"example": {"detail": "Некорректный ИНН 7707049389: …"}}}},
    404: {"description": "Ни один источник ничего не знает об этом ИНН"},
}


def _json(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _card(inn: str) -> dict | None:
    engine = state["engine"]
    facts, runs = storage.load_facts(engine, inn), storage.load_runs(engine, inn)
    if not facts:
        return None
    row, fields = build_company(inn, facts, runs)
    storage.save_company(engine, row)  # витрина companies всегда соответствует последним фактам
    values = {k: v["value"] for k, v in fields.items()}
    contacts = None
    if "contacts" in runs:
        contacts = {k: values.get(f"contact_{k}") for k in ("phones", "emails", "postal_address", "contract_url",
                                                             "found_by")} | {"found": bool(values.get("contacts_found"))}
    return {
        "inn": inn,
        "kind": inn_kind(inn),
        "contacts": contacts,
        "links": links(inn, row | values),
        "company": {k: _json(v) for k, v in row.items()},
        "role": classify_role(row | values),
        "risk_flags": row.get("risk_flags") or [],
        "fields": fields,
        "sources_status": [
            {"source": s, "name": SOURCE_LABELS.get(s, s), "status": st, "url": _url(s, inn, fields)}
            for s, st in sorted(runs.items())
        ],
    }


@lru_cache(maxsize=1)
def _reqnums() -> dict:
    """Номера выигранных закупок ЕИС по ИНН — ключ к контракту с контактами. Нет CSV — пусто."""
    try:
        return history.won_reqnums()
    except Exception as e:  # noqa: BLE001
        log.warning("reqnums: %s — контакты будут искаться только по названию", e)
        return {}


def _hints(inn: str) -> dict:
    facts = {f.field: f.value for f in storage.load_facts(state["engine"], inn)}
    return {"reqnums": _reqnums().get(inn), "name": facts.get("name_short") or facts.get("name_full")}


async def _enrich(inn: str, refresh: bool) -> dict:
    if not is_valid_inn(inn):
        raise HTTPException(400, f"Некорректный ИНН {inn}: 10 цифр для юрлица, 12 для ИП, проверка контрольной суммы")
    runs = await asyncio.to_thread(storage.load_runs, state["engine"], inn)
    todo = LIVE_SOURCES if refresh else [s for s in LIVE_SOURCES if s not in runs]
    fetched = []
    if todo:
        hints = await asyncio.to_thread(_hints, inn) if "contacts" in todo else None
        results = await fetch_all(state["http"], inn, todo, captcha_retries=0, timeout=SOURCE_TIMEOUT, hints=hints)
        await asyncio.to_thread(storage.save_results, state["engine"], results)
        fetched = [r.source for r in results]
    card = await asyncio.to_thread(_card, inn)
    if card is None:
        raise HTTPException(404, f"По ИНН {inn} ничего не найдено ни в одном источнике")
    card["cached"] = not fetched
    card["fetched_now"] = fetched
    return card


@app.get("/api/suppliers/{inn}", response_model=SupplierCard, tags=["Контрагенты"], responses=ERRORS,
         summary="Карточка компании по ИНН")
async def get_supplier(
    inn: str = PathParam(..., description="ИНН: 10 цифр — юрлицо, 12 — ИП", examples=["7804428656"]),
    refresh: bool = Query(False, description="перезапросить все онлайн-источники, даже если данные уже есть"),
):
    """Из базы, если ИНН уже обогащён; иначе — опрос источников (до ~15 с на источник), запись в базу и ответ."""
    return await _enrich(inn.strip(), refresh)


class BatchRequest(BaseModel):
    inns: list[str] = Field(..., max_length=BATCH_MAX, description=f"До {BATCH_MAX} ИНН",
                            examples=[["7804428656", "7707083893"]])
    refresh: bool = False


@app.post("/api/suppliers/batch", response_model=BatchResponse, tags=["Контрагенты"],
          summary="Карточки по списку ИНН")
async def batch(req: BatchRequest):
    """Несколько ИНН за запрос, по 4 параллельно. Ошибка по одному ИНН не ломает остальные."""
    sem = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def one(inn: str):
        async with sem:
            try:
                return await _enrich(inn.strip(), req.refresh)
            except HTTPException as e:
                return {"inn": inn, "error": e.detail}

    return {"items": await asyncio.gather(*(one(i) for i in dict.fromkeys(req.inns)))}


@app.get("/api/health", response_model=Health, tags=["Служебное"], summary="Состояние сервиса")
async def health():
    def count():
        with state["engine"].connect() as conn:
            return conn.execute(select(func.count()).select_from(storage.companies)).scalar()
    return {"status": "ok", "companies_in_db": await asyncio.to_thread(count), "live_sources": LIVE_SOURCES,
            "source_timeout_s": SOURCE_TIMEOUT}
