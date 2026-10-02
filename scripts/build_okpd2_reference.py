"""Справочник ОКПД2 для проверки кодов во входных файлах → models/okpd2_reference.json.gz.

Коды — все, что встречались в ТРУ_24-25.csv (8,4 тыс.), для каждого — частые основы слов из названий позиций
и типичное наименование (самое частое название позиции с этим кодом — подсказка пользователю).
По нему web-service/okpd_check.py находит несуществующий код и восстанавливает его по названию позиции
(в файлах предзащиты у всех кодов был подменён класс: 32.50.13.110 → 02.50.13.110).

Из корня репозитория: python scripts/build_okpd2_reference.py [dataset/ТРУ_24-25.csv]
"""
import csv
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORDS_PER_CODE = 30
csv.field_size_limit(1_000_000)


def stems(text: str) -> set[str]:
    """Основы слов: первые 5 букв слов от 4 букв — грубо, но без лемматизатора и одинаково с okpd_check."""
    return {w[:5] for w in re.findall(r"[а-яёa-z]{4,}", text.lower().replace("ё", "е"))}


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dataset" / "ТРУ_24-25.csv"
    words: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, Counter] = defaultdict(Counter)
    with src.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            code = (row.get("okpd2_code") or "").strip()
            name = " ".join((row.get("product_name") or "").split())
            if code:
                words[code].update(stems(name))
                if 3 <= len(name) <= 120:
                    names[code][name[0].upper() + name[1:]] += 1
    ref = {code: [w for w, _ in c.most_common(WORDS_PER_CODE)] for code, c in sorted(words.items())}
    titles = {code: c.most_common(1)[0][0] for code, c in sorted(names.items())}
    out = ROOT / "models" / "okpd2_reference.json.gz"
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump({"source": src.name, "codes": ref, "titles": titles}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{out}: {len(ref)} кодов, {out.stat().st_size / 1024:.0f} КБ")


if __name__ == "__main__":
    main()
