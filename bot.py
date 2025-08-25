import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional, List, Set, Dict, Any

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.types import LabeledPrice, ForceReply, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
RECIPIENT_DEFAULT = os.getenv("RECIPIENT", "")
PRICE_MIN_DEFAULT = int(os.getenv("PRICE_MIN", "100"))
PRICE_MAX_DEFAULT = int(os.getenv("PRICE_MAX", "1000"))
SCAN_INTERVAL_MS = int(os.getenv("SCAN_INTERVAL_MS", "300"))
DB_PATH = "giftbot.sqlite3"

if not BOT_TOKEN or not ADMIN_IDS or not RECIPIENT_DEFAULT:
    raise SystemExit("Configure BOT_TOKEN, ADMIN_IDS, RECIPIENT in .env")

# ---------------- State ----------------
@dataclass
class GiftInfo:
    id: str
    star_count: int
    total_count: Optional[int]
    remaining_count: Optional[int]
    upgrade_star_count: Optional[int]

class Store:
    def __init__(self):
        self.last_seen_ids: Set[str] = set()
        self.recipient: str = RECIPIENT_DEFAULT
        self.min_price: int = PRICE_MIN_DEFAULT
        self.max_price: int = PRICE_MAX_DEFAULT
        self.autobuy: bool = True
        self.multi_buy: bool = True
        self.blocked_ids: Set[str] = set()
        self.admins: Set[int] = ADMIN_IDS
        self.panel_msg_id: Optional[int] = None
        self.panel_chat_id: Optional[int] = None
        self.waiting: Dict[int, str] = {}  # user_id -> "set_recipient" | "refund"

store = Store()
router = Router()

