"""Keep only the OCR and OpenCV assets used by the application."""
from __future__ import annotations

def filter_optional_assets(datas, binaries):
    """Drop alternate OCR models and OpenCV's unused video backend."""
    unused_models = {"common.onnx", "common_det.onnx"}

    def is_unused_model(item):
        return any(
            value.replace("\\", "/").lower().rsplit("/", 1)[-1] in unused_models
            for value in item if isinstance(value, str)
        )

    def is_video_backend(item):
        return any(
            "opencv_videoio_ffmpeg" in value.lower()
            for value in item if isinstance(value, str)
        )

    filtered_data = [
        item for item in datas
        if not is_unused_model(item) and not is_video_backend(item)
    ]
    filtered_binaries = [
        item for item in binaries
        if not is_unused_model(item) and not is_video_backend(item)
    ]
    return filtered_data, filtered_binaries
