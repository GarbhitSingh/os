"""
PRESENCE BOT — EVENT ENGINE
Global async loop. Runs every 10 minutes via scheduler.

Architecture: C (global loop + per-group cooldown logic)
  - One scheduler job fires globally
  - Each group evaluated independently using its own state
  - One group failure never stops others
  - Groups shuffled each tick — no group always processed first

Flow per tick:
  load active groups → shuffle → for each group:
    condition_checker.evaluate()
    build eligible event list (DB query, filtered by conditions)
    weighted random select (rarity field = weight)
    check per-event-type cooldown
    select message from pool
    post to group
    log to events_log
    update cooldown fields on group
    update anomaly_score
  trigger pending unlock recheck (after all groups)
  update global_state.total_events_fired

Anomaly score deltas (Step 5.5):
  log/spike    0 / +4
  silence      +3
  night        +5
  anomaly T1   +10  T2 +15  T3 +20
  classified   +15
"""

import json
import logging
import random
import os
from datetime import datetime, timezone

from telegram import Bot
from telegram.error import TelegramError

from bot import db
from bot.engine import condition_checker
from bot.engine.unlock_engine import run_pending_checks, anomaly_tier_unlocked
from bot.utils.formatter import fmt_log, fmt_anomaly

logger = logging.getLogger(__name__)

# ── Message pool cache ────────────────────────────────────────────────────────

_message_pools: dict[str, list[str]] = {}
_pools_loaded  = False

POOLS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "message_pools.json"
)


def _load_pools() -> None:
    global _message_pools, _pools_loaded
    if _pools_loaded:
        return
    try:
        with open(POOLS_PATH, "r", encoding="utf-8") as f:
            _message_pools = json.load(f)
        _pools_loaded = True
        logger.info(f"[EVENT ENGINE] Loaded {len(_message_pools)} message pools")
    except Exception as e:
        logger.error(f"[EVENT ENGINE] Failed to load pools: {e}")
        _message_pools = {}
        _pools_loaded  = True  # mark loaded so we don't retry every tick


def _pick_message(pool_key: str) -> str | None:
    pool = _message_pools.get(pool_key)
    if not pool:
        logger.warning(f"[EVENT ENGINE] Missing pool: {pool_key}")
        return None
    return random.choice(pool)


# ── Anomaly delta config ──────────────────────────────────────────────────────

ANOMALY_DELTA = {
    "log":        0,
    "spike":      4,
    "silence":    3,
    "night":      5,
    "classified": 15,
}

ANOMALY_TIER_DELTA = {
    "anomaly_tier1": 10,
    "anomaly_tier2": 15,
    "anomaly_tier3": 20,
}

# Maps event type → which groups table field tracks that type's cooldown
COOLDOWN_FIELD = {
    "log":        "last_log_time",
    "spike":      "last_log_time",
    "silence":    "last_event_time",
    "night":      "last_event_time",
    "anomaly":    "last_anomaly_time",
    "classified": "last_anomaly_time",
}


def _type_cooldown_clear(group, event_type: str, cooldown_minutes: int) -> bool:
    """Checks per-event-type cooldown. Returns True if enough time has passed."""
    field = COOLDOWN_FIELD.get(event_type, "last_event_time")
    last  = group[field]
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last)).total_seconds() / 60
        return elapsed >= cooldown_minutes
    except (ValueError, TypeError):
        return True


def _get_anomaly_delta(evt) -> int:
    """Returns anomaly_score delta for a fired event."""
    if evt["type"] == "anomaly":
        return ANOMALY_TIER_DELTA.get(evt["message_pool"], 10)
    return ANOMALY_DELTA.get(evt["type"], 0)


def _anomaly_tier_for_pool(pool_key: str) -> int:
    """anomaly_tier2 → 2, etc."""
    if "tier3" in pool_key:
        return 3
    if "tier2" in pool_key:
        return 2
    return 1


# ── Main scheduler entry point ────────────────────────────────────────────────

async def run(context) -> None:
    """Called by scheduler every 10 minutes."""
    _load_pools()

    bot    = context.bot if hasattr(context, "bot") else context
    groups = list(db.get_active_groups())

    if not groups:
        return

    random.shuffle(groups)  # no group always first or last
    fired_count = 0

    for group in groups:
        try:
            fired = await _process_group(group, bot)
            if fired:
                fired_count += 1
        except Exception as e:
            db.log_error("event_engine", str(e), "L1", group["group_id"])
            logger.error(f"[EVENT ENGINE] Group {group['group_id']} unhandled error: {e}")
            continue  # next group — never stop the loop

    if fired_count:
        logger.info(f"[EVENT ENGINE] Tick done. Fired: {fired_count}/{len(groups)}")
        try:
            gs = db.get_global_state()
            db.update_global_state(total_events_fired=gs["total_events_fired"] + fired_count)
        except Exception as e:
            logger.warning(f"[EVENT ENGINE] global_state update failed: {e}")

    # Anomaly decay is handled by the 12-hour XP decay scheduler job (activity.apply_xp_decay)
    # NOT here — per-tick decay would negate all score gain.

    # After all groups: recheck pending unlocks
    try:
        await run_pending_checks(bot)
    except Exception as e:
        logger.error(f"[EVENT ENGINE] Pending unlock recheck failed: {e}")


