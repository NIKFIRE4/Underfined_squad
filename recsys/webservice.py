"""Адаптер модели под контракт web-service (web-service/INTEGRATION.md, раздел «Команда модели»).

В web-service/integrations/recommender.py достаточно:

    import sys; sys.path.insert(0, "..")          # корень репозитория, если пакет не установлен
    from recsys.webservice import READY, recommend

Модель грузится один раз при первом вызове (~6 с), дальше ~1–2 с на лот.
Переменные окружения:
  RANKER_MODEL_DIR   папка с артефактами (по умолчанию models/ в корне репозитория)
  RANKER_OVERFETCH   во сколько раз больше top_k отдавать, чтобы после фильтров обогащения осталось top_k (по умолчанию 2)
"""
import math
import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

READY = True

ROOT = Path(__file__).resolve().parent.parent
HISTORY_SOURCE = 'История закупок АИС ГЗ и ЭМ (выгрузка организаторов)'
EM_PRICE_LIMIT = 600_000  # 99,9% лотов ЭМ не дороже — по нему угадываем площадку, если её нет в извещении


@lru_cache(maxsize=1)
def _recommender():
    from .inference import Recommender
    return Recommender(os.environ.get('RANKER_MODEL_DIR', ROOT / 'models'))


def _candidate_cls():
    from models import Candidate  # модуль web-service
    return Candidate


def _num(v):
    try:
        x = float(str(v).replace('\xa0', '').replace(' ', '').replace(',', '.'))
        return x if math.isfinite(x) and x >= 0 else None
    except (TypeError, ValueError):
        return None


def _digits(v, lengths):
    s = ''.join(ch for ch in str(v or '') if ch.isdigit())
    return s if len(s) in lengths else None


def _platform(notice, price):
    raw = str(notice.get('is_eshop_or_aisgz') or notice.get('platform') or '').strip().upper()
    if raw in ('ЭМ', 'EM', 'ESHOP'):
        return 'EM'
    if raw.startswith('АИС') or raw == 'AISGZ':
        return 'AISGZ'
    return 'EM' if price is None or price <= EM_PRICE_LIMIT else 'AISGZ'


def lot_to_input(lot):
    """Lot из web-service (все поля — строки) → вход Recommender."""
    n = lot.notice
    price = _num(n.get('start_price'))
    return {
        'subject': n.get('subject') or n.get('procedure_name') or '',
        'items': [{'name': i.get('product_name') or '', 'okpd2': (i.get('okpd2_code') or '').strip() or None}
                  for i in lot.items],
        'start_price': price,
        'is_smp': str(n.get('is_smp', '')).strip().lower() in ('true', '1', 'да'),
        'platform': _platform(n, price),
        'customer_inn': _digits(n.get('customer_inn'), (10,)),
        'customer_kpp': _digits(n.get('customer_kpp'), (9,)),
    }


def warmup():
    """Загрузить модель заранее (сервер вызывает в фоне при старте), чтобы первый запрос не ждал ~6 с."""
    _recommender().lemm('прогрев')


OKPD2_RE = re.compile(r'^\d{2}(\.\d(\d(\.\d(\d(\.\d{3})?)?)?)?)?$')


