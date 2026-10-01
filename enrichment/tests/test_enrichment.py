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
    assert not is_valid_inn("0000000000")  # заглушка из выгрузки проходит контрольную сумму


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


def test_bo_search_gives_okved():
    http = FakeHttp({"advanced-search": {"content": [{"id": 6530703, "inn": "<strong>7605016030</strong>",
                                                      "shortName": "ООО ТЕНЗОР", "okved2": "62.01",
                                                      "statusCode": "ACTIVE", "statusDate": "2002-10-04"}]},
                     "/bfo/": []})
    f = facts(run(bo.fetch(http, "7605016030")))
    assert f["okved_main"] == "62.01" and f["bo_id"] == "6530703" and f["bfo_found"] is False
    assert f["status"] == "Действующая организация" and f["is_liquidated"] is False


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


def test_storage_roundtrip(db_url):
    engine = storage.connect(db_url)
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
    storage.connect(db_url)


# --- discovery: новые компании и роль ---

from enrichment import discovery  # noqa: E402


def test_okpd2_levels_and_depth():
    assert discovery.okpd2_levels("32.50.13.190") == ["32.50.13.190", "32.50.13", "32.50", "32"]
    assert discovery.okpd2_levels("33.12.1") == ["33.12.1", "33.12", "33"]
    assert discovery._match_depth("32.50.13.190", ["32.50.13.190"]) == 4
    assert discovery._match_depth("32.50.13.110", ["32.50.13.190"]) == 3
    assert discovery._match_depth("33.12.11", ["33.12.1"]) == 2
    assert discovery._match_depth("21.20", ["32.50.13.190"]) == 0


def test_find_new_companies(db_url):
    engine = storage.connect(db_url)
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


# --- адаптер web-service (контракт web-service/INTEGRATION.md) ---

from dataclasses import dataclass as _dc, field as _field  # noqa: E402


@_dc
class WsLot:
    lot_id: str
    notice: dict
    items: list


@_dc
class WsCandidate:
    supplier_name: str
    supplier_inn: str = ""
    supplier_kpp: str = ""
    score: float = 0
    role: str = "Не определена"
    status: str = "Требует проверки"
    region: str = ""
    is_smp: bool | None = None
    is_new: bool = False
    reasons: list = _field(default_factory=list)
    sources: list = _field(default_factory=list)
    enrichment_status: str = "Не обогащено"


def test_webservice_adapter(db_url, monkeypatch):
    from enrichment import webservice
    monkeypatch.setenv("ENRICHMENT_DB", db_url)
    webservice._engine.cache_clear()
    webservice._known_inns.cache_clear()
    engine = webservice._engine()
    ts = now_utc()

    def res(inn, source, **fields):
        r = SourceResult(source, inn)
        for k, v in fields.items():
            r.add(k, v, ts)
        return r

    good, rnp_inn, big = "7804428656", "7814778459", "7707049388"
    storage.save_results(engine, [
        res(good, "pb", status="Действующая организация", okved_main="32.50", reg_date="2009-12-22"),
        res(good, "rmsp", is_smp=True, smp_category=1),
        res(good, "bo", revenue=17_366_000.0, finance_year=2025),
        res(good, "fns_sshr2019", employees=8.0),
        res(rnp_inn, "pb", status="Действующая организация"), res(rnp_inn, "rnp", in_rnp=True),
        res(big, "pb", status="Действующая организация"), res(big, "rmsp", is_smp=False),
        res(big, "fns_sshr2019"),
    ])
    storage.replace_registry(engine, "gisp", [
        {"inn": "7801000001", "registry": "gisp", "okpd2": "32.50.13.190", "items_count": 3,
         "sample": "Зажим", "org_name": "ООО ЗАВОД", "source": "reg_gisp", "fetched_at": ts}])

    lot = WsLot("1", {"is_smp": "true"}, [{"lot_id": "1", "product_name": "Зажим", "okpd2_code": "32.50.13.190"}])
    cands = [WsCandidate("", good, score=80), WsCandidate("X", rnp_inn, score=70),
             WsCandidate("Y", big, score=60), WsCandidate("Z", "7802000002", score=50)]
    out = {c.supplier_inn: c for c in webservice.enrich(lot, cands)}

    assert rnp_inn not in out          # в РНП — исключён
    assert big not in out              # закупка только для МСП, а он не МСП
    g = out[good]
    assert g.supplier_name == good or g.supplier_name  # имя подставлено
    assert g.is_smp is True and g.region == "78" and g.enrichment_status.startswith("Обогащено")
    assert g.role == "Производитель"   # ОКВЭД 32.50 того же класса, что и лот
    assert any("Выручка 17.4 млн ₽" in r for r in g.reasons)
    assert all(s["url"].startswith("https://") and "+" in s["checked_at"] for s in g.sources)
    assert out["7802000002"].enrichment_status == "Не обогащено"   # нет в БД: оставлен, но под проверку
    assert "7801000001" not in out    # МСП-закупка: статус МСП новой компании не подтверждён — не добавляем

    lot.notice["is_smp"] = "false"
    out = {c.supplier_inn: c for c in webservice.enrich(lot, [WsCandidate("", good, score=80)])}
    new = out["7801000001"]
    assert new.is_new and new.status == "Новый в пуле" and new.role == "Производитель"
    assert 0 < new.score <= webservice.NEW_SCORE_CAP


