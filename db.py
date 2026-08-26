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
    note          TEXT,               -- 发图时附带的文字说明（Telegram 图片 caption）
    urls          TEXT,
    msg_time      TEXT,
    ocr_confidence REAL,
    raw_ocr       TEXT,
    image_path    TEXT,
    user_id       INTEGER,
    user_name     TEXT,
    created_at    TEXT,
    consumed_at   TEXT,               -- 被查时间；NULL=未被查过（同一号码可上传多次，FIFO 逐条消耗）
    consumed_chat_id INTEGER          -- 认领该记录的查询群
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
    message_id INTEGER,
    package    TEXT NOT NULL DEFAULT '',   -- 包号：数据包名称
    url        TEXT NOT NULL DEFAULT '',   -- 命中记录的网址
    found      INTEGER NOT NULL DEFAULT 1  -- 是否命中（=发送成功）；旧数据均为命中
);
CREATE TABLE IF NOT EXISTS admins (
    user_id    INTEGER PRIMARY KEY,
    added_by   INTEGER,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_query_logs_chat_time ON query_logs(chat_id, queried_at);
CREATE INDEX IF NOT EXISTS idx_query_logs_phone ON query_logs(phone);
CREATE INDEX IF NOT EXISTS idx_query_logs_chat_phone ON query_logs(chat_id, phone);
"""

COLUMNS = ("sender_number", "operator", "phone_in_body", "content", "note", "urls",
           "msg_time", "ocr_confidence", "raw_ocr", "image_path",
           "user_id", "user_name", "created_at")


async def init():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.IMAGES_DIR, exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    await _migrate_query_logs()
    await _migrate_messages()


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


async def _migrate_query_logs():
    """迁移：为 query_logs 补齐老库缺失的列（message_id/package/url/found）。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("PRAGMA table_info(query_logs)")
        columns = {row[1] for row in await cur.fetchall()}
        if "message_id" not in columns:
            await db.execute("ALTER TABLE query_logs ADD COLUMN message_id INTEGER")
        if "package" not in columns:
            await db.execute("ALTER TABLE query_logs ADD COLUMN package TEXT NOT NULL DEFAULT ''")
        if "url" not in columns:
            await db.execute("ALTER TABLE query_logs ADD COLUMN url TEXT NOT NULL DEFAULT ''")
        if "found" not in columns:
            # 旧数据都是命中才记录的，默认 found=1
            await db.execute("ALTER TABLE query_logs ADD COLUMN found INTEGER NOT NULL DEFAULT 1")
        await db.commit()


async def _migrate_messages():
    """迁移：为 messages 补齐老库缺失的列（consumed_at/consumed_chat_id/note）。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in await cur.fetchall()}
        if "consumed_at" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN consumed_at TEXT")
        if "consumed_chat_id" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN consumed_chat_id INTEGER")
        if "note" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN note TEXT")
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


async def add_admin(user_id, added_by):
    """登记管理员（幂等：同 user_id 不重复插入）。"""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO admins (user_id, added_by, created_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, added_by, created_at))
        await db.commit()


async def remove_admin(user_id):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()


async def list_admins():
    """数据库里登记的管理员 user_id 列表（不含 .env 里的超管）。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM admins ORDER BY created_at")
        return [row[0] for row in await cur.fetchall()]


async def is_db_admin(user_id) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id=? LIMIT 1", (user_id,))
        return await cur.fetchone() is not None


async def claim_next_record(phone, chat_id):
    """FIFO 认领：把该号码当天最早一条未查记录标记为已查并返回它；无可认领的返回 None。

    UPDATE...RETURNING 是单条原子语句，多个群同时查同一号码也不会抢到同一条记录，
    天然保证「A群查过的记录，B群查不到」。只认「当天」（上海时区，凌晨 0 点为界）。
    """
    today = config.today_str()
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE messages SET consumed_at = ?, consumed_chat_id = ? "
            "WHERE id = (SELECT id FROM messages WHERE (sender_number = ? OR phone_in_body LIKE ?) "
            "AND consumed_at IS NULL "
            "AND substr(COALESCE(NULLIF(msg_time, ''), created_at), 1, 10) = ? "
            "ORDER BY COALESCE(NULLIF(msg_time, ''), created_at) ASC, id ASC LIMIT 1) "
            "RETURNING *",
            (config.now_str(), chat_id, phone, f"%{phone}%", today))
        row = await cur.fetchone()
        await db.commit()
        return dict(row) if row else None


async def count_today_records(phone):
    """该号码当天上传的记录数（用于区分「从未上传」与「查询次数已用完」）。"""
    today = config.today_str()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM messages WHERE (sender_number = ? OR phone_in_body LIKE ?) "
            "AND substr(COALESCE(NULLIF(msg_time, ''), created_at), 1, 10) = ?",
            (phone, f"%{phone}%", today))
        row = await cur.fetchone()
        return row[0]


async def get_record(record_id):
    """按主键 ID 取单条记录（删除前用来拿 image_path）。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (record_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_record(record_id):
    """按主键 ID 删除记录，返回是否删到。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("DELETE FROM messages WHERE id=?", (record_id,))
        await db.commit()
        return cur.rowcount > 0


async def log_query(chat_id, phone, message_id=None, package="", url="", found=True):
    """记录一次查询：手机号、包号（数据包名）、命中网址、是否命中，以及返回结果的第一条消息 ID。"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO query_logs (chat_id, phone, queried_at, message_id, package, url, found) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, phone, config.now_str(),
             message_id, package, url, 1 if found else 0))
        await db.commit()


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


async def group_query_stats(chat_id, time_from="", time_to=""):
    """统计某群查询：总数、已发送（命中）、未发送（未命中）。"""
    sql = "SELECT COUNT(*) AS total, COALESCE(SUM(found), 0) AS received FROM query_logs WHERE chat_id = ?"
    params = [chat_id]
    if time_from:
        sql += " AND queried_at >= ?"
        params.append(time_from)
    if time_to:
        sql += " AND queried_at <= ?"
        params.append(time_to)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        total, received = await cur.fetchone()
        return {"total": total, "received": received, "not_received": total - received}


if __name__ == "__main__":
    # 自检：FIFO 认领 / 可查次数=上传次数 / 跨群不重复 / 不同号码互不影响
    import asyncio
    import tempfile
    from pathlib import Path

    _tmp = Path(tempfile.mkdtemp())
    config.DATA_DIR = _tmp
    config.IMAGES_DIR = _tmp / "images"
    config.DB_PATH = _tmp / "test.db"

    async def _check():
        await init()
        base = {"sender_number": "13800000001", "msg_time": config.now_str(),
                "created_at": config.now_str()}
        await insert(dict(base, content="第1条"))
        await insert(dict(base, content="第2条"))
        await insert(dict(base, content="别的号码", sender_number="13900000002"))
        assert await count_today_records("13800000001") == 2
        r1 = await claim_next_record("13800000001", -100)
        assert r1 and r1["content"] == "第1条", r1            # FIFO：先拿最早的一条
        r2 = await claim_next_record("13800000001", -200)
        assert r2 and r2["content"] == "第2条", r2            # B群拿到下一条，而非同一条
        assert r2["consumed_chat_id"] == -200
        assert await claim_next_record("13800000001", -100) is None  # 2次上传=2次查询，已用完
        other = await claim_next_record("13900000002", -100)
        assert other and other["content"] == "别的号码"        # 不同号码互不影响
        print("db 自检通过：FIFO 认领 / 可查次数=上传次数 / 跨群不重复")

    asyncio.run(_check())
