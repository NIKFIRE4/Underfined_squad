"""Входные файлы любого вида → notices.csv + items.csv для pipeline.

Принимаются CSV и Excel (.xlsx). Тип определяется по столбцам, а не по имени файла:
в книге Excel может быть один лист или сразу два («Извещения» и «ТРУ»).
Только стандартная библиотека: .xlsx читается потоково через zipfile + iterparse.
"""
import codecs
import csv
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree.ElementTree import iterparse

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
KIND_LABEL = {"notices": "Извещения", "items": "ТРУ"}


def classify(headers):
    """Тип таблицы по заголовкам: 'notices', 'items' или None. Та же логика, что в web/file-selection.js."""
    has = {h.strip().lower() for h in headers}.__contains__
    notices = has("lot_id") and (has("subject") or has("procedure_name"))
    items = has("lot_id") and has("product_name") and has("okpd2_code")
    if notices and items:
        raise ValueError("в одной таблице найдены столбцы и извещений, и ТРУ — разнесите их по разным файлам или листам")
    return "notices" if notices else "items" if items else None


def csv_headers(path: Path):
    with path.open("rb") as f:
        sample = f.read(65536)
    try:
        codecs.getincrementaldecoder("utf-8-sig")().decode(sample, final=False)
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        encoding = "cp1251"
    with path.open(encoding=encoding, newline="") as f:
        first = f.readline()
        delimiter = ";" if first.count(";") >= first.count(",") else ","
        f.seek(0)
        return [h for cell in next(csv.reader(f, delimiter=delimiter), []) for h in cell.split(delimiter)]


def _col(ref):
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _text(el):
    # Обычная строка — <t>, форматированная — несколько <r><t>; фонетика <rPh> пропускается
    return "".join(t.text or "" for t in el.iter(MAIN + "t")) if el is not None else ""


def xlsx_sheets(path: Path):
    """[(имя листа, путь внутри архива)] в порядке книги."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("это не книга Excel .xlsx")
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            with z.open("xl/_rels/workbook.xml.rels") as fh:
                for _, el in iterparse(fh):
                    if el.tag == PKG + "Relationship":
                        target = el.get("Target", "")
                        rels[el.get("Id")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        sheets = []
        with z.open("xl/workbook.xml") as fh:
            for _, el in iterparse(fh):
                if el.tag == MAIN + "sheet":
                    target = rels.get(el.get(REL + "id"))
                    if target in names:
                        sheets.append((el.get("name") or "Лист", target))
        return sheets


def _shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out = []
    with z.open("xl/sharedStrings.xml") as fh:
        for _, el in iterparse(fh):
            if el.tag == MAIN + "si":
                out.append("".join(t.text or "" for t in el.iter(MAIN + "t")))
                el.clear()
    return out


def xlsx_rows(path: Path, sheet_path: str, strings=None):
    """Строки листа как списки строк; пропуски ячеек заполняются пустыми значениями."""
    with zipfile.ZipFile(path) as z, z.open(sheet_path) as fh:
        if strings is None:
            strings = _shared_strings(z)
        for _, el in iterparse(fh):
            if el.tag != MAIN + "row":
                continue
            row = []
            for c in el.iter(MAIN + "c"):
                idx = _col(c.get("r", "")) if c.get("r") else len(row)
                kind, v = c.get("t"), c.find(MAIN + "v")
                raw = v.text if v is not None and v.text is not None else ""
                if kind == "s":
                    value = strings[int(raw)] if raw.isdigit() and int(raw) < len(strings) else ""
                elif kind == "inlineStr":
                    value = _text(c.find(MAIN + "is"))
                elif kind == "b":
                    value = "true" if raw == "1" else "false"
                elif kind == "e":
                    value = ""
                else:
                    value = raw[:-2] if re.fullmatch(r"-?\d+\.0", raw) else raw
                if idx >= len(row):
                    row.extend([""] * (idx - len(row)))
                    row.append(value)
                else:
                    row[idx] = value
            el.clear()
            yield row


def xlsx_to_csv(path: Path, sheet_path: str, target: Path, strings=None):
    """Лист → CSV (UTF-8 BOM, «;»). Первая непустая строка — заголовки, строки выравниваются по их ширине."""
    headers = None
    with target.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        for row in xlsx_rows(path, sheet_path, strings):
            if headers is None:
                if not any(v.strip() for v in row):
                    continue
                while row and not row[-1].strip():
                    row.pop()
                headers = row
                writer.writerow(headers)
                continue
            if not any(v.strip() for v in row):
                continue
            writer.writerow((row + [""] * len(headers))[:len(headers)])
    return headers or []


def prepare_sources(folder: Path, files: dict, update=lambda **kw: None):
    """Файлы из загрузки (src-N.csv / src-N.xlsx) → folder/notices.csv и folder/items.csv."""
    found = {}  # kind -> (описание, путь)
    unknown = []
    for key in sorted(files, key=lambda k: int(k.split("-")[1])):
        meta = files[key]
        label = meta.get("name", key)
        path = folder / f"{key}{meta['ext']}"
        update(message=f"Читаем {label}…")
        if meta["ext"] == ".xlsx":
            try:
                sheets = xlsx_sheets(path)
                with zipfile.ZipFile(path) as z:
                    strings = _shared_strings(z)
            except (zipfile.BadZipFile, KeyError, SyntaxError) as exc:
                raise ValueError(f"{label}: не удалось прочитать книгу Excel. Сохраните её как .xlsx или CSV.") from exc
            tables = []
            for i, (name, sheet) in enumerate(sheets, 1):
                out = folder / f"{key}-sheet{i}.csv"
                try:
                    headers = xlsx_to_csv(path, sheet, out, strings)
                except SyntaxError as exc:
                    raise ValueError(f"{label}, лист «{name}»: повреждённые данные листа") from exc
                tables.append((f"{label}, лист «{name}»", out, headers))
        else:
            tables = [(label, path, csv_headers(path))]
        for title, table, headers in tables:
            try:
                kind = classify(headers)
            except ValueError as exc:
                raise ValueError(f"{title}: {exc}") from None
            if kind is None:
                unknown.append(title)
                continue
            if kind in found:
                raise ValueError(f"Найдено две таблицы «{KIND_LABEL[kind]}»: {found[kind][0]} и {title}. Оставьте одну.")
            found[kind] = (title, table)
    missing = [KIND_LABEL[k] for k in ("notices", "items") if k not in found]
    if missing:
        hint = {"Извещения": "lot_id и subject", "ТРУ": "lot_id, product_name и okpd2_code"}
        need = "; ".join(f"«{m}» — столбцы {hint[m]}" for m in missing)
        extra = f" Не распознаны: {', '.join(unknown)}." if unknown else ""
        raise ValueError(f"Не хватает данных: {need}.{extra}")
    for kind, (_, table) in found.items():
        table.replace(folder / f"{kind}.csv")
    return {kind: title for kind, (title, _) in found.items()}
