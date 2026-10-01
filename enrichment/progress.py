"""Прогресс поштучного прогона (шаг 5: ГИР БО, РНП) — страница /progress и JSON /api/progress.

Источник цифр — enrichment_runs: batch пишет туда статус каждого ИНН по каждому источнику.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from . import storage

router = APIRouter(tags=["Служебное"])

TRACKED = {"bo": "ГИР БО (финансы, ОКВЭД, статус)", "rnp": "РНП ЕИС",
           "contacts": "Контакты (контракты ЕИС: телефон, почта)",
           "egrul": "ЕГРЮЛ (статус)", "pb": "Прозрачный бизнес"}
RATE_WINDOW = timedelta(minutes=10)
STALE_AFTER = timedelta(minutes=5)
PAGE = (Path(__file__).parent / "progress.html").read_text(encoding="utf-8")


def _aware(ts: datetime | None) -> datetime | None:
    if ts is not None and ts.tzinfo is None:  # SQLite отдаёт naive, пишем UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def compute(engine) -> dict:
    runs = storage.enrichment_runs
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        # всего ИНН поставщиков = все, по кому загружались выгрузки ФНС (шаги 1–4)
        total = conn.execute(select(func.count(func.distinct(runs.c.inn)))
                             .where(runs.c.source == "fns_rsmp")).scalar() or 0
        by_status = conn.execute(select(runs.c.source, runs.c.status, func.count())
                                 .where(runs.c.source.in_(TRACKED)).group_by(runs.c.source, runs.c.status)).all()
        recent = {src: (n, _aware(first)) for src, n, first in conn.execute(
            select(runs.c.source, func.count(), func.min(runs.c.updated_at))
            .where(runs.c.source.in_(TRACKED), runs.c.updated_at >= now - RATE_WINDOW)
            .group_by(runs.c.source)).all()}
        last = dict(conn.execute(select(runs.c.source, func.max(runs.c.updated_at))
                                 .where(runs.c.source.in_(TRACKED)).group_by(runs.c.source)).all())
        found = conn.execute(select(func.count()).select_from(storage.company_facts).where(
            storage.company_facts.c.source == "contacts", storage.company_facts.c.field == "contact_phones")).scalar()
        found_email = conn.execute(select(func.count()).select_from(storage.company_facts).where(
            storage.company_facts.c.source == "contacts", storage.company_facts.c.field == "contact_emails")).scalar()
        errors = conn.execute(select(runs.c.source, runs.c.inn, runs.c.status, runs.c.error, runs.c.updated_at)
                              .where(runs.c.source.in_(TRACKED), runs.c.status != "ok")
                              .order_by(runs.c.updated_at.desc()).limit(30)).all()

    sources = []
    for src, name in TRACKED.items():
        counts = {st: n for s, st, n in by_status if s == src}
        done = sum(counts.values())
        # скорость за последние 10 мин, а если прогон идёт меньше — за фактическое время работы
        n_recent, first = recent.get(src, (0, None))
        span_min = max((now - first).total_seconds() / 60, 1.0) if first else 1.0
        per_min = n_recent / span_min
        # ночной прогон контактов идёт только по ИНН с номером закупки ЕИС — считаем от них
        target = total
        if src == "contacts":
            from .api import _reqnums
            target = len(_reqnums()) or total
        # ЕГРЮЛ и ПБ в шаг 5 не входят: показываем, только если по ним реально идёт прогон,
        # а не единичные запросы из API (меньше 10 записей за 10 минут)
        if (src in ("egrul", "pb") and n_recent < 10) or (src == "contacts" and not done):
            continue  # в шаг 5 не входят — показываем, только пока их кто-то гоняет
        remaining = max(target - done, 0)
        last_ts = _aware(last.get(src))
        sources.append({
            "source": src, "name": name, "total": target, "done": done, "remaining": remaining,
            "ok": counts.get("ok", 0), "error": counts.get("error", 0), "captcha": counts.get("captcha", 0),
            "percent": round(min(done / target, 1) * 100, 1) if target else 0.0,
            "per_min": round(per_min, 1),
            "eta_min": round(remaining / per_min) if per_min else None,
            "last_at": last_ts.isoformat() if last_ts else None,
            "idle_s": int((now - last_ts).total_seconds()) if last_ts else None,
            "running": bool(last_ts and now - last_ts < STALE_AFTER),
            "found": {"с телефоном": found, "с почтой": found_email} if src == "contacts" else None,
        })
    return {
        "now": now.isoformat(), "total_inns": total, "sources": sources,
        "recent_errors": [{"source": s, "inn": i, "status": st, "error": (e or "")[:200],
                           "at": _aware(t).isoformat()} for s, i, st, e, t in errors
                          if s in {x["source"] for x in sources}],
    }


@router.get("/api/progress", summary="Прогресс поштучного прогона (JSON)")
def api_progress():
    from .api import state  # engine создаётся в lifespan приложения
    return compute(state["engine"])


@router.get("/progress", response_class=HTMLResponse, include_in_schema=False)
def page():
    return PAGE
