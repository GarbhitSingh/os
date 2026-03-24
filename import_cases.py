"""
PRESENCE BOT — IMPORT PIPELINE
Reads: data/cases_wiki_raw.json + data/cases_ips_raw.json
Validates, transforms, assigns IDs, splits into parts
Writes: cases table + case_parts table

Rules:
  - Idempotent: safe to run multiple times
  - Deduplicates by case_id
  - Never touches bot runtime
  - Reports: imported / skipped / errors
"""

import json
import sqlite3
import os
import re
import sys
import hashlib
from datetime import datetime

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(BASE_DIR, "data")
DB_PATH    = os.path.join(BASE_DIR, "database", "presence.db")

SOURCES = {
    "wiki": os.path.join(DATA_DIR, "cases_wiki_raw.json"),
    "ips":  os.path.join(DATA_DIR, "cases_ips_raw.json"),
}

# ── ID Generator ──────────────────────────────────────────────────────────────

def make_case_id(prefix: str, name: str, index: int) -> str:
    """
    Generates stable case IDs.
    Format: IPS-001, WIKI-042, etc.
    Hash-based fallback ensures no collision on re-import.
    """
    slug = re.sub(r"[^a-z0-9]", "", name.lower())[:8]
    short_hash = hashlib.md5(name.encode()).hexdigest()[:4].upper()
    return f"{prefix.upper()}-{index:03d}-{short_hash}"


# ── Tier → unlock level mapping ───────────────────────────────────────────────

TIER_UNLOCK_LEVEL = {
    1: 3,   # common      → unlocks at level 3
    2: 4,   # investigation→ unlocks at level 4
    3: 6,   # anomaly     → unlocks at level 6
    4: 8,   # restricted  → unlocks at level 8
    5: 10,  # classified  → unlocks at level 10
}

RARITY_TIER_MAP = {
    "common":       1,
    "investigation":2,
    "anomaly":      3,
    "restricted":   4,
    "classified":   5,
}


# ── Part splitter ─────────────────────────────────────────────────────────────

def split_into_parts(description: str, case_id: str, base_unlock: int) -> list[dict]:
    """
    Splits a case description into investigation parts.

    Short cases (< 300 chars)  → 1 part
    Medium cases (300-700)     → 2 parts
    Long cases (700+)          → 3 parts

    Parts unlock at increasing levels.
    This creates the multi-part unlock progression.
    """
    parts = []
    text = description.strip()
    length = len(text)

    if length < 300:
        # Single part
        parts.append({
            "case_id":      case_id,
            "part_number":  1,
            "title":        "Initial Report",
            "content":      text,
            "unlock_level": base_unlock,
            "is_redacted":  0,
        })

    elif length < 700:
        # Two parts — split at natural boundary
        split_at = _find_split_point(text, 0.5)
        parts.append({
            "case_id":      case_id,
            "part_number":  1,
            "title":        "Initial Report",
            "content":      text[:split_at].strip(),
            "unlock_level": base_unlock,
            "is_redacted":  0,
        })
        parts.append({
            "case_id":      case_id,
            "part_number":  2,
            "title":        "Investigation Notes",
            "content":      text[split_at:].strip(),
            "unlock_level": base_unlock + 1,
            "is_redacted":  0,
        })

    else:
        # Three parts
        s1 = _find_split_point(text, 0.35)
        s2 = _find_split_point(text, 0.70)
        parts.append({
            "case_id":      case_id,
            "part_number":  1,
            "title":        "Initial Report",
            "content":      text[:s1].strip(),
            "unlock_level": base_unlock,
            "is_redacted":  0,
        })
        parts.append({
            "case_id":      case_id,
            "part_number":  2,
            "title":        "Investigation Notes",
            "content":      text[s1:s2].strip(),
            "unlock_level": base_unlock + 1,
            "is_redacted":  0,
        })
        parts.append({
            "case_id":      case_id,
            "part_number":  3,
            "title":        "Conclusion / Status",
            "content":      text[s2:].strip(),
            "unlock_level": base_unlock + 2,
            "is_redacted":  0,
        })

    return parts


def _find_split_point(text: str, ratio: float) -> int:
    """Finds nearest sentence boundary to the target ratio."""
    target = int(len(text) * ratio)
    # Look for sentence end near target
    for offset in range(0, 150):
        for pos in [target + offset, target - offset]:
            if 0 < pos < len(text) and text[pos] in ".!?":
                return pos + 1
    return target  # fallback to exact position


# ── Validator ─────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = ["name", "description", "country"]

def validate(raw: dict) -> tuple[bool, str]:
    """Returns (valid: bool, reason: str)"""
    for field in REQUIRED_FIELDS:
        if not raw.get(field):
            return False, f"Missing required field: {field}"
    if len(raw["name"]) < 3:
        return False, "Name too short"
    if len(raw["description"]) < 20:
        return False, "Description too short"
    return True, "ok"


