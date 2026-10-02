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
_titles: dict[str, str] = {}               # код → типичное наименование (подсказка, автозаполнение наименования)
_index: dict[str, list] = {}               # основа слова → [(код, вес)] — поиск кода по наименованию


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
                    data = json.load(f)
                codes = data["codes"]
            except (OSError, ValueError, KeyError):
                _ref = False
            else:
                weights = {c: {w: 1 - i / len(ws) for i, w in enumerate(ws)} for c, ws in codes.items()}
                by_tail = defaultdict(list)
                index = defaultdict(list)
                for c, ws in weights.items():
                    by_tail[c[2:]].append(c)
                    for w, x in ws.items():
                        index[w].append((c, x))
                _titles.update(data.get("titles", {}))
                _index.update(index)
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
    row["okpd2_fix_reason"] = why
    return {"from": row["okpd2_original"], "to": fixed, "name": row.get("product_name", ""), "why": why}


def unknown(code) -> bool:
    """Код похож на ОКПД2, но его нет в справочнике истории (и исправить не удалось)."""
    ref = reference()
    code = normalize(code)
    return bool(ref and code and code not in ref[0])


def _plural(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    return one if m10 == 1 and m100 != 11 else few if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else many


def summary(fixes: list[dict], total: int, left_unknown: int) -> list[str]:
    """Предупреждения для задачи: что и как исправлено в кодах, сколько осталось неизвестных."""
    out = []
    pos = lambda n: f"{n} {_plural(n, 'позиции', 'позиций', 'позиций')}"  # noqa: E731
    auto = [f for f in fixes if f["why"] not in ("исправлено пользователем", "по наименованию (выбор пользователя)")]
    user = [f for f in fixes if f["why"] == "исправлено пользователем"]
    by_name = [f for f in fixes if f["why"] == "по наименованию (выбор пользователя)"]
    if auto:
        classes = {f["from"][:2] for f in auto if f["from"]}
        hint = f" (во всех класс «{classes.pop()}» — похоже, подменены первые две цифры)" if len(classes) == 1 and len(auto) > 3 else ""
        examples = "; ".join(f'{f["from"] or "без кода"} → {f["to"]} («{f["name"][:40]}»)' for f in auto[:3])
        out.append(f"Коды ОКПД2: у {pos(len(auto))} из {total} код не указан или не существует в классификаторе{hint}. "
                   f"Восстановлены автоматически по справочнику кодов закупок СПб и названиям позиций, например: {examples}. "
                   f"Исходный код показан в позициях лота.")
    if user:
        out.append(f"Коды ОКПД2: у {pos(len(user))} код исправлен вручную в окне проверки.")
    if by_name:
        out.append(f"Коды ОКПД2: у {pos(len(by_name))} код не соответствовал наименованию — по вашему выбору подбор шёл "
                   f"по наименованию, код заменён подходящим из справочника.")
    if left_unknown:
        out.append(f"Коды ОКПД2: у {pos(left_unknown)} код не встречался в истории закупок СПб и не исправлен — "
                   f"для них подбор идёт по тексту и родительским уровням кода.")
    return out


# ---------- Проверка строк ТРУ перед подбором: наименование, код и их соответствие ----------
# Пороги противоречия «код ↔ наименование» подобраны на 36 тыс. строк истории: ложных срабатываний 0,7%
# (часть из них — реальные ошибки заказчиков), ловит ~40% подменённых кодов у описательных наименований.
CONFLICT_SCORE = 2.5
CONFLICT_WORDS = 2
GUESS_SCORE = 1.0      # ниже — подсказки по наименованию не даём
MAX_ROWS = 300         # строк в ответе проверки; остальные — только счётчиком


def name_ok(name) -> bool:
    """Наименование: хотя бы 3 буквы и слово от 3 букв (не «-», «123», «???»)."""
    text = str(name or "")
    return len(re.findall(r"[а-яёa-z]", text.lower())) >= 3 and bool(re.search(r"[а-яёa-z]{3,}", text.lower()))


def title(code) -> str:
    reference()
    return _titles.get(normalize(code), "")


def guess(name, k: int = 3) -> list[dict]:
    """Коды справочника по словам наименования: [{code, title, score}] по убыванию."""
    if reference() is None:
        return []
    scores = defaultdict(float)
    for w in stems(name):
        for c, x in _index.get(w, ()):
            scores[c] += x
    best = sorted(scores.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[:k]  # при равенстве — полный код
    return [{"code": c, "title": _titles.get(c, ""), "score": round(v, 2)} for c, v in best if v >= GUESS_SCORE]


def conflict(code, name) -> dict | None:
    """Код существует, но наименование уверенно указывает на другой класс → лучший код по наименованию."""
    ref = reference()
    if ref is None or code not in ref[0]:
        return None
    words = stems(name)
    if len(words) < CONFLICT_WORDS or any(w in ref[0][code] for w in words):
        return None
    top = guess(name, 10)
    if not top or top[0]["score"] < CONFLICT_SCORE or sum(w in ref[0][top[0]["code"]] for w in words) < CONFLICT_WORDS:
        return None
    if any(g["code"][:2] == code[:2] for g in top):
        return None
    return top[0]


def group_known(code) -> bool:
    """Группа кода (XX.XX) встречается в справочнике — код, скорее всего, настоящий, просто редкий."""
    ref = reference()
    return bool(ref) and any(c.startswith(code[:5]) for c in ref[0]) if len(code) >= 5 else False


def code_state(code) -> str:
    """ok — есть в справочнике; empty; format — не похоже на код; unknown — нет в справочнике."""
    code = normalize(code)
    if not code:
        return "empty"
    if not CODE.fullmatch(code):
        return "format"
    ref = reference()
    return "ok" if ref is None or code in ref[0] else "unknown"


def analyze_row(row: dict) -> dict | None:
    """Проблема строки ТРУ или None. kind: both (не принимаем), code, name (одно поле), conflict (противоречие)."""
    name, raw = row.get("product_name", ""), row.get("okpd2_code", "")
    code, state, good_name = normalize(raw), code_state(raw), name_ok(name)
    if not good_name and not str(name or "").strip() and state == "empty":
        return None  # пустая строка — пропускается, как раньше
    if not good_name and state != "ok":
        why = {"empty": "код не указан", "format": f"«{raw}» — не код ОКПД2 (нужно XX.XX.XX.XXX)",
               "unknown": f"кода {code} нет в классификаторе"}[state]
        return {"kind": "both", "code": code, "name": name,
                "problem": f"{why}; наименование {'пустое' if not str(name or '').strip() else f'«{name}» — не наименование товара'}",
                "hint": "Укажите в строке хотя бы одно корректное поле: наименование товара или код ОКПД2 вида 32.50.13.110."}
    if not good_name:
        return {"kind": "name", "code": code, "name": name,
                "problem": "наименование пустое" if not str(name or "").strip() else f"«{name}» — не наименование товара",
                "suggest": {"name": title(code)} if title(code) else None,
                "hint": f"Типичное наименование для кода {code} в закупках СПб: «{title(code)}»." if title(code) else "Впишите наименование товара или услуги."}
    if state != "ok":
        fixed, why = check(code, name) if state == "unknown" else (code, None)
        if state == "unknown" and why is None and (not guess(name) or group_known(code)):
            return None  # редкий, но правдоподобный код (его группа есть в справочнике) — не трогаем
        options = ([{"code": fixed, "title": title(fixed), "why": why}] if why else []) + \
                  [{**g, "why": "по наименованию позиции"} for g in guess(name) if g["code"] != fixed]
        problem = {"empty": "код не указан", "format": f"«{raw}» — не код ОКПД2 (нужно XX.XX.XX.XXX)",
                   "unknown": f"кода {code} нет в классификаторе"}[state]
        return {"kind": "code", "code": code, "name": name, "problem": problem,
                "suggest": {"code": options[0]["code"]} if options else None, "options": options[:3],
                "hint": "Выберите код из подсказок или впишите свой в формате XX.XX.XX.XXX." if options else
                        "Подобрать код не удалось — без кода подбор пойдёт по тексту наименования."}
    if (c := conflict(code, name)) is not None:
        return {"kind": "conflict", "code": code, "name": name,
                "problem": f"код {code} — «{title(code)}», а наименование ближе к коду {c['code']} — «{c['title']}»",
                "code_title": title(code), "suggest": {"code": c["code"]}, "options": [{**c, "why": "по наименованию позиции"}],
                "hint": "Выберите, на что ориентироваться при подборе: на код ОКПД2 или на наименование."}
    return None


def analyze(rows) -> dict:
    """rows — [(номер строки файла, lot_id, строка)] → сводка проверки для окна выбора сценария."""
    out = {"total": 0, "counts": {"both": 0, "code": 0, "name": 0, "conflict": 0}, "rows": [], "truncated": False}
    for line, lot_id, row in rows:
        out["total"] += 1
        issue = analyze_row(row)
        if issue is None:
            continue
        out["counts"][issue["kind"]] += 1
        if len(out["rows"]) < MAX_ROWS:
            out["rows"].append({"line": line, "lot_id": lot_id, **issue})
        else:
            out["truncated"] = True
    return out


def resolve(row: dict, choice: dict | None, trust: str | None) -> dict | None:
    """Применяет к строке выбор пользователя (choice — исправленные поля) или автоисправление.
    trust — для противоречия: 'code' (по умолчанию) или 'name'. Возвращает описание изменения или None."""
    before = (row.get("okpd2_code", ""), row.get("product_name", ""))
    if choice and choice.get("keep"):
        return None  # пользователь подтвердил: оставить как в файле
    if choice:
        if "okpd2_code" in choice:
            row["okpd2_code"] = normalize(choice["okpd2_code"])
        if "product_name" in choice:
            row["product_name"] = str(choice["product_name"]).strip()
        how = "исправлено пользователем"
    else:
        issue = analyze_row(row)
        if issue is None or issue["kind"] == "both":
            return None
        if issue["kind"] == "conflict" and trust != "name":
            return None
        suggest = issue.get("suggest") or {}
        if "code" in suggest:
            row["okpd2_code"] = suggest["code"]
        if "name" in suggest:
            row["product_name"] = suggest["name"]
        how = "по наименованию (выбор пользователя)" if issue["kind"] == "conflict" else "автоисправление по справочнику"
    after = (row.get("okpd2_code", ""), row.get("product_name", ""))
    if after == before:
        return None
    if after[0] != before[0]:
        row["okpd2_original"] = normalize(before[0])
        row["okpd2_fix_reason"] = how
    if after[1] != before[1]:
        row["product_name_original"] = before[1]
    return {"from": normalize(before[0]), "to": after[0], "name": after[1], "why": how}
