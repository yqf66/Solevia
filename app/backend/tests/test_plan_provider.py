import json
from urllib.error import HTTPError, URLError

import pytest
from pydantic import SecretStr

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.plan_ai import (
    DeepSeekPlanProvider,
    FakePlanProvider,
    NaturalPlanDetailAuthor,
    NaturalPlanOutlineAuthor,
    OpenAICompatiblePlanProvider,
    PlanContext,
    SemanticPlanProvider,
    normalize_compiled_outline_capacity,
    normalize_compiled_plan_days,
    normalize_hybrid_contract,
    normalize_hybrid_group_bounds,
    _validate_plan_response,
)


def test_hybrid_missing_binding_keeps_flexibility_and_explicit_fixed_binding():
    from planner_api.schemas import HybridPlanDetailResult

    provider = FakePlanProvider()
    outline = provider.draft_hybrid_outline(context()).model_dump(mode='json')
    raw = provider.draft_hybrid_details(context(), outline, 1, 1).model_dump(mode='json')
    tasks = [task for day in raw['days'] for task in day['tasks']]
    tasks[0].pop('scheduling_policy')
    tasks[0].pop('scheduling_policy_data')
    tasks[-1]['scheduling_policy'] = 'EXACT_DAY'
    parsed = _validate_plan_response(HybridPlanDetailResult, json.dumps(raw))
    result = [task for day in parsed.days for task in day.tasks]
    assert result[0].scheduling_policy == 'FLOATING'
    assert result[-1].scheduling_policy == 'EXACT_DAY'


def settings(**overrides) -> Settings:
    values = {
        "llm_provider": "openai_compatible",
        "llm_api_key": SecretStr("test-key"),
        "llm_base_url": "https://gateway.example/v1",
        "llm_model": "test-model",
        "plan_outline_max_output_tokens": 4000,
        "plan_max_output_tokens": 12000,
        "plan_hybrid_author_max_output_tokens": 6000,
        "plan_hybrid_compiler_max_output_tokens": 24000,
        "plan_max_retries": 1,
    }
    values.update(overrides)
    return Settings(**values)


def context() -> PlanContext:
    return PlanContext(
        requirements={"available_capacity": "每周 2 天，每天 90 分钟"},
        route={
            "stages": [
                {
                    "order": 1,
                    "title": "建立基础",
                    "purpose": "完成基础准备。",
                    "outputs": ["基础清单"],
                }
            ]
        },
        planning_context={
            "precedence_rules": ["指定日期规则覆盖通用节奏"],
            "schedule_context": {
                "timezone": "Asia/Shanghai",
                "fixed_occupancy_rules": [{"title": "健身", "start_local": "14:00"}],
            },
        },
    )


@pytest.mark.parametrize("provider_class", [
    DeepSeekPlanProvider, OpenAICompatiblePlanProvider, NaturalPlanOutlineAuthor,
])
def test_rejected_plan_request_has_non_retryable_code(provider_class) -> None:
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(1)
        raise HTTPError(url, 401, "Unauthorized", {}, None)

    provider = provider_class(settings(), transport=transport, sleeper=lambda _: None)
    with pytest.raises(ProviderUnavailableError) as raised:
        if isinstance(provider, NaturalPlanOutlineAuthor):
            provider.draft_document(context())
        else:
            provider.draft_outline(context())
    assert raised.value.code == "MODEL_REQUEST_REJECTED"
    assert raised.value.diagnostic == "HTTP 401"
    assert len(calls) == 1


