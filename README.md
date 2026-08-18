# Section Monitor Bot

# Self hosting
Telegram bot that monitors KSU Edugate course sections and notifies when:
- 🆕 New sections become available
- ❌ Sections fill up

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
```
BOT_TOKEN=your_bot_token
ADMIN_ID=123456789
EDUGATE_USERNAME=your_student_id
EDUGATE_PASSWORD=your_edugate_password
CHECK_INTERVAL=60
MIN_CHECK_INTERVAL=15
CHECK_JITTER=5
```

Edugate credentials stay in `.env` only. The bot never asks for them in Telegram.

`CHECK_INTERVAL` and `MIN_CHECK_INTERVAL` are in minutes. `CHECK_JITTER` is seconds — each cycle waits interval ± jitter (e.g. 3 minutes ± 5 seconds).

Then start the bot:
```bash
python bot.py
```

## Coolify

This is a long-running worker (Telegram polling), not an HTTP app. Do not assign a public domain or port.

1. New resource → **Docker Compose** (recommended) or **Dockerfile**.
2. Point it at this repo.
3. Set these environment variables in Coolify (same names as `.env`):
   - `BOT_TOKEN`
   - `ADMIN_ID`
   - `EDUGATE_USERNAME`
   - `EDUGATE_PASSWORD`
   - `CHECK_INTERVAL` (minutes, default 60)
   - `MIN_CHECK_INTERVAL` (minutes, default 15)
   - `CHECK_JITTER` (seconds, default 5)
4. Deploy. Compose already mounts a volume at `/app/data` so `users.json` survives restarts.

If you use the Dockerfile resource instead of Compose, add a persistent storage mount to `/app/data`. The image defaults `USERS_FILE` to `/app/data/users.json`.

Local Docker:

```bash
docker compose up --build
```

## User Commands

| Command | Description |
|---------|-------------|
| `/start` | Subscribe this chat to alerts |
| `/check` | Check for changes now |
| `/sections` | View available sections |
| `/stats` | Your statistics |
| `/settings` | Your settings |
| `/interval [min]` | Set check interval (min 15) |
| `/help` | Show all commands |
| `/logout` | Remove your account |

## Admin Commands

| Command | Description |
|---------|-------------|
| `/admin` | Admin dashboard |
| `/users` | List all users |
| `/broadcast [msg]` | Send to all users |

## Files

- `bot.py` - Main bot
- `config.py` - Loads settings from `.env`
- `.env` - Bot token, admin ID, and Edugate login (not committed)
- `users.json` - Chat snapshots & stats only (auto-created, not committed)
- `Dockerfile` / `docker-compose.yml` - Coolify / local Docker
