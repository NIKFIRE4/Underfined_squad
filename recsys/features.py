"""Признаки пары «лот × кандидат». Все признаки имеют предметный смысл и используются в объяснениях."""
import numpy as np
import pandas as pd

# (имя, группа, монотонность, описание). Монотонность +1: больше значение → оценка не ниже.
FEATURE_SPEC = [
    ('lot_em', 'лот', 0, 'Площадка лота — Электронный магазин'),
    ('lot_smp', 'лот', 0, 'Закупка только у МСП'),
    ('lot_price_log', 'лот', 0, 'НМЦК лота, log(1+₽)'),
    ('lot_n_positions', 'лот', 0, 'Число позиций в лоте'),
    ('lot_n_classes', 'лот', 0, 'Число классов ОКПД2 в лоте'),
    ('s_n_win', 'поставщик', 0, 'Побед всего'),
    ('s_n_part', 'поставщик', 0, 'Участий всего'),
    ('s_win_rate', 'поставщик', 0, 'Доля побед среди участий'),
    ('s_m_since_win', 'поставщик', 0, 'Месяцев с последней победы'),
    ('s_m_since_part', 'поставщик', 0, 'Месяцев с последнего участия'),
    ('s_n_customers', 'поставщик', 0, 'Число разных заказчиков'),
    ('s_em_share', 'поставщик', 0, 'Доля участий в Электронном магазине'),
    ('s_is_ip', 'поставщик', 0, 'Индивидуальный предприниматель'),
    ('s_is_spb', 'поставщик', 0, 'Зарегистрирован в Санкт-Петербурге'),
    ('platform_fit', 'поставщик', 0, 'Доля участий поставщика на площадке лота'),
    ('price_dev', 'цена', 0, 'НМЦК лота минус медиана НМЦК его лотов (лог-шкала)'),
    ('price_in_range', 'цена', 0, 'НМЦК в его диапазоне p10–p90'),
    ('code_win_l6', 'ОКПД2', 1, 'Побед с тем же кодом ОКПД2 (категория)'),
    ('code_win_l5', 'ОКПД2', 1, 'Побед с тем же видом ОКПД2'),
    ('code_win_l4', 'ОКПД2', 1, 'Побед с той же подгруппой ОКПД2'),
    ('code_win_l3', 'ОКПД2', 1, 'Побед с той же группой ОКПД2'),
    ('code_part_l5', 'ОКПД2', 1, 'Участий с тем же видом ОКПД2'),
    ('code_part_l3', 'ОКПД2', 1, 'Участий с той же группой ОКПД2'),
    ('code_cover_l5', 'ОКПД2', 1, 'Доля видов ОКПД2 лота, где он участвовал'),
    ('code_cover_l3', 'ОКПД2', 1, 'Доля групп ОКПД2 лота, где он участвовал'),
    ('code_m_since_win', 'ОКПД2', 0, 'Месяцев с последней победы по кодам лота'),
    ('group_share', 'ОКПД2', 1, 'Доля побед в основной группе ОКПД2 лота'),
    ('cust_win', 'заказчик', 1, 'Побед у этого заказчика'),
    ('cust_part', 'заказчик', 0, 'Участий у этого заказчика'),
    ('cust_class_win', 'заказчик', 1, 'Побед у этого заказчика в классе ОКПД2 лота'),
    ('cust_m_since_win', 'заказчик', 0, 'Месяцев с последней победы у этого заказчика'),
    ('district_class_win', 'заказчик', 1, 'Побед в классе ОКПД2 лота у заказчиков того же района'),
    ('text_cos', 'текст', 1, 'Сходство текста лота с его прошлыми лотами'),
]
FEATURES = [f[0] for f in FEATURE_SPEC]
MONOTONE = [f[2] for f in FEATURE_SPEC]
GROUP = {f[0]: f[1] for f in FEATURE_SPEC}
TITLE = {f[0]: f[3] for f in FEATURE_SPEC}


