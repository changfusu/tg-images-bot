"""字段抽取：从 OCR 文本行解析短信字段（启发式，需真实截图校准）。

输入：ocr.ocr_image() 返回的文本行列表 [{text, score, x, y}]（已按阅读顺序排序）。
输出：字段 dict，字段名与 db.COLUMNS 对齐。
"""
import re
from datetime import datetime

# 11 位手机号；1[3-9] 首两位天然排除 106 短信端口
PHONE_RE = re.compile(r"1[3-9]\d{9}")

# 网址：http(s)、www、裸域名（覆盖常见顶级域；可随样本扩充）
# ponytail: 域名后只保留 URL 合法 ASCII 字符，防止中文粘连被一起吞入
URL_RE = re.compile(
    r"https?://[a-zA-Z0-9_\-./?=&%+~#@:]+|www\.[a-zA-Z0-9_\-./?=&%+~#@:]+|[\w-]+\.(?:com|cn|net|org|io|co|xyz|top|vip|club|shop|my|中国|公司|网络)[a-zA-Z0-9_\-./?=&%+~#@:]*",
    re.IGNORECASE,
)

# 短信时间多组正则，按序尝试（第 1 组最完整）
TIME_RES = [
    re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"),
    re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})"),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})"),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
]

# ponytail: 运营商归属只用短号 + 关键词，按 134-139 号段精确归属需接号段库，量级上来再加
OPERATOR_BY_SHORT = {"10086": "中国移动", "10010": "中国联通", "10000": "中国电信"}
OPERATOR_KEYWORDS = (("移动", "中国移动"), ("联通", "中国联通"), ("电信", "中国电信"))


def parse(lines):
    """解析字段。lines 为 ocr 文本行列表（已按阅读顺序排序）。"""
    texts = [l["text"].strip() for l in lines]
    texts = [t for t in texts if t]
    full = "\n".join(texts)

    sender_number = _extract_sender(texts)
    operator = _classify_operator(full, sender_number)
    msg_time = _extract_time(full)
    phone_in_body = _extract_phones(full, exclude=sender_number)
    urls = _extract_urls(full)
    content = _extract_content(texts, sender_number)

    conf = sum(l["score"] for l in lines) / len(lines) if lines else 0.0

    return {
        "sender_number": sender_number,
        "operator": operator,
        "phone_in_body": ",".join(phone_in_body),
        "content": content,
        "urls": "\n".join(urls),
        "msg_time": msg_time or "",
        "ocr_confidence": round(conf, 4),
        "raw_ocr": full,
    }


def _extract_sender(texts):
    """发件号码：顶部第一条短文本（号码/短号/银行名），去掉行尾时间戳。"""
    if not texts:
        return ""
    first = texts[0]
    # 首行若纯是时间，发件人可能在下一行
    if _looks_like_time(first) and len(texts) > 1:
        first = texts[1]
    # 过长视为正文被识别成首行，放弃
    if len(first) > 40:
        return ""
    first = TIME_RES[0].sub("", first).strip(" :：")
    return first


def _classify_operator(full, sender):
    for short, name in OPERATOR_BY_SHORT.items():
        if sender and short in sender:
            return name
    for kw, name in OPERATOR_KEYWORDS:
        if kw in full:
            return name
    return "其他"


def _extract_time(full):
    m = TIME_RES[0].search(full)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = int(m.group(4)), int(m.group(5))
        s = int(m.group(6) or 0)
        return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
    m = TIME_RES[1].search(full)
    if m:
        mo, d, h, mi = (int(m.group(i)) for i in range(1, 5))
        y = datetime.now().year  # ponytail: 无年份时用当前年，跨年边界可后续校正
        return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:00"
    m = TIME_RES[2].search(full)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
    m = TIME_RES[3].search(full)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} 00:00:00"
    return ""


def _extract_phones(full, exclude=""):
    found = []
    for m in PHONE_RE.finditer(full):
        num = m.group(0)
        if num == exclude or num in found:
            continue
        found.append(num)
    return found


def _extract_urls(full):
    urls = []
    for m in URL_RE.finditer(full):
        u = m.group(0).rstrip(".,;:!?()[]{}'\"")
        if u not in urls:
            urls.append(u)
    return urls


def _extract_content(texts, sender):
    """正文：发件行之后、剔除纯时间行的其余文本。"""
    body = []
    for i, t in enumerate(texts):
        if i == 0 and sender:
            continue
        if _looks_like_time(t):
            continue
        body.append(t)
    return "\n".join(body).strip()


def _looks_like_time(text):
    """判断一行是否纯时间（用于剔除时间戳行）。"""
    if len(text) > 30:
        return False
    return bool(TIME_RES[0].match(text) or TIME_RES[1].match(text) or TIME_RES[2].match(text))


if __name__ == "__main__":
    # 自检：用一段模拟短信文本行验证字段抽取，任一断言失败即报错
    lines = [
        {"text": "10086", "score": 0.99, "x": 10, "y": 10},
        {"text": "2026-08-16 14:30", "score": 0.98, "x": 220, "y": 10},
        {"text": "【中国移动】您的套餐已使用 800MB，详情请登录 https://m.10086.cn 查询，咨询请拨 13800138000。",
         "score": 0.95, "x": 10, "y": 45},
    ]
    r = parse(lines)
    assert r["operator"] == "中国移动", r
    assert r["sender_number"] == "10086", r
    assert "13800138000" in r["phone_in_body"], r
    assert r["urls"], r
    assert r["msg_time"].startswith("2026-08-16"), r
    assert r["content"].startswith("【中国移动】"), r

    # regression: .my 等 ccTLD 也要被识别（含无空格粘连、大小写）
    assert "48036.my" in _extract_urls("请点击 48036.my 进行验证"), _extract_urls("请点击 48036.my 进行验证")
    assert "48036.my" in _extract_urls("请点击48036.my进行验证"), _extract_urls("请点击48036.my进行验证")
    assert "48036.my" in _extract_urls("请点击 48036.MY 进行验证"), _extract_urls("请点击 48036.MY 进行验证")

    print("parser 自检通过：")
    for k, v in r.items():
        print(f"  {k}: {v}")
