import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ------------- Config -------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

HARDCODE_ADMINS = {7940894807, 5770074932}
ENV_ADMINS = {int(x) for x in os.getenv("ADMINS", "").replace(" ", "").split(",") if x}
ADMINS = HARDCODE_ADMINS | ENV_ADMINS

NOTIFIER_CHANNEL_ID = os.getenv("NOTIFIER_CHANNEL_ID")  # optional string

DB_PATH = "bot.db"
CURRENCY_XTR = "XTR"

# 💝 only
GIFT_NAME = "💝 Gift"
GIFT_PRICE = 15  # ⭐ per unit
PACKS: List[int] = [1, 2, 3, 5, 10, 20, 30, 50, 75, 100]  # grid packs

TOPUP_PRESETS = [50, 1000, 3000]
CUSTOM_MIN, CUSTOM_MAX = 15, 200_000

E = dict(
    bolt="⚡", ok="✅", bad="❌", gear="⚙️", gift="🎁",
    profile="🧑‍💼", deposit="⭐", logs="📄", health="🩺",
    back="◀️", rocket="🚀", recycle="♻️", wallet="👛",
    warn="🚨", lock="🔒"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("dream-bot")

# ------------- Simple state (no FSM lib needed) -------------
WAIT = {}  # dict[user_id] -> {"key": True}
def set_wait(uid: int, key: str): WAIT.setdefault(uid, {})[key] = True
def pop_wait(uid: int, key: str) -> bool: return bool(WAIT.get(uid, {}).pop(key, None))
def is_wait(uid: int, key: str) -> bool: return bool(WAIT.get(uid, {}).get(key))

# ------------- DB -------------
CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  internal_credit INTEGER NOT NULL DEFAULT 0,
  bot_stars INTEGER NOT NULL DEFAULT 0,
  auto_on INTEGER NOT NULL DEFAULT 0,
  notify_on INTEGER NOT NULL DEFAULT 1,
  cycles INTEGER NOT NULL DEFAULT 1,
  daily_budget INTEGER,
  spent_today INTEGER NOT NULL DEFAULT 0,
  last_spent_day INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  stars INTEGER NOT NULL,
  payload TEXT NOT NULL,
  charge_id TEXT,
  created_at INTEGER NOT NULL,
  ok INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  event TEXT NOT NULL,
  detail TEXT,
  created_at INTEGER NOT NULL
);
"""

async def open_db():
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(CREATE_SCHEMA)
    await db.commit()
    return db

@dataclass
class User:
    user_id: int
    internal_credit: int = 0
    bot_stars: int = 0
    auto_on: bool = False
    notify_on: bool = True
    cycles: int = 1
    daily_budget: Optional[int] = None
    spent_today: int = 0
    last_spent_day: int = 0  # yyyyMMdd

async def ensure_user(db, uid: int) -> User:
    cur = await db.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if not row:
        await db.execute("INSERT INTO users(user_id) VALUES(?)", (uid,))
        await db.commit()
        return User(user_id=uid)
    return User(
        user_id=row[0], internal_credit=row[1], bot_stars=row[2],
        auto_on=bool(row[3]), notify_on=bool(row[4]), cycles=row[5],
        daily_budget=row[6], spent_today=row[7], last_spent_day=row[8]
    )

async def save_user(db, u: User):
    await db.execute(
        """UPDATE users SET internal_credit=?, bot_stars=?, auto_on=?, notify_on=?,
           cycles=?, daily_budget=?, spent_today=?, last_spent_day=? WHERE user_id=?""",
        (u.internal_credit, u.bot_stars, int(u.auto_on), int(u.notify_on),
         u.cycles, u.daily_budget, u.spent_today, u.last_spent_day, u.user_id)
    )
    await db.commit()

async def add_log(db, user_id: Optional[int], event: str, detail: str = ""):
    await db.execute(
        "INSERT INTO logs(user_id, event, detail, created_at) VALUES(?,?,?,?)",
        (user_id, event, detail, int(time.time()))
    )
    await db.commit()

async def credit_of(db, uid: int) -> int:
    cur = await db.execute("SELECT internal_credit FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def adjust_credit(db, uid: int, delta: int):
    await db.execute("UPDATE users SET internal_credit = internal_credit + ? WHERE user_id=?", (delta, uid))
    await db.commit()

async def recent_logs(db, limit=20):
    cur = await db.execute("SELECT event, detail, created_at FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    return await cur.fetchall()

# ------------- Bot -------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

def is_admin(uid: int) -> bool: return uid in ADMINS

# ------------- Keyboards -------------
def main_menu_kb(auto: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"{E['bolt']} Auto-Buy: {'ON '+E['ok'] if auto else 'OFF '+E['bad']}", callback_data="auto:toggle"),
          InlineKeyboardButton(text=f"{E['health']} Health", callback_data="menu:health"))
    b.row(InlineKeyboardButton(text=f"{E['gear']} Auto-Purchase Settings", callback_data="menu:settings"))
    b.row(InlineKeyboardButton(text=f"{E['gift']} Gift Catalog", callback_data="menu:catalog:0"))
    b.row(InlineKeyboardButton(text=f"{E['profile']} Profile", callback_data="menu:profile"),
          InlineKeyboardButton(text=f"{E['deposit']} Deposit", callback_data="menu:deposit"))
    b.row(InlineKeyboardButton(text=f"{E['rocket']} Test Event", callback_data="menu:test"),
          InlineKeyboardButton(text=f"{E['logs']} Logs", callback_data="menu:logs"))
    return b.as_markup()

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{E['back']} Back", callback_data="menu:back")]])

def deposit_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for amt in TOPUP_PRESETS:
        b.button(text=f"Add {amt}⭐", callback_data=f"deposit:{amt}")
    b.button(text="Custom amount…", callback_data="deposit:custom")
    b.button(text="Back", callback_data="menu:back")
    b.adjust(3, 1, 1)
    return b.as_markup()

def catalog_text(page: int) -> str:
    return f"{E['gift']} Gift Catalog page {page+1}/{(len(PACKS)+5)//6} ({GIFT_NAME})"

def catalog_kb(page: int) -> InlineKeyboardMarkup:
    page = max(0, min(page, (len(PACKS)-1)//6))
    start = page * 6
    items = PACKS[start:start+6]
    b = InlineKeyboardBuilder()
    for cnt in items:
        total = cnt * GIFT_PRICE
        b.button(text=f"{cnt}× for {total}⭐", callback_data=f"buy-pack:{cnt}")
    b.adjust(2, 2, 2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"menu:catalog:{page-1}"))
    if start + 6 < len(PACKS):
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"menu:catalog:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="Back", callback_data="menu:back"))
    return b.as_markup()

def settings_kb(u) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Cycles", callback_data="set:cycles")
    b.button(text="Daily budget", callback_data="set:budget")
    b.button(text="Back", callback_data="menu:back")
    b.adjust(1, 1, 1)
    return b.as_markup()

def refunds_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Refund last purchase (credit)", callback_data="refund:last")
    b.button(text="Refund by TxID (credit)", callback_data="refund:tx")
    b.button(text="+ Add manual credit", callback_data="refund:add")
    b.button(text="Back", callback_data="menu:back")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()

def test_event_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Simulate drop (notify)", callback_data="test:notify")
    b.button(text="Back", callback_data="menu:back")
    b.adjust(1, 1)
    return b.as_markup()

# ------------- Helpers -------------
def today_key() -> int: return int(time.strftime("%Y%m%d"))

def within_budget(u: User, price: int) -> Tuple[bool, str]:
    d = today_key()
    if u.last_spent_day != d:
        u.last_spent_day, u.spent_today = d, 0
    if u.daily_budget is None: return True, ""
    if u.spent_today + price <= u.daily_budget: return True, ""
    return False, f"Daily budget exceeded ({u.spent_today}⭐/{u.daily_budget}⭐)."

async def send_menu(target: Message | CallbackQuery, u: User):
    kb = main_menu_kb(u.auto_on)
    if isinstance(target, Message):
        await target.answer("Menu", reply_markup=kb)
    else:
        await target.message.edit_text("Menu", reply_markup=kb)

async def send_gift_once(to_user_id: int, stars: int, reason: str) -> Tuple[bool, str]:
    payload = {"chat_id": to_user_id, "gift": json.dumps({"star_count": stars}), "text": f"{GIFT_NAME}\n{reason}"}
    try:
        res = await bot.request("sendGift", payload)
        return bool(res), "OK"
    except Exception as e:
        return False, str(e)

async def buy_units(db, u: User, units: int, mode: str) -> Tuple[int, str]:
    total = units * GIFT_PRICE
    bal = await credit_of(db, u.user_id)
    if bal < total:
        return 0, f"Not enough credit ({bal}⭐ < {total}⭐)."
    ok_budget, why = within_budget(u, total)
    if not ok_budget:
        return 0, why

    sent = 0
    # fire fast but not crazy to avoid rate limits
    for _ in range(units):
        ok, detail = await send_gift_once(u.user_id, GIFT_PRICE, mode)
        if not ok:
            await add_log(db, u.user_id, f"{mode}_FAIL", detail)
            return sent, f"Stopped: {detail}"
        sent += 1
        u.spent_today += GIFT_PRICE
        await asyncio.sleep(0.15)  # tiny spacing for reliability

    await adjust_credit(db, u.user_id, -total)
    await save_user(db, u)
    await add_log(db, u.user_id, f"{mode}_OK", f"{units}× {GIFT_PRICE}⭐ (total {total}⭐)")
    return sent, "OK"

def parse_price_from_text(s: str) -> Optional[int]:
    s = s.lower()
    # matches "for 50 stars", "— 15⭐", "15 star"
    m = re.search(r"(\d{1,6})\s*(stars|star|⭐)", s)
    return int(m.group(1)) if m else None

# ------------- Handlers -------------
@router.message(CommandStart())
async def start(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer(f"{E['lock']} Private bot.")
        return
    async with await open_db() as db:
        u = await ensure_user(db, m.from_user.id)
        await send_menu(m, u)

@router.callback_query(F.data == "menu:back")
async def cb_back(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Private bot.", show_alert=True)
        return
    async with await open_db() as db:
        u = await ensure_user(db, c.from_user.id)
        await send_menu(c, u)

@router.callback_query(F.data == "menu:profile")
async def cb_profile(c: CallbackQuery):
    async with await open_db() as db:
        u = await ensure_user(db, c.from_user.id)
        txt = (f"{E['profile']} Profile\n"
               f"{E['wallet']} Internal credit: {u.internal_credit}⭐\n"
               f"🤖 Bot Stars: {u.bot_stars}⭐\n\n"
               f"{E['recycle']} Auto-Purchase\n"
               f"— Cycles: {u.cycles}\n"
               f"— Daily budget: {('∞' if u.daily_budget is None else str(u.daily_budget))}⭐ "
               f"(spent today: {u.spent_today}⭐)\n"
               f"{E['bolt']} Auto: {'on' if u.auto_on else 'off'}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Refunds / Credits", callback_data="menu:refunds")],
            [InlineKeyboardButton(text="Back", callback_data="menu:back")]
        ])
        await c.message.edit_text(txt, reply_markup=kb)

@router.callback_query(F.data == "menu:deposit")
async def cb_deposit(c: CallbackQuery):
    await c.message.edit_text("Top up Stars", reply_markup=deposit_kb())

@router.callback_query(F.data.startswith("deposit:"))
async def cb_deposit_buttons(c: CallbackQuery):
    kind = c.data.split(":")[1]
    if kind == "custom":
        set_wait(c.from_user.id, "custom")
        await c.message.edit_text(
            f"Send the Stars amount (e.g., 3300). Min {CUSTOM_MIN}, Max {CUSTOM_MAX}.",
            reply_markup=back_kb()
        )
        return
    await create_invoice(c.message, c.from_user.id, int(kind))

async def create_invoice(msg: Message, uid: int, amount: int):
    try:
        await bot.send_invoice(
            chat_id=uid,
            title="Top up Stars",
            description=f"Add {amount}⭐ to bot balance",
            payload=f"topup:{amount}",
            currency=CURRENCY_XTR,
            prices=[LabeledPrice(label="Bot balance", amount=amount)],
        )
    except Exception as e:
        await msg.answer(f"{E['bad']} Invoice error: {e}")

@router.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)

@router.message(F.successful_payment)
async def on_payment(m: Message):
    sp = m.successful_payment
    stars = sp.total_amount
    async with await open_db() as db:
        await adjust_credit(db, m.from_user.id, stars)
        await db.execute(
            "INSERT INTO payments(user_id, stars, payload, charge_id, created_at, ok) VALUES(?,?,?,?,?,1)",
            (m.from_user.id, stars, sp.invoice_payload, sp.provider_payment_charge_id or "", int(time.time()))
        )
        await db.commit()
        await add_log(db, m.from_user.id, "TOPUP", f"+{stars}⭐ payload={sp.invoice_payload}")
        bal = await credit_of(db, m.from_user.id)
    await m.answer(f"Received {stars}⭐. Internal credit: {bal}⭐.")

@router.callback_query(F.data == "auto:toggle")
async def cb_auto_toggle(c: CallbackQuery):
    async with await open_db() as db:
        u = await ensure_user(db, c.from_user.id)
        u.auto_on = not u.auto_on
        await save_user(db, u)
        await add_log(db, u.user_id, "AUTO", "ON" if u.auto_on else "OFF")
        await c.message.edit_text("Menu", reply_markup=main_menu_kb(u.auto_on))

@router.callback_query(F.data == "menu:health")
async def cb_health(c: CallbackQuery):
    async with await open_db() as db:
        rows = await recent_logs(db, 6)
    lines = [f"{E['health']} Health", "DB: OK", "Threads: 1 (background)"]
    if NOTIFIER_CHANNEL_ID:
        lines.append(f"Notifier: listening to channel {NOTIFIER_CHANNEL_ID}")
    else:
        lines.append("Notifier: test only (no channel configured)")
    lines.append("\nRecent logs:")
    for e, d, _ in rows:
        lines.append(f"• {e} — {d}")
    await c.message.edit_text("\n".join(lines), reply_markup=back_kb())

@router.callback_query(F.data == "menu:logs")
async def cb_logs(c: CallbackQuery):
    async with await open_db() as db:
        rows = await recent_logs(db, 20)
    if not rows:
        await c.message.edit_text("No logs yet.", reply_markup=back_kb()); return
    text = f"{E['logs']} Logs (latest):\n" + "\n".join(f"• {e} — {d}" for e, d, _ in rows)
    await c.message.edit_text(text[:4000], reply_markup=back_kb())

@router.callback_query(F.data == "menu:settings")
async def cb_settings(c: CallbackQuery):
    async with await open_db() as db:
        u = await ensure_user(db, c.from_user.id)
    txt = (f"{E['gear']} Auto-Purchase Settings\n"
           f"Cycles: {u.cycles}\n"
           f"Daily budget: {('∞' if u.daily_budget is None else str(u.daily_budget))}⭐")
    await c.message.edit_text(txt, reply_markup=settings_kb(u))

@router.callback_query(F.data == "set:cycles")
async def cb_set_cycles(c: CallbackQuery):
    set_wait(c.from_user.id, "cycles")
    await c.message.edit_text("Send number of cycles (0=∞).", reply_markup=back_kb())

@router.callback_query(F.data == "set:budget")
async def cb_set_budget(c: CallbackQuery):
    set_wait(c.from_user.id, "budget")
    await c.message.edit_text("Send daily budget in ⭐ (or 'inf').", reply_markup=back_kb())

@router.callback_query(F.data == "menu:refunds")
async def cb_refunds(c: CallbackQuery):
    await c.message.edit_text("Refunds / Credits — internal only (Telegram Stars are final).", reply_markup=refunds_kb())

@router.callback_query(F.data == "refund:last")
async def cb_refund_last(c: CallbackQuery):
    async with await open_db() as db:
        cur = await db.execute("SELECT id, stars FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1", (c.from_user.id,))
        row = await cur.fetchone()
        if not row:
            await c.answer("Nothing to refund", show_alert=True); return
        pid, stars = row
        await adjust_credit(db, c.from_user.id, -stars)
        await db.execute("DELETE FROM payments WHERE id=?", (pid,))
        await db.commit()
        bal = await credit_of(db, c.from_user.id)
    await c.message.answer(f"Credited {stars}⭐ back. Internal credit: {bal}⭐.", reply_markup=back_kb())

@router.callback_query(F.data == "refund:tx")
async def cb_refund_tx(c: CallbackQuery):
    set_wait(c.from_user.id, "refundtx")
    await c.message.edit_text("Send TxID (from logs) to credit back.", reply_markup=back_kb())

@router.callback_query(F.data == "refund:add")
async def cb_refund_add(c: CallbackQuery):
    set_wait(c.from_user.id, "addcredit")
    await c.message.edit_text("Send amount to add to internal credit.", reply_markup=back_kb())

# One text handler to process all waits (prevents random numbers being treated as top-ups)
@router.message(F.text)
async def any_text(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return

    # custom top up (invoice)
    if is_wait(uid, "custom") and re.fullmatch(r"\d+", m.text or ""):
        pop_wait(uid, "custom")
        amount = int(m.text)
        if amount < CUSTOM_MIN or amount > CUSTOM_MAX:
            await m.answer(f"Amount must be between {CUSTOM_MIN} and {CUSTOM_MAX} Stars.", reply_markup=back_kb())
            return
        await create_invoice(m, uid, amount)
        return

    # cycles
    if is_wait(uid, "cycles") and re.fullmatch(r"\d+", m.text or ""):
        pop_wait(uid, "cycles")
        val = max(0, int(m.text))
        async with await open_db() as db:
            u = await ensure_user(db, uid)
            u.cycles = val if val != 0 else 500_000_000
            await save_user(db, u)
        await m.answer("Cycles updated.", reply_markup=back_kb()); return

    # budget
    if is_wait(uid, "budget"):
        pop_wait(uid, "budget")
        new_budget: Optional[int]
        if (m.text or "").lower().strip() in {"inf", "∞", "none"}:
            new_budget = None
        elif re.fullmatch(r"\d+", m.text or ""):
            new_budget = max(1, int(m.text))
        else:
            await m.answer("Send a number or 'inf'.", reply_markup=back_kb()); return
        async with await open_db() as db:
            u = await ensure_user(db, uid)
            u.daily_budget = new_budget
            await save_user(db, u)
        await m.answer("Budget updated.", reply_markup=back_kb()); return

    # refund by tx
    if is_wait(uid, "refundtx"):
        pop_wait(uid, "refundtx")
        txid_text = m.text.strip()
        if not re.fullmatch(r"\d+", txid_text):
            await m.answer("TxID must be a number.", reply_markup=back_kb()); return
        txid = int(txid_text)
        async with await open_db() as db:
            cur = await db.execute("SELECT id, stars FROM payments WHERE id=?", (txid,))
            row = await cur.fetchone()
            if not row:
                await m.answer("Tx not found.", reply_markup=back_kb()); return
            _, stars = row
            await adjust_credit(db, uid, -stars)
            await db.execute("DELETE FROM payments WHERE id=?", (txid,))
            await db.commit()
        await m.answer(f"Credited {stars}⭐ back.", reply_markup=back_kb()); return

    # add manual credit
    if is_wait(uid, "addcredit") and re.fullmatch(r"\d+", m.text or ""):
        pop_wait(uid, "addcredit")
        amt = max(0, int(m.text))
        async with await open_db() as db:
            await adjust_credit(db, uid, amt)
            bal = await credit_of(db, uid)
        await m.answer(f"Credit updated. Internal: {bal}⭐.", reply_markup=back_kb())
        return

@router.callback_query(F.data.startswith("menu:catalog:"))
async def cb_catalog_page(c: CallbackQuery):
    page = int(c.data.split(":")[2])
    await c.message.edit_text(catalog_text(page), reply_markup=catalog_kb(page))

@router.callback_query(F.data == "menu:catalog:0")
async def cb_catalog_root(c: CallbackQuery):
    await c.message.edit_text(catalog_text(0), reply_markup=catalog_kb(0))

@router.callback_query(F.data.startswith("buy-pack:"))
async def cb_buy_pack(c: CallbackQuery):
    units = int(c.data.split(":")[1])
    async with await open_db() as db:
        u = await ensure_user(db, c.from_user.id)
        sent, info = await buy_units(db, u, units, "MANUAL")
        if sent == 0:
            await c.answer(info, show_alert=True)
            return
        bal = await credit_of(db, u.user_id)
    await c.message.answer(f"{E['gift']} Sent {units}× {GIFT_PRICE}⭐ ({units*GIFT_PRICE}⭐). Balance: {bal}⭐.")

@router.callback_query(F.data == "menu:test")
async def cb_test(c: CallbackQuery):
    await c.message.edit_text("Test Event\n— Simulate a limited drop.", reply_markup=test_event_kb())

@router.callback_query(F.data == "test:notify")
async def cb_test_notify(c: CallbackQuery):
    await c.message.answer(f"{E['warn']} Limited gift detected (test) — will try to buy if Auto-Buy is ON.")
    async with await open_db() as db:
        u = await ensure_user(db, c.from_user.id)
        if u.auto_on:
            # try to burn cycles (up to balance/budget)
            max_units = min(u.cycles, (await credit_of(db, u.user_id)) // GIFT_PRICE)
            if max_units > 0:
                sent, _ = await buy_units(db, u, max_units, "AUTO")
                if sent == 0:
                    await c.answer("Auto-Buy failed (see logs).", show_alert=True)

# ---- Real notifier: auto-runs when the channel posts about a new limited gift ----
@router.channel_post()
async def on_channel_post(msg: Message):
    if not NOTIFIER_CHANNEL_ID:  # disabled
        return
    if str(msg.chat.id) != str(NOTIFIER_CHANNEL_ID):
        return
    text = (msg.text or msg.caption or "")
    price = parse_price_from_text(text) or GIFT_PRICE
    # broadcast to admins who enabled auto
    async with await open_db() as db:
        for aid in ADMINS:
            u = await ensure_user(db, aid)
            if not u.auto_on:
                continue
            max_units = min(u.cycles, (await credit_of(db, u.user_id)) // price)
            if max_units <= 0:
                await add_log(db, u.user_id, "AUTO_SKIP", "insufficient balance"); continue
            # If the limited gift has a different price (e.g., 50/100), adapt to that price.
            global GIFT_PRICE
            old_price = GIFT_PRICE
            try:
                GIFT_PRICE = price
                sent, info = await buy_units(db, u, max_units, "AUTO")
                if sent == 0:
                    await add_log(db, u.user_id, "AUTO_FAIL", info)
            finally:
                GIFT_PRICE = old_price

# ------------- Background -------------
async def background_task():
    while True:
        await asyncio.sleep(60)

# ------------- Run -------------
async def main():
    async with await open_db() as db:
        pass
    asyncio.create_task(background_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
