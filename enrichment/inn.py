"""Проверка ИНН по контрольной сумме: 10 знаков — юрлицо, 12 — ИП/физлицо."""

_W10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12_1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12_2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _check(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights)) % 11 % 10


def is_valid_inn(inn: str) -> bool:
    if not inn.isdigit() or len(inn) not in (10, 12) or not inn.strip("0"):
        return False  # «0000000000» проходит контрольную сумму, но это заглушка из выгрузки
    d = [int(c) for c in inn]
    if len(inn) == 10:
        return _check(d, _W10) == d[9]
    return _check(d, _W12_1) == d[10] and _check(d, _W12_2) == d[11]


def inn_kind(inn: str) -> str:
    """'ul' — юрлицо, 'ip' — ИП."""
    return "ul" if len(inn) == 10 else "ip"
