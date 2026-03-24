"""
PRESENCE BOT — MAIN ENTRY POINT
Boot sequence (exact order from Step 6.1/6.2):

  1. config loaded
  2. logging configured
  3. DB connected + schema verified
  4. settings loaded into cache
  5. modules initialized
  6. handlers registered
  7. schedulers started
  8. polling started

Steps 1-4 are blocking. If any fail → bot does not start.
Schedulers only start after DB + settings confirmed.
"""

import logging
import logging.handlers
import asyncio
import os
import sys

# ── Step 1: config (must be first — everything imports from it) ──────────────
from bot.config import BOT_TOKEN, DB_PATH, ADMIN_ID, IS_DEV

# ── Step 2: logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if IS_DEV else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            os.path.join(os.path.dirname(__file__), "logs", "presence.log"),
            maxBytes=5 * 1024 * 1024,   # 5 MB per file
            backupCount=5,              # keep last 5 rotated files
            encoding="utf-8"
        )
    ]
)
logger = logging.getLogger(__name__)

# Suppress noisy PTB and APScheduler logs unless in dev
if not IS_DEV:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

# ── Telegram + scheduler imports ──────────────────────────────────────────────
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ── Step 3+4: DB init ─────────────────────────────────────────────────────────
from bot import db


# ── Module imports ────────────────────────────────────────────────────────────
# Order matches Step 6.2 exactly
from bot.modules import activity
from bot.modules import moderation
from bot.modules import cases
from bot.modules import logs as log_module
from bot.modules import anomaly as anomaly_module
from bot.modules import hidden
from bot.modules import admin_tools
from bot.modules import notes as notes_module
from bot.modules import locks as locks_module
from bot.modules import welcome as welcome_module
from bot.modules import log_channel as log_channel_module
from bot.modules import backup as backup_module
from bot.engine import unlock_engine
from bot.engine import event_engine
from bot.engine import condition_checker  # imported by event_engine, explicit here for clarity


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_message(update, context):
    """
    Core message handler — runs on every non-command group message.
    Step 6.4 flow: passive check → XP → level check → unlock trigger.
    Must complete fast (< 200ms). No heavy operations.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    if not update.effective_user or update.effective_user.is_bot:
        return

    group_id = update.effective_chat.id
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or ""

    # Ensure group exists in DB
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))

    # Step 1: Passive moderation check
    passed = await moderation.passive_check(update, context)
    if not passed:
        return  # Spam/filter action taken, stop processing

    # Step 1b: Lock check (after moderation, before XP)
    passed = await locks_module.check_locks(update, context)
    if not passed:
        return  # Message deleted by active lock, stop processing

    # Step 2: Award XP
    result = activity.award_message_xp(group_id, user_id, username)

    # Step 3: Level up?
    if result["level_changed"]:
        old_level = result["old_level"]
        new_level = result["new_level"]

        # Post level-up notification
        from bot.utils.formatter import fmt_level_up
        await update.message.reply_text(
            fmt_level_up(old_level, new_level),
            parse_mode="Markdown"
        )

        # Queue unlock check (non-blocking — fires on next unlock_watcher tick)
        unlock_engine.queue_check(group_id, new_level)


async def handle_new_member(update, context):
    """Fires on NEW_CHAT_MEMBERS."""
    if not update.effective_chat:
        return
    group_id = update.effective_chat.id
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))
    await welcome_module.handle_new_member(update, context)
    activity.award_join_xp(group_id)
    # Log to channel (best-effort)
    if update.message and update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if not member.is_bot:
                name = f"@{member.username}" if member.username else member.first_name
                await log_channel_module.log_join(group_id, name, member.id, context.bot)


async def handle_left_member(update, context):
    """Fires on LEFT_CHAT_MEMBER."""
    if not update.effective_chat:
        return
    await welcome_module.handle_left_member(update, context)
    # Log to channel (best-effort)
    if update.message and update.message.left_chat_member:
        member = update.message.left_chat_member
        if not member.is_bot:
            name = f"@{member.username}" if member.username else member.first_name
            await log_channel_module.log_leave(
                update.effective_chat.id, name, member.id, context.bot
            )


# ── Command: /start ───────────────────────────────────────────────────────────

async def _send(update, text_kb_tuple: tuple) -> None:
    """Helper for main.py command handlers."""
    text, kb = text_kb_tuple
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.warning(f"[MAIN] Send failed: {e}")


# ── Command: /start ───────────────────────────────────────────────────────────

async def cmd_start(update, context):
    """
    Installs bot in a group. Creates group record.
    Produces the same minimal response every time — idempotent.
    """
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.type == "private":
        await update.message.reply_text("Add me to a group to begin.")
        return

    group_id   = update.effective_chat.id
    group_name = update.effective_chat.title or str(group_id)
    db.upsert_group(group_id, group_name)

    from bot.utils.formatter import fmt_system
    await _send(update, fmt_system(
        "Presence initialized.\nMonitoring active.\nInvestigation records loading..."
    ))
    logger.info(f"[MAIN] /start in group {group_id} ({group_name})")


# ── Command: /help ────────────────────────────────────────────────────────────

async def cmd_help(update, context):
    """Minimal command list. No feature explanations."""
    if not update.message:
        return

    from bot.utils.formatter import fmt_help
    await _send(update, fmt_help())


# ── Command: /level ───────────────────────────────────────────────────────────

async def cmd_level(update, context):
    """Shows group's current level and XP progress."""
    if not update.effective_chat or not update.message:
        return
    if update.effective_chat.type == "private":
        from bot.utils.formatter import fmt_system
        await _send(update, fmt_system("Level tracking is per-group."))
        return

    group_id = update.effective_chat.id
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))

    try:
        progress = activity.get_group_progress(group_id)
        if not progress:
            from bot.utils.formatter import fmt_system
            await _send(update, fmt_system("No data available."))
            return

        from bot.utils.formatter import fmt_progress
        await _send(update, fmt_progress(
            progress["level"],
            progress["xp"],
            progress["xp_to_next"],
            progress["progress_pct"]
        ))
    except Exception as e:
        logger.warning(f"[MAIN] /level failed for {group_id}: {e}")
        db.log_error("main", str(e), "L1", group_id)


