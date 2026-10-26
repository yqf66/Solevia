"""Retrieve separate verbatim prose windows from an already verified archive."""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from pathlib import Path
import re

from planner_api.evidence_windows import ExcerptWindow, bounded_window

from .ranking import tokenize

ROOT = Path(__file__).resolve().parents[4]
ARCHIVES = ROOT / ".dev-logs" / "web-page-originals"
API_HEADING = re.compile(r"^\s*(?:class\s+)?(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\(")

# Reading clues, not factual rules. Existing search intents often mix Chinese
# questions with English documentation. Ranking never certifies an answer.
CLUES = {
    "等待": "wait waiting awaited", "添加": "add added", "收尾": "shutting teardown",
    "异常后": "fails failed", "报错": "fails failed", "取消": "cancelled cancel",
    "异常": "exception exceptions", "语句体": "body", "并行": "concurrently concurrency",
    "并发": "concurrently concurrency", "读": "reader readers read", "写": "writer writers write",
    "长读": "long read transaction checkpoint checkpointer", "权限": "permission permissions inaccessible",
    "版本": "version changed", "不存在": "missing", "系统错误": "oserror",
}


def _terms(query):
    terms = {t for t in tokenize(query) if not t.isascii() or (len(t) >= 3 and t[0].isalpha())}
    for clue, aliases in CLUES.items():
        if clue in query:
            terms.update(aliases.split())
    terms -= {"the", "and", "for", "with", "python", "official", "documentation"}
    return terms


def source_queries(packet, source):
    related = [q for q in packet.subqueries if q.query in source.matched_queries]
    queries = [" ".join([q.query_intent or q.query, q.query, *q.required_terms]) for q in related]
    # A degraded planner can have only broad conversational queries. Retain
    # the actual question's separate clauses rather than assuming terms exist.
    if len(related) < 2 or not any(q.required_terms for q in related):
        clauses = re.split(r"[、，。；？!?\n]|\s+和", re.sub(r"https?://\S+", " ", packet.question))
        entities = set(re.findall(r"[a-z][a-z0-9_]{2,}", " ".join(
            entity for q in related for entity in q.anchor_entities).casefold()))
        # Conversational Chinese clauses should not displace technical clues
        # when the available document/query anchors are in English.
        useful = [q for q in clauses if {t for t in _terms(q) if t.isascii()} - entities
                  or re.search(r"\w+_\w+", q)]
        queries = sorted(useful, key=lambda q: -len({t for t in _terms(q) if t.isascii()})) if useful else clauses
    return list(dict.fromkeys(q for q in queries if q.strip()))[:8]


