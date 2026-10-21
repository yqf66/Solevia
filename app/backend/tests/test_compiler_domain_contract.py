"""Real schema classification and retry boundaries; no live-model claims here."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.plan_ai import (
    FakePlanProvider, OpenAICompatiblePlanProvider, PlanContext, SemanticPlanProvider,
    _plan_generation_error, _plan_input,
)
from planner_api.plan_compiler_contract import (
    CompilerRepresentationError, compiler_instructions, detail_capacity_contract, representation_error,
)
from planner_api.plan_prompts import HYBRID_PLAN_DETAIL_COMPILER_INSTRUCTIONS
from planner_api.schemas import HybridPlanDetailResult
from planner_api.specialist_context import resolve_snapshot_references


def context():
    return PlanContext(requirements={"objective": "报告", "total_minutes": 600},
                       route={"stages": [{"order": 1, "title": "报告", "purpose": "完成阅读报告", "outputs": ["报告"]}],
                              "schedule_type": "OUTCOME"})


def draft():
    ctx = context()
    outline = FakePlanProvider().draft_hybrid_outline(ctx).model_dump(mode="json")
    return FakePlanProvider().draft_hybrid_details(ctx, outline, 1, 1).model_dump(mode="json")


def schema_error(raw):
    with pytest.raises(ValidationError) as caught:
        HybridPlanDetailResult.model_validate(raw)
    return caught.value


@pytest.mark.parametrize("field", ["task", "item"])
def test_only_known_field_enum_errors_are_representation_errors(field):
    raw = draft()
    if field == "task":
        raw["days"][0]["tasks"][0]["scheduling_policy"] = "WINDOW"
    else:
        raw["work_items"][0]["constraint_set"]["binding_mode"] = "DATE_WINDOW"
    original = deepcopy(raw)
    error = schema_error(raw)
    assert representation_error(error)
    assert isinstance(_plan_generation_error(error), CompilerRepresentationError)
    assert raw == original  # No automatic WINDOW conversion or lost constraints.


@pytest.mark.parametrize("other", ["capacity", "missing", "dependency"])
def test_mixed_enum_and_semantic_errors_never_get_compile_only_label(other):
    raw = draft()
    raw["days"][0]["tasks"][0]["scheduling_policy"] = "WINDOW"
    if other == "capacity":
        day = deepcopy(raw["days"][0])
        day["day_index"] = 2
        day["tasks"][0]["scheduling_policy"] = "FLOATING"
        day["suggested_minutes"] = 10
        day["tasks"][0]["estimate_minutes"] = 80
        raw["days"].append(day)
    elif other == "missing":
        del raw["work_items"][0]["title"]
    else:
        raw["work_item_dependencies"] = [{"predecessor_key": "SELF", "successor_key": "SELF"}]
    error = schema_error(raw)
    assert not representation_error(error)
    assert not isinstance(_plan_generation_error(error), CompilerRepresentationError)


@pytest.mark.parametrize("method", ["draft_outline", "draft_details", "draft_hybrid_outline", "draft_hybrid_details"])
@pytest.mark.parametrize("enabled", [True, False])
def test_repeated_known_enum_error_is_bounded_and_keeps_author_only_when_enabled(method, enabled):
    class Author:
        calls = 0
        def generate(self, *_args):
            self.calls += 1
            return f"完整设计稿{self.calls}：保留600分钟目标与阶段容量60/60/480/480"
        draft_document = draft_hybrid_document = generate

    class Compiler(FakePlanProvider):
        _compiler_field_retry_enabled = enabled
        calls = 0
        def generate(self, ctx, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise CompilerRepresentationError("字段非法", code="INVALID_MODEL_RESPONSE", diagnostic="scheduling_policy: WINDOW")
            assert ctx.source_document.startswith("完整设计稿1" if enabled else "完整设计稿2")
            if enabled:
                feedback = ctx.planning_context["generation_validation_feedback"]
                assert feedback["failure_class"] == "COMPILER_FIELD_DOMAIN" and feedback["compile_only_repair"]
            return getattr(super(), method)(ctx, *args, **kwargs)

    setattr(Compiler, method, Compiler.generate)
    author, compiler = Author(), Compiler()
    provider = SemanticPlanProvider(author, author, compiler)
    args = (context(),)
    if "details" in method:
        args += (FakePlanProvider().draft_hybrid_outline(context()).model_dump(mode="json"), 1, 1)
    result = getattr(provider, method)(*args)
    assert compiler.calls == 3 and author.calls == (1 if enabled else 2)
    assert result.source_document.startswith("完整设计稿1" if enabled else "完整设计稿2")


def test_persistent_field_errors_stop_at_existing_retry_limit():
    class Author:
        calls = 0
        def draft_hybrid_document(self, *_args):
            self.calls += 1
            return "原稿全部保留"
    class Compiler:
        calls = 0
        def draft_hybrid_outline(self, *_args):
            self.calls += 1
            raise CompilerRepresentationError("字段非法", code="INVALID_MODEL_RESPONSE")
    author, compiler = Author(), Compiler()
    with pytest.raises(ProviderUnavailableError):
        SemanticPlanProvider(author, author, compiler).draft_hybrid_outline(context())
    assert compiler.calls == 3 and author.calls == 1


def test_capacity_ledger_keeps_each_phase_and_scoped_override_without_modifying_input():
    outline = {"weekly_execution_days": 2, "temporal_segments": [
        {"start_week": 1, "end_week": 2, "weekly_execution_days": 2, "suggested_daily_minutes": 30},
        {"start_week": 3, "end_week": 4, "weekly_execution_days": 4, "suggested_daily_minutes": 120},
    ]}
    original = deepcopy(outline)
    ledger = detail_capacity_contract(outline, {"start": 1, "end": 4})
    assert [row["weekly_minutes"] for row in ledger["weeks"]] == [60, 60, 480, 480]
    assert outline == original
    outline["temporal_segments"][1]["weekly_capacity_overrides"] = [
        {"week_index": 3, "available_days_per_week": 2, "suggested_daily_minutes": 30,
         "exception_days_per_week": 1, "exception_daily_minutes": 20}]
    assert [row["weekly_minutes"] for row in detail_capacity_contract(outline, {"start": 1, "end": 4})["weeks"]] == [60, 60, 50, 480]


def test_capacity_unknown_stays_unknown_and_contract_off_keeps_original_input():
    outline = {"weekly_execution_days": 2, "temporal_segments": [{"start_week": 1, "end_week": 1}]}
    assert detail_capacity_contract(outline, {"start": 1, "end": 1})["weeks"][0]["weekly_minutes"] is None
    assert detail_capacity_contract({}, {"start": 1, "end": 1}) is None
    assert detail_capacity_contract(outline, {"start": 1, "end": 520}) is None
    kwargs = dict(plan_outline=outline, requested_weeks={"start": 1, "end": 1})
    on = resolve_snapshot_references(json.loads(_plan_input(context(), compiler_contract_enabled=True, **kwargs)))
    off = resolve_snapshot_references(json.loads(_plan_input(context(), compiler_contract_enabled=False, **kwargs)))
    assert on.pop("compiled_week_capacity")["weeks"][0]["weekly_minutes"] is None
    on.pop("_context_manifest")
    off.pop("_context_manifest")
    assert on == off


def test_domain_instructions_preserve_other_contracts_and_deduplicate_exact_set_check():
    original = HYBRID_PLAN_DETAIL_COMPILER_INSTRUCTIONS
    assert compiler_instructions(original, HybridPlanDetailResult, enabled=False) == original
    enriched = compiler_instructions(original, HybridPlanDetailResult, enabled=True)
    assert enriched.count("输出前必须完成集合自检：work_items") == 1
    assert "WINDOW 不能填入任务字段" in enriched and "workload_key" in enriched
    assert "次数承诺只由完整行动的验收任务引用" in enriched
    assert "M{阶段}-W{周}-D{日}-T{序号}" in enriched


def test_actual_provider_classifies_schema_error_without_changing_user_capacity():
    raw = draft()
    raw["days"][0]["tasks"][0]["scheduling_policy"] = "WINDOW"
    settings = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key="unit-only", plan_detail_max_retries=0)
    provider = OpenAICompatiblePlanProvider(settings, transport=lambda *_args: {
        "choices": [{"message": {"content": json.dumps(raw)}}]}, sleeper=lambda _: None)
    with pytest.raises(CompilerRepresentationError):
        provider.draft_hybrid_details(context(), FakePlanProvider().draft_hybrid_outline(context()).model_dump(mode="json"), 1, 1)


def test_wrong_unrelated_enum_never_gets_representation_classification():
    raw = draft()
    raw["work_items"][0]["constraint_set"]["fulfillment_mode"] = "UNKNOWN"
    assert not representation_error(schema_error(raw))
    assert not representation_error(ValueError("WINDOW"))


def test_error_classification_checks_semantic_failure_beyond_first_six_diagnostics():
    raw = draft()
    template = deepcopy(raw["days"][0]["tasks"][0])
    raw["days"][0]["suggested_minutes"] = 1000
    raw["days"][0]["tasks"] = [{**deepcopy(template), "order": order, "scheduling_policy": "WINDOW"}
                              for order in range(1, 8)]
    del raw["work_items"][0]["title"]
    error = schema_error(raw)
    assert len(error.errors()) >= 8
    assert not representation_error(error)


def test_production_defaults_keep_experimental_prompt_off_and_field_retry_on():
    settings = Settings(_env_file=None)
    assert not settings.plan_compiler_contract_enabled
    assert settings.plan_compiler_field_retry_enabled


def test_default_field_retry_does_not_change_successful_wire_request_or_result():
    raw = draft()
    calls = []
    def transport(_url, _headers, payload, _timeout):
        calls.append(deepcopy(payload))
        return {"choices": [{"message": {"content": json.dumps(raw)}}]}
    base = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key="unit-only")
    results = []
    for enabled in (False, True):
        provider = OpenAICompatiblePlanProvider(base.model_copy(update={"plan_compiler_field_retry_enabled": enabled}),
                                               transport=transport)
        results.append(provider.draft_hybrid_details(context(),
            FakePlanProvider().draft_hybrid_outline(context()).model_dump(mode="json"), 1, 1))
    assert len(calls) == 2 and calls[0] == calls[1]
    assert results[0] == results[1]
    assert "compiled_week_capacity" not in json.loads(calls[0]["messages"][1]["content"])
