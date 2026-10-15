from fastapi.testclient import TestClient
from backend.app import app

client = TestClient(app)


def test_health():
    assert client.get("/health/live").json() == {"status": "ok"}


def test_job_lifecycle_contract():
    result = client.post("/api/v1/jobs", json={"ids": ["id1", "id2", "id1"]})
    assert result.status_code == 202
    data = result.json()
    assert data["total"] == 2
    status = client.get("/api/v1/jobs/" + data["job_id"])
    assert status.status_code == 200
    assert status.json()["status"] == "PENDING"


def test_missing_job():
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404
