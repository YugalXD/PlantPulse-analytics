"""Reads the Laravel tables into app.core.model objects (read-only)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

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
    values = {k: (json.loads(v) if isinstance(v, str) else v) for k, v in rows}
    weights = {**DEFAULT_WEIGHTS, **(values.get("health_weights") or {})}
    return PlantSettings(
        weights={k: int(v) for k, v in weights.items()},
        rca_threshold_h=float(values.get("rca_downtime_threshold_hours", 4)),
        repeat_window_days=int(values.get("repeat_failure_window_days", 30)),
    )


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

    for r in conn.execute(text(
        """select b.id, b.equipment_id, b.failure_at, b.handback_at, b.is_repeat, b.object_part_code, b.damage_code,
                  b.days_since_pm, exists (select 1 from rcas where rcas.breakdown_id = b.id) as has_rca
           from breakdowns b
           where b.plant_id = :p and b.equipment_id = any(:ids) and b.status <> 'REJECTED' and b.failure_at >= :since
           order by b.failure_at"""), {**p, "since": today - timedelta(days=365)}):
        end = r.handback_at or now
        facts[r.equipment_id].breakdowns.append(Breakdown(
            r.id, r.failure_at, r.handback_at, max(0.0, (end - r.failure_at).total_seconds() / 3600), bool(r.is_repeat),
            r.object_part_code, r.damage_code, float(r.days_since_pm) if r.days_since_pm is not None else None, bool(r.has_rca)))

    first_seen: dict[int, date] = {}

    def seen(eid: int, d: date) -> None:
        first_seen[eid] = min(first_seen.get(eid, d), d)

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
           ) x group by equipment_id"""), {**p, "today": today}):
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
    for r in conn.execute(text(
        """select e.equipment_type_id, b.equipment_id, b.object_part_code || '/' || b.damage_code as mode,
                  extract(epoch from b.failure_at) / 86400 as day
           from breakdowns b join equipment e on e.id = b.equipment_id
           where b.plant_id = :p and b.status <> 'REJECTED' and e.equipment_type_id is not null
             and b.object_part_code is not null and b.damage_code is not null"""), {"p": plant_id}):
        groups[(r.equipment_type_id, r.mode)][r.equipment_id].append(float(r.day))
    return {k: list(v.values()) for k, v in groups.items()}
