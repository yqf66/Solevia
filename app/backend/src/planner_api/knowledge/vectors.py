"""Optional FAISS exact cosine search over authorized vectors; DB is authoritative."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import math
import threading

_lock = threading.Lock()
_indexes = OrderedDict()
_ready = {}
_indexing = threading.Event()


def publish_encoder(identity, instance):
    with _lock:
        _ready.clear()
        _ready[identity] = instance


def ready_encoder(identity):
    with _lock:
        return None if _indexing.is_set() else _ready.get(identity)


def valid(vector, dimension=None):
    return (isinstance(vector, list) and 1 <= len(vector) <= 4096
            and (dimension is None or len(vector) == dimension)
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= 1e6 for v in vector)
            and sum(v * v for v in vector) > 0)


def nearest(rows, query, *, limit=24):
    if not valid(query):
        raise ValueError("Invalid query embedding")
    candidates = [c for c in rows if valid(c.vector_json, len(query))]
    if not candidates:
        return [], "NO_INDEXED_CHUNKS"
    import numpy as np

    q = np.asarray([query], dtype="float32")
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    try:
        import faiss
    except ImportError:
        matrix = np.asarray([c.vector_json for c in candidates], dtype="float32")
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        scores = (matrix @ q[0]).tolist()
        return sorted(zip(candidates, scores, strict=True), key=lambda x: (-x[1], x[0].id))[:limit], "NUMPY_EXACT"
    # Access filtering and model identity precede this cache. Include actual
    # vectors in the signature: repairs, eviction and model changes invalidate it.
    signature = hashlib.sha256(repr([(c.id, c.content_hash, c.vector_model, c.vector_json) for c in candidates]).encode()).digest()
    with _lock:
        index = _indexes.pop(signature, None)
        if index is None:
            matrix = np.asarray([c.vector_json for c in candidates], dtype="float32")
            faiss.normalize_L2(matrix)
            index = faiss.IndexFlatIP(len(query))
            index.add(matrix)
        _indexes[signature] = index
        while len(_indexes) > 4:
            _indexes.popitem(last=False)
    scores, positions = index.search(q, min(limit, len(candidates)))
    return [(candidates[int(i)], float(s)) for i, s in zip(positions[0], scores[0], strict=True) if i >= 0], "FAISS_FLAT_IP"
