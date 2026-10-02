import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from okpd_coverage import compare_codes, coverage


class CoverageTests(unittest.TestCase):
    def test_exact_match_unique_codes_not_prefix(self):
        items = [{'okpd2_code': code} for code in [' 32.50.13.110 ', '32.50.13.110', '32.50.13.120', '', '17.12']]
        result = compare_codes(items, {'32.50.13.110', '32.50', '17.12'})
        self.assertEqual((result['matched'], result['total']), (2, 3))
        self.assertEqual([r['present'] for r in result['items']], [True, False, True])

    def test_unknown_is_not_absent(self):
        result = compare_codes([{'okpd2_code': '32.50'}], None)
        self.assertFalse(result['available'])
        self.assertIsNone(result['matched'])
        self.assertIsNone(result['items'][0]['present'])

    def test_known_empty_profile_and_empty_lot(self):
        self.assertEqual(compare_codes([{'okpd2_code': '32.50'}], set())['matched'], 0)
        self.assertEqual(compare_codes([{}], set())['total'], 0)

    def test_uses_all_items_and_exact_lot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with closing(sqlite3.connect(folder / 'input.sqlite')) as db, db:
                db.execute('CREATE TABLE notices (lot_id TEXT, data TEXT)')
                db.execute('CREATE TABLE items (lot_id TEXT, data TEXT)')
                db.execute('INSERT INTO notices VALUES (?, ?)', ('0001', '{}'))
                db.executemany('INSERT INTO items VALUES (?, ?)', [('0001', json.dumps({'okpd2_code': f'32.50.13.{i:03}'})) for i in range(45)])
                db.execute('INSERT INTO items VALUES (?, ?)', ('other', '{"okpd2_code":"99.99"}'))
            result = coverage(folder, '0001', '0123456789', 'demo')
            self.assertEqual(result['total'], 45)
            self.assertFalse(result['available'])
            with self.assertRaises(LookupError):
                coverage(folder, '1', '0123456789', 'demo')
