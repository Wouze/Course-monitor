import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing {name}. Copy .env.example to .env and fill in your values."
        )
    return value


BOT_TOKEN = _require("BOT_TOKEN")
ADMIN_ID = int(_require("ADMIN_ID"))
EDUGATE_USERNAME = _require("EDUGATE_USERNAME")
EDUGATE_PASSWORD = _require("EDUGATE_PASSWORD")
EDUGATE_PROXY = os.getenv("EDUGATE_PROXY", "").strip()
USERS_FILE = os.getenv(
    "USERS_FILE", str(Path(__file__).resolve().parent / "users.json")
)
SESSION_FILE = os.getenv(
    "SESSION_FILE", str(Path(USERS_FILE).resolve().parent / "session.json")
)
MAX_WATCHES = max(1, int(os.getenv("MAX_WATCHES", "15")))

# Intervals in .env are minutes; jitter is seconds. bot.py stores seconds.
DEFAULT_CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60")) * 60
MIN_CHECK_INTERVAL = int(os.getenv("MIN_CHECK_INTERVAL", "15")) * 60
CHECK_JITTER = max(0, int(os.getenv("CHECK_JITTER", "5")))

if DEFAULT_CHECK_INTERVAL < MIN_CHECK_INTERVAL:
    raise RuntimeError(
        f"CHECK_INTERVAL must be at least MIN_CHECK_INTERVAL "
        f"({MIN_CHECK_INTERVAL // 60} minutes)."
    )
