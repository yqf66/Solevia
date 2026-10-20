"""Offline-only ranking policies. Not connected to production routing.

No labels enter these functions. Source restrictions must come from an explicit
request contract, never from a model's relevance score or guessed intent.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass

POLICY = "bge-controlled-v1"
RRF_WEIGHTS = (0.60, 0.25, 0.15)  # BGE, lexical, original order; fixed before evaluation
RRF_K = 20


@dataclass(frozen=True)
class EvidenceInput:
    passage_id: str
    text: str
    source_id: str
    title: str = ""
    section: str = ""
    revision: str = ""
    source_kind: str = "UNKNOWN"
    entity: str = ""


def explicit_scope(candidates: list[EvidenceInput], contract: dict | None) -> tuple[list[int], dict]:
    all_ids = list(range(len(candidates)))
    if not contract:
        return all_ids, {"status": "UNRESTRICTED", "excluded": []}
    allowed = {"source_ids", "source_kinds", "entities", "revisions"}
    # Ambiguous/unconfirmed contracts fail open, with an explicit audit state.
    if contract.get("confirmed") is not True or set(contract) - (allowed | {"confirmed"}):
        return all_ids, {"status": "UNRESOLVED_CONTRACT", "excluded": []}
    filters = {key: value for key, value in contract.items() if key in allowed}
    if not filters or any(not isinstance(value, list) or not value
                          or any(not isinstance(v, str) or not v for v in value)
                          for value in filters.values()):
        return all_ids, {"status": "UNRESOLVED_CONTRACT", "excluded": []}
    keys = {"source_ids": "source_id", "source_kinds": "source_kind",
            "entities": "entity", "revisions": "revision"}
    selected, unknown, excluded = [], [], []
    for i, candidate in enumerate(candidates):
        values = [(getattr(candidate, keys[key]), permitted) for key, permitted in filters.items()]
        if any(value and value != "UNKNOWN" and value not in permitted for value, permitted in values):
            excluded.append(i)
        elif any(not value or value == "UNKNOWN" for value, _ in values):
            unknown.append(i)
        else:
            selected.append(i)
    # Unknown provenance isn't silently admitted as authorized evidence. Until
    # resolved, retain the baseline set and mark that scope was not enforceable.
    if unknown:
        return all_ids, {"status": "UNRESOLVED_METADATA", "unknown": unknown, "excluded": []}
    return selected, {"status": "APPLIED" if selected else "NO_AUTHORIZED_EVIDENCE",
                      "excluded": excluded}


def table_sentences(text: str) -> list[str]:
    """Conservative rectangular pipe-table expansion, never fills missing cells."""
    rows = [re.sub(r"[。.]$", "", row.strip()).strip("|").strip()
            for row in re.split(r"[；;\n]", text) if row.strip()]
    if len(rows) < 2 or any("|" not in row for row in rows):
        return []
    cells = [[c.strip() for c in row.split("|")] for row in rows]
    cells = [row for row in cells if not all(re.fullmatch(r":?-{2,}:?", c) for c in row)]
    if len(cells) < 2 or len(cells[0]) < 2 or len(cells[0]) > 12:
        return []
    if any(len(row) != len(cells[0]) or any(not c for c in row) for row in cells):
        return []
    if len(set(cells[0])) != len(cells[0]) or len({r[0] for r in cells[1:]}) != len(cells)-1:
        return []
    return [f"{cells[0][j]}：{row[0]}为{row[j]}。" for row in cells[1:] for j in range(1, len(row))]


def scoring_views(candidate: EvidenceInput, *, target_chars: int = 300) -> tuple[list[str], dict]:
    text = candidate.text
    if not text:
        raise ValueError("empty evidence")
    if any(len(v) > 160 for v in (candidate.title, candidate.section, candidate.revision)):
        raise ValueError("metadata exceeds bounded scoring contract")
    prefix = "\n".join(f"{key}: {value}" for key, value in (
        ("Title", candidate.title), ("Section", candidate.section), ("Revision", candidate.revision)
    ) if value)
    prefix = prefix + "\n" if prefix else ""
    # All raw characters, including whitespace, remain in contiguous core spans.
    boundaries = [0, *(m.end() for m in re.finditer(r"[。！？!?；;\n]|[.](?=\s+[A-Z])", text)), len(text)]
    boundaries = list(dict.fromkeys(boundaries))
    atoms = [(a, b) for a, b in zip(boundaries, boundaries[1:], strict=False) if b > a]
    spans, start, end = [], 0, 0
    for a, b in atoms:
        if end > start and b-start > target_chars:
            spans.append((start, end))
            start = a
        end = b
    if end > start:
        spans.append((start, end))
    views = [prefix + text[a:b] for a, b in spans]
    extras = []
    # Score complete distinct sentences as well; this exposes answers buried in
    # repetitive logs without selecting sentences using the gold answer/query.
    seen = set(views)
    for a, b in atoms:
        sentence = text[a:b].strip()
        view = prefix + sentence
        if sentence and view not in seen:
            seen.add(view)
            views.append(view)
            extras.append({"kind": "sentence", "start": a, "end": b})
    for sentence in table_sentences(text):
        view = prefix + sentence
        if view not in seen:
            seen.add(view)
            views.append(view)
            extras.append({"kind": "rectangular_table_cell", "derived_text": sentence})
    if len(views) > 64:
        raise ValueError("semantic view budget exceeded; must use baseline")
    complete = "".join(text[a:b] for a, b in spans) == text
    if not complete:
        raise ValueError("raw character coverage mismatch")
    return views, {"passage_id": candidate.passage_id, "source_id": candidate.source_id,
                   "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
                   "raw_chars": len(text), "core_spans": spans,
                   "supplemental_views": extras, "raw_coverage_complete": complete,
                   "view_count": len(views)}


def terms(text: str) -> Counter:
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_.:/+-]*", text.casefold())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(run[i:i+2] for i in range(max(1, len(run)-1)))
    return Counter(words)


def lexical_scores(query: str, candidates: list[EvidenceInput]) -> list[float]:
    query_terms = terms(query)
    documents = [terms(c.text + "\n" + c.title + "\n" + c.section) for c in candidates]
    n = len(documents)
    frequency = Counter(term for document in documents for term in document)
    return [sum(math.log(1+(n-frequency[t]+0.5)/(frequency[t]+0.5))
                * min(2, document[t]) for t in query_terms if document[t]) for document in documents]


def rank_candidates(query: str, candidates: list[EvidenceInput], scores: list[float],
                    selected: list[int], *, hybrid: bool) -> tuple[list[int], dict]:
    if len(scores) != len(candidates) or any(not math.isfinite(s) for s in scores):
        raise ValueError("incomplete or nonfinite score array")
    if len(set(selected)) != len(selected) or any(i < 0 or i >= len(candidates) for i in selected):
        raise ValueError("invalid selection")
    bge = sorted(selected, key=lambda i: (-scores[i], i))
    if not hybrid:
        return bge, {"method": "BGE", "scores": scores}
    lexical = lexical_scores(query, candidates)
    orders = [bge, sorted(selected, key=lambda i: (-lexical[i], i)), list(selected)]
    # Different logit scales cannot be mixed as calibrated probabilities. Use
    # fixed reciprocal rank fusion; evaluate rather than assert improvement.
    combined = {i: 0.0 for i in selected}
    for weight, order in zip(RRF_WEIGHTS, orders, strict=True):
        for rank, i in enumerate(order, 1):
            combined[i] += weight/(RRF_K+rank)
    return sorted(selected, key=lambda i: (-combined[i], i)), {
        "method": "FIXED_RRF", "weights": RRF_WEIGHTS, "k": RRF_K,
        "bge_scores": scores, "lexical_scores": lexical,
        "fused_scores": combined, "baseline_is_retrieval_score": False,
    }
