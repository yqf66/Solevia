"""Readiness for local web dependencies; never calls a paid answering model."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import math
from threading import Event, Lock, Thread
import time
from uuid import uuid4

import httpx

from planner_api.local_model_shadow import _local_post
from planner_api.materials.reranking import IDENTITY


class ResearchReadiness:
    def __init__(self, settings, *, idle_seconds=600, clock=time.monotonic):
        self.settings = settings
        self._owner = str(uuid4())
        self._clock = clock
        self.idle_seconds = idle_seconds
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._thread = None
        self._started = clock()
        self._last_activity = clock()
        self._clients = {}
        self._sequences = {}
        self._jobs = 0
        self._suspended = False
        self._suspending = False
        self._generation = 0
        self._checked = 0.0
        self._first_ready_ms = None
        self._components = {"search_reader": "LOADING"}
        if settings.research_rerank_mode == "BGE":
            self._components["bge"] = "LOADING"
        self._bge_probed = False

    def start(self):
        if self._thread is None:
            self._thread = Thread(target=self._run, name="research-readiness", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    def snapshot(self):
        with self._lock:
            self._expire_clients()
            components = dict(self._components)
            fresh = self._checked > 0 and self._clock() - self._checked < 90
            ready = fresh and not (self._suspended or self._suspending) and all(value == "READY" for value in components.values())
            return {"ready": ready, "status": "SUSPENDED" if self._suspended else "READY" if ready else "LOADING",
                    "components": components, "first_ready_ms": self._first_ready_ms,
                    "elapsed_ms": round((self._clock() - self._started) * 1000),
                    "checked_ago_ms": round((self._clock() - self._checked) * 1000) if self._checked else None,
                    "active_clients": len(self._clients), "active_jobs": self._jobs,
                    "idle_seconds_remaining": max(0, math.ceil(self.idle_seconds - (self._clock() - self._last_activity)))
                    if not self._clients and not self._jobs else None}

    def _expire_clients(self):
        now = self._clock()
        self._clients = {key: expiry for key, expiry in self._clients.items() if expiry > now}

    def _activate(self):
        self._last_activity = self._clock()
        if self._suspended or self._suspending:
            self._generation += 1
            self._suspended = False
            self._checked = 0
            self._started = self._clock()
            self._first_ready_ms = None
            self._components = {key: "LOADING" for key in self._components}

    def connection(self, client_id, enabled, sequence):
        with self._lock:
            self._expire_clients()
            if sequence > self._sequences.get(client_id, -1):
                self._sequences[client_id] = sequence
                if enabled:
                    self._activate()
                    self._clients[client_id] = self._clock() + 75
                elif client_id in self._clients:
                    self._clients.pop(client_id)
                    self._last_activity = self._clock()
                # Bound stale client bookkeeping without evicting live clients.
                if len(self._sequences) > 512:
                    for key in list(self._sequences):
                        if key not in self._clients and key != client_id:
                            del self._sequences[key]
                            break
        self._wake.set()
        return self.snapshot()

    def request_use(self):
        with self._lock:
            self._activate()
        self._wake.set()

    @contextmanager
    def usage(self):
        with self._lock:
            self._jobs += 1
            self._activate()
        try:
            yield
        finally:
            with self._lock:
                self._jobs -= 1
                self._last_activity = self._clock()
            self._wake.set()

    def _check_search_reader(self):
        with httpx.Client(trust_env=False, timeout=32) as client:
            response = client.get(self.settings.web_search_base_url.rstrip("/") + "/v1/ready",
                                  headers={"X-Research-Client": self._owner})
            response.raise_for_status()
            data = response.json()
            if data.get("ready") is not True or any(
                data.get("components", {}).get(name) is not True for name in ("search", "reader")
            ):
                raise ValueError("SEARCH_READER_NOT_READY")

    def _check_bge(self):
        base = self.settings.local_model_base_url.rstrip("/")
        with httpx.Client(trust_env=False, timeout=2) as client:
            response = client.get(base + "/health")
            response.raise_for_status()
            cold = "bge" not in response.json().get("ready", [])
        # Refreshes the idle lease while the backend is running. Only BGE is
        # retained, and OFF never contacts the optional sidecar.
        data = _local_post(base + "/warmup", {"models": ["bge"], "leaseId": self._owner}, 60)
        if "bge" not in data.get("ready", []):
            raise ValueError("BGE_NOT_READY")
        if cold or not self._bge_probed:
            data = _local_post(base + "/rerank", {
                "query": "museum reservation", "passages": [
                    "Museum visitors must book a reservation.", "Python is a programming language."]}, 15)
            scores = data.get("scores", [])
            if (data.get("identity") != IDENTITY or len(scores) != 2
                    or any(type(score) not in (int, float) or not math.isfinite(score) for score in scores)
                    or data.get("coverage", {}).get("complete") is not True):
                raise ValueError("BGE_PROBE_FAILED")
            self._bge_probed = True

    def check_once(self):
        generation = self._generation
        checks = {"search_reader": self._check_search_reader}
        if self.settings.research_rerank_mode == "BGE":
            checks["bge"] = self._check_bge
        components = {}
        # Search/reader and model warmup are independent, so their waits overlap.
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            futures = {pool.submit(check): name for name, check in checks.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    components[name] = "READY"
                except Exception:
                    components[name] = "UNAVAILABLE"
                with self._lock:
                    if generation == self._generation:
                        self._components[name] = components[name]
        with self._lock:
            if generation == self._generation:
                self._components = components
                self._checked = self._clock()
                if all(value == "READY" for value in components.values()) and self._first_ready_ms is None:
                    self._first_ready_ms = round((self._checked - self._started) * 1000)
        return self.snapshot()

    def _release_resources(self):
        with httpx.Client(trust_env=False, timeout=15) as client:
            response = client.post(self.settings.web_search_base_url.rstrip("/") + "/v1/suspend",
                                   headers={"X-Research-Client": self._owner})
            response.raise_for_status()
        if self.settings.research_rerank_mode == "BGE" and self.settings.material_rerank_mode != "GUARDED":
            data = _local_post(self.settings.local_model_base_url.rstrip("/") + "/release",
                               {"models": ["bge"], "minIdleSeconds": self.idle_seconds, "leaseId": self._owner}, 5)
            if data.get("retained"):
                # A shared consumer used the model recently. Never unload it.
                return False
        self._bge_probed = False
        return True

    def tick(self):
        with self._lock:
            self._expire_clients()
            expired = not self._clients and not self._jobs and self._clock() - self._last_activity >= self.idle_seconds
            suspended = self._suspended
            generation = self._generation
            if expired and not suspended:
                self._suspending = True
                self._checked = 0
        if suspended:
            return self.snapshot()
        if not expired:
            return self.check_once()
        try:
            released = self._release_resources()
        except Exception:
            with self._lock:
                self._suspending = False
            return self.snapshot()
        if released is False:
            with self._lock:
                self._suspending = False
            return self.snapshot()
        with self._lock:
            self._suspending = False
            if generation == self._generation and not self._clients and not self._jobs:
                self._suspended = True
                self._components = {key: "SUSPENDED" for key in self._components}
        return self.snapshot()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            result = self.tick()
            delay = 30 if result["ready"] or result["status"] == "SUSPENDED" else 2
            remaining = result["idle_seconds_remaining"]
            if remaining and not result["active_clients"]:
                delay = min(delay, remaining)
            self._wake.wait(delay)


def readiness_snapshot(application):
    manager = getattr(application.state, "research_readiness", None)
    return manager.snapshot() if manager else {"ready": True, "status": "DISABLED", "components": {}}
