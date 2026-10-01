import csv
import io
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from models import Candidate, Lot
from integrations import recommender, enricher

csv.field_size_limit(1_000_000)
OUTPUT_FIELDS = ["lot_id", "subject", "rank", "supplier_name", "supplier_inn", "supplier_kpp", "score", "role", "status", "region", "is_smp", "is_new", "reasons", "sources", "enrichment_status", "is_demo"]


def read_csv(path: Path, kind: str):
    import codecs
    sample = path.read_bytes() if path.stat().st_size < 65536 else None
    if sample is None:
        with path.open("rb") as f:
            sample = f.read(65536)
    if b"\x00" in sample:
        raise ValueError("Файл содержит нулевые байты. Сохраните его как CSV UTF-8.")
    try:
        codecs.getincrementaldecoder("utf-8-sig")().decode(sample, final=False)
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        encoding = "cp1251"
    with path.open(encoding=encoding, newline="") as f:
        header_line = f.readline()
        delimiter = ";" if header_line.count(";") >= header_line.count(",") else ","
        f.seek(0)
        reader = csv.reader(f, delimiter=delimiter, strict=True)
        headers = [h.strip().lower() for h in next(reader, [])]
        required = {"lot_id"} if kind == "notices" else {"lot_id", "product_name", "okpd2_code"}
        missing = required - set(headers)
        if kind == "notices" and not {"subject", "procedure_name"}.intersection(headers):
            missing.add("subject (или procedure_name)")
        if missing:
            raise ValueError(f'{"Извещения" if kind == "notices" else "ТРУ"}: не найдены столбцы: {", ".join(sorted(missing))}. Разделитель — ; или ,.')
        if len(headers) != len(set(headers)) or any(not h for h in headers):
            raise ValueError("Заголовки столбцов пустые или повторяются")
        for values in reader:
            if not values or not any(v.strip() for v in values):
                continue
            if len(values) != len(headers):
                raise ValueError(f"Строка {reader.line_num}: число значений не совпадает с числом столбцов")
            row = dict(zip(headers, (v.strip() for v in values)))
            if not row["lot_id"]:
                raise ValueError(f"Строка {reader.line_num}: пустой lot_id")
            if kind == "notices":
                row["subject"] = row.get("subject") or row.get("procedure_name", "")
                if not row["subject"]:
                    raise ValueError(f"Строка {reader.line_num}: пустой предмет закупки")
            yield row, reader.line_num, encoding, delimiter


def safe_cell(value):
    text = "" if value is None else str(value)
    # Neutralize spreadsheet formulas, including prefixed whitespace/control chars.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def demo_recommend(lot, top_k):
    names = ["ДЕМО · Северный поставщик", "ДЕМО · Балтийский производитель", "ДЕМО · Городской дистрибьютор"]
    roles = ["Поставщик-исполнитель", "Производитель", "Дистрибьютор"]
    return [Candidate(supplier_name=n, score=92-i*7, role=roles[i], status="Вымышленная компания", region="Санкт-Петербург", reasons=["Демонстрационная строка для проверки интерфейса", "Релевантность моделью не рассчитана"], enrichment_status="Демо: источники не запрашивались") for i, n in enumerate(names[:top_k])]


def validate_candidates(candidates, mode):
    if not isinstance(candidates, list) or any(not isinstance(c, Candidate) for c in candidates):
        raise ValueError("Адаптер должен вернуть list[Candidate]. См. INTEGRATION.md")
    seen = set()
    result = []
    for c in candidates:
        if not isinstance(c.supplier_name, str) or not c.supplier_name.strip():
            raise ValueError("Адаптер вернул пустое название поставщика")
        if isinstance(c.score, bool) or not isinstance(c.score, (int, float)) or not math.isfinite(c.score) or not 0 <= c.score <= 100:
            raise ValueError("Адаптер вернул score вне диапазона 0–100")
        if not isinstance(c.supplier_inn, str) or not isinstance(c.supplier_kpp, str):
            raise ValueError("ИНН и КПП в контракте должны быть строками")
        if mode == "live" and (not c.supplier_inn.isdigit() or len(c.supplier_inn) not in (10, 12)):
            raise ValueError("Адаптер вернул ИНН не из 10 или 12 цифр")
        if not isinstance(c.reasons, list) or any(not isinstance(s, str) for s in c.reasons):
            raise ValueError("reasons должен быть списком строк")
        if not isinstance(c.sources, list) or any(not isinstance(s, dict) for s in c.sources):
            raise ValueError("sources должен быть списком объектов")
        key = c.supplier_inn or c.supplier_name
        if key not in seen:
            result.append(c)
            seen.add(key)
    return sorted(result, key=lambda c: c.score, reverse=True)


