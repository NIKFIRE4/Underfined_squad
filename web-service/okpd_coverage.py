"""Точное и частичное (по уровням ОКПД2) совпадение кодов лота с сохранённой историей поставщика в модели."""
import json
import sqlite3
from contextlib import closing


# Уровни ОКПД2 от точного к общему: длина префикса кода XX.XX.XX.XXX и название уровня
LEVELS = [(8, 'вид'), (7, 'подгруппа'), (5, 'группа'), (2, 'класс')]


def match_level(code, supplier_codes, classes):
    """Самый глубокий общий уровень кода лота с историей поставщика: (уровень, код уровня) или (None, None)."""
    if code in supplier_codes:
        return 'точный', code
    for size, level in LEVELS:
        prefix = code[:size]
        if len(prefix) < len(code) and (prefix in classes if size == 2 else prefix in supplier_codes):
            return level, prefix
    return None, None


def compare_codes(items, supplier_codes):
    codes = list(dict.fromkeys(str(i.get('okpd2_code') or '').strip() for i in items))
    # код, исправленный проверкой при загрузке (okpd_check), — с исходным из файла
    original = {str(i.get('okpd2_code') or '').strip(): i['okpd2_original'] for i in items if i.get('okpd2_original')}
    classes = {c[:2] for c in supplier_codes or ()}
    rows = []
    for code in filter(None, codes):
        level, prefix = match_level(code, supplier_codes, classes) if supplier_codes is not None else (None, None)
        rows.append({'code': code, 'present': None if supplier_codes is None else level == 'точный',
                     'match': level, 'match_code': prefix, **({'original': original[code]} if code in original else {})})
    known = supplier_codes is not None
    return {'items': rows, 'total': len(rows),
            'matched': sum(r['present'] for r in rows) if known else None,
            'partial': sum(r['match'] not in (None, 'точный') for r in rows) if known else None,
            'available': known}


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
    if supplier_codes is None and (groups := pool_groups(inn)):
        # новая компания («Непроверенные»): истории закупок нет — сверяем с группами ОКПД2 из реестров (ОКВЭД, продукция)
        return {**compare_codes(items, groups), "basis": "pool"}
    return {**compare_codes(items, supplier_codes), "basis": "history"}


def pool_groups(inn):
    """Группы ОКПД2 (XX.XX) компании из пула новых: unverified_pool_groups в базе стенда. Нет базы — None."""
    try:
        import psycopg2
        from integrations.new_pool import db_url
        with closing(psycopg2.connect(db_url(), connect_timeout=3)) as conn, conn.cursor() as cur:
            cur.execute("select okpd2_group from unverified_pool_groups where inn = %s", (inn,))
            return {r[0] for r in cur.fetchall()} or None
    except Exception:
        return None
