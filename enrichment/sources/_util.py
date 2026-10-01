from datetime import datetime


def ru_date(s: str | None) -> str | None:
    """'22.12.2009' или '01.08.2016 00:00:00' → '2009-12-22'."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def flag(v) -> bool | None:
    n = num(v)
    return None if n is None else n != 0
