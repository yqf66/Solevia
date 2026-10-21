import os
from pathlib import Path
from types import SimpleNamespace

from planner_api.schedule_profile import routine_windows
from planner_api.schemas import ModelConnectionUpdate
from planner_api.user_settings_service import UserSettingsService


def test_model_settings_are_saved_without_returning_plaintext_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    service = UserSettingsService(env_path)

    saved = service.update_model_connection(
        ModelConnectionUpdate(
            provider="openai_compatible",
            base_url="https://example.test/v1",
            proxy_url="",
            model="model-a",
            api_key="secret-1234",
        )
    )

    assert saved.provider == "openai_compatible"
    assert saved.api_key_configured is True
    assert saved.api_key_hint == "末四位 1234"
    assert "secret-1234" not in saved.model_dump_json()
    assert ("secret-1234" in env_path.read_text(encoding="utf-8")) == (os.name != "nt")


def test_routine_windows_remove_meals_and_midday_rest() -> None:
    profile = SimpleNamespace(
        wake_time="07:00",
        available_from="08:00",
        breakfast_enabled=True,
        breakfast_start="07:30",
        breakfast_end="08:00",
        lunch_enabled=True,
        lunch_start="12:00",
        lunch_end="13:00",
        nap_enabled=True,
        nap_start="13:00",
        nap_end="14:00",
        dinner_enabled=True,
        dinner_start="18:00",
        dinner_end="19:00",
        sleep_time="23:00",
        routine_exclusions_json=[],
    )

    assert routine_windows(profile) == [
        ("08:00", "12:00"),
        ("14:00", "18:00"),
        ("19:00", "23:00"),
    ]


def test_routine_windows_support_disabled_meals_and_custom_exclusions() -> None:
    profile = SimpleNamespace(
        wake_time="07:00",
        available_from="07:30",
        sleep_time="23:00",
        breakfast_enabled=False,
        breakfast_start="07:30",
        breakfast_end="08:00",
        lunch_enabled=True,
        lunch_start="12:00",
        lunch_end="13:00",
        nap_enabled=False,
        nap_start="13:00",
        nap_end="14:00",
        dinner_enabled=True,
        dinner_start="18:00",
        dinner_end="19:00",
        routine_exclusions_json=[
            {"label": "通勤", "start_local": "08:00", "end_local": "09:00", "enabled": True},
            {"label": "忽略", "start_local": "15:00", "end_local": "16:00", "enabled": False},
        ],
    )

    assert routine_windows(profile) == [
        ("07:30", "08:00"),
        ("09:00", "12:00"),
        ("13:00", "18:00"),
        ("19:00", "23:00"),
    ]
