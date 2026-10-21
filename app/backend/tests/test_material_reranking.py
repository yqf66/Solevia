from dataclasses import asdict

import pytest

from planner_api.config import Settings
from planner_api.materials.analysis_ai import MaterialAnalysisPassage
from planner_api.materials.reranking import IDENTITY, guarded_order, _slot


def passages():
    return [MaterialAnalysisPassage(str(i), "a" if i < 4 else "b", f"r{i//4}", i,
                                   "章节", "页码", text, "完整标题", "paper.pdf")
            for i, text in enumerate(["唯一资料锚点", "普通背景", "关键相关证据",
                                      "例外：仅当空库才适用", "另一资料的冲突说明", "另一资料细节"])]


def valid(scores):
    return {"identity": IDENTITY, "scores": scores,
            "coverage": {"version": "bge-coverage-v2", "complete": True,
                         "windows_per_passage": [1]*len(scores), "total_windows": len(scores)}}


def test_guard_pins_topics_sources_counterevidence_and_keeps_all_original_content():
    original = passages()
    result, info = guarded_order("迁移条件", original,
        Settings(_env_file=None, material_rerank_mode="GUARDED"),
        transport=lambda *_: valid([0, 1, 99, -100, -200, 50]))
    assert [p.passage_id for p in result] == ["0", "2", "5", "3", "4", "1"]
    assert {p.passage_id: asdict(p) for p in result} == {p.passage_id: asdict(p) for p in original}
    assert info["status"] == "APPLIED" and info["dropped_count"] == 0
    result, _ = guarded_order("迁移条件", original,
        Settings(_env_file=None, material_rerank_mode="GUARDED"), topic_anchors={"1"},
        transport=lambda *_: valid([0, 1, 99, -100, -200, 50]))
    assert result[1] == original[1]


@pytest.mark.parametrize("mutation", ["identity", "nan", "bool", "missing", "coverage", "windows"])
def test_invalid_local_response_preserves_baseline(mutation):
    response = valid([0, 1, 2, 3, 4, 5])
    if mutation == "identity":
        response["identity"] = "unverified-model"
    if mutation == "nan":
        response["scores"][0] = float("nan")
    if mutation == "bool":
        response["scores"][0] = True
    if mutation == "missing":
        response["scores"].pop()
    if mutation == "coverage":
        response["coverage"]["complete"] = False
    if mutation == "windows":
        response["coverage"]["windows_per_passage"][0] = 0
    result, info = guarded_order("测试", passages(), Settings(_env_file=None, material_rerank_mode="GUARDED"),
                                transport=lambda *_: response)
    assert result == passages() and info["status"] == "FALLBACK"


def test_off_busy_budget_and_timeout_never_block_main_evidence():
    def forbidden(*_): pytest.fail("unexpected inference")
    result, info = guarded_order("测试", passages(), Settings(_env_file=None), transport=forbidden)
    assert result == passages() and info["status"] == "OFF"
    guarded = Settings(_env_file=None, material_rerank_mode="GUARDED")
    result, info = guarded_order("长"*1001, passages(), guarded, transport=forbidden)
    assert result == passages() and info["status"] == "SKIPPED_INPUT_BUDGET"
    _slot.acquire()
    try:
        result, info = guarded_order("测试", passages(), guarded, transport=forbidden)
        assert result == passages() and info["status"] == "SKIPPED_BUSY"
    finally:
        _slot.release()
    def timeout(*_): raise TimeoutError()
    result, info = guarded_order("测试", passages(), guarded, transport=timeout)
    assert result == passages() and info["status"] == "FALLBACK"


def test_final_multi_topic_selection_is_identical_with_optional_reranking(monkeypatch):
    from types import SimpleNamespace
    from planner_api.materials import analysis_service
    from planner_api.materials.analysis_service import MaterialAnalysisService
    from planner_api.materials.schemas import MaterialAnalyzeRequest, MaterialSearchHitRead
    rows = [MaterialSearchHitRead(source_id="source", revision_id="revision", revision=1,
            sequence=i, section_title="章节", text=f"证据{i}", score=6-i, locator="页1")
            for i in range(6)]
    class Processing:
        settings = Settings(_env_file=None)
        def search_goal_materials(self, goal_id, request):
            return rows[:3] if request.query == "主题甲" else rows[3:]
    processing = Processing()
    provider = SimpleNamespace(plan_queries=lambda _: ["主题甲", "主题乙"])
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(title="资料", relative_path="file.md"))
    service = MaterialAnalysisService(session, provider, processing)
    service._goal = lambda _: None
    request = MaterialAnalyzeRequest(query="同时检查两个主题", max_chunks=6, max_total_chars=1000)
    baseline, _, _ = service._retrieve("goal", request)
    processing.settings = Settings(_env_file=None, material_rerank_mode="GUARDED")
    real_guard = analysis_service.guarded_order
    def guard(*args, **kwargs):
        return real_guard(*args, **kwargs, transport=lambda *_: valid([0, 0, 1, 2, 99, 90]))
    monkeypatch.setattr(analysis_service, "guarded_order", guard)
    reordered, _, _ = service._retrieve("goal", request)
    assert {p.passage_id: asdict(p) for p in baseline} == {p.passage_id: asdict(p) for p in reordered}
    assert reordered[0] == baseline[0] and reordered[1] == baseline[1]
    assert service.rerank_status["status"] == "APPLIED"
    assert [p.passage_id for p in reordered] != [p.passage_id for p in baseline]


def test_all_protected_evidence_does_not_call_optional_runtime():
    original = passages()

    def forbidden(*_):
        pytest.fail("No movable pair; optional scoring must not run")

    output, info = guarded_order(
        "同一主题", original, Settings(_env_file=None, material_rerank_mode="GUARDED"),
        topic_anchors={p.passage_id for p in original}, transport=forbidden,
    )
    assert output == original
    assert info["status"] == "SKIPPED_NO_MOVABLE_PAIR"
    assert info["order_changed"] is False


def test_optional_prewarm_is_off_or_single_background_job(monkeypatch):
    import threading
    from planner_api.materials import reranking

    monkeypatch.setattr(reranking, "_last_warmup", 0.0)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def warm(url, payload, timeout):
        calls.append((url, payload, timeout))
        entered.set()
        assert release.wait(2)
        return {"ready": ["bge"]}

    assert reranking.prewarm_bge(Settings(_env_file=None), transport=warm) is None
    settings = Settings(_env_file=None, material_rerank_mode="GUARDED")
    worker = reranking.prewarm_bge(settings, transport=warm)
    try:
        assert entered.wait(1)
        assert worker.is_alive()  # startup/answer can continue during loading
        assert reranking.prewarm_bge(settings, transport=warm) is None
    finally:
        release.set()
        worker.join(2)
    assert calls == [(settings.local_model_base_url + "/warmup", {"models": ["bge"]}, 60)]
    assert reranking.prewarm_bge(settings, transport=warm) is None  # cooldown


def test_optional_prewarm_failure_releases_lock_and_keeps_evidence(monkeypatch):
    from planner_api.materials import reranking

    monkeypatch.setattr(reranking, "_last_warmup", 0.0)

    def unavailable(*_):
        raise ConnectionError()

    settings = Settings(_env_file=None, material_rerank_mode="GUARDED")
    worker = reranking.prewarm_bge(settings, transport=unavailable)
    worker.join(2)
    assert not reranking._warmup_lock.locked()
    output, info = guarded_order("恢复", passages(), settings, transport=unavailable)
    assert output == passages() and info["status"] == "FALLBACK"
    assert info["runtime_ms"] >= 0
