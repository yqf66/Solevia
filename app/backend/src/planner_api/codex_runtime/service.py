from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .protocol import CodexAppServer, CodexUnavailable
from .store import ChatStore, TERMINAL


class CodexService:
    def __init__(self, *, data_dir: Path, workspace: Path, server_factory=CodexAppServer):
        self.workspace = workspace.resolve()
        self.store = ChatStore(data_dir / "solevia-chat.sqlite3")
        self.server_factory = server_factory
        self.server: CodexAppServer | None = None
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="solevia-codex")
        self.lock = threading.RLock()
        self.threads: dict[str, str] = {}
        self.runs_by_thread: dict[str, str] = {}
        self.items: dict[str, dict[str, str]] = {}
        self.requests: dict[str, Any] = {}
        self.done: dict[str, threading.Event] = {}
        self.cancelled: set[str] = set()
        self.resumed: set[str] = set()
        self.orphan_turns: set[str] = set()
        self.retired_turns: set[str] = set()
        self.interruption_reasons: dict[str, str] = {}
        self.probe: dict[str, Any] = {"models": [], "skills": [], "account": None}
        self.start_error: str | None = None
        self.stopping = False

    def start(self) -> None:
        try:
            from .workspace import prepare_workspace
            prepare_workspace(self.workspace)
            self.server = self.server_factory(cwd=self.workspace, on_event=self.on_event, on_request=self.on_request, on_disconnect=self.disconnected)
            self.server.start()
            self.probe = self.probe_runtime()
        except Exception as exc:
            self.start_error = str(exc)
            if self.server:
                self.server.stop()

    def stop(self) -> None:
        self.stopping = True
        if self.server:
            self.server.stop()
        with self.lock:
            for run_id in list(self.done):
                self.finish(run_id, "interrupted", "本地服务正在关闭。")
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.store.close()

    def probe_runtime(self) -> dict[str, Any]:
        assert self.server
        result: dict[str, Any] = {"models": [], "skills": [], "account": None}
        for method, params in (("model/list", {}), ("skills/list", {"cwds": [str(self.workspace)], "forceReload": True}), ("account/read", {})):
            try:
                value = self.server.call(method, params, timeout=10)
                if method == "model/list":
                    result["models"] = [{key: model.get(key) for key in ("id", "model", "displayName", "isDefault")} for model in value.get("data", [])]
                elif method == "skills/list":
                    skills = [skill for entry in value.get("data", []) for skill in entry.get("skills", [])]
                    result["skills"] = [{"name": skill.get("name"), "enabled": skill.get("enabled", True), "local": str(self.workspace) in str(skill.get("path", ""))} for skill in skills]
                    result["local_skills"] = [skill["name"] for skill in result["skills"] if skill["local"] and skill["enabled"]]
                else:
                    account = value.get("account")
                    result["account"] = {"type": account.get("type"), "planType": account.get("planType")} if isinstance(account, dict) else None
            except Exception as exc:
                result.setdefault("errors", {})[method] = str(exc)
        return result

    def status(self) -> dict[str, Any]:
        process = self.server.process if self.server else None
        return {
            "runtime": "codex-app-server",
            "connected": bool(self.server and self.server.initialized and process and process.poll() is None),
            "workspace": str(self.workspace),
            "probe": self.probe,
            "error": self.start_error,
        }

    def send(self, session_id: str, content: str, client_request_id: str) -> dict[str, Any]:
        if not content.strip():
            raise ValueError("消息不能为空。")
        run, duplicate = self.store.create_run(session_id, content.strip(), client_request_id)
        if not duplicate:
            with self.lock:
                self.done[run["id"]] = threading.Event()
            self.executor.submit(self.run, run["id"], session_id, content.strip())
        return run

    def run(self, run_id: str, session_id: str, content: str) -> None:
        try:
            if self.stopping or not self.server or not self.server.initialized:
                raise CodexUnavailable(self.start_error or "Codex app-server 不可用。")
            if run_id in self.cancelled:
                self.finish(run_id, "interrupted", "用户停止了这次回答。")
                return
            session = self.store.session(session_id)
            thread_id = session.get("thread_id")
            if thread_id in self.orphan_turns:
                raise RuntimeError("上一轮尚未确认停止，请重启 Codex 支线服务后继续，避免重叠执行。")
            params: dict[str, Any] = {
                "cwd": str(self.workspace), "approvalPolicy": "on-request", "sandbox": "read-only",
                "developerInstructions": (
                    "你在 Solevia 内为用户处理业务问题。每轮使用 sole-via-assistant 技能判断用户意图，"
                    "需要时选择 sole-via-planning 或 sole-via-research。不要修改代码、配置、数据库或本地文件。"
                    "首版只能输出规划草案，还不能向 Solevia 计划表写入；不要声称已创建或确认业务计划。"
                    "提供简洁回答和实际来源，不展示内部推理；检索到片段不能声称已读正文。"
                ),
            }
            if thread_id and thread_id not in self.resumed:
                self.server.call("thread/resume", {**params, "threadId": thread_id}, timeout=30)
            elif not thread_id:
                result = self.server.call("thread/start", params, timeout=30)
                thread_id = result["thread"]["id"]
                self.store.set_thread(session_id, thread_id)
            self.resumed.add(thread_id)
            with self.lock:
                self.threads[run_id] = thread_id
                self.runs_by_thread[thread_id] = run_id
            if run_id in self.cancelled:
                self.finish(run_id, "interrupted", "用户停止了这次回答。")
                return
            self.store.update_run(run_id, status="running")
            self.store.append_event(run_id, "run_started", {"thread_id": thread_id})
            master = self.workspace / ".agents/skills/solevia-assistant/SKILL.md"
            inputs = [{"type": "text", "text": content}]
            if master.is_file():
                inputs.append({"type": "skill", "name": "sole-via-assistant", "path": str(master)})
            result = self.server.call("turn/start", {
                "threadId": thread_id, "input": inputs, "cwd": str(self.workspace),
                "approvalPolicy": "on-request", "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "effort": os.environ.get("SOLEVIA_CODEX_EFFORT", "low"),
            }, timeout=30)
            turn_id = result["turn"]["id"]
            self.store.update_run(run_id, turn_id=turn_id)
            if run_id in self.cancelled:
                self.interrupt(run_id)
            if not self.done[run_id].wait(float(os.environ.get("SOLEVIA_CODEX_TURN_TIMEOUT", "600"))):
                self.interruption_reasons[run_id] = "Codex 回答等待超时，已请求中断并保留收到的内容。"
                self.interrupt(run_id)
                if not self.done[run_id].wait(10):
                    self.orphan_turns.add(thread_id)
                    self.finish(run_id, "failed", "Codex 回答超时且尚未确认停止，请重启服务后继续；已保留收到的内容。")
        except Exception as exc:
            thread = self.threads.get(run_id)
            if thread and not self.done[run_id].is_set():
                self.orphan_turns.add(thread)
            self.finish(run_id, "failed", str(exc))
        finally:
            with self.lock:
                thread = self.threads.get(run_id)
                if thread and self.runs_by_thread.get(thread) == run_id:
                    self.runs_by_thread.pop(thread, None)
                self.items.pop(run_id, None)
                self.threads.pop(run_id, None)
                self.cancelled.discard(run_id)
                self.done.pop(run_id, None)
                self.interruption_reasons.pop(run_id, None)
                for request_id in list(self.requests):
                    if not self.store.pending_request(request_id):
                        self.requests.pop(request_id, None)

    def interrupt(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        thread = self.threads.get(run_id)
        if self.server and thread and run.get("turn_id"):
            self.server.call("turn/interrupt", {"threadId": thread, "turnId": run["turn_id"]}, timeout=10)

    def cancel(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run["status"] in TERMINAL:
            return
        self.cancelled.add(run_id)
        self.interruption_reasons[run_id] = "用户停止了这次回答。"
        if run.get("turn_id"):
            self.interrupt(run_id)
        self.store.append_event(run_id, "cancel_requested", {})

    def respond(self, request_id: str, answer: dict[str, Any]) -> None:
        record = self.store.pending_request(request_id)
        if not record or request_id not in self.requests or not self.server:
            raise ValueError("此请求已处理或已失去连接，请刷新当前对话。")
        method = record["method"]
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            if answer.get("decision") not in ("accept", "decline"):
                raise ValueError("请选择允许本次或拒绝。")
            payload = {"decision": answer["decision"]}
        elif method == "item/permissions/requestApproval":
            # Permission profile editing is not exposed in this first UI.
            payload = {"permissions": {}, "scope": "turn"}
        elif method == "item/tool/requestUserInput":
            questions = json.loads(record["payload"]).get("questions", [])
            answers = answer.get("answers", {})
            if set(answers) != {q["id"] for q in questions}:
                raise ValueError("请回答每一个问题。")
            payload = {"answers": answers}
        else:
            raise ValueError("此类请求暂未支持；请停止本次回答。")
        self.server.respond(self.requests[request_id], payload)
        self.store.complete_request(request_id)
        self.requests.pop(request_id, None)

    def run_for(self, params: dict[str, Any]) -> str | None:
        thread = params.get("threadId")
        run_id = self.runs_by_thread.get(thread)
        if run_id:
            run = self.store.get_run(run_id)
            incoming_turn = params.get("turnId") or params.get("turn", {}).get("id")
            if incoming_turn in self.retired_turns:
                return None
            if run["status"] not in TERMINAL and (not incoming_turn or not run["turn_id"] or incoming_turn == run["turn_id"]):
                return run_id
        return None

    def on_request(self, message: dict[str, Any]) -> None:
        method, params = message.get("method", ""), message.get("params", {})
        run_id = self.run_for(params)
        if not run_id:
            if self.server:
                self.server.reject(message["id"], "Solevia cannot handle this request outside an active turn.")
            return
        if method == "item/fileChange/requestApproval":
            if self.server:
                self.server.respond(message["id"], {"decision": "decline"})
            self.store.append_event(run_id, "notice", {"text": "首版对话工作区不支持文件修改，已拒绝该请求。"})
            return
        if method not in ("item/commandExecution/requestApproval", "item/permissions/requestApproval", "item/tool/requestUserInput"):
            if self.server:
                self.server.reject(message["id"], "This request type is not supported by Solevia yet.")
            self.store.append_event(run_id, "notice", {"text": "Codex 请求了一项尚未接入的交互，已拒绝。"})
            return
        request_id = self.store.request(run_id, method, params)
        self.requests[request_id] = message["id"]

    def on_event(self, message: dict[str, Any]) -> None:
        params, method = message.get("params", {}), message.get("method", "")
        run_id = self.run_for(params)
        if not run_id:
            return
        if method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId", "answer"), params.get("delta", "")
            parts = self.items.setdefault(run_id, {})
            parts[item_id] = parts.get(item_id, "") + delta
            self.store.answer(run_id, "\n\n".join(parts.values()))
        elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            item = params["item"]
            parts = self.items.setdefault(run_id, {})
            if item.get("phase") == "commentary":
                parts.pop(item["id"], None)
                self.store.append_event(run_id, "notice", {"text": item.get("text", "")})
            else:
                parts[item["id"]] = item.get("text", "")
            self.store.answer(run_id, "\n\n".join(parts.values()))
        elif method == "turn/started":
            turn = params.get("turn", {})
            self.store.update_run(run_id, turn_id=turn.get("id"), status="running")
        elif method == "turn/completed":
            turn = params["turn"]
            status = {"completed": "succeeded", "interrupted": "interrupted"}.get(turn.get("status"), "failed")
            error = turn.get("error")
            reason = self.interruption_reasons.get(run_id) if status == "interrupted" else None
            if reason and run_id not in self.cancelled:
                status = "failed"
            self.finish(run_id, status, reason or (error.get("message") if isinstance(error, dict) else str(error) if error else None))
        elif method in ("item/started", "item/completed"):
            item = params.get("item", {})
            kind = item.get("type")
            if kind == "webSearch":
                self.store.append_event(run_id, "research", {"item_id": item.get("id"), "stage": method, "query": item.get("query"), "action": item.get("action"), "successful_reads": None})
            elif kind in ("commandExecution", "fileChange", "mcpToolCall"):
                self.store.append_event(run_id, "tool", {key: item.get(key) for key in ("id", "type", "command", "name", "status")})
        elif method == "error":
            error = params.get("error", {})
            self.store.append_event(run_id, "notice", {"text": error.get("message", "Codex 正在重试。")})
        # Reasoning and raw model context never enter the browser event stream.

    def finish(self, run_id: str, status: str, error: str | None = None) -> None:
        turn_id = self.store.get_run(run_id).get("turn_id")
        if turn_id:
            self.retired_turns.add(turn_id)
        self.store.finish(run_id, status, error)
        event = self.done.get(run_id)
        if event:
            event.set()

    def disconnected(self) -> None:
        with self.lock:
            for run_id, event in list(self.done.items()):
                if not event.is_set():
                    self.finish(run_id, "interrupted", "Codex app-server 连接已关闭，已保留收到的回答。")
