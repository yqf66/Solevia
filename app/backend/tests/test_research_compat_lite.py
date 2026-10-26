import json

import pytest
from pydantic import SecretStr

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.models import Conversation, Goal, Message, ResearchRun
from planner_api.research.intent import ModelResearchIntentAnalyzer, request_signals_search
from planner_api.research.orchestrator import ResearchOrchestrator, _deterministic_fallback_plan
from planner_api.research.persistence import ResearchRunService
from planner_api.research.providers import ResearchProviderError
from planner_api.research.schemas import (
    PageDocument,
    ResearchPacket,
    ResearchSubquery,
    ResearchDecision,
    SearchHit,
)


@pytest.mark.parametrize(
    "provider,model",
    [("deepseek", "other-responses-model"), ("openai_compatible", "other-chat-model")],
)
def test_schema_contract_and_single_repair_are_shared_across_models(provider, model):
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        if provider == "openai_compatible":
            assert '"engine_group":{"items":{"type":"string"}' in payload["messages"][0]["content"]
        else:
            assert (
                payload["text"]["format"]["schema"]["properties"]["engine_group"]["type"] == "array"
            )
        content = {"should_search": True, "engine_group": "bing" if len(calls) == 1 else ["bing"]}
        return {"output_text": json.dumps(content)}

    settings = Settings(
        _env_file=None, llm_provider=provider, llm_model=model, llm_api_key=SecretStr("test")
    )
    result = ModelResearchIntentAnalyzer(settings, transport=transport).analyze(
        "查找社区对同步的讨论"
    )
    assert result.engine_group == ["bing"] and len(calls) == 2


@pytest.mark.parametrize("failure", ["schema", "transport"])
def test_repair_limit_does_not_turn_into_unbounded_or_transport_retries(failure):
    calls = []

    def transport(*args):
        calls.append(args)
        if failure == "transport":
            raise TimeoutError()
        return {"output_text": '{"should_search":true,"engine_group":"bing"}'}

    settings = Settings(
        _env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test")
    )
    with pytest.raises(ProviderUnavailableError):
        ModelResearchIntentAnalyzer(settings, transport=transport).analyze("搜索资料")
    assert len(calls) == (2 if failure == "schema" else 1)


def test_fallback_keeps_user_subject_without_inventing_facets():
    question = "查找空气炸锅清洁体验，区分机型和个人体验，不要把单篇测评当普遍结论"
    brief, queries = _deterministic_fallback_plan(question, max_questions=12)
    assert queries == [question]
    assert "权威资料与事实基线" not in brief.required_aspects
    assert request_signals_search(question)
    assert not request_signals_search("不要联网搜索，只查找我给的资料")


def test_sync_and_queued_runs_receive_conversation_subject_and_scope(session_factory):
    captured = []

    class Orchestrator:
        limits = ResearchOrchestrator().limits

        def research(self, question, **kwargs):
            captured.append(kwargs["decision_context"])
            return ResearchPacket(question=question, status="PARTIAL")

    with session_factory() as session:
        goal = Goal(title="参观", raw_input="查苏州博物馆本馆预约，只看官方来源")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        session.add(
            Message(
                conversation_id=conversation.id, role="USER", content=goal.raw_input, sequence=1
            )
        )
        previous = ResearchRun(
            goal_id=goal.id,
            conversation_id=conversation.id,
            question=goal.raw_input,
            status="PARTIAL",
            result_json={"brief": {"constraints": ["只看官方来源"]}},
        )
        session.add(previous)
        session.commit()
        service = ResearchRunService(session, orchestrator=Orchestrator())
        service.execute(goal.id, "再核对周末公告", conversation_id=conversation.id)
        queued = service.enqueue(goal.id, "再找预约规则", conversation_id=conversation.id)
        service.process_queued(goal.id, queued.id)
    for context in captured:
        assert "苏州博物馆" in context["original_input"]
        assert context["recent_messages"][0]["role"] == "USER"
        assert context["previous_research"]


@pytest.mark.parametrize("permanent", [False, True])
def test_busy_reader_retries_once_without_browser_retry_loop(permanent, monkeypatch):
    calls = []

    class Reader:
        def read(self, request):
            calls.append(request)
            if len(calls) == 1 or permanent:
                raise ResearchProviderError("busy", code="CRAWL4AI_BUSY")
            return PageDocument(
                url="https://example.com/a",
                finalUrl="https://example.com/a",
                title="Airfryer cleaning",
                content="Cleaning instructions and conditions. " * 80,
            )

    monkeypatch.setattr("planner_api.research.orchestrator.sleep", lambda _: None)
    runner = ResearchOrchestrator(Reader())
    if permanent:
        with pytest.raises(ResearchProviderError, match="busy"):
            runner._read_page("https://example.com/a", max_chars=6000)
    else:
        assert runner._read_page("https://example.com/a", max_chars=6000)[0].content
    assert len(calls) == 2


