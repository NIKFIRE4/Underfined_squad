"""Ранкер v2 вне ноутбука: признаки по месяцам, варианты обучения, оценка на тесте, сохранение артефактов.

Та же схема, что в 02_ranker.ipynb (срез профилей строго до месяца лота, кандидаты, LambdaRank), но:
  * признаки v2 (recsys/features.py): свежесть, специализация, сравнение с лучшим кандидатом лота;
  * длинное окно обучения — с октября 2024, а не с апреля 2025;
  * лоты обрабатываются порциями, отрицательных примеров на лот меньше — укладывается в ~2–3 ГБ памяти.

Из корня репозитория:
  python scripts/train_ranker.py gen                    # признаки всех месяцев → data/features_v2/mNN/part*.parquet
  python scripts/train_ranker.py train v2_long --features v2 --train 9-19
  python scripts/train_ranker.py save v2_long --refit   # → models_v2 (основная модель в models/ не трогается)
Результаты вариантов — data/features_v2/results.json.
"""
import argparse
import gc
import json
import shutil
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recsys import candidates as C  # noqa: E402
from recsys.data import Dataset, month_label  # noqa: E402
from recsys.features import FEATURE_SPEC, FEATURES, FEATURES_V1, GROUP, MONOTONE, TITLE, build_features  # noqa: E402
from recsys.inference import softmax_by_lot  # noqa: E402
from recsys.metrics import evaluate  # noqa: E402
from recsys.profiles import Snapshot  # noqa: E402

FEAT_DIR = ROOT / 'data' / 'features_v2'
RESULTS = FEAT_DIR / 'results.json'
VALID_MONTH, TEST_MONTHS = 20, [21, 22, 23]
# сначала то, что нужно для сравнения на старом окне, затем более ранние месяцы для длинного окна
GEN_ORDER = [20, 21, 22, 23, 15, 16, 17, 18, 19, 14, 13, 12, 11, 10, 9]
HARD_NEG, RAND_NEG, VALID_LOTS, CHUNK_LOTS, SEED = 20, 10, 5_000, 5_000, 42
TAG = ''  # суффикс папок обучающих месяцев: '' — 20+10 отрицательных, '_full' — 60+30, как в ноутбуке
RANK_COLS = ['rank_code', 'rank_cust', 'rank_text', 'cand_rank']
PARAMS = dict(objective='lambdarank', metric='ndcg', eval_at=[10, 1, 5], label_gain=[0, 1, 3],
              lambdarank_truncation_level=30, learning_rate=0.05, num_leaves=63, min_data_in_leaf=200,
              feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
              monotone_constraints_method='intermediate', num_threads=8, seed=SEED, verbosity=-1)


def log(msg):
    print(f'{time.strftime("%H:%M:%S")} {msg}', flush=True)


def months_arg(s):
    a, b = s.split('-')
    return list(range(int(a), int(b) + 1))


def dataset():
    t = time.time()
    ds = Dataset(ROOT / 'data' / 'processed')
    vecs = np.load(ROOT / 'data' / 'features' / 'lot_vecs.npy', mmap_mode='r')
    log(f'данные: {len(ds.lots):,} лотов, {time.time() - t:.0f} с')
    return ds, vecs


def sample_train(F, hard=None, rand=None):
    hard, rand = hard or HARD_NEG, rand or RAND_NEG
    F = F[F.lot_id.isin(F.loc[F.label > 0, 'lot_id'].unique())]
    neg = F[F.label == 0].copy()
    neg['nr'] = neg.groupby('lot_id').cumcount()
    rnd = neg[neg.nr >= hard].sample(frac=1, random_state=SEED).groupby('lot_id').head(rand)
    return pd.concat([F[F.label > 0], neg[neg.nr < hard], rnd]).drop(columns='nr')


