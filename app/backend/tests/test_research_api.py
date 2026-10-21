from fastapi.testclient import TestClient

from planner_api.models import Goal, ResearchRun
from planner_api.research.consumer import ResearchContextService, research_summary_from_run
from planner_api.research.schemas import ResearchPacket, ResearchSource


def _goal(session_factory) -> str:
    with session_factory() as session:
        goal = Goal(title="研究任务 API", raw_input="验证异步联网研究任务")
        session.add(goal)
        session.commit()
        return goal.id


def test_research_task_api_exposes_queue_status_and_cancel(
    client: TestClient, session_factory
) -> None:
    goal_id = _goal(session_factory)

    response = client.post(
        f"/api/goals/{goal_id}/research/runs",
        json={
            "question": "验证异步任务状态",
            "enabled": True,
            "idempotency_key": "api-research-1",
        },
    )

    assert response.status_code == 202
    queued = response.json()
    assert queued["status"] == "QUEUED"

    fetched = client.get(f"/api/goals/{goal_id}/research/runs/{queued['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["question"] == "验证异步任务状态"

    cancelled = client.post(
        f"/api/goals/{goal_id}/research/runs/{queued['id']}/cancel",
        json={},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


def test_research_task_api_idempotency_returns_existing_queue(
    client: TestClient, session_factory
) -> None:
    goal_id = _goal(session_factory)
    path = f"/api/goals/{goal_id}/research/runs"
    payload = {
        "question": "同一研究任务",
        "enabled": True,
        "idempotency_key": "api-research-idempotent",
    }

    first = client.post(path, json=payload)
    second = client.post(path, json=payload)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get(path).json()) == 1


def test_followup_reuses_latest_research_and_projects_sources(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="研究复用", raw_input="验证后续对话复用资料")
        session.add(goal)
        session.flush()
        packet = ResearchPacket(
            run_id="research-reuse-packet",
            question="招聘市场能力要求",
            status="PARTIAL",
            sources=[
                ResearchSource(
                    title="示例招聘资料",
                    url="https://example.com/research",
                    canonical_url="https://example.com/research",
                    snippet="搜索摘要",
                    content_excerpt="正文摘录",
                    read_status="SUCCEEDED",
                    retrieval_method="request",
                    evidence_level="FULL_TEXT",
                )
            ],
        )
        run = ResearchRun(
            goal_id=goal.id,
            question=packet.question,
            enabled=True,
            status="PARTIAL",
            current_stage="COMPLETED",
            result_json=packet.model_dump(mode="json"),
        )
        session.add(run)
        session.commit()

        context, run_id, status = ResearchContextService(session).prepare(
            goal.id,
            "继续基于招聘资料回答",
            phase="CLARIFICATION",
            enabled=False,
        )
        summary = research_summary_from_run(run)

        assert run_id == run.id
        assert status == "PARTIAL"
        assert context is not None
        assert context["untrusted_external_sources"][0]["url"] == "https://example.com/research"
        assert summary is not None
        assert summary["sources"][0]["evidence_level"] == "FULL_TEXT"