def test_hard_official_scope_keeps_the_publishers_site_and_excludes_community():
    from test_research_orchestrator import FakeResearchService

    service = FakeResearchService(
        {
            "*": [
                SearchHit(
                    title="苏州博物馆预约须知",
                    url="https://szmuseum.com/Other/ReservationTip",
                    description="苏州博物馆参观预约须知",
                    engine="bing",
                ),
                SearchHit(
                    title="苏州博物馆预约须知",
                    url="https://blog.csdn.net/a",
                    description="苏州博物馆参观预约须知",
                    engine="bing",
                ),
            ]
        }
    )
    runner = ResearchOrchestrator(service)
    query = ResearchSubquery(
        query="苏州博物馆预约",
        source_profiles=["official_docs", "government"],
        source_scope_mode="HARD",
    )
    packet = ResearchPacket(question=query.query, subqueries=[query])
    hits = runner._search_question(packet, query, query.query)
    assert any("szmuseum.com" in str(h.hit.url) for h in hits)
    assert not any("csdn.net" in str(h.hit.url) for h in hits)


def test_primary_planner_soft_preference_ranks_but_does_not_restrict_sites():
    from test_research_orchestrator import FakeResearchService, AlwaysSearchAnalyzer
    from planner_api.research.orchestrator import ResearchLimits

    class Analyzer(AlwaysSearchAnalyzer):
        def analyze(self, question, **kwargs):
            return ResearchDecision(
                should_search=True,
                planned_questions=[
                    ResearchSubquery(
                        query="Obsidian Sync 使用体验",
                        source_profiles=["community", "zhihu"],
                        source_scope_mode="SOFT",
                    )
                ],
            )

    packet = ResearchOrchestrator(
        FakeResearchService({"*": []}),
        intent_analyzer=Analyzer(),
        limits=ResearchLimits(max_queries=1, max_rounds=1),
    ).research("查找社区使用体验", enabled=True)
    query = packet.subqueries[0]
    assert query.source_profiles == [] and query.source_strategy_hint == "community"


@pytest.mark.parametrize("scope", ["HARD", "SOFT"])
def test_adaptive_source_switch_preserves_hard_scope_only(scope):
    from planner_api.research.schemas import ResearchAction, ResearchBrief
    from planner_api.research.strategy import prepare_strategy_followups

    packet = ResearchPacket(
        question="查预约公告",
        brief=ResearchBrief(required_aspects=["预约公告"]),
        subqueries=[
            ResearchSubquery(
                query="本馆预约规则",
                covers_aspects=["预约公告"],
                source_scope_mode=scope,
                source_profiles=["official_docs", "government"],
            )
        ],
    )
    followups, _ = prepare_strategy_followups(
        packet,
        [
            ResearchAction(
                kind="SWITCH_SOURCE",
                aspect="预约公告",
                query_intent="周末临时开放通知",
                source_profiles=["community"],
                reason="换来源补查",
            )
        ],
        max_new=1,
    )
    assert len(followups) == 1
    if scope == "HARD":
        assert followups[0].source_scope_mode == "HARD"
        assert followups[0].source_profiles == ["official_docs", "government"]
    else:
        assert followups[0].source_profiles == ["community"]


@pytest.mark.parametrize(
    "relation,inherit", [("FOLLOW_UP", True), ("NEW_TOPIC", False), ("FOLLOW_UP", False)]
)
def test_followup_inherits_subject_but_topic_switch_or_release_does_not_lock_scope(
    relation, inherit
):
    from test_research_orchestrator import FakeResearchService, AlwaysSearchAnalyzer
    from planner_api.research.orchestrator import ResearchLimits

    class Analyzer(AlwaysSearchAnalyzer):
        def analyze(self, question, **kwargs):
            return ResearchDecision(
                should_search=True,
                topic_relation=relation,
                inherit_source_restrictions=inherit,
                planned_questions=[ResearchSubquery(query="预约临时公告")],
            )

    context = {
        "previous_research": {
            "brief": {"constraints": ["只看官方来源"]},
            "queries": [{"anchor_entities": ["苏州博物馆"]}],
        }
    }
    packet = ResearchOrchestrator(
        FakeResearchService({"*": []}),
        intent_analyzer=Analyzer(),
        limits=ResearchLimits(max_queries=1, max_rounds=1),
    ).research("再查临时公告", enabled=True, decision_context=context)
    query = packet.subqueries[0]
    assert ("苏州博物馆" in query.query) == (relation == "FOLLOW_UP")
    assert (query.source_scope_mode == "HARD") == (relation == "FOLLOW_UP" and inherit)
