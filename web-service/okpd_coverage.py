"""Точное совпадение кодов лота с сохранённой историей поставщика в модели."""
import json
import sqlite3
from contextlib import closing


def compare_codes(items, supplier_codes):
    codes = list(dict.fromkeys(str(i.get('okpd2_code') or '').strip() for i in items))
    rows = [{'code': code, 'present': None if supplier_codes is None else code in supplier_codes}
            for code in codes if code]
    return {'items': rows, 'total': len(rows),
            'matched': None if supplier_codes is None else sum(r['present'] for r in rows),
            'available': supplier_codes is not None}


def coverage(folder, lot_id, inn, mode):
    path = folder / 'input.sqlite'
    if not path.exists():
        raise LookupError('Исходные данные лота недоступны')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        notice = db.execute('SELECT data FROM notices WHERE lot_id=?', (lot_id,)).fetchone()
        if notice is None:
            raise LookupError('Лот не найден')
        items = [json.loads(row[0]) for row in db.execute('SELECT data FROM items WHERE lot_id=?', (lot_id,))]
    supplier_codes = None
    if mode == 'live':
        from recsys.webservice import _recommender
        rec = _recommender()
        snap = rec.snapshot_for(json.loads(notice[0]))
        sid = rec.vocab.sid([inn])[0]
        history = snap.SK[snap.SK.sid == sid] if sid >= 0 else snap.SK.iloc[:0]
        if len(history):
            supplier_codes = set(rec.vocab.okpd_key[history.kid.to_numpy()])
    return compare_codes(items, supplier_codes)
