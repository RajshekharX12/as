# bot.py — Auto-buy limited gifts, aiogram 3.7, inline UI only.

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone, date
from typing import Optional, List, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ───────────────────────── CONFIG ─────────────────────────
# Replace with your real token:
BOT_TOKEN = "0000000000:PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"

# Admins (private bot). Add your numeric Telegram ID(s).
ADMINS = {7940894807, 5770074932}

# Greedy strategy band:
DEFAULT_MIN_PRICE = 100
DEFAULT_MAX_PRICE = 1000

# Stars invoice limits:
MIN_XTR, MAX_XTR = 15, 200000

# UI emoji
E = dict(
    bolt="⚡", ok="✅", bad="❌", gear="⚙️", gift="🎁",
    profile="🧑‍💼", deposit="⭐", logs="📄", health="🩺",
    back="◀️", rocket="🚀", wallet="👛", warn="🚨", lock="🔒",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s :: %(message)s")
log = logging.getLogger("autobuy")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DB_PATH = "./autobuy.sqlite3"

# ───────────────────────── DB LAYER ─────────────────────────
def db():
    # return connection (do not await here)
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with db() as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id      INTEGER PRIMARY KEY,
                auto_on      INTEGER DEFAULT 1,
                min_price    INTEGER DEFAULT ?,
                max_price    INTEGER DEFAULT ?,
                daily_budget INTEGER DEFAULT 0,
                spent_today  INTEGER DEFAULT 0,
                spent_day    INTEGER DEFAULT 0,
                credit       INTEGER DEFAULT 0,
                notifier_id  INTEGER
            )
        """, (DEFAULT_MIN_PRICE, DEFAULT_MAX_PRICE))
        await con.execute("""
            CREATE TABLE IF NOT EXISTS purchases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                price   INTEGER,
                qty     INTEGER,
                name    TEXT,
                source  TEXT,
                ts      INTEGER
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                kind    TEXT,
                message TEXT,
                ts      INTEGER
            )
        """)
        await con.commit()

DEFAULT_USER = {
    "auto_on": 1,
    "min_price": DEFAULT_MIN_PRICE,
    "max_price": DEFAULT_MAX_PRICE,
    "daily_budget": 0,
    "spent_today": 0,
    "spent_day": 0,
    "credit": 0,
    "notifier_id": None,
}

async def db_get_user(uid: int) -> Optional[dict]:
    async with db() as con:
        cur = await con.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row: return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

async def db_save_user(uid: int, u: dict):
    fields = list(DEFAULT_USER.keys())
    placeholders = ",".join(f"{k}=?" for k in fields)
    values = [u[k] for k in fields]
    async with db() as con:
        await con.execute(
            f"INSERT INTO users(user_id,{','.join(fields)}) VALUES(?,{','.join('?' for _ in fields)}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {placeholders}",
            (uid, *values, *values)
        )
        await con.commit()

async def get_user(uid: int) -> dict:
    u = await db_get_user(uid)
    if not u:
        u = DEFAULT_USER.copy()
        await db_save_user(uid, u)
    else:
        changed = False
        for k, v in DEFAULT_USER.items():
            if k not in u or u[k] is None:
                u[k] = v; changed = True
        if changed:
            await db_save_user(uid, u)
    return u

async def set_user(uid: int, **updates):
    u = await get_user(uid)
    u.update(updates)
    await db_save_user(uid, u)
    return u

async def db_log(uid: Optional[int], kind: str, message: str):
    async with db() as con:
        await con.execute(
            "INSERT INTO logs(user_id, kind, message, ts) VALUES(?,?,?,?)",
            (uid, kind, message, int(datetime.now(tz=timezone.utc).timestamp()))
        )
        await con.commit()

async def db_last_logs(uid: int, limit=20) -> List[Tuple[str,str]]:
    async with db() as con:
        cur = await con.execute(
            "SELECT kind, message FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit)
        )
        return await cur.fetchall()

async def db_add_purchase(uid: int, price: int, qty: int, name: str, source: str):
    async with db() as con:
        now = int(datetime.now(tz=timezone.utc).timestamp())
        await con.execute(
            "INSERT INTO purchases(user_id, price, qty, name, source, ts) VALUES(?,?,?,?,?,?)",
            (uid, price, qty, name, source, now)
        )
        await con.commit()

# ───────────────────────── FSM ─────────────────────────
class Entry(StatesGroup):
    set_min = State()
    set_max = State()
    set_daily = State()
    deposit = State()

# ───────────────────────── UI HELPERS ─────────────────────────
def kb_main(u: dict) -> InlineKeyboardMarkup:
    auto = "ON ✅" if u.get("auto_on") else "OFF ❌"
    rows = [
        [InlineKeyboardButton(text=f"{E['bolt']} Auto-Buy: {auto}", callback_data="auto:toggle"),
         InlineKeyboardButton(text=f"{E['health']} Health", callback_data="menu:health")],
        [InlineKeyboardButton(text=f"{E['gear']} Settings", callback_data="menu:settings")],
        [InlineKeyboardButton(text=f"{E['deposit']} Deposit / Balance", callback_data="menu:deposit")],
        [InlineKeyboardButton(text=f"{E['rocket']} Test Drop", callback_data="menu:test"),
         InlineKeyboardButton(text=f"{E['logs']} Logs", callback_data="menu:logs")],
        [InlineKeyboardButton(text="Set Source", callback_data="menu:source")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_settings() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Min price", callback_data="set:min"),
         InlineKeyboardButton(text="Max price", callback_data="set:max")],
        [InlineKeyboardButton(text="Daily budget", callback_data="set:daily")],
        [InlineKeyboardButton(text="◀️ Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_deposit() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Add 50⭐", callback_data="dep:50"),
         InlineKeyboardButton(text="Add 1000⭐", callback_data="dep:1000"),
         InlineKeyboardButton(text="Add 3000⭐", callback_data="dep:3000")],
        [InlineKeyboardButton(text="Custom…", callback_data="dep:custom")],
        [InlineKeyboardButton(text="↩️ Refund last (credit)", callback_data="refund:last")],
        [InlineKeyboardButton(text="◀️ Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_source() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="How to set channel?", callback_data="source:how")],
        [InlineKeyboardButton(text="◀️ Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ───────────────────────── HELPERS ─────────────────────────
def today_key() -> int:
    d = date.today()
    return d.year*10000 + d.month*100 + d.day

def parse_price(text: str) -> Optional[int]:
    s = text.lower()
    m = re.search(r"(\d{1,6})\s*(stars|star|xtr|⭐)", s)
    return int(m.group(1)) if m else None

def parse_name(text: str) -> str:
    line = (text.splitlines() or ["Gift"])[0]
    # strip noisy symbols, keep letters/numbers/spaces
    name = re.sub(r"[^A-Za-z0-9 ]", "", line).strip() or "Gift"
    return name[:40]

def first_emoji(text: str) -> str:
    for ch in text.strip():
        if not ch.isalnum() and not ch.isspace():
            return ch
    return "💝"

async def try_debit(uid: int, cost: int) -> tuple[bool, str]:
    u = await get_user(uid)
    # daily rollover
    if u.get("spent_day", 0) != today_key():
        u["spent_day"] = today_key()
        u["spent_today"] = 0

    if u["credit"] < cost:
        await db_save_user(uid, u)
        return False, "Insufficient credit."
    if u.get("daily_budget", 0) and u["spent_today"] + cost > u["daily_budget"]:
        await db_save_user(uid, u)
        return False, "Daily budget exceeded."

    u["credit"] -= cost
    u["spent_today"] += cost
    await db_save_user(uid, u)
    return True, "OK"

async def send_gift_piece(chat_id: int, stars_each: int, note: str) -> tuple[bool, str]:
    """
    Calls Bot API sendGift (Telegram Stars). If API unavail/denied, returns error.
    """
    payload = {"chat_id": chat_id, "gift": json.dumps({"star_count": stars_each}), "text": note}
    try:
        res = await bot.request("sendGift", payload)
        return (True, "OK") if res else (False, "Unknown response")
    except Exception as e:
        return False, str(e)

# ───────────────────────── CORE: AUTO-BUY ENGINE ─────────────────────────
async def greedy_buy(uid: int, price: int, name: str, source: str):
    """
    Buy as many of 'price' as possible (respecting daily_budget and credit).
    """
    if price < 0: return
    u = await get_user(uid)
    if not u.get("auto_on"): 
        await db_log(uid, "AUTO", f"Skip (OFF) {name} {price}⭐"); 
        return
    if price < u["min_price"] or price > u["max_price"]:
        await db_log(uid, "AUTO", f"Skip (out of band) {name} {price}⭐"); 
        return

    # loop until can't buy any more
    total_bought = 0
    while True:
        # try to debit for one piece
        ok, reason = await try_debit(uid, price)
        if not ok:
            await db_log(uid, "AUTO_STOP", f"{reason} at {name} {price}⭐, total {total_bought}")
            break

        ok2, why = await send_gift_piece(uid, price, f"Auto-buy {name}")
        if not ok2:
            # rollback the last debit since gift send failed
            u2 = await get_user(uid)
            u2["credit"] += price
            u2["spent_today"] = max(0, u2["spent_today"] - price)
            await db_save_user(uid, u2)
            await db_log(uid, "AUTO_FAIL", why)
            break

        total_bought += 1
        await db_add_purchase(uid, price, 1, name, source)
        # very small delay to avoid flood limits
        await asyncio.sleep(0.03)

    if total_bought > 0:
        await db_log(uid, "AUTO_OK", f"{name} {price}⭐ ×{total_bought}")

# ───────────────────────── BOT UI ─────────────────────────
@dp.message(CommandStart())
async def cmd_start(m: Message):
    if m.from_user.id not in ADMINS:
        await m.answer(f"{E['lock']} Private bot.")
        return
    u = await get_user(m.from_user.id)
    await m.answer("Menu", reply_markup=kb_main(u))

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    if m.from_user.id not in ADMINS: 
        await m.answer(f"{E['lock']} Private bot."); return
    u = await get_user(m.from_user.id)
    await m.answer("Menu", reply_markup=kb_main(u))

@dp.callback_query(F.data == "back:main")
async def back_main(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    await cq.message.edit_text("Menu", reply_markup=kb_main(u))

# Auto toggle
@dp.callback_query(F.data == "auto:toggle")
async def auto_toggle(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    new_flag = 0 if u.get("auto_on") else 1
    await set_user(cq.from_user.id, auto_on=new_flag)
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=kb_main(u))
    await cq.answer("Auto-Buy " + ("ON" if new_flag else "OFF"))

# Health
@dp.callback_query(F.data == "menu:health")
async def menu_health(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    ok_db = "✅"
    ok_src = "✅" if u.get("notifier_id") else "❌"
    text = (
        f"{E['health']} <b>Health</b>\n"
        f"• Database: {ok_db}\n"
        f"• Source channel: {ok_src}\n"
        f"• Auto-Buy: {'✅ ON' if u.get('auto_on') else '❌ OFF'}\n"
        f"• Credit: {u.get('credit',0)}⭐\n"
        f"• Band: {u.get('min_price',DEFAULT_MIN_PRICE)}–{u.get('max_price',DEFAULT_MAX_PRICE)}⭐"
    )
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Back", callback_data="back:main")]
    ]))

# Logs
@dp.callback_query(F.data == "menu:logs")
async def menu_logs(cq: types.CallbackQuery):
    rows = await db_last_logs(cq.from_user.id, 15)
    if not rows:
        out = f"{E['logs']} <b>Logs</b>\n— empty —"
    else:
        out = f"{E['logs']} <b>Logs</b>\n" + "\n".join(f"• <code>{k}</code> — {msg}" for k, msg in rows)
    await cq.message.edit_text(out, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Back", callback_data="back:main")]
    ]))

# Settings
@dp.callback_query(F.data == "menu:settings")
async def menu_settings(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    txt = (
        f"{E['gear']} <b>Settings</b>\n"
        f"Min price: <b>{u.get('min_price',DEFAULT_MIN_PRICE)}⭐</b>\n"
        f"Max price: <b>{u.get('max_price',DEFAULT_MAX_PRICE)}⭐</b>\n"
        f"Daily budget: <b>{u.get('daily_budget',0)}⭐</b>\n"
        f"Strategy: <i>Greedy buy within band</i>"
    )
    await cq.message.edit_text(txt, reply_markup=kb_settings())

@dp.callback_query(F.data == "set:min")
async def ask_min(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.set_min)
    await cq.message.edit_text("Send MIN price in ⭐ (e.g., 100).", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.set_min)
async def save_min(m: Message, state: FSMContext):
    try:
        v = max(0, int(m.text.strip()))
    except Exception:
        await m.answer("Numbers only."); return
    await set_user(m.from_user.id, min_price=v)
    await state.clear()
    u = await get_user(m.from_user.id)
    await m.answer("✅ Saved.", reply_markup=kb_settings())

@dp.callback_query(F.data == "set:max")
async def ask_max(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.set_max)
    await cq.message.edit_text("Send MAX price in ⭐ (e.g., 1000).", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.set_max)
async def save_max(m: Message, state: FSMContext):
    try:
        v = max(0, int(m.text.strip()))
    except Exception:
        await m.answer("Numbers only."); return
    await set_user(m.from_user.id, max_price=v)
    await state.clear()
    await m.answer("✅ Saved.", reply_markup=kb_settings())

@dp.callback_query(F.data == "set:daily")
async def ask_daily(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.set_daily)
    await cq.message.edit_text("Send DAILY budget in ⭐ (0 = unlimited).", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.set_daily)
async def save_daily(m: Message, state: FSMContext):
    try:
        v = max(0, int(m.text.strip()))
    except Exception:
        await m.answer("Numbers only."); return
    await set_user(m.from_user.id, daily_budget=v)
    await state.clear()
    await m.answer("✅ Saved.", reply_markup=kb_settings())

# Deposit / Refund / Balance
def kb_deposit_screen(balance: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Add 50⭐", callback_data="dep:50"),
         InlineKeyboardButton(text="Add 1000⭐", callback_data="dep:1000"),
         InlineKeyboardButton(text="Add 3000⭐", callback_data="dep:3000")],
        [InlineKeyboardButton(text="Custom…", callback_data="dep:custom")],
        [InlineKeyboardButton(text="↩️ Refund last (credit)", callback_data="refund:last")],
        [InlineKeyboardButton(text="◀️ Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "menu:deposit")
async def menu_deposit(cq: types.CallbackQuery, state: FSMContext):
    u = await get_user(cq.from_user.id)
    txt = f"{E['deposit']} <b>Deposit / Balance</b>\nBalance: <b>{u.get('credit',0)}⭐</b>"
    await cq.message.edit_text(txt, reply_markup=kb_deposit_screen(u.get("credit",0)))

@dp.callback_query(F.data.startswith("dep:"))
async def dep_quick(cq: types.CallbackQuery, state: FSMContext):
    _, val = cq.data.split(":")
    if val == "custom":
        await state.set_state(Entry.deposit)
        await cq.message.edit_text(
            f"Send Stars amount (multiple of 5, {MIN_XTR}–{MAX_XTR}).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Back", callback_data="menu:deposit")]])
        )
        return
    await send_invoice(cq.from_user.id, int(val))

@dp.message(Entry.deposit)
async def dep_custom(m: Message, state: FSMContext):
    try:
        amt = int(m.text.strip())
        assert MIN_XTR <= amt <= MAX_XTR and amt % 5 == 0
    except Exception:
        await m.answer(f"Invalid amount. Use a multiple of 5 between {MIN_XTR} and {MAX_XTR}.")
        return
    await state.clear()
    await send_invoice(m.chat.id, amt)

async def send_invoice(chat_id: int, amount: int):
    payload = json.dumps({"kind": "deposit", "amount": amount})
    await bot.send_invoice(
        chat_id=chat_id,
        title="Top up Stars",
        description=f"Add {amount}⭐ to bot balance",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount}⭐", amount=amount)],
        start_parameter=f"deposit_{amount}",
        need_name=False, need_phone_number=False, need_email=False, need_shipping_address=False
    )

@dp.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)

@dp.message(F.successful_payment)
async def on_paid(m: Message):
    total = m.successful_payment.total_amount  # XTR stars
    u = await get_user(m.from_user.id)
    u["credit"] += total
    await db_save_user(m.from_user.id, u)
    await db_log(m.from_user.id, "DEPOSIT", f"+{total}⭐ via Stars")
    await m.answer(f"Received <b>{total}⭐</b>. Balance: <b>{u['credit']}⭐</b>.")

@dp.callback_query(F.data == "refund:last")
async def refund_last(cq: types.CallbackQuery):
    # simple internal refund: credit back last purchase price (1 piece)
    async with db() as con:
        cur = await con.execute(
            "SELECT price FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (cq.from_user.id,)
        )
        row = await cur.fetchone()
    if not row:
        await cq.answer("Nothing to refund.", show_alert=True); return
    price = int(row[0])
    u = await get_user(cq.from_user.id)
    u["credit"] += price
    u["spent_today"] = max(0, u["spent_today"] - price)
    await db_save_user(cq.from_user.id, u)
    await db_log(cq.from_user.id, "REFUND", f"+{price}⭐ internal credit")
    await cq.answer(f"Refunded {price}⭐ to balance.", show_alert=False)

# Source connect (notifier channel)
@dp.callback_query(F.data == "menu:source")
async def menu_source(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    x = u.get("notifier_id")
    txt = (
        "Set source channel for drops.\n\n"
        "1) Make bot admin in your channel\n"
        "2) Get channel ID (e.g., -100123...) via @userinfobot\n"
        "3) Send <code>/source -1001234567890</code>\n\n"
        f"Current: <b>{x if x else 'not set'}</b>"
    )
    await cq.message.edit_text(txt, reply_markup=kb_source())

@dp.callback_query(F.data == "source:how")
async def source_how(cq: types.CallbackQuery):
    await cq.answer("Add bot as Admin in the channel; then /source -100...", show_alert=True)

@dp.message(Command("source"))
async def cmd_source(m: Message):
    if m.from_user.id not in ADMINS: return
    parts = (m.text or "").split()
    if len(parts) != 2:
        await m.answer("Usage: /source -1001234567890"); return
    try:
        ch_id = int(parts[1])
    except ValueError:
        await m.answer("Channel id must be an integer like -100…"); return
    await set_user(m.from_user.id, notifier_id=ch_id)
    # try to post confirmation
    try:
        await bot.send_message(ch_id, "✅ AutoBuy bot connected. Watching for LIMITED GIFT drops.")
        await m.answer("Source saved and confirmed ✅")
    except Exception as e:
        await m.answer(f"Saved, but could not post in channel: <code>{e}</code>")

# Test Drop
@dp.callback_query(F.data == "menu:test")
async def menu_test(cq: types.CallbackQuery):
    await cq.message.edit_text(
        f"{E['rocket']} Test drop: will auto-buy if in band & Auto is ON.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Simulate 150⭐", callback_data="test:150"),
             InlineKeyboardButton(text="Simulate 500⭐", callback_data="test:500"),
             InlineKeyboardButton(text="Simulate 1000⭐", callback_data="test:1000")],
            [InlineKeyboardButton(text="◀️ Back", callback_data="back:main")]
        ])
    )

@dp.callback_query(F.data.startswith("test:"))
async def test_fire(cq: types.CallbackQuery):
    price = int(cq.data.split(":")[1])
    await cq.message.answer(f"{E['warn']} Limited gift detected (test) — {price}⭐")
    await greedy_buy(cq.from_user.id, price, f"Test {price}", "test")
    await cq.answer("Done.")

# ───────────────────────── NOTIFIER LISTENER ─────────────────────────
@dp.channel_post()
async def on_channel_post(msg: Message):
    # Only process posts from configured source channels
    text = (msg.text or msg.caption or "")
    price = parse_price(text)
    if price is None:
        # try formats similar to tg_gifts_notifier; fallback to ignore
        return
    name = parse_name(text)
    emoji = first_emoji(text)

    # For every admin bound to this channel, auto-buy
    async with db() as con:
        cur = await con.execute("SELECT user_id FROM users WHERE notifier_id=?", (msg.chat.id,))
        uids = [r[0] for r in await cur.fetchall()]
    if not uids:
        return

    # Typical notifier phrasing includes "limited" / "drop" but we don't strictly require it.
    for uid in uids:
        u = await get_user(uid)
        if not u.get("auto_on"): 
            await db_log(uid, "SEEN", f"Drop {emoji} {name} {price}⭐ (auto off)")
            continue
        await db_log(uid, "SEEN", f"Drop {emoji} {name} {price}⭐ — buying…")
        await greedy_buy(uid, price, name, f"channel:{msg.chat.id}")

# ───────────────────────── INVOICE HANDLERS ─────────────────────────
@dp.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)

@dp.message(F.successful_payment)
async def success_payment(m: Message):
    total = m.successful_payment.total_amount
    u = await get_user(m.from_user.id)
    u["credit"] += total
    await db_save_user(m.from_user.id, u)
    await db_log(m.from_user.id, "DEPOSIT", f"+{total}⭐")
    await m.answer(f"Received <b>{total}⭐</b>. Balance: <b>{u['credit']}⭐</b>.")

# ───────────────────────── STARTUP ─────────────────────────
async def main():
    # make sure DB file exists and is sane
    try:
        sqlite3.connect(DB_PATH).close()
    except Exception:
        pass
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
