from models import Candidate, Lot

READY = False


def enrich(lot: Lot, candidates: list[Candidate]) -> list[Candidate]:
    """Enrich existing candidates; optionally add companies from open sources.

    Each source: {field, source, url, checked_at} (checked_at is ISO 8601).
    Apply registry filters, SMP restrictions and role evidence here.
    Use explicit request timeouts/cache in your implementation.
    Set READY = True only after replacing this function.
    """
    raise NotImplementedError("Поиск и обогащение ещё не подключены")
