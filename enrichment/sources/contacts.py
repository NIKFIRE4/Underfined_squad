"""Контакты поставщика (телефон, почта, почтовый адрес) из карточек контрактов ЕИС (zakupki.gov.ru).

ФНС контакты не публикует, а в контракте 44-ФЗ блок «Информация о поставщиках» содержит
телефон и почту организации. Как найти контракт этого поставщика:
  1. по номеру закупки ЕИС (reqnum) из выигранных им лотов — точно (есть у ~43% поставщиков);
  2. иначе по названию (supplierTitle) — неточно, поэтому контакты берём только из строки,
     где ИНН совпал с искомым.
Таблица участников — лёгкий эндпоинт participants.html (~3 КБ вместо 160 КБ карточки).
"""

import re

from selectolax.parser import HTMLParser

from ..http import Http
from ..models import RawResponse, SourceResult, now_utc

SOURCE = "contacts"
BASE = "https://zakupki.gov.ru/epz/contract"
MAX_REQNUMS = 2      # номеров закупок на ИНН
MAX_CARDS = 3        # карточек контрактов на ИНН
EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
OPF = re.compile(r'^(ООО|АО|ПАО|ЗАО|ОАО|НАО|ИП|ГУП|МУП|ФГУП|АНО|НКО)\s+', re.I)


def contract_numbers(html: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"contractCard/common-info\.html\?reestrNumber=(\d+)", html)))


def _phones(text: str) -> list[str]:
    out = []
    for chunk in re.split(r"[;,]|\s{2,}", text):
        digits = re.sub(r"\D", "", chunk)
        if 10 <= len(digits) <= 12:
            if len(digits) == 10:
                digits = "7" + digits
            if digits.startswith("8") and len(digits) == 11:
                digits = "7" + digits[1:]
            out.append("+" + digits)
    return list(dict.fromkeys(out))


def parse_participants(html: str) -> list[dict]:
    """Строки таблицы «Информация о поставщиках»: inn, kpp, name, address, postal, phones, emails."""
    rows = []
    for tr in HTMLParser(html).css("tbody tr"):
        tds = tr.css("td")
        if len(tds) < 5:
            continue
        org = tds[0]
        spans = [s.text(strip=True) for s in org.css("span")]
        ids = {spans[i].rstrip(":"): spans[i + 1] for i in range(len(spans) - 1) if spans[i].endswith(":")}
        name = (org.text(deep=False, strip=True) or "").strip()
        contact_text = tds[4].text(separator="\n", strip=True)
        emails = list(dict.fromkeys(m.lower() for m in EMAIL.findall(contact_text)))
        phones = _phones(EMAIL.sub(" ", contact_text).replace("\n", ";"))
        rows.append({
            "inn": ids.get("ИНН"), "kpp": ids.get("КПП"), "name": name,
            "address": tds[2].text(strip=True) or None, "postal": tds[3].text(strip=True) or None,
            "phones": phones, "emails": emails,
        })
    return rows


def search_name(name: str | None) -> str | None:
    """'ООО "БРАСС"' → 'БРАСС'; 'ИП ИВАНОВ ИВАН' → 'ИВАНОВ ИВАН'."""
    if not name:
        return None
    s = OPF.sub("", name.strip()).replace('"', " ").replace("«", " ").replace("»", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) >= 3 else None


async def fetch(http: Http, inn: str, reqnums: list[str] | None = None, name: str | None = None) -> SourceResult:
    res = SourceResult(SOURCE, inn)
    candidates: list[tuple[str, str]] = []  # (номер контракта, как найден)
    for reqnum in (reqnums or [])[:MAX_REQNUMS]:
        r = await http.request(SOURCE, "GET", f"{BASE}/search/results.html", insecure=True,
                               params={"searchString": reqnum, "fz44": "on"})
        candidates += [(n, f"закупка {reqnum}") for n in contract_numbers(r.text)[:1]]
    if not candidates and (q := search_name(name)):
        r = await http.request(SOURCE, "GET", f"{BASE}/search/results.html", insecure=True,
                               params={"supplierTitle": q, "fz44": "on", "recordsPerPage": "_10",
                                       "sortBy": "UPDATE_DATE"})
        candidates += [(n, f"поиск по названию «{q}»") for n in contract_numbers(r.text)]

    seen = set()
    for number, how in candidates[:MAX_CARDS]:
        if number in seen:
            continue
        seen.add(number)
        r = await http.request(SOURCE, "GET", f"{BASE}/contractCard/participants.html", insecure=True,
                               limiter_key="contacts_card", params={"reestrNumber": number})
        mine = [p for p in parse_participants(r.text) if p["inn"] == inn]
        if not mine:
            continue
        p, ts = mine[0], now_utc()
        res.raws.append(RawResponse(inn, SOURCE, f"participants/{number}", r.status_code, p, ts))
        url = f"{BASE}/contractCard/common-info.html?reestrNumber={number}"
        res.add("contacts_found", bool(p["phones"] or p["emails"]), ts)
        res.add("contact_phones", p["phones"], ts)
        res.add("contact_emails", p["emails"], ts)
        res.add("contact_postal_address", p["postal"], ts)
        res.add("contact_contract_url", url, ts)
        res.add("contact_found_by", how, ts)
        return res

    res.add("contacts_found", False, now_utc())
    return res
