"""Метрики ранжирования по всем лотам с победителем, включая лоты, где победитель не попал в кандидаты."""
import numpy as np
import pandas as pd


def winner_ranks(scored, truth, score_col):
    """Ранг победителя в выдаче по score_col для каждого лота из truth (inf — победителя нет среди кандидатов)."""
    s = scored[['lot_id', 'sid', score_col]].sort_values(['lot_id', score_col], ascending=[True, False])
    s['rank'] = s.groupby('lot_id').cumcount() + 1
    w = truth[truth.label == 2][['lot_id', 'sid']].merge(s[['lot_id', 'sid', 'rank']], on=['lot_id', 'sid'], how='left')
    return w.groupby('lot_id')['rank'].min().fillna(np.inf)


def ndcg_at(scored, truth, score_col, k=10):
    s = scored[['lot_id', 'sid', score_col]].sort_values(['lot_id', score_col], ascending=[True, False])
    s['rank'] = s.groupby('lot_id').cumcount() + 1
    s = s[s['rank'] <= k].merge(truth, on=['lot_id', 'sid'], how='inner')
    dcg = ((2.0 ** s.label - 1) / np.log2(s['rank'] + 1)).groupby(s.lot_id).sum()
    t = truth.sort_values(['lot_id', 'label'], ascending=[True, False]).copy()
    t['i'] = t.groupby('lot_id').cumcount() + 1
    t = t[t.i <= k]
    idcg = ((2.0 ** t.label - 1) / np.log2(t.i + 1)).groupby(t.lot_id).sum()
    lots = truth[truth.label == 2].lot_id.unique()
    return float((dcg.reindex(lots).fillna(0) / idcg.reindex(lots)).mean())


def evaluate(scored, truth, score_col, ks=(1, 5, 10, 20, 50, 100)):
    r = winner_ranks(scored, truth, score_col)
    res = {f'Recall@{k}': float((r <= k).mean()) for k in ks}
    res['MRR'] = float((1.0 / r).mean())
    res['NDCG@10'] = ndcg_at(scored, truth, score_col, 10)
    res['лотов'] = int(len(r))
    return res
