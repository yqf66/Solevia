from __future__ import annotations

import re
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from planner_api.ai import ClarificationProviderError, FakeClarificationProvider
from planner_api.light_arrangement_ai import LightArrangementProvider
from planner_api.light_schemas import (
    LightDraftCreate,
    LightDraftEdit,
    LightDraftRead,
    LightDraftResult,
    LightRevisionRequest,
)
from planner_api.manual_plan_service import ManualPlanService
from planner_api.models import Goal, LightArrangementDraft, ManualPlan, utc_now
from planner_api.schedule_constraints import shared_unavailable_windows
from planner_api.schemas import ManualPlanCreate, ManualTaskCreate
from planner_api.today_service import TodayService


class LightArrangementService:
    """Short-horizon arrangement flow; no requirement/route/plan lifecycle."""

    def __init__(self, session: Session, provider: LightArrangementProvider | None = None) -> None:
        self.session = session
        self.provider = provider

    @staticmethod
    def _title(raw: str) -> str:
        first = re.split(r"[\n。；;.!！?？]", raw.strip())[0].strip()
        return (first or "近期安排")[:160]

    @staticmethod
    def _task_parts(raw: str) -> list[str]:
        return [part.strip() for part in raw.splitlines() if part.strip()][:30]

    def _result(self, payload: LightDraftCreate) -> LightDraftResult:
        parts = self._task_parts(payload.raw_input)
        tasks = []
        for title in parts:
            tasks.append(
                {
                    "title": title[:180],
                    "description": title[:4000] if len(title) > 180 else "",
                    "estimate_minutes": 30,
                    "priority": 3,
                    "scheduled_date": None,
                    "start_local": None,
                    "end_local": None,
                    "recurrence": "ONCE",
                    "recurrence_weekdays": [],
                    "recurrence_end_date": None,
                    "flexible_recurrence": True,
                }
            )
        if not tasks:
            tasks = [
                {
                    "title": payload.raw_input[:180],
                    "description": "",
                    "estimate_minutes": 30,
                    "priority": 3,
                    "scheduled_date": None,
                    "start_local": None,
                    "end_local": None,
                    "recurrence": "ONCE",
                    "recurrence_weekdays": [],
                    "recurrence_end_date": None,
                    "flexible_recurrence": False,
                }
            ]
        return LightDraftResult(
            title=self._title(payload.raw_input),
            tasks=tasks,
            assumptions=["当前按输入分行保留事项，耗时暂估 30 分钟，日期和时段未安排。"]
            + (
                ["当前草案仅保留前 30 行事项，完整输入仍保存在原始输入中。"]
                if len(payload.raw_input.splitlines()) > 30
                else []
            )
            + (
                ["过长事项已截短为备注，完整内容仍保存在原始输入中。"]
                if any(len(part) > 4000 for part in parts)
                else []
            ),
            questions=[],
            full_planning_recommended=False,
        )

    @staticmethod
    def _read(draft: LightArrangementDraft) -> LightDraftRead:
        return LightDraftRead(
            id=draft.id,
            raw_input=draft.raw_input,
            timezone=draft.timezone,
            start_date=draft.start_date,
            horizon_days=draft.horizon_days,
            status=draft.status,
            revision=draft.revision,
            result=LightDraftResult.model_validate(draft.result_json),
            error_message=draft.error_message,
            quick_plan_id=draft.manual_plan_id,
            manual_plan_id=draft.manual_plan_id,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
        )

    def create(self, payload: LightDraftCreate) -> LightDraftRead:
        result = self._result(payload)
        draft = LightArrangementDraft(
            raw_input=payload.raw_input,
            timezone=payload.timezone,
            start_date=payload.start_date,
            horizon_days=payload.horizon_days,
            status="DRAFT",
            revision=1,
            result_json=result.model_dump(mode="json"),
        )
        self.session.add(draft)
        self.session.commit()
        return self._read(draft)

    def _lock(self) -> None:
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))

    def _get(self, draft_id: str) -> LightArrangementDraft:
        draft = self.session.get(LightArrangementDraft, draft_id)
        if draft is None:
            raise HTTPException(404, "Light arrangement draft not found")
        return draft

    def get(self, draft_id: str) -> LightDraftRead:
        return self._read(self._get(draft_id))

    def list_drafts(self) -> list[LightDraftRead]:
        return [
            self._read(draft)
            for draft in self.session.scalars(
                select(LightArrangementDraft)
                .where(LightArrangementDraft.status != "APPLIED")
                .order_by(LightArrangementDraft.updated_at.desc())
            )
        ]

    def discard(self, draft_id: str, payload: LightRevisionRequest) -> None:
        self._lock()
        draft = self._get(draft_id)
        self._check_revision(draft, payload.revision)
        if draft.status in {"APPLIED", "GENERATING"}:
            raise HTTPException(409, "草案已发布或正在整理，不能丢弃。")
        self.session.delete(draft)
        self.session.commit()

    def archive(self, plan_id: str, restore: bool = False):
        self._lock()
        plan = self.session.get(ManualPlan, plan_id)
        if plan is None or plan.kind != "QUICK":
            raise HTTPException(404, "Quick plan not found")
        if restore and (plan.arrangement_json or {}).get("handoff_at"):
            raise HTTPException(409, "这份安排已由完整规划接管，不能恢复为另一份执行任务。")
        if restore and plan.status != "ACTIVE":
            manual = ManualPlanService(self.session)
            for task in manual.get_plan(plan_id).tasks:
                manual._validate_light_task(
                    plan,
                    task.model_dump(include=set(ManualTaskCreate.model_fields)),
                    task.id,
                )
        plan.status = "ACTIVE" if restore else "ARCHIVED"
        self.session.commit()
        return ManualPlanService(self.session).get_plan(plan_id)

    @staticmethod
    def _validate_result(
        draft: LightArrangementDraft, result: LightDraftResult
    ) -> LightDraftResult:
        request = LightDraftCreate(
            raw_input=draft.raw_input,
            timezone=draft.timezone,
            start_date=draft.start_date,
            horizon_days=draft.horizon_days,
        )
        try:
            LightArrangementProvider._validate_scope(request, result)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        end = draft.start_date + timedelta(days=draft.horizon_days - 1)
        return result.model_copy(
            update={
                "tasks": [
                    task.model_copy(update={"recurrence_end_date": task.recurrence_end_date or end})
                    if task.recurrence != "ONCE"
                    else task
                    for task in result.tasks
                ]
            }
        )

    def generate(self, draft_id: str, payload: LightRevisionRequest) -> LightDraftRead:
        self._lock()
        draft = self._get(draft_id)
        self._check_revision(draft, payload.revision)
        if draft.status in {"APPLIED", "GENERATING"}:
            raise HTTPException(409, "草案已发布或正在整理，请刷新后继续。")
        draft.status = "GENERATING"
        draft.revision += 1
        claimed_revision = draft.revision
        request = LightDraftCreate(
            raw_input=draft.raw_input,
            timezone=draft.timezone,
            start_date=draft.start_date,
            horizon_days=draft.horizon_days,
        )
        self.session.commit()
        result = None
        error_message = None
        try:
            if self.provider is None:
                raise ClarificationProviderError("模型未配置，原输入已保留，可直接编辑安排。")
            generated = self.provider.arrange(request)
            result = self._validate_result(draft, generated)
        except (ClarificationProviderError, HTTPException) as error:
            error_message = str(error)
        except Exception:
            error_message = "本次整理暂未完成，原输入和草案已保留，可直接编辑。"
        self._lock()
        self.session.refresh(draft)
        if draft.revision != claimed_revision or draft.status != "GENERATING":
            self.session.rollback()
            return self._read(draft)
        if result is not None:
            draft.result_json = result.model_dump(mode="json")
        draft.status = "FAILED" if error_message else "READY"
        draft.error_message = error_message
        draft.revision += 1
        self.session.commit()
        return self._read(draft)

    @staticmethod
    def _check_revision(draft: LightArrangementDraft, revision: int) -> None:
        if revision != draft.revision:
            raise HTTPException(409, "这份轻量草案已经更新，请刷新后继续。")

    def edit(self, draft_id: str, payload: LightDraftEdit) -> LightDraftRead:
        self._lock()
        draft = self._get(draft_id)
        self._check_revision(draft, payload.revision)
        if draft.status == "APPLIED":
            raise HTTPException(409, "安排已发布，请直接编辑任务。")
        draft.result_json = self._validate_result(draft, payload.result).model_dump(mode="json")
        draft.revision += 1
        draft.status = "READY"
        draft.error_message = None
        self.session.commit()
        return self._read(draft)

    def apply(self, draft_id: str, payload: LightRevisionRequest) -> LightDraftRead:
        self._lock()
        draft = self._get(draft_id)
        if draft.status == "APPLIED" and draft.manual_plan_id:
            self.session.rollback()
            return self._read(draft)
        self._check_revision(draft, payload.revision)
        if draft.status == "GENERATING":
            raise HTTPException(409, "请先保存手动草案，或等待整理完成。")
        result = self._validate_result(draft, LightDraftResult.model_validate(draft.result_json))
        if not result.tasks:
            raise HTTPException(422, "请至少添加一项任务。")
        self._check_conflicts(draft, result)
        created = ManualPlanService(self.session).create_plan(
            ManualPlanCreate(
                title=result.title,
                description=draft.raw_input,
                timezone=draft.timezone,
                tasks=result.tasks,
            ),
            commit=False,
        )
        plan = self.session.get(ManualPlan, created.id)
        plan.kind = "QUICK"
        plan.arrangement_json = {
            "draft_id": draft.id,
            "start_date": draft.start_date.isoformat(),
            "horizon_days": draft.horizon_days,
            "unavailable_windows": [
                window.model_dump(mode="json") for window in result.unavailable_windows
            ],
            "assumptions": result.assumptions,
            "questions": result.questions,
        }
        draft.manual_plan_id = plan.id
        draft.status = "APPLIED"
        draft.revision += 1
        draft.updated_at = utc_now()
        self.session.commit()
        return self._read(draft)

    def _check_conflicts(self, draft: LightArrangementDraft, result: LightDraftResult) -> None:
        today = TodayService(self.session)
        for offset in range(draft.horizon_days):
            day = draft.start_date + timedelta(days=offset)
            occupied = [
                (task.start_local, task.end_local, task.title)
                for task in today.get_today(day).fixed_tasks
                if task.start_local and task.end_local
            ]
            occupied += [
                (window.start_local, window.end_local, window.reason)
                for window in result.unavailable_windows
                if window.date == day
            ]
            occupied += [
                (f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}", reason)
                for start, end, reason in shared_unavailable_windows(self.session, day)
            ]
            for task in result.tasks:
                occurs = (
                    task.scheduled_date is not None
                    and day >= task.scheduled_date
                    and (
                        task.recurrence == "DAILY"
                        or task.recurrence == "WEEKLY"
                        and day.weekday() in task.recurrence_weekdays
                        or task.recurrence == "ONCE"
                        and day == task.scheduled_date
                    )
                    and (task.recurrence_end_date is None or day <= task.recurrence_end_date)
                )
                if not occurs or not task.start_local:
                    continue
                conflict = next(
                    (
                        title
                        for start, end, title in occupied
                        if task.start_local < end and start < task.end_local
                    ),
                    None,
                )
                if conflict:
                    raise HTTPException(
                        409,
                        {
                            "code": "LIGHT_SCHEDULE_CONFLICT",
                            "message": (
                                f"{day} 的“{task.title}”与“{conflict}”重叠，"
                                "请修改时间或保持自由安排。"
                            ),
                        },
                    )
                occupied.append((task.start_local, task.end_local, task.title))

    def upgrade(self, plan_id: str) -> str:
        from planner_api.service import ClarificationService

        self._lock()
        plan = self.session.get(ManualPlan, plan_id)
        if plan is None or plan.kind != "QUICK":
            raise HTTPException(404, "Quick plan not found")
        existing = plan.arrangement_json.get("upgraded_goal_id")
        if existing:
            if self.session.get(Goal, existing) is None:
                raise HTTPException(409, "关联的完整规划已不存在，原安排与历史仍保留。")
            return str(existing)
        current = ManualPlanService(self.session).get_plan(plan_id)
        summary = "\n".join(
            f"- {task.title}；状态 {task.status}；预计 {task.estimate_minutes} 分钟；"
            f"日期 {task.scheduled_date}；重复 {task.recurrence}；"
            f"重复截止日期（含当日）{task.recurrence_end_date}；"
            f"已记录实例 {[(str(item.override_date), item.status) for item in task.overrides]}"
            for task in current.tasks
        )
        raw = (
            f"将这份近期安排升级为完整规划：{plan.title}\n原始需求：{plan.description}\n"
            f"已有任务与进度（原安排保留，不重新计算已完成内容）：\n{summary}\n"
            "升级不代表延长重复任务的有效期；保留各任务原截止日期，只承接尚未完成的实例。"
            "只有用户后来明确要求延长该项重复行动时才能改变其截止日期。"
        )
        workspace = ClarificationService(self.session, FakeClarificationProvider()).create_goal(
            raw,
            plan.timezone,
            defer_initial_response=True,
            commit=False,
        )
        plan.arrangement_json = {**plan.arrangement_json, "upgraded_goal_id": workspace.goal_id}
        self.session.get(Goal, workspace.goal_id).title = plan.title
        self.session.commit()
        return workspace.goal_id
