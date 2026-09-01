"""OCR 封装：RapidOCR（onnxruntime）识别图片为文本行列表。

懒加载单例（模型随包内置，无需联网下载），返回按阅读顺序排序的 [{text, score, x, y}]。
从 PaddleOCR 迁移：服务器 CPU 无 AVX-512，Paddle 的 MKLDNN 内核会 SIGILL。
"""
import os

_ocr = None


def get_ocr():
    """懒加载 RapidOCR 单例。"""
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()  # 默认即中文 ch_PP-OCR 模型
    return _ocr


def ocr_image(path: str):
    """识别图片，返回按阅读顺序排序的文本行列表。大图片会先缩放以提升速度。"""
    ocr = get_ocr()
    processed_path = _resize_for_ocr(path)
    try:
        result, _ = ocr(processed_path)
        lines = [
            {
                "text": text,
                "score": float(score),
                "x": min(p[0] for p in box),
                "y": min(p[1] for p in box),
            }
            for box, text, score in result or []
        ]
    finally:
        if processed_path != path:
            os.remove(processed_path)
    return _sort_lines(lines)


def _resize_for_ocr(path, max_side=1280):
    """将图片最长边等比缩放到 max_side 以内，减少 OCR 耗时；小图直接返回原路径。"""
    from PIL import Image

    try:
        with Image.open(path) as im:
            w, h = im.size
            if max(w, h) <= max_side:
                return path
            scale = max_side / max(w, h)
            new_size = (int(w * scale), int(h * scale))
            # ponytail: Pillow 各版本兼容，不用 Image.Resampling
            img = im.resize(new_size, Image.LANCZOS)
    except Exception:
        return path
    base, ext = os.path.splitext(path)
    resized_path = f"{base}_resized{ext}"
    img.save(resized_path)
    return resized_path


def _sort_lines(lines):
    # 自上而下、行内自左向右；纵坐标差 < 15px 视为同一行
    lines.sort(key=lambda l: (round(l["y"] / 15), l["x"]))
    return lines
