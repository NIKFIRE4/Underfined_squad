"""Массовые выгрузки открытых данных ФНС (nalog.gov.ru/opendata).

Признаки по всем ИНН выгрузки без поштучных запросов и капчи: численность, уплаченные налоги,
недоимки, налоговые правонарушения. Каждый набор — ZIP с XML-файлами из элементов <Документ>.

Источник факта — `fns_<набор>`, fetched_at — момент скачивания архива,
дата, на которую ФНС составила сведения, пишется отдельным полем `*_as_of`.
"""

import logging
import zipfile
import zlib
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


def first_bad_offset(path: Path) -> int | None:
    """Смещение первого повреждённого файла в ZIP (проверка CRC) или None, если архив цел.
    Нечитаемый центральный каталог — повреждён хвост архива."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return max(path.stat().st_size - (1 << 20), 0)
    with z:
        for info in z.infolist():
            try:
                with z.open(info) as f:
                    while f.read(1 << 20):
                        pass
            except (zipfile.BadZipFile, zlib.error, OSError, EOFError):
                return info.header_offset
    return None


PART = 32 << 20  # 32 МБ: часть качается за секунды, обрыв соединения стоит одной части


def _fetch_part(url: str, path: Path, start: int, end: int) -> None:
    """Скачать байты [start, end] и записать по смещению; часть засчитывается только целиком."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Range": f"bytes={start}-{end}"}
    with httpx.Client(timeout=httpx.Timeout(120, connect=30), follow_redirects=True) as c:
        r = c.get(url, headers=headers)
    if r.status_code != 206 or not r.headers.get("content-range", "").startswith(f"bytes {start}-{end}/"):
        raise httpx.HTTPError(f"bad range response {r.status_code} {r.headers.get('content-range')}")
    if len(r.content) != end - start + 1:
        raise httpx.HTTPError(f"short part {len(r.content)} != {end - start + 1}")
    with open(path, "r+b") as f:
        f.seek(start)
        f.write(r.content)


