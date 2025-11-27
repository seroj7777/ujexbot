import logging
logging.basicConfig(level=logging.INFO)
# ─────────────────────────────────────────────────────────────────────────────
# Telegram Moderator Bot — MVP v1
# Stack: Python 3.11+, aiogram 3.x, PostgreSQL (SQLAlchemy), Redis (aioredis)
# Features in this MVP:
#  - Join gate ("captcha via subscription"): require user to be subscribed to a channel before writing
#  - Content filters: profanity, links, @username mentions (configurable)
#  - Warn system with thresholds → auto-mute/ban; adjustable limits and mute durations
#  - Media permissions toggles (photo/video/voice/gif/stickers)
#  - Logging to database (deleted msgs, warns, bans) - viewable with !logs command
#  - Commands: !help, !rules, !me, !report, !warn, !kick, !ban, !unban, !mute, !unmute, !logs
#              !setwarns, !setmutetime, /settings, /setcaptcha
#  - Per-chat settings persisted in Postgres
#  - Admin vs members privileges
# Notes:
#  - For subscription check, bot must be admin in the target channel (or at least be able to read members).
#  - Replace placeholders (PROFANITY, TOKEN, DB) with your actual data.
# ─────────────────────────────────────────────────────────────────────────────

# Project layout (single file for canvas; split into modules in real repo):
#  - config: Env settings
#  - db: SQLAlchemy models + session
#  - utils: helpers for checks, formatting
#  - filters: content filters + rate limiting (slowmode)
#  - handlers: commands + message handlers
#  - services: logging, punishments, subscription gate

import os
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router # type: ignore
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Integer, String, JSON, Text, ForeignKey,
    create_engine, select, update, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
TOKEN = (os.getenv("BOT_TOKEN") or "8430358415:AAF-j2MpV1rhTaU7JuxYGmB6btuUVx5tpgM")

# ⚠️ Set your token above or export BOT_TOKEN in your shell before running.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///bot.db")
PROFANITY = {"сука", "блять", "нахуй", "хуй", "пизда", "ебать"}  # пример, расширьте словарь
LINK_RE = re.compile(r"https?://|t\.me/|\bwww\.", re.IGNORECASE)
AT_USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{5,}\b")

if not TOKEN:
    raise SystemExit("Please set BOT_TOKEN env var")

# ─────────────────────────────────────────────────────────────────────────────
# Database (SQLAlchemy)
# ─────────────────────────────────────────────────────────────────────────────
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Chat(Base):
    __tablename__ = "chats"
    chat_id = Column(BigInteger, primary_key=True)
    title = Column(String)
    # Settings
    required_channel = Column(String, nullable=True)   # e.g. @mychannel
    log_channel_id = Column(BigInteger, nullable=True)
    warns_limit = Column(Integer, default=3)
    mute_minutes = Column(Integer, default=120)        # default 2h
    slowmode_seconds = Column(Integer, default=0)      # 0 = off
    allow_links = Column(Boolean, default=False)
    allow_usernames = Column(Boolean, default=True)
    allow_media = Column(Boolean, default=True)
    allow_gif = Column(Boolean, default=True)
    allow_stickers = Column(Boolean, default=True)
    allow_voice = Column(Boolean, default=True)
    rules_text = Column(Text, default="Правила чата не заданы. Используйте !rules для показа.")
    created_at = Column(DateTime, default=datetime.utcnow)

class UserState(Base):
    __tablename__ = "user_state"
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    username = Column(String, nullable=True)  # Store username for lookup
    warns = Column(Integer, default=0)
    last_message_at = Column(DateTime, nullable=True)
    muted_until = Column(DateTime, nullable=True)

class ModLog(Base):
    __tablename__ = "mod_logs"
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    actor_id = Column(BigInteger, nullable=True)  # admin who acted (or bot)
    target_id = Column(BigInteger, nullable=True)
    action = Column(String)  # delete, warn, mute, ban, kick
    reason = Column(String)
    meta = Column(JSON, default={})
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─────────────────────────────────────────────────────────────────────────────
# Database migration: Add username column if not exists
# ─────────────────────────────────────────────────────────────────────────────
def migrate_database():
    """Add username column to user_state table if it doesn't exist"""
    try:
        from sqlalchemy import inspect, text
        insp = inspect(engine)
        cols = [c['name'] for c in insp.get_columns('user_state')]
        if 'username' not in cols:
            logging.info("Adding 'username' column to user_state table...")
            try:
                with engine.begin() as con:
                    con.execute(text("ALTER TABLE user_state ADD COLUMN username VARCHAR"))
                logging.info("Successfully added 'username' column")
            except Exception as e:
                logging.error(f"Error adding username column: {e}")
    except Exception as e:
        logging.error(f"Error in database migration: {e}")

migrate_database()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except Exception:
        return False

def ensure_chat(db, chat_id: int, title: Optional[str]) -> Chat:
    chat = db.get(Chat, chat_id)
    if not chat:
        chat = Chat(chat_id=chat_id, title=title or "")
        db.add(chat)
        db.commit()
    return chat

def update_user_state(db, chat_id: int, user_id: int, username: Optional[str] = None):
    """Update or create user state with username tracking"""
    st = db.execute(select(UserState).where(
        UserState.chat_id == chat_id,
        UserState.user_id == user_id
    )).scalar_one_or_none()
    
    if not st:
        st = UserState(chat_id=chat_id, user_id=user_id, username=username)
        db.add(st)
    elif username and st.username != username:
        # Update username if changed
        st.username = username
    
    return st

