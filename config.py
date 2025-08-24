import os
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

DB_PATH = os.getenv("DB_PATH", "starsbot.db")

INTERVAL_NORMAL = 10.0
INTERVAL_FAST   = 3.0
INTERVAL_INSANE = 1.0

DEFAULT_MIN_PRICE   = 0
DEFAULT_MAX_PRICE   = 0
DEFAULT_ALLOW_IDS   = []
DEFAULT_ALLOW_KEYS  = ["teddy","ring"]
DEFAULT_RECIPS      = []
DEFAULT_WINDOW      = ""
BUY_COOLDOWN_SEC    = 600

DEFAULT_NOTIFICATIONS = True
DEFAULT_CYCLES         = 500
DEFAULT_OVERALL_LIMIT  = 0
DEFAULT_SUPPLY_LIMIT   = 0

FEED_MODE         = os.getenv("FEED_MODE", "api").lower()
NOTIFIER_JSON     = os.getenv("NOTIFIER_JSON", "")
NOTIFIER_POLL_SEC = float(os.getenv("NOTIFIER_POLL_SEC", "1.0"))
