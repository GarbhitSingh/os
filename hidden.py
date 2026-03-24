"""
PRESENCE BOT — HIDDEN LAYER MODULE (PHASE 11 VOICE)
All messages through formatter.py.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot import db
from bot.utils.formatter import (
    fmt_case_classified,
    fmt_restricted_list,
    fmt_restricted_high_anomaly,
    fmt_no_restricted,
)

logger = logging.getLogger(__name__)


async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"[HIDDEN] Send failed: {e}")


async def cmd_restricted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/restricted — shows restricted case access status."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))
    group = db.get_group(group_id)
    level = group["level"] if group else 1

    cursor = db.get_db().cursor()
    cursor.execute("""
        SELECT case_id, title, unlock_level
        FROM cases
        WHERE is_restricted = 1 AND is_hidden = 0
        ORDER BY unlock_level ASC
    """)
    restricted = cursor.fetchall()

    if not restricted:
        await _send(update, fmt_no_restricted())
        return

    entries = [dict(r) for r in restricted]
    await _send(update, fmt_restricted_list(entries, level))

    # Bump anomaly — probing restricted layer
    from bot.modules.activity import update_anomaly_score
    new_score = update_anomaly_score(group_id, delta=2)

    # At elevated anomaly, append a secondary note
    if new_score >= 40:
        await _send(update, fmt_restricted_high_anomaly())

    logger.info(f"[HIDDEN] /restricted accessed by group {group_id} | anomaly → {new_score}")


async def cmd_classified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/classified — acknowledges classified tier exists. Never varies. Never reveals."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))

    # Always the same response regardless of group state
    await _send(update, fmt_case_classified())

    from bot.modules.activity import update_anomaly_score
    new_score = update_anomaly_score(group_id, delta=5)
    logger.info(f"[HIDDEN] /classified accessed by group {group_id} | anomaly → {new_score}")