def test_outline_and_details_use_separate_output_budgets() -> None:
    captured_tokens = []
    captured_inputs = []
    responses = [
        {
            "assistant_message": "总体分配已完成。",
            "summary": "用一周完成基础准备，并保留机动时间。",
            "total_weeks": 1,
            "weekly_execution_days": 1,
            "weekly_minutes": 90,
            "milestones": [
                {
                    "stage_order": 1,
                    "title": "建立基础",
                    "purpose": "完成后续执行需要的基础准备。",
                    "start_week": 1,
                    "end_week": 1,
                    "outputs": ["基础清单"],
                }
            ],
            "buffer_strategy": ["预留未排满时间处理延期。"],
            "assumptions": [],
            "open_questions": [],
        },
        {
            "days": [
                {
                    "stage_order": 1,
                    "week_index": 1,
                    "day_index": 1,
                    "primary_goal": "整理完成后续执行需要的基础清单。",
                    "suggested_minutes": 90,
                    "tasks": [
                        {
                            "order": 1,
                            "title": "整理基础清单",
                            "description": "列出当前已经具备和仍需补齐的基础。",
                            "estimate_minutes": 60,
                            "priority": 1,
                            "task_kind": "PREPARE",
                            "is_required": True,
                            "evidence": ["基础清单"],
                            "depends_on": [],
                        }
                    ],
                    "evidence": ["基础清单"],
                    "do_not": [],
                }
            ]
        },
    ]

    def transport(_url, _headers, payload, _timeout):
        captured_tokens.append(payload["max_tokens"])
        captured_inputs.append(json.loads(payload["messages"][1]["content"]))
        value = responses.pop(0)
        return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}

    provider = OpenAICompatiblePlanProvider(settings(), transport=transport)
    outline = provider.draft_outline(context())
    provider.draft_details(context(), outline.model_dump(), 1, 1)

    assert captured_tokens == [4000, 12000]
    assert all(
        item["planning_context"]["schedule_context"]["timezone"] == "Asia/Shanghai"
        for item in captured_inputs
    )
    assert captured_inputs[1]["planning_context"]["schedule_context"][
        "fixed_occupancy_rules"
    ][0]["title"] == "健身"


def test_hybrid_outline_uses_work_item_output_budget() -> None:
    author_tokens = []

    def author_transport(_url, _headers, payload, _timeout):
        author_tokens.append(payload["max_tokens"])
        return {"choices": [{"message": {"content": "# 弹性工作项设计"}}]}

    author = NaturalPlanOutlineAuthor(settings(), transport=author_transport)
    assert author.draft_hybrid_document(context()) == "# 弹性工作项设计"
    assert author_tokens == [6000]

    compiler_tokens = []

    def compiler_transport(url, _headers, payload, _timeout):
        compiler_tokens.append(payload["max_tokens"])
        raise HTTPError(url, 401, "Unauthorized", {}, None)

    compiler = OpenAICompatiblePlanProvider(
        settings(), transport=compiler_transport, sleeper=lambda _: None
    )
    with pytest.raises(ProviderUnavailableError):
        compiler.draft_hybrid_outline(context())
    assert compiler_tokens == [24000]


def test_hybrid_group_bound_normalization_repairs_only_selection_arithmetic() -> None:
    raw = {
        "work_item_groups": [
            {
                "group_key": "PLAN.CHOICE",
                "minimum_selected": 0,
                "maximum_selected": 5,
                "candidate_item_keys": ["PLAN.CHOICE.A", "PLAN.CHOICE.B"],
            }
        ]
    }

    normalize_hybrid_group_bounds(raw)

    assert raw["work_item_groups"][0]["minimum_selected"] == 1
    assert raw["work_item_groups"][0]["maximum_selected"] == 2
    assert raw["work_item_groups"][0]["candidate_item_keys"] == [
        "PLAN.CHOICE.A",
        "PLAN.CHOICE.B",
    ]


