"""Chat delivery must not wait for extraction or compromise requirement state."""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from planner_api.ai import ProviderUnavailableError
from planner_api.models import ClarificationAnalysisJob, Message, utc_now
from planner_api.schemas import ConversationReply, RequirementAnalysis, ProposedFact
from planner_api.service import ClarificationService


class Provider:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        return ConversationReply(assistant_message="先给你建议：按实际需要选。")

    def analyze(self, context, answer):
        self.contexts.append(context)
        return RequirementAnalysis(summary="已记下用户条件。", readiness={
            "ready_for_review": False, "reason": "继续讨论。"})


def jobs(session):
    return list(session.scalars(select(ClarificationAnalysisJob).order_by(
        ClarificationAnalysisJob.created_at, ClarificationAnalysisJob.id)))


def test_reply_is_durable_before_analysis_and_queue_survives_new_session(session_factory):
    provider = Provider()
    with session_factory() as session:
        workspace = ClarificationService(session, provider, defer_analysis=True).create_goal("选择空气炸锅")
        goal_id = workspace.goal_id
        assert workspace.messages[-1].generation_status == "SUCCEEDED"
        assert workspace.messages[-1].metadata["analysis_status"] == "PENDING"
        assert not provider.contexts
        assert jobs(session)[0].attempts == 0
    with session_factory() as session:
        service = ClarificationService(session, provider)
        assert service.process_pending_analysis(goal_id=goal_id) == (True, None)
        assert jobs(session)[0].status == "SUCCEEDED"
        assert service.get_workspace(goal_id).messages[-1].metadata["analysis_status"] == "SUCCEEDED"
    assert len(provider.contexts) == 1


def test_followups_remain_visible_and_extraction_is_ordered_without_future_messages(session_factory):
    provider = Provider()
    with session_factory() as session:
        service = ClarificationService(session, provider, defer_analysis=True)
        first = service.create_goal("选择空气炸锅")
        second = service.add_user_message(first.goal_id, "更正：预算只有300元")
        assert len(second.messages) == 4 and not provider.contexts
        older, newer = jobs(session)
        assert service.process_pending_analysis(newer.id) == (False, None)
        assert service.process_pending_analysis(older.id) == (True, None)
        assert not any("300元" in row["content"] for row in provider.contexts[0].recent_messages)
        assert service.process_pending_analysis(newer.id) == (True, None)
        assert provider.contexts[1].latest_user_message == "更正：预算只有300元"
        assert all(job.status == "SUCCEEDED" for job in jobs(session))


def test_pending_analysis_guards_formal_review_but_not_chat(session_factory):
    with session_factory() as session:
        service = ClarificationService(session, Provider(), defer_analysis=True)
        first = service.create_goal("整理学习安排")
        for method in (service.create_requirement_draft, service.confirm_requirement):
            with pytest.raises(HTTPException) as error:
                method(first.goal_id)
            assert error.value.detail["code"] == "CLARIFICATION_ANALYSIS_PENDING"
        assert service.add_user_message(first.goal_id, "先聊聊建议").messages[-1].generation_status == "SUCCEEDED"


def test_analysis_failure_retries_without_replacing_or_hiding_reply(session_factory):
    class FailsOnce(Provider):
        def analyze(self, context, answer):
            if not self.contexts:
                self.contexts.append(context)
                raise ProviderUnavailableError("暂时超时", code="MODEL_TIMEOUT")
            return super().analyze(context, answer)

    with session_factory() as session:
        service = ClarificationService(session, FailsOnce(), defer_analysis=True)
        first = service.create_goal("整理学习安排")
        original = first.messages[-1]
        worked, delay = service.process_pending_analysis()
        assert worked and delay
        reply = service.get_workspace(first.goal_id).messages[-1]
        assert reply.id == original.id and reply.content == original.content
        assert reply.generation_status == "SUCCEEDED"
        jobs(session)[0].next_retry_at = utc_now()
        session.commit()
        assert service.process_pending_analysis() == (True, None)
        assert jobs(session)[0].status == "SUCCEEDED"


