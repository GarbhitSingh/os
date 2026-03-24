"""
PRESENCE BOT — LOCKS MODULE (Phase 12-B)
Commands: /lock /unlock /locks
Passive: check_locks() called from message handler

Lock types:
  links     — URLs and t.me links
  media     — photos, videos, documents
  stickers  — sticker messages
  gifs      — animations/GIFs
  forwards  — forwarded messages
  bots      — messages from bots (other than this bot)
  all       — read-only mode (only admins can send)

Lock application model:
  Violation → delete message (silent by default)
  No automatic warn or mute unless combined separately
  Admins are never affected by locks
  Bot itself is never affected

Storage: B (locks table, DB only — no cache needed)
  Locks change rarely. DB read on passive_check is acceptable.
  A per-group in-memory cache is optional optimization for later.
"""

import re
import logging
from datetime import datetime, timezone

from telegram import Update, Message
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot import db
from bot.modules.admin_tools import is_admin_cached
from bot.utils.formatter import (
    fmt_lock_set,
    fmt_lock_unset,
    fmt_lock_unknown,
    fmt_lock_list,
    fmt_error_no_permission,
    fmt_system,
)

logger = logging.getLogger(__name__)

# ── Lock type definitions ─────────────────────────────────────────────────────

VALID_LOCK_TYPES = {"links", "media", "stickers", "gifs", "forwards", "bots", "all"}

# Regex for link detection
_URL_RE = re.compile(
    r'(https?://\S+|www\.\S+|t\.me/\S+)',
    re.IGNORECASE
)


# ── Lock cache ────────────────────────────────────────────────────────────────
# Per-group set of active lock types. Populated lazily. Invalidated on write.
# For Phase 12-B this is a simple dict. Can be expanded with TTL later.

_lock_cache: dict[int, set[str]] = {}


def _get_locks(group_id: int) -> set[str]:
    """Returns the set of active lock types for a group. Reads from cache or DB."""
    if group_id in _lock_cache:
        return _lock_cache[group_id]

    cursor = db.get_db().cursor()
    cursor.execute(
        "SELECT lock_type FROM locks WHERE group_id = ? AND enabled = 1",
        (group_id,)
    )
    active = {row["lock_type"] for row in cursor.fetchall()}
    _lock_cache[group_id] = active
    return active


def _invalidate_lock_cache(group_id: int) -> None:
    _lock_cache.pop(group_id, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    if not text:
        return  # Silent — no message (lock deletion)
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"[LOCKS] Send failed: {e}")


def _is_link(text: str) -> bool:
    return bool(_URL_RE.search(text))


def _is_media(message: Message) -> bool:
    return bool(
        message.photo or message.video or message.document or
        message.audio or message.voice or message.video_note
    )


def _is_sticker(message: Message) -> bool:
    return bool(message.sticker)


def _is_gif(message: Message) -> bool:
    # GIFs come as animation or document with mime type
    if message.animation:
        return True
    if message.document and message.document.mime_type == "image/gif":
        return True
    return False


def _is_forward(message: Message) -> bool:
    return bool(message.forward_date or message.forward_from or message.forward_from_chat)


# ── Passive lock check ────────────────────────────────────────────────────────

async def check_locks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from message handler AFTER moderation passive_check passes.
    Returns True if message is allowed, False if it was deleted by a lock.

    Admins are always exempt.
    Bot messages are always exempt.
    """
    if not update.message or not update.effective_user:
        return True
    if update.effective_user.is_bot:
        return True

    group_id = update.effective_chat.id
    user_id  = update.effective_user.id

    # Admins exempt from all locks
    if await is_admin_cached(group_id, user_id, context.bot):
        return True

    active_locks = _get_locks(group_id)
    if not active_locks:
        return True

    message = update.message
    text    = message.text or message.caption or ""

    violated = False

    # Check each active lock
    if "all" in active_locks:
        violated = True

    elif "links" in active_locks and _is_link(text):
        violated = True

    elif "media" in active_locks and _is_media(message):
        violated = True

    elif "stickers" in active_locks and _is_sticker(message):
        violated = True

    elif "gifs" in active_locks and _is_gif(message):
        violated = True

    elif "forwards" in active_locks and _is_forward(message):
        violated = True

    elif "bots" in active_locks and message.via_bot:
        violated = True

    if violated:
        try:
            await message.delete()
        except TelegramError as e:
            logger.debug(f"[LOCKS] Delete failed for lock violation: {e}")
        return False

    return True


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /lock [type] — enables a lock for this group.
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
            "Usage: /lock [type]\n"
            "Types: links  media  stickers  gifs  forwards  bots  all"
        ))
        return

    lock_type = context.args[0].lower().strip()

    if lock_type not in VALID_LOCK_TYPES:
        await _send(update, fmt_lock_unknown(lock_type))
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    try:
        db.get_db().execute("""
        INSERT INTO locks (group_id, lock_type, enabled, set_by, set_at)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(group_id, lock_type)
        DO UPDATE SET enabled = 1, set_by = ?, set_at = ?
        """, (group_id, lock_type, update.effective_user.id, now,
              update.effective_user.id, now))
        db.get_db().commit()

        _invalidate_lock_cache(group_id)
        await _send(update, fmt_lock_set(lock_type))

        logger.info(f"[LOCKS] Lock set: {lock_type} in group {group_id}")

    except Exception as e:
        logger.error(f"[LOCKS] Lock set failed: {e}")
        db.log_error("locks", str(e), "L1", group_id)
        await _send(update, fmt_system("Failed to set lock."))


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /unlock [type] — disables a lock.
    Admin only.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    if not context.args:
        await _send(update, fmt_system("Usage: /unlock [type]"))
        return

    lock_type = context.args[0].lower().strip()

    if lock_type not in VALID_LOCK_TYPES:
        await _send(update, fmt_lock_unknown(lock_type))
        return

    db.get_db().execute(
        "UPDATE locks SET enabled = 0 WHERE group_id = ? AND lock_type = ?",
        (group_id, lock_type)
    )
    db.get_db().commit()

    _invalidate_lock_cache(group_id)
    await _send(update, fmt_lock_unset(lock_type))
    logger.info(f"[LOCKS] Lock removed: {lock_type} in group {group_id}")


async def cmd_locks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /locks — lists active locks for this group.
    Available to all members.
    """
    if not update.effective_chat or not update.message:
        return

    group_id     = update.effective_chat.id
    active_locks = list(_get_locks(group_id))
    await _send(update, fmt_lock_list(active_locks))
