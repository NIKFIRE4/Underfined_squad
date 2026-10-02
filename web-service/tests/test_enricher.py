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


if __name__ == "__main__":
    unittest.main()
