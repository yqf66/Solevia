import pytest
from fastapi.testclient import TestClient

from planner_api.main import create_app


@pytest.fixture
def unauthenticated():
    return TestClient(create_app(), base_url="http://127.0.0.1", headers={})


@pytest.mark.parametrize("host", ["attacker.example", "127.0.0.1.attacker.example", "localhost.attacker.example"])
def test_rebinding_host_rejected_before_bootstrap(unauthenticated, host):
    response = unauthenticated.get("/api/session", headers={"host": host, "X-Solevia-Bootstrap": "1"})
    assert response.status_code == 400
    assert "token" not in response.text


@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "http://127.0.0.1:9999"])
def test_foreign_and_null_origins_cannot_bootstrap_or_mutate(unauthenticated, origin):
    assert unauthenticated.get("/api/session", headers={"Origin": origin, "X-Solevia-Bootstrap": "1"}).status_code == 403
    assert unauthenticated.put("/api/settings/model", headers={"Origin": origin}, json={}).status_code == 403


def test_token_required_for_reads_and_writes_and_not_in_health(unauthenticated):
    assert unauthenticated.get("/api/health").json() == {"status": "ok"}
    assert unauthenticated.get("/api/settings/model").status_code == 401
    assert unauthenticated.put("/api/settings/model", json={}).status_code == 401
    assert unauthenticated.get("/api/session").status_code == 403
    response = unauthenticated.get("/api/session", headers={"X-Solevia-Bootstrap": "1", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    token = response.json()["token"]
    assert len(token) >= 32
    headers = {"X-Solevia-Token": token, "Origin": "http://127.0.0.1:5173"}
    # Validation is reached only after authentication; malformed settings never save.
    assert unauthenticated.put("/api/settings/model", headers=headers, json={}).status_code == 422
    assert unauthenticated.get("/api/settings/model", headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert unauthenticated.options("/api/settings/model", headers={"Origin": "https://attacker.example", "Access-Control-Request-Headers": "X-Solevia-Token"}).status_code == 403


def test_new_process_session_does_not_reuse_old_token(monkeypatch):
    monkeypatch.delenv("SOLEVIA_SESSION_TOKEN", raising=False)
    one = TestClient(create_app(), base_url="http://localhost", headers={})
    two = TestClient(create_app(), base_url="http://localhost", headers={})
    token = one.get("/api/session", headers={"X-Solevia-Bootstrap": "1"}).json()["token"]
    assert two.get("/api/settings/model", headers={"X-Solevia-Token": token}).status_code == 401