def test_hybrid_contract_normalizes_only_unambiguous_representation_fields() -> None:
    raw = {
        "total_weeks": 4,
        "milestones": [
            {"stage_order": 3, "start_week": 1, "end_week": 2},
            {"stage_order": 7, "start_week": 3, "end_week": 3},
        ],
        "temporal_segments": [
            {
                "start_week": 1,
                "end_week": 2,
                "suggested_daily_minutes": 1200,
                "exception_daily_minutes": 60,
                "weekly_execution_days": 9,
            },
            {"start_week": 2, "end_week": 3},
        ],
        "work_item_groups": [
            {
                "group_key": "M2.CHOICE",
                "minimum_selected": 1,
                "maximum_selected": 1,
                "candidate_item_keys": ["M2.W04.CHOICE"],
            }
        ],
        "standing_work_items": [
            {
                "item_key": "M2.W04.CHOICE",
                "scope_type": "WEEK",
                "constraint_set": {
                    "fulfillment_mode": "SELECT_K",
                    "temporal": {
                        "start_week": 4,
                        "end_week": 4,
                        "allowed_weekdays": [1, 3, 7],
                    },
                    "selection": {},
                    "completion": {},
                },
            },
            {
                "item_key": "PLAN.PRACTICE",
                "scope_type": "PLAN",
                "constraint_set": {
                    "fulfillment_mode": "DURATION",
                    "completion": {"target_unit": "COUNT"},
                },
            },
        ],
    }

    normalize_hybrid_contract(raw)

    assert raw["milestones"][-1]["end_week"] == 4
    assert [item["stage_order"] for item in raw["milestones"]] == [1, 2]
    assert raw["temporal_segments"] == [
        {
            "start_week": 1,
            "end_week": 2,
            "suggested_daily_minutes": 1200,
            "exception_daily_minutes": 60,
            "weekly_execution_days": 7,
        },
        {"start_week": 3, "end_week": 4},
    ]
    choice = raw["standing_work_items"][0]
    assert choice["stage_order"] == 2
    assert choice["constraint_set"]["temporal"]["allowed_weekdays"] == [0, 2, 6]
    assert choice["constraint_set"]["selection"] == {
        "mode": "SELECT_K",
        "group_key": "M2.CHOICE",
    }
    assert raw["standing_work_items"][1]["constraint_set"]["completion"][
        "target_unit"
    ] == "MINUTES"


def test_hybrid_contract_treats_select_k_without_candidates_as_count() -> None:
    raw = {
        "standing_work_items": [
            {
                "item_key": "PLAN.PRACTICE.SIX",
                "scope_type": "PLAN",
                "constraint_set": {
                    "fulfillment_mode": "SELECT_K",
                    "selection": {"minimum_selected": 6},
                    "completion": {"target_value": 6},
                },
            }
        ]
    }

    normalize_hybrid_contract(raw)

    constraints = raw["standing_work_items"][0]["constraint_set"]
    assert constraints["fulfillment_mode"] == "COUNT"
    assert constraints["completion"] == {"target_value": 6, "target_unit": "COUNT"}
    assert constraints["selection"] == {
        "group_key": None,
        "mode": "ALL",
        "minimum_selected": 1,
        "maximum_selected": None,
    }


def test_hybrid_contract_canonicalizes_keys_and_derived_cadence_fields() -> None:
    raw = {
        "work_items": [
            {
                "item_key": "m1/w1 practice",
                "scope_type": "PLAN",
                "constraint_set": {
                    "binding_mode": "CADENCE",
                    "fulfillment_mode": "COUNT",
                    "recurrence": {"period": "ONCE", "allowed_weekdays": [1, 7]},
                    "selection": {"mode": "SELECT_K", "group_key": "weekly choices"},
                    "completion": {"target_value": 2},
                },
            }
        ],
        "work_item_groups": [
            {
                "group_key": "weekly choices",
                "minimum_selected": 1,
                "maximum_selected": 1,
                "candidate_item_keys": ["m1/w1 practice"],
            }
        ],
        "work_item_dependencies": [
            {
                "predecessor_key": "m1/w1 practice",
                "successor_key": "m1/w1 practice",
            }
        ],
    }

    normalize_hybrid_contract(raw)

    item = raw["work_items"][0]
    assert item["item_key"] == "M1_W1_PRACTICE"
    assert item["constraint_set"]["recurrence"]["period"] == "WEEK"
    assert item["constraint_set"]["temporal"]["allowed_weekdays"] == [0, 6]
    # Selection is independent of COUNT completion: preserve the explicit gate.
    assert item["constraint_set"]["selection"] == {
        "group_key": "WEEKLY_CHOICES",
        "mode": "SELECT_K",
    }
    assert item["constraint_set"]["fulfillment_mode"] == "COUNT"
    assert item["constraint_set"]["completion"]["target_value"] == 2
    assert raw["work_item_groups"][0]["group_key"] == "WEEKLY_CHOICES"
    assert raw["work_item_groups"][0]["candidate_item_keys"] == ["M1_W1_PRACTICE"]
    # A self dependency is a real graph error, not disposable bookkeeping.
    assert raw["work_item_dependencies"] == [{
        "predecessor_key": "M1_W1_PRACTICE", "successor_key": "M1_W1_PRACTICE"}]


