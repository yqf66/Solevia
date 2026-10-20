"""Bounded optional MinerU 4 adapter, isolated from the planner's dependencies."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from planner_api.worker_sandbox import run_contained, worker_environment


def parse_pdf(
    path: Path,
    *,
    tier="standard",
    python="",
    home="",
    timeout_seconds=600,
    max_chars=25_000_000,
    check_cancelled=None,
) -> dict:
    env = worker_environment()
    env["MINERU_MODEL_SOURCE"] = "local"
    env.setdefault("MINERU_MODEL_SMALL_BACKEND", "onnx")
    env.setdefault("MINERU_MODEL_VLM_ENGINE", "llama-cpp")
    if home:
        env["MINERU_HOME"] = home
    env.setdefault(
        "MINERU_HOME", str(Path(os.getenv("TEMP", tempfile.gettempdir())) / "planner-mineru-models")
    )
    runner = Path(__file__).with_name("mineru_runner.py")
    with tempfile.TemporaryDirectory(prefix="planner-mineru-") as folder:
        output = Path(folder) / "result.json"
        # llama.cpp on Windows can fail when the source path contains non-ASCII
        # characters. Stage the PDF under an ASCII basename inside the temp dir.
        staged_input = Path(folder) / "input.pdf"
        shutil.copy2(path, staged_input)
        env.update({key: folder for key in ("TEMP", "TMP", "TMPDIR")})
        try:
            run_contained([
                str(Path(python).absolute()) if python else sys.executable,
                "-I", str(runner), str(staged_input), str(output), tier, str(max_chars),
            ], Path(folder), env=env, timeout_seconds=timeout_seconds,
                memory_mb=4096, check_cancelled=check_cancelled)
        except TimeoutError as error:
            raise RuntimeError("MinerU 超过解析时间预算") from error
        if output.stat().st_size > max_chars * 6 + 1_000_000:
            raise RuntimeError("MinerU 输出超过正文预算")
        result = json.loads(output.read_text(encoding="utf-8"))
        pages = result.get("pages", [])
        if not pages or not all(
            isinstance(page.get("text"), str) and isinstance(page.get("page"), int)
            for page in pages
        ):
            raise RuntimeError("MinerU 未返回有效页级正文")
        if sum(len(page["text"]) for page in pages) > max_chars:
            raise RuntimeError("MinerU 正文超过字符预算")
        return result
