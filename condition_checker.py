"""
PRESENCE BOT — CONDITION CHECKER
Evaluates the current state of a single group.
Returns a structured report consumed by the event engine.

This module has no side effects — it only reads.
All decisions are made by the event engine based on this report.

Output fields (from Step 5.7):
  is_night              bool   — current time inside night window
  silence_minutes       int    — minutes since last group message
  level                 int    — current group level
  anomaly_score         int    — current group anomaly score
  cooldown_clear        bool   — enough time since last event
  activity_spike        bool   — group messaged within last 10 min
  night_events_fired    int    — total night events this group has seen
  silence_events_fired  int    — total silence events
  anomaly_events_fired  int    — total anomaly events
  pending_unlocks       list   — reference IDs of pending unlock entries
"""

import logging
from datetime import datetime, timezone

from bot import db

logger = logging.getLogger(__name__)


def evaluate(group) -> dict:
    """
    Evaluates current conditions for a group row.
    group: sqlite3.Row from groups table.
    Returns: condition report dict.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # ── Night window ──────────────────────────────────────────────────────────
    night_start  = db.setting("night_start", 23)
    night_end    = db.setting("night_end",   5)
    current_hour = now.hour

    # Night window wraps midnight: 23:00 → 05:00
    if night_start > night_end:
        is_night = current_hour >= night_start or current_hour < night_end
    else:
        is_night = night_start <= current_hour < night_end

    # ── Silence duration ──────────────────────────────────────────────────────
    silence_minutes = 0
    last_msg = group["last_message_time"]
    if last_msg:
        try:
            last_msg_dt     = datetime.fromisoformat(last_msg)
            silence_minutes = int((now - last_msg_dt).total_seconds() / 60)
            silence_minutes = max(0, silence_minutes)
        except (ValueError, TypeError):
            silence_minutes = 0

    # ── Global event cooldown ─────────────────────────────────────────────────
    cooldown_clear = True
    last_evt       = group["last_event_time"]
    event_cooldown = group["event_cooldown"] or db.setting("event_check_interval", 10)

    if last_evt:
        try:
            last_evt_dt         = datetime.fromisoformat(last_evt)
            minutes_since_event = (now - last_evt_dt).total_seconds() / 60
            cooldown_clear      = minutes_since_event >= event_cooldown
        except (ValueError, TypeError):
            cooldown_clear = True

    # ── Activity spike ────────────────────────────────────────────────────────
    # True if group sent a message within the last 10 minutes
    activity_spike = False
    if last_msg:
        try:
            last_msg_dt       = datetime.fromisoformat(last_msg)
            minutes_since_msg = (now - last_msg_dt).total_seconds() / 60
            activity_spike    = minutes_since_msg <= 10
        except (ValueError, TypeError):
            activity_spike = False

    # ── Events fired (from events_log) ───────────────────────────────────────
    group_id = group["group_id"]
    cursor   = db.get_db().cursor()

    cursor.execute("""
        SELECT event_type, COUNT(*) as cnt
        FROM events_log
        WHERE group_id = ?
        GROUP BY event_type
    """, (group_id,))

    event_counts = {row["event_type"]: row["cnt"] for row in cursor.fetchall()}

    night_events_fired   = event_counts.get("night",   0)
    silence_events_fired = event_counts.get("silence", 0)
    anomaly_events_fired = event_counts.get("anomaly", 0)

    # ── Pending unlocks ───────────────────────────────────────────────────────
    # Import here to avoid circular import at module level
    try:
        from bot.engine.unlock_engine import _pending_store
        pending_unlocks = [
            item["reference_id"]
            for item in _pending_store.get(group_id, [])
        ]
    except ImportError:
        pending_unlocks = []

    report = {
        "is_night":             is_night,
        "silence_minutes":      silence_minutes,
        "level":                group["level"],
        "anomaly_score":        group["anomaly_score"],
        "cooldown_clear":       cooldown_clear,
        "activity_spike":       activity_spike,
        "night_events_fired":   night_events_fired,
        "silence_events_fired": silence_events_fired,
        "anomaly_events_fired": anomaly_events_fired,
        "pending_unlocks":      pending_unlocks,
    }

    logger.debug(
        f"[CONDITION] Group {group_id}: "
        f"night={is_night} silence={silence_minutes}m "
        f"lvl={group['level']} anomaly={group['anomaly_score']} "
        f"cooldown_clear={cooldown_clear} spike={activity_spike}"
    )

    return report
