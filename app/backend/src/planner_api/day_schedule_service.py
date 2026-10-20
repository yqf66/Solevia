"""Single-day scheduling facade shared by Today and plan lifecycle flows.

Plan generation produces content and relative day/task structure.  This facade
is the separate, day-scoped scheduling boundary: it receives one date and the
current shared calendar, produces a read-only draft, and only applies after the
fingerprint is confirmed.  Keeping this boundary small lets an AI provider be
introduced later without changing plan generation or persistence semantics.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

from planner_api.ai import ClarificationProviderError
from planner_api.config import get_settings
from planner_api.model_telemetry import submit_model_task
from planner_api.day_allocation import DayAllocator
from planner_api.day_schedule_ai import DayScheduleProvider, build_day_schedule_provider
from planner_api.schemas import (
    DayAllocationApply,
    DayAllocationPreview,
    DayAllocationRequest,
    UnifiedTodayRead,
)


class DayScheduleService:
    """Authoritative entry point for a single day's schedule draft/apply."""

    def __init__(
        self,
        session: Session,
        provider: DayScheduleProvider | None = None,
        shared_context: dict[str, object] | None = None,
    ):
        self.allocator = DayAllocator(session)
        self.provider = provider or build_day_schedule_provider(get_settings())
        self.shared_context = shared_context or {}

    def _provider_context(self, context: dict[str, object]) -> dict[str, object]:
        if not self.shared_context:
            return context
        return {**context, "agent_orchestration": self.shared_context}

    def preview(self, request: DayAllocationRequest) -> DayAllocationPreview:
        # Applying a preview must be repeatable.  Once model fields are present
        # in the request, do not call the model a second time during apply.
        if not request.use_ai or request.model_task_order or request.model_preferred_starts:
            return self.allocator.preview(request)
        baseline = self.allocator.preview(request.model_copy(update={"use_ai": False}))
        return self._preview_with_context(request, baseline)

    def _preview_with_context(
        self, request: DayAllocationRequest, baseline: DayAllocationPreview
    ) -> DayAllocationPreview:
        try:
            advice = self.provider.suggest(self._provider_context(baseline.scheduling_context))
        except ClarificationProviderError as error:
            baseline.notes.append(str(error))
            return baseline
        known_ids = {
            item["task_id"] for item in baseline.scheduling_context.get("tasks", [])
            if isinstance(item, dict) and isinstance(item.get("task_id"), str)
        }
        suggestions = [item for item in advice.suggestions if item.task_id in known_ids]
        assisted_request = request.model_copy(update={
            "model_task_order": [item.task_id for item in suggestions],
            "model_preferred_starts": {
                item.task_id: item.preferred_start_local
                for item in suggestions if item.preferred_start_local
            },
            "model_notes": [advice.summary],
        })
        result = self.allocator.preview(assisted_request)
        result.notes = [*result.notes, advice.summary]
        return result

    def preview_many(
        self, requests: list[DayAllocationRequest]
    ) -> list[DayAllocationPreview]:
        """Build all day contexts locally, then request AI suggestions concurrently.

        SQLAlchemy work stays on the caller's session.  Only provider calls run
        in worker threads, so concurrent model requests cannot race database
        writes or produce a partially applied schedule.
        """
        baselines = [
            self.allocator.preview(request.model_copy(update={"use_ai": False}))
            for request in requests
        ]
        ai_requests = [
            (index, request, baseline)
            for index, (request, baseline) in enumerate(zip(requests, baselines, strict=True))
            if (
                request.use_ai
                and not request.model_task_order
                and not request.model_preferred_starts
            )
        ]
        advice_by_index = {}
        if ai_requests:
            workers = min(4, len(ai_requests))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="day-schedule") as pool:
                futures = {
                    submit_model_task(pool,
                        self.provider.suggest, self._provider_context(baseline.scheduling_context)
                    ): (index, baseline)
                    for index, _request, baseline in ai_requests
                }
                for future in as_completed(futures):
                    index, baseline = futures[future]
                    try:
                        advice_by_index[index] = future.result()
                    except Exception as error:  # provider errors must degrade per day
                        baseline.notes.append(f"当天 AI 排课未完成，已使用本地规则：{error}")

        previews = []
        for index, request in enumerate(requests):
            baseline = baselines[index]
            advice = advice_by_index.get(index)
            if advice is None:
                if not request.model_task_order and not request.model_preferred_starts:
                    previews.append(baseline)
                else:
                    previews.append(self.preview(request))
                continue
            known_ids = {
                item["task_id"] for item in baseline.scheduling_context.get("tasks", [])
                if isinstance(item, dict) and isinstance(item.get("task_id"), str)
            }
            suggestions = [item for item in advice.suggestions if item.task_id in known_ids]
            assisted_request = request.model_copy(update={
                "model_task_order": [item.task_id for item in suggestions],
                "model_preferred_starts": {
                    item.task_id: item.preferred_start_local
                    for item in suggestions if item.preferred_start_local
                },
                "model_notes": [advice.summary],
            })
            result = self.allocator.preview(assisted_request)
            result.notes = [*result.notes, advice.summary]
            previews.append(result)
        return previews

    def apply(self, payload: DayAllocationApply) -> UnifiedTodayRead:
        return self.allocator.apply(payload)
