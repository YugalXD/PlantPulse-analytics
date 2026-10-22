"""Two-parameter Weibull fit by median-rank regression (Benard's approximation).

Phase 3 reliability (TECHNICAL_DESIGN §6.4): fitted only with >= 5 failures; planners decide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

MIN_FAILURES = 5


@dataclass(frozen=True)
class WeibullFit:
    n: int
    beta: float | None
    eta: float | None
    r_squared: float | None
    interpretation: str


def fit(times: list[float]) -> WeibullFit:
    """times: times between failures (days), all > 0."""
    xs = sorted(t for t in times if t > 0)
    n = len(xs)
    if n < MIN_FAILURES:
        return WeibullFit(n, None, None, None, f"Insufficient data ({n} failures, {MIN_FAILURES} needed)")

    # x = ln t, y = ln(-ln(1 - F)), F = (i - 0.3) / (n + 0.4)
    px = [math.log(t) for t in xs]
    py = [math.log(-math.log(1 - (i - 0.3) / (n + 0.4))) for i in range(1, n + 1)]
    mx, my = sum(px) / n, sum(py) / n
    sxx = sum((x - mx) ** 2 for x in px)
    if sxx == 0:
        return WeibullFit(n, None, None, None, "All failures at the same interval: cannot fit")
    sxy = sum((x - mx) * (y - my) for x, y in zip(px, py))
    beta = sxy / sxx
    intercept = my - beta * mx
    eta = math.exp(-intercept / beta)
    ss_tot = sum((y - my) ** 2 for y in py)
    ss_res = sum((y - (beta * x + intercept)) ** 2 for x, y in zip(px, py))
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0

    if beta < 0.95:
        meaning = "early-life / random-start failures: time-based PM will not help much; check installation and quality"
    elif beta <= 1.05:
        meaning = "random failures: time-based PM will not help; use condition monitoring"
    else:
        meaning = f"wear-out (β {beta:.2f}): time-based PM helps; characteristic life about {eta:.0f} days"
    return WeibullFit(n, round(beta, 3), round(eta, 1), round(r2, 3), meaning)


def intervals(failure_days: list[list[float]]) -> list[float]:
    """Days between consecutive failures, per equipment, pooled."""
    out: list[float] = []
    for days in failure_days:
        s = sorted(days)
        out.extend(b - a for a, b in pairwise(s) if b - a > 0)
    return out


def probability_of_failure(beta: float, eta: float, age_days: float, horizon_days: float = 30.0) -> float:
    """P(fail within horizon | survived age) = 1 - R(age + h) / R(age), R(t) = exp(-(t/eta)^beta)."""
    if beta <= 0 or eta <= 0:
        return 0.0
    age = max(0.0, age_days)
    return 1.0 - math.exp((age / eta) ** beta - ((age + horizon_days) / eta) ** beta)
