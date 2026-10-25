"""Plain data the analytics work on, loaded by app.repository from the Laravel tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Defaults mirror App\Models\Setting::DEFAULTS in the Laravel app.
DEFAULT_WEIGHTS: dict[str, int] = {
    "breakdowns_90d": 25,
    "downtime_90d": 15,
    "repeat_failures": 10,
    "pm_overdue": 15,
    "hours_since_pm": 10,
    "open_defects": 15,
    "cm_alarm": 10,
}

CRITICALITY_SCALE = {"A": 1.0, "B": 0.8, "C": 0.6}

# SAP cycle units -> days per unit (hours handled separately).
CYCLE_UNIT_DAYS = {"D": 1, "TAG": 1, "DAY": 1, "W": 7, "WK": 7, "WOC": 7, "MON": 30.4, "M": 30.4, "YR": 365, "Y": 365, "JHR": 365}


@dataclass(frozen=True)
class Breakdown:
    id: int
    failure_at: datetime
    handback_at: datetime | None
    downtime_h: float | None
    is_repeat: bool
    object_part: str | None
    damage: str | None
    days_since_pm: float | None
    has_rca: bool = False
    source: str = "APP"  # APP = breakdown log; PERMIT = corrective Eclipse permit before the cut-over (no damage code)

    @property
    def mode(self) -> str | None:
        if self.source == "PERMIT":
            return f"{self.object_part or '*'}/*"
        if not self.object_part or not self.damage:
            return None
        return f"{self.object_part}/{self.damage}"


@dataclass(frozen=True)
class PlanCall:
    plan_date: date
    completion_date: date | None
    cycle_value: float | None
    cycle_unit: str | None

    @property
    def cycle_days(self) -> float | None:
        if self.cycle_value is None or not self.cycle_unit:
            return None
        unit = self.cycle_unit.upper()
        if unit in ("H", "HR", "STD"):
            return None  # running-hour based; handled via hours_cycle
        per = CYCLE_UNIT_DAYS.get(unit)
        return None if per is None else float(self.cycle_value) * per

    @property
    def hours_cycle(self) -> float | None:
        if self.cycle_value is None or not self.cycle_unit:
            return None
        return float(self.cycle_value) if self.cycle_unit.upper() in ("H", "HR", "STD") else None


@dataclass(frozen=True)
class Defect:
    severity: int
    age_days: float
    est_hours: float | None


@dataclass(frozen=True)
class CmPoint:
    name: str
    ratio: float | None  # latest value / alarm limit (inverted for LOW points)
    level: str  # NORMAL / ALARM / TRIP


@dataclass
class EquipmentFacts:
    id: int
    plant_id: int
    tag_no: str
    name: str
    type_id: int | None
    criticality: str
    breakdowns: list[Breakdown] = field(default_factory=list)  # last 365 days, not rejected
    plan_calls: list[PlanCall] = field(default_factory=list)
    last_pm: date | None = None
    defects: list[Defect] = field(default_factory=list)  # outstanding
    cm_points: list[CmPoint] = field(default_factory=list)
    running_hours: dict[date, float] = field(default_factory=dict)  # month start -> hours
    first_seen: date | None = None  # earliest PM / running-hours record (history length)


@dataclass(frozen=True)
class PlantSettings:
    weights: dict[str, int]
    rca_threshold_h: float = 4.0
    repeat_window_days: int = 30
    failure_history_until: date | None = None  # permits are the failures before this date, the breakdown log from it


@dataclass(frozen=True)
class Factor:
    key: str
    label: str
    value: float | str | None
    normalised: float
    weight: int
    contribution: float
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "normalised": round(self.normalised, 3),
            "weight": self.weight,
            "contribution": round(self.contribution, 2),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Health:
    score: float
    band: str
    risk: float
    factors: list[Factor]

    def factor(self, key: str) -> Factor:
        return next(f for f in self.factors if f.key == key)


@dataclass(frozen=True)
class Recommendation:
    kind: str
    title: str
    rationale: str
    priority: int
    evidence: dict[str, object] = field(default_factory=dict)