def download(ds: Dataset, dest_dir: Path = DUMPS_DIR, workers: int = 4, attempts: int = 8) -> Path:
    """Скачивание частями по Range в несколько потоков, с докачкой между запусками и проверкой CRC.

    file.nalog.ru рвёт соединения каждые ~30 с, а докачка одним потоком склеивала мусор на стыках.
    Здесь каждая часть — отдельный запрос с проверкой длины; прогресс — в файле <имя>.zip.parts.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{ds.name}.zip"
    state = path.with_suffix(".zip.parts")
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        total = int(c.head(ds.url, headers={"User-Agent": USER_AGENT}).headers["content-length"])

    if path.exists() and path.stat().st_size == total and not state.exists() and first_bad_offset(path) is None:
        log.info("%s: %s уже скачан и цел", ds.name, path)
        return path
    parts = [(o, min(o + PART, total) - 1) for o in range(0, total, PART)]
    done: set[int] = set()
    if state.exists() and path.exists() and path.stat().st_size == total:
        done = {int(x) for x in state.read_text().split()}
    else:
        with open(path, "wb") as f:
            f.truncate(total)
        state.write_text("")

    for round_ in range(1, attempts + 1):
        todo = [p for p in parts if p[0] not in done]
        if todo:
            log.info("%s: раунд %d, осталось %d из %d частей", ds.name, round_, len(todo), len(parts))
            with ThreadPoolExecutor(workers) as pool:
                futs = {pool.submit(_fetch_part, ds.url, path, s, e): s for s, e in todo}
                for fut in as_completed(futs):
                    try:
                        fut.result()
                        done.add(futs[fut])
                        with open(state, "a") as f:
                            f.write(f"{futs[fut]}\n")
                    except httpx.HTTPError as e:
                        log.warning("%s: часть %d: %s", ds.name, futs[fut], e)
            continue
        bad = first_bad_offset(path)
        if bad is None:
            state.unlink(missing_ok=True)
            log.info("%s: %s (%d MB), архив цел", ds.name, path, total >> 20)
            return path
        # перекачиваем части от повреждённого файла архива до следующих 64 МБ
        redo = {s for s, e in parts if e >= bad and s <= bad + 2 * PART}
        log.warning("%s: CRC не сошёлся с байта %d, перекачиваю %d частей", ds.name, bad, len(redo))
        done -= redo
        state.write_text("".join(f"{x}\n" for x in sorted(done)))
    raise RuntimeError(f"{ds.name}: архив не скачан за {attempts} раундов, перезапустите fns-download")


def iter_docs(path: Path) -> Iterator[etree._Element]:
    """Потоковый обход <Документ>. В выгрузках ФНС попадаются файлы с битыми байтами:
    парсер восстанавливается, а если файл не читается совсем — пропускаем его с предупреждением."""
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            try:
                with z.open(name) as f:
                    for _, doc in etree.iterparse(f, tag="Документ", huge_tree=True, recover=True):
                        yield doc
                        doc.clear()
                        while doc.getprevious() is not None:
                            del doc.getparent()[0]
            except (etree.XMLSyntaxError, zipfile.BadZipFile, OSError) as e:
                log.warning("%s/%s: пропущен, %s", path.name, name, e)


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


# --- Реестр МСП: признаки для известных ИНН и пул новых компаний (ФТ-06) ---

POOL_REGIONS = {"78", "47"}  # Санкт-Петербург и Ленобласть


def _rsmp(doc) -> dict:
    org, ip = doc.find("ОргВклМСП"), doc.find("ИПВклМСП")
    rec: dict = {"is_smp": True, "smp_category": int(doc.get("КатСубМСП") or 0) or None,
                 "smp_since": ru_date(doc.get("ДатаВклМСП")),
                 "smp_is_new": doc.get("ПризНовМСП") == "1",
                 "smp_is_social": doc.get("СведСоцПред") == "1",
                 "employees_rmsp": num(doc.get("ССЧР"))}
    if org is not None:
        rec.update(kind="ul", inn=org.get("ИННЮЛ"), ogrn=org.get("ОГРН"),
                   name_full=org.get("НаимОрг"), name_short=org.get("НаимОргСокр"))
    elif ip is not None:
        fio = ip.find("ФИОИП")
        name = " ".join(filter(None, (fio.get(k) for k in ("Фамилия", "Имя", "Отчество")))) if fio is not None else ""
        rec.update(kind="ip", inn=ip.get("ИННФЛ"), ogrn=ip.get("ОГРНИП"), name_full=f"ИП {name}".strip())
    mn = doc.find("СведМН")
    if mn is not None:
        rec["region_code"] = mn.get("КодРегион")
        place = mn.find("Город") if mn.find("Город") is not None else mn.find("НаселПункт")
        rec["locality"] = place.get("Наим") if place is not None else None
    main = doc.find("СвОКВЭД/СвОКВЭДОсн")
    if main is not None:
        rec["okved_main"], rec["okved_main_name"] = main.get("КодОКВЭД"), main.get("НаимОКВЭД")
    rec["okved_extra"] = [e.get("КодОКВЭД") for e in doc.iterfind("СвОКВЭД/СвОКВЭДДоп")]
    rec["products"] = [{"okpd2": e.get("КодПрод"), "name": e.get("НаимПрод"),
                        "innovative": e.get("ПрОтнПрод") == "1"} for e in doc.iterfind("СвПрод")]
    rec["licenses_count"] = len(doc.findall("СвЛиценз"))
    rec["as_of"] = ru_date(doc.get("ДатаСост"))
    return rec


# В DATASETS для скачивания; загружается своей функцией load_rsmp, а не общей load
DATASETS["rsmp"] = Dataset(
    "rsmp", "https://file.nalog.ru/opendata/7707329152-rsmp/data-10092026-structure-12052026.zip",
    _rsmp, "smp_as_of", {"is_smp": False},
)

FACT_FIELDS = ("is_smp", "smp_category", "smp_since", "smp_is_new", "smp_is_social", "employees_rmsp",
               "ogrn", "name_full", "name_short", "region_code", "okved_main", "okved_main_name",
               "okved_extra", "products", "licenses_count")


def load_rsmp(targets: set[str], on_pool_chunk: Callable[[list[dict], list[dict]], None],
              regions: set[str] = POOL_REGIONS, dest_dir: Path = DUMPS_DIR,
              chunk: int = 5000) -> list[SourceResult]:
    """Один проход по РМСП: факты для `targets` и пул МСП из `regions` порциями в `on_pool_chunk`."""
    path = dest_dir / "rsmp.zip"
    fetched_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    source = "fns_rsmp"
    found: dict[str, SourceResult] = {}
    comps: list[dict] = []
    codes: list[dict] = []
    seen = pooled = 0
    pool_inns: set[str] = set()  # в РМСП встречаются повторы ИНН, берём первую запись
    for doc in iter_docs(path):
        seen += 1
        rec = _rsmp(doc)
        inn = rec.get("inn")
        if not inn:
            continue
        if inn in targets:
            res = found.setdefault(inn, SourceResult(source, inn))
            for k in FACT_FIELDS:
                res.add(k, rec.get(k), fetched_at)
            res.add("smp_as_of", rec["as_of"], fetched_at)
        if rec.get("region_code") in regions and inn not in pool_inns:
            pool_inns.add(inn)
            pooled += 1
            comps.append({k: rec.get(k) for k in (
                "inn", "kind", "ogrn", "name_full", "name_short", "region_code", "locality", "smp_category",
                "smp_since", "okved_main", "okved_main_name", "okved_extra", "products", "licenses_count", "as_of")}
                | {"employees": rec.get("employees_rmsp"), "source": source, "fetched_at": fetched_at})
            row_codes = {(rec.get("okved_main"), "okved_main")} if rec.get("okved_main") else set()
            row_codes |= {(c, "okved") for c in rec["okved_extra"] if c}
            row_codes |= {(p["okpd2"], "product") for p in rec["products"] if p["okpd2"]}
            codes += [{"inn": inn, "code": c, "kind": k} for c, k in row_codes]
            if len(comps) >= chunk:
                on_pool_chunk(comps, codes)
                comps, codes = [], []
        if seen % 500_000 == 0:
            log.info("rsmp: %d документов, в пуле %d, совпало с поставщиками %d", seen, pooled, len(found))
    if comps:
        on_pool_chunk(comps, codes)
    matched = len(found)
    for inn in targets - found.keys():
        res = SourceResult(source, inn)
        res.add("is_smp", False, fetched_at)
        found[inn] = res
    log.info("rsmp: %d документов, пул %d (регионы %s), совпало %d из %d ИНН",
             seen, pooled, sorted(regions), matched, len(targets))
    return list(found.values())