def parse_request(p: dict) -> tuple[dict, int]:
    """Тело POST /api/recommendations → вход Recommender и top_k. Ошибки — ValueError с понятным текстом."""
    subject = p.get('subject')
    if not isinstance(subject, str) or len(subject.strip()) < 3:
        raise ValueError('subject: укажите предмет закупки (не короче 3 символов)')
    items = p.get('items') or []
    if not isinstance(items, list) or len(items) > 5000:
        raise ValueError('items: ожидается список позиций (не больше 5000)')
    clean_items = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ValueError(f'items[{i}]: ожидается объект {{"name", "okpd2"}}')
        code = (it.get('okpd2') or '').strip() or None
        if code and not OKPD2_RE.match(code):
            raise ValueError(f'items[{i}].okpd2: неверный формат кода ОКПД2 «{code}», пример: 10.61.32.113')
        clean_items.append({'name': str(it.get('name') or ''), 'okpd2': code})
    price = p.get('start_price')
    if price is not None and (_num(price) is None):
        raise ValueError('start_price: ожидается неотрицательное число')
    inn, kpp = p.get('customer_inn'), p.get('customer_kpp')
    if inn not in (None, '') and _digits(inn, (10,)) != str(inn):
        raise ValueError('customer_inn: 10 цифр')
    if kpp not in (None, '') and _digits(kpp, (9,)) != str(kpp):
        raise ValueError('customer_kpp: 9 цифр')
    top_k = p.get('top_k', 10)
    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise ValueError('top_k: целое число от 1 до 50')
    price = _num(price) if price is not None else None
    # Площадки нет в контракте — модели она нужна как признак, угадываем по НМЦК (по умолчанию ЭМ).
    return {'subject': subject, 'items': clean_items, 'start_price': price,
            'is_smp': bool(p.get('is_smp', False)), 'platform': _platform({}, price),
            'customer_inn': inn or None, 'customer_kpp': kpp or None}, top_k


def recommend_detailed(payload: dict) -> dict:
    """POST /api/recommendations: данные новой закупки → топ-K поставщиков со скором, причинами и признаками."""
    lot, top_k = parse_request(payload)
    rec = _recommender()
    t0 = time.time()
    out = rec.rank(lot, top_n=top_k, n_factors=8)
    top, n_cands = out['top'], out['n_candidates']
    items = [] if top.empty else [{
        'rank': int(r['rank']),
        'inn': r['inn'],
        'score': round(100.0 * (1 - (r['rank'] - 1) / max(n_cands, 1)), 1),
        'p_win': round(float(r['p_win']), 4),
        'status': r['status'],
        'status_rule': r['status_rule'],
        'reasons': r['reasons'],
        'risks': r['risks'],
        'factors': r['factors'],
    } for r in top.to_dict('records')]
    return {
        'model_version': f"lgbm-lambdarank-{rec.meta['test_months'][-1]}-it{rec.meta['best_iteration']}",
        'took_ms': int((time.time() - t0) * 1000),
        'candidates_considered': n_cands,
        'lot': {'customer_known': bool(out['batch'].ctx.cid.iloc[0] >= 0),
                'okpd2_recognized': int(len(out['batch'].keys))},
        'warnings': out['warnings'],
        'items': items,
    }


def recommend(lot, top_k: int) -> list:
    """Контракт web-service: кандидаты для одного лота, score 0–100 по убыванию."""
    Candidate = _candidate_cls()
    rec = _recommender()
    k = max(top_k, top_k * int(os.environ.get('RANKER_OVERFETCH', '2')))
    out = rec.rank(lot_to_input(lot), top_n=k, n_factors=0)
    top, n_cands = out['top'], out['n_candidates']
    if top.empty:
        return []
    checked_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    res = []
    for r in top.to_dict('records'):
        # score — место среди всех кандидатов лота по шкале 0–100: 100 у лучшего, ~97 у десятого из 300.
        # Так кандидаты с историей стоят выше новых компаний из обогащения (у них потолок 75).
        score = round(100.0 * (1 - (r['rank'] - 1) / max(n_cands, 1)), 1)
        res.append(Candidate(
            supplier_name=r['inn'],  # временно: название подставит обогащение по ИНН
            supplier_inn=r['inn'],
            score=score,
            status=r['status'],
            reasons=[f"Вероятность победы по модели: {100 * r['p_win']:.0f}%"] + r['reasons'][:2],
            sources=[{'field': 'score', 'source': HISTORY_SOURCE, 'url': 'https://zakupki.gov.ru/', 'checked_at': checked_at}],
            explanation=r['explanation'] | {'place': int(r['rank']), 'of': n_cands},
        ))
    return res
