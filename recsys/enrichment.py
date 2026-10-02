"""Признаки поставщика из обогащения (база стенда: companies + company_facts) для ранкера.

Правило — никакого будущего относительно лота:
  * финансы (ГИР БО) — за год, предшествующий году лота: лоты 2025 → отчётность 2024, лоты 2026 → 2025;
    отчётность раньше 2024 года не используется (на 2026 год она устарела) — для лотов 2024 года финансов нет;
  * возраст компании и МСП — на месяц лота (дата регистрации, дата включения в реестр МСП);
  * ОКВЭД и реестры — справочные, меняются редко.
Не используются: история закупок из обогащения (hist_*), роль, контакты — посчитаны по тем же победам, что и метки;
РНП, статус и ликвидация — сведения из будущего относительно обучающих лотов.
Численность и налоги есть только за 2025 год: для лотов 2025 года это тот же год, поэтому они отделены
(ENRICH_SAME_YEAR) и включаются в модель, только если не дают утечки (03_enrichment_features.ipynb).

Таблица поставщиков: build_supplier_table(facts, companies) → parquet; признаки пар: pair_features(E, pairs).
"""
import json

import numpy as np
import pandas as pd

from .data import BASE_YEAR

MIN_FIN_YEAR = 2024

# (имя, группа, монотонность, описание) — в формате FEATURE_SPEC
ENRICH_SPEC = [
    ('e_age_years', 'компания', 0, 'Возраст компании на дату лота'),
    ('e_msp_cat', 'компания', 0, 'Категория МСП на дату лота'),
    ('e_rev_log', 'компания', 0, 'Выручка за год до лота'),
    ('e_margin', 'компания', 0, 'Рентабельность по чистой прибыли за год до лота'),
    ('e_assets_log', 'компания', 0, 'Активы за год до лота'),
    ('e_neg_equity', 'компания', 0, 'Отрицательный капитал за год до лота'),
    ('e_rev_to_price', 'компания', 0, 'Выручка за год до лота относительно НМЦК'),
    ('e_okved_main', 'компания', 0, 'Основной ОКВЭД совпадает с группой ОКПД2 лота'),
    ('e_okved_any', 'компания', 0, 'Группа ОКПД2 лота среди его ОКВЭД (основной или дополнительные)'),
    ('e_okved_class', 'компания', 0, 'Класс ОКПД2 лота среди классов его ОКВЭД'),
    ('e_n_okved', 'компания', 0, 'Число видов деятельности по ОКВЭД'),
    ('e_gisp', 'компания', 0, 'Продукция в реестре промпродукции (ГИСП)'),
    ('e_software', 'компания', 0, 'Продукция в реестре российского ПО'),
    ('e_licenses', 'компания', 0, 'Число лицензий в реестре МСП'),
]
ENRICH_SAME_YEAR = [
    ('e_employees_log', 'компания', 0, 'Численность сотрудников за 2025 год, log(1+чел.)'),
    ('e_taxes_log', 'компания', 0, 'Уплаченные налоги за 2025 год, log(1+₽)'),
]
ENRICH_FEATURES = [f[0] for f in ENRICH_SPEC]
ENRICH_SAME_YEAR_FEATURES = [f[0] for f in ENRICH_SAME_YEAR]

SOURCE_PRIORITY = ['rmsp', 'fns_rsmp', 'bo', 'pb', 'fns_sshr2019', 'fns_paytax', 'reg_gisp', 'reg_software', 'egrul']


def _month(iso):
    """'2025-03-17' → номер месяца от января BASE_YEAR (как month в recsys.data)."""
    d = pd.to_datetime(iso, errors='coerce')
    return (d.dt.year - BASE_YEAR) * 12 + d.dt.month - 1


def _ogrn_year(ogrn):
    """Год регистрации из ОГРН/ОГРНИП: 2-3 знаки — две последние цифры года."""
    s = ogrn.fillna('').astype(str)
    yy = pd.to_numeric(s.str[1:3], errors='coerce')
    return np.where(s.str.len() >= 13, np.where(yy > 50, 1900 + yy, 2000 + yy), np.nan)


