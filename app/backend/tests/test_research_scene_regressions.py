"""Failures observed in real community/life conversations, plus boundaries."""
import json

import pytest
from pydantic import SecretStr

from planner_api.ai import ClarificationContext, DeepSeekClarificationProvider, ProviderUnavailableError
from planner_api.config import Settings
from planner_api.research.intent import ModelResearchIntentAnalyzer, request_signals_search
from planner_api.research.orchestrator import ResearchOrchestrator
from planner_api.research.span_context import missing_original_answer
from test_research_orchestrator import AlwaysSearchAnalyzer, FakeResearchService


@pytest.mark.parametrize("question", [
    "帮我找开发者社区有具体环境和处理办法的讨论",
    "请查现在中国铁路对充电宝的官方规则，再找旅客社区经验",
    "除湿机和空调哪种更适合晾衣？别人实际都是怎么处理的？",
])
def test_natural_source_requests_keep_browsing_intent_when_gate_fails(question):
    class BrokenGate(AlwaysSearchAnalyzer):
        def analyze(self, *_args, **_kwargs):
            raise ProviderUnavailableError("truncated", code="RESEARCH_DECISION_FAILED")

    service = FakeResearchService({"*": []})
    packet = ResearchOrchestrator(service, intent_analyzer=BrokenGate()).research(question, enabled=True)
    assert packet.status != "SKIPPED"
    assert service.search_calls
    assert packet.decision.should_search


def test_ambiguous_failed_gate_is_failure_not_negative_search_decision():
    class BrokenGate(AlwaysSearchAnalyzer):
        def analyze(self, *_args, **_kwargs):
            raise ProviderUnavailableError("truncated")

    service = FakeResearchService({"*": []})
    packet = ResearchOrchestrator(service, intent_analyzer=BrokenGate()).research("这个怎么办？", enabled=True)
    assert packet.status == "FAILED" and packet.stop_reason == "DECISION_FAILED"
    assert not service.search_calls


@pytest.mark.parametrize("question", [
    "不用查外部资料，根据以上安排晾衣",
    "不要联网搜索，请根据我提供的时间确认计划",
    "只根据上述资料确认结论",
])
def test_permission_to_search_does_not_override_explicit_prohibition(question):
    assert not request_signals_search(question)


def test_truncated_decision_is_not_reported_as_no_search():
    settings = Settings(_env_file=None, llm_provider="deepseek", llm_api_key=SecretStr("test"))

    def transport(*_args):
        return {"output_text": '{"should_search":true,"planned_questions":['}

    with pytest.raises(ProviderUnavailableError) as error:
        ModelResearchIntentAnalyzer(settings, transport=transport).analyze("帮我找社区经验")
    assert error.value.code == "RESEARCH_DECISION_FAILED"


def _weak_pack():
    return {"research_question": "本馆是否需要预约？", "research_status": "PARTIAL",
            "evidence_mode": "SPANS", "research_brief": {"required_aspects": ["预约要求"]},
            "selected_evidence": [{"evidence_id": "s1", "evidence_level": "SEARCH_SNIPPET",
                                   "quote": "分时全预约", "requested_version_matches": True}]}


def test_no_original_pack_still_generates_useful_bounded_answer_with_checker_off():
    calls = []

    def transport(*args):
        calls.append(args)
        return {"status": "completed", "output_text": "预约规则尚未核实。如果你准备出行，可以先安排一个备选行程，避免扑空。"}

    settings = Settings(_env_file=None, llm_provider="deepseek", llm_api_key=SecretStr("test"),
                        research_answer_verification_mode="OFF")
    context = ClarificationContext(raw_input="本馆是否需要预约？", active_facts={},
                                   latest_user_message="本馆是否需要预约？", answer_for_keys=[],
                                   research_context=_weak_pack())
    reply = DeepSeekClarificationProvider(settings, transport=transport).respond(context)
    assert "尚未核实" in reply.assistant_message
    assert "无需预约" not in reply.assistant_message
    assert len(calls) == 1
    assert "不停止整个回答" in calls[0][2].get("instructions", "") or "不要只回" in calls[0][2].get("instructions", "")


