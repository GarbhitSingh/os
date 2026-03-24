"""
PRESENCE BOT — MODERATION MODULE (FULL + PHASE 11 VOICE)
All messages routed through formatter.py.
No inline strings. No direct text construction.

Storage: C (hybrid — tables for state, settings for config)
Voice: system / cold / neutral (Phase 11 rules)
"""

import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from telegram import Update, ChatPermissions
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot import db
from bot.utils.formatter import (
    fmt_warn_confirm, fmt_warn_auto_mute, fmt_unwarn_confirm, fmt_warnings_count,
    fmt_mute_confirm, fmt_unmute_confirm,
    fmt_ban_confirm, fmt_kick_confirm,
    fmt_filter_added, fmt_filter_removed, fmt_filter_list,
    fmt_rules, fmt_rules_saved, fmt_welcome_saved,
    fmt_error_no_reply, fmt_error_no_permission,
    fmt_error_bot_not_admin, fmt_error_cannot_target_bot,
    fmt_error_generic, fmt_system,
)

logger = logging.getLogger(__name__)


async def _log_to_channel(
    group_id: int,
    action: str,
    target,          # Telegram User object
    issuer,          # Telegram User object or None (automated)
    reason: str = "",
    extra: str = "",
    bot=None
) -> None:
    """
    Forwards a moderation action to the group's log channel.
    Best-effort — never raises. Called after every action.
    """
    if not bot:
        return
    try:
        from bot.modules.log_channel import log_action
        target_name = f"@{target.username}" if target.username else target.first_name
        issuer_name = (f"@{issuer.username}" if issuer and issuer.username
                       else (issuer.first_name if issuer else "System"))
        issuer_id   = issuer.id if issuer else 0
        await log_action(group_id, action, target_name, target.id,
                         issuer_name, issuer_id, reason, extra, bot)
    except Exception as e:
        logger.debug(f"[MOD] Log channel forward non-critical: {e}")


# ── Permission helpers ────────────────────────────────────────────────────────

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the message sender is a group admin or creator."""
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id
        )
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


def _log_action(group_id: int, action: str, target_id: int, issued_by: int,
                reason: str = "", expires_at: str | None = None) -> None:
    """Writes a moderation action to moderation_log. Never raises."""
    if not db.setting("log_mod_actions", 1):
        return
    try:
        db.get_db().execute("""
        INSERT INTO moderation_log
        (group_id, action, target_user_id, issued_by, reason, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (group_id, action, target_id, issued_by, reason[:500], expires_at))
        db.get_db().commit()
    except Exception as e:
        logger.warning(f"[MOD] Failed to log action {action}: {e}")


async def _send(update, text_kb_tuple: tuple) -> None:
    """Helper: sends formatted message. Silently ignores Telegram errors."""
    text, kb = text_kb_tuple
    try:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=kb
        )
    except TelegramError as e:
        logger.warning(f"[MOD] Send failed: {e}")


# ── Warn system ───────────────────────────────────────────────────────────────

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/warn — reply to a message to warn the user. Admin only."""
    if not await is_admin(update, context):
        return

    if not update.message.reply_to_message:
        await _send(update, fmt_error_no_reply())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    issuer   = update.effective_user.id
    reason   = " ".join(context.args) if context.args else "No reason given"

    if target.is_bot:
        await _send(update, fmt_error_cannot_target_bot())
        return

    conn = db.get_db()
    now  = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute("""
    INSERT INTO warnings (group_id, user_id, count, last_warned_at)
    VALUES (?, ?, 1, ?)
    ON CONFLICT(group_id, user_id)
    DO UPDATE SET count = count + 1, last_warned_at = ?
    """, (group_id, target.id, now, now))
    conn.commit()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT count FROM warnings WHERE group_id = ? AND user_id = ?",
        (group_id, target.id)
    )
    row        = cursor.fetchone()
    warn_count = row["count"] if row else 1
    warn_limit = db.setting("warn_limit", 3)
    name       = target.username or target.first_name

    _log_action(group_id, "warn", target.id, issuer, reason)

    if warn_count >= warn_limit and db.setting("auto_mute_on_warn_limit", 1):
        mute_ok = await _do_mute(group_id, target.id, 60, context.bot)
        _log_action(group_id, "mute", target.id, 0,
                    f"Auto: warn limit reached ({warn_count}/{warn_limit})",
                    (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=60)).isoformat())
        await _log_to_channel(group_id, "warn+auto_mute", target,
                              update.effective_user, reason,
                              f"Warn {warn_count}/{warn_limit} — auto-mute 60m",
                              context.bot)
        await _send(update, fmt_warn_auto_mute(name, warn_count, warn_limit, reason))
    else:
        await _log_to_channel(group_id, "warn", target, update.effective_user,
                              reason, f"{warn_count}/{warn_limit}", context.bot)
        await _send(update, fmt_warn_confirm(name, warn_count, warn_limit, reason))


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unwarn — removes one warning. Admin only."""
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await _send(update, fmt_error_no_reply())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id

    db.get_db().execute("""
    UPDATE warnings SET count = MAX(0, count - 1)
    WHERE group_id = ? AND user_id = ?
    """, (group_id, target.id))
    db.get_db().commit()

    _log_action(group_id, "unwarn", target.id, update.effective_user.id)
    await _send(update, fmt_unwarn_confirm(target.username or target.first_name))


