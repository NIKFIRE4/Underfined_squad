from models import Candidate, Lot

# Load model weights / supplier history once at module level, not for every lot.
READY = False


def recommend(lot: Lot, top_k: int) -> list[Candidate]:
    """Return candidates for one lot. Keep identifiers as strings, score 0..100.

    The historical suppliers dataset belongs to this adapter's model/index.
    No third upload is required from the procurement user.
    Set READY = True only after replacing this function.
    """
    raise NotImplementedError("Рекомендательная модель ещё не подключена")