def run_pipeline(folder: Path, mode: str, top_k: int, update):
    warnings = []
    stats = {"notices": 0, "items": 0, "lots": 0, "recommendations": 0, "without_items": 0, "without_candidates": 0, "enrichment_errors": 0, "skipped_items": 0}
    preview = []
    formats = {}
    with closing(sqlite3.connect(folder / "input.sqlite")) as db, db:
        db.execute("CREATE TABLE notices (lot_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        db.execute("CREATE TABLE items (lot_id TEXT NOT NULL, data TEXT NOT NULL)")
        for kind, table, stat, base in [("notices", "notices", "notices", 5), ("items", "items", "items", 20)]:
            update(stage="validation", progress=base, message=f'Проверяем {"извещения" if kind == "notices" else "позиции ТРУ"}…')
            for row, line, encoding, delimiter in read_csv(folder / f"{kind}.csv", kind):
                if kind == "items" and not row["product_name"] and not row["okpd2_code"]:
                    stats["skipped_items"] += 1
                    continue
                try:
                    db.execute(f"INSERT INTO {table} VALUES (?, ?)", (row["lot_id"], json.dumps(row, ensure_ascii=False)))
                except sqlite3.IntegrityError:
                    raise ValueError(f"Извещения, строка {line}: повторяется lot_id {row['lot_id'][:80]}") from None
                stats[stat] += 1
                formats[kind] = {"encoding": encoding, "delimiter": delimiter}
                if stats[stat] % 5000 == 0:
                    db.commit()
                    update(message=f'Проверено строк: {stats[stat]:,}', stats=dict(stats))
            if not stats[stat]:
                raise ValueError(f'{"Извещения" if kind == "notices" else "ТРУ"}: нет строк данных')
            db.commit()
        update(progress=38, message="Связываем файлы по lot_id…", stats=dict(stats), formats=formats)
        db.execute("CREATE INDEX items_lot ON items(lot_id)")
        orphan_count = db.execute("SELECT COUNT(*) FROM items i WHERE NOT EXISTS (SELECT 1 FROM notices n WHERE n.lot_id=i.lot_id)").fetchone()[0]
        if orphan_count:
            raise ValueError(f"У {orphan_count} позиций ТРУ lot_id отсутствует в Извещениях. Загрузите согласованную пару файлов.")
        stats["without_items"] = db.execute("SELECT COUNT(*) FROM notices n WHERE NOT EXISTS (SELECT 1 FROM items i WHERE i.lot_id=n.lot_id)").fetchone()[0]
        stats["lots"] = stats["notices"] - stats["without_items"]
        if not stats["lots"]:
            raise ValueError("В файлах нет лотов с совпадающим lot_id")
        if stats["without_items"]:
            warnings.append(f'Пропущено извещений без ТРУ: {stats["without_items"]}.')
        if stats["skipped_items"]:
            warnings.append(f'Пропущено строк ТРУ без названия и кода ОКПД2: {stats["skipped_items"]}. Остальные позиции включены в подбор.')
        update(progress=43, message="Файлы проверены", stats=dict(stats), warnings=warnings)
        with (folder / "result.part").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, delimiter=";")
            writer.writeheader()
            cursor = db.execute("SELECT n.lot_id,n.data FROM notices n WHERE EXISTS (SELECT 1 FROM items i WHERE i.lot_id=n.lot_id) ORDER BY n.rowid")
            for index, (lot_id, data) in enumerate(cursor, 1):
                lot = Lot(lot_id, json.loads(data), [json.loads(r[0]) for r in db.execute("SELECT data FROM items WHERE lot_id=?", (lot_id,))])
                if index == 1 or index % 50 == 0:
                    update(stage="recommendation", progress=45+int(49*(index-1)/stats["lots"]), message=f'Подбираем поставщиков: лот {index} из {stats["lots"]}', stats=dict(stats))
                candidates = demo_recommend(lot, top_k) if mode == "demo" else recommender.recommend(lot, top_k)
                candidates = validate_candidates(candidates, mode)
                if mode == "live":
                    try:
                        import copy
                        enriched = enricher.enrich(lot, copy.deepcopy(candidates))
                    except Exception:
                        stats["enrichment_errors"] += 1
                        for c in candidates:
                            c.enrichment_status = "Источник недоступен — требуется проверка"
                            c.status = "Требует проверки"
                    else:
                        candidates = validate_candidates(enriched, mode)
                candidates = candidates[:top_k]
                if not candidates:
                    stats["without_candidates"] += 1
                for rank, candidate in enumerate(candidates, 1):
                    row = {"lot_id": lot_id, "subject": lot.notice["subject"], "rank": rank, **asdict(candidate), "is_demo": mode == "demo"}
                    if len(preview) < 100:
                        preview.append(row)
                    exported = dict(row)
                    exported["reasons"] = " | ".join(row["reasons"])
                    exported["sources"] = json.dumps(row["sources"], ensure_ascii=False)
                    writer.writerow({k: safe_cell(v) for k, v in exported.items()})
                    stats["recommendations"] += 1
        if stats["enrichment_errors"]:
            warnings.append(f'Не удалось обогатить лотов: {stats["enrichment_errors"]}. Данные источников не подтверждены.')
        if stats["without_candidates"]:
            warnings.append(f'Для {stats["without_candidates"]} лотов кандидаты не найдены.')
        update(stage="export", progress=97, message="Сохраняем файл «Поставщики»…")
        (folder / "result.part").replace(folder / "suppliers.csv")
        update(status="completed", stage="completed", progress=100, message="Файл «Поставщики» готов", stats=stats, warnings=warnings, preview=preview, formats=formats)
