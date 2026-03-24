"""
PRESENCE BOT — ADMIN TOOLS MODULE (Phase 12-C)
Commands: /admins /id /userinfo /report

Admin cache:
  Fetched from Telegram API on first use per group.
  TTL: 5 minutes. In-memory only (not DB).
  Refreshed on demand or when stale.
  Cache key: group_id
  Value: list of admin ChatMember objects + fetch timestamp

Report system:
  /report — reply to a message to report it
  Stores in reports table
  Notifies all current admins via private message (best-effort)
  Admin-only /reports command lists open reports
"""

import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot import db
from bot.utils.formatter import (
    fmt_admins_list,
    fmt_user_id,
    fmt_user_info,
    fmt_report_sent,
    fmt_report_received,
    fmt_no_report_target,
    fmt_report_self,
    fmt_report_admin,
    fmt_system,
    fmt_error_no_reply,
)

logger = logging.getLogger(__name__)


# ── Admin cache ───────────────────────────────────────────────────────────────

_admin_cache: dict[int, dict] = {}
# Structure: { group_id: {"admins": [...], "fetched_at": datetime} }
_ADMIN_CACHE_TTL_SECONDS = 300   # 5 minutes


async def _get_admins(group_id: int, bot) -> list:
    """
    Returns admin list for the group.
    Uses cache if fresh (< 5 min). Otherwise fetches from Telegram API.
    Returns list of ChatMember objects. Returns [] on error.
    """
    now   = datetime.now(timezone.utc).replace(tzinfo=None)
    entry = _admin_cache.get(group_id)

    if entry:
        age = (now - entry["fetched_at"]).total_seconds()
        if age < _ADMIN_CACHE_TTL_SECONDS:
            return entry["admins"]

    # Fetch fresh
    try:
        admins = await bot.get_chat_administrators(group_id)
        _admin_cache[group_id] = {
            "admins":     list(admins),
            "fetched_at": now,
        }
        return list(admins)
    except TelegramError as e:
        logger.warning(f"[ADMIN TOOLS] get_chat_administrators failed for {group_id}: {e}")
        return []


async def is_admin_cached(group_id: int, user_id: int, bot) -> bool:
    """
    Returns True if user_id is an admin in the group.
    Uses cache — no API call if cache is fresh.
    """
    admins = await _get_admins(group_id, bot)
    return any(a.user.id == user_id for a in admins)


def invalidate_admin_cache(group_id: int) -> None:
    """Force cache refresh on next admin check for this group."""
    _admin_cache.pop(group_id, None)


async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"[ADMIN TOOLS] Send failed: {e}")


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /admins — lists current group admins.
    Available to all members.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id
    admins   = await _get_admins(group_id, context.bot)

    if not admins:
        await _send(update, fmt_system("Admin list unavailable."))
        return

    formatted = [
        {
            "user_id": a.user.id,
            "name":    f"@{a.user.username}" if a.user.username else a.user.first_name,
            "status":  a.status,
        }
        for a in admins
        if not a.user.is_bot
    ]

    await _send(update, fmt_admins_list(formatted))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /id — shows user ID and chat ID.
    With reply: shows replied user's ID.
    Without reply: shows sender's ID + chat ID.
    """
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    await _send(update, fmt_user_id(
        user_id=target.id,
        username=target.username,
        chat_id=chat_id
    ))


async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /userinfo — shows DB profile for a user.
    Reply to a message to see that user's info.
    Without reply: shows own info.
    Admin command — non-admins silently ignored.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    # Admin-only
    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        return

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    cursor = db.get_db().cursor()

    # Members table data
    cursor.execute(
        "SELECT * FROM members WHERE group_id = ? AND user_id = ?",
        (group_id, target.id)
    )
    member = cursor.fetchone()

    # Warnings
    cursor.execute(
        "SELECT count FROM warnings WHERE group_id = ? AND user_id = ?",
        (group_id, target.id)
    )
    warn_row = cursor.fetchone()
    warn_count = warn_row["count"] if warn_row else 0
    warn_limit = db.setting("warn_limit", 3)

    # Moderation log entries
    cursor.execute(
        "SELECT COUNT(*) FROM moderation_log WHERE group_id = ? AND target_user_id = ?",
        (group_id, target.id)
    )
    mod_count = cursor.fetchone()[0]

    await _send(update, fmt_user_info(
        user_id=target.id,
        username=target.username,
        message_count=member["message_count"] if member else 0,
        xp_contributed=member["xp_contributed"] if member else 0,
        warn_count=warn_count,
        warn_limit=warn_limit,
        joined_at=member["joined_at"] if member else None,
        last_active=member["last_active"] if member else None,
        mod_actions=mod_count
    ))


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /report [reason] — reply to a message to report it.
    Stores in reports table.
    Notifies admins via DM (best-effort).
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    if not update.message.reply_to_message:
        await _send(update, fmt_no_report_target())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    reporter = update.effective_user

    # Cannot report yourself
    if target.id == reporter.id:
        await _send(update, fmt_report_self())
        return

    # Cannot report admins
    if await is_admin_cached(group_id, target.id, context.bot):
        await _send(update, fmt_report_admin())
        return

    reason          = " ".join(context.args) if context.args else "No reason given"
    message_text    = update.message.reply_to_message.text or ""
    now             = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    # Store report
    db.get_db().execute("""
    INSERT INTO reports
    (group_id, reporter_id, target_user_id, message_text, reason, reported_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (group_id, reporter.id, target.id, message_text[:1000], reason[:500], now))
    db.get_db().commit()

    # Confirm to reporter
    await _send(update, fmt_report_sent())

    # Notify admins (best-effort — don't crash if DM fails)
    reporter_name = f"@{reporter.username}" if reporter.username else reporter.first_name
    target_name   = f"@{target.username}"   if target.username   else target.first_name
    report_msg    = fmt_report_received(reporter_name, target_name, reason, message_text[:200])

    admins = await _get_admins(group_id, context.bot)
    for admin in admins:
        if admin.user.is_bot:
            continue
        try:
            await context.bot.send_message(
                chat_id=admin.user.id,
                text=report_msg[0],
                parse_mode="Markdown"
            )
        except TelegramError:
            pass  # Admin may not have started the bot — silently skip

    logger.info(
        f"[ADMIN TOOLS] Report filed in group {group_id} "
        f"by {reporter.id} against {target.id}"
    )


async def cmd_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /reports — shows open (unresolved) reports for this group.
    Admin only.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        return

    cursor = db.get_db().cursor()
    cursor.execute("""
    SELECT id, reporter_id, target_user_id, reason, reported_at
    FROM reports
    WHERE group_id = ? AND resolved = 0
    ORDER BY reported_at DESC
    LIMIT 10
    """, (group_id,))
    rows = cursor.fetchall()

    if not rows:
        await _send(update, fmt_system("No open reports."))
        return

    lines = [f"`[REPORTS]` Open ({len(rows)}):\n"]
    for r in rows:
        date = r["reported_at"][:10] if r["reported_at"] else "?"
        lines.append(
            f"  `#{r['id']}` — user {r['target_user_id']}\n"
            f"  Reason: {r['reason'][:60]}\n"
            f"  Filed: {date}"
        )

    await _send(update, ("\n".join(lines), None))
