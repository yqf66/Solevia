import pytest
from sqlalchemy import func, select

from planner_api.config import Settings
from planner_api.materials.processing import MaterialProcessingService
from planner_api.materials.schemas import MaterialSearchRequest
from planner_api.materials.worker import MaterialProcessingWorker
from planner_api.models import MaterialChunk, MaterialEmbedding, MaterialRevision, RequirementFact


def goal(client):
    return client.post("/api/goals", json={"raw_input": "资料缓存验收"}).json()["goal_id"]


def upload(client, goal_id, filename, content):
    response = client.post(
        f"/api/goals/{goal_id}/materials/upload",
        params={"filename": filename},
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_cache_reuses_text_locators_and_vectors_with_independent_goal_scope(
    client, session_factory, monkeypatch
):
    first, second, third = goal(client), goal(client), goal(client)
    with session_factory() as session:
        original_fact_count = session.scalar(
            select(func.count())
            .select_from(RequirementFact)
            .where(RequirementFact.goal_id == second)
        )
    content = ("# Release\n\nOctober release requires rollback rehearsal.\n" * 90).encode()
    original = upload(client, first, "report.md", content)
    assert MaterialProcessingWorker(session_factory).run_once()
    settings = Settings(_env_file=None)
    with session_factory() as session:
        chunks = list(session.scalars(select(MaterialChunk)))
        for chunk in chunks:
            session.add(
                MaterialEmbedding(
                    chunk_id=chunk.id,
                    model=settings.material_embedding_identity,
                    content_hash=chunk.content_hash,
                    vector_json=[1.0, 0.0],
                )
            )
        session.commit()

    monkeypatch.setattr(
        "planner_api.materials.processing.parse_material",
        lambda *_a, **_kw: pytest.fail("cache hit must not parse"),
    )
    copied = upload(client, second, "renamed.md", content)
    revision = copied["latest_revision"]
    assert revision["status"] == revision["processing"]["status"] == "READY"
    assert revision["processing"]["attempts"] == 0
    assert revision["metadata"]["cache_reused"]
    assert revision["metadata"]["cache_source_revision_id"] == original["latest_revision"]["id"]
    assert revision["id"] != original["latest_revision"]["id"]
    assert copied["source_id"] != original["source_id"]
    path = f"/api/goals/{second}/materials/{copied['source_id']}"
    assert client.get(path + "/read").json()["text"] == content.decode().strip()
    assert client.get(path.replace(second, third) + "/read").status_code == 404
    assert (
        client.get(
            path + "/read", params={"revision_id": original["latest_revision"]["id"]}
        ).status_code
        == 404
    )
    with session_factory() as session:
        copied_chunks = list(
            session.scalars(
                select(MaterialChunk).where(MaterialChunk.revision_id == revision["id"])
            )
        )
        ids = [chunk.id for chunk in copied_chunks]
        assert session.scalar(
            select(func.count())
            .select_from(MaterialEmbedding)
            .where(MaterialEmbedding.chunk_id.in_(ids))
        ) == len(chunks)
        assert (
            session.scalar(
                select(func.count())
                .select_from(RequirementFact)
                .where(RequirementFact.goal_id == second)
            )
            == original_fact_count
        )
        hits = MaterialProcessingService(session, settings).search_goal_materials(
            second, MaterialSearchRequest(query="rollback")
        )
        assert hits and all(hit.revision_id == revision["id"] for hit in hits)
    assert not MaterialProcessingWorker(session_factory).run_once()
    assert client.delete(f"/api/goals/{first}/materials/{original['source_id']}").status_code == 204
    assert "rollback" in client.get(path + "/read").json()["text"]
    changed = upload(client, second, "renamed.md", b"changed bytes")
    assert changed["latest_revision"]["status"] == "QUEUED"
    assert changed["latest_revision"]["revision"] == 2
    repeated = upload(client, third, "another-copy.md", content)
    assert repeated["latest_revision"]["status"] == "READY"


@pytest.mark.parametrize("damage", ["failed", "partial", "chunks", "hash", "profile", "extension"])
def test_cache_rejects_failed_incomplete_corrupt_or_incompatible_results(
    client, session_factory, damage
):
    first, second = goal(client), goal(client)
    content = b"# Cache\n\nA fully readable original document."
    original = upload(client, first, "original.md", content)
    assert MaterialProcessingWorker(session_factory).run_once()
    with session_factory() as session:
        revision = session.get(MaterialRevision, original["latest_revision"]["id"])
        metadata = dict(revision.metadata_json)
        if damage == "failed":
            revision.status = "FAILED"
        elif damage == "partial":
            metadata["coverage"] = "PARTIAL"
        elif damage == "chunks":
            session.delete(session.scalar(select(MaterialChunk)))
        elif damage == "hash":
            session.scalar(select(MaterialChunk)).text = "corrupt text"
        elif damage == "profile":
            metadata["parse_cache_profile"] = {"schema": -1}
        elif damage == "extension":
            metadata["extension"] = ".txt"
        revision.metadata_json = metadata
        session.commit()
    copied = upload(client, second, "copy.md", content)
    assert copied["latest_revision"]["status"] == "QUEUED"


def test_duplicate_queued_documents_reuse_after_first_worker_finishes(
    client, session_factory, monkeypatch
):
    first, second = goal(client), goal(client)
    one = upload(client, first, "first.md", b"Shared queue document")
    two = upload(client, second, "second.md", b"Shared queue document")
    assert one["latest_revision"]["status"] == two["latest_revision"]["status"] == "QUEUED"
    worker = MaterialProcessingWorker(session_factory)
    assert worker.run_once()
    monkeypatch.setattr(
        "planner_api.materials.processing.parse_material",
        lambda *_a, **_kw: pytest.fail("second queued copy must reuse extraction"),
    )
    assert worker.run_once()
    revision = client.get(f"/api/goals/{second}/materials").json()[0]["latest_revision"]
    assert revision["status"] == "READY" and revision["metadata"]["cache_reused"]


def test_cached_mineru_pdf_reuses_original_pages_but_explicit_reparse_bypasses_cache(
    client, session_factory, pdf_bytes_factory, monkeypatch
):
    from planner_api.materials.parsers import ParsedLocation, ParsedMaterial

    settings = Settings(_env_file=None, material_mineru_enabled=True)
    monkeypatch.setattr("planner_api.materials.service.get_settings", lambda: settings)
    first, second = goal(client), goal(client)
    pdf = pdf_bytes_factory(["MinerU paper with formula and tables"])
    original = upload(client, first, "paper.pdf", pdf)
    parsed = ParsedMaterial(
        "Recognized paper",
        "mineru",
        "4.0.10",
        {"page_count": 1, "mineru_used": True, "coverage": "COMPLETE"},
        (ParsedLocation(0, 16, {"kind": "pdf_page", "page": 1, "extraction": "MinerU"}),),
    )
    monkeypatch.setattr(
        "planner_api.materials.processing.parse_material", lambda *_a, **_kw: parsed
    )
    worker = MaterialProcessingWorker(session_factory, settings)
    assert worker.run_once()
    # Pre-upgrade records have no cache profile; they remain reusable.
    with session_factory() as session:
        revision = session.get(MaterialRevision, original["latest_revision"]["id"])
        metadata = dict(revision.metadata_json)
        metadata.pop("parse_cache_profile")
        revision.metadata_json = metadata
        session.commit()
    cached = upload(client, second, "renamed-paper.pdf", pdf)
    assert cached["latest_revision"]["status"] == "READY"
    assert cached["latest_revision"]["parser_name"] == "mineru"
    with session_factory() as session:
        chunk = session.scalar(
            select(MaterialChunk).where(
                MaterialChunk.revision_id == cached["latest_revision"]["id"]
            )
        )
        assert chunk.locator_json[0]["page"] == 1
        MaterialProcessingService(session, settings).process_for_goal(
            second, cached["source_id"], "MINERU"
        )
    calls = []
    monkeypatch.setattr(
        "planner_api.materials.processing.parse_material",
        lambda *_a, **kw: calls.append(kw) or parsed,
    )
    assert worker.run_once()
    assert calls and calls[0]["mineru_mode"] == "FORCE"


def test_reuse_can_rebuild_keywords_without_using_stale_embedding_model(client, session_factory):
    first, second = goal(client), goal(client)
    original = upload(client, first, "first.md", b"evidence rollback")
    assert MaterialProcessingWorker(session_factory).run_once()
    with session_factory() as session:
        chunk = session.scalar(select(MaterialChunk))
        session.add(
            MaterialEmbedding(
                chunk_id=chunk.id,
                model="old-model@old-revision",
                content_hash=chunk.content_hash,
                vector_json=[1.0],
            )
        )
        session.connection().exec_driver_sql("DELETE FROM material_chunks_fts")
        session.commit()
    copied = upload(client, second, "copy.md", b"evidence rollback")
    revision = copied["latest_revision"]
    assert revision["status"] == "READY"
    with session_factory() as session:
        hits = MaterialProcessingService(session, Settings(_env_file=None)).search_goal_materials(
            second, MaterialSearchRequest(query="rollback")
        )
        assert hits and hits[0].revision_id == revision["id"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(MaterialEmbedding)
                .join(MaterialChunk)
                .where(MaterialChunk.revision_id == revision["id"])
            )
            == 0
        )
    assert original["latest_revision"]["id"] != revision["id"]
