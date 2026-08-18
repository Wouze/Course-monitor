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
MAX_WATCHES=15
EDUGATE_PROXY=
```

Edugate credentials stay in `.env` only. The bot never asks for them in Telegram.

`CHECK_INTERVAL` and `MIN_CHECK_INTERVAL` are in minutes. `CHECK_JITTER` is seconds — each cycle waits interval ± jitter (e.g. 3 minutes ± 5 seconds).

Then start the bot:
```bash
python bot.py
```

## Coolify / Docker

Edugate **resets Docker-bridge connections** (`curl: (56) Connection reset by peer`), even on a home PC. This machine can open the login page from the host; the Coolify container cannot. Coolify also usually ignores `network_mode: host`.

**Recommended:** run the bot on the host, not in Coolify.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

Or copy `scripts/course-monitor.service` to systemd and enable it.

If you keep the bot in Coolify, start a host-side tunnel (stdlib only):

```bash
python3 edugate_proxy.py
```

The bot then tries `http://172.17.0.1:18080`. Use `scripts/edugate-proxy.service` to keep that running.

This is a worker (Telegram polling), not an HTTP app. Do not assign a public domain or port. Persist `/app/data` if you still deploy the image.

## User Commands

| Command | Description |
|---------|-------------|
| `/start` | Subscribe this chat to alerts |
| `/check` | Check for changes now |
| `/sections` | View available sections |
| `/stats` | Your statistics |
| `/settings` | Your settings |
| `/interval [min]` | Set check interval (min 15) |
| `/watch [id]` | Watch a section ID (official lookup) |
| `/unwatch [id]` | Stop watching |
| `/watches` | List watched sections |
| `/course [code]` | Watch every section of a course (e.g. `339`) |
| `/uncourse [code]` | Stop watching a course |
| `/courses` | List watched courses |
| `/sections [code]` | List catalog sections, optionally for one course |
| `/help` | Show all commands |
| `/logout` | Remove your account |

## Admin Commands

| Command | Description |
|---------|-------------|
| `/admin` | Admin dashboard |
| `/users` | List all users |
| `/broadcast [msg]` | Send to all users |

## Files

- `bot.py` - Telegram commands
- `edugate.py` - Session reuse, catalog parse, section lookup
- `config.py` - Loads settings from `.env`
- `.env` - Bot token, admin ID, and Edugate login (not committed)
- `users.json` / `session.json` - Chat snapshots and Edugate cookies (not committed)
- `Dockerfile` / `docker-compose.yml` - Coolify / local Docker
