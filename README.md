# CHP Analytics Sidecar (Python)

Internal FastAPI service of PlantPulse. It does the heavy analytics the Laravel app ([PlantPulse-web](https://github.com/ydesignshq/PlantPulse-web), cloned as `../web`) does not do: equipment **health score**, **PM recommendations**, **Weibull** analysis, and later **ML failure prediction**.

> Status: health score v1, recommendations and Weibull (median-rank regression) are live. ML prediction waits for 6+ months of real data.

## Rules (contract with the Laravel app)

- **Schema is owned by Laravel migrations.** This service never creates or alters tables.
- It writes **only** to: `analytics_runs`, `health_snapshots`, `recommendations` (system rows, status `OPEN`), `weibull_results`, later `predictions`. Everything else is read-only.
- Production uses a dedicated DB user `analytics_user` with exactly those grants.
- It is **not exposed to the internet**: it listens on 127.0.0.1 only, with a bearer token (`ANALYTICS_TOKEN`) shared with Laravel.
- Laravel's scheduler triggers runs (nightly 02:00 IST and after imports/approvals). If this service is down, the app keeps working; only health scores go stale.

See `TECHNICAL_DESIGN.md` §3.1 and §6 in [PlantPulse-core](https://github.com/ydesignshq/PlantPulse-core).

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

## API (all but `/health` need `Authorization: Bearer $ANALYTICS_TOKEN`)

| Method | Path | Does |
|---|---|---|
| GET | `/health` | liveness + DB check |
| POST | `/runs` `{run_id, plant_id, kind, equipment_ids?}` | executes a run Laravel recorded in `analytics_runs` (NIGHTLY also fits Weibull) |
| GET | `/health/{equipment_id}/explain` | latest score with each factor's contribution |
| POST | `/weibull` `{plant_id, equipment_type_id, failure_mode}` | on-demand Weibull fit (`BRG/WEAR` = object part / damage) |

## How the score works

Seven factors (weights editable in *Plant settings*): breakdowns (90 d), downtime vs type median, repeat failures, SAP PM overdue, running hours since PM, open defects by severity, condition readings vs alarm. `risk = Σ weight × factor (0–1)`, scaled by criticality (A 1.0, B 0.8, C 0.6); `score = 100 − risk`; bands Good ≥ 80, Watch ≥ 60, Poor ≥ 40, Critical < 40. Recommendations: PM at next opportunity, inspect, increase / decrease PM frequency, add to AOH/COH, raise RCA — each with its reason. Rules and choices: `DECISIONS.md` in PlantPulse-core (D-38 …).

**Failure sources.** Failures are the app's breakdown log. If the plant sets a cut-over date (*Plant settings → Permits are the failure history before*), corrective / breakdown Eclipse permits before that date are failures too (source `PERMIT`, failure mode `PART/*`), and breakdowns count only from that date on — JPL has no other history before SAP (D-74, D-75).

## Tests and lint

```bash
pytest                                    # unit tests (no database)
scripts/prepare_test_db.sh                # builds chp_analytics_test from the Laravel migrations + demo data
ANALYTICS_TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@127.0.0.1:5432/chp_analytics_test pytest   # + integration
ruff check .
```

## Layout

```
app/
  main.py        FastAPI routes          auth.py       bearer token check
  runner.py      one run: load → score → recommend → Weibull → write
  repository.py  reads the Laravel tables
  core/          pure logic (no DB): model.py, health.py, recommend.py, weibull.py
tests/           test_core.py (unit), test_auth.py, test_integration.py (real DB)
```
