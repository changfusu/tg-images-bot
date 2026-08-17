"""aiogram 机器人：分群分工。

- 识别群（role=source）：收图 → 下载 → OCR → 解析 → 入库 → 回复摘要。
- 查询群（role=query）：输入手机号 → 命中则返回 图+手机号+网址。
- 管理员：拉机器人进群时播报群 ID，并用按钮登记群角色。
"""
import asyncio
import os
import time
import uuid
from datetime import datetime

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, ChatMemberUpdated, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

import config
import db
import ocr
import parser

dp = Dispatcher()

# ponytail: PaddleOCR 的 predictor 非线程安全，批量传图时会并发触发 SIGSEGV；
# 用 asyncio 锁把 OCR 串行化（模型本身也是单 GPU/CPU 在跑，并发不会更快）。
_ocr_lock = asyncio.Lock()


class LogMiddleware(BaseMiddleware):
    """记录所有到达 dispatcher 的 update，用于排查消息是否被接收。"""

    async def __call__(self, handler, event, data):
        # dp.update 层的 event 是原始 Update 对象
        m = getattr(event, "message", None)
        if m is not None:
            uid = m.from_user.id if m.from_user else "?"
            print(f"[update] Message chat_id={m.chat.id} from={uid} text={m.text!r}", flush=True)
        elif getattr(event, "callback_query", None):
            print(f"[update] CallbackQuery data={event.callback_query.data!r}", flush=True)
        elif getattr(event, "my_chat_member", None):
            print(f"[update] my_chat_member chat_id={event.my_chat_member.chat.id}", flush=True)
        else:
            print(f"[update] {type(event).__name__}", flush=True)
        return await handler(event, data)


dp.update.middleware(LogMiddleware())


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _role_label(role):
    return "识别群" if role == "source" else "查询群"


