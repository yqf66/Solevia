"""Native research readiness shares the established client/job lease contract."""
from contextlib import nullcontext


class CodexResearchReadiness:
    """Native search has no external search/reader/BGE process to warm up."""
    def __init__(self, settings):
        self.settings = settings

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return {"ready": True, "status": "NATIVE", "components": {}}

    def connection(self, client_id, enabled, sequence):
        return self.snapshot()

    def request_use(self):
        pass

    def usage(self):
        return nullcontext()