def test_hybrid_contract_disambiguates_duplicate_item_keys() -> None:
    raw = {
        "work_items": [
            {"item_key": "M1.W01.ACTION", "constraint_set": {}},
            {"item_key": "M1.W01.ACTION", "constraint_set": {}},
        ]
    }

    normalize_hybrid_contract(raw)

    assert [item["item_key"] for item in raw["work_items"]] == [
        "M1.W01.ACTION",
        "M1.W01.ACTION.2",
    ]


def test_semantic_plan_provider_authors_then_compiles_outline() -> None:
    calls = []
    document = "# 总体策略\n九月碎片推进，十月全天冲刺。"
    outline = {
        "assistant_message": "总体分配已完成。",
        "summary": "前期碎片推进，后期集中冲刺并保留缓冲。",
        "total_weeks": 2,
        "weekly_execution_days": 6,
        "weekly_minutes": 900,
        "milestones": [
            {
                "stage_order": 1,
                "title": "建立基础",
                "purpose": "完成后续执行需要的基础准备。",
                "start_week": 1,
                "end_week": 2,
                "outputs": ["基础清单"],
            }
        ],
        "temporal_segments": [
            {
                "label": "碎片推进期",
                "start_week": 1,
                "end_week": 1,
                "date_scope": "九月",
                "capacity_summary": "工作日碎片推进，周日集中完成。",
                "weekly_execution_days": 3,
                "suggested_daily_minutes": 120,
                "frequency_rules": ["周日完成一次完整练习。"],
                "rest_rules": [],
                "holiday_rules": [],
            },
            {
                "label": "全天冲刺期",
                "start_week": 2,
                "end_week": 2,
                "date_scope": "十月",
                "capacity_summary": "每天可全天投入。",
                "weekly_execution_days": 6,
                "suggested_daily_minutes": 480,
                "frequency_rules": ["每个执行日完成一套练习。"],
                "rest_rules": ["周日用于综合复盘。"],
                "holiday_rules": [],
            },
        ],
        "buffer_strategy": ["每周保留机动时间。"],
        "assumptions": [],
        "open_questions": [],
    }

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        value = document if len(calls) == 1 else json.dumps(outline, ensure_ascii=False)
        return {"choices": [{"message": {"content": value}}]}

    provider = SemanticPlanProvider(
        NaturalPlanOutlineAuthor(settings(), transport=transport),
        NaturalPlanDetailAuthor(settings(), transport=transport),
        OpenAICompatiblePlanProvider(settings(), transport=transport),
    )
    result = provider.draft_outline(context())

    assert len(calls) == 2
    assert "只输出自然语言大纲" in calls[0]["messages"][0]["content"]
    compiler_input = json.loads(calls[1]["messages"][1]["content"])
    assert compiler_input["source_document"] == document
    assert result.source_document == document
    assert [segment.weekly_execution_days for segment in result.temporal_segments] == [3, 6]