def _code_features(snap, batch, pairs):
    pk = pairs[['lot_id', 'sid']].merge(batch.keys, on='lot_id')
    pk = pk.merge(snap.SK, on=['kid', 'sid'], how='left')
    pk['covered'] = (pk.part > 0).astype('int8')
    agg = pk.groupby(['lot_id', 'sid', 'level']).agg(
        win=('win', 'max'), part=('part', 'max'), covered=('covered', 'sum'), last_win=('last_win', 'max'))
    nkeys = batch.keys.groupby(['lot_id', 'level']).size().rename('nk')
    agg = agg.join(nkeys, on=['lot_id', 'level'])
    agg['cover'] = agg.covered / agg.nk
    wide = agg[['win', 'part', 'cover']].unstack('level')
    wide.columns = [f'code_{a}_l{b}' for a, b in wide.columns]
    wide['code_last_win'] = agg.last_win.groupby(['lot_id', 'sid']).max()
    for col in ['code_win_l6', 'code_win_l5', 'code_win_l4', 'code_win_l3', 'code_part_l5', 'code_part_l3',
                'code_cover_l5', 'code_cover_l3']:
        if col not in wide:
            wide[col] = np.nan
    return wide.reset_index()


def build_features(snap, batch, cands, chunk_lots=4000):
    """Матрица признаков для пар (lot_id, sid) из cands. Возвращает DataFrame с lot_id, sid и FEATURES."""
    lot_ids = batch.ctx.lot_id.values
    out = []
    for i in range(0, len(lot_ids), chunk_lots):
        ids = lot_ids[i:i + chunk_lots]
        b = batch.subset(ids)
        x = cands.loc[cands.lot_id.isin(ids), ['lot_id', 'sid']].merge(b.ctx, on='lot_id', how='left')
        x = x.merge(snap.S, left_on='sid', right_index=True, how='left')
        x = x.merge(_code_features(snap, b, x), on=['lot_id', 'sid'], how='left')

        mg = snap.SK[['kid', 'sid', 'win']].rename(columns={'kid': 'mg_kid', 'win': 'mg_win'})
        x = x.merge(mg, on=['mg_kid', 'sid'], how='left').merge(
            snap.KT.rename(columns={'kid': 'mg_kid'}), on='mg_kid', how='left')
        x = x.merge(snap.SC.rename(columns={'part': 'cust_part', 'win': 'cust_win', 'last_win': 'cust_last_win'}),
                    on=['cid', 'sid'], how='left')
        x = x.merge(snap.SCC[['cid', 'mc', 'sid', 'win']].rename(columns={'win': 'cust_class_win'}),
                    on=['cid', 'mc', 'sid'], how='left')
        x = x.merge(snap.SDC.rename(columns={'win': 'district_class_win'}), on=['did', 'mc', 'sid'], how='left')

        cut = snap.cutoff
        f = pd.DataFrame({'lot_id': x.lot_id.values, 'sid': x.sid.values})
        f['lot_em'] = x.em
        f['lot_smp'] = x.is_smp
        f['lot_price_log'] = x.price_log
        f['lot_n_positions'] = x.n_positions
        f['lot_n_classes'] = x.n_classes
        f['s_n_win'] = x.s_n_win.fillna(0)
        f['s_n_part'] = x.s_n_part.fillna(0)
        f['s_win_rate'] = x.s_n_win / x.s_n_part
        f['s_m_since_win'] = cut - x.last_win
        f['s_m_since_part'] = cut - x.last_part
        f['s_n_customers'] = x.s_n_customers.fillna(0)
        f['s_em_share'] = x.s_em_share
        f['s_is_ip'] = x.s_is_ip
        f['s_is_spb'] = x.s_is_spb
        f['platform_fit'] = np.where(x.em == 1, x.s_em_share, 1 - x.s_em_share)
        f['price_dev'] = x.price_log - x.price_med
        f['price_in_range'] = ((x.price_log >= x.price_p10) & (x.price_log <= x.price_p90)).astype('float32')
        f.loc[x.price_med.isna() | x.price_log.isna(), 'price_in_range'] = np.nan
        for col in ['code_win_l6', 'code_win_l5', 'code_win_l4', 'code_win_l3', 'code_part_l5', 'code_part_l3',
                    'code_cover_l5', 'code_cover_l3']:
            f[col] = x[col].fillna(0) if col.startswith(('code_win', 'code_part')) else x[col]
        f['code_m_since_win'] = cut - x.code_last_win
        f['group_share'] = x.mg_win.fillna(0) / x.kt
        f['cust_win'] = x.cust_win.fillna(0)
        f['cust_part'] = x.cust_part.fillna(0)
        f['cust_class_win'] = x.cust_class_win.fillna(0)
        f['cust_m_since_win'] = cut - x.cust_last_win
        f['district_class_win'] = x.district_class_win.fillna(0)
        f['text_cos'] = np.einsum('ij,ij->i', b.vecs[x.row.values], snap.centroids[x.sid.values])
        out.append(f)
    res = pd.concat(out, ignore_index=True)
    res[FEATURES] = res[FEATURES].astype('float32')
    return res
