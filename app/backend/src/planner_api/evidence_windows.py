"""Bounded verbatim windows shared by Reader and research, without model imports."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExcerptWindow:
    text: str
    char_start: int
    char_end: int
    partial_start: bool = False
    partial_end: bool = False

    def locator(self) -> dict:
        return {"char_start": self.char_start, "char_end": self.char_end,
                "partial_start": self.partial_start, "partial_end": self.partial_end}


def bounded_window(content: str, start: int, max_chars: int) -> ExcerptWindow:
    """Return one verbatim window, preferring paragraphs then sentence boundaries.

    Boundary completeness is structural only: it does not certify that a rule
    has all its qualifications. Long indivisible sentences remain marked partial.
    """
    if max_chars <= 0:
        raise ValueError("Excerpt budget must be positive")
    paragraphs = {0, len(content)} | {m.end() for m in re.finditer(r"\n[ \t\r]*\n", content)}
    paragraphs |= {m.start() for m in re.finditer(r"(?m)^(?:#{1,6}[^\n]+|[^\n]{2,75}¶[^\n]*)$", content)}
    sentences = paragraphs | {m.end() for m in re.finditer(r"[.!?](?:[ \t]+|\r?\n|$)|[。！？][ \t]*", content)}
    start = max(0, min(start, len(content)))
    previous = max(n for n in paragraphs if n <= start)
    if start - previous > max_chars // 3:
        previous = max(n for n in sentences if n <= start)
    # Do not retreat an unbounded distance inside a very long sentence.
    if start - previous <= max_chars // 3:
        start = previous
    limit = min(len(content), start + max_chars)
    ends = [n for n in paragraphs if start + max_chars // 2 <= n <= limit]
    if not ends:
        ends = [n for n in sentences if start < n <= limit]
    end = max(ends) if ends else limit
    return ExcerptWindow(content[start:end], start, end,
                         start not in sentences, end not in sentences)


def focused_excerpt(content: str, question: str, max_chars: int) -> str:
    return focused_excerpt_window(content, question, max_chars).text


def focused_excerpt_window(content: str, question: str, max_chars: int) -> ExcerptWindow:
    if len(content) <= max_chars:
        return ExcerptWindow(content, 0, len(content))
    terms = {term.casefold() for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", question)}
    for clue, equivalents in {"取消": {"cancel", "cancellation"}, "异常": {"exception", "exceptions"},
                               "超时": {"timeout"}, "依赖": {"dependency", "dependencies"}}.items():
        if clue in question:
            terms.update(equivalents)
    if not terms:
        return bounded_window(content, 0, max_chars)
    # A documentation section gives more complete conditions than the densest
    # paragraph alone. Plain-text readers retain short headings and pilcrows.
    api_names = re.findall(r"\b[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+\b", question)
    headings = list(re.finditer(r"(?m)^(?:#{1,6}[^\S\n]*)?([^\n]{2,75})[^\S\n]*$", content))
    for name in api_names:
        key = re.sub(r"[^a-z0-9]", "", name.casefold())
        for heading in headings:
            label = re.sub(r"\[¶\]\[[^\]]+\]|\[[0-9]+\]", "", heading.group(1))
            title = re.sub(r"[^a-z0-9]", "", label.casefold()).rstrip("s")
            if title == key.rstrip("s"):
                if "¶" not in heading.group() and not heading.group().startswith("#"):
                    after = content[heading.end():heading.end() + 3].replace("\r", "")
                    if not after.startswith("\n\n"):
                        continue
                block = content[heading.start():heading.start() + max_chars]
                if any(marker in block.casefold() for marker in ("exception", "cancel", "异常", "取消")):
                    return bounded_window(content, heading.start(), max_chars)
    # Prefer an explicitly requested section over an introduction that merely
    # repeats many query words. Keep its original paragraphs and locators.
    for heading in re.finditer(r"(?m)^#{1,6}[ \t]+([^\n]+)$", content):
        label = re.sub(r"^[\d.]+\s*", "", heading.group(1)).strip().casefold()
        if label in terms:
            return bounded_window(content, heading.start(), max_chars)
    width = min(max_chars, 1200)
    step = max(1, width // 2)
    ranked = []
    for start in range(0, len(content), step):
        segment = content[start:start + width]
        words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", segment.casefold())
        score = sum(min(words.count(term), 3) * (2 if len(term) > 6 else 1) for term in terms)
        if score:
            ranked.append((score, start))
    if not ranked:
        return bounded_window(content, 0, max_chars)
    _, best_start = max(ranked, key=lambda item: (item[0], -item[1]))
    start = max(0, min(best_start - (max_chars - width) // 2, len(content) - max_chars))
    return bounded_window(content, start, max_chars)
