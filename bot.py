import asyncio
import json
import logging
import re
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

# ───────────────────────── EDIT THIS ─────────────────────────
BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"     # ← put your bot token
ADMINS = {7940894807, 5770074932}           # allowed users
DB_PATH = "./autobuy.sqlite3"               # sqlite file
PRICE_MIN_DEFAULT = 100
PRICE_MAX_DEFAULT = 1000
MIN_XTR, MAX_XTR = 15, 200000               # Stars invoice bounds
# ─────────────────────────────────────────────────────────────

E = dict(bolt="⚡", ok="✅", bad="❌", gear="⚙️", gift="🎁",
         profile="🧑‍💼", deposit="⭐", logs="📄", health="🩺",
         back="◀️", rocket="🚀", wallet="👛", warn="🚨",
         connect="🔗", trash="🗑️", support="🆘")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
log = logging.getLogger("autobuy")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ───────────────────────── DB ─────────────────────────
def db():
    return aiosqlite.connect(DB_PATH)

async def init_db():
    # NOTE: SQLite doesn't allow ? placeholders in DDL, so we inline defaults.
    async with db() as con:
        await con.execute(f"""
            CREATE TABLE IF NOT EXISTS users(
                user_id      INTEGER PRIMARY KEY,
                auto_on      INTEGER DEFAULT 0,
                min_price    INTEGER DEFAULT {PRICE_MIN_DEFAULT},
                max_price    INTEGER DEFAULT {PRICE_MAX_DEFAULT},
                daily_budget INTEGER DEFAULT 0,
                spent_today  INTEGER DEFAULT 0,
                spent_day    INTEGER DEFAULT 0,
                credit       INTEGER DEFAULT 0,
                notifier_id  INTEGER
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
        await con.execute("""
            CREATE TABLE IF NOT EXISTS purchases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                price   INTEGER,
                qty     INTEGER,
                mode    TEXT,
                ts      INTEGER
            )
        """)
        await con.commit()

DEFAULT_USER = {
    "auto_on": False,
    "min_price": PRICE_MIN_DEFAULT,
    "max_price": PRICE_MAX_DEFAULT,
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
    fields = [k for k in DEFAULT_USER.keys()]
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

async def last_logs(uid: int, limit=20) -> List[Tuple[str, str]]:
    async with db() as con:
        cur = await con.execute(
            "SELECT kind, message FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit)
        )
        return await cur.fetchall()

# ───────────────────────── FSM ─────────────────────────
class Entry(StatesGroup):
    set_min = State()
    set_max = State()
    set_daily = State()
    wait_channel_forward = State()
    custom_deposit = State()

# ───────────────────────── UI ─────────────────────────
def kb_main(u: dict) -> InlineKeyboardMarkup:
    auto = "ON ✅" if u.get("auto_on") else "OFF ❌"
    rows = [
        [InlineKeyboardButton(text=f"{E['bolt']} Auto-Buy: {auto}", callback_data="auto:toggle"),
         InlineKeyboardButton(text=f"{E['health']} Health", callback_data="menu:health")],
        [InlineKeyboardButton(text=f"{E['gear']} Settings", callback_data="menu:settings"),
         InlineKeyboardButton(text=f"{E['gift']} Test Drop", callback_data="menu:test")],
        [InlineKeyboardButton(text=f"{E['deposit']} Deposit / Balance / Refund", callback_data="menu:money")],
        [InlineKeyboardButton(text=f"{E['connect']} Connect Notifier", callback_data="menu:connect")],
        [InlineKeyboardButton(text=f"{E['logs']} Logs", callback_data="menu:logs")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_settings() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Set MIN price", callback_data="set:min"),
         InlineKeyboardButton(text="Set MAX price", callback_data="set:max")],
        [InlineKeyboardButton(text="Set DAILY budget", callback_data="set:daily")],
        [InlineKeyboardButton(text=f"{E['back']} Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_money() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Add 100⭐", callback_data="dep:100"),
         InlineKeyboardButton(text="Add 1000⭐", callback_data="dep:1000"),
         InlineKeyboardButton(text="Add 3000⭐", callback_data="dep:3000")],
        [InlineKeyboardButton(text="Custom…", callback_data="dep:custom"),
         InlineKeyboardButton(text="Refund last", callback_data="refund:last")],
        [InlineKeyboardButton(text=f"{E['back']} Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_health(u: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Test Notifier", callback_data="health:test")],
        [InlineKeyboardButton(text=f"{E['back']} Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ───────────────────────── Helpers ─────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMINS

def parse_price(text: str) -> Optional[int]:
    s = text.lower()
    m = re.search(r"(\d{1,6})\s*(stars|star|⭐|xtr)", s)
    return int(m.group(1)) if m else None

def today_key() -> int:
    d = date.today()
    return d.year * 10000 + d.month * 100 + d.day

async def try_send_gift(to_user_id: int, stars_each: int, reason: str) -> Tuple[bool, str]:
    payload = {"chat_id": to_user_id, "gift": json.dumps({"star_count": stars_each}), "text": reason}
    try:
        res = await bot.request("sendGift", payload)
        return (True, "OK") if res else (False, "Unknown API response")
    except Exception as e:
        return False, str(e)

async def debit_once(uid: int, price: int) -> Tuple[bool, str]:
    u = await get_user(uid)
    # rollover daily spend
    if u.get("spent_day", 0) != today_key():
        u["spent_day"] = today_key()
        u["spent_today"] = 0
    if u["credit"] < price:
        await db_save_user(uid, u); return False, "Insufficient credit."
    if u.get("daily_budget", 0) and u["spent_today"] + price > u["daily_budget"]:
        await db_save_user(uid, u); return False, "Daily budget exceeded."
    u["credit"] -= price
    u["spent_today"] += price
    await db_save_user(uid, u)
    return True, "OK"

async def autobuy(uid: int, price: int, source: str):
    """Buy as many items of `price` as possible within range/budget/credit."""
    u = await get_user(uid)
    if not u.get("auto_on"):
        await db_log(uid, "AUTO", "off"); return
    if price < u.get("min_price", PRICE_MIN_DEFAULT) or price > u.get("max_price", PRICE_MAX_DEFAULT):
        await db_log(uid, "AUTO_SKIP", f"price {price}⭐ out of range"); return

    sent = 0
    while True:
        ok, why = await debit_once(uid, price)
        if not ok:
            await db_log(uid, "AUTO_STOP", why)
            break
        ok2, why2 = await try_send_gift(uid, price, f"Auto ({source})")
        if not ok2:
            # refund the failed debit
            u2 = await get_user(uid)
            u2["credit"] += price
            u2["spent_today"] = max(0, u2["spent_today"] - price)
            await db_save_user(uid, u2)
            await db_log(uid, "AUTO_FAIL", why2)
            break
        sent += 1
        if sent >= 1000:  # absolute safety ceiling
            break
        await asyncio.sleep(0.02)  # very fast but not hammering

    if sent:
        async with db() as con:
            now = int(datetime.now(tz=timezone.utc).timestamp())
            await con.execute(
                "INSERT INTO purchases(user_id, price, qty, mode, ts) VALUES(?,?,?,?,?)",
                (uid, price, sent, "auto", now)
            )
            await con.commit()
    await db_log(uid, "AUTO_DONE", f"{sent}× at {price}⭐")

# ───────────────────────── UI Handlers ─────────────────────────
@dp.message(CommandStart())
async def start(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("🔒 Private bot."); return
    u = await get_user(m.from_user.id)
    await m.answer("Menu", reply_markup=kb_main(u))

@dp.callback_query(F.data == "back:main")
async def back_main(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    await cq.message.edit_text("Menu", reply_markup=kb_main(u))

@dp.callback_query(F.data == "auto:toggle")
async def auto_toggle(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    flag = not u.get("auto_on")
    await set_user(cq.from_user.id, auto_on=flag)
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=kb_main(u))
    await cq.answer("Auto-Buy is now " + ("ON" if flag else "OFF"))

@dp.callback_query(F.data == "menu:settings")
async def menu_settings(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    txt = (
        f"{E['gear']} <b>Settings</b>\n"
        f"Price range: <b>{u.get('min_price', PRICE_MIN_DEFAULT)}–{u.get('max_price', PRICE_MAX_DEFAULT)}⭐</b>\n"
        f"Daily budget: <b>{u.get('daily_budget',0)}⭐</b>\n"
        "Auto-buy consumes balance greedily for each drop."
    )
    await cq.message.edit_text(txt, reply_markup=kb_settings())

@dp.callback_query(F.data == "set:min")
async def set_min(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.set_min)
    await cq.message.edit_text("Send MIN price (e.g., 100)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"{E['back']} Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.set_min)
async def set_min_value(m: Message, state: FSMContext):
    try:
        v = max(0, int(m.text.strip()))
    except:
        await m.answer("Numbers only."); return
    u = await get_user(m.from_user.id)
    await set_user(m.from_user.id, min_price=v, max_price=max(v, u.get("max_price", PRICE_MAX_DEFAULT)))
    await state.clear()
    await m.answer("Saved.", reply_markup=kb_settings())

@dp.callback_query(F.data == "set:max")
async def set_max(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.set_max)
    await cq.message.edit_text("Send MAX price (e.g., 1000)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"{E['back']} Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.set_max)
async def set_max_value(m: Message, state: FSMContext):
    try:
        v = max(0, int(m.text.strip()))
    except:
        await m.answer("Numbers only."); return
    u = await get_user(m.from_user.id)
    await set_user(m.from_user.id, max_price=v, min_price=min(v, u.get("min_price", PRICE_MIN_DEFAULT)))
    await state.clear()
    await m.answer("Saved.", reply_markup=kb_settings())

@dp.callback_query(F.data == "set:daily")
async def set_daily(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.set_daily)
    await cq.message.edit_text("Send DAILY budget in ⭐ (0 = unlimited)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"{E['back']} Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.set_daily)
async def set_daily_value(m: Message, state: FSMContext):
    try:
        v = max(0, int(m.text.strip()))
    except:
        await m.answer("Numbers only."); return
    await set_user(m.from_user.id, daily_budget=v)
    await state.clear()
    await m.answer("Saved.", reply_markup=kb_settings())

# Money
@dp.callback_query(F.data == "menu:money")
async def menu_money(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    txt = (
        f"{E['wallet']} <b>Balance</b>: <b>{u.get('credit',0)}⭐</b>\n"
        f"Daily spent: <b>{u.get('spent_today',0)}⭐</b>"
    )
    await cq.message.edit_text(txt, reply_markup=kb_money())

@dp.callback_query(F.data.startswith("dep:"))
async def dep_quick(cq: types.CallbackQuery, state: FSMContext):
    _, val = cq.data.split(":")
    if val == "custom":
        await state.set_state(Entry.custom_deposit)
        await cq.message.edit_text(
            f"Send amount (multiple of 5) {MIN_XTR}–{MAX_XTR}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{E['back']} Back", callback_data="menu:money")]
            ])
        ); return
    await send_invoice(cq.from_user.id, int(val))

@dp.message(Entry.custom_deposit)
async def dep_custom(m: Message, state: FSMContext):
    try:
        amt = int(m.text.strip())
        assert MIN_XTR <= amt <= MAX_XTR and amt % 5 == 0
    except:
        await m.answer(f"Use a multiple of 5 between {MIN_XTR} and {MAX_XTR}."); return
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
        start_parameter=f"dep_{amount}",
        need_name=False, need_phone_number=False, need_email=False, need_shipping_address=False
    )

@dp.pre_checkout_query()
async def prechk(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)

@dp.message(F.successful_payment)
async def on_paid(m: Message):
    total = m.successful_payment.total_amount  # XTR
    u = await get_user(m.from_user.id)
    u["credit"] += total
    await db_save_user(m.from_user.id, u)
    await db_log(m.from_user.id, "DEPOSIT", f"+{total}⭐")
    await m.answer(f"Received <b>{total}⭐</b>. Balance: <b>{u['credit']}⭐</b>.")

@dp.callback_query(F.data == "refund:last")
async def refund_last(cq: types.CallbackQuery):
    # internal refund of last purchase row
    async with db() as con:
        cur = await con.execute(
            "SELECT id, price, qty FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (cq.from_user.id,)
        )
        r = await cur.fetchone()
    if not r:
        await cq.answer("Nothing to refund.", show_alert=True); return
    pid, price, qty = r
    amount = price * qty
    u = await get_user(cq.from_user.id)
    u["credit"] += amount
    u["spent_today"] = max(0, u["spent_today"] - amount)  # adjust daily spent (best effort)
    await db_save_user(cq.from_user.id, u)
    async with db() as con:
        await con.execute("DELETE FROM purchases WHERE id=?", (pid,))
        await con.commit()
    await db_log(cq.from_user.id, "REFUND", f"+{amount}⭐ (last purchase)")
    await cq.answer(f"Refunded {amount}⭐", show_alert=True)
    await cq.message.edit_reply_markup(reply_markup=kb_money())

# Connect notifier (by forwarding any post from the channel)
@dp.callback_query(F.data == "menu:connect")
async def connect_prompt(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.wait_channel_forward)
    await cq.message.edit_text(
        f"{E['connect']} Forward ANY post from the channel here.\n"
        "Make sure the bot is ADMIN in that channel.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{E['back']} Back", callback_data="back:main")]
        ])
    )

@dp.message(Entry.wait_channel_forward)
async def on_forward(m: Message, state: FSMContext):
    ch_obj = None
    # Legacy field, if present:
    if hasattr(m, "forward_from_chat") and m.forward_from_chat:
        ch_obj = m.forward_from_chat
    # Bot API ≥7: forward_origin may hold channel info
    if ch_obj is None and getattr(m, "forward_origin", None):
        fo = m.forward_origin
        for attr in ("chat", "sender_chat", "from_chat"):
            if hasattr(fo, attr) and getattr(fo, attr):
                ch_obj = getattr(fo, attr)
                break
    if not ch_obj or not getattr(ch_obj, "id", None):
        await m.answer("That wasn’t a forwarded channel post. Try again."); return

    ch_id = ch_obj.id
    await set_user(m.from_user.id, notifier_id=ch_id)
    try:
        await bot.send_message(ch_id, "✅ AutoBuy connected and watching this channel.")
        await m.answer("Connected and confirmed ✅")
    except Exception as e:
        await m.answer(f"Saved channel id, but couldn’t post there:\n<code>{e}</code>")
    await state.clear()

# Health & Logs
@dp.callback_query(F.data == "menu:health")
async def menu_health(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    ok_notifier = "✅" if u.get("notifier_id") else "❌"
    txt = (
        f"{E['health']} <b>Health</b>\n"
        f"• DB: ✅\n"
        f"• Notifier: {ok_notifier}\n"
        f"• Auto: {'✅ ON' if u.get('auto_on') else '❌ OFF'}\n"
        f"• Balance: {u.get('credit',0)}⭐\n"
        f"• Price range: {u.get('min_price',PRICE_MIN_DEFAULT)}–{u.get('max_price',PRICE_MAX_DEFAULT)}⭐"
    )
    await cq.message.edit_text(txt, reply_markup=kb_health(u))

@dp.callback_query(F.data == "health:test")
async def health_test(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    ch = u.get("notifier_id")
    if not ch:
        await cq.answer("No channel connected.", show_alert=True); return
    try:
        await bot.send_message(ch, "🧪 Notifier test: OK.")
        await cq.answer("Posted ✅")
    except Exception as e:
        await cq.answer(f"Failed: {e}", show_alert=True)

@dp.callback_query(F.data == "menu:logs")
async def menu_logs(cq: types.CallbackQuery):
    rows = await last_logs(cq.from_user.id, 12)
    out = "— empty —" if not rows else "\n".join(f"• <code>{k}</code> — {msg}" for k, msg in rows)
    await cq.message.edit_text(f"{E['logs']} <b>Logs</b>\n{out}", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"{E['back']} Back", callback_data="back:main")]]
    ))

# Test drop (simulate)
@dp.callback_query(F.data == "menu:test")
async def menu_test(cq: types.CallbackQuery):
    await cq.message.edit_text(
        "Simulating: limited gift 150⭐ drop.\n"
        "Auto-buy will run if ON and in range.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Trigger", callback_data="test:go")],
            [InlineKeyboardButton(text=f"{E['back']} Back", callback_data="back:main")],
        ])
    )

@dp.callback_query(F.data == "test:go")
async def test_go(cq: types.CallbackQuery):
    await cq.answer("Drop detected (test).")
    await autobuy(cq.from_user.id, 150, "test")

# ───────────────────────── Channel listening (drop detector) ─────────────────────────
@dp.channel_post()
async def on_channel_post(msg: Message):
    text = (msg.text or msg.caption or "")
    has_keywords = ("gift" in text.lower() and ("limited" in text.lower() or "drop" in text.lower()))
    price = parse_price(text)
    if not (has_keywords or price):
        return

    # Which admins bound this channel?
    async with db() as con:
        cur = await con.execute("SELECT user_id FROM users WHERE notifier_id=?", (msg.chat.id,))
        uids = [r[0] for r in await cur.fetchall()]
    if not uids:
        return
    if not price:
        for uid in uids:
            await db_log(uid, "DROP_SEEN", "no price parsed")
        return

    # Fire autobuy fast per admin
    for uid in uids:
        asyncio.create_task(autobuy(uid, price, f"#{msg.chat.id}"))

# ───────────────────────── Run ─────────────────────────
async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN at top of bot.py")
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
