"""Тестовый набор для сервиса из выгрузки организаторов.

Берёт лоты тестового периода модели (октябрь–декабря 2025, при обучении не использовались):
с кодом ОКПД2, несколькими участниками и известным победителем; по одному-два лота на отрасль.

    python scripts/make_test_set.py [--lots 24] [--out web-service/examples/test-set]

Результат:
  Извещения.csv, ТРУ.csv — входные файлы для загрузки в сервис;
  Закупки.xlsx          — те же две таблицы на двух листах одной книги;
  Ответы.csv            — реальные участники и победители, чтобы сверить выдачу модели.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web-service" / "tests"))


def write_xlsx(path, sheets):
    """Книга .xlsx без сторонних библиотек: строки как inline-строки, чтобы сохранить ведущие нули."""
    import zipfile
    from xml.sax.saxutils import escape

    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    def sheet_xml(df):
        rows = [list(df.columns)] + df.astype(str).values.tolist()
        body = "".join(
            f'<row r="{ri}">' + "".join(f'<c r="{col(ci)}{ri}" t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>' for ci, v in enumerate(r)) + "</row>"
            for ri, r in enumerate(rows, 1))
        return f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>'

    names = list(sheets)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(names) + 1)) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1)) + "</Relationships>")
        for i, n in enumerate(names, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml(sheets[n]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=24)
    ap.add_argument("--out", default=str(ROOT / "web-service" / "examples" / "test-set"))
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    read = lambda name: pd.read_csv(ROOT / name, sep=";", dtype=str, keep_default_na=False)
    notices, tru, sup = read("Извещения_24-25.csv"), read("ТРУ_24-25.csv"), read("Поставщики_24-25.csv")

    period = notices[(notices.publish_date >= "2025-10-01") & (notices.publish_date <= "2025-12-31")]
    stats = sup.groupby("lot_id").agg(n=("supplier_inn", "size"), wins=("is_winner", lambda s: (s == "true").sum()))
    good = stats[(stats.n >= 3) & (stats.wins == 1)].index
    tru_ok = tru[tru.okpd2_code.str.len() >= 4]
    lots = period[period.lot_id.isin(good) & period.lot_id.isin(tru_ok.lot_id)].copy()
    lots = lots[lots.subject.str.len().between(20, 160)]
    first_code = tru_ok.drop_duplicates("lot_id").set_index("lot_id").okpd2_code.str[:2]
    lots["sector"] = lots.lot_id.map(first_code)
    # разнообразие: по одному лоту на отрасль ОКПД2, затем добор случайными
    picked = lots.sample(frac=1, random_state=args.seed).drop_duplicates("sector").head(args.lots)
    if len(picked) < args.lots:
        rest = lots[~lots.lot_id.isin(picked.lot_id)].sample(frac=1, random_state=args.seed)
        picked = pd.concat([picked, rest.head(args.lots - len(picked))])
    picked = picked.sort_values("publish_date").drop(columns="sector")

    items = tru[tru.lot_id.isin(picked.lot_id)]
    answers = sup[sup.lot_id.isin(picked.lot_id)].merge(picked[["lot_id", "subject"]], on="lot_id")
    answers = answers.sort_values(["lot_id", "is_winner"], ascending=[True, False])[["lot_id", "subject", "supplier_inn", "supplier_kpp", "is_winner"]]

    picked.to_csv(out / "Извещения.csv", sep=";", index=False, encoding="utf-8-sig")
    items.to_csv(out / "ТРУ.csv", sep=";", index=False, encoding="utf-8-sig")
    answers.to_csv(out / "Ответы.csv", sep=";", index=False, encoding="utf-8-sig")
    write_xlsx(out / "Закупки.xlsx", {"Извещения": picked, "ТРУ": items})
    print(f"{len(picked)} лотов, {len(items)} позиций ТРУ, {len(answers)} участников → {out}")


if __name__ == "__main__":
    main()
