"""Проверка моделей на тестовом датасете организаторов (лоты января–августа 2026, 02.10.2026).

Модели этот датасет не видели: обучение и срезы истории — по декабрь 2025 включительно.
Ответов в датасете нет, поэтому победителей берём из ЕИС (zakupki.gov.ru): номер закупки (reqnum) →
результаты определения поставщика → карточка контракта → ИНН поставщика. Так находятся победители лотов АИС ГЗ
с номером закупки; у лотов Электронного магазина номера нет, их контрактов в реестре ЕИС нет.
Ответы кэшируются в data/test2026/eis_winners.csv (--refresh — запросить заново).

    python scripts/eval_test2026.py models models_v3            # сравнить модели
Вход модели — тот же, что у сервиса (recsys.webservice.lot_to_input).
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
NOTICES = ROOT / 'Тестовый датасет_извещения_20261002.csv'
ITEMS = ROOT / 'Тестовый датасет_потоварка_20261002.csv'
OUT = ROOT / 'data' / 'test2026'
EIS = 'https://zakupki.gov.ru/epz'
KINDS = ['ea20', 'ok20', 'zk20', 'ezk20', 'ep44']
TOP = (1, 5, 10, 20)


def read_csv(path):
    return pd.read_csv(path, sep=';', dtype=str, encoding='utf-8-sig', keep_default_na=False)


def fetch_winners(notices):
    """reqnum → ИНН поставщика(ов) по контракту из ЕИС."""
    import warnings
    import httpx
    from enrichment.sources.contacts import parse_participants
    warnings.filterwarnings('ignore')
    c = httpx.Client(verify=False, timeout=40, follow_redirects=True,
                     headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36'})

    def get(url, **params):
        for _ in range(3):
            try:
                r = c.get(url, params=params)
                time.sleep(1.2)
                if r.status_code == 200:
                    return r.text
            except httpx.HTTPError:
                time.sleep(5)
        return ''

    rows = []
    for n in notices[notices.reqnum != ''].itertuples():
        contracts, stage = [], ''
        for kind in KINDS:
            html = get(f'{EIS}/order/notice/{kind}/view/supplier-results.html', regNumber=n.reqnum)
            if 'Объект закупки' in html:
                contracts = list(dict.fromkeys(re.findall(r'contractCard/common-info\.html\?reestrNumber=(\d+)', html)))
                m = re.search(r'Определение поставщика (завершено|отменено)|Работа комиссии|Подача заявок', html)
                stage = m[0] if m else ''
                break
        for num in contracts:
            for p in parse_participants(get(f'{EIS}/contract/contractCard/participants.html', reestrNumber=num)):
                rows.append({'lot_id': n.lot_id, 'reqnum': n.reqnum, 'stage': stage, 'contract': num,
                             'supplier_inn': p['inn'], 'supplier_name': p['name']})
        if not contracts:
            rows.append({'lot_id': n.lot_id, 'reqnum': n.reqnum, 'stage': stage})
        print(n.lot_id, n.reqnum, stage, contracts, flush=True)
    return pd.DataFrame(rows)


def lots(notices, items):
    from recsys.webservice import lot_to_input
    by_lot = {k: g.to_dict('records') for k, g in items.groupby('lot_id')}
    return {n['lot_id']: lot_to_input(SimpleNamespace(notice=n, items=by_lot.get(n['lot_id'], [])))
            for n in notices.to_dict('records')}


def rank_all(model_dir, lot_inputs):
    """Полный список кандидатов каждого лота с местом: lot_id, inn, rank."""
    from recsys.inference import Recommender
    rec = Recommender(ROOT / model_dir)
    out = []
    for lot_id, lot in lot_inputs.items():
        r = rec.rank(lot, top_n=10_000, n_factors=0)['top']
        if len(r):
            out.append(pd.DataFrame({'lot_id': lot_id, 'inn': r.inn.values, 'rank': r['rank'].values}))
    return pd.concat(out, ignore_index=True)


def metrics(ranked, winners):
    """Recall@k и MRR по лотам с известным победителем (нескольких победителей лота считаем одним успехом)."""
    w = winners.dropna(subset=['supplier_inn'])[['lot_id', 'supplier_inn']].drop_duplicates()
    hit = w.merge(ranked, left_on=['lot_id', 'supplier_inn'], right_on=['lot_id', 'inn'], how='left')
    best = hit.groupby('lot_id')['rank'].min()
    res = {f'Recall@{k}': float((best <= k).mean()) for k in TOP}
    res['MRR'] = float((1 / best).fillna(0).mean())
    res['в кандидатах'] = float(best.notna().mean())
    res['лотов'] = int(len(best))
    return res, best


def main():
    p = argparse.ArgumentParser()
    p.add_argument('models', nargs='+', help='папки моделей относительно корня репозитория')
    p.add_argument('--refresh', action='store_true', help='заново получить победителей из ЕИС')
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    notices, items = read_csv(NOTICES), read_csv(ITEMS)
    cache = OUT / 'eis_winners.csv'
    winners = fetch_winners(notices) if a.refresh or not cache.exists() else pd.read_csv(cache, sep=';', dtype=str)
    winners.to_csv(cache, sep=';', index=False)
    labeled = winners.dropna(subset=['supplier_inn']).lot_id.nunique()
    print(f'лотов {len(notices)}, с номером закупки {int((notices.reqnum != "").sum())}, с победителем из ЕИС {labeled}')

    inputs = lots(notices, items)
    report, per_lot = {}, {}
    for m in a.models:
        t = time.time()
        ranked = rank_all(m, inputs)
        ranked.to_csv(OUT / f'ranked_{Path(m).name}.csv', sep=';', index=False)
        report[m], per_lot[m] = metrics(ranked, winners)
        print(f'{m}: {json.dumps(report[m], ensure_ascii=False)}  ({time.time() - t:.0f} с)')
    table = pd.DataFrame(per_lot).rename_axis('lot_id')
    print('\nместо победителя по лотам:\n' + table.to_string())
    (OUT / 'report.json').write_text(json.dumps({'metrics': report, 'winner_rank': table.astype(object).where(table.notna(), None).to_dict()},
                                                ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
