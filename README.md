# Ostad-Bot

A Telegram bot for rating and reviewing university professors ("Ostad" means *professor* in Persian). Students can search professors, submit 1–5 star ratings with short comments, and suggest new professors — a moderation workflow keeps submissions under admin control before they become public.

## Features

- Professor search with fuzzy matching (`difflib.get_close_matches`) and paginated browsing
- Star-based rating flow (inline keyboards) with an optional short comment (max 20 words)
- Comment moderation queue — only approved content becomes visible
- Admin panel driven by the `ADMIN_IDS` environment variable: manage the professor list and review pending submissions
- Persian text normalization (Arabic/Yeh variants, whitespace) for reliable search
- SQLite storage with WAL mode and foreign keys enabled
- Flask keep-alive endpoint for hosting on platforms like Heroku (`Procfile` included)

## Tech Stack

- Python 3 · python-telegram-bot v20+
- SQLite
- Flask (keep-alive web server)
- Heroku (Procfile)

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Create a bot with [@BotFather](https://t.me/BotFather) and configure the environment variables (see `.env.example`):

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Bot token from @BotFather — **required, never hardcode it** |
| `ADMIN_IDS` | Comma-separated Telegram numeric user IDs with admin rights |
| `DATABASE_PATH` | SQLite file path (default: `ostad_bot.db`) |

3. Run:

```bash
python app.py
```

On the first run, if the database is empty, the bot seeds its professor list from `result.json` (a Telegram channel export) when that file is present.

## Deployment (Heroku)

The included `Procfile` starts the bot with the Flask keep-alive server:

```
web: python app.py
```

Set `BOT_TOKEN`, `ADMIN_IDS` and `DATABASE_PATH` in *Settings → Config Vars* on the Heroku dashboard.

## Project Structure

| File | Purpose |
|---|---|
| `app.py` | Bot logic: handlers, conversations, admin panel, database access |
| `requirements.txt` | Python dependencies |
| `Procfile` | Heroku process definition |
| `.env.example` | Template for required environment variables |

## Security Notes

- The bot token is read **only** from environment variables — no secrets are stored in the repository.
- `result.json` contains third-party channel content and is only used as optional seed data; keep it trimmed or remove it once the database is populated.
