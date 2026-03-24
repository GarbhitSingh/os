"""
PRESENCE BOT — ACTIVITY MODULE
Handles all XP and level logic.

Rules (from Step 5.1 + 5.2):
  - Message = +1 XP to group
  - First message of day per user = +10 bonus
  - Join = +5 XP to group
  - Max XP per user per minute = 8 (anti-spam)
  - Max XP per group per minute = 50
  - Decay: 48h silence → -5 XP per 12h tick
  - Level never drops (XP floor at current level threshold)
"""

import logging
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict

from bot import db
from bot.config import xp_to_level, xp_for_level, MAX_LEVEL

logger = logging.getLogger(__name__)

# ── In-memory rate limiters ───────────────────────────────────────────────────
# Per-user: tracks XP earned this minute
# Per-group: tracks total XP earned this minute
# Both reset every 60 seconds (handled by message timestamps)

_user_xp_this_minute:  dict[tuple[int, int], list[datetime]] = defaultdict(list)
_group_xp_this_minute: dict[int, list[datetime]]             = defaultdict(list)


def _clean_window(timestamps: list[datetime], now: datetime, seconds: int = 60) -> list[datetime]:
    """Removes timestamps older than the window. Returns filtered list."""
    cutoff = now - timedelta(seconds=seconds)
    return [t for t in timestamps if t > cutoff]


# ── Core XP functions ─────────────────────────────────────────────────────────

def award_message_xp(group_id: int, user_id: int, username: str = "") -> dict:
    """
    Awards XP for a regular message.

    Returns:
        {
            "xp_awarded":    int,
            "level_changed": bool,
            "old_level":     int,
            "new_level":     int,
            "total_xp":      int,
        }
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Ensure group + member exist
    db.upsert_member(group_id, user_id, username)
    group = db.get_group(group_id)
    if not group:
        return {"xp_awarded": 0, "level_changed": False,
                "old_level": 1, "new_level": 1, "total_xp": 0}

    # ── Rate limiting ─────────────────────────────────────────────────────────

    user_key = (group_id, user_id)
    _user_xp_this_minute[user_key]  = _clean_window(_user_xp_this_minute[user_key], now)
    _group_xp_this_minute[group_id] = _clean_window(_group_xp_this_minute[group_id], now)

    max_user  = db.setting("max_xp_per_minute", 8)
    max_group = db.setting("max_group_xp_per_min", 50)

    if len(_user_xp_this_minute[user_key]) >= max_user:
        return {"xp_awarded": 0, "level_changed": False,
                "old_level": group["level"], "new_level": group["level"],
                "total_xp": group["xp"]}

    if len(_group_xp_this_minute[group_id]) >= max_group:
        return {"xp_awarded": 0, "level_changed": False,
                "old_level": group["level"], "new_level": group["level"],
                "total_xp": group["xp"]}

    # ── Calculate XP to award ─────────────────────────────────────────────────

    xp_earned = db.setting("xp_per_message", 1)

    # Daily bonus — first message of today for this user
    if _is_first_message_today(group_id, user_id, now):
        xp_earned += db.setting("xp_daily_bonus", 10)

    # ── Apply XP ──────────────────────────────────────────────────────────────

    new_xp    = group["xp"] + xp_earned
    old_level = group["level"]
    new_level = xp_to_level(new_xp)

    # Update rate limiter windows
    for _ in range(xp_earned):
        _user_xp_this_minute[user_key].append(now)
        _group_xp_this_minute[group_id].append(now)

    # Write to DB
    db.update_group(group_id,
        xp=new_xp,
        level=new_level,
        last_message_time=now.isoformat()
    )

    # Update member stats
    member = db.get_member(group_id, user_id)
    if member:
        db.update_member(group_id, user_id,
            message_count=member["message_count"] + 1,
            xp_contributed=member["xp_contributed"] + xp_earned,
            last_active=now.isoformat()
        )

    level_changed = new_level != old_level
    if level_changed:
        logger.info(f"[ACTIVITY] Group {group_id} leveled up: {old_level} → {new_level} ({new_xp} XP)")

    return {
        "xp_awarded":    xp_earned,
        "level_changed": level_changed,
        "old_level":     old_level,
        "new_level":     new_level,
        "total_xp":      new_xp,
    }


def award_join_xp(group_id: int) -> None:
    """Awards XP when a new member joins. No rate limiting — joins are rare."""
    group = db.get_group(group_id)
    if not group:
        return
    xp_per_join = db.setting("xp_per_join", 5)
    new_xp      = group["xp"] + xp_per_join
    new_level   = xp_to_level(new_xp)
    db.update_group(group_id, xp=new_xp, level=new_level)
    logger.debug(f"[ACTIVITY] Group {group_id} join XP +{xp_per_join} → {new_xp}")


# ── Daily bonus tracking ──────────────────────────────────────────────────────

# Tracks {(group_id, user_id): date_string} for daily bonus
_daily_bonus_given: dict[tuple[int, int], str] = {}


def _is_first_message_today(group_id: int, user_id: int, now: datetime) -> bool:
    """Returns True if this is the user's first message today in this group."""
    key = (group_id, user_id)
    today = now.date().isoformat()
    if _daily_bonus_given.get(key) == today:
        return False
    _daily_bonus_given[key] = today
    return True


