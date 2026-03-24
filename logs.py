"""
PRESENCE BOT — LOGS MODULE
Surveillance log generation.

The event engine handles all log posting via message_pools.json.
This module provides helper functions used by other modules
when they need to post standalone log entries directly
(not through the event engine scheduler).

Used by:
  cases.py  → case access log
  main.py   → module is imported, confirms presence in init chain
"""

import logging
from datetime import datetime, timezone

from bot import db
from bot.utils.formatter import fmt_log, fmt_case_log

logger = logging.getLogger(__name__)


def post_case_access_log(group_id: int, case_title: str) -> None:
    """
    Records a case access event in events_log.
    Does NOT post to Telegram — logging only.
    Called from cases.py after a case is successfully displayed.
    """
    try:
        db.get_db().execute("""
            INSERT INTO events_log
            (group_id, event_type, message_sent, triggered_at, trigger_reason)
            VALUES (?, 'log', ?, ?, ?)
        """, (
            group_id,
            f"case_access:{case_title[:100]}",
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "user_command:/case"
        ))
        db.get_db().commit()
    except Exception as e:
        logger.warning(f"[LOGS] case_access log failed: {e}")


def get_recent_events(group_id: int, limit: int = 5) -> list:
    """
    Returns the N most recent events for a group.
    Used for debugging and future admin panel.
    """
    cursor = db.get_db().cursor()
    cursor.execute("""
        SELECT event_type, message_sent, triggered_at
        FROM events_log
        WHERE group_id = ?
        ORDER BY triggered_at DESC
        LIMIT ?
    """, (group_id, limit))
    return cursor.fetchall()