async def get_target_user(msg: Message, db) -> Tuple[Optional[any], str]:
    """
    Get target user from reply or mention.
    Returns: (user_object, reason_text)
    """
    target = None
    reason_text = msg.text.partition(" ")[2].strip()
    username_to_find = None
    
    # First, try to get target from reply
    if msg.reply_to_message:
        return msg.reply_to_message.from_user, reason_text
    
    # Check for mention in entities
    if msg.entities:
        for entity in msg.entities:
            if entity.type == "mention":
                username_to_find = msg.text[entity.offset:entity.offset + entity.length].lstrip('@')
                reason_text = reason_text.replace(f"@{username_to_find}", "").strip()
                break
            elif entity.type == "text_mention":
                return entity.user, reason_text
    
    # Try to parse @username from text manually
    if not username_to_find and reason_text and reason_text.startswith('@'):
        parts = reason_text.split(maxsplit=1)
        username_to_find = parts[0].lstrip('@')
        reason_text = parts[1] if len(parts) > 1 else ""
    
    # If we have a username to find, try multiple methods
    if username_to_find:
        # Method 1: Search in database
        user_id = find_user_by_username(db, msg.chat.id, username_to_find)
        if user_id:
            try:
                chat_member = await bot.get_chat_member(msg.chat.id, user_id)
                target = chat_member.user
            except Exception:
                pass
        
        # Method 2: Try get_chat_member with @username
        if not target:
            try:
                chat_member = await bot.get_chat_member(msg.chat.id, f"@{username_to_find}")
                target = chat_member.user
            except Exception:
                pass
        
        # Method 3: Search in admins
        if not target:
            try:
                admins = await bot.get_chat_administrators(msg.chat.id)
                for admin in admins:
                    if admin.user.username and admin.user.username.lower() == username_to_find.lower():
                        target = admin.user
                        break
            except Exception:
                pass
    
    return target, reason_text

def find_user_by_username(db, chat_id: int, username: str) -> Optional[int]:
    """Find user ID by username in the database"""
    username_lower = username.lower()
    st = db.execute(select(UserState).where(
        UserState.chat_id == chat_id,
        UserState.username != None
    )).scalars().all()
    
    for user in st:
        if user.username and user.username.lower() == username_lower:
            return user.user_id
    
    return None

def human_td(seconds: int) -> str:
    if seconds < 60: return f"{seconds}с"
    m, s = divmod(seconds, 60)
    if m < 60: return f"{m}м"
    h, m = divmod(m, 60)
    return f"{h}ч {m}м"

async def log_action(bot: Bot, chat_row: Chat, action: str, reason: str, actor_id: Optional[int], target_id: Optional[int], meta: dict):
    # Save to database only
    with SessionLocal() as db:
        db.add(ModLog(chat_id=chat_row.chat_id, actor_id=actor_id, target_id=target_id, action=action, reason=reason, meta=meta))
        db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Subscription Gate ("captcha via subscription")
# ─────────────────────────────────────────────────────────────────────────────
# Track subscription state per user to detect unsubscribes
class SubscriptionState(Base):
    __tablename__ = "subscription_state"
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    verified_at = Column(DateTime, nullable=True)  # when user last verified subscription
    last_checked = Column(DateTime, nullable=True)  # last periodic check

# Create table if not exists
Base.metadata.create_all(engine)

async def check_subscription(bot: Bot, required_channel: str, user_id: int) -> bool:
    if not required_channel:
        return True
    try:
        member = await bot.get_chat_member(required_channel, user_id)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}
    except Exception:
        return False

async def subscription_keyboard(channel: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Открыть канал", url=f"https://t.me/{channel.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
    ])

async def enforce_subscription_captcha(bot: Bot, chat_id: int, user_id: int, chat_row: Chat):
    """Re-restrict user and send captcha message"""
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions={"can_send_messages": False})
    except Exception:
        pass
    try:
        await bot.send_message(
            chat_id,
            f"📢 Для участия в чате необходимо подписаться на канал:\n"
            f"<code>{chat_row.required_channel}</code>\n\n"
            f"После подписки нажмите «Проверить подписку».",
            reply_markup=await subscription_keyboard(chat_row.required_channel),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

async def periodic_subscription_check():
    """Background task to check if verified users have unsubscribed"""
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        with SessionLocal() as db:
            now = datetime.utcnow()
            # Get users verified within last 24h to avoid spam
            states = db.execute(
                select(SubscriptionState).where(
                    SubscriptionState.verified_at != None,
                    SubscriptionState.last_checked < now - timedelta(minutes=10)
                )
            ).scalars().all()
            
            for state in states:
                chat_row = db.get(Chat, state.chat_id)
                if not chat_row or not chat_row.required_channel:
                    continue
                
                # Check if still subscribed
                is_subscribed = await check_subscription(bot, chat_row.required_channel, state.user_id)
                state.last_checked = now
                
                if not is_subscribed:
                    # User unsubscribed - reset verification and re-restrict
                    state.verified_at = None
                    await enforce_subscription_captcha(bot, state.chat_id, state.user_id, chat_row)
                
                db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Bot + Routers
# ─────────────────────────────────────────────────────────────────────────────
bot = Bot(token=TOKEN, default=DefaultBotProperties())
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ─────────────────────────────────────────────────────────────────────────────
# /start in PM
# ─────────────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "Привет! Я модератор-бот.\n"
        "Добавьте меня админом в ваш чат и используйте /settings в группе."
    )

