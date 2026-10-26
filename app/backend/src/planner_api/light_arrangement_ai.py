from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from planner_api.ai import (
    JSONTransport,
    ProviderUnavailableError,
    _safe_validation_feedback,
    _strip_code_fence,
    _urllib_json_transport,
    configured_json_transport,
)
from planner_api.config import Settings
from planner_api.context_compiler import compile_context_payload
from planner_api.light_schemas import LightDraftCreate, LightDraftResult
from planner_api.model_telemetry import model_node, observed_model_call, mark_attempt_failure, trace_policy

LIGHT_PROMPT_VERSION = "light-arrangement-v1"
INSTRUCTIONS = """你负责把用户近期想做的事情整理成一份轻量、可编辑的行动草案。
只规划输入 start_date 起 horizon_days 天内的事情。不要要求长期目标、路线或成功标准。
原则：
- 保留用户表达的事项；只在动作明显过大时有限拆解，总任务不超过 12 个。
- 未给日期就保持 scheduled_date=null；未给具体时刻就保持 start_local/end_local=null。
- 用户明确说每天/每周时才设置重复；重复活动可以没有固定时段，flexible_recurrence=true。
- WEEKLY 必须设置 recurrence_weekdays。所有日期都必须在范围内。
- 今天/明天等相对日期以 reference_date 为基准，不以 start_date 为基准。
- 估时没有依据时使用 30 分钟，并把该默认写入 assumptions。
- “有事/没空/上课”等不是要完成的任务。只有用户给出明确起止时间时才写入 unavailable_windows；
  模糊时段写入 questions，不猜时间。
- 不虚构资料、精确空闲时间、长期成果或用户承诺。
- 若需求显然需要多阶段、严格依赖或超过两周，可将 full_planning_recommended=true，
  但仍先给近期可执行草案。
只输出符合 Schema 的 JSON。"""


class LightArrangementProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        key = settings.llm_api_key.get_secret_value().strip()
        if not settings.model_runtime_configured:
            raise ProviderUnavailableError("模型未配置，已保留可手动编辑的草案。")
        self.settings = settings
        suffix = "/responses" if settings.llm_provider == "deepseek" else "/chat/completions"
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}{suffix}"
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    @model_node('light_arrangement_ai.LightArrangementProvider.arrange')
    def arrange(self, request: LightDraftCreate) -> LightDraftResult:
        schema = LightDraftResult.model_json_schema()
        data = request.model_dump(mode="json")
        data["reference_date"] = datetime.now(ZoneInfo(request.timezone)).date().isoformat()
        data = compile_context_payload(data, task_type="LIGHT_ARRANGEMENT")
        if self.settings.llm_provider == "deepseek":
            payload: dict[str, object] = {
                "model": self.settings.llm_model,
                "instructions": INSTRUCTIONS,
                "input": json.dumps(data, ensure_ascii=False),
                "reasoning": {"effort": "low"},
                "max_output_tokens": self.settings.light_max_output_tokens,
                "metadata": {"prompt_version": LIGHT_PROMPT_VERSION},
                "text": {
                    "format": {"type": "json_schema", "name": "light_arrangement", "schema": schema}
                },
            }
        else:
            payload = {
                "model": self.settings.llm_model,
                "messages": [
                    {
                        "role": "system",
                        "content": INSTRUCTIONS
                        + "\nSchema: "
                        + json.dumps(schema, ensure_ascii=False),
                    },
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.settings.light_max_output_tokens,
                "stream": False,
            }
        last: Exception | None = None
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                response = observed_model_call(self.transport,
                    self.endpoint, self.headers, payload, self.settings.llm_timeout_seconds
                )
                content = self._text(response)
                result = LightDraftResult.model_validate_json(_strip_code_fence(content))
                self._validate_scope(request, result)
                return result
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "模型请求被拒绝，已保留可手动编辑的草案。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last = error
            except (ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last = error
                feedback = "\n修复上次输出：" + _safe_validation_feedback(error)
                if self.settings.llm_provider == "deepseek":
                    payload["instructions"] = INSTRUCTIONS + feedback
                else:
                    payload["messages"][0]["content"] += feedback
            except (URLError, TimeoutError, OSError) as error:
                mark_attempt_failure(error)
                last = error
            if attempt < self.settings.llm_max_retries:
                self.sleeper(0.5 * (2**attempt))
        raise ProviderUnavailableError(
            "这次没有整理完整，原输入和可编辑草案已经保留。",
            diagnostic=_safe_validation_feedback(last) if last else None,
            code="LIGHT_ARRANGEMENT_FAILED",
        )

    @staticmethod
    def _text(response: dict[str, object]) -> str:
        if response.get("status") in {"incomplete", "failed", "cancelled"}:
            raise ValueError("Provider response was not complete")
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                if choice.get("finish_reason") in {"length", "content_filter"}:
                    raise ValueError("Provider response was truncated or filtered")
                message = choice.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
        direct = response.get("output_text")
        if isinstance(direct, str):
            return direct
        output = response.get("output")
        if isinstance(output, str) and output.strip():
            return output
        parts = []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") == "reasoning":
                    continue
                if item.get("type") == "output_text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                            if isinstance(part.get("text"), str):
                                parts.append(part["text"])
        if parts:
            return "".join(parts)
        raise ValueError("Provider response did not contain text")

    @staticmethod
    def _validate_scope(request: LightDraftCreate, result: LightDraftResult) -> None:
        end = request.start_date + timedelta(days=request.horizon_days - 1)
        for task in result.tasks:
            if task.scheduled_date and not request.start_date <= task.scheduled_date <= end:
                raise ValueError("Task date was outside the requested horizon")
            if task.recurrence_end_date and task.recurrence_end_date > end:
                raise ValueError("Recurrence exceeded the requested horizon")
        for window in result.unavailable_windows:
            if not request.start_date <= window.date <= end:
                raise ValueError("Unavailable window was outside the requested horizon")


def build_light_provider(settings: Settings) -> LightArrangementProvider:
    return LightArrangementProvider(settings)
