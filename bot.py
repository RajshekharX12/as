import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

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
from dotenv import load_dotenv

# ───────────────────────── Config ─────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "./dream.sqlite3")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing in .env")

ADMINS = {7940894807, 5770074932}  # private bot

# Stars rules (custom deposit guard)
MIN_XTR, MAX_XTR = 15, 200000

# Catalog (extend as needed)
CATALOG = [
    {"id": "heart", "emoji": "💝", "name": "Heart", "price": 15},
    {"id": "teddy", "emoji": "🧸", "name": "Teddy", "price": 15},
    {"id": "rose",  "emoji": "🌹", "name": "Rose",  "price": 25},
    {"id": "gift",  "emoji": "🎁", "name": "Gift",  "price": 25},
    {"id": "cake",  "emoji": "🎂", "name": "Cake",  "price": 50},
    {"id": "bouq",  "emoji": "💐", "name": "Bouquet", "price": 50},
    {"id": "rocket","emoji": "🚀", "name": "Rocket", "price": 50},
    {"id": "ring",  "emoji": "💍", "name": "Ring",  "price": 100},
]
PACKS = [1, 2, 3, 5, 10, 20, 30, 50, 75, 100]

DEFAULT_USER = {
    "auto_on": False,
    "notify_on": True,
    "cycles": 1,
    "lower_limit": 0,
    "upper_limit": 0,
    "overall_limit": 0,
    "supply_limit": 0,
    "daily_budget": 0,
    "spent_today": 0,
    "credit": 0,
    "notifier_id": None,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s :: %(message)s")
log = logging.getLogger("dream")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ───────────────────────── DB ─────────────────────────
def db():
    # IMPORTANT: return the connection (do NOT await here)
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with db() as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id        INTEGER PRIMARY KEY,
                auto_on        INTEGER DEFAULT 0,
                notify_on      INTEGER DEFAULT 1,
                cycles         INTEGER DEFAULT 1,
                lower_limit    INTEGER DEFAULT 0,
                upper_limit    INTEGER DEFAULT 0,
                overall_limit  INTEGER DEFAULT 0,
                supply_limit   INTEGER DEFAULT 0,
                daily_budget   INTEGER DEFAULT 0,
                spent_today    INTEGER DEFAULT 0,
                credit         INTEGER DEFAULT 0,
                notifier_id    INTEGER
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS payments(
                tx_id    TEXT PRIMARY KEY,
                user_id  INTEGER,
                amount   INTEGER,
                ts       INTEGER
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS logs(
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                kind    TEXT,
                message TEXT,
                ts      INTEGER
            )
        """)
        await con.commit()

async def db_get_user(uid: int) -> Optional[dict]:
    async with db() as con:
        cur = await con.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

async def db_save_user(uid: int, u: dict):
    fields = [k for k in DEFAULT_USER.keys()]
    placeholders = ",".join(f"{k}=?" for k in fields)
    values = [int(u[k]) if isinstance(DEFAULT_USER[k], bool) else u[k] for k in fields]
    async with db() as con:
        await con.execute(
            f"INSERT INTO users(user_id,{','.join(fields)}) "
            f"VALUES(?,{','.join('?' for _ in fields)}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {placeholders}",
            (uid, *values, *values)
        )
        await con.commit()

async def db_all_users() -> list[int]:
    async with db() as con:
        cur = await con.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]

async def db_all_notifier_ids() -> list[int]:
    async with db() as con:
        cur = await con.execute("SELECT DISTINCT notifier_id FROM users WHERE notifier_id IS NOT NULL")
        out = []
        for r in await cur.fetchall():
            try:
                out.append(int(r[0]))
            except:
                pass
        return out

async def db_log(uid: int, kind: str, message: str):
    async with db() as con:
        await con.execute(
            "INSERT INTO logs(user_id, kind, message, ts) VALUES(?,?,?,?)",
            (uid, kind, message, int(datetime.now(tz=timezone.utc).timestamp()))
        )
        await con.commit()

async def db_last_logs(uid: int, limit=12) -> list[tuple[str,str]]:
    async with db() as con:
        cur = await con.execute(
            "SELECT kind, message FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit)
        )
        return await cur.fetchall()

# ensure defaults
async def get_user(uid: int) -> dict:
    u = await db_get_user(uid)
    if not u:
        u = DEFAULT_USER.copy()
        await db_save_user(uid, u)
    else:
        changed = False
        for k, v in DEFAULT_USER.items():
            if k not in u or u[k] is None:
                u[k] = v
                changed = True
        if changed:
            await db_save_user(uid, u)
    return u

async def set_user(uid: int, **updates):
    u = await get_user(uid)
    u.update(updates)
    await db_save_user(uid, u)
    return u

# ───────────────────────── FSM ─────────────────────────
class Entry(StatesGroup):
    field = State()    # which field we are asking for
    deposit = State()  # deposit custom amount

# ───────────────────────── UI helpers ─────────────────────────
def pretty_inf(n: int, suffix="⭐"):
    return f"∞{suffix if suffix else ''}" if not n or n < 0 else f"{n}{suffix}"

def main_menu_kb(u: dict) -> InlineKeyboardMarkup:
    auto = "ON ✅" if u.get("auto_on") else "OFF ❌"
    rows = [
        [InlineKeyboardButton(f"⚡ Auto-Buy: {auto}", callback_data="auto:toggle"),
         InlineKeyboardButton("🩺 Health", callback_data="menu:health")],
        [InlineKeyboardButton("⚙️ Auto-Purchase Settings", callback_data="menu:settings")],
        [InlineKeyboardButton("🎁 Gift Catalog", callback_data="menu:catalog")],
        [InlineKeyboardButton("👤 Profile", callback_data="menu:profile"),
         InlineKeyboardButton("⭐ Deposit", callback_data="menu:deposit")],
        [InlineKeyboardButton("🚀 Test Event", callback_data="menu:test"),
          InlineKeyboardButton("🗒️ Logs", callback_data="menu:logs")],
        [InlineKeyboardButton("🆘 Support", callback_data="menu:support")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def settings_view(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    txt = (
        "⚙️ <b>Auto-Purchase Settings</b>\n"
        f"Cycles: <b>{u.get('cycles',1)}</b>\n"
        f"Lower limit: <b>{pretty_inf(u.get('lower_limit',0))}</b>\n"
        f"Upper limit: <b>{pretty_inf(u.get('upper_limit',0))}</b>\n"
        f"Overall limit: <b>{pretty_inf(u.get('overall_limit',0))}</b>\n"
        f"Supply limit: <b>{pretty_inf(u.get('supply_limit',0), suffix='')}</b>\n"
        f"Daily budget: <b>{pretty_inf(u.get('daily_budget',0))}</b>"
    )
    rows = [
        [InlineKeyboardButton("Cycles", callback_data="set:cycles")],
        [InlineKeyboardButton("Lower limit", callback_data="set:lower"),
         InlineKeyboardButton("Upper limit", callback_data="set:upper")],
        [InlineKeyboardButton("Overall limit", callback_data="set:overall"),
         InlineKeyboardButton("Supply limit", callback_data="set:supply")],
        [InlineKeyboardButton("Daily budget", callback_data="set:daily")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back:main")],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)

def catalog_list_kb(page=0, per_page=6):
    start = page * per_page
    items = CATALOG[start:start+per_page]
    title = "🎁 <b>Catalog</b>"
    rows = []
    for g in items:
        rows.append([InlineKeyboardButton(
            f"{g['emoji']} {g['name']} — {g['price']}⭐", callback_data=f"cat:sel:{g['id']}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cat:page:{page-1}"))
    if start + per_page < len(CATALOG):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cat:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back:main")])
    return title, InlineKeyboardMarkup(inline_keyboard=rows)

def catalog_detail_kb(gid: str):
    g = next(x for x in CATALOG if x["id"] == gid)
    header = f"🎁 <b>{g['emoji']} {g['name']}</b> <i>({g['price']}⭐ each)</i>"
    rows, row = [], []
    for i, n in enumerate(PACKS, 1):
        row.append(InlineKeyboardButton(f"{n}× for {n*g['price']}⭐", callback_data=f"buy:{gid}:{n}"))
        if i % 2 == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:catalog")])
    return header, InlineKeyboardMarkup(inline_keyboard=rows)

def deposit_kb():
    rows = [
        [InlineKeyboardButton("Add 50⭐", callback_data="deposit:50"),
         InlineKeyboardButton("Add 1000⭐", callback_data="deposit:1000"),
         InlineKeyboardButton("Add 3000⭐", callback_data="deposit:3000")],
        [InlineKeyboardButton("Custom amount…", callback_data="deposit:custom")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ───────────────────────── Access guard ─────────────────────────
def private_guard(m: Message) -> bool:
    return m.from_user and m.from_user.id in ADMINS

@dp.message()
async def guard_all(m: Message):
    if not private_guard(m):
        if m.text and (m.text.startswith("/") or m.text.lower() in {"menu", "start"}):
            await m.answer("🔒 Private bot.")
        return

# ───────────────────────── Start / Menu ─────────────────────────
@dp.message(CommandStart())
async def cmd_start(m: Message):
    if not private_guard(m):
        await m.answer("🔒 Private bot.")
        return
    u = await get_user(m.from_user.id)
    await m.answer("Menu", reply_markup=main_menu_kb(u))

# ───────────────────────── Profile / Health / Logs / Support ─────────────────────────
@dp.callback_query(F.data == "menu:profile")
async def menu_profile(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    txt = (
        "👤 <b>Profile</b>\n"
        f"💰 Internal credit: <b>{u.get('credit',0)}⭐</b>\n"
        f"🤖 Bot Stars: <b>0⭐</b>\n\n"
        "♻️ <b>Auto-Purchase</b>\n"
        f"— Cycles: <b>{u.get('cycles',1)}</b>\n"
        f"— Daily budget: <b>{pretty_inf(u.get('daily_budget',0))}</b>\n"
        f"⚡ Auto: <b>{'on' if u.get('auto_on') else 'off'}</b>"
    )
    await cq.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton("Refunds / Credits", callback_data="menu:refunds")],
                         [InlineKeyboardButton("⬅️ Back", callback_data="back:main")]]
    ))

@dp.callback_query(F.data == "menu:support")
async def menu_support(cq: types.CallbackQuery):
    txt = (
        "🆘 <b>Support</b>\n"
        "Contact: <b>@safoneapi</b>\n"
        "We can check your logs, limits and notifier settings.\n"
        "Tip: keep Auto-Buy ON and connect a notifier channel for instant drops."
    )
    await cq.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton("⬅️ Back", callback_data="back:main")]]
    ))

@dp.callback_query(F.data == "menu:health")
async def menu_health(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    ok_db = "✅"
    ok_notifier = "✅" if u.get("notifier_id") else "❌"
    txt = (
        "🩺 <b>Health</b>\n"
        f"• Database: {ok_db}\n"
        f"• Notifier: {ok_notifier}\n"
        f"• Auto-Buy: {'✅ ON' if u.get('auto_on') else '❌ OFF'}\n"
        f"• Credit: {u.get('credit',0)}⭐"
    )
    await cq.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton("⬅️ Back", callback_data="back:main")]]
    ))

@dp.callback_query(F.data == "menu:logs")
async def menu_logs(cq: types.CallbackQuery):
    rows = await db_last_logs(cq.from_user.id, 12)
    if not rows:
        out = "🗒️ <b>Logs</b>\n— empty —"
    else:
        lines = ["🗒️ <b>Logs</b>"]
        for k, msg in rows:
            lines.append(f"• <code>{k}</code> — {msg}")
        out = "\n".join(lines)
    await cq.message.edit_text(out, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton("⬅️ Back", callback_data="back:main")]]
    ))

# ───────────────────────── Settings ─────────────────────────
@dp.callback_query(F.data == "menu:settings")
async def menu_settings(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    txt, kb = settings_view(u)
    await cq.message.edit_text(txt, reply_markup=kb)

class FieldNames(StatesGroup):
    pass

ENTRY_MAP = {
    "set:cycles":  ("Send number of cycles (0 = ∞).", "cycles"),
    "set:lower":   ("Send LOWER price limit in ⭐ (0 = none).", "lower_limit"),
    "set:upper":   ("Send UPPER price limit in ⭐ (0 = none).", "upper_limit"),
    "set:overall": ("Send OVERALL limit in ⭐ (0 = ∞).", "overall_limit"),
    "set:supply":  ("Send SUPPLY limit in pcs (0 = ∞).", "supply_limit"),
    "set:daily":   ("Send DAILY budget in ⭐ (0 = ∞).", "daily_budget"),
}

@dp.callback_query(F.data.in_(ENTRY_MAP.keys()))
async def ask_field(cq: types.CallbackQuery, state: FSMContext):
    prompt, field = ENTRY_MAP[cq.data]
    await state.set_state(Entry.field)
    await state.update_data(field=field)
    await cq.message.edit_text(prompt, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton("⬅️ Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.field)
async def set_field(m: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    try:
        v = int(m.text.strip())
        if v < 0:
            v = 0
    except Exception:
        await m.answer("Numbers only. Try again.")
        return
    await set_user(m.from_user.id, **{field: v})
    await state.clear()
    u = await get_user(m.from_user.id)
    txt, kb = settings_view(u)
    await m.answer("✅ Saved.\n\n" + txt, reply_markup=kb)

# ───────────────────────── Catalog ─────────────────────────
@dp.callback_query(F.data == "menu:catalog")
async def catalog_root(cq: types.CallbackQuery):
    title, kb = catalog_list_kb(0)
    await cq.message.edit_text(title, reply_markup=kb)

@dp.callback_query(F.data.startswith("cat:page:"))
async def catalog_page(cq: types.CallbackQuery):
    page = int(cq.data.split(":")[2])
    title, kb = catalog_list_kb(page)
    await cq.message.edit_text(title, reply_markup=kb)

@dp.callback_query(F.data.startswith("cat:sel:"))
async def catalog_detail(cq: types.CallbackQuery):
    gid = cq.data.split(":")[2]
    header, kb = catalog_detail_kb(gid)
    await cq.message.edit_text(header, reply_markup=kb)

# ───────────────────────── Manual Buy ─────────────────────────
async def try_debit(uid: int, cost: int) -> tuple[bool, str]:
    u = await get_user(uid)
    if u["credit"] < cost:
        return False, "Insufficient credit."
    if u.get("daily_budget", 0) and u.get("spent_today", 0) + cost > u["daily_budget"]:
        return False, "Daily budget exceeded."
    if u.get("overall_limit", 0) and u["overall_limit"] < cost:
        return False, "Overall limit reached."
    u["credit"] -= cost
    u["spent_today"] += cost
    if u.get("overall_limit", 0):
        u["overall_limit"] -= cost
    await db_save_user(uid, u)
    return True, "OK"

async def send_fake_gift(chat_id: int, emoji: str, name: str, total_cost: int, qty: int, mode: str):
    caption = f"<b>Gift from Dream</b>\nMode: <i>{mode}</i>\n{emoji} {name} × <b>{qty}</b>\nSpent: <b>{total_cost}⭐</b>"
    await bot.send_message(chat_id, caption)

@dp.callback_query(F.data.startswith("buy:"))
async def on_buy(cq: types.CallbackQuery):
    # data: buy:<gid>:<count>
    _, gid, count_s = cq.data.split(":")
    count = int(count_s)
    g = next(x for x in CATALOG if x["id"] == gid)
    cost = g["price"] * count

    ok, reason = await try_debit(cq.from_user.id, cost)
    if not ok:
        await cq.answer(reason, show_alert=True)
        return

    await db_log(cq.from_user.id, "BUY", f"{g['name']}×{count} cost={cost}")
    await send_fake_gift(cq.from_user.id, g["emoji"], g["name"], cost, count, "Manual")
    await cq.answer("Purchased ✅", show_alert=False)

# ───────────────────────── Deposit (Stars) ─────────────────────────
def deposit_menu_kb() -> InlineKeyboardMarkup:
    return deposit_kb()

@dp.callback_query(F.data == "menu:deposit")
async def menu_deposit(cq: types.CallbackQuery):
    await cq.message.edit_text("Top up Stars", reply_markup=deposit_menu_kb())

async def send_invoice(chat_id: int, amount: int):
    title = "Top up Stars"
    desc = f"Add {amount}⭐ to bot balance"
    payload = json.dumps({"kind": "deposit", "amount": amount})
    prices = [LabeledPrice(label=f"{amount}⭐", amount=amount)]
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=desc,
        payload=payload,
        currency="XTR",
        prices=prices,
        start_parameter=f"deposit_{amount}",
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False
    )

@dp.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)

@dp.message(F.successful_payment)
async def on_paid(m: Message):
    total = m.successful_payment.total_amount  # for Stars (XTR), this is the stars count
    u = await get_user(m.from_user.id)
    u["credit"] += total
    await db_save_user(m.from_user.id, u)
    await db_log(m.from_user.id, "DEPOSIT", f"+{total}⭐ via Stars")
    await m.answer(f"Received <b>{total}⭐</b>. Internal credit: <b>{u['credit']}⭐</b>.")

@dp.callback_query(F.data.startswith("deposit:"))
async def dep_quick(cq: types.CallbackQuery, state: FSMContext):
    kind, val = cq.data.split(":")
    if val == "custom":
        await state.set_state(Entry.deposit)
        await cq.message.edit_text(
            f"Send the Stars amount (e.g., 3300). Min {MIN_XTR}, Max {MAX_XTR}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("⬅️ Back", callback_data="menu:deposit")]])
        )
        return
    amount = int(val)
    await send_invoice(cq.from_user.id, amount)

@dp.message(Entry.deposit)
async def dep_custom(m: Message, state: FSMContext):
    try:
        amt = int(m.text.strip())
        assert MIN_XTR <= amt <= MAX_XTR
        assert amt % 5 == 0
    except Exception:
        await m.answer(f"Invalid amount. Use a multiple of 5 between {MIN_XTR} and {MAX_XTR}.")
        return
    await state.clear()
    await send_invoice(m.chat.id, amt)

# ───────────────────────── Refunds / Credits (internal) ─────────────────────────
@dp.callback_query(F.data == "menu:refunds")
async def refunds_menu(cq: types.CallbackQuery):
    txt = "Refunds / Credits — internal only (Telegram Stars are final)."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("↩️ Refund last purchase (credit)", callback_data="credit:refund_last")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back:main")]
    ])
    await cq.message.edit_text(txt, reply_markup=kb)

@dp.callback_query(F.data == "credit:refund_last")
async def do_refund(cq: types.CallbackQuery):
    await cq.answer("Nothing to refund", show_alert=True)

# ───────────────────────── Auto-Buy toggle ─────────────────────────
@dp.callback_query(F.data == "auto:toggle")
async def toggle_auto(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    new_flag = not u.get("auto_on")
    await set_user(cq.from_user.id, auto_on=new_flag)
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=main_menu_kb(u))
    await cq.answer("Auto-Buy is now " + ("ON" if new_flag else "OFF"))

# ───────────────────────── Notifier channel connect + listener ─────────────────────────
@dp.message(Command("source"))
async def cmd_source(m: Message):
    if not private_guard(m): return
    parts = (m.text or "").split()
    if len(parts) != 2:
        await m.answer("Send: <code>/source -1001234567890</code>")
        return
    try:
        ch_id = int(parts[1])
    except ValueError:
        await m.answer("Channel id must be an integer like -100…")
        return
    await set_user(m.from_user.id, notifier_id=ch_id)
    try:
        await bot.send_message(ch_id, "✅ Dream is now connected and listening for limited gift drops.")
        await m.answer("Source saved and confirmed ✅")
    except Exception as e:
        await m.answer(f"Saved, but couldn’t write to channel: <code>{e}</code>")

@dp.channel_post()
async def on_channel_post(m: Message):
    text = (m.text or m.caption or "").lower()
    if ("limited" in text and "gift" in text) or ("drop" in text and "gift" in text):
        for uid in await db_all_users():
            u = await get_user(uid)
            if u.get("auto_on") and u.get("notifier_id") == m.chat.id:
                await try_auto_buy(uid)

# ───────────────────────── Test Event ─────────────────────────
@dp.callback_query(F.data == "menu:test")
async def menu_test(cq: types.CallbackQuery):
    n = len(CATALOG)
    names = ", ".join(f"{g['emoji']} {g['name']}" for g in CATALOG)
    txt = f"Test Event\n— Simulate a limited drop.\n\nAvailable gifts now: <b>{n}</b>\n{names}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Simulate drop (notify)", callback_data="test:notify")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back:main")]
    ])
    await cq.message.edit_text(txt, reply_markup=kb)

@dp.callback_query(F.data == "test:notify")
async def test_notify(cq: types.CallbackQuery):
    await cq.message.answer("🚨 Limited gift detected (test) — will try to buy if Auto-Buy is ON.")
    await try_auto_buy(cq.from_user.id)

# ───────────────────────── Auto-buy engine ─────────────────────────
def first_allowed_gift(u: dict):
    low = u.get("lower_limit", 0) or 0
    up  = u.get("upper_limit", 0) or 0
    for g in CATALOG:
        if low and g["price"] < low: continue
        if up  and g["price"] > up:  continue
        return g
    return None

async def try_auto_buy(uid: int):
    u = await get_user(uid)
    if not u.get("auto_on"):
        await db_log(uid, "AUTO", "Skipped (auto off)")
        return
    g = first_allowed_gift(u)
    if not g:
        await db_log(uid, "AUTO", "No gift matches price filters")
        return
    cycles = u.get("cycles", 1)
    qty = 1 if cycles <= 0 else 1  # conservative: buy 1 per trigger
    if u.get("supply_limit", 0):
        qty = min(qty, u["supply_limit"])
    total = g["price"] * qty
    ok, reason = await try_debit(uid, total)
    if not ok:
        await db_log(uid, "AUTO", f"Fail: {reason}")
        return
    await db_log(uid, "AUTO", f"Bought {g['name']}×{qty} cost={total}")
    await send_fake_gift(uid, g["emoji"], g["name"], total, qty, "Auto")

# ───────────────────────── Back to main & commands ─────────────────────────
@dp.callback_query(F.data == "back:main")
async def back_main(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    await cq.message.edit_text("Menu", reply_markup=main_menu_kb(u))

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    if not private_guard(m): 
        await m.answer("🔒 Private bot."); return
    u = await get_user(m.from_user.id)
    await m.answer("Menu", reply_markup=main_menu_kb(u))

@dp.message(Command("deposit"))
async def cmd_deposit(m: Message):
    if not private_guard(m): return
    await m.answer("Top up Stars", reply_markup=deposit_kb())

@dp.message(Command("support"))
async def cmd_support(m: Message):
    await m.answer("🆘 Support: @safoneapi")

# ───────────────────────── Startup ─────────────────────────
async def notify_channels_startup():
    for ch_id in await db_all_notifier_ids():
        try:
            await bot.send_message(ch_id, "✅ Dream bot is online and watching this channel.")
        except Exception as e:
            log.warning("Notify failed for %s: %s", ch_id, e)

async def main():
    await init_db()
    await notify_channels_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
