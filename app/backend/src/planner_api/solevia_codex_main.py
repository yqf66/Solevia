"""Full Solevia API, isolated storage, and the Codex business runtime."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = Path(os.environ.get("SOLEVIA_CODEX_RUNTIME_ROOT", ROOT / ".runtime/codex")).resolve()

# Resolve isolation before importing the original database/settings modules.
os.environ.setdefault("SOLEVIA_DATA_DIR", str(RUNTIME / "business"))
os.environ.setdefault("SOLEVIA_ENV_FILE", str(RUNTIME / ".env"))
os.environ.setdefault("SOLEVIA_DEFAULT_KERNEL", "codex")
from dotenv import dotenv_values  # noqa: E402
_profile = dotenv_values(os.environ["SOLEVIA_ENV_FILE"])
if "RESEARCH_RERANK_MODE" not in _profile:
    os.environ.setdefault("RESEARCH_RERANK_MODE", "OFF")
# Use the same work-item execution contract as the full Solevia release cases.
# This affects new Codex plans only; existing plans retain their stored model.
if os.environ.get("SOLEVIA_DEFAULT_KERNEL") == "codex" and "HYBRID_ROLLOUT_MODE" not in _profile:
    os.environ.setdefault("HYBRID_ROLLOUT_MODE", "NEW_PLANS")
os.environ.setdefault("SOLEVIA_FRONTEND_ORIGINS", "http://127.0.0.1:5175,http://localhost:5175")

from planner_api.main import create_app  # noqa: E402
from planner_api.config import get_settings  # noqa: E402
from planner_api.codex_runtime.business_api import router  # noqa: E402
from planner_api.codex_runtime.workspace import prepare_workspace  # noqa: E402
from planner_api.codex_runtime.business import BusinessCodexRuntime, bind_runtime  # noqa: E402
from planner_api.codex_runtime.conversation_files import ConversationFileService, bind_file_service  # noqa: E402
from planner_api.codex_runtime.conversation_file_api import router as file_router  # noqa: E402

app = create_app(start_material_worker=True, start_research_worker=True,
                 health_details={"runtime": "unified", "project_root": str(ROOT)}, switchable_kernel=True)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    from alembic import command
    from alembic.config import Config

    business_dir = Path(os.environ["SOLEVIA_DATA_DIR"])
    business_dir.mkdir(parents=True, exist_ok=True)
    migration = Config(str(ROOT / "backend/alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "backend/migrations"))
    await asyncio.to_thread(command.upgrade, migration, "head")
    from planner_api.plan_generation import recover_interrupted_plan_generation
    await asyncio.to_thread(recover_interrupted_plan_generation)
    workspace = RUNTIME / "workspace"
    await asyncio.to_thread(prepare_workspace, workspace)
    runtime = BusinessCodexRuntime(workspace)
    application.state.codex_business = runtime
    # Discovery is harmless; launching Codex requires an explicit user choice.
    if (get_settings().llm_provider == "codex"
            or _profile.get("SOLEVIA_CODEX_CONSENT") == "1"):
        await asyncio.to_thread(runtime.start)
        if runtime.connected:
            from planner_api.codex_runtime.preferences import ensure_initial_model
            try:
                await asyncio.to_thread(ensure_initial_model, runtime)
            except Exception:
                await asyncio.to_thread(runtime.stop)
                runtime.connection_error_code = 'CODEX_MODEL_REQUIRED'
                runtime.connection_error = '尚未读取到 Codex 可用模型，请检查本机模型配置，再点击「检测并连接」。'
    else:
        runtime.connection_error_code = "CODEX_CONSENT_REQUIRED"
        runtime.connection_error = "尚未连接 Codex。可以在模型连接中点击「检测并连接」。"
    bind_runtime(runtime)
    files = ConversationFileService(RUNTIME / 'conversation-files', runtime)
    application.state.conversation_files = files
    bind_file_service(files)
    try:
        async with original_lifespan(application):
            yield
    finally:
        bind_file_service(None)
        bind_runtime(None)
        await asyncio.to_thread(runtime.stop)
        await asyncio.to_thread(files.stop)


app.router.lifespan_context = lifespan
app.include_router(router)
app.include_router(file_router)


@app.get("/api/codex/workspace")
def workspace_status():
    runtime = app.state.codex_business
    return {"runtime": "codex", "project_root": str(ROOT), "connected": runtime.connected,
            "error": runtime.connection_error, "error_code": runtime.connection_error_code}
