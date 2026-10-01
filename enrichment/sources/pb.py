"""«Прозрачный бизнес»: pb.nalog.ru.

Поиск по ИНН даёт статус, ОКВЭД и токен карточки; карточка — финансы по налоговой отчётности,
численность, налоги, спецрежимы, признаки недостоверности и массовости, сообщения Вестника.
Карточка защищена капчей при частых запросах: ловим её и отдаём CaptchaRequired наверх.
"""

import asyncio

from ..http import CaptchaRequired, Http, SourceError
from ..inn import inn_kind
from ..models import RawResponse, SourceResult, now_utc
from ._util import flag, num, ru_date

SOURCE = "pb"
SEARCH = "https://pb.nalog.ru/search-proc.json"
CARD = "https://pb.nalog.ru/company-proc.json"

TAX_REGIMES = ("usn", "ausn", "eshn", "envd", "psn", "npd", "spr")


def _check_captcha(body: dict) -> None:
    # pbCaptcha — на карточке, pbSearchCaptcha — на поиске
    if body.get("captchaRequired") or any("captcha" in k.lower() for k in body.get("ERRORS") or {}):
        raise CaptchaRequired("pb: captcha")


async def _async_call(http: Http, url: str, data: dict) -> dict:
    """ПБ отвечает в два шага: запрос → id, затем get-response, пока не готово."""
    r = await http.request(SOURCE, "POST", url, data=data)
    body = r.json()
    _check_captcha(body)
    if "id" not in body:
        raise SourceError(f"pb: unexpected response {str(body)[:200]}")
    for _ in range(6):
        await asyncio.sleep(0.8)
        r = await http.request(
            SOURCE, "POST", url, data={**data, "id": body["id"], "method": "get-response"}
        )
        resp = r.json()
        _check_captcha(resp)
        if resp.get("status") != "wait" and resp != {}:
            return resp
    raise SourceError("pb: response not ready")


def _parse_search(res: SourceResult, row: dict, ts) -> None:
    res.add("status", row.get("sulst_name_ex"), ts)
    res.add("is_liquidated", flag(row.get("pr_liq")), ts)
    res.add("is_invalid_info", flag(row.get("invalid")), ts)
    res.add("okved_main", row.get("okved2main") or row.get("okved2"), ts)
    res.add("okved_main_name", row.get("okved2mainname") or row.get("okved2name"), ts)
    if bourl := (row.get("bourl") or "").strip():
        res.context["bo_id"] = bourl.rsplit("/", 1)[-1]


def _codes(items: list[str] | None) -> list[str]:
    return [s.split(" - ", 1)[0].strip() for s in items or []]


def _latest(items: list[dict] | None, key: str) -> tuple[float | None, int | None]:
    rows = [i for i in items or [] if num(i.get(key)) is not None]
    if not rows:
        return None, None
    top = max(rows, key=lambda i: (i.get("yearcode") or 0, i.get("periodcode") or 0))
    return num(top[key]), int(top["yearcode"])


