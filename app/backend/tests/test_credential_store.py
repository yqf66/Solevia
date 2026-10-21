import os

import pytest
from pydantic import SecretStr

from planner_api.config import load_user_settings
from planner_api.credential_store import key_path, read_key, write_key


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI")
def test_dpapi_roundtrip_migration_rotation_and_clear(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    path = tmp_path / ".env"
    key = "synthetic-test-credential-1234"
    path.write_text(f"LLM_API_KEY='{key}'\n", encoding="utf8")
    assert load_user_settings(path, migrate=True).llm_api_key == SecretStr(key)
    assert key not in path.read_text(encoding="utf8")
    assert key.encode() not in key_path(path).read_bytes()
    assert read_key(path) == key
    write_key(path, "replacement-key")
    assert read_key(path) == "replacement-key"
    write_key(path, "")
    assert not key_path(path).exists()
    assert not load_user_settings(path).llm_api_key.get_secret_value()


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI")
def test_corrupt_dpapi_fails_without_exposing_contents(tmp_path):
    path = tmp_path / ".env"
    path.touch()
    key_path(path).write_bytes(b"invalid-private-ciphertext")
    with pytest.raises(OSError) as error:
        load_user_settings(path)
    assert "invalid-private-ciphertext" not in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI")
def test_process_secret_does_not_get_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "process-only-key")
    path = tmp_path / ".env"
    path.write_text("LLM_API_KEY=''\n", encoding="utf8")
    assert load_user_settings(path, migrate=True).llm_api_key.get_secret_value() == "process-only-key"
    assert not key_path(path).exists()
