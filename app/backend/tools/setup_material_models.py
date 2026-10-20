"""Explicit weight installation; document processing never downloads E5 weights."""

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from planner_api.config import get_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--mineru", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if args.semantic:
        from huggingface_hub import snapshot_download

        path = snapshot_download(
            settings.material_embedding_model,
            revision=settings.material_embedding_revision,
            cache_dir=settings.material_embedding_cache_dir,
            allow_patterns=[
                "config.json",
                "model.safetensors",
                "tokenizer*",
                "special_tokens_map.json",
                "sentencepiece.bpe.model",
                "README.md",
            ],
        )
        print(f"Semantic weights ready: {path}")
    if args.ocr:
        from rapidocr.utils.download_models import download_models

        download_models(None)
        print("OCR weights ready")
    if args.mineru:
        command = (
            str(
                Path(settings.material_mineru_python).parent
                / ("mineru-kit.exe" if os.name == "nt" else "mineru-kit")
            )
            if settings.material_mineru_python
            else shutil.which("mineru-kit")
        )
        if command is None:
            raise SystemExit("请先安装可选依赖：pip install -e '.[mineru]'")
        if settings.material_mineru_home:
            os.environ["MINERU_HOME"] = settings.material_mineru_home
        os.environ.setdefault(
            "MINERU_HOME",
            str(Path(os.getenv("TEMP", tempfile.gettempdir())) / "planner-mineru-models"),
        )
        deployment_tier = (
            "standard"
            if settings.material_mineru_tier == "advanced"
            else settings.material_mineru_tier
        )
        backend = os.environ.get("MINERU_MODEL_SMALL_BACKEND", "onnx")
        engine = os.environ.get("MINERU_MODEL_VLM_ENGINE", "llama-cpp")
        subprocess.run(
            [
                command,
                "models",
                "download",
                "--tier",
                deployment_tier,
                "--small-backend",
                backend,
                "--vlm-engine",
                engine,
            ],
            check=True,
        )
        subprocess.run(
            [
                command,
                "models",
                "verify",
                "--tier",
                deployment_tier,
                "--small-backend",
                backend,
                "--vlm-engine",
                engine,
            ],
            check=True,
        )
        print(f"MinerU {settings.material_mineru_tier} models ready")


if __name__ == "__main__":
    main()
