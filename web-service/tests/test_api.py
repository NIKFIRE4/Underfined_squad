import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        self.assertEqual(result['stats']['recommendations'],9)
        code,output=self.call(f'/api/jobs/{job}/download')
        self.assertEqual(code,200);self.assertIn(b'is_demo',output)
        self.assertEqual(self.call(f'/api/jobs/{job}/start','POST',b'')[0],409)

    def test_start_requires_both_files(self):
        self.assertEqual(self.call(f'/api/jobs/{self.create()}/start','POST',b'')[0],409)

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
        with patch('server.MODE','live'):
            self.assertEqual(self.call('/api/jobs','POST',b'{"top_k":3}')[0],503)

    def test_upload_limits_and_wrong_extension(self):
        job=self.create()
        self.assertEqual(self.call(f'/api/jobs/{job}/files/notices','PUT',b'abc',{'X-Filename':'evil.html'})[0],400)
        with patch('server.MAX_FILE',2):
            self.assertEqual(self.call(f'/api/jobs/{job}/files/notices','PUT',b'abc',{'X-Filename':'a.csv'})[0],400)


if __name__ == '__main__':unittest.main()
