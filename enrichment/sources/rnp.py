"""Реестр недобросовестных поставщиков (РНП) ЕИС: zakupki.gov.ru, HTML."""

from selectolax.parser import HTMLParser

from ..http import Http
from ..models import RawResponse, SourceResult, now_utc
from ._util import ru_date

SOURCE = "rnp"
URL = "https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html"


def parse_entries(html: str) -> list[dict]:
    entries = []
    for node in HTMLParser(html).css("div.registry-entry__form"):
        def text(sel: str) -> str:
            n = node.css_first(sel)
            return n.text(strip=True) if n else ""

        body = {}
        for block in node.css("div.registry-entry__body-block"):
            t, v = block.css_first(".registry-entry__body-title"), block.css_first(".registry-entry__body-value")
            if t and v:
                body[t.text(strip=True)] = v.text(strip=True)
        dates = {}
        for blk in node.css("div.data-block__title"):
            val = blk.next
            while val is not None and val.tag != "div":
                val = val.next
            if val is not None:
                dates[blk.text(strip=True)] = val.text(strip=True)
        entries.append(
            {
                "number": text(".registry-entry__header-mid__number").lstrip("№ ").strip(),
                "law": text(".registry-entry__header-top__title"),
                "state": text(".registry-entry__header-mid__title"),
                "name": body.get("Наименование (ФИО) недобросовестного поставщика"),
                "inn": body.get("ИНН (аналог ИНН)"),
                "included": ru_date(dates.get("Включено")),
                "updated": ru_date(dates.get("Обновлено")),
            }
        )
    return entries


async def fetch(http: Http, inn: str) -> SourceResult:
    res = SourceResult(SOURCE, inn)
    r = await http.request(
        SOURCE, "GET", URL, insecure=True,
        params={"searchString": inn, "fz44": "on", "fz223": "on", "fz94": "on",
                "recordsPerPage": "_50", "pageNumber": 1},
    )
    ts = now_utc()
    entries = [e for e in parse_entries(r.text) if e["inn"] == inn]
    res.raws.append(RawResponse(inn, SOURCE, "search", r.status_code, {"entries": entries}, ts))
    active = [e for e in entries if "исключ" not in e["state"].lower()]
    res.add("in_rnp", bool(active), ts)
    res.add("rnp_entries", entries, ts)
    res.add("rnp_ever", bool(entries), ts)
    return res