# ─────────────────────────────────────────────────────────────────────────────
# Chat member updates: greet & mark for subscription
# ─────────────────────────────────────────────────────────────────────────────
@router.chat_member()
async def on_member(update: ChatMemberUpdated):
    if update.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    with SessionLocal() as db:
        chat_row = ensure_chat(db, update.chat.id, update.chat.title)
    
    old = update.old_chat_member
    new = update.new_chat_member
    
    # Check if user joined (including rejoining after leaving)
    if new and new.user and new.status == ChatMemberStatus.MEMBER:
        if chat_row.required_channel:
            # Reset verification state when user joins/rejoins
            with SessionLocal() as db:
                state = db.execute(
                    select(SubscriptionState).where(
                        SubscriptionState.chat_id == update.chat.id,
                        SubscriptionState.user_id == new.user.id
                    )
                ).scalar_one_or_none()
                if state:
                    state.verified_at = None  # Reset verification
                    state.last_checked = None
                    db.commit()
            
            # Restrict user immediately
            try:
                await bot.restrict_chat_member(update.chat.id, new.user.id, permissions={"can_send_messages": False})
            except Exception:
                pass
            # Send captcha message
            try:
                await bot.send_message(
                    update.chat.id,
                    f"👋 <b>Добро пожаловать, {new.user.mention_html()}!</b>\n\n"
                    f"📢 Для участия в чате необходимо подписаться на наш канал:\n"
                    f"<code>{chat_row.required_channel}</code>\n\n"
                    f"После подписки нажмите кнопку «Проверить подписку» ниже.",
                    reply_markup=await subscription_keyboard(chat_row.required_channel),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    
    # Check if user left the group
    elif old and old.status == ChatMemberStatus.MEMBER and new and new.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        # Reset verification state when user leaves
        with SessionLocal() as db:
            state = db.execute(
                select(SubscriptionState).where(
                    SubscriptionState.chat_id == update.chat.id,
                    SubscriptionState.user_id == new.user.id
                )
            ).scalar_one_or_none()
            if state:
                state.verified_at = None
                state.last_checked = None
                db.commit()

@router.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    chat_id = cb.message.chat.id if cb.message else None
    user_id = cb.from_user.id
    if not chat_id:
        return await cb.answer("Ошибка контекста", show_alert=True)
    with SessionLocal() as db:
        chat_row = ensure_chat(db, chat_id, None)
    ok = await check_subscription(bot, chat_row.required_channel, user_id)
    if ok:
        # Unban/unrestrict user
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions={
                "can_send_messages": True, "can_send_media_messages": chat_row.allow_media,
                "can_send_polls": True, "can_send_other_messages": True,
                "can_add_web_page_previews": chat_row.allow_links
            })
        except Exception:
            pass
        
        # Mark user as verified in database
        with SessionLocal() as db:
            state = db.execute(
                select(SubscriptionState).where(
                    SubscriptionState.chat_id == chat_id,
                    SubscriptionState.user_id == user_id
                )
            ).scalar_one_or_none()
            if not state:
                state = SubscriptionState(chat_id=chat_id, user_id=user_id)
                db.add(state)
            state.verified_at = datetime.utcnow()
            state.last_checked = datetime.utcnow()
            db.commit()
        
        await cb.answer("Подписка подтверждена!", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await cb.answer("Не вижу подписку. Подпишитесь и попробуйте снова.", show_alert=True)

# ─────────────────────────────────────────────────────────────────────────────
# Commands visible in groups
# ─────────────────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "Доступные команды:\n"
    "!help — команды\n"
    "!rules — правила\n"
    "!me — мои предупреждения\n"
    "!report — пожаловаться (в ответ на сообщение)\n\n"
    "Админы (работают ответом или @username):\n"
    "!warn @username [причина]\n"
    "!kick @username\n"
    "!ban @username [причина]\n"
    "!unban @username\n"
    "!mute @username [минуты]\n"
    "!unmute @username\n"
    "!logs [количество] — просмотр логов (макс. 100)\n\n"
    "Настройки:\n"
    "!setwarns <N>, !setmutetime <мин>\n"
    "/settings, /setcaptcha"
)

@router.message(F.text.lower().in_({"!help", "/help"}))
async def cmd_help(msg: Message):
    await msg.reply(HELP_TEXT)

@router.message(
        F.text.lower().in_({"!rules"}))
async def cmd_rules(msg: Message):
    with SessionLocal() as db:
        chat_row = ensure_chat(db, msg.chat.id, msg.chat.title)
        await msg.reply(chat_row.rules_text)

@router.message(F.text.lower().in_({"!me"}))
async def cmd_me(msg: Message):
    with SessionLocal() as db:
        st = db.execute(select(UserState).where(UserState.chat_id==msg.chat.id, UserState.user_id==msg.from_user.id)).scalar_one_or_none()
        warns = st.warns if st else 0
        muted = st.muted_until and st.muted_until > datetime.utcnow()
        muted_left = int((st.muted_until - datetime.utcnow()).total_seconds()) if muted else 0
        await msg.reply(f"Ваши предупреждения: {warns}\nСтатус: {'Muted ('+human_td(muted_left)+')' if muted else 'OK'}")

@router.message(F.text.lower().startswith("!report"))
async def cmd_report(msg: Message):
    if not msg.reply_to_message:
        return await msg.reply("Используйте в ответ на сообщение: !report")
    
    with SessionLocal() as db:
        chat_row = ensure_chat(db, msg.chat.id, msg.chat.title)
    
    # Log to database
    await log_action(bot, chat_row, "report", "user_report", msg.from_user.id, msg.reply_to_message.from_user.id, {
        "message_text": msg.reply_to_message.text or "",
        "message_id": msg.reply_to_message.message_id,
    })
    
    # Prepare report message
    reported_user = msg.reply_to_message.from_user
    reporter = msg.from_user
    message_text = msg.reply_to_message.text or msg.reply_to_message.caption or "[медиа сообщение]"
    
    report_text = (
        f"🚨 <b>ЖАЛОБА</b>\n\n"
        f"От: {reporter.mention_html()} (ID: <code>{reporter.id}</code>)\n"
        f"На: {reported_user.mention_html()} (ID: <code>{reported_user.id}</code>)\n\n"
        f"Сообщение:\n<i>{message_text[:200]}</i>\n\n"
        f"Группа: {msg.chat.title}\n"
        f"Ссылка: https://t.me/c/{str(msg.chat.id)[4:]}/{msg.reply_to_message.message_id}"
    )
    
    # Send to all admins via DM
    try:
        admins = await bot.get_chat_administrators(msg.chat.id)
        admin_count = 0
        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                await bot.send_message(admin.user.id, report_text, parse_mode="HTML")
                admin_count += 1
            except Exception:
                # Admin has blocked the bot or hasn't started it
                pass
        
        if admin_count > 0:
            await msg.reply(f"✅ Жалоба отправлена {admin_count} админам")
        else:
            await msg.reply("⚠️ Не удалось отправить жалобу админам (они не запустили бота)")
    except Exception:
        await msg.reply("❌ Ошибка при отправке жалобы")
    
    # Delete the report command to keep chat clean
    try:
        await msg.delete()
    except Exception:
        pass

