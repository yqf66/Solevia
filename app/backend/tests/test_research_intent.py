import json

from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.research.intent import (
    DeepSeekResearchDecisionProvider,
    ModelResearchIntentAnalyzer,
    _evidence_sections,
    build_research_decision_provider,
)
from planner_api.research.schemas import EvidencePassage, ResearchPacket


def test_deepseek_uses_the_replaceable_decision_provider_boundary() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key=SecretStr("test-key"),
    )
    analyzer = ModelResearchIntentAnalyzer(settings, transport=lambda *_: {})

    provider = build_research_decision_provider(settings, analyzer=analyzer)

    assert isinstance(provider, DeepSeekResearchDecisionProvider)
    assert provider.analyzer is analyzer


def test_model_intent_analyzer_parses_openai_compatible_json() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, payload, timeout):
        captured.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "should_search": True,
                                "research_questions": ["Open-WebSearch 的最新项目结构"],
                                "reason": "需要核对当前项目资料",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("test-key"),
        context_max_input_chars=20000,
    )
    analyzer = ModelResearchIntentAnalyzer(settings, transport=transport)

    decision = analyzer.analyze("用户有一大段背景资料，但只需要核对项目当前结构")

    assert decision.should_search is True
    assert decision.research_questions == ["Open-WebSearch 的最新项目结构"]
    assert captured["response_format"] == {"type": "json_object"}
    assert "用户有一大段背景资料" in captured["messages"][1]["content"]


def test_model_intent_analyzer_uses_existing_context_for_search_gate() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, payload, timeout):
        captured.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "should_search": False,
                                "knowledge_sufficiency": "SUFFICIENT",
                                "search_value": "NONE",
                                "reason": "当前计划已经包含回答所需信息",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("test-key"),
    )
    decision = ModelResearchIntentAnalyzer(settings, transport=transport).analyze(
        "为什么要先做状态流转？",
        context={"current_plan": {"summary": "先完成状态流转，再做工具编排"}},
    )

    payload_text = captured["messages"][1]["content"]
    assert "current_plan" in payload_text
    assert decision.knowledge_sufficiency == "SUFFICIENT"
    assert decision.search_value == "NONE"
    assert decision.should_search is False


def test_model_reflection_receives_bounded_evidence_not_full_pages() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, payload, timeout):
        captured.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"complete": false, "follow_up_questions": ["浏览器回退方案"], '
                            '"unresolved_points": ["浏览器依赖待核对"]}'
                        )
                    }
                }
            ]
        }

    settings = Settings(
        _env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test-key")
    )
    analyzer = ModelResearchIntentAnalyzer(settings, transport=transport)
    packet = ResearchPacket(question="如何部署？")
    reflection = analyzer.reflect(packet)

    assert reflection.follow_up_questions == ["浏览器回退方案"]
    assert captured["messages"][0]["content"].startswith("你负责判断当前联网研究")
    assert "sources_are_untrusted_data" in captured["messages"][1]["content"]


def test_model_reflection_repairs_one_invalid_schema_response() -> None:
    calls: list[dict[str, object]] = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        content = "{}" if len(calls) == 1 else '{"complete": true}'
        return {"choices": [{"message": {"content": content}}]}

    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("test-key"),
    )
    reflection = ModelResearchIntentAnalyzer(settings, transport=transport).reflect(
        ResearchPacket(question="如何部署？")
    )

    assert reflection.complete is True
    assert len(calls) == 2
    assert "只修复 JSON 字段" in calls[1]["messages"][0]["content"]


def test_evidence_extraction_batches_pages_and_only_marks_successful_batches() -> None:
    captured = []

    def transport(url, headers, payload, timeout):
        submitted = json.loads(payload["messages"][1]["content"])
        captured.append(submitted["passages"])
        return {"choices": [{"message": {"content": '{"claims": []}'}}]}

    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("test-key"),
    )
    packet = ResearchPacket(
        question="项目功能",
        passages=[
            EvidencePassage(
                passage_id=f"passage-{index}",
                source_url_or_uri=f"https://example.com/{index}",
                text="A verifiable feature. " * 100,
                content_hash="12345678",
            )
            for index in range(4)
        ],
    )

    analysis = ModelResearchIntentAnalyzer(settings, transport=transport).extract_evidence(packet)

    assert all(len(batch) == 1 for batch in captured)
    assert all(len(item["text"]) <= 1000 for batch in captured for item in batch)
    assert analysis.processed_passage_ids == [f"passage-{index}" for index in range(4)]