# ── Transformer ───────────────────────────────────────────────────────────────

def transform(raw: dict, prefix: str, index: int) -> dict:
    """Converts raw scraped dict → DB-ready case dict."""
    rarity = raw.get("rarity", "common")
    tier   = raw.get("tier") or RARITY_TIER_MAP.get(rarity, 1)

    is_restricted = 1 if tier >= 4 else 0
    is_hidden     = 1 if tier >= 5 else 0
    unlock_level  = TIER_UNLOCK_LEVEL.get(tier, 3)

    case_id = make_case_id(prefix, raw["name"], index)

    parts = split_into_parts(raw["description"], case_id, unlock_level)

    return {
        "case": {
            "case_id":       case_id,
            "title":         raw["name"][:255],
            "country":       raw.get("country", "India"),
            "location":      raw.get("location", raw.get("country", "")),
            "type":          raw.get("type", "unknown"),
            "source":        raw.get("source", "")[:500],
            "unlock_level":  unlock_level,
            "total_parts":   len(parts),
            "tier":          tier,
            "rarity":        rarity,
            "is_restricted": is_restricted,
            "is_hidden":     is_hidden,
        },
        "parts": parts
    }


# ── DB Writer ─────────────────────────────────────────────────────────────────

def write_case(conn: sqlite3.Connection, case: dict, parts: list[dict]) -> str:
    """
    Writes one case + its parts.
    Returns: 'inserted' | 'skipped' | 'error'
    """
    cursor = conn.cursor()

    # Check if already exists
    cursor.execute("SELECT case_id FROM cases WHERE case_id = ?", (case["case_id"],))
    if cursor.fetchone():
        return "skipped"

    try:
        cursor.execute("""
        INSERT INTO cases
        (case_id, title, country, location, type, source,
         unlock_level, total_parts, tier, rarity, is_restricted, is_hidden)
        VALUES
        (:case_id, :title, :country, :location, :type, :source,
         :unlock_level, :total_parts, :tier, :rarity, :is_restricted, :is_hidden)
        """, case)

        for part in parts:
            cursor.execute("""
            INSERT INTO case_parts
            (case_id, part_number, title, content, unlock_level, is_redacted)
            VALUES
            (:case_id, :part_number, :title, :content, :unlock_level, :is_redacted)
            """, part)

        conn.commit()
        return "inserted"

    except sqlite3.Error as e:
        conn.rollback()
        print(f"[DB ERROR] {case['case_id']}: {e}")
        return "error"


# ── Main runner ───────────────────────────────────────────────────────────────

def run(dry_run: bool = False):
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found at {DB_PATH}")
        print("        Run database/db_init.py first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    stats = {
        "inserted": 0,
        "skipped":  0,
        "error":    0,
        "invalid":  0,
    }

    global_index = 1

    for source_key, source_path in SOURCES.items():
        if not os.path.exists(source_path):
            print(f"[IMPORT] Source not found, skipping: {source_path}")
            continue

        with open(source_path, "r", encoding="utf-8") as f:
            raw_cases = json.load(f)

        print(f"\n[IMPORT] Processing {len(raw_cases)} records from {source_key}...")

        prefix = "IPS" if source_key == "ips" else "WIKI"

        for raw in raw_cases:
            # Validate
            valid, reason = validate(raw)
            if not valid:
                stats["invalid"] += 1
                print(f"  [SKIP] Invalid ({reason}): {raw.get('name', '?')[:50]}")
                continue

            # Transform
            transformed = transform(raw, prefix, global_index)
            global_index += 1

            # Write (or dry-run report)
            if dry_run:
                print(f"  [DRY] {transformed['case']['case_id']} → "
                      f"{len(transformed['parts'])} parts | "
                      f"tier {transformed['case']['tier']} | "
                      f"unlock L{transformed['case']['unlock_level']}")
                stats["inserted"] += 1
            else:
                result = write_case(conn, transformed["case"], transformed["parts"])
                stats[result] += 1
                if result == "inserted":
                    print(f"  [OK] {transformed['case']['case_id']} — "
                          f"{transformed['case']['title'][:50]} "
                          f"({len(transformed['parts'])} parts)")

    conn.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"IMPORT COMPLETE {'(DRY RUN)' if dry_run else ''}")
    print(f"  Inserted : {stats['inserted']}")
    print(f"  Skipped  : {stats['skipped']} (already in DB)")
    print(f"  Invalid  : {stats['invalid']} (failed validation)")
    print(f"  Errors   : {stats['error']}")
    print(f"{'='*50}\n")

    return stats


if __name__ == "__main__":
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv
    if dry:
        print("[IMPORT] DRY RUN mode — no DB writes")
    run(dry_run=dry)
