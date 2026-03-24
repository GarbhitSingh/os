"""
PRESENCE BOT — DATABASE INITIALIZATION
Builds full schema from scratch.
Safe to run multiple times (IF NOT EXISTS).
Run this before any scraper or bot.
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "presence.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn):
    cursor = conn.cursor()

    # ── GLOBAL STATE ────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS global_state (
        id                  INTEGER PRIMARY KEY,
        global_anomaly      INTEGER DEFAULT 0,
        global_level        INTEGER DEFAULT 1,
        last_global_event   DATETIME,
        total_groups        INTEGER DEFAULT 0,
        total_cases         INTEGER DEFAULT 0,
        total_events_fired  INTEGER DEFAULT 0
    )
    """)

    # Seed single row
    cursor.execute("""
    INSERT OR IGNORE INTO global_state (id, global_anomaly, global_level)
    VALUES (1, 0, 1)
    """)

    # ── GROUPS ──────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        group_id            INTEGER PRIMARY KEY,
        group_name          TEXT,
        xp                  INTEGER DEFAULT 0,
        level               INTEGER DEFAULT 1,
        unlock_stage        INTEGER DEFAULT 0,
        anomaly_score       INTEGER DEFAULT 0,
        last_message_time   DATETIME,
        last_event_time     DATETIME,
        last_unlock_time    DATETIME,
        last_log_time       DATETIME,
        last_anomaly_time   DATETIME,
        event_cooldown      INTEGER DEFAULT 180,
        installed_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_active           INTEGER DEFAULT 1
    )
    """)

    # ── MEMBERS ─────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS members (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id            INTEGER NOT NULL REFERENCES groups(group_id),
        user_id             INTEGER NOT NULL,
        username            TEXT,
        message_count       INTEGER DEFAULT 0,
        xp_contributed      INTEGER DEFAULT 0,
        joined_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_active         DATETIME,
        UNIQUE(group_id, user_id)
    )
    """)

    # ── CASES ───────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cases (
        case_id             TEXT PRIMARY KEY,
        title               TEXT NOT NULL,
        country             TEXT DEFAULT 'India',
        location            TEXT,
        type                TEXT DEFAULT 'unknown',
        source              TEXT,
        unlock_level        INTEGER DEFAULT 3,
        total_parts         INTEGER DEFAULT 1,
        tier                INTEGER DEFAULT 1,
        rarity              TEXT DEFAULT 'common',
        is_restricted       INTEGER DEFAULT 0,
        is_hidden           INTEGER DEFAULT 0,
        added_at            DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ── CASE PARTS ──────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS case_parts (
        part_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id             TEXT NOT NULL REFERENCES cases(case_id),
        part_number         INTEGER NOT NULL,
        title               TEXT,
        content             TEXT NOT NULL,
        unlock_level        INTEGER NOT NULL,
        is_redacted         INTEGER DEFAULT 0,
        UNIQUE(case_id, part_number)
    )
    """)

    # ── GROUP UNLOCKS ────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS group_unlocks (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id            INTEGER NOT NULL REFERENCES groups(group_id),
        unlock_type         TEXT NOT NULL,
        reference_type      TEXT,
        reference_id        TEXT,
        part_number         INTEGER DEFAULT 0,
        unlocked_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
        was_announced       INTEGER DEFAULT 0
    )
    """)

    # ── EVENTS LOG ──────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS events_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id            INTEGER NOT NULL REFERENCES groups(group_id),
        event_type          TEXT NOT NULL,
        message_sent        TEXT,
        triggered_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
        trigger_reason      TEXT
    )
    """)

    # ── EVENT TYPES ─────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS event_types (
        event_id            TEXT PRIMARY KEY,
        type                TEXT NOT NULL,
        min_level           INTEGER DEFAULT 1,
        night_only          INTEGER DEFAULT 0,
        silence_only        INTEGER DEFAULT 0,
        cooldown_minutes    INTEGER DEFAULT 120,
        rarity              INTEGER DEFAULT 100,
        message_pool        TEXT,
        is_active           INTEGER DEFAULT 1
    )
    """)

    # ── MODERATION LOG ──────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS moderation_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id            INTEGER NOT NULL,
        action              TEXT NOT NULL,
        target_user_id      INTEGER NOT NULL,
        issued_by           INTEGER NOT NULL,
        reason              TEXT,
        timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at          DATETIME
    )
    """)

    # ── WARNINGS ────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS warnings (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id            INTEGER NOT NULL,
        user_id             INTEGER NOT NULL,
        count               INTEGER DEFAULT 0,
        last_warned_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, user_id)
    )
    """)

    # ── FILTERS ─────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS filters (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id            INTEGER NOT NULL,
        word                TEXT    NOT NULL,
        added_by            INTEGER DEFAULT 0,
        added_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, word)
    )
    """)

    # ── REPORTS ─────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id        INTEGER NOT NULL,
        reporter_id     INTEGER NOT NULL,
        target_user_id  INTEGER,
        message_text    TEXT,
        reason          TEXT,
        reported_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        resolved        INTEGER DEFAULT 0,
        resolved_by     INTEGER
    )
    """)

    # ── NOTES ────────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    INTEGER NOT NULL,
        name        TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        created_by  INTEGER DEFAULT 0,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, name)
    )
    """)

    # ── LOCKS ─────────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS locks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    INTEGER NOT NULL,
        lock_type   TEXT    NOT NULL,
        enabled     INTEGER DEFAULT 1,
        set_by      INTEGER DEFAULT 0,
        set_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, lock_type)
    )
    """)

    # ── ERRORS ──────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS errors (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id            INTEGER,
        module              TEXT,
        error_type          TEXT,
        message             TEXT,
        timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,
        resolved            INTEGER DEFAULT 0
    )
    """)

    # ── SETTINGS ────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key                 TEXT PRIMARY KEY,
        value               TEXT NOT NULL,
        description         TEXT
    )
    """)

    _seed_settings(cursor)
    _seed_event_types(cursor)
    conn.commit()
    print(f"[DB INIT] Schema ready → {DB_PATH}")


