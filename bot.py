"""aiogram 机器人：分群分工。

- 识别群（role=source）：收图 → 下载 → OCR → 解析 → 入库 → 回复摘要。
- 查询群（role=query）：输入手机号 → 命中则返回 图+手机号+网址。
  同一号码当天上传几次就能查几次，FIFO 逐条认领，每条记录只会发给一个群。
- 管理员：拉机器人进群时播报群 ID，并用按钮登记群角色。
"""
import asyncio
import os
import re
import time
import uuid

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, ChatMemberUpdated, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

import config
import db
import ocr
import parser

dp = Dispatcher()

# ponytail: OCR 引擎的推理会话非线程安全，批量传图时并发调用可能崩溃；
# 用 asyncio 锁把 OCR 串行化（模型本身也是单 CPU 在跑，并发不会更快）。
_ocr_lock = asyncio.Lock()


class LogMiddleware(BaseMiddleware):
    """记录所有到达 dispatcher 的 update，用于排查消息是否被接收。"""

    async def __call__(self, handler, event, data):
        # dp.update 层的 event 是原始 Update 对象
        m = getattr(event, "message", None)
        if m is not None:
            # 只记来源，不记消息文本（含手机号等 PII，不应留在 docker logs）
            uid = m.from_user.id if m.from_user else "?"
            print(f"[update] Message chat_id={m.chat.id} from={uid}", flush=True)
        elif getattr(event, "callback_query", None):
            print(f"[update] CallbackQuery from={event.callback_query.from_user.id}", flush=True)
        elif getattr(event, "my_chat_member", None):
            print(f"[update] my_chat_member chat_id={event.my_chat_member.chat.id}", flush=True)
        else:
            print(f"[update] {type(event).__name__}", flush=True)
        return await handler(event, data)


dp.update.middleware(LogMiddleware())


def _now_iso():
    """当前上海时间（服务器可能部署在其他时区，统一按上海算）。"""
    return config.now_str()


async def _is_admin(user_id) -> bool:
    """管理员 = .env 里的超管(ADMIN_USER_IDS) + 数据库里登记的管理员。"""
    return config.is_admin(user_id) or await db.is_db_admin(user_id)


async def _all_admin_ids():
    """全部管理员 user_id（.env 超管 + 数据库管理员），用于通知。"""
    return set(config.ADMIN_USER_IDS) | set(await db.list_admins())


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


