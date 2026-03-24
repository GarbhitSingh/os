"""
PRESENCE BOT — BACKUP MODULE (Phase 12-F)
Commands: /export /import /backup

Storage model: C (JSON + DB)
  /export → produces per-group JSON file, sends to chat
  /import → reads JSON file from replied message, restores group config
  DB backup → manual / cron / sysadmin task (documented, not a bot command)

Export scope (per-group JSON):
  - groups row (level, xp, anomaly_score, group_name)
  - warnings (active user warn counts)
  - filters (keyword list)
  - locks (active lock types)
  - notes (all note names + content)
  - group_unlocks (unlock history — reference IDs and types)
  - settings (group-keyed: welcome, rules, log_channel, etc.)
  - export metadata (timestamp, version, group_id)

Excluded from export (privacy + size):
  - members table (user activity data)
  - moderation_log (historical actions)
  - events_log (engine history)
  - global_state (system-wide, not per-group)

Import rules:
  - Idempotent: safe to import same file twice
  - Does NOT overwrite existing group_unlocks (preserve progression)
  - DOES overwrite: filters, locks, notes, warnings, settings
  - Group level and XP NOT overwritten on import (would be cheating)
  - Validates JSON schema before writing anything

Commands:
  /export   — generates and sends group config JSON (admin only)
  /import   — reply to a JSON file to restore config (admin only)
  /backup   — shows backup instructions
"""

import json
import logging
import io
from datetime import datetime, timezone

from telegram import Update, Document
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot import db
from bot.modules.admin_tools import is_admin_cached
from bot.utils.formatter import (
    fmt_export_ready,
    fmt_export_failed,
    fmt_import_success,
    fmt_import_failed,
    fmt_import_invalid_file,
    fmt_backup_instructions,
    fmt_error_no_permission,
    fmt_system,
)

logger = logging.getLogger(__name__)

# ── Export version for forward compatibility ──────────────────────────────────
EXPORT_VERSION = "1.0"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    if not text:
        return
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"[BACKUP] Send failed: {e}")


def _collect_group_settings(group_id: int) -> dict:
    """
    Extracts all group-keyed settings for export.
    Returns dict of {key_suffix: value} for keys that match group_id.
    e.g. welcome_{group_id} → stored as "welcome": value
    """
    cursor = db.get_db().cursor()
    cursor.execute("SELECT key, value FROM settings", )
    all_settings = {row["key"]: row["value"] for row in cursor.fetchall()}

    prefix = f"_{group_id}"
    group_settings = {}

    for key, value in all_settings.items():
        if key.endswith(prefix) and value:
            # Strip group_id suffix to get the logical key
            logical_key = key[: -len(prefix)]
            group_settings[logical_key] = value

    return group_settings


# ── Export ────────────────────────────────────────────────────────────────────

def build_export(group_id: int) -> dict:
    """
    Builds the complete export dict for a group.
    All data collected here. No side effects.
    """
    cursor = db.get_db().cursor()
    now    = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    # ── Group row ─────────────────────────────────────────────────────────────
    group = db.get_group(group_id)
    group_data = {}
    if group:
        group_data = {
            "group_name":    group["group_name"],
            "level":         group["level"],
            "xp":            group["xp"],
            "anomaly_score": group["anomaly_score"],
        }

    # ── Warnings (active only) ────────────────────────────────────────────────
    cursor.execute(
        "SELECT user_id, count FROM warnings WHERE group_id = ? AND count > 0",
        (group_id,)
    )
    warnings = [{"user_id": r["user_id"], "count": r["count"]} for r in cursor.fetchall()]

    # ── Filters ───────────────────────────────────────────────────────────────
    cursor.execute("SELECT word FROM filters WHERE group_id = ? ORDER BY word", (group_id,))
    filters = [r["word"] for r in cursor.fetchall()]

    # ── Locks (enabled only) ──────────────────────────────────────────────────
    cursor.execute(
        "SELECT lock_type FROM locks WHERE group_id = ? AND enabled = 1",
        (group_id,)
    )
    locks = [r["lock_type"] for r in cursor.fetchall()]

    # ── Notes ─────────────────────────────────────────────────────────────────
    cursor.execute(
        "SELECT name, content FROM notes WHERE group_id = ? ORDER BY name",
        (group_id,)
    )
    notes = {r["name"]: r["content"] for r in cursor.fetchall()}

    # ── Group unlocks (reference IDs for progression) ─────────────────────────
    cursor.execute("""
        SELECT unlock_type, reference_id, part_number, unlocked_at
        FROM group_unlocks WHERE group_id = ?
        ORDER BY unlocked_at
    """, (group_id,))
    unlocks = [
        {
            "type":       r["unlock_type"],
            "ref":        r["reference_id"],
            "part":       r["part_number"],
            "at":         r["unlocked_at"],
        }
        for r in cursor.fetchall()
    ]

    # ── Group-keyed settings ──────────────────────────────────────────────────
    settings = _collect_group_settings(group_id)

    return {
        "_meta": {
            "version":    EXPORT_VERSION,
            "exported_at": now,
            "group_id":   group_id,
            "source":     "Presence Bot",
        },
        "group":    group_data,
        "warnings": warnings,
        "filters":  filters,
        "locks":    locks,
        "notes":    notes,
        "unlocks":  unlocks,
        "settings": settings,
    }


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /export — generates and sends the group config as a JSON file.
    Admin only.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    try:
        export_data = build_export(group_id)
        export_json = json.dumps(export_data, ensure_ascii=False, indent=2)
        export_bytes = export_json.encode("utf-8")
        size_kb = len(export_bytes) / 1024

        timestamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M")
        filename  = f"presence_export_{group_id}_{timestamp}.json"

        file_obj = io.BytesIO(export_bytes)
        file_obj.name = filename

        await update.message.reply_document(
            document=file_obj,
            filename=filename,
            caption=fmt_export_ready(filename, size_kb)[0],
            parse_mode="Markdown"
        )

        logger.info(f"[BACKUP] Export sent for group {group_id} ({size_kb:.1f} KB)")

    except Exception as e:
        logger.error(f"[BACKUP] Export failed for group {group_id}: {e}")
        db.log_error("backup", str(e), "L1", group_id)
        await _send(update, fmt_export_failed())


