import json
import csv
import io
import zipfile
import xml.etree.ElementTree as ET
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


def make_xlsx(rows):
    """Минимальная книга .xlsx: один лист, строки через общий словарь строк."""
    import io, zipfile
    from xml.sax.saxutils import escape
    strings=[v for r in rows for v in r]
    cells=lambda r,ri:''.join(f'<c r="{chr(65+ci)}{ri}" t="s"><v>{strings.index(v)}</v></c>' for ci,v in enumerate(r))
    sheet='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(f'<row r="{i}">{cells(r,i)}</row>' for i,r in enumerate(rows,1))+'</sheetData></worksheet>'
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Лист1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',sheet)
        z.writestr('xl/sharedStrings.xml','<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'+''.join(f'<si><t>{escape(v)}</t></si>' for v in strings)+'</sst>')
    return buf.getvalue()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mode=patch('server.MODE','demo')  # тесты API — на деморежиме; по умолчанию сервер работает моделью
        cls.mode.start()
        cls.temp=tempfile.TemporaryDirectory()
        cls.original_data=server.DATA
        server.DATA=Path(cls.temp.name)
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.base=f'http://127.0.0.1:{cls.http.server_port}'
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join()
        server.DATA=cls.original_data
        cls.temp.cleanup()
        cls.mode.stop()

    def call(self,path,method='GET',data=None,headers=None):
        req=Request(self.base+path,data=data,method=method,headers=headers or {})
        try:
            with urlopen(req,timeout=5) as r:return r.status,r.read()
        except HTTPError as e:
            with e:return e.code,e.read()

    def create(self):
        code,raw=self.call('/api/jobs','POST',b'{"top_k": 3}',{'Content-Type':'application/json'})
        self.assertEqual(code,201)
        return json.loads(raw)['id']

    def test_http_round_trip(self):
        job=self.create()
        self.assertEqual(self.call(f'/api/jobs/{job}/download')[0],409)
        for kind in ['notices','items']:
            payload=(server.ROOT/'examples'/f'{kind}.csv').read_bytes()
            code,_=self.call(f'/api/jobs/{job}/files/{kind}','PUT',payload,{'X-Filename':f'{kind}.csv'})
            self.assertEqual(code,200)
        self.assertEqual(self.call(f'/api/jobs/{job}/start','POST',b'')[0],202)
        for _ in range(100):
            code,raw=self.call(f'/api/jobs/{job}')
            result=json.loads(raw)
            if result['status'] in ('completed','failed'):break
            time.sleep(.03)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['stats']['recommendations'],15)
        code,raw=self.call(f'/api/jobs/{job}/lots?limit=2')
        page=json.loads(raw)
        self.assertEqual(code,200)
        self.assertEqual([l['lot_id'] for l in page['lots']],['000101','000102'])
        self.assertTrue(page['has_more'])
        self.assertEqual(page['total'],3)
        code,raw=self.call(f'/api/jobs/{job}/lots?offset=2&limit=2')
        self.assertEqual([l['lot_id'] for l in json.loads(raw)['lots']],['000103'])
        self.assertFalse(json.loads(raw)['has_more'])
        code,raw=self.call(f'/api/jobs/{job}/lots?q='+quote('канцеляр'))
        self.assertEqual([l['lot_id'] for l in json.loads(raw)['lots']],['000102'])
        code,output=self.call(f'/api/jobs/{job}/download')
        self.assertEqual(code,200);self.assertIn(b'is_demo',output)
        self.assertEqual(self.call(f'/api/jobs/{job}/start','POST',b'')[0],409)

    def test_auto_upload_detects_csv_and_xlsx_by_columns(self):
        job=self.create()
        notices=(server.ROOT/'examples'/'notices.csv').read_bytes()
        code,_=self.call(f'/api/jobs/{job}/files/auto','PUT',make_xlsx([['lot_id','product_name','okpd2_code'],['000101','Шприц','32.50.13.110'],['000102','Бумага','17.12.14.110']]),{'X-Filename':quote('ТРУ.xlsx')})
        self.assertEqual(code,200)
        self.assertEqual(self.call(f'/api/jobs/{job}/files/auto','PUT',notices,{'X-Filename':'random.csv'})[0],200)
        self.assertEqual(self.call(f'/api/jobs/{job}/start','POST',b'')[0],202)
        result=self.wait(job)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['stats']['lots'],2)
        self.assertEqual(result['detected']['items'],'ТРУ.xlsx, лист «Лист1»')

    def test_per_lot_exports_use_full_result_and_preserve_identifiers(self):
        job = self.create()
        folder = server.DATA / job
        lot_id = '00042 / лот & 7'
        fields = ['lot_id', 'supplier_inn', 'supplier_kpp', 'supplier_name', 'is_new']
        selected = [dict(zip(fields, [lot_id, '0123456789', '001234567', "'=1+1", 'False'])),
                    dict(zip(fields, [lot_id, '012345678901', '', 'Компания; «А»\nБ', 'True']))]
        with (folder / 'suppliers.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fields, delimiter=';')
            writer.writeheader()
            writer.writerows([dict(zip(fields, ['other', '9999999999', '', 'Другая', 'False']))] * 120)
            writer.writerows(selected)
        (folder / 'lots.jsonl').write_text(json.dumps({'lot_id': 'empty'}) + '\n', encoding='utf-8')
        server.JOBS[job].update(status='completed', preview=[])
        base = f'/api/jobs/{job}/download'
        code, raw = self.call(base + '?lot_id=' + quote(lot_id) + '&format=csv')
        self.assertEqual(code, 200)
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';')), selected)
        with urlopen(self.base + base + '?lot_id=' + quote(lot_id) + '&format=xlsx') as response:
            self.assertIn('spreadsheetml.sheet', response.headers['Content-Type'])
            self.assertIn('filename=suppliers.xlsx', response.headers['Content-Disposition'])
            self.assertIn('%D0%BB%D0%BE%D1%82', response.headers['Content-Disposition'])
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        rows = [[c.find('m:is/m:t', ns).text or '' for c in row] for row in root.findall('m:sheetData/m:row', ns)]
        self.assertEqual(rows, [fields] + [[r[k] for k in fields] for r in selected])
        self.assertFalse(root.findall('.//m:f', ns))
        self.assertEqual(self.call(base + '?lot_id=unknown')[0], 404)
        self.assertEqual(self.call(base + '?lot_id=&format=csv')[0], 400)
        self.assertEqual(self.call(base + '?lot_id=empty&format=pdf')[0], 400)
        code, raw = self.call(base + '?lot_id=empty&format=csv')
        self.assertEqual(code, 200)
        self.assertEqual(list(csv.reader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';')), [fields])
        self.assertEqual(self.call(base + '?lot_id=empty&format=xlsx')[0], 200)
        self.assertEqual(self.call(base)[1], (folder / 'suppliers.csv').read_bytes())
        server.JOBS[job]['status'] = 'processing'
        self.assertEqual(self.call(base + '?lot_id=empty&format=xlsx')[0], 409)
        server.JOBS[job]['status'] = 'completed'

    def test_auto_upload_reports_missing_table(self):
        job=self.create()
        notices=(server.ROOT/'examples'/'notices.csv').read_bytes()
        self.call(f'/api/jobs/{job}/files/auto','PUT',notices,{'X-Filename':'a.csv'})
        self.assertEqual(self.call(f'/api/jobs/{job}/start','POST',b'')[0],202)
        result=self.wait(job)
        self.assertEqual(result['status'],'failed')
        self.assertIn('ТРУ',result['message'])

    def test_upload_rejects_xls_and_supplier_stats_is_stub(self):
        job=self.create()
        self.assertEqual(self.call(f'/api/jobs/{job}/files/auto','PUT',b'abc',{'X-Filename':'old.xls'})[0],400)
        code,raw=self.call('/api/suppliers/7806410527/stats')
        self.assertEqual((code,json.loads(raw)),(200,{'inn':'7806410527','available':False}))
        self.assertEqual(self.call('/api/suppliers/123/stats')[0],404)

    def wait(self,job):
        for _ in range(200):
            result=json.loads(self.call(f'/api/jobs/{job}')[1])
            if result['status'] in ('completed','failed'):return result
            time.sleep(.03)
        return result

    def test_start_requires_both_files(self):
        self.assertEqual(self.call(f'/api/jobs/{self.create()}/start','POST',b'')[0],409)

    def test_roles_load_offline_and_ignore_unknown(self):
        response = {'items': [{'inn': '0123456789', 'role': {'value': 'manufacturer', 'label': 'Производитель'}},
                              {'inn': '1234567890', 'role': {'value': 'unknown', 'label': 'Не определена'}},
                              {'inn': '9999999999', 'error': 'Нет данных'}]}
        with patch.object(server.enricher, 'READY', True), patch.object(server.enricher, '_request', return_value=response) as fetch:
            code, raw = self.call('/api/suppliers/roles', 'POST', json.dumps({'inns': ['0123456789', '1234567890']}).encode())
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw), {'roles': {'0123456789': 'Производитель'}})
            self.assertTrue(fetch.call_args.args[2]['offline'])
            for inns in ([], ['wrong'], ['0123456789'] * 51):
                self.assertEqual(self.call('/api/suppliers/roles', 'POST', json.dumps({'inns': inns}).encode())[0], 400)

    def test_recommendations_route_preserved_with_large_payload(self):
        payload = {'subject': 'Поставка бумаги', 'items': [{'name': 'Бумага ' * 3000}], 'top_k': 10}
        expected = {'items': [], 'warnings': []}
        with patch.object(server.recommender, 'READY', True), patch.object(server.recommender, 'recommend_detailed', return_value=expected) as recommend:
            code, raw = self.call('/api/recommendations', 'POST', json.dumps(payload).encode(), {'Content-Type': 'application/json'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), expected)
        recommend.assert_called_once_with(payload)

    def test_invalid_top_k_and_unsafe_origin(self):
        self.assertEqual(self.call('/api/jobs','POST',b'{"top_k":0}')[0],400)
        self.assertEqual(self.call('/api/jobs','POST',b'{"top_k":3}',{'Origin':'https://example.com'})[0],403)

    def test_live_mode_refuses_unconnected_modules(self):
        with patch('server.MODE','live'), patch.object(server.recommender,'READY',False):
            self.assertEqual(self.call('/api/jobs','POST',b'{"top_k":3}')[0],503)
        with patch('server.MODE','live'), patch.object(server.recommender,'READY',True), patch.object(server.enricher,'READY',False):
            self.assertEqual(self.call('/api/jobs','POST',b'{"top_k":3}')[0],201)

    def test_upload_limits_and_wrong_extension(self):
        job=self.create()
        self.assertEqual(self.call(f'/api/jobs/{job}/files/notices','PUT',b'abc',{'X-Filename':'evil.html'})[0],400)
        with patch('server.MAX_FILE',2):
            self.assertEqual(self.call(f'/api/jobs/{job}/files/notices','PUT',b'abc',{'X-Filename':'a.csv'})[0],400)

    def test_same_files_served_from_cache(self):
        def run():
            job=self.create()
            for kind in ['notices','items']:
                self.call(f'/api/jobs/{job}/files/{kind}','PUT',(server.ROOT/'examples'/f'{kind}.csv').read_bytes(),{'X-Filename':f'{kind}.csv'})
            self.assertEqual(self.call(f'/api/jobs/{job}/start','POST',b'')[0],202)
            return self.wait(job)
        with patch('server.CACHE_TTL',0):  # те же примеры уже обрабатывали другие тесты класса
            first=run()
        self.assertEqual(first['status'],'completed',first)
        self.assertNotIn('cached_from',first)
        second=run()
        self.assertEqual(second['status'],'completed',second)
        self.assertEqual(second['cached_from'],first['id'])
        self.assertEqual(second['stats'],first['stats'])
        self.assertEqual(self.call(f'/api/jobs/{second["id"]}/lots')[0],200)
        # сверка ОКПД2 у результата из кэша берёт позиции лота из исходной задачи
        lot=first['preview'][0]['lot_id']
        self.assertEqual(self.call(f'/api/jobs/{second["id"]}/coverage?lot_id={lot}&inn=7707083893')[0],200)
        self.assertEqual(self.call(f'/api/jobs/{second["id"]}/download')[1],self.call(f'/api/jobs/{first["id"]}/download')[1])
        with patch('server.CACHE_TTL',0):
            self.assertNotIn('cached_from',run())


if __name__ == '__main__':unittest.main()
