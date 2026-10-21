from planner_api.research.excerpts import focused_excerpt
from planner_api.research.excerpts import focused_excerpt_window
from planner_api.evidence_windows import bounded_window


def test_focused_excerpt_keeps_tail_rules_instead_of_table_of_contents():
    header = "Table of contents and navigation. " * 200
    rule = "TaskGroup cancellation: first exception cancels other tasks. " * 30
    result = focused_excerpt(header + rule, "Python TaskGroup 的异常取消规则", 1200)
    assert 'first exception cancels other tasks' in result
    assert len(result) <= 1200
    assert result in header + rule


def test_unmatched_or_short_content_preserves_verbatim_prefix():
    assert focused_excerpt('正文', 'TaskGroup', 1200) == '正文'
    assert focused_excerpt('intro ' * 500, 'unknown', 1000) == ('intro ' * 500)[:1000]


def test_large_budget_never_splices_distant_passages_into_one_quote():
    content = 'TaskGroup exception ' * 80 + 'unrelated ' * 700 + 'TaskGroup cancel ' * 100
    result = focused_excerpt(content, 'TaskGroup exception cancel', 3000)
    assert len(result) == 3000
    assert result in content


def test_documentation_heading_preserves_early_condition_and_later_exception():
    content = "Navigation\n" * 300 + "Task Groups¶\n\n" + "First exception cancels siblings.\n" + "Example code\n" * 90 + "ExceptionGroup and external cancellation.\n" * 20
    excerpt = focused_excerpt(content, "TaskGroup exception cancellation", 1800)
    assert excerpt.startswith("Task Groups")
    assert "First exception cancels siblings" in excerpt
    assert "ExceptionGroup and external cancellation" in excerpt


def test_heading_in_table_of_contents_does_not_hide_the_actual_section():
    toc = "Task groups\nSleeping\nShielding from cancellation\n" + "Intro\n" * 300
    section = "Task groups¶\n\nTaskGroup: first exception cancels siblings.\n" + "Example\n" * 60
    content = toc + section
    assert focused_excerpt(content, "TaskGroup exception cancellation", 800).startswith("Task groups¶")


def test_dense_window_never_starts_inside_exception_condition():
    prefix = "Navigation. " * 150
    rule = "Once all tasks have finished, if any tasks have failed with an exception other than CancelledError, those exceptions are combined in an ExceptionGroup.\n\n"
    content = prefix + rule * 10
    window = focused_excerpt_window(content, "TaskGroup exception ExceptionGroup CancelledError", 1000)
    assert len(window.text) <= 1000 and content[window.char_start:window.char_end] == window.text
    assert not window.partial_start and not window.partial_end
    assert window.text.replace("Navigation. ", "").startswith("Once all tasks")


def test_markdown_footnotes_do_not_hide_api_heading():
    content = "Navigation\n\n" * 200 + "### Task Groups[¶][4]\n\nFirst exception cancels siblings.\n\n" + "Example\n\n" * 100
    assert focused_excerpt(content, "TaskGroup exception", 800).startswith("### Task Groups")


def test_unavoidably_long_sentence_is_marked_incomplete_without_budget_relaxation():
    window = bounded_window("unbrokenword" * 1000, 500, 1000)
    assert window.partial_start and window.partial_end and len(window.text) == 1000


def test_reader_prefix_ends_at_complete_sentence_before_hard_limit():
    content = "Complete rule.\n\n" + "Only when " + "condition " * 200 + "is met."
    window = bounded_window(content, 0, 1000)
    assert window.text == "Complete rule.\n\n" and not window.partial_end


def test_requested_section_wins_over_keyword_dense_introduction_with_same_budget():
    intro = "# 1. Overview\n\n" + "WAL read write concurrency. " * 200 + "\n\n"
    rules = "## 2.2. Concurrency\n\nOnly one writer at a time.\n\n" + "Details.\n\n" * 200
    content = intro + rules
    window = focused_excerpt_window(content, "WAL read write concurrency single writer", 1000)
    assert window.text.startswith("## 2.2. Concurrency")
    assert "Only one writer at a time" in window.text
    assert len(window.text) <= 1000 and content[window.char_start:window.char_end] == window.text
