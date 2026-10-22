"""Failure chance in the next 30 days from the type-level Weibull fit of the equipment's failure modes."""

from __future__ import annotations

from datetime import date

from app.core.model import EquipmentFacts
from app.core.weibull import probability_of_failure

HORIZON_DAYS = 30.0


def predict(facts: EquipmentFacts, today: date, fits: dict[str, tuple[float | None, float | None]]) -> dict[str, object] | None:
    """fits: failure mode -> (beta, eta_days) for the equipment's type. Uses the worst mode seen on this equipment."""
    best: dict[str, object] | None = None
    for mode, (beta, eta) in fits.items():
        if beta is None or eta is None:
            continue
        last = max((b.failure_at.date() for b in facts.breakdowns if b.mode == mode), default=None)
        if last is None:
            continue  # this equipment never failed this way in the last year: no basis for its age
        age = (today - last).days
        p = probability_of_failure(beta, eta, age, HORIZON_DAYS)
        if best is None or p > float(best["probability"]):
            best = {"probability": round(p, 3), "mode": mode, "beta": beta, "eta_days": eta, "days_since_failure": age,
                    "horizon_days": HORIZON_DAYS,
                    "explanation": f"{mode}: last failure {age} days ago; type-level Weibull β {beta:.2f}, η {eta:.0f} days → "
                                   f"{p * 100:.0f} % chance of another in the next {HORIZON_DAYS:.0f} days"}
    return best
