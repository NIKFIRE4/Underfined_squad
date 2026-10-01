"""ГИР БО (бухгалтерская отчётность): bo.nalog.gov.ru.

Суммы в отчётности — в тысячах рублей, переводим в рубли.
Отчётность части крупных компаний скрыта (ПП РФ № 1173), тогда `bfo_found = False`.
"""

import re

from ..http import Http
from ..models import RawResponse, SourceResult, now_utc

SOURCE = "bo"
BASE = "https://bo.nalog.gov.ru"
HEADERS = {"Accept": "application/json"}

LINES = {
    "revenue": ("financialResult", "2110"),
    "gross_profit": ("financialResult", "2100"),
    "net_profit": ("financialResult", "2400"),
    "equity": ("balance", "1300"),
    "assets": ("balance", "1600"),
    "long_liabilities": ("balance", "1400"),
    "short_liabilities": ("balance", "1500"),
}


async def find_org(http: Http, inn: str) -> dict | None:
    """Строка поиска ГИР БО: id карточки, ОКВЭД, краткое имя (ОКВЭД — бесплатно, без доп. запроса)."""
    r = await http.request(
        SOURCE, "GET", f"{BASE}/advanced-search/organizations/search",
        params={"query": inn, "page": 0}, headers=HEADERS,
    )
    for row in r.json().get("content", []):
        if re.sub(r"<[^>]+>", "", row.get("inn") or "") == inn:
            return row
    return None


def _year_values(correction: dict) -> dict:
    out = {}
    for name, (section, line) in LINES.items():
        v = (correction.get(section) or {}).get(f"current{line}")
        if v is not None:
            out[name] = v * 1000
    return out


async def fetch(http: Http, inn: str, bo_id: str | None = None) -> SourceResult:
    res = SourceResult(SOURCE, inn)
    if not bo_id:
        org = await find_org(http, inn)
        ts = now_utc()
        if org:
            bo_id = str(org["id"])
            okved = org.get("okved2")
            res.add("okved_main", okved.get("id") if isinstance(okved, dict) else okved, ts)
            res.add("name_short", org.get("shortName"), ts)
    ts = now_utc()
    res.add("bo_id", bo_id, ts)
    if not bo_id:
        res.add("bfo_found", False, ts)
        return res

    r = await http.request(SOURCE, "GET", f"{BASE}/nbo/organizations/{bo_id}/bfo/", headers=HEADERS)
    body = r.json()
    ts = now_utc()
    res.raws.append(RawResponse(inn, SOURCE, f"bfo/{bo_id}", r.status_code, body, ts))

    by_year: dict[str, dict] = {}
    for rep in body if isinstance(body, list) else []:
        corrections = rep.get("typeCorrections") or []
        if not corrections:
            continue
        values = _year_values(corrections[-1].get("correction") or {})
        if values:
            by_year[str(rep["period"])] = values

    res.add("bfo_found", bool(by_year), ts)
    if not by_year:
        return res
    years = sorted(by_year, reverse=True)
    last = by_year[years[0]]
    prev = by_year[years[1]] if len(years) > 1 else {}
    res.add("finance_by_year", by_year, ts)
    res.add("finance_year", int(years[0]), ts)
    res.add("revenue", last.get("revenue"), ts)
    res.add("revenue_prev", prev.get("revenue"), ts)
    res.add("net_profit", last.get("net_profit"), ts)
    res.add("equity", last.get("equity"), ts)
    res.add("assets", last.get("assets"), ts)
    return res
