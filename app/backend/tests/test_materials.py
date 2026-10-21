import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Thread
from time import monotonic, perf_counter, sleep

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import planner_api.materials.processing as material_processing
from planner_api.config import Settings
from planner_api.database import Base, get_db_session
from planner_api.main import create_app
from planner_api.materials.processing import MaterialProcessingService
from planner_api.materials.service import MaterialService
from planner_api.materials.worker import MaterialProcessingWorker
from planner_api.models import (
    MaterialChunk,
    MaterialProcessingJob,
    MaterialRevision,
    MaterialRoot,
    MaterialSource,
)


def _goal(client: TestClient) -> str:
    response = client.post("/api/goals", json={"raw_input": "根据项目资料制定计划"})
    assert response.status_code == 201
    return response.json()["goal_id"]


def _root(client: TestClient, path: Path) -> dict:
    response = client.post(
        "/api/material-roots",
        json={"display_name": "测试资料", "path": str(path)},
    )
    assert response.status_code == 201
    return response.json()


def test_local_material_is_linked_with_immutable_revision(
    client: TestClient, tmp_path: Path
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "brief.md"
    document.write_text("第一版项目目标", encoding="utf-8")
    root = _root(client, tmp_path)

    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "brief.md", "scope": "PLAN"},
    )

    assert created.status_code == 201
    material = created.json()
    assert material["relative_path"] == "brief.md"
    assert material["scope"] == "PLAN"
    assert material["latest_revision"]["revision"] == 1
    assert material["latest_revision"]["status"] == "QUEUED"
    assert material["latest_revision"]["file_size"] == len("第一版项目目标".encode())

    repeated = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "brief.md", "scope": "ALL"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["source_id"] == material["source_id"]
    assert repeated.json()["latest_revision"]["id"] == material["latest_revision"]["id"]
    assert repeated.json()["scope"] == "ALL"


def test_refresh_creates_revision_only_when_file_changes(
    client: TestClient, tmp_path: Path
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "plan.txt"
    document.write_text("version one", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "plan.txt"},
    ).json()

    unchanged = client.post(
        f"/api/goals/{goal_id}/materials/{created['source_id']}/refresh"
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["latest_revision"]["revision"] == 1

    document.write_text("version two with changes", encoding="utf-8")
    changed = client.post(
        f"/api/goals/{goal_id}/materials/{created['source_id']}/refresh"
    )
    assert changed.status_code == 200
    assert changed.json()["latest_revision"]["revision"] == 2
    assert (
        changed.json()["latest_revision"]["content_hash"]
        != created["latest_revision"]["content_hash"]
    )


def test_local_reference_rejects_escape_and_absolute_paths(
    client: TestClient, tmp_path: Path
) -> None:
    goal_id = _goal(client)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    root = _root(client, allowed)

    escaped = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "../outside.md"},
    )
    absolute = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": str(outside)},
    )

    assert escaped.status_code == 422
    assert absolute.status_code == 422


def test_material_links_are_goal_scoped(client: TestClient, tmp_path: Path) -> None:
    first_goal = _goal(client)
    second_goal = _goal(client)
    document = tmp_path / "scope.md"
    document.write_text("shared source", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{first_goal}/materials/local",
        json={"root_id": root["id"], "relative_path": "scope.md"},
    ).json()

    first = client.get(f"/api/goals/{first_goal}/materials")
    second = client.get(f"/api/goals/{second_goal}/materials")
    foreign_refresh = client.post(
        f"/api/goals/{second_goal}/materials/{created['source_id']}/refresh"
    )

    assert len(first.json()) == 1
    assert second.json() == []
    assert foreign_refresh.status_code == 404


def test_disabling_root_prevents_new_references(client: TestClient, tmp_path: Path) -> None:
    goal_id = _goal(client)
    document = tmp_path / "disabled.md"
    document.write_text("content", encoding="utf-8")
    root = _root(client, tmp_path)

    disabled = client.delete(f"/api/material-roots/{root['id']}")
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    )

    assert disabled.status_code == 204
    assert created.status_code == 409


