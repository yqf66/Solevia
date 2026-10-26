import json
import threading
from concurrent.futures import ThreadPoolExecutor

from planner_api.ai import DeepSeekClarificationProvider, OpenAICompatibleClarificationProvider
from planner_api.context_compiler import ContextCompiler, deduplicate_context_snapshots
from planner_api.generation_orchestration import GenerationOrchestrator
from planner_api.plan_ai import _expand_truncated_plan_output


def test_context_references_are_lossless_and_do_not_merge_distinct_tasks():
    requirements = {"capacity": {"week_1": 60, "week_2": 480}, "user": "约束" * 200}
    tasks = [{"id": "a", "minutes": 20}, {"id": "b", "minutes": 20}]
    payload = {
        "confirmed_requirements": requirements,
        "planning_context": {"requirements": requirements, "tasks": tasks},
    }
    compiled = deduplicate_context_snapshots(payload)
    ref = compiled["planning_context"]["requirements"]["$context_ref"]
    assert ref == "$.confirmed_requirements"
    assert compiled["confirmed_requirements"] == payload["planning_context"]["requirements"]
    assert compiled["planning_context"]["tasks"] == tasks
    assert payload["planning_context"]["requirements"] == requirements
    assert len(json.dumps(compiled)) < len(json.dumps(payload))


def test_nested_requirement_snapshot_is_referenced_only_when_identical():
    snapshot = {"scope": "原始约束" * 200, "capacity": {"week1": 60, "week3": 480}}
    payload = {"confirmed_requirements": snapshot,
               "planning_context": {"requirement": {"id": "r1", "snapshot": snapshot}}}
    result = deduplicate_context_snapshots(payload)
    assert result["planning_context"]["requirement"]["id"] == "r1"
    assert result["planning_context"]["requirement"]["snapshot"] == {"$context_ref": "$.confirmed_requirements"}
    payload["planning_context"]["requirement"]["snapshot"] = {**snapshot, "new_constraint": "禁止改动周日"}
    assert "_context_references" not in deduplicate_context_snapshots(payload)


def test_exact_evidence_deduplication_keeps_status_changes_and_repeated_messages():
    fact = {"id": "f1", "status": "CONFIRMED", "value": "每周60分钟"}
    changed = {**fact, "status": "SUPERSEDED"}
    message = {"role": "user", "content": "我确认"}
    payload = {
        "recent_changes": [fact, fact, changed],
        "recent_messages": [message, message],
        "tasks": [{"title": "训练"}, {"title": "训练"}],
    }
    result = ContextCompiler().compile(payload, task_type="TEST").payload
    assert result["recent_changes"] == [fact, changed]
    assert result["recent_messages"] == [message, message]
    assert len(result["tasks"]) == 2


def test_replaced_snapshot_parent_has_no_dangling_child_references():
    snapshot = {"scope": "保留约束" * 200, "minutes": 60}
    wrapper = {"id": "r1", "snapshot": snapshot}
    payload = {"confirmed_requirements": snapshot,
               "planning_context": {"requirement": wrapper},
               "existing_context": {"requirement": wrapper}}
    result = deduplicate_context_snapshots(payload)

    def resolve(path):
        node = result
        for key in path.removeprefix("$.").split("."):
            node = node[key]
        return node

    for reference in result["_context_references"]["references"]:
        assert resolve(reference["path"]) == {"$context_ref": reference["target"]}
        assert resolve(reference["target"])
    assert result["existing_context"]["requirement"]["id"] == "r1"
    assert result["existing_context"]["requirement"]["snapshot"] == {
        "$context_ref": "$.confirmed_requirements"
    }
    assert payload["existing_context"]["requirement"] == wrapper


def test_repeated_retry_feedback_has_one_latest_diagnostic_and_original_contract():
    payload = {"instructions": "容量60分钟，全部任务保留", "max_output_tokens": 3000}
    first = DeepSeekClarificationProvider._add_retry_feedback(payload, "old-error")
    second = DeepSeekClarificationProvider._add_retry_feedback(first, "new-error")
    assert "old-error" not in second["instructions"]
    assert "new-error" in second["instructions"]
    assert "容量60分钟，全部任务保留" in second["instructions"]
    chat = {"messages": [{"role": "system", "content": "不可删任务"}]}
    chat = OpenAICompatibleClarificationProvider._add_retry_feedback(chat, "old")
    chat = OpenAICompatibleClarificationProvider._add_retry_feedback(chat, "new")
    assert len(chat["messages"]) == 2
    assert chat["messages"][0]["content"] == "不可删任务"


def test_truncation_retry_remains_bounded_and_keeps_capacity_protection():
    payload = {"instructions": "原始合同", "max_output_tokens": 4000}
    for final_retry in (False, True):
        payload = _expand_truncated_plan_output(payload, 32000, final_retry=final_retry)
    assert payload["max_output_tokens"] == 32000
    assert payload["instructions"].count("[RETRY_FEEDBACK]") == 1
    assert "不得删任务" in payload["instructions"]
    assert "用户时间容量" in payload["instructions"]


def test_specialist_receives_date_contract_and_confirmed_requirements():
    context = {
        "requirement": {"deadline": "2026-11-01"},
        "planning_horizon_contract": {"start": "2026-10-11"},
        "precedence_rules": ["已确认事实优先"],
        "schedule_context": {"weekday": 6, "minutes": 60},
    }
    assert GenerationOrchestrator._compact(context, "CALENDAR_SPECIALIST") == context


def test_concurrent_cold_encoder_requests_load_once(monkeypatch):
    from planner_api.materials import semantic

    calls = []
    barrier = threading.Barrier(4)

    def loader(*args):
        calls.append(args)
        return object()

    monkeypatch.setattr(semantic, "LocalE5Encoder", loader)
    semantic._cached_encoder.cache_clear()
    try:
        def request(_):
            barrier.wait(timeout=5)
            return semantic.encoder("model", "cache", "revision")

        with ThreadPoolExecutor(max_workers=4) as pool:
            instances = list(pool.map(request, range(4)))
        assert len(calls) == 1
        assert all(instance is instances[0] for instance in instances)
    finally:
        semantic._cached_encoder.cache_clear()


def test_encoder_prewarm_is_opt_in_and_fails_without_breaking_startup(monkeypatch):
    from planner_api.config import Settings
    from planner_api.materials import semantic

    assert semantic.prewarm_encoder(Settings(_env_file=None)) is None
    started = threading.Event()

    def unavailable(*_args):
        started.set()
        raise RuntimeError("offline weights unavailable")

    monkeypatch.setattr(semantic, "encoder", unavailable)
    worker = semantic.prewarm_encoder(Settings(_env_file=None, material_semantic_enabled=True, material_semantic_prewarm=True))
    assert started.wait(timeout=2)
    worker.join(timeout=2)
    assert not worker.is_alive()
