"""Codex-specific execution strategy over the shared Solevia contracts."""
import json

from planner_api.ai import (
    OpenAICompatibleClarificationProvider, _context_payload, _research_reply_instructions,
    _guard_research_reply,
    _parse_conversation_text,
)
from planner_api.model_telemetry import model_node
from planner_api.plan_ai import OpenAICompatiblePlanProvider, _validate_plan_response
from planner_api.plan_prompts import HYBRID_PLAN_OUTLINE_AUTHOR_INSTRUCTIONS, HYBRID_PLAN_DETAIL_AUTHOR_INSTRUCTIONS
from planner_api.route_ai import OpenAICompatibleRouteProvider, _route_input, ROUTE_INSTRUCTIONS, ROUTE_COMPILER_INSTRUCTIONS
from planner_api.schemas import ConversationReply, RouteDraftResult
from .reliability import validated_generation
from .materials import material_tools


def with_document(result):
    """Render the accepted structure locally; no second author/model pass."""
    data = result.model_dump(mode="json", exclude={"compiler_report", "source_document"})
    document = result.source_document or json.dumps(data, ensure_ascii=False, indent=2)
    return result.model_copy(update={
        "source_document": document,
        "compiler_report": {"status": "SCHEMA_VALIDATED", "runtime": "codex",
                            "strategy": "DIRECT_STRUCTURED", "business_validation": "PENDING"},
    })


class CodexClarificationProvider(OpenAICompatibleClarificationProvider):
    @model_node('codex.Clarification.respond')
    def respond(self, context):
        payload = {
            "model": self.model,
            "instructions": _research_reply_instructions(self, context) + "\n当前输出格式为普通文本。直接输出给用户的回答，不要 JSON 对象，不要 assistant_message 字段。",
            "input": json.dumps(_context_payload(context), ensure_ascii=False),
            "solevia_visible_text": True,
            "text": {"format": {"type": "text"}},
        }
        research = context.research_context or {}
        coverage = research.get('research_coverage') or {}
        if coverage.get('read_pages', 0) and research.get('research_status') != 'READY':
            payload['instructions'] += (
                '\n宿主已经取得部分网页正文，证据选择或覆盖核验仍可能不完整。'
                '说明实际缺口，不要误称没有联网工具或从未读到正文；未核验的结论不能当成已证实。'
            )
        payload.update(material_tools(context.material_context))
        def validate(response):
            parsed = _parse_conversation_text(self._extract_message_content(response))
            reply = parsed if isinstance(parsed, ConversationReply) else ConversationReply(assistant_message=parsed)
            return _guard_research_reply(self, context, reply)
        return validated_generation(self, payload, validate)


class CodexRouteProvider(OpenAICompatibleRouteProvider):
    def draft_route(self, context):
        schema = RouteDraftResult.model_json_schema()
        payload = {"model": self.model,
                   "instructions": ROUTE_COMPILER_INSTRUCTIONS if context.source_document else ROUTE_INSTRUCTIONS,
                   "input": _route_input(context), "solevia_schema": schema,
                   "text": {"format": {"type": "json_schema", "schema": schema}}}
        result = validated_generation(self, payload, lambda response: RouteDraftResult.model_validate_json(self._extract_content(response)))
        return with_document(result)


class CodexPlanProvider(OpenAICompatiblePlanProvider):
    requires_sequential_details = True

    def _generate(self, schema_model, _schema_name, instructions, input_text,
                  max_output_tokens, generation_mode, dependency_registry=None):
        # The shared Hybrid compiler instructions expect an author's draft.
        # Codex performs both responsibilities in one validated structured call.
        if _schema_name in {'hybrid_plan_outline', 'hybrid_plan_details'}:
            author = HYBRID_PLAN_OUTLINE_AUTHOR_INSTRUCTIONS if _schema_name == 'hybrid_plan_outline' else HYBRID_PLAN_DETAIL_AUTHOR_INSTRUCTIONS
            instructions = ('你直接完成规划设计与结构化输出，不存在上游作者稿；不要等待 source_document。'
                            '综合下面设计规则和字段约束，只返回当前 Schema 的完整 JSON。'
                            'JSON 字符串内换行、制表符及双引号必须正确转义，禁止直接嵌入控制字符。\n' + author + '\n' + instructions)
        schema = schema_model.model_json_schema()
        payload = {"model": self.model, "instructions": instructions, "input": input_text,
                   "solevia_schema": schema, "text": {"format": {"type": "json_schema", "schema": schema}}}
        # The service owns retries once it has supplied business-validation feedback.
        try:
            context = json.loads(input_text)
        except (ValueError, TypeError):
            context = {}
        def has_feedback(value):
            if isinstance(value, dict):
                return bool(value.get('generation_validation_feedback')) or any(has_feedback(row) for row in value.values())
            return isinstance(value, list) and any(has_feedback(row) for row in value)
        attempts = 1 if has_feedback(context) else 3
        result = validated_generation(self, payload,
            lambda response: _validate_plan_response(schema_model, self._extract_content(response), dependency_registry),
            attempts=attempts)
        return with_document(result)