def test_processes_long_markdown_into_bounded_searchable_chunks(
    client: TestClient, tmp_path: Path, session_factory
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "long.md"
    document.write_text(
        "# 项目目标\n\n"
        + ("这是项目目标和验收标准的正文。 " * 300)
        + "\n\n## 实施约束\n\n"
        + ("浏览器依赖和本地部署约束需要单独处理。 " * 300),
        encoding="utf-8",
    )
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "long.md"},
    ).json()

    processed = client.post(
        f"/api/goals/{goal_id}/materials/{created['source_id']}/process"
    )
    assert processed.status_code == 200
    revision = processed.json()["latest_revision"]
    assert revision["status"] == "QUEUED"

    worker = MaterialProcessingWorker(session_factory)
    assert worker.run_once() is True
    processed = client.get(f"/api/goals/{goal_id}/materials")
    revision = processed.json()[0]["latest_revision"]
    assert revision["status"] == "READY"
    assert revision["parser_name"] == "builtin-text"
    assert revision["metadata"]["chunk_count"] > 1

    searched = client.post(
        f"/api/goals/{goal_id}/materials/search",
        json={
            "query": "浏览器 本地部署",
            "max_chunks": 3,
            "max_chars": 800,
            "max_total_chars": 1000,
        },
    )
    assert searched.status_code == 200
    hits = searched.json()
    assert hits
    assert len(hits) <= 3
    assert all(len(hit["text"]) <= 800 for hit in hits)
    assert sum(len(hit["text"]) for hit in hits) <= 1000
    assert all(hit["locator"].startswith("chunk:") for hit in hits)


def test_hundred_page_pdf_is_ingested_and_search_returns_page_locator(
    client: TestClient, tmp_path: Path, session_factory, pdf_bytes_factory
) -> None:
    goal_id = _goal(client)
    pages = [
        f"PAGE {number}: general planning material and standard review notes"
        for number in range(1, 101)
    ]
    pages[72] = "PAGE 73: QUARTZ KERNEL calibration is required for the rare component"
    document = tmp_path / "hundred-pages.pdf"
    document.write_bytes(pdf_bytes_factory(pages))
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    ).json()
    queued = client.post(f"/api/goals/{goal_id}/materials/{created['source_id']}/process")
    assert queued.status_code == 200
    assert queued.json()["latest_revision"]["processing"]["status"] == "QUEUED"

    tracemalloc.start()
    started = perf_counter()
    assert MaterialProcessingWorker(session_factory).run_once() is True
    elapsed_seconds = perf_counter() - started
    _, peak_python_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed_seconds < 30
    assert peak_python_bytes < 128 * 1024 * 1024
    materials = client.get(f"/api/goals/{goal_id}/materials").json()
    revision = materials[0]["latest_revision"]
    assert revision["status"] == "READY"
    assert revision["parser_name"] == "pdfplumber"
    assert revision["metadata"]["page_count"] == 100
    assert revision["metadata"]["chunk_count"] > 1
    assert revision["processing"]["status"] == "READY"
    assert revision["processing"]["processing_duration_ms"] is not None

    response = client.post(
        f"/api/goals/{goal_id}/materials/search",
        json={
            "query": "quartz kernel calibration",
            "max_chunks": 3,
            "max_chars": 400,
            "max_total_chars": 600,
        },
    )
    assert response.status_code == 200
    hits = response.json()
    assert hits
    assert all(hit["revision_id"] == revision["id"] for hit in hits)
    assert sum(len(hit["text"]) for hit in hits) <= 600
    page_hit = next((hit for hit in hits if "QUARTZ KERNEL" in hit["text"]), None)
    assert page_hit is not None, hits[0]["text"]
    assert "PDF 第 73 页" in page_hit["locator"]
    assert any(
        item.get("kind") == "pdf_page" and item.get("page") == 73
        for item in page_hit["locator_details"]
    )
    print(
        "100-page PDF pressure: "
        f"elapsed={elapsed_seconds:.3f}s, Python peak={peak_python_bytes / 1024 / 1024:.1f} MiB, "
        f"retrieved_page=73, returned_chars={sum(len(hit['text']) for hit in hits)}"
    )