def _seed_settings(cursor):
    defaults = [
        ("xp_per_message",       "1",    "XP awarded per group message"),
        ("xp_per_join",          "5",    "XP awarded when member joins"),
        ("xp_daily_bonus",       "10",   "Bonus XP for first message of day per user"),
        ("max_xp_per_minute",    "8",    "Max XP one user can earn per minute (anti-spam)"),
        ("max_group_xp_per_min", "50",   "Max XP entire group earns per minute"),
        ("xp_decay_hours",       "48",   "Hours of silence before XP decay starts"),
        ("xp_decay_amount",      "5",    "XP removed per decay tick"),
        ("event_check_interval", "10",   "Minutes between event engine ticks"),
        ("night_start",          "23",   "Hour when night window starts (24h)"),
        ("night_end",            "5",    "Hour when night window ends (24h)"),
        ("silence_threshold",    "120",  "Minutes of silence before silence events eligible"),
        ("anomaly_score_max",    "100",  "Maximum anomaly score per group"),
        ("anomaly_score_decay",  "1",    "Anomaly score lost per engine tick with no trigger"),
        ("warn_limit",           "3",    "Warns before auto-mute"),
        ("mute_duration",        "60",   "Default mute duration in minutes"),
        ("anti_flood_msgs",      "5",    "Max messages per user in 10 seconds before flood action"),
        ("anti_flood_window",    "10",   "Seconds window for flood detection"),
        ("anti_repeat_count",    "3",    "Same message N times triggers auto-warn"),
        ("auto_mute_on_warn_limit", "1", "Auto-mute when warn limit reached (1=yes, 0=no)"),
        ("log_mod_actions",      "1",    "Log all mod actions to moderation_log"),
        ("level_2_xp",           "200",  "XP required for level 2"),
        ("level_3_xp",           "500",  "XP required for level 3"),
        ("level_4_xp",           "1000", "XP required for level 4"),
        ("level_5_xp",           "1800", "XP required for level 5"),
        ("level_6_xp",           "2800", "XP required for level 6"),
        ("level_7_xp",           "4000", "XP required for level 7"),
        ("level_8_xp",           "5600", "XP required for level 8"),
        ("level_9_xp",           "7600", "XP required for level 9"),
        ("level_10_xp",          "10000","XP required for level 10"),
    ]
    for key, value, desc in defaults:
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)",
            (key, value, desc)
        )


def _seed_event_types(cursor):
    types = [
        # event_id, type, min_level, night_only, silence_only, cooldown, rarity, pool
        ("EVT_LOG_ACTIVITY",    "log",      1,  0, 0, 60,  100, "log_activity"),
        ("EVT_LOG_CASE",        "log",      3,  0, 0, 120, 80,  "log_case"),
        ("EVT_LOG_SIGNAL",      "log",      2,  0, 0, 90,  90,  "log_signal"),
        ("EVT_SILENCE_1",       "silence",  2,  0, 1, 180, 70,  "silence_tier1"),
        ("EVT_SILENCE_2",       "silence",  5,  0, 1, 240, 40,  "silence_tier2"),
        ("EVT_NIGHT_1",         "night",    3,  1, 0, 360, 60,  "night_tier1"),
        ("EVT_NIGHT_2",         "night",    6,  1, 0, 480, 25,  "night_tier2"),
        ("EVT_ANOMALY_1",       "anomaly",  5,  0, 0, 360, 30,  "anomaly_tier1"),
        ("EVT_ANOMALY_2",       "anomaly",  7,  0, 0, 480, 15,  "anomaly_tier2"),
        ("EVT_ANOMALY_3",       "anomaly",  9,  1, 0, 720, 5,   "anomaly_tier3"),
        ("EVT_SPIKE",           "spike",    1,  0, 0, 120, 80,  "spike_activity"),
        ("EVT_CLASSIFIED",      "classified",8, 1, 1, 720, 2,   "classified_tier1"),
    ]
    for row in types:
        cursor.execute("""
        INSERT OR IGNORE INTO event_types
        (event_id, type, min_level, night_only, silence_only, cooldown_minutes, rarity, message_pool)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, row)


if __name__ == "__main__":
    conn = get_connection()
    init_schema(conn)
    conn.close()
