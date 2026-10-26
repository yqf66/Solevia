"""Codex model catalog and inference preferences, isolated from API settings."""
import os
import tempfile
from pathlib import Path
from planner_api.kernel import settings_lock

from dotenv import set_key
from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from planner_api.config import get_settings, settings_env_file

WAIT_SECONDS = {'low': 600, 'medium': 1200, 'high': 2400, 'xhigh': 3600,
                'max': 5400, 'ultra': 7200}
_lock = settings_lock


def generation_timeout(settings):
    return WAIT_SECONDS[settings.codex_reasoning_effort]


class CodexPreferenceUpdate(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal['low', 'medium', 'high', 'xhigh', 'max', 'ultra']


def read_preferences(runtime, settings=None):
    settings = settings or get_settings()
    if not runtime.connected:
        raise HTTPException(503, 'Codex 尚未连接，暂时不能读取模型选项。')
    cursor = None
    models = {}
    seen_cursors = set()
    for _ in range(20):
        result = runtime.server.call('model/list', {'limit': 100, **({'cursor': cursor} if cursor else {})}, timeout=10)
        for row in result.get('data', []):
            model_id = row.get('model') or row.get('id')
            if not isinstance(model_id, str) or row.get('hidden'):
                continue
            efforts = list(dict.fromkeys(item['reasoningEffort']
                for item in row.get('supportedReasoningEfforts', [])
                if item.get('reasoningEffort') in WAIT_SECONDS))
            if not efforts:
                continue
            default = row.get('defaultReasoningEffort')
            models[model_id] = {'id': model_id, 'display_name': row.get('displayName') or model_id, 'is_default': bool(row.get('isDefault')),
                                'supported_efforts': efforts,
                                'default_effort': default if default in efforts else efforts[0]}
        cursor = result.get('nextCursor')
        if not cursor:
            break
        if cursor in seen_cursors:
            raise HTTPException(503, 'Codex 模型目录分页异常，请稍后重试。')
        seen_cursors.add(cursor)
    else:
        raise HTTPException(503, 'Codex 模型目录未读取完整，请稍后重试。')
    efforts = models.get(settings.llm_model, {}).get('supported_efforts', [])
    return {'model': settings.llm_model, 'reasoning_effort': settings.codex_reasoning_effort,
            'models': list(models.values()), 'supported_efforts': efforts,
            'wait_seconds': generation_timeout(settings), 'wait_seconds_by_effort': WAIT_SECONDS.copy()}


def ensure_initial_model(runtime, path=None):
    """First connection must never publish a usable kernel with an empty model."""
    from planner_api.config import load_user_settings
    from planner_api.kernel import save_values
    path = path or settings_env_file()
    with _lock:
        settings = load_user_settings(path, kernel='codex')
        if settings.llm_model.strip():
            return settings.llm_model
        configured = ''
        try:
            config = runtime.server.call('config/read', {'includeLayers': False}, timeout=10).get('config', {})
            if isinstance(config.get('model'), str):
                configured = config['model'].strip()
        except Exception:
            pass
        catalog = read_preferences(runtime, settings=settings)
        choices = catalog['models']
        selected = configured or next((row['id'] for row in choices if row.get('is_default')), '') or (choices[0]['id'] if choices else '')
        if not selected:
            raise HTTPException(503, 'Codex 已连接，但尚未读取到可用模型。请检查 Codex 的模型配置后重新连接。')
        save_values(path, {'CODEX_MODEL': selected})
        return selected


def save_preferences(runtime, payload, *, env_path: Path | None = None):
    with _lock:
        current = read_preferences(runtime)
        model_id = payload.model or current['model']
        target = next((row for row in current['models'] if row['id'] == model_id), None)
        if not target or payload.reasoning_effort not in target['supported_efforts']:
            raise HTTPException(422, '所选模型或思考强度不在本机 Codex 目录中，请刷新选项。')
        path = env_path or settings_env_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish both values together and preserve unrelated API settings.
        fd, temporary = tempfile.mkstemp(prefix='.codex-preferences-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(path.read_text(encoding='utf-8') if path.exists() else '')
            set_key(temporary, 'CODEX_MODEL', model_id, quote_mode='auto')
            set_key(temporary, 'CODEX_REASONING_EFFORT', payload.reasoning_effort, quote_mode='auto')
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        get_settings.cache_clear()
        return {**current, 'model': model_id, 'reasoning_effort': payload.reasoning_effort,
                'supported_efforts': target['supported_efforts'],
                'wait_seconds': WAIT_SECONDS[payload.reasoning_effort]}
