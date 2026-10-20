"""Private parser process. Receives only a staged document and explicit options."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

# -I ignores inherited Python paths; bootstrap only the trusted application code.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from planner_api.worker_sandbox import install_parser_policy, worker_ready


def main():
    folder = Path.cwd()
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    worker_ready(folder, request["memory_mb"])
    options = request["options"]
    install_parser_policy(folder, model_home=options.get("mineru_home", ""),
                          mineru_python=options.get("mineru_python", ""))
    from planner_api.materials.parsers import MaterialParseError, parse_material
    try:
        parsed = parse_material(folder / request["input"], **options)
        result = {"parsed": asdict(parsed)}
    except MaterialParseError as error:
        # Avoid passing arbitrary library diagnostics (paths/content) to the API.
        result = {"error": error.code}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
