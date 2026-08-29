"""全局配置：从 .env 读取环境变量。"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（本文件所在目录），用于定位 data/ 与 .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# 白名单用户 ID，逗号分隔；留空 = 允许所有人（多人使用）
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}

# 管理员用户 ID，逗号分隔；留空 = 无管理员（无法增删群/登记角色）
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))

# 网页台账 Basic Auth；WEB_PASSWORD 留空 = 不启用鉴权（仅限本机调试）
WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "bot.db"


def is_allowed(user_id: int) -> bool:
    """白名单为空时放行所有人，否则仅放行列表内用户。"""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def is_admin(user_id: int) -> bool:
    """仅 ADMIN_USER_IDS 内的用户是管理员。"""
    return user_id in ADMIN_USER_IDS


# 上海时区（UTC+8，中国无夏令时，用固定偏移避免依赖系统 tzdata）。
# 服务器可能部署在其他时区，入库时间戳与「当天」判断统一按上海时间算。
SH_TZ = timezone(timedelta(hours=8))


def now_str() -> str:
    """当前上海时间，入库时间戳统一用它（与 created_at/msg_time 格式一致）。"""
    return datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    """当前上海日期（凌晨 0 点为界），查询群只查当天。"""
    return datetime.now(SH_TZ).strftime("%Y-%m-%d")
