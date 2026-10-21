# CHP Analytics Sidecar (Python)

Internal FastAPI service for the CHP Maintenance Planner. It does the heavy analytics the Laravel app (`../web`) does not do: equipment **health score**, **PM recommendations**, **Weibull** analysis, and later **ML failure prediction**.

> Status: skeleton (health endpoint only). Analytics arrive in milestone **M7** — see `../docs/superpowers/plans/2026-10-21-build-roadmap.md`.

## Rules (contract with the Laravel app)

- **Schema is owned by Laravel migrations.** This service never creates or alters tables.
- It writes **only** to: `analytics_runs`, `health_snapshots`, `recommendations` (system rows, status `OPEN`), `weibull_results`, later `predictions`. Everything else is read-only.
- Production uses a dedicated DB user `analytics_user` with exactly those grants.
- It is **not exposed to the internet**: reachable only on the internal Docker network, with a bearer token (`ANALYTICS_TOKEN`) shared with Laravel.
- Laravel's scheduler triggers runs (nightly 02:00 IST and after imports/approvals). If this service is down, the app keeps working; only health scores go stale.

See `../TECHNICAL_DESIGN.md` §3.1 and §6.

## Local setup

Requires Python 3.12+ and the local PostgreSQL used by the web app.

```bash
cd analytics
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # adjust DATABASE_URL if needed
uvicorn app.main:app --reload --port 8100
curl http://localhost:8100/health   # {"status":"ok","db":"ok"}
```

## Tests and lint

```bash
pytest
ruff check .
```

## Layout

```
app/
  main.py      FastAPI routes
  config.py    settings from env (.env)
  db.py        SQLAlchemy engine
  (M7) health.py, recommend.py, weibull.py, runs.py
tests/
```