# ── Import ────────────────────────────────────────────────────────────────────

def restore_from_export(group_id: int, data: dict) -> dict:
    """
    Restores group config from export dict.
    Returns stats dict: {filters, locks, notes, warnings}
    Does NOT restore: group level/xp (preserve progression), events_log.
    Idempotent: safe to run twice on same data.
    """
    conn   = db.get_db()
    now    = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    stats  = {"filters": 0, "locks": 0, "notes": 0, "warnings": 0}

    # ── Filters ───────────────────────────────────────────────────────────────
    # Clear existing, insert from export
    conn.execute("DELETE FROM filters WHERE group_id = ?", (group_id,))
    for word in data.get("filters", []):
        if word and len(word) <= 100:
            conn.execute(
                "INSERT OR IGNORE INTO filters (group_id, word) VALUES (?, ?)",
                (group_id, word.lower().strip())
            )
            stats["filters"] += 1

    # ── Locks ─────────────────────────────────────────────────────────────────
    conn.execute("DELETE FROM locks WHERE group_id = ?", (group_id,))
    valid_locks = {"links", "media", "stickers", "gifs", "forwards", "bots", "all"}
    for lock_type in data.get("locks", []):
        if lock_type in valid_locks:
            conn.execute(
                "INSERT OR IGNORE INTO locks (group_id, lock_type, enabled, set_at) VALUES (?, ?, 1, ?)",
                (group_id, lock_type, now)
            )
            stats["locks"] += 1

    # ── Notes ─────────────────────────────────────────────────────────────────
    for name, content in data.get("notes", {}).items():
        if name and content and len(name) <= 32 and len(content) <= 4000:
            conn.execute("""
            INSERT INTO notes (group_id, name, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_id, name) DO UPDATE SET content = ?, updated_at = ?
            """, (group_id, name, content, now, now, content, now))
            stats["notes"] += 1

    # ── Warnings ──────────────────────────────────────────────────────────────
    for w in data.get("warnings", []):
        user_id = w.get("user_id")
        count   = w.get("count", 0)
        if user_id and count > 0:
            conn.execute("""
            INSERT INTO warnings (group_id, user_id, count, last_warned_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET count = ?, last_warned_at = ?
            """, (group_id, user_id, count, now, count, now))
            stats["warnings"] += 1

    # ── Settings (group-keyed only) ───────────────────────────────────────────
    for logical_key, value in data.get("settings", {}).items():
        if value:
            db.set_setting(f"{logical_key}_{group_id}", value)

    conn.commit()

    # Invalidate caches that depend on this data
    from bot.modules.notes import _cache_invalidate as notes_invalidate
    from bot.modules.locks import _invalidate_lock_cache
    notes_invalidate(group_id)
    _invalidate_lock_cache(group_id)

    return stats


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /import — reply to a Presence export JSON file to restore config.
    Admin only.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id

    if not await is_admin_cached(group_id, update.effective_user.id, context.bot):
        await _send(update, fmt_error_no_permission())
        return

    # Must reply to a document
    replied = update.message.reply_to_message
    if not replied or not replied.document:
        await _send(update, fmt_import_invalid_file())
        return

    doc = replied.document

    # Validate it's a JSON file
    if not (doc.file_name or "").endswith(".json"):
        await _send(update, fmt_import_invalid_file())
        return

    # Size check — 500KB max
    if doc.file_size and doc.file_size > 500_000:
        await _send(update, fmt_import_failed("File too large (max 500KB)"))
        return

    try:
        # Download file
        file_obj = await context.bot.get_file(doc.file_id)
        data_bytes = await file_obj.download_as_bytearray()
        data = json.loads(data_bytes.decode("utf-8"))

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        await _send(update, fmt_import_invalid_file())
        return
    except TelegramError as e:
        await _send(update, fmt_import_failed("Could not download file."))
        return

    # Validate structure
    if not isinstance(data, dict) or "_meta" not in data:
        await _send(update, fmt_import_invalid_file())
        return

    meta = data.get("_meta", {})
    if meta.get("source") != "Presence Bot":
        await _send(update, fmt_import_failed("Not a Presence export file."))
        return

    try:
        stats = restore_from_export(group_id, data)
        await _send(update, fmt_import_success(
            filters=stats["filters"],
            locks=stats["locks"],
            notes=stats["notes"],
            warnings=stats["warnings"]
        ))
        logger.info(f"[BACKUP] Import complete for group {group_id}: {stats}")

    except Exception as e:
        logger.error(f"[BACKUP] Import failed for group {group_id}: {e}")
        db.log_error("backup", str(e), "L1", group_id)
        await _send(update, fmt_import_failed())


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — shows backup instructions."""
    if not update.message:
        return
    await _send(update, fmt_backup_instructions())
