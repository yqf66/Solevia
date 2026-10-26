"""Unified installed entry point. Existing API installations retain their profile."""
import os
from pathlib import Path

backend = Path(__file__).resolve().parents[2]
data = Path(os.environ.get("SOLEVIA_DATA_DIR", backend / "data")).resolve()
os.environ.setdefault("SOLEVIA_DEFAULT_KERNEL", "api")
os.environ.setdefault("SOLEVIA_DATA_DIR", str(data))
os.environ.setdefault("SOLEVIA_ENV_FILE", str(backend / ".env"))
os.environ.setdefault("SOLEVIA_CODEX_RUNTIME_ROOT", str(data / "codex"))
from planner_api.solevia_codex_main import app  # noqa: E402, F401
