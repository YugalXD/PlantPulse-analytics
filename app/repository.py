"""Reads the Laravel tables into app.core.model objects (read-only)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.core.model import (
    DEFAULT_WEIGHTS,
    Breakdown,
    CmPoint,
    Defect,
    EquipmentFacts,
    PlanCall,
    PlantSettings,
)


def plant_settings(conn: Connection, plant_id: int) -> PlantSettings:
    rows = conn.execute(text("select key, value from settings where plant_id = :p"), {"p": plant_id}).all()
    values = dict(rows)  # settings.value is jsonb: the driver already decodes it (a JSON string arrives as str)
    weights = {**DEFAULT_WEIGHTS, **(values.get("health_weights") or {})}
    cut = values.get("failure_history_until")
    return PlantSettings(
        weights={k: int(v) for k, v in weights.items()},
        rca_threshold_h=float(values.get("rca_downtime_threshold_hours", 4)),
        repeat_window_days=int(values.get("repeat_failure_window_days", 30)),
        failure_history_until=date.fromisoformat(cut) if cut else None,
    )


# Permits that were never worked on are no intervention.
_PERMIT_WORKED = "coalesce(e.status, '') not in ('NOT ISSUED', 'DELETED') and e.issued_at is not null"
_MAX_PERMIT_HOURS = 168.0  # a permit open longer than a week says nothing about downtime


def _cut_over(conn: Connection, plant_id: int) -> datetime | None:
    """Start of the cut-over day in the plant's time zone: permits are the failures before it, breakdowns from it."""
    cut = plant_settings(conn, plant_id).failure_history_until
    if cut is None:
        return None
    tz = conn.execute(text("select timezone from plants where id = :p"), {"p": plant_id}).scalar_one() or "Asia/Kolkata"
    return datetime.combine(cut, time(), ZoneInfo(tz))


def load_facts(conn: Connection, plant_id: int, today: date, equipment_ids: list[int] | None = None) -> list[EquipmentFacts]:
    now = datetime.now(UTC)
    sql = "select id, plant_id, tag_no, name, equipment_type_id, criticality from equipment where plant_id = :p"
    params: dict[str, object] = {"p": plant_id}
    if equipment_ids:
        sql += " and id = any(:ids)"
        params["ids"] = equipment_ids

    facts: dict[int, EquipmentFacts] = {}
    for r in conn.execute(text(sql + " order by tag_no"), params):
        facts[r.id] = EquipmentFacts(r.id, r.plant_id, r.tag_no, r.name, r.equipment_type_id, r.criticality)
    if not facts:
        return []
    p = {"p": plant_id, "ids": list(facts)}
    cut = _cut_over(conn, plant_id)
    repeat_days = plant_settings(conn, plant_id).repeat_window_days

    for r in conn.execute(text(
        """select b.id, b.equipment_id, b.failure_at, b.handback_at, b.is_repeat, b.object_part_code, b.damage_code,
                  b.days_since_pm, exists (select 1 from rcas where rcas.breakdown_id = b.id) as has_rca
           from breakdowns b
           where b.plant_id = :p and b.equipment_id = any(:ids) and b.status <> 'REJECTED' and b.failure_at >= :since
             and (cast(:cut as timestamptz) is null or b.failure_at >= :cut)
           order by b.failure_at"""), {**p, "since": today - timedelta(days=365), "cut": cut}):
        end = r.handback_at or now
        facts[r.equipment_id].breakdowns.append(Breakdown(
            r.id, r.failure_at, r.handback_at, max(0.0, (end - r.failure_at).total_seconds() / 3600), bool(r.is_repeat),
            r.object_part_code, r.damage_code, float(r.days_since_pm) if r.days_since_pm is not None else None, bool(r.has_rca)))

    first_seen: dict[int, date] = {}

    def seen(eid: int, d: date) -> None:
        first_seen[eid] = min(first_seen.get(eid, d), d)

    if cut is not None:
        _load_permit_failures(conn, p, facts, today - timedelta(days=365), cut, now, repeat_days)
        for r in conn.execute(text(
            f"""select e.equipment_id, min(e.issued_at)::date as first from eclipse_permits e
               where e.plant_id = :p and e.equipment_id = any(:ids) and {_PERMIT_WORKED} group by e.equipment_id"""), p):
            seen(r.equipment_id, r.first)

    for r in conn.execute(text(
        """select equipment_id, plan_date, completion_date, cycle_value, cycle_unit from sap_plan_calls
           where plant_id = :p and equipment_id = any(:ids) and plan_date >= :since"""), {**p, "since": today - timedelta(days=730)}):
        facts[r.equipment_id].plan_calls.append(PlanCall(
            r.plan_date, r.completion_date, float(r.cycle_value) if r.cycle_value is not None else None, r.cycle_unit))
        seen(r.equipment_id, r.plan_date)

    for r in conn.execute(text(
        """select equipment_id, max(d) as last_pm from (
               select equipment_id, completion_date as d from sap_plan_calls
                where plant_id = :p and equipment_id = any(:ids) and completion_date is not null and completion_date <= :today
               union all
               select equipment_id, coalesce(actual_finish, reference_date) from sap_orders
                where plant_id = :p and equipment_id = any(:ids) and category = 'PM' and is_deleted = false
                  and coalesce(actual_finish, reference_date) <= :today
               union all
               select e.equipment_id, coalesce(e.closed_at, e.issued_at)::date from eclipse_permits e
                where cast(:cut as timestamptz) is not null and e.plant_id = :p and e.equipment_id = any(:ids)
                  and e.work_category = 'PM' and """ + _PERMIT_WORKED + """ and coalesce(e.closed_at, e.issued_at)::date <= :today
           ) x group by equipment_id"""), {**p, "today": today, "cut": cut}):
        facts[r.equipment_id].last_pm = r.last_pm

    for r in conn.execute(text(
        """select equipment_id, severity, extract(epoch from (:now - reported_at)) / 86400 as age, est_hours from defects
           where plant_id = :p and equipment_id = any(:ids) and status in ('OPEN', 'PLANNED', 'IN_PROGRESS')"""), {**p, "now": now}):
        facts[r.equipment_id].defects.append(Defect(int(r.severity), float(r.age), float(r.est_hours) if r.est_hours is not None else None))

    for r in conn.execute(text(
        """select mp.equipment_id, mp.name, mp.direction, mp.alarm_limit, lr.value, lr.level
           from measuring_points mp
           join lateral (select value, level from cm_readings r where r.measuring_point_id = mp.id order by reading_at desc limit 1) lr on true
           where mp.plant_id = :p and mp.equipment_id = any(:ids) and mp.is_active"""), p):
        facts[r.equipment_id].cm_points.append(CmPoint(r.name, _ratio(r.direction, r.alarm_limit, r.value), r.level))

    for r in conn.execute(text(
        """select equipment_id, period_month, hours from running_hours
           where plant_id = :p and equipment_id = any(:ids) and period_month >= :since"""), {**p, "since": today.replace(day=1) - timedelta(days=460)}):
        facts[r.equipment_id].running_hours[r.period_month] = float(r.hours)
        seen(r.equipment_id, r.period_month)

    for eid, d in first_seen.items():
        facts[eid].first_seen = d

    return list(facts.values())