# --- история закупок и роль «дистрибьютор» ---

def test_history_compute(tmp_path):
    from enrichment import history
    (tmp_path / "s.csv").write_text('"lot_id";"supplier_inn";"supplier_kpp";"is_winner"\n'
                                    '1;"7804428656";"1";true\n2;"7804428656";"1";false\n3;"0274000001";;true\n')
    (tmp_path / "t.csv").write_text('"lot_id";"product_name";"okpd2_code"\n'
                                    '1;А;"21.20.10.110"\n1;Б;"21.20.10.120"\n2;В;"32.50.13.190"\n3;Г;"33.12"\n')
    (tmp_path / "n.csv").write_text('"publish_date";"lot_id";"customer_inn"\n'
                                    '2024-01-01;1;"7800000001"\n2025-05-01;2;"7800000002"\n2025-01-01;3;"7800000001"\n')
    df = history.compute(str(tmp_path / "s.csv"), str(tmp_path / "t.csv"), str(tmp_path / "n.csv"))
    r = {x["supplier_inn"]: x for x in df.to_dicts()}
    a = r["7804428656"]
    assert (a["hist_lots"], a["hist_wins"], a["hist_customers"]) == (2, 1, 2)
    assert (a["hist_okpd2_codes"], a["hist_okpd2_classes"], a["hist_last_date"]) == (3, 2, "2025-05-01")
    assert {c["cls"]: c["n"] for c in a["hist_class_codes"]} == {"21": 2, "32": 1}
    assert "0274000001" in r  # ведущий ноль ИНН сохранён


def test_classify_distributor_by_history():
    wide = {"hist_class_codes": {"21": 37}, "hist_okpd2_codes": 40, "hist_okpd2_classes": 3, "hist_customers": 12}
    r = discovery.classify_role({"okved_main": "46.46"} | wide, ["21.20.10.120"])
    assert r["value"] == "distributor" and r["confidence"] == "high"
    assert "37 разными кодами ОКПД2 класса 21" in " ".join(r["evidence"])
    assert discovery.classify_role({"okved_main": "46.46"}, ["21.20.10.120"])["confidence"] == "medium"
    only_hist = discovery.classify_role(wide, ["21.20.10.120"])
    assert only_hist["value"] == "distributor" and only_hist["confidence"] == "low"  # только из истории
    narrow = discovery.classify_role({"hist_lots": 3, "hist_wins": 1, "hist_class_codes": {"21": 1}}, ["21.20"])
    assert narrow["value"] == "supplier" and narrow["confidence"] == "low"


# --- HTTP API: ИНН → карточка ---

