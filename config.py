"""
PRESENCE BOT — CONFIG
Loads environment variables from .env file.
Provides typed constants used throughout the bot.
FAILS HARD at import if required variables are missing.
This is the first thing main.py loads — if this fails, nothing starts.
"""

import os
from dotenv import load_dotenv

# Load .env file relative to this file's location
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))


def _require(key: str) -> str:
    """Gets a required env var. Raises immediately if missing."""
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"[CONFIG] Required environment variable '{key}' is not set.\n"
            f"         Copy .env.example to .env and fill in the value."
        )
    return val


def _optional(key: str, default: str) -> str:
    return os.getenv(key, default)


# ── Required ──────────────────────────────────────────────────────────────────

BOT_TOKEN: str = _require("BOT_TOKEN")

# ── Optional with defaults ────────────────────────────────────────────────────

DB_PATH: str = _optional(
    "DB_PATH",
    os.path.join(_BASE_DIR, "database", "presence.db")
)

ADMIN_ID: int | None = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None

ENV: str = _optional("ENV", "development")
IS_DEV: bool = ENV == "development"

# ── Derived paths ─────────────────────────────────────────────────────────────

BASE_DIR:  str = _BASE_DIR
DATA_DIR:  str = os.path.join(_BASE_DIR, "data")
LOG_DIR:   str = os.path.join(_BASE_DIR, "logs")

# ── Runtime constants (fallbacks if settings table unreachable) ───────────────
# These are ONLY used before settings are loaded from DB.
# Once DB is live, settings_cache in db.py overrides these.

FALLBACK = {
    "xp_per_message":       1,
    "xp_per_join":          5,
    "xp_daily_bonus":       10,
    "max_xp_per_minute":    8,
    "max_group_xp_per_min": 50,
    "xp_decay_hours":       48,
    "xp_decay_amount":      5,
    "event_check_interval": 10,
    "night_start":          23,
    "night_end":            5,
    "silence_threshold":    120,
    "anomaly_score_max":    100,
    "anomaly_score_decay":  1,
    "warn_limit":           3,
    "mute_duration":        60,
}

# Level XP thresholds — used as fallback and for level calculation
# Tuned for slow progression (Phase 8):
#   Low activity  group: L10 in ~20 weeks
#   Medium activity group: L10 in ~9 weeks
#   High activity group: L10 in ~4 weeks
LEVEL_THRESHOLDS: dict[int, int] = {
    1:  0,
    2:  200,
    3:  500,
    4:  1000,
    5:  1800,
    6:  2800,
    7:  4000,
    8:  5600,
    9:  7600,
    10: 10000,
}

MAX_LEVEL: int = max(LEVEL_THRESHOLDS.keys())


def xp_to_level(xp: int) -> int:
    """Returns the level corresponding to a given XP amount."""
    level = 1
    for lvl, threshold in sorted(LEVEL_THRESHOLDS.items()):
        if xp >= threshold:
            level = lvl
        else:
            break
    return level


def xp_for_level(level: int) -> int:
    """Returns the XP required to reach a given level."""
    return LEVEL_THRESHOLDS.get(level, LEVEL_THRESHOLDS[MAX_LEVEL])


def xp_to_next_level(xp: int) -> tuple[int, int]:
    """
    Returns (current_level, xp_needed_for_next_level).
    Returns (MAX_LEVEL, 0) if already at max.
    """
    current = xp_to_level(xp)
    if current >= MAX_LEVEL:
        return (MAX_LEVEL, 0)
    next_threshold = LEVEL_THRESHOLDS[current + 1]
    return (current, next_threshold - xp)