@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_photo(message: Message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    print(f"[on_photo] 收到图片 chat_id={chat_id} type={chat_type}", flush=True)

    # 白名单（.env ALLOWED_USER_IDS）非空时，仅放行列表内用户
    if not config.is_allowed(message.from_user.id if message.from_user else 0):
        return

    # 仅识别群发图才处理；私聊发图给提示
    if await db.get_group_role(chat_id) != "source":
        print(f"[on_photo] 群 {chat_id} 不是识别群，跳过", flush=True)
        if chat_type == "private":
            await message.reply("请在「识别群」内发送截图进行识别。")
        return

    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        file_id = message.document.file_id

    f = await message.bot.get_file(file_id)
    ext = os.path.splitext(f.file_path)[-1] or ".jpg"
    name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    path = os.path.join(config.IMAGES_DIR, name)
    await message.bot.download_file(f.file_path, path)
    print(f"[on_photo] 已下载图片 {path}", flush=True)

    user_id = message.from_user.id
    user_name = message.from_user.full_name or message.from_user.username or str(message.from_user.id)
    # 发图时附带的文字说明（Telegram caption），查询时随结果一并返回
    note = (message.caption or "").strip()
    data = None
    try:
        data = await _process_photo(path, user_id, user_name, note)
    except Exception as e:
        # OCR/入库失败：删掉已下载的图片避免孤儿文件，并告知用户
        print(f"[on_photo] 处理失败 err={type(e).__name__}", flush=True)
        try:
            os.remove(path)
        except OSError:
            pass
        await message.reply("图片处理失败，请重发。")
        return
    if data is None:
        await message.reply("未识别到手机号码，未保存。")
        return

    await message.reply(_format_summary(data))


async def _process_photo(path, user_id, user_name, note=""):
    """OCR + 解析 + 入库。有手机号才保存；无手机号删图并返回 None。note=发图附带的说明。"""
    # OCR + 解析是阻塞操作，丢线程池避免卡事件循环；
    # 加锁防止批量传图时多个线程同时使用 OCR 推理会话导致崩溃。
    async with _ocr_lock:
        result = await asyncio.to_thread(_ocr_and_parse, path)

    # 不打印 OCR 原文与号码（PII 不应留在 docker logs），只记数量
    print(f"[_process_photo] 识别完成：手机号 {len(_phones_from_result(result))} 个", flush=True)

    # 只认手机号：识别不到手机号的一律不保存
    if not _phones_from_result(result):
        os.remove(path)
        return None

    data = dict(result)
    data.update({
        "image_path": path,
        "note": note,
        "user_id": user_id,
        "user_name": user_name,
        "created_at": _now_iso(),
    })
    await db.insert(data)
    return data


@dp.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message):

    # 查询群：发号→查图、包号→数据包名；支持批量。
    # 同一号码当天上传几次就能查几次（FIFO 逐条认领，哪群抢到归哪群，同一条记录不会被两群看到）
    if await db.get_group_role(message.chat.id) == "query":
        # 白名单非空时仅放行列表内用户
        if not config.is_allowed(message.from_user.id if message.from_user else 0):
            return
        pairs = _parse_queries(message.text)
        if not pairs:
            return
        chat_id = message.chat.id
        for phone, package in pairs:
            # 认领 + 当天序号 + 日志在同一事务完成（BEGIN IMMEDIATE），并发查询不会产生重复序号；
            # 只数命中（found=1），未命中/次数已用完不占序号，保证显示序号连续自增 1。
            record, seq, log_id = await db.claim_and_log(phone, chat_id, package)
            if record is None:
                total = await db.count_today_records(phone)
                if total:
                    sent = await message.reply(f"手机号 {phone} 今日已上传 {total} 次，查询次数已用完。")
                else:
                    sent = await message.reply(f"未找到手机号 {phone} 的相关记录。")
                await db.update_query_log(log_id, sent.message_id if sent else None, found=False)
                continue
            try:
                msg_id = await _send_query_result(message, phone, record, seq)
            except Exception as e:
                # 发送失败（如 caption 超长、图片损坏）：撤销认领并记为未发送，
                # 否则记录已被 consumed 却永远发不出去，且中断同批后续号码
                print(f"[on_text] 发送查询结果失败 record={record['id']} err={type(e).__name__}", flush=True)
                await db.release_claim(record["id"])
                await db.update_query_log(log_id, found=False)
                try:
                    await message.reply(f"手机号 {phone} 查询结果发送失败，请稍后重试。")
                except Exception:
                    pass
                continue
            await db.update_query_log(log_id, msg_id, found=True)
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
    for admin_id in await _all_admin_ids():
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
    if await _is_admin(message.from_user.id):
        text = ("你好，我是短信识别台账机器人。\n\n"
                "把机器人拉进群后：群里会播报群 ID，我会私聊发你按钮登记「识别群 / 查询群」。\n\n"
                "管理命令：\n"
                "· /groups — 查看/删除已登记群\n"
                "· /addgroup <群ID> — 手动登记群\n"
                "· /admins — 查看管理员\n"
                "· /addadmin <用户ID> — 添加管理员\n"
                "· /deladmin <用户ID> — 移除管理员\n"
                "· /delrecord <记录ID> — 删除台账记录\n\n"
                "群内用法：识别群发短信截图，查询群发手机号查图。")
    else:
        text = "你好，我是短信识别台账机器人。请在已登记的群内使用：识别群发截图、查询群发手机号。"
    await message.answer(text)


@dp.message(Command("groups"))
async def cmd_groups(message: Message):
    if not await _is_admin(message.from_user.id):
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
    if not await _is_admin(message.from_user.id):
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


def _parse_user_id(arg):
    """把命令参数解析为 Telegram 用户 ID（纯数字），失败返回 None。"""
    try:
        return int((arg or "").strip())
    except ValueError:
        return None


@dp.message(Command("admins"))
async def cmd_admins(message: Message):
    if not await _is_admin(message.from_user.id):
        await message.reply("无权限。")
        return
    super_admins = sorted(config.ADMIN_USER_IDS)
    db_admins = await db.list_admins()
    lines = ["当前管理员："]
    lines += [f"· {uid}（超管，来自配置）" for uid in super_admins]
    lines += [f"· {uid}" for uid in db_admins]
    if not super_admins and not db_admins:
        lines.append("（暂无，请在 .env 配置 ADMIN_USER_IDS 作为首位超管）")
    await message.reply("\n".join(lines))


@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject):
    if not await _is_admin(message.from_user.id):
        await message.reply("无权限。")
        return
    uid = _parse_user_id(command.args)
    if uid is None:
        await message.reply("用法：/addadmin <用户ID>（纯数字）")
        return
    if config.is_admin(uid):
        await message.reply(f"{uid} 已是配置里的超管，无需重复添加。")
        return
    await db.add_admin(uid, message.from_user.id)
    await message.reply(f"✅ 已添加管理员 {uid}。")


@dp.message(Command("deladmin"))
async def cmd_deladmin(message: Message, command: CommandObject):
    if not await _is_admin(message.from_user.id):
        await message.reply("无权限。")
        return
    uid = _parse_user_id(command.args)
    if uid is None:
        await message.reply("用法：/deladmin <用户ID>（纯数字）")
        return
    if config.is_admin(uid):
        await message.reply(f"{uid} 是配置里的超管，请到 .env 的 ADMIN_USER_IDS 中移除。")
        return
    await db.remove_admin(uid)
    await message.reply(f"已移除管理员 {uid}（若此前不是管理员则无影响）。")


