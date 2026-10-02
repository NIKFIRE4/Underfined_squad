"""Шкала «Соответствие» 0–100 для модели ранжирования → <model_dir>/fit_calibration.json.

Шанс победы (softmax по лоту) делится между всеми ~300 кандидатами, поэтому у сильной компании в лоте с сильным
лидером он выходит «меньше 1%». Соответствие — абсолютная мера: процентиль оценки модели среди оценок реальных
победителей месяца валидации. 70 значит «оценка выше, чем у 70% настоящих победителей». На тесте (октябрь 2025):
у №1 лота медиана 69, а доля побед растёт с баллом — 0,7% при ≤10, 8% при 50–75, 43% при 90+.

Из корня репозитория (нужны признаки месяца валидации data/features/m{N}.parquet из 02_ranker.ipynb):
  python scripts/calibrate_fit.py [--model-dir models] [--month 20]
"""
import argparse
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from recsys.data import month_label  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=str(ROOT / "models"))
    ap.add_argument("--month", type=int, default=None, help="месяц валидации; по умолчанию valid_month из meta.json")
    ap.add_argument("--features", default=str(ROOT / "data" / "features"))
    args = ap.parse_args()
    d = Path(args.model_dir)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    month = args.month
    if month is None:
        y, m = map(int, meta["valid_month"].split("-"))
        month = (y - 2024) * 12 + m - 1
    feats = meta["features"]
    booster = lgb.Booster(model_str=(d / "ranker.txt").read_text(encoding="utf-8").replace("\r\n", "\n"))
    F = pd.read_parquet(Path(args.features) / f"m{month}.parquet", columns=["label"] + feats)
    wins = F[F.label == 2]
    scores = booster.predict(wins[feats], num_iteration=meta["best_iteration"])
    q = np.quantile(scores, np.linspace(0, 1, 101))
    out = {"winner_score_quantiles": [round(float(v), 5) for v in q],
           "month": month_label(month), "winners": int(len(wins))}
    (d / "fit_calibration.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{d / 'fit_calibration.json'}: {len(wins)} победителей {month_label(month)}, медиана оценки {q[50]:.2f}")


if __name__ == "__main__":
    main()