def _role_keyboard(chat_id, with_ignore=False):
    """登记群角色的按钮：设为识别群 / 设为查询群（可选忽略）。"""
    kb = [[
        InlineKeyboardButton(text="设为识别群", callback_data=f"role:source:{chat_id}"),
        InlineKeyboardButton(text="设为查询群", callback_data=f"role:query:{chat_id}"),
    ]]
    if with_ignore:
        kb.append([InlineKeyboardButton(text="忽略", callback_data=f"ignore:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(F.photo)
async def on_photo(message: Message):
    # 仅识别群发图才处理；私聊发图给提示
    if await db.get_group_role(message.chat.id) != "source":
        if message.chat.type == "private":
            await message.reply("请在「识别群」内发送截图进行识别。")
        return

    photo = message.photo[-1]  # 取最大尺寸
    f = await message.bot.get_file(photo.file_id)
    ext = os.path.splitext(f.file_path)[-1] or ".jpg"
    name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    path = os.path.join(config.IMAGES_DIR, name)
    await message.bot.download_file(f.file_path, path)

    user_id = message.from_user.id
    user_name = message.from_user.full_name or message.from_user.username or str(message.from_user.id)
    data, dup_phone = await _process_photo(path, user_id, user_name)
    if dup_phone:
        await message.reply(f"手机号 {dup_phone} 已识别过，跳过。")
        return
    if data is None:
        return  # 无网址，静默忽略

    await message.reply(_format_summary(data))


async def _process_photo(path, user_id, user_name):
    """OCR + 解析 + 手机号去重入库。返回 (data, duplicate_phone)。"""
    # OCR + 解析是阻塞操作，丢线程池避免卡事件循环；
    # 加锁防止批量传图时多个线程同时使用 PaddleOCR predictor 导致段错误。
    async with _ocr_lock:
        result = await asyncio.to_thread(_ocr_and_parse, path)

        # 无网址的图自动忽略，不入库
        if not (result.get("urls") or "").strip():
            os.remove(path)
            return None, None

        # 去重：图中任一手机号码已被识别过，只保留最早的图
        for phone in _phones_from_result(result):
            if await db.exists_phone(phone):
                os.remove(path)
                return None, phone

        data = dict(result)
        data.update({
            "image_path": path,
            "user_id": user_id,
            "user_name": user_name,
            "created_at": _now_iso(),
        })
        await db.insert(data)
        return data, None


@dp.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message):

    # 查询群：手机号 → 查图；支持一行一个号码批量查询；全局去重，命中过的号码静默跳过
    if await db.get_group_role(message.chat.id) == "query":
        phones = _unique_phones(message.text)
        if not phones:
            return
        chat_id = message.chat.id
        for phone in phones:
            # 任意查询群已命中过 → 直接跳过，不提示、不记录
            if await db.get_query_log(phone):
                continue
            first_msg_id, found = await _send_query_results(message, phone)
            # 只有命中记录才写入查询日志；未命中的允许后续补上记录后再查
            if found:
                await db.log_query(chat_id, phone, first_msg_id)
        return

    # 私聊：兜底提示，保证私聊必有回应
    if message.chat.type == "private":
        await message.reply("请用命令操作：\n/start 查看用法\n/groups 管理已登记群\n/addgroup <群ID> 登记群")
        return


@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    # 机器人被拉入群：群里播报群 ID，并私聊通知管理员（附角色按钮）
    if event.chat.type not in ("group", "supergroup"):
        return
    new, old = event.new_chat_member.status, event.old_chat_member.status
    if new not in ("member", "administrator") or old not in ("left", "kicked"):
        return

    chat = event.chat
    await event.bot.send_message(
        chat.id,
        f"本群 ID：{chat.id}\n请将本 ID 发送给管理员，登记本群为「识别群」或「查询群」。")

    # 管理员需先私聊 /start 过本机器人，否则私聊发送会失败（此处静默忽略）
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await event.bot.send_message(
                admin_id,
                f"机器人被加入群「{chat.title}」（ID：{chat.id}）",
                reply_markup=_role_keyboard(chat.id, with_ignore=True))
        except Exception:
            pass


@dp.message(Command("start"))
async def cmd_start(message: Message):
    print(f"[handler] cmd_start 被调用, chat_type={message.chat.type}, from={message.from_user.id}", flush=True)
    if message.chat.type != "private":
        return
    if config.is_admin(message.from_user.id):
        text = ("你好，我是短信识别台账机器人。\n\n"
                "把机器人拉进群后：群里会播报群 ID，我会私聊发你按钮登记「识别群 / 查询群」。\n\n"
                "管理命令：\n"
                "· /groups — 查看/删除已登记群\n"
                "· /addgroup <群ID> — 手动登记群\n\n"
                "群内用法：识别群发短信截图，查询群发手机号查图。")
    else:
        text = "你好，我是短信识别台账机器人。请在已登记的群内使用：识别群发截图、查询群发手机号。"
    await message.answer(text)


@dp.message(Command("groups"))
async def cmd_groups(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.reply("无权限。")
        return
    groups = await db.list_groups()
    if not groups:
        await message.reply("暂无登记群。把机器人拉进群后我会通知你，或用 /addgroup <群ID> 手动登记。")
        return
    lines = [f"· [{_role_label(g['role'])}] {g['title']}（{g['chat_id']}）" for g in groups]
    kb = [[InlineKeyboardButton(text=f"删除 {g['title']}", callback_data=f"del:{g['chat_id']}")] for g in groups]
    await message.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.message(Command("addgroup"))
async def cmd_addgroup(message: Message, command: CommandObject):
    if not config.is_admin(message.from_user.id):
        await message.reply("无权限。")
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("用法：/addgroup <群ID>")
        return
    try:
        chat_id = int(arg)
    except ValueError:
        await message.reply("群 ID 应为数字。")
        return
    await message.reply(f"请选择群 {chat_id} 的角色：", reply_markup=_role_keyboard(chat_id))


@dp.callback_query()
async def on_callback(cb: CallbackQuery):
    if not config.is_admin(cb.from_user.id):
        await cb.answer("无权限。", show_alert=True)
        return
    parts = (cb.data or "").split(":")
    if not parts:
        await cb.answer()
        return
    action = parts[0]

    if action == "role" and len(parts) >= 3:
        role, cid = parts[1], int(parts[2])
        try:
            chat = await cb.bot.get_chat(cid)
            title = chat.title or str(cid)
        except Exception:
            title = str(cid)
        await db.upsert_group(cid, title, role, cb.from_user.id)
        if cb.message:
            await cb.message.edit_text(f"✅ 已登记群「{title}」为{_role_label(role)}。")
    elif action == "del" and len(parts) >= 2:
        cid = int(parts[1])
        await db.delete_group(cid)
        if cb.message:
            await cb.message.edit_text(f"已删除群 {cid} 的登记。")
    elif action == "ignore":
        await cb.answer("已忽略。")
        return
    await cb.answer()


def _ocr_and_parse(path):
    lines = ocr.ocr_image(path)
    return parser.parse(lines)


def _phones_from_result(result):
    """从识别结果中提取全部 11 位手机号码（发件号码 + 正文号码）。"""
    text = ",".join(filter(None, [result.get("sender_number"), result.get("phone_in_body")]))
    return set(parser.PHONE_RE.findall(text))


def _unique_phones(text):
    """按出现顺序提取文本中所有不重复的手机号，用于批量查询。"""
    seen = set()
    phones = []
    for m in parser.PHONE_RE.finditer(text):
        p = m.group(0)
        if p not in seen:
            seen.add(p)
            phones.append(p)
    return phones


async def _send_query_results(message: Message, phone: str):
    """在查询群发送手机号查询结果，返回 (第一条消息 message_id, 是否命中)。"""
    records = await db.search_by_phone(phone)
    if not records:
        sent = await message.reply(f"未找到手机号 {phone} 的相关记录。")
        return (sent.message_id if sent else None), False

    first_msg_id = None
    for r in records:
        caption = _format_query_result(phone, r)
        path = r.get("image_path")
        if path and os.path.exists(path):
            sent = await message.answer_photo(FSInputFile(path), caption=caption)
        else:
            sent = await message.reply(caption)
        if first_msg_id is None and sent:
            first_msg_id = sent.message_id
    return first_msg_id, True


def _format_summary(d):
    """识别群摘要：仅显示手机号与网址。"""
    parts = []
    for label, key in (("发件号码", "sender_number"), ("正文号码", "phone_in_body"), ("网址", "urls")):
        v = d.get(key) or ""
        if v:
            parts.append(f"{label}：{v}")
    if not parts:
        return "未能识别出有效内容，原图已保留。"
    return "✅ 识别完成\n" + "\n".join(parts)


def _format_query_result(phone, r):
    """查询命中结果 caption：纯手机号 + 网址（如有）。"""
    parts = [phone]
    urls = (r.get("urls") or "").strip()
    if urls:
        parts.append(urls)
    return "\n".join(parts)


async def run_bot(bot_token: str):
    print(f"[bot] run_bot 启动，token 长度={len(bot_token)}", flush=True)
    bot = Bot(token=bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="查看用法"),
        BotCommand(command="groups", description="查看/删除已登记群"),
        BotCommand(command="addgroup", description="手动登记群（/addgroup <群ID>）"),
    ])
    print("[bot] 命令已注册，开始 polling", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    print("bot 模块无运行时自校验")
