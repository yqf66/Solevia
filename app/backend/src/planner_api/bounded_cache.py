"""Bounded process-local cache for pure validated computations."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import threading
import time


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


class BoundedCache:
    def __init__(self, max_entries=128, max_bytes=2 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._rows = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self._flights = {}

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._bytes = 0

    def get(self, key):
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                return None
            expires, size, value = row
            if expires <= time.monotonic():
                self._rows.pop(key)
                self._bytes -= size
                return None
            self._rows.move_to_end(key)
            return deepcopy(value)

    def put(self, key, value, ttl):
        size = len(json.dumps(value, ensure_ascii=False).encode()) + len(key)
        if size > self.max_bytes:
            return
        with self._lock:
            if key in self._rows:
                self._bytes -= self._rows.pop(key)[1]
            self._rows[key] = (time.monotonic() + ttl, size, deepcopy(value))
            self._bytes += size
            while len(self._rows) > self.max_entries or self._bytes > self.max_bytes:
                self._bytes -= self._rows.popitem(last=False)[1][1]

    def compute(self, key, loader, *, ttl, wait_seconds):
        cached = self.get(key)
        if cached is not None:
            return cached, "HIT"
        with self._lock:
            flight = self._flights.get(key)
            owner = flight is None
            if owner and len(self._flights) < self.max_entries:
                flight = self._flights[key] = threading.Event()
            elif owner:
                flight = None
        if not owner:
            flight.wait(timeout=min(60, wait_seconds))
            cached = self.get(key)
            if cached is not None:
                return cached, "COALESCED_HIT"
        try:
            cached = self.get(key)
            if cached is not None:
                return cached, "HIT"
            value, cacheable = loader()
            if cacheable:
                self.put(key, value, ttl)
            return deepcopy(value), "MISS" if cacheable else "UNCACHED_FALLBACK"
        finally:
            if owner and flight is not None:
                with self._lock:
                    self._flights.pop(key, None)
                    flight.set()
