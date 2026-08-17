"""SQLite 存储层：建表、插入、查询、统计（aiosqlite 异步访问）。"""
import os
from datetime import datetime

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_number TEXT,
    operator      TEXT,
    phone_in_body TEXT,
    content       TEXT,
    urls          TEXT,
    msg_time      TEXT,
    ocr_confidence REAL,
    raw_ocr       TEXT,
    image_path    TEXT,
    user_id       INTEGER,
    user_name     TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_operator ON messages(operator);
CREATE INDEX IF NOT EXISTS idx_msg_time ON messages(msg_time);
CREATE INDEX IF NOT EXISTS idx_sender_number ON messages(sender_number);
CREATE INDEX IF NOT EXISTS idx_phone_in_body ON messages(phone_in_body);
CREATE TABLE IF NOT EXISTS groups (
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT,
    role       TEXT NOT NULL,          -- 'source'=识别群 | 'query'=查询群
    added_by   INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS query_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    phone      TEXT NOT NULL,
    queried_at TEXT NOT NULL,
    message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_query_logs_chat_time ON query_logs(chat_id, queried_at);
CREATE INDEX IF NOT EXISTS idx_query_logs_phone ON query_logs(phone);
CREATE INDEX IF NOT EXISTS idx_query_logs_chat_phone ON query_logs(chat_id, phone);
"""

COLUMNS = ("sender_number", "operator", "phone_in_body", "content", "urls",
           "msg_time", "ocr_confidence", "raw_ocr", "image_path",
           "user_id", "user_name", "created_at")


async def init():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.IMAGES_DIR, exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    await _migrate_query_logs_message_id()


def _where(q="", operator="", time_from="", time_to=""):
    """构建过滤条件（查询与统计共用）。时间按 msg_time 兜底 created_at。"""
    sql, params = "1=1", []
    if q:
        sql += " AND (content LIKE ? OR sender_number LIKE ? OR phone_in_body LIKE ? OR urls LIKE ? OR raw_ocr LIKE ?)"
        params += [f"%{q}%"] * 5
    if operator:
        sql += " AND operator = ?"
        params.append(operator)
    if time_from:
        sql += " AND COALESCE(msg_time, created_at) >= ?"
        params.append(time_from)
    if time_to:
        sql += " AND COALESCE(msg_time, created_at) <= ?"
        params.append(time_to)
    return sql, params


async def _migrate_query_logs_message_id():
    """迁移：为 query_logs 增加 message_id 列（老数据库兼容）。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("PRAGMA table_info(query_logs)")
        columns = {row[1] for row in await cur.fetchall()}
        if "message_id" in columns:
            return
        await db.execute("ALTER TABLE query_logs ADD COLUMN message_id INTEGER")
        await db.commit()


async def insert(data: dict):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            f"INSERT INTO messages ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
            tuple(data.get(c) for c in COLUMNS))
        await db.commit()


async def query(q="", operator="", time_from="", time_to="", limit=200, offset=0):
    where, params = _where(q, operator, time_from, time_to)
    sql = f"SELECT * FROM messages WHERE {where} ORDER BY COALESCE(msg_time, created_at) DESC LIMIT ? OFFSET ?"
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params + [limit, offset])
        return [dict(r) for r in await cur.fetchall()]


async def count(q="", operator="", time_from="", time_to=""):
    where, params = _where(q, operator, time_from, time_to)
    sql = f"SELECT COUNT(*) AS n FROM messages WHERE {where}"
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return row[0]


async def upsert_group(chat_id, title, role, added_by):
    """登记群角色（幂等：同 chat_id 覆盖 role/title）。"""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO groups (chat_id, title, role, added_by, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, role=excluded.role, added_by=excluded.added_by",
            (chat_id, title, role, added_by, created_at))
        await db.commit()


async def delete_group(chat_id):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM groups WHERE chat_id=?", (chat_id,))
        await db.commit()


async def list_groups():
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM groups ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]


async def get_group_role(chat_id):
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT role FROM groups WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def exists_phone(phone) -> bool:
    """判断手机号是否已作为发件号码或正文号码存在。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM messages WHERE sender_number = ? OR phone_in_body LIKE ? LIMIT 1",
            (phone, f"%{phone}%"))
        return await cur.fetchone() is not None


async def search_by_phone(phone, limit=5):
    """按手机号查记录：命中发件号码或正文号码，按短信时间倒序。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM messages WHERE sender_number = ? OR phone_in_body LIKE ? "
            "ORDER BY COALESCE(msg_time, created_at) DESC LIMIT ?",
            (phone, f"%{phone}%", limit))
        return [dict(r) for r in await cur.fetchall()]


async def log_query(chat_id, phone, message_id=None):
    """记录某查询群查询过的手机号，以及返回结果的第一条消息 ID。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO query_logs (chat_id, phone, queried_at, message_id) VALUES (?, ?, ?, ?)",
            (chat_id, phone, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message_id))
        await db.commit()


async def get_query_log(phone):
    """取该手机号最近一次被查询的记录（任意查询群）。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM query_logs WHERE phone = ? ORDER BY queried_at DESC LIMIT 1",
            (phone,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def query_group_queries(chat_id, time_from="", time_to="", limit=200, offset=0):
    """按群ID查询该群查过的号码，支持时间范围。"""
    sql = "SELECT * FROM query_logs WHERE chat_id = ?"
    params = [chat_id]
    if time_from:
        sql += " AND queried_at >= ?"
        params.append(time_from)
    if time_to:
        sql += " AND queried_at <= ?"
        params.append(time_to)
    sql += " ORDER BY queried_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def count_group_queries(chat_id, time_from="", time_to=""):
    """统计某群查询过的号码数量。"""
    sql = "SELECT COUNT(*) AS n FROM query_logs WHERE chat_id = ?"
    params = [chat_id]
    if time_from:
        sql += " AND queried_at >= ?"
        params.append(time_from)
    if time_to:
        sql += " AND queried_at <= ?"
        params.append(time_to)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return row[0]