# ── Command: /status (admin only) ────────────────────────────────────────────

async def cmd_status(update, context):
    """
    /status — admin-only diagnostic.
    Shows group DB state, anomaly score, event counts.
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id
        )
        if member.status not in ("administrator", "creator"):
            return
    except Exception:
        return

    group_id = update.effective_chat.id
    db.upsert_group(group_id, update.effective_chat.title or str(group_id))

    try:
        group  = db.get_group(group_id)
        cursor = db.get_db().cursor()

        cursor.execute("SELECT COUNT(*) FROM events_log WHERE group_id=?", (group_id,))
        total_events = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM group_unlocks WHERE group_id=?", (group_id,))
        total_unlocks = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM warnings WHERE group_id=? AND count > 0", (group_id,))
        active_warns = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM filters WHERE group_id=?", (group_id,))
        filter_count = cursor.fetchone()[0]

        from bot.utils.formatter import fmt_status
        await _send(update, fmt_status(
            level=group["level"],
            xp=group["xp"],
            anomaly_score=group["anomaly_score"],
            total_events=total_events,
            total_unlocks=total_unlocks,
            active_warnings=active_warns,
            filter_count=filter_count,
            is_active=bool(group["is_active"])
        ))

    except Exception as e:
        logger.warning(f"[MAIN] /status failed for {group_id}: {e}")
        db.log_error("main", str(e), "L1", group_id)

# ── Handler registration ──────────────────────────────────────────────────────

def register_handlers(app: Application) -> None:
    """Registers all command and message handlers."""

    # Core commands
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("level",      cmd_level))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("case",       cases.cmd_case))
    app.add_handler(CommandHandler("cases",      cases.cmd_cases))

    # Moderation commands
    app.add_handler(CommandHandler("warn",       moderation.cmd_warn))
    app.add_handler(CommandHandler("unwarn",     moderation.cmd_unwarn))
    app.add_handler(CommandHandler("warnings",   moderation.cmd_warnings))
    app.add_handler(CommandHandler("mute",       moderation.cmd_mute))
    app.add_handler(CommandHandler("unmute",     moderation.cmd_unmute))
    app.add_handler(CommandHandler("ban",        moderation.cmd_ban))
    app.add_handler(CommandHandler("kick",       moderation.cmd_kick))
    app.add_handler(CommandHandler("filter",     moderation.cmd_filter))
    app.add_handler(CommandHandler("unfilter",   moderation.cmd_unfilter))
    app.add_handler(CommandHandler("filters",    moderation.cmd_filters))
    app.add_handler(CommandHandler("rules",      moderation.cmd_rules))
    app.add_handler(CommandHandler("setrules",   moderation.cmd_setrules))
    app.add_handler(CommandHandler("setwelcome", moderation.cmd_setwelcome))

    # Hidden / investigation layer
    app.add_handler(CommandHandler("restricted", hidden.cmd_restricted))
    app.add_handler(CommandHandler("classified", hidden.cmd_classified))

    # Backup / Export (Phase 12-F)
    app.add_handler(CommandHandler("export", backup_module.cmd_export))
    app.add_handler(CommandHandler("import", backup_module.cmd_import))
    app.add_handler(CommandHandler("backup", backup_module.cmd_backup))

    # Log channel (Phase 12-E)
    app.add_handler(CommandHandler("setlogchannel",   log_channel_module.cmd_setlogchannel))
    app.add_handler(CommandHandler("clearlogchannel", log_channel_module.cmd_clearlogchannel))
    app.add_handler(CommandHandler("logchannel",      log_channel_module.cmd_logchannel))

    # Welcome / Goodbye (Phase 12-D)
    app.add_handler(CommandHandler("setwelcome",         welcome_module.cmd_setwelcome))
    app.add_handler(CommandHandler("clearwelcome",       welcome_module.cmd_clearwelcome))
    app.add_handler(CommandHandler("setgoodbye",         welcome_module.cmd_setgoodbye))
    app.add_handler(CommandHandler("cleargoodbye",       welcome_module.cmd_cleargoodbye))
    app.add_handler(CommandHandler("setwelcomebuttons",  welcome_module.cmd_setwelcomebuttons))
    app.add_handler(CommandHandler("clearwelcomebuttons",welcome_module.cmd_clearwelcomebuttons))

    # Locks (Phase 12-B)
    app.add_handler(CommandHandler("lock",   locks_module.cmd_lock))
    app.add_handler(CommandHandler("unlock", locks_module.cmd_unlock))
    app.add_handler(CommandHandler("locks",  locks_module.cmd_locks))

    # Notes (Phase 12-A)
    app.add_handler(CommandHandler("save",    notes_module.cmd_save))
    app.add_handler(CommandHandler("get",     notes_module.cmd_get))
    app.add_handler(CommandHandler("notes",   notes_module.cmd_notes))
    app.add_handler(CommandHandler("delnote", notes_module.cmd_delnote))

    # Admin tools (Phase 12-C)
    app.add_handler(CommandHandler("admins",   admin_tools.cmd_admins))
    app.add_handler(CommandHandler("id",       admin_tools.cmd_id))
    app.add_handler(CommandHandler("userinfo", admin_tools.cmd_userinfo))
    app.add_handler(CommandHandler("report",   admin_tools.cmd_report))
    app.add_handler(CommandHandler("reports",  admin_tools.cmd_reports))

    # Message handlers
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_member
    ))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_left_member
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        handle_message
    ))

    logger.info("[MAIN] Handlers registered.")


# ── Scheduler setup ───────────────────────────────────────────────────────────

def build_scheduler(app: Application) -> AsyncIOScheduler:
    """
    Builds the async scheduler with all jobs.
    Jobs from Step 6.3:
      event_engine_tick     10 min
      unlock_watcher         1 min
      mute_expiry            5 min
      xp_decay_tick         12 hours
      global_state_sync     30 min
      settings_refresh      30 min
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        event_engine.run,
        "interval",
        minutes=10,
        id="event_engine_tick",
        args=[app],
        misfire_grace_time=60,
        coalesce=True,
    )
    scheduler.add_job(
        unlock_engine.run_pending_checks,
        "interval",
        minutes=1,
        id="unlock_watcher",
        args=[app],
        misfire_grace_time=30,
        coalesce=True,
    )
    scheduler.add_job(
        moderation.expire_mutes,
        "interval",
        minutes=5,
        id="mute_expiry",
        args=[app],           # expire_mutes uses context.bot — app has .bot attribute
        misfire_grace_time=30,
        coalesce=True,
    )
    scheduler.add_job(
        activity.apply_xp_decay,
        "interval",
        hours=12,
        id="xp_decay_tick",
        misfire_grace_time=300,
        coalesce=True,
    )
    scheduler.add_job(
        db.reload_settings,
        "interval",
        minutes=30,
        id="settings_refresh",
        misfire_grace_time=120,
        coalesce=True,
    )

    logger.info("[MAIN] Scheduler configured.")
    return scheduler