def gen(args):
    ds, vecs = dataset()
    for M in args.months or GEN_ORDER:
        if TAG and M >= VALID_MONTH:
            continue  # valid и test хранятся целиком, от выборки отрицательных не зависят
        out = mdir(M)
        if (out / 'stats.json').exists():
            log(f'{month_label(M)}: уже есть')
            continue
        out.mkdir(parents=True, exist_ok=True)
        t = time.time()
        snap = Snapshot.build(ds, M, vecs)
        lots = ds.lots[(ds.lots.month == M) & ds.lots.train_eligible].lot_id.values
        train = M < VALID_MONTH
        found = total = rows = 0
        for i in range(0, len(lots), CHUNK_LOTS):
            part = lots[i:i + CHUNK_LOTS]
            b = ds.batch(part, vecs)
            cands = C.generate(snap, b)
            F = build_features(snap, b, cands).merge(cands[['lot_id', 'sid', *RANK_COLS]], on=['lot_id', 'sid'], how='left')
            truth = ds.labels(part)
            F = F.merge(truth, on=['lot_id', 'sid'], how='left')
            F['label'] = F.label.fillna(0).astype('int8')
            win_lots = truth.loc[truth.label == 2, 'lot_id'].unique()
            found += F.loc[F.label == 2, 'lot_id'].nunique()
            total += len(win_lots)
            if train:
                F = sample_train(F)
            F[RANK_COLS] = F[RANK_COLS].astype('float32')
            F.to_parquet(out / f'part{i // CHUNK_LOTS:03d}.parquet', compression='zstd')
            rows += len(F)
            del F, cands, b
            gc.collect()
        st = {'month': month_label(M), 'lots_with_winner': int(total), 'coverage': found / max(total, 1), 'rows': int(rows)}
        (out / 'stats.json').write_text(json.dumps(st))
        log(f'{month_label(M)}: {rows:,} строк, победитель среди кандидатов {st["coverage"]:.1%}, {time.time() - t:.0f} с')
        del snap
        gc.collect()


def mdir(M):
    return FEAT_DIR / (f'm{M}{TAG}' if M < VALID_MONTH else f'm{M}')


def parts_of(M):
    parts = sorted(mdir(M).glob('part*.parquet'))
    if not parts:
        raise SystemExit(f'нет признаков за {month_label(M)}: сначала gen')
    return parts


def read(M, cols, lots=None):
    flt = None if lots is None else [('lot_id', 'in', [int(x) for x in lots])]
    return pd.concat([pd.read_parquet(p, columns=cols, filters=flt) for p in parts_of(M)], ignore_index=True)


def to_lgb(F, feats, ref=None):
    F = F.sort_values(['lot_id', 'cand_rank'])
    group = F.groupby('lot_id', sort=False).size().values
    return lgb.Dataset(F[feats].to_numpy(np.float32), label=F.label.values, group=group, feature_name=feats,
                       reference=ref, free_raw_data=True).construct()


def segments(ds, truth):
    """Сегмент лота по истории победителя до месяца лота (как в ноутбуке, раздел 4.1)."""
    lots = ds.lots.set_index('lot_id')
    w = truth[truth.label == 2].drop_duplicates('lot_id')[['lot_id', 'sid']]
    w = w.join(lots[['month', 'cid', 'mc']], on='lot_id')
    ev = ds.events
    wins = ev[ev.win == 1]
    fw_cust = wins.groupby(['sid', 'cid']).month.min().rename('fw_cust')
    fw_cls = wins.groupby(['sid', 'mc']).month.min().rename('fw_cls')
    fp = ev.groupby('sid').month.min().rename('fp')
    w = w.join(fw_cust, on=['sid', 'cid']).join(fw_cls, on=['sid', 'mc']).join(fp, on='sid')
    seg = np.select([w.fw_cust < w.month, w.fw_cls < w.month, w.fp < w.month],
                    ['1. побеждал у заказчика', '2. новый для заказчика', '3. участвовал, без побед в классе'],
                    '4. нет истории')
    return pd.Series(seg, index=w.lot_id)


def evaluate_test(ds, booster, feats, best, temp, coverage):
    scored, truths = [], []
    for M in TEST_MONTHS:
        for p in parts_of(M):  # по частям: месяц теста целиком — миллионы пар
            F = pd.read_parquet(p, columns=['lot_id', 'sid', *feats])
            F['score'] = booster.predict(F[feats].to_numpy(np.float32), num_iteration=best)
            scored.append(F[['lot_id', 'sid', 'score']])
            del F
        truths.append(ds.labels(ds.lots[(ds.lots.month == M) & ds.lots.train_eligible].lot_id.values))
        gc.collect()
    scored, truth = pd.concat(scored, ignore_index=True), pd.concat(truths, ignore_index=True)
    res = {'Все': evaluate(scored, truth, 'score')}
    seg = segments(ds, truth)
    for name in sorted(seg.unique()):
        lots = seg.index[seg == name]
        res[name] = evaluate(scored[scored.lot_id.isin(lots)], truth[truth.lot_id.isin(lots)], 'score')
    return res


