"""Health score v1 (TECHNICAL_DESIGN §6.3): explainable, rule-based, 0-100 with a band."""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from app.core.model import CRITICALITY_SCALE, EquipmentFacts, Factor, Health, PlantSettings

LABELS = {
    "breakdowns_90d": "Breakdowns (90 days)",
    "downtime_90d": "Downtime (90 days)",
    "repeat_failures": "Repeat failures (90 days)",
    "pm_overdue": "PM overdue",
    "hours_since_pm": "Running hours since last PM",
    "open_defects": "Open defects",
    "cm_alarm": "Condition monitoring",
}

SEVERITY_POINTS = {1: 1, 2: 2, 3: 4, 4: 8}
DEFAULT_CYCLE_DAYS = 30.0
DOWNTIME_BASELINE_H = 8.0


def band(score: float) -> str:
    if score >= 80:
        return "GOOD"
    if score >= 60:
        return "WATCH"
    if score >= 40:
        return "POOR"
    return "CRITICAL"


def recent(facts: EquipmentFacts, today: date, days: int):
    since = today - timedelta(days=days)
    return [b for b in facts.breakdowns if b.failure_at.date() >= since]


def downtime_90d(facts: EquipmentFacts, today: date) -> float:
    return sum(b.downtime_h or 0.0 for b in recent(facts, today, 90))


def type_baselines(all_facts: list[EquipmentFacts], today: date) -> dict[int | None, float]:
    """Median 90-day downtime of equipment of the same type that had downtime (else 8 h)."""
    by_type: dict[int | None, list[float]] = {}
    for f in all_facts:
        dt = downtime_90d(f, today)
        if dt > 0:
            by_type.setdefault(f.type_id, []).append(dt)
    return {t: max(median(v), 1.0) for t, v in by_type.items()}


def pm_cycle_days(facts: EquipmentFacts) -> float | None:
    cycles = [c.cycle_days for c in facts.plan_calls if c.cycle_days]
    return min(cycles) if cycles else None


def _factor(key: str, value, normalised: float, weights: dict[str, int], detail: str) -> Factor:
    normalised = max(0.0, min(1.0, normalised))
    weight = int(weights.get(key, 0))
    return Factor(key, LABELS[key], value, normalised, weight, weight * normalised, detail)


def factors(facts: EquipmentFacts, settings: PlantSettings, today: date, baseline_h: float | None) -> list[Factor]:
    w = settings.weights
    out: list[Factor] = []

    bd90 = recent(facts, today, 90)
    n = len(bd90)
    out.append(_factor("breakdowns_90d", n, n / 3, w, f"{n} breakdown(s); 3 or more counts in full"))

    dt = downtime_90d(facts, today)
    base = baseline_h or DOWNTIME_BASELINE_H
    out.append(_factor("downtime_90d", round(dt, 1), dt / (2 * base), w,
                       f"{dt:.1f} h vs {base:.1f} h typical for this equipment type (full at twice typical)"))

    repeats = [b for b in bd90 if b.is_repeat]
    out.append(_factor("repeat_failures", len(repeats), 1.0 if repeats else 0.0, w,
                       f"{len(repeats)} repeat failure(s)" if repeats else "No repeat failures"))

    # PM overdue: worst open SAP plan call past its date, relative to the cycle.
    overdue = [(today - c.plan_date).days / (c.cycle_days or DEFAULT_CYCLE_DAYS)
               for c in facts.plan_calls if c.completion_date is None and c.plan_date < today]
    worst_days = max(((today - c.plan_date).days for c in facts.plan_calls if c.completion_date is None and c.plan_date < today), default=0)
    out.append(_factor("pm_overdue", worst_days, max(overdue, default=0.0), w,
                       f"Oldest open PM call is {worst_days} day(s) past its date" if worst_days else "No overdue SAP PM"))

    out.append(_hours_since_pm(facts, today, w))

    points = sum(SEVERITY_POINTS.get(d.severity, 2) for d in facts.defects)
    by_sev = {s: sum(1 for d in facts.defects if d.severity == s) for s in (4, 3, 2, 1)}
    out.append(_factor("open_defects", points, points / 8, w,
                       f"Open defects — critical {by_sev[4]}, high {by_sev[3]}, medium {by_sev[2]}, low {by_sev[1]}"))

    out.append(_cm(facts, w))
    return out


def _hours_since_pm(facts: EquipmentFacts, today: date, w: dict[str, int]) -> Factor:
    if facts.last_pm is None:
        return _factor("hours_since_pm", None, 0.0, w, "No completed SAP PM on record")

    days_since = (today - facts.last_pm).days
    hours_cycle = next((c.hours_cycle for c in facts.plan_calls if c.hours_cycle), None)
    cycle_days = pm_cycle_days(facts)

    run_since = running_hours_since(facts, facts.last_pm, today)
    if run_since is not None:
        per_day = average_daily_running(facts, today)
        expected = hours_cycle or ((cycle_days or DEFAULT_CYCLE_DAYS) * per_day if per_day else None)
        if expected:
            return _factor("hours_since_pm", round(run_since), run_since / expected, w,
                           f"{run_since:.0f} running h since PM on {facts.last_pm:%d %b} (interval ≈ {expected:.0f} h)")

    cycle = cycle_days or DEFAULT_CYCLE_DAYS
    return _factor("hours_since_pm", days_since, days_since / cycle, w,
                   f"{days_since} day(s) since PM on {facts.last_pm:%d %b} (cycle {cycle:.0f} days; calendar time, no SCADA hours)")


def _cm(facts: EquipmentFacts, w: dict[str, int]) -> Factor:
    scored = [p for p in facts.cm_points if p.ratio is not None or p.level == "TRIP"]
    if not scored:
        return _factor("cm_alarm", None, 0.0, w, "No condition readings")
    worst = max(scored, key=lambda p: (p.level == "TRIP", p.ratio or 0))
    if worst.level == "TRIP":
        return _factor("cm_alarm", worst.ratio, 1.0, w, f"{worst.name} at trip level")
    ratio = worst.ratio or 0.0
    # Nothing below 70 % of the alarm limit, full at the alarm limit.
    return _factor("cm_alarm", round(ratio, 2), (ratio - 0.7) / 0.3, w, f"{worst.name} at {ratio * 100:.0f} % of its alarm limit")


def running_hours_since(facts: EquipmentFacts, since: date, today: date) -> float | None:
    if not facts.running_hours:
        return None
    total = 0.0
    for month, hours in facts.running_hours.items():
        nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        start, end = max(month, since), min(nxt, today)
        if end <= start:
            continue
        total += hours * (end - start).days / (nxt - month).days
    return total


def average_daily_running(facts: EquipmentFacts, today: date) -> float | None:
    months = sorted(facts.running_hours)[-3:]
    if not months:
        return None
    days = sum(((m.replace(day=28) + timedelta(days=4)).replace(day=1) - m).days for m in months)
    return sum(facts.running_hours[m] for m in months) / days if days else None


def score(facts: EquipmentFacts, settings: PlantSettings, today: date, baseline_h: float | None) -> Health:
    fs = factors(facts, settings, today, baseline_h)
    scale = CRITICALITY_SCALE.get(facts.criticality, 0.8)
    risk = sum(f.contribution for f in fs) * scale
    s = round(max(0.0, 100.0 - risk), 1)
    return Health(s, band(s), round(risk, 2), fs)
