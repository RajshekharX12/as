import os
from dotenv import load_dotenv
load_dotenv()

# Required
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Optional: comma-separated (else first /start user becomes owner)
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

DB_PATH = os.getenv("DB_PATH", "starsbot.db")

# Poll speeds (seconds)
INTERVAL_NORMAL = 10.0
INTERVAL_FAST   = 3.0
INTERVAL_INSANE = 1.0

# Defaults (runtime settings can change via inline buttons)
DEFAULT_MIN_PRICE   = 0        # Lower limit ⭐ (0 = no min)
DEFAULT_MAX_PRICE   = 0        # Upper limit ⭐ (0 = no max)
DEFAULT_ALLOW_IDS   = []       # allowed Gift IDs (empty = any)
DEFAULT_ALLOW_KEYS  = ["teddy","ring"]  # title contains any (empty = any)
DEFAULT_RECIPS      = []       # recipients; will default to owner
DEFAULT_WINDOW      = ""       # "HH:MM-HH:MM" local; empty = always
BUY_COOLDOWN_SEC    = 600      # per-gift cooldown (anti-spam)

DEFAULT_NOTIFICATIONS = True   # notifications on/off
DEFAULT_CYCLES         = 500   # poll cycles per run (0 = infinite)
DEFAULT_OVERALL_LIMIT  = 0     # max stars to spend this run (0 = unlimited)
DEFAULT_SUPPLY_LIMIT   = 0     # max pieces to buy this run (0 = unlimited)

# Optional notifier feed
FEED_MODE         = os.getenv("FEED_MODE", "api").lower()  # "api" or "notifier"
NOTIFIER_JSON     = os.getenv("NOTIFIER_JSON", "")         # path to gifts.json
NOTIFIER_POLL_SEC = float(os.getenv("NOTIFIER_POLL_SEC", "1.0"))