@router.message(F.text.lower().startswith("!logs"))
async def cmd_logs(msg: Message):
    # Only admins can view logs
    ok = await is_admin(bot, msg.chat.id, msg.from_user.id)
    if not ok:
        return await msg.reply("Команда только для админов")
    
    # Get limit from command (default 20)
    parts = msg.text.split()
    limit = 20
    if len(parts) > 1 and parts[1].isdigit():
        limit = min(int(parts[1]), 100)  # Max 100 logs
    
    with SessionLocal() as db:
        # Fetch recent logs for this chat
        logs = db.execute(
            select(ModLog)
            .where(ModLog.chat_id == msg.chat.id)
            .order_by(ModLog.created_at.desc())
            .limit(limit)
        ).scalars().all()
        
        if not logs:
            return await msg.reply("📝 Логов пока нет")
        
        # Format logs
        log_text = f"📝 <b>Последние {len(logs)} логов:</b>\n\n"
        for log in logs:
            timestamp = log.created_at.strftime("%d.%m %H:%M")
            actor_str = f"Admin {log.actor_id}" if log.actor_id else "System"
            target_str = f"User {log.target_id}" if log.target_id else "—"
            reason_str = log.reason if log.reason else "—"
            
            log_text += (
                f"🕐 <code>{timestamp}</code>\n"
                f"Action: <b>{log.action}</b>\n"
                f"Reason: <i>{reason_str}</i>\n"
                f"{actor_str} → {target_str}\n\n"
            )
        
        # Send logs as a private reply (delete after reading)
        try:
            # Send to admin privately
            sent_msg = await bot.send_message(msg.from_user.id, log_text, parse_mode="HTML")
            await msg.reply("✅ Логи отправлены вам в личные сообщения")
        except Exception:
            # If bot is blocked by admin, send in group but delete quickly
            sent_msg = await msg.reply(log_text, parse_mode="HTML")
            # Delete original command
            try:
                await msg.delete()
            except Exception:
                pass
            # Auto-delete logs after 30 seconds
            await asyncio.sleep(30)
            try:
                await sent_msg.delete()
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# Admin actions: warn/kick/ban/mute/unmute
# ─────────────────────────────────────────────────────────────────────────────
async def require_admin(msg: Message) -> Tuple[bool, Optional[Chat]]:
    ok = await is_admin(bot, msg.chat.id, msg.from_user.id)
    if not ok:
        await msg.reply("Команда только для админов")
        return False, None
    with SessionLocal() as db:
        chat_row = ensure_chat(db, msg.chat.id, msg.chat.title)
    return True, chat_row