def test_semantic_plan_provider_authors_then_compiles_week_details() -> None:
    calls = []
    document = "# W1\n## D1\n1. 整理基础清单，60 分钟，产出基础清单。"
    details = {
        "days": [
            {
                "stage_order": 1,
                "week_index": 1,
                "day_index": 1,
                "primary_goal": "整理完成后续执行需要的基础清单。",
                "suggested_minutes": 90,
                "tasks": [
                    {
                        "order": 1,
                        "title": "整理基础清单",
                        "description": "列出当前已经具备和仍需补齐的基础。",
                        "estimate_minutes": 60,
                        "priority": 1,
                        "task_kind": "PREPARE",
                        "is_required": True,
                        "evidence": ["基础清单"],
                        "depends_on": [],
                    }
                ],
                "evidence": ["基础清单"],
                "do_not": [],
            }
        ]
    }
    outline = {
        "total_weeks": 1,
        "weekly_execution_days": 1,
        "milestones": [
            {
                "stage_order": 1,
                "title": "建立基础",
                "purpose": "完成基础准备。",
                "start_week": 1,
                "end_week": 1,
                "outputs": ["基础清单"],
            }
        ],
        "temporal_segments": [
            {
                "label": "碎片推进期",
                "start_week": 1,
                "end_week": 1,
                "date_scope": "第一周",
                "capacity_summary": "本周安排一个执行日。",
                "weekly_execution_days": 1,
                "suggested_daily_minutes": 90,
                "frequency_rules": [],
                "rest_rules": [],
                "holiday_rules": [],
            }
        ],
    }

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        value = document if len(calls) == 1 else json.dumps(details, ensure_ascii=False)
        return {"choices": [{"message": {"content": value}}]}

    provider = SemanticPlanProvider(
        NaturalPlanOutlineAuthor(settings(), transport=transport),
        NaturalPlanDetailAuthor(settings(), transport=transport),
        OpenAICompatiblePlanProvider(settings(), transport=transport),
    )
    result = provider.draft_details(context(), outline, 1, 1)

    assert len(calls) == 2
    assert "只输出自然语言周计划" in calls[0]["messages"][0]["content"]
    compiler_input = json.loads(calls[1]["messages"][1]["content"])
    assert compiler_input["source_document"] == document
    assert compiler_input["requested_weeks"] == {"start": 1, "end": 1}
    assert result.source_document == document
    assert result.compiler_report["task_count"] == 1


def test_fake_hybrid_provider_emits_work_items_and_progressive_plan_days() -> None:
    provider = FakePlanProvider()
    checkpoints: list[str] = []

    outline = provider.draft_hybrid_outline(context(), checkpoint=checkpoints.append)
    details = provider.draft_hybrid_details(
        context(), outline.model_dump(mode="json"), 1, 1, checkpoint=checkpoints.append
    )

    assert len(checkpoints) == 2
    assert outline.standing_work_items[0].item_key == "PLAN.WEEKLY.REVIEW"
    assert details.work_items[0].item_key == "M1.W01.PRIMARY"
    assert details.work_items[0].constraint_set.temporal.start_week == 1
    assert len(details.days) == outline.weekly_execution_days
    assert details.days[0].tasks[0].title != details.days[1].tasks[0].title


def test_hybrid_schema_retry_reuses_author_with_validation_feedback() -> None:
    class CountingAuthor:
        calls = 0

        def draft_hybrid_document(self, _context, _mode):
            self.calls += 1
            return "# 可恢复的自然语言设计稿"

    class FailingOnceCompiler(FakePlanProvider):
        calls = 0

        def draft_hybrid_outline(self, context, generation_mode="STANDARD", checkpoint=None):
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailableError(
                    "编译失败",
                    code="INVALID_MODEL_RESPONSE",
                    diagnostic="invalid json",
                )
            return super().draft_hybrid_outline(context, generation_mode, checkpoint)

    author = CountingAuthor()
    compiler = FailingOnceCompiler()
    provider = SemanticPlanProvider(author, author, compiler)  # type: ignore[arg-type]
    checkpoints: list[str] = []

    result = provider.draft_hybrid_outline(context(), checkpoint=checkpoints.append)

    assert author.calls == 1
    assert compiler.calls == 2
    assert checkpoints == ["# 可恢复的自然语言设计稿"]
    assert result.source_document == checkpoints[0]
    assert result.compiler_report["semantic_repair_rounds"] == 1