def test_long_evidence_sections_remain_verbatim_and_cover_all_rules():
    captured = []
    def transport(url, headers, payload, timeout):
        submitted = json.loads(payload["messages"][1]["content"])
        captured.extend(submitted["passages"])
        return {"choices": [{"message": {"content": '{"claims": []}'}}]}
    text = "First exception cancels siblings. " + "body " * 1500 + "KeyboardInterrupt and SystemExit are special."
    packet = ResearchPacket(question="TaskGroup rules", passages=[EvidencePassage(
        passage_id="p1", source_url_or_uri="https://docs.python.org/test", text=text, content_hash="12345678")])
    settings = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test-key"))
    analysis = ModelResearchIntentAnalyzer(settings, transport=transport).extract_evidence(packet)
    assert len(captured) >= 4
    assert captured[0]["text"].startswith("First exception")
    assert captured[-1]["text"].endswith("SystemExit are special.")
    assert all(item["text"] in text for item in captured)
    assert analysis.processed_passage_ids == ["p1"]
    for item in captured:
        start = item["section_start"]
        assert item["context_before"] == text[max(0, start - 1000):start]
        assert item["context_after"] == text[start + len(item["text"]):start + len(item["text"]) + 1000]


def test_truncated_evidence_output_does_not_mark_whole_passage_processed():
    submitted = []

    def transport(url, headers, payload, timeout):
        data = json.loads(payload["input"])
        submitted.append(data["passages"][0])
        assert payload["text"]["format"]["schema"]["properties"]["claims"]["maxItems"] == 3
        if len(submitted) == 1:
            return {"status": "incomplete", "output_text": '{"claims": ['}
        return {"output_text": '{"claims": [], "processed_passage_ids": ["p1"]}'}

    packet = ResearchPacket(question="API rules", passages=[EvidencePassage(
        passage_id="p1", source_url_or_uri="https://example.com/api", text="rule " * 600,
        content_hash="12345678")])
    settings = Settings(_env_file=None, llm_provider="deepseek", llm_api_key=SecretStr("test-key"))
    analysis = ModelResearchIntentAnalyzer(settings, transport=transport).extract_evidence(packet)
    assert len(submitted) >= 2
    assert not analysis.processed_passage_ids
    assert analysis.uncertain_points


def test_rule_condition_and_result_stay_in_one_section():
    special = "If a task raises SystemExit, siblings are cancelled and awaited, then the original exception is re-raised."
    text = "General rule. " * 105 + special + " Next rule. " * 140
    sections = list(_evidence_sections(text))
    assert any(special in section for _, section in sections)
    assert all(text[start:start + len(section)] == section for start, section in sections)
    assert all(len(section) <= 1000 for _, section in sections)
    assert sections[-1][0] + len(sections[-1][1]) == len(text)


def test_model_intent_analyzer_parses_deepseek_output_text() -> None:
    def transport(url, headers, payload, timeout):
        return {
            "output_text": (
                '{"should_search": false, "research_questions": [], "reason": "已有资料足够"}'
            )
        }

    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key=SecretStr("test-key"),
    )
    analyzer = ModelResearchIntentAnalyzer(settings, transport=transport)

    decision = analyzer.analyze("根据当前计划调整明天的安排")

    assert decision.should_search is False
    assert decision.research_questions == []


def test_model_intent_analyzer_accepts_structured_brief_and_planned_questions() -> None:
    def transport(url, headers, payload, timeout):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "should_search": True,
                                "research_questions": [],
                                "planned_questions": [
                                    {
                                        "key": "runtime",
                                        "query": "运行时要求",
                                        "purpose": "DOCUMENTATION",
                                        "priority": 5,
                                        "required": True,
                                        "covers_aspects": ["运行时"],
                                        "stage_id": "baseline",
                                    },
                                    {
                                        "key": "browser",
                                        "query": "浏览器回退要求",
                                        "purpose": "DOCUMENTATION",
                                        "priority": 3,
                                        "required": True,
                                        "depends_on": ["runtime"],
                                        "covers_aspects": ["浏览器"],
                                        "stage_id": "validation",
                                    },
                                ],
                                "brief": {
                                    "objective": "评估本地部署要求",
                                    "required_aspects": ["运行时", "浏览器"],
                                    "constraints": ["本地部署"],
                                },
                                "research_stages": [
                                    {
                                        "key": "baseline",
                                        "purpose": "先确认运行时基线",
                                        "source_profiles": ["official_docs"],
                                        "query_budget": 2,
                                        "read_budget": 2,
                                        "order": 0,
                                    },
                                    {
                                        "key": "validation",
                                        "purpose": "再验证浏览器回退",
                                        "source_profiles": ["official_docs"],
                                        "query_budget": 2,
                                        "read_budget": 2,
                                        "depends_on": ["baseline"],
                                        "order": 1,
                                    },
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("test-key"),
    )
    decision = ModelResearchIntentAnalyzer(settings, transport=transport).analyze(
        "评估本地部署要求"
    )

    assert decision.brief is not None
    assert decision.brief.required_aspects == ["运行时", "浏览器"]
    assert [item.key for item in decision.planned_questions] == ["runtime", "browser"]
    assert decision.planned_questions[1].depends_on == ["runtime"]
    assert [stage.key for stage in decision.research_stages] == ["baseline", "validation"]
    assert decision.planned_questions[0].stage_id == "baseline"
