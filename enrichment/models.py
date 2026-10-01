from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Fact:
    """Одно поле карточки с источником и датой получения (требование ФТ-07)."""

    inn: str
    field: str
    value: Any
    source: str
    fetched_at: datetime


@dataclass
class RawResponse:
    inn: str
    source: str
    request_key: str
    status_code: int
    body: Any
    fetched_at: datetime


@dataclass
class SourceResult:
    source: str
    inn: str
    facts: list[Fact] = field(default_factory=list)
    raws: list[RawResponse] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    # данные, которые источник передаёт следующим шагам (токен карточки ПБ, id в ГИР БО)
    context: dict[str, Any] = field(default_factory=dict)

    def add(self, field_name: str, value: Any, fetched_at: datetime) -> None:
        if value is None or value == "" or value == []:
            return
        self.facts.append(Fact(self.inn, field_name, value, self.source, fetched_at))
