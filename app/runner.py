"""One analytics run: health snapshots, recommendations and (nightly) Weibull fits for a plant.

Writes only to analytics_runs, health_snapshots, recommendations (system fields) and weibull_results.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, text

from app.core import health as health_mod
from app.core.model import EquipmentFacts, Health, Recommendation
from app.core.predict import predict
from app.core.recommend import recommend
from app.core.weibull import fit, intervals
from app.repository import failure_days_by_type_mode, load_facts, plant_settings

PLANT_TZ = ZoneInfo("Asia/Kolkata")
# A planner's decision is respected for this long before the same advice comes back.
DECISION_QUIET_DAYS = 30


def today_local() -> date:
    return datetime.now(PLANT_TZ).date()


def execute(engine: Engine, run_id: int, plant_id: int, kind: str, equipment_ids: list[int] | None = None) -> dict[str, object]:
    with engine.begin() as conn:
        conn.execute(text("update analytics_runs set status = 'RUNNING', started_at = now(), updated_at = now() where id = :id"), {"id": run_id})

    try:
        with engine.begin() as conn:
            summary = _run(conn, run_id, plant_id, kind, equipment_ids)
            conn.execute(text(
                "update analytics_runs set status = 'DONE', finished_at = now(), rows_written = :n, summary = cast(:s as jsonb), error = null, updated_at = now() where id = :id"),
                {"id": run_id, "n": summary["rows_written"], "s": json.dumps(summary)})
        return summary
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(text(
                "update analytics_runs set status = 'FAILED', finished_at = now(), error = :e, updated_at = now() where id = :id"),
                {"id": run_id, "e": f"{type(exc).__name__}: {exc}"[:4000]})
        raise


def _run(conn: Connection, run_id: int, plant_id: int, kind: str, equipment_ids: list[int] | None) -> dict[str, object]:
    today = today_local()
    settings = plant_settings(conn, plant_id)

    # Type baselines always use the whole plant, even for an incremental run.
    all_facts = load_facts(conn, plant_id, today)
    facts = [f for f in all_facts if not equipment_ids or f.id in equipment_ids]
    baselines = health_mod.type_baselines(all_facts, today)

    weibull = _weibull(conn, run_id, plant_id, today) if kind in ("NIGHTLY", "WEIBULL") else _latest_weibull(conn, plant_id)

    rows = 0
    bands: dict[str, int] = {}
    recs_open = 0
    for f in facts:
        h = health_mod.score(f, settings, today, baselines.get(f.type_id))
        type_weibull = {mode: v for (t, mode), v in weibull.items() if t == f.type_id}
        snapshot_id = _save_snapshot(conn, run_id, f, h, today, predict(f, today, type_weibull))
        rows += 1
        bands[h.band] = bands.get(h.band, 0) + 1
        recs = recommend(f, h, settings, today, type_weibull)
        rows += _save_recommendations(conn, run_id, snapshot_id, f, recs, today)
        recs_open += len(recs)

    return {"kind": kind, "equipment": len(facts), "bands": bands, "recommendations": recs_open,
            "weibull_fits": sum(1 for v in weibull.values() if v[0] is not None), "rows_written": rows, "as_of": today.isoformat()}


def _save_snapshot(conn: Connection, run_id: int, f: EquipmentFacts, h: Health, today: date, prediction: dict | None) -> int:
    return conn.execute(text(
        """insert into health_snapshots (plant_id, equipment_id, analytics_run_id, as_of_date, score, band, risk, factors,
                                            failure_probability_30d, prediction, created_at, updated_at)
           values (:p, :e, :r, :d, :s, :b, :risk, cast(:f as jsonb), :prob, cast(:pred as jsonb), now(), now())
           on conflict (equipment_id, as_of_date) do update
              set analytics_run_id = excluded.analytics_run_id, score = excluded.score, band = excluded.band,
                  risk = excluded.risk, factors = excluded.factors, failure_probability_30d = excluded.failure_probability_30d,
                  prediction = excluded.prediction, updated_at = now()
           returning id"""),
        {"p": f.plant_id, "e": f.id, "r": run_id, "d": today, "s": h.score, "b": h.band, "risk": h.risk,
         "f": json.dumps([x.as_dict() for x in h.factors]),
         "prob": prediction["probability"] if prediction else None, "pred": json.dumps(prediction) if prediction else None}).scalar_one()


def _save_recommendations(conn: Connection, run_id: int, snapshot_id: int, f: EquipmentFacts, recs: list[Recommendation], today: date) -> int:
    written = 0
    quiet_since = datetime.now(UTC) - timedelta(days=DECISION_QUIET_DAYS)
    kinds = {r.kind for r in recs}

    for r in recs:
        params = {"p": f.plant_id, "e": f.id, "k": r.kind, "snap": snapshot_id, "run": run_id, "t": r.title,
                  "why": r.rationale, "ev": json.dumps(r.evidence, default=str), "pri": r.priority}
        updated = conn.execute(text(
            """update recommendations set title = :t, rationale = :why, evidence = cast(:ev as jsonb), priority = :pri,
                      health_snapshot_id = :snap, analytics_run_id = :run, updated_at = now()
               where equipment_id = :e and kind = :k and status = 'OPEN'"""), params).rowcount
        if updated:
            written += 1
            continue

        # Respect recent decisions: deferred until a date, or accepted / rejected in the last 30 days.
        blocked = conn.execute(text(
            """select 1 from recommendations where equipment_id = :e and kind = :k and (
                   (status = 'DEFERRED' and coalesce(deferred_until, current_date) > :today)
                or (status in ('ACCEPTED', 'REJECTED') and decided_at >= :quiet))
               limit 1"""), {**params, "today": today, "quiet": quiet_since}).first()
        if blocked:
            continue

        conn.execute(text(
            """insert into recommendations (plant_id, equipment_id, health_snapshot_id, analytics_run_id, kind, title, rationale, evidence,
                                            priority, status, created_at, updated_at)
               values (:p, :e, :snap, :run, :k, :t, :why, cast(:ev as jsonb), :pri, 'OPEN', now(), now())"""), params)
        written += 1

    # Advice that no longer applies is closed as EXPIRED (kept for history).
    conn.execute(text(
        """update recommendations set status = 'EXPIRED', updated_at = now()
           where equipment_id = :e and status = 'OPEN' and not (kind = any(:kinds))"""), {"e": f.id, "kinds": list(kinds) or [""]})
    return written


def _weibull(conn: Connection, run_id: int, plant_id: int, today: date) -> dict[tuple[int, str], tuple[float | None, float | None]]:
    out: dict[tuple[int, str], tuple[float | None, float | None]] = {}
    for (type_id, mode), days in failure_days_by_type_mode(conn, plant_id).items():
        result = fit(intervals(days))
        n_failures = sum(len(d) for d in days)
        conn.execute(text(
            """insert into weibull_results (plant_id, equipment_type_id, failure_mode, n_failures, beta, eta_days, r_squared, interpretation,
                                            as_of_date, analytics_run_id, created_at, updated_at)
               values (:p, :t, :m, :n, :b, :eta, :r2, :i, :d, :run, now(), now())
               on conflict (plant_id, equipment_type_id, failure_mode, as_of_date) do update
                  set n_failures = excluded.n_failures, beta = excluded.beta, eta_days = excluded.eta_days, r_squared = excluded.r_squared,
                      interpretation = excluded.interpretation, analytics_run_id = excluded.analytics_run_id, updated_at = now()"""),
            {"p": plant_id, "t": type_id, "m": mode, "n": n_failures, "b": result.beta, "eta": result.eta, "r2": result.r_squared,
             "i": result.interpretation, "d": today, "run": run_id})
        out[(type_id, mode)] = (result.beta, result.eta)
    return out


def _latest_weibull(conn: Connection, plant_id: int) -> dict[tuple[int, str], tuple[float | None, float | None]]:
    rows = conn.execute(text(
        """select distinct on (equipment_type_id, failure_mode) equipment_type_id, failure_mode, beta, eta_days
           from weibull_results where plant_id = :p order by equipment_type_id, failure_mode, as_of_date desc"""), {"p": plant_id})
    return {(r.equipment_type_id, r.failure_mode): (float(r.beta) if r.beta is not None else None,
                                                     float(r.eta_days) if r.eta_days is not None else None) for r in rows}
