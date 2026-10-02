"""Новые компании по ОКПД2 лота (ФТ-06) и роль контрагента (ТЗ, раздел 6.4).

Каналы поиска новых компаний (все без поштучных запросов, по загруженным снимкам):
  1. реестры производителей и правообладателей — registry_items (РРПП ПП 719, реестр ПО): ОКПД2 продукции;
  2. продукция из реестра МСП — pool_codes kind=product: ОКПД2, заявленный самой компанией;
  3. ОКВЭД из реестра МСП — pool_codes kind=okved_main/okved: «как правило, соответствие
     устанавливается для первых четырёх знаков» ОКПД2 и ОКВЭД2 (введение к ОК 034-2014).

Контентный скор 0–100 (ТЗ 6.3: кандидаты без истории) — максимум по каналам плюс бонусы;
у каждого кандидата — доказательства с источником.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine

from . import storage

# Вес совпадения ОКПД2 по глубине: полный код, 3 уровня (XX.XX.XX), 2 уровня (XX.XX), класс (XX)
DEPTH_SCORE = {4: 60, 3: 50, 2: 40, 1: 20}
CHANNEL_PENALTY = {"registry": 0, "product": -5}
OKVED_SCORE = {("okved_main", 2): 35, ("okved", 2): 20, ("okved_main", 1): 20, ("okved", 1): 8}
REGION_BONUS = 10
MULTI_CHANNEL_BONUS = 10

REGISTRY_NAMES = {"gisp": "реестре промышленной продукции (ПП 719)", "software": "реестре российского ПО"}


def okpd2_levels(code: str) -> list[str]:
    """'32.50.13.190' → ['32.50.13.190', '32.50.13', '32.50', '32'] (без дублей для коротких кодов)."""
    parts = code.strip().split(".")
    levels = [".".join(parts[:3]) + ("." + parts[3] if len(parts) > 3 else ""), ".".join(parts[:3]),
              ".".join(parts[:2]), parts[0]]
    out = []
    for lv in levels:
        if lv and lv not in out:
            out.append(lv)
    return out


def _depth(code: str) -> int:
    return min(len(code.split(".")), 4)


def _match_depth(candidate_code: str, lot_codes: list[str]) -> int:
    """Глубина совпадения кода компании с любым кодом лота (0 — не совпадает)."""
    best = 0
    for lot in lot_codes:
        for lv in okpd2_levels(lot):
            if candidate_code == lv or candidate_code.startswith(lv + "."):
                best = max(best, _depth(lv))
                break
    return best


@dataclass
class Candidate:
    inn: str
    score: float = 0.0
    name: str | None = None
    region_code: str | None = None
    is_smp: bool | None = None
    evidence: list[dict] = field(default_factory=list)  # {text, source, channel, score, fetched_at}
    channels: set[str] = field(default_factory=set)
    okved_main: str | None = None
    products: list[dict] = field(default_factory=list)
    registry_codes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def as_company(self) -> dict:
        """Вход для classify_role."""
        return {"okved_main": self.okved_main, "products": self.products,
                "gisp_okpd2": [{"okpd2": c} for c in self.registry_codes.get("gisp", [])],
                "software_okpd2": [{"okpd2": c} for c in self.registry_codes.get("software", [])]}

    def hit(self, channel: str, score: float, text: str, source: str, fetched_at=None) -> None:
        self.channels.add(channel)
        self.score = max(self.score, score)
        self.evidence.append({"text": text, "source": source, "channel": channel, "score": score,
                              "fetched_at": fetched_at})


def find_new_companies(engine: Engine, okpd2: list[str], *, regions: set[str] | None = None,
                       exclude: set[str] = frozenset(), only_smp: bool = False,
                       limit: int = 50) -> list[Candidate]:
    """Кандидаты по ОКПД2 лота из снимков реестров. `exclude` — ИНН с историей (они не «новые»)."""
    okpd2 = [c for c in okpd2 if c]
    if not okpd2:
        return []
    levels = sorted({lv for c in okpd2 for lv in okpd2_levels(c)})
    okved_prefixes = sorted({lv for c in okpd2 for lv in okpd2_levels(c)[-2:]})  # XX.XX и XX
    cands: dict[str, Candidate] = {}

    def cand(inn: str) -> Candidate:
        return cands.setdefault(inn, Candidate(inn))

    def prefix_filter(col, prefixes):
        return or_(*[or_(col == p, col.like(p + ".%")) for p in prefixes])

    ri, pc, pool = storage.registry_items, storage.pool_codes, storage.pool_companies
    with engine.connect() as conn:
        # 1. реестры производителей и правообладателей
        for r in conn.execute(select(ri).where(prefix_filter(ri.c.okpd2, levels))).mappings():
            d = _match_depth(r["okpd2"], okpd2)
            if not d:
                continue
            c = cand(r["inn"])
            c.name = c.name or r["org_name"]
            c.registry_codes[r["registry"]].append(r["okpd2"])
            c.hit("registry", DEPTH_SCORE[d] + CHANNEL_PENALTY["registry"],
                  f"В {REGISTRY_NAMES.get(r['registry'], r['registry'])}: {r['items_count']} поз. "
                  f"ОКПД2 {r['okpd2']}" + (f" («{r['sample'][:80]}»)" if r["sample"] else ""),
                  r["source"], r["fetched_at"])
        # 2–3. реестр МСП: продукция и ОКВЭД
        for r in conn.execute(select(pc).where(or_(
            (pc.c.kind == "product") & prefix_filter(pc.c.code, levels),
            (pc.c.kind != "product") & prefix_filter(pc.c.code, okved_prefixes),
        ))).mappings():
            if r["kind"] == "product":
                d = _match_depth(r["code"], okpd2)
                if d:
                    cand(r["inn"]).hit("product", DEPTH_SCORE[d] + CHANNEL_PENALTY["product"],
                                       f"Заявляет в реестре МСП продукцию ОКПД2 {r['code']}", "fns_rsmp")
            else:
                d = min(_match_depth(r["code"], okpd2), 2)
                if d:
                    main = r["kind"] == "okved_main"
                    cand(r["inn"]).hit("okved", OKVED_SCORE[(r["kind"], d)],
                                       f"{'Основной' if main else 'Дополнительный'} ОКВЭД {r['code']} "
                                       f"соответствует ОКПД2 лота", "fns_rsmp")
        # данные пула для найденных
        inns = [i for i in cands if i not in exclude]
        info = {}
        for i in range(0, len(inns), 5000):
            for r in conn.execute(select(pool).where(pool.c.inn.in_(inns[i:i + 5000]))).mappings():
                info[r["inn"]] = r

    out = []
    for inn in inns:
        c = cands[inn]
        p = info.get(inn)
        if p is not None and p["pool_status"] in ("excluded", "supplier"):
            continue  # неактивные по выгрузкам ФНС, РНП или уже поставщики (pool-activity)
        if p is not None:
            if p["pool_status"] == "unverified" and p["pool_reason"]:
                c.evidence.append({"text": f"Активность: {p['pool_reason']}", "source": "fns_rsmp",
                                   "channel": "activity", "score": 0, "fetched_at": p["fetched_at"]})
            if p["pool_tier"] == "strong":
                c.score += 5
            c.name = p["name_short"] or p["name_full"] or c.name
            c.region_code, c.is_smp = p["region_code"], True
            c.okved_main, c.products = p["okved_main"], p["products"] or []
            for e in c.evidence:
                e["fetched_at"] = e["fetched_at"] or p["fetched_at"]
        else:
            c.region_code = inn[:2]
        if only_smp and not c.is_smp:
            continue
        if regions and c.region_code not in regions and "registry" not in c.channels:
            continue  # производителей из реестров берём из любого региона, остальных — только из своего
        if regions and c.region_code in regions:
            c.score += REGION_BONUS
        if len(c.channels) > 1:
            c.score += MULTI_CHANNEL_BONUS
        c.score = min(100.0, c.score)
        c.evidence.sort(key=lambda e: -e["score"])
        out.append(c)
    # при равном скоре: больше каналов, затем есть название, затем больше позиций в реестрах
    out.sort(key=lambda c: (-c.score, -len(c.channels), c.name is None,
                            -sum(len(v) for v in c.registry_codes.values()), c.inn))
    return out[:limit]


# --- Роль (ТЗ 6.4) ---

MANUFACTURING = range(10, 33)  # раздел C ОКВЭД2 без 33 «ремонт и монтаж»: по ТЗ это исполнитель
SERVICE_CLASSES = {33, 41, 42, 43, 49, 52, 56, 62, 63, 68, 69, 70, 71, 72, 73, 74, 77, 78, 80, 81, 82,
                   85, 86, 87, 88, 93, 95, 96}


# «Широкий набор разных ОКПД2 в истории» (ТЗ 6.4). Медиана — 1 код, 90-й перцентиль — 12, 99-й — 113.
WIDE_IN_CLASS = 10          # разных кодов в классе лота
WIDE_TOTAL, WIDE_CLASSES = 30, 5
MANY_CUSTOMERS = 5


def _history(company: dict, lot_classes: set) -> tuple[bool, list[str]]:
    """(широкий ассортимент по истории, доказательства)."""
    by_class = company.get("hist_class_codes") or {}
    total, classes = company.get("hist_okpd2_codes") or 0, company.get("hist_okpd2_classes") or 0
    customers = company.get("hist_customers") or 0
    ev = []
    in_class = [(c, by_class.get(f"{c:02d}", 0)) for c in lot_classes if c is not None]
    wide_class = [(c, n) for c, n in in_class if n >= WIDE_IN_CLASS]
    for c, n in wide_class:
        ev.append(f"Участвовал в лотах с {n} разными кодами ОКПД2 класса {c:02d}")
    wide_total = total >= WIDE_TOTAL and classes >= WIDE_CLASSES
    if wide_total and not wide_class:
        ev.append(f"В истории {total} разных кодов ОКПД2 из {classes} классов")
    if (wide_class or wide_total) and customers >= MANY_CUSTOMERS:
        ev.append(f"Поставлял {customers} разным заказчикам")
    return bool(wide_class or wide_total), ev


def _supplier_history_evidence(company: dict) -> list[str]:
    lots, wins = company.get("hist_lots"), company.get("hist_wins")
    return [f"В истории закупок СПб: {lots} лотов, {wins} побед"] if lots else []


def _cls(code: str | None) -> int | None:
    try:
        return int((code or "").split(".")[0])
    except ValueError:
        return None


def classify_role(company: dict, lot_okpd2: list[str] | None = None) -> dict:
    """{value, label, confidence, evidence}. `company` — строка витрины companies или факты карточки:
    okved_main, gisp_okpd2, software_okpd2, products, hist_* (история закупок).
    Уверенность по ТЗ 6.4: high — совпали правила по ОКВЭД и по реестру/истории; medium — одно правило;
    low — роль выведена только из истории закупок или по нетипичному ОКВЭД."""
    main = company.get("okved_main")
    main_cls = _cls(main)
    lot_okpd2 = lot_okpd2 or []
    lot_classes = {_cls(c) for c in lot_okpd2}

    reg_codes = [i["okpd2"] for i in company.get("gisp_okpd2") or []]
    sw_codes = [i["okpd2"] for i in company.get("software_okpd2") or []]
    prod_codes = [p["okpd2"] for p in company.get("products") or [] if p.get("okpd2")]

    def matches(codes):
        return [c for c in codes if not lot_okpd2 or _match_depth(c, lot_okpd2) >= 2]

    reg_hit, sw_hit, prod_hit = matches(reg_codes), matches(sw_codes), matches(prod_codes)
    evidence = []
    if main:
        evidence.append(f"Основной ОКВЭД {main}" + (f" — {company['okved_main_name']}" if company.get("okved_main_name") else ""))

    rule_okved = main_cls in MANUFACTURING and (not lot_classes or main_cls in lot_classes)
    rule_registry = bool(reg_hit or sw_hit or prod_hit)
    if rule_okved or rule_registry:
        if reg_hit:
            evidence.append(f"Найден в реестре промышленной продукции (ПП 719): ОКПД2 {', '.join(reg_hit[:3])}")
        if sw_hit:
            evidence.append(f"Правообладатель ПО в реестре Минцифры: ОКПД2 {', '.join(sw_hit[:3])}")
        if prod_hit:
            evidence.append(f"Заявляет производство продукции ОКПД2 {', '.join(prod_hit[:3])} (реестр МСП)")
        conf = "high" if rule_okved and rule_registry else "medium"
        label = "Правообладатель" if sw_hit and not reg_hit and not rule_okved else "Производитель"
        return {"value": "manufacturer", "label": label, "confidence": conf, "evidence": evidence}
    wide, hist_ev = _history(company, lot_classes)
    if main_cls == 46:
        return {"value": "distributor", "label": "Дистрибьютор", "confidence": "high" if wide else "medium",
                "evidence": evidence + hist_ev}
    if main_cls == 47 or main_cls in SERVICE_CLASSES:
        return {"value": "supplier", "label": "Поставщик-исполнитель", "confidence": "medium",
                "evidence": evidence + _supplier_history_evidence(company)}
    if wide:
        # ОКВЭД не торговый и не сервисный (или неизвестен), но ассортимент в истории широкий
        return {"value": "distributor", "label": "Дистрибьютор", "confidence": "medium" if main else "low",
                "evidence": evidence + hist_ev}
    if main:
        return {"value": "supplier", "label": "Поставщик-исполнитель", "confidence": "low",
                "evidence": evidence + _supplier_history_evidence(company)}
    if company.get("hist_lots"):
        return {"value": "supplier", "label": "Поставщик-исполнитель", "confidence": "low",
                "evidence": _supplier_history_evidence(company)}
    return {"value": "unknown", "label": "Не определена", "confidence": "low", "evidence": []}
