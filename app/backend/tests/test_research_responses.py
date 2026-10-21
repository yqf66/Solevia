from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from planner_api.models import Goal, Message, ResearchResponseRequest, ResearchRun, utc_now
from planner_api.research.context import compile_research_context
from planner_api.research.response_schemas import ResearchResponseCreate
from planner_api.research.responses import ResearchResponseService
from planner_api.research.schemas import ResearchBrief, ResearchPacket


def create(session, phase="PLAN", intent="EXPLAIN", question="查官方资料解释安排"):
    goal = Goal(title="隔离研究验收", raw_input="验证研究到回答的闭环")
    session.add(goal)
    session.commit()
    service = ResearchResponseService(session)
    payload = ResearchResponseCreate(question=question, phase=phase, intent=intent, idempotency_key=str(uuid4()))
    response = service.create(goal.id, payload)
    return service, goal, payload, response


def complete_run(session, response, status="READY"):
    run = session.get(ResearchRun, response.run.id)
    run.status = status
    run.result_json = ResearchPacket(run_id=run.id, question=run.question, status=status).model_dump(mode="json")
    session.commit()


def record_answer(service, row, *, regenerate):
    messages = list(service.session.scalars(select(Message).where(Message.conversation_id == row.conversation_id).order_by(Message.sequence)))
    if not regenerate:
        service.session.add(Message(conversation_id=row.conversation_id, sequence=len(messages) + 1, role="USER", content=row.question))
    assistant = messages[-1] if regenerate and messages[-1].role == "ASSISTANT" else Message(
        conversation_id=row.conversation_id, sequence=len(messages) + (1 if regenerate else 2), role="ASSISTANT", content="",
    )
    assistant.content = "基于本次资料的回答"
    assistant.generation_status = "SUCCEEDED"
    service.session.add(assistant)
    service.session.commit()


@pytest.mark.parametrize("phase,intent", [("CLARIFICATION", "MESSAGE"), ("ROUTE", "EXPLAIN"), ("PLAN", "REVISE"), ("EXECUTION", "MESSAGE"), ("RECOVERY", "EXPLAIN")])
def test_refresh_reuses_persisted_request_and_answers_exactly_once(session_factory, monkeypatch, phase, intent):
    calls = []
    def answer(service, row, *, regenerate):
        calls.append(row.run_id)
        record_answer(service, row, regenerate=regenerate)
    monkeypatch.setattr(ResearchResponseService, "dispatch", answer)
    with session_factory() as session:
        service, goal, payload, response = create(session, phase, intent)
        assert service.create(goal.id, payload).id == response.id
        complete_run(session, response, "PARTIAL")
        request_id, goal_id = response.id, goal.id
    with session_factory() as session:
        service = ResearchResponseService(session)
        assert service.list(goal_id)[0].id == request_id
        assert service.process_next()
        assert not service.process_next()
        result = service.get(goal_id, request_id)
        assert result.status == "COMPLETED"
        assert result.response_message_id
        assert result.run.id == response.run.id
        assert calls == [response.run.id]
        assert len(list(session.scalars(select(Message)))) == 2


def test_scope_and_full_question_are_part_of_idempotency(session_factory):
    with session_factory() as session:
        service, goal, payload, response = create(session, question="a" * 80 + "结尾一")
        with pytest.raises(HTTPException) as error:
            service.create(goal.id, payload.model_copy(update={"question": "a" * 80 + "结尾二"}))
        assert error.value.status_code == 409
        other = Goal(title="另一个规划", raw_input="不能访问前一个规划的研究")
        session.add(other)
        session.commit()
        with pytest.raises(HTTPException) as error:
            service.get(other.id, response.id)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException):
            service.create(other.id, payload)


def test_cancel_does_not_answer_or_restart_on_refresh(session_factory, monkeypatch):
    monkeypatch.setattr(ResearchResponseService, "dispatch", lambda *args, **kwargs: pytest.fail("Cancelled request must not answer"))
    with session_factory() as session:
        service, goal, payload, response = create(session)
        result = service.action(goal.id, response.id, "cancel")
        assert result.status == "CANCELLED"
        assert result.run.status == "CANCELLED"
        assert not service.process_next()
        assert service.create(goal.id, payload).status == "CANCELLED"
        result = service.action(goal.id, response.id, "retry-research")
        assert result.status == "WAITING_RESEARCH"
        assert result.run.status == "QUEUED"
        assert result.run.id == response.run.id


