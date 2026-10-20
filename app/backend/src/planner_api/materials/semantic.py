from __future__ import annotations

import math
import logging
import threading
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.config import Settings
from planner_api.models import MaterialChunk, MaterialEmbedding

_model_lock = threading.Lock()
_load_lock = threading.Lock()


class LocalE5Encoder:
    """E5 query/passage prefixes and masked mean pooling, using offline weights only."""

    def __init__(self, model: str, cache_dir: str, revision: str) -> None:
        from transformers import AutoModel, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(
            model, revision=revision, cache_dir=cache_dir, local_files_only=True
        )
        self.model = AutoModel.from_pretrained(
            model, revision=revision, cache_dir=cache_dir, local_files_only=True
        )
        self.model.eval()

    def encode(self, values: list[str], *, query: bool = False) -> list[list[float]]:
        import torch
        import torch.nn.functional as functional

        prefix = "query: " if query else "passage: "
        results = []
        with _model_lock, torch.inference_mode():
            for offset in range(0, len(values), 8):
                inputs = self.tokenizer(
                    [prefix + item for item in values[offset : offset + 8]],
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt",
                    return_overflowing_tokens=True,
                    stride=64,
                )
                mapping = inputs.pop("overflow_to_sample_mapping")
                windows = []
                for start in range(0, len(mapping), 8):
                    batch = {key: value[start : start + 8] for key, value in inputs.items()}
                    hidden = self.model(**batch).last_hidden_state
                    mask = batch["attention_mask"].unsqueeze(-1)
                    pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
                    windows.extend(functional.normalize(pooled, p=2, dim=1).tolist())
                for sample in range(len(values[offset : offset + 8])):
                    parts = torch.tensor(
                        [
                            vector
                            for index, vector in enumerate(windows)
                            if int(mapping[index]) == sample
                        ]
                    )
                    results.append(functional.normalize(parts.mean(0), p=2, dim=0).tolist())
        return results


@lru_cache(maxsize=1)
def _cached_encoder(
    model: str, cache_dir: str, revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
) -> LocalE5Encoder:
    return LocalE5Encoder(model, cache_dir, revision)


def encoder(
    model: str, cache_dir: str, revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
) -> LocalE5Encoder:
    # functools.lru_cache permits concurrent misses to execute the loader more
    # than once. Serialize cold initialization, retain one model identity, and
    # reuse the same instance for indexing and retrieval.
    with _load_lock:
        return _cached_encoder(model, cache_dir, revision)


def prewarm_encoder(settings: Settings) -> threading.Thread | None:
    """Opt-in local warmup off the app startup/health path; never download weights."""
    if not settings.material_semantic_enabled or not settings.material_semantic_prewarm:
        return None

    def warm():
        try:
            encoder(settings.material_embedding_model, settings.material_embedding_cache_dir,
                    settings.material_embedding_revision).encode(["预热资料检索"], query=True)
            logging.getLogger(__name__).info("Local material encoder ready")
        except Exception as error:
            logging.getLogger(__name__).warning("Local material encoder warmup unavailable: %s", type(error).__name__)

    worker = threading.Thread(target=warm, name="material-encoder-warmup", daemon=True)
    worker.start()
    return worker


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    denominator = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return (
        sum(x * y for x, y in zip(left, right, strict=True)) / denominator if denominator else -1.0
    )


def encode_passages(values: list[str], settings: Settings, cancelled=None):
    if len(values) > settings.material_embedding_max_chunks:
        raise RuntimeError("资料超过语义索引预算，请拆分文档或提高配置上限")
    encode = encoder(
        settings.material_embedding_model,
        settings.material_embedding_cache_dir,
        settings.material_embedding_revision,
    )
    vectors = []
    for start in range(0, len(values), 32):
        if cancelled and cancelled():
            raise RuntimeError("语义索引已取消")
        vectors.extend(encode.encode(values[start : start + 32]))
    return vectors


