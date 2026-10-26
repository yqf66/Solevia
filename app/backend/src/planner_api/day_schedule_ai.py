"""AI suggestions for one day; all suggestions remain locally validated."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError, URLError

from pydantic import ValidationError

from planner_api.ai import (
    JSONTransport,
    ProviderConfigurationError,
    ProviderUnavailableError,
    _safe_validation_feedback,
    _urllib_json_transport,
    configured_json_transport,
)
from planner_api.config import Settings
from planner_api.context_compiler import compile_context_payload
from planner_api.schemas import DayScheduleAIResult
from planner_api.model_telemetry import model_node, observed_model_call, mark_attempt_failure, trace_policy

DAY_SCHEDULE_PROMPT_VERSION = "day-schedule-v4-format-feedback"
DAY_SCHEDULE_INSTRUCTIONS = """
你是 Solevia 的单日排课助手。只根据输入的当天上下文提出排课建议，不生成新任务。
只能使用上下文中的 task_id；优先尊重固定安排、可用窗口、任务前置关系和任务时长。
day_context.holiday_calendar 会明确当天是否为法定休息日或调休工作日；不得根据名称自行猜测。
调休工作日按正常工作日处理；法定休息日默认保留休息，除非上下文中已有用户主动安排的任务。
tasks[].dependencies 是系统解析后的依赖账本。未完成前置任务也在当天候选任务中时，必须先返回
前置任务，并为它安排更早的 preferred_start_local；前置任务不在当天或无法识别时，不得建议
安排后置任务。suggestions 的数组顺序就是建议执行顺序，不得让后置任务排在前置任务之前。
判断“前置任务是否在当天”必须按 dependencies[].task_id 是否存在于 tasks[].task_id，而不是按
当前 status 是否已完成。只要前置与后置都在 tasks 中、当天空档也放得下，就必须在同一份
suggestions 中同时返回它们，按“前置 → 休息间隔 → 后置”安排；不得仅返回前置任务。
preferred_start_local 只是建议起点，不能跨越可用时间，也不能与已占用时间冲突。
preferred_start_local 仅输出 HH:MM（例如 19:00）或 null，不输出日期、秒或 ISO 日期时间。
相邻任务之间至少保留 day_context.break_minutes 指定的休息间隔；依赖任务同日执行时，
后置任务建议起点不得早于前置任务建议结束时间加该间隔。
如果任务放不下，可以不返回该任务，让本地校验器保留为未安排。只输出符合 Schema 的 JSON。
day_context.user_schedule_style 是用户的轻量安排偏好。只在多个时间都满足硬约束时用它做定向调整；
它不能改变任务内容、总体路线、日期规划、依赖顺序、固定任务或可用时间边界。
tasks[].scheduling_policy 表示该任务的时间弹性。EXACT_DAY 优先在当天落实；其他策略可以结合
scheduling_policy_data 和本次完整规划分析决定当天顺序，但不得把窗口、配额或候选池误当成
必须塞满当天的硬指标。agent_orchestration.specialist_reports 是只读辅助结论，需与当天原始
任务和本地约束共同核对，不能覆盖固定占用与依赖。
""".strip()


class DayScheduleProvider(Protocol):
    def suggest(self, context: dict[str, object]) -> DayScheduleAIResult: ...


def _input(context: dict[str, object]) -> str:
    compiled = compile_context_payload(context, task_type="DAY_SCHEDULE")
    return json.dumps(
        {"prompt_version": DAY_SCHEDULE_PROMPT_VERSION, "day_context": compiled},
        ensure_ascii=False,
    )


def _strip_fence(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        line_end = normalized.find("\n")
        if line_end >= 0:
            return normalized[line_end + 1 : -3].strip()
    return normalized


def _chat_content(response: dict[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("Provider response did not contain choices")
    if choices[0].get("finish_reason") in {"length", "content_filter"}:
        raise ValueError("Provider day schedule response was truncated or filtered")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("Provider response did not contain message content")
    return message["content"]


def _response_content(response: dict[str, object]) -> str:
    if response.get("status") in {"incomplete", "failed", "cancelled", "queued", "in_progress"}:
        raise ValueError("Provider day schedule response was not completed")
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    output = response.get("output")
    if isinstance(output, str) and output.strip():
        return output
    if not isinstance(output, list):
        raise ValueError("Provider response did not contain output")
    for item in output:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str):
            return item["text"]
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    return part["text"]
    raise ValueError("Provider response did not contain output_text")


class _HTTPDayScheduleProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not settings.model_runtime_configured:
            raise ProviderConfigurationError("Solevia 暂时无法请求当天排课建议，请稍后再试。")
        self.api_key = api_key
        self.model = settings.llm_model
        self.timeout = settings.day_schedule_timeout_seconds
        self.max_retries = settings.day_schedule_max_retries
        self.max_output_tokens = settings.day_schedule_max_output_tokens
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def _parse(self, response: dict[str, object]) -> DayScheduleAIResult:
        raise NotImplementedError

    @model_node('day_schedule_ai._HTTPDayScheduleProvider.suggest')
    def suggest(self, context: dict[str, object]) -> DayScheduleAIResult:
        payload = self._payload(context)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                return self._parse(response)
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "当天排课建议请求被拒绝。", code="MODEL_HTTP_ERROR"
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            if attempt < self.max_retries:
                if isinstance(last_error, (ValueError, ValidationError)):
                    self._repair_payload(payload, last_error)
                self.sleeper(0.5 * (2**attempt))
        raise ProviderUnavailableError(
            "当天 AI 排课暂时没有完成，已自动使用本地排课规则。",
            diagnostic=_safe_validation_feedback(last_error) if last_error else None,
            code="DAY_SCHEDULE_FALLBACK",
        ) from None

    def _repair_payload(self, payload: dict[str, object], error: Exception) -> None:
        feedback = (
            "\n上次输出未通过校验，请只修正报告的错误，保留任务和约束："
            + _safe_validation_feedback(error)
        )
        if "instructions" in payload:
            payload["instructions"] = DAY_SCHEDULE_INSTRUCTIONS + feedback
        else:
            payload["messages"][0]["content"] = (
                DAY_SCHEDULE_INSTRUCTIONS
                + "\nJSON Schema："
                + json.dumps(DayScheduleAIResult.model_json_schema(), ensure_ascii=False)
                + feedback
            )

    def _payload(self, context: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError


class DeepSeekDayScheduleProvider(_HTTPDayScheduleProvider):
    def __init__(self, settings: Settings, **kwargs: object) -> None:
        super().__init__(settings, **kwargs)
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/responses"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, context: dict[str, object]) -> dict[str, object]:
        return {
            "model": self.model,
            "instructions": DAY_SCHEDULE_INSTRUCTIONS,
            "input": _input(context),
            "reasoning": {"effort": "none"},
            "max_output_tokens": self.max_output_tokens,
            "metadata": {"prompt_version": DAY_SCHEDULE_PROMPT_VERSION},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "day_schedule",
                    "schema": DayScheduleAIResult.model_json_schema(),
                }
            },
        }

    def _parse(self, response: dict[str, object]) -> DayScheduleAIResult:
        return DayScheduleAIResult.model_validate_json(_strip_fence(_response_content(response)))


class OpenAICompatibleDayScheduleProvider(_HTTPDayScheduleProvider):
    def __init__(self, settings: Settings, **kwargs: object) -> None:
        super().__init__(settings, **kwargs)
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Elastic-Planner/0.1",
        }

    def _payload(self, context: dict[str, object]) -> dict[str, object]:
        schema = json.dumps(DayScheduleAIResult.model_json_schema(), ensure_ascii=False)
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"{DAY_SCHEDULE_INSTRUCTIONS}\nJSON Schema：{schema}",
                },
                {"role": "user", "content": _input(context)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }

    def _parse(self, response: dict[str, object]) -> DayScheduleAIResult:
        return DayScheduleAIResult.model_validate_json(_strip_fence(_chat_content(response)))


class FakeDayScheduleProvider:
    def suggest(self, context: dict[str, object]) -> DayScheduleAIResult:
        tasks = context.get("tasks", [])
        suggestions = [
            {
                "task_id": item["task_id"],
                "preferred_start_local": None,
                "rationale": "按当天优先级安排。",
            }
            for item in tasks
            if isinstance(item, dict) and isinstance(item.get("task_id"), str)
        ]
        return DayScheduleAIResult(
            suggestions=suggestions,
            summary="已根据当天任务优先级和共享日历生成排课建议，具体时段由本地校验器确认。",
        )


def build_day_schedule_provider(settings: Settings) -> DayScheduleProvider:
    if settings.llm_provider == "fake":
        return FakeDayScheduleProvider()
    if settings.llm_provider == "deepseek":
        return DeepSeekDayScheduleProvider(settings)
    return OpenAICompatibleDayScheduleProvider(settings)
