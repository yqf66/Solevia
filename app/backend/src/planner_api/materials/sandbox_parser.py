"""Process boundary used by the material processing service, including text input."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from planner_api.worker_sandbox import run_contained, worker_environment
from .parsers import MaterialParseError, ParsedLocation, ParsedMaterial


def parse_material(path: Path, *, timeout_seconds=900, memory_mb=4096,
                   check_cancelled=None, **options) -> ParsedMaterial:
    if check_cancelled:
        check_cancelled()
    if not options.get("mineru_home"):
        options["mineru_home"] = str(Path(os.getenv("TEMP", tempfile.gettempdir())) / "planner-mineru-models")
    with tempfile.TemporaryDirectory(prefix="planner-parse-") as temporary:
        folder = Path(temporary).resolve()
        source = folder / ("input" + path.suffix.lower())
        shutil.copyfile(path, source)
        (folder / "request.json").write_text(json.dumps({"input": source.name,
            "options": options, "memory_mb": memory_mb}), encoding="utf-8")
        try:
            run_contained([sys.executable, "-I", str(Path(__file__).with_name("parser_worker.py"))],
                folder, env=worker_environment(folder), timeout_seconds=timeout_seconds,
                memory_mb=memory_mb, check_cancelled=check_cancelled)
        except TimeoutError as error:
            raise MaterialParseError("PARSE_TIMEOUT", "文档解析超时，请缩小文档后重试。") from error
        except (OSError, RuntimeError) as error:
            raise MaterialParseError("PARSE_WORKER_FAILED", "文档解析进程未完成，请重试。") from error
        output = folder / "result.json"
        limit = options.get("max_chars", 25_000_000)
        if not output.is_file() or output.stat().st_size > limit * 8 + 4_000_000:
            raise MaterialParseError("PARSE_OUTPUT_BUDGET", "解析结果超过允许的大小。")
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
            if result.get("error"):
                raise MaterialParseError(result["error"], "文档无法完整解析，请检查文件格式与处理上限。")
            parsed = result["parsed"]
            if not isinstance(parsed["text"], str) or len(parsed["text"]) > limit:
                raise ValueError("Invalid parsed text")
            parsed["locations"] = tuple(ParsedLocation(**item) for item in parsed["locations"])
            for location in parsed["locations"]:
                if not 0 <= location.char_start <= location.char_end <= len(parsed["text"]):
                    raise ValueError("Invalid parsed location")
            return ParsedMaterial(**parsed)
        except (ValueError, KeyError, TypeError) as error:
            raise MaterialParseError("PARSE_INVALID_RESULT", "文档解析结果无效，请重试。") from error
