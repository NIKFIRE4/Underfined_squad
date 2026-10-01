"""HTTP-слой: общий клиент, лимитер частоты на источник, повторы."""

import asyncio
import time
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class SourceError(Exception):
    pass


class CaptchaRequired(SourceError):
    pass


class RetryableStatus(SourceError):
    pass


class RateLimited(CaptchaRequired):
    """HTTP 429: эндпоинт просит снизить частоту. Его лимитер уже поставлен на паузу и замедлен
    в Http.request — вызывающему остаётся только повторить."""

    def __init__(self, limiter_key: str):
        super().__init__(f"{limiter_key}: HTTP 429")
        self.limiter_key = limiter_key


RATE_LIMIT_COOLDOWN = 150  # секунд паузы эндпоинта после 429


class RateLimiter:
    """Не чаще одного запроса в `interval` секунд на источник, общий для всех корутин."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval

    def pause(self, seconds: float) -> None:
        """Остановить источник на `seconds` (после капчи или бана)."""
        self._next = max(self._next, time.monotonic() + seconds)

    def slow_down(self, factor: float = 1.5, cap: float = 30.0) -> None:
        """Адаптация к лимитам источника: после капчи запросы идут реже до конца прогона."""
        self.interval = min(self.interval * factor, cap)


# Стартовые интервалы (секунды между запросами). Уточняем на пакетном прогоне.
DEFAULT_INTERVALS = {
    "egrul": 0.5,
    "pb": 0.35,
    "rmsp": 0.3,
    "bo": 0.5,
    "rnp": 1.0,
    "contacts": 1.5,       # поиск контрактов ЕИС: 429 уже при ~1,4 запроса/с суммарно с РНП
    "contacts_card": 0.5,  # таблица участников контракта: лимит мягче, отдельный лимитер
}


class Http:
    def __init__(self, intervals: dict[str, float] | None = None, timeout: float = 30):
        self.limiters = {
            k: RateLimiter(v) for k, v in {**DEFAULT_INTERVALS, **(intervals or {})}.items()
        }
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        # zakupki.gov.ru подписан корневым сертификатом Минцифры, которого нет в certifi.
        # Отдаём оттуда только публичные данные, поэтому проверку TLS отключаем точечно.
        self.client_noverify = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
            verify=False,
        )

    async def request(
        self, source: str, method: str, url: str, *, insecure: bool = False,
        limiter_key: str | None = None, **kw: Any
    ) -> httpx.Response:
        client = self.client_noverify if insecure else self.client
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential_jitter(initial=1, max=20),
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
            reraise=True,
        ):
            with attempt:
                limiter = self.limiters[limiter_key or source]
                await limiter.wait()
                r = await client.request(method, url, **kw)
                if r.status_code == 429:
                    limiter.pause(RATE_LIMIT_COOLDOWN)
                    limiter.slow_down()
                    raise RateLimited(limiter_key or source)
                if r.status_code >= 500:
                    raise RetryableStatus(f"{source}: HTTP {r.status_code}")
                return r
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.client_noverify.aclose()
