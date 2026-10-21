from threading import Barrier
from types import SimpleNamespace

from planner_api.research.readiness import ResearchReadiness


def manager(mode="BGE"):
    return ResearchReadiness(SimpleNamespace(research_rerank_mode=mode, local_model_base_url="http://localhost:3212"))


def lifecycle():
    now = [1000.0]
    readiness = ResearchReadiness(SimpleNamespace(research_rerank_mode="BGE"), clock=lambda: now[0])
    readiness._check_search_reader = lambda: None
    readiness._check_bge = lambda: None
    released = []
    readiness._release_resources = lambda: released.append(now[0])
    return readiness, now, released


def test_ten_minutes_retained_and_reenable_reuses_ready_components():
    readiness, now, released = lifecycle()
    readiness.tick()
    readiness.connection("window-a", True, 1)
    readiness.connection("window-a", False, 2)
    now[0] += 599
    assert readiness.tick()["ready"] and not released
    assert readiness.connection("window-a", True, 3)["ready"]
    readiness.connection("window-a", False, 4)
    now[0] += 599
    assert readiness.tick()["ready"] and not released
    now[0] += 1
    assert readiness.tick()["status"] == "SUSPENDED"
    assert len(released) == 1
    assert readiness.connection("window-a", True, 5)["status"] == "LOADING"
    assert readiness.tick()["ready"]


def test_another_window_and_active_job_prevent_release():
    readiness, now, released = lifecycle()
    readiness.connection("window-a", True, 1)
    readiness.connection("window-b", True, 1)
    readiness.connection("window-a", False, 2)
    for sequence in range(2, 42):
        now[0] += 20
        readiness.connection("window-b", True, sequence)
    assert readiness.tick()["ready"] and not released
    readiness.connection("window-b", False, 42)
    with readiness.usage():
        now[0] += 1000
        assert readiness.tick()["ready"] and not released
    now[0] += 599
    assert readiness.tick()["ready"] and not released
    now[0] += 1
    assert readiness.tick()["status"] == "SUSPENDED"


def test_closed_tab_expires_and_late_toggle_responses_cannot_reverse_intent():
    readiness, now, released = lifecycle()
    readiness.connection("window-a", True, 2)
    assert readiness.connection("window-a", False, 1)["active_clients"] == 1
    readiness.connection("window-a", False, 3)
    assert readiness.connection("window-a", True, 2)["active_clients"] == 0
    readiness.connection("window-b", True, 1)
    now[0] += 600  # No heartbeat: closing/killing the browser cannot pin resources forever.
    assert readiness.tick()["status"] == "SUSPENDED" and released


def test_enable_while_release_is_in_flight_cannot_report_stale_ready():
    readiness, now, _ = lifecycle()
    readiness.tick()
    now[0] += 600
    readiness._release_resources = lambda: readiness.connection("window-a", True, 1)
    assert readiness.tick()["status"] == "LOADING"
    assert readiness.tick()["ready"]


def test_busy_shared_resource_retries_release_without_rewarming():
    readiness, now, released = lifecycle()
    readiness.tick()
    now[0] += 600
    readiness._release_resources = lambda: False
    readiness._check_bge = lambda: (_ for _ in ()).throw(AssertionError("should not rewarm while releasing"))
    assert not readiness.tick()["ready"]
    readiness._release_resources = lambda: released.append(now[0])
    assert readiness.tick()["status"] == "SUSPENDED"


def test_checks_overlap_and_require_every_component():
    readiness = manager()
    barrier = Barrier(2)
    readiness._check_search_reader = lambda: barrier.wait(timeout=2)
    readiness._check_bge = lambda: barrier.wait(timeout=2)
    assert not readiness.snapshot()["ready"]
    assert readiness.check_once()["ready"]
    def broken():
        raise RuntimeError("unavailable")
    readiness._check_search_reader = broken
    readiness._check_bge = lambda: None
    assert not readiness.check_once()["ready"]
    assert readiness.snapshot()["components"]["search_reader"] == "UNAVAILABLE"


def test_off_does_not_call_bge():
    readiness = manager("OFF")
    readiness._check_search_reader = lambda: None
    def forbidden():
        raise AssertionError("OFF contacted BGE")
    readiness._check_bge = forbidden
    assert readiness.check_once()["ready"]
    assert "bge" not in readiness.snapshot()["components"]


def test_api_blocks_submission_while_dependencies_are_loading(monkeypatch):
    from fastapi.testclient import TestClient
    from planner_api.main import create_app
    from planner_api.research.responses import ResearchResponseService
    application = create_app()
    monkeypatch.setattr(ResearchResponseService, "create", lambda *args: (_ for _ in ()).throw(AssertionError("submitted early")))
    with TestClient(application) as client:
        application.state.research_readiness = SimpleNamespace(snapshot=lambda: {"ready": False, "status": "LOADING"})
        assert client.get("/api/research/readiness").json()["ready"] is False
        result = client.post("/api/goals/test/research/requests", json={
            "question": "test", "phase": "CLARIFICATION", "idempotency_key": "readiness-test"})
        assert result.status_code == 503
        assert result.headers["Retry-After"] == "2"


def test_explicit_web_readiness_is_active_when_automatic_research_is_off(monkeypatch):
    from fastapi.testclient import TestClient
    import planner_api.main as main
    import planner_api.research.readiness as module
    from planner_api.config import Settings
    settings = Settings(_env_file=None, web_research_enabled=False, web_search_provider="open_websearch",
                        research_rerank_mode="OFF", material_rerank_mode="OFF", knowledge_enabled=False,
                        material_semantic_enabled=False)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "ResearchWorker", lambda *args, **kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None))
    monkeypatch.setattr(module, "ResearchReadiness", lambda *args: SimpleNamespace(
        start=lambda: None, stop=lambda: None, snapshot=lambda: {"ready": False, "status": "LOADING"}))
    with TestClient(main.create_app(start_research_worker=True)) as client:
        assert client.get("/api/research/readiness").json()["status"] == "LOADING"


def test_bge_loaded_is_not_enough_and_idle_lease_is_refreshed(monkeypatch):
    import planner_api.research.readiness as module
    readiness = manager()
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url): return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"ready": ["bge"]})
    monkeypatch.setattr(module.httpx, "Client", Client)
    calls = []
    def post(url, payload, timeout):
        calls.append(url.rsplit("/", 1)[-1])
        return {"ready": ["bge"]} if url.endswith("warmup") else {
            "identity": module.IDENTITY, "scores": [1., -1.], "coverage": {"complete": True}}
    monkeypatch.setattr(module, "_local_post", post)
    readiness._check_bge()
    readiness._check_bge()
    assert calls == ["warmup", "rerank", "warmup"]
    readiness._bge_probed = False
    monkeypatch.setattr(module, "_local_post", lambda *args: {"ready": ["bge"], "identity": "wrong"})
    readiness._check_search_reader = lambda: None
    assert not readiness.check_once()["ready"]
