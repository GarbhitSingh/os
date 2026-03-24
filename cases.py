"""
PRESENCE BOT — CASES MODULE
Read-only. Never writes. Never calls scraper.
Displays cases + parts based on group level and unlock state.

Commands:
  /case [id]    → shows unlocked parts of a specific case
  /cases        → lists all cases visible to this group's level

Rules:
  - Parts unlock from case_parts.unlock_level (NOT cases.unlock_level)
  - Locked parts shown as [REDACTED] indicators
  - Classified cases (tier 5) show nothing until unlocked
  - Restricted cases (tier 4) show title only until unlocked
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from bot import db
from bot.utils.formatter import (
    fmt_case_part,
    fmt_case_locked,
    fmt_case_classified,
    fmt_case_classified_level_met,
    fmt_case_not_found,
    fmt_cases_index,
    fmt_system,
    fmt_log,
)

logger = logging.getLogger(__name__)

async def _send(update, text_kb_tuple: tuple) -> None:
    text, kb = text_kb_tuple
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.warning(f"[CASES] Send failed: {e}")



# ── /case [id] ────────────────────────────────────────────────────────────────

async def cmd_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /case IPS-001
    Shows all unlocked parts of the case for this group.
    Locked parts shown as redacted indicators.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        await _send(update, fmt_system("Case files are only accessible in group channels."))
        return

    group_id = update.effective_chat.id
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))
    group = db.get_group(group_id)

    if not context.args:
        await _send(update, fmt_system("Usage: /case [case-id]\nUse /cases to see available files."))
        return

    case_id_input = context.args[0].upper().strip()
    cursor = db.get_db().cursor()

    # Fuzzy match — allow partial ID (e.g. "IPS-001" matches "IPS-001-DFFA")
    cursor.execute("""
        SELECT * FROM cases
        WHERE case_id = ? OR case_id LIKE ?
        LIMIT 1
    """, (case_id_input, f"{case_id_input}%"))
    case = cursor.fetchone()

    if not case:
        await _send(update, fmt_system(f"No record found for: {case_id_input}"))
        return

    group_level = group["level"] if group else 1

    # ── Classified (tier 5): invisible until level + anomaly both met ────────
    if case["is_hidden"] and case["tier"] == 5:
        already_unlocked = _case_has_any_unlock(group_id, case["case_id"])
        if not already_unlocked:
            # Accessing a classified case always bumps anomaly score
            from bot.modules.activity import update_anomaly_score
            update_anomaly_score(group_id, delta=2)

            # Check level gate first
            if group_level < case["unlock_level"]:
                await _send(update, fmt_case_classified())
                return

            # Level met — now check anomaly_score threshold
            min_anomaly = db.setting("anomaly_score_min_classified", 50)
            current_score = group["anomaly_score"] if group else 0
            if current_score < min_anomaly:
                # Level is enough to know it exists, but not enough to open it
                await _send(update, fmt_case_classified_level_met())
                return

    # ── Restricted (tier 4): visible title, locked parts until level ─────────
    if case["is_restricted"] and case["tier"] == 4:
        if not _case_has_any_unlock(group_id, case["case_id"]):
            from bot.modules.activity import update_anomaly_score
            update_anomaly_score(group_id, delta=2)
            await _send(update, fmt_case_locked(case["case_id"], case["unlock_level"], group_level))
            return

    # ── Standard level gate ───────────────────────────────────────────────────
    if group_level < case["unlock_level"]:
        await _send(update, fmt_case_locked(case["case_id"], case["unlock_level"], group_level))
        return

    # ── Fetch all parts ───────────────────────────────────────────────────────
    cursor.execute("""
        SELECT * FROM case_parts
        WHERE case_id = ?
        ORDER BY part_number ASC
    """, (case["case_id"],))
    all_parts = cursor.fetchall()

    unlocked_parts = []
    locked_count   = 0

    for part in all_parts:
        if group_level >= part["unlock_level"]:
            unlocked_parts.append(part)
        else:
            locked_count += 1

    if not unlocked_parts:
        await _send(update, fmt_case_locked(case["case_id"], all_parts[0]["unlock_level"], group_level))
        return

    # ── Post each unlocked part ───────────────────────────────────────────────
    for i, part in enumerate(unlocked_parts):
        # Only show locked indicator on the last part
        locked_indicator = locked_count if i == len(unlocked_parts) - 1 else 0

        text = fmt_case_part(
            case_id=case["case_id"],
            case_title=case["title"],
            part_number=part["part_number"],
            part_title=part["title"],
            content=part["content"],
            total_parts=case["total_parts"],
            locked_parts=locked_indicator
        )
        await _send(update, (text, None))

    # Log case access — contributes to anomaly score slightly
    from bot.modules.activity import update_anomaly_score
    update_anomaly_score(group_id, delta=1)


# ── /cases ────────────────────────────────────────────────────────────────────

async def cmd_cases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /cases — lists all cases accessible to this group's level.
    Restricted/classified show as locked entries.
    Creates FOMO — users see names of things they can't access yet.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    group_id = update.effective_chat.id
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))
    group    = db.get_group(group_id)
    level    = group["level"] if group else 1

    cursor = db.get_db().cursor()
    cursor.execute("""
        SELECT case_id, title, tier, rarity, unlock_level, is_restricted, is_hidden
        FROM cases
        ORDER BY tier ASC, unlock_level ASC
    """)
    all_cases = cursor.fetchall()

    lines = ["`[CASE INDEX]` Available records:\n"]

    unlocked_count    = 0
    locked_count      = 0
    classified_exists = False  # we hint existence but not count

    for c in all_cases:
        case_level  = c["unlock_level"]
        is_unlocked = level >= case_level

        if c["is_hidden"] and c["tier"] == 5:
            # Classified: NEVER show ID, title, or quantity.
            # Only show one generic marker regardless of how many classified cases exist.
            classified_exists = True
            continue  # handled below after loop

        elif c["is_restricted"] and not is_unlocked:
            # Restricted, not unlocked: show name + lock (creates pull)
            lines.append(
                f"  `[{c['case_id']}]` {c['title']}\n"
                f"  _🔒 Requires Level {case_level}_"
            )
            locked_count += 1

        elif not is_unlocked:
            # Standard locked case
            lines.append(
                f"  `[{c['case_id']}]` _{c['title']}_\n"
                f"  _Locked — Level {case_level} required_"
            )
            locked_count += 1

        else:
            # Accessible: show with tier indicator
            tier_mark = _tier_prefix(c["tier"])
            lines.append(f"  `[{c['case_id']}]` {tier_mark} {c['title']}")
            unlocked_count += 1

    # Classified hint — one line, always the same, never more specific
    if classified_exists:
        lines.append(f"  `████████` — _Clearance insufficient_")

    lines.append(f"\n`{unlocked_count} accessible | {locked_count} restricted`")
    lines.append("_Use /case [id] to open a file._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _case_has_any_unlock(group_id: int, case_id: str) -> bool:
    """Returns True if any part of this case has been unlocked for this group."""
    cursor = db.get_db().cursor()
    cursor.execute("""
        SELECT id FROM group_unlocks
        WHERE group_id = ? AND reference_id = ?
        LIMIT 1
    """, (group_id, case_id))
    return cursor.fetchone() is not None


def _tier_prefix(tier: int) -> str:
    return {
        1: "📄",
        2: "🔍",
        3: "⚠️",
        4: "🔒",
        5: "☠️",
    }.get(tier, "📄")
