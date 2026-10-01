"""Признаки из истории закупок (выгрузки организаторов) для роли «дистрибьютор» (ТЗ 6.4):
«широкий набор разных ОКПД2 в истории; поставки нескольким типам заказчиков».

По каждому ИНН поставщика: участия и победы, число разных кодов ОКПД2 (всего и по классам),
число разных заказчиков, последняя активность. Источник факта — `history`.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from .inn import is_valid_inn
from .models import SourceResult

log = logging.getLogger("enrichment")

SUPPLIERS = "dataset/Поставщики_24-25.csv"
ITEMS = "dataset/ТРУ_24-25.csv"
NOTICES = "dataset/Извещения_24-25.csv"


def _read(path: str, cols: list[str]) -> pl.LazyFrame:
    # все колонки строками: ИНН и коды с ведущими нулями не должны превратиться в числа
    return pl.scan_csv(path, separator=";", infer_schema=False, quote_char='"',
                       truncate_ragged_lines=True).select(cols)


def compute(suppliers: str = SUPPLIERS, items: str = ITEMS, notices: str = NOTICES) -> pl.DataFrame:
    sup = _read(suppliers, ["lot_id", "supplier_inn", "is_winner"]).with_columns(
        pl.col("supplier_inn").str.strip_chars(), (pl.col("is_winner") == "true").alias("win"))
    trus = _read(items, ["lot_id", "okpd2_code"]).with_columns(
        pl.col("okpd2_code").str.strip_chars().alias("code")).filter(pl.col("code").str.len_chars() > 0)
    trus = trus.select("lot_id", "code", pl.col("code").str.slice(0, 2).alias("cls")).unique()
    notes = _read(notices, ["lot_id", "customer_inn", "publish_date"])

    lots = sup.join(notes, on="lot_id", how="left")
    base = lots.group_by("supplier_inn").agg(
        pl.col("lot_id").n_unique().alias("hist_lots"),
        pl.col("win").sum().alias("hist_wins"),
        pl.col("customer_inn").n_unique().alias("hist_customers"),
        pl.col("publish_date").max().alias("hist_last_date"),
    )
    codes = sup.select("supplier_inn", "lot_id").unique().join(trus, on="lot_id")
    per_class = (codes.group_by("supplier_inn", "cls").agg(pl.col("code").n_unique().alias("n"))
                 .group_by("supplier_inn").agg(pl.struct("cls", "n").alias("hist_class_codes")))
    totals = codes.group_by("supplier_inn").agg(
        pl.col("code").n_unique().alias("hist_okpd2_codes"), pl.col("cls").n_unique().alias("hist_okpd2_classes"))
    return (base.join(totals, on="supplier_inn", how="left").join(per_class, on="supplier_inn", how="left")
            .collect())


def load(targets: set[str] | None = None, suppliers: str = SUPPLIERS) -> list[SourceResult]:
    fetched_at = datetime.fromtimestamp(Path(suppliers).stat().st_mtime, timezone.utc)
    df = compute(suppliers)
    out = []
    for r in df.iter_rows(named=True):
        inn = r["supplier_inn"]
        if not inn or not is_valid_inn(inn) or (targets is not None and inn not in targets):
            continue
        res = SourceResult("history", inn)
        for k in ("hist_lots", "hist_wins", "hist_customers", "hist_okpd2_codes", "hist_okpd2_classes"):
            res.add(k, int(r[k] or 0), fetched_at)
        res.add("hist_last_date", r["hist_last_date"], fetched_at)
        classes = sorted(r["hist_class_codes"] or [], key=lambda x: -x["n"])
        res.add("hist_class_codes", {c["cls"]: c["n"] for c in classes}, fetched_at)
        out.append(res)
    log.info("история: %d поставщиков", len(out))
    return out
