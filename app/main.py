from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import require_token
from app.core.weibull import fit, intervals
from app.db import engine
from app.repository import failure_days_by_type_mode
from app.runner import execute

app = FastAPI(title="CHP Analytics Sidecar", version="1.0.0")


class RunRequest(BaseModel):
    run_id: int = Field(description="analytics_runs.id created by Laravel")
    plant_id: int
    kind: Literal["NIGHTLY", "INCREMENTAL", "WEIBULL"] = "NIGHTLY"
    equipment_ids: list[int] | None = None


class WeibullRequest(BaseModel):
    plant_id: int
    equipment_type_id: int
    failure_mode: str = Field(description="object part code / damage code, e.g. BRG/WEAR")


@app.get("/health")
def health() -> dict[str, str]:
    with engine().connect() as conn:
        conn.execute(text("select 1"))
    return {"status": "ok", "db": "ok"}


@app.post("/runs", dependencies=[Depends(require_token)])
def run(req: RunRequest) -> dict[str, object]:
    with engine().connect() as conn:
        row = conn.execute(text("select plant_id, status from analytics_runs where id = :id"), {"id": req.run_id}).first()
    if row is None or row.plant_id != req.plant_id:
        raise HTTPException(404, "Unknown run for this plant")
    if row.status not in ("QUEUED", "FAILED"):
        raise HTTPException(409, f"Run is already {row.status}")
    return execute(engine(), req.run_id, req.plant_id, req.kind, req.equipment_ids)


@app.get("/health/{equipment_id}/explain", dependencies=[Depends(require_token)])
def explain(equipment_id: int) -> dict[str, object]:
    with engine().connect() as conn:
        row = conn.execute(text(
            "select as_of_date, score, band, risk, factors from health_snapshots where equipment_id = :e order by as_of_date desc limit 1"),
            {"e": equipment_id}).first()
    if row is None:
        raise HTTPException(404, "No health snapshot yet")
    return {"as_of": row.as_of_date.isoformat(), "score": float(row.score), "band": row.band, "risk": float(row.risk), "factors": row.factors}


@app.post("/weibull", dependencies=[Depends(require_token)])
def weibull(req: WeibullRequest) -> dict[str, object]:
    with engine().connect() as conn:
        days = failure_days_by_type_mode(conn, req.plant_id).get((req.equipment_type_id, req.failure_mode), [])
    result = fit(intervals(days))
    return {"n_intervals": result.n, "beta": result.beta, "eta_days": result.eta, "r_squared": result.r_squared, "interpretation": result.interpretation}