def prose_windows(content, queries, max_chars, *, ranker=None):
    """Round-robin query candidates within one shared, unchanged char budget.

    Code and link-reference lists stay archived; they do not crowd out prose.
    Distant ranges remain separate. Never join them into a single quotation.
    """
    if len(content) <= max_chars:
        return [ExcerptWindow(content, 0, len(content))]
    blocks, scopes, fenced, start, api = [], [], False, 0, None
    for line in content.splitlines(keepends=True):
        end = start + len(line)
        label = API_HEADING.match(line) if not fenced else None
        if label:
            api = label.group(1).casefold()
        elif line.startswith("#"):
            api = None
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif (not fenced and line.strip() and not re.match(r"\s*(?:\[\d+\]:|#)", line)
              and (len(re.findall(r"[a-z]+", line.casefold())) >= 8
                   or len(re.findall(r"[\u4e00-\u9fff]", line)) >= 16)):
            if len(line) <= (min(1200, max_chars) if ranker is not None else max_chars):
                blocks.append(ExcerptWindow(line, start, end))
            elif ranker is None:
                window = bounded_window(content, start, min(1200, max_chars))
                blocks.append(window)
            else:
                # Scan the entire oversized paragraph, including its tail.
                # Sentence boundaries and fragment labels retain exact offsets.
                from .passage_retrieval import split_long_block

                pieces = split_long_block(line, start, min(1200, max_chars))
                blocks.extend(pieces)
                scopes.extend([api] * (len(pieces) - 1))
            scopes.append(api)
        start = end
    if not blocks:
        return [bounded_window(content, 0, max_chars)]
    words = [tokenize(w.text) for w in blocks]
    df = Counter(word for group in words for word in group)
    term_sets = []
    for query in queries:
        terms = _terms(query)
        literal = tokenize(query)
        # Conditions matter more than repeated API names. These are lexical
        # reading hints only, never domain-specific assertions or coverage.
        conditions = set()
        for clue in ("异常后", "报错", "长读", "语句体", "等待"):
            if clue in query:
                conditions.update(CLUES[clue].split())
        if terms:
            tokens = re.findall(r"[a-z][a-z0-9_]{2,}", query.casefold())
            phrases = {" ".join(tokens[i:i + 2]) for i in range(len(tokens) - 1)
                       if all(t in terms for t in tokens[i:i + 2])}
            phrases |= {p.replace("single ", "one ", 1) for p in phrases if p.startswith("single ")}
            phrases |= {"one " + aliases.split()[0] for clue, aliases in CLUES.items()
                        if "一个" + clue in query}
            term_sets.append((terms, literal, conditions, phrases))
        else:
            term_sets.append((set(), literal, conditions, set()))
    if not term_sets:
        return [bounded_window(content, 0, max_chars)]
    ranks = []
    requested_apis = {m.group(1).casefold() for q in queries for m in re.finditer(
        r"\b([A-Za-z_]\w*)\s*\(", q)}
    for terms, query_words, conditions, phrases in term_sets:
        scored = []
        for i, group in enumerate(words):
            score = sum(math.log1p(len(blocks) / df[t]) * (4 if t in conditions else 3 if t in query_words else 1)
                        for t in terms & group)
            prose = " " + " ".join(re.findall(r"[a-z][a-z0-9_]{2,}", blocks[i].text.casefold())) + " "
            # Exact short relations (e.g. "one writer") must not lose to a
            # long paragraph merely repeating the individual query nouns.
            score += 6 * sum(" " + phrase + " " in prose for phrase in phrases)
            if requested_apis and scopes[i] and scopes[i] not in requested_apis:
                score *= 0.2
            if score:
                scored.append((score, i))
        ranks.append([i for _, i in sorted(scored, key=lambda x: (-x[0], x[1]))])
    if ranker is not None:
        ranks = ranker(blocks, ranks)
    selected, spent = [], 0
    # A small set of spans is enough; no new model or network call is needed.
    for depth in range(3):
        for rank in ranks:
            if depth >= len(rank):
                continue
            i = rank[depth]
            left, right = blocks[i].char_start, blocks[i].char_end
            lower = blocks[i].text.casefold()
            # Carry adjacent exception/referent prose as an indivisible window.
            if "previous paragraph" in lower and i > 0:
                left = blocks[i - 1].char_start
            if "previous paragraph" not in lower and re.search(r"treated specially|special case", lower):
                # A heavily repeated exception name can outrank the general
                # rule it qualifies. Preserve the preceding rule/result pair
                # within the same API scope instead of reading only exceptions.
                previous = i
                for j in range(i - 1, max(-1, i - 3), -1):
                    if scopes[j] != scopes[i] or blocks[previous].char_start - blocks[j].char_end > 100:
                        break
                    if not re.match(r"\s*(?:the first time\b|when\b|if\b|once\b|after\b|provided\b)",
                                    blocks[j].text.casefold()):
                        break
                    previous = j
                    left = blocks[j].char_start
            if i + 1 < len(blocks) and re.search(
                    r"treated specially|special case|\bexcept\b|\bunless\b|^thus\b|^this means\b", blocks[i + 1].text.casefold()):
                right = blocks[i + 1].char_end
            if any(left < w.char_end and right > w.char_start for w in selected):
                continue
            size = right - left
            separator = 2 if selected else 0
            if spent + separator + size > max_chars:
                continue
            first = next(w for w in blocks if w.char_start == left)
            last = blocks[i + 1] if right != blocks[i].char_end else blocks[i]
            selected.append(ExcerptWindow(content[left:right], left, right,
                                          first.partial_start, last.partial_end))
            spent += separator + size
            if len(selected) == 6:
                break
        if len(selected) == 6:
            break
    return sorted(selected, key=lambda w: w.char_start) or [bounded_window(content, 0, max_chars)]


def _heading_before(content, position):
    """Keep the original section/API label so a rule cannot silently lose scope."""
    heading, heading_start, offset, fenced = "", 0, 0, False
    for line in content[:position].splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (line.startswith("#") or API_HEADING.match(line)):
            heading, heading_start = line.rstrip("\r\n"), offset
        offset += len(line)
    return {"heading": heading[:300], "heading_char_start": heading_start}


def archived_excerpt(packet, source, document, max_chars, *, settings=None):
    """Only open reader-owned, hash-verified files; otherwise keep old fallback.

    Paths arriving from remote providers/checkpoints are not arbitrary file
    access. No archive file is altered and no rank is treated as coverage.
    """
    archive = document.evidence_archive
    if archive.get("reader") != "crawl4ai":
        return None
    try:
        artifact = archive.get("artifacts", {}).get("section_markdown", {})
        path = (ROOT / artifact["path"]).resolve()
        if not path.is_relative_to(ARCHIVES.resolve()) or path.stat().st_size > 2_000_000:
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact.get("sha256"):
            return None
        content = data.decode("utf8").replace("\r\n", "\n")
    except (AttributeError, TypeError, KeyError, OSError, UnicodeError, ValueError):
        return None
    queries = source_queries(packet, source)
    retrieval = None
    ranker = None
    if settings is not None and settings.research_passage_optimization_enabled:
        from .passage_retrieval import PassageRanker

        ranker = PassageRanker(queries, settings, len(content))
    windows = prose_windows(content, queries, max_chars, ranker=ranker)
    if ranker is not None:
        retrieval = ranker.info
    mappings, texts, offset = [], [], 0
    for window in windows:
        if texts:
            offset += 2
        texts.append(window.text)
        mappings.append({**window.locator(), **_heading_before(content, window.char_start), "stored_start": offset,
                         "stored_end": offset + len(window.text)})
        offset += len(window.text)
    if not texts:
        return None
    return "\n\n".join(texts), {**archive, "excerpt_windows": mappings,
                                **({"passage_retrieval": retrieval} if retrieval else {})}