def test_outline_compiler_keeps_capacity_when_promises_exceed_it() -> None:
    raw = {
        "temporal_segments": [
            {
                "start_week": 1,
                "end_week": 2,
                "suggested_daily_minutes": 90,
            },
            {
                "start_week": 3,
                "end_week": 3,
                "suggested_daily_minutes": 360,
            },
        ],
        "commitments": [
            {
                "start_week": 1,
                "end_week": 3,
                "cadence": "PER_EXECUTION_DAY",
                "minimum_occurrences": 1,
                "minimum_minutes_per_occurrence": 60,
            },
            {
                "start_week": 1,
                "end_week": 2,
                "cadence": "PER_EXECUTION_DAY",
                "minimum_occurrences": 1,
                "minimum_minutes_per_occurrence": 150,
            },
            {
                "start_week": 1,
                "end_week": 2,
                "cadence": "PER_WEEK",
                "minimum_occurrences": 6,
                "source_text": "六个普通日完成日常训练。",
            },
            {
                "start_week": 1,
                "end_week": 2,
                "cadence": "PER_WEEK",
                "minimum_occurrences": 1,
                "source_text": "周日只完成一整套试卷。",
            },
            {
                "start_week": 1,
                "end_week": 2,
                "cadence": "PER_WEEK",
                "minimum_occurrences": 1,
                "minimum_minutes_per_occurrence": 60,
                "source_text": "大作文复盘另安排一次。",
            },
        ],
    }

    normalize_compiled_outline_capacity(raw)

    assert raw["temporal_segments"][0]["suggested_daily_minutes"] == 90
    assert "weekly_execution_days" not in raw["temporal_segments"][0]
    assert "exception_days_per_week" not in raw["temporal_segments"][0]
    assert "exception_daily_minutes" not in raw["temporal_segments"][0]
    assert raw["temporal_segments"][1]["suggested_daily_minutes"] == 360
    assert "weekly_execution_days" not in raw
    assert raw["commitments"][-2]["requires_separate_execution_day"] is True
    assert raw["commitments"][-1]["adds_to_daily_baseline"] is True


def test_detail_compiler_normalizes_capacity_and_named_dependencies() -> None:
    raw = {
        "days": [
            {
                "stage_order": 1,
                "week_index": 1,
                "day_index": 1,
                "suggested_minutes": 30,
                "tasks": [
                    {
                        "order": 1,
                        "title": "整理考试材料",
                        "estimate_minutes": 45,
                        "evidence": ["材料清单"],
                        "depends_on": [],
                    },
                    {
                        "order": 2,
                        "title": "完成第一套模拟卷",
                        "estimate_minutes": 90,
                        "evidence": ["答题记录", "错题清单"],
                        "depends_on": ["整理考试材料"],
                    },
                ],
            }
        ]
    }

    normalize_compiled_plan_days(raw, "days")

    day = raw["days"][0]
    assert day["suggested_minutes"] == 135
    assert day["evidence"] == ["材料清单", "答题记录", "错题清单"]
    assert day["tasks"][1]["depends_on"] == ["M1-W1-D1-T1"]


def test_detail_compiler_resolves_dependency_from_prior_week_registry() -> None:
    raw = {
        "days": [
            {
                "stage_order": 1,
                "week_index": 2,
                "day_index": 1,
                "primary_goal": "复盘上一周模拟训练",
                "suggested_minutes": 60,
                "evidence": ["复盘记录"],
                "tasks": [
                    {
                        "order": 1,
                        "title": "复盘第一套模拟卷",
                        "description": "根据答题记录完成错因复盘。",
                        "estimate_minutes": 60,
                        "priority": 1,
                        "task_kind": "REVIEW",
                        "is_required": True,
                        "evidence": ["错因清单"],
                        "depends_on": ["M4-W1-D7-T1"],
                    }
                ],
                "do_not": [],
            }
        ]
    }

    normalize_compiled_plan_days(
        raw,
        "days",
        [{"title": "完成第一套模拟卷", "reference": "M1-W1-D7-T1"}],
    )

    assert raw["days"][0]["tasks"][0]["depends_on"] == ["M1-W1-D7-T1"]


