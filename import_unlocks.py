"""
PRESENCE BOT — UNLOCK TABLE IMPORTER
Reads:  data/unlock_table.json
Writes: unlock_entries table

Idempotent — safe to re-run.
Run this after db_init.py and whenever unlock_table.json changes.

Usage:
    python scraper/import_unlocks.py
    python scraper/import_unlocks.py --dry
"""

import json
import sqlite3
import os
import sys

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH  = os.path.join(BASE_DIR, "data",     "unlock_table.json")
DB_PATH    = os.path.join(BASE_DIR, "database", "presence.db")


def run(dry_run: bool = False) -> dict:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}\nRun run_setup.py first.")
        sys.exit(1)

    if not os.path.exists(JSON_PATH):
        print(f"[ERROR] unlock_table.json not found: {JSON_PATH}")
        sys.exit(1)

    with open(JSON_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    stats = {"inserted": 0, "skipped": 0, "error": 0}

    for entry in entries:
        entry_id = entry.get("id")
        if not entry_id:
            print(f"  [SKIP] Entry missing id field: {entry}")
            continue

        # Check if already exists
        cursor.execute("SELECT entry_id FROM unlock_entries WHERE entry_id = ?", (entry_id,))
        if cursor.fetchone():
            stats["skipped"] += 1
            if dry_run:
                print(f"  [DRY SKIP] {entry_id} already in DB")
            continue

        extra = json.dumps(entry.get("extra_conditions", [])) if entry.get("extra_conditions") else None

        if dry_run:
            print(f"  [DRY INSERT] {entry_id} | L{entry['level']} | {entry['type']}")
            stats["inserted"] += 1
            continue

        try:
            cursor.execute("""
            INSERT INTO unlock_entries
            (entry_id, level, type, message, extra_conditions, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (
                entry_id,
                entry["level"],
                entry["type"],
                entry.get("message"),
                extra,
                entry.get("note", "")
            ))
            conn.commit()
            stats["inserted"] += 1
            print(f"  [OK] {entry_id} | L{entry['level']} | {entry['type']}")
        except sqlite3.Error as e:
            stats["error"] += 1
            print(f"  [ERROR] {entry_id}: {e}")

    conn.close()

    print(f"\nUnlock table import: {stats['inserted']} inserted | "
          f"{stats['skipped']} skipped | {stats['error']} errors")
    return stats


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    run(dry_run=dry)
