import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError

from pydantic import ValidationError

from planner_api.ai import (
    JSONTransport,
    ModelOutputTruncated,
    ProviderConfigurationError,
    ProviderUnavailableError,
    _expand_output_budget,
    _replace_retry_feedback,
    _safe_validation_feedback,
    _urllib_json_transport,
    configured_json_transport,
)
from planner_api.config import Settings
from planner_api.context_compiler import compile_context_payload
from planner_api.route_prompts import (
    ROUTE_AUTHOR_INSTRUCTIONS,
    ROUTE_COMPILER_INSTRUCTIONS,
    ROUTE_INSTRUCTIONS,
    ROUTE_PROMPT_VERSION,
)
from planner_api.schemas import RouteDraftResult, RouteStageDraft
from planner_api.model_telemetry import model_node, observed_model_call, mark_attempt_failure, trace_policy


@dataclass(frozen=True)
class RouteContext:
    requirements: dict[str, object]
    planning_mode: str = "COMMITTED"
    schedule_type: str = "RHYTHM"
    current_route: dict[str, object] | None = None
    user_feedback: str | None = None
    requirement_memory: dict[str, object] | None = None
    source_document: str | None = None
    research_context: dict[str, object] | None = None
    material_context: dict[str, object] | None = None


class RouteProvider(Protocol):
    def draft_route(self, context: RouteContext) -> RouteDraftResult: ...


def _route_input(context: RouteContext) -> str:
    payload = compile_context_payload(
        {
            "prompt_version": ROUTE_PROMPT_VERSION,
            "mode": "REVISE" if context.current_route else "CREATE",
            "confirmed_requirements": context.requirements,
            "planning_mode": context.planning_mode,
            "schedule_type": context.schedule_type,
            "current_route": context.current_route,
            "user_feedback": context.user_feedback,
            "requirement_memory": context.requirement_memory,
            "source_document": context.source_document,
            "research_context": context.research_context,
            "material_context": context.material_context,
        },
        task_type="ROUTE_REVISION" if context.current_route else "ROUTE_DRAFT",
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


class DeepSeekRouteProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not settings.model_runtime_configured:
            raise ProviderConfigurationError(
                "Solevia 暂时无法开始整理路线，请稍后再试。",
            )
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/responses"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.model = settings.llm_model
        self.reasoning_effort = settings.llm_reasoning_effort
        self.timeout = settings.route_timeout_seconds
        self.max_retries = settings.route_max_retries
        self.max_output_tokens = settings.route_max_output_tokens
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def draft_route(self, context: RouteContext) -> RouteDraftResult:
        instructions = (
            ROUTE_COMPILER_INSTRUCTIONS if context.source_document else ROUTE_INSTRUCTIONS
        )
        payload: dict[str, object] = {
            "model": self.model,
            "instructions": instructions,
            "input": _route_input(context),
            "reasoning": {"effort": self.reasoning_effort},
            "max_output_tokens": self.max_output_tokens,
            "metadata": {"prompt_version": ROUTE_PROMPT_VERSION},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "route_draft",
                    "schema": RouteDraftResult.model_json_schema(),
                }
            },
        }
        return self._request(payload)

    @model_node('route_ai.DeepSeekRouteProvider._request')
    def _request(self, payload: dict[str, object]) -> RouteDraftResult:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                return RouteDraftResult.model_validate_json(
                    _strip_code_fence(self._extract_text(response))
                )
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次路线整理，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            if attempt < self.max_retries:
                if isinstance(last_error, (ValueError, ValidationError)):
                    if isinstance(last_error, ModelOutputTruncated):
                        payload = _expand_output_budget(
                            payload, final_retry=attempt == self.max_retries - 1
                        )
                    feedback = _safe_validation_feedback(last_error)
                    payload = dict(payload)
                    payload["instructions"] = _replace_retry_feedback(
                        str(payload["instructions"]),
                        f"上一次输出未通过 Schema 校验，请修正：{feedback}",
                    )
                self.sleeper(0.5 * (2**attempt))
        raise ProviderUnavailableError(
            "Solevia 暂时没有完成这次路线整理，你的目标和反馈已经保存，可以稍后重试。",
            diagnostic=_safe_validation_feedback(last_error) if last_error else None,
        ) from None

    @staticmethod
    def _extract_text(response: dict[str, object]) -> str:
        details = response.get("incomplete_details")
        if response.get("status") == "incomplete" and isinstance(details, dict) and details.get("reason") == "max_output_tokens":
            raise ModelOutputTruncated("Provider output truncated: max_output_tokens")
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
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        combined = "".join(text_parts)
        if combined.strip():
            return combined
        raise ValueError("Provider response did not contain output_text")


class OpenAICompatibleRouteProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not settings.model_runtime_configured:
            raise ProviderConfigurationError(
                "Solevia 暂时无法开始整理路线，请稍后再试。",
            )
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Elastic-Planner/0.1",
        }
        self.model = settings.llm_model
        self.timeout = settings.route_timeout_seconds
        self.max_retries = settings.route_max_retries
        self.max_output_tokens = settings.route_max_output_tokens
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def draft_route(self, context: RouteContext) -> RouteDraftResult:
        schema = json.dumps(RouteDraftResult.model_json_schema(), ensure_ascii=False)
        instructions = (
            ROUTE_COMPILER_INSTRUCTIONS if context.source_document else ROUTE_INSTRUCTIONS
        )
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{instructions}\n只输出 JSON。输出必须符合这个 JSON Schema：{schema}"
                    ),
                },
                {"role": "user", "content": _route_input(context)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }
        return self._request(payload)

    @model_node('route_ai.OpenAICompatibleRouteProvider._request')
    def _request(self, payload: dict[str, object]) -> RouteDraftResult:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                content = self._extract_content(response)
                return RouteDraftResult.model_validate_json(_strip_code_fence(content))
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次路线整理，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            if attempt < self.max_retries:
                if isinstance(last_error, (ValueError, ValidationError)):
                    if isinstance(last_error, ModelOutputTruncated):
                        payload = _expand_output_budget(
                            payload, final_retry=attempt == self.max_retries - 1
                        )
                    messages = [
                        item
                        for item in list(payload["messages"])  # type: ignore[arg-type]
                        if not (
                            isinstance(item, dict)
                            and item.get("role") == "system"
                            and "[RETRY_FEEDBACK]" in str(item.get("content", ""))
                        )
                    ]
                    messages.append({
                        "role": "system",
                        "content": _replace_retry_feedback(
                            "",
                            "上一次路线未通过 Schema 校验，请重新生成完整 JSON 并修正："
                            f"{_safe_validation_feedback(last_error)}",
                        ),
                    })
                    payload = {**payload, "messages": messages}
                self.sleeper(0.5 * (2**attempt))
        raise ProviderUnavailableError(
            "Solevia 暂时没有完成这次路线整理，你的目标和反馈已经保存，可以稍后重试。",
            diagnostic=_safe_validation_feedback(last_error) if last_error else None,
        ) from None

    @staticmethod
    def _extract_content(response: dict[str, object]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Provider response did not contain choices")
        if choices[0].get("finish_reason") in {"length", "max_tokens"}:
            raise ModelOutputTruncated("Provider output truncated: finish_reason=length")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("Provider response did not contain message content")
        return message["content"]


class NaturalRouteAuthor:
    """Free-form author call kept separate from structural route compilation."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not settings.model_runtime_configured:
            raise ProviderConfigurationError("Solevia 暂时无法开始整理路线，请稍后再试。")
        self.provider = settings.llm_provider
        self.endpoint = (
            f"{settings.llm_base_url.rstrip('/')}/responses"
            if self.provider == "deepseek"
            else f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        )
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Elastic-Planner/0.1",
        }
        self.model = settings.llm_model
        self.timeout = settings.route_timeout_seconds
        self.max_retries = settings.route_max_retries
        self.max_output_tokens = settings.route_max_output_tokens
        self.reasoning_effort = settings.llm_reasoning_effort
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    @model_node('route_ai.NaturalRouteAuthor.draft_document')
    def draft_document(self, context: RouteContext) -> str:
        source_context = RouteContext(
            requirements=context.requirements,
            planning_mode=context.planning_mode,
            schedule_type=context.schedule_type,
            current_route=context.current_route,
            user_feedback=context.user_feedback,
            requirement_memory=context.requirement_memory,
        )
        if self.provider == "deepseek":
            payload: dict[str, object] = {
                "model": self.model,
                "instructions": ROUTE_AUTHOR_INSTRUCTIONS,
                "input": _route_input(source_context),
                "reasoning": {"effort": self.reasoning_effort},
                "max_output_tokens": self.max_output_tokens,
                "metadata": {"prompt_version": ROUTE_PROMPT_VERSION, "stage": "AUTHOR"},
            }
        else:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": ROUTE_AUTHOR_INSTRUCTIONS},
                    {"role": "user", "content": _route_input(source_context)},
                ],
                "max_tokens": self.max_output_tokens,
                "stream": False,
            }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                content = (
                    DeepSeekRouteProvider._extract_text(response)
                    if self.provider == "deepseek"
                    else OpenAICompatibleRouteProvider._extract_content(response)
                )
                if not content.strip():
                    raise ValueError("Route author returned an empty document")
                return content.strip()
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次路线整理，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError) as error:
                mark_attempt_failure(error)
                last_error = error
            if attempt < self.max_retries:
                if isinstance(last_error, ModelOutputTruncated):
                    payload = _expand_output_budget(
                        payload, final_retry=attempt == self.max_retries - 1
                    )
                self.sleeper(0.5 * (2**attempt))
        raise ProviderUnavailableError(
            "Solevia 暂时没有完成路线原稿，你的目标和反馈已经保存，可以稍后重试。",
            diagnostic=_safe_validation_feedback(last_error) if last_error else None,
        ) from None


class SemanticRouteProvider:
    def __init__(self, author: NaturalRouteAuthor, compiler: RouteProvider) -> None:
        self.author = author
        self.compiler = compiler

    def draft_route(self, context: RouteContext) -> RouteDraftResult:
        document = self.author.draft_document(context)
        compiled = self.compiler.draft_route(
            RouteContext(
                requirements=context.requirements,
                planning_mode=context.planning_mode,
                schedule_type=context.schedule_type,
                current_route=context.current_route,
                user_feedback=context.user_feedback,
                requirement_memory=context.requirement_memory,
                source_document=document,
            )
        )
        return compiled.model_copy(
            update={
                "source_document": document,
                "compiler_report": {
                    "status": "VALIDATED",
                    "source_length": len(document),
                    "unresolved_questions": list(compiled.open_questions),
                    "prompt_version": ROUTE_PROMPT_VERSION,
                },
            }
        )


class FakeRouteProvider:
    def draft_route(self, context: RouteContext) -> RouteDraftResult:
        objective = str(context.requirements.get("objective") or "完成这个目标")
        revised = context.current_route is not None
        exploratory = context.planning_mode == "EXPLORATORY"
        if exploratory:
            return RouteDraftResult(
                assistant_message=(
                    "我按你想先试试看的选择，把路线调整成了低门槛、能尽早看到效果的体验闭环。"
                    if revised
                    else "我按你想先试试看的选择，先整理了一条低门槛、能尽早看到效果的体验路线。"
                ),
                summary=f"先用基础体验、真实产出和阶段复盘判断这件事是否值得继续：{objective}",
                stages=[
                    RouteStageDraft(
                        order=1,
                        title="完成最小准备",
                        purpose="只补齐开始体验所必需的基础，避免前期准备过重。",
                        topics=["必要基础", "最低启动条件"],
                        outputs=["一份可以立即开始的最小准备清单"],
                        depends_on=[],
                        options=[],
                    ),
                    RouteStageDraft(
                        order=2,
                        title="做出一次真实成果",
                        purpose="尽早完成一次贴近真实目标的小成果，用体验而不是想象判断效果。",
                        topics=["核心体验", "小型实践", "结果记录"],
                        outputs=["一个可检查的小型真实成果"],
                        depends_on=[1],
                        options=[],
                    ),
                    RouteStageDraft(
                        order=3,
                        title="复盘是否继续",
                        purpose="结合投入感受和实际效果，明确继续深入、调整方向或停止。",
                        topics=["成效复盘", "兴趣与适配度", "下一步决定"],
                        outputs=["一份基于真实体验的继续或退出决定"],
                        depends_on=[2],
                        options=[],
                    ),
                ],
                tradeoffs=["暂不追求完整覆盖，优先验证兴趣、适配度和实际成效。"],
                assumptions=[],
                open_questions=[],
                change_summary=["改为先体验、再根据实际效果决定是否深入"] if revised else [],
                source_document="# 体验路线\n完成最小准备，做出真实成果，再复盘是否继续。",
                compiler_report={"status": "VALIDATED", "provider": "FAKE"},
            )
        source_document = (
            "# 路线概览\n先确认起点，再推进核心工作，最后验证成果。\n\n"
            "## 阶段 1：确认起点与边界\n明确当前条件、约束和验证方式。\n\n"
            "## 阶段 2：完成核心推进\n形成可检查的核心成果。\n\n"
            "## 阶段 3：验证成果并收尾\n按成功标准补齐缺口并交付。"
        )
        return RouteDraftResult(
            assistant_message=(
                "我根据你的反馈调整了一级路线，保留未受影响的部分。"
                if revised
                else "我先把目标整理成三个一级阶段。现在只确认方向与顺序，不安排每天任务。"
            ),
            summary=f"这条路线通过确认起点、推进核心工作和验证成果三个阶段完成：{objective}",
            stages=[
                RouteStageDraft(
                    order=1,
                    title="确认起点与边界",
                    purpose="把当前条件、关键约束和判断标准变成后续可以使用的清晰基线。",
                    topics=["当前基线", "约束与资源", "验证方式"],
                    outputs=["一份清晰的起点与边界清单"],
                    depends_on=[],
                    options=[],
                ),
                RouteStageDraft(
                    order=2,
                    title="完成核心推进",
                    purpose="围绕最终成果推进最必要的主体工作，并尽早形成可以检查的中间结果。",
                    topics=["核心模块", "小步验证", "问题修正"],
                    outputs=["一个可以检查的核心成果"],
                    depends_on=[1],
                    options=[
                        {
                            "label": "最小可行方向",
                            "description": "先完成范围最小、最容易验证的一条主线。",
                            "tradeoff": "覆盖面较窄，但更容易按期完成。",
                            "recommended": True,
                        },
                        {
                            "label": "完整覆盖方向",
                            "description": "同时推进多个主要模块，形成更完整的成果。",
                            "tradeoff": "覆盖更广，但复杂度和延期风险更高。",
                            "recommended": False,
                        },
                    ],
                ),
                RouteStageDraft(
                    order=3,
                    title="验证成果并收尾",
                    purpose="使用已确认的成功标准检查结果，补齐缺口并形成最终可交付成果。",
                    topics=["成果验证", "缺口修正", "最终交付"],
                    outputs=["通过成功标准检查的最终成果"],
                    depends_on=[2],
                    options=[],
                ),
            ],
            tradeoffs=["优先保证核心成果，不在一级路线中展开每日安排。"],
            assumptions=[],
            open_questions=[],
            change_summary=["根据反馈调整路线表达与阶段重点。"] if revised else [],
            source_document=source_document,
            compiler_report={"status": "VALIDATED", "provider": "FAKE"},
        )


def build_route_provider(settings: Settings) -> RouteProvider:
    if settings.llm_provider == "fake":
        return FakeRouteProvider()
    if settings.llm_provider == "codex":
        from planner_api.codex_runtime.providers import CodexRouteProvider

        return CodexRouteProvider(settings)
    if settings.llm_provider == "deepseek":
        compiler: RouteProvider = DeepSeekRouteProvider(settings)
    else:
        compiler = OpenAICompatibleRouteProvider(settings)
    return SemanticRouteProvider(NaturalRouteAuthor(settings), compiler)