def _load_permit_failures(conn: Connection, p: dict, facts: dict[int, EquipmentFacts], since: date, cut: datetime,
                          now: datetime, repeat_days: int) -> None:
    """Corrective / breakdown permits before the cut-over, as failures (JPL: no other history before SAP, Q90)."""
    last_by_part: dict[tuple[int, str], datetime] = {}
    for r in conn.execute(text(
        f"""select e.id, e.equipment_id, coalesce(e.work_start_at, e.issued_at) as failure_at, e.closed_at, e.object_part_code,
                  (select max(coalesce(pm.closed_at, pm.issued_at)) from eclipse_permits pm
                    where pm.plant_id = e.plant_id and pm.equipment_id = e.equipment_id and pm.work_category = 'PM'
                      and coalesce(pm.status, '') not in ('NOT ISSUED', 'DELETED') and pm.issued_at is not null
                      and coalesce(pm.closed_at, pm.issued_at) < coalesce(e.work_start_at, e.issued_at)) as last_pm_at
           from eclipse_permits e
           where e.plant_id = :p and e.equipment_id = any(:ids) and e.work_category in ('CM', 'BD') and {_PERMIT_WORKED}
             and coalesce(e.work_start_at, e.issued_at) >= :since and coalesce(e.work_start_at, e.issued_at) < :cut
           order by 3"""), {**p, "since": since, "cut": cut}):
        hours = (r.closed_at - r.failure_at).total_seconds() / 3600 if r.closed_at else None
        key = (r.equipment_id, r.object_part_code or "")
        previous = last_by_part.get(key) if r.object_part_code else None
        last_by_part[key] = r.failure_at
        facts[r.equipment_id].breakdowns.append(Breakdown(
            -r.id, r.failure_at, r.closed_at,
            hours if hours is not None and 0 <= hours <= _MAX_PERMIT_HOURS else None,
            previous is not None and (r.failure_at - previous) <= timedelta(days=repeat_days),
            r.object_part_code, None,
            (r.failure_at - r.last_pm_at).total_seconds() / 86400 if r.last_pm_at else None,
            source="PERMIT"))
    for f in facts.values():
        f.breakdowns.sort(key=lambda b: b.failure_at)


def _ratio(direction: str, alarm, value) -> float | None:
    if alarm is None or value is None or float(alarm) == 0:
        return None
    alarm, value = float(alarm), float(value)
    if direction == "LOW":
        return 2.0 if value <= 0 else alarm / value
    return value / alarm


def failure_days_by_type_mode(conn: Connection, plant_id: int) -> dict[tuple[int, str], list[list[float]]]:
    """All non-rejected coded failures, as day numbers per equipment, grouped by (type, object part / damage)."""
    groups: dict[tuple[int, str], dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    cut = _cut_over(conn, plant_id)
    for r in conn.execute(text(
        f"""select e.equipment_type_id, b.equipment_id, b.object_part_code || '/' || b.damage_code as mode,
                  extract(epoch from b.failure_at) / 86400 as day
           from breakdowns b join equipment e on e.id = b.equipment_id
           where b.plant_id = :p and b.status <> 'REJECTED' and e.equipment_type_id is not null
             and b.object_part_code is not null and b.damage_code is not null
             and (cast(:cut as timestamptz) is null or b.failure_at >= :cut)
           union all
           select q.equipment_type_id, e.equipment_id, coalesce(e.object_part_code, '*') || '/*',
                  extract(epoch from coalesce(e.work_start_at, e.issued_at)) / 86400
           from eclipse_permits e join equipment q on q.id = e.equipment_id
           where cast(:cut as timestamptz) is not null and e.plant_id = :p and q.equipment_type_id is not null
             and e.work_category in ('CM', 'BD') and {_PERMIT_WORKED} and coalesce(e.work_start_at, e.issued_at) < :cut"""),
            {"p": plant_id, "cut": cut}):
        groups[(r.equipment_type_id, r.mode)][r.equipment_id].append(float(r.day))
    return {k: list(v.values()) for k, v in groups.items()}
