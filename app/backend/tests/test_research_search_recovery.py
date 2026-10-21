from uuid import uuid4

import pytest

from planner_api.config import Settings
from planner_api.models import ResearchResponseRequest, ResearchRun
from planner_api.research.orchestrator import ResearchLimits, ResearchOrchestrator
from planner_api.research.response_schemas import ResearchResponseCreate
from planner_api.research.schemas import ResearchBrief, ResearchDecision, ResearchPacket, ResearchSubquery, SearchHit
from test_research_orchestrator import AlwaysSearchAnalyzer, FakeResearchService
from test_research_responses import create


@pytest.mark.parametrize("mode", ["OFF", "BGE"])
def test_previous_source_is_reread_without_reusing_previous_facts(mode):
    query = ResearchSubquery(query="博物馆预约", query_role="AUTHORITATIVE", source_profiles=["official_docs"])
    packet = ResearchPacket(question="博物馆预约", subqueries=[query])
    service = FakeResearchService({})
    runner = ResearchOrchestrator(service, settings=Settings(_env_file=None, research_rerank_mode=mode),
                                  limits=ResearchLimits(max_read_pages=1, max_pages_per_round=1))
    runner._seed_previous_read_sources(packet, {"previous_read_urls": ["https://www.museum.example.org/reservation"]})
    assert packet.sources[0].evidence_level == "UNVERIFIED"
    assert not packet.sources[0].content_excerpt and not packet.claims
    assert runner._read_selected(packet) == 1
    assert str(service.read_calls[0].url) == "https://www.museum.example.org/reservation"
    assert packet.sources[0].source_category == "OTHER"
    assert not packet.sources[0].source_profiles


def test_previous_source_cannot_bypass_official_scope():
    packet = ResearchPacket(question="官方预约", subqueries=[ResearchSubquery(
        query="官方预约", query_role="AUTHORITATIVE", source_profiles=["official_docs"])])
    ResearchOrchestrator._seed_previous_read_sources(packet, {"previous_read_urls": [
        "https://suzhou.bendibao.com/tour/1", "https://wenku.baidu.com/view/1"]})
    assert not packet.sources


def test_official_life_query_does_not_use_technical_community_engine():
    query = ResearchSubquery(query="博物馆预约规则", query_role="AUTHORITATIVE", source_profiles=["official_docs"])
    service = FakeResearchService({})
    runner = ResearchOrchestrator(service, settings=Settings(_env_file=None, research_engine_pool="bing,duckduckgo,csdn"))
    runner._search_question(ResearchPacket(question=query.query, subqueries=[query]), query, query.query)
    assert service.search_calls[0].engines == ["bing", "duckduckgo"]


def test_reference_domain_is_only_a_repair_search_hint():
    query = ResearchSubquery(query="苏州博物馆预约", query_role="AUTHORITATIVE", source_profiles=["official_docs"],
                             reference_urls=["https://www.szmuseum.com/"], covers_aspects=["预约规则"])
    packet = ResearchPacket(question=query.query, subqueries=[query], brief=ResearchBrief(required_aspects=["预约规则"]))
    runner = ResearchOrchestrator(FakeResearchService({}))
    repair = runner._fallback_reflection(packet, RuntimeError("no evidence"))
    assert repair.strategy_actions[0].query_intent.startswith("site:www.szmuseum.com ")
    assert not packet.sources and not packet.claims


def test_reference_domain_narrows_search_without_direct_read_or_authority_label():
    query = ResearchSubquery(query="博物馆预约", query_role="AUTHORITATIVE", source_profiles=["official_docs"],
                             reference_urls=["https://museum.example.org/"])
    packet = ResearchPacket(question=query.query, subqueries=[query])
    service = FakeResearchService({})
    runner = ResearchOrchestrator(service, settings=Settings(_env_file=None))
    runner._search_question(packet, query, query.query)
    assert service.search_calls[0].query == "site:museum.example.org 博物馆预约"
    assert not service.read_calls and not packet.sources


def test_empty_round_gets_one_bounded_repair_instead_of_immediate_stop():
    query = ResearchSubquery(query="预约规则", query_role="AUTHORITATIVE", source_profiles=["official_docs"],
                             covers_aspects=["reservation"], query_intent="预约规则")

    class Analyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(should_search=True, planned_questions=[query],
                                    brief=ResearchBrief(required_aspects=["reservation"]))

    service = FakeResearchService({"*": [], "预约规则 具体数据 原始出处": [SearchHit(
        title="museum reservation", url="https://museum.example.org/rules", snippet="museum reservation", engine="bing")]})
    runner = ResearchOrchestrator(service, intent_analyzer=Analyzer(), settings=Settings(_env_file=None),
                                  limits=ResearchLimits(max_rounds=2, max_queries=2))
    packet = runner.research("museum reservation", enabled=True)
    assert 1 < len(service.search_calls) <= 2
    assert packet.coverage.rounds <= 2


def test_prior_urls_are_scoped_to_same_question_and_conversation(session_factory):
    with session_factory() as session:
        service, goal, payload, response = create(session, question="官方预约")
        run = session.get(ResearchRun, response.run.id)
        run.status = "PARTIAL"
        run.result_json = {"sources": [{"url": "https://museum.example.org/reservation", "read_status": "SUCCEEDED"}]}
        row = session.get(ResearchResponseRequest, response.id)
        row.status = "COMPLETED"
        session.commit()
        again = service.create(goal.id, payload.model_copy(update={"idempotency_key": str(uuid4())}))
        context = session.get(ResearchResponseRequest, again.id).snapshot_json["decision_context"]
        assert context["previous_read_urls"] == ["https://museum.example.org/reservation"]
        session.get(ResearchResponseRequest, again.id).status = "COMPLETED"
        session.commit()
        different = service.create(goal.id, ResearchResponseCreate(question="另一个问题", phase=payload.phase,
                                    intent=payload.intent, idempotency_key=str(uuid4())))
        assert "previous_read_urls" not in session.get(ResearchResponseRequest, different.id).snapshot_json["decision_context"]
