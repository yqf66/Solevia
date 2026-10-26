"""Read-only conversation attachments and explicitly requested output copies.

This service never opens a source path for writing and exposes no shell or
permission escalation. Consent belongs to one durable user message, not a model.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from planner_api.database import SessionLocal
from planner_api.models import Conversation, Message

from .conversation_file_store import ConversationFileStore

_service = None


def bind_file_service(service):
    global _service
    _service = service


def bind_file_message(context, message):
    if context and context.get("goal_id"):
        context["user_message_id"] = message.id


def request_file_copy(context):
    if _service is None:
        raise ValueError("文件生成服务尚未连接。")
    return _service.propose(context)


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                "required": ["name", "content"],
            },
        },
    },
    "required": ["summary", "files"],
}
EXTENSIONS = {
    ".md",
    ".txt",
    ".csv",
    ".json",
    ".html",
    ".xml",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".ts",
    ".css",
    ".sql",
}


def validate_files(value):
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
        raise ValueError("文件结果缺少说明。")
    files = value.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 4:
        raise ValueError("每次需要生成 1 至 4 个完整文件。")
    names, size = set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("文件结构不完整。")
        name, content = item.get("name"), item.get("content")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 120
            or re.search(r'[\\/<>:"|?*\x00-\x1f]', name)
            or name.startswith(".")
            or name.endswith((".", " "))
            or Path(name).suffix.lower() not in EXTENSIONS
            or re.match(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", name, re.I)
            or name.casefold() in names
        ):
            raise ValueError("请使用不带路径的普通文本文件名，且名称不能重复。")
        if not isinstance(content, str) or not content.strip() or "\0" in content:
            raise ValueError("文件正文为空或不是可显示的文本。")
        length = len(content.encode("utf-8"))
        if length > 256_000:
            raise ValueError("单个文件超过 256 KB，请缩小本次修改范围。")
        size += length
        names.add(name.casefold())
    if size > 512_000:
        raise ValueError("本次文件总量超过 512 KB，请拆分任务。")
    return {"summary": value["summary"][:2000], "files": files}


class ConversationFileService:
    def __init__(self, directory, runtime, *, session_factory=SessionLocal, generator=None):
        self.store = ConversationFileStore(Path(directory))
        self.runtime = runtime
        self.session_factory = session_factory
        self.generator = generator or self._generate
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="solevia-file-copy")
        for task in self.store.list("file_copy"):
            if task["status"] in {"QUEUED", "RUNNING"}:
                self.store.update(
                    task["id"], status="INTERRUPTED", error="服务已重启，可重新生成。"
                )

    def stop(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.store.db.close()

    def _check_context(self, context):
        from planner_api.materials.reading import MaterialReadingService

        with self.session_factory() as session:
            message = session.get(Message, context.get("user_message_id"))
            conversation = session.get(Conversation, message.conversation_id) if message else None
            sources = context.get("source_ids") or []
            if (
                not message
                or message.role != "USER"
                or not conversation
                or conversation.goal_id != context.get("goal_id")
                or message.content.strip() != context.get("query", "").strip()
                or not sources
                or set(sources) != set((message.metadata_json or {}).get("material_source_ids", []))
            ):
                raise ValueError("本次文件请求与原对话附件不匹配，请重新选择文件。")
            revisions = {
                row["source_id"]: row["revision_id"] for row in context.get("revisions", [])
            }
            if set(revisions) != set(sources):
                raise ValueError("文件版本不完整，请重新选择文件。")
            for source in sources:
                # Revalidate the live goal link and the exact selected revision.
                MaterialReadingService(session).read(
                    conversation.goal_id,
                    source,
                    revision_id=revisions[source],
                    offset=0,
                    limit=1,
                    outline=False,
                )

    def propose(self, context):
        self._check_context(context)
        with self.lock:
            existing = next(
                (
                    row
                    for row in self.store.list("file_copy", context["goal_id"])
                    if row["user_message_id"] == context["user_message_id"]
                ),
                None,
            )
            if existing is None:
                if len(self.store.list("file_copy", context["goal_id"])) >= 100:
                    raise ValueError("这个目标的文件结果已达上限，请新建目标。")
                existing = self.store.create(
                    "file_copy",
                    context["goal_id"],
                    goal_id=context["goal_id"],
                    user_message_id=context["user_message_id"],
                    context=context,
                    status="NEEDS_CONFIRMATION",
                    reason="",
                    error="",
                    files=[],
                    summary="",
                )
        return {
            "request_id": existing["id"],
            "status": existing["status"],
            "message": "请在回复中询问用户是否允许生成修改版，并等待下方允许/拒绝操作。原文件保持只读；此时还没有生成文件。",
        }

    def for_answer(self, message_id):
        with self.session_factory() as session:
            answer = session.get(Message, message_id)
            if not answer or answer.role != "ASSISTANT" or answer.generation_status != "SUCCEEDED":
                return []
            conversation = session.get(Conversation, answer.conversation_id)
            user = session.scalar(
                select(Message)
                .where(
                    Message.conversation_id == answer.conversation_id,
                    Message.role == "USER",
                    Message.sequence < answer.sequence,
                )
                .order_by(Message.sequence.desc())
                .limit(1)
            )
            if not conversation or not user:
                return []
            return [
                self.public(row)
                for row in self.store.list("file_copy", conversation.goal_id)
                if row["user_message_id"] == user.id
            ]

    @staticmethod
    def public(task):
        return {
            key: task[key]
            for key in ("id", "goal_id", "user_message_id", "status", "reason", "error", "summary")
        } | {
            "source_names": [row["title"] for row in task["context"].get("sources", [])],
            "files": [
                {
                    "index": index,
                    "name": item["name"],
                    "bytes": len(item["content"].encode("utf-8")),
                }
                for index, item in enumerate(task["files"])
            ],
        }

    def decide(self, goal_id, task_id, decision, reason=""):
        with self.lock:
            task = self.store.get(task_id, "file_copy", goal_id)
            if decision not in {"ALLOW", "DENY"}:
                raise ValueError("请选择允许或拒绝。")
            if task["status"] in {"DENIED", "SUCCEEDED", "RUNNING", "QUEUED"}:
                return self.public(task)
            if decision == "DENY":
                return self.public(
                    self.store.update(task_id, status="DENIED", reason=reason.strip()[:1000])
                )
            self._check_context(task["context"])
            if time.time() - task["created"] > 86400:
                raise ValueError("这次确认已超过一天，请在对话中重新提出文件需求。")
            from planner_api.config import get_settings
            from .preferences import generation_timeout

            settings = get_settings()
            task = self.store.update(
                task_id,
                status="QUEUED",
                error="",
                model=settings.llm_model,
                effort=settings.codex_reasoning_effort,
                wait_seconds=generation_timeout(settings),
            )
            self.executor.submit(self._run, task_id)
            return self.public(task)

    def _run(self, task_id):
        try:
            task = self.store.update(task_id, status="RUNNING")
            self._check_context(task["context"])
            result = validate_files(self.generator(task))
            self._check_context(task["context"])
            # Publish all complete, validated files together; never partial drafts.
            self.store.update(task_id, status="SUCCEEDED", **result)
        except Exception:
            self.store.update(
                task_id,
                status="FAILED",
                files=[],
                error="修改版暂时没有生成完成，可重试；原文件仍保持不变。",
            )

    def _generate(self, task):
        from .materials import material_tools
        from .reliability import retryable
        from urllib.error import URLError

        context = {key: value for key, value in task["context"].items() if key != "user_message_id"}
        deadline = time.monotonic() + task["wait_seconds"]
        error = ""
        for attempt in range(2):
            tools = material_tools(context, session_factory=self.session_factory)
            read_ranges = {}
            read_lock = threading.Lock()

            def read(
                name,
                arguments,
                handler=tools["solevia_tool_handler"],
                ranges=read_ranges,
                guard=read_lock,
            ):
                result = handler(name, arguments)
                if name == "solevia_read_material":
                    start, end = result["offset"], result["offset"] + len(result["text"])
                    with guard:
                        state = ranges.setdefault(
                            result["source_id"], {"total": result["total_chars"], "ranges": []}
                        )
                        state["ranges"].append((start, end))
                return result

            payload = {
                "model": task["model"],
                "reasoning_effort": task["effort"],
                "solevia_schema": OUTPUT_SCHEMA,
                "instructions": "用户已在原对话允许本次生成文件修改版。先用资料工具读取选定版本，按用户原话完成修改。"
                "只生成独立的 UTF-8 文本文件，原资料保持只读，不运行命令，不联网。"
                "不声称修改了本地原件或保存了规划。保留原文中用户未要求改变的内容。"
                "Office/PDF 资料若无法保留原格式，返回 .md 或 .txt 并在 summary 明确说明。"
                "无法读取充分正文时不要用摘要冒充完整修改版，应报错。只返回符合 Schema 的完整 JSON。",
                "input": json.dumps(
                    {
                        "user_request": context["query"],
                        "material_context": context,
                        "output_schema": OUTPUT_SCHEMA,
                        "validation_feedback": error,
                    },
                    ensure_ascii=False,
                ),
                **tools,
                "solevia_tool_handler": read,
            }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("文件生成超时")
            try:
                text = self.runtime.generate(payload, remaining)
                for source in context["source_ids"]:
                    state = read_ranges.get(source, {})
                    end = 0
                    for start, stop in sorted(state.get("ranges", [])):
                        if start > end:
                            break
                        end = max(end, stop)
                    if source not in read_ranges or end < state.get("total", 1):
                        raise ValueError(
                            "生成修改版前必须完整读取所选原文；不能用片段或摘要代替全文。"
                        )
                return validate_files(json.loads(text))
            except (ValueError, TypeError, URLError, TimeoutError, OSError) as exc:
                error = (
                    str(exc)[:500]
                    if isinstance(exc, (ValueError, TypeError))
                    else "上次连接未完成，请在剩余时间内重试。"
                )
                if attempt == 1 or not retryable(exc):
                    raise
        raise ValueError("文件结果不完整")

    def content(self, goal_id, task_id, index):
        task = self.store.get(task_id, "file_copy", goal_id)
        if task["status"] != "SUCCEEDED" or not 0 <= index < len(task["files"]):
            raise ValueError("文件尚未生成完成。")
        return task["files"][index]