def _parse_card(res: SourceResult, card: dict, ts) -> None:
    v = card.get("vyp") or {}

    res.add("status", v.get("sulst_name_ex"), ts)
    res.add("is_liquidated", bool(card.get("liquidated") or flag(v.get("pr_liq"))), ts)
    res.add("ogrn", v.get("ОГРН") or v.get("ОГРНИП"), ts)
    res.add("kpp", v.get("КПП"), ts)
    res.add("reg_date", v.get("ДатаОГРН") or v.get("ДатаОГРНИП"), ts)
    res.add("name_full", v.get("НаимЮЛПолн"), ts)
    res.add("name_short", v.get("НаимЮЛСокр"), ts)
    if v.get("Фамилия"):
        fio = " ".join(filter(None, (v.get("Фамилия"), v.get("Имя"), v.get("Отчество"))))
        res.add("name_full", f"ИП {fio}", ts)
    res.add("address", v.get("АдресРФ") or v.get("Адрес"), ts)
    res.add("region_code", (v.get("КодРО") or "")[:2] or None, ts)
    res.add("charter_capital", num(v.get("СумКап")), ts)
    res.add("okved_main", v.get("КодОКВЭД"), ts)
    res.add("okved_main_name", v.get("НаимОКВЭД"), ts)
    res.add("okved_extra", _codes(card.get("okved2exs")), ts)
    res.add("is_invalid_info", flag(v.get("invalid")), ts)

    # реестр МСП (дублирует rmsp, но приходит бесплатно)
    if (cat := num(v.get("rsmpcategory"))) is not None:
        res.add("smp_category", int(cat) or None, ts)

    regimes = [t for t in TAX_REGIMES if flag(v.get(t))]
    res.add("tax_regimes", regimes, ts)

    # финансы из налоговой отчётности (форма 1), рубли
    res.add("revenue_tax", num(v.get("revenuesum")), ts)
    res.add("expense_tax", num(v.get("expensesum")), ts)
    res.add("finance_tax_year", num(v.get("form1_yearcode")), ts)
    res.add(
        "finance_tax_by_year",
        {
            str(int(f["yearcode"])): {"revenue": f.get("revenue"), "expense": f.get("expense")}
            for f in card.get("form1") or []
            if not f.get("empty")
        },
        ts,
    )
    res.add("taxes_paid", num(v.get("taxpaysum")), ts)
    res.add("taxes_paid_year", num(v.get("taxpay_yearcode")), ts)
    employees, emp_year = _latest(card.get("sschr"), "sschr")
    res.add("employees", employees, ts)
    res.add("employees_year", emp_year, ts)

    # признаки риска
    res.add("has_tax_debt", flag(v.get("pr_zd")), ts)  # задолженность по налогам > 1000 ₽
    res.add("no_tax_reporting", flag(v.get("pr_otch")), ts)  # не сдаёт отчётность > года
    arrears = [a for a in card.get("arrear") or [] if not a.get("empty")]
    res.add("tax_arrears", arrears, ts)
    res.add("offense_years", sorted({int(o["yearcode"]) for o in card.get("offense") or []}), ts)

    directors = v.get("masruk") or []
    founders = v.get("masuchr") or []
    strip = lambda rows: [{k: r.get(k) for k in ("inn", "name", "position", "cnt")} for r in rows]
    res.add("directors", strip(directors), ts)
    res.add("founders", strip(founders), ts)
    res.add("director_companies_max", max((int(r.get("cnt") or 0) for r in directors), default=None), ts)
    res.add("is_director_invalid", flag(v.get("masrukinvalid")), ts)
    res.add("is_founder_invalid", flag(v.get("masuchrinvalid")), ts)
    if "masaddress" in card:
        res.add("is_mass_address", bool(card["masaddress"]), ts)
    res.add(
        "vestnik_messages",
        [{"code": int(m.get("code", 0)), "count": int(m.get("count", 0))} for m in card.get("vestnik") or []],
        ts,
    )

    if bourl := (v.get("bourl") or "").strip():
        res.context.setdefault("bo_id", bourl.rsplit("/", 1)[-1])


async def fetch(http: Http, inn: str, *, with_card: bool = True) -> SourceResult:
    res = SourceResult(SOURCE, inn)
    kind = inn_kind(inn)
    mode, key = ("search-ul", "queryUl") if kind == "ul" else ("search-ip", "queryIp")
    body = await _async_call(http, SEARCH, {"mode": mode, key: inn, "page": 1, "pageSize": 10})
    ts = now_utc()
    res.raws.append(RawResponse(inn, SOURCE, "search", 200, body, ts))

    rows = [r for r in (body.get(kind) or {}).get("data", []) if r.get("inn") == inn]
    if not rows:
        res.add("found_pb", False, ts)
        return res
    row = rows[0]
    res.add("found_pb", True, ts)
    _parse_search(res, row, ts)

    if with_card and row.get("token"):
        card = await _async_call(http, CARD, {"token": row["token"], "method": "get-request"})
        ts = now_utc()
        res.raws.append(RawResponse(inn, SOURCE, "card", 200, card, ts))
        _parse_card(res, card, ts)
    return res
