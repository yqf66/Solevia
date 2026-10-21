"""Reader fidelity and original-archive propagation, without a remote model."""
import hashlib
import importlib.util
from pathlib import Path
import socket
import uuid

import pytest

from planner_api.research.evidence import build_web_passages
from planner_api.research.schemas import PageDocument, ResearchPacket, ResearchSource

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("crawl4ai_reader", ROOT / "services/web-research/src/crawl4ai-reader.py")
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def test_original_evidence_retains_deleted_rule_and_current_rule_separately(monkeypatch):
    cache = ROOT / ".dev-logs" / "reader-unit-tests" / uuid.uuid4().hex
    cache.mkdir(parents=True)
    monkeypatch.setenv("CRAWL4_AI_BASE_DIRECTORY", str(cache))
    html = "<html><h1>Rules</h1><p><s>The old rule prohibits large transactions.</s> Since 3.11 the new rule applies.</p></html>"
    result = reader.make_document(html, "https://sqlite.org/wal.html", "https://sqlite.org/wal.html",
                                  "Rules", 1000, "", archive_root=cache)
    assert "~~The old rule prohibits large transactions.~~" in result["content"]
    assert "Since 3.11 the new rule applies." in result["content"]
    document = PageDocument.model_validate(result)
    archive = document.evidence_archive
    assert archive["evidence_basis"] == "section_markdown"
    for kind, artifact in archive["artifacts"].items():
        data = (ROOT / artifact["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
        if kind == "html":
            assert data.decode() == html
    packet = ResearchPacket(question="Check the rule", sources=[ResearchSource(
        url=document.final_url, canonical_url=str(document.final_url), read_status="SUCCEEDED",
        content_excerpt=document.content, evidence_archive=document.evidence_archive)])
    passage = build_web_passages(packet)[0]
    assert passage.trust_metadata["evidence_archive"] == archive
    assert "~~The old rule" in passage.text


def test_api_focus_retains_conditions_and_exceptions_but_archives_whole_page():
    html = """<html><h1>Documentation</h1><p>unrelated introduction</p>
      <section><h2>Task Groups</h2><p>While waiting, tasks can be added.</p>
      <p>After a non-cancellation failure, no further tasks can be added.</p>
      <p>KeyboardInterrupt is re-raised instead of a group.</p></section>
      <section><h2>Other API</h2><p>unrelated content</p></section></html>"""
    selected, focused = reader.focused_html(html, "https://docs.python.org/3.14/library/asyncio-task.html",
                                           "TaskGroup中正常等待和异常处理")
    assert focused
    assert "While waiting" in selected and "no further tasks" in selected and "KeyboardInterrupt" in selected
    assert "unrelated" not in selected
    assert "unrelated" in html


def test_explicit_anchor_selects_api_definition_with_its_version_note():
    html = "<dl><dt id='pathlib.Path.exists'>Path.exists()</dt><dd>Returns False for inaccessible files. Changed in version 3.14.</dd></dl>"
    selected, focused = reader.focused_html(html, "https://docs.python.org/3.14/library/pathlib.html#pathlib.Path.exists", "")
    assert focused and "Changed in version 3.14" in selected


def test_api_anchor_keeps_shared_version_and_exception_notes():
    html = """<section><h2>Querying file status</h2>
      <p>Path.exists and Path.is_file share the following rule.</p>
      <p>Changed in version 3.14: all operating system errors return False.</p>
      <dl><dt id='pathlib.Path.stat'>Path.stat</dt><dd>Unrelated definition.</dd></dl>
      <dl><dt id='pathlib.Path.exists'>Path.exists</dt><dd>Return False if inaccessible.</dd></dl>
      </section>"""
    selected, focused = reader.focused_html(html, "https://docs.python.org/3.14/library/pathlib.html#pathlib.Path.exists", "")
    assert focused and "Changed in version 3.14" in selected
    assert "share the following rule" in selected and "if inaccessible" in selected
    assert "Unrelated definition" not in selected


def test_navigation_does_not_consume_evidence_budget_or_remove_rule_notes():
    html = "<html><nav>" + "menu link " * 500 + "</nav><main><h1>Rules</h1>" \
           "<p>Only one writer at a time.</p><aside>Exception: this note is evidence.</aside>" \
           "<p><s>Old rule.</s> New rule.</p></main></html>"
    selected = reader.body_html(html, "https://example.com/rules")
    assert "menu link" not in selected
    assert "Only one writer" in selected and "Exception: this note" in selected
    assert "<s>Old rule.</s> New rule." in selected
    assert "menu link" in html


def test_link_addresses_are_archived_as_footnotes_without_crowding_out_rule():
    archive_root = ROOT / ".dev-logs" / "reader-unit-tests" / uuid.uuid4().hex
    address = "https://example.com/" + "long-address-" * 100
    html = f'<main><p>See <a href="{address}">the source</a>.</p><p>Only one writer at a time.</p></main>'
    result = reader.make_document(html, "https://example.com/", "https://example.com/",
                                  "Rules", 1000, "", archive_root=archive_root)
    assert "Only one writer at a time." in result["content"]
    original = (ROOT / result["evidenceArchive"]["artifacts"]["raw_markdown"]["path"]).read_text(encoding="utf8")
    assert address in original and "the source" in original


def test_changed_markdown_conversion_cannot_overwrite_previous_archive(monkeypatch):
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    archive_root = ROOT / ".dev-logs" / "reader-unit-tests" / uuid.uuid4().hex
    html = "<main><p>Preserve the original rule.</p></main>"
    first = reader.make_document(html, "https://example.com/", "https://example.com/",
                                 "Rules", 1000, "", archive_root=archive_root)
    artifact = first["evidenceArchive"]["artifacts"]["raw_markdown"]
    original_bytes = (ROOT / artifact["path"]).read_bytes()
    generate = DefaultMarkdownGenerator.generate_markdown

    def changed_conversion(self, *args, **kwargs):
        result = generate(self, *args, **kwargs)
        result.raw_markdown += "\nChanged converter formatting.\n"
        return result

    monkeypatch.setattr(DefaultMarkdownGenerator, "generate_markdown", changed_conversion)
    second = reader.make_document(html, "https://example.com/", "https://example.com/",
                                  "Rules", 1000, "", archive_root=archive_root)
    assert first["evidenceArchive"]["archive_id"] != second["evidenceArchive"]["archive_id"]
    assert (ROOT / artifact["path"]).read_bytes() == original_bytes
    assert hashlib.sha256(original_bytes).hexdigest() == artifact["sha256"]


def test_actual_focused_markdown_is_archived_and_each_focus_has_distinct_identity():
    root = ROOT / ".dev-logs" / "reader-unit-tests" / uuid.uuid4().hex
    html = "<main><section><h2>Task Groups</h2><p>Only non-cancellation errors trigger cancellation.</p></section>" \
           "<section><h2>Other Methods</h2><p>Different rule.</p></section></main>"
    focused = reader.make_document(html, "https://example.com/", "https://example.com/", "Rules", 1000, "TaskGroup", root)
    whole = reader.make_document(html, "https://example.com/", "https://example.com/", "Rules", 1000, "", root)
    archive = focused["evidenceArchive"]
    text = (ROOT / archive["artifacts"]["section_markdown"]["path"]).read_text(encoding="utf8")
    loc = archive["reader_window"]
    assert text[loc["char_start"]:loc["char_end"]] == focused["content"]
    assert "Different rule" not in text
    assert focused["evidenceArchive"]["archive_id"] != whole["evidenceArchive"]["archive_id"]
    assert (ROOT / archive["artifacts"]["html"]["path"]).read_text(encoding="utf8") == html


@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "http://localhost/", "file:///secret", "https://" + "name:secret" + "@example.com/"])
def test_reader_rejects_private_or_credentialed_navigation(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 80))])
    with pytest.raises(ValueError):
        reader.assert_public_url(url)
