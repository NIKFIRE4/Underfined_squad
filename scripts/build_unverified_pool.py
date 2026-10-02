"""Пул вкладки «Непроверенные» из таблицы unverified_suppliers (база стенда) → таблицы unverified_pool и unverified_pool_groups.

unverified_suppliers — новые поставщики СПб и ЛО из реестра МСП (снимок 10.09.2026), которых нет в истории закупок;
загружается scripts/db_merge_ydisk.sh. Здесь для каждой компании ищем группы ОКПД2 лотов, которые она может закрыть,
по тем же правилам, что и прежний пул (data/external/scripts, README там же):
  * ОКВЭД → группа ОКПД2: выучено по победителям выгрузки (lift, число и доля побед) плюс прямое совпадение кода;
  * коды продукции из реестра МСП;
  * для компаний, которые были в прежнем пуле, — доказательства из госконтрактов Портала поставщиков и реестров
    производителей и лицензий (data/external/new_counterparties*.parquet).
Порядок внутри группы — сила доказательства, затем уровень пула (strong / signal / active), налоги и численность за 2025 год.

Из корня репозитория: python scripts/build_unverified_pool.py
База — NEW_POOL_DB или POSTGRES_* из .env (как у web-service/integrations/new_pool.py).
"""
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'data' / 'external' / 'scripts'))
sys.path.append(str(ROOT / 'web-service'))
import assemble as A  # noqa: E402
import build_new_counterparties as B  # noqa: E402
from integrations.new_pool import db_url  # noqa: E402

EXT = ROOT / 'data' / 'external'
MSP_SOURCE = 'ФНС: Единый реестр МСП'
TIER_BONUS = {'strong': 1.0, 'signal': 0.5, 'active': 0.0}
CONTRACT_SCORE, REGISTRY_SCORE = np.log(B.MIN_LIFT * 3), np.log(B.MIN_LIFT * 4)  # как в assemble.py


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def connect():
    import psycopg2
    return psycopg2.connect(db_url())


def read_unverified(conn):
    u = pd.read_sql('select * from unverified_suppliers', conn)
    codes = lambda v: v if isinstance(v, list) else json.loads(v or '[]')  # noqa: E731
    u['okved_add'] = u.okved_extra.map(lambda v: ';'.join(codes(v)))
    u['prod_codes'] = u.products.map(lambda v: ';'.join(p['okpd2'] for p in codes(v) if p.get('okpd2')))
    u['region'] = u.region_code
    return u


def old_evidence(inns):
    """Госконтракты и реестры из прежнего пула: (inn, main_group, score, via) и сведения об источниках."""
    g = pd.read_parquet(EXT / 'new_counterparties_groups.parquet', columns=['inn', 'okpd2_group', 'evidence'])
    g = g[g.inn.isin(inns)]
    parts = g.assign(via=g.evidence.fillna('').str.split('; ')).explode('via')
    parts = parts[(parts.via.str.len() > 0) & ~parts.via.str.startswith('ОКВЭД')]  # ОКВЭД — по свежему реестру
    n = pd.to_numeric(parts.via.str.extract(r'^контракты [\d.]+ \((\d+)\)$')[0], errors='coerce')
    parts['score'] = np.where(n.notna(), CONTRACT_SCORE + np.log1p(n.fillna(0)), REGISTRY_SCORE).astype('float32')
    ev = parts.rename(columns={'okpd2_group': 'main_group'})[['inn', 'main_group', 'score', 'via']]
    src = pd.read_parquet(EXT / 'new_counterparties.parquet',
                          columns=['inn', 'mos_contracts', 'mos_source', 'registry_sources', 'registry_source_date', 'retrieved_at'])
    return ev, src[src.inn.isin(inns)].set_index('inn')


