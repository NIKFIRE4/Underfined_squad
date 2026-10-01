import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.received = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(self.server.result_status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(self.server.result_body)


class AnalyzeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.model = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
        cls.threads = []
        for service in (cls.http, cls.model):
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            cls.threads.append(thread)

    @classmethod
    def tearDownClass(cls):
        for service in (cls.http, cls.model):
            service.shutdown()
            service.server_close()
        for thread in cls.threads:
            thread.join()

    def setUp(self):
        self.model.received = None
        self.model.result_status = 200
        self.model.result_body = json.dumps({'top_10': [{'inn': '0123456789', 'score': 0.91}]}).encode()
        self.environment = patch.dict('os.environ', {
            'MODEL_ANALYZE_URL': f'http://127.0.0.1:{self.model.server_port}/predict',
            'MODEL_ANALYZE_TIMEOUT': '2',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def call(self, raw=b'{"subject":"test"}', route='/analyze', headers=None):
        req = Request(f'http://127.0.0.1:{self.http.server_port}{route}', data=raw,
                      headers=headers or {'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_forwards_fields_and_returns_model_result_without_enrichment(self):
        fields = {'lot_id': '0007', 'subject': 'Бумага', 'top_k': 10,
                  'items': [{'product_name': 'А4', 'okpd2_code': '17.12'}], 'filters': {'is_smp': True}}
        for route in ('/analyze', '/api/analyze'):
            code, result = self.call(json.dumps(fields, ensure_ascii=False).encode(), route)
            self.assertEqual(code, 200)
            self.assertEqual(self.model.received, fields)
            self.assertEqual(result, json.loads(self.model.result_body))

    def test_unconfigured_model_returns_503_not_demo(self):
        with patch.dict('os.environ', {'MODEL_ANALYZE_URL': ''}):
            self.assertEqual(self.call()[0], 503)
        self.assertIsNone(self.model.received)

    def test_invalid_requests_never_reach_model(self):
        for raw in (b'{}', b'[]', b'{', b'{"score":NaN}', b'{"x":"\xff"}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.call(raw)[0], 400)
        self.assertEqual(self.call(headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.call(headers={'Content-Type': 'application/json', 'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.call(b' ' * (1024 * 1024 + 1))[0], 413)
        self.assertIsNone(self.model.received)

    def test_model_http_error_returns_502(self):
        self.model.result_status = 422
        self.assertEqual(self.call()[0], 502)

    def test_invalid_model_responses_return_502(self):
        for response in (b'not json', b'null', b'{"score":NaN}', b'{"score":1e10000}', b'x' * (4 * 1024 * 1024 + 1)):
            with self.subTest(response_length=len(response)):
                self.model.result_body = response
                self.assertEqual(self.call()[0], 502)

    def test_model_timeout_returns_504(self):
        with patch('integrations.analysis.build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError()
            self.assertEqual(self.call()[0], 504)

    def test_rejects_recursive_model_address(self):
        with patch.dict('os.environ', {'MODEL_ANALYZE_URL': f'http://127.0.0.1:{self.http.server_port}/analyze'}):
            self.assertEqual(self.call()[0], 503)


if __name__ == '__main__':
    unittest.main()
