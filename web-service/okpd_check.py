"""Проверка кодов ОКПД2 во входных файлах («защита от дурака»).

В файлах предзащиты у всех кодов был подменён класс — первые две цифры (32.50.13.110 → 02.50.13.110):
такие коды не существуют, модель не находит по ним поставщиков, сверка ОКПД2 пустая.

Правило: код, которого нет в справочнике (models/okpd2_reference.json.gz — все коды истории закупок СПб),
восстанавливается по остальной части кода «XX.**.**.***»: если в справочнике один код с таким хвостом — берём его,
если несколько — тот, чьи частые слова из названий позиций больше совпадают с названием позиции.
Нет кандидатов или ни одного общего слова — код не трогаем: лучше без кода (работает поиск по тексту),
чем с выдуманным. Исходный код сохраняется в okpd2_original, исправления — в предупреждениях задачи.
"""
import gzip
import json
import re
import threading
from collections import defaultdict
from pathlib import Path

REFERENCE = Path(__file__).resolve().parents[1] / "models" / "okpd2_reference.json.gz"
CODE = re.compile(r"\d{2}(\.\d{1,2}(\.\d{1,2}(\.\d{1,3})?)?)?")
_lock = threading.Lock()
_ref = None


def stems(text: str) -> set[str]:
    """Как в scripts/build_okpd2_reference.py: первые 5 букв слов от 4 букв."""
    return {w[:5] for w in re.findall(r"[а-яёa-z]{4,}", str(text or "").lower().replace("ё", "е"))}


def reference():
    """(коды → {основа: вес}, хвост кода → коды) или None, если справочника нет."""
    global _ref
    with _lock:
        if _ref is None:
            try:
                with gzip.open(REFERENCE, "rt", encoding="utf-8") as f:
                    codes = json.load(f)["codes"]
            except (OSError, ValueError, KeyError):
                _ref = False
            else:
                weights = {c: {w: 1 - i / len(ws) for i, w in enumerate(ws)} for c, ws in codes.items()}
                by_tail = defaultdict(list)
                for c in codes:
                    by_tail[c[2:]].append(c)
                _ref = (weights, dict(by_tail))
        return _ref or None


def normalize(code) -> str:
    return re.sub(r"\s+", "", str(code or "")).replace(",", ".").strip(".")


def check(code, name: str) -> tuple[str, str | None]:
    """(код для подбора, причина исправления или None). Справочника нет — код как есть."""
    code = normalize(code)
    ref = reference()
    if not code or ref is None:
        return code, None
    weights, by_tail = ref
    if code in weights or not CODE.fullmatch(code) or len(code) < 5:
        return code, None
    tail = code[2:]
    candidates = by_tail.get(tail, [])
    if len(candidates) == 1:
        return candidates[0], "единственный существующий код с такими цифрами после класса"
    if not candidates:
        # короткий код (02.39.18): в справочнике только полные коды — ищем класс среди кодов, которые с него начинаются
        longer = [c for t, cs in by_tail.items() if t.startswith(tail) for c in cs]
        by_class = defaultdict(list)
        for c in longer:
            by_class[c[:2]].append(c)
        words = stems(name)
        scored = sorted(((max(sum(weights[c].get(w, 0) for w in words) for c in cs), cl) for cl, cs in by_class.items()), reverse=True)
        if len(scored) == 1 or (scored and scored[0][0] > 0 and scored[0][0] > scored[1][0]):
            return scored[0][1] + tail, "класс по существующим кодам, которые начинаются с этих цифр"
        return code, None
    words = stems(name)
    scored = sorted(((sum(weights[c].get(w, 0) for w in words), c) for c in candidates), reverse=True)
    if scored[0][0] > 0 and scored[0][0] > scored[1][0]:
        return scored[0][1], "выбран по названию позиции среди кодов с такими цифрами после класса"
    return code, None


def fix_item(row: dict) -> dict | None:
    """Исправляет okpd2_code позиции на месте. Возвращает описание исправления или None."""
    original = row.get("okpd2_code", "")
    fixed, why = check(original, row.get("product_name", ""))
    if why is None:
        return None
    row["okpd2_original"] = normalize(original)
    row["okpd2_code"] = fixed
    return {"from": row["okpd2_original"], "to": fixed, "name": row.get("product_name", ""), "why": why}


def unknown(code) -> bool:
    """Код похож на ОКПД2, но его нет в справочнике истории (и исправить не удалось)."""
    ref = reference()
    code = normalize(code)
    return bool(ref and code and code not in ref[0])


def summary(fixes: list[dict], total: int, left_unknown: int) -> list[str]:
    """Предупреждения для задачи: сколько исправлено, примеры, сколько осталось неизвестных."""
    out = []
    if fixes:
        classes = {f["from"][:2] for f in fixes}
        hint = f" (во всех исправленных кодах класс «{classes.pop()}» — похоже, подменены первые две цифры)" if len(classes) == 1 and len(fixes) > 3 else ""
        examples = "; ".join(f'{f["from"]} → {f["to"]} («{f["name"][:40]}»)' for f in fixes[:3])
        out.append(f"Коды ОКПД2: {len(fixes)} из {total} позиций не существуют в классификаторе{hint}. "
                   f"Исправлены по справочнику и названиям позиций, например: {examples}. Исходный код показан в позициях лота.")
    if left_unknown:
        out.append(f"Коды ОКПД2: у {left_unknown} позиций код не встречался в истории закупок СПб и не исправлен — "
                   f"для них подбор идёт по тексту и родительским уровням кода.")
    return out