def index_chunks(
    session: Session, chunks: list[MaterialChunk], settings: Settings, cancelled=None
) -> int:
    if len(chunks) > settings.material_embedding_max_chunks:
        raise RuntimeError("资料超过自动语义索引预算，请分批建立索引")
    count = 0
    for offset in range(0, len(chunks), 32):
        if cancelled and cancelled():
            raise RuntimeError("语义索引已取消")
        batch = chunks[offset : offset + 32]
        existing = {
            item.chunk_id: item
            for item in session.scalars(
                select(MaterialEmbedding).where(
                    MaterialEmbedding.chunk_id.in_([c.id for c in batch]),
                    MaterialEmbedding.model == settings.material_embedding_identity,
                )
            )
        }
        pending = [
            c
            for c in batch
            if c.id not in existing or existing[c.id].content_hash != c.content_hash
        ]
        # Finish any read transaction before inference; no write lock spans the model call.
        session.commit()
        vectors = (
            encode_passages([c.section_title + "\n" + c.text for c in pending], settings, cancelled)
            if pending
            else []
        )
        for chunk, vector in zip(pending, vectors, strict=True):
            row = existing.get(chunk.id)
            if row is None:
                row = MaterialEmbedding(
                    chunk_id=chunk.id, model=settings.material_embedding_identity
                )
                session.add(row)
            row.content_hash = chunk.content_hash
            row.vector_json = vector
            count += 1
        session.flush()
        session.commit()
    session.flush()
    return count


def semantic_candidates(
    session: Session, revision_ids: list[str], query: str, settings: Settings, limit: int
):
    query_vector = encoder(
        settings.material_embedding_model,
        settings.material_embedding_cache_dir,
        settings.material_embedding_revision,
    ).encode([query], query=True)[0]
    ranked = []
    # Stream rows rather than retaining every document vector in memory.
    rows = session.execute(
        select(MaterialChunk, MaterialEmbedding)
        .join(MaterialEmbedding, MaterialEmbedding.chunk_id == MaterialChunk.id)
        .where(
            MaterialChunk.revision_id.in_(revision_ids),
            MaterialEmbedding.model == settings.material_embedding_identity,
        )
        .execution_options(yield_per=100, populate_existing=True)
    )
    for chunk, embedding in rows:
        if embedding.content_hash != chunk.content_hash:
            continue
        score = cosine(query_vector, embedding.vector_json)
        if score >= settings.material_semantic_min_similarity:
            ranked.append((score, chunk))
            ranked.sort(key=lambda item: (-item[0], item[1].id))
            del ranked[limit:]
    return ranked


def semantic_candidates_many(session: Session, revision_ids: list[str], queries: list[str],
                             settings: Settings, limits: list[int]):
    """Independent query vectors/scores over one streamed document-vector scan."""
    from .retrieval_cache import fingerprint, query_vectors

    unique = list(dict.fromkeys(queries))
    keys = {query: fingerprint({"query": query, "model": settings.material_embedding_identity,
        "cache_dir": settings.material_embedding_cache_dir,
        "policy": "e5-query-prefix-overflow512-stride64-mean-v1"}) for query in unique}
    vectors = {query: query_vectors.get(keys[query]) if settings.material_query_cache_enabled else None
               for query in unique}
    missing = [query for query in unique if vectors[query] is None]
    if missing:
        encoded = encoder(settings.material_embedding_model, settings.material_embedding_cache_dir,
                          settings.material_embedding_revision).encode(missing, query=True)
        if len(encoded) != len(missing):
            raise ValueError("Query vector batch length mismatch")
        for query, vector in zip(missing, encoded, strict=True):
            if not vector or any(not isinstance(v, (float, int)) or isinstance(v, bool)
                                 or not math.isfinite(v) for v in vector):
                raise ValueError("Invalid query vector")
            vectors[query] = vector
            if settings.material_query_cache_enabled:
                query_vectors.put(keys[query], vector, settings.material_query_cache_ttl_seconds)
    rankings = [[] for _ in queries]
    rows = session.execute(select(MaterialChunk, MaterialEmbedding)
        .join(MaterialEmbedding, MaterialEmbedding.chunk_id == MaterialChunk.id)
        .where(MaterialChunk.revision_id.in_(revision_ids),
               MaterialEmbedding.model == settings.material_embedding_identity)
        .execution_options(yield_per=100, populate_existing=True))
    scanned = 0
    for chunk, embedding in rows:
        scanned += 1
        if embedding.content_hash != chunk.content_hash:
            continue
        for index, query in enumerate(queries):
            score = cosine(vectors[query], embedding.vector_json)
            if score >= settings.material_semantic_min_similarity:
                rankings[index].append((score, chunk))
                rankings[index].sort(key=lambda item: (-item[0], item[1].id))
                del rankings[index][limits[index]:]
    return rankings, {"vector_scans": 1, "vector_rows": scanned,
                      "query_vector_hits": len(unique) - len(missing),
                      "query_vectors_encoded": len(missing), "encode_batches": int(bool(missing))}