def main():
    t = time.time()
    conn = connect()
    u = read_unverified(conn)
    log(f'unverified_suppliers: {len(u):,}')

    lots, sup = B.load_history()
    demand = B.group_demand(lots)
    known = A.known_inns(sup)
    winners = sup.loc[sup.is_winner, 'supplier_inn'].unique().tolist()
    reg_w = pq.read_table(EXT / 'rsmp_20250910_all.parquet', columns=['inn', 'okved_main', 'okved_add'],
                          filters=[('inn', 'in', winners)]).to_pandas()
    gmap = B.build_map(reg_w, lots, sup, demand)
    log(f'связей ОКВЭД → группа: {len(gmap):,}; групп со спросом: {len(demand)}')

    u = u[~u.inn.isin(known)]
    fit = B.company_groups(u, gmap)
    ev, src = old_evidence(set(u.inn))
    log(f'по ОКВЭД и продукции: {fit.inn.nunique():,} компаний; доказательства прежнего пула: {ev.inn.nunique():,}')

    allfit = pd.concat([ev, fit], ignore_index=True).sort_values('score', ascending=False)
    allfit = allfit.drop_duplicates(['inn', 'main_group', 'via'])
    allfit = allfit.groupby(['inn', 'main_group'], sort=False).agg(score=('score', 'first'), via=('via', '; '.join)).reset_index()
    allfit = allfit.merge(demand.rename('group_lots'), left_on='main_group', right_index=True)
    allfit['w'] = allfit.score * np.log1p(allfit.group_lots)
    top = allfit.sort_values(['inn', 'w'], ascending=[True, False]).groupby('inn').head(B.TOP_GROUPS)

    c = u[u.inn.isin(top.inn)].set_index('inn').join(src)
    c['mos_contracts'] = c.mos_contracts.fillna(0).astype('int32')
    role, role_ev, role_conf = B.role_of(c.assign(prod_codes=c.prod_codes.fillna('')))
    c['role'], c['role_evidence'], c['role_confidence'] = role, role_ev, role_conf
    rs = c.registry_sources.fillna('')
    manuf = rs.str.contains(r'ГИСП|Минпромторг|медицинских изделий \(.*производитель', regex=True)
    c.loc[manuf, 'role'] = 'производитель'
    rep = rs.str.contains('уполномоченный представитель') & ~rs.str.contains('производитель')
    c.loc[rep, 'role'] = 'дистрибьютор'
    bonus = (c.pool_tier.map(TIER_BONUS).fillna(0) + 0.3 * np.log1p(c.employees_2025.fillna(0))
             + 0.1 * np.log1p(c.taxes_paid_2025.fillna(0) / 1e5) + 0.5 * np.log1p(c.mos_contracts)
             + c.registry_sources.notna() * 1.0)

    groups = top.rename(columns={'main_group': 'okpd2_group', 'via': 'evidence'})
    groups['tier'] = groups.inn.map(c.pool_tier)
    groups['priority'] = (groups.score + groups.inn.map(bonus)).astype('float32')
    groups = groups[['inn', 'okpd2_group', 'evidence', 'tier', 'priority']]

    pool = pd.DataFrame({
        'inn': c.index, 'name': c.name_full.values, 'name_short': c.name_short.values, 'region': c.region_code.values,
        'locality': c.locality.values, 'is_msp': True, 'role': c.role.values, 'role_evidence': c.role_evidence.values,
        'role_confidence': c.role_confidence.values, 'tier': c.pool_tier.values, 'pool_reason': c.pool_reason.values,
        'employees_2025': c.employees_2025.values, 'taxes_paid_2025': c.taxes_paid_2025.values,
        'mos_contracts': c.mos_contracts.values, 'mos_source': c.mos_source.values,
        'registry_sources': c.registry_sources.values, 'registry_source_date': pd.to_datetime(c.registry_source_date).dt.date.values,
        'msp_source': MSP_SOURCE, 'msp_source_date': pd.to_datetime(c.as_of).dt.date.values,
        'okpd2_groups': top.groupby('inn').main_group.agg(';'.join).reindex(c.index).values,
        'priority': bonus.values.astype('float32'),
    })
    write(conn, pool, groups)
    log(f'unverified_pool: {len(pool):,} компаний из {len(u):,}; unverified_pool_groups: {len(groups):,} связок, '
        f'групп {groups.okpd2_group.nunique()}, медиана компаний на группу {groups.groupby("okpd2_group").size().median():,.0f}; '
        f'{time.time() - t:.0f} с')
    log(f'по уровням: {pool.tier.value_counts().to_dict()}; роли: {pool.role.value_counts().to_dict()}')


DDL = """
drop table if exists unverified_pool_groups;
drop table if exists unverified_pool;
create table unverified_pool (
  inn varchar(12) primary key, name text, name_short text, region text, locality text, is_msp boolean,
  role text, role_evidence text, role_confidence text, tier text, pool_reason text,
  employees_2025 double precision, taxes_paid_2025 double precision, mos_contracts integer, mos_source text,
  registry_sources text, registry_source_date date, msp_source text, msp_source_date date, okpd2_groups text,
  priority real);
create table unverified_pool_groups (
  inn varchar(12) not null references unverified_pool (inn), okpd2_group varchar(5) not null,
  evidence text, tier text, priority real, primary key (okpd2_group, inn));
comment on table unverified_pool is 'Вкладка «Непроверенные»: компании из unverified_suppliers с подходящими группами ОКПД2 (scripts/build_unverified_pool.py)';
comment on table unverified_pool_groups is 'Индекс поиска: компания × группа ОКПД2 с доказательством и приоритетом';
"""


def write(conn, pool, groups):
    with conn, conn.cursor() as cur:
        cur.execute(DDL)
        for name, df in (('unverified_pool', pool), ('unverified_pool_groups', groups)):
            buf = io.StringIO()
            df.to_csv(buf, index=False, header=False, na_rep='\\N')
            buf.seek(0)
            cur.copy_expert(f"copy {name} ({', '.join(df.columns)}) from stdin with (format csv, null '\\N')", buf)


if __name__ == '__main__':
    main()
