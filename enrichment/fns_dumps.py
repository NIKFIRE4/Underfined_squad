"""Массовые выгрузки открытых данных ФНС (nalog.gov.ru/opendata).

Признаки по всем ИНН выгрузки без поштучных запросов и капчи: численность, уплаченные налоги,
недоимки, налоговые правонарушения. Каждый набор — ZIP с XML-файлами из элементов <Документ>.

Источник факта — `fns_<набор>`, fetched_at — момент скачивания архива,
дата, на которую ФНС составила сведения, пишется отдельным полем `*_as_of`.
"""

import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import httpx
from lxml import etree

from .http import USER_AGENT
from .models import SourceResult
from .sources._util import num, ru_date

log = logging.getLogger("enrichment")

DUMPS_DIR = Path("data/fns")

Parsed = tuple[str, dict]  # (ИНН, поля)


def _inn(doc) -> str | None:
    np_ = doc.find("СведНП")
    if np_ is None:
        return None
    return np_.get("ИННЮЛ") or np_.get("ИННФЛ")


def _sshr(doc) -> dict:
    e = doc.find("СведССЧР")
    return {"employees": num(e.get("КолРаб")) if e is not None else None}


def _paytax(doc) -> dict:
    rows = [(e.get("НаимНалог"), num(e.get("СумУплНал")) or 0.0) for e in doc.iterfind("СвУплСумНал")]
    return {"taxes_paid": round(sum(v for _, v in rows), 2),
            "taxes_paid_detail": {k: v for k, v in rows if v}}


def _debtam(doc) -> dict:
    rows = [{"tax": e.get("НаимНалог"), "total": num(e.get("ОбщСумНедоим")) or 0.0,
             "penalty": num(e.get("СумПени")) or 0.0, "fine": num(e.get("СумШтраф")) or 0.0}
            for e in doc.iterfind("СведНедоим")]
    return {"tax_arrears_total": round(sum(r["total"] for r in rows), 2), "tax_arrears": rows}


def _taxoffence(doc) -> dict:
    total = sum(num(e.get("СумШтраф")) or 0.0 for e in doc.iterfind("СведНаруш"))
    return {"tax_fines_total": round(total, 2)}


@dataclass
class Dataset:
    name: str
    url: str
    parse: Callable
    as_of_field: str
    # значения для юрлиц, которых нет в наборе: набор перечисляет всех, у кого признак есть
    absent_ul: dict = field(default_factory=dict)


DATASETS = {
    d.name: d for d in [
        Dataset("sshr2019",
                "https://file.nalog.ru/opendata/7707329152-sshr2019/data-20260825-structure-20200408.zip",
                _sshr, "employees_as_of"),
        Dataset("paytax",
                "https://file.nalog.ru/opendata/7707329152-paytax/data-20260401-structure-20180110.zip",
                _paytax, "taxes_paid_as_of"),
        Dataset("debtam",
                "https://file.nalog.ru/opendata/7707329152-debtam/data-20260725-structure-20181201.zip",
                _debtam, "tax_arrears_as_of", {"tax_arrears_total": 0.0}),
        Dataset("taxoffence",
                "https://data.nalog.ru/opendata/7707329152-taxoffence/data-20251201-structure-20191201.zip",
                _taxoffence, "tax_fines_as_of", {"tax_fines_total": 0.0}),
    ]
}


def download(ds: Dataset, dest_dir: Path = DUMPS_DIR) -> Path:
    """Скачивание с докачкой: file.nalog.ru медленный и рвёт соединения."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{ds.name}.zip"
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=httpx.Timeout(600, connect=60), follow_redirects=True) as c:
        total = int(c.head(ds.url, headers=headers).headers.get("content-length", 0))
        for attempt in range(10):
            have = path.stat().st_size if path.exists() else 0
            if total and have >= total:
                break
            try:
                with c.stream("GET", ds.url, headers={**headers, "Range": f"bytes={have}-"}) as r:
                    r.raise_for_status()
                    with open(path, "ab" if r.status_code == 206 else "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
            except httpx.HTTPError as e:
                log.warning("%s: %s, retry %d", ds.name, e, attempt + 1)
    log.info("%s: %s (%d MB)", ds.name, path, path.stat().st_size >> 20)
    return path


def iter_docs(path: Path) -> Iterator[etree._Element]:
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            with z.open(name) as f:
                for _, doc in etree.iterparse(f, tag="Документ", huge_tree=True):
                    yield doc
                    doc.clear()
                    while doc.getprevious() is not None:
                        del doc.getparent()[0]


def load(ds: Dataset, targets: set[str], dest_dir: Path = DUMPS_DIR) -> list[SourceResult]:
    """Факты набора для ИНН из `targets` (в выгрузках десятки миллионов строк, храним только нужное)."""
    path = dest_dir / f"{ds.name}.zip"
    fetched_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    source = f"fns_{ds.name}"
    found: dict[str, SourceResult] = {}
    seen = 0
    for doc in iter_docs(path):
        seen += 1
        inn = _inn(doc)
        if inn not in targets:
            continue
        res = found.setdefault(inn, SourceResult(source, inn))
        for k, v in ds.parse(doc).items():
            res.add(k, v, fetched_at)
        res.add(ds.as_of_field, ru_date(doc.get("ДатаСост")), fetched_at)
    matched = len(found)
    for inn in targets - found.keys():
        res = SourceResult(source, inn)
        if len(inn) == 10:
            for k, v in ds.absent_ul.items():
                res.add(k, v, fetched_at)
        found[inn] = res
    log.info("%s: %d документов, найдено %d из %d ИНН",
             ds.name, seen, matched, len(targets))
    return list(found.values())
