"""Milvus is a rebuildable vector index; SQLite owns text and authorization."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import threading
import time

from .vectors import valid

_lock = threading.Lock()
_stores = OrderedDict()


class MilvusUnavailable(RuntimeError):
    pass


def collection_name(settings, dimension: int) -> str:
    # Isolate production, OFF and acceptance databases during reconciliation.
    identity = f"{settings.database_url}:{settings.material_embedding_identity}:{dimension}:v1"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return f"{settings.knowledge_milvus_collection_prefix}_{suffix}"


def sync_target(settings, dimension):
    return hashlib.sha256((settings.knowledge_milvus_uri + ":" + collection_name(settings, dimension)).encode()).hexdigest()


def normalized(vector):
    if not valid(vector):
        raise ValueError("Invalid embedding")
    norm = math.sqrt(sum(v * v for v in vector))
    return [float(v / norm) for v in vector]


class MilvusStore:
    """Bounded RPCs, lazy SDK import, no network on the FAISS/OFF path."""

    def __init__(self, settings, *, client=None):
        self.settings = settings
        self._client = client
        self._collections = set()
        self._connect_lock = threading.Lock()
        self.unavailable_until = 0.0

    @property
    def timeout(self):
        return self.settings.knowledge_milvus_timeout_seconds

    @property
    def client(self):
        if time.monotonic() < self.unavailable_until:
            raise MilvusUnavailable("Milvus circuit is cooling down")
        with self._connect_lock:
            if self._client is None:
                from pymilvus import MilvusClient
                self._client = MilvusClient(uri=self.settings.knowledge_milvus_uri,
                    token=self.settings.knowledge_milvus_token.get_secret_value(), timeout=self.timeout)
        return self._client

    def failed(self):
        self.unavailable_until = time.monotonic() + 10

    def ensure_collection(self, dimension):
        name = collection_name(self.settings, dimension)
        if name in self._collections:
            return name
        client = self.client
        if not client.has_collection(collection_name=name, timeout=self.timeout):
            from pymilvus import DataType
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", datatype=DataType.VARCHAR, is_primary=True, max_length=36)
            schema.add_field("document_id", datatype=DataType.VARCHAR, max_length=36)
            schema.add_field("content_hash", datatype=DataType.VARCHAR, max_length=64)
            schema.add_field("vector", datatype=DataType.FLOAT_VECTOR, dim=dimension)
            indexes = client.prepare_index_params()
            indexes.add_index(field_name="vector", index_type="FLAT", metric_type="IP")
            try:
                client.create_collection(collection_name=name, schema=schema, index_params=indexes,
                    consistency_level="Strong", timeout=self.timeout)
            except Exception:
                if not client.has_collection(collection_name=name, timeout=self.timeout):
                    raise
        info = client.describe_collection(collection_name=name, timeout=self.timeout)
        fields = {f["name"]: f for f in info["fields"]}
        if (not {"chunk_id", "document_id", "content_hash", "vector"} <= fields.keys()
                or int(fields["vector"]["params"]["dim"]) != dimension):
            raise MilvusUnavailable("Incompatible knowledge collection schema")
        client.load_collection(collection_name=name, timeout=self.timeout)
        self._collections.add(name)
        return name

    def upsert(self, rows):
        if not rows:
            return
        dimension = len(rows[0][3])
        if any(not valid(v, dimension) for _, _, _, v in rows):
            raise ValueError("Invalid embedding batch")
        name = self.ensure_collection(dimension)
        self.client.upsert(collection_name=name, data=[
            {"chunk_id": key, "document_id": doc, "content_hash": digest, "vector": normalized(vector)}
            for key, doc, digest, vector in rows], timeout=self.timeout)

    def search(self, query, chunk_hashes, *, limit=24):
        """Scope before scoring; batch filters to bound the RPC payload size."""
        if not chunk_hashes:
            return []
        client = self.client
        name = collection_name(self.settings, len(query))
        if not client.has_collection(collection_name=name, timeout=self.timeout):
            self._collections.discard(name)
            raise MilvusUnavailable("Knowledge collection has not been built")
        self.ensure_collection(len(query))
        ranked = {}
        ids, q = list(chunk_hashes), normalized(query)
        deadline = time.monotonic() + self.timeout
        for start in range(0, len(ids), 1024):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MilvusUnavailable("Scoped search budget exhausted")
            scope = ids[start:start + 1024]
            result = client.search(collection_name=name, data=[q], anns_field="vector",
                limit=min(limit, len(scope)), filter="chunk_id in " + json.dumps(scope),
                output_fields=["chunk_id", "content_hash"], search_params={"metric_type": "IP"},
                consistency_level="Strong", timeout=remaining)
            if not result or len(result[0]) < min(limit, len(scope)):
                raise MilvusUnavailable("Knowledge index is incomplete")
            for item in result[0] if result else []:
                key, score = str(item.id), float(item.distance)
                entity = item.entity or {}
                if (key not in chunk_hashes or not math.isfinite(score)
                        or entity.get("content_hash") != chunk_hashes[key]):
                    raise MilvusUnavailable("Stale or invalid knowledge index result")
                ranked[key] = score
        return sorted(ranked.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]

    def inventory(self, dimension):
        name = collection_name(self.settings, dimension)
        if not self.client.has_collection(collection_name=name, timeout=self.timeout):
            self._collections.discard(name)
            return None
        iterator = self.client.query_iterator(collection_name=name, batch_size=512,
            output_fields=["chunk_id"], consistency_level="Strong", timeout=self.timeout)
        ids = []
        try:
            while batch := iterator.next():
                ids.extend(str(item["chunk_id"]) for item in batch)
        finally:
            iterator.close()
        return ids

    def delete(self, ids, dimension):
        name = collection_name(self.settings, dimension)
        for start in range(0, len(ids), 512):
            self.client.delete(collection_name=name, ids=ids[start:start + 512], timeout=self.timeout)

    def close(self):
        if self._client is not None:
            self._client.close()


def try_store(settings):
    if settings.knowledge_vector_backend != "MILVUS":
        return None
    key = (settings.knowledge_milvus_uri, settings.knowledge_milvus_token.get_secret_value(),
           settings.knowledge_milvus_timeout_seconds, settings.database_url,
           settings.material_embedding_identity, settings.knowledge_milvus_collection_prefix)
    with _lock:
        store = _stores.pop(key, None) or MilvusStore(settings)
        _stores[key] = store
        while len(_stores) > 4:
            _, old = _stores.popitem(last=False)
            old.close()
        return store
