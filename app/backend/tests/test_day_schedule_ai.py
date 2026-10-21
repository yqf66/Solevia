import json

from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.day_schedule_ai import (
    DAY_SCHEDULE_PROMPT_VERSION,
    OpenAICompatibleDayScheduleProvider,
)


def test_openai_day_schedule_provider_retries_invalid_json() -> None:
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        content = (
            "not-json"
            if len(calls) == 1
            else json.dumps(
                {
                    "suggestions": [
                        {
                            "task_id": "task-1",
                            "preferred_start_local": "09:00",
                            "rationale": "先完成前置任务",
                        }
                    ],
                    "summary": "按前置关系安排。",
                },
                ensure_ascii=False,
            )
        )
        return {"choices": [{"message": {"content": content}}]}

    provider = OpenAICompatibleDayScheduleProvider(
        Settings(
            llm_provider="openai_compatible",
            llm_api_key=SecretStr("test-key"),
            llm_base_url="https://gateway.example/v1",
            llm_model="schedule-model",
            day_schedule_max_retries=1,
        ),
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    result = provider.suggest(
        {
            "date": "2035-09-10",
            "tasks": [{"task_id": "task-1", "estimate_minutes": 30}],
        }
    )

    assert len(calls) == 2
    assert calls[0][0] == "https://gateway.example/v1/chat/completions"
    assert calls[0][2]["messages"][1]["content"].find(DAY_SCHEDULE_PROMPT_VERSION) >= 0
    assert result.suggestions[0].preferred_start_local == "09:00"
    assert "上次输出未通过校验" in calls[-1][2]["messages"][0]["content"]


def test_deepseek_feedback_keeps_context_and_repairs_hhmm():
    from planner_api.day_schedule_ai import DeepSeekDayScheduleProvider

    calls = []

    def transport(_url, _headers, payload, _timeout):
        calls.append(json.loads(json.dumps(payload)))
        start = "2035-09-10T09:00" if len(calls) == 1 else "09:00"
        return {
            "status": "completed",
            "output_text": json.dumps(
                {
                    "suggestions": [
                        {"task_id": "read", "preferred_start_local": start, "rationale": "完成前置"}
                    ],
                    "summary": "先阅读",
                }
            ),
        }

    provider = DeepSeekDayScheduleProvider(
        Settings(
            llm_provider="deepseek",
            llm_api_key=SecretStr("test-key"),
            day_schedule_max_retries=1,
        ),
        transport=transport,
        sleeper=lambda _: None,
    )
    result = provider.suggest(
        {
            "date": "2035-09-10",
            "tasks": [
                {"task_id": "read", "estimate_minutes": 20, "dependencies": []},
            ],
        }
    )
    assert result.suggestions[0].preferred_start_local == "09:00"
    assert len(calls) == 2 and calls[0]["input"] == calls[1]["input"]
    assert "preferred_start_local" in calls[1]["instructions"]
    assert "上次输出未通过校验" in calls[1]["instructions"]
