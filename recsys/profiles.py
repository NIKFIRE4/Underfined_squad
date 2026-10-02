"""Профили поставщиков на срез: агрегаты по истории участий строго до месяца cutoff.

Для лота месяца M используется срез cutoff=M (история до начала месяца). На проде cutoff — следующий
месяц после последних данных. Один класс для обучения и сервиса, поэтому признаки совпадают.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp

TOPK_PER_KEY = 300  # сколько лучших поставщиков хранить на каждый код ОКПД2 для канала кандидатов


class Snapshot:
    TABLES = ['S', 'SK', 'KT', 'SC', 'SCC', 'SDC', 'topk_key']

    def __init__(self, cutoff, tables, centroids):
        self.cutoff = int(cutoff)
        for name in self.TABLES:
            setattr(self, name, tables[name])
        self.centroids = centroids
        self.active = np.flatnonzero(np.abs(centroids).sum(axis=1) > 0).astype('int32')

    @classmethod
    def build(cls, ds, cutoff, lot_vecs, suppliers=None):
        ev = ds.events[ds.events.month < cutoff]
        n_sup = len(ds.vocab.supplier_inn)
        suppliers = ds.suppliers if suppliers is None else suppliers

        # поставщик целиком
        g = ev.groupby('sid')
        S = pd.DataFrame({
            's_n_part': g.size(),
            's_n_win': g.win.sum(),
            'last_part': g.month.max(),
            's_n_customers': ev[ev.cid >= 0].groupby('sid').cid.nunique(),
            's_em_share': g.em.mean(),
            'price_p10': g.price_log.quantile(0.1),
            'price_med': g.price_log.median(),
            'price_p90': g.price_log.quantile(0.9),
        })
        S['last_win'] = ev[ev.win == 1].groupby('sid').month.max()
        # окна свежести: те же счётчики за последние месяцы перед срезом
        for col, months, what in [('s_win_3m', 3, 'win'), ('s_win_6m', 6, 'win'), ('s_part_6m', 6, 'part')]:
            w = ev[ev.month >= cutoff - months]
            S[col] = w.groupby('sid').win.sum() if what == 'win' else w.groupby('sid').size()
        S = suppliers.set_index('sid')[['s_is_ip', 's_is_spb']].join(S, how='left')
        cnt = ['s_n_part', 's_n_win', 's_n_customers', 's_win_3m', 's_win_6m', 's_part_6m']
        S[cnt] = S[cnt].fillna(0)
        S = S.astype('float32')

        # поставщик × код ОКПД2 (все уровни) и число лотов по коду
        ekm = ds.ek_month[ds.ek_month.month < cutoff]
        SK = ekm.groupby(['kid', 'sid']).agg(part=('part', 'sum'), win=('win', 'sum')).reset_index()
        lw = ekm[ekm.win > 0].groupby(['kid', 'sid']).month.max().rename('last_win')
        SK = SK.merge(lw, on=['kid', 'sid'], how='left')
        recent = ekm[ekm.month >= cutoff - 6]
        r6 = recent.groupby(['kid', 'sid']).agg(win_6m=('win', 'sum'), part_6m=('part', 'sum'))
        r3 = recent[recent.month >= cutoff - 3].groupby(['kid', 'sid']).win.sum().rename('win_3m')
        SK = SK.merge(r6.join(r3, how='left').reset_index(), on=['kid', 'sid'], how='left')
        SK[['win_6m', 'part_6m', 'win_3m']] = SK[['win_6m', 'part_6m', 'win_3m']].fillna(0).astype('float32')
        KT = ds.key_month[ds.key_month.month < cutoff].groupby('kid').n_lots.sum().rename('kt').reset_index()

        topk = SK.assign(sc=SK.win + 0.2 * SK.part).sort_values(['kid', 'sc'], ascending=[True, False])
        topk = topk.groupby('kid').head(TOPK_PER_KEY)[['kid', 'sid', 'sc']].reset_index(drop=True)

        # поставщик × заказчик, × заказчик × класс, × район × класс
        evc = ev[ev.cid >= 0]
        SC = evc.groupby(['cid', 'sid']).agg(part=('win', 'size'), win=('win', 'sum')).reset_index()
        SC = SC.merge(evc[evc.win == 1].groupby(['cid', 'sid']).month.max().rename('last_win'),
                      on=['cid', 'sid'], how='left')
        SC = SC.merge(evc[evc.month >= cutoff - 12].groupby(['cid', 'sid']).win.sum().rename('win_12m'),
                      on=['cid', 'sid'], how='left')
        SC['win_12m'] = SC.win_12m.fillna(0).astype('float32')
        SCC = evc[evc.mc >= 0].groupby(['cid', 'mc', 'sid']).agg(part=('win', 'size'), win=('win', 'sum')).reset_index()
        evd = ev[(ev.did >= 0) & (ev.mc >= 0)]
        SDC = evd.groupby(['did', 'mc', 'sid']).win.sum().reset_index()

        # текстовый центроид поставщика: взвешенная сумма векторов его лотов (победа весит вдвое)
        A = sp.csr_matrix(((1 + ev.win.values).astype(np.float32), (ev.sid.values, ev.row.values)),
                          shape=(n_sup, lot_vecs.shape[0]))
        C = np.asarray(A @ lot_vecs, dtype=np.float32)
        C /= np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-8)

        tables = {'S': S, 'SK': SK, 'KT': KT, 'SC': SC, 'SCC': SCC, 'SDC': SDC, 'topk_key': topk}
        return cls(cutoff, tables, C)

    def save(self, path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        for name in self.TABLES:
            getattr(self, name).to_parquet(path / f'{name}.parquet')
        np.save(path / 'centroids.npy', self.centroids)
        (path / 'cutoff.txt').write_text(str(self.cutoff))

    @classmethod
    def load(cls, path):
        path = Path(path)
        tables = {name: pd.read_parquet(path / f'{name}.parquet') for name in cls.TABLES}
        return cls(int((path / 'cutoff.txt').read_text()), tables, np.load(path / 'centroids.npy'))
