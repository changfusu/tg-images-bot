"""OCR 封装：PaddleOCR 识别图片为文本行列表。

懒加载单例（模型大，只加载一次），返回按阅读顺序排序的 [{text, score, x, y}]。
兼容 PaddleOCR 2.x（ocr.ocr）与 3.x（predict）两种返回结构。
"""
import os

_ocr = None


def get_ocr():
    """懒加载 PaddleOCR 单例。首次调用可能联网下载模型，较慢。"""
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR
        try:
            _ocr = PaddleOCR(
                lang="ch",
                use_textline_orientation=True,
                use_angle_cls=True,
                show_log=False,
            )  # 3.x
        except Exception:
            _ocr = PaddleOCR(lang="ch", use_angle_cls=True, show_log=False)  # 2.x
    return _ocr


def ocr_image(path: str):
    """识别图片，返回按阅读顺序排序的文本行列表。大图片会先缩放以提升速度。"""
    ocr = get_ocr()
    processed_path = _resize_for_ocr(path)
    try:
        result = ocr.predict(processed_path)  # 3.x
        lines = _from_v3(result)
    except (AttributeError, TypeError):
        result = ocr.ocr(processed_path, cls=True)  # 2.x
        lines = _from_v2(result)
    finally:
        if processed_path != path:
            os.remove(processed_path)
    return _sort_lines(lines)


def _resize_for_ocr(path, max_side=1280):
    """将图片最长边等比缩放到 max_side 以内，减少 OCR 耗时；小图直接返回原路径。"""
    from PIL import Image

    try:
        img = Image.open(path)
    except Exception:
        return path
    w, h = img.size
    if max(w, h) <= max_side:
        return path
    scale = max_side / max(w, h)
    new_size = (int(w * scale), int(h * scale))
    # ponytail: Pillow 各版本兼容，不用 Image.Resampling
    img = img.resize(new_size, Image.LANCZOS)
    base, ext = os.path.splitext(path)
    resized_path = f"{base}_resized{ext}"
    img.save(resized_path)
    return resized_path


def _from_v3(result):
    lines = []
    for r in result or []:
        texts = getattr(r, "rec_texts", None) or []
        scores = getattr(r, "rec_scores", None) or []
        polys = getattr(r, "rec_polys", None) or getattr(r, "rec_boxes", None) or []
        for text, score, box in zip(texts, scores, polys):
            lines.append({"text": text, "score": float(score), "x": min(p[0] for p in box), "y": min(p[1] for p in box)})
    return lines


def _from_v2(result):
    lines = []
    for page in result or []:
        for item in page or []:
            box, (text, score) = item
            lines.append({"text": text, "score": float(score), "x": min(p[0] for p in box), "y": min(p[1] for p in box)})
    return lines


def _sort_lines(lines):
    # 自上而下、行内自左向右；纵坐标差 < 15px 视为同一行
    lines.sort(key=lambda l: (round(l["y"] / 15), l["x"]))
    return lines