def test_failed_research_still_answers_with_failure_context(session_factory, monkeypatch):
    calls = []
    def answer(service, row, *, regenerate):
        assert not row.fallback  # Failed packet stays visible; no fake success.
        calls.append(row.id)
        record_answer(service, row, regenerate=regenerate)
    monkeypatch.setattr(ResearchResponseService, "dispatch", answer)
    with session_factory() as session:
        service, goal, _, response = create(session)
        complete_run(session, response, "FAILED")
        assert service.process_next()
        result = service.get(goal.id, response.id)
        assert result.status == "COMPLETED" and result.run.status == "FAILED"
        assert calls == [response.id]
        assert not service.process_next()


def test_retry_answer_reuses_evidence_and_user_turn(session_factory, monkeypatch):
    count = 0
    def answer(service, row, *, regenerate):
        nonlocal count
        count += 1
        record_answer(service, row, regenerate=regenerate)
        if count == 1:
            message = service.session.scalar(select(Message).where(Message.role == "ASSISTANT"))
            message.generation_status = "FAILED"
            service.session.commit()
    monkeypatch.setattr(ResearchResponseService, "dispatch", answer)
    with session_factory() as session:
        service, goal, _, response = create(session)
        complete_run(session, response)
        assert service.process_next()
        assert service.get(goal.id, response.id).status == "ANSWER_FAILED"
        service.action(goal.id, response.id, "retry-answer")
        assert service.process_next()
        result = service.get(goal.id, response.id)
        assert result.status == "COMPLETED"
        assert result.run.attempt_count == 0
        assert result.attempt_count == 2
        assert len(list(session.scalars(select(Message)))) == 2


def test_historical_false_ready_retry_uses_effective_evidence_status(session_factory):
    with session_factory() as session:
        service, goal, _, response = create(session)
        run = session.get(ResearchRun, response.run.id)
        packet = ResearchPacket(question=run.question, status="READY",
                                brief=ResearchBrief(required_aspects=["尚未取得完整规则"]))
        run.status = "READY"
        run.result_json = packet.model_dump(mode="json")
        run.checkpoint_json = dict(run.result_json)
        request = session.get(ResearchResponseRequest, response.id)
        request.status = "AWAITING_DECISION"
        session.commit()
        assert service.get(goal.id, response.id).run.status == "PARTIAL"
        result = service.action(goal.id, response.id, "retry-research")
        assert result.status == "WAITING_RESEARCH" and result.run.status == "QUEUED"
        assert result.run.checkpoint == packet.model_dump(mode="json")


@pytest.mark.parametrize("change", ["plan", "conversation"])
def test_stale_request_cannot_override_changed_plan_or_later_discussion(session_factory, monkeypatch, change):
    monkeypatch.setattr(ResearchResponseService, "dispatch", lambda *args, **kwargs: pytest.fail("Stale request must not dispatch"))
    with session_factory() as session:
        service, goal, _, response = create(session)
        complete_run(session, response)
        if change == "plan":
            goal.active_plan_version_id = "another-plan-version"
        else:
            session.add(Message(conversation_id=response.conversation_id, sequence=1, role="USER", content="后来的另一个问题"))
        session.commit()
        assert service.process_next()
        assert service.get(goal.id, response.id).status == "ANSWER_FAILED"


def test_expired_answer_lease_requires_explicit_retry(session_factory, monkeypatch):
    monkeypatch.setattr(ResearchResponseService, "dispatch", record_answer)
    with session_factory() as session:
        service, goal, _, response = create(session)
        complete_run(session, response)
        row = session.get(ResearchResponseRequest, response.id)
        row.status = "ANSWERING"
        row.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
        assert service.process_next()
        assert service.get(goal.id, response.id).status == "INTERRUPTED"
        assert not service.process_next()
        service.action(goal.id, response.id, "retry-answer")
        assert service.process_next()
        assert service.get(goal.id, response.id).status == "COMPLETED"


def test_recovery_evidence_cannot_override_user_constraints():
    context = compile_research_context(ResearchPacket(question="恢复练习", status="PARTIAL"), phase="RECOVERY")
    assert context["research_phase"] == "RECOVERY"
    assert "目标、期限、恢复范围与已完成历史不可被外部建议覆盖" in context["source_handling_rule"]


