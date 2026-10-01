"""РНП целиком: обход реестра недобросовестных поставщиков ЕИС по месяцам включения.

Вместо запроса на каждый из 44 тыс. ИНН (~11 ч) — ~2,4 тыс. страниц по 50 записей (~40 мин),
сопоставление с ИНН поставщиков — локально. ЕИС отдаёт не больше 100 страниц на один поиск,
поэтому окно, упёршееся в лимит, делится пополам.

Записи сохраняются в rnp_registry; для ИНН поставщиков пишутся те же факты, что у поштучного
источника rnp (in_rnp, rnp_ever, rnp_entries) — с источником rnp и датой обхода.
"""

import logging
from datetime import date, timedelta

from sqlalchemy.engine import Engine

from . import storage
from .http import Http
from .models import SourceResult, now_utc
from .sources.rnp import URL, parse_entries

log = logging.getLogger("enrichment")

PAGE_SIZE = 50
MAX_PAGES = 100
START = date(2006, 1, 1)  # РНП ведётся с 94-ФЗ


def _months(start: date, end: date):
    d = start.replace(day=1)
    while d <= end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield d, min(nxt - timedelta(days=1), end)
        d = nxt


async def _window(http: Http, start: date, end: date, on_page) -> int:
    """Все записи с датой включения в [start, end]; при упоре в 100 страниц — делим окно."""
    got = 0
    for page in range(1, MAX_PAGES + 1):
        r = await http.request("rnp", "GET", URL, insecure=True, params={
            "fz44": "on", "fz223": "on", "fz94": "on", "recordsPerPage": f"_{PAGE_SIZE}", "pageNumber": page,
            "inclusionDateFrom": start.strftime("%d.%m.%Y"), "inclusionDateTo": end.strftime("%d.%m.%Y"),
            "sortBy": "UPDATE_DATE",
        })
        entries = parse_entries(r.text)
        on_page(entries)
        got += len(entries)
        if len(entries) < PAGE_SIZE:
            return got
    if start == end:
        log.warning("РНП: за %s больше %d записей — часть может не попасть", start, MAX_PAGES * PAGE_SIZE)
        return got
    mid = start + (end - start) / 2
    log.info("РНП: окно %s–%s больше лимита, делю пополам", start, end)
    return await _window(http, start, mid, on_page) + await _window(http, mid + timedelta(days=1), end, on_page)


async def crawl(http: Http, engine: Engine, start: date = START, end: date | None = None) -> int:
    end = end or date.today()
    total = 0
    fetched_at = now_utc()

    def save(entries: list[dict]) -> None:
        rows = [e | {"fetched_at": fetched_at} for e in entries if e.get("number") and e.get("inn")]
        storage.upsert_rnp(engine, rows)

    for m_start, m_end in _months(start, end):
        n = await _window(http, m_start, m_end, save)
        total += n
        if n:
            log.info("РНП: %s — %d записей, всего %d", m_start.strftime("%Y-%m"), n, total)
    return total


def apply_to_suppliers(engine: Engine, targets: set[str]) -> list[SourceResult]:
    """Факты rnp для ИНН поставщиков из скачанного реестра (нет записей — значит, не в РНП)."""
    by_inn = storage.rnp_by_inn(engine, targets)
    ts = storage.rnp_fetched_at(engine) or now_utc()  # дата проверки = дата обхода реестра
    out = []
    for inn in targets:
        entries = by_inn.get(inn, [])
        res = SourceResult("rnp", inn)
        active = [e for e in entries if "исключ" not in (e["state"] or "").lower()]
        res.add("in_rnp", bool(active), ts)
        res.add("rnp_ever", bool(entries), ts)
        res.add("rnp_entries", entries, ts)
        out.append(res)
    return out
