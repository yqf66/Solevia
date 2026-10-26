import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException as HTTPProtocolError
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from pydantic import ValidationError

from planner_api.config import Settings
from planner_api.context_compiler import compile_context_payload
from planner_api.prompts import (
    CLARIFICATION_INSTRUCTIONS,
    CLARIFICATION_PROMPT_VERSION,
    REQUIREMENT_ANALYSIS_INSTRUCTIONS,
)
from planner_api.schemas import (
    ClarificationResult,
    ConversationReply,
    ProposedFact,
    ReadinessResult,
    RequirementAnalysis,
)
from planner_api.model_telemetry import model_node, observed_model_call, mark_attempt_failure, trace_policy

FIELD_DETAILS = {
    "objective": ("OBJECTIVE", "最终想完成什么"),
    "current_state": ("CURRENT_STATE", "当前基础和进展"),
    "desired_outcomes": ("OUTCOME", "希望得到的具体成果"),
    "success_criteria": ("SUCCESS", "如何判断目标已经完成"),
    "available_capacity": ("CAPACITY", "大致可投入的时间"),
    "deadline": ("DEADLINE", "截止时间或目标时间"),
}

QUESTION_BANK = {
    "current_state": "你目前已经具备哪些相关基础，或者已经进行到哪一步？",
    "desired_outcomes": "完成这件事后，你最希望拿到什么具体成果？",
    "available_capacity": "你每周大概可以投入多少时间？不需要特别精确。",
    "deadline": "有没有硬截止日期或希望完成的大致时间？没有也可以直接说没有。",
    "success_criteria": "到什么程度时，你会认为这个目标已经完成？",
}


@dataclass(frozen=True)
class ClarificationContext:
    raw_input: str
    active_facts: dict[str, object]
    latest_user_message: str | None
    answer_for_keys: list[str]
    recent_messages: list[dict[str, object]] | None = None
    summary: str | None = None
    current_topic: str | None = None
    proposed_facts: dict[str, object] | None = None
    recent_changes: list[dict[str, object]] | None = None
    user_profile_memory: list[dict[str, object]] | None = None
    error_avoidance_memory: list[dict[str, object]] | None = None
    research_context: dict[str, object] | None = None
    material_context: dict[str, object] | None = None
    temporal_context: dict[str, object] | None = None
    long_term_memory: list[dict[str, object]] | None = None


class ClarificationProvider(Protocol):
    def clarify(self, context: ClarificationContext) -> ClarificationResult: ...

    def respond(self, context: ClarificationContext) -> ConversationReply: ...

    def analyze(
        self, context: ClarificationContext, assistant_message: str
    ) -> RequirementAnalysis: ...