# ── Per-group logic ───────────────────────────────────────────────────────────

async def _process_group(group, bot: Bot) -> bool:
    """
    Processes one group. Returns True if an event was fired.
    Any exception here propagates to run() which catches and continues.
    """
    group_id = group["group_id"]
    report   = condition_checker.evaluate(group)

    # Global cooldown — skip group entirely if too recent
    if not report["cooldown_clear"]:
        return False

    silence_threshold = db.setting("silence_threshold", 120)

    # ── Build eligible event list ─────────────────────────────────────────────
    cursor = db.get_db().cursor()
    cursor.execute("""
        SELECT event_id, type, min_level, night_only, silence_only,
               cooldown_minutes, rarity, message_pool
        FROM event_types
        WHERE is_active = 1
          AND min_level <= ?
        ORDER BY rarity DESC
    """, (report["level"],))

    eligible = []
    for evt in cursor.fetchall():

        # Night-only events: skip if not night
        if evt["night_only"] and not report["is_night"]:
            continue

        # Silence-only events: skip if not silent enough
        if evt["silence_only"] and report["silence_minutes"] < silence_threshold:
            continue

        # Anomaly events: require anomaly tier to be unlocked
        if evt["type"] == "anomaly":
            tier = _anomaly_tier_for_pool(evt["message_pool"])
            if not anomaly_tier_unlocked(group_id, tier):
                continue

        # Classified: requires BOTH night AND silence
        if evt["type"] == "classified":
            if not report["is_night"]:
                continue
            if report["silence_minutes"] < silence_threshold:
                continue

        # Per-event-type cooldown
        if not _type_cooldown_clear(group, evt["type"], evt["cooldown_minutes"]):
            continue

        eligible.append(evt)

    if not eligible:
        return False

    # ── Weighted random selection ─────────────────────────────────────────────
    # rarity field IS the weight. Higher = more common. Lower = rarer.
    weights = [evt["rarity"] for evt in eligible]
    chosen  = random.choices(eligible, weights=weights, k=1)[0]

    # ── Pick message ──────────────────────────────────────────────────────────
    message_text = _pick_message(chosen["message_pool"])
    if not message_text:
        return False

    # ── Format ───────────────────────────────────────────────────────────────
    # Anomaly + classified: no [LOG] prefix — raw text, feels more real
    if chosen["type"] in ("anomaly", "classified"):
        formatted = fmt_anomaly(message_text)
    else:
        formatted = fmt_log(message_text)

    # ── Post ──────────────────────────────────────────────────────────────────
    try:
        await bot.send_message(
            chat_id=group_id,
            text=formatted,
            parse_mode="Markdown"
        )
    except TelegramError as e:
        err_lower = str(e).lower()
        if any(s in err_lower for s in ["blocked", "chat not found", "kicked", "deactivated"]):
            db.set_group_inactive(group_id, f"TelegramError on send: {e}")
            logger.warning(f"[EVENT ENGINE] Group {group_id} marked inactive")
        else:
            db.log_error("event_engine", f"Send failed: {e}", "L2", group_id)
            logger.warning(f"[EVENT ENGINE] Send failed for {group_id}: {e}")
        return False

    # ── Log ───────────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    trigger_note = (
        f"evt={chosen['event_id']} "
        f"silence={report['silence_minutes']}m "
        f"night={report['is_night']} "
        f"anomaly_score={report['anomaly_score']}"
    )
    db.get_db().execute("""
        INSERT INTO events_log
        (group_id, event_type, message_sent, triggered_at, trigger_reason)
        VALUES (?, ?, ?, ?, ?)
    """, (group_id, chosen["type"], message_text[:500], now, trigger_note))
    db.get_db().commit()

    # ── Update cooldowns ──────────────────────────────────────────────────────
    update_fields = {"last_event_time": now}
    specific_field = COOLDOWN_FIELD.get(chosen["type"])
    if specific_field and specific_field != "last_event_time":
        update_fields[specific_field] = now
    db.update_group(group_id, **update_fields)

    # ── Anomaly score ─────────────────────────────────────────────────────────
    delta = _get_anomaly_delta(chosen)
    if delta:
        from bot.modules.activity import update_anomaly_score
        update_anomaly_score(group_id, delta)

    logger.info(
        f"[EVENT ENGINE] {chosen['event_id']} fired → group {group_id} "
        f"| type={chosen['type']} delta={delta:+d}"
    )
    return True


async def _apply_anomaly_decay(groups: list) -> None:
    """
    Called after the main event loop each tick.
    Groups that did NOT fire an anomaly event lose anomaly_score_decay points.
    This prevents anomaly score from staying high indefinitely without activity.
    """
    decay = db.setting("anomaly_score_decay", 1)
    if not decay:
        return

    from bot.modules.activity import update_anomaly_score
    for group in groups:
        group_id    = group["group_id"]
        last_anomaly = group["last_anomaly_time"]

        # Only decay if no anomaly event has fired in this tick window
        # Proxy: last_anomaly_time is older than 10 minutes
        should_decay = True
        if last_anomaly:
            try:
                elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last_anomaly)).total_seconds() / 60
                should_decay = elapsed > 10
            except (ValueError, TypeError):
                should_decay = True

        if should_decay and group["anomaly_score"] > 0:
            update_anomaly_score(group_id, delta=-decay)
