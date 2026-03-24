"""
PRESENCE BOT — UNLOCK ENGINE
Pure deterministic logic. No scheduler. No event logic. No anomaly.

Responsibilities (Step 3.1 exactly):
  input: group_id, new_level
  ↓
  1. find case_parts WHERE unlock_level = new_level (from DB)
  2. find unlock_entries WHERE level = new_level (from DB, imported from JSON)
  3. for each found item:
       check group_unlocks (already fired?)
       check extra_conditions (if any)
       if clear → write group_unlocks → post message
  4. mark pending unlocks that have conditions not yet met

Two entry points:
  queue_check(group_id, level)        → called by message handler (non-blocking)
  run_pending_checks(context)         → called by scheduler every 1 min
  _process_group(group_id, level, bot) → actual logic (shared)
"""

import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from bot import db
from bot.utils.formatter import (
    fmt_unlock,
    fmt_log,
    fmt_level_up,
)

logger = logging.getLogger(__name__)

# ── Pending queue ─────────────────────────────────────────────────────────────
# Holds (group_id, level) pairs queued by message handler
# Consumed by run_pending_checks on scheduler tick

_pending_queue: list[tuple[int, int]] = []


def queue_check(group_id: int, level: int) -> None:
    """
    Non-blocking entry point called from message handler on level-up.
    Just queues the check — actual processing happens on next scheduler tick.
    """
    _pending_queue.append((group_id, level))
    logger.debug(f"[UNLOCK] Queued check: group {group_id} level {level}")


async def run_pending_checks(context) -> None:
    """
    Called by scheduler every 1 minute.
    Drains the queue and processes each pending unlock check.
    Also re-evaluates PENDING unlocks (conditions-gated).
    """
    bot = context.bot if hasattr(context, "bot") else context

    # Drain new level-up queue
    while _pending_queue:
        group_id, level = _pending_queue.pop(0)
        try:
            await _process_group(group_id, level, bot)
        except Exception as e:
            db.log_error("unlock_engine", str(e), "L1", group_id)
            logger.error(f"[UNLOCK] Error processing group {group_id}: {e}")

    # Re-check all groups with pending unlocks
    await _retry_pending(bot)


# ── Core processing ───────────────────────────────────────────────────────────

async def _process_group(group_id: int, level: int, bot: Bot) -> None:
    """
    Main unlock logic for one group at one level.
    Checks both case_parts and unlock_entries for this level.
    """
    logger.info(f"[UNLOCK] Processing group {group_id} at level {level}")

    # ── 1. Case parts that unlock at this level ───────────────────────────────
    cursor = db.get_db().cursor()
    cursor.execute("""
        SELECT cp.case_id, cp.part_number, cp.title, cp.unlock_level,
               c.title as case_title, c.tier, c.rarity
        FROM case_parts cp
        JOIN cases c ON cp.case_id = c.case_id
        WHERE cp.unlock_level = ?
        AND c.is_hidden = 0
        ORDER BY c.tier ASC, cp.case_id, cp.part_number
    """, (level,))
    parts = cursor.fetchall()

    for part in parts:
        already = _already_unlocked(group_id, "case_part", part["case_id"], part["part_number"])
        if already:
            continue

        # No extra conditions on case parts — level is sufficient
        await _fire_case_part_unlock(group_id, part, bot)

    # ── 2. Supplementary unlock_entries at this level ─────────────────────────
    cursor.execute("""
        SELECT * FROM unlock_entries
        WHERE level = ? AND is_active = 1
    """, (level,))
    entries = cursor.fetchall()

    for entry in entries:
        already = _already_unlocked(group_id, entry["type"], entry["entry_id"], 0)
        if already:
            continue

        # Check extra_conditions
        if entry["extra_conditions"]:
            conditions = json.loads(entry["extra_conditions"])
            if not _conditions_met(group_id, conditions):
                # Mark as pending — will retry later
                _mark_pending(group_id, entry["type"], entry["entry_id"], conditions)
                logger.info(f"[UNLOCK] Pending: {entry['entry_id']} for group {group_id} — conditions not met")
                continue

        await _fire_entry_unlock(group_id, entry, bot)


# ── Individual fire functions ─────────────────────────────────────────────────

async def _fire_case_part_unlock(group_id: int, part, bot: Bot) -> None:
    """Posts case part unlock notification and records it."""
    try:
        message = fmt_unlock(
            case_title=part["case_title"],
            part_title=part["title"],
            level=part["unlock_level"]
        )
        await bot.send_message(
            chat_id=group_id,
            text=message,
            parse_mode="Markdown"
        )

        _record_unlock(
            group_id=group_id,
            unlock_type="case_part",
            reference_type="case",
            reference_id=part["case_id"],
            part_number=part["part_number"]
        )

        logger.info(
            f"[UNLOCK] Case part fired: {part['case_id']} pt{part['part_number']} "
            f"→ group {group_id}"
        )

    except TelegramError as e:
        logger.warning(f"[UNLOCK] Failed to post to group {group_id}: {e}")
        # Mark L2 — will retry
        db.log_error("unlock_engine", f"Post failed: {e}", "L2", group_id)