def test_direct_recovery_accepts_scoped_evidence_without_publishing(client, session_factory):
    from planner_api.models import PlanningBrief
    from test_recovery_scope import preview_selection, scoped_recovery

    goal_id, plan_id, (selected, deferred, _) = scoped_recovery(client, session_factory)
    preview = preview_selection(client, goal_id, plan_id, [selected]).json()
    with session_factory() as session:
        run = ResearchRun(goal_id=goal_id, question="如何恢复", status="READY")
        session.add(run)
        session.flush()
        run.result_json = ResearchPacket(run_id=run.id, question=run.question, status="READY").model_dump(mode="json")
        session.commit()
        run_id = run.id
    response = client.post(f"/api/goals/{goal_id}/execution/recovery/ai", json={
        "preview_id": preview["id"], "missed_dates": [], "backlog_minutes": 0,
        "original_end_date": preview["original_end_date"], "research_run_id": run_id,
    })
    assert response.status_code == 200, response.text
    with session_factory() as session:
        brief = session.scalar(select(PlanningBrief).where(
            PlanningBrief.goal_id == goal_id, PlanningBrief.brief_kind == "RECOVERY",
        ).order_by(PlanningBrief.version.desc()))
        context = brief.brief_json["recovery_context"]
        assert context["_research_context"]["research_phase"] == "RECOVERY"
        assert context["recovery_scope"]["selected_task_ids"] == [selected]
        assert deferred in context["recovery_scope"]["deferred_task_ids"]
        assert session.get(Goal, goal_id).active_plan_version_id == plan_id
        assert response.json()["messages"][-1]["metadata"]["research_run_id"] == run_id


@pytest.mark.parametrize("foreign", [True, False])
def test_direct_recovery_rejects_foreign_or_running_research_before_writes(client, session_factory, foreign):
    from planner_api.models import PlanningBrief
    from sqlalchemy import func
    from test_recovery_scope import scoped_recovery

    goal_id, _, _ = scoped_recovery(client, session_factory)
    with session_factory() as session:
        other = Goal(title="另一规划", raw_input="隔离")
        session.add(other)
        session.flush()
        run = ResearchRun(goal_id=other.id if foreign else goal_id, question="恢复", status="READY" if foreign else "RUNNING")
        session.add(run)
        session.commit()
        before = session.scalar(select(func.count()).select_from(PlanningBrief))
        run_id = run.id
    response = client.post(f"/api/goals/{goal_id}/execution/recovery/ai", json={
        "missed_dates": [], "backlog_minutes": 0, "original_end_date": "2026-10-30", "research_run_id": run_id,
    })
    assert response.status_code == (404 if foreign else 409), response.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlanningBrief)) == before


def test_cannot_restart_old_request_over_new_active_request(session_factory):
    with session_factory() as session:
        service, goal, payload, response = create(session)
        service.action(goal.id, response.id, "cancel")
        service.create(goal.id, payload.model_copy(update={"question": "新的问题", "idempotency_key": str(uuid4())}))
        with pytest.raises(HTTPException) as error:
            service.action(goal.id, response.id, "ordinary")
        assert error.value.status_code == 409
        assert service.get(goal.id, response.id).status == "CANCELLED"


