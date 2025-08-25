import asyncio
import csv
import json
import logging
import os
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
    FSInputFile,
)
from dotenv import load_dotenv

# ───────────────────────── Config ─────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "./beast.sqlite3")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing in .env")

# Admins (private bot)
ADMINS = {7940894807, 5770074932}

# Deposit rules (Stars/XTR)
MIN_XTR, MAX_XTR = 15, 200000

# Packs grid
PACKS = [1, 2, 3, 5, 10, 20, 30, 50, 75, 100]

# Emojis set
E = dict(
    bolt="⚡", ok="✅", bad="❌", gear="⚙️", gift="🎁",
    profile="🧑‍💼", deposit="⭐", logs="📄", health="🩺",
    back="◀️", rocket="🚀", recycle="♻️", wallet="👛",
    warn="🚨", lock="🔒", support="🆘"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s :: %(message)s")
log = logging.getLogger("beast")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ───────────────────────── DB ─────────────────────────
def db():
    # IMPORTANT: return the connection (do NOT await here)
    return aiosqlite.connect(DB_PATH)

async def ensure_column(con, table: str, column: str, decl: str):
    cur = await con.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in await cur.fetchall()]
    if column not in cols:
        await con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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
        await con.execute("""
            CREATE TABLE IF NOT EXISTS gifts(
                gid      TEXT PRIMARY KEY,
                emoji    TEXT NOT NULL,
                name     TEXT NOT NULL,
                price    INTEGER NOT NULL,
                active   INTEGER NOT NULL DEFAULT 1,
                created  INTEGER NOT NULL
            )
        """)
        # Migrations (new columns)
        await ensure_column(con, "users", "per_drop_max", "INTEGER DEFAULT 1")
        await ensure_column(con, "users", "gift_ttl_hours", "INTEGER DEFAULT 24")
        await ensure_column(con, "users", "spent_day", "INTEGER DEFAULT 0")
        await con.commit()

# User defaults
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
    # new
    "per_drop_max": 1,
    "gift_ttl_hours": 24,
    "spent_day": 0,
}

# User helpers
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

async def db_all_users() -> List[int]:
    async with db() as con:
        cur = await con.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]

async def db_all_notifier_ids() -> List[int]:
    async with db() as con:
        cur = await con.execute("SELECT DISTINCT notifier_id FROM users WHERE notifier_id IS NOT NULL")
        rows = await cur.fetchall()
        out = []
        for r in rows:
            try: out.append(int(r[0]))
            except: pass
        return out

async def db_log(uid: Optional[int], kind: str, message: str):
    async with db() as con:
        await con.execute(
            "INSERT INTO logs(user_id, kind, message, ts) VALUES(?,?,?,?)",
            (uid, kind, message, int(datetime.now(tz=timezone.utc).timestamp()))
        )
        await con.commit()

async def db_last_logs(uid: int, limit=12) -> List[Tuple[str,str]]:
    async with db() as con:
        cur = await con.execute(
            "SELECT kind, message FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit)
        )
        return await cur.fetchall()

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
        if changed: await db_save_user(uid, u)
    return u

async def set_user(uid: int, **updates):
    u = await get_user(uid); u.update(updates)
    await db_save_user(uid, u); return u

# Gifts (dynamic)
async def gifts_active() -> List[dict]:
    async with db() as con:
        cur = await con.execute("SELECT gid, emoji, name, price FROM gifts WHERE active=1 ORDER BY created DESC")
        rows = await cur.fetchall()
        return [dict(gid=r[0], emoji=r[1], name=r[2], price=r[3]) for r in rows]

async def gift_get(gid: str) -> Optional[dict]:
    async with db() as con:
        cur = await con.execute("SELECT gid, emoji, name, price, active, created FROM gifts WHERE gid=?", (gid,))
        r = await cur.fetchone()
        if not r: return None
        return dict(gid=r[0], emoji=r[1], name=r[2], price=r[3], active=bool(r[4]), created=r[5])

