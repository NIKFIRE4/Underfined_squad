import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models import Candidate
from pipeline import lot_quality, read_csv, run_pipeline, safe_cell, sort_lots, validate_candidates


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.state = {}
        # адаптер обогащения в репозитории — заглушка; в тестах live-режима считаем его подключённым
        self.enricher_ready = patch('pipeline.enricher.READY', True)
        self.enricher_ready.start()

    def tearDown(self):
        self.enricher_ready.stop()
        self.temp.cleanup()

    def inputs(self, notices='lot_id;subject\n0001;Медизделия\n', items='lot_id;product_name;okpd2_code\n0001;Шприц;32.50\n', encoding='utf-8-sig'):
        (self.folder/'notices.csv').write_text(notices, encoding=encoding)
        (self.folder/'items.csv').write_text(items, encoding=encoding)

    def run_job(self, mode='demo', top_k=10):
        run_pipeline(self.folder, mode, top_k, lambda **kw:self.state.update(kw))
        return self.state

    def test_end_to_end_demo_and_leading_zero(self):
        self.inputs()
        result = self.run_job(top_k=2)
        self.assertEqual(result['status'], 'completed')
        # top_k действует отдельно на «проверенных» (модель) и «непроверенных» (новые из обогащения)
        self.assertEqual((result['stats']['verified'], result['stats']['unverified'], result['stats']['recommendations']), (2, 2, 4))
        self.assertEqual(result['preview'][0]['lot_id'], '0001')
        self.assertTrue(result['preview'][0]['is_demo'])
        content = (self.folder/'suppliers.csv').read_bytes()
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(content.decode('utf-8-sig')), delimiter=';'))), 4)
        lots = [json.loads(line) for line in (self.folder/'lots.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]['lot_id'], '0001')
        self.assertEqual([c['rank'] for c in lots[0]['verified']], [1, 2])
        self.assertTrue(all(c['is_new'] for c in lots[0]['unverified']))
        self.assertEqual(lots[0]['items'], [{'name': 'Шприц', 'okpd2': '32.50'}])

    def test_cp1251_comma_and_procedure_alias(self):
        self.inputs('lot_id,procedure_name\n001,Поставка\n','lot_id,product_name,okpd2_code\n001,Бумага,17.12\n', 'cp1251')
        self.assertEqual(self.run_job()['formats']['notices']['encoding'], 'cp1251')

    def test_quoted_multiline_and_delimiter(self):
        self.inputs('lot_id;subject\n0001;"Бумага;\nкартон"\n')
        self.assertEqual('Бумага;\nкартон', self.run_job()['preview'][0]['subject'].replace('\r\n','\n'))

    def test_glued_quoted_header_cells(self):
        # файлы предзащиты: «"reqnum;procedure_name"» — два столбца в одной ячейке заголовка
        self.inputs('"lot_id";"reqnum;procedure_name";"subject"\n0001;;Поставка;Поставка бумаги\n')
        self.assertEqual('Поставка бумаги', self.run_job()['preview'][0]['subject'])

    def test_missing_column(self):
        self.inputs(items='lot_id;product_name\n0001;Шприц\n')
        with self.assertRaisesRegex(ValueError, 'okpd2_code'):
            self.run_job()

    def test_duplicate_lot(self):
        self.inputs(notices='lot_id;subject\n0001;A\n0001;B\n')
        with self.assertRaisesRegex(ValueError, 'повторяется'):
            self.run_job()

    def test_unknown_lot(self):
        self.inputs(items='lot_id;product_name;okpd2_code\nunknown;A;32\n')
        with self.assertRaisesRegex(ValueError, 'отсутствует'):
            self.run_job()

    def test_skip_notice_without_items(self):
        self.inputs(notices='lot_id;subject\n0001;A\n0002;B\n')
        result = self.run_job()
        self.assertEqual(result['stats']['without_items'], 1)
        self.assertEqual(result['stats']['lots'], 1)
        self.assertTrue(result['warnings'])

    def test_bad_row_width_and_empty_file(self):
        for contents in ['lot_id;subject\n1;A;unexpected\n', 'lot_id;subject\n']:
            with self.subTest(contents=contents):
                path = self.folder/'bad.csv'
                path.write_text(contents,encoding='utf-8')
                if 'unexpected' in contents:
                    with self.assertRaisesRegex(ValueError, 'число значений'):list(read_csv(path,'notices'))
                else:self.assertEqual(list(read_csv(path,'notices')),[])

    def test_export_formula_neutralization(self):
        for value in ['=1+1','  +1','@SUM(1)','\t=1']:
            self.assertTrue(safe_cell(value).startswith("'"))
        self.assertEqual(safe_cell('000101'), '000101')

    def test_live_enrichment_can_add_sort_and_deduplicate(self):
        self.inputs()
        base = Candidate('Один','0000000001',score=40)
        added = Candidate('Два','0000000002',score=90, is_new=True)
        with patch('pipeline.recommender.recommend',return_value=[base]), patch('pipeline.enricher.enrich',return_value=[added,base,base]):
            result = self.run_job('live', 1)
        # новая компания со скором выше не вытесняет кандидата модели: у каждого списка свой top_k
        self.assertEqual([(r['supplier_name'], r['rank'], r['is_new']) for r in result['preview']], [('Один', 1, False), ('Два', 1, True)])
        self.assertFalse(result['preview'][0]['is_demo'])
        self.assertEqual(result['stats']['recommendations'],2)

    def test_enrichment_outage_keeps_unverified_candidates(self):
        self.inputs()
        base = Candidate('Один','0000000001',score=40,status='Проверенный')
        with patch('pipeline.recommender.recommend',return_value=[base]),patch('pipeline.enricher.enrich',side_effect=TimeoutError):
            result=self.run_job('live')
        self.assertEqual(result['stats']['enrichment_errors'],1)
        self.assertEqual(result['preview'][0]['status'],'Требует проверки')

    def test_live_without_enrichment_uses_model_only(self):
        self.inputs()
        base = Candidate('0000000001','0000000001',score=40)
        with patch('pipeline.enricher.READY', False), patch('pipeline.recommender.recommend',return_value=[base]), patch('pipeline.enricher.enrich') as enrich:
            result = self.run_job('live')
        enrich.assert_not_called()
        self.assertEqual(result['stats']['verified'], 1)
        self.assertTrue(any('Обогащение не подключено' in w for w in result['warnings']))

    def test_no_candidates_is_valid(self):
        self.inputs()
        with patch('pipeline.recommender.recommend',return_value=[]),patch('pipeline.enricher.enrich',return_value=[]):
            result=self.run_job('live')
        self.assertEqual(result['stats']['recommendations'],0)
        self.assertEqual(result['stats']['without_candidates'],1)

    def test_model_error_does_not_fall_back_to_demo(self):
        self.inputs()
        with patch('pipeline.recommender.recommend',side_effect=RuntimeError('model failed')):
            with self.assertRaisesRegex(RuntimeError,'model failed'):self.run_job('live')
        self.assertFalse((self.folder/'suppliers.csv').exists())

    def test_invalid_scores_rejected(self):
        for score in [float('nan'),float('inf'),101,-1,'90',True]:
            with self.subTest(score=score):
                with self.assertRaises(ValueError):validate_candidates([Candidate('A',score=score)],'demo')

    def test_missing_product_name_allowed_with_code_and_empty_items_reported(self):
        self.inputs(items='lot_id;product_name;okpd2_code\n0001;;32.50\n0001;;\n')
        result=self.run_job()
        self.assertEqual(result['stats']['items'],1)
        self.assertEqual(result['stats']['skipped_items'],1)
        self.assertTrue(any('без названия' in w for w in result['warnings']))

    def test_lot_quality_and_sorting(self):
        # качество = 0,7 × среднее соответствие трёх лучших + 0,3 × доля компаний с данными из источников
        strong = [{'score': 90, 'supplier_name': 'ООО А', 'supplier_inn': '1', 'enrichment_status': 'Обогащено'}] * 3
        unknown = [{'score': 30, 'supplier_name': '2', 'supplier_inn': '2', 'enrichment_status': 'Нет данных в источниках'}] * 3
        self.assertEqual(lot_quality(strong), {'value': 93, 'strength': 90, 'data': 100})
        self.assertEqual(lot_quality(unknown), {'value': 21, 'strength': 30, 'data': 0})
        self.assertEqual(lot_quality([])['value'], 0)
        src = self.folder/'lots.part'
        lines = [b'{"lot_id": "weak"}\n', b'{"lot_id": "best"}\n', b'{"lot_id": "mid"}\n']
        src.write_bytes(b''.join(lines))
        keys, offset = [], 0
        for q, line in zip([10, 90, 50], lines):
            keys.append((q, offset, len(line))); offset += len(line)
        sort_lots(src, self.folder/'lots.jsonl', keys)
        self.assertEqual([json.loads(l)['lot_id'] for l in (self.folder/'lots.jsonl').read_text().splitlines()], ['best', 'mid', 'weak'])
        self.assertFalse(src.exists())


if __name__ == '__main__':unittest.main()
