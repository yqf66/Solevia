"""Preview one dated occurrence; preserve the activity, recurrence and history."""
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text

from planner_api.models import (FixedActivityMovePreview, Goal, ManualTaskDateOverride,
                               PlanVersion, ScheduleChange, WorkspaceScheduleProfile, utc_now)
from planner_api.manual_plan_service import ManualPlanService
from planner_api.schedule_constraints import shared_unavailable_windows
from planner_api.schedule_profile import routine_windows
from planner_api.schemas import _validate_local_time
from planner_api.today_service import TodayService

PROTECTED_ACTIVITY = re.compile(r"睡眠|睡觉|早餐|午餐|晚餐|吃饭|三餐|课程|上课|工作|通勤|午休")


class FixedActivityMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=36)
    source_date: date
    target_date: date
    start_local: str
    end_local: str

    @field_validator("start_local", "end_local")
    @classmethod
    def clock(cls, value):
        return _validate_local_time(value)

    @model_validator(mode="after")
    def ordered(self):
        if self.start_local >= self.end_local:
            raise ValueError("活动结束时间须晚于开始时间")
        if abs((self.target_date - self.source_date).days) > 31:
            raise ValueError("单次活动调整限于前后31天")
        return self


class FixedActivityMoveConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


def minutes(clock):
    hour, minute = map(int, clock.split(":"))
    return hour * 60 + minute


def business_error(status, message):
    return HTTPException(status, {"message": message})