# ── Boot ──────────────────────────────────────────────────────────────────────

async def error_handler(update, context) -> None:
    """
    Global PTB error handler.
    Catches any unhandled exception from any handler or job.
    Logs to DB + Python logger. Never re-raises.
    """
    error = context.error
    if error is None:
        return

    group_id = None
    if update and update.effective_chat:
        group_id = update.effective_chat.id

    db.log_error("ptb_error_handler", str(error)[:1000], "L1", group_id)
    logger.error(f"[MAIN] Unhandled error in handler: {error}", exc_info=error)


def _verify_db_ready(db_path: str) -> None:
    """
    Checks that the DB is ready before starting.
    Raises on any problem — bot does not start if DB is not right.
    """
    import os
    import sqlite3

    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"[BOOT] Database not found: {db_path}\n"
            f"       Run: python run_setup.py"
        )

    # Verify it's a valid SQLite file and required tables exist
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    required = ["groups", "cases", "settings", "global_state", "event_types", "unlock_entries"]
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {r[0] for r in cursor.fetchall()}
    conn.close()

    missing = [t for t in required if t not in existing]
    if missing:
        raise RuntimeError(
            f"[BOOT] DB schema incomplete. Missing tables: {missing}\n"
            f"       Run: python run_setup.py"
        )


