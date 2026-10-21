import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.materials.analysis_ai import (
    MaterialAnalysisContext,
    MaterialAnalysisPassage,
    RealMaterialAnalysisProvider,
)
from planner_api.materials.worker import MaterialProcessingWorker


def _goal(client: TestClient) -> str:
    response = client.post("/api/goals", json={"raw_input": "根据课程资料制定学习计划"})
    assert response.status_code == 201
    return response.json()["goal_id"]


def test_material_analysis_returns_citations_and_confirmed_findings_enter_memory(
    client: TestClient, tmp_path: Path, session_factory
) -> None:
    goal_id = _goal(client)
    document = tmp_path / "course.md"
    document.write_text(
        "# 课程要求\n\n必须完成三次阶段练习，并在月底前提交项目。\n\n"
        "# 评价方式\n\n每次练习需要留下复盘记录。",
        encoding="utf-8",
    )
    root = client.post(
        "/api/material-roots",
        json={"display_name": "课程资料", "path": str(tmp_path)},
    ).json()
    material = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    ).json()
    client.post(f"/api/goals/{goal_id}/materials/{material['source_id']}/process")
    assert MaterialProcessingWorker(session_factory).run_once() is True

    analysis = client.post(
        f"/api/goals/{goal_id}/materials/analyze",
        json={"query": "课程有哪些完成要求和评价方式？"},
    )
    assert analysis.status_code == 201, analysis.text
    body = analysis.json()
    assert body["status"] == "COMPLETED"
    assert body["key_points"]
    assert body["key_points"][0]["citations"]
    assert body["findings"]
    analysis_id = body["id"]

    confirmed = client.post(
        f"/api/goals/{goal_id}/materials/analyses/{analysis_id}/confirm",
        json={"finding_indexes": [0]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["findings"][0]["status"] == "CONFIRMED"

    from sqlalchemy import select
    from planner_api.models import RequirementFact

    with session_factory() as session:
        fact = session.scalar(
            select(RequirementFact).where(
                RequirementFact.goal_id == goal_id,
                RequirementFact.source == "MATERIAL_CONFIRMED",
            )
        )
        assert fact is not None
        assert fact.status == "CONFIRMED"
        assert fact.value_json["citations"]


def test_material_analysis_requires_ready_material(client: TestClient, tmp_path: Path) -> None:
    goal_id = _goal(client)
    document = tmp_path / "pending.md"
    document.write_text("尚未解析的课程资料", encoding="utf-8")
    root = client.post(
        "/api/material-roots",
        json={"display_name": "待处理资料", "path": str(tmp_path)},
    ).json()
    material = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": document.name},
    ).json()
    assert material["latest_revision"]["status"] == "QUEUED"
    response = client.post(
        f"/api/goals/{goal_id}/materials/analyze",
        json={"query": "这份资料讲什么？"},
    )
    assert response.status_code == 409


def test_real_material_analysis_retries_truncated_json() -> None:
    valid = {
        "summary": "已定位到关键交付约束。",
        "key_points": [],
        "findings": [],
        "open_questions": [],
    }
    truncated = '{"summary":"被截断'
    responses = [
        {"choices": [{"message": {"content": truncated}}]},
        {"choices": [{"message": {"content": json.dumps(valid)}}]},
    ]

    def transport(*_args):
        return responses.pop(0)

    provider = RealMaterialAnalysisProvider(
        Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key=SecretStr("test-key"),
            llm_max_retries=1,
        ),
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    result = provider.analyze(
        MaterialAnalysisContext(
            query="交付日期是什么？",
            passages=[
                MaterialAnalysisPassage(
                    passage_id="p1",
                    source_id="source",
                    revision_id="revision",
                    sequence=0,
                    section_title="交付要求",
                    locator="chunk:0",
                    text="必须在月底前完成。",
                )
            ],
        )
    )
    assert result.summary == "已定位到关键交付约束。"
    assert not responses


def test_fast_query_policy_does_not_change_evidence_answer_policy():
    calls = []
    def transport(_url, _headers, payload, _timeout):
        from copy import deepcopy
        calls.append(deepcopy(payload))
        result = ({'queries': ['测试环境重试与导出上限，排除生产环境']}
                  if len(calls) == 1 else
                  {'summary': '测试12次、1536 MiB', 'key_points': [], 'findings': [], 'open_questions': []})
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
    provider = RealMaterialAnalysisProvider(
        Settings(_env_file=None, llm_provider='deepseek', llm_api_key='test'),
        transport=transport, sleeper=lambda _: None)
    question = '只查测试环境，排除生产：重试次数和导出上限？'
    assert provider.plan_queries(question) == ['测试环境重试与导出上限，排除生产环境']
    provider.analyze(MaterialAnalysisContext(query=question, passages=[
        MaterialAnalysisPassage('p1', 's1', 'r1', 0, '配置', 'chunk:0', '测试12次，1536 MiB')]))
    assert calls[0]['thinking'] == {'type': 'disabled'} and calls[0]['max_tokens'] == 1024
    assert question in calls[0]['messages'][1]['content']
    assert 'thinking' not in calls[1] and calls[1]['max_tokens'] >= 6000


def test_query_fast_failure_falls_back_to_complete_original_request():
    from urllib.error import HTTPError
    question = '比较A和B，排除旧版，不足时明确不知道，不得用宣传数字'
    def transport(*_args):
        raise HTTPError('https://example.invalid', 400, 'unsupported', {}, None)
    provider = RealMaterialAnalysisProvider(
        Settings(_env_file=None, llm_provider='deepseek', llm_api_key='test'),
        transport=transport, sleeper=lambda _: None)
    assert provider.plan_queries(question) == [question]


def test_length_stop_retries_even_if_json_is_parseable_and_expands_output_only():
    from copy import deepcopy

    valid = {"summary": "余量未知，先确认已有负荷。", "key_points": [], "findings": [], "open_questions": []}
    calls = []

    def transport(_url, _headers, payload, _timeout):
        calls.append(deepcopy(payload))
        return {"choices": [{"finish_reason": "length" if len(calls) == 1 else "stop",
                              "message": {"content": json.dumps(valid)}}]}

    provider = RealMaterialAnalysisProvider(
        Settings(_env_file=None, llm_provider="deepseek", llm_api_key="test", llm_max_retries=1),
        transport=transport, sleeper=lambda _: None,
    )
    context = MaterialAnalysisContext(query="能否承接50分钟？", passages=[
        MaterialAnalysisPassage("p1", "s1", "r1", 0, "容量", "chunk:0", "第3周总容量180分钟，已有任务占用未知。")])
    result = provider.analyze(context)
    assert "余量未知" in result.summary
    assert [c["max_tokens"] for c in calls] == [6000, 16000]
    assert calls[0]["messages"][1] == calls[1]["messages"][1]
    assert "保留用户要求" in calls[1]["messages"][-1]["content"]


def test_payment_failure_returns_actionable_error_without_repeated_paid_calls():
    from planner_api.ai import ProviderUnavailableError

    attempts = []

    def transport(url, *_args):
        attempts.append(url)
        raise HTTPError(url, 402, "Payment required", {}, None)

    provider = RealMaterialAnalysisProvider(
        Settings(_env_file=None, llm_provider="deepseek", llm_api_key="test", llm_max_retries=2),
        transport=transport, sleeper=lambda _: pytest.fail("payment failure should not wait/retry"),
    )
    with pytest.raises(ProviderUnavailableError, match="额度不足") as caught:
        provider.analyze(MaterialAnalysisContext("解释资料", []))
    assert len(attempts) == 1
    assert caught.value.diagnostic == "HTTP 402"
