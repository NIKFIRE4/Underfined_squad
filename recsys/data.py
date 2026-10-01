"""Загрузка очищенных таблиц, словари идентификаторов, история участий и пакеты лотов."""
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

BASE_YEAR = 2024
CODE_LEVELS = {6: 'okpd_l6', 5: 'okpd_l5', 4: 'okpd_l4', 3: 'okpd_l3'}  # категория, вид, подгруппа, группа
MAX_KEYS_PER_LEVEL = 5


def to_month(dt):
    """Номер месяца от января 2024: 0 = 2024-01, 23 = 2025-12."""
    return ((dt.dt.year - BASE_YEAR) * 12 + dt.dt.month - 1).astype('int16')


def month_label(m):
    return f'{BASE_YEAR + m // 12}-{m % 12 + 1:02d}'


def code_levels(code):
    """Уровни иерархии ОКПД2 для одного кода: {6: категория, 5: вид, 4: подгруппа, 3: группа}."""
    if not isinstance(code, str) or len(code) < 5:
        return {}
    out = {3: code[:5]}
    if len(code) >= 7:
        out[4] = code[:7]
    if len(code) >= 8:
        out[5] = code[:8]
    if len(code) == 12:
        out[6] = code
    return out


def build_lot_keys(tru):
    """(lot_id, level, key, weight): до MAX_KEYS_PER_LEVEL самых весомых кодов лота на каждом уровне."""
    parts = []
    for level, col in CODE_LEVELS.items():
        x = tru[['lot_id', col, 'n_rows']].dropna(subset=[col])
        x = (x.groupby(['lot_id', col], sort=False).n_rows.sum().reset_index()
               .rename(columns={col: 'key', 'n_rows': 'weight'})
               .sort_values(['lot_id', 'weight'], ascending=[True, False]))
        x = x.groupby('lot_id').head(MAX_KEYS_PER_LEVEL).copy()
        x['level'] = np.int8(level)
        parts.append(x)
    return pd.concat(parts, ignore_index=True)


@dataclass
class Vocab:
    """Словари «внешний идентификатор → внутренний номер». Неизвестное значение → -1."""
    supplier_inn: np.ndarray
    customer_inn: np.ndarray
    district: np.ndarray
    okpd_class: np.ndarray
    okpd_key: np.ndarray

    def _index(self, arr, values):
        idx = pd.Index(arr).get_indexer(pd.Series(values).astype(object))
        return idx.astype('int32')

    def sid(self, inn): return self._index(self.supplier_inn, inn)
    def cid(self, inn): return self._index(self.customer_inn, inn)
    def did(self, d): return self._index(self.district, d).astype('int16')
    def mc(self, c): return self._index(self.okpd_class, c).astype('int16')
    def kid(self, k): return self._index(self.okpd_key, k)

    def save(self, path):
        np.savez_compressed(path, **{k: getattr(self, k).astype(str) for k in self.__dataclass_fields__})

    @classmethod
    def load(cls, path):
        z = np.load(path, allow_pickle=False)
        return cls(**{k: z[k].astype(object) for k in cls.__dataclass_fields__})


@dataclass
class LotBatch:
    """Пакет лотов в формате, который понимают генератор кандидатов и построитель признаков.

    ctx  — по строке на лот: lot_id, cid, did, mc, mg_kid, em, is_smp, price_log, n_positions, n_classes, row
    keys — коды ОКПД2 лота по уровням: lot_id, level, kid
    vecs — текстовые векторы лотов, строка ctx.row
    """
    ctx: pd.DataFrame
    keys: pd.DataFrame
    vecs: np.ndarray

    def subset(self, lot_ids):
        ctx = self.ctx[self.ctx.lot_id.isin(lot_ids)].reset_index(drop=True)
        return LotBatch(ctx, self.keys[self.keys.lot_id.isin(lot_ids)], self.vecs)


