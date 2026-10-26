"""Standalone child entry point; only stdlib and the optional MinerU runtime."""

import json
import sys
from importlib.metadata import version
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from planner_api.worker_sandbox import install_parser_policy, worker_ready
    import os
    worker_ready(Path.cwd())
    install_parser_policy(Path.cwd(), model_home=os.environ.get("MINERU_HOME", ""))
    from mineru.config import VlmConfig
    from mineru.model.download import ModelRepo
    from mineru.parser import parse
    from mineru.render import render_markdown

    path, output, tier, budget = sys.argv[1:]
    # Offline model files remain read-only. MinerU uses advisory download locks
    # even for ready local models; keep those ephemeral locks in our workspace.
    import hashlib
    ModelRepo.lock_path = lambda repo: Path.cwd() / "model-locks" / (
        hashlib.sha256(repo.name.encode()).hexdigest() + ".lock")
    result = parse(path, tier=tier, image_analysis=False, vlm_config=VlmConfig(server_url=""))
    pages = []
    size = 0
    for page in result.pages:
        document = result.middle_json.model_copy(update={"pages": [page]})
        # Preserve captions and structured tables/formulas, while keeping bitmap
        # payloads out of the searchable text and the model's context window.
        text = render_markdown(document, image_renderer=lambda _block: "[图片内容请查看原 PDF]")
        size += len(text)
        if size > int(budget):
            raise RuntimeError("MinerU text budget exceeded")
        pages.append({"page": page.page_idx + 1, "text": text})
    Path(output).write_text(
        json.dumps({"pages": pages, "version": version("mineru")}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
