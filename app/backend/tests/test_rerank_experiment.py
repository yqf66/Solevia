from dataclasses import asdict

import pytest

from planner_api.materials.rerank_experiment import (
    EvidenceInput, explicit_scope, rank_candidates, scoring_views, table_sentences,
)


def item(text, **kwargs):
    return EvidenceInput("p1", text, "source-1", **kwargs)


def test_scope_only_explicit_and_confirmed_unknown_origin_not_silently_admitted():
    items = [item("广告", source_kind="ADVERTISEMENT"), item("实验", source_kind="TRIAL")]
    assert explicit_scope(items, {"confirmed": True, "source_kinds": ["TRIAL"]})[0] == [1]
    assert explicit_scope(items, {"confirmed": False, "source_kinds": ["TRIAL"]})[0] == [0, 1]
    unknown = [*items, item("不明来源", source_kind="UNKNOWN")]
    selection, status = explicit_scope(unknown, {"confirmed": True, "source_kinds": ["TRIAL"]})
    assert selection == [0, 1, 2] and status["status"] == "UNRESOLVED_METADATA"


def test_empty_scope_does_not_make_up_permitted_evidence():
    selection, info = explicit_scope([item("原文")], {"confirmed": True, "source_ids": ["missing"]})
    assert selection == [] and info["status"] == "NO_AUTHORIZED_EVIDENCE"


@pytest.mark.parametrize("contract", [{"confirmed": True, "source_ids": "source-1"},
    {"confirmed": True, "source_ids": []}, {"confirmed": True, "interpretation": "只有这个"}])
def test_invalid_contract_is_unresolved_not_guessed(contract):
    assert explicit_scope([item("原文")], contract)[1]["status"] == "UNRESOLVED_CONTRACT"


@pytest.mark.parametrize("text", ["重复记录。"*300+"最后必须保留这句。", "log ready. "*180+"Final approved bundle is BC-9.",
    "第一个条件：\n  第二个条件；例外不得合并。\n\n末尾证据\n", "完全没有句子边界"*700],
    ids=["zh_tail", "en_tail", "whitespace", "no_boundary"])
def test_scoring_view_core_spans_reconstruct_entire_original_and_never_edit_citation(text):
    source = item(text, title="原始标题", section="第2节", revision="r7")
    original = asdict(source)
    views, coverage = scoring_views(source)
    assert "".join(text[a:b] for a, b in coverage["core_spans"]) == text
    assert coverage["raw_coverage_complete"] is True
    assert coverage["view_count"] == len(views)
    assert asdict(source) == original


def test_unique_tail_sentence_available_independently_of_repetitive_noise():
    source = item("Sensor stable. "*80+"The approved calibration is CAL-827.")
    views, coverage = scoring_views(source)
    assert "The approved calibration is CAL-827." in views
    assert len(views) < 64 and coverage["raw_coverage_complete"]


def test_broken_or_duplicate_header_table_never_fabricates_cells():
    assert table_sentences("模式 | 快 | 慢；限额 | 4") == []
    assert table_sentences("模式 | 快 | 快；限额 | 4 | 8") == []
    assert table_sentences("普通说明，不是表格") == []
    assert table_sentences("模式 | 快 | 慢；限额 | 4 | 8") == ["快：限额为4。", "慢：限额为8。"]
    markdown = "| 档位 | 基础 | 增强 |\n| --- | --- | --- |\n| 限额 | 3 | 12 |"
    assert table_sentences(markdown) == ["基础：限额为3。", "增强：限额为12。"]


def test_semantic_budget_exceeded_refuses_instead_of_losing_sentences():
    with pytest.raises(ValueError, match="budget"):
        scoring_views(item("".join(f"独立句子{i}。" for i in range(100))))


def test_fusion_is_permutation_and_never_recovers_excluded_source():
    items = [item("alpha exact_id"), item("beta exact_id"), item("gamma")]
    order, strategy = rank_candidates("exact_id", items, [100, 1, 2], [1, 2], hybrid=True)
    assert set(order) == {1, 2} and 0 not in order
    assert strategy["baseline_is_retrieval_score"] is False
    with pytest.raises(ValueError):
        rank_candidates("query", items, [float("nan"), 1, 2], [0, 1, 2], hybrid=True)
