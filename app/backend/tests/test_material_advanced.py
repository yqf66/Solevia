from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from planner_api.config import Settings
from planner_api.materials.worker import MaterialProcessingWorker
from planner_api.materials.processing import MaterialProcessingService
from planner_api.materials.schemas import MaterialSearchRequest
from planner_api.models import MaterialChunk, MaterialEmbedding


def upload(client, goal_id, name, text):
    response = client.post(
        f"/api/goals/{goal_id}/materials/upload",
        params={"filename": name},
        content=text.encode(),
        headers={"Content-Type": "text/markdown"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_upload_versions_reader_overlap_diff_and_scope(client, session_factory):
    goal_id = client.post("/api/goals", json={"raw_input": "资料验收"}).json()["goal_id"]
    other = client.post("/api/goals", json={"raw_input": "其他规划"}).json()["goal_id"]
    before = "# Delivery\n" + "baseline payload\n" * 400 + "\nDeadline June."
    one = upload(client, goal_id, "report.md", before)
    assert MaterialProcessingWorker(session_factory).run_once()
    reader_path = f"/api/goals/{goal_id}/materials/{one['source_id']}"
    text = ""
    offset = 0
    while True:
        read = client.get(reader_path + "/read", params={"offset": offset, "limit": 500})
        assert read.status_code == 200, read.text
        page = read.json()
        text += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert text == before
    after = before.replace("Deadline June.", "Deadline July.")
    two = upload(client, goal_id, "report.md", after)
    assert two["source_id"] == one["source_id"]
    assert two["latest_revision"]["revision"] == 2
    assert MaterialProcessingWorker(session_factory).run_once()
    history = client.get(reader_path + "/revisions").json()
    assert len(history) == 2
    diff = client.get(
        reader_path + "/diff",
        params={
            "from_revision_id": one["latest_revision"]["id"],
            "to_revision_id": two["latest_revision"]["id"],
        },
    ).json()
    assert diff["added_lines"] == diff["removed_lines"] == 1
    assert any(line["kind"] == "added" and "July" in line["text"] for line in diff["lines"])
    assert not diff["truncated"]
    assert (
        "June"
        in client.get(
            reader_path + "/read",
            params={"revision_id": one["latest_revision"]["id"], "offset": len(before) - 100},
        ).json()["text"]
    )
    repeated = upload(client, goal_id, "report.md", after)
    assert repeated["latest_revision"]["revision"] == 2
    assert client.get(reader_path.replace(goal_id, other) + "/read").status_code == 404
    unrelated = upload(client, goal_id, "unrelated.md", "unrelated")
    assert (
        client.get(
            reader_path + "/read", params={"revision_id": unrelated["latest_revision"]["id"]}
        ).status_code
        == 404
    )


def test_semantic_fusion_budgets_latest_and_fallback(client, session_factory, monkeypatch):
    goal = client.post("/api/goals", json={"raw_input": "semantic"}).json()["goal_id"]
    source = upload(
        client, goal, "climate.md", "The device prevents overheating using passive radiators."
    )
    assert MaterialProcessingWorker(session_factory).run_once()
    settings = Settings(
        _env_file=None, material_semantic_enabled=True, material_semantic_min_similarity=0.7
    )

    class Encoder:
        def encode(self, _values, query=False):
            return [[1.0, 0.0]]

    monkeypatch.setattr("planner_api.materials.semantic.encoder", lambda *_args: Encoder())
    with session_factory() as session:
        chunk = session.scalar(select(MaterialChunk))
        session.add(
            MaterialEmbedding(
                chunk_id=chunk.id,
                model=settings.material_embedding_identity,
                content_hash=chunk.content_hash,
                vector_json=[1.0, 0.0],
            )
        )
        session.commit()
        service = MaterialProcessingService(session, settings)
        hits = service.search_goal_materials(
            goal, MaterialSearchRequest(query="thermal management", max_total_chars=500)
        )
        assert hits[0].retrieval_method == "SEMANTIC"
        assert "radiators" in hits[0].text
        assert sum(len(hit.text) for hit in hits) <= 500
        monkeypatch.setattr(
            "planner_api.materials.semantic.encoder",
            lambda *_: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        assert service.search_goal_materials(goal, MaterialSearchRequest(query="radiators"))
        assert service.semantic_status == "FALLBACK"
    upload(client, goal, "climate.md", "Changed body not parsed yet.")
    with session_factory() as session:
        assert (
            MaterialProcessingService(session, settings).search_goal_materials(
                goal, MaterialSearchRequest(query="radiators", source_ids=[source["source_id"]])
            )
            == []
        )


def test_execution_material_context_retries_scope_and_unready(client, session_factory, monkeypatch):
    from test_execution_discussion import _started_goal
    from planner_api.api import get_execution_discussion_service
    from planner_api.execution_discussion_service import ExecutionDiscussionService
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    from planner_api.materials.analysis_ai import FakeMaterialAnalysisProvider

    contexts = []

    class Provider(FakePlanDiscussionProvider):
        def discuss_execution(self, context):
            contexts.append(context)
            return super().discuss_execution(context)

    def override():
        with session_factory() as session:
            yield ExecutionDiscussionService(session, Provider())

    client.app.dependency_overrides[get_execution_discussion_service] = override
    monkeypatch.setattr(
        "planner_api.execution_discussion_service.build_material_analysis_provider",
        lambda *_: FakeMaterialAnalysisProvider(),
    )
    goal = _started_goal(client)
    source = upload(
        client, goal, "execution.md", "# Review\nPerformance review must precede deployment."
    )
    path = f"/api/goals/{goal}/execution/discussion/messages"
    assert (
        client.post(
            path,
            json={"content": "performance review?", "material_source_ids": [source["source_id"]]},
        ).status_code
        == 409
    )
    assert MaterialProcessingWorker(session_factory).run_once()
    response = client.post(
        path, json={"content": "performance review?", "material_source_ids": [source["source_id"]]}
    )
    assert response.status_code == 200, response.text
    context = contexts[-1].material_context
    assert context["source_ids"] == [source["source_id"]]
    assert "deployment" in context["passages"][0]["text"]
    assert (
        client.post(path, json={"content": "performance review?", "regenerate": True}).status_code
        == 200
    )
    assert contexts[-1].material_context == context
    other = client.post("/api/goals", json={"raw_input": "other"}).json()["goal_id"]
    other_source = upload(client, other, "secret.md", "other goal data")
    assert (
        client.post(
            path, json={"content": "review", "material_source_ids": [other_source["source_id"]]}
        ).status_code
        == 422
    )


def test_ocr_mixed_pdf_limits_and_cancel(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from planner_api.materials.parsers import parse_material
    from planner_api.materials.processing import MaterialProcessingCancelled
    import pytest

    class Page:
        width = height = 100
        images = [{"width": 100, "height": 100}]

        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

        def extract_tables(self):
            return []

        def close(self):
            pass

    class PDF:
        pages = [Page("Text based page " * 5), Page("1"), Page("")]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setitem(
        __import__("sys").modules, "pdfplumber", SimpleNamespace(open=lambda _: PDF())
    )
    monkeypatch.setattr(
        "planner_api.materials.ocr.recognize_page",
        lambda *_args, **_kwargs: ("Scanned technical requirements", 0.98, 0),
    )
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"fixture")
    parsed = parse_material(path, ocr_enabled=True, ocr_max_pages=1)
    assert "Text based" in parsed.text and "Scanned technical" in parsed.text
    assert parsed.metadata["ocr_pages"] == [2]
    assert parsed.metadata["unread_pages"] == [3]
    assert parsed.metadata["coverage"] == "PARTIAL"
    assert parsed.locations[1].detail["confidence"] == 0.98
    assert (
        parsed.text[parsed.locations[1].char_start : parsed.locations[1].char_end]
        == "Scanned technical requirements"
    )
    monkeypatch.setattr(
        "planner_api.materials.ocr.recognize_page",
        lambda *_args, **_kwargs: ("Scanned technical requirements", 0.98, 1),
    )
    partial = parse_material(path, ocr_enabled=True)
    assert partial.metadata["coverage"] == "PARTIAL"
    assert partial.metadata["unread_pages"] == [2, 3]
    assert any("低置信度" in warning for warning in partial.metadata["warnings"])
    with pytest.raises(MaterialProcessingCancelled):
        parse_material(
            path,
            ocr_enabled=True,
            check_cancelled=lambda: (_ for _ in ()).throw(MaterialProcessingCancelled()),
        )


def test_mineru_is_optional_complex_pdf_backend_and_falls_back(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from planner_api.materials.parsers import parse_material

    class Page:
        width = height = 100
        images = [{"width": 100, "height": 100}]

        def extract_text(self):
            return "native text"

        def extract_tables(self):
            return [[["complex", "table"]]]

        def close(self):
            pass

    class PDF:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setitem(
        __import__("sys").modules, "pdfplumber", SimpleNamespace(open=lambda _: PDF())
    )
    path = tmp_path / "complex.pdf"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "planner_api.materials.mineru_adapter.parse_pdf",
        lambda *_args, **_kwargs: {
            "version": "4.0.10",
            "pages": [{"page": 1, "text": "# Recovered layout\n\n| complex | table |"}],
        },
    )
    enhanced = parse_material(path, mineru_enabled=True, mineru_auto_threshold=1)
    assert enhanced.parser_name == "mineru"
    assert "Recovered layout" in enhanced.text
    assert enhanced.metadata["mineru_used"] is True
    assert enhanced.metadata["complexity_reasons"] == ["large_image_or_scan", "table_layout"]

    basic = parse_material(path, mineru_enabled=False)
    assert basic.parser_name == "pdfplumber" and not basic.metadata["mineru_attempted"]

    bounded = parse_material(path, mineru_enabled=True, mineru_mode="FORCE", mineru_max_pages=0)
    assert (
        not bounded.metadata["mineru_used"] and "页数超过预算" in bounded.metadata["mineru_warning"]
    )

    monkeypatch.setattr(
        "planner_api.materials.mineru_adapter.parse_pdf",
        lambda *_args, **_kwargs: {
            "version": "4.0.10",
            "pages": [{"page": 2, "text": "wrong page"}],
        },
    )
    incomplete = parse_material(path, mineru_enabled=True, mineru_mode="FORCE")
    assert not incomplete.metadata["mineru_used"]
    assert "页码覆盖不完整" in incomplete.metadata["mineru_warning"]

    monkeypatch.setattr(
        "planner_api.materials.mineru_adapter.parse_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("models unavailable")),
    )
    fallback = parse_material(path, mineru_enabled=True, mineru_auto_threshold=1)
    assert fallback.parser_name == "pdfplumber"
    assert fallback.text == "native text\n\ncomplex | table"
    assert fallback.metadata["mineru_used"] is False
    assert fallback.metadata["mineru_warning"] == "models unavailable"
    assert any("已回退基础解析" in warning for warning in fallback.metadata["warnings"])


def test_complexity_gutter_math_and_ordinary_pages():
    from types import SimpleNamespace
    from planner_api.materials.parsers import _pdf_complexity_signals

    words = [{"x0": x, "x1": x + 18, "top": y} for y in (20, 40, 60) for x in (20, 65, 340, 375)]
    page = SimpleNamespace(width=600, height=800, images=[], extract_words=lambda: words)
    assert _pdf_complexity_signals(page, "normal text", [])[1] == {"multi_column_layout"}
    assert _pdf_complexity_signals(page, "x = 2", [])[1] == {
        "multi_column_layout",
        "formula_or_math",
    }
    words.extend({"x0": 288, "x1": 317, "top": y} for y in (20, 40, 60))
    assert _pdf_complexity_signals(page, "ordinary paragraph", [])[0] == 0


def test_manual_mineru_reparse_preserves_old_revision(client, session_factory, monkeypatch):
    from planner_api.materials.parsers import ParsedMaterial, ParsedLocation

    goal = client.post("/api/goals", json={"raw_input": "PDF reading"}).json()["goal_id"]
    source = upload(client, goal, "complex.pdf", "fake file bytes")
    original = ParsedMaterial(
        "baseline",
        "pdfplumber",
        "test",
        {},
        (ParsedLocation(0, 8, {"kind": "pdf_page", "page": 1}),),
    )
    monkeypatch.setattr(
        "planner_api.materials.processing.parse_material", lambda *_args, **_kwargs: original
    )
    worker = MaterialProcessingWorker(
        session_factory, Settings(_env_file=None, material_mineru_enabled=True)
    )
    assert worker.run_once()
    with session_factory() as session:
        service = MaterialProcessingService(
            session, Settings(_env_file=None, material_mineru_enabled=True)
        )
        revision = service.process_for_goal(goal, source["source_id"], "MINERU")
        assert revision.revision == 2
        assert revision.metadata_json["requested_pdf_parser"] == "MINERU"
    assert worker.run_once()
    revisions = client.get(f"/api/goals/{goal}/materials/{source['source_id']}/revisions").json()
    assert len(revisions) == 2 and all(item["status"] == "READY" for item in revisions)
    assert revisions[0]["metadata"]["requested_pdf_parser"] == "MINERU"

    # AUTO is an explicit selection and must override a globally forced backend.
    calls = []
    monkeypatch.setattr(
        "planner_api.materials.processing.parse_material",
        lambda *_args, **kwargs: calls.append(kwargs) or original,
    )
    forced = Settings(_env_file=None, material_mineru_enabled=True, material_mineru_mode="FORCE")
    with session_factory() as session:
        MaterialProcessingService(session, forced).process_for_goal(
            goal, source["source_id"], "AUTO"
        )
    assert MaterialProcessingWorker(session_factory, forced).run_once()
    assert calls[0]["mineru_mode"] == "AUTO"

    other = client.post("/api/goals", json={"raw_input": "unrelated"}).json()["goal_id"]
    endpoint = f"/api/goals/{goal}/materials/{source['source_id']}/process"
    assert client.post(endpoint, params={"pdf_parser": "UNKNOWN"}).status_code == 422
    assert (
        client.post(endpoint.replace(goal, other), params={"pdf_parser": "BASIC"}).status_code
        == 404
    )
    with session_factory() as session:
        with pytest.raises(HTTPException, match="MinerU 尚未启用"):
            MaterialProcessingService(session, Settings(_env_file=None)).process_for_goal(
                goal, source["source_id"], "MINERU"
            )


def test_mineru_subprocess_timeout_and_cancel_kills_process(tmp_path, monkeypatch):
    import pytest
    from planner_api.materials import mineru_adapter
    from planner_api.materials.processing import MaterialProcessingCancelled

    input_file = tmp_path / "资料.pdf"
    input_file.write_bytes(b"pdf")

    class Process:
        killed = False
        pid = 99999999

        def poll(self):
            return 1 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self):
            return 1

    spawned = []

    def spawn(*args, **kwargs):
        process = Process()
        spawned.append(process)
        assert kwargs["env"]["MINERU_MODEL_SOURCE"] == "local"
        assert args[0][1] == "-I"
        assert Path(args[0][3]).name == "input.pdf"
        return process

    from planner_api import worker_sandbox
    monkeypatch.setattr(worker_sandbox.subprocess, "Popen", spawn)
    monkeypatch.setattr(worker_sandbox.WorkerJob, "attach", lambda *_: None)
    with pytest.raises(RuntimeError, match="时间预算"):
        mineru_adapter.parse_pdf(input_file, timeout_seconds=0)
    assert spawned[-1].killed
    with pytest.raises(MaterialProcessingCancelled):
        mineru_adapter.parse_pdf(
            input_file, check_cancelled=lambda: (_ for _ in ()).throw(MaterialProcessingCancelled())
        )
    assert spawned[-1].killed