# ---------------- DB & Persistence ----------------
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER, charge_id TEXT, amount INTEGER, ts INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS purchases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          gift_id TEXT, star_count INTEGER, recipient TEXT, result_ok INTEGER, ts INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY, val TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS blocked(
          gift_id TEXT PRIMARY KEY
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS last_seen(
          gift_id TEXT PRIMARY KEY, ts INTEGER
        )""")
        await db.commit()

async def settings_set(key: str, val: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,val) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
            (key, val)
        )
        await db.commit()

async def settings_get(key: str, default: Optional[str] = None) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT val FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

async def db_add_payment(user_id: int, charge_id: str, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments(user_id, charge_id, amount, ts) VALUES(?,?,?,?)",
            (user_id, charge_id, amount, int(time.time()))
        )
        await db.commit()

async def db_add_purchase(gift_id: str, star: int, recipient: str, ok: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO purchases(gift_id, star_count, recipient, result_ok, ts) VALUES(?,?,?,?,?)",
            (gift_id, star, recipient, 1 if ok else 0, int(time.time()))
        )
        await db.commit()

async def blocked_load():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT gift_id FROM blocked") as cur:
            rows = await cur.fetchall()
            store.blocked_ids = {r[0] for r in rows}

async def last_seen_load():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT gift_id FROM last_seen") as cur:
            rows = await cur.fetchall()
            store.last_seen_ids = {r[0] for r in rows}

async def last_seen_add(gift_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO last_seen(gift_id, ts) VALUES(?,?)",
            (gift_id, int(time.time()))
        )
        await db.execute(
            "DELETE FROM last_seen WHERE gift_id NOT IN "
            "(SELECT gift_id FROM last_seen ORDER BY ts DESC LIMIT 2000)"
        )
        await db.commit()

async def persist_current_settings():
    await settings_set("recipient", store.recipient)
    await settings_set("min_price", str(store.min_price))
    await settings_set("max_price", str(store.max_price))
    await settings_set("autobuy", "1" if store.autobuy else "0")
    await settings_set("multi_buy", "1" if store.multi_buy else "0")

async def load_persistent_state():
    await db_init()
    v = await settings_get("recipient");        store.recipient  = v if v else store.recipient
    v = await settings_get("min_price");        store.min_price  = int(v) if v else store.min_price
    v = await settings_get("max_price");        store.max_price  = int(v) if v else store.max_price
    v = await settings_get("autobuy");          store.autobuy    = (v == "1") if v else store.autobuy
    v = await settings_get("multi_buy");        store.multi_buy  = (v == "1") if v else store.multi_buy
    await blocked_load()
    await last_seen_load()

# ---------------- Helpers ----------------
def is_admin(uid: int) -> bool:
    return uid in store.admins

def is_limited(g: GiftInfo) -> bool:
    return g.total_count is not None and (g.remaining_count or 0) > 0

def in_band(g: GiftInfo) -> bool:
    return store.min_price <= g.star_count <= store.max_price

def panel_text(balance: int) -> str:
    return (
        "<b>Auto Gift Buyer</b>\n"
        f"Recipient: <code>{store.recipient}</code>\n"
        f"Band: <b>{store.min_price}→{store.max_price}⭐</b>\n"
        f"Auto-buy: {'🟢 ON' if store.autobuy else '🔴 OFF'} | Multi-buy: {'YES' if store.multi_buy else 'NO'}\n"
        f"Scan: {SCAN_INTERVAL_MS} ms\n"
        f"Bot Stars: <b>{balance}⭐</b>"
    )

def panel_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Auto-buy ON/OFF", callback_data="p:toggle")
    kb.button(text="–50 min", callback_data="p:min:-50")
    kb.button(text="+50 min", callback_data="p:min:+50")
    kb.button(text="–50 max", callback_data="p:max:-50")
    kb.button(text="+50 max", callback_data="p:max:+50")
    kb.button(text="Multi-buy ON/OFF", callback_data="p:multi")
    kb.button(text="Set Recipient", callback_data="p:set_recipient")
    kb.button(text="Balance ⟳", callback_data="p:balance")
    kb.button(text="Deposit 100⭐", callback_data="p:dep:100")
    kb.button(text="Deposit 500⭐", callback_data="p:dep:500")
    kb.button(text="Deposit 1000⭐", callback_data="p:dep:1000")
    kb.button(text="Refund…", callback_data="p:refund")
    kb.adjust(3, 4, 2, 3)
    return kb.as_markup()

# ---------------- API wrappers (with safe fallback) ----------------
async def api_call(bot: Bot, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
    params = params or {}
    return await bot.session.api.call(method, params)

async def fetch_available_gifts(bot: Bot) -> List[GiftInfo]:
    try:
        data = await bot.get_available_gifts()  # aiogram typed
        gifts_obj = getattr(data, "gifts", [])
        result: List[GiftInfo] = []
        for g in gifts_obj:
            result.append(GiftInfo(
                id=str(g.id),
                star_count=int(g.star_count),
                total_count=int(g.total_count) if getattr(g, "total_count", None) is not None else None,
                remaining_count=int(g.remaining_count) if getattr(g, "remaining_count", None) is not None else None,
                upgrade_star_count=int(g.upgrade_star_count) if getattr(g, "upgrade_star_count", None) is not None else None,
            ))
        return result
    except AttributeError:
        raw = await api_call(bot, "getAvailableGifts")
        gifts = raw.get("gifts", [])
        result: List[GiftInfo] = []
        for g in gifts:
            result.append(GiftInfo(
                id=str(g.get("id")),
                star_count=int(g.get("star_count", 0)),
                total_count=g.get("total_count"),
                remaining_count=g.get("remaining_count"),
                upgrade_star_count=g.get("upgrade_star_count"),
            ))
        return result

async def get_balance(bot: Bot) -> int:
    try:
        amt = await bot.get_my_star_balance()
        return int(getattr(amt, "amount", 0))
    except AttributeError:
        raw = await api_call(bot, "getMyStarBalance")
        return int(raw.get("amount", 0))

async def refund_stars(bot: Bot, user_id: int, charge_id: str) -> bool:
    try:
        return bool(await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id))
    except AttributeError:
        raw = await api_call(bot, "refundStarPayment", {
            "user_id": user_id,
            "telegram_payment_charge_id": charge_id
        })
        return bool(raw)

async def send_gift(bot: Bot, gift: GiftInfo, recipient: str, text: str = "") -> bool:
    try:
        # Prefer numeric user_id
        uid = int(recipient)
        try:
            ok = await bot.send_gift(user_id=uid, gift_id=gift.id, text=text)
            return bool(ok)
        except AttributeError:
            raw = await api_call(bot, "sendGift", {"user_id": uid, "gift_id": gift.id, "text": text})
            return bool(raw)
    except ValueError:
        # Fallback: channel username like @your_channel
        try:
            ok = await bot.send_gift(chat_id=recipient, gift_id=gift.id, text=text)
            return bool(ok)
        except AttributeError:
            raw = await api_call(bot, "sendGift", {"chat_id": recipient, "gift_id": gift.id, "text": text})
            return bool(raw)

# ---------------- Sniper loop ----------------
async def sniper_loop(bot: Bot):
    await load_persistent_state()
    while True:
        try:
            gifts = await fetch_available_gifts(bot)

            # detect newly appeared ids
            appeared = [g for g in gifts if g.id not in store.last_seen_ids]
            for g in appeared:
                await last_seen_add(g.id)
                store.last_seen_ids.add(g.id)
                if is_limited(g) and in_band(g):
                    try:
                        await bot.send_message(list(store.admins)[0],
                                               f"🆕 Limited gift: {g.star_count}⭐ (id={g.id})")
                    except Exception:
                        pass

            if not store.autobuy:
                await asyncio.sleep(SCAN_INTERVAL_MS / 1000)
                continue

            # candidates: limited + band
            candidates = [g for g in gifts if is_limited(g) and in_band(g)]
            candidates.sort(key=lambda x: (x.star_count, (x.remaining_count or 10**9)))

            bal = await get_balance(bot)
            for g in candidates:
                if g.id in store.blocked_ids:
                    continue
                if g.star_count > bal:
                    continue
                ok = await send_gift(bot, g, store.recipient, text=f"Auto-bought {g.star_count}⭐")
                await db_add_purchase(g.id, g.star_count, store.recipient, ok)
                if ok:
                    bal -= g.star_count
                    if not store.multi_buy:
                        break

            await asyncio.sleep(SCAN_INTERVAL_MS / 1000)
        except Exception:
            # stay alive on transient errors
            await asyncio.sleep(0.7)

# ---------------- Commands ----------------
@router.message(Command("start"))
async def on_start(m: types.Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return await m.answer("Access denied.")
    await persist_current_settings()
    bal = await get_balance(bot)
    await m.answer("Bot online ✅")
    msg = await m.answer(panel_text(bal), reply_markup=panel_kb())
    store.panel_chat_id, store.panel_msg_id = m.chat.id, msg.message_id

@router.message(Command("panel"))
async def on_panel(m: types.Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    bal = await get_balance(bot)
    msg = await m.answer(panel_text(bal), reply_markup=panel_kb())
    store.panel_chat_id, store.panel_msg_id = m.chat.id, msg.message_id

@router.message(Command("band")))
async def on_band(m: types.Message):
    # FIX: ensure syntax correct
    pass
