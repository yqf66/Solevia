"""Bounded process-local pure query caches, never document authority."""

import time as time  # Compatibility for existing clock instrumentation.

from planner_api.bounded_cache import BoundedCache as BoundedCache, fingerprint as fingerprint

query_plans = BoundedCache()
query_vectors = BoundedCache(max_entries=256, max_bytes=4 * 1024 * 1024)
bge_scores = BoundedCache(max_entries=64, max_bytes=2 * 1024 * 1024)
