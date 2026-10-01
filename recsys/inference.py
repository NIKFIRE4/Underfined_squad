"""Сервисный инференс: сырой лот (извещение + позиции ТРУ) → ранжированный список контрагентов с объяснениями.

Пример:
    rec = Recommender('models')
    rec.recommend({
        'subject': 'Поставка крупы гречневой для нужд ГБОУ школа № 1',
        'items': [{'name': 'Крупа гречневая ядрица', 'okpd2': '10.61.32.113'}],
        'start_price': 250000, 'is_smp': True, 'platform': 'EM',
        'customer_inn': '7802141070', 'customer_kpp': '780201001',
    })
"""
import json
from collections import Counter
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .candidates import generate
from .data import LotBatch, Vocab, code_levels, MAX_KEYS_PER_LEVEL
from .explain import explain
from .features import FEATURES, build_features
from .profiles import Snapshot
from .text import TextModel
from .textprep import Lemmatizer, clean_text


def softmax_by_lot(lot_ids, scores, temperature):
    s = pd.Series(np.asarray(scores, dtype=np.float64) / temperature)
    m = s.groupby(np.asarray(lot_ids)).transform('max')
    e = np.exp(s - m)
    return (e / e.groupby(np.asarray(lot_ids)).transform('sum')).values


class Recommender:
    def __init__(self, model_dir='models'):
        d = Path(model_dir)
        self.meta = json.loads((d / 'meta.json').read_text(encoding='utf-8'))
        self.booster = lgb.Booster(model_file=str(d / 'ranker.txt'))
        self.text = TextModel.load(d / 'text_model.joblib')
        self.vocab = Vocab.load(d / 'vocab.npz')
        self.suppliers = pd.read_parquet(d / 'suppliers.parquet')
        self.snap = Snapshot.load(d / 'snapshot')
        self.lemm = Lemmatizer()

    def make_batch(self, lot):
        items = lot.get('items') or []
        subj = self.lemm(clean_text(lot.get('subject', '')))
        names = [self.lemm(clean_text(i.get('name', ''))) for i in items[:20]]
        vecs = self.text.transform([' '.join([subj, *names]).strip()])

        weights = Counter()
        for it in items:
            for level, key in code_levels(it.get('okpd2')).items():
                weights[(level, key)] += 1
        keys = pd.DataFrame([(0, lv, k, w) for (lv, k), w in weights.items()], columns=['lot_id', 'level', 'key', 'w'])
        if len(keys):
            keys = keys.sort_values(['level', 'w'], ascending=[True, False]).groupby('level').head(MAX_KEYS_PER_LEVEL)
            keys['kid'] = self.vocab.kid(keys.key)
            keys = keys[keys.kid >= 0]
        keys = keys.reindex(columns=['lot_id', 'level', 'kid']).astype({'lot_id': 'int64', 'level': 'int8', 'kid': 'int32'})

        codes = [i.get('okpd2') for i in items if isinstance(i.get('okpd2'), str)]
        main_class = Counter(c[:2] for c in codes).most_common(1)[0][0] if codes else None
        main_group = Counter(c[:5] for c in codes if len(c) >= 5).most_common(1)
        main_group = main_group[0][0] if main_group else None
        em = int(lot.get('platform', 'AISGZ') == 'EM')
        price = lot.get('start_price')
        cap = self.meta['price_cap']['EM' if em else 'AISGZ']
        kpp = lot.get('customer_kpp') or ''
        ctx = pd.DataFrame([{
            'lot_id': 0, 'month': self.snap.cutoff,
            'cid': int(self.vocab.cid([lot.get('customer_inn')])[0]),
            'did': int(self.vocab.did([kpp[:4] or None])[0]),
            'mc': int(self.vocab.mc([main_class])[0]),
            'mg_kid': int(self.vocab.kid([main_group])[0]),
            'em': em, 'is_smp': int(bool(lot.get('is_smp'))),
            'price_log': float(np.log1p(min(price, cap))) if price and price > 0 else np.nan,
            'n_positions': max(len(items), 1), 'n_classes': max(len({c[:2] for c in codes}), 1), 'row': 0,
        }])
        return LotBatch(ctx, keys, vecs)

    def recommend(self, lot, top_n=20):
        b = self.make_batch(lot)
        cands = generate(self.snap, b)
        if cands.empty:
            return pd.DataFrame()
        F = build_features(self.snap, b, cands)
        F['score'] = self.booster.predict(F[FEATURES], num_iteration=self.meta['best_iteration'])
        F['p_win'] = softmax_by_lot(F.lot_id, F.score, self.meta['temperature']) * self.meta['candidate_coverage']
        top = F.sort_values('score', ascending=False).head(top_n).reset_index(drop=True)
        contrib = self.booster.predict(top[FEATURES], num_iteration=self.meta['best_iteration'], pred_contrib=True)
        ex = explain(top, contrib)
        top = top.merge(self.suppliers[['sid', 'inn']], on='sid', how='left')
        res = pd.concat([top[['inn', 'score', 'p_win']], ex], axis=1)
        res.insert(0, 'rank', np.arange(1, len(res) + 1))
        return res
