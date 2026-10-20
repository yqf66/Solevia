import json
import time
from collections.abc import Callable
from dataclasses import dataclass
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
from planner_api.route_explanation_prompts import (
    ROUTE_EXPLANATION_INSTRUCTIONS,
    ROUTE_EXPLANATION_PROMPT_VERSION,
)
from planner_api.schemas import RouteExplanationResult
from planner_api.model_telemetry import model_node, observed_model_call, mark_attempt_failure, trace_policy


@dataclass(frozen=True)
class RouteExplanationContext:
    requirements: dict[str, object]
    current_route: dict[str, object]
    question: str
    conversation_memory: dict[str, object] | None = None
    research_context: dict[str, object] | None = None


class RouteExplanationProvider(Protocol):
    def explain(self, context: RouteExplanationContext) -> RouteExplanationResult: ...


def _explanation_input(context: RouteExplanationContext) -> str:
    payload = compile_context_payload(
        {
            "prompt_version": ROUTE_EXPLANATION_PROMPT_VERSION,
            "confirmed_requirements": context.requirements,
            "current_route": context.current_route,
            "user_question": context.question,
            "conversation_memory": context.conversation_memory,
            "research_context": context.research_context,
        },
        task_type="ROUTE_EXPLANATION",
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
    )


def _strip_code_fence(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        line_end = normalized.find("\n")
        if line_end != -1:
            return normalized[line_end + 1 : -3].strip()
    return normalized


def _extract_response_text(response: dict[str, object]) -> str:
    if response.get("status") != "completed":
        raise ValueError("Provider response was not completed")
    direct_text = response.get("output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text
    output = response.get("output")
    if isinstance(output, str) and output.strip():
        return output
    if not isinstance(output, list):
        raise ValueError("Provider response did not contain output")
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
            continue
        content = item.get("content")
        if isinstance(content, str):
            text_parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {"output_text", "text"}:
                continue
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    combined = "".join(text_parts)
    if combined.strip():
        return combined
    raise ValueError("Provider response did not contain output_text")


def _extract_chat_content(response: dict[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("Provider response did not contain choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("Provider response did not contain message content")
    return message["content"]


class DeepSeekRouteExplanationProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not api_key:
            raise ProviderConfigurationError("Solevia 暂时无法开始回复，请稍后再试。")
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/responses"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.model = settings.llm_model
        self.reasoning_effort = settings.llm_reasoning_effort
        self.timeout = settings.route_explanation_timeout_seconds
        self.max_retries = settings.route_explanation_max_retries
        self.max_output_tokens = settings.route_explanation_max_output_tokens
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def explain(self, context: RouteExplanationContext) -> RouteExplanationResult:
        payload: dict[str, object] = {
            "model": self.model,
            "instructions": ROUTE_EXPLANATION_INSTRUCTIONS,
            "input": _explanation_input(context),
            "reasoning": {"effort": self.reasoning_effort},
            "max_output_tokens": self.max_output_tokens,
            "metadata": {"prompt_version": ROUTE_EXPLANATION_PROMPT_VERSION},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "route_explanation",
                    "schema": RouteExplanationResult.model_json_schema(),
                }
            },
        }
        return self._request(payload)

    @model_node('route_explanation_ai.DeepSeekRouteExplanationProvider._request')
    def _request(self, payload: dict[str, object]) -> RouteExplanationResult:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                return RouteExplanationResult.model_validate_json(
                    _strip_code_fence(_extract_response_text(response))
                )
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次解释，请稍后再试。"
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            if attempt < self.max_retries:
                if isinstance(last_error, (ValueError, ValidationError)):
                    payload = {
                        **payload,
                        "instructions": (
                            f"{payload['instructions']}\n"
                            "上一次解释未通过 Schema 校验，请完整回答并修正："
                            f"{_safe_validation_feedback(last_error)}"
                        ),
                    }
                self.sleeper(0.5 * (2**attempt))
        raise ProviderUnavailableError(
            "Solevia 暂时没有完成这次解释，你的问题已经保存，可以稍后重试。",
            diagnostic=_safe_validation_feedback(last_error) if last_error else None,
        ) from None


class OpenAICompatibleRouteExplanationProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not api_key:
            raise ProviderConfigurationError("Solevia 暂时无法开始回复，请稍后再试。")
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Elastic-Planner/0.1",
        }
        self.model = settings.llm_model
        self.timeout = settings.route_explanation_timeout_seconds
        self.max_retries = settings.route_explanation_max_retries
        self.max_output_tokens = settings.route_explanation_max_output_tokens
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def explain(self, context: RouteExplanationContext) -> RouteExplanationResult:
        schema = json.dumps(RouteExplanationResult.model_json_schema(), ensure_ascii=False)
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{ROUTE_EXPLANATION_INSTRUCTIONS}\n只输出 JSON。"
                        f"输出必须符合这个 JSON Schema：{schema}"
                    ),
                },
                {"role": "user", "content": _explanation_input(context)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }
        return self._request(payload)

    @model_node('route_explanation_ai.OpenAICompatibleRouteExplanationProvider._request')
    def _request(self, payload: dict[str, object]) -> RouteExplanationResult:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                return RouteExplanationResult.model_validate_json(
                    _strip_code_fence(_extract_chat_content(response))
                )
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次解释，请稍后再试。"
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            if attempt < self.max_retries:
                if isinstance(last_error, (ValueError, ValidationError)):
                    messages = list(payload["messages"])  # type: ignore[arg-type]
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "上一次解释未通过 Schema 校验。请直接给出完整、自包含的答案并修正："
                                f"{_safe_validation_feedback(last_error)}"
                            ),
                        }
                    )
                    payload = {**payload, "messages": messages}
                self.sleeper(0.5 * (2**attempt))
        raise ProviderUnavailableError(
            "Solevia 暂时没有完成这次解释，你的问题已经保存，可以稍后重试。",
            diagnostic=_safe_validation_feedback(last_error) if last_error else None,
        ) from None


class FakeRouteExplanationProvider:
    def explain(self, context: RouteExplanationContext) -> RouteExplanationResult:
        return RouteExplanationResult(
            answer=(
                "这一安排服务于当前路线的前后依赖：先完成前置阶段留下必要输入，再通过当前阶段"
                "形成可以检查的成果，后续阶段才能在此基础上推进。具体执行时先核对阶段目的，"
                "再完成主要模块，最后用阶段产出检查是否可以进入下一步。"
            ),
            covered_questions=[context.question],
            needs_clarification=False,
        )


def build_route_explanation_provider(settings: Settings) -> RouteExplanationProvider:
    if settings.llm_provider == "fake":
        return FakeRouteExplanationProvider()
    if settings.llm_provider == "deepseek":
        return DeepSeekRouteExplanationProvider(settings)
    return OpenAICompatibleRouteExplanationProvider(settings)