def test_gateway_504_is_retried_once_and_has_clear_code() -> None:
    attempts = []

    def transport(url, *_args):
        attempts.append(1)
        raise HTTPError(url, 504, "Gateway Timeout", None, None)

    provider = OpenAICompatiblePlanProvider(
        settings(plan_max_retries=3),
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        provider.draft_outline(context())

    assert len(attempts) == 2
    assert raised.value.code == "MODEL_GATEWAY_TIMEOUT"
    assert raised.value.diagnostic == "HTTP 504"
    assert "等待得有些久" in str(raised.value)


def test_deepseek_maps_standard_and_deep_modes_inside_its_adapter() -> None:
    calls = []
    outline = {
        "assistant_message": "总体分配已完成。",
        "summary": "用一周完成基础准备。",
        "total_weeks": 1,
        "weekly_execution_days": 1,
        "weekly_minutes": 90,
        "milestones": [
            {
                "stage_order": 1,
                "title": "建立基础",
                "purpose": "完成后续执行需要的基础准备。",
                "start_week": 1,
                "end_week": 1,
                "outputs": ["基础清单"],
            }
        ],
        "buffer_strategy": ["保留机动时间。"],
        "assumptions": [],
        "open_questions": [],
    }

    def transport(_url, _headers, payload, timeout):
        calls.append((payload, timeout))
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"```json\n{json.dumps(outline, ensure_ascii=False)}\n```",
                        }
                    ],
                }
            ],
        }

    provider = DeepSeekPlanProvider(
        settings(
            llm_provider="deepseek",
            plan_timeout_seconds=120,
            plan_deep_timeout_seconds=900,
        ),
        transport=transport,
    )

    provider.draft_outline(context(), "STANDARD")
    provider.draft_outline(context(), "DEEP")
    provider.draft_hybrid_outline(context(), "STANDARD")

    assert provider.supported_generation_modes == ("STANDARD", "DEEP")
    assert calls[0][0]["reasoning"] == {"effort": "none"}
    assert "strict" not in calls[0][0]["text"]["format"]
    assert calls[0][1] == 120
    assert calls[1][0]["reasoning"] == {"effort": "high"}
    assert calls[1][1] == 900
    assert calls[2][0]["reasoning"] == {"effort": "low"}


def test_outline_drops_unexpected_top_level_note() -> None:
    outline = {
        "assistant_message": "总体分配已经完成。",
        "summary": "用一周完成基础准备，并保留合理的机动时间。",
        "total_weeks": 1,
        "weekly_execution_days": 1,
        "weekly_minutes": 90,
        "milestones": [
            {
                "stage_order": 1,
                "title": "建立基础",
                "purpose": "完成后续执行需要的基础准备。",
                "start_week": 1,
                "end_week": 1,
                "outputs": ["基础清单"],
            }
        ],
        "buffer_strategy": ["保留机动时间。"],
        "assumptions": [],
        "open_questions": [],
        "open_questions_note": "当前没有阻塞问题。",
    }

    def transport(_url, _headers, _payload, _timeout):
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(outline)}],
                }
            ],
        }

    provider = DeepSeekPlanProvider(settings(llm_provider="deepseek"), transport=transport)

    result = provider.draft_outline(context())

    assert result.open_questions == []
    assert not hasattr(result, "open_questions_note")


def test_outline_and_detail_have_separate_retry_limits() -> None:
    attempts = {"outline": 0, "detail": 0}
    current_kind = "outline"

    def transport(_url, *_args):
        attempts[current_kind] += 1
        raise URLError("temporary")

    provider = DeepSeekPlanProvider(
        settings(
            llm_provider="deepseek",
            plan_outline_max_retries=2,
            plan_detail_max_retries=1,
        ),
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(ProviderUnavailableError):
        provider.draft_outline(context())
    current_kind = "detail"
    with pytest.raises(ProviderUnavailableError):
        provider.draft_details(context(), {}, 1, 1)

    assert attempts == {"outline": 3, "detail": 2}


def _length_response(provider_name, content="unfinished"):
    if provider_name == "deepseek":
        return {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    # Even syntactically complete JSON must not be accepted when the provider
    # explicitly reports a length stop; the final required task could be missing.
    return {"choices": [{"finish_reason": "length", "message": {"content": content}}]}


def _completed_response(provider_name, content):
    if provider_name == "deepseek":
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": content},
        ]}]}
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


