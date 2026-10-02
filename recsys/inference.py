"""Сервисный инференс: сырой лот (извещение + позиции ТРУ) → ранжированный список контрагентов с объяснениями.

Пример:
    rec = Recommender('models')
    rec.recommend({
        'subject': 'Поставка крупы гречневой для нужд ГБОУ школа № 1',
        'items': [{'name': 'Крупа гречневая ядрица', 'okpd2': '10.61.32.113'}],
        'start_price': 250000, 'is_smp': True, 'platform': 'EM',
        'customer_inn': '7802141070', 'customer_kpp': '780201001',
    })

Необязательное поле лота 'publish_date' (YYYY-MM-DD) включает бэктест: если лот старше основного среза,
берётся самый свежий срез models/snapshot_YYYY-MM с cutoff не позже месяца лота — история строго
до публикации, как при оценке модели. Без подходящего среза — основной (тогда в выдачу попадает будущее).
"""
import json
import re
from collections import Counter
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .candidates import generate
from .data import BASE_YEAR, LotBatch, Vocab, code_levels, MAX_KEYS_PER_LEVEL
from .explain import explain, value_text
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
        # CRLF после git checkout на Windows ломает парсер LightGBM, поэтому нормализуем переводы строк
        self.booster = lgb.Booster(model_str=(d / 'ranker.txt').read_text(encoding='utf-8').replace('\r\n', '\n'))
        self.text = TextModel.load(d / 'text_model.joblib')
        self.vocab = Vocab.load(d / 'vocab.npz')
        self.suppliers = pd.read_parquet(d / 'suppliers.parquet')
        self.snap = Snapshot.load(d / 'snapshot')
        self.backtest = sorted((Snapshot.load(p) for p in d.glob('snapshot_*') if (p / 'cutoff.txt').exists()),
                               key=lambda s: s.cutoff)
        # шкала «Соответствие» 0–100 (scripts/calibrate_fit.py): процентиль оценки среди оценок реальных победителей
        cal = d / 'fit_calibration.json'
        self.fit_q = np.array(json.loads(cal.read_text(encoding='utf-8'))['winner_score_quantiles']) if cal.exists() else None
        self.lemm = Lemmatizer()

    def fit(self, scores):
        """Соответствие 0–100: доля реальных победителей с оценкой ниже. Не делится между кандидатами, как шанс победы."""
        if self.fit_q is None:
            return np.full(len(scores), np.nan)
        return np.interp(np.asarray(scores, dtype=np.float64), self.fit_q, np.linspace(0, 100, len(self.fit_q)))

    def snapshot_for(self, lot):
        """Срез истории для лота: основной или, для лота из прошлого, бэктестовый без заглядывания в будущее."""
        m = re.match(r'(\d{4})-(\d{2})', str(lot.get('publish_date') or ''))
        if not m:
            return self.snap
        month = (int(m[1]) - BASE_YEAR) * 12 + int(m[2]) - 1
        if month >= self.snap.cutoff:
            return self.snap
        fit = [s for s in self.backtest if s.cutoff <= month]
        return fit[-1] if fit else self.snap

    def make_batch(self, lot, snap=None):
        snap = snap or self.snap
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
            'lot_id': 0, 'month': snap.cutoff,
            'cid': int(self.vocab.cid([lot.get('customer_inn')])[0]),
            'did': int(self.vocab.did([kpp[:4] or None])[0]),
            'mc': int(self.vocab.mc([main_class])[0]),
            'mg_kid': int(self.vocab.kid([main_group])[0]),
            'em': em, 'is_smp': int(bool(lot.get('is_smp'))),
            'price_log': float(np.log1p(min(price, cap))) if price and price > 0 else np.nan,
            'n_positions': max(len(items), 1), 'n_classes': max(len({c[:2] for c in codes}), 1), 'row': 0,
        }])
        return LotBatch(ctx, keys, vecs)

    def rank(self, lot, top_n=10, n_factors=8):
        """Полный результат для API: топ-N с объяснениями и вкладами признаков, число кандидатов, предупреждения."""
        snap = self.snapshot_for(lot)
        b = self.make_batch(lot, snap)
        warnings = []
        if b.ctx.cid.iloc[0] < 0:
            warnings.append('Заказчик не встречался в истории закупок: признаки по заказчику не используются')
        if not any(isinstance(i.get('okpd2'), str) and i['okpd2'] for i in lot.get('items') or []):
            warnings.append('Не передан ни один код ОКПД2: подбор идёт по тексту и заказчику')
        elif b.keys.empty:
            warnings.append('Ни один код ОКПД2 лота не встречался в истории: подбор идёт по тексту и заказчику')
        cands = generate(snap, b)
        if cands.empty:
            return {'top': pd.DataFrame(), 'n_candidates': 0, 'warnings': warnings + ['Кандидаты не найдены'], 'batch': b}
        F = build_features(snap, b, cands)
        F['score'] = self.booster.predict(F[FEATURES], num_iteration=self.meta['best_iteration'])
        F['p_win'] = softmax_by_lot(F.lot_id, F.score, self.meta['temperature']) * self.meta['candidate_coverage']
        F['fit'] = self.fit(F.score)
        top = F.sort_values('score', ascending=False).head(top_n).reset_index(drop=True)
        contrib = self.booster.predict(top[FEATURES], num_iteration=self.meta['best_iteration'], pred_contrib=True)
        ex = explain(top, contrib)
        titles, groups = self.meta['titles'], self.meta['groups']
        usable = [j for j, f in enumerate(FEATURES) if groups[f] != 'лот']
        factors = []
        for i in range(len(top)):
            order = sorted(usable, key=lambda j: -abs(contrib[i, j]))[:n_factors]
            factors.append([{
                'feature': FEATURES[j], 'title': titles[FEATURES[j]], 'group': groups[FEATURES[j]],
                'value': None if pd.isna(top.at[i, FEATURES[j]]) else round(float(top.at[i, FEATURES[j]]), 4),
                'contribution': round(float(contrib[i, j]), 4),
            } for j in order])
        # Полный разбор оценки для интерфейса: вклад каждого признака (SHAP, логиты LambdaRank) и его значение.
        # TreeSHAP аддитивен: разность оценок двух кандидатов = сумма разностей вкладов, поэтому интерфейс может
        # честно объяснить, за счёт чего №1 выше №2.
        explanations = [{
            'p_win': round(float(top.at[i, 'p_win']), 4),
            'fit': None if pd.isna(top.at[i, 'fit']) else round(float(top.at[i, 'fit']), 1),
            'score': round(float(top.at[i, 'score']), 4),
            'factors': [{
                'feature': FEATURES[j], 'title': titles[FEATURES[j]], 'group': groups[FEATURES[j]],
                'value': value_text(FEATURES[j], top.at[i, FEATURES[j]]),
                'phi': round(float(contrib[i, j]), 4),
            } for j in usable],
        } for i in range(len(top))]
        top = top.merge(self.suppliers[['sid', 'inn']], on='sid', how='left')
        res = pd.concat([top[['inn', 'score', 'p_win', 'fit']], ex], axis=1)
        res.insert(0, 'rank', np.arange(1, len(res) + 1))
        res['factors'] = factors
        res['explanation'] = explanations
        return {'top': res, 'n_candidates': int(len(F)), 'warnings': warnings, 'batch': b}

    def recommend(self, lot, top_n=20):
        return self.rank(lot, top_n)['top']