def main():
    # Ensure logs dir exists
    os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)

    logger.info("=" * 52)
    logger.info("  PRESENCE BOT — STARTING")
    logger.info("=" * 52)

    # Step 3: DB pre-check (before even connecting through db.py)
    logger.info("[BOOT] Verifying database...")
    _verify_db_ready(DB_PATH)

    # Step 4: DB init — connect, load settings cache
    logger.info("[BOOT] Connecting to database...")
    db.init(DB_PATH)
    logger.info(f"[BOOT] DB ready | Settings loaded")

    # Step 5–7: Build app
    logger.info("[BOOT] Building application...")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    register_handlers(app)

    # Global error handler — catches anything PTB misses
    app.add_error_handler(error_handler)

    scheduler = build_scheduler(app)

    # Scheduler starts inside event loop — after DB confirmed
    async def post_init(application):
        scheduler.start()
        logger.info("[BOOT] Scheduler started.")
        logger.info("[BOOT] Presence is running.")
        logger.info(f"[BOOT] Admin ID: {ADMIN_ID or 'not set'}")

    async def post_shutdown(application):
        if scheduler.running:
            scheduler.shutdown(wait=False)
        logger.info("[BOOT] Scheduler stopped. Shutdown complete.")

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("[BOOT] Starting polling...")
    app.run_polling(
        allowed_updates=["message", "chat_member"],
        drop_pending_updates=True,  # ignore updates that queued while bot was offline
    )


if __name__ == "__main__":
    main()
