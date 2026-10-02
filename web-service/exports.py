"""Выгрузка полного списка поставщиков выбранного лота из сохранённого результата."""
import csv
import io
import json
import re
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape

from pipeline import safe_cell


def lot_rows(folder, lot_id):
    # CSV содержит весь результат, в отличие от ограниченного preview в job.json.
    with (folder / "suppliers.csv").open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter=";")
        fields = reader.fieldnames
        rows = [row for row in reader if row["lot_id"] == safe_cell(lot_id)]
    if not rows:
        lots = folder / "lots.jsonl"
        found = False
        if lots.exists():
            with lots.open(encoding="utf-8") as source:
                found = any(json.loads(line)["lot_id"] == lot_id for line in source)
        if not found:
            raise LookupError("Лот не найден")
    return fields, rows


def xlsx_bytes(fields, rows):
    """Минимальная OOXML-книга: текстовые ячейки сохраняют ИНН, КПП и ведущие нули."""
    def cell(value):
        text = str(value or "")
        if len(text) > 32767:
            raise ValueError("Значение превышает лимит ячейки Excel. Скачайте этот лот в CSV.")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", "", text)
        return '<c t="inlineStr"><is><t xml:space="preserve">' + escape(text) + '</t></is></c>'

    table = [fields, *[[row.get(key, "") for key in fields] for row in rows]]
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
             '<sheetData>' + ''.join('<row>' + ''.join(map(cell, row)) + '</row>' for row in table)
             + '</sheetData></worksheet>')
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Поставщики" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


def export_lot(folder, lot_id, file_format):
    fields, rows = lot_rows(folder, lot_id)
    if file_format == "xlsx":
        return xlsx_bytes(fields, rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, delimiter=";")
    writer.writeheader()
    writer.writerows(rows)  # safe_cell уже применён при записи исходного результата.
    return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