@dp.message(Command("delrecord"))
async def cmd_delrecord(message: Message, command: CommandObject):
    """按记录 ID 删除台账记录（可同时删本地图片），ID 在网页台账页首列查看。"""
    if not await _is_admin(message.from_user.id):
        await message.reply("无权限。")
        return
    ids = []
    for tok in (command.args or "").split():
        try:
            ids.append(int(tok))
        except ValueError:
            pass
    if not ids:
        await message.reply("用法：/delrecord <记录ID> [更多ID...]\n记录 ID 见网页台账页首列。")
        return
    deleted, missing = [], []
    for rid in ids:
        rec = await db.get_record(rid)
        if not rec:
            missing.append(rid)
            continue
        await db.delete_record(rid)
        # 同步删掉本地图片，避免孤儿文件
        path = rec.get("image_path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        deleted.append(rid)
    parts = []
    if deleted:
        parts.append(f"✅ 已删除记录：{', '.join(map(str, deleted))}")
    if missing:
        parts.append(f"未找到记录：{', '.join(map(str, missing))}")
    await message.reply("\n".join(parts))


@dp.callback_query()
async def on_callback(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("无权限。", show_alert=True)
        return
    parts = (cb.data or "").split(":")
    if not parts:
        await cb.answer()
        return
    action = parts[0]

    if action == "role" and len(parts) >= 3:
        try:
            cid = int(parts[2])
        except ValueError:
            await cb.answer("数据异常，已忽略。", show_alert=True)
            return
        role = parts[1]
        try:
            chat = await cb.bot.get_chat(cid)
            title = chat.title or str(cid)
        except Exception:
            title = str(cid)
        await db.upsert_group(cid, title, role, cb.from_user.id)
        if cb.message:
            await cb.message.edit_text(f"✅ 已登记群「{title}」为{_role_label(role)}。")
    elif action == "del" and len(parts) >= 2:
        try:
            cid = int(parts[1])
        except ValueError:
            await cb.answer("数据异常，已忽略。", show_alert=True)
            return
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


# 查询群格式：发号：15775412943 包号：5654-44—44（冒号中英文皆可，"号"字可省）
FA_HAO_RE = re.compile(r"发号?\s*[:：]\s*(1[3-9]\d{9})")
BAO_HAO_RE = re.compile(r"包号?\s*[:：]\s*(\S+)")


def _parse_queries(text):
    """解析查询消息，返回 [(phone, package), ...]，按出现顺序去重手机号。

    优先按「发号：…」取号；没有发号标记时兼容旧的纯手机号批量查询。
    包号先按位置与发号一一配对，再按手机号去重（先去重再配对会让重复号后面的包号错位）。
    缺省用最后一个包号（同一批常为同包）。
    """
    phones = FA_HAO_RE.findall(text) or _unique_phones(text)
    pkgs = BAO_HAO_RE.findall(text)
    seen, pairs = set(), []
    for i, p in enumerate(phones):
        if p in seen:
            continue
        seen.add(p)
        pkg = pkgs[i] if i < len(pkgs) else (pkgs[-1] if pkgs else "")
        pairs.append((p, pkg))
    return pairs


async def _send_query_result(message: Message, phone: str, record: dict, seq: int = 1):
    """发送一条已认领的查询记录（图+手机号+网址+统计），返回消息 message_id。"""
    caption = _format_query_result(phone, record, seq)
    path = record.get("image_path")
    if path and os.path.exists(path):
        sent = await message.answer_photo(FSInputFile(path), caption=caption)
    else:
        sent = await message.reply(caption)
    return sent.message_id if sent else None


def _format_summary(d):
    """识别群摘要：固定只显示号码与网址两行。"""
    phones = _phones_from_result(d)
    phone_str = "，".join(sorted(phones)) if phones else "无"
    urls = (d.get("urls") or "").strip()
    url_str = urls if urls else "无"
    return f"✅ 识别完成\n号码：{phone_str}\n网址：{url_str}"


def _format_query_result(phone, r, seq=1):
    """查询命中结果 caption：手机号 + 网址（如有）+ 说明（如有）+ 本群当天查询次数。"""
    parts = [phone]
    urls = (r.get("urls") or "").strip()
    if urls:
        parts.append(urls)
    # 发图附带的说明文字，放在网址下一行
    note = (r.get("note") or "").strip()
    if note:
        parts.append(note)
    parts.append(f"统计：{seq}")
    return "\n".join(parts)


async def run_bot(bot_token: str):
    print(f"[bot] run_bot 启动，token 长度={len(bot_token)}", flush=True)
    bot = Bot(token=bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="查看用法"),
        BotCommand(command="groups", description="查看/删除已登记群"),
        BotCommand(command="addgroup", description="手动登记群（/addgroup <群ID>）"),
        BotCommand(command="admins", description="查看管理员"),
        BotCommand(command="addadmin", description="添加管理员（/addadmin <用户ID>）"),
        BotCommand(command="deladmin", description="移除管理员（/deladmin <用户ID>）"),
        BotCommand(command="delrecord", description="删除台账记录（/delrecord <记录ID>）"),
    ])
    print("[bot] 命令已注册，开始 polling", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    print("bot 模块无运行时自校验")
