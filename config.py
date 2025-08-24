import os
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

DB_PATH = os.getenv("DB_PATH", "starsbot.db")

# Poll speeds (seconds)
INTERVAL_NORMAL = float(os.getenv("INTERVAL_NORMAL", "10"))
INTERVAL_FAST   = float(os.getenv("INTERVAL_FAST", "3"))
INTERVAL_INSANE = float(os.getenv("INTERVAL_INSANE", "1"))

# Limits & rules
DEFAULT_MAX_PRICE  = int(os.getenv("DEFAULT_MAX_PRICE", "0"))  # 0 = no cap
DEFAULT_ALLOW_IDS  = [x for x in os.getenv("DEFAULT_ALLOW_IDS", "").split(",") if x.strip()]
DEFAULT_ALLOW_KEYS = [x for x in os.getenv("DEFAULT_ALLOW_KEYS","teddy,ring").split(",") if x.strip()]
DEFAULT_RECIPS     = [int(x) for x in os.getenv("DEFAULT_RECIPS", "").split(",") if x.strip()]
BUY_COOLDOWN_SEC   = int(os.getenv("BUY_COOLDOWN_SEC", "600"))  # per-gift cooldown (seconds)
DEFAULT_WINDOW     = os.getenv("DEFAULT_WINDOW", "")            # "HH:MM-HH:MM" (local)
DEFAULT_NOTIFY_ONLY = os.getenv("DEFAULT_NOTIFY_ONLY", "false").lower() in ("1","true","yes")

# Feed integration (optionally use tg_gifts_notifier JSON)
FEED_MODE         = os.getenv("FEED_MODE", "api").lower()          # "api" or "notifier"
NOTIFIER_JSON     = os.getenv("NOTIFIER_JSON", "")                 # path to gifts.json
NOTIFIER_POLL_SEC = float(os.getenv("NOTIFIER_POLL_SEC", "1.0"))
