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
        ))
    return res
