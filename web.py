"""FastAPI 网页：表格页 + 搜索筛选 + CSV/Excel 导出 + 群查询日志。"""
import csv
import io
import os
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import config
import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="短信识别台账")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

OPERATORS = ["中国移动", "中国联通", "中国电信", "其他"]
COLS = (("msg_time", "短信时间"), ("sender_number", "发件号码"), ("operator", "运营商"),
        ("phone_in_body", "正文号码"), ("content", "信息内容"), ("urls", "网址"),
        ("user_name", "上传人"), ("created_at", "上传时间"))
PER_PAGE = 50


def _to_full(to):
    """日期范围上界：纯日期补全到当天 23:59:59，避免漏掉当天记录。"""
    if to and " " not in to:
        return to + " 23:59:59"
    return to


def _range_to_dates(range_value):
    """把快捷范围转成 (time_from, time_to)。today/week/month/all。"""
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_value == "today":
        return today.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if range_value == "week":
        start = today - timedelta(days=7)
        return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if range_value == "month":
        start = today - timedelta(days=30)
        return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    return "", ""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: str = "", operator: str = "", frm: str = "", to: str = "", page: int = 1):
    page = max(1, page)
    to_full = _to_full(to)
    rows = await db.query(q=q, operator=operator, time_from=frm, time_to=to_full,
                          limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    total = await db.count(q=q, operator=operator, time_from=frm, time_to=to_full)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows, "total": total, "page": page, "pages": pages,
        "q": q, "operator": operator, "frm": frm, "to": to, "operators": OPERATORS,
    })


@app.get("/export")
async def export(fmt: str = "csv", q: str = "", operator: str = "", frm: str = "", to: str = ""):
    rows = await db.query(q=q, operator=operator, time_from=frm, time_to=_to_full(to),
                          limit=100000, offset=0)
    if fmt == "xlsx":
        return _export_xlsx(rows)
    return _export_csv(rows)


@app.get("/group-queries", response_class=HTMLResponse)
async def group_queries(request: Request, chat_id: str = "", range: str = "all", page: int = 1):
    page = max(1, page)
    groups = await db.list_groups()
    selected_chat_id = None
    rows = []
    total = 0
    pages = 1
    time_from, time_to = _range_to_dates(range)
    if chat_id:
        try:
            selected_chat_id = int(chat_id)
        except ValueError:
            selected_chat_id = None
        if selected_chat_id:
            rows = await db.query_group_queries(selected_chat_id, time_from=time_from, time_to=time_to,
                                                limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
            total = await db.count_group_queries(selected_chat_id, time_from=time_from, time_to=time_to)
            pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return templates.TemplateResponse(request, "group_queries.html", {
        "groups": groups, "rows": rows, "total": total, "page": page, "pages": pages,
        "chat_id": chat_id, "range": range, "time_from": time_from, "time_to": time_to,
    })


def _export_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[1] for c in COLS])
    for r in rows:
        w.writerow([r.get(c[0], "") for c in COLS])
    data = buf.getvalue().encode("utf-8-sig")  # BOM 便于 Excel 打开中文
    return StreamingResponse(io.BytesIO(data), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=records.csv"})


def _export_xlsx(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append([c[1] for c in COLS])
    for r in rows:
        ws.append([r.get(c[0], "") for c in COLS])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return StreamingResponse(bio,
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=records.xlsx"})
