import json
import time

import pytest
from sqlalchemy import select

from planner_api.models import Conversation, Message
from planner_api.materials.worker import MaterialProcessingWorker
from planner_api.materials.analysis_service import MaterialAnalysisService
from planner_api.materials.analysis_ai import FakeMaterialAnalysisProvider
from planner_api.codex_runtime.conversation_files import ConversationFileService, validate_files
from planner_api.codex_runtime.conversation_file_api import router


@pytest.fixture
def copies(client, session_factory, tmp_path, monkeypatch):
    goal_id = client.post("/api/goals", json={"raw_input": "整理我的面试准备资料"}).json()[
        "goal_id"
    ]
    original = tmp_path / "source" / "interview.md"
    original.parent.mkdir()
    original.write_text("# 面试准备\n每天复练20分钟。\n", encoding="utf-8")
    root = client.post(
        "/api/material-roots", json={"display_name": "验收资料", "path": str(original.parent)}
    ).json()
    source_id = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": original.name},
    ).json()["source_id"]
    client.post(f"/api/goals/{goal_id}/materials/{source_id}/process")
    assert MaterialProcessingWorker(session_factory).run_once()
    monkeypatch.setenv("LLM_PROVIDER", "codex")
    from planner_api.config import get_settings

    get_settings.cache_clear()
    with session_factory() as session:
        context = MaterialAnalysisService(
            session, FakeMaterialAnalysisProvider()
        ).prepare_conversation_context(goal_id, "请把复练时长改为30分钟，生成修改版", [source_id])
        conversation = session.scalar(select(Conversation).where(Conversation.goal_id == goal_id))
        user = Message(
            conversation_id=conversation.id,
            sequence=100,
            role="USER",
            content=context["query"],
            metadata_json={"material_source_ids": [source_id]},
        )
        session.add(user)
        session.flush()
        context["user_message_id"] = user.id
        answer = Message(
            conversation_id=conversation.id,
            sequence=101,
            role="ASSISTANT",
            content="允许生成修改版吗？",
            generation_status="SUCCEEDED",
        )
        session.add(answer)
        session.commit()
        answer_id = answer.id
    calls = []

    def generate(task):
        calls.append(task["id"])
        return {
            "summary": "已调整为30分钟",
            "files": [{"name": "interview-修改版.md", "content": "# 面试准备\n每天复练30分钟。\n"}],
        }

    service = ConversationFileService(
        tmp_path / "copies", None, session_factory=session_factory, generator=generate
    )
    client.app.state.conversation_files = service
    client.app.include_router(router)
    yield service, context, answer_id, original, calls
    service.stop()


def test_consent_is_per_user_message_and_denial_never_generates(copies):
    service, context, answer_id, original, calls = copies
    task = service.propose(context)
    assert service.propose(context)["request_id"] == task["request_id"]
    assert service.for_answer(answer_id)[0]["status"] == "NEEDS_CONFIRMATION"
    denied = service.decide(context["goal_id"], task["request_id"], "DENY", "先保留原稿")
    assert denied["reason"] == "先保留原稿"
    assert service.decide(context["goal_id"], task["request_id"], "ALLOW")["status"] == "DENIED"
    assert not calls and "20分钟" in original.read_text(encoding="utf-8")


def test_allow_generates_once_and_keeps_original_read_only(copies):
    service, context, answer_id, original, calls = copies
    task_id = service.propose(context)["request_id"]
    service.decide(context["goal_id"], task_id, "ALLOW")
    service.decide(context["goal_id"], task_id, "ALLOW")
    deadline = time.monotonic() + 3
    while (
        service.store.get(task_id)["status"] in {"QUEUED", "RUNNING"}
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert service.for_answer(answer_id)[0]["status"] == "SUCCEEDED"
    assert len(calls) == 1
    assert "30分钟" in service.content(context["goal_id"], task_id, 0)["content"]
    assert "20分钟" in original.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        service.decide("another-goal", task_id, "ALLOW")


def test_api_rejects_stale_or_mismatched_scope(client, copies):
    service, context, answer_id, original, calls = copies
    forged = {**context, "goal_id": "other-goal"}
    with pytest.raises(ValueError, match="不匹配"):
        service.propose(forged)
    task_id = service.propose(context)["request_id"]
    base = f"/api/codex/conversation-files/{context['goal_id']}/{task_id}"
    assert client.get(base + "/files/0").status_code == 409
    assert (
        client.post(
            base + "/decision", json={"decision": "ALLOW", "cwd": str(original.parent)}
        ).status_code
        == 422
    )
    client.delete(f"/api/goals/{context['goal_id']}/materials/{context['source_ids'][0]}")
    assert client.post(base + "/decision", json={"decision": "ALLOW"}).status_code == 409
    assert not calls


@pytest.mark.parametrize(
    "files",
    [
        [{"name": "../source.md", "content": "bad"}],
        [{"name": ".env", "content": "bad"}],
        [{"name": "CON.txt", "content": "bad"}],
        [{"name": "file.md", "content": ""}],
        [{"name": "file.md", "content": "x" * 256001}],
        [{"name": "a.md", "content": "a"}, {"name": "A.md", "content": "b"}],
    ],
)
def test_invalid_outputs_never_publish(files):
    with pytest.raises(ValueError):
        validate_files({"summary": "结果", "files": files})


def test_copy_generation_requires_actual_full_read_and_repairs_output(copies):
    service, context, _, _, _ = copies
    calls = []

    class Runtime:
        def generate(self, payload, _timeout):
            calls.append(payload)
            payload["solevia_tool_handler"](
                "solevia_read_material",
                {"source_id": context["source_ids"][0], "offset": 0, "limit": 8000},
            )
            return json.dumps(
                {
                    "summary": "已修改",
                    "files": [
                        {
                            "name": "../bad.md" if len(calls) == 1 else "interview.md",
                            "content": "完整修改版",
                        }
                    ],
                }
            )

    service.runtime = Runtime()
    task = {"context": context, "model": "model", "effort": "low", "wait_seconds": 10}
    result = service._generate(task)
    assert result["files"][0]["name"] == "interview.md" and len(calls) == 2
    assert "validation_feedback" in calls[1]["input"]

    class NoRead:
        def generate(self, payload, _timeout):
            return json.dumps(result)

    service.runtime = NoRead()
    with pytest.raises(ValueError, match="完整读取"):
        service._generate(task)
