"""Модель подбора контрагентов: пакет recsys в корне репозитория. Контракт — INTEGRATION.md.

recommend(lot, top_k)        — для обработки CSV (pipeline.py)
recommend_detailed(payload)  — для POST /api/recommendations (одна закупка из формы)
warmup()                     — загрузка модели в фоне при старте сервера
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # корень репозитория

from recsys.webservice import READY, recommend, recommend_detailed, warmup  # noqa: E402,F401