class ClarificationProviderError(RuntimeError):
    """Safe, user-displayable failure from an external model provider."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic: str | None = None,
        code: str = "PROVIDER_ERROR",
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic
        self.code = code


class ProviderConfigurationError(ClarificationProviderError):
    pass


class ProviderUnavailableError(ClarificationProviderError):
    def __init__(self, message: str, *, diagnostic: str | None = None,
                 code: str = "PROVIDER_ERROR") -> None:
        if diagnostic == "HTTP 402":
            message = "模型服务拒绝请求（HTTP 402，通常是余额或额度不足）。输入和草稿已保存，请恢复额度后重试。"
        super().__init__(message, diagnostic=diagnostic, code=code)


def _unavailable_error(error: Exception | None) -> ProviderUnavailableError:
    if isinstance(error, TimeoutError):
        return ProviderUnavailableError(
            "这次回复等待得有些久。你的内容已经保存，可以直接重试，不需要重新填写。",
            diagnostic="Provider request timed out",
            code="MODEL_TIMEOUT",
        )
    if isinstance(error, (ValueError, ValidationError)):
        return ProviderUnavailableError(
            "这次回复没有整理完整。你的内容已经保存，可以直接重试。",
            diagnostic=_safe_validation_feedback(error),
            code="INVALID_MODEL_RESPONSE",
        )
    if isinstance(error, HTTPError):
        return ProviderUnavailableError(
            "Solevia 暂时没有得到完整回复。你的内容已经保存，可以直接重试。",
            diagnostic=f"HTTP {error.code}",
            code="MODEL_HTTP_ERROR",
        )
    return ProviderUnavailableError(
        "Solevia 暂时没有得到完整回复。你的内容已经保存，可以直接重试。",
        diagnostic=_safe_validation_feedback(error) if error else None,
        code="MODEL_CONNECTION_ERROR",
    )


JSONTransport = Callable[[str, dict[str, str], dict[str, object], float], dict[str, object]]
_RETRY_FEEDBACK_MARKER = "\n[RETRY_FEEDBACK]\n"


def _replace_retry_feedback(instructions: str, feedback: str) -> str:
    """Keep only the latest validation feedback on a retry.

    Appending a new copy on every retry made otherwise small requests grow
    with stale errors. The original instructions remain intact and the latest
    diagnostic is bounded so a malformed provider response cannot create a
    second context-expansion loop.
    """
    base = instructions.split(_RETRY_FEEDBACK_MARKER, 1)[0]
    return f"{base}{_RETRY_FEEDBACK_MARKER}{feedback[:1200]}"


class ModelOutputTruncated(ValueError):
    """A provider length stop must not be treated as ordinary invalid JSON."""


def _expand_output_budget(
    payload: dict[str, object], *, final_retry: bool, ceiling: int = 16000
) -> dict[str, object]:
    field = "max_output_tokens" if "max_output_tokens" in payload else "max_tokens"
    current = int(payload[field])
    ceiling = max(current, ceiling)
    budget = ceiling if final_retry else min(ceiling, current * 2)
    return {**payload, field: budget}


def _context_payload(context: ClarificationContext) -> dict[str, object]:
    active_facts = dict(context.active_facts)
    if active_facts.get("objective") == context.raw_input:
        active_facts["objective"] = "已确认，完整内容见 original_goal"
    return compile_context_payload(
        {
            "prompt_version": CLARIFICATION_PROMPT_VERSION,
            "turn_kind": "INITIAL" if context.latest_user_message is None else "FOLLOW_UP",
            "original_goal": context.raw_input,
            "confirmed_facts": active_facts,
            "proposed_facts": context.proposed_facts or {},
            "conversation_summary": context.summary or "",
            "current_topic": context.current_topic,
            "recent_messages": context.recent_messages or [],
            "recent_changes": context.recent_changes or [],
            "user_profile_memory": context.user_profile_memory or [],
            "error_avoidance_memory": context.error_avoidance_memory or [],
            "long_term_memory": context.long_term_memory or [],
            "research_context": context.research_context,
            "material_context": context.material_context,
            "latest_user_message": context.latest_user_message,
            "latest_questions_answered": context.answer_for_keys,
            "temporal_context": context.temporal_context,
        },
        task_type="CLARIFICATION",
    )


def _analysis_payload(context: ClarificationContext, assistant_message: str) -> dict[str, object]:
    payload = _context_payload(context)
    payload["assistant_message"] = assistant_message
    return compile_context_payload(payload, task_type="REQUIREMENT_MEMORY_EXTRACTION")


def _safe_validation_feedback(error: Exception) -> str:
    if isinstance(error, ValidationError):
        details = []
        for item in error.errors(include_input=False)[:6]:
            location = ".".join(str(part) for part in item["loc"])
            details.append(f"{location}: {item['msg']}")
        return "；".join(details)
    detail = str(error).strip()
    if detail:
        return f"返回结构无法读取：{type(error).__name__}: {detail[:500]}"
    return f"返回结构无法读取：{type(error).__name__}"


def _parse_conversation_text(value: str) -> str | ConversationReply:
    """Accept natural text, while handling providers that ignore the plain-text instruction."""
    normalized = value.strip()
    if normalized.startswith("{"):
        try:
            return ConversationReply.model_validate_json(normalized)
        except ValidationError:
            pass
    internal_markers = (
        "我应该",
        "需要避免",
        "保持不排期",
        "留一个问题",
        "对话策略",
        "内部思路",
    )
    if sum(marker in normalized for marker in internal_markers) >= 2:
        raise ValueError("response contains internal planning notes")
    return normalized


def _guard_research_reply(provider, context, reply):
    if not getattr(provider, "_research_scope_guard_enabled", False):
        return reply
    from planner_api.research.scope_guard import AnswerScopeReview, SCOPE_INSTRUCTIONS, guard_answer_scope

    research = context.research_context
    if not research or research.get("evidence_mode") != "SPANS":
        return reply

    def review(text):
        options = dict(instructions=SCOPE_INSTRUCTIONS, input_payload=json.loads(text),
                       schema=AnswerScopeReview.model_json_schema())
        if isinstance(provider, DeepSeekClarificationProvider):
            options["schema_name"] = "research_answer_scope"
        payload = provider._build_structured_payload(**options)
        return provider._request(payload, AnswerScopeReview.model_validate_json)

    provider.last_research_scope_review = []
    answer = guard_answer_scope(context.latest_user_message or context.raw_input,
        research, reply.assistant_message, review, audit=provider.last_research_scope_review)
    return reply.model_copy(update={"assistant_message": answer})


def _research_reply_instructions(provider, context):
    from planner_api.research.scope_guard import SCOPE_AUTHOR_INSTRUCTIONS
    if context.research_context and context.research_context.get("evidence_mode") == "SPANS":
        return SCOPE_AUTHOR_INSTRUCTIONS
    return CLARIFICATION_INSTRUCTIONS


def _urllib_json_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: float,
    *,
    proxy_url: str = "",
) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        if proxy_url:
            opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
            response_context = opener.open(request, timeout=timeout)  # noqa: S310
        else:
            response_context = urlopen(request, timeout=timeout)  # noqa: S310
        with response_context as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except HTTPProtocolError as error:
        raise URLError("模型连接中断，未收到完整响应。") from error
    if not isinstance(decoded, dict):
        raise ValueError("Provider response was not a JSON object")
    return decoded


def configured_json_transport(
    settings: Settings,
    transport: JSONTransport,
) -> JSONTransport:
    """Select the app runtime only for the built-in transport.

    Injected transports used by tests or callers remain untouched.
    """
    if settings.llm_provider == "codex" and transport is _urllib_json_transport:
        from planner_api.codex_runtime.business import codex_json_transport

        return codex_json_transport(settings)
    proxy_url = settings.llm_proxy_url.strip()
    if not proxy_url or transport is not _urllib_json_transport:
        return transport

    def proxied_transport(
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        return _urllib_json_transport(
            url,
            headers,
            payload,
            timeout,
            proxy_url=proxy_url,
        )

    return proxied_transport


def _strip_code_fence(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        first_line_end = normalized.find("\n")
        if first_line_end != -1:
            return normalized[first_line_end + 1 : -3].strip()
    return normalized


class DeepSeekClarificationProvider:
    """DeepSeek Responses API adapter with schema validation and bounded retry."""

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
                "Solevia 暂时无法开始回复，请稍后再试。",
            )
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/responses"
        self.model = settings.llm_model
        self.reasoning_effort = settings.llm_reasoning_effort
        self.analysis_reasoning_effort = settings.llm_analysis_reasoning_effort
        self.timeout = settings.llm_timeout_seconds
        self.max_retries = settings.llm_max_retries
        self.max_output_tokens = settings.llm_max_output_tokens
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._model_trace_policy = trace_policy(settings)
        self._research_scope_guard_enabled = settings.research_answer_scope_guard_enabled
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def clarify(self, context: ClarificationContext) -> ClarificationResult:
        return self._request(self._build_payload(context), ClarificationResult.model_validate_json)  # type: ignore[return-value]

    def respond(self, context: ClarificationContext) -> ConversationReply:
        payload = self._build_plain_text_payload(
            instructions=(
                f"{_research_reply_instructions(self, context)}\n"
                "这次请求只输出给用户看的自然语言回复，不要输出 JSON、代码块或字段名。"
            ),
            input_payload=_context_payload(context),
        )
        try:
            parsed = self._request(payload, _parse_conversation_text)
            if isinstance(parsed, ConversationReply):
                reply = parsed
            else:
                reply = ConversationReply(assistant_message=parsed)
        except ProviderUnavailableError as structured_error:
            if structured_error.diagnostic == "HTTP 402":
                raise
            # Keep a compatibility fallback for deployments that reject plain text format.
            # Structured memory extraction remains an independent durable analysis job.
            fallback = self._build_structured_payload(
                instructions=_research_reply_instructions(self, context),
                input_payload=_context_payload(context),
                schema_name="conversation_reply",
                schema=ConversationReply.model_json_schema(),
            )
            try:
                reply = self._request(
                    fallback, ConversationReply.model_validate_json
                )
            except ProviderUnavailableError:
                raise structured_error from None
        return _guard_research_reply(self, context, reply)

    def analyze(self, context: ClarificationContext, assistant_message: str) -> RequirementAnalysis:
        payload = self._build_structured_payload(
            instructions=REQUIREMENT_ANALYSIS_INSTRUCTIONS,
            input_payload=_analysis_payload(context, assistant_message),
            schema_name="requirement_analysis",
            schema=RequirementAnalysis.model_json_schema(),
        )
        payload["reasoning"] = {"effort": self.analysis_reasoning_effort}
        return self._request(payload, RequirementAnalysis.model_validate_json)  # type: ignore[return-value]

    @model_node('ai.DeepSeekClarificationProvider._request', operation_arg='parser')
    def _request(self, payload: dict[str, object], parser: Callable[[str], object]) -> object:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                output_text = self._extract_output_text(response)
                return parser(_strip_code_fence(output_text))
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次回复，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error

            if isinstance(last_error, TimeoutError) and attempt >= min(self.max_retries, 1):
                break
            if attempt < self.max_retries:
                if isinstance(last_error, (ValueError, ValidationError)):
                    if isinstance(last_error, ModelOutputTruncated):
                        payload = _expand_output_budget(
                            payload, final_retry=attempt == self.max_retries - 1
                        )
                    payload = self._add_retry_feedback(
                        payload, _safe_validation_feedback(last_error)
                    )
                self.sleeper(0.5 * (2**attempt))

        raise _unavailable_error(last_error) from None

    @staticmethod
    def _add_retry_feedback(payload: dict[str, object], feedback: str) -> dict[str, object]:
        updated = dict(payload)
        updated["instructions"] = _replace_retry_feedback(
            str(payload["instructions"]),
            f"上一次输出未通过 Schema 校验。请重新生成并修正这些问题：{feedback}",
        )
        return updated

    def _build_payload(self, context: ClarificationContext) -> dict[str, object]:
        return self._build_structured_payload(
            instructions=CLARIFICATION_INSTRUCTIONS,
            input_payload=_context_payload(context),
            schema_name="clarification_result",
            schema=ClarificationResult.model_json_schema(),
        )

    def _build_structured_payload(
        self,
        *,
        instructions: str,
        input_payload: dict[str, object],
        schema_name: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        return {
            "model": self.model,
            "instructions": instructions,
            "input": json.dumps(input_payload, ensure_ascii=False),
            "reasoning": {"effort": self.reasoning_effort},
            "max_output_tokens": self.max_output_tokens,
            "metadata": {"prompt_version": CLARIFICATION_PROMPT_VERSION},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                }
            },
        }

    def _build_plain_text_payload(
        self,
        *,
        instructions: str,
        input_payload: dict[str, object],
    ) -> dict[str, object]:
        return {
            "model": self.model,
            "instructions": instructions,
            "input": json.dumps(input_payload, ensure_ascii=False),
            "reasoning": {"effort": self.reasoning_effort},
            "max_output_tokens": self.max_output_tokens,
            "metadata": {"prompt_version": CLARIFICATION_PROMPT_VERSION},
            "text": {"format": {"type": "text"}},
        }

    @staticmethod
    def _extract_output_text(response: dict[str, object]) -> str:
        if response.get("status") != "completed":
            if response.get("status") == "incomplete" and (response.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
                raise ModelOutputTruncated("Provider response was incomplete: max_output_tokens")
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
        if not combined.strip():
            raise ValueError("Provider response did not contain output_text")
        return combined


class OpenAICompatibleClarificationProvider:
    """Adapter for gateways implementing the OpenAI chat-completions protocol."""

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
                "Solevia 暂时无法开始回复，请稍后再试。",
            )
        self.endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_seconds
        self.max_retries = settings.llm_max_retries
        self.max_output_tokens = settings.llm_max_output_tokens
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Elastic-Planner/0.1",
        }
        self._model_trace_policy = trace_policy(settings)
        self._research_scope_guard_enabled = settings.research_answer_scope_guard_enabled
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def clarify(self, context: ClarificationContext) -> ClarificationResult:
        return self._request(self._build_payload(context), ClarificationResult.model_validate_json)  # type: ignore[return-value]

    def respond(self, context: ClarificationContext) -> ConversationReply:
        payload = self._build_structured_payload(
            instructions=_research_reply_instructions(self, context),
            input_payload=_context_payload(context),
            schema=ConversationReply.model_json_schema(),
        )
        reply = self._request(payload, ConversationReply.model_validate_json)
        return _guard_research_reply(self, context, reply)

    def analyze(self, context: ClarificationContext, assistant_message: str) -> RequirementAnalysis:
        payload = self._build_structured_payload(
            instructions=REQUIREMENT_ANALYSIS_INSTRUCTIONS,
            input_payload=_analysis_payload(context, assistant_message),
            schema=RequirementAnalysis.model_json_schema(),
        )
        return self._request(payload, RequirementAnalysis.model_validate_json)  # type: ignore[return-value]

    @model_node('ai.OpenAICompatibleClarificationProvider._request', operation_arg='parser')
    def _request(self, payload: dict[str, object], parser: Callable[[str], object]) -> object:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                content = self._extract_message_content(response)
                return parser(_strip_code_fence(content))
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次回复，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            if isinstance(last_error, TimeoutError) and attempt >= min(self.max_retries, 1):
                break
            if attempt < self.max_retries:
                if isinstance(last_error, (ValueError, ValidationError)):
                    if isinstance(last_error, ModelOutputTruncated):
                        payload = _expand_output_budget(
                            payload, final_retry=attempt == self.max_retries - 1
                        )
                    payload = self._add_retry_feedback(
                        payload, _safe_validation_feedback(last_error)
                    )
                self.sleeper(0.5 * (2**attempt))

        raise _unavailable_error(last_error) from None

    @staticmethod
    def _add_retry_feedback(payload: dict[str, object], feedback: str) -> dict[str, object]:
        updated = dict(payload)
        messages = list(payload["messages"])  # type: ignore[arg-type]
        messages = [
            item
            for item in messages
            if not (
                isinstance(item, dict)
                and item.get("role") == "system"
                and _RETRY_FEEDBACK_MARKER.strip() in str(item.get("content", ""))
            )
        ]
        messages.append({
            "role": "system",
            "content": _replace_retry_feedback(
                "",
                f"上一次输出未通过 Schema 校验。请重新生成完整 JSON，并修正这些问题：{feedback}",
            ),
        })
        updated["messages"] = messages
        return updated

    def _build_payload(self, context: ClarificationContext) -> dict[str, object]:
        return self._build_structured_payload(
            instructions=CLARIFICATION_INSTRUCTIONS,
            input_payload=_context_payload(context),
            schema=ClarificationResult.model_json_schema(),
        )

    def _build_structured_payload(
        self,
        *,
        instructions: str,
        input_payload: dict[str, object],
        schema: dict[str, object],
    ) -> dict[str, object]:
        schema_json = json.dumps(schema, ensure_ascii=False)
        input_context = json.dumps(input_payload, ensure_ascii=False)
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{instructions}\n只输出 JSON，不要使用 Markdown。"
                        f"输出必须符合这个 JSON Schema：{schema_json}"
                    ),
                },
                {"role": "user", "content": input_context},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }

    @staticmethod
    def _extract_message_content(response: dict[str, object]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("Provider response did not contain choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ValueError("Provider choice was invalid")
        if first.get("finish_reason") in {"length", "max_tokens"}:
            raise ModelOutputTruncated("Provider output truncated: finish_reason=length")
        message = first.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("Provider response did not contain message content")
        return message["content"]


def build_clarification_provider(settings: Settings) -> ClarificationProvider:
    if settings.llm_provider == "fake":
        return FakeClarificationProvider()
    if settings.llm_provider == "codex":
        from planner_api.codex_runtime.providers import CodexClarificationProvider

        return CodexClarificationProvider(settings)
    if settings.llm_provider == "deepseek":
        return DeepSeekClarificationProvider(settings)
    return OpenAICompatibleClarificationProvider(settings)


class FakeClarificationProvider:
    """Deterministic provider for local use and tests.

    It treats the user's response as the answer to the question keys shown in the
    previous assistant turn. A real provider can later extract more granular facts
    while keeping this exact contract.
    """

    def clarify(self, context: ClarificationContext) -> ClarificationResult:
        facts: list[ProposedFact] = []
        active = dict(context.active_facts)

        if "objective" not in active:
            facts.append(
                ProposedFact(
                    category="OBJECTIVE",
                    field_key="objective",
                    value=context.raw_input,
                    source="USER",
                    status="CONFIRMED",
                    confidence=1,
                )
            )
            active["objective"] = context.raw_input

        if context.latest_user_message:
            for key in context.answer_for_keys:
                if key in FIELD_DETAILS:
                    category, _ = FIELD_DETAILS[key]
                    facts.append(
                        ProposedFact(
                            category=category,
                            field_key=key,
                            value=context.latest_user_message,
                            source="USER",
                            status="CONFIRMED",
                            confidence=1,
                        )
                    )
                    active[key] = context.latest_user_message

        missing = [key for key in QUESTION_BANK if key not in active]
        question_keys = missing[:2]
        questions = [QUESTION_BANK[key] for key in question_keys]

        if not missing:
            return ClarificationResult(
                response_type="REVIEW_READY",
                assistant_message=(
                    "目前的信息已经足以先确定整体路线。我整理了一份需求摘要，"
                    "你可以确认，也可以继续修改。"
                ),
                proposed_facts=facts,
                questions=[],
                question_keys=[],
                open_questions=[],
                readiness=ReadinessResult(
                    ready_for_review=True,
                    blocking_fields=[],
                    reason="六项核心需求已经明确，没有阻塞路线设计的问题。",
                ),
            )

        if context.latest_user_message is None:
            prefix = self._initial_analysis(context.raw_input)
        else:
            prefix = "明白了，我已经把这部分记下来。接下来再确认两个关键点。"

        return ClarificationResult(
            response_type="CLARIFY",
            assistant_message=prefix
            + "\n\n"
            + "\n".join(f"{index + 1}. {question}" for index, question in enumerate(questions)),
            proposed_facts=facts,
            questions=questions,
            question_keys=question_keys,
            open_questions=questions,
            readiness=ReadinessResult(
                ready_for_review=False,
                blocking_fields=missing,
                reason=f"还需要明确：{'、'.join(FIELD_DETAILS[key][1] for key in missing)}。",
            ),
        )

    def respond(self, context: ClarificationContext) -> ConversationReply:
        active = dict(context.active_facts)
        missing = [key for key in QUESTION_BANK if key not in active]
        if context.latest_user_message is None:
            prefix = self._initial_analysis(context.raw_input)
        else:
            prefix = "我明白你的侧重点了，这会影响后面路线怎么取舍。"
        if not missing:
            return ConversationReply(
                assistant_message=(
                    f"{prefix}\n\n我们已经聊出了一个可以先往前走的版本。"
                    "你可以继续讨论，也可以在右侧先看看我整理的理解。"
                )
            )
        question = QUESTION_BANK[missing[0]]
        return ConversationReply(assistant_message=f"{prefix}\n\n1. {question}")

    def analyze(self, context: ClarificationContext, assistant_message: str) -> RequirementAnalysis:
        del assistant_message
        facts: list[ProposedFact] = []
        active = dict(context.active_facts)
        if "objective" not in active:
            facts.append(
                ProposedFact(
                    category="OBJECTIVE",
                    field_key="objective",
                    value=context.raw_input,
                    source="USER",
                    status="CONFIRMED",
                    confidence=1,
                )
            )
            active["objective"] = context.raw_input
        if context.latest_user_message:
            missing_before = [key for key in QUESTION_BANK if key not in active]
            primary_key = (
                context.answer_for_keys
                or ([context.current_topic] if context.current_topic in FIELD_DETAILS else [])
                or missing_before[:1]
                or [None]
            )[0]
            detected_keys = [primary_key] if primary_key else []
            content = context.latest_user_message
            if "current_state" in missing_before and any(
                marker in content for marker in ("基础", "会", "做过", "经验", "目前")
            ):
                detected_keys.append("current_state")
            if "desired_outcomes" in missing_before and any(
                marker in content for marker in ("希望", "作品", "项目", "成果", "拿到")
            ):
                detected_keys.append("desired_outcomes")
            if "available_capacity" in missing_before and any(
                marker in content for marker in ("小时", "每天", "每周", "投入")
            ):
                detected_keys.append("available_capacity")
            if "deadline" in missing_before and any(
                marker in content for marker in ("月后", "周后", "截止", "之前", "投递")
            ):
                detected_keys.append("deadline")
            if "success_criteria" in missing_before and any(
                marker in content for marker in ("能够", "算完成", "标准", "独立", "达到")
            ):
                detected_keys.append("success_criteria")
            for key in dict.fromkeys(detected_keys):
                if key not in FIELD_DETAILS or key not in missing_before:
                    continue
                category, _ = FIELD_DETAILS[key]
                facts.append(
                    ProposedFact(
                        category=category,
                        field_key=key,
                        value=context.latest_user_message,
                        source="USER",
                        status="CONFIRMED",
                        confidence=1,
                    )
                )
                active[key] = context.latest_user_message
        missing = [key for key in QUESTION_BANK if key not in active]
        ready = not missing
        summary_parts = [f"{key}: {value}" for key, value in active.items()]
        return RequirementAnalysis(
            proposed_facts=facts,
            conflicts=[],
            open_questions=[QUESTION_BANK[key] for key in missing],
            current_topic=missing[0] if missing else "整理目标理解",
            summary="；".join(summary_parts) or "正在理解用户的初步想法。",
            entity_refs=[],
            readiness=ReadinessResult(
                ready_for_review=ready,
                blocking_fields=missing,
                reason=(
                    "已经足以先形成一级路线。"
                    if ready
                    else f"还有可能改变路线的议题：{'、'.join(missing)}。"
                ),
                confidence=0.9 if ready else 0.4,
                blocking_topics=[QUESTION_BANK[key] for key in missing],
            ),
        )

    @staticmethod
    def _initial_analysis(raw_input: str) -> str:
        normalized = raw_input.lower()
        if any(word in normalized for word in ("agent", "大模型", "实习", "求职")):
            return (
                "我先说说对这个目标的初步理解。\n\n"
                "这不只是“学习一些大模型知识”，而是要把四件事连成一个闭环：\n\n"
                "- 补齐能够独立完成应用开发的工程基础；\n"
                "- 理解模型调用、工具、记忆、检索和评估等核心模块；\n"
                "- 做出可以运行、解释和展示的项目；\n"
                "- 把项目经验整理成适合求职和面试的表达。\n\n"
                "路线应该根据你现有基础决定从哪里切入，而不是默认从零开始。"
                "现在先不安排每天做什么，我们先确认最影响路线的两点。"
            )
        if any(word in normalized for word in ("项目", "开发", "产品", "上线")):
            return (
                "我先说说对这个目标的初步理解。\n\n"
                "一个可执行的项目目标通常需要同时明确：要解决的问题、当前基线、"
                "可以验收的产出，以及现实中的时间和资源限制。已有的技术路线会尽量保留，"
                "我们主要补齐会影响执行的空白。\n\n"
                "现在先不拆每日任务，我们先确认最影响路线的两点。"
            )
        return (
            "我先说说对这个目标的初步理解。\n\n"
            "要把一个想法变成可以推进的规划，通常需要分清最终结果、当前起点、"
            "可用资源和完成标准。你已经明确的部分会直接保留，不会要求重新回答；"
            "不确定的部分再由我们逐步补齐。\n\n"
            "现在先不安排每天做什么，我们先确认最影响路线的两点。"
        )