# ── XP Decay ─────────────────────────────────────────────────────────────────

def apply_xp_decay() -> int:
    """
    Called every 12 hours by the scheduler.
    For each group silent for decay_hours, removes decay_amount XP.
    XP never drops below the floor for the current level.
    Returns: number of groups affected.
    """
    now            = datetime.now(timezone.utc).replace(tzinfo=None)
    decay_hours    = db.setting("xp_decay_hours", 48)
    decay_amount   = db.setting("xp_decay_amount", 5)
    affected       = 0

    for group in db.get_active_groups():
        last_msg = group["last_message_time"]
        if not last_msg:
            continue

        last_msg_dt = datetime.fromisoformat(last_msg)
        silence_hours = (now - last_msg_dt).total_seconds() / 3600

        if silence_hours < decay_hours:
            continue

        current_xp    = group["xp"]
        current_level = group["level"]

        # Floor = XP required to reach current level
        floor_xp = xp_for_level(current_level)
        new_xp   = max(floor_xp, current_xp - decay_amount)

        if new_xp == current_xp:
            continue  # already at floor

        db.update_group(group["group_id"], xp=new_xp)
        affected += 1
        logger.debug(f"[ACTIVITY] Decay group {group['group_id']}: {current_xp} → {new_xp}")

    if affected:
        logger.info(f"[ACTIVITY] XP decay applied to {affected} groups")

    # ── Anomaly score decay (same 12h tick) ───────────────────────────────────
    # -1 per 12h tick for groups that exist.
    # Net weekly effect: -14 points. Groups need sustained event activity to maintain score.
    anomaly_decay = db.setting("anomaly_score_decay", 1)
    anomaly_affected = 0

    for group in db.get_active_groups():
        if group["anomaly_score"] > 0:
            new_score = max(0, group["anomaly_score"] - anomaly_decay)
            if new_score != group["anomaly_score"]:
                db.update_group(group["group_id"], anomaly_score=new_score)
                anomaly_affected += 1

    if anomaly_affected:
        logger.debug(f"[ACTIVITY] Anomaly decay applied to {anomaly_affected} groups")

    return affected


# ── Group progress info ───────────────────────────────────────────────────────

def get_group_progress(group_id: int) -> dict | None:
    """
    Returns a structured progress summary for a group.
    Used by /status and /level commands.
    """
    group = db.get_group(group_id)
    if not group:
        return None

    current_level = group["level"]
    current_xp    = group["xp"]

    if current_level >= MAX_LEVEL:
        xp_to_next  = 0
        next_level  = MAX_LEVEL
        progress_pct = 100
    else:
        next_threshold = xp_for_level(current_level + 1)
        curr_threshold = xp_for_level(current_level)
        xp_to_next     = next_threshold - current_xp
        range_size     = next_threshold - curr_threshold
        progress_pct   = int(((current_xp - curr_threshold) / range_size) * 100)
        next_level     = current_level + 1

    return {
        "group_id":      group_id,
        "level":         current_level,
        "xp":            current_xp,
        "xp_to_next":    xp_to_next,
        "next_level":    next_level,
        "progress_pct":  progress_pct,
        "anomaly_score": group["anomaly_score"],
    }


def update_anomaly_score(group_id: int, delta: int) -> int:
    """
    Adds delta to a group's anomaly_score.
    Clamps between 0 and anomaly_score_max.
    Returns new score.
    """
    group     = db.get_group(group_id)
    if not group:
        return 0
    max_score = db.setting("anomaly_score_max", 100)
    new_score = max(0, min(max_score, group["anomaly_score"] + delta))
    db.update_group(group_id, anomaly_score=new_score)

    if delta > 0 and new_score >= 80:
        # Check if we should increment global anomaly
        _check_global_anomaly(group_id, new_score)

    return new_score


def _check_global_anomaly(group_id: int, group_score: int) -> None:
    """Increments global anomaly counter when group score is very high."""
    from bot import db as database
    global_state = database.get_global_state()
    if group_score == 100:
        database.update_global_state(
            global_anomaly=global_state["global_anomaly"] + 1
        )
        logger.info(f"[ACTIVITY] Group {group_id} hit anomaly_score=100 → global anomaly incremented")