async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/warnings — shows warning count for replied user."""
    if not update.message.reply_to_message:
        await _send(update, fmt_error_no_reply())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    cursor   = db.get_db().cursor()
    cursor.execute(
        "SELECT count FROM warnings WHERE group_id = ? AND user_id = ?",
        (group_id, target.id)
    )
    row        = cursor.fetchone()
    count      = row["count"] if row else 0
    warn_limit = db.setting("warn_limit", 3)
    await _send(update, fmt_warnings_count(target.username or target.first_name, count, warn_limit))


# ── Mute ─────────────────────────────────────────────────────────────────────

async def _do_mute(chat_id: int, user_id: int, minutes: int, bot) -> bool:
    until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        return True
    except TelegramError as e:
        logger.warning(f"[MOD] Mute failed {user_id}@{chat_id}: {e}")
        return False


async def _do_unmute(chat_id: int, user_id: int, bot) -> bool:
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        return True
    except TelegramError as e:
        logger.warning(f"[MOD] Unmute failed {user_id}@{chat_id}: {e}")
        return False


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mute [minutes] [reason] — reply to a message. Admin only."""
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await _send(update, fmt_error_no_reply())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id

    if target.is_bot:
        await _send(update, fmt_error_cannot_target_bot())
        return

    minutes = db.setting("mute_duration", 60)
    reason  = "No reason given"

    if context.args:
        try:
            minutes = int(context.args[0])
            reason  = " ".join(context.args[1:]) or reason
        except ValueError:
            reason = " ".join(context.args)

    minutes = max(1, min(minutes, 10080))
    success = await _do_mute(group_id, target.id, minutes, context.bot)
    expires = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)).isoformat()
    name    = target.username or target.first_name

    _log_action(group_id, "mute", target.id, update.effective_user.id, reason, expires)

    if success:
        await _log_to_channel(group_id, "mute", target, update.effective_user,
                              reason, f"Duration: {minutes}m", context.bot)
        await _send(update, fmt_mute_confirm(name, minutes, reason))
    else:
        await _send(update, fmt_error_bot_not_admin())


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unmute — reply to muted user. Admin only."""
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await _send(update, fmt_error_no_reply())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    success  = await _do_unmute(group_id, target.id, context.bot)
    name     = target.username or target.first_name

    _log_action(group_id, "unmute", target.id, update.effective_user.id, "Manual unmute")

    if success:
        await _send(update, fmt_unmute_confirm(name))
    else:
        await _send(update, fmt_error_bot_not_admin())


# ── Ban / Kick ────────────────────────────────────────────────────────────────

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ban [reason] — reply to a message. Admin only."""
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await _send(update, fmt_error_no_reply())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    reason   = " ".join(context.args) if context.args else "No reason given"

    try:
        await context.bot.ban_chat_member(group_id, target.id)
        _log_action(group_id, "ban", target.id, update.effective_user.id, reason)
        await _log_to_channel(group_id, "ban", target, update.effective_user, reason, "", context.bot)
        await _send(update, fmt_ban_confirm(target.username or target.first_name, reason))
    except TelegramError as e:
        logger.warning(f"[MOD] Ban failed: {e}")
        await _send(update, fmt_error_bot_not_admin())


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kick [reason] — ban then immediately unban. Admin only."""
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await _send(update, fmt_error_no_reply())
        return

    target   = update.message.reply_to_message.from_user
    group_id = update.effective_chat.id
    reason   = " ".join(context.args) if context.args else "No reason given"

    try:
        await context.bot.ban_chat_member(group_id, target.id)
        await context.bot.unban_chat_member(group_id, target.id)
        _log_action(group_id, "kick", target.id, update.effective_user.id, reason)
        await _log_to_channel(group_id, "kick", target, update.effective_user, reason, "", context.bot)
        await _send(update, fmt_kick_confirm(target.username or target.first_name, reason))
    except TelegramError as e:
        logger.warning(f"[MOD] Kick failed: {e}")
        await _send(update, fmt_error_bot_not_admin())


# ── Mute expiry scheduler ─────────────────────────────────────────────────────

async def expire_mutes(context) -> None:
    """Called every 5 minutes by scheduler. Auto-unmutes expired mutes."""
    bot = context.bot if hasattr(context, "bot") else context
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    cursor = db.get_db().cursor()
    cursor.execute("""
    SELECT DISTINCT m.group_id, m.target_user_id
    FROM moderation_log m
    WHERE m.action = 'mute'
      AND m.expires_at IS NOT NULL
      AND m.expires_at <= ?
      AND NOT EXISTS (
          SELECT 1 FROM moderation_log u
          WHERE u.group_id       = m.group_id
            AND u.target_user_id = m.target_user_id
            AND u.action         = 'unmute'
            AND u.timestamp      > m.timestamp
      )
    """, (now,))

    for row in cursor.fetchall():
        ok = await _do_unmute(row["group_id"], row["target_user_id"], bot)
        if ok:
            _log_action(row["group_id"], "unmute", row["target_user_id"], 0, "Auto-expired")


# ── Passive checks ────────────────────────────────────────────────────────────

_flood_tracker:  dict[tuple, list[datetime]] = defaultdict(list)
_repeat_tracker: dict[tuple, list[str]]      = defaultdict(list)
_tracker_last_cleaned: datetime | None = None
_TRACKER_CLEAN_INTERVAL_HOURS = 6
_TRACKER_MAX_ENTRIES = 10000


def _maybe_clean_trackers() -> None:
    global _tracker_last_cleaned
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if (_tracker_last_cleaned is not None and
            (now - _tracker_last_cleaned).total_seconds() < _TRACKER_CLEAN_INTERVAL_HOURS * 3600):
        return

    _tracker_last_cleaned = now
    cutoff = now - timedelta(minutes=30)

    stale = [k for k, v in _flood_tracker.items()
             if not v or all(t < cutoff for t in v)]
    for k in stale:
        del _flood_tracker[k]

    if len(_repeat_tracker) > _TRACKER_MAX_ENTRIES:
        keys = list(_repeat_tracker.keys())
        for k in keys[:len(keys)//2]:
            del _repeat_tracker[k]

    logger.debug(f"[MOD] Tracker cleanup: flood={len(_flood_tracker)} repeat={len(_repeat_tracker)}")


async def passive_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Runs before XP is awarded.
    Returns True if message passes.
    Returns False if spam action was taken.
    """
    if not update.message or not update.effective_user:
        return True

    _maybe_clean_trackers()

    group_id = update.effective_chat.id
    user_id  = update.effective_user.id
    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    text     = update.message.text or ""
    key      = (group_id, user_id)

    # ── Anti-flood ────────────────────────────────────────────────────────────
    flood_limit  = db.setting("anti_flood_msgs",   5)
    flood_window = db.setting("anti_flood_window", 10)
    cutoff       = now - timedelta(seconds=flood_window)

    _flood_tracker[key] = [t for t in _flood_tracker[key] if t > cutoff]
    _flood_tracker[key].append(now)

    if len(_flood_tracker[key]) > flood_limit:
        try:
            await update.message.delete()
        except TelegramError:
            pass
        muted = await _do_mute(group_id, user_id, 10, context.bot)
        if muted:
            _log_action(group_id, "mute", user_id, 0,
                        f"Auto-flood ({len(_flood_tracker[key])} msgs/{flood_window}s)",
                        (now + timedelta(minutes=10)).isoformat())
        _flood_tracker[key].clear()
        return False

    # ── Anti-repeat ───────────────────────────────────────────────────────────
    if text:
        repeat_limit = db.setting("anti_repeat_count", 3)
        _repeat_tracker[key].append(text.lower().strip())
        _repeat_tracker[key] = _repeat_tracker[key][-(repeat_limit + 1):]
        recent = _repeat_tracker[key]

        if len(recent) >= repeat_limit and len(set(recent[-repeat_limit:])) == 1:
            try:
                await update.message.delete()
            except TelegramError:
                pass
            ts = now.isoformat()
            db.get_db().execute("""
            INSERT INTO warnings (group_id, user_id, count, last_warned_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(group_id, user_id)
            DO UPDATE SET count = count + 1, last_warned_at = ?
            """, (group_id, user_id, ts, ts))
            db.get_db().commit()
            _log_action(group_id, "warn", user_id, 0, f"Auto-repeat: same message {repeat_limit}x")
            _repeat_tracker[key].clear()
            return False

    # ── Keyword filter ────────────────────────────────────────────────────────
    if text and await _check_filters(group_id, user_id, text, update):
        return False

    return True