async def _fire_entry_unlock(group_id: int, entry, bot: Bot) -> None:
    """Posts supplementary unlock entry and records it."""
    try:
        # Only post to group if entry has a message
        # anomaly_tier_unlock entries have null message — they just flip a flag
        if entry["message"]:
            entry_type = entry["type"]

            if entry_type == "log":
                text = fmt_log(entry["message"])
            elif entry_type == "hidden":
                text = f"`[CLASSIFIED]` _{entry['message']}_"
            else:
                text = fmt_log(entry["message"])

            await bot.send_message(
                chat_id=group_id,
                text=text,
                parse_mode="Markdown"
            )

        _record_unlock(
            group_id=group_id,
            unlock_type=entry["type"],
            reference_type="entry",
            reference_id=entry["entry_id"],
            part_number=0
        )

        logger.info(f"[UNLOCK] Entry fired: {entry['entry_id']} → group {group_id}")

    except TelegramError as e:
        logger.warning(f"[UNLOCK] Entry post failed for group {group_id}: {e}")
        db.log_error("unlock_engine", f"Entry post failed: {e}", "L2", group_id)


# ── Condition checking ────────────────────────────────────────────────────────

def _conditions_met(group_id: int, conditions: list[str]) -> bool:
    """
    Evaluates all extra_conditions for a group.
    All conditions must be True for the unlock to fire.

    Supported conditions:
      anomaly_event_fired_once   → at least 1 anomaly event in events_log
      night_event_fired_once     → at least 1 night event in events_log
      silence_event_fired_once   → at least 1 silence event in events_log
    """
    cursor = db.get_db().cursor()

    for condition in conditions:
        if condition == "anomaly_event_fired_once":
            cursor.execute("""
                SELECT COUNT(*) FROM events_log
                WHERE group_id = ? AND event_type = 'anomaly'
            """, (group_id,))
            if cursor.fetchone()[0] < 1:
                return False

        elif condition == "night_event_fired_once":
            cursor.execute("""
                SELECT COUNT(*) FROM events_log
                WHERE group_id = ? AND event_type = 'night'
            """, (group_id,))
            if cursor.fetchone()[0] < 1:
                return False

        elif condition == "silence_event_fired_once":
            cursor.execute("""
                SELECT COUNT(*) FROM events_log
                WHERE group_id = ? AND event_type = 'silence'
            """, (group_id,))
            if cursor.fetchone()[0] < 1:
                return False

        # Unknown condition — fail safe (don't unlock)
        else:
            logger.warning(f"[UNLOCK] Unknown condition: {condition}")
            return False

    return True


# ── Pending retry ─────────────────────────────────────────────────────────────

# In-memory pending store: { group_id: [(type, reference_id, conditions)] }
_pending_store: dict[int, list[dict]] = {}


def _mark_pending(group_id: int, unlock_type: str, reference_id: str, conditions: list) -> None:
    if group_id not in _pending_store:
        _pending_store[group_id] = []

    # Don't add duplicate
    for item in _pending_store[group_id]:
        if item["reference_id"] == reference_id:
            return

    _pending_store[group_id].append({
        "type":         unlock_type,
        "reference_id": reference_id,
        "conditions":   conditions,
    })


async def _retry_pending(bot: Bot) -> None:
    """
    Re-checks all pending unlocks for all groups.
    Called at the end of every run_pending_checks tick.
    """
    if not _pending_store:
        return

    for group_id, items in list(_pending_store.items()):
        group = db.get_group(group_id)
        if not group or not group["is_active"]:
            del _pending_store[group_id]
            continue

        still_pending = []
        for item in items:
            if _conditions_met(group_id, item["conditions"]):
                # Conditions now met — fire it
                cursor = db.get_db().cursor()
                cursor.execute(
                    "SELECT * FROM unlock_entries WHERE entry_id = ?",
                    (item["reference_id"],)
                )
                entry = cursor.fetchone()
                if entry:
                    already = _already_unlocked(group_id, item["type"], item["reference_id"], 0)
                    if not already:
                        try:
                            await _fire_entry_unlock(group_id, entry, bot)
                        except Exception as e:
                            still_pending.append(item)
                            logger.error(f"[UNLOCK] Pending retry failed: {e}")
                            continue
                # Successfully fired — don't re-add to pending
            else:
                still_pending.append(item)

        if still_pending:
            _pending_store[group_id] = still_pending
        else:
            del _pending_store[group_id]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _already_unlocked(
    group_id: int,
    unlock_type: str,
    reference_id: str,
    part_number: int
) -> bool:
    """Returns True if this exact unlock already exists for this group."""
    cursor = db.get_db().cursor()
    cursor.execute("""
        SELECT id FROM group_unlocks
        WHERE group_id = ?
          AND unlock_type = ?
          AND reference_id = ?
          AND part_number = ?
    """, (group_id, unlock_type, reference_id, part_number))
    return cursor.fetchone() is not None


def _record_unlock(
    group_id: int,
    unlock_type: str,
    reference_type: str,
    reference_id: str,
    part_number: int
) -> None:
    """Writes the unlock record. Never raises."""
    try:
        conn = db.get_db()
        conn.execute("""
            INSERT INTO group_unlocks
            (group_id, unlock_type, reference_type, reference_id, part_number,
             unlocked_at, was_announced)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (
            group_id, unlock_type, reference_type,
            reference_id, part_number,
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"[UNLOCK] Failed to record unlock: {e}")


# ── Public check: anomaly tier unlocked? ──────────────────────────────────────

def anomaly_tier_unlocked(group_id: int, tier: int) -> bool:
    """
    Used by event_engine to check if a group has unlocked a given anomaly tier.
    Tier 1 = ANOMALY-TIER-1, etc.
    """
    tier_id = f"ANOMALY-TIER-{tier}"
    return _already_unlocked(group_id, "anomaly_tier_unlock", tier_id, 0)
