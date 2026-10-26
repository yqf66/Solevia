from pathlib import Path

from planner_api.kernel import settings_lock, save_values

from planner_api.config import Settings, load_user_settings, settings_env_file
from planner_api.credential_store import write_key
from planner_api.schemas import ModelConnectionRead, ModelConnectionUpdate


class UserSettingsService:
    def __init__(self, env_path: Path | None = None) -> None:
        self.env_path = env_path or settings_env_file()

    def _settings(self) -> Settings:
        # API configuration is independent of the active answering kernel.
        return load_user_settings(self.env_path, kernel="api")

    def _read(self) -> ModelConnectionRead:
        settings = self._settings()
        key = settings.llm_api_key.get_secret_value().strip()
        return ModelConnectionRead(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            proxy_url=settings.llm_proxy_url,
            model=settings.llm_model,
            api_key_configured=bool(key),
            api_key_hint=f"末四位 {key[-4:]}" if key else None,
        )

    def get_model_connection(self) -> ModelConnectionRead:
        return self._read()

    def update_model_connection(self, payload: ModelConnectionUpdate) -> ModelConnectionRead:
        with settings_lock:
            self.env_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.env_path.exists():
                self.env_path.touch()
            active = load_user_settings(self.env_path).llm_provider
            values = {
                "SOLEVIA_KERNEL": "codex" if active == "codex" else "api",
                "LLM_PROVIDER": payload.provider,
                "LLM_BASE_URL": payload.base_url,
                "LLM_PROXY_URL": payload.proxy_url,
                "LLM_MODEL": payload.model.strip(),
            }
            if payload.clear_api_key:
                write_key(self.env_path, "")
            elif payload.api_key is not None and payload.api_key.get_secret_value().strip():
                write_key(self.env_path, payload.api_key.get_secret_value().strip())
            save_values(self.env_path, values)
            return self._read()
