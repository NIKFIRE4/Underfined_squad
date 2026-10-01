"""Оркестрация: все источники по одному ИНН параллельно, ошибки источника не роняют остальных."""

import asyncio
import logging
from typing import Awaitable, Callable

from sqlalchemy.engine import Engine

from . import storage
from .card import build_company
from .http import CaptchaRequired, Http, RateLimited
from .inn import is_valid_inn
from .models import SourceResult
from .sources import bo, contacts, egrul, pb, rmsp, rnp

log = logging.getLogger("enrichment")

ALL_SOURCES = ["pb", "bo", "rmsp", "rnp", "egrul"]
CAPTCHA_COOLDOWN = 150  # секунд паузы после капчи; блок ФНС снимается за ~2–3 мин
CAPTCHA_RETRIES = 2


async def _guard(source: str, inn: str, http: Http, call: Callable[[], Awaitable[SourceResult]],
                 captcha_retries: int = CAPTCHA_RETRIES, timeout: float | None = None) -> SourceResult:
    for attempt in range(captcha_retries + 1):
        try:
            return await asyncio.wait_for(call(), timeout)
        except asyncio.TimeoutError:
            log.warning("%s: timeout %ss on %s", source, timeout, inn)
            return SourceResult(source, inn, ok=False, error="timeout")
        except CaptchaRequired as e:  # и RateLimited (HTTP 429)
            limiter = http.limiters[getattr(e, "limiter_key", source)]
            if not isinstance(e, RateLimited):  # для 429 пауза уже выставлена в Http.request
                limiter.pause(CAPTCHA_COOLDOWN)
                limiter.slow_down()
            log.warning("%s: %s on %s, cooldown %ss, interval now %.1fs",
                        source, e, inn, CAPTCHA_COOLDOWN, limiter.interval)
            if attempt == captcha_retries:
                return SourceResult(source, inn, ok=False, error="captcha")
        except Exception as e:  # noqa: BLE001 — источник не должен ронять карточку
            log.warning("%s: %s on %s: %s", source, type(e).__name__, inn, e)
            return SourceResult(source, inn, ok=False, error=f"{type(e).__name__}: {e}"[:500])
    raise AssertionError("unreachable")


async def fetch_all(http: Http, inn: str, sources: list[str], *, captcha_retries: int = CAPTCHA_RETRIES,
                    timeout: float | None = None, hints: dict | None = None) -> list[SourceResult]:
    """Все источники по ИНН. Для онлайн-запроса: captcha_retries=0 и timeout — ответ не ждёт капчу.
    hints — подсказки для контактов: {"reqnums": [...], "name": "..."}."""
    hints = hints or {}
    kw = {"captcha_retries": captcha_retries, "timeout": timeout}
    tasks: dict[str, asyncio.Task] = {}

    async def pb_then_bo() -> None:
        # ГИР БО ищем по id из ПБ, это экономит запрос поиска
        pb_res = await _guard("pb", inn, http, lambda: pb.fetch(http, inn), **kw)
        results.append(pb_res)
        if "bo" in sources:
            bo_id = pb_res.context.get("bo_id")
            results.append(await _guard("bo", inn, http, lambda: bo.fetch(http, inn, bo_id), **kw))

    results: list[SourceResult] = []
    plain = {"egrul": egrul.fetch, "rmsp": rmsp.fetch, "rnp": rnp.fetch}

    async def run(name: str, fn) -> None:
        results.append(await _guard(name, inn, http, lambda: fn(http, inn), **kw))

    coros = [run(n, fn) for n, fn in plain.items() if n in sources]
    if "contacts" in sources:
        coros.append(run("contacts", lambda h, i: contacts.fetch(h, i, hints.get("reqnums"), hints.get("name"))))
    if "pb" in sources:
        coros.append(pb_then_bo())
    elif "bo" in sources:
        coros.append(run("bo", lambda h, i: bo.fetch(h, i)))
    await asyncio.gather(*coros)
    return results


def rebuild_company(engine: Engine, inn: str) -> tuple[dict, dict]:
    row, card = build_company(inn, storage.load_facts(engine, inn), storage.load_runs(engine, inn))
    storage.save_company(engine, row)
    return row, card


async def enrich(http: Http, engine: Engine, inn: str, sources: list[str] = ALL_SOURCES,
                 hints: dict | None = None) -> dict:
    if not is_valid_inn(inn):
        raise ValueError(f"invalid INN: {inn}")
    results = await fetch_all(http, inn, sources, hints=hints)
    storage.save_results(engine, results)
    row, _ = rebuild_company(engine, inn)
    return row
