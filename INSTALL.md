# PRESENCE BOT — INSTALLATION GUIDE

## Prerequisites

- Python 3.11 or higher
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your personal Telegram user ID (from [@userinfobot](https://t.me/userinfobot))

---

## 1. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 2. Configure Environment

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```
BOT_TOKEN=your_bot_token_here
ADMIN_ID=your_telegram_user_id
ENV=production
```

Leave `DB_PATH` as default unless you have a specific reason to change it.

---

## 3. Initialize Database and Import Cases

Run the setup pipeline once before starting the bot:

```bash
python run_setup.py
```

This will:
- Create the SQLite database (`database/presence.db`)
- Build the full schema (14 tables)
- Import all seed cases from `data/cases_ips_raw.json`
- Import the unlock table from `data/unlock_table.json`
- Seed all default settings

To verify the setup ran correctly:

```bash
python run_setup.py --status
```

Expected output:
```
── DATABASE STATUS ──────────────────────────────────
  groups                   0 rows
  cases                   12 rows
  case_parts              36 rows
  event_types             12 rows
  settings                28 rows
  global_state             1 rows
```

---

## 4. (Optional) Run Live Scrapers

To add additional cases from Wikipedia:

```bash
python run_setup.py --scrape
```

This will scrape Wikipedia's haunted locations lists and import any new cases.
It is safe to run after the initial setup — the importer is idempotent.

---

## 5. Start the Bot

```bash
python bot/main.py
```

The bot will log to both stdout and `logs/presence.log`.

In development mode (`ENV=development` in `.env`), debug-level logging is active.
In production mode, only INFO and above is logged.

---

## 6. Add to a Group

1. Add the bot to a Telegram group
2. Make the bot an **administrator** with the following permissions:
   - Delete messages
   - Ban users
   - Restrict members
3. Send `/start` in the group

The bot will confirm with a minimal system message and begin monitoring.

---

## Commands Reference

### Available to all members
| Command | Description |
|---|---|
| `/level` | Shows group's current clearance level and XP progress |
| `/cases` | Lists all investigation records (locked and unlocked) |
| `/case [id]` | Opens a specific case file |
| `/restricted` | Shows restricted access layer |
| `/classified` | Shows classified access layer |
| `/help` | Command reference |
| `/rules` | Shows group rules (if set) |

### Admin only
| Command | Description |
|---|---|
| `/warn` | Reply to a message to warn the user |
| `/unwarn` | Reply to a message to remove one warning |
| `/warnings` | Reply to a message to check warning count |
| `/mute [minutes] [reason]` | Mute user (default: 60 min) |
| `/unmute` | Unmute user |
| `/ban [reason]` | Ban user |
| `/kick [reason]` | Remove user (allows rejoin) |
| `/filter [word]` | Add a word to the keyword filter |
| `/unfilter [word]` | Remove a word from the filter |
| `/filters` | List all active filters |
| `/setrules [text]` | Set group rules |
| `/setwelcome [text]` | Set welcome message (use `{name}` and `{group}`) |
| `/status` | Bot diagnostic — group stats, event count, anomaly score |

---

## Directory Structure

```
presence/
├── bot/
│   ├── main.py          — entry point, boot sequence, handlers
│   ├── config.py        — environment variables, level thresholds
│   ├── db.py            — database layer, settings cache
│   ├── engine/
│   │   ├── event_engine.py       — background event loop
│   │   ├── unlock_engine.py      — deterministic unlock logic
│   │   └── condition_checker.py  — group state evaluator
│   ├── modules/
│   │   ├── activity.py   — XP, levels, decay
│   │   ├── moderation.py — warn/mute/ban/filter
│   │   ├── cases.py      — case display and gating
│   │   ├── hidden.py     — restricted/classified commands
│   │   ├── anomaly.py    — anomaly score helpers
│   │   └── logs.py       — surveillance log helpers
│   └── utils/
│       └── formatter.py  — all message styling
├── database/
│   └── db_init.py       — schema definition
├── scraper/
│   ├── scraper_ips.py    — IPS/paranormal source scraper
│   ├── scraper_wiki.py   — Wikipedia scraper
│   ├── import_cases.py   — case import pipeline
│   └── import_unlocks.py — unlock table importer
├── data/
│   ├── cases_ips_raw.json    — seed case data
│   ├── unlock_table.json     — unlock event map
│   └── message_pools.json    — atmospheric message content
├── logs/                     — runtime logs (auto-created)
├── requirements.txt
├── run_setup.py
└── .env.example
```

---

## Tuning

All behavior is configurable via the `settings` table in the database.
No code changes required — use DB updates or add a settings editor later.

Key settings:
```
xp_per_message          = 1      (XP per message)
xp_daily_bonus          = 10     (first message of day per user)
warn_limit              = 3      (warnings before auto-mute)
mute_duration           = 60     (default mute in minutes)
silence_threshold       = 120    (minutes before silence events trigger)
event_check_interval    = 10     (minutes between event engine ticks)
anomaly_score_decay     = 1      (per 12h decay tick)
```

Level thresholds:
```
L2 = 200 XP    L6 = 2800 XP
L3 = 500 XP    L7 = 4000 XP
L4 = 1000 XP   L8 = 5600 XP
L5 = 1800 XP   L9 = 7600 XP
               L10 = 10000 XP
```

---

## Production Notes

- Bot requires `Administrator` rights in groups to mute/ban users
- SQLite is sufficient for hundreds of groups; migrate to PostgreSQL for larger scale
- Logs rotate manually — consider `logrotate` for production deployments
- Run with `nohup python bot/main.py &` or use a process manager (systemd, supervisor)
- The bot uses `drop_pending_updates=True` — messages sent while offline are ignored on restart
