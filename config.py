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

# Default check interval (in seconds) - users can customize (min 15 min)
DEFAULT_CHECK_INTERVAL = 60 * 60  # 1 hour
MIN_CHECK_INTERVAL = 15 * 60  # 15 minutes minimum
