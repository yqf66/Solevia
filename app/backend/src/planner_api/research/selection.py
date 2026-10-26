"""Address-only selection, with all quotations and context copied by code."""
from __future__ import annotations

import hashlib
import re

from .schemas import EvidencePassage, EvidenceSpanChoices, ResearchPacket, SelectedEvidence


def _archive_locator(passage: EvidencePassage, locator: dict) -> dict:
    locator = dict(locator)
    locator.pop("archive_range", None)
    archive = passage.trust_metadata.get("evidence_archive", {})
    origin = archive.get("excerpt_window", archive.get("reader_window", {})) if isinstance(archive, dict) else {}
    if (isinstance(origin, dict) and type(origin.get("char_start")) is int
            and archive.get("evidence_basis") == "section_markdown"):
        base = origin["char_start"]
        locator["archive_range"] = {
            "artifact": archive["evidence_basis"],
            "artifact_sha256": archive.get("artifacts", {}).get("section_markdown", {}).get("sha256"),
            **{k: base + locator[k] for k in ("char_start", "char_end", "context_start", "context_end")},
        }
        locator["contains_fragment"] |= bool(
            (locator["context_start"] == 0 and origin.get("partial_start")) or
            (locator["context_end"] == len(passage.text) and origin.get("partial_end")))
        locator["context_before_truncated"] |= base + locator["context_start"] > 0
        locator["context_after_truncated"] |= bool(
            locator["context_end"] == len(passage.text) and passage.trust_metadata.get("excerpt_truncated"))
    return locator

def evidence_spans(passage: EvidencePassage) -> list[dict]:
    """Prefer Markdown paragraphs; split oversized paragraphs at sentence ends.

    Every character belongs to exactly one addressable span. Unavoidably split
    long sentences are labelled fragments, rather than silently made complete.
    """
    text = passage.text
    ends = [m.end() for m in re.finditer(r"\n\s*\n|[.!?。！？](?=\s|$)", text)]
    spans = []
    start = 0
    while start < len(text):
        limit = min(start + 1000, len(text))
        if limit == len(text):
            end = limit
        else:
            boundaries = [end for end in ends if start + 200 <= end <= limit]
            end = max(boundaries) if boundaries else limit
        spans.append({"span_id": f"{passage.passage_id}:{start}:{end}",
                      "char_start": start, "char_end": end, "text": text[start:end],
                      "fragment": (start > 0 and start not in ends)
                                  or (end < len(text) and end not in ends)})
        start = end
    return spans


def resolve_span_choices(packet: ResearchPacket, passage: EvidencePassage,
                         choices: EvidenceSpanChoices) -> tuple[list[SelectedEvidence], list[str]]:
    """Reject unknown addresses/aspects; preserve neighbouring spans verbatim.

    Adjacent context windows merge to avoid duplicate evidence. Copy fidelity
    establishes provenance only; it never establishes entailment or coverage.
    """
    spans = evidence_spans(passage)
    by_id = {span["span_id"]: index for index, span in enumerate(spans)}
    allowed = set(packet.brief.required_aspects if packet.brief else [])
    if packet.decision:
        allowed.update(a for stage in packet.decision.research_stages for a in stage.required_aspects)
    selected = {}
    diagnostics = []
    for choice in choices.selections:
        if choice.span_id not in by_id or any(a not in allowed for a in choice.covers_aspects):
            diagnostics.append("原文选择返回了未知片段或必答项，已拒绝该选择。")
            continue
        index = by_id[choice.span_id]
        selected[index] = list(dict.fromkeys([*selected.get(index, []), *choice.covers_aspects]))
    groups = []
    for index in sorted(selected):
        left, right = max(0, index - 1), min(len(spans) - 1, index + 1)
        if (groups and left <= groups[-1][1] + 1
                and spans[max(right, groups[-1][1])]["char_end"] - spans[groups[-1][0]]["char_start"] <= 3200):
            groups[-1][1] = max(right, groups[-1][1])
            groups[-1][2].append(index)
        else:
            groups.append([left, right, [index]])
    result = []
    for left, right, indexes in groups:
        quote_start, quote_end = spans[min(indexes)]["char_start"], spans[max(indexes)]["char_end"]
        window_start, window_end = spans[left]["char_start"], spans[right]["char_end"]
        aspects = list(dict.fromkeys(a for i in indexes for a in selected[i]))
        locator = {**passage.locator, "char_start": quote_start, "char_end": quote_end,
                   "context_start": window_start, "context_end": window_end,
                   "selected_ranges": [[spans[i]["char_start"], spans[i]["char_end"]] for i in indexes],
                   "passage_sha256": hashlib.sha256(passage.text.encode()).hexdigest(),
                   "contains_fragment": any(spans[i]["fragment"] for i in range(left, right + 1)),
                   "context_before_truncated": window_start > 0,
                   "context_after_truncated": window_end < len(passage.text)}
        locator = _archive_locator(passage, locator)
        identity = f"{passage.passage_id}:{quote_start}:{quote_end}"
        result.append(SelectedEvidence(
            evidence_id="evidence:" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            passage_id=passage.passage_id, source_url_or_uri=passage.source_url_or_uri,
            quote=passage.text[quote_start:quote_end],
            context_before=passage.text[window_start:quote_start],
            context_after=passage.text[quote_end:window_end],
            covers_aspects=aspects,
            question_keys=[q.key for q in packet.subqueries if set(q.covers_aspects).intersection(aspects)],
            locator=locator, trust_metadata=dict(passage.trust_metadata),
        ))
    return result, list(dict.fromkeys(diagnostics))


def validated_selected_evidence(packet: ResearchPacket) -> list[SelectedEvidence]:
    """Recheck restored checkpoints against their stored original passages."""
    passages = {p.passage_id: p for p in packet.passages}
    valid = []
    for evidence in packet.selected_evidence:
        passage = passages.get(evidence.passage_id)
        if passage is None or evidence.source_url_or_uri != passage.source_url_or_uri:
            continue
        loc = evidence.locator
        start, end = loc.get("char_start"), loc.get("char_end")
        before, after = loc.get("context_start"), loc.get("context_end")
        if not all(type(n) is int for n in (start, end, before, after)):
            continue
        if not (0 <= before <= start < end <= after <= len(passage.text)):
            continue
        if loc.get("passage_sha256") != hashlib.sha256(passage.text.encode()).hexdigest():
            continue
        if (evidence.quote != passage.text[start:end]
                or evidence.context_before != passage.text[before:start]
                or evidence.context_after != passage.text[end:after]):
            continue
        # Trust comes from the actual passage, not serialized model fields.
        valid.append(evidence.model_copy(update={"trust_metadata": dict(passage.trust_metadata),
                                                "locator": _archive_locator(passage, loc)}))
    return valid
