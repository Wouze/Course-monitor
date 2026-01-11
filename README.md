# Section Monitor Bot

Multi-user Telegram bot that monitors KSU Edugate course sections and notifies when:
- 🆕 New sections become available
- ❌ Sections fill up

## Setup

```bash
pip install -r requirements.txt
python bot.py
```

Configure `config.py`:
```python
BOT_TOKEN = 'your_bot_token'
ADMIN_ID = 123456789  # Your Telegram chat ID
```

## User Commands

| Command | Description |
|---------|-------------|
| `/start` | Register with Edugate credentials |
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
- `config.py` - Bot token & admin ID
- `users.json` - User data (auto-created)
