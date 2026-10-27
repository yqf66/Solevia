"""Adapt Solevia's validated model requests to the local Codex runtime.

Business services keep ownership of persistence and validation. Each bounded
model call gets an isolated thread, so parallel goals cannot share context.
"""
from __future__ import annotations

import json
import copy
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError

from .protocol import CodexAppServer, CodexUnavailable
from .failures import CodexFailure, classify_failure, retry_delay

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def protocol_output_schema(schema):
    """Adapt Pydantic defaults to Codex's strict response-format subset.

    Open dictionaries cannot be represented by that subset. They remain in the
    model input and are validated by the unchanged business schema afterwards.
    """
    if not schema:
        return None
    result = copy.deepcopy(schema)
    def visit(node):
        if isinstance(node, list):
            return all(visit(value) for value in node)
        if not isinstance(node, dict):
            return True
        if node.get('type') == 'object' or 'properties' in node:
            if node.get('additionalProperties', False) is not False:
                return False
            node['additionalProperties'] = False
            node['required'] = list(node.get('properties', {}))
        return all(visit(value) for value in node.values())
    return result if visit(result) else None


@dataclass
class PendingTurn:
    done: threading.Event = field(default_factory=threading.Event)
    turn_id: str | None = None
    parts: dict[str, str] = field(default_factory=dict)
    phases: dict[str, str | None] = field(default_factory=dict)
    error: Any = None
    tool_handler: Any = None
    completed: bool = False
    events: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    stage: str = 'STARTING'
    effort: str = 'low'
    model: str = ''
    material_reads: int = 0
    denied_tools: int = 0
    timeout_seconds: float = 0
    host_operation_requested: bool = False