def test_api_supplier(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from enrichment import api

    calls = []

    async def fake_fetch_all(http, inn, sources, **kw):
        calls.append((inn, tuple(sources), kw))
        ts = now_utc()
        pb_res = SourceResult("pb", inn)
        pb_res.add("status", "Действующая организация", ts)
        pb_res.add("okved_main", "46.46", ts)
        pb_res.add("name_short", "ООО ТЕСТ", ts)
        bo_res = SourceResult("bo", inn)
        bo_res.add("revenue", 5_000_000.0, ts)
        bo_res.add("bo_id", "4436757", ts)
        ct = SourceResult("contacts", inn)
        ct.add("contacts_found", True, ts)
        ct.add("contact_phones", ["+78123271380"], ts)
        ct.add("contact_emails", ["do@example.ru"], ts)
        ct.add("contact_contract_url", "https://zakupki.gov.ru/epz/contract/contractCard/common-info.html?reestrNumber=1", ts)
        return [pb_res, bo_res, ct] + [SourceResult(s, inn, ok=False, error="captcha")
                                        for s in sources if s not in ("pb", "bo", "contacts")]

    monkeypatch.setenv("ENRICHMENT_DB", db_url)
    monkeypatch.setattr(api, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(api, "_reqnums", lambda: {INN: ["0172200004925000426"]})
    with TestClient(api.app) as client:
        r = client.get(f"/api/suppliers/{INN}")
        assert r.status_code == 200
        body = r.json()
        assert body["cached"] is False and set(body["fetched_now"]) == set(api.LIVE_SOURCES)
        assert body["company"]["name_short"] == "ООО ТЕСТ" and body["company"]["is_active"] is True
        assert body["role"]["value"] == "distributor"
        assert body["fields"]["revenue"]["source"] == "bo"
        assert {s["source"]: s["status"] for s in body["sources_status"]}["rnp"] == "captcha"
        assert calls[0][2] == {"captcha_retries": 0, "timeout": api.SOURCE_TIMEOUT,
                               "hints": {"reqnums": ["0172200004925000426"], "name": None}}
        assert body["contacts"]["phones"] == ["+78123271380"] and body["contacts"]["found"] is True
        kinds = {l["title"]: l["kind"] for l in body["links"]}
        assert kinds["Контракт ЕИС с контактами поставщика"] == "contact"
        assert any("organizations-card/4436757" in l["url"] for l in body["links"])

        # повторный запрос — из базы, без похода в интернет
        r2 = client.get(f"/api/suppliers/{INN}").json()
        assert r2["cached"] is True and len(calls) == 1
        # refresh — снова во все источники
        client.get(f"/api/suppliers/{INN}?refresh=true")
        assert len(calls) == 2

        assert client.get("/api/suppliers/7707049389").status_code == 400  # контрольная сумма
        b = client.post("/api/suppliers/batch", json={"inns": [INN, "123"]}).json()["items"]
        assert b[0]["inn"] == INN and "error" in b[1]
        assert client.get("/api/health").json()["companies_in_db"] >= 1


def test_age_from_ogrn():
    from datetime import date as _d
    assert card.ogrn_year("1099847036750") == 2009
    assert card.ogrn_year("316784700083119") == 2016  # ОГРНИП
    assert card.ogrn_year("123") is None
    row, _ = card.build_company("7804428656", [_fact("ogrn", "1099847036750", "fns_rsmp")], {"fns_rsmp": "ok"})
    assert row["age_source"] == "ogrn" and row["reg_year"] == 2009
    assert abs(row["age_years"] - (_d.today() - _d(2009, 7, 1)).days / 365.25) < 0.01
    exact, _ = card.build_company("7804428656", [_fact("ogrn", "1099847036750", "fns_rsmp"),
                                                _fact("reg_date", "2009-12-22", "egrul")], {"egrul": "ok"})
    assert exact["age_source"] == "reg_date"  # точная дата приоритетнее оценки


def test_progress(db_url):
    from enrichment import progress
    engine = storage.connect(db_url)
    storage.save_results(engine, [SourceResult("fns_rsmp", i) for i in ("7804428656", "7814778459", "7707049388")]
                         + [SourceResult("bo", "7804428656"), SourceResult("bo", "7814778459", ok=False, error="boom")])
    d = progress.compute(engine)
    bo = next(s for s in d["sources"] if s["source"] == "bo")
    assert d["total_inns"] == 3 and bo["done"] == 2 and bo["remaining"] == 1 and bo["error"] == 1
    assert bo["running"] and bo["per_min"] > 0 and bo["eta_min"] is not None
    assert [s["source"] for s in d["sources"]] == ["bo", "rnp"]  # rnp с 0% виден, egrul/pb вне шага 5 скрыты
    assert d["recent_errors"][0]["inn"] == "7814778459"


def test_contacts_parse_and_fetch():
    from enrichment.sources import contacts
    html = (FX / "eis_participants.html").read_text(encoding="utf-8")
    p = contacts.parse_participants(html)[0]
    assert p["inn"] == "7813037232" and p["phones"] == ["+78123271380"] and p["emails"] == ["do@zaoff.spb.ru"]
    assert contacts._phones("8(812)327-13-80, +7 921 000 11 22") == ["+78123271380", "+79210001122"]
    assert contacts.search_name('ООО "БРАСС"') == "БРАСС"
    search = '<a href="/epz/contract/contractCard/common-info.html?reestrNumber=2781409670624000014">'
    http = FakeHttp({"search/results": search, "participants": html})
    f = facts(run(contacts.fetch(http, "7813037232", ["0172200004923000344"], None)))
    assert f["contacts_found"] and f["contact_emails"] == ["do@zaoff.spb.ru"]
    assert f["contact_found_by"] == "закупка 0172200004923000344"
    # чужой ИНН в карточке — контакты не берём
    f2 = facts(run(contacts.fetch(http, "7804428656", ["0172200004923000344"], None)))
    assert f2 == {"contacts_found": False}


def test_rate_limited_pauses_and_retries(monkeypatch):
    from enrichment import pipeline
    from enrichment.http import Http, RateLimited
    http = Http()
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited("contacts: HTTP 429")
        return SourceResult("contacts", INN)

    before = http.limiters["contacts"].interval
    res = run(pipeline._guard("contacts", INN, http, flaky))
    assert res.ok and len(calls) == 2  # повтор после паузы
    assert http.limiters["contacts"].interval > before  # и замедление
    run(http.aclose())
