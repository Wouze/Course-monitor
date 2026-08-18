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
```

Edugate credentials stay in `.env` only. The bot never asks for them in Telegram.

Then start the bot:
```bash
python bot.py
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
