"""ЕГРЮЛ/ЕГРИП: egrul.nalog.ru. Руководитель, ОГРН, КПП, даты регистрации и прекращения."""

import asyncio

from ..http import CaptchaRequired, Http, SourceError
from ..models import RawResponse, SourceResult, now_utc
from ._util import ru_date

SOURCE = "egrul"
BASE = "https://egrul.nalog.ru"


def _pick_row(rows: list[dict], inn: str) -> dict | None:
    """У ИП бывает несколько записей (закрытые и действующая): берём без даты прекращения `e`,
    иначе самую свежую по дате регистрации."""
    rows = [r for r in rows if r.get("i") == inn and r.get("k") in ("ul", "fl")]
    if not rows:
        return None
    active = [r for r in rows if not r.get("e")]
    pool = active or rows
    return max(pool, key=lambda r: ru_date(r.get("r")) or "")


async def fetch(http: Http, inn: str) -> SourceResult:
    res = SourceResult(SOURCE, inn)
    r = await http.request(SOURCE, "POST", f"{BASE}/", data={"query": inn})
    body = r.json()
    if any("captcha" in k.lower() for k in body.get("ERRORS") or {}):
        raise CaptchaRequired("egrul: captcha")
    token = body.get("t")
    if not token:
        raise SourceError(f"egrul: no token, captcha? {r.text[:200]}")
    body = None
    for _ in range(5):  # результат готовится асинхронно
        await asyncio.sleep(0.6)
        r = await http.request(SOURCE, "GET", f"{BASE}/search-result/{token}")
        body = r.json()
        if body.get("status") != "wait":
            break
    ts = now_utc()
    res.raws.append(RawResponse(inn, SOURCE, "search-result", r.status_code, body, ts))

    row = _pick_row((body or {}).get("rows", []), inn)
    if row is None:
        res.add("found_egrul", False, ts)
        return res

    res.add("found_egrul", True, ts)
    res.add("name_full", row.get("n"), ts)
    res.add("name_short", row.get("c"), ts)
    res.add("ogrn", row.get("o"), ts)
    res.add("kpp", row.get("p"), ts)
    res.add("reg_date", ru_date(row.get("r")), ts)
    res.add("liquidation_date", ru_date(row.get("e")), ts)
    res.add("region_name", row.get("rn"), ts)
    if g := row.get("g"):
        post, _, name = g.partition(": ")
        res.add("director", {"post": post, "name": name} if name else {"name": g}, ts)
    return res
