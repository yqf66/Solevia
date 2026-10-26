"""Bounded original-passage recall. Ranks are relevance, never factual support."""
from __future__ import annotations

from functools import lru_cache
import re
import time
from types import SimpleNamespace

from planner_api.evidence_windows import ExcerptWindow

from .reranking import rerank_candidates


def split_long_block(text, offset, width):
    boundaries = {0, len(text)} | {m.end() for m in re.finditer(r"[.!?](?:\s+|$)|[。！？]\s*", text)}
    pieces, start = [], 0
    while start < len(text):
        limit = min(start + width, len(text))
        ends = [n for n in boundaries if start + width // 2 <= n <= limit]
        end = max(ends) if ends else limit
        pieces.append(ExcerptWindow(text[start:end], offset + start, offset + end,
                                    start not in boundaries, end not in boundaries))
        start = end
    return pieces


@lru_cache(maxsize=4)
def _vectors(texts, model, cache_dir, revision):
    # Bounded process cache only; no public knowledge base, database or downloads.
    from planner_api.materials.semantic import encoder

    return encoder(model, cache_dir, revision).encode(list(texts))


def _fuse(*ranks):
    scores = {}
    for rank in ranks:
        for position, index in enumerate(rank):
            scores[index] = scores.get(index, 0) + 1 / (60 + position + 1)
    return sorted(scores, key=lambda i: (-scores[i], i))


class PassageRanker:
    def __init__(self, queries, settings, original_chars):
        self.queries, self.settings, self.original_chars = queries, settings, original_chars
        self.info = {"policy": "original-passages-v1", "embedding": "NOT_NEEDED",
                     "bge": "NOT_NEEDED" if settings.research_rerank_mode == "BGE" else "OFF",
                     "runtime_ms": 0.0}

    def __call__(self, blocks, lexical):
        began = time.perf_counter()
        ranks = [list(rank) for rank in lexical]
        self.info["chunk_count"] = len(blocks)
        if self.settings.research_rerank_mode != "BGE":
            self.info["embedding"] = "OFF"
            self.info["runtime_ms"] = round((time.perf_counter() - began) * 1000, 3)
            return ranks
        # E5 recalls paraphrases in long documents. The existing optional offline
        # encoder is reused; oversized pages keep complete lexical scanning.
        if not self.settings.research_passage_semantic_enabled:
            self.info["embedding"] = "OFF"
        if self.original_chars >= 16000 and self.settings.research_passage_semantic_enabled:
            self.info["embedding"] = "SKIPPED_BUDGET"
            if len(blocks) <= 160 and sum(len(b.text) for b in blocks) <= 160000:
                try:
                    from planner_api.materials.semantic import cosine, encoder

                    args = (self.settings.material_embedding_model,
                            self.settings.material_embedding_cache_dir,
                            self.settings.material_embedding_revision)
                    vectors = _vectors(tuple(b.text for b in blocks), *args)
                    queries = encoder(*args).encode(self.queries, query=True)
                    if len(vectors) != len(blocks) or len(queries) != len(self.queries):
                        raise ValueError("Incomplete embedding batch")
                    semantic = [sorted(range(len(blocks)), key=lambda i: (-cosine(q, vectors[i]), i))
                                for q in queries]
                    ranks = [_fuse(lexical[i] if i < len(lexical) else [], rank)
                             for i, rank in enumerate(semantic)]
                    self.info["embedding"] = "APPLIED"
                except Exception as error:
                    self.info.update(embedding="FALLBACK", embedding_error=type(error).__name__)
        # Fair recall slots per subquestion, one bounded BGE request per page.
        pool = []
        for depth in range(24):
            for rank in ranks:
                if depth < len(rank) and rank[depth] not in pool:
                    pool.append(rank[depth])
                    if len(pool) == 24:
                        break
            if len(pool) == 24:
                break
        items = [SimpleNamespace(hit=SimpleNamespace(title="", snippet=blocks[i].text),
                                 score=len(pool) - n, quality_score=0, reasons=[], index=i)
                 for n, i in enumerate(pool)]
        query = "\n".join(q[:max(1, 990 // max(1, len(self.queries)))] for q in self.queries)
        ordered, info = rerank_candidates(query, items, self.settings)
        self.info.update(bge=info["status"], bge_runtime_ms=info["runtime_ms"],
                         candidate_count=len(pool))
        if info["status"] == "APPLIED":
            semantic = [item.index for item in ordered]
            fused = [_fuse(rank[:24], [i for i in semantic if i in rank[:24]]) for rank in ranks]
            # An existing lexical best match keeps its slot, protecting exact
            # method names/conditions from a semantic false positive.
            ranks = [[*lexical[n][:1], *[i for i in rank if i not in lexical[n][:1]]]
                     if n < len(lexical) else rank for n, rank in enumerate(fused)]
        self.info["runtime_ms"] = round((time.perf_counter() - began) * 1000, 3)
        return ranks