class Dataset:
    """Очищенные данные из 01_eda_preprocessing в виде, удобном для расчёта профилей по срезам."""

    LOT_COLS = ['lot_id', 'publish_date', 'split', 'platform', 'is_smp', 'price_log', 'customer_inn',
                'customer_district', 'main_class', 'main_group', 'n_positions', 'n_classes',
                'subject_lemma', 'train_eligible']

    def __init__(self, processed_dir='data/processed'):
        p = Path(processed_dir)
        lots = pd.read_parquet(p / 'lots.parquet', columns=self.LOT_COLS)
        tru = pd.read_parquet(p / 'tru.parquet', columns=['lot_id', 'name_lemma', 'n_rows', *CODE_LEVELS.values()])
        sup = pd.read_parquet(p / 'suppliers.parquet',
                              columns=['lot_id', 'supplier_inn', 'is_winner', 'inn_type', 'supplier_region'])

        lot_keys = build_lot_keys(tru)
        self.vocab = Vocab(
            supplier_inn=np.sort(sup.supplier_inn.unique()).astype(object),
            customer_inn=np.sort(lots.customer_inn.dropna().unique()).astype(object),
            district=np.sort(lots.customer_district.dropna().unique()).astype(object),
            okpd_class=np.sort(lots.main_class.dropna().unique()).astype(object),
            okpd_key=np.sort(pd.concat([lot_keys.key, lots.main_group.dropna()]).unique()).astype(object),
        )
        v = self.vocab

        lots = lots.reset_index(drop=True)
        lots['row'] = np.arange(len(lots), dtype='int32')
        lots['month'] = to_month(lots.publish_date)
        lots['cid'] = v.cid(lots.customer_inn)
        lots['did'] = v.did(lots.customer_district)
        lots['mc'] = v.mc(lots.main_class)
        lots['mg_kid'] = v.kid(lots.main_group)
        lots['em'] = (lots.platform == 'EM').astype('int8')
        lots['is_smp'] = lots.is_smp.astype('int8')
        lots['price_log'] = lots.price_log.astype('float32')
        self.lots = lots

        lot_keys['kid'] = v.kid(lot_keys.key)
        self.lot_keys = lot_keys[['lot_id', 'level', 'kid', 'weight']].reset_index(drop=True)

        sid = v.sid(sup.supplier_inn)
        first = sup.assign(sid=sid).drop_duplicates('sid').set_index('sid').sort_index()
        self.suppliers = pd.DataFrame({
            'sid': np.arange(len(v.supplier_inn), dtype='int32'),
            'inn': v.supplier_inn,
            's_is_ip': (first.inn_type == 'IP').astype('int8').values,
            's_is_spb': (first.supplier_region == '78').astype('int8').values,
        })

        ev = pd.DataFrame({'lot_id': sup.lot_id.values, 'sid': sid, 'win': sup.is_winner.astype('int8').values})
        ev = ev.merge(lots[['lot_id', 'row', 'month', 'cid', 'did', 'mc', 'em', 'price_log']], on='lot_id')
        self.events = ev

        ek = ev[['lot_id', 'sid', 'win', 'month']].merge(self.lot_keys[['lot_id', 'kid']], on='lot_id')
        self.ek_month = (ek.groupby(['kid', 'sid', 'month'], sort=False)
                           .agg(part=('win', 'size'), win=('win', 'sum')).reset_index())
        self.key_month = (self.lot_keys[['lot_id', 'kid']].merge(lots[['lot_id', 'month']], on='lot_id')
                            .groupby(['kid', 'month']).size().rename('n_lots').reset_index())
        self._tru_names = tru[['lot_id', 'name_lemma']]

    def lot_docs(self, max_names=20):
        """Текст лота для TF-IDF: предмет + названия первых позиций (леммы), в порядке self.lots."""
        names = (self._tru_names.dropna().groupby('lot_id').head(max_names)
                   .groupby('lot_id').name_lemma.agg(' '.join))
        docs = self.lots.subject_lemma.fillna('') + ' ' + self.lots.lot_id.map(names).fillna('')
        return docs.str.strip().tolist()

    def batch(self, lot_ids, vecs):
        ctx = self.lots[self.lots.lot_id.isin(lot_ids)][
            ['lot_id', 'month', 'cid', 'did', 'mc', 'mg_kid', 'em', 'is_smp', 'price_log', 'n_positions', 'n_classes', 'row']
        ].reset_index(drop=True)
        keys = self.lot_keys[self.lot_keys.lot_id.isin(lot_ids)][['lot_id', 'level', 'kid']]
        return LotBatch(ctx, keys, vecs)

    def labels(self, lot_ids):
        """Истинные участники лотов: label 2 — победитель, 1 — участник."""
        e = self.events[self.events.lot_id.isin(lot_ids)]
        return e[['lot_id', 'sid']].assign(label=(1 + e.win).astype('int8').values)