def test_valid_original_and_followup_still_reach_normal_answer_path():
    pack = _weak_pack()
    pack["selected_evidence"][0]["evidence_level"] = "FULL_TEXT"
    assert missing_original_answer(pack, "本馆是否需要预约？") is None
    pack["selected_evidence"][0]["requested_version_matches"] = False
    assert missing_original_answer(pack, "本馆是否需要预约？")
    assert missing_original_answer(pack, "把刚才的话改写成一句") is None


def test_missing_original_guard_matches_the_orchestrators_whitespace_normalization():
    pack = _weak_pack()
    pack["research_question"] = "本馆 是否需要预约？"
    assert missing_original_answer(pack, "本馆\n  是否需要预约？")


@pytest.mark.parametrize("mode", ["OFF", "BGE"])
def test_paraphrased_official_title_is_recalled_without_removing_semantic_constraints(mode):
    from planner_api.research.reranking import recall_constraints
    from planner_api.research.ranking import RankingPolicy, rank_candidates
    from planner_api.research.schemas import ResearchQueryConstraints, SearchHit

    constraints = ResearchQueryConstraints(entities=["苏州博物馆"], strict_terms=["提前预约要求"])
    recall = recall_constraints(constraints, "查苏州博物馆是否需要提前预约")
    outcome = rank_candidates("苏州博物馆 预约", [SearchHit(
        title="苏州博物馆预约须知", url="https://wglj.suzhou.gov.cn/museum/reservation",
        description="实行实名制分时全预约参观机制。", engine="bing")], max_results=3,
        purpose="DOCUMENTATION", query_role="AUTHORITATIVE", constraints=recall,
        policy=RankingPolicy(min_relevance_score=0, enforce_exact_terms=mode == "BGE"))
    assert outcome.accepted
    assert constraints.strict_terms == ["提前预约要求"]
    exact = ResearchQueryConstraints(literal_terms=["Path.exists"], strict_terms=["Python 3.14"])
    recalled = recall_constraints(exact, '只看 Python 3.14，原文必须包含 "Path.exists"')
    assert recalled.literal_terms == ["Path.exists"] and recalled.strict_terms == ["Python 3.14"]


def test_compact_decision_retains_context_and_does_not_increase_output_budget():
    captured = {}

    def transport(_url, _headers, payload, _timeout):
        captured.update(payload)
        return {"output_text": json.dumps({"should_search": False, "knowledge_sufficiency": "SUFFICIENT"})}

    settings = Settings(_env_file=None, llm_provider="deepseek", llm_api_key=SecretStr("test"),
                        llm_max_output_tokens=3000)
    decision = ModelResearchIntentAnalyzer(settings, transport=transport).analyze(
        "根据我给的资料计算", context={"provided_data": "每次40分钟，每周两次"})
    assert not decision.should_search
    assert "provided_data" in captured["input"]
    assert captured["max_output_tokens"] == 3000


@pytest.mark.parametrize("url", [
    "https://learn.microsoft.com/en-us/windows/wsl/networking",
    "https://www.sqlite.org/wal.html",
])
def test_known_publisher_documentation_is_readable_without_docs_url_pattern(url):
    from planner_api.research.ranking import RankingPolicy, classify_source, rank_candidates
    from planner_api.research.schemas import ResearchQueryConstraints, SearchHit

    assert classify_source(url) == "DOCUMENTATION"
    outcome = rank_candidates("networking WAL", [SearchHit(
        title="networking WAL", url=url, description="Official reference.", engine="bing")],
        max_results=3, purpose="DOCUMENTATION", query_role="AUTHORITATIVE",
        constraints=ResearchQueryConstraints(), policy=RankingPolicy(min_relevance_score=0))
    assert outcome.accepted and not outcome.snippet_fallback
    assert classify_source("https://learn.microsoft.com.example.net/networking") != "DOCUMENTATION"
