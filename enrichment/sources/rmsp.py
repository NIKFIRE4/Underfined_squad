"""Единый реестр субъектов МСП: rmsp.nalog.ru."""

from ..http import Http
from ..models import RawResponse, SourceResult, now_utc
from ._util import flag, ru_date

SOURCE = "rmsp"
URL = "https://rmsp.nalog.ru/search-proc.json"

# 1 — микро, 2 — малое, 3 — среднее
CATEGORY_NAMES = {1: "micro", 2: "small", 3: "medium"}


async def fetch(http: Http, inn: str) -> SourceResult:
    res = SourceResult(SOURCE, inn)
    r = await http.request(SOURCE, "POST", URL, data={"query": inn, "page": 1})
    body = r.json()
    ts = now_utc()
    res.raws.append(RawResponse(inn, SOURCE, "search", r.status_code, body, ts))

    rows = [d for d in body.get("data", []) if d.get("inn") == inn]
    if not rows:
        res.add("is_smp", False, ts)
        return res
    d = rows[0]
    res.add("is_smp", True, ts)
    res.add("smp_category", d.get("category"), ts)
    res.add("smp_since", ru_date(d.get("dtregistry")), ts)
    res.add("employees_rmsp", d.get("od2_sschr"), ts)
    res.add("smp_has_licenses", flag(d.get("has_licenses")), ts)
    res.add("smp_has_contracts", flag(d.get("has_contracts")), ts)  # контракты по 44/223-ФЗ
    res.add("smp_is_hitech", flag(d.get("is_hitech")), ts)
    res.add("smp_is_partnership", flag(d.get("is_partnership")), ts)
    res.add("smp_is_social", flag(d.get("pr_soc")), ts)
    res.add("region_code", d.get("regioncode"), ts)
    return res