async def _check_filters(group_id: int, user_id: int, text: str, update: Update) -> bool:
    """Checks message against filters table. Returns True if filtered."""
    cursor = db.get_db().cursor()
    cursor.execute("SELECT word FROM filters WHERE group_id = ?", (group_id,))
    words = [row["word"] for row in cursor.fetchall()]
    if not words:
        return False

    text_lower = text.lower()
    for word in words:
        if word.lower() in text_lower:
            try:
                await update.message.delete()
            except TelegramError:
                pass
            _log_action(group_id, "filter_delete", user_id, 0, f"Matched: {word}")
            return True

    return False


# ── Filter management ─────────────────────────────────────────────────────────

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/filter <word>"""
    if not await is_admin(update, context):
        return
    if not context.args:
        await _send(update, fmt_system("Usage: /filter <word>"))
        return

    group_id = update.effective_chat.id
    word     = context.args[0].lower().strip()[:100]

    try:
        db.get_db().execute(
            "INSERT OR IGNORE INTO filters (group_id, word, added_by) VALUES (?, ?, ?)",
            (group_id, word, update.effective_user.id)
        )
        db.get_db().commit()
        await _send(update, fmt_filter_added(word))
    except Exception as e:
        logger.warning(f"[MOD] filter add failed: {e}")
        await _send(update, fmt_error_generic())


async def cmd_unfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unfilter <word>"""
    if not await is_admin(update, context):
        return
    if not context.args:
        await _send(update, fmt_system("Usage: /unfilter <word>"))
        return

    group_id = update.effective_chat.id
    word     = context.args[0].lower().strip()

    db.get_db().execute("DELETE FROM filters WHERE group_id = ? AND word = ?", (group_id, word))
    db.get_db().commit()
    await _send(update, fmt_filter_removed(word))


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/filters — list all active filters."""
    group_id = update.effective_chat.id
    cursor   = db.get_db().cursor()
    cursor.execute("SELECT word FROM filters WHERE group_id = ? ORDER BY word", (group_id,))
    words = [row["word"] for row in cursor.fetchall()]
    await _send(update, fmt_filter_list(words))


# ── Welcome / Rules ───────────────────────────────────────────────────────────

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Posts welcome message on new member join."""
    if not update.message or not update.message.new_chat_members:
        return

    group_id = update.effective_chat.id
    welcome  = db.setting(f"welcome_{group_id}", "")

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        if welcome:
            name = f"@{member.username}" if member.username else member.first_name
            text = (welcome
                    .replace("{name}", name)
                    .replace("{group}", update.effective_chat.title or ""))
            try:
                await update.message.reply_text(text)
            except TelegramError as e:
                logger.warning(f"[MOD] Welcome send failed: {e}")


async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setwelcome <message>"""
    if not await is_admin(update, context):
        return
    if not context.args:
        await _send(update, fmt_system("Usage: /setwelcome <message>\nVariables: {name}, {group}"))
        return

    group_id = update.effective_chat.id
    db.set_setting(f"welcome_{group_id}", " ".join(context.args))
    await _send(update, fmt_welcome_saved())


async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setrules <rules text>"""
    if not await is_admin(update, context):
        return
    if not context.args:
        await _send(update, fmt_system("Usage: /setrules <rules text>"))
        return

    group_id = update.effective_chat.id
    db.set_setting(f"rules_{group_id}", " ".join(context.args))
    await _send(update, fmt_rules_saved())


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rules"""
    group_id = update.effective_chat.id
    rules    = db.setting(f"rules_{group_id}", "")

    if not rules:
        await _send(update, fmt_system("No rules on record. Use /setrules to add them."))
    else:
        await _send(update, fmt_rules(rules))