@pytest.mark.parametrize("provider_name", ["deepseek", "openai_compatible"])
@pytest.mark.parametrize("author_class", [NaturalPlanOutlineAuthor, NaturalPlanDetailAuthor])
def test_author_expands_truncated_output_and_resets_budget_for_next_request(
    provider_name, author_class,
):
    calls = []
    document = "# 完整规划\n1. 阅读训练\n2. 真题训练\n3. 订正复盘"

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        if len(calls) <= 2:
            return _length_response(provider_name)
        return _completed_response(provider_name, document)

    author = author_class(settings(
        llm_provider=provider_name, plan_outline_max_retries=2, plan_detail_max_retries=2,
    ), transport=transport, sleeper=lambda _: None)
    ctx = context()
    outline = FakePlanProvider().draft_hybrid_outline(ctx).model_dump()

    def draft():
        if author_class == NaturalPlanDetailAuthor:
            return author.draft_hybrid_document(ctx, outline, 1, 1)
        return author.draft_hybrid_document(ctx)

    assert draft() == document
    token_key = "max_output_tokens" if provider_name == "deepseek" else "max_tokens"
    initial_budget = 12000 if author_class == NaturalPlanDetailAuthor else 6000
    assert [call[token_key] for call in calls] == [initial_budget, initial_budget * 2, 32000]
    instruction = calls[-1].get("instructions") or calls[-1]["messages"][-1]["content"]
    assert "不得删任务" in instruction
    inputs = [call.get("input") or call["messages"][1]["content"] for call in calls]
    assert all(value == inputs[0] for value in inputs)
    assert json.loads(inputs[-1])["confirmed_requirements"] == ctx.requirements
    assert draft() == document
    assert calls[-1][token_key] == initial_budget  # Expansion never mutates the global setting.


@pytest.mark.parametrize("provider_class,provider_name", [
    (DeepSeekPlanProvider, "deepseek"),
    (OpenAICompatiblePlanProvider, "openai_compatible"),
])
@pytest.mark.parametrize("hybrid", [False, True])
def test_compiler_expands_length_stop_without_accepting_partial_task_sets(
    provider_class, provider_name, hybrid,
):
    ctx = context()
    fake = FakePlanProvider()
    expected = fake.draft_hybrid_outline(ctx) if hybrid else fake.draft_outline(ctx)
    encoded = expected.model_dump_json()
    calls = []

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return _length_response(provider_name, encoded)
        return _completed_response(provider_name, encoded)

    provider = provider_class(settings(llm_provider=provider_name, plan_outline_max_retries=1),
                              transport=transport, sleeper=lambda _: None)
    result = provider.draft_hybrid_outline(ctx) if hybrid else provider.draft_outline(ctx)
    key = "max_output_tokens" if provider_name == "deepseek" else "max_tokens"
    assert [call[key] for call in calls] == [24000 if hybrid else 4000, 32000]
    assert result.milestones == expected.milestones
    assert result.commitments == expected.commitments


@pytest.mark.parametrize("provider_class,provider_name", [
    (DeepSeekPlanProvider, "deepseek"),
    (OpenAICompatiblePlanProvider, "openai_compatible"),
])
def test_repeated_truncation_is_bounded_and_not_misreported_as_schema_error(
    provider_class, provider_name,
):
    calls = []

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        return _length_response(provider_name)

    provider = provider_class(settings(llm_provider=provider_name,
                                       plan_outline_max_retries=2, plan_output_retry_max_tokens=12000),
                              transport=transport, sleeper=lambda _: None)
    with pytest.raises(ProviderUnavailableError) as raised:
        provider.draft_outline(context())
    assert raised.value.code == "MODEL_OUTPUT_TRUNCATED"
    key = "max_output_tokens" if provider_name == "deepseek" else "max_tokens"
    assert [call[key] for call in calls] == [4000, 8000, 12000]


@pytest.mark.parametrize("provider_class,provider_name", [
    (DeepSeekPlanProvider, "deepseek"),
    (OpenAICompatiblePlanProvider, "openai_compatible"),
])
def test_schema_failure_does_not_raise_output_budget(provider_class, provider_name):
    calls = []
    encoded = FakePlanProvider().draft_outline(context()).model_dump_json()

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        return _completed_response(provider_name, "{}" if len(calls) == 1 else encoded)

    provider = provider_class(settings(llm_provider=provider_name, plan_outline_max_retries=1),
                              transport=transport, sleeper=lambda _: None)
    provider.draft_outline(context())
    key = "max_output_tokens" if provider_name == "deepseek" else "max_tokens"
    assert [call[key] for call in calls] == [4000, 4000]