def test_response_migration_preserves_existing_data_and_can_rollback():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = Path(__file__).parents[1] / "migrations/versions/0061_research_response_requests.py"
    spec = importlib.util.spec_from_file_location("migration_0061", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    index_path = path.with_name("0062_research_response_active_goal.py")
    index_spec = importlib.util.spec_from_file_location("migration_0062", index_path)
    index_migration = importlib.util.module_from_spec(index_spec)
    index_spec.loader.exec_module(index_migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE goals (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE conversations (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE research_runs (id VARCHAR(36) PRIMARY KEY, question TEXT)")
        connection.exec_driver_sql("INSERT INTO research_runs VALUES ('existing', '已有研究不得改变')")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            index_migration.upgrade()
            index_migration.upgrade()
        assert connection.exec_driver_sql("SELECT question FROM research_runs").scalar_one() == "已有研究不得改变"
        assert "research_response_requests" in inspect(connection).get_table_names()
        indexes = inspect(connection).get_indexes("research_response_requests")
        assert any(index["name"] == "uq_research_response_active_goal" and index["unique"] for index in indexes)
        with Operations.context(MigrationContext.configure(connection)):
            index_migration.downgrade()
            migration.downgrade()
        assert "research_response_requests" not in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("SELECT question FROM research_runs").scalar_one() == "已有研究不得改变"


def test_worker_claimed_retry_passes_checkpoint_and_decision_context(session_factory, monkeypatch):
    from planner_api.research.persistence import ResearchRunService
    from planner_api.research.worker import ResearchWorker

    captured = {}
    def run_claimed(service, row, **kwargs):
        captured.update(kwargs)
        return service._read(row)
    monkeypatch.setattr(ResearchRunService, "_run_claimed", run_claimed)
    with session_factory() as session:
        _, goal, _, response = create(session)
        row = session.get(ResearchRun, response.run.id)
        row.checkpoint_json = ResearchPacket(run_id=row.id, question=row.question, status="PARTIAL").model_dump(mode="json")
        row.resume_stage = "READING"
        session.commit()
        goal_id, run_id, lease_id = ResearchWorker(session_factory)._claim_next_run()
        session.expire_all()
        ResearchRunService(session).process_queued(goal_id, run_id, lease_id=lease_id,
                                                  decision_context={"phase": "RECOVERY"})
        assert captured["checkpoint_packet"].run_id == run_id
        assert captured["resume_stage"] == "READING"
        assert captured["decision_context"]["phase"] == "RECOVERY"


def test_attachment_revision_change_rejects_old_request(session_factory, monkeypatch):
    from planner_api.models import GoalMaterialLink, MaterialRevision, MaterialRoot, MaterialSource

    monkeypatch.setattr(ResearchResponseService, "dispatch", lambda *args, **kwargs: pytest.fail("Changed attachment must not answer"))
    with session_factory() as session:
        service, goal, _, response = create(session)
        service.action(goal.id, response.id, "cancel")
        root = MaterialRoot(display_name="合成资料", canonical_path="C:/synthetic-p1", status="ACTIVE")
        session.add(root)
        session.flush()
        source = MaterialSource(root_id=root.id, relative_path="paper.md", canonical_uri="local://synthetic/paper.md", title="合成资料", mime_type="text/markdown")
        session.add(source)
        session.flush()
        revision = MaterialRevision(source_id=source.id, revision=1, content_hash="a" * 64, file_size=10, status="READY")
        session.add_all([revision, GoalMaterialLink(goal_id=goal.id, source_id=source.id)])
        session.commit()
        response = service.create(goal.id, ResearchResponseCreate(
            phase="PLAN", intent="EXPLAIN", question="解释资料", material_source_ids=[source.id], idempotency_key=str(uuid4()),
        ))
        complete_run(session, response)
        session.add(MaterialRevision(source_id=source.id, revision=2, content_hash="b" * 64, file_size=20, status="READY"))
        session.commit()
        assert service.process_next()
        assert service.get(goal.id, response.id).status == "ANSWER_FAILED"


def test_requests_api_restores_and_isolates(client, session_factory):
    with session_factory() as session:
        goal = Goal(title="API闭环", raw_input="刷新恢复")
        session.add(goal)
        session.commit()
        goal_id = goal.id
    path = f"/api/goals/{goal_id}/research/requests"
    payload = {"phase": "PLAN", "intent": "EXPLAIN", "question": "查询官方资料", "idempotency_key": str(uuid4())}
    queued = client.post(path, json=payload)
    assert queued.status_code == 202
    request_id = queued.json()["id"]
    assert client.get(path).json()[0]["id"] == request_id
    assert client.post(path, json=payload).json()["id"] == request_id
    assert client.post(f"{path}/{request_id}/cancel").json()["status"] == "CANCELLED"
    assert client.get(f"/api/goals/other/research/requests/{request_id}").status_code == 404


@pytest.mark.parametrize("phase", ["CLARIFICATION", "ROUTE", "PLAN", "EXECUTION", "RECOVERY"])
def test_real_service_dispatch_contract_offline(client, session_factory, monkeypatch, phase):
    from planner_api.config import Settings
    from test_execution_discussion import _started_goal
    from test_plan import _completed_plan
    from test_route import _confirmed_requirement

    if phase in {"EXECUTION", "RECOVERY"}:
        goal_id = _started_goal(client)
    elif phase == "PLAN":
        goal_id = _completed_plan(client)["goal_id"]
    elif phase == "ROUTE":
        goal_id = _confirmed_requirement(client)["goal_id"]
        assert client.post(f"/api/goals/{goal_id}/route/draft").status_code == 200
    else:
        goal_id = client.post("/api/goals", json={"raw_input": "准备英语考试，每周学习三天"}).json()["goal_id"]
    monkeypatch.setattr("planner_api.config.get_settings", lambda: Settings(_env_file=None, llm_provider="fake", llm_api_key=""))
    with session_factory() as session:
        service = ResearchResponseService(session)
        response = service.create(goal_id, ResearchResponseCreate(
            phase=phase, intent="MESSAGE" if phase in {"EXECUTION", "CLARIFICATION"} else "EXPLAIN",
            question="为什么应当保留复习缓冲？只解释，不要修改安排。", idempotency_key=str(uuid4()),
        ))
        complete_run(session, response)
        assert service.process_next()
        result = service.get(goal_id, response.id)
        assert result.status == "COMPLETED", result.error_message
        assert session.get(Message, result.response_message_id).metadata_json["research_run_id"] == response.run.id
