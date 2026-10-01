"""Реестры производителей и правообладателей из файлов, скачанных вручную (сайты отдают 403 скриптам).

- РРПП, ПП РФ 719 (gisp.gov.ru/pp719v2/pub/prod/ → «Скачать перечень (XLSX)») → dataset/production.xlsx
- Реестр российского ПО (reestr.digital.gov.ru/reestr/ → «Экспорт (XLS)») → dataset/Экспорт Реестра*.xlsx

Дают роль «производитель» или «правообладатель» с доказательством (реестровая запись + ОКПД2),
а таблица registry_items — поиск новых компаний по ОКПД2 лота (ФТ-06).
"""

import glob
import re
import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from python_calamine import CalamineWorkbook

from .inn import is_valid_inn
from .models import SourceResult

log = logging.getLogger("enrichment")

GISP_PATH = "dataset/production.xlsx"
SOFTWARE_GLOB = "dataset/Экспорт Реестра*.xlsx"

MAX_CODES_PER_FACT = 50  # в карточку кладём не больше 50 кодов, полный список — в registry_items


def _rows(path: Path):
    """Строки первого листа как словари по строке заголовка (ищем её среди первых 10 строк)."""
    sheet = CalamineWorkbook.from_path(str(path)).get_sheet_by_index(0)
    header = None
    for row in sheet.iter_rows():
        if header is None:
            if "ИНН" in row or any(str(c).startswith("Идентификационный номер (ИНН)") for c in row):
                header = [str(c).strip() for c in row]
            continue
        yield dict(zip(header, row))


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(v.strip()[:10], fmt).date()
            except ValueError:
                pass
    return None


def _okpd2(v) -> str:
    return str(v or "").strip().rstrip(".")


_CODE = re.compile(r"^\s*(\d{2}(?:\.\d+)*)")


def _okpd2_list(v) -> list[str]:
    """'58.29.2 Обеспечение ...;\n62.01.2 Оригиналы ...' → ['58.29.2', '62.01.2']"""
    return [m.group(1) for line in str(v or "").splitlines() if (m := _CODE.match(line))]


def _inn(v) -> str:
    """Excel хранит ИНН числом: 7708806062.0, а ведущий ноль теряется (0274... → 274...)."""
    if isinstance(v, float):
        v = int(v)
    s = str(v or "").strip()
    if s.isdigit() and len(s) in (9, 11):
        s = s.zfill(len(s) + 1)
    return s


def _resolve(path_or_glob: str) -> Path | None:
    hits = sorted(glob.glob(path_or_glob))
    return Path(hits[-1]) if hits else None


def load_gisp(path: Path) -> dict[str, dict]:
    """ИНН → {okpd2: {count, sample}} по действующим записям РРПП."""
    today = date.today()
    out: dict[str, dict] = defaultdict(dict)
    total = active = 0
    for r in _rows(path):
        total += 1
        inn = _inn(r.get("ИНН"))
        if not is_valid_inn(inn):
            continue
        ended = _as_date(r.get("Фактическая дата прекращения действия реестровой записи"))
        valid_to = _as_date(r.get("Срок действия"))
        if ended or (valid_to and valid_to < today):
            continue
        code = _okpd2(r.get("ОКПД2"))
        if not code:
            continue
        active += 1
        item = out[inn].setdefault(code, {"count": 0, "sample": r.get("Наименование продукции"),
                                          "org": r.get("Предприятие")})
        item["count"] += 1
    log.info("РРПП: %d записей, действующих %d, производителей %d", total, active, len(out))
    return out


def load_software(path: Path) -> dict[str, dict]:
    """ИНН правообладателя → {okpd2: {count, sample}} по невыключенным записям реестра ПО."""
    out: dict[str, dict] = defaultdict(dict)
    total = active = 0
    for r in _rows(path):
        total += 1
        inn = _inn(r.get("Идентификационный номер (ИНН)"))
        if not is_valid_inn(inn) or _as_date(r.get("Дата исключения")):
            continue
        active += 1
        for code in _okpd2_list(r.get("Код продукции")) or ["58.29"]:
            item = out[inn].setdefault(code, {"count": 0, "sample": r.get("Наименование ПО"),
                                              "org": r.get("Сокращенное наименование (ФИО) правообладателя")})
            item["count"] += 1
    log.info("Реестр ПО: %d записей, действующих %d, правообладателей %d", total, active, len(out))
    return out


REGISTRIES = {
    # имя: (путь по умолчанию, загрузчик, поле-признак, поле с кодами, человекочитаемое имя)
    "gisp": (GISP_PATH, load_gisp, "in_gisp", "gisp_okpd2", "реестр промышленной продукции (ПП 719)"),
    "software": (SOFTWARE_GLOB, load_software, "in_software_registry", "software_okpd2", "реестр российского ПО"),
}


def load(name: str, targets: set[str], path: str | None = None) -> tuple[list[SourceResult], list[dict]]:
    """(факты для targets, строки registry_items для всех компаний реестра)."""
    default, loader, flag_field, codes_field, _ = REGISTRIES[name]
    p = _resolve(path or default)
    if p is None:
        log.warning("%s: файл %s не найден, пропускаю (скачать вручную, см. docs/enrichment)", name, path or default)
        return [], []
    data = loader(p)
    fetched_at = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    source = f"reg_{name}"
    items = [{"inn": inn, "registry": name, "okpd2": code, "items_count": v["count"],
              "sample": str(v.get("sample") or "")[:300], "org_name": v.get("org"),
              "source": source, "fetched_at": fetched_at}
             for inn, codes in data.items() for code, v in codes.items()]
    results = []
    for inn in targets:
        res = SourceResult(source, inn)
        codes = data.get(inn)
        res.add(flag_field, bool(codes), fetched_at)
        if codes:
            top = sorted(codes.items(), key=lambda kv: -kv[1]["count"])[:MAX_CODES_PER_FACT]
            res.add(codes_field, [{"okpd2": c, "count": v["count"], "sample": v.get("sample")} for c, v in top], fetched_at)
        results.append(res)
    return results, items
