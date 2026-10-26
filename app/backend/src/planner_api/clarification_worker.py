"""Process the existing durable extraction queue independently of chat replies."""
import logging
from threading import Event, Thread

from planner_api.ai import build_clarification_provider
from planner_api.config import get_settings
from planner_api.service import ClarificationService

logger = logging.getLogger(__name__)


class ClarificationAnalysisWorker:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self._stop = Event()
        self._thread = None

    def start(self):
        self._thread = Thread(target=self.run_forever, name="clarification-analysis", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def run_once(self):
        with self.session_factory() as session:
            service = ClarificationService(session, build_clarification_provider(get_settings()))
            worked, _ = service.process_pending_analysis()
            return worked

    def run_forever(self):
        while not self._stop.is_set():
            try:
                if get_settings().llm_provider == "fake":
                    self._stop.wait(1)
                    continue
                if self.run_once():
                    continue
            except Exception:
                logger.exception("Clarification analysis iteration failed")
            self._stop.wait(1)
