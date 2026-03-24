"""
PRESENCE BOT — ANOMALY MODULE
Provides anomaly score querying and threshold checking.

The event engine handles all anomaly message posting.
This module provides:
  - score threshold checks used by cases.py and hidden.py
  - anomaly state query for any module that needs it
  - the anomaly message pool is in data/message_pools.json
    and is consumed by event_engine.py directly

Anomaly score thresholds (from settings table):
  anomaly_score_min_classified = 50  → classified access gate
  anomaly_score_min_restricted = 20  → restricted hint trigger
  anomaly_score_max            = 100 → ceiling
"""

import logging

from bot import db

logger = logging.getLogger(__name__)


def get_anomaly_score(group_id: int) -> int:
    """Returns current anomaly_score for a group. Returns 0 if group not found."""
    group = db.get_group(group_id)
    return group["anomaly_score"] if group else 0


def is_classified_accessible(group_id: int) -> bool:
    """
    Returns True if this group's anomaly_score meets the classified threshold.
    Used by cases.py before revealing classified content.
    """
    score     = get_anomaly_score(group_id)
    threshold = db.setting("anomaly_score_min_classified", 50)
    return score >= threshold


def is_restricted_hint_active(group_id: int) -> bool:
    """
    Returns True if anomaly_score is high enough to show restricted hints.
    Used by hidden.py to decide whether to add atmospheric notes.
    """
    score     = get_anomaly_score(group_id)
    threshold = db.setting("anomaly_score_min_restricted", 20)
    return score >= threshold


def get_anomaly_tier(group_id: int) -> int:
    """
    Returns the current anomaly tier for a group (1, 2, or 3).
    Based on score range:
      0–33   → tier 1 (or no tier if no ANOMALY-TIER-1 unlocked)
      34–66  → tier 2
      67–100 → tier 3
    Used by event_engine to determine which anomaly pool to draw from.
    This is the score-based tier, separate from the unlock-based tier.
    """
    score = get_anomaly_score(group_id)
    if score >= 67:
        return 3
    if score >= 34:
        return 2
    return 1


def log_anomaly_state(group_id: int) -> None:
    """Writes current anomaly state to debug log. Called by event engine if needed."""
    from bot.engine.unlock_engine import anomaly_tier_unlocked
    score = get_anomaly_score(group_id)
    t1    = anomaly_tier_unlocked(group_id, 1)
    t2    = anomaly_tier_unlocked(group_id, 2)
    t3    = anomaly_tier_unlocked(group_id, 3)
    logger.debug(
        f"[ANOMALY] Group {group_id}: score={score} "
        f"tiers_unlocked={int(t1)}/{int(t2)}/{int(t3)}"
    )
