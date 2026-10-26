"""Research readiness follows the selected kernel without restarting Solevia."""

from threading import RLock
from planner_api.config import get_settings


class KernelReadiness:
    def __init__(self):
        self.lock = RLock()
        self.managers = {}

    def _current(self):
        with self.lock:
            settings = get_settings()
            if (
                settings.llm_provider != "codex"
                and settings.web_search_provider != "open_websearch"
            ):
                return None
            key = (
                settings.llm_provider == "codex",
                settings.web_search_base_url,
                settings.research_rerank_mode,
            )
            if key not in self.managers:
                if key[0]:
                    from planner_api.codex_runtime.readiness import CodexResearchReadiness

                    manager = CodexResearchReadiness(settings)
                else:
                    from planner_api.research.readiness import ResearchReadiness

                    manager = ResearchReadiness(settings)
                manager.start()
                self.managers[key] = manager
            return self.managers[key]

    def start(self):
        self._current()

    def stop(self):
        with self.lock:
            for manager in self.managers.values():
                manager.stop()
            self.managers.clear()

    def snapshot(self):
        manager = self._current()
        return (
            manager.snapshot()
            if manager
            else {"ready": True, "status": "DISABLED", "components": {}}
        )

    def connection(self, client_id, enabled, sequence):
        manager = self._current()
        return manager.connection(client_id, enabled, sequence) if manager else self.snapshot()

    def request_use(self):
        manager = self._current()
        if manager:
            manager.request_use()

    def usage(self):
        from contextlib import nullcontext

        manager = self._current()
        return manager.usage() if manager else nullcontext()
