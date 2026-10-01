"""Генерация кандидатов: три канала по истории и их объединение (reciprocal rank fusion)."""
import numpy as np
import pandas as pd

CODE_LEVEL_WEIGHT = {6: 1.0, 5: 0.8, 4: 0.6, 3: 0.4}
N_CODE, N_CUST, N_TEXT, KEEP, RRF_K = 200, 100, 100, 300, 60


def _top(df, score, n, rank_name):
    df = df.sort_values(['lot_id', score], ascending=[True, False])
    df[rank_name] = (df.groupby('lot_id').cumcount() + 1).astype('int16')
    return df.loc[df[rank_name] <= n, ['lot_id', 'sid', rank_name]]


def code_channel(snap, batch, n=N_CODE):
    """Поставщики с лучшей историей по кодам лота; глубокое совпадение кода весит больше."""
    x = batch.keys.merge(snap.topk_key, on='kid')
    x['s'] = x.level.map(CODE_LEVEL_WEIGHT).astype('float32') * np.log1p(x.sc).astype('float32')
    g = x.groupby(['lot_id', 'sid'], sort=False).s.sum().reset_index()
    return _top(g, 's', n, 'rank_code')


def customer_channel(snap, batch, n=N_CUST):
    """Прошлые исполнители у этого заказчика, сначала в классе ОКПД2 лота."""
    c = batch.ctx.loc[batch.ctx.cid >= 0, ['lot_id', 'cid', 'mc']]
    x = c.merge(snap.SC[['cid', 'sid', 'part', 'win']], on='cid')
    x = x.merge(snap.SCC[['cid', 'mc', 'sid', 'win']].rename(columns={'win': 'win_cc'}),
                on=['cid', 'mc', 'sid'], how='left')
    x['s'] = 2 * np.log1p(x.win_cc.fillna(0)) + np.log1p(x.win) + 0.3 * np.log1p(x.part)
    return _top(x, 's', n, 'rank_cust')


def text_channel(snap, batch, n=N_TEXT, chunk=2000):
    """Поставщики, чьи прошлые лоты ближе всего к тексту лота (косинус к центроиду)."""
    act = snap.active
    Ca = snap.centroids[act]
    rows = batch.ctx.row.values
    out = []
    for i in range(0, len(rows), chunk):
        q = batch.vecs[rows[i:i + chunk]]
        sim = q @ Ca.T
        k = min(n, sim.shape[1])
        idx = np.argpartition(-sim, k - 1, axis=1)[:, :k]
        part = np.take_along_axis(sim, idx, axis=1)
        order = np.argsort(-part, axis=1)
        idx = np.take_along_axis(idx, order, axis=1)
        lot = np.repeat(batch.ctx.lot_id.values[i:i + chunk], k)
        out.append(pd.DataFrame({'lot_id': lot, 'sid': act[idx.ravel()],
                                 'rank_text': np.tile(np.arange(1, k + 1, dtype='int16'), len(q))}))
    return pd.concat(out, ignore_index=True)


def generate(snap, batch, keep=KEEP, n_code=N_CODE, n_cust=N_CUST, n_text=N_TEXT):
    """Объединение каналов. Возвращает lot_id, sid, ранги в каналах, rrf и итоговый cand_rank."""
    c = code_channel(snap, batch, n_code)
    u = customer_channel(snap, batch, n_cust)
    t = text_channel(snap, batch, n_text)
    x = c.merge(u, on=['lot_id', 'sid'], how='outer').merge(t, on=['lot_id', 'sid'], how='outer')
    x['rrf'] = sum((1.0 / (RRF_K + x[col].astype('float32'))).fillna(0)
                   for col in ['rank_code', 'rank_cust', 'rank_text'])
    x = x.sort_values(['lot_id', 'rrf'], ascending=[True, False])
    x['cand_rank'] = (x.groupby('lot_id').cumcount() + 1).astype('int16')
    return x[x.cand_rank <= keep].reset_index(drop=True)