class BusinessCodexRuntime:
    def __init__(self, workspace: Path, *, server_factory=CodexAppServer):
        self.workspace = workspace
        self.server = server_factory(cwd=workspace, on_event=self.on_event,
                                     on_request=self.on_request, on_disconnect=self.disconnected)
        self.pending: dict[str, PendingTurn] = {}
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.start_error: str | None = None
        self.connection_error: str | None = None
        self.connection_error_code: str | None = None
        self.connected = False
        self.queued = 0
        self.retry_waits: dict[int, dict] = {}
        self.reconnect_lock = threading.Lock()
        self.was_connected = False
        self.connection_epoch = 0
        self.stopping = False
        self.sleeper = time.sleep

    def generation_status(self):
        now = time.monotonic()
        with self.lock:
            return {'connected': self.connected, 'queued_count': self.queued,
                    'retries': [dict({key: value for key, value in row.items() if key != 'retry_at'}, wait_seconds=max(0, round(row['retry_at'] - now)))
                                for row in self.retry_waits.values()],
                    'active_count': len(self.pending), 'requests': [
                        {'stage': pending.stage, 'elapsed_seconds': round(now - pending.started),
                         'last_activity_seconds': round(now - pending.last_activity),
                         'reasoning_effort': pending.effort, 'model': pending.model,
                         'material_reads': pending.material_reads, 'denied_tools': pending.denied_tools,
                         'wait_seconds': pending.timeout_seconds}
                        for pending in self.pending.values() if not pending.done.is_set()]}

    def start(self):
        # Alembic logging setup can disable module loggers during startup.
        logger.disabled = False
        self.stopping = False
        self.connected = False
        self.start_error = None
        self.connection_error = "Codex 尚未连接，请先完成本机安装与登录。"
        self.connection_error_code = "CODEX_START_FAILED"
        try:
            self.server.start()
            account = self.server.call("account/read", {}, timeout=15)
            if not account.get("account") and account.get("requiresOpenaiAuth") is not False:
                self.connection_error = "已找到 Codex，但尚未登录。请先在 Codex 完成登录，再点击「检测并连接」。"
                raise CodexUnavailable(self.connection_error, code="CODEX_LOGIN_REQUIRED")
            self.connected = True
            self.was_connected = True
            self.connection_epoch += 1
            self.connection_error = None
            self.connection_error_code = None
        except Exception as exc:
            self.start_error = str(exc)
            self.server.stop()
            code = getattr(exc, "code", "CODEX_START_FAILED") if isinstance(exc, CodexUnavailable) else "CODEX_START_FAILED"
            if isinstance(exc, FileNotFoundError):
                code = "CODEX_NOT_FOUND"
            elif isinstance(exc, PermissionError):
                code = "CODEX_ACCESS_DENIED"
            elif isinstance(exc, TimeoutError):
                code = "CODEX_START_TIMEOUT"
            elif code in {'CODEX_START_FAILED', 'CODEX_UNAVAILABLE'}:
                classified = classify_failure(exc)
                if classified.code != 'CODEX_GENERATION_UNAVAILABLE':
                    code = classified.code
            messages = {
                "CODEX_NOT_FOUND": "尚未找到本机 Codex。请先安装 Codex，再点击「检测并连接」。",
                "CODEX_LOGIN_REQUIRED": "已找到 Codex，但尚未登录。请先在 Codex 完成登录，再点击「检测并连接」。",
                "CODEX_ACCESS_DENIED": "系统不允许启动 Codex。请检查程序访问权限或安全软件的拦截记录，再点击「检测并连接」。",
                "CODEX_START_TIMEOUT": "Codex 启动或登录状态检查超时。请先确认 Codex 本身能正常打开，再点击「检测并连接」。",
            }
            self.connection_error_code = code
            from .failures import failure_for_code
            failure = failure_for_code(code)
            self.connection_error = messages.get(code) or (failure.message if failure else "Codex 本机连接未建立。请先确认 Codex 能正常运行并更新到可用版本，再点击「检测并连接」；这不代表模型无法生成。")

    def stop(self):
        self.stopping = True
        with self.reconnect_lock:
            self.stopping = True
            self.server.stop()
            self.disconnected()

    def disconnected(self):
        if self.connected:
            self.connection_error_code = "CODEX_DISCONNECTED"
            self.connection_error = "Codex 本机连接已中断。请点击「检测并连接」，再查看连接状态。"
        self.connected = False
        with self.lock:
            for pending in self.pending.values():
                pending.error = CodexUnavailable("Codex 连接已关闭。", code="CODEX_DISCONNECTED")
                pending.done.set()

    def on_event(self, message):
        params = message.get("params", {})
        with self.lock:
            pending = self.pending.get(params.get("threadId"))
            if pending is None or pending.done.is_set():
                return
            method = message.get("method")
            event_turn = params.get('turnId') or (params.get('turn') or {}).get('id')
            if event_turn and pending.turn_id and event_turn != pending.turn_id:
                return
            pending.last_activity = time.monotonic()
            if method == "turn/started":
                pending.turn_id = params["turn"]["id"]
                pending.stage = 'RUNNING'
            elif method == 'item/started' and params.get('item', {}).get('type') == 'reasoning':
                pending.stage = 'THINKING'
            elif method == "item/started" and params.get("item", {}).get("type") == "agentMessage":
                item = params["item"]
                pending.phases[item["id"]] = item.get("phase")
            elif method == "item/agentMessage/delta":
                item = params["itemId"]
                if pending.phases.get(item) == "commentary":
                    return
                pending.stage = 'GENERATING'
                pending.parts[item] = pending.parts.get(item, "") + params.get("delta", "")
            elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                item = params["item"]
                if item.get("phase") == "commentary":
                    pending.parts.pop(item["id"], None)
                else:
                    pending.parts[item["id"]] = item.get("text", "")
                    pending.phases[item["id"]] = item.get("phase")
            elif method == "turn/completed":
                pending.completed = True
                turn = params["turn"]
                if turn.get("status") != "completed":
                    error = turn.get("error") or {}
                    pending.error = error or {"message": "Codex 本次生成未完成。", "status": turn.get("status")}
                pending.done.set()
                logger.info('codex_turn_completed status=%s elapsed_seconds=%.3f model=%s effort=%s material_reads=%s denied_tools=%s',
                            turn.get('status'), time.monotonic() - pending.started, pending.model,
                            pending.effort, pending.material_reads, pending.denied_tools)
            elif method == "error":
                error = params.get("error") or {}
                # Some versions retry their own stream. Keep waiting for that
                # turn instead of starting a duplicate request at the host.
                if params.get("willRetry") is True:
                    pending.stage = 'RETRYING'
                else:
                    pending.error = error or {"message": "Codex 本次生成未完成。"}
                    pending.done.set()
            elif method == "item/completed":
                pending.events.append(params.get("item", {}))

    def on_request(self, message):
        # Model nodes cannot authorize operations on the user's behalf.
        method = message.get("method", "")
        if method == "item/tool/call":
            # Tool handlers may read the database. Do not block the JSONL reader.
            threading.Thread(target=self._tool_call, args=(message,), daemon=True).start()
        elif method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            self.server.respond(message["id"], {"decision": "decline"})
        elif method == "item/permissions/requestApproval":
            self.server.respond(message["id"], {"permissions": {}, "scope": "turn"})
        else:
            self.server.reject(message["id"], "Return clarification in the required business schema.")

    def _tool_call(self, message):
        params = message.get("params", {})
        pending = None
        try:
            with self.lock:
                pending = self.pending.get(params.get("threadId"))
                if pending is None or pending.done.is_set() or pending.tool_handler is None:
                    raise ValueError("当前请求没有此资料读取授权。")
                if params.get('tool') not in {'solevia_list_materials', 'solevia_read_material'}:
                    pending.host_operation_requested = True
            result = pending.tool_handler(params.get("tool"), params.get("arguments", {}))
            with self.lock:
                if params.get('tool') == 'solevia_read_material':
                    pending.material_reads += 1
            response = {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception:
            with self.lock:
                if pending is not None:
                    pending.denied_tools += 1
            response = {"success": False, "contentItems": [{"type": "inputText", "text": "资料不可读取：请检查当前目标授权、版本和读取范围。"}]}
        try:
            self.server.respond(message["id"], response)
        except (RuntimeError, OSError):
            pass

    def generate(self, payload: dict[str, Any], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        key = id(threading.current_thread())
        reconnected = False
        try:
            for attempt in range(3):
                request_epoch = self.connection_epoch
                try:
                    if not self.connected and reconnected:
                        raise CodexFailure('CODEX_DISCONNECTED').as_error()
                    reconnected = self._reconnect_if_needed() or reconnected
                    request_epoch = self.connection_epoch
                    return self._generate_once(payload, max(0.001, deadline - time.monotonic()))
                except Exception as error:
                    from planner_api.ai import ClarificationProviderError
                    if not isinstance(error, (ClarificationProviderError, CodexUnavailable, URLError, RuntimeError, OSError)):
                        raise
                    failure = classify_failure(error)
                    if self.stopping:
                        raise CodexFailure('CODEX_INTERRUPTED').as_error() from None
                    if failure.code == 'CODEX_DISCONNECTED' and request_epoch == self.connection_epoch:
                        self.disconnected()
                    elif failure.code == 'CODEX_LOGIN_REQUIRED':
                        self.connected = False
                        self.connection_error_code = failure.code
                        self.connection_error = failure.message
                    delay = retry_delay(failure, attempt, deadline - time.monotonic(), jitter=random.random())
                    if not failure.retryable or attempt == 2 or delay is None or (reconnected and failure.code == 'CODEX_DISCONNECTED'):
                        raise failure.as_error() from None
                    with self.lock:
                        self.retry_waits[key] = {'code': failure.code, 'message': failure.message,
                            'attempt': attempt + 2, 'maximum_attempts': 3, 'retry_at': time.monotonic() + delay}
                    self.sleeper(delay)
                    with self.lock:
                        self.retry_waits.pop(key, None)
        finally:
            with self.lock:
                self.retry_waits.pop(key, None)

    def _reconnect_if_needed(self):
        if self.connected:
            return
        # Consent comes from a previously successful connection. Never start a
        # fresh installation or replace a process that still owns other turns.
        if not self.was_connected or self.stopping or self.connection_error_code == 'CODEX_LOGIN_REQUIRED':
            from .failures import failure_for_code
            raise (failure_for_code(self.connection_error_code) or CodexFailure('CODEX_NOT_CONNECTED')).as_error()
        with self.reconnect_lock:
            if self.stopping:
                raise CodexFailure('CODEX_INTERRUPTED').as_error()
            if self.connected:
                return
            with self.lock:
                if self.pending:
                    raise CodexFailure('CODEX_DISCONNECTED').as_error()
            self.server.stop()
            self.start()
            if not self.connected:
                failure = classify_failure(CodexUnavailable(self.connection_error or '', code=self.connection_error_code or 'CODEX_DISCONNECTED'))
                raise failure.as_error()
            return True

    def _generate_once(self, payload: dict[str, Any], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self.lock:
            self.queued += 1
        try:
            acquired = self.slots.acquire(timeout=timeout)
        finally:
            with self.lock:
                self.queued -= 1
        if not acquired:
            raise CodexFailure('CODEX_QUEUE_TIMEOUT').as_error()
        # Waiting for a slot is distinct from the model's inference budget.
        deadline = time.monotonic() + timeout
        thread_id = None
        effort = payload.get("reasoning_effort") or (payload.get("reasoning") or {}).get("effort") or "low"
        pending = PendingTurn(tool_handler=payload.get("solevia_tool_handler"), effort=effort,
                              model=payload.get('model') or '', timeout_seconds=timeout)
        try:
            if not self.connected:
                raise CodexUnavailable("Codex 连接已关闭。", code="CODEX_DISCONNECTED")
            result = self.server.call("thread/start", {
                "cwd": str(self.workspace), "sandbox": "read-only", "approvalPolicy": "on-request",
                **({"model": payload['model']} if payload.get('model') else {}),
                "config": {"web_search": "live",
                           "features.shell_tool": False},
                **({"dynamicTools": payload["solevia_tools"]} if payload.get("solevia_tools") else {}),
                "developerInstructions": (
                    "你是 Solevia 的业务内核。使用 sole-via-workspace 技能，按业务请求选择规划或研究规则。"
                    "当前请求的完整提示词、上下文和输出 Schema 在输入里；严格遵守它们。"
                    "只返回所需结果，不发送进度说明或解释 JSON 格式，不调用文件修改工具。"
                    "只使用本次明确开放的工具；缺失证据保留不确定性。禁止 shell、任意磁盘读取和跨目标资料访问。"
                    "已提供 Codex 原生联网搜索；需要核实时效性或外部事实时自行搜索，无需让用户开启联网开关。"
                    "不因工具可用就声称已搜索或已读正文。只引用本次真实取得的来源与内容，普通回答用可点击来源链接；"
                    "无法核实则明确不确定，不能编造来源或放宽结构与业务校验。"
                    "用户选定附件默认只读。用户明确要求编辑或生成附件修改版时，调用 solevia_request_file_copy 确认工具（如果提供），"
                    "并在本次要求的输出结构内用一句话询问是否允许。等待宿主允许/拒绝选项，不在确认前生成或声称已经编辑文件。"
                    "纯阅读、分析、总结、讨论或只修改规划时不要请求文件确认。"
                    "事实提取节点的 deadline 值只填 YYYY-MM-DD 或 NONE，解释放 rationale/evidence 等说明字段；"
                    "不要在日期值中附带括号、相对日期原话或理由。"
                    "由 Solevia 校验并保存结果，不能声称你已经操作日历、数据库或确认计划。"
                ) + (" 本次是可见对话：输出普通文本，禁止 JSON 包装和 assistant_message 字段。" if payload.get("solevia_visible_text") else ""),
            }, timeout=max(0.1, min(30, deadline - time.monotonic())))
            thread_id = result["thread"]["id"]
            with self.lock:
                self.pending[thread_id] = pending
            inputs = [{"type": "text", "text": json.dumps({
                "instructions": payload.get("instructions"),
                "input": payload.get("input"), "messages": payload.get("messages"),
                "output_format": payload.get("text", payload.get("response_format")),
                "output_schema": payload.get("solevia_schema"),
            }, ensure_ascii=False)}]
            skill = self.workspace / ".agents/skills/solevia-workspace/SKILL.md"
            if skill.is_file():
                inputs.append({"type": "skill", "name": "sole-via-workspace", "path": str(skill)})
            for name in ("planning", "research"):
                selected = self.workspace / f".agents/skills/solevia-{name}/SKILL.md"
                if selected.is_file():
                    inputs.append({"type": "skill", "name": f"sole-via-{name}", "path": str(selected)})
            output_schema = protocol_output_schema(payload.get('solevia_schema'))
            result = self.server.call("turn/start", {
                "threadId": thread_id, "input": inputs, "cwd": str(self.workspace),
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "approvalPolicy": "on-request", "effort": effort,
                **({"outputSchema": output_schema} if output_schema else {}),
            }, timeout=max(0.1, min(30, deadline - time.monotonic())))
            pending.turn_id = result["turn"]["id"]
            while True:
                done = pending.done.wait(min(0.2, max(0, deadline - time.monotonic())))
                with self.lock:
                    events, pending.events = pending.events, []
                if observer := payload.get("solevia_on_item"):
                    for item in events:
                        observer(item)
                if done:
                    break
                if time.monotonic() >= deadline:
                    logger.warning('codex_turn_wait_expired elapsed_seconds=%.3f budget_seconds=%.3f stage=%s effort=%s final_characters=%s completed=%s',
                                   time.monotonic() - pending.started, timeout, pending.stage, effort,
                                   sum(len(value) for value in pending.parts.values()), pending.completed)
                    raise TimeoutError("Codex 生成超时，原有业务数据保持不变。")
            if pending.error:
                raise classify_failure(pending.error).as_error()
            final = [text for key, text in pending.parts.items() if pending.phases.get(key) == "final_answer"]
            text = "\n\n".join(final or pending.parts.values()).strip()
            if not text:
                raise CodexUnavailable("Codex 未返回业务结果。", code='CODEX_EMPTY_RESPONSE')
            return text
        except (CodexUnavailable, RuntimeError, URLError, OSError) as exc:
            failure = classify_failure(exc)
            if pending.host_operation_requested:
                failure = CodexFailure('CODEX_OUTCOME_UNKNOWN')
            raise failure.as_error() from None
        finally:
            if thread_id:
                if not pending.completed and pending.turn_id:
                    try:
                        self.server.call("turn/interrupt", {"threadId": thread_id, "turnId": pending.turn_id}, timeout=5)
                        logger.warning('codex_turn_interrupt_requested effort=%s completed_before_interrupt=%s', effort, pending.completed)
                    except Exception:
                        pass
                with self.lock:
                    self.pending.pop(thread_id, None)
                try:
                    self.server.call("thread/archive", {"threadId": thread_id}, timeout=5)
                except Exception:
                    pass
            self.slots.release()


_runtime: BusinessCodexRuntime | None = None


def bind_runtime(runtime: BusinessCodexRuntime | None):
    global _runtime
    _runtime = runtime


def codex_json_transport(settings):
    from .preferences import generation_timeout
    budget = generation_timeout(settings)
    def transport(_url, _headers, payload, timeout):
        if _runtime is None:
            raise URLError("Solevia Codex 业务内核尚未启动。")
        # Credentials and HTTP endpoints are intentionally never forwarded.
        adapted = {**payload, 'model': settings.llm_model, 'reasoning_effort': settings.codex_reasoning_effort}
        adapted.pop('reasoning', None)
        # Shared providers use either Responses or Chat Completions envelopes.
        output_format = (payload.get('text') or {}).get('format') or payload.get('response_format') or {}
        schema = output_format.get('schema') or (output_format.get('json_schema') or {}).get('schema')
        if schema and not adapted.get('solevia_schema'):
            adapted['solevia_schema'] = schema
        # Only the explicit top-level material context from a business provider
        # can open read tools. Its goal link and pinned revision are rechecked.
        input_text = payload.get('input')
        if not input_text:
            input_text = next((row.get('content') for row in reversed(payload.get('messages') or [])
                               if row.get('role') == 'user'), None)
        try:
            context = json.loads(input_text) if isinstance(input_text, str) else input_text
            if isinstance(context, dict) and context.get('material_context') and not adapted.get('solevia_tools'):
                from .materials import material_tools
                adapted.update(material_tools(context['material_context']))
        except (ValueError, TypeError):
            pass
        text = _runtime.generate(adapted, min(timeout, budget) if payload.get('solevia_budget_remaining') else budget)
        return {
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "output_text": text, "status": "completed",
        }
    transport.codex_timeout_seconds = budget
    transport.codex_reasoning_effort = settings.codex_reasoning_effort
    return transport
