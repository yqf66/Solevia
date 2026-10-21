from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from planner_api.ai import ProviderUnavailableError
from planner_api.materials.analysis_ai import (
    FakeMaterialAnalysisProvider,
    MaterialAnalysisConflict,
    MaterialAnalysisFinding,
    MaterialAnalysisResult,
)
from planner_api.materials.analysis_service import MaterialAnalysisService
from planner_api.materials.schemas import MaterialAnalyzeRequest
from planner_api.materials.worker import MaterialProcessingWorker
from planner_api.models import (
    MaterialProcessingJob,
    MaterialRevision,
    MaterialSource,
    RequirementFact,
)


def setup_materials(client, tmp_path: Path, files: dict[str, str]):
    goal_id = client.post("/api/goals", json={"raw_input": "根据多份技术资料规划交付"}).json()[
        "goal_id"
    ]
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    root = client.post(
        "/api/material-roots", json={"display_name": "技术资料", "path": str(tmp_path)}
    ).json()
    return goal_id, root["id"]


def import_batch(client, goal_id, root_id, paths):
    response = client.post(
        f"/api/goals/{goal_id}/materials/local/batch",
        json={"root_id": root_id, "relative_paths": paths},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_scan_recursive_metadata_and_exclusions(client, tmp_path):
    goal_id, root_id = setup_materials(
        client,
        tmp_path,
        {
            "brief.md": "目标",
            "nested/spec.txt": "性能",
            "unknown.bin": "binary",
            ".hidden.md": "隐藏",
            ".git/private.md": "隐藏",
            "node_modules/dependency.md": "依赖",
        },
    )
    result = client.get(f"/api/goals/{goal_id}/materials/roots/{root_id}/scan").json()
    files = {item["relative_path"]: item for item in result["files"]}
    assert set(files) == {"brief.md", "nested/spec.txt", "unknown.bin"}
    assert files["nested/spec.txt"]["supported"]
    assert not files["unknown.bin"]["supported"]
    assert files["unknown.bin"]["reason"]
    assert not files["brief.md"]["linked"]
    import_batch(client, goal_id, root_id, ["brief.md"])
    scanned = client.get(f"/api/goals/{goal_id}/materials/roots/{root_id}/scan").json()
    assert next(item for item in scanned["files"] if item["relative_path"] == "brief.md")["linked"]
    client.delete(f"/api/material-roots/{root_id}")
    assert client.get(f"/api/goals/{goal_id}/materials/roots/{root_id}/scan").status_code == 409


def test_batch_partial_failures_idempotence_and_independent_parsing(
    client, tmp_path, session_factory
):
    goal_id, root_id = setup_materials(
        client, tmp_path, {"good.md": "交付期限是六月底", "bad.json": ""}
    )
    result = import_batch(
        client, goal_id, root_id, ["good.md", "bad.json", "good.md", "../outside.md", "missing.md"]
    )
    assert len(result["imported"]) == 2
    assert len(result["errors"]) == 2
    repeated = import_batch(client, goal_id, root_id, ["good.md", "bad.json"])
    assert [item["source_id"] for item in repeated["imported"]] == [
        item["source_id"] for item in result["imported"]
    ]
    with session_factory() as session:
        for model in (MaterialSource, MaterialRevision, MaterialProcessingJob):
            assert session.scalar(select(func.count()).select_from(model)) == 2
    worker = MaterialProcessingWorker(session_factory)
    assert worker.run_once()
    assert worker.run_once()
    statuses = {
        item["title"]: item["latest_revision"]["status"]
        for item in client.get(f"/api/goals/{goal_id}/materials").json()
    }
    assert statuses == {"good.md": "READY", "bad.json": "FAILED"}


def test_search_tail_terms_and_scope(client, tmp_path, session_factory):
    text = " ".join(f"term{index}" for index in range(90)) + " 尾部唯一关键词 nebula_capacity 210"
    goal_id, root_id = setup_materials(
        client, tmp_path, {"long.md": text, "other.md": "nebula_capacity 500"}
    )
    materials = import_batch(client, goal_id, root_id, ["long.md", "other.md"])["imported"]
    worker = MaterialProcessingWorker(session_factory)
    while worker.run_once():
        pass
    response = client.post(
        f"/api/goals/{goal_id}/materials/search",
        json={"query": "nebula_capacity", "source_ids": [materials[0]["source_id"]]},
    )
    assert response.status_code == 200
    assert response.json() and all(
        item["source_id"] == materials[0]["source_id"] for item in response.json()
    )
    assert "210" in response.json()[0]["text"]
    assert (
        client.post(
            f"/api/goals/{goal_id}/materials/search",
            json={"query": "nebula_capacity", "source_ids": ["foreign-source"]},
        ).status_code
        == 422
    )


class ConflictProvider(FakeMaterialAnalysisProvider):
    def plan_queries(self, query):
        return ["交付期限", "人力预算", "性能验收"]

    def analyze(self, context):
        by_name = {item.source_title: item for item in context.passages}
        assert {"a.md", "b.md", "capacity.md", "performance.md"} <= by_name.keys()
        ids = [by_name[name].passage_id for name in ("a.md", "b.md")]
        return MaterialAnalysisResult(
            summary="交付期限存在冲突。",
            conflicts=[
                MaterialAnalysisConflict(description="六月与七月的交付期限冲突", citation_ids=ids)
            ],
            findings=[
                MaterialAnalysisFinding(text="六月交付", citation_ids=[ids[0]]),
                MaterialAnalysisFinding(text="没有来源", citation_ids=["invented"]),
            ],
        )


def test_compound_analysis_conflicts_and_confirmation_guard(client, tmp_path, session_factory):
    goal_id, root_id = setup_materials(
        client,
        tmp_path,
        {
            "a.md": "交付期限六月",
            "b.md": "交付期限七月",
            "capacity.md": "人力预算210小时",
            "performance.md": "性能验收p95 250ms",
        },
    )
    import_batch(client, goal_id, root_id, ["a.md", "b.md", "capacity.md", "performance.md"])
    worker = MaterialProcessingWorker(session_factory)
    while worker.run_once():
        pass
    with session_factory() as session:
        service = MaterialAnalysisService(session, ConflictProvider())
        result = service.analyze(
            goal_id, MaterialAnalyzeRequest(query="交付期限、人力预算和性能验收分别是什么？")
        )
        assert result.file_count == 4
        assert all(item.retrieved_chunks for item in result.retrieval_coverage)
        assert len(result.conflicts[0].citations) == 2
        assert all(item.status == "BLOCKED" for item in result.findings)
        assert result.conflicts[0].citations[0].source_title
        from planner_api.materials.schemas import MaterialFindingConfirmRequest

        with pytest.raises(HTTPException) as error:
            service.confirm_findings(
                goal_id, result.id, MaterialFindingConfirmRequest(finding_indexes=[0, 1])
            )
        assert error.value.status_code == 409
        assert (
            session.scalar(
                select(func.count())
                .select_from(RequirementFact)
                .where(RequirementFact.source == "MATERIAL_CONFIRMED")
            )
            == 0
        )


def test_provider_failure_returns_retryable_error_without_analysis(
    client, tmp_path, session_factory
):
    goal_id, root_id = setup_materials(client, tmp_path, {"brief.md": "交付期限六月"})
    import_batch(client, goal_id, root_id, ["brief.md"])
    assert MaterialProcessingWorker(session_factory).run_once()

    class Unavailable(FakeMaterialAnalysisProvider):
        def analyze(self, context):
            raise ProviderUnavailableError("模型超时，请重试")

    with session_factory() as session:
        service = MaterialAnalysisService(session, Unavailable())
        with pytest.raises(HTTPException) as error:
            service.analyze(goal_id, MaterialAnalyzeRequest(query="交付期限"))
        assert error.value.status_code == 503
        assert service.list_analyses(goal_id) == []


def test_topic_heading_preserves_deadline_evidence_among_repeated_appendices(
    client, tmp_path, session_factory
):
    files = {
        "delivery.md": "# 交付\n验收目标：2027年6月30日前完成第一阶段上线。",
        "draft.md": "# 另一份排期\n项目管理草案将第一阶段交付日期写为2027年7月31日。",
        **{
            f"appendix-{index}.md": f"# Appendix {index}\n接口示例 X{index}，本附录无交付、人力、性能或培训信息。"
            for index in range(30)
        },
    }
    goal_id, root_id = setup_materials(client, tmp_path, files)
    import_batch(client, goal_id, root_id, list(files))
    worker = MaterialProcessingWorker(session_factory)
    while worker.run_once():
        pass

    class Capture(FakeMaterialAnalysisProvider):
        def plan_queries(self, query):
            return ["交付日期 交付时间 里程碑 项目排期 关键日期"]

        def analyze(self, context):
            assert {"delivery.md", "draft.md"} <= {item.source_title for item in context.passages}
            return super().analyze(context)

    with session_factory() as session:
        MaterialAnalysisService(session, Capture()).analyze(
            goal_id, MaterialAnalyzeRequest(query="不同文件对交付日期是否存在冲突？")
        )
