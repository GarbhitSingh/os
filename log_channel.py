"""
PRESENCE BOT — LOG CHANNEL MODULE (Phase 12-E)
Routes moderation events to a configured Telegram channel.

Storage: A (settings key only)
  Key: log_channel_{group_id} → integer chat_id of the log channel

Event routing:
  warn       → fmt_log_event
  unwarn     → fmt_log_event
  mute       → fmt_log_event (includes duration)
  unmute     → fmt_log_event
  ban        → fmt_log_event
  kick       → fmt_log_event
  join       → fmt_log_join
  leave      → fmt_log_leave
  report     → fmt_log_report
  filter_del → fmt_log_event (silent — no issued_by admin)

Design rules:
  - All sends are best-effort (never crash calling module)
  - If channel is unreachable, log error, continue
  - If channel not set, skip silently (most groups won't configure this)
  - Bot must be member + admin of target channel
  - Module is called from other modules, not from message handler directly

Commands:
  /setlogchannel [chat_id]  — sets the log channel (admin only)
  /clearlogchannel          — removes log channel routing
  /logchannel               — shows current channel (admin only)
"""

import logging

from telegram.error import TelegramError

from bot import db
from bot.utils.formatter import (
    fmt_log_event,
    fmt_log_join,
    fmt_log_leave,
    fmt_log_report,
    fmt_log_channel_set,
    fmt_log_channel_cleared,
    fmt_log_channel_not_set,
    fmt_log_channel_show,
    fmt_error_no_permission,
    fmt_system,
)

logger = logging.getLogger(__name__)


# ── Core router ───────────────────────────────────────────────────────────────

def _get_log_channel(group_id: int) -> int | None:
    """Returns the configured log channel ID for a group. None if not set."""
    val = db.setting(f"log_channel_{group_id}", "")
    if not val:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


async def _send_to_channel(group_id: int, text: str, bot) -> bool:
    """
    Sends a formatted message to the group's log channel.
    Returns True on success. Logs L1 error on failure. Never raises.
    """
    channel_id = _get_log_channel(group_id)
    if not channel_id:
        return False  # Not configured — silent skip

    try:
        await bot.send_message(
            chat_id=channel_id,
            text=text,
            parse_mode="Markdown"
        )
        return True
    except TelegramError as e:
        # Common causes: bot not in channel, channel deleted, privacy setting
        logger.warning(f"[LOG CHANNEL] Send failed for group {group_id} → {channel_id}: {e}")
        db.log_error("log_channel", f"Send failed: {e}", "L1", group_id)
        return False


# ── Public log functions (called by other modules) ────────────────────────────

async def log_action(
    group_id: int,
    action: str,
    target_name: str,
    target_id: int,
    issued_by_name: str,
    issued_by_id: int,
    reason: str = "",
    extra: str = "",
    bot=None
) -> None:
    """
    Routes a moderation action to the log channel.
    Called by moderation.py after every action.
    """
    if not bot:
        return

    text, _ = fmt_log_event(
        action=action,
        target_name=target_name,
        target_id=target_id,
        issued_by_name=issued_by_name,
        issued_by_id=issued_by_id,
        reason=reason,
        extra=extra
    )
    await _send_to_channel(group_id, text, bot)


async def log_join(
    group_id: int,
    member_name: str,
    member_id: int,
    bot=None
) -> None:
    """Routes a join event to the log channel."""
    if not bot:
        return
    text, _ = fmt_log_join(member_name, member_id)
    await _send_to_channel(group_id, text, bot)


async def log_leave(
    group_id: int,
    member_name: str,
    member_id: int,
    bot=None
) -> None:
    """Routes a leave event to the log channel."""
    if not bot:
        return
    text, _ = fmt_log_leave(member_name, member_id)
    await _send_to_channel(group_id, text, bot)


async def log_report(
    group_id: int,
    reporter_name: str,
    reporter_id: int,
    target_name: str,
    target_id: int,
    reason: str,
    bot=None
) -> None:
    """Routes a report to the log channel."""
    if not bot:
        return
    text, _ = fmt_log_report(reporter_name, reporter_id, target_name, target_id, reason)
    await _send_to_channel(group_id, text, bot)


# ── Commands ──────────────────────────────────────────────────────────────────

from telegram import Update
from telegram.ext import ContextTypes
from bot.modules.admin_tools import is_admin_cached


async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"[LOG CHANNEL] Send failed: {e}")


async def cmd_setlogchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setlogchannel [channel_id]
    Sets the Telegram channel where mod logs are sent.
    The bot must be admin in that channel.
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
            "Usage: /setlogchannel [channel_id]\n"
            "The bot must be admin in the target channel.\n"
            "Channel IDs are negative numbers (e.g. -1001234567890)."
        ))
        return

    raw = context.args[0].strip()
    try:
        channel_id = int(raw)
    except ValueError:
        await _send(update, fmt_system("Channel ID must be a number."))
        return

    # Test that bot can send to this channel before saving
    try:
        await context.bot.send_message(
            chat_id=channel_id,
            text="`[SYSTEM]` Log channel connected.",
            parse_mode="Markdown"
        )
    except TelegramError as e:
        await _send(update, fmt_system(
            f"Could not send to channel `{channel_id}`.\n"
            f"Make sure the bot is admin in that channel.\n"
            f"Error: {str(e)[:100]}"
        ))
        return

    db.set_setting(f"log_channel_{group_id}", str(channel_id))
    await _send(update, fmt_log_channel_set(channel_id))
    logger.info(f"[LOG CHANNEL] Set for group {group_id} → {channel_id}")


async def cmd_clearlogchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clearlogchannel — removes log channel routing."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    db.set_setting(f"log_channel_{group_id}", "")
    await _send(update, fmt_log_channel_cleared())


async def cmd_logchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/logchannel — shows current log channel. Admin only."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    channel_id = _get_log_channel(group_id)
    if not channel_id:
        await _send(update, fmt_log_channel_not_set())
    else:
        await _send(update, fmt_log_channel_show(channel_id))
