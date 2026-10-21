import json
from threading import Event
from time import monotonic, sleep

import pytest
from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.research.intent import ModelResearchIntentAnalyzer
from planner_api.research.model_contract import (
    compact_model_schema, model_deadline, remaining_model_timeout,
)
from planner_api.research.orchestrator import ResearchOrchestrator, _bounded_call
from planner_api.research.schemas import (
    EvidenceSpanChoices, ResearchDecision, ResearchPacket, ResearchReflection,
)


def settings(**values):
    return Settings(_env_file=None, llm_provider="openai_compatible",
                    llm_api_key=SecretStr("test"), **values)


@pytest.mark.parametrize("model", [ResearchDecision, ResearchReflection])
def test_wire_schema_keeps_all_contract_fields_and_validation_limits(model):
    original = model.model_json_schema()
    compact = compact_model_schema(original)

    def compare(before, after):
        if isinstance(before, dict):
            assert set(after) == set(before) - {"title", "default", "examples"}
            for key in after:
                if key in {"properties", "$defs", "definitions"}:
                    assert set(before[key]) == set(after[key])
                    for name in before[key]:
                        compare(before[key][name], after[key][name])
                else:
                    compare(before[key], after[key])
        elif isinstance(before, list):
            assert len(before) == len(after)
            for left, right in zip(before, after, strict=True):
                compare(left, right)
        else:
            assert before == after

    compare(original, compact)
    assert len(json.dumps(compact)) < len(json.dumps(original)) * 0.85


def test_schema_fields_named_like_annotations_are_preserved():
    schema = {"title": "Model", "properties": {
        "title": {"type": "string", "maxLength": 50},
        "default": {"type": "integer", "minimum": 1},
    }}
    assert set(compact_model_schema(schema)["properties"]) == {"title", "default"}


@pytest.mark.parametrize("provider", ["deepseek", "openai_compatible"])
def test_output_override_is_provider_independent_and_planning_only(provider):
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        return {"output_text": '{"should_search":false}'}

    configured = settings(research_model_max_output_tokens=5000).model_copy(
        update={"llm_provider": provider})
    analyzer = ModelResearchIntentAnalyzer(configured, transport=transport)
    assert not analyzer.analyze("根据以上计算即可").should_search
    assert calls[0].get("max_tokens", calls[0].get("max_output_tokens")) == 5000
    assert configured.llm_max_output_tokens == 3000


def test_reflection_keeps_original_conditions_constraints_and_query_tail():
    from test_research_span_selection import choose, packet
    from planner_api.research.schemas import ResearchSubquery

    original = "## Changed in 3.14\n\nExcept KeyboardInterrupt, ordinary exceptions are grouped."
    p = choose(packet(original))
    p.brief.constraints = ["只看 Python 3.14 官方原文", "不得混入旧版本"]
    for i in range(2, 13):
        p.subqueries.append(ResearchSubquery(key=f"Q{i}", query=f"TaskGroup {i}",
            source_scope_mode="HARD", source_profiles=["official_docs"],
            constraints={"entities": ["TaskGroup"], "strict_terms": ["Python 3.14"]}))
    before = p.model_dump_json()
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(json.loads(payload["messages"][1]["content"]))
        return {"output_text": '{"complete":false}'}

    ModelResearchIntentAnalyzer(settings(), transport=transport).reflect(p)
    assert captured["research_brief"]["constraints"] == p.brief.constraints
    assert len(captured["planned_questions"]) == 12
    tail = captured["planned_questions"][-1]
    assert tail["source_scope_mode"] == "HARD"
    assert tail["constraints"]["strict_terms"] == ["Python 3.14"]
    delivered = captured["compiled_research_context"]["selected_evidence"]
    assert delivered[0]["quote"] == original
    assert "candidate" == delivered[0]["coverage"]
    assert "research_brief" not in captured["compiled_research_context"]
    assert before == p.model_dump_json()


def test_large_answer_budget_does_not_silently_expand_research_output():
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(payload)
        return {"output_text": '{"should_search":false}'}

    analyzer = ModelResearchIntentAnalyzer(settings(llm_max_output_tokens=12000), transport=transport)
    analyzer.analyze("仅根据已给数据计算")
    assert captured["max_tokens"] == 3000


@pytest.mark.parametrize("provider", ["deepseek", "openai_compatible"])
def test_evidence_selector_wire_contract_and_budget_are_unchanged(provider):
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append((payload, timeout))
        return {"output_text": '{"selections":[]}'}

    configured = settings(research_model_max_output_tokens=6000).model_copy(
        update={"llm_provider": provider})
    analyzer = ModelResearchIntentAnalyzer(configured, transport=transport)
    analyzer._request({"spans": []}, EvidenceSpanChoices, "Choose original spans")
    payload, timeout = calls[0]
    original = EvidenceSpanChoices.model_json_schema()
    if provider == "deepseek":
        assert payload["text"]["format"]["schema"] == original
        assert payload["max_output_tokens"] == 3000
    else:
        assert json.dumps(original, ensure_ascii=False, separators=(",", ":")) in payload["messages"][0]["content"]
        assert payload["max_tokens"] == 2500
    assert timeout == configured.llm_timeout_seconds


def test_schema_repair_shares_the_remaining_outer_deadline():
    timeouts = []

    def transport(url, headers, payload, timeout):
        timeouts.append(timeout)
        sleep(0.02)
        return {"output_text": "{}" if len(timeouts) == 1 else '{"complete":false}'}

    with model_deadline(monotonic() + 0.7):
        result = ModelResearchIntentAnalyzer(settings(), transport=transport).reflect(
            ResearchPacket(question="check"))
    assert not result.complete
    assert len(timeouts) == 2 and 0 < timeouts[1] < timeouts[0] < 0.7


def test_exhausted_deadline_does_not_start_another_http_request():
    with model_deadline(monotonic() - 1), pytest.raises(TimeoutError):
        remaining_model_timeout(30, deadline=monotonic() + 30)


def test_bounded_call_discards_late_result_and_recovers_for_next_call(caplog):
    released, completed = Event(), Event()

    def late():
        released.wait(2)
        completed.set()
        return "late"

    with caplog.at_level("INFO"), pytest.raises(TimeoutError):
        _bounded_call(late, 0.1)
    released.set()
    assert completed.wait(1)
    assert _bounded_call(lambda: "next", 1) == "next"


def test_reflection_provider_gets_an_isolated_snapshot():
    class Provider:
        def reflect(self, snapshot):
            snapshot.question = "changed by provider"
            snapshot.uncertain_points.append("provider mutation")
            return ResearchReflection(complete=False)

    runner = ResearchOrchestrator(settings=settings())
    runner.decision_provider = Provider()
    p = ResearchPacket(question="original")
    runner._reflect(p, None)
    assert p.question == "original"
    assert "provider mutation" not in p.uncertain_points
