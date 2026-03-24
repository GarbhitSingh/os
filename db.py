"""
PRESENCE BOT — DATABASE LAYER
Single connection handler for the entire bot runtime.
All modules import from here — no module opens its own connection.

Provides:
  - get_db()         → sqlite3 connection (shared)
  - setting(key)     → cached setting value
  - reload_settings()→ refreshes settings cache from DB
  - log_error()      → writes to errors table
"""

import sqlite3
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Module state ──────────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None
_settings_cache: dict[str, str] = {}
_db_path: str = ""


# ── Init ──────────────────────────────────────────────────────────────────────

def init(db_path: str) -> sqlite3.Connection:
    """
    Opens the DB connection, verifies schema exists, loads settings.
    Called once at boot from main.py — after this, all modules use get_db().
    Raises on failure — bot does not start if DB is unavailable.
    """
    global _conn, _db_path

    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"[DB] Database not found at {db_path}\n"
            f"     Run: python run_setup.py"
        )

    _db_path = db_path
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("PRAGMA journal_mode = WAL")

    # Verify schema is initialized
    cursor = _conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
    if not cursor.fetchone():
        raise RuntimeError(
            "[DB] Schema not initialized.\n"
            "     Run: python run_setup.py"
        )

    reload_settings()
    logger.info(f"[DB] Connected → {db_path} | {len(_settings_cache)} settings loaded")
    return _conn


def get_db() -> sqlite3.Connection:
    """
    Returns the active connection.
    Performs a lightweight health check and reconnects if the connection
    has gone stale (can happen after long idle periods or system sleep).
    Raises if not initialized.
    """
    global _conn

    if _conn is None:
        raise RuntimeError("[DB] Database not initialized. Call db.init() first.")

    # Health check — detect stale connection
    try:
        _conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        # Connection closed — reconnect
        logger.warning("[DB] Stale connection detected. Reconnecting...")
        try:
            _conn = sqlite3.connect(_db_path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA foreign_keys = ON")
            _conn.execute("PRAGMA journal_mode = WAL")
            reload_settings()
            logger.info("[DB] Reconnected successfully.")
        except Exception as e:
            raise RuntimeError(f"[DB] Reconnect failed: {e}")

    return _conn


# ── Settings cache ────────────────────────────────────────────────────────────

def reload_settings() -> None:
    """Reloads all settings from DB into memory cache."""
    global _settings_cache
    cursor = get_db().cursor()
    cursor.execute("SELECT key, value FROM settings")
    _settings_cache = {row["key"]: row["value"] for row in cursor.fetchall()}
    logger.debug(f"[DB] Settings cache refreshed ({len(_settings_cache)} keys)")


def setting(key: str, fallback: Any = None) -> Any:
    """
    Returns a setting value from cache.
    Tries to cast to int automatically if possible.
    Falls back to config.FALLBACK if not in cache.
    """
    if key not in _settings_cache:
        if fallback is not None:
            return fallback
        # Try config fallback
        from bot.config import FALLBACK
        return FALLBACK.get(key)

    val = _settings_cache[key]
    try:
        return int(val)
    except (ValueError, TypeError):
        return val


def set_setting(key: str, value: Any) -> None:
    """Updates a setting in both DB and cache."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value))
    )
    conn.commit()
    _settings_cache[key] = str(value)


# ── Error logging ─────────────────────────────────────────────────────────────

def log_error(
    module: str,
    message: str,
    error_type: str = "L1",
    group_id: int | None = None
) -> None:
    """
    Writes an error record to the errors table.
    error_type: L1 (log+continue) | L2 (retry) | L3 (disable) | L4 (halt)
    Never raises — error logging must always succeed silently.
    """
    try:
        conn = get_db()
        conn.execute("""
        INSERT INTO errors (group_id, module, error_type, message, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """, (group_id, module, error_type, message[:1000], datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))
        conn.commit()
    except Exception:
        # Error logging itself failed — emit to Python logger only
        logger.error(f"[DB] Failed to log error: {module} | {message[:100]}")


# ── Group helpers ─────────────────────────────────────────────────────────────

def get_group(group_id: int) -> sqlite3.Row | None:
    cursor = get_db().cursor()
    cursor.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,))
    return cursor.fetchone()


def upsert_group(group_id: int, group_name: str) -> None:
    """Creates group record if it doesn't exist."""
    conn = get_db()
    conn.execute("""
    INSERT OR IGNORE INTO groups (group_id, group_name, installed_at)
    VALUES (?, ?, ?)
    """, (group_id, group_name, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))
    conn.commit()


def update_group(group_id: int, **fields) -> None:
    """
    Updates specific fields on a group row.
    Usage: update_group(group_id, xp=150, level=2)
    """
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [group_id]
    conn = get_db()
    conn.execute(f"UPDATE groups SET {set_clause} WHERE group_id = ?", values)
    conn.commit()


def set_group_inactive(group_id: int, reason: str = "") -> None:
    """Marks a group as inactive (bot removed, banned, etc.)"""
    update_group(group_id, is_active=0)
    log_error("db", f"Group {group_id} set inactive: {reason}", "L3", group_id)


def get_active_groups() -> list[sqlite3.Row]:
    cursor = get_db().cursor()
    cursor.execute("SELECT * FROM groups WHERE is_active = 1")
    return cursor.fetchall()


# ── Member helpers ────────────────────────────────────────────────────────────

def get_member(group_id: int, user_id: int) -> sqlite3.Row | None:
    cursor = get_db().cursor()
    cursor.execute(
        "SELECT * FROM members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id)
    )
    return cursor.fetchone()


def upsert_member(group_id: int, user_id: int, username: str = "") -> None:
    conn = get_db()
    conn.execute("""
    INSERT OR IGNORE INTO members (group_id, user_id, username, joined_at)
    VALUES (?, ?, ?, ?)
    """, (group_id, user_id, username, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))
    conn.commit()


def update_member(group_id: int, user_id: int, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [group_id, user_id]
    conn = get_db()
    conn.execute(
        f"UPDATE members SET {set_clause} WHERE group_id = ? AND user_id = ?",
        values
    )
    conn.commit()


# ── Global state ──────────────────────────────────────────────────────────────

def get_global_state() -> sqlite3.Row:
    cursor = get_db().cursor()
    cursor.execute("SELECT * FROM global_state WHERE id = 1")
    return cursor.fetchone()


def update_global_state(**fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())   # WHERE id=1 is a literal, not a binding
    conn = get_db()
    conn.execute(f"UPDATE global_state SET {set_clause} WHERE id = 1", values)
    conn.commit()