def fit(feats, train_months, valid_lots, rounds=None):
    """Матрица собирается по месяцам в заранее выделенный float32-массив: без копии всего train в памяти."""
    cols = ['lot_id', 'label', 'cand_rank', *feats]
    months = []
    for M in train_months:
        F = read(M, cols)
        if M >= VALID_MONTH:  # при refit valid и test хранятся целиком — сэмплируем как train
            F = sample_train(F, *((60, 30) if TAG == '_full' else (None, None)))
        months.append((M, len(F)) if M < VALID_MONTH else (M, F))
        if M < VALID_MONTH:
            del F
    n = sum(x if isinstance(x, int) else len(x) for _, x in months)
    X = np.empty((n, len(feats)), dtype=np.float32)
    y, groups, lots, at = np.empty(n, dtype=np.int8), [], 0, 0
    for M, F in months:
        if isinstance(F, int):
            F = read(M, cols)
        F = F.sort_values(['lot_id', 'cand_rank'])
        k = len(F)
        X[at:at + k] = F[feats].to_numpy(np.float32)
        y[at:at + k] = F.label.to_numpy()
        g = F.groupby('lot_id', sort=False).size().values
        groups.append(g)
        lots += len(g)
        at += k
        del F
        gc.collect()
    log(f'train: {lots:,} лотов, {n:,} строк, {len(feats)} признаков')
    dtrain = lgb.Dataset(X, label=y, group=np.concatenate(groups), feature_name=feats, free_raw_data=True).construct()
    del X, y, months
    gc.collect()
    params = {**PARAMS, 'monotone_constraints': [MONOTONE[FEATURES.index(f)] for f in feats]}
    evals = {}
    if rounds:
        booster = lgb.train(params, dtrain, num_boost_round=rounds)
        return booster, rounds, evals
    dvalid = to_lgb(read(VALID_MONTH, cols, valid_lots), feats, ref=dtrain)
    booster = lgb.train(params, dtrain, num_boost_round=3000, valid_sets=[dvalid], valid_names=['valid'],
                        callbacks=[lgb.early_stopping(100, first_metric_only=True, verbose=False),
                                   lgb.record_evaluation(evals), lgb.log_evaluation(100)])
    return booster, booster.best_iteration, evals


def calibrate(booster, feats, best):
    L = read(VALID_MONTH, ['lot_id', 'label'])
    V = read(VALID_MONTH, ['lot_id', 'label', *feats], L.loc[L.label == 2, 'lot_id'].unique())
    s = booster.predict(V[feats].to_numpy(np.float32), num_iteration=best)

    def nll(T):
        p = softmax_by_lot(V.lot_id.values, s, T)
        return -np.log(np.clip(p[V.label.values == 2], 1e-12, None)).mean()

    grid = np.exp(np.linspace(np.log(0.1), np.log(10), 61))
    return float(grid[int(np.argmin([nll(T) for T in grid]))])


def valid_lots():
    V = read(VALID_MONTH, ['lot_id', 'label'])
    pos = V.loc[V.label > 0, 'lot_id'].unique()
    return np.random.default_rng(SEED).choice(pos, size=min(VALID_LOTS, len(pos)), replace=False)


