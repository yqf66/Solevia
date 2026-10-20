from __future__ import annotations

import threading
from functools import lru_cache

_ocr_lock = threading.Lock()


@lru_cache(maxsize=1)
def _engine():
    from pathlib import Path

    import rapidocr
    from rapidocr import RapidOCR

    root = Path(rapidocr.__file__).parent / "models"
    paths = {
        "Det.model_path": root / "PP-OCRv6_det_small.onnx",
        "Cls.model_path": root / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        "Rec.model_path": root / "PP-OCRv6_rec_small.onnx",
    }
    if any(not path.is_file() for path in paths.values()):
        raise RuntimeError("OCR 权重未安装，请运行 tools/setup_material_models.py --ocr")
    return RapidOCR(
        params={
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 2,
            **{key: str(path) for key, path in paths.items()},
        }
    )


def recognize_page(page, *, dpi: int, min_confidence: float) -> tuple[str, float, int]:
    if page.width * page.height * (dpi / 72) ** 2 > 12_000_000:
        raise RuntimeError("扫描页超过 OCR 渲染像素预算")
    image = page.to_image(resolution=dpi).original
    with _ocr_lock:
        result = _engine()(image)
    recognized = [
        (text, float(score))
        for text, score in zip(result.txts or (), result.scores or (), strict=True)
        if text.strip()
    ]
    accepted = [(text, score) for text, score in recognized if score >= min_confidence]
    confidence = sum(score for _, score in accepted) / len(accepted) if accepted else 0.0
    return "\n".join(text for text, _ in accepted), confidence, len(recognized) - len(accepted)