async def gift_upsert(gid: str, emoji: str, name: str, price: int, active: int = 1):
    async with db() as con:
        now = int(datetime.now(tz=timezone.utc).timestamp())
        await con.execute("""
            INSERT INTO gifts(gid, emoji, name, price, active, created)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(gid) DO UPDATE SET emoji=excluded.emoji, name=excluded.name,
                price=excluded.price, active=excluded.active
        """, (gid, emoji, name, price, active, now))
        await con.commit()

async def gifts_clear():
    async with db() as con:
        await con.execute("DELETE FROM gifts")
        await con.commit()

async def gift_gc(ttl_hours: int) -> int:
    """Deactivate gifts older than ttl_hours."""
    cutoff = int(datetime.now(tz=timezone.utc).timestamp()) - ttl_hours * 3600
    async with db() as con:
        cur = await con.execute("UPDATE gifts SET active=0 WHERE created < ? AND active=1", (cutoff,))
        await con.commit()
        return cur.rowcount

async def gift_gc_loop():
    while True:
        try:
            # collect unique TTLs among users (use smallest)
            async with db() as con:
                cur = await con.execute("SELECT MIN(gift_ttl_hours) FROM users")
                row = await cur.fetchone()
                ttl = row[0] if row and row[0] else 24
            n = await gift_gc(int(ttl))
            if n:
                await db_log(None, "GC", f"Archived {n} gift(s)")
        except Exception as e:
            log.warning("gift_gc_loop error: %s", e)
        await asyncio.sleep(600)  # every 10min

