"""Важность признаков перестановкой на валидационном месяце (сентябрь 2025) — отбор признаков без участия теста.

Значения признака перемешиваются между кандидатами внутри каждого лота (контекст лота сохраняется), считается
падение NDCG@10 валидации. Повтор REPEATS раз, среднее и разброс. Признак, без которого NDCG не падает, — лишний.

    python scripts/perm_importance.py v3_full [--only e_]
"""
import argparse
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import train_ranker as T  # noqa: E402

REPEATS, LOTS = 3, 5000


def ndcg10(lot_ids, label, score):
    """Средний NDCG@10 по лотам с gain 0/1/3, как в обучении (label_gain=[0, 1, 3])."""
    gain = np.array([0, 1, 3], dtype=float)[label]
    df = pd.DataFrame({'lot': lot_ids, 'g': gain, 's': score})
    df['r'] = df.groupby('lot').s.rank(ascending=False, method='first')
    disc = 1 / np.log2(df.r + 1)
    dcg = (df.g * disc).where(df.r <= 10, 0).groupby(df.lot).sum()
    ideal = df.sort_values(['lot', 'g'], ascending=[True, False])
    ideal['r'] = ideal.groupby('lot').cumcount() + 1
    idcg = (ideal.g / np.log2(ideal.r + 1)).where(ideal.r <= 10, 0).groupby(ideal.lot).sum()
    ok = idcg > 0
    return float((dcg[ok] / idcg[ok]).mean())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('name')
    p.add_argument('--only', default='', help='префикс признаков для проверки (например, e_)')
    a = p.parse_args()
    r = json.loads(T.RESULTS.read_text(encoding='utf-8'))[a.name]
    T.TAG = r.get('tag', '')
    feats = T.FEATURE_SETS[r['features']]
    booster = lgb.Booster(model_file=str(T.FEAT_DIR / f'{a.name}.txt'))
    lots = T.valid_lots()[:LOTS]
    V = T.read(T.VALID_MONTH, ['lot_id', 'label', *feats], lots).reset_index(drop=True)
    X = V[feats].to_numpy(np.float32)
    base = ndcg10(V.lot_id.values, V.label.values, booster.predict(X))
    rng = np.random.default_rng(0)
    lot_codes = pd.factorize(V.lot_id)[0]
    grouped = np.argsort(lot_codes, kind='stable')  # позиции, сгруппированные по лотам
    rows = []
    for j, f in enumerate(feats):
        if not f.startswith(a.only):
            continue
        drops = []
        for _ in range(REPEATS):
            shuffled = np.lexsort((rng.random(len(V)), lot_codes))  # те же группы, случайный порядок внутри лота
            Xp = X.copy()
            Xp[grouped, j] = X[shuffled, j]
            drops.append(base - ndcg10(V.lot_id.values, V.label.values, booster.predict(Xp)))
        rows.append({'feature': f, 'drop_mean': np.mean(drops), 'drop_std': np.std(drops)})
        print(f'{f:22s} ΔNDCG@10 {np.mean(drops):+.4f} ± {np.std(drops):.4f}', flush=True)
    gain = dict(zip(booster.feature_name(), booster.feature_importance('gain')))
    out = pd.DataFrame(rows).assign(gain_share=lambda d: d.feature.map(gain) / sum(gain.values()))
    out = out.sort_values('drop_mean', ascending=False)
    out.to_csv(T.FEAT_DIR / f'perm_{a.name}.csv', index=False)
    print(f'\nNDCG@10 валидации {base:.4f}; {len(V.lot_id.unique()):,} лотов\n' + out.to_string(index=False))


if __name__ == '__main__':
    main()
