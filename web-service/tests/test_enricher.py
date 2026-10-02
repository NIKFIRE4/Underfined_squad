import unittest
from unittest.mock import patch

from integrations import enricher
from models import Candidate, Lot


def card(inn, **company):
    co = {"inn": inn, "name_short": f'ООО "К{inn[-2:]}"', "kpp": "780601001", "region_name": "Санкт-Петербург",
          "is_smp": True, "is_active": True, "in_rnp": False, "enrichment_status": "full", **company}
    return {"inn": inn, "company": co, "role": {"value": "supplier", "label": "Поставщик-исполнитель"},
            "risk_flags": co.pop("risk_flags", []),
            "fields": {"status": {"value": "Действующая", "source": "pb", "fetched_at": "2026-10-01T12:00:00+00:00"}},
            "sources_status": [{"source": "pb", "name": "ФНС «Прозрачный бизнес»", "status": "ok", "url": "https://pb.nalog.ru/"}]}


class EnricherTests(unittest.TestCase):
    def lot(self, smp="false"):
        return Lot("1", {"subject": "Шприцы", "is_smp": smp}, [])

    def cand(self, inn):
        return Candidate(supplier_name=inn, supplier_inn=inn, score=90, status="Проверенный", reasons=["Победы по ОКПД2"])

    def test_fills_candidate_from_card(self):
        with patch.object(enricher, "fetch_cards", return_value={"7804428656": card("7804428656")}):
            [c] = enricher.enrich(self.lot(), [self.cand("7804428656")])
        self.assertEqual((c.supplier_name, c.supplier_kpp, c.region, c.is_smp, c.role, c.status, c.enrichment_status),
                         ('ООО "К56"', "780601001", "Санкт-Петербург", True, "Поставщик-исполнитель", "Проверенный", "Обогащено"))
        self.assertEqual(c.sources, [{"field": "Статус", "source": "ФНС «Прозрачный бизнес»", "url": "https://pb.nalog.ru/",
                                      "checked_at": "2026-10-01T12:00:00+00:00"}])

    def test_business_filters_and_review_status(self):
        cards = {"7804428656": card("7804428656", is_active=False), "7707083893": card("7707083893", in_rnp=True),
                 "7802174011": card("7802174011", is_smp=False), "7813668206": card("7813668206", is_active=None),
                 "7806344338": card("7806344338", risk_flags=[{"code": "young", "text": "Компания моложе года"}])}
        with patch.object(enricher, "fetch_cards", return_value=cards):
            out = enricher.enrich(self.lot(smp="true"), [self.cand(i) for i in [*cards, "7839135474"]])
        self.assertEqual([c.supplier_inn for c in out], ["7813668206", "7806344338", "7839135474"])
        self.assertEqual([c.status for c in out[:2]], ["Требует проверки", "Требует проверки"])
        self.assertIn("Риск: Компания моложе года", out[1].reasons)
        self.assertEqual(out[2].enrichment_status, "Нет данных в источниках")

    def test_unreachable_service_raises(self):
        with patch.object(enricher, "API_URL", "http://127.0.0.1:9"):
            with self.assertRaises(enricher.EnrichmentError) as ctx:
                enricher.fetch_card("7804428656")
        self.assertEqual(ctx.exception.status, 502)


class NewPoolTests(unittest.TestCase):
    """Вкладка «Непроверенные»: пул новых компаний из базы (integrations/new_pool.py), таблицы подменены."""

    def setUp(self):
        import pandas as pd
        from integrations import new_pool
        self.new_pool = new_pool
        rows = [  # inn, группа, доказательство, уровень пула, приоритет
            ("7801000001", "28.93", "контракты 28.93 (26)", "active", 9.0),             # сильное
            ("7801000002", "28.93", "контракты 28.93 (1); ОКВЭД 47.79", "strong", 4.0),  # сильное
            ("7801000003", "28.93", "ОКВЭД 59.1 (осн.)", "strong", 8.0),                 # только ассоциация по ОКВЭД
            ("4701000004", "28.93", "ОКВЭД 28.93 (осн.)", "signal", 3.0),                # основной ОКВЭД = группа, ЛО
            ("7801000005", "21.20", "контракты 21.20 (5)", "strong", 9.9),               # другая группа
        ]
        groups = pd.DataFrame(rows, columns=["inn", "okpd2_group", "evidence", "tier", "priority"])
        companies = pd.DataFrame([{"inn": r[0], "name": f"ООО Н{r[0][-1]}", "name_short": None, "region": r[0][:2],
                                   "is_msp": r[0] != "4701000004", "role": "производитель (по реестру)", "role_evidence": "",
                                   "tier": r[3], "pool_reason": "налоги за 2025 г.: 1 000 ₽", "mos_source": "Портал поставщиков",
                                   "registry_sources": None, "registry_source_date": None,
                                   "msp_source": "ФНС: Единый реестр МСП", "msp_source_date": pd.Timestamp("2026-09-10")}
                                  for r in rows])
        self.patches = [patch.object(new_pool, "_tables", return_value=(groups, companies)),
                        patch.object(new_pool, "_pool", None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def lot(self, smp="false", code="28.93.15.110"):
        return Lot("1", {"subject": "Мясорубка", "is_smp": smp}, [{"product_name": "Мясорубка", "okpd2_code": code}])

    def test_order_by_evidence_then_priority(self):
        out = self.new_pool.find(self.lot(), enricher.REGIONS, exclude=set())
        # сильное доказательство по группе → только ОКВЭД, хотя у последнего приоритет выше; внутри — приоритет
        self.assertEqual([c.supplier_inn for c in out], ["7801000001", "7801000002", "4701000004", "7801000003"])
        second = out[1]
        self.assertTrue(second.is_new)
        self.assertEqual((second.supplier_name, second.status, second.role, second.region),
                         ("ООО Н2", "Новый в пуле", "Производитель", "Санкт-Петербург"))
        self.assertEqual(second.reasons[0], "1 госконтракт по группе ОКПД2 28.93 на Портале поставщиков")
        self.assertIn("Налоги за 2025 г.: 1 000 ₽", second.reasons)
        self.assertEqual([s["field"] for s in second.sources], ["Госконтракты", "МСП"])
        self.assertEqual(out[0].score, 75.0)
        self.assertTrue(all(c.score <= 75 for c in out))

    def test_pool_unavailable_gives_empty_block(self):
        with patch.object(self.new_pool, "_tables", side_effect=OSError("нет базы")):
            self.assertEqual(self.new_pool.find(self.lot(), enricher.REGIONS, set()), [])

    def test_smp_only_lot_and_exclusions(self):
        out = self.new_pool.find(self.lot(smp="true"), enricher.REGIONS, exclude={"7801000001"})
        self.assertEqual([c.supplier_inn for c in out], ["7801000002", "7801000003"])  # не МСП и уже в списке модели — нет

    def test_lot_without_codes_skips_pool(self):
        self.assertEqual(self.new_pool.find(self.lot(code=""), enricher.REGIONS, set()), [])
        self.assertIsNone(self.new_pool._pool)  # пул даже не загружался

    def test_new_companies_survive_enrichment_outage(self):
        with patch.object(enricher, "fetch_cards", side_effect=enricher.EnrichmentError(502, "down")):
            out = enricher.enrich(self.lot(), [Candidate(supplier_name="7804428656", supplier_inn="7804428656", score=90)])
        self.assertEqual(out[0].status, "Требует проверки")
        self.assertEqual(sum(c.is_new for c in out), 4)


if __name__ == "__main__":
    unittest.main()