def test_new_revision_search_never_mixes_chunks_from_previous_revision(
    client: TestClient, tmp_path: Path, session_factory
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "revised.md"
    document.write_text("legacy walrus design", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    ).json()
    client.post(f"/api/goals/{goal_id}/materials/{created['source_id']}/process")
    worker = MaterialProcessingWorker(session_factory)
    assert worker.run_once() is True
    first_revision = client.get(f"/api/goals/{goal_id}/materials").json()[0][
        "latest_revision"
    ]["id"]

    document.write_text("current narwhal design", encoding="utf-8")
    refreshed = client.post(
        f"/api/goals/{goal_id}/materials/{created['source_id']}/refresh"
    ).json()
    assert refreshed["latest_revision"]["revision"] == 2
    client.post(f"/api/goals/{goal_id}/materials/{created['source_id']}/process")
    assert worker.run_once() is True

    search = client.post(
        f"/api/goals/{goal_id}/materials/search",
        json={"query": "narwhal design"},
    )
    assert search.status_code == 200
    hits = search.json()
    assert hits
    assert all(hit["revision_id"] != first_revision for hit in hits)
    assert all("legacy walrus" not in hit["text"] for hit in hits)


def test_queued_material_job_can_be_cancelled_and_requeued(
    client: TestClient, tmp_path: Path, session_factory
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "cancel-queued.md"
    document.write_text("cancellable queued text", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    ).json()
    source_id = created["source_id"]
    client.post(f"/api/goals/{goal_id}/materials/{source_id}/process")

    cancelled = client.post(
        f"/api/goals/{goal_id}/materials/{source_id}/process/cancel"
    )
    assert cancelled.status_code == 200
    latest = cancelled.json()["latest_revision"]
    assert latest["status"] == "CANCELLED"
    assert latest["processing"]["status"] == "CANCELLED"

    requeued = client.post(f"/api/goals/{goal_id}/materials/{source_id}/process")
    assert requeued.json()["latest_revision"]["status"] == "QUEUED"
    assert MaterialProcessingWorker(session_factory).run_once() is True
    ready = client.get(f"/api/goals/{goal_id}/materials").json()[0]["latest_revision"]
    assert ready["status"] == "READY"
    assert ready["processing"]["attempts"] == 1


def test_processing_cancel_is_cooperative_and_does_not_write_partial_chunks(
    client: TestClient, tmp_path: Path, session_factory, monkeypatch
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "cancel-running.md"
    document.write_text("document content to process", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    ).json()
    source_id = created["source_id"]
    client.post(f"/api/goals/{goal_id}/materials/{source_id}/process")

    parse_started = Event()
    continue_parse = Event()
    real_parse_material = material_processing.parse_material

    def delayed_parse(*args, **kwargs):
        parse_started.set()
        assert continue_parse.wait(5)
        return real_parse_material(*args, **kwargs)

    monkeypatch.setattr("planner_api.materials.processing.parse_material", delayed_parse)
    worker = MaterialProcessingWorker(session_factory)
    task = Thread(target=worker.run_once)
    task.start()
    assert parse_started.wait(5)

    requested = client.post(
        f"/api/goals/{goal_id}/materials/{source_id}/process/cancel"
    )
    assert requested.status_code == 200
    assert requested.json()["latest_revision"]["processing"]["cancellation_requested"]
    continue_parse.set()
    task.join(timeout=5)
    assert not task.is_alive()

    latest = client.get(f"/api/goals/{goal_id}/materials").json()[0]["latest_revision"]
    assert latest["status"] == "CANCELLED"
    with session_factory() as session:
        chunks = list(session.scalars(select(MaterialChunk)))
        job = session.scalar(
            select(MaterialProcessingJob).where(
                MaterialProcessingJob.revision_id == latest["id"]
            )
        )
        assert chunks == []
        assert job.status == "CANCELLED"
        assert job.processing_duration_ms is not None


def test_worker_stop_timeout_does_not_allow_a_second_worker_thread(session_factory) -> None:
    worker = MaterialProcessingWorker(session_factory)
    entered = Event()
    release = Event()

    def blocked_loop() -> None:
        entered.set()
        release.wait(5)

    worker.run_forever = blocked_loop
    worker.start()
    assert entered.wait(2)
    original_thread = worker._thread
    worker.stop(timeout=0.01)
    assert worker._thread is original_thread
    assert original_thread is not None and original_thread.is_alive()

    worker.start()
    assert worker._thread is original_thread
    release.set()
    worker.stop(timeout=2)
    assert worker._thread is None


def test_two_workers_cannot_claim_the_same_sqlite_job(tmp_path: Path) -> None:
    database = tmp_path / "worker-claims.db"
    engine = create_engine(
        f"sqlite:///{database.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE material_chunks_fts USING fts5("
            "chunk_id UNINDEXED, revision_id UNINDEXED, search_text)"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER material_chunks_fts_delete AFTER DELETE ON material_chunks "
            "BEGIN DELETE FROM material_chunks_fts WHERE chunk_id = old.id; END"
        )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        root = MaterialRoot(display_name="root", canonical_path=str(tmp_path))
        session.add(root)
        session.flush()
        source = MaterialSource(
            root_id=root.id,
            relative_path="unused.md",
            canonical_uri=f"local-material://{root.id}/unused.md",
            title="unused.md",
            mime_type="text/markdown",
        )
        session.add(source)
        session.flush()
        revision = MaterialRevision(
            source_id=source.id,
            revision=1,
            content_hash="0" * 64,
            file_size=0,
            status="QUEUED",
        )
        session.add(revision)
        session.flush()
        job = MaterialProcessingJob(revision_id=revision.id)
        session.add(job)
        session.commit()
        job_id = job.id

    gate = Barrier(2)

    def claim() -> str | None:
        gate.wait(timeout=3)
        return MaterialProcessingWorker(factory)._claim_next_job()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))

    assert claims.count(job_id) == 1
    assert claims.count(None) == 1
    with factory() as session:
        claimed = session.get(MaterialProcessingJob, job_id)
        assert claimed.status == "PROCESSING"
        assert claimed.attempts == 1
    engine.dispose()


def test_reference_rejects_file_over_configured_size_without_hashing_it(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    oversized = tmp_path / "oversized.md"
    with oversized.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)

    with session_factory() as session:
        service = MaterialService(
            session,
            Settings(_env_file=None, material_max_file_bytes=1024 * 1024),
        )
        monkeypatch.setattr(
            "planner_api.materials.service.hashlib.sha256",
            lambda: pytest.fail("oversized file should be rejected before hashing"),
        )
        with pytest.raises(HTTPException) as error:
            service._fingerprint(oversized)
    assert error.value.status_code == 413


def test_processing_failure_is_recorded_without_failing_material_reference(
    client: TestClient, tmp_path: Path, session_factory
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "empty.md"
    document.write_text("", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "empty.md"},
    ).json()

    queued = client.post(
        f"/api/goals/{goal_id}/materials/{created['source_id']}/process"
    )
    assert queued.json()["latest_revision"]["status"] == "QUEUED"
    assert MaterialProcessingWorker(session_factory).run_once() is True
    processed = client.get(f"/api/goals/{goal_id}/materials")
    assert processed.status_code == 200
    revision = processed.json()[0]["latest_revision"]
    assert revision["status"] == "FAILED"
    assert revision["error_code"] == "EMPTY_DOCUMENT"

    listed = client.get(f"/api/goals/{goal_id}/materials")
    assert listed.status_code == 200
    assert listed.json()[0]["source_id"] == created["source_id"]


def test_worker_recovers_expired_processing_lease(
    client: TestClient, tmp_path: Path, session_factory
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "recover.md"
    document.write_text("# Recovery\n\nrecoverable queue content " * 100, encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "recover.md"},
    ).json()
    client.post(f"/api/goals/{goal_id}/materials/{created['source_id']}/process")

    with session_factory() as session:
        revision = session.scalar(
            select(MaterialRevision).where(MaterialRevision.id == created["latest_revision"]["id"])
        )
        job = session.scalar(
            select(MaterialProcessingJob).where(
                MaterialProcessingJob.revision_id == revision.id
            )
        )
        job.status = "PROCESSING"
        job.attempts = 1
        job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
        revision.status = "PROCESSING"
        session.commit()

    worker = MaterialProcessingWorker(session_factory)
    assert worker.run_once() is True
    materials = client.get(f"/api/goals/{goal_id}/materials").json()
    assert materials[0]["latest_revision"]["status"] == "READY"
    with session_factory() as session:
        job = session.scalar(
            select(MaterialProcessingJob).where(
                MaterialProcessingJob.revision_id == created["latest_revision"]["id"]
            )
        )
        assert job.status == "READY"
        assert job.attempts == 2


def test_ready_revision_recreates_missing_queue_record(
    client: TestClient, tmp_path: Path, session_factory
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "ready.md"
    document.write_text("already processed", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "ready.md"},
    ).json()
    client.post(f"/api/goals/{goal_id}/materials/{created['source_id']}/process")
    assert MaterialProcessingWorker(session_factory).run_once() is True

    with session_factory() as session:
        revision = session.get(MaterialRevision, created["latest_revision"]["id"])
        session.delete(
            session.scalar(
                select(MaterialProcessingJob).where(
                    MaterialProcessingJob.revision_id == revision.id
                )
            )
        )
        session.commit()

        restored_job = MaterialProcessingService(session).enqueue_revision(revision)

        assert restored_job.status == "READY"
        assert restored_job.revision_id == revision.id


@pytest.fixture
def threaded_material_database(tmp_path, session_factory):
    """The worker and polling API must use independent SQLite connections.

    StaticPool's single in-memory connection is not safe for simultaneous
    transactions: it can mix cursors or rollback another thread's write. Copy
    the same schema (including FTS/triggers) to an isolated WAL database.
    """
    original = session_factory.kw["bind"]
    engine = create_engine(f"sqlite:///{(tmp_path / 'worker.db').as_posix()}")
    source, target = original.raw_connection(), engine.raw_connection()
    try:
        source.driver_connection.backup(target.driver_connection)
    finally:
        source.close()
        target.close()
    session_factory.configure(bind=engine)
    try:
        yield
    finally:
        session_factory.configure(bind=original)
        engine.dispose()


def test_api_lifespan_worker_processes_persisted_queue(
    client: TestClient, tmp_path: Path, session_factory, monkeypatch, threaded_material_database
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "worker-start.md"
    document.write_text("worker starts with the API lifespan", encoding="utf-8")
    root = _root(client, tmp_path)
    created = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    ).json()
    queued = client.post(f"/api/goals/{goal_id}/materials/{created['source_id']}/process")
    assert queued.json()["latest_revision"]["status"] == "QUEUED"

    monkeypatch.setattr("planner_api.main.SessionLocal", session_factory)
    app = create_app(start_material_worker=True)

    def override_session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    with TestClient(app) as api_client:
        deadline = monotonic() + 5
        status = "QUEUED"
        while monotonic() < deadline:
            materials = api_client.get(f"/api/goals/{goal_id}/materials").json()
            status = materials[0]["latest_revision"]["status"]
            if status == "READY":
                break
            sleep(0.05)

    assert status == "READY"
