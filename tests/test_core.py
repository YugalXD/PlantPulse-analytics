"""Unit tests of the pure analytics (no database)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.health import band, score, type_baselines
from app.core.model import (
    DEFAULT_WEIGHTS,
    Breakdown,
    CmPoint,
    Defect,
    EquipmentFacts,
    PlanCall,
    PlantSettings,
)
from app.core.recommend import recommend
from app.core.weibull import fit, intervals

TODAY = date(2026, 10, 22)
SETTINGS = PlantSettings(weights=dict(DEFAULT_WEIGHTS), rca_threshold_h=4)


def at(days_ago: float) -> datetime:
    return datetime(2026, 10, 22, 6, tzinfo=UTC) - timedelta(days=days_ago)


def bd(i: int, days_ago: float, hours: float, *, repeat=False, part="BRG", damage="WEAR", since_pm=None, rca=False) -> Breakdown:
    return Breakdown(i, at(days_ago), at(days_ago) + timedelta(hours=hours), hours, repeat, part, damage, since_pm, rca)


def healthy(crit="A") -> EquipmentFacts:
    return EquipmentFacts(1, 1, "RG-1A", "Ring Granulator 1A", 7, crit,
                          plan_calls=[PlanCall(TODAY - timedelta(days=10), TODAY - timedelta(days=9), 1, "MON")],
                          last_pm=TODAY - timedelta(days=9))


def test_bands():
    assert [band(s) for s in (95, 80, 79.9, 60, 59, 40, 39.9)] == ["GOOD", "GOOD", "WATCH", "WATCH", "POOR", "POOR", "CRITICAL"]


def test_healthy_equipment_scores_100_and_explains_every_factor():
    f = healthy()
    f.last_pm = TODAY
    h = score(f, SETTINGS, TODAY, None)
    assert h.score == 100.0 and h.band == "GOOD"
    assert [f.key for f in h.factors] == list(DEFAULT_WEIGHTS)
    assert sum(f.weight for f in h.factors) == 100


def test_bad_actor_scores_low_with_contributions():
    f = healthy()
    f.breakdowns = [bd(1, 5, 6, since_pm=4), bd(2, 20, 10, repeat=True, since_pm=19), bd(3, 60, 4)]
    f.defects = [Defect(4, 3, 8), Defect(3, 40, 12)]
    f.cm_points = [CmPoint("DE bearing vibration", 1.1, "ALARM")]
    f.plan_calls = [PlanCall(TODAY - timedelta(days=40), None, 1, "MON")]

    h = score(f, SETTINGS, TODAY, baseline_h=5.0)
    c = {x.key: x.contribution for x in h.factors}
    assert c["breakdowns_90d"] == 25          # 3 breakdowns -> full
    assert c["downtime_90d"] == 15            # 20 h vs 2 x 5 h -> full
    assert c["repeat_failures"] == 10
    assert c["pm_overdue"] == 15              # 40 days late on a ~30 day cycle -> capped
    assert c["open_defects"] == pytest.approx(15 * 12 / 8 if 12 / 8 < 1 else 15)
    assert c["cm_alarm"] == 10
    assert h.band == "CRITICAL" and h.score < 40


def test_criticality_scales_the_risk():
    crit_c, crit_a = healthy("C"), healthy("A")
    crit_c.breakdowns = crit_a.breakdowns = [bd(1, 5, 2), bd(2, 10, 2), bd(3, 15, 2)]
    assert score(crit_c, SETTINGS, TODAY, 10.0).risk == pytest.approx(score(crit_a, SETTINGS, TODAY, 10.0).risk * 0.6, rel=1e-3)


def test_running_hours_since_pm_uses_scada_when_available():
    f = healthy()
    f.last_pm = date(2026, 8, 1)
    f.plan_calls = [PlanCall(date(2026, 8, 1), date(2026, 8, 1), 1, "MON")]
    f.running_hours = {date(2026, 8, 1): 620.0, date(2026, 9, 1): 600.0, date(2026, 10, 1): 620.0}
    h = score(f, SETTINGS, TODAY, None)
    hrs = h.factor("hours_since_pm")
    assert "running h since PM" in hrs.detail and hrs.normalised == 1.0


def test_type_baseline_is_median_of_equipment_with_downtime():
    a, b, c = healthy(), healthy(), healthy()
    a.breakdowns, b.breakdowns = [bd(1, 3, 4)], [bd(2, 3, 12)]
    assert type_baselines([a, b, c], TODAY) == {7: 8.0}


def test_recommendations_explain_themselves():
    f = healthy("A")
    f.plan_calls = [PlanCall(TODAY - timedelta(days=20), None, 1, "MON")]
    f.breakdowns = [bd(1, 10, 6, since_pm=25), bd(2, 50, 2, repeat=True, since_pm=22)]
    f.cm_points = [CmPoint("DE bearing vibration", 0.95, "NORMAL")]
    h = score(f, SETTINGS, TODAY, 4.0)

    recs = {r.kind: r for r in recommend(f, h, SETTINGS, TODAY, {"BRG/WEAR": (2.1, 40.0)})}
    assert h.band == "CRITICAL"
    assert set(recs) == {"DO_PM_NEXT_WINDOW", "INSPECT", "INCREASE_PM_FREQ", "RAISE_RCA", "ADD_TO_SHUTDOWN"}
    assert "20 day(s) past" in recs["DO_PM_NEXT_WINDOW"].rationale
    assert recs["INSPECT"].title == "Inspect: DE bearing vibration"
    assert "β = 2.10" in recs["INCREASE_PM_FREQ"].rationale
    assert recs["RAISE_RCA"].evidence["breakdown_ids"] == [1, 2]


def test_no_rca_advice_once_an_rca_exists():
    f = healthy()
    f.breakdowns = [bd(1, 10, 9, rca=True)]
    recs = recommend(f, score(f, SETTINGS, TODAY, 8.0), SETTINGS, TODAY)
    assert "RAISE_RCA" not in {r.kind for r in recs}


def test_permit_failures_count_but_never_ask_for_an_rca():
    f = healthy()
    f.breakdowns = [Breakdown(-7, at(10), at(9.5), 12.0, True, "BELT", None, None, source="PERMIT")]
    recs = recommend(f, score(f, SETTINGS, TODAY, 8.0), SETTINGS, TODAY)
    assert "RAISE_RCA" not in {r.kind for r in recs}
    assert f.breakdowns[0].mode == "BELT/*"
    assert Breakdown(-8, at(5), None, None, False, None, None, None, source="PERMIT").mode == "*/*"
    assert bd(1, 5, 1, part="BRG", damage=None).mode is None  # app breakdowns still need part and damage


def test_decrease_pm_only_for_long_clean_history_on_non_critical_equipment():
    f = healthy("C")
    f.first_seen = TODAY - timedelta(days=400)
    assert "DECREASE_PM_FREQ" in {r.kind for r in recommend(f, score(f, SETTINGS, TODAY, None), SETTINGS, TODAY)}
    f.criticality = "A"
    assert "DECREASE_PM_FREQ" not in {r.kind for r in recommend(f, score(f, SETTINGS, TODAY, None), SETTINGS, TODAY)}


def test_weibull_needs_five_failures_and_recovers_shape():
    assert fit([10, 20, 30]).beta is None
    wear_out = fit([80, 95, 100, 105, 110, 120, 90, 100])
    assert wear_out.beta and wear_out.beta > 3 and 95 < wear_out.eta < 110
    assert "wear-out" in wear_out.interpretation
    random_like = fit([5, 12, 30, 60, 120, 200, 2, 45, 90, 15])
    assert random_like.beta and random_like.beta < 1.3


def test_intervals_are_per_equipment():
    assert intervals([[0, 10, 30], [5, 6]]) == [10, 20, 1]


def test_failure_probability_grows_with_age_for_wear_out():
    from app.core.weibull import probability_of_failure

    young, old = probability_of_failure(3.0, 100, 10), probability_of_failure(3.0, 100, 90)
    assert 0 < young < old < 1
    # beta = 1 (random): no memory, same chance at any age
    assert probability_of_failure(1.0, 100, 10) == pytest.approx(probability_of_failure(1.0, 100, 200), rel=1e-9)


def test_prediction_uses_the_worst_mode_seen_on_the_equipment():
    from app.core.predict import predict

    f = healthy()
    f.breakdowns = [bd(1, 80, 2, part="BRG", damage="WEAR"), bd(2, 5, 1, part="BELT", damage="BRKN")]
    p = predict(f, TODAY, {"BRG/WEAR": (3.0, 100.0), "BELT/BRKN": (1.2, 400.0), "GBX/WEAR": (2.0, 50.0)})
    assert p is not None and p["mode"] == "BRG/WEAR" and p["days_since_failure"] == 80
    assert "chance of another in the next 30 days" in str(p["explanation"])
    assert predict(healthy(), TODAY, {"BRG/WEAR": (3.0, 100.0)}) is None
