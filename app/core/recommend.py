"""Recommendation rules (requirements M10). Each rule explains itself; planners decide."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from statistics import median

from app.core.health import pm_cycle_days, recent
from app.core.model import EquipmentFacts, Health, PlantSettings, Recommendation

CRIT_BONUS = {"A": 15, "B": 5, "C": 0}

OBJECT_PART_TEXT = {
    "BRG": "bearings", "GBX": "gearbox", "CPL": "coupling", "MTR": "motor", "PUL": "pulleys", "IDL": "idlers",
    "BELT": "belt", "SPL": "belt splice", "LIN": "liners", "HAM": "hammers / rings", "CAG": "cage / grinding plates",
    "SCR": "screen rollers", "CHU": "chute / hopper", "SKT": "skirt boards", "SCP": "belt scrapers", "HYD": "hydraulics",
    "BRK": "brake", "SHF": "shaft", "CHN": "chain / sprockets", "WHL": "wheels / LT drive", "BWH": "bucket wheel",
    "SLW": "slew drive", "BOOM": "boom / luffing", "CBL": "cable reel", "NOZ": "spray nozzles", "IMP": "impeller",
    "STR": "structure", "TT": "take-up",
}


def recommend(facts: EquipmentFacts, health: Health, settings: PlantSettings, today: date,
              weibull: dict[str, tuple[float, float]] | None = None) -> list[Recommendation]:
    """weibull: failure mode -> (beta, eta_days) for this equipment's type."""
    bonus = CRIT_BONUS.get(facts.criticality, 0)
    out: list[Recommendation] = []

    pm = health.factor("pm_overdue")
    hrs = health.factor("hours_since_pm")
    if pm.normalised >= 0.5 or hrs.normalised >= 0.9:
        why = pm.detail if pm.normalised >= 0.5 else hrs.detail
        out.append(Recommendation("DO_PM_NEXT_WINDOW", "Take PM at the next release opportunity", why + ".", 60 + bonus,
                                  {"pm_overdue": pm.normalised, "hours_since_pm": hrs.normalised}))

    cm = health.factor("cm_alarm")
    bd90 = recent(facts, today, 90)
    parts = Counter(b.object_part for b in bd90 if b.object_part)
    if cm.normalised >= 0.7:
        out.append(Recommendation("INSPECT", "Inspect: " + cm.detail.split(" at ")[0], cm.detail + " — check before it trips.", 65 + bonus,
                                  {"cm_ratio": cm.value}))
    elif parts and parts.most_common(1)[0][1] >= 2:
        part, count = parts.most_common(1)[0]
        name = OBJECT_PART_TEXT.get(part, part)
        out.append(Recommendation("INSPECT", f"Inspect sub-assembly: {name}", f"{count} breakdowns on the {name} in 90 days.", 60 + bonus,
                                  {"object_part": part, "breakdowns": count}))

    cycle = pm_cycle_days(facts)
    year = recent(facts, today, 365)
    since_pm = [b.days_since_pm for b in year if b.days_since_pm is not None]
    if cycle and len(since_pm) >= 2 and median(since_pm) >= 0.5 * cycle:
        mode = Counter(b.mode for b in year if b.mode).most_common(1)
        wb = weibull.get(mode[0][0]) if (weibull and mode) else None
        extra = f" Weibull β = {wb[0]:.2f} (wear-out) for this failure mode across the type." if wb and wb[0] > 1 else ""
        out.append(Recommendation("INCREASE_PM_FREQ", "Increase PM frequency",
                                  f"{len(since_pm)} failures in 12 months, typically {median(since_pm):.0f} days after a PM (cycle {cycle:.0f} days): "
                                  "failures come late in the PM cycle." + extra, 50 + bonus,
                                  {"failures_12m": len(since_pm), "median_days_after_pm": median(since_pm), "cycle_days": cycle}))

    history_ok = facts.first_seen is not None and facts.first_seen <= today - timedelta(days=365)
    if (cycle and history_ok and not year and facts.criticality != "A"
            and not any(d.severity >= 3 for d in facts.defects) and cm.normalised == 0):
        out.append(Recommendation("DECREASE_PM_FREQ", "Consider a longer PM interval",
                                  f"No breakdowns in 12 months, no serious defects and normal condition (criticality {facts.criticality}, cycle {cycle:.0f} days).",
                                  20, {"cycle_days": cycle}))

    big_old = [d for d in facts.defects if d.severity >= 3 and d.age_days >= 30 and (d.est_hours or 0) >= 8]
    if health.band == "CRITICAL" or big_old:
        why = (f"Health score {health.score:.0f} (critical)." if health.band == "CRITICAL" else "")
        why += (f" {len(big_old)} serious defect(s) older than 30 days need ≥ 8 h each." if big_old else "")
        out.append(Recommendation("ADD_TO_SHUTDOWN", "Add to the next AOH / COH job list", why.strip(), 55 + bonus,
                                  {"score": health.score, "defects": len(big_old)}))

    rca_due = [b for b in bd90 if b.source == "APP" and not b.has_rca and ((b.downtime_h or 0) > settings.rca_threshold_h or b.is_repeat)]
    if rca_due:
        worst = max(rca_due, key=lambda b: (b.downtime_h or 0))
        reason = "repeat failure" if worst.is_repeat else f"{worst.downtime_h:.1f} h downtime (threshold {settings.rca_threshold_h:g} h)"
        out.append(Recommendation("RAISE_RCA", "Raise an RCA", f"Breakdown on {worst.failure_at:%d %b}: {reason}.", 70 + bonus,
                                  {"breakdown_ids": [b.id for b in rca_due]}))

    return out
