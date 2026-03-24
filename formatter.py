"""
PRESENCE BOT — FORMATTER (FULL MESSAGE ARCHITECTURE)

Single source of truth for all bot messages.
No module constructs message strings directly.
All functions return (text: str, keyboard: None | list).

Voice rules (Phase 11):
  - No emojis in system messages
  - Short sentences
  - No friendly tone (no "Hi", "Done!", "Successfully", "Please", "Thanks")
  - Use tags where appropriate ([SYSTEM] [LOG] [ACCESS] [RECORD] etc.)
  - No long paragraphs
  - Consistent neutral system voice

Return type:
  All functions return tuple (text, keyboard).
  keyboard is None unless explicitly set.
  Modules unpack: text, kb = fmt_action(...)
  When buttons exist: await message.reply_text(text, reply_markup=kb)
  For now: await message.reply_text(text, parse_mode='Markdown')
"""

import json
import random
import os
import logging

logger = logging.getLogger(__name__)

# ── Response template loader ─────────────────────────────────────────────────

_templates: dict = {}
_templates_loaded = False

_TEMPLATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "response_templates.json"
)


def _load_templates() -> None:
    global _templates, _templates_loaded
    if _templates_loaded:
        return
    try:
        with open(_TEMPLATES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        # Strip the _note key
        _templates = {k: v for k, v in raw.items() if not k.startswith("_")}
        _templates_loaded = True
    except Exception as e:
        logger.error(f"[FORMATTER] Failed to load response templates: {e}")
        _templates = {}
        _templates_loaded = True


def _pick(key: str, fallback: str = "") -> str:
    """
    Picks a weighted random response from the template pool.
    Falls back to `fallback` if key not found.
    """
    _load_templates()
    pool = _templates.get(key)
    if not pool:
        return fallback

    texts   = [entry[0] for entry in pool]
    weights = [entry[1] for entry in pool]
    return random.choices(texts, weights=weights, k=1)[0]


# ── Type alias ────────────────────────────────────────────────────────────────

Response = tuple[str, None]   # (text, keyboard) — keyboard always None for now


# ── Core formatters ───────────────────────────────────────────────────────────

def fmt_system(text: str) -> Response:
    """
    System-level message. Monospace block.
    Used for: /start, /status, info responses.
    """
    return (f"`{text}`", None)


def fmt_log(text: str) -> Response:
    """
    Surveillance log. [LOG] prefix, inline code.
    Used by: event engine, case access, observation logs.
    """
    return (f"`[LOG]` {text}", None)


def fmt_action(text: str) -> Response:
    """
    Moderation action confirmation.
    Clean plain text — no prefix, no code.
    Short. Direct.
    """
    return (text, None)


def fmt_warning(name: str, count: int, limit: int, reason: str) -> Response:
    """
    Warning issued to a user.
    """
    text = (
        f"`[WARNING]`\n\n"
        f"{name}\n"
        f"Record: `{count}/{limit}`\n"
        f"Reason: {reason}"
    )
    return (text, None)


def fmt_anomaly(text: str) -> Response:
    """
    Anomaly message. No prefix, no formatting.
    Plain text only — makes it feel more real.
    """
    return (text, None)


def fmt_unlock(case_title: str, part_title: str, level: int) -> Response:
    """
    Unlock notification.
    Styled as system alert. Does not celebrate. Just reports.
    """
    text = (
        f"`[SYSTEM]` Access level updated.\n\n"
        f"Case record unlocked.\n"
        f"`{case_title}`\n"
        f"Section: `{part_title}`\n\n"
        f"_Use /case to access investigation files._"
    )
    return (text, None)


def fmt_level_up(old_level: int, new_level: int) -> Response:
    """
    Group level-up notification.
    System voice. No celebration.
    """
    text = (
        f"`[SYSTEM]` Activity threshold reached.\n\n"
        f"Clearance level updated: `{old_level}` → `{new_level}`\n\n"
        f"_New records may now be accessible._"
    )
    return (text, None)


def fmt_progress(level: int, xp: int, xp_to_next: int, progress_pct: int) -> Response:
    """
    Group progress display for /level.
    Progress bar from blocks.
    """
    filled  = progress_pct // 10
    empty   = 10 - filled
    bar     = "█" * filled + "░" * empty

    text = (
        f"`[STATUS]`\n\n"
        f"Clearance Level: `{level}`\n"
        f"Progress: `{bar}` {progress_pct}%\n"
        f"XP: `{xp}` (+{xp_to_next} to next level)\n"
    )
    return (text, None)


def fmt_case_part(
    case_id: str,
    case_title: str,
    part_number: int,
    part_title: str,
    content: str,
    total_parts: int,
    locked_parts: int
) -> Response:
    """
    Single case part display.
    Shows locked part indicators at bottom.
    """
    header = (
        f"`[CASE {case_id}]` *{case_title}*\n"
        f"Part {part_number} of {total_parts} — _{part_title}_\n"
        f"{'─' * 30}\n\n"
    )
    body   = content
    footer = ""

    if locked_parts > 0:
        locked_str = "  ".join(["▓▓▓▓▓"] * locked_parts)
        footer = (
            f"\n\n`[REDACTED: {locked_parts} section(s) require higher clearance]`\n"
            f"`{locked_str}`"
        )

    return (header + body + footer, None)


def fmt_case_locked(case_id: str, unlock_level: int, group_level: int) -> Response:
    """Shown when a group tries to access a case they haven't unlocked."""
    text = (
        f"`[CASE {case_id}]`\n\n"
        f"Access denied.\n"
        f"Required clearance: Level {unlock_level}\n"
        f"Current clearance: Level {group_level}\n\n"
        f"`This file is restricted.`"
    )
    return (text, None)


def fmt_case_classified() -> Response:
    """Shown for tier 5 cases — always identical, never varies."""
    text = (
        "`[RESTRICTED FILE]`\n\n"
        "This record is not accessible at your current clearance level.\n\n"
        "`████████████████████`\n"
        "`Classification: WITHHELD`"
    )
    return (text, None)


def fmt_case_classified_level_met() -> Response:
    """
    Shown when level is sufficient but anomaly score gate is not.
    Level is confirmed. Something else is blocking access.
    """
    text = (
        "`[CLASSIFIED FILE]`\n\n"
        "Level clearance confirmed.\n"
        "Access still restricted.\n\n"
        "`Further authorization required.`\n"
        "`████████████████████`"
    )
    return (text, None)


def fmt_silent_log(silence_minutes: int) -> Response:
    """Surveillance log for silence events."""
    hours = silence_minutes // 60
    if hours >= 1:
        duration = f"{hours}h {silence_minutes % 60}m"
    else:
        duration = f"{silence_minutes}m"
    text = f"`[LOG]` Inactivity period detected. Duration: {duration}. Monitoring continues."
    return (text, None)


def fmt_night_log() -> Response:
    return ("`[LOG]` Late-hour observation active. Signal quality reduced.", None)


def fmt_spike_log() -> Response:
    return ("`[LOG]` Unusual activity spike detected. Recording in progress.", None)


def fmt_case_log(case_title: str) -> Response:
    return (f"`[LOG]` Case file accessed: _{case_title}_. Cross-referencing records.", None)


# ── Moderation formatters (use response templates) ────────────────────────────

def fmt_ban_confirm(name: str, reason: str) -> Response:
    msg  = _pick("ban", "Access terminated.")
    text = f"`[ACTION]`\n\n{name}\n{msg}\nReason: {reason}"
    return (text, None)


def fmt_kick_confirm(name: str, reason: str) -> Response:
    msg  = _pick("kick", "Subject removed.")
    text = f"`[ACTION]`\n\n{name}\n{msg}\nReason: {reason}"
    return (text, None)


def fmt_mute_confirm(name: str, minutes: int, reason: str) -> Response:
    msg  = _pick("mute", "Communication restricted.")
    text = f"`[ACTION]`\n\n{name}\n{msg}\nDuration: {minutes} minutes\nReason: {reason}"
    return (text, None)


def fmt_unmute_confirm(name: str) -> Response:
    msg  = _pick("unmute", "Restrictions lifted.")
    text = f"`[ACTION]`\n\n{name}\n{msg}"
    return (text, None)


def fmt_warn_confirm(name: str, count: int, limit: int, reason: str) -> Response:
    msg  = _pick("warn", "Behavior recorded.")
    text = (
        f"`[WARNING]`\n\n"
        f"{name} — {count}/{limit}\n"
        f"{msg}\n"
        f"Reason: {reason}"
    )
    return (text, None)


def fmt_warn_auto_mute(name: str, count: int, limit: int, reason: str) -> Response:
    """Warn that also triggers auto-mute."""
    text = (
        f"`[WARNING]`\n\n"
        f"{name} — {count}/{limit}\n"
        f"Limit reached. Communication suspended for 60 minutes.\n"
        f"Reason: {reason}"
    )
    return (text, None)


def fmt_unwarn_confirm(name: str) -> Response:
    msg  = _pick("unwarn", "Record updated.")
    text = f"`[ACTION]`\n\n{name}\n{msg}"
    return (text, None)


def fmt_warnings_count(name: str, count: int, limit: int) -> Response:
    text = f"`[RECORD]`\n\n{name}\nWarnings: `{count}/{limit}`"
    return (text, None)


def fmt_filter_added(word: str) -> Response:
    msg  = _pick("filter_added", "Filter active.")
    text = f"`[SYSTEM]`\n\n{msg}\nPattern: `{word}`"
    return (text, None)


def fmt_filter_removed(word: str) -> Response:
    msg  = _pick("filter_removed", "Filter removed.")
    text = f"`[SYSTEM]`\n\n{msg}\nPattern: `{word}`"
    return (text, None)


def fmt_filter_list(words: list[str]) -> Response:
    if not words:
        msg  = _pick("no_filters", "No active filters.")
        return (f"`[SYSTEM]`\n\n{msg}", None)
    word_list = "\n".join(f"  {w}" for w in sorted(words))
    text = f"`[FILTER LIST]`\n\nActive ({len(words)}):\n\n{word_list}"
    return (text, None)


def fmt_rules(rules_text: str) -> Response:
    text = f"`[DIRECTIVES]`\n\n{rules_text}"
    return (text, None)


def fmt_rules_saved() -> Response:
    msg  = _pick("rules_set", "Rules updated.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


def fmt_welcome_saved() -> Response:
    msg  = _pick("welcome_set", "Welcome message saved.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


# ── Error formatters ──────────────────────────────────────────────────────────

def fmt_error_no_reply() -> Response:
    msg = _pick("error_no_reply", "Reply to a message to target a user.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


def fmt_error_no_permission() -> Response:
    msg = _pick("error_no_permission", "Insufficient clearance.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


def fmt_error_bot_not_admin() -> Response:
    msg = _pick("error_bot_not_admin", "Action failed. Insufficient bot permissions.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


def fmt_error_cannot_target_bot() -> Response:
    msg = _pick("error_cannot_target_bot", "Cannot target bots.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


def fmt_error_generic() -> Response:
    msg = _pick("error_generic", "Action failed.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


def fmt_case_not_found(case_id: str) -> Response:
    msg = _pick("case_not_found", "No record found.")
    return (f"`[SYSTEM]`\n\n{msg}: `{case_id}`", None)


# ── Status / info formatters ──────────────────────────────────────────────────

def fmt_status(
    level: int,
    xp: int,
    anomaly_score: int,
    total_events: int,
    total_unlocks: int,
    active_warnings: int,
    filter_count: int,
    is_active: bool
) -> Response:
    """Admin /status output."""
    text = (
        "`[STATUS]` Operational Diagnostic\n\n"
        f"Level: `{level}`\n"
        f"XP: `{xp}`\n"
        f"Anomaly score: `{anomaly_score}`\n\n"
        f"Events logged: `{total_events}`\n"
        f"Unlocks fired: `{total_unlocks}`\n"
        f"Active warnings: `{active_warnings}`\n"
        f"Active filters: `{filter_count}`\n\n"
        f"_Group is {'active' if is_active else 'inactive'}._"
    )
    return (text, None)


def fmt_help(is_admin: bool = False) -> Response:
    """
    Minimal help output. Lists commands only — no descriptions.
    Presence does not explain itself.
    """
    text = (
        "`[PRESENCE]`\n\n"
        "`/level` `/cases` `/case` `/restricted` `/classified`\n\n"
        "_Admin:_\n"
        "`/warn` `/unwarn` `/warnings` `/mute` `/unmute`\n"
        "`/ban` `/kick` `/filter` `/unfilter` `/filters`\n"
        "`/setrules` `/rules` `/setwelcome` `/status`"
    )
    return (text, None)


# ── Restricted/classified layer formatters ────────────────────────────────────

def fmt_restricted_list(entries: list[dict], group_level: int) -> Response:
    """
    /restricted command output.
    Shows restricted cases with access status.
    """
    lines = ["`[RESTRICTED ACCESS]`\n"]

    for c in entries:
        if group_level >= c["unlock_level"]:
            lines.append(f"  `[{c['case_id']}]` {c['title']} — _Accessible_")
        else:
            lines.append(
                f"  `[{c['case_id']}]` {c['title']}\n"
                f"  _Requires Level {c['unlock_level']} — Current: {group_level}_"
            )

    lines.append("\n_Some files require additional conditions beyond level clearance._")
    lines.append("_Classified records are not listed here._")
    return ("\n".join(lines), None)


def fmt_restricted_high_anomaly() -> Response:
    """Appended to /restricted when anomaly score is elevated."""
    return ("`[NOTE]` _Repeated access to this layer has been logged._", None)


def fmt_no_restricted() -> Response:
    msg = _pick("no_restricted", "No restricted records in current archive.")
    return (f"`[SYSTEM]`\n\n{msg}", None)


# ── Case index formatter ──────────────────────────────────────────────────────

def fmt_cases_index(
    unlocked: list[dict],
    locked: list[dict],
    restricted_locked: list[dict],
    classified_exists: bool
) -> Response:
    """
    /cases output.
    Classified cases: single ████████ marker, never title/ID/count.
    """
    lines = ["`[CASE INDEX]` Available records:\n"]

    for c in unlocked:
        tier_mark = _tier_prefix(c["tier"])
        lines.append(f"  `[{c['case_id']}]` {tier_mark} {c['title']}")

    for c in restricted_locked:
        lines.append(
            f"  `[{c['case_id']}]` {c['title']}\n"
            f"  _Locked — Level {c['unlock_level']} required_"
        )

    for c in locked:
        lines.append(
            f"  `[{c['case_id']}]` _{c['title']}_\n"
            f"  _Locked — Level {c['unlock_level']} required_"
        )

    if classified_exists:
        lines.append(f"  `████████` — _Clearance insufficient_")

    unlocked_count = len(unlocked)
    locked_count   = len(locked) + len(restricted_locked)
    lines.append(f"\n`{unlocked_count} accessible | {locked_count} restricted`")
    lines.append("_Use /case [id] to open a file._")

    return ("\n".join(lines), None)


def _tier_prefix(tier: int) -> str:
    """Returns the tier indicator for case index. No emoji for tiers 1-2."""
    return {
        1: "—",
        2: "—",
        3: "[!]",
        4: "[R]",
        5: "[C]",
    }.get(tier, "—")


# ── Phase 12-C: Admin tools formatters ───────────────────────────────────────

def fmt_admins_list(admins: list[dict]) -> Response:
    """
    /admins — lists current group admins.
    admins: list of {user_id, name, status}
    """
    if not admins:
        return ("`[SYSTEM]`\n\nNo admin records available.", None)

    lines = ["`[ADMIN LIST]`\n"]
    for a in admins:
        role = "Owner" if a.get("status") == "creator" else "Admin"
        name = a.get("name", str(a.get("user_id", "?")))
        lines.append(f"  {role}: {name}")

    return ("\n".join(lines), None)


def fmt_user_id(user_id: int, username: str | None, chat_id: int) -> Response:
    """
    /id — shows user and chat IDs.
    """
    name_part = f"@{username}" if username else "No username"
    text = (
        f"`[RECORD]`\n\n"
        f"User: `{user_id}` ({name_part})\n"
        f"Chat: `{chat_id}`"
    )
    return (text, None)


def fmt_user_info(
    user_id: int,
    username: str | None,
    message_count: int,
    xp_contributed: int,
    warn_count: int,
    warn_limit: int,
    joined_at: str | None,
    last_active: str | None,
    mod_actions: int
) -> Response:
    """
    /userinfo — full user profile from DB.
    """
    name    = f"@{username}" if username else "No username"
    joined  = joined_at[:10] if joined_at else "Unknown"
    active  = last_active[:10] if last_active else "Unknown"

    text = (
        f"`[RECORD]` User Profile\n\n"
        f"ID: `{user_id}`\n"
        f"Name: {name}\n\n"
        f"Messages: `{message_count}`\n"
        f"XP contributed: `{xp_contributed}`\n"
        f"Warnings: `{warn_count}/{warn_limit}`\n"
        f"Mod actions logged: `{mod_actions}`\n\n"
        f"Joined: `{joined}`\n"
        f"Last active: `{active}`"
    )
    return (text, None)


def fmt_report_sent() -> Response:
    return ("`[SYSTEM]`\n\nReport logged. Admins notified.", None)


def fmt_report_received(
    reporter_name: str,
    target_name: str | None,
    reason: str,
    message_preview: str | None
) -> Response:
    """
    Message sent TO ADMINS when a report is filed.
    """
    target_str = f"Subject: {target_name}\n" if target_name else ""
    preview    = f"\nMessage: _{message_preview[:200]}_\n" if message_preview else ""

    text = (
        f"`[REPORT]`\n\n"
        f"Filed by: {reporter_name}\n"
        f"{target_str}"
        f"Reason: {reason}"
        f"{preview}"
    )
    return (text, None)


def fmt_no_report_target() -> Response:
    return ("`[SYSTEM]`\n\nReply to a message to report it.", None)


def fmt_report_self() -> Response:
    return ("`[SYSTEM]`\n\nCannot report yourself.", None)


def fmt_report_admin() -> Response:
    return ("`[SYSTEM]`\n\nCannot report admins.", None)



# ── Phase 12-A: Notes formatters ─────────────────────────────────────────────

def fmt_note_content(name: str, content: str) -> Response:
    """
    /get [name] — displays a note.
    No prefix tag — notes are user-facing content, not system messages.
    """
    return (content, None)


def fmt_note_saved(name: str) -> Response:
    return (f"`[SYSTEM]`\n\nNote saved: `{name}`", None)


def fmt_note_deleted(name: str) -> Response:
    return (f"`[SYSTEM]`\n\nNote removed: `{name}`", None)


def fmt_note_not_found(name: str) -> Response:
    return (f"`[SYSTEM]`\n\nNo note found: `{name}`", None)


def fmt_note_list(names: list[str], group_name: str = "") -> Response:
    """
    /notes — lists all note names for this group.
    Shows names only, not content.
    """
    if not names:
        return ("`[SYSTEM]`\n\nNo notes saved.", None)

    sorted_names = sorted(names)
    name_list    = "  ".join(f"`{n}`" for n in sorted_names)
    text = (
        f"`[NOTES]`\n\n"
        f"{name_list}\n\n"
        f"_Use /get [name] to retrieve._"
    )
    return (text, None)


def fmt_note_invalid_name() -> Response:
    return (
        "`[SYSTEM]`\n\n"
        "Invalid note name.\n"
        "Use lowercase letters, numbers, underscores only.\n"
        "Max 32 characters.",
        None
    )


def fmt_note_too_long(max_len: int) -> Response:
    return (f"`[SYSTEM]`\n\nContent too long. Max {max_len} characters.", None)


# ── Phase 12-B: Locks formatters ─────────────────────────────────────────────

def fmt_lock_set(lock_type: str) -> Response:
    return (f"`[SYSTEM]`\n\n`{lock_type}` locked.", None)


def fmt_lock_unset(lock_type: str) -> Response:
    return (f"`[SYSTEM]`\n\n`{lock_type}` unlocked.", None)


def fmt_lock_unknown(lock_type: str) -> Response:
    return (
        f"`[SYSTEM]`\n\n"
        f"Unknown lock type: `{lock_type}`\n"
        f"Valid: links, media, stickers, gifs, forwards, bots, all",
        None
    )


def fmt_lock_list(active_locks: list[str]) -> Response:
    """
    /locks — lists currently active locks.
    """
    if not active_locks:
        return ("`[SYSTEM]`\n\nNo active locks.", None)

    lock_str = "  ".join(f"`{l}`" for l in sorted(active_locks))
    text = f"`[LOCKS]`\n\nActive:\n\n{lock_str}"
    return (text, None)


def fmt_lock_deleted(reason: str = "") -> Response:
    """Posted when a message is deleted by a lock. Silent by default — empty string."""
    return ("", None)   # Silent deletion — no message posted to group


# ── Phase 12-D: Welcome / Goodbye / Buttons formatters ───────────────────────

def fmt_welcome_text(
    raw_text: str,
    member_name: str,
    group_name: str,
    member_count: int | None = None
) -> str:
    """
    Resolves variables in welcome/goodbye text.
    Returns resolved string only — keyboard built separately.

    Supported variables:
      {name}    → member's display name or @username
      {group}   → group title
      {count}   → current member count (if available)
      {mention} → @username if available, else first name
    """
    text = raw_text
    text = text.replace("{name}",    member_name)
    text = text.replace("{mention}", member_name)
    text = text.replace("{group}",   group_name)
    if member_count is not None:
        text = text.replace("{count}", str(member_count))
    else:
        text = text.replace("{count}", "")
    return text.strip()


def fmt_build_keyboard(buttons_json: str | None):
    """
    Builds an InlineKeyboardMarkup from stored JSON.
    Returns None if no buttons or invalid JSON.

    Expected JSON format:
      [[{"text": "Label", "url": "https://..."}, ...], ...]
      Each inner list = one row of buttons.

    Note: requires telegram.InlineKeyboardMarkup and InlineKeyboardButton
    to be available at call time. Only called from welcome module at runtime.
    """
    if not buttons_json:
        return None

    import json
    try:
        rows = json.loads(buttons_json)
        if not rows:
            return None

        from telegram import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = []
        for row in rows:
            kb_row = []
            for btn in row:
                text = btn.get("text", "")
                if not text:
                    continue
                if "url" in btn:
                    kb_row.append(InlineKeyboardButton(text=text, url=btn["url"]))
                elif "callback_data" in btn:
                    kb_row.append(InlineKeyboardButton(text=text, callback_data=btn["callback_data"]))
            if kb_row:
                keyboard.append(kb_row)

        return InlineKeyboardMarkup(keyboard) if keyboard else None

    except Exception:
        return None


def fmt_welcome_set() -> Response:
    return ("`[SYSTEM]`\n\nWelcome message saved.", None)


def fmt_welcome_cleared() -> Response:
    return ("`[SYSTEM]`\n\nWelcome message cleared.", None)


def fmt_goodbye_set() -> Response:
    return ("`[SYSTEM]`\n\nGoodbye message saved.", None)


def fmt_goodbye_cleared() -> Response:
    return ("`[SYSTEM]`\n\nGoodbye message cleared.", None)


def fmt_welcome_buttons_set() -> Response:
    return ("`[SYSTEM]`\n\nWelcome buttons saved.", None)


def fmt_welcome_buttons_cleared() -> Response:
    return ("`[SYSTEM]`\n\nWelcome buttons cleared.", None)


def fmt_welcome_buttons_invalid() -> Response:
    return (
        "`[SYSTEM]`\n\n"
        "Invalid button format.\n"
        "Expected JSON: `[[{\"text\": \"Label\", \"url\": \"https://...\"}]]`",
        None
    )


def fmt_welcome_preview(text: str, keyboard=None) -> Response:
    """Returns the welcome message as it would appear, for /welcome preview."""
    return (text, keyboard)


def fmt_welcome_not_set() -> Response:
    return ("`[SYSTEM]`\n\nNo welcome message configured.", None)


def fmt_welcome_show_current(text: str) -> Response:
    """Shows admin the current welcome message template (with variables unexpanded)."""
    return (f"`[WELCOME TEMPLATE]`\n\n{text}", None)


# ── Phase 12-E: Log channel formatters ───────────────────────────────────────

def fmt_log_channel_set(channel_id: int) -> Response:
    return (f"`[SYSTEM]`\n\nLog channel set: `{channel_id}`", None)


def fmt_log_channel_cleared() -> Response:
    return ("`[SYSTEM]`\n\nLog channel removed.", None)


def fmt_log_channel_not_set() -> Response:
    return ("`[SYSTEM]`\n\nNo log channel configured.", None)


def fmt_log_channel_show(channel_id: int) -> Response:
    return (f"`[SYSTEM]`\n\nCurrent log channel: `{channel_id}`", None)


def fmt_log_event(
    action: str,
    target_name: str,
    target_id: int,
    issued_by_name: str,
    issued_by_id: int,
    reason: str = "",
    extra: str = ""
) -> Response:
    """
    Formats a single mod action for posting to the log channel.
    Clean, tagged, consistent with Phase 11 voice.
    No emojis. Short lines.
    """
    lines = [
        f"`[LOG]` `{action.upper()}`\n",
        f"Target: {target_name} (`{target_id}`)",
        f"By: {issued_by_name} (`{issued_by_id}`)",
    ]
    if reason and reason != "No reason given":
        lines.append(f"Reason: {reason}")
    if extra:
        lines.append(extra)

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\n_{ts}_")

    return ("\n".join(lines), None)


def fmt_log_join(member_name: str, member_id: int) -> Response:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")
    text = f"`[LOG]` `JOIN`\n\n{member_name} (`{member_id}`)\n\n_{ts}_"
    return (text, None)


def fmt_log_leave(member_name: str, member_id: int) -> Response:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")
    text = f"`[LOG]` `LEAVE`\n\n{member_name} (`{member_id}`)\n\n_{ts}_"
    return (text, None)


def fmt_log_report(
    reporter_name: str,
    reporter_id: int,
    target_name: str,
    target_id: int,
    reason: str
) -> Response:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"`[LOG]` `REPORT`\n\n"
        f"By: {reporter_name} (`{reporter_id}`)\n"
        f"Against: {target_name} (`{target_id}`)\n"
        f"Reason: {reason}\n\n"
        f"_{ts}_"
    )
    return (text, None)


# ── Phase 12-F: Backup/Export formatters ─────────────────────────────────────

def fmt_export_ready(filename: str, size_kb: float) -> Response:
    return (
        f"`[SYSTEM]`\n\n"
        f"Export ready.\n"
        f"File: `{filename}`\n"
        f"Size: `{size_kb:.1f} KB`\n\n"
        f"_Use /import to restore on a different instance._",
        None
    )


def fmt_export_failed() -> Response:
    return ("`[SYSTEM]`\n\nExport failed. Check bot permissions.", None)


def fmt_import_success(
    filters: int, locks: int, notes: int, warnings: int
) -> Response:
    return (
        f"`[SYSTEM]`\n\n"
        f"Import complete.\n\n"
        f"Filters: `{filters}`\n"
        f"Locks: `{locks}`\n"
        f"Notes: `{notes}`\n"
        f"Warnings: `{warnings}`",
        None
    )


def fmt_import_failed(reason: str = "") -> Response:
    msg = f"\nReason: {reason}" if reason else ""
    return (f"`[SYSTEM]`\n\nImport failed.{msg}", None)


def fmt_import_invalid_file() -> Response:
    return (
        "`[SYSTEM]`\n\n"
        "Invalid export file.\n"
        "Reply to a valid Presence export JSON.",
        None
    )


def fmt_backup_instructions() -> Response:
    return (
        "`[SYSTEM]`\n\n"
        "Bot data backup:\n\n"
        "`/export` — exports group config as JSON\n"
        "`/import` — reply to JSON file to restore\n\n"
        "_Full DB backup: copy `database/presence.db` on the server._",
        None
    )
