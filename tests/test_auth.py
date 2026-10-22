from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_runs_need_the_token():
    assert client.post("/runs", json={"run_id": 1, "plant_id": 1}).status_code == 401
    assert client.post("/runs", json={"run_id": 1, "plant_id": 1}, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_explain_needs_the_token():
    assert client.get("/health/1/explain").status_code == 401
