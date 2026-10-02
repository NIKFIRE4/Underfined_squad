"""Shared, dependency-free contract for the recommendation and enrichment teams."""
from dataclasses import dataclass, field


@dataclass
class Lot:
    lot_id: str
    notice: dict[str, str]
    items: list[dict[str, str]]


@dataclass
class Candidate:
    supplier_name: str
    supplier_inn: str = ""
    supplier_kpp: str = ""
    score: float = 0
    role: str = "Не определена"
    status: str = "Требует проверки"
    region: str = ""
    is_smp: bool | None = None
    is_new: bool = False
    reasons: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    enrichment_status: str = "Не обогащено"
    # Разбор оценки модели для интерфейса: {"p_win", "score", "factors": [{feature, title, group, value, phi}]}.
    # В CSV не выгружается.
    explanation: dict = field(default_factory=dict)
