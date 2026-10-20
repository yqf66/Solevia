from pathlib import Path

from dotenv import set_key

from planner_api.config import Settings, get_settings, settings_env_file
from planner_api.schemas import ModelConnectionRead, ModelConnectionUpdate


class UserSettingsService:
    def __init__(self, env_path: Path | None = None) -> None:
        self.env_path = env_path or settings_env_file()

    def _settings(self) -> Settings:
        if self.env_path.resolve() == settings_env_file():
            return get_settings()
        return Settings(_env_file=self.env_path)

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
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.env_path.exists():
            self.env_path.touch()
        values = {
            "LLM_PROVIDER": payload.provider,
            "LLM_BASE_URL": payload.base_url,
            "LLM_PROXY_URL": payload.proxy_url,
            "LLM_MODEL": payload.model.strip(),
        }
        for key, value in values.items():
            set_key(str(self.env_path), key, value, quote_mode="auto")
        if payload.clear_api_key:
            set_key(str(self.env_path), "LLM_API_KEY", "", quote_mode="auto")
        elif payload.api_key is not None and payload.api_key.get_secret_value().strip():
            set_key(
                str(self.env_path),
                "LLM_API_KEY",
                payload.api_key.get_secret_value().strip(),
                quote_mode="auto",
            )
        if self.env_path.resolve() == settings_env_file():
            get_settings.cache_clear()
        return self._read()
