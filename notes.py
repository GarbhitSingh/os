"""
PRESENCE BOT — NOTES MODULE (Phase 12-A)
Commands: /save /get /notes /delnote

Storage: C (DB + per-group memory cache)
  - DB is source of truth
  - Cache keyed by group_id → { name: content }
  - Cache populated on first /get or /notes access per group
  - Cache invalidated on every write or delete
  - Write-through: DB write → cache clear → next read repopulates

Note rules:
  - Name: lowercase, alphanumeric + underscore, max 32 chars
  - Content: max 4000 chars (Telegram message limit)
  - Admin-only write (save/delete)
  - All members can read (/get)
  - /save uses reply-to for content (common pattern) or inline text

Commands:
  /save [name] [content]    — saves note inline
  /save [name]              — saves note from replied message content
  /get [name]               — retrieves and posts note
  /notes                    — lists all saved note names
  /delnote [name]           — deletes a note (admin only)

The #[name] hashtag trigger is intentionally NOT implemented yet.
It requires message parsing changes. Comes in Phase 12-D (buttons).
"""

import re
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot import db
from bot.modules.admin_tools import is_admin_cached
from bot.utils.formatter import (
    fmt_note_content,
    fmt_note_saved,
    fmt_note_deleted,
    fmt_note_not_found,
    fmt_note_list,
    fmt_note_invalid_name,
    fmt_note_too_long,
    fmt_error_no_permission,
    fmt_system,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_NOTE_NAME_RE   = re.compile(r'^[a-z0-9_]{1,32}$')
_NOTE_MAX_LEN   = 4000
_NOTE_NAME_MAX  = 32

# ── Cache ─────────────────────────────────────────────────────────────────────
# { group_id: { note_name: content } }
# None entry means "cache miss — group has been loaded and notes exist"
# Empty dict means "group has no notes"

_note_cache: dict[int, dict[str, str]] = {}


def _cache_load(group_id: int) -> dict[str, str]:
    """Loads all notes for a group into cache if not already loaded."""
    if group_id in _note_cache:
        return _note_cache[group_id]

    cursor = db.get_db().cursor()
    cursor.execute(
        "SELECT name, content FROM notes WHERE group_id = ?",
        (group_id,)
    )
    _note_cache[group_id] = {row["name"]: row["content"] for row in cursor.fetchall()}
    return _note_cache[group_id]


def _cache_invalidate(group_id: int) -> None:
    """Clears cache for a group. Next read will repopulate from DB."""
    _note_cache.pop(group_id, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_name(name: str) -> bool:
    """Returns True if name is valid for a note key. Must already be lowercase."""
    return bool(_NOTE_NAME_RE.match(name))


async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"[NOTES] Send failed: {e}")


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /save [name] [content]
    /save [name] (with reply-to) — saves replied message as note content

    Admin only.
    Content sources (priority order):
      1. Inline text after name (/save rules No spam allowed)
      2. Replied message text
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    if not update.message:
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    if not context.args:
        await _send(update, fmt_system("Usage: /save [name] [content]\nOr reply to a message: /save [name]"))
        return

    name = context.args[0].lower().strip()

    if not _validate_name(name):
        await _send(update, fmt_note_invalid_name())
        return

    # Get content: inline args or replied message
    if len(context.args) > 1:
        content = " ".join(context.args[1:]).strip()
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        content = update.message.reply_to_message.text.strip()
    else:
        await _send(update, fmt_system("Provide content after the name, or reply to a message."))
        return

    if not content:
        await _send(update, fmt_system("Note content cannot be empty."))
        return

    if len(content) > _NOTE_MAX_LEN:
        await _send(update, fmt_note_too_long(_NOTE_MAX_LEN))
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    try:
        db.get_db().execute("""
        INSERT INTO notes (group_id, name, content, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id, name)
        DO UPDATE SET content = ?, updated_at = ?
        """, (group_id, name, content, update.effective_user.id, now, now, content, now))
        db.get_db().commit()

        _cache_invalidate(group_id)
        await _send(update, fmt_note_saved(name))

        logger.info(f"[NOTES] Saved note '{name}' in group {group_id} by {update.effective_user.id}")

    except Exception as e:
        logger.error(f"[NOTES] Save failed: {e}")
        db.log_error("notes", str(e), "L1", group_id)
        await _send(update, fmt_system("Failed to save note."))


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /get [name] — retrieves and posts a note.
    Available to all members.
    """
    if not update.effective_chat or not update.message:
        return

    group_id = update.effective_chat.id

    if not context.args:
        await _send(update, fmt_system("Usage: /get [name]\nUse /notes to see available notes."))
        return

    name  = context.args[0].lower().strip()
    notes = _cache_load(group_id)

    if name not in notes:
        # Double-check DB in case cache is stale
        cursor = db.get_db().cursor()
        cursor.execute(
            "SELECT content FROM notes WHERE group_id = ? AND name = ?",
            (group_id, name)
        )
        row = cursor.fetchone()
        if not row:
            await _send(update, fmt_note_not_found(name))
            return
        # Found in DB but not cache — refresh cache
        _cache_invalidate(group_id)
        notes = _cache_load(group_id)

    content = notes.get(name, "")
    if not content:
        await _send(update, fmt_note_not_found(name))
        return

    await _send(update, fmt_note_content(name, content))


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /notes — lists all saved note names for this group.
    Available to all members.
    """
    if not update.effective_chat or not update.message:
        return

    group_id = update.effective_chat.id
    notes    = _cache_load(group_id)
    group_name = update.effective_chat.title or ""

    await _send(update, fmt_note_list(list(notes.keys()), group_name))


async def cmd_delnote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /delnote [name] — deletes a note.
    Admin only.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    if not context.args:
        await _send(update, fmt_system("Usage: /delnote [name]"))
        return

    name = context.args[0].lower().strip()

    cursor = db.get_db().cursor()
    cursor.execute(
        "SELECT id FROM notes WHERE group_id = ? AND name = ?",
        (group_id, name)
    )
    if not cursor.fetchone():
        await _send(update, fmt_note_not_found(name))
        return

    db.get_db().execute(
        "DELETE FROM notes WHERE group_id = ? AND name = ?",
        (group_id, name)
    )
    db.get_db().commit()
    _cache_invalidate(group_id)

    await _send(update, fmt_note_deleted(name))
    logger.info(f"[NOTES] Deleted note '{name}' in group {group_id}")
