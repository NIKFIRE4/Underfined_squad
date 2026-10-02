"""Расширение базы поставщиков вглубь: компании из реестра МСП (СПб/ЛО), которых нет в истории закупок,
очищенные от неактивных, добавляются со статусом «непроверенный».

Критерии (стартовые, обратимые — меняется только pool_status):
  юрлицо — активно, если за 2025 г. уплачены налоги или есть хотя бы один сотрудник (выгрузки ФНС
  paytax и sshr2019); tier strong — от 5 сотрудников, иначе active;
  ИП — ФНС не публикует по ИП налоги и численность, поэтому берём только ИП с лицензиями или
  заявленной продукцией в реестре МСП (tier signal);
  исключаются: уже поставщики (supplier), действующие записи РНП, юрлица без налогов и сотрудников,
  ИП без признаков деятельности — с причиной в pool_reason.
"""

import logging

from sqlalchemy import select
from sqlalchemy.engine import Engine

from . import fns_dumps, storage

log = logging.getLogger("enrichment")

STRONG_EMPLOYEES = 5


def compute(engine: Engine, suppliers: set[str]) -> dict:
    with engine.connect() as conn:
        pool = {r.inn: r for r in conn.execute(select(
            storage.pool_companies.c.inn, storage.pool_companies.c.kind,
            storage.pool_companies.c.licenses_count, storage.pool_companies.c.products)).all()}
        rnp = {r[0] for r in conn.execute(select(storage.rnp_registry.c.inn)
                                          .where(~storage.rnp_registry.c.state.ilike("%исключ%")))}
    emp, tax = {}, {}
    for doc in fns_dumps.iter_docs(fns_dumps.DUMPS_DIR / "sshr2019.zip"):
        if (i := fns_dumps._inn(doc)) in pool:
            emp[i] = fns_dumps._sshr(doc)["employees"]
    for doc in fns_dumps.iter_docs(fns_dumps.DUMPS_DIR / "paytax.zip"):
        if (i := fns_dumps._inn(doc)) in pool:
            tax[i] = fns_dumps._paytax(doc)["taxes_paid"]

    rows, stats = [], {}
    for inn, p in pool.items():
        e, t = emp.get(inn), tax.get(inn)
        status, tier = "excluded", None
        if inn in suppliers:
            status, reason = "supplier", "уже есть в истории закупок"
        elif inn in rnp:
            reason = "в реестре недобросовестных поставщиков"
        elif p.kind == "ul":
            if (t or 0) > 0 or (e or 0) >= 1:
                status = "unverified"
                tier = "strong" if (e or 0) >= STRONG_EMPLOYEES else "active"
                reason = "; ".join(filter(None, [f"налоги за 2025 г.: {t:,.0f} ₽".replace(",", " ") if t else None,
                                                 f"сотрудников: {int(e)}" if e else None]))
            else:
                reason = "нет налогов и сотрудников за 2025 г."
        elif (p.licenses_count or 0) > 0 or p.products:
            status, tier = "unverified", "signal"
            reason = "ИП с лицензией или заявленной продукцией в реестре МСП"
        else:
            reason = "ИП без признаков деятельности в открытых данных"
        rows.append({"inn": inn, "employees_2025": e, "taxes_paid_2025": t, "pool_status": status,
                     "pool_reason": reason, "pool_tier": tier})
        key = f"{status}:{tier}" if tier else status
        stats[key] = stats.get(key, 0) + 1
    storage.update_pool_activity(engine, rows)
    return stats