def test_processing_turn_cannot_be_claimed_twice_or_overtaken(session_factory):
    with session_factory() as session:
        service = ClarificationService(session, Provider(), defer_analysis=True)
        first = service.create_goal("整理学习安排")
        service.add_user_message(first.goal_id, "每周只有两小时")
        older, newer = jobs(session)
        older.status = "PROCESSING"
        session.commit()
        assert service.process_pending_analysis() == (False, None)
        older.updated_at = utc_now() - timedelta(minutes=16)
        session.commit()
        assert service.process_pending_analysis() == (True, None)
        assert older.status == "SUCCEEDED" and newer.status == "PENDING"


def test_background_result_preserves_research_metadata_and_direct_panel_edits(session_factory):
    provider = Provider()
    with session_factory() as session:
        service = ClarificationService(session, provider, defer_analysis=True)
        first = service.create_goal("整理学习安排")
        fact = first.facts[0]
        service.update_fact(first.goal_id, fact.id, "UPDATE", "用户后来在面板里修改的目标")
        reply = session.get(Message, first.messages[-1].id)
        reply.metadata_json = {**reply.metadata_json, "research_response_request_id": "request-kept"}
        session.commit()
        def analyze(context, answer):
            return RequirementAnalysis(summary="旧提取", proposed_facts=[ProposedFact(
                category="OBJECTIVE", field_key="objective", value="旧的目标",
                source="USER", status="CONFIRMED", confidence=1)],
                readiness={"ready_for_review": False, "reason": "继续讨论"})
        provider.analyze = analyze
        assert service.process_pending_analysis() == (True, None)
        result = service.get_workspace(first.goal_id)
        assert any(f.value == "用户后来在面板里修改的目标" for f in result.facts)
        assert result.messages[-1].metadata["research_response_request_id"] == "request-kept"


def test_regeneration_during_analysis_discards_late_old_extraction(session_factory):
    provider = Provider()
    with session_factory() as session:
        service = ClarificationService(session, provider, defer_analysis=True)
        first = service.create_goal("整理学习安排")
        original = provider.analyze
        def regenerate(context, answer):
            provider.analyze = original
            service.regenerate_last_message(first.goal_id)
            return RequirementAnalysis(summary="迟到的旧结果", readiness={
                "ready_for_review": False, "reason": "旧结果"})
        provider.analyze = regenerate
        assert service.process_pending_analysis() == (True, None)
        assert jobs(session)[0].status == "PENDING"
        assert service.get_workspace(first.goal_id).messages[-1].metadata["analysis_status"] == "PENDING"
        assert service.process_pending_analysis() == (True, None)
        assert jobs(session)[0].status == "SUCCEEDED"


@pytest.mark.parametrize("provider_name", ["deepseek", "openai_compatible"])
def test_no_search_uses_one_model_call_and_preserves_cache_and_source_inheritance(provider_name):
    import json
    from planner_api.config import Settings
    from planner_api.research.intent import ModelResearchIntentAnalyzer
    calls = []
    def transport(url, headers, payload, timeout):
        calls.append(payload)
        return {"output_text": json.dumps({
            "should_search": False, "knowledge_sufficiency": "SUFFICIENT",
            "cache_coverage": "PARTIAL", "cached_evidence_ids": ["kb:one"],
            "cache_missing_aspects": ["最新价格未核实"], "topic_relation": "FOLLOW_UP",
            "inherit_source_restrictions": True,
            "brief": {"objective": "不必要的旧查询"},
            "planned_questions": [{"query": "多余的查询"}],
        }, ensure_ascii=False)}
    analyzer = ModelResearchIntentAnalyzer(Settings(_env_file=None,
        llm_provider=provider_name, llm_api_key="test-key"), transport=transport)
    decision = analyzer.analyze("根据刚才的材料给判断，不需要比较实时价格")
    assert len(calls) == 1
    assert not decision.should_search and not decision.planned_questions and decision.brief is None
    assert decision.cached_evidence_ids == ["kb:one"]
    assert decision.cache_missing_aspects == ["最新价格未核实"]
    assert decision.inherit_source_restrictions
