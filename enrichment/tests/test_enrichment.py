"""Офлайн-тесты: парсеры на сохранённых реальных ответах источников, карточка, хранилище.
Сеть не нужна. Запуск: python -m pytest enrichment/tests -q
"""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from lxml import etree

from enrichment import card, registries, storage
from enrichment.fns_dumps import _rsmp
from enrichment.inn import is_valid_inn
from enrichment.models import Fact, SourceResult, now_utc
from enrichment.sources import bo, egrul, pb, rmsp, rnp

FX = Path(__file__).parent / "fixtures"
INN = "7804428656"  # ООО «БРАСС»


def fx_json(name: str):
    return json.loads((FX / name).read_text(encoding="utf-8"))


class FakeHttp:
    """Подмена Http: отдаёт ответы по подстроке URL."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes

    async def request(self, source, method, url, **kw):
        for part, body in self.routes.items():
            if part in url:
                if isinstance(body, str):
                    return httpx.Response(200, text=body)
                return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected url {url}")


def facts(res: SourceResult) -> dict:
    return {f.field: f.value for f in res.facts}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_):
        return None
    monkeypatch.setattr(asyncio, "sleep", _sleep)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_inn_checksum():
    assert is_valid_inn("7707049388")
    assert is_valid_inn("636200108061")
    assert not is_valid_inn("7707049389")
    assert not is_valid_inn("123")


def test_egrul_ul():
    http = FakeHttp({"search-result": fx_json(f"egrul_{INN}.json"), "egrul.nalog.ru/": {"t": "x"}})
    f = facts(run(egrul.fetch(http, INN)))
    assert f["ogrn"] == "1099847036750"
    assert f["kpp"] == "780601001"
    assert f["reg_date"] == "2009-12-22"
    assert f["director"]["post"] == "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"


def test_egrul_ip_picks_active_record():
    http = FakeHttp({"search-result": fx_json("egrul_636200108061.json"), "egrul.nalog.ru/": {"t": "x"}})
    f = facts(run(egrul.fetch(http, "636200108061")))
    assert f["ogrn"] == "316784700083119"  # действующая запись, а не закрытая 2005 года
    assert "liquidation_date" not in f


def test_egrul_captcha():
    http = FakeHttp({"egrul.nalog.ru/": {"ERRORS": {"captchaSearch": ["..."]}}})
    with pytest.raises(egrul.CaptchaRequired):
        run(egrul.fetch(http, INN))


def test_pb_search_and_card():
    search, card_body = fx_json(f"pb_search_{INN}.json"), fx_json(f"pb_card_{INN}.json")

    class PbHttp:
        async def request(self, source, method, url, data=None, **kw):
            if data.get("method") == "get-response":
                return httpx.Response(200, json=search if "search" in url else card_body)
            return httpx.Response(200, json={"id": "1", "captchaRequired": False})

    res = run(pb.fetch(PbHttp(), INN))
    f = facts(res)
    assert f["status"] == "Действующая организация"
    assert f["okved_main"] == "33.12"
    assert f["employees"] == 8
    assert f["taxes_paid"] == 1618399
    assert f["smp_category"] == 1
    assert f["tax_regimes"] == ["usn"]
    assert f["director_companies_max"] == 2
    assert "36.00" in f["okved_extra"]
    assert res.context["bo_id"] == "4436757"


def test_pb_captcha_detected():
    with pytest.raises(pb.CaptchaRequired):
        pb._check_captcha({"ERRORS": {"pbSearchCaptcha": ["..."]}, "STATUS": 400})


def test_bo_finance_in_rubles():
    http = FakeHttp({"/bfo/": fx_json(f"bo_bfo_{INN}.json")})
    f = facts(run(bo.fetch(http, INN, bo_id="4436757")))
    assert f["finance_year"] == 2025
    assert f["revenue"] == 17_366_000
    assert f["revenue_prev"] == 24_525_000
    assert f["equity"] == 3_220_000
    assert f["net_profit"] == 3_907_000


def test_bo_hidden_reporting():
    http = FakeHttp({"advanced-search": {"content": []}})
    assert facts(run(bo.fetch(http, "7707049388"))) == {"bfo_found": False}


def test_rmsp():
    f = facts(run(rmsp.fetch(FakeHttp({"rmsp": fx_json(f"rmsp_{INN}.json")}), INN)))
    assert f["is_smp"] is True and f["smp_category"] == 1 and f["region_code"] == "78"


def test_rnp_parse_and_filter_by_inn():
    html = (FX / "rnp_search.html").read_text(encoding="utf-8")
    entries = rnp.parse_entries(html)
    assert entries and all(e["inn"] and e["number"] for e in entries)
    target = entries[0]["inn"]
    f = facts(run(rnp.fetch(FakeHttp({"dishonestsupplier": html}), target)))
    assert f["rnp_ever"] is True
    assert all(e["inn"] == target for e in f["rnp_entries"])


def test_rsmp_xml_document():
    doc = etree.fromstring(
        '<Документ ДатаСост="10.09.2026" ДатаВклМСП="01.08.2016" ВидСубМСП="1" КатСубМСП="2" '
        'ПризНовМСП="2" СведСоцПред="2" ССЧР="24"><ОргВклМСП НаимОрг="ООО &quot;А&quot;" '
        'НаимОргСокр="ООО А" ИННЮЛ="7719815020" ОГРН="1127746497647"/><СведМН КодРегион="78"/>'
        '<СвОКВЭД><СвОКВЭДОсн КодОКВЭД="80.10" НаимОКВЭД="Охрана"/><СвОКВЭДДоп КодОКВЭД="80.20"/></СвОКВЭД>'
        '<СвПрод КодПрод="32.50.13.190" НаимПрод="Инструменты" ПрОтнПрод="1"/></Документ>'
    )
    r = _rsmp(doc)
    assert r["inn"] == "7719815020" and r["kind"] == "ul" and r["smp_category"] == 2
    assert r["okved_main"] == "80.10" and r["okved_extra"] == ["80.20"]
    assert r["products"][0] == {"okpd2": "32.50.13.190", "name": "Инструменты", "innovative": True}
    assert r["employees_rmsp"] == 24 and r["region_code"] == "78"


def test_registry_helpers():
    assert registries._inn(7708806062.0) == "7708806062"
    assert registries._inn(274000001.0) == "0274000001"  # Excel съел ведущий ноль
    assert registries._okpd2_list("58.29.2 Обеспечение;\n62.01.2 Оригиналы ПО") == ["58.29.2", "62.01.2"]


def _fact(field, value, source="pb"):
    return Fact("7804428656", field, value, source, now_utc())


def test_card_risk_flags_and_priority():
    young = (date.today() - timedelta(days=100)).isoformat()
    fs = [
        _fact("status", "Действующая организация"),
        _fact("reg_date", young),
        _fact("revenue", 1_000_000, "bo"), _fact("revenue_prev", 5_000_000, "bo"),
        _fact("equity", -10, "bo"),
        _fact("smp_category", 2, "pb"), _fact("smp_category", 1, "rmsp"),
        _fact("tax_arrears_total", 50_000.0, "fns_debtam"),
    ]
    row, fields = card.build_company("7804428656", fs, {"pb": "ok", "bo": "ok", "rmsp": "error"})
    codes = {f["code"] for f in row["risk_flags"]}
    assert {"young", "revenue_drop", "negative_equity", "tax_debt"} <= codes
    assert row["needs_review"] and row["is_active"]
    assert row["smp_category"] == 1  # РМСП приоритетнее ПБ
    assert fields["smp_category"]["conflicts"] == [{"value": 2, "source": "pb"}]
    assert row["enrichment_status"] == "partial"


def test_card_region_from_kpp():
    row, _ = card.build_company("7707049388", [_fact("kpp", "784201001", "egrul")], {"egrul": "ok"})
    assert row["region_code"] == "78"


def test_storage_roundtrip(tmp_path):
    engine = storage.connect(f"sqlite:///{tmp_path}/t.db")
    res = SourceResult("pb", INN)
    res.add("okved_main", "33.12", now_utc())
    res.add("okved_extra", ["36.00"], now_utc())
    storage.save_results(engine, [res, SourceResult("bo", INN, ok=False, error="captcha")])
    assert storage.load_runs(engine, INN) == {"pb": "ok", "bo": "captcha"}
    row, _ = card.build_company(INN, storage.load_facts(engine, INN), storage.load_runs(engine, INN))
    storage.save_company(engine, row)
    got = storage.get_company(engine, INN)
    assert got["okved_main"] == "33.12" and got["okved_extra"] == ["36.00"]
    assert got["enrichment_status"] == "partial"
    # повторный connect к той же БД не падает (мини-миграция колонок)
    storage.connect(f"sqlite:///{tmp_path}/t.db")


# --- discovery: новые компании и роль ---

from enrichment import discovery  # noqa: E402


def test_okpd2_levels_and_depth():
    assert discovery.okpd2_levels("32.50.13.190") == ["32.50.13.190", "32.50.13", "32.50", "32"]
    assert discovery.okpd2_levels("33.12.1") == ["33.12.1", "33.12", "33"]
    assert discovery._match_depth("32.50.13.190", ["32.50.13.190"]) == 4
    assert discovery._match_depth("32.50.13.110", ["32.50.13.190"]) == 3
    assert discovery._match_depth("33.12.11", ["33.12.1"]) == 2
    assert discovery._match_depth("21.20", ["32.50.13.190"]) == 0


def test_find_new_companies(tmp_path):
    engine = storage.connect(f"sqlite:///{tmp_path}/d.db")
    ts = now_utc()
    storage.replace_registry(engine, "gisp", [
        {"inn": "7801000001", "registry": "gisp", "okpd2": "32.50.13.190", "items_count": 5,
         "sample": "Зажим", "org_name": "ООО ЗАВОД", "source": "reg_gisp", "fetched_at": ts},
        {"inn": "5001000001", "registry": "gisp", "okpd2": "32.50.21", "items_count": 1,
         "sample": None, "org_name": "ООО ДАЛЕКО", "source": "reg_gisp", "fetched_at": ts},
    ])
    storage.clear_pool(engine)
    base = {"kind": "ul", "ogrn": None, "name_full": None, "locality": None, "smp_category": 1,
            "smp_since": None, "employees": 3, "okved_main_name": None, "okved_extra": [],
            "products": [], "licenses_count": 0, "as_of": None, "source": "fns_rsmp", "fetched_at": ts}
    storage.insert_pool_chunk(engine, [
        base | {"inn": "7801000001", "name_short": "ООО ЗАВОД", "region_code": "78", "okved_main": "32.50"},
        base | {"inn": "7802000002", "name_short": "ООО ОПТ", "region_code": "78", "okved_main": "46.46"},
        base | {"inn": "7803000003", "name_short": "ООО ИЗВЕСТНЫЙ", "region_code": "78", "okved_main": "32.50"},
    ], [
        {"inn": "7801000001", "code": "32.50", "kind": "okved_main"},
        {"inn": "7802000002", "code": "46.46", "kind": "okved_main"},
        {"inn": "7802000002", "code": "32.50", "kind": "okved"},
        {"inn": "7803000003", "code": "32.50", "kind": "okved_main"},
    ])
    found = discovery.find_new_companies(engine, ["32.50.13.190"], regions={"78", "47"},
                                         exclude={"7803000003"})
    by_inn = {c.inn: c for c in found}
    assert "7803000003" not in by_inn  # уже есть в истории — не «новый»
    top = found[0]
    assert top.inn == "7801000001" and top.channels == {"registry", "okved"}
    assert top.score == 60 + 10 + 10  # полный ОКПД2 в реестре + регион + два канала
    assert by_inn["7802000002"].score == 20 + 10  # дополнительный ОКВЭД + регион
    assert "5001000001" in by_inn  # производитель из реестра — из любого региона


def test_classify_role():
    r = discovery.classify_role({"okved_main": "32.50", "gisp_okpd2": [{"okpd2": "32.50.13.190"}]},
                                ["32.50.13.190"])
    assert r["value"] == "manufacturer" and r["confidence"] == "high"
    assert discovery.classify_role({"okved_main": "46.46"})["value"] == "distributor"
    assert discovery.classify_role({"okved_main": "33.12"})["value"] == "supplier"
    sw = discovery.classify_role({"okved_main": "62.01", "software_okpd2": [{"okpd2": "58.29.21"}]}, ["58.29.21"])
    assert sw["label"] == "Правообладатель" and sw["confidence"] == "medium"
    assert discovery.classify_role({})["value"] == "unknown"
