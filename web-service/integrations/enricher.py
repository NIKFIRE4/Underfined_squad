"""Обогащение кандидатов модели через HTTP API обогащения (enrichment/api.py, в docker compose — enrichment-api).

  POST {ENRICH_API_URL}/api/suppliers/batch   ИНН кандидатов лота → карточки компаний, только из базы (offline)
  GET  {ENRICH_API_URL}/api/suppliers/{inn}   одна карточка для интерфейса (прокси /api/suppliers/{inn}/card);
                                              здесь недостающие источники опрашиваются вживую, кроме ПБ и ЕГРЮЛ
  POST {ENRICH_API_URL}/api/suppliers/prefetch кандидаты лота с оценкой → ПБ и ЕГРЮЛ догружаются в фоне,
                                              от высшей оценки к низшей (у ФНС капча); карточка отдаёт pending

ENRICH_API_URL — адрес сервиса (по умолчанию http://127.0.0.1:8010); пустая строка отключает обогащение.
ENRICH_API_TIMEOUT — таймаут одного запроса, с (по умолчанию 60: карточка ИНН, которого нет в базе, опрашивает источники).
ENRICH_PIPELINE_LIVE=1 — в пайплайне тоже опрашивать источники вживую (медленно: до ~30 с на ИНН при капче ФНС).
Только стандартная библиотека, как и весь web-service.
"""
import json
import logging
import os
import urllib.error
import urllib.request

from models import Candidate, Lot

from . import new_pool

API_URL = os.environ.get("ENRICH_API_URL", "http://127.0.0.1:8010").rstrip("/")
TIMEOUT = float(os.environ.get("ENRICH_API_TIMEOUT", "60"))
PIPELINE_LIVE = os.environ.get("ENRICH_PIPELINE_LIVE") == "1"
BATCH_MAX = 50  # лимит API
READY = bool(API_URL)

# Поля карточки, которые подтверждаем источником в выгрузке (ТЗ, ФТ-07)
SOURCE_FIELDS = {"name_short": "Название", "status": "Статус", "is_smp": "МСП", "region_code": "Регион",
                 "okved_main": "ОКВЭД", "revenue": "Выручка", "employees": "Численность", "in_rnp": "РНП"}
# Код региона → название: у ИП и крупных компаний без ответа ЕГРЮЛ region_name пуст, а код есть всегда
REGIONS = {"78": "Санкт-Петербург", "47": "Ленинградская область", "77": "Москва", "50": "Московская область",
           "10": "Республика Карелия", "29": "Архангельская область", "35": "Вологодская область",
           "39": "Калининградская область", "51": "Мурманская область", "53": "Новгородская область",
           "60": "Псковская область", "69": "Тверская область", "76": "Ярославская область",
           "16": "Республика Татарстан", "02": "Республика Башкортостан", "23": "Краснодарский край",
           "24": "Красноярский край", "52": "Нижегородская область", "54": "Новосибирская область",
           "55": "Омская область", "59": "Пермский край", "61": "Ростовская область", "63": "Самарская область",
           "66": "Свердловская область", "74": "Челябинская область"}
ENRICHMENT_STATUS = {"full": "Обогащено", "partial": "Обогащено частично", "failed": "Источники недоступны"}


class EnrichmentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, body: dict | None = None, timeout: float = TIMEOUT):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise EnrichmentError(e.code, detail if isinstance(detail, str) else f"Сервис обогащения ответил {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EnrichmentError(502, f"Сервис обогащения недоступен ({API_URL}): {getattr(e, 'reason', e)}") from None


def fetch_card(inn: str, refresh: bool = False) -> dict:
    """Полная карточка компании по ИНН. Ошибки — EnrichmentError со статусом API (400, 404, 502)."""
    return _request("GET", f"/api/suppliers/{inn}" + ("?refresh=true" if refresh else ""))


def fetch_cards(inns: list[str]) -> dict[str, dict]:
    """Карточки пачками по BATCH_MAX. ИНН, по которым API вернул ошибку, в ответ не попадают."""
    cards = {}
    for i in range(0, len(inns), BATCH_MAX):
        body = {"inns": inns[i:i + BATCH_MAX], "offline": not PIPELINE_LIVE}
        for item in _request("POST", "/api/suppliers/batch", body)["items"]:
            if "company" in item:
                cards[item["inn"]] = item
    return cards


def prefetch(candidates: list[Candidate]) -> None:
    """ПБ и ЕГРЮЛ (капча ФНС) — в фоновую очередь API, от высшей оценки к низшей. Ошибки не мешают выдаче."""
    items = [{"inn": c.supplier_inn, "score": float(c.score or 0)} for c in candidates if c.supplier_inn]
    if not items:
        return
    try:
        _request("POST", "/api/suppliers/prefetch", {"items": items}, timeout=5)
    except EnrichmentError as e:
        logging.info("Фоновая догрузка не поставлена: %s", e)


def _sources(card: dict) -> list[dict]:
    urls = {s["source"]: (s["name"], s["url"]) for s in card.get("sources_status", [])}
    out = []
    for field, label in SOURCE_FIELDS.items():
        f = card.get("fields", {}).get(field)
        if not f or f.get("value") is None or not f.get("fetched_at"):
            continue
        name, url = urls.get(f["source"], (f["source"], ""))
        if url.startswith(("http://", "https://")):
            out.append({"field": label, "source": name, "url": url, "checked_at": f["fetched_at"]})
    return out


def _excluded(company: dict, smp_only: bool) -> bool:
    """Бизнес-фильтры закупки: ликвидированные, РНП и не-МСП в закупке только для МСП."""
    return company.get("is_active") is False or company.get("is_liquidated") is True \
        or company.get("in_rnp") is True or (smp_only and company.get("is_smp") is False)


def _apply(c: Candidate, card: dict) -> Candidate:
    co, role = card["company"], card.get("role") or {}
    c.supplier_name = co.get("name_short") or co.get("name_full") or c.supplier_name
    c.supplier_kpp = co.get("kpp") or c.supplier_kpp
    code = co.get("region_code") or c.supplier_inn[:2]
    c.region = REGIONS.get(code) or co.get("region_name") or c.region or f"Регион {code}"
    if co.get("is_smp") is not None:
        c.is_smp = bool(co["is_smp"])
    if role.get("value") and role["value"] != "unknown":
        c.role = role["label"]
    flags = card.get("risk_flags") or []
    if flags:
        c.status = "Требует проверки"
        c.reasons = c.reasons + ["Риск: " + "; ".join(f["text"] for f in flags[:2])]
    elif co.get("is_active") is None:
        c.status = "Требует проверки"  # статус ЕГРЮЛ не подтверждён — не выдаём за действующую
    c.sources = c.sources + _sources(card)
    c.enrichment_status = ENRICHMENT_STATUS.get(co.get("enrichment_status"), "Обогащено частично")
    return c


def enrich(lot: Lot, candidates: list[Candidate]) -> list[Candidate]:
    inns = [c.supplier_inn for c in candidates if c.supplier_inn]
    smp_only = str(lot.notice.get("is_smp", "")).strip().lower() in ("true", "1", "да")
    result = []
    try:
        cards = fetch_cards(list(dict.fromkeys(inns))) if inns else {}
    except EnrichmentError as e:
        # Сервис обогащения недоступен: кандидаты модели без подтверждения, но новые компании из пула всё равно ищем
        logging.warning("Обогащение лота %s: %s", lot.lot_id, e)
        for c in candidates:
            c.enrichment_status = "Источник недоступен — требуется проверка"
            c.status = "Требует проверки"
        result = list(candidates)
    else:
        for c in candidates:
            card = cards.get(c.supplier_inn)
            if card is None:
                c.enrichment_status = "Нет данных в источниках"
                result.append(c)
            elif not _excluded(card["company"], smp_only):
                result.append(_apply(c, card))
    # «Непроверенные»: компании из открытых реестров, которых нет в истории закупок (integrations/new_pool.py)
    result += new_pool.find(lot, REGIONS, exclude=set(inns))
    prefetch(result)
    return result
