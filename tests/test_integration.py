"""Full run against a database built by scripts/prepare_test_db.sh (Laravel schema + demo data).

Skipped unless ANALYTICS_TEST_DATABASE_URL is set.
"""

import os

import pytest
from sqlalchemy import create_engine, text

from app.runner import execute

URL = os.environ.get("ANALYTICS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set ANALYTICS_TEST_DATABASE_URL (see scripts/prepare_test_db.sh)")


@pytest.fixture()
def engine():
    eng = create_engine(URL)
    with eng.begin() as conn:
        conn.execute(text("delete from recommendations; delete from health_snapshots; delete from weibull_results; delete from analytics_runs"))
    yield eng
    eng.dispose()


def new_run(engine, kind="NIGHTLY") -> tuple[int, int]:
    with engine.begin() as conn:
        plant_id = conn.execute(text("select id from plants where code = 'JPL-CHP'")).scalar_one()
        run_id = conn.execute(text(
            "insert into analytics_runs (plant_id, kind, status, created_at, updated_at) values (:p, :k, 'QUEUED', now(), now()) returning id"),
            {"p": plant_id, "k": kind}).scalar_one()
    return run_id, plant_id


def test_nightly_run_scores_every_equipment_and_records_the_run(engine):
    run_id, plant_id = new_run(engine)
    summary = execute(engine, run_id, plant_id, "NIGHTLY")

    with engine.connect() as conn:
        equipment = conn.execute(text("select count(*) from equipment where plant_id = :p"), {"p": plant_id}).scalar_one()
        snaps = conn.execute(text("select count(*) from health_snapshots where analytics_run_id = :r"), {"r": run_id}).scalar_one()
        run = conn.execute(text("select status, rows_written, summary from analytics_runs where id = :r"), {"r": run_id}).one()
        rg = conn.execute(text(
            """select h.score, h.factors from health_snapshots h join equipment e on e.id = h.equipment_id
               where e.tag_no = 'RG-1A' and h.analytics_run_id = :r"""), {"r": run_id}).one()

    assert snaps == equipment == summary["equipment"]
    assert run.status == "DONE" and run.rows_written >= snaps
    assert len(rg.factors) == 7 and float(rg.score) < 100  # demo RG-1A has rising vibration and an open defect


def test_rerun_updates_open_advice_and_respects_a_rejection(engine):
    run_id, plant_id = new_run(engine)
    execute(engine, run_id, plant_id, "NIGHTLY")
    with engine.begin() as conn:
        rec = conn.execute(text("select id, equipment_id, kind from recommendations where status = 'OPEN' order by id limit 1")).first()
        assert rec is not None
        conn.execute(text("update recommendations set status = 'REJECTED', decided_at = now(), remark = 'Not now' where id = :id"), {"id": rec.id})
        before = conn.execute(text("select count(*) from recommendations where status = 'OPEN'")).scalar_one()

    run2, _ = new_run(engine, "INCREMENTAL")
    execute(engine, run2, plant_id, "INCREMENTAL", [rec.equipment_id])

    with engine.connect() as conn:
        again = conn.execute(text("select count(*) from recommendations where equipment_id = :e and kind = :k and status = 'OPEN'"),
                             {"e": rec.equipment_id, "k": rec.kind}).scalar_one()
        after = conn.execute(text("select count(*) from recommendations where status = 'OPEN'")).scalar_one()
    assert again == 0 and after == before


def test_failed_run_is_recorded(engine, monkeypatch):
    run_id, plant_id = new_run(engine)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.runner._run", boom)
    with pytest.raises(RuntimeError):
        execute(engine, run_id, plant_id, "NIGHTLY")

    with engine.connect() as conn:
        row = conn.execute(text("select status, error from analytics_runs where id = :r"), {"r": run_id}).one()
    assert row.status == "FAILED" and "disk full" in row.error