# Support memory
async def support_push(uid: int, user_text: str, bot_text: str):
    async with db() as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS support_threads(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                msg_in TEXT NOT NULL,
                msg_out TEXT NOT NULL,
                ts INTEGER NOT NULL
            )""")
        await con.execute(
            "INSERT INTO support_threads(user_id, msg_in, msg_out, ts) VALUES(?,?,?,?)",
            (uid, user_text, bot_text, int(datetime.now(tz=timezone.utc).timestamp()))
        )
        await con.commit()

async def support_tail(uid: int, limit=5) -> List[Tuple[str,str]]:
    async with db() as con:
        cur = await con.execute(
            "SELECT msg_in, msg_out FROM support_threads WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit)
        )
        return await cur.fetchall()

# ───────────────────────── FSM ─────────────────────────
class Entry(StatesGroup):
    field = State()     # settings fields
    deposit = State()   # custom deposit
    support = State()   # support free text
    addgift = State()   # add gift parser "emoji name | price"

# ───────────────────────── UI helpers ─────────────────────────
def pretty_inf(n: int, suffix="⭐"):
    return f"∞{suffix if suffix else ''}" if not n or n < 0 else f"{n}{suffix}"

def main_menu_kb(u: dict) -> InlineKeyboardMarkup:
    auto = "ON ✅" if u.get("auto_on") else "OFF ❌"
    rows = [
        [InlineKeyboardButton(text=f"{E['bolt']} Auto-Buy: {auto}", callback_data="auto:toggle"),
         InlineKeyboardButton(text=f"{E['health']} Health", callback_data="menu:health")],
        [InlineKeyboardButton(text=f"{E['gear']} Auto-Purchase Settings", callback_data="menu:settings")],
        [InlineKeyboardButton(text=f"{E['gift']} Gift Catalog", callback_data="menu:catalog")],
        [InlineKeyboardButton(text=f"{E['profile']} Profile", callback_data="menu:profile"),
         InlineKeyboardButton(text=f"{E['deposit']} Deposit", callback_data="menu:deposit")],
        [InlineKeyboardButton(text=f"{E['rocket']} Test Event", callback_data="menu:test"),
         InlineKeyboardButton(text=f"{E['logs']} Logs", callback_data="menu:logs")],
        [InlineKeyboardButton(text=f"{E['support']} Support", callback_data="menu:support")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def settings_view(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    txt = (
        f"{E['gear']} <b>Auto-Purchase Settings</b>\n"
        f"Cycles: <b>{u.get('cycles',1)}</b>\n"
        f"Per-drop max: <b>{u.get('per_drop_max',1)}</b>\n"
        f"Lower limit: <b>{pretty_inf(u.get('lower_limit',0))}</b>\n"
        f"Upper limit: <b>{pretty_inf(u.get('upper_limit',0))}</b>\n"
        f"Overall limit: <b>{pretty_inf(u.get('overall_limit',0))}</b>\n"
        f"Supply limit: <b>{pretty_inf(u.get('supply_limit',0), suffix='')}</b>\n"
        f"Daily budget: <b>{pretty_inf(u.get('daily_budget',0))}</b>\n"
        f"Gift TTL: <b>{u.get('gift_ttl_hours',24)}h</b>"
    )
    rows = [
        [InlineKeyboardButton(text="Cycles", callback_data="set:cycles"),
         InlineKeyboardButton(text="Per-drop max", callback_data="set:perdrop")],
        [InlineKeyboardButton(text="Lower limit", callback_data="set:lower"),
         InlineKeyboardButton(text="Upper limit", callback_data="set:upper")],
        [InlineKeyboardButton(text="Overall limit", callback_data="set:overall"),
         InlineKeyboardButton(text="Supply limit", callback_data="set:supply")],
        [InlineKeyboardButton(text="Daily budget", callback_data="set:daily"),
         InlineKeyboardButton(text="Gift TTL (h)", callback_data="set:giftttl")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)

def catalog_list_kb(items: List[dict], page=0, per_page=6):
    start = page * per_page
    chunk = items[start:start+per_page]
    title = f"{E['gift']} <b>Catalog</b>"
    rows = []
    if not chunk:
        rows.append([InlineKeyboardButton(text="No gifts yet — Add one", callback_data="cat:add")])
    else:
        for g in chunk:
            rows.append([InlineKeyboardButton(
                text=f"{g['emoji']} {g['name']} — {g['price']}⭐", callback_data=f"cat:sel:{g['gid']}")])
        nav = []
        if start > 0: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"cat:page:{page-1}"))
        if start + per_page < len(items): nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"cat:page:{page+1}"))
        if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")])
    return title, InlineKeyboardMarkup(inline_keyboard=rows)

def catalog_detail_kb(g: dict):
    header = f"{E['gift']} <b>{g['emoji']} {g['name']}</b> <i>({g['price']}⭐ each)</i>"
    rows, row = [], []
    for i, n in enumerate(PACKS, 1):
        row.append(InlineKeyboardButton(text=f"{n}× for {n*g['price']}⭐", callback_data=f"buy:{g['gid']}:{n}"))
        if i % 2 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu:catalog")])
    return header, InlineKeyboardMarkup(inline_keyboard=rows)

def deposit_kb():
    rows = [
        [InlineKeyboardButton(text="Add 50⭐", callback_data="deposit:50"),
         InlineKeyboardButton(text="Add 1000⭐", callback_data="deposit:1000"),
         InlineKeyboardButton(text="Add 3000⭐", callback_data="deposit:3000")],
        [InlineKeyboardButton(text="Custom amount…", callback_data="deposit:custom")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ───────────────────────── Guards ─────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMINS

# ───────────────────────── Commands (list) ─────────────────────────
COMMANDS_TEXT = (
    "<b>Commands</b>\n"
    "/start — open menu\n"
    "/menu — open menu\n"
    "/help — show commands\n"
    "/support — talk to support (short, remembers thread)\n"
    "/deposit — deposit Stars\n"
    "/source &lt;channel_id&gt; — set notifier channel (bot must be admin)\n"
    "/addgift — add a gift (emoji name | price)\n"
    "/listgifts — list active gifts\n"
    "/cleargifts — delete all gifts\n"
    "/giftgc — archive stale gifts now\n"
    "/exportlogs — export last 200 logs (CSV)\n"
    "/stats — show stats\n"
    "/ping — check bot\n"
)

# ───────────────────────── Start / Help / Ping ─────────────────────────
@dp.message(CommandStart())
async def cmd_start(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer(f"{E['lock']} Private bot."); return
    u = await get_user(m.from_user.id)
    await m.answer("Menu", reply_markup=main_menu_kb(u))

@dp.message(Command("help"))
async def cmd_help(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer(COMMANDS_TEXT)

@dp.message(Command("ping"))
async def cmd_ping(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("pong")

@dp.message(Command("stats")))
async def cmd_stats(m: Message):
    if not is_admin(m.from_user.id): return
    u = await get_user(m.from_user.id)
    glist = await gifts_active()
    await m.answer(
        f"Users: {len(await db_all_users())}\n"
        f"Active gifts: {len(glist)}\n"
        f"Credit: {u.get('credit',0)}⭐\nAuto: {'ON' if u.get('auto_on') else 'OFF'}\n"
        f"Per-drop max: {u.get('per_drop_max',1)} | TTL: {u.get('gift_ttl_hours',24)}h"
    )

# ───────────────────────── Support (short, remembers) ─────────────────────────
@dp.callback_query(F.data == "menu:support")
async def menu_support(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.support)
    await cq.message.edit_text(
        f"{E['support']} <b>Support</b>\n"
        "Send one message with your issue. I’ll reply short.\n",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")]
        ])
    )

@dp.message(Command("support"))
async def cmd_support(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.set_state(Entry.support)
    await m.answer("Send your issue (one message). Short reply, I’ll remember context.")

@dp.message(Entry.support)
async def support_flow(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    summary = "Noted. Checking logs & limits. Reply if urgent."
    await support_push(m.from_user.id, text, summary)
    tail = await support_tail(m.from_user.id, 3)
    if tail:
        bullets = "\n".join(f"• {t[0][:48]}…" for t in tail[::-1])
        await m.answer(f"{summary}\nRecent:\n{bullets}")
    else:
        await m.answer(summary)
    await state.clear()

# ───────────────────────── Profile / Health / Logs ─────────────────────────
@dp.callback_query(F.data == "menu:profile")
async def menu_profile(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    txt = (
        f"{E['profile']} <b>Profile</b>\n"
        f"{E['wallet']} Internal credit: <b>{u.get('credit',0)}⭐</b>\n"
        f"♻️ Auto cycles: <b>{u.get('cycles',1)}</b>\n"
        f"Per-drop max: <b>{u.get('per_drop_max',1)}</b>\n"
        f"Daily budget: <b>{pretty_inf(u.get('daily_budget',0))}</b>\n"
        f"{E['bolt']} Auto: <b>{'on' if u.get('auto_on') else 'off'}</b>"
    )
    await cq.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Refunds / Credits", callback_data="menu:refunds")],
                         [InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")]]
    ))

@dp.callback_query(F.data == "menu:health")
async def menu_health(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    ok_db = "✅"
    ok_notifier = "✅" if u.get("notifier_id") else "❌"
    rows = await db_last_logs(cq.from_user.id, 6)
    lines = [
        f"{E['health']} <b>Health</b>",
        f"• Database: {ok_db}",
        f"• Notifier: {ok_notifier}",
        f"• Auto-Buy: {'✅ ON' if u.get('auto_on') else '❌ OFF'}",
        f"• Credit: {u.get('credit',0)}⭐",
        "",
        "Recent logs:" if rows else "No logs yet."
    ]
    for k, msg in rows:
        lines.append(f"• <code>{k}</code> — {msg}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Test Notifier", callback_data="health:test")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")]
    ])
    await cq.message.edit_text("\n".join(lines), reply_markup=kb)

@dp.callback_query(F.data == "health:test")
async def health_test_notifier(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    ch = u.get("notifier_id")
    if not ch:
        await cq.answer("No notifier set. Use /source <id>.", show_alert=True); return
    try:
        await bot.send_message(ch, "🧪 Notifier test from Beast: OK.")
        await cq.answer("Posted ✅", show_alert=False)
    except Exception as e:
        await cq.answer(f"Failed: {e}", show_alert=True)

@dp.callback_query(F.data == "menu:logs")
async def menu_logs(cq: types.CallbackQuery):
    rows = await db_last_logs(cq.from_user.id, 20)
    out = (f"{E['logs']} <b>Logs</b>\n" + "\n".join(f"• <code>{k}</code> — {msg}" for k, msg in rows)) if rows else "— empty —"
    await cq.message.edit_text(out, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")]]
    ))

# ───────────────────────── Settings ─────────────────────────
@dp.callback_query(F.data == "menu:settings")
async def menu_settings(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    txt, kb = settings_view(u)
    await cq.message.edit_text(txt, reply_markup=kb)

ENTRY_MAP = {
    "set:cycles":   ("Send number of cycles (0 = ∞).", "cycles"),
    "set:perdrop":  ("Send MAX pieces per drop (>=1).", "per_drop_max"),
    "set:lower":    ("Send LOWER price limit in ⭐ (0 = none).", "lower_limit"),
    "set:upper":    ("Send UPPER price limit in ⭐ (0 = none).", "upper_limit"),
    "set:overall":  ("Send OVERALL limit in ⭐ (0 = ∞).", "overall_limit"),
    "set:supply":   ("Send SUPPLY limit in pcs (0 = ∞).", "supply_limit"),
    "set:daily":    ("Send DAILY budget in ⭐ (0 = ∞).", "daily_budget"),
    "set:giftttl":  ("Send Gift TTL in hours (e.g., 24).", "gift_ttl_hours"),
}

class EntryFields(StatesGroup):
    pass

@dp.callback_query(F.data.in_(ENTRY_MAP.keys()))
async def ask_field(cq: types.CallbackQuery, state: FSMContext):
    prompt, field = ENTRY_MAP[cq.data]
    await state.set_state(Entry.field)
    await state.update_data(field=field)
    await cq.message.edit_text(prompt, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="menu:settings")]]
    ))

@dp.message(Entry.field)
async def set_field(m: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    try:
        v = int(m.text.strip()); v = max(0, v)
        if field == "per_drop_max" and v < 1: v = 1
    except Exception:
        await m.answer("Numbers only. Try again."); return
    await set_user(m.from_user.id, **{field: v})
    await state.clear()
    u = await get_user(m.from_user.id)
    txt, kb = settings_view(u)
    await m.answer("✅ Saved.\n\n" + txt, reply_markup=kb)

# ───────────────────────── Catalog (dynamic) ─────────────────────────
@dp.callback_query(F.data == "menu:catalog")
async def catalog_root(cq: types.CallbackQuery):
    items = await gifts_active()
    title, kb = catalog_list_kb(items, 0)
    await cq.message.edit_text(title, reply_markup=kb)

@dp.callback_query(F.data.startswith("cat:page:"))
async def catalog_page(cq: types.CallbackQuery):
    page = int(cq.data.split(":")[2])
    items = await gifts_active()
    title, kb = catalog_list_kb(items, page)
    await cq.message.edit_text(title, reply_markup=kb)

@dp.callback_query(F.data == "cat:add")
async def catalog_add_btn(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(Entry.addgift)
    await cq.message.edit_text(
        "Send gift like:\n<code>🧸 Teddy | 15</code>\n(emoji name | price)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:catalog")]
        ])
    )

@dp.message(Entry.addgift)
async def addgift_input(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Format: <code>🧸 Teddy | 15</code>"); return
    first = parts[0]
    emoji = first.split()[0]
    name = first[len(emoji):].strip() or "Gift"
    price = int(parts[1])
    gid = re.sub(r"\s+", "_", name.lower())[:40]
    await gift_upsert(gid, emoji, name, price, 1)
    await state.clear()
    await m.answer(f"Added: {emoji} {name} — {price}⭐")

@dp.callback_query(F.data.startswith("cat:sel:"))
async def catalog_detail(cq: types.CallbackQuery):
    gid = cq.data.split(":")[2]
    g = await gift_get(gid)
    if not g or not g["active"]:
        await cq.answer("Gift not found.", show_alert=True); return
    header, kb = catalog_detail_kb(g)
    await cq.message.edit_text(header, reply_markup=kb)

# ───────────────────────── Manual Buy ─────────────────────────
def today_key() -> int:
    d = date.today()
    return d.year*10000 + d.month*100 + d.day

async def try_debit(uid: int, cost: int) -> tuple[bool, str]:
    u = await get_user(uid)
    # daily rollover
    if u.get("spent_day", 0) != today_key():
        u["spent_day"] = today_key()
        u["spent_today"] = 0
    if u["credit"] < cost:
        await db_save_user(uid, u)
        return False, "Insufficient credit."
    if u.get("daily_budget", 0) and u.get("spent_today", 0) + cost > u["daily_budget"]:
        await db_save_user(uid, u)
        return False, "Daily budget exceeded."
    if u.get("overall_limit", 0) and u["overall_limit"] < cost:
        await db_save_user(uid, u)
        return False, "Overall limit reached."
    u["credit"] -= cost
    u["spent_today"] += cost
    if u.get("overall_limit", 0): u["overall_limit"] -= cost
    await db_save_user(uid, u)
    return True, "OK"

async def try_send_gift(to_user_id: int, stars_each: int, reason: str) -> Tuple[bool,str]:
    payload = {"chat_id": to_user_id, "gift": json.dumps({"star_count": stars_each}), "text": reason}
    try:
        res = await bot.request("sendGift", payload)
        return (True, "OK") if res else (False, "Unknown API response")
    except Exception as e:
        return False, str(e)

@dp.callback_query(F.data.startswith("buy:"))
async def on_buy(cq: types.CallbackQuery):
    # data: buy:<gid>:<count>
    _, gid, count_s = cq.data.split(":")
    g = await gift_get(gid)
    if not g or not g["active"]:
        await cq.answer("Gift unavailable", show_alert=True); return
    count = int(count_s); total = g["price"] * count

    ok, reason = await try_debit(cq.from_user.id, total)
    if not ok: await cq.answer(reason, show_alert=True); return

    sent = 0
    for _ in range(count):
        ok2, why = await try_send_gift(cq.from_user.id, g["price"], "Manual")
        if not ok2:
            await db_log(cq.from_user.id, "BUY_FAIL", why); break
        sent += 1
        await asyncio.sleep(0.05)

    await db_log(cq.from_user.id, "BUY", f"{g['name']}×{sent}/{count} cost={total}")
    await cq.answer(("Purchased ✅" if sent == count else f"Partial: {sent}/{count}"), show_alert=(sent != count))

# ───────────────────────── Deposit (Stars) ─────────────────────────
@dp.callback_query(F.data == "menu:deposit")
async def menu_deposit(cq: types.CallbackQuery):
    await cq.message.edit_text("Top up Stars", reply_markup=deposit_kb())

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
    u = await get_user(m.from_user.id); u["credit"] += total
    await db_save_user(m.from_user.id, u)
    await db_log(m.from_user.id, "DEPOSIT", f"+{total}⭐ via Stars")
    await m.answer(f"Received <b>{total}⭐</b>. Internal credit: <b>{u['credit']}⭐</b>.")

@dp.callback_query(F.data.startswith("deposit:"))
async def dep_quick(cq: types.CallbackQuery, state: FSMContext):
    _, val = cq.data.split(":")
    if val == "custom":
        await state.set_state(Entry.deposit)
        await cq.message.edit_text(
            f"Send the Stars amount (e.g., 3300). Min {MIN_XTR}, Max {MAX_XTR}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="menu:deposit")]])
        ); return
    await send_invoice(cq.from_user.id, int(val))

@dp.message(Entry.deposit)
async def dep_custom(m: Message, state: FSMContext):
    try:
        amt = int(m.text.strip()); assert MIN_XTR <= amt <= MAX_XTR and amt % 5 == 0
    except Exception:
        await m.answer(f"Invalid amount. Use a multiple of 5 between {MIN_XTR} and {MAX_XTR}."); return
    await state.clear()
    await send_invoice(m.chat.id, amt)

# ───────────────────────── Refunds / Credits (internal) ─────────────────────────
@dp.callback_query(F.data == "menu:refunds")
async def refunds_menu(cq: types.CallbackQuery):
    txt = "Refunds / Credits — internal only (Telegram Stars are final)."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Refund last purchase (credit)", callback_data="credit:refund_last")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")]
    ])
    await cq.message.edit_text(txt, reply_markup=kb)

@dp.callback_query(F.data == "credit:refund_last")
async def do_refund(cq: types.CallbackQuery):
    await cq.answer("Nothing to refund", show_alert=True)

# ───────────────────────── Auto-Buy toggle ─────────────────────────
@dp.callback_query(F.data == "auto:toggle")
async def toggle_auto(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    flag = not u.get("auto_on"); await set_user(cq.from_user.id, auto_on=flag)
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=main_menu_kb(u))
    await cq.answer("Auto-Buy is now " + ("ON" if flag else "OFF"))

# ───────────────────────── Notifier: set + listener ─────────────────────────
@dp.message(Command("source"))
async def cmd_source(m: Message):
    if not is_admin(m.from_user.id): return
    parts = (m.text or "").split()
    if len(parts) != 2:
        await m.answer("Send: <code>/source -1001234567890</code>"); return
    try:
        ch_id = int(parts[1])
    except ValueError:
        await m.answer("Channel id must be an integer like -100…"); return
    await set_user(m.from_user.id, notifier_id=ch_id)
    try:
        await bot.send_message(ch_id, "✅ Beast is connected and watching for limited gift drops.")
        await m.answer("Source saved and confirmed ✅")
    except Exception as e:
        await m.answer(f"Saved, but couldn’t write to channel: <code>{e}</code>")

def parse_price_from_text(s: str) -> Optional[int]:
    s = s.lower()
    m = re.search(r"(\d{1,6})\s*(stars|star|⭐|xtr)", s)
    return int(m.group(1)) if m else None

def first_emoji(s: str) -> str:
    for ch in s.strip():
        if not ch.isalnum() and not ch.isspace():
            return ch
    return "💝"

@dp.channel_post()
async def on_channel_post(msg: Message):
    text = (msg.text or msg.caption or "")
    if "gift" in text.lower() and ("limited" in text.lower() or "drop" in text.lower()):
        price = parse_price_from_text(text) or 15
        name_line = text.splitlines()[0][:40]
        emoji = first_emoji(name_line)
        name = re.sub(r"[^A-Za-z0-9 ]", "", name_line).strip() or "Gift"
        gid = re.sub(r"\s+", "_", name.lower() + f"_{price}")[:40]
        await gift_upsert(gid, emoji, name, price, 1)
        for uid in await db_all_users():
            u = await get_user(uid)
            if u.get("auto_on") and u.get("notifier_id") == msg.chat.id:
                await try_auto_buy(uid)

# ───────────────────────── Test Event ─────────────────────────
@dp.callback_query(F.data == "menu:test")
async def menu_test(cq: types.CallbackQuery):
    items = await gifts_active()
    names = ", ".join(f"{g['emoji']} {g['name']}" for g in items) or "— none —"
    txt = f"Test Event\n— Simulate a limited drop.\n\nAvailable gifts now: <b>{len(items)}</b>\n{names}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Simulate drop (notify)", callback_data="test:notify")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back:main")]
    ])
    await cq.message.edit_text(txt, reply_markup=kb)

@dp.callback_query(F.data == "test:notify")
async def test_notify(cq: types.CallbackQuery):
    await cq.message.answer(f"{E['warn']} Limited gift detected (test) — will try to buy if Auto-Buy is ON.")
    await try_auto_buy(cq.from_user.id)

# ───────────────────────── Auto-buy engine ─────────────────────────
async def first_allowed_gift(u: dict) -> Optional[dict]:
    low = u.get("lower_limit", 0) or 0
    up  = u.get("upper_limit", 0) or 0
    items = await gifts_active()
    for g in items:
        if low and g["price"] < low: continue
        if up  and g["price"] > up:  continue
        return g
    return None

async def try_auto_buy(uid: int):
    u = await get_user(uid)
    if not u.get("auto_on"):
        await db_log(uid, "AUTO", "Skipped (auto off)"); return
    g = await first_allowed_gift(u)
    if not g:
        await db_log(uid, "AUTO", "No gift matches price filters"); return

    cycles = u.get("cycles", 1)
    permax = max(1, int(u.get("per_drop_max", 1)))
    qty = min(permax, cycles if cycles > 0 else permax)
    if u.get("supply_limit", 0):
        qty = min(qty, u["supply_limit"])
    total = g["price"] * qty

    ok, reason = await try_debit(uid, total)
    if not ok:
        await db_log(uid, "AUTO_FAIL", reason); return

    sent = 0
    for _ in range(qty):
        ok2, why = await try_send_gift(uid, g["price"], "Auto")
        if not ok2:
            await db_log(uid, "AUTO_FAIL", why); break
        sent += 1
        await asyncio.sleep(0.03)  # very fast but not spammy

    await db_log(uid, "AUTO_OK" if sent == qty else "AUTO_PARTIAL", f"{g['name']}×{sent}/{qty} cost={total}")

# ───────────────────────── Export logs / Gift GC / Back / Menu ─────────────────────────
@dp.message(Command("exportlogs"))
async def cmd_exportlogs(m: Message):
    if not is_admin(m.from_user.id): return
    async with db() as con:
        cur = await con.execute(
            "SELECT user_id, kind, message, ts FROM logs ORDER BY id DESC LIMIT 200"
        )
        rows = await cur.fetchall()
    path = "/tmp/logs.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["user_id","kind","message","ts"])
        for r in rows: w.writerow(r)
    await m.answer_document(FSInputFile(path), caption="Recent logs (CSV)")

@dp.message(Command("giftgc"))
async def cmd_giftgc(m: Message):
    if not is_admin(m.from_user.id): return
    u = await get_user(m.from_user.id)
    n = await gift_gc(int(u.get("gift_ttl_hours",24)))
    await m.answer(f"Archived {n} gift(s) older than {u.get('gift_ttl_hours',24)}h.")

@dp.callback_query(F.data == "back:main")
async def back_main(cq: types.CallbackQuery):
    u = await get_user(cq.from_user.id)
    await cq.message.edit_text("Menu", reply_markup=main_menu_kb(u))

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    if not is_admin(m.from_user.id): 
        await m.answer(f"{E['lock']} Private bot."); return
    u = await get_user(m.from_user.id)
    await m.answer("Menu", reply_markup=main_menu_kb(u))

@dp.message(Command("deposit"))
async def cmd_deposit(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("Top up Stars", reply_markup=deposit_kb())

# Startup notifier
async def notify_channels_startup():
    for ch_id in await db_all_notifier_ids():
        try:
            await bot.send_message(ch_id, "✅ Beast is online and watching this channel.")
        except Exception as e:
            log.warning("Notify failed for %s: %s", ch_id, e)

# ───────────────────────── Run ─────────────────────────
async def main():
    await init_db()
    asyncio.create_task(gift_gc_loop())   # background GC for stale gifts
    await notify_channels_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