class FixedActivityService:
    def __init__(self, session):
        self.session = session

    def _goal(self, goal_id):
        goal = self.session.get(Goal, goal_id)
        if not goal:
            raise business_error(404, "规划不存在")
        return goal

    def _today(self):
        profile = self.session.get(WorkspaceScheduleProfile, "default")
        return datetime.now(ZoneInfo(profile.timezone if profile else "Asia/Shanghai")).date()

    def candidates(self, goal_id, on_date):
        self._goal(goal_id)
        return {"date": on_date.isoformat(), "activities": [item.model_dump(mode="json")
            for item in TodayService(self.session).get_today(on_date).fixed_tasks
            if item.source == "QUICK" and item.status == "NOT_STARTED"
            and not PROTECTED_ACTIVITY.search(item.title)]}

    def _inspect(self, goal_id, request):
        goal = self._goal(goal_id)
        if min(request.source_date, request.target_date) < self._today():
            raise business_error(409, "过去的活动保留历史，请选择未来的安排")
        dates = sorted({request.source_date, request.target_date})
        boards = {day: TodayService(self.session).get_today(day) for day in dates}
        source = next((item for item in boards[request.source_date].fixed_tasks
            if item.id == request.task_id and item.source == "QUICK"), None)
        if not source or source.status != "NOT_STARTED":
            raise business_error(409, "该活动已变化或已经开始，请重新选择")
        if PROTECTED_ACTIVITY.search(source.title):
            raise business_error(409, "睡眠、三餐、课程和工作属于保护活动，请在原安排入口明确调整后再核算容量")
        if source.timezone != boards[request.target_date].timezone:
            raise business_error(409, "活动时区与共享日历不同，请先统一时区")
        duration = minutes(source.end_local) - minutes(source.start_local)
        if minutes(request.end_local) - minutes(request.start_local) != duration:
            raise business_error(422, "迁移必须保留活动原时长；缩短活动需要另行讨论")
        if (request.source_date, source.start_local, source.end_local) == (
                request.target_date, request.start_local, request.end_local):
            raise business_error(422, "活动的位置没有变化")
        # Never overwrite another occurrence of a recurring activity.
        if request.source_date != request.target_date and any(item.id == source.id for item in
                boards[request.target_date].fixed_tasks + boards[request.target_date].flexible_tasks):
            raise business_error(409, "目标日期已有该活动的一次安排，请选择其他日期")
        windows, blocked = {}, {}
        for day in dates:
            windows[day] = ManualPlanService(self.session).get_effective_windows(day).model_dump(mode="json")
            blocked[day] = shared_unavailable_windows(self.session, day)
        profile = self.session.get(WorkspaceScheduleProfile, "default")
        left, right = minutes(request.start_local), minutes(request.end_local)
        now = datetime.now(ZoneInfo(boards[request.target_date].timezone))
        if (request.target_date == now.date() and left <= now.hour * 60 + now.minute
                or request.source_date == now.date()
                and minutes(source.start_local) <= now.hour * 60 + now.minute):
            raise business_error(409, "今天已开始的时段保留历史，请选择尚未开始的活动和目标时间")
        allowed = any(minutes(w["start_local"]) <= left and right <= minutes(w["end_local"])
                      for w in windows[request.target_date]["windows"])
        if profile:
            allowed = allowed and any(minutes(a) <= left and right <= minutes(b)
                                      for a, b in routine_windows(profile))
        conflicts = []
        if not allowed:
            conflicts.append("目标时段不在已确认的可用时间内（睡眠、三餐等作息保持不变）")
        gap = profile.break_minutes if profile else 10
        for item in boards[request.target_date].fixed_tasks:
            if item.id == source.id and request.source_date == request.target_date:
                continue
            if left < minutes(item.end_local) + gap and minutes(item.start_local) - gap < right:
                conflicts.append(f"与{item.plan_title}的活动“{item.title}”或其休息间隔冲突")
        for start, end, label in blocked[request.target_date]:
            if left < end + gap and start - gap < right:
                conflicts.append(f"与{label}冲突")
        plan = self.session.scalar(select(PlanVersion).where(PlanVersion.goal_id == goal_id,
            PlanVersion.status.in_(["GENERATING", "DRAFT", "CONFIRMED"])).order_by(PlanVersion.created_at.desc()))
        context = {"goal": [goal.status, goal.current_requirement_version_id, goal.active_plan_version_id],
                   "plan": [plan.id, plan.revision] if plan else None,
                   "profile": ManualPlanService._profile_read(profile).model_dump(mode="json") if profile else None,
                   "days": [{"date": day.isoformat(), "windows": windows[day], "blocked": blocked[day],
                              "tasks": [item.model_dump(mode="json") for item in boards[day].fixed_tasks + boards[day].flexible_tasks]}
                             for day in dates]}
        fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Compute both dates' free capacity, including overlaps, bounds and rest gaps.
        def free(day, moved):
            occupied = [(minutes(t.start_local) - gap, minutes(t.end_local) + gap)
                        for t in boards[day].fixed_tasks if not (moved and day == request.source_date and t.id == source.id)]
            occupied += [(a - gap, b + gap) for a, b, _ in blocked[day]]
            if moved and day == request.target_date:
                occupied.append((left - gap, right + gap))
            points = set()
            for w in windows[day]["windows"]:
                points.update(range(minutes(w["start_local"]), minutes(w["end_local"])))
            if profile:
                awake = set()
                for a, b in routine_windows(profile):
                    awake.update(range(minutes(a), minutes(b)))
                points.intersection_update(awake)
            for a, b in occupied:
                points.difference_update(range(max(0, a), min(1440, b)))
            return len(points)
        summary = {"request": request.model_dump(mode="json"), "title": source.title,
                   "plan_title": source.plan_title,
                   "before": {"date": request.source_date.isoformat(), "start_local": source.start_local, "end_local": source.end_local},
                   "after": {"date": request.target_date.isoformat(), "start_local": request.start_local, "end_local": request.end_local},
                   "capacity_changes": [{"date": day.isoformat(), "before_minutes": free(day, False),
                                         "after_minutes": free(day, True), "delta_minutes": free(day, True) - free(day, False)} for day in dates],
                   "conflicts": conflicts, "can_apply": not conflicts,
                   "message": "只调整这一次活动，保留时长和后续重复安排。确认后需重新核算本规划的容量，不能把释放的时间重复分配。"}
        return summary, fingerprint

    def preview(self, goal_id, request):
        summary, fingerprint = self._inspect(goal_id, request)
        row = FixedActivityMovePreview(goal_id=goal_id, fingerprint=fingerprint, preview_json=summary)
        self.session.add(row)
        self.session.commit()
        return {"id": row.id, "status": row.status, **summary}

    def get(self, goal_id, preview_id):
        row = self.session.get(FixedActivityMovePreview, preview_id)
        if not row or row.goal_id != goal_id:
            raise business_error(404, "活动调整预览不存在")
        return row

    def apply(self, goal_id, preview_id, confirmed):
        if not confirmed:
            raise business_error(422, "请明确确认这一次活动迁移")
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        try:
            row = self.get(goal_id, preview_id)
            if row.status == "APPLIED":
                self.session.commit()
                return {"id": row.id, "status": row.status, **row.preview_json}
            created = row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at
            if row.status != "PREVIEW" or utc_now() - created > timedelta(minutes=30):
                raise business_error(409, "预览已取消或过期，请重新预览")
            request = FixedActivityMoveRequest.model_validate(row.preview_json["request"])
            summary, fingerprint = self._inspect(goal_id, request)
            if fingerprint != row.fingerprint:
                raise business_error(409, "活动、日历或规划已变化，请重新预览")
            if summary["conflicts"]:
                raise business_error(409, "目标时段有冲突，请修改安排后重新预览")
            for day in {request.source_date, request.target_date}:
                override = self.session.scalar(select(ManualTaskDateOverride).where(
                    ManualTaskDateOverride.manual_task_id == request.task_id, ManualTaskDateOverride.override_date == day))
                if override is None:
                    override = ManualTaskDateOverride(manual_task_id=request.task_id, override_date=day)
                    self.session.add(override)
                override.is_deleted = day != request.target_date
                if day == request.target_date:
                    override.time_overridden = True
                    override.start_local, override.end_local = request.start_local, request.end_local
            self.session.add(ScheduleChange(goal_id=goal_id, entity_type="FIXED_ACTIVITY", entity_id=request.task_id,
                change_kind="CONFIRMED_MOVE", before_json=summary["before"], after_json=summary["after"], reason=summary["title"]))
            row.status, row.applied_at = "APPLIED", utc_now()
            self.session.commit()
            return {"id": row.id, "status": row.status, **row.preview_json}
        except Exception:
            self.session.rollback()
            raise

    def discard(self, goal_id, preview_id):
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        try:
            row = self.get(goal_id, preview_id)
            if row.status == "APPLIED":
                raise business_error(409, "已生效的活动不能通过取消预览撤销，请重新发起调整")
            row.status = "DISCARDED"
            self.session.commit()
            return {"id": row.id, "status": row.status}
        except Exception:
            self.session.rollback()
            raise
