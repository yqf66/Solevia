"""Keep bounded verbatim evidence near the user's question, not only the page header."""

from planner_api.evidence_windows import ExcerptWindow
from planner_api.evidence_windows import focused_excerpt as focused_excerpt
from planner_api.evidence_windows import focused_excerpt_window as focused_excerpt_window


def source_read_focus(packet, source) -> str:
    """Use existing subqueries to locate prose before the reader budget is spent."""
    if source.source_category != "DOCUMENTATION":
        return ""
    related = [q for q in packet.subqueries if q.query in source.matched_queries]
    terms = list(dict.fromkeys(term for q in related for term in q.required_terms))
    # Keep API names in the human question for section selection, and use the
    # query's existing terms (not generated facts) to locate long-page rules.
    return (packet.question[:250] + " " + " ".join(terms)[:249]).strip()


def archive_for_excerpt(archive: dict, window: ExcerptWindow, content_length: int) -> dict:
    archive = dict(archive)
    reader = archive.get("reader_window")
    if isinstance(reader, dict) and archive.get("evidence_basis") == "section_markdown":
        base = reader.get("char_start")
        if type(base) is int:
            archive["excerpt_window"] = {
                **window.locator(), "char_start": base + window.char_start,
                "char_end": base + window.char_end,
                "partial_start": window.partial_start or (window.char_start == 0 and reader.get("partial_start", False)),
                "partial_end": window.partial_end or (window.char_end == content_length and reader.get("partial_end", False)),
            }
    return archive

