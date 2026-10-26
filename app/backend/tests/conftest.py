from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from planner_api.ai import FakeClarificationProvider
from planner_api.api import (
    get_execution_discussion_service,
    get_light_arrangement_service,
    get_plan_service,
    get_route_service,
    get_service,
)
from planner_api.config import Settings
from planner_api.database import Base, get_db_session
from planner_api.execution_discussion_service import ExecutionDiscussionService
from planner_api.light_arrangement_service import LightArrangementService
from planner_api.main import create_app
from planner_api.plan_ai import FakePlanProvider
from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
from planner_api.plan_service import PlanService
from planner_api.route_ai import FakeRouteProvider
from planner_api.route_explanation_ai import FakeRouteExplanationProvider
from planner_api.route_service import RouteService
from planner_api.service import ClarificationService


def _build_test_pdf(
    pages: list[str], tables: dict[int, list[list[str]]] | None = None
) -> bytes:
    objects: list[bytes] = []
    page_ids = [4 + page_index * 2 for page_index in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_index, (page_id, page_text) in enumerate(
        zip(page_ids, pages, strict=True), start=1
    ):
        escaped = page_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands = [f"BT /F1 10 Tf 48 740 Td ({escaped}) Tj ET"]
        if tables and page_index in tables:
            rows = tables[page_index]
            commands.append("q 0.5 w 50 700 m 330 700 l S 50 680 m 330 680 l S")
            for row_index in range(1, len(rows) + 1):
                y = 700 - row_index * 20
                commands.append(f"50 {y} m 330 {y} l S")
            commands.extend(["50 700 m 50 660 l S", "180 700 m 180 660 l S"])
            commands.append("330 700 m 330 660 l S Q")
            for row_index, row in enumerate(rows):
                y = 685 - row_index * 20
                for col_index, value in enumerate(row[:2]):
                    x = 55 if col_index == 0 else 185
                    cell = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                    commands.append(f"BT /F1 9 Tf {x} {y} Td ({cell}) Tj ET")
        stream = "\n".join(commands).encode("ascii")
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
        ).encode("ascii")
        objects.extend(
            [
                page,
                f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
                + stream
                + b"\nendstream",
            ]
        )

    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{object_id} 0 obj\n".encode("ascii"))
        content.extend(body)
        content.extend(b"\nendobj\n")
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(content)


@pytest.fixture(autouse=True)
def offline_model_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLEVIA_ENV_FILE", str(tmp_path / "test-settings.env"))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    from planner_api.config import get_settings
    get_settings.cache_clear()
    # Authorize test clients without relaxing the production middleware.
    token = "test-session-credential-that-is-never-used-in-production"
    monkeypatch.setenv("SOLEVIA_SESSION_TOKEN", token)
    original_init = TestClient.__init__
    def local_test_client(self, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        kwargs.setdefault("headers", {"X-Solevia-Token": token})
        from planner_api.model_connection import require_model_connection
        app = args[0] if args else kwargs.get("app")
        if hasattr(app, "dependency_overrides"):
            app.dependency_overrides[require_model_connection] = lambda: None
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(TestClient, "__init__", local_test_client)
    # Crawl4AI imports python-dotenv at test time and otherwise promotes the
    # backend .env file into process-wide environment variables. That makes
    # later `_env_file=None` settings tests depend on test order.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("RESEARCH_RERANK_MODE", "OFF")
    offline = Settings(
        _env_file=None,
        llm_provider="fake",
        llm_api_key="",
        llm_proxy_url="",
    )
    monkeypatch.setattr(
        "planner_api.day_schedule_service.get_settings",
        lambda: offline,
    )
    monkeypatch.setattr("planner_api.generation_orchestration.get_settings", lambda: offline)
    monkeypatch.setattr("planner_api.research.orchestrator.get_settings", lambda: offline)
    # Local optional model switches must not leak into the offline regression
    # suite; dedicated tests inject enabled settings explicitly.
    monkeypatch.setattr("planner_api.materials.processing.get_settings", lambda: offline)
    monkeypatch.setattr("planner_api.materials.worker.get_settings", lambda: offline)
    monkeypatch.setattr("planner_api.materials.api.get_settings", lambda: offline)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE VIRTUAL TABLE material_chunks_fts USING fts5(
                chunk_id UNINDEXED,
                revision_id UNINDEXED,
                search_text,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER material_chunks_fts_delete
            AFTER DELETE ON material_chunks
            BEGIN
                DELETE FROM material_chunks_fts WHERE chunk_id = old.id;
            END
            """
        )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    app = create_app()

    def override_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    def override_service() -> Iterator[ClarificationService]:
        with session_factory() as session:
            yield ClarificationService(session, FakeClarificationProvider())

    def override_route_service() -> Iterator[RouteService]:
        with session_factory() as session:
            yield RouteService(
                session,
                FakeRouteProvider(),
                FakeRouteExplanationProvider(),
            )

    def override_plan_service() -> Iterator[PlanService]:
        with session_factory() as session:
            yield PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())

    def override_light_service() -> Iterator[LightArrangementService]:
        with session_factory() as session:
            yield LightArrangementService(session)

    def override_execution_discussion_service() -> Iterator[ExecutionDiscussionService]:
        with session_factory() as session:
            yield ExecutionDiscussionService(session, FakePlanDiscussionProvider())

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_service] = override_service
    app.dependency_overrides[get_route_service] = override_route_service
    app.dependency_overrides[get_plan_service] = override_plan_service
    app.dependency_overrides[get_light_arrangement_service] = override_light_service
    app.dependency_overrides[get_execution_discussion_service] = (
        override_execution_discussion_service
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def pdf_bytes_factory() -> Callable[[list[str]], bytes]:
    return _build_test_pdf
