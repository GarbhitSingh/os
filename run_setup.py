"""
PRESENCE BOT — MASTER SETUP RUNNER
Runs the full pipeline in correct order:

  1. Initialize DB (create schema + seed defaults)
  2. Import seed cases (cases_ips_raw.json — works offline)
  3. [Optional] Run live scrapers then re-import

Usage:
  python run_setup.py              → DB init + seed import
  python run_setup.py --scrape     → DB init + live scrape + import
  python run_setup.py --dry        → Dry run, no DB writes
  python run_setup.py --status     → Print current DB state
"""

import sys
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "database"))
sys.path.insert(0, os.path.join(BASE_DIR, "scraper"))

DB_PATH = os.path.join(BASE_DIR, "database", "presence.db")


def print_status():
    if not os.path.exists(DB_PATH):
        print("[STATUS] Database does not exist yet. Run setup first.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    tables = ["groups", "cases", "case_parts", "event_types", "settings", "global_state"]
    print("\n── DATABASE STATUS ─────────────────────────────────")
    for table in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            print(f"  {table:<20} {count:>5} rows")
        except sqlite3.Error:
            print(f"  {table:<20}   N/A (table missing)")

    print()

    # Case breakdown by tier
    try:
        cursor.execute("""
            SELECT tier, rarity, COUNT(*) as n
            FROM cases
            GROUP BY tier, rarity
            ORDER BY tier
        """)
        rows = cursor.fetchall()
        if rows:
            print("── CASE BREAKDOWN ──────────────────────────────────")
            for tier, rarity, count in rows:
                bar = "█" * count
                print(f"  Tier {tier} ({rarity:<15}) {count:>3}  {bar}")
        print()
    except sqlite3.Error:
        pass

    conn.close()


def run_init():
    import db_init
    conn = db_init.get_connection()
    db_init.init_schema(conn)
    conn.close()
    print("[SETUP] Database initialized.")


def run_import(dry=False):
    import import_cases
    return import_cases.run(dry_run=dry)


def run_scraper():
    print("\n[SETUP] Running live scrapers...")
    try:
        import scraper_wiki
        scraper_wiki.run()
    except Exception as e:
        print(f"[WARN] Wiki scraper failed: {e}")

    try:
        import scraper_ips
        scraper_ips.run()
    except Exception as e:
        print(f"[WARN] IPS scraper failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    dry   = "--dry" in args
    scrape = "--scrape" in args
    status = "--status" in args

    if status:
        print_status()
        sys.exit(0)

    print("=" * 52)
    print("  PRESENCE BOT — SETUP PIPELINE")
    print("=" * 52)

    # Step 1: DB init
    print("\n[STEP 1] Initializing database schema...")
    run_init()

    # Step 2: Optional live scrape
    if scrape:
        print("\n[STEP 2] Running live scrapers...")
        run_scraper()
    else:
        print("\n[STEP 2] Skipping live scrape (use --scrape to enable)")

    # Step 3: Import
    print("\n[STEP 3] Importing cases into database...")
    stats = run_import(dry=dry)

    # Step 4: Status report
    print("\n[STEP 4] Final status:")
    print_status()

    print("Setup complete. Bot is ready to run.")