@router.message(F.text.lower().startswith("!warn"))
async def cmd_warn(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    
    target = None
    reason_text = msg.text.partition(" ")[2].strip()
    username_to_find = None
    
    # First, try to get target from reply
    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        reason = reason_text or "Нарушение правил"
    else:
        # Check for mention in entities first
        if msg.entities:
            for entity in msg.entities:
                if entity.type == "mention":
                    # Extract username from text
                    username_to_find = msg.text[entity.offset:entity.offset + entity.length].lstrip('@')
                    reason = reason_text.replace(f"@{username_to_find}", "").strip() or "Нарушение правил"
                    break
                elif entity.type == "text_mention":
                    # Direct mention with user object
                    target = entity.user
                    reason = reason_text or "Нарушение правил"
                    break
        
        # If no entity found, try to parse @username from text manually
        if not target and not username_to_find and reason_text and reason_text.startswith('@'):
            parts = reason_text.split(maxsplit=1)
            username_to_find = parts[0].lstrip('@')
            reason = parts[1] if len(parts) > 1 else "Нарушение правил"
        
        # If we have a username to find, try multiple methods
        if username_to_find and not target:
            # Method 1: Search in database (users who have sent messages)
            with SessionLocal() as db:
                user_id = find_user_by_username(db, msg.chat.id, username_to_find)
                if user_id:
                    try:
                        chat_member = await bot.get_chat_member(msg.chat.id, user_id)
                        target = chat_member.user
                    except Exception:
                        pass
            
            # Method 2: Try get_chat_member with @username
            if not target:
                try:
                    chat_member = await bot.get_chat_member(msg.chat.id, f"@{username_to_find}")
                    target = chat_member.user
                except Exception:
                    pass
            
            # Method 3: Search in admins
            if not target:
                try:
                    admins = await bot.get_chat_administrators(msg.chat.id)
                    for admin in admins:
                        if admin.user.username and admin.user.username.lower() == username_to_find.lower():
                            target = admin.user
                            break
                except Exception:
                    pass
            
            if not target:
                return await msg.reply(f"❌ Пользователь @{username_to_find} не найден.\n"
                                      f"Возможные причины:\n"
                                      f"• Пользователь не отправлял сообщения в этом чате\n"
                                      f"• Username указан неверно\n"
                                      f"• Используйте ответ на сообщение для гарантированного результата")
    
    if not target:
        return await msg.reply("Используйте в ответ на сообщение или упомяните пользователя: !warn @username [причина]")
    
    # Don't allow warning bots or self
    if target.is_bot:
        return await msg.reply("Нельзя предупредить бота")
    
    with SessionLocal() as db:
        st = db.execute(select(UserState).where(UserState.chat_id==msg.chat.id, UserState.user_id==target.id)).scalar_one_or_none()
        if not st:
            st = UserState(chat_id=msg.chat.id, user_id=target.id, warns=0)
            db.add(st)
        st.warns += 1
        db.commit()
        warns = st.warns
    await log_action(bot, chat_row, "warn", reason, msg.from_user.id, target.id, {"warns": warns})
    await msg.reply(f"⚠️ Предупреждение для {target.mention_html()} ({warns}/{chat_row.warns_limit})\nПричина: {reason}", parse_mode="HTML")
    if warns >= chat_row.warns_limit:
        until = datetime.utcnow() + timedelta(minutes=chat_row.mute_minutes)
        try:
            await bot.restrict_chat_member(msg.chat.id, target.id, permissions={"can_send_messages": False}, until_date=until)
        except Exception:
            pass
        with SessionLocal() as db:
            st = db.execute(select(UserState).where(UserState.chat_id==msg.chat.id, UserState.user_id==target.id)).scalar_one_or_none()
            if st:
                st.muted_until = until
                st.warns = 0  # Reset warns to 0 when muted
                db.commit()
        await log_action(bot, chat_row, "auto_mute", "warns_limit", msg.from_user.id, target.id, {"until": str(until)})
        await msg.answer(f"🔇 Автоматический мут {target.mention_html()} на {chat_row.mute_minutes} мин.", parse_mode="HTML")

@router.message(F.text.lower().startswith("!kick"))
async def cmd_kick(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    
    with SessionLocal() as db:
        target, _ = await get_target_user(msg, db)
    
    if not target:
        return await msg.reply("Используйте в ответ на сообщение или упомяните пользователя: !kick @username")
    
    if target.is_bot:
        return await msg.reply("Нельзя кикнуть бота")
    
    try:
        # Kick = ban + unban immediately
        await bot.ban_chat_member(msg.chat.id, target.id)
        await bot.unban_chat_member(msg.chat.id, target.id, only_if_banned=True)
    except Exception as e:
        logging.error(f"Error kicking user: {e}")
        pass
    await log_action(bot, chat_row, "kick", "admin_kick", msg.from_user.id, target.id, {})
    await msg.reply(f"👢 Кикнут {target.mention_html()}", parse_mode="HTML")

@router.message(F.text.lower().startswith("!ban"))
async def cmd_ban(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    
    with SessionLocal() as db:
        target, reason_text = await get_target_user(msg, db)
    
    if not target:
        return await msg.reply("Используйте в ответ на сообщение или упомяните пользователя: !ban @username [причина]")
    
    if target.is_bot:
        return await msg.reply("Нельзя забанить бота")
    
    reason = reason_text or "Нарушение правил"
    
    try:
        await bot.ban_chat_member(msg.chat.id, target.id)
    except Exception:
        pass
    await log_action(bot, chat_row, "ban", reason, msg.from_user.id, target.id, {})
    await msg.reply(f"⛔️ Бан {target.mention_html()} — {reason}", parse_mode="HTML")

@router.message(F.text.lower().startswith("!unban"))
async def cmd_unban(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    
    with SessionLocal() as db:
        target, _ = await get_target_user(msg, db)
    
    if not target:
        return await msg.reply("Используйте в ответ на сообщение или упомяните пользователя: !unban @username")
    
    if target.is_bot:
        return await msg.reply("Нельзя разбанить бота")
    
    try:
        await bot.unban_chat_member(msg.chat.id, target.id, only_if_banned=True)
    except Exception:
        pass
    await log_action(bot, chat_row, "unban", "", msg.from_user.id, target.id, {})
    await msg.reply(f"✅ Разбан {target.mention_html()}", parse_mode="HTML")

@router.message(F.text.lower().startswith("!mute"))
async def cmd_mute(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    
    with SessionLocal() as db:
        target, arg_text = await get_target_user(msg, db)
    
    if not target:
        return await msg.reply("Используйте в ответ на сообщение или упомяните пользователя: !mute @username [минуты]")
    
    if target.is_bot:
        return await msg.reply("Нельзя замутить бота")
    
    # Parse minutes from argument text
    minutes = chat_row.mute_minutes
    if arg_text and arg_text.isdigit():
        minutes = int(arg_text)
    
    until = datetime.utcnow() + timedelta(minutes=minutes)
    try:
        await bot.restrict_chat_member(msg.chat.id, target.id, permissions={"can_send_messages": False}, until_date=until)
    except Exception:
        pass
    with SessionLocal() as db:
        st = db.execute(select(UserState).where(UserState.chat_id==msg.chat.id, UserState.user_id==target.id)).scalar_one_or_none()
        if not st:
            st = UserState(chat_id=msg.chat.id, user_id=target.id)
            db.add(st)
        st.muted_until = until
        st.warns = 0  # Reset warns to 0 when manually muted
        db.commit()
    await log_action(bot, chat_row, "mute", f"{minutes}m", msg.from_user.id, target.id, {})
    await msg.reply(f"🔇 Мут {target.mention_html()} на {minutes} мин.", parse_mode="HTML")

@router.message(F.text.lower().startswith("!unmute"))
async def cmd_unmute(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    
    with SessionLocal() as db:
        target, _ = await get_target_user(msg, db)
    
    if not target:
        return await msg.reply("Используйте в ответ на сообщение или упомяните пользователя: !unmute @username")
    
    if target.is_bot:
        return await msg.reply("Нельзя размутить бота")
    
    try:
        await bot.restrict_chat_member(msg.chat.id, target.id, permissions={
            "can_send_messages": True,
            "can_send_media_messages": chat_row.allow_media,
            "can_send_other_messages": True,
            "can_add_web_page_previews": chat_row.allow_links
        })
    except Exception:
        pass
    with SessionLocal() as db:
        st = db.execute(select(UserState).where(UserState.chat_id==msg.chat.id, UserState.user_id==target.id)).scalar_one_or_none()
        if st:
            st.muted_until = None
            db.commit()
    await log_action(bot, chat_row, "unmute", "", msg.from_user.id, target.id, {})
    await msg.reply(f"🔊 Размут {target.mention_html()}", parse_mode="HTML")

# ─────────────────────────────────────────────────────────────────────────────
# Settings commands (admins)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(Command("settings"))
async def cmd_settings(msg: Message):
    if msg.chat.type == ChatType.PRIVATE:
        return await msg.answer("Эта команда работает в группе")
    ok = await is_admin(bot, msg.chat.id, msg.from_user.id)
    if not ok:
        return await msg.reply("Команда только для админов")
    with SessionLocal() as db:
        chat_row = ensure_chat(db, msg.chat.id, msg.chat.title)
    text = (
        "Текущие настройки\n"
        f"• Требуемый канал: {chat_row.required_channel or '—'}\n"
        f"• Лог-канал: {chat_row.log_channel_id or '—'}\n"
        f"• Лимит предупреждений: {chat_row.warns_limit}\n"
        f"• Время мута (мин): {chat_row.mute_minutes}\n"
        f"• Slowmode (сек): {chat_row.slowmode_seconds}\n"
        f"• Разрешить ссылки: {chat_row.allow_links}\n"
        f"• Разрешить медиа: {chat_row.allow_media}, GIF: {chat_row.allow_gif}, Стикеры: {chat_row.allow_stickers}, Голос: {chat_row.allow_voice}\n"
    )
    await msg.reply(text)

@router.message(Command("setcaptcha"))
async def cmd_setcaptcha(msg: Message):
    ok = await is_admin(bot, msg.chat.id, msg.from_user.id)
    if not ok:
        return await msg.reply("Команда только для админов")
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.reply("Использование: /setcaptcha @channelusername или /setcaptcha off")
    arg = parts[1].strip()
    with SessionLocal() as db:
        chat_row = ensure_chat(db, msg.chat.id, msg.chat.title)
        chat_row.required_channel = None if arg.lower()=="off" else arg
        db.commit()
    await msg.reply(f"✅ Капча через подписку: {'выключена' if arg=='off' else 'требует подписку на ' + arg}")

@router.message(F.text.lower().startswith("!setwarns"))
async def cmd_setwarns(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.reply("Использование: !setwarns <число>")
    n = int(parts[1])
    with SessionLocal() as db:
        row = ensure_chat(db, msg.chat.id, msg.chat.title)
        row.warns_limit = n
        db.commit()
    await msg.reply(f"✅ Лимит предупреждений: {n}")

@router.message(F.text.lower().startswith("!setmutetime"))
async def cmd_setmutetime(msg: Message):
    ok, chat_row = await require_admin(msg); 
    if not ok: return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.reply("Использование: !setmutetime <минуты>")
    minutes = int(parts[1])
    with SessionLocal() as db:
        row = ensure_chat(db, msg.chat.id, msg.chat.title)
        row.mute_minutes = minutes
        db.commit()
    await msg.reply(f"✅ Время мута по умолчанию: {minutes} мин.")

# ─────────────────────────────────────────────────────────────────────────────
# Content moderation (delete with reason)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text)
async def on_text(msg: Message):
    if msg.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}: return
    with SessionLocal() as db:
        chat_row = ensure_chat(db, msg.chat.id, msg.chat.title)
        
        # Track username for future lookups
        update_user_state(db, msg.chat.id, msg.from_user.id, msg.from_user.username)
        db.commit()
        
        # Check mute
        st = db.execute(select(UserState).where(UserState.chat_id==msg.chat.id, UserState.user_id==msg.from_user.id)).scalar_one_or_none()
        if st and st.muted_until and st.muted_until > datetime.utcnow():
            try: await msg.delete()
            except Exception: pass
            return
        
        # Subscription gate: if required channel set, check subscription on EVERY message
        if chat_row.required_channel:
            # Always check if user is currently subscribed to the channel
            ok = await check_subscription(bot, chat_row.required_channel, msg.from_user.id)
            
            if not ok:
                # User is NOT subscribed - delete message and show captcha
                try: 
                    await msg.delete()
                except Exception: 
                    pass
                
                # Update or create subscription state
                sub_state = db.execute(
                    select(SubscriptionState).where(
                        SubscriptionState.chat_id == msg.chat.id,
                        SubscriptionState.user_id == msg.from_user.id
                    )
                ).scalar_one_or_none()
                
                if not sub_state:
                    sub_state = SubscriptionState(chat_id=msg.chat.id, user_id=msg.from_user.id)
                    db.add(sub_state)
                
                sub_state.verified_at = None  # Reset verification
                sub_state.last_checked = datetime.utcnow()
                db.commit()
                
                # Show captcha
                await enforce_subscription_captcha(bot, msg.chat.id, msg.from_user.id, chat_row)
                return
            else:
                # User IS subscribed - update verification state
                sub_state = db.execute(
                    select(SubscriptionState).where(
                        SubscriptionState.chat_id == msg.chat.id,
                        SubscriptionState.user_id == msg.from_user.id
                    )
                ).scalar_one_or_none()
                
                if not sub_state:
                    sub_state = SubscriptionState(chat_id=msg.chat.id, user_id=msg.from_user.id)
                    db.add(sub_state)
                
                sub_state.verified_at = datetime.utcnow()
                sub_state.last_checked = datetime.utcnow()
                db.commit()
        
        text = msg.text or ""
        reason = None
        is_profanity = False
        
        if not chat_row.allow_links and LINK_RE.search(text):
            reason = "ссылка"
        elif not chat_row.allow_usernames and AT_USERNAME_RE.search(text):
            reason = "username"
        else:
            # profanity
            low = text.lower()
            if any(w in low for w in PROFANITY):
                reason = "мат"
                is_profanity = True
        
        if reason:
            try:
                await msg.delete()
            except Exception:
                pass
            
            # If profanity, add a warning
            if is_profanity:
                st = db.execute(select(UserState).where(
                    UserState.chat_id==msg.chat.id, 
                    UserState.user_id==msg.from_user.id
                )).scalar_one_or_none()
                
                if not st:
                    st = UserState(chat_id=msg.chat.id, user_id=msg.from_user.id, warns=0)
                    db.add(st)
                
                st.warns += 1
                db.commit()
                warns = st.warns
                
                await log_action(bot, chat_row, "delete", reason, None, msg.from_user.id, {
                    "text": text[:200], 
                    "warns": warns
                })
                
                try:
                    await bot.send_message(
                        msg.chat.id, 
                        f"⚠️ {msg.from_user.mention_html()} — нарушение: {reason} ({warns}/{chat_row.warns_limit})",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                
                # Check if user should be auto-muted
                if warns >= chat_row.warns_limit:
                    until = datetime.utcnow() + timedelta(minutes=chat_row.mute_minutes)
                    try:
                        await bot.restrict_chat_member(msg.chat.id, msg.from_user.id, permissions={"can_send_messages": False}, until_date=until)
                    except Exception:
                        pass
                    
                    with SessionLocal() as db2:
                        st2 = db2.execute(select(UserState).where(
                            UserState.chat_id==msg.chat.id, 
                            UserState.user_id==msg.from_user.id
                        )).scalar_one_or_none()
                        if st2:
                            st2.muted_until = until
                            st2.warns = 0  # Reset warns when muted
                            db2.commit()
                    
                    await log_action(bot, chat_row, "auto_mute", "warns_limit", None, msg.from_user.id, {"until": str(until)})
                    try:
                        await bot.send_message(
                            msg.chat.id, 
                            f"🔇 Автоматический мут {msg.from_user.mention_html()} на {chat_row.mute_minutes} мин.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            else:
                # For links and usernames, just log and notify
                await log_action(bot, chat_row, "delete", reason, None, msg.from_user.id, {"text": text[:200]})
                try:
                    await bot.send_message(msg.chat.id, f"@{msg.from_user.username or msg.from_user.id} — нарушение: {reason}")
                except Exception:
                    pass

# Media gating (basic)
@router.message(F.animation | F.sticker | F.voice | F.photo | F.video)
async def on_media(msg: Message):
    with SessionLocal() as db:
        chat_row = ensure_chat(db, msg.chat.id, msg.chat.title)
        
        # Track username for future lookups
        update_user_state(db, msg.chat.id, msg.from_user.id, msg.from_user.username)
        db.commit()
        
        # Check mute first
        st = db.execute(select(UserState).where(UserState.chat_id==msg.chat.id, UserState.user_id==msg.from_user.id)).scalar_one_or_none()
        if st and st.muted_until and st.muted_until > datetime.utcnow():
            try: await msg.delete()
            except Exception: pass
            return
        
        # Subscription gate: check on EVERY message
        if chat_row.required_channel:
            # Always check if user is currently subscribed to the channel
            ok = await check_subscription(bot, chat_row.required_channel, msg.from_user.id)
            
            if not ok:
                # User is NOT subscribed - delete message and show captcha
                try: 
                    await msg.delete()
                except Exception: 
                    pass
                
                # Update or create subscription state
                sub_state = db.execute(
                    select(SubscriptionState).where(
                        SubscriptionState.chat_id == msg.chat.id,
                        SubscriptionState.user_id == msg.from_user.id
                    )
                ).scalar_one_or_none()
                
                if not sub_state:
                    sub_state = SubscriptionState(chat_id=msg.chat.id, user_id=msg.from_user.id)
                    db.add(sub_state)
                
                sub_state.verified_at = None  # Reset verification
                sub_state.last_checked = datetime.utcnow()
                db.commit()
                
                # Show captcha
                await enforce_subscription_captcha(bot, msg.chat.id, msg.from_user.id, chat_row)
                return
            else:
                # User IS subscribed - update verification state
                sub_state = db.execute(
                    select(SubscriptionState).where(
                        SubscriptionState.chat_id == msg.chat.id,
                        SubscriptionState.user_id == msg.from_user.id
                    )
                ).scalar_one_or_none()
                
                if not sub_state:
                    sub_state = SubscriptionState(chat_id=msg.chat.id, user_id=msg.from_user.id)
                    db.add(sub_state)
                
                sub_state.verified_at = datetime.utcnow()
                sub_state.last_checked = datetime.utcnow()
                db.commit()
    
    # Check media permissions
    block = (
        (msg.animation and not chat_row.allow_gif) or
        (msg.sticker and not chat_row.allow_stickers) or
        (msg.voice and not chat_row.allow_voice) or
        ((msg.photo or msg.video) and not chat_row.allow_media)
    )
    if block:
        try: await msg.delete()
        except Exception: pass
        await log_action(bot, chat_row, "delete", "media_block", None, msg.from_user.id, {"type": "media"})

# ─────────────────────────────────────────────────────────────────────────────
# Background Tasks: Auto-unmute scheduler
# ─────────────────────────────────────────────────────────────────────────────
async def auto_unmute_scheduler():
    """Background task to automatically unmute users whose mute time has expired"""
    while True:
        await asyncio.sleep(60)  # Check every minute
        now = datetime.utcnow()
        
        with SessionLocal() as db:
            # Find all users whose mute has expired
            expired_mutes = db.execute(
                select(UserState).where(
                    UserState.muted_until != None,
                    UserState.muted_until <= now
                )
            ).scalars().all()
            
            for user_state in expired_mutes:
                try:
                    # Get chat info
                    chat_row = db.get(Chat, user_state.chat_id)
                    if not chat_row:
                        continue
                    
                    # Unrestrict the user
                    await bot.restrict_chat_member(
                        user_state.chat_id,
                        user_state.user_id,
                        permissions={
                            "can_send_messages": True,
                            "can_send_media_messages": chat_row.allow_media,
                            "can_send_polls": True,
                            "can_send_other_messages": True,
                            "can_add_web_page_previews": chat_row.allow_links
                        }
                    )
                    
                    # Clear muted_until in database
                    user_state.muted_until = None
                    
                    # Log the auto-unmute
                    await log_action(
                        bot, chat_row, "auto_unmute", "mute_expired",
                        None, user_state.user_id, {"expired_at": str(now)}
                    )
                    
                    # Notify in chat (optional)
                    try:
                        try:
                            member = await bot.get_chat_member(user_state.chat_id, user_state.user_id)
                            username = f"@{member.user.username}" if member.user.username else f"ID: {user_state.user_id}"
                        except Exception:
                            username = f"ID: {user_state.user_id}"
                        
                        member = await bot.get_chat_member(user_state.chat_id, user_state.user_id)
                        if member.user.username:
                            username = f"@{member.user.username}"
                        elif member.user.first_name:
                            username = member.user.first_name
                        else:
                            username = "Пользователь"
                        
                        await bot.send_message(
                            user_state.chat_id,
                            f"🔊 {username} автоматически размучен — время мута истекло."
                        )
                    except Exception:
                        pass
                        
                except Exception as e:
                    logging.error(f"Error auto-unmuting user {user_state.user_id} in chat {user_state.chat_id}: {e}")
                    # Still clear the muted_until to prevent repeated errors
                    user_state.muted_until = None
            
            # Commit all changes
            if expired_mutes:
                db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    # Start background tasks
    asyncio.create_task(auto_unmute_scheduler())
    asyncio.create_task(periodic_subscription_check())
    
    # Ensure no webhook is set; avoid getUpdates conflicts
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message","chat_member","callback_query"])  # switch to webhooks in prod

if __name__ == "__main__":
    asyncio.run(main())


# ─────────────────────────────────────────────────────────────────────────────
# Docker deployment (Dockerfile, docker-compose.yml, .env)
# ─────────────────────────────────────────────────────────────────────────────
# Create three files alongside this script:
#
# 1. Dockerfile:
# --------------
# syntax=docker/dockerfile:1
# FROM python:3.11-slim
# WORKDIR /app
# ENV PYTHONDONTWRITEBYTECODE=1 \
#     PYTHONUNBUFFERED=1
# RUN apt-get update && apt-get install -y build-essential libpq-dev && rm -rf /var/lib/apt/lists/*
# COPY requirements.txt ./
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# CMD ["python", "ujexbot.py"]
#
# 2. requirements.txt:
# --------------------
# aiogram==3.13.1
# SQLAlchemy==2.0.36
# psycopg2-binary==2.9.9
# redis==5.0.8
# aiohttp==3.9.1
#
# 3. docker-compose.yml:
# ----------------------
# version: "3.9"
# services:
#   postgres:
#     image: postgres:16-alpine
#     environment:
#       POSTGRES_USER: modbot
#       POSTGRES_PASSWORD: modbot
#       POSTGRES_DB: modbot
#     volumes:
#       - pgdata:/var/lib/postgresql/data
#     ports:
#       - "5432:5432"
#
#   redis:
#     image: redis:7-alpine
#     ports:
#       - "6379:6379"
#
#   bot:
#     build: .
#     env_file: .env
#     depends_on:
#       - postgres
#       - redis
#     restart: unless-stopped
#
# volumes:
#   pgdata:
#
# Run:
#   docker compose up -d --build
# Logs:
#   docker compose logs -f bot

# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION SETUP (webhooks, TLS, Caddy, compose.prod, reminders)
# ─────────────────────────────────────────────────────────────────────────────
# This section adds a production-ready deployment using webhooks behind Caddy with Let's Encrypt.
# It also enables expiry reminders and basic health checks.

# 1) Project structure (recommended split)
# ---------------------------------------
# .
# ├─ bot.py                # main entry (kept for canvas; you may split later)
# ├─ requirements.txt
# ├─ Dockerfile            # dev/prod base image
# ├─ docker-compose.yml    # dev (polling)
# ├─ docker-compose.prod.yml
# ├─ Caddyfile
# ├─ .env                  # dev env
# ├─ .env.prod             # prod env (DO NOT COMMIT)
# └─ (optional) src/handlers, src/services, src/db, src/config

# 2) Webhook-enabled runner (add to bot.py beneath current polling main)
# ---------------------------------------------------------------------
# You can keep both modes and switch by MODE env: MODE=webhook or MODE=polling

import os as _os
from aiohttp import web as _web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler as _SimpleRequestHandler, setup_application as _setup_application

PUBLIC_URL = _os.getenv("PUBLIC_URL", "")  # e.g. https://bot.example.com
WEBHOOK_PATH = _os.getenv("WEBHOOK_PATH", f"/webhook/{TOKEN}")
WEBHOOK_SECRET = _os.getenv("WEBHOOK_SECRET", "")
PORT = int(_os.getenv("PORT", "8080"))
MODE = _os.getenv("MODE", "polling")  # polling | webhook

async def on_startup_webhook(app: _web.Application):
    # set webhook on startup
    url = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH
    await bot.set_webhook(url=url, secret_token=WEBHOOK_SECRET)

async def on_shutdown_webhook(app: _web.Application):
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass

async def main_webhook():
    # Start background tasks
    asyncio.create_task(auto_unmute_scheduler())
    asyncio.create_task(periodic_subscription_check())
    
    app = _web.Application()
    _SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    _setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup_webhook)
    app.on_shutdown.append(on_shutdown_webhook)
    # health endpoint
    async def health(_): return _web.json_response({"ok": True, "ts": datetime.utcnow().isoformat()})
    app.router.add_get("/healthz", health)
    runner = _web.AppRunner(app)
    await runner.setup()
    site = _web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    while True:
        await asyncio.sleep(3600)

# Replace previous __main__ section with the switcher:
# if __name__ == "__main__":
#     if MODE == "webhook" and PUBLIC_URL:
#         asyncio.run(main_webhook())
#     else:
#         asyncio.run(main())

# 3) Caddyfile (reverse proxy + automatic TLS)
# --------------------------------------------
# Use a real domain that points to your server public IP. Ensure ports 80 and 443 open.
# File: ./Caddyfile
#
# {your_domain}
#     encode zstd gzip
#     tls {your_email@example.com}
#     @webhook path /webhook/*
#     handle @webhook {
#         reverse_proxy bot:8080
#     }
#     handle_path /healthz* {
#         reverse_proxy bot:8080
#     }
#     # default: show simple 200 page
#     respond "OK" 200

# 4) docker-compose.prod.yml
# --------------------------
# File: ./docker-compose.prod.yml
#
# version: "3.9"
# services:
#   postgres:
#     image: postgres:16-alpine
#     environment:
#       POSTGRES_USER: modbot
#       POSTGRES_PASSWORD: modbot
#       POSTGRES_DB: modbot
#     volumes:
#       - pgdata:/var/lib/postgresql/data
#     restart: unless-stopped
#
#   redis:
#     image: redis:7-alpine
#     restart: unless-stopped
#
#   bot:
#     build: .
#     env_file: .env.prod
#     depends_on:
#       - postgres
#       - redis
#     environment:
#       MODE: webhook
#       PORT: 8080
#     expose:
#       - "8080"
#     restart: unless-stopped
#
#   caddy:
#     image: caddy:2-alpine
#     ports:
#       - "80:80"
#       - "443:443"
#     volumes:
#       - ./Caddyfile:/etc/caddy/Caddyfile
#       - caddydata:/data
#       - caddyconfig:/config
#     depends_on:
#       - bot
#     restart: unless-stopped
#
# volumes:
#   pgdata:
#   caddydata:
#   caddyconfig:

# 5) .env.prod (example)
# ----------------------
# BOT_TOKEN=123456:ABC...
# DATABASE_URL=postgresql+psycopg2://modbot:modbot@postgres:5432/modbot
# REDIS_URL=redis://redis:6379/0
# PUBLIC_URL=https://your-domain.tld
# WEBHOOK_PATH=/webhook/secret-path
# WEBHOOK_SECRET=super-secret-token
# DEFAULT_PLAN=basic

# 6) Production run commands
# --------------------------
# docker compose -f docker-compose.prod.yml up -d --build
# docker compose -f docker-compose.prod.yml logs -f bot
# curl -k https://your-domain.tld/healthz  # should return {"ok": true}

# - Pin library versions, audit dependencies on updates.

# 9) Zero-downtime deploy hint
# ----------------------------
# - Use rolling update: build new image, docker compose pull/up, Caddy keeps connections.
# - If you later separate services, place a message queue (e.g., Redis streams/RabbitMQ) for heavy tasks.