def build_supplier_table(facts: pd.DataFrame, companies: pd.DataFrame) -> pd.DataFrame:
    """facts: inn, field, source, value (JSON-текст); companies: inn, kind. Одна строка на ИНН."""
    f = facts.copy()
    f['rank'] = f.source.map({s: i for i, s in enumerate(SOURCE_PRIORITY)}).fillna(len(SOURCE_PRIORITY))
    f = f.sort_values('rank').drop_duplicates(['inn', 'field'])
    w = f.pivot(index='inn', columns='field', values='value')
    val = lambda col: w[col].map(lambda v: json.loads(v) if isinstance(v, str) else None) if col in w else pd.Series(None, index=w.index)  # noqa: E731

    E = pd.DataFrame(index=w.index)
    reg = _month(val('reg_date'))
    ogrn_m = (pd.Series(_ogrn_year(val('ogrn')), index=w.index) - BASE_YEAR) * 12 + 6  # середина года из ОГРН
    E['reg_month'] = reg.fillna(ogrn_m)
    E['smp_since_month'] = _month(val('smp_since'))
    E['msp_cat'] = pd.to_numeric(val('smp_category'), errors='coerce')
    E['in_msp_snapshot'] = val('smp_as_of').notna().astype('int8')

    fin = val('finance_by_year')
    for year in (2024, 2025):
        y = fin.map(lambda d: (d or {}).get(str(year)) or {})
        for k in ('revenue', 'net_profit', 'assets', 'equity'):
            E[f'{k}_{year}'] = pd.to_numeric(y.map(lambda d: d.get(k)), errors='coerce')

    main = val('okved_main').map(lambda c: c[:5] if isinstance(c, str) else None)
    extra = val('okved_extra').map(lambda v: [c[:5] for c in v] if isinstance(v, list) else [])
    E['okved_main'] = main
    E['okved_groups'] = [sorted({c for c in ([m] if m else []) + x if c}) for m, x in zip(main, extra)]
    E['okved_classes'] = E.okved_groups.map(lambda g: sorted({c[:2] for c in g}))
    E['n_okved'] = E.okved_groups.map(len).where(main.notna())
    E['in_gisp'] = val('in_gisp').map(lambda v: float(v) if v is not None else np.nan)
    E['in_software'] = val('in_software_registry').map(lambda v: float(v) if v is not None else np.nan)
    E['licenses'] = pd.to_numeric(val('licenses_count'), errors='coerce')
    E['employees_2025'] = pd.to_numeric(val('employees'), errors='coerce').fillna(pd.to_numeric(val('employees_rmsp'), errors='coerce'))
    E['taxes_2025'] = pd.to_numeric(val('taxes_paid'), errors='coerce')
    E = E.join(companies.set_index('inn')[['kind']], how='left')
    return E.reset_index().rename(columns={'index': 'inn'})


def pair_features(E: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    """pairs: sid, month (месяц лота), main_group (XX.XX), price_log. E — таблица поставщиков с колонкой sid.
    Возвращает признаки ENRICH_FEATURES + ENRICH_SAME_YEAR_FEATURES в порядке строк pairs."""
    x = pairs[['sid', 'month', 'main_group', 'price_log']].merge(E, on='sid', how='left')
    out = pd.DataFrame(index=pairs.index)
    v = lambda a: np.asarray(a, dtype=np.float32)  # noqa: E731
    age = (x.month - x.reg_month) / 12
    out['e_age_years'] = v(age.where(age >= 0))
    known_msp = x.in_msp_snapshot == 1
    out['e_msp_cat'] = v(np.where(known_msp & (x.smp_since_month <= x.month), x.msp_cat, np.nan))

    fin_year = BASE_YEAR + x.month // 12 - 1  # год отчётности: предыдущий к году лота
    ok = fin_year >= MIN_FIN_YEAR
    pick = lambda k: np.where(ok & (fin_year == 2024), x[f'{k}_2024'], np.where(ok & (fin_year == 2025), x[f'{k}_2025'], np.nan))  # noqa: E731
    rev, prof, assets, equity = pick('revenue'), pick('net_profit'), pick('assets'), pick('equity')
    out['e_rev_log'] = v(np.log1p(np.clip(rev, 0, None)))
    out['e_margin'] = v(np.clip(np.where(rev > 0, prof / np.where(rev > 0, rev, 1), np.nan), -1, 1))
    out['e_assets_log'] = v(np.log1p(np.clip(assets, 0, None)))
    out['e_neg_equity'] = v(np.where(np.isnan(equity), np.nan, (equity < 0).astype(float)))
    out['e_rev_to_price'] = v(out.e_rev_log.values - x.price_log.values)

    has_okved = x.okved_main.notna().values & (x.main_group.fillna('') != '').values
    out['e_okved_main'] = v(np.where(has_okved, (x.okved_main == x.main_group).astype(float), np.nan))
    sg = E[['sid', 'okved_groups']].explode('okved_groups').dropna().rename(columns={'okved_groups': 'main_group'})
    hit = x[['sid', 'main_group']].merge(sg.assign(hit=1.0).drop_duplicates(['sid', 'main_group']), on=['sid', 'main_group'], how='left').hit
    out['e_okved_any'] = v(np.where(has_okved, hit.fillna(0).values, np.nan))
    sc = E[['sid', 'okved_classes']].explode('okved_classes').dropna().rename(columns={'okved_classes': 'cls'})
    hit = (x[['sid']].assign(cls=x.main_group.str[:2]).merge(sc.assign(hit=1.0).drop_duplicates(['sid', 'cls']), on=['sid', 'cls'], how='left').hit)
    out['e_okved_class'] = v(np.where(has_okved, hit.fillna(0).values, np.nan))
    out['e_n_okved'] = v(x.n_okved)
    out['e_gisp'] = v(x.in_gisp)
    out['e_software'] = v(x.in_software)
    out['e_licenses'] = v(x.licenses)
    out['e_employees_log'] = v(np.log1p(x.employees_2025))
    out['e_taxes_log'] = v(np.log1p(np.clip(x.taxes_2025, 0, None)))
    return out
