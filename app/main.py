from fastapi import FastAPI
from sqlalchemy import text

from app.db import engine

app = FastAPI(title="CHP Analytics Sidecar", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    with engine().connect() as conn:
        conn.execute(text("select 1"))
    return {"status": "ok", "db": "ok"}
