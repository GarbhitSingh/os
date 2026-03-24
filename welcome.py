"""
PRESENCE BOT — WELCOME MODULE (Phase 12-D)
Handles welcome/goodbye messages with variables and inline buttons.

Storage: C (settings table + settings_cache inherited from db.py)
  Keys:
    welcome_{group_id}         → welcome text template
    welcome_buttons_{group_id} → JSON button layout
    goodbye_{group_id}         → goodbye text template

Variables supported in message text:
  {name}    → member display name
  {mention} → @username or first name
  {group}   → group title
  {count}   → current member count

Button format (stored as JSON string):
  [[{"text": "Rules", "url": "..."}, {"text": "Info", "callback_data": "..."}]]

Commands:
  /setwelcome <text>            — sets welcome message
  /setwelcome                   — shows current welcome template
  /clearwelcome                 — removes welcome message
  /setgoodbye <text>            — sets goodbye message
  /clearwelcome                 — removes goodbye message
  /setwelcomebuttons <json>     — adds inline buttons to welcome
  /clearwelcomebuttons          — removes welcome buttons

Welcome fires on: NEW_CHAT_MEMBERS update
Goodbye fires on: LEFT_CHAT_MEMBER update

Note: This module replaces the skeleton in moderation.py.
moderation.handle_new_member() now delegates to this module.
"""

import json
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot import db
from bot.modules.admin_tools import is_admin_cached
from bot.utils.formatter import (
    fmt_welcome_text,
    fmt_build_keyboard,
    fmt_welcome_set,
    fmt_welcome_cleared,
    fmt_goodbye_set,
    fmt_goodbye_cleared,
    fmt_welcome_buttons_set,
    fmt_welcome_buttons_cleared,
    fmt_welcome_buttons_invalid,
    fmt_welcome_not_set,
    fmt_welcome_show_current,
    fmt_error_no_permission,
    fmt_system,
)

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    if not text:
        return
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"[WELCOME] Send failed: {e}")


async def _get_member_count(chat_id: int, bot) -> int | None:
    """Returns group member count. Returns None on failure."""
    try:
        return await bot.get_chat_member_count(chat_id)
    except TelegramError:
        return None


# ── Event handlers ────────────────────────────────────────────────────────────

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires on NEW_CHAT_MEMBERS.
    Posts welcome message with variable substitution and optional buttons.
    """
    if not update.message or not update.message.new_chat_members:
        return

    group_id   = update.effective_chat.id
    group_name = update.effective_chat.title or ""

    welcome_text    = db.setting(f"welcome_{group_id}", "")
    buttons_json    = db.setting(f"welcome_buttons_{group_id}", "")

    if not welcome_text:
        return  # No welcome configured — silent

    member_count = await _get_member_count(group_id, context.bot)

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        name = f"@{member.username}" if member.username else member.first_name

        resolved = fmt_welcome_text(
            raw_text=welcome_text,
            member_name=name,
            group_name=group_name,
            member_count=member_count
        )

        keyboard = fmt_build_keyboard(buttons_json)

        try:
            await update.message.reply_text(
                resolved,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except TelegramError as e:
            logger.warning(f"[WELCOME] Welcome post failed: {e}")


async def handle_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires on LEFT_CHAT_MEMBER.
    Posts goodbye message if configured.
    """
    if not update.message or not update.message.left_chat_member:
        return

    member = update.message.left_chat_member
    if member.is_bot:
        return

    group_id   = update.effective_chat.id
    group_name = update.effective_chat.title or ""

    goodbye_text = db.setting(f"goodbye_{group_id}", "")
    if not goodbye_text:
        return

    name     = f"@{member.username}" if member.username else member.first_name
    resolved = fmt_welcome_text(
        raw_text=goodbye_text,
        member_name=name,
        group_name=group_name,
        member_count=None
    )

    try:
        await update.message.reply_text(resolved, parse_mode="Markdown")
    except TelegramError as e:
        logger.warning(f"[WELCOME] Goodbye post failed: {e}")


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setwelcome <text>     — sets the welcome message
    /setwelcome            — shows current welcome template
    Variables: {name}, {mention}, {group}, {count}
    Admin only.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    if not context.args:
        # Show current
        current = db.setting(f"welcome_{group_id}", "")
        if not current:
            await _send(update, fmt_welcome_not_set())
        else:
            await _send(update, fmt_welcome_show_current(current))
        return

    text = " ".join(context.args)
    db.set_setting(f"welcome_{group_id}", text)
    await _send(update, fmt_welcome_set())
    logger.info(f"[WELCOME] Welcome set in group {group_id}")


async def cmd_clearwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clearwelcome — removes welcome message and buttons."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    db.set_setting(f"welcome_{group_id}", "")
    db.set_setting(f"welcome_buttons_{group_id}", "")
    await _send(update, fmt_welcome_cleared())


async def cmd_setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setgoodbye <text> — sets the goodbye message. Admin only."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    if not context.args:
        await _send(update, fmt_system("Usage: /setgoodbye <text>\nVariables: {name}, {group}"))
        return

    text = " ".join(context.args)
    db.set_setting(f"goodbye_{group_id}", text)
    await _send(update, fmt_goodbye_set())


async def cmd_cleargoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cleargoodbye — removes goodbye message."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    db.set_setting(f"goodbye_{group_id}", "")
    await _send(update, fmt_goodbye_cleared())


async def cmd_setwelcomebuttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setwelcomebuttons <json>
    JSON format: [[{"text": "Label", "url": "https://..."}]]
    Admin only.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    if not context.args:
        await _send(update, fmt_system(
            "Usage: /setwelcomebuttons [[{\"text\": \"Rules\", \"url\": \"https://...\"}]]"
        ))
        return

    raw = " ".join(context.args)

    # Validate JSON
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("Must be a list of rows")
        for row in parsed:
            if not isinstance(row, list):
                raise ValueError("Each row must be a list")
            for btn in row:
                if "text" not in btn:
                    raise ValueError("Each button must have 'text'")
                if "url" not in btn and "callback_data" not in btn:
                    raise ValueError("Each button must have 'url' or 'callback_data'")
    except (json.JSONDecodeError, ValueError, TypeError):
        await _send(update, fmt_welcome_buttons_invalid())
        return

    db.set_setting(f"welcome_buttons_{group_id}", raw)
    await _send(update, fmt_welcome_buttons_set())


async def cmd_clearwelcomebuttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clearwelcomebuttons — removes inline buttons from welcome message."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    db.set_setting(f"welcome_buttons_{group_id}", "")
    await _send(update, fmt_welcome_buttons_cleared())