def train(args):
    feats = FEATURES if args.features == 'v2' else FEATURES_V1
    t = time.time()
    booster, best, evals = fit(feats, months_arg(args.train), valid_lots())
    log(f'{args.name}: лучшая итерация {best}, NDCG@10 valid {evals["valid"]["ndcg@10"][best - 1]:.4f}, {time.time() - t:.0f} с')
    temp = calibrate(booster, feats, best)
    gc.collect()
    ds, _ = dataset()  # история нужна только для оценки: метки всех лотов теста и сегменты
    coverage = json.loads((FEAT_DIR / f'm{VALID_MONTH}' / 'stats.json').read_text())['coverage']
    res = evaluate_test(ds, booster, feats, best, temp, coverage)
    for seg, m in res.items():
        log(f'  {seg:36s} R@1 {m["Recall@1"]:.3f}  R@5 {m["Recall@5"]:.3f}  R@10 {m["Recall@10"]:.3f}  '
            f'MRR {m["MRR"]:.3f}  NDCG@10 {m["NDCG@10"]:.3f}  лотов {m["лотов"]:,}')
    all_res = json.loads(RESULTS.read_text(encoding='utf-8')) if RESULTS.exists() else {}
    all_res[args.name] = {'features': args.features, 'train': args.train, 'tag': TAG, 'best_iteration': best,
                          'temperature': temp, 'coverage': coverage, 'test': res}
    RESULTS.write_text(json.dumps(all_res, ensure_ascii=False, indent=1), encoding='utf-8')
    booster.save_model(str(FEAT_DIR / f'{args.name}.txt'), num_iteration=best)


def save(args):
    """Артефакты в models_v2: модель (или refit на всех месяцах по декабрь 2025), meta, срезы профилей."""
    ds, vecs = dataset()
    global TAG
    r = json.loads(RESULTS.read_text(encoding='utf-8'))[args.name]
    TAG = r.get('tag', '')
    feats = FEATURES if r['features'] == 'v2' else FEATURES_V1
    out = ROOT / 'models_v2'
    out.mkdir(exist_ok=True)
    best = r['best_iteration']
    if args.refit:
        # метрики остаются с честного разбиения; финальная модель видит все месяцы, число итераций зафиксировано
        months = months_arg(r['train']) + [VALID_MONTH, *TEST_MONTHS]
        booster, _, _ = fit(feats, months, None, rounds=best)
        booster.save_model(str(out / 'ranker.txt'))
    else:
        shutil.copy(FEAT_DIR / f'{args.name}.txt', out / 'ranker.txt')
    old = json.loads((ROOT / 'models' / 'meta.json').read_text(encoding='utf-8'))
    m0 = int(months_arg(r['train'])[0])
    meta = {
        'features': feats, 'titles': TITLE, 'groups': GROUP, 'monotone': [MONOTONE[FEATURES.index(f)] for f in feats],
        'params': PARAMS, 'best_iteration': best, 'temperature': r['temperature'], 'candidate_coverage': r['coverage'],
        'price_cap': old['price_cap'], 'train_months': [month_label(m) for m in months_arg(r['train'])],
        'valid_month': month_label(VALID_MONTH), 'test_months': [month_label(m) for m in TEST_MONTHS],
        'refit_on_all_months': bool(args.refit), 'test_metrics': r['test'], 'candidates': old['candidates'],
        'variant': args.name, 'train_from': month_label(m0),
    }
    (out / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    for name in ('text_model.joblib', 'vocab.npz', 'suppliers.parquet'):
        shutil.copy(ROOT / 'models' / name, out / name)
    for cutoff, folder in [(int(ds.lots.month.max()) + 1, 'snapshot'), (VALID_MONTH + 1, 'snapshot_2025-10')]:
        Snapshot.build(ds, cutoff, vecs).save(out / folder)
        log(f'срез {folder}: cutoff {month_label(cutoff)}')
    log(f'готово: {out}')


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('gen')
    g.add_argument('--months', type=lambda s: [int(x) for x in s.split(',')])
    t = sub.add_parser('train')
    t.add_argument('name')
    t.add_argument('--features', choices=['v1', 'v2'], default='v2')
    t.add_argument('--train', default='15-19', help='месяцы обучения, 0 = 2024-01; 15-19 — апрель–август 2025')
    s = sub.add_parser('save')
    s.add_argument('name')
    s.add_argument('--refit', action='store_true')
    p.add_argument('--full', action='store_true', help='обучающие месяцы с 60+30 отрицательными (папки mNN_full)')
    a = p.parse_args()
    global TAG, HARD_NEG, RAND_NEG
    if a.full:
        TAG, HARD_NEG, RAND_NEG = '_full', 60, 30
    FEAT_DIR.mkdir(parents=True, exist_ok=True)
    {'gen': gen, 'train': train, 'save': save}[a.cmd](a)


if __name__ == '__main__':
    main()
