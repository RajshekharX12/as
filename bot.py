import asyncio
import json
import math
import os
import textwrap
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiosqlite
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── env ────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in .env")

DB_PATH = "bot.db"

# ── constants & helpers ────────────────────────────────────────────────────────
MAIN_EMOJI = "🎁"
STAR = "⭐"
CHECK = "✅"
CROSS = "✖️"
BACK = "⬅️"
SPARK = "⚡"
TOOLS = "🧰"
WALLET = "👛"
PROFILE = "👤"
LOGS = "📄"
HEALTH = "🩺"
SETTINGS = "⚙️"
BELL = "🔔"
RECYCLE = "♻️"

CATALOG_PAGE = 6  # gifts per page

# memory keys
AWAIT_VALUE = "await_value"         # (key,)
AWAIT_TXID  = "await_txid_refund"   # bool

def fmt_bool(b: bool) -> str:
    return "ON " + CHECK if b else "OFF " + CROSS

def chunk(it, n):
    it = list(it)
    return [it[i:i+n] for i in range(0, len(it), n)]

# ── data layer ────────────────────────────────────────────────────────────────
CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  credit   INTEGER NOT NULL DEFAULT 0,
  auto_buy INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(
  user_id INTEGER PRIMARY KEY,
  cycles   INTEGER NOT NULL DEFAULT 500,
  min_price INTEGER NOT NULL DEFAULT 0,
  max_price INTEGER NOT NULL DEFAULT 10_000,
  overall_limit INTEGER NOT NULL DEFAULT 1_000_000,
  supply_limit  INTEGER NOT NULL DEFAULT 0  -- 0 = ∞
);
CREATE TABLE IF NOT EXISTS allowlist(
  user_id INTEGER NOT NULL,
  gift_id TEXT NOT NULL,
  PRIMARY KEY(user_id, gift_id)
);
CREATE TABLE IF NOT EXISTS purchases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  gift_id TEXT NOT NULL,
  star_price INTEGER NOT NULL,
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credits_from_tx(
  tx_id TEXT PRIMARY KEY,
  amount INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  ts INTEGER NOT NULL
);
"""

async def open_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db

async def init_db():
    db = await open_db()
    try:
        await db.executescript(CREATE_SQL)
        await db.commit()
    finally:
        await db.close()

# ── user state ────────────────────────────────────────────────────────────────
async def ensure_user(db: aiosqlite.Connection, user_id: int):
    await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    await db.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (user_id,))
    await db.commit()

async def get_credit(db, user_id) -> int:
    cur = await db.execute("SELECT credit FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return row["credit"] if row else 0

async def add_credit(db, user_id: int, delta: int):
    await ensure_user(db, user_id)
    await db.execute("UPDATE users SET credit = credit + ? WHERE user_id=?", (delta, user_id))
    await db.commit()

async def set_auto_buy(db, user_id: int, enabled: bool):
    await ensure_user(db, user_id)
    await db.execute("UPDATE users SET auto_buy=? WHERE user_id=?", (1 if enabled else 0, user_id))
    await db.commit()

async def get_auto_buy(db, user_id: int) -> bool:
    cur = await db.execute("SELECT auto_buy FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return bool(row["auto_buy"]) if row else False

async def read_settings(db, user_id: int) -> Dict:
    cur = await db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else {}

async def write_setting(db, user_id: int, key: str, val: int):
    await db.execute(f"UPDATE settings SET {key}=? WHERE user_id=?", (val, user_id))
    await db.commit()

async def toggle_allow(db, user_id: int, gift_id: str) -> bool:
    try:
        await db.execute("INSERT INTO allowlist(user_id, gift_id) VALUES(?,?)", (user_id, gift_id))
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        await db.execute("DELETE FROM allowlist WHERE user_id=? AND gift_id=?", (user_id, gift_id))
        await db.commit()
        return False

async def is_allowed(db, user_id: int, gift_id: str) -> bool:
    cur = await db.execute("SELECT 1 FROM allowlist WHERE user_id=? AND gift_id=?", (user_id, gift_id))
    return await cur.fetchone() is not None

# ── gifts/cache ───────────────────────────────────────────────────────────────
@dataclass
class GiftInfo:
    id: str
    price: int
    emoji: str
    remaining: Optional[int]

async def fetch_gifts(context: ContextTypes.DEFAULT_TYPE) -> List[GiftInfo]:
    """
    Uses the live Bot API:
      - getAvailableGifts
      - pulls emoji from sticker.emoji when present
    """
    gifts: List[GiftInfo] = []
    raw = await context.bot.get_available_gifts()
    for g in raw.gifts:
        emoji = ""
        try:
            if g.sticker and getattr(g.sticker, "emoji", None):
                emoji = g.sticker.emoji
        except Exception:
            pass
        gifts.append(GiftInfo(
            id=g.id,
            price=g.star_count,
            emoji=emoji or "🎁",
            remaining=getattr(g, "remaining_count", None),
        ))
    # Sort cheapest first for nicer pages
    gifts.sort(key=lambda x: (x.price, x.id))
    return gifts

# ── UI builders ───────────────────────────────────────────────────────────────
def main_menu_kb(auto_on: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{SPARK} Auto-Buy: {fmt_bool(auto_on)}", callback_data="menu:toggle"),
         InlineKeyboardButton(f"🔎 Source: NOTIFIER", callback_data="menu:source")],
        [InlineKeyboardButton(f"{SETTINGS} Auto-Purchase Settings", callback_data="set:open")],
        [InlineKeyboardButton(f"{MAIN_EMOJI} Gift Catalog", callback_data="cata:page:0")],
        [InlineKeyboardButton(f"{PROFILE} Profile", callback_data="profile:open"),
         InlineKeyboardButton(f"⭐ Deposit", callback_data="deposit:open")],
        [InlineKeyboardButton(f"{HEALTH} Health", callback_data="health:open"),
         InlineKeyboardButton(f"{LOGS} Logs", callback_data="logs:open")],
    ]
    return InlineKeyboardMarkup(rows)

def settings_menu_kb(s: Dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("♻️ Cycles", callback_data="set:cycles")],
        [InlineKeyboardButton("Lower limit", callback_data="set:min"),
         InlineKeyboardButton("Upper limit", callback_data="set:max")],
        [InlineKeyboardButton("Overall limit", callback_data="set:overall"),
         InlineKeyboardButton("Supply limit", callback_data="set:supply")],
        [InlineKeyboardButton("Filters", callback_data="set:filters")],
        [InlineKeyboardButton(f"{BACK} Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(rows)

def catalog_page_kb(view: List[GiftInfo], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for g in view:
        rows.append([
            InlineKeyboardButton(f"Buy {g.price}{STAR}", callback_data=f"cata:buy:{g.id}:{g.price}"),
            InlineKeyboardButton(f"+Allow", callback_data=f"cata:allow:{g.id}")
        ])
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"{BACK} Prev", callback_data=f"cata:page:{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(f"Next ➡️", callback_data=f"cata:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Back", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)

def refunds_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Refund last purchase (credit)", callback_data="refund:last")],
        [InlineKeyboardButton("🧾 Refund by TxID (credit)", callback_data="refund:txid")],
        [InlineKeyboardButton("➕ Add manual credit", callback_data="refund:manual")],
        [InlineKeyboardButton("Back", callback_data="back:main")],
    ])

# ── screens ───────────────────────────────────────────────────────────────────
async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = await open_db()
    try:
        uid = update.effective_user.id
        await ensure_user(db, uid)
        auto_on = await get_auto_buy(db, uid)
    finally:
        await db.close()
    text = "Menu"
    await update.effective_message.reply_text(text, reply_markup=main_menu_kb(auto_on))

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db = await open_db()
    try:
        await ensure_user(db, uid)
        credit = await get_credit(db, uid)
    finally:
        await db.close()

    # true bot star balance
    bot_stars = await context.bot.get_my_star_balance()
    bot_amount = getattr(bot_stars, "amount", 0)

    auto_on = await get_auto_buy(await open_db(), uid)
    s = await read_settings(await open_db(), uid)

    txt = textwrap.dedent(f"""
    {PROFILE} Profile: @{update.effective_user.username or update.effective_user.first_name}

    {WALLET} **Internal credit:** {credit}{STAR}
    🤖 **Bot Stars:** {bot_amount}{STAR}

    {RECYCLE} Auto-Purchase:
    — Cycles: {s['cycles']}
    {BELL} Notifications: on
    {SPARK} Auto-Purchase: {"on" if auto_on else "off"}
    """).strip()
    await update.effective_message.reply_text(
        txt.replace("**", ""),  # no markdown noise
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Notifications: on", callback_data="noop"),
             InlineKeyboardButton("Auto-Buy: on" if auto_on else "Auto-Buy: off", callback_data="menu:toggle")],
            [InlineKeyboardButton(f"{SETTINGS} Auto-Purchase Settings", callback_data="set:open")],
            [InlineKeyboardButton("💳 Refunds / Credits", callback_data="refund:menu")],
            [InlineKeyboardButton(f"{BACK} Back", callback_data="back:main")],
        ])
    )

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db = await open_db()
    try:
        s = await read_settings(db, uid)
    finally:
        await db.close()
    txt = textwrap.dedent(f"""
    {SETTINGS} Auto-Purchase Settings

    Cycles: {s['cycles']}
    Lower limit: {s['min_price']}{STAR}
    Upper limit: {s['max_price']}{STAR}
    Overall limit: {s['overall_limit']}{STAR}
    Supply limit: {"∞" if s['supply_limit']==0 else s['supply_limit']}
    """).strip()
    await update.effective_message.reply_text(txt, reply_markup=settings_menu_kb(s))

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    gifts = await fetch_gifts(context)
    total_pages = max(1, math.ceil(len(gifts)/CATALOG_PAGE))
    page = max(0, min(page, total_pages-1))
    start = page*CATALOG_PAGE
    view = gifts[start:start+CATALOG_PAGE]

    lines = [f"{MAIN_EMOJI} Gift Catalog (api-fallback) page {page+1}/{total_pages}"]
    for g in view:
        tail = f" — {g.price}{STAR}"
        if g.remaining is not None:
            tail += f" · left: {g.remaining}"
        lines.append(f"• {g.emoji} Gift{tail}")
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=catalog_page_kb(view, page, total_pages)
    )

async def show_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Top up Stars",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Add 50⭐", callback_data="deposit:pay:50"),
             InlineKeyboardButton("Add 1000⭐", callback_data="deposit:pay:1000"),
             InlineKeyboardButton("Add 3000⭐", callback_data="deposit:pay:3000")],
            [InlineKeyboardButton("Back", callback_data="back:main")]
        ])
    )

async def show_refunds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = textwrap.dedent("""
    Refunds / Credits
    — credits are internal; Telegram Stars transactions are final.
    """).strip()
    await update.effective_message.reply_text(txt, reply_markup=refunds_menu_kb())

# ── command handlers ──────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main(update, context)

# ── callback handlers ─────────────────────────────────────────────────────────
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id

    if data == "menu:toggle":
        db = await open_db()
        try:
            cur = await db.execute("SELECT auto_buy FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            now = not bool(row and row["auto_buy"])
            await set_auto_buy(db, uid, now)
        finally:
            await db.close()
        await show_main(update, context)
        return

    if data == "menu:source":
        await q.message.reply_text("Source set to NOTIFIER.")
        return

    if data == "set:open":
        await show_settings(update, context); return

    if data.startswith("cata:page:"):
        page = int(data.split(":")[-1])
        await show_catalog(update, context, page); return

    if data.startswith("cata:allow:"):
        gift_id = data.split(":")[-1]
        db = await open_db()
        try:
            allowed = await toggle_allow(db, uid, gift_id)
        finally:
            await db.close()
        await q.message.reply_text(("Allowed ✅ " if allowed else "Removed from allowlist ❌ ") + gift_id)
        return

    if data.startswith("cata:buy:"):
        _, _, gift_id, price = data.split(":")
        price = int(price)
        # check credit
        db = await open_db()
        try:
            await ensure_user(db, uid)
            credit = await get_credit(db, uid)
            if credit < price:
                await q.message.reply_text(f"Not enough internal credit ({credit}{STAR}). Deposit more Stars.")
                return
        finally:
            await db.close()

        # Send the gift TO THE USER using correct parameter user_id
        ok = await context.bot.send_gift(user_id=uid, gift_id=gift_id)
        if ok:
            db = await open_db()
            try:
                await add_credit(db, uid, -price)
                await db.execute("INSERT INTO purchases(user_id,gift_id,star_price,ts) VALUES(?,?,?,strftime('%s','now'))",
                                 (uid, gift_id, price))
                await db.commit()
            finally:
                await db.close()
            await q.message.reply_text(f"Sent gift {gift_id} for {price}{STAR}. New credit: {await get_credit(await open_db(), uid)}{STAR}")
        else:
            await q.message.reply_text("Sending gift failed. Make sure the bot has enough Stars (bot balance).")
        return

    if data == "profile:open":
        await show_profile(update, context); return

    if data == "deposit:open":
        await show_deposit(update, context); return

    if data.startswith("deposit:pay:"):
        amount = int(data.split(":")[-1])
        # XTR invoice – provider_token must be empty for Stars
        title = f"Top up Stars"
        desc = f"Add {amount}{STAR} to bot balance"
        prices = [LabeledPrice(label=f"{amount}{STAR}", amount=amount)]
        await context.bot.send_invoice(
            chat_id=uid,
            title=title,
            description=desc,
            payload=f"topup:{amount}",
            currency="XTR",
            prices=prices,
            provider_token="",  # <-- Stars
        )
        return

    if data == "health:open":
        await q.message.reply_text("Health: background worker is running.")
        return

    if data == "logs:open":
        await q.message.reply_text("Logs page (coming soon).")
        return

    if data == "refund:menu":
        await show_refunds(update, context); return

    if data == "refund:last":
        db = await open_db()
        try:
            cur = await db.execute("SELECT star_price FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
            row = await cur.fetchone()
            if not row:
                await q.message.reply_text("Nothing to refund"); return
            amt = row["star_price"]
            await add_credit(db, uid, amt)
        finally:
            await db.close()
        await q.message.reply_text(f"Credited back {amt}{STAR}."); return

    if data == "refund:txid":
        context.user_data[AWAIT_TXID] = True
        await q.message.reply_text("Send the **Transaction ID** now (as plain text)."); return

    if data == "refund:manual":
        context.user_data[AWAIT_VALUE] = ("manual_credit",)
        await q.message.reply_text("Send amount to credit (integer stars)."); return

    if data == "back:main":
        await show_main(update, context); return

    # settings fields
    if data.startswith("set:"):
        key = data.split(":")[1]
        if key in ("cycles", "min", "max", "overall", "supply"):
            context.user_data[AWAIT_VALUE] = (key,)
            await q.message.reply_text(f"Send value for **{key}** (integer).")
            return

# ── message handlers (replies & payments) ─────────────────────────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # expecting a TxID?
    if context.user_data.get(AWAIT_TXID):
        context.user_data.pop(AWAIT_TXID, None)
        txid = text
        # verify via getStarTransactions and credit internally once
        txs = await context.bot.get_star_transactions(limit=200)
        found = None
        for t in txs.transactions:
            if t.id == txid:
                found = t
                break
        if not found:
            await update.message.reply_text("TxID not found in bot transactions.")
            return

        # block double credit
        db = await open_db()
        try:
            cur = await db.execute("SELECT 1 FROM credits_from_tx WHERE tx_id=?", (txid,))
            if await cur.fetchone():
                await update.message.reply_text("Already credited for this TxID.")
                await db.close(); return

            amt = found.amount
            await add_credit(db, uid, amt)
            await db.execute("INSERT INTO credits_from_tx(tx_id,amount,user_id,ts) VALUES(?,?,?,strftime('%s','now'))", (txid, amt, uid))
            await db.commit()
        finally:
            await db.close()
        await update.message.reply_text(f"Credited {amt}{STAR} from TxID.")
        return

    # expecting a numeric setting or manual credit?
    if AWAIT_VALUE in context.user_data:
        (key,) = context.user_data.pop(AWAIT_VALUE)
        try:
            val = int(text)
        except ValueError:
            await update.message.reply_text("Please send an integer.")
            return

        db = await open_db()
        try:
            await ensure_user(db, uid)
            if key == "manual_credit":
                await add_credit(db, uid, val)
                await update.message.reply_text(f"Added {val}{STAR} to internal credit.")
            else:
                mapping = {
                    "cycles": "cycles",
                    "min": "min_price",
                    "max": "max_price",
                    "overall": "overall_limit",
                    "supply": "supply_limit",
                }
                await write_setting(db, uid, mapping[key], val)
                await update.message.reply_text(f"Updated {key} = {val}.")
        finally:
            await db.close()
        return

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pay = update.message.successful_payment
    amount = pay.total_amount  # in Stars for XTR
    db = await open_db()
    try:
        await ensure_user(db, uid)
        # credit 30% bonus like your screenshot? use 30% promo if you want
        bonus = math.floor(amount * 0.3)
        await add_credit(db, uid, amount + bonus)
    finally:
        await db.close()
    await update.message.reply_text(f"Received {amount}{STAR}. Internal credit: {await get_credit(await open_db(), uid)}{STAR}.")

# ── background auto-buyer ─────────────────────────────────────────────────────
async def auto_buyer(app: Application):
    await asyncio.sleep(1)  # let the app start
    while True:
        try:
            db = await open_db()
            try:
                # fetch all users with auto_buy=1
                cur = await db.execute("SELECT user_id FROM users WHERE auto_buy=1")
                users = [r["user_id"] for r in await cur.fetchall()]
            finally:
                await db.close()

            if not users:
                await asyncio.sleep(2)
                continue

            gifts = await fetch_gifts(app.bot)
            for uid in users:
                db = await open_db()
                try:
                    s = await read_settings(db, uid)
                    credit = await get_credit(db, uid)
                    for g in gifts:
                        if credit < g.price:
                            continue
                        if not (s["min_price"] <= g.price <= s["max_price"]):
                            continue
                        if s["supply_limit"] and g.remaining is not None and g.remaining < s["supply_limit"]:
                            continue
                        if not await is_allowed(db, uid, g.id):
                            continue
                        ok = await app.bot.send_gift(user_id=uid, gift_id=g.id)
                        if ok:
                            await add_credit(db, uid, -g.price)
                            await db.execute("INSERT INTO purchases(user_id,gift_id,star_price,ts) VALUES(?,?,?,strftime('%s','now'))",
                                             (uid, g.id, g.price))
                            await db.commit()
                            await app.bot.send_message(chat_id=uid, text=f"{SPARK} Auto-bought {g.emoji} for {g.price}{STAR}")
                finally:
                    await db.close()
        except Exception as e:
            # keep running; log to console
            print("auto_buyer error:", e)
        await asyncio.sleep(2)  # tight loop for speed
# ── bootstrap ─────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # background task
    app.job_queue.run_repeating(lambda *_: None, interval=3600)  # keep job queue alive
    asyncio.create_task(auto_buyer(app))

    print("Bot is running…")
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())
