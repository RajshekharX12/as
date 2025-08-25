# Dream bot — Stars Gifts autobuyer + notifier (final)
import asyncio, json, os, math, re, traceback
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp, aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# ───────── CONFIG
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

ADMIN_IDS = {7940894807, 5770074932}  # private bot

DB_PATH = "autogifts.db"
AUTO_LOOP_SEC   = 0.25   # faster loop
NOTIFY_LOOP_SEC = 1.0

E = {"gift":"🎁","heart":"💝","star":"⭐","ok":"✅","bad":"❌","back":"⬅️",
     "spark":"⚡","bell":"🔔","wallet":"👛","profile":"👤","logs":"📄","health":"🩺",
     "gear":"⚙️","recycle":"♻️","rocket":"🚀"}

# ───────── DB (create + migrate)
CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  credit  INTEGER NOT NULL DEFAULT 0,
  auto_on INTEGER NOT NULL DEFAULT 0,
  notify_on INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings(
  user_id INTEGER PRIMARY KEY,
  cycles INTEGER NOT NULL DEFAULT 1,
  min_price INTEGER NOT NULL DEFAULT 0,
  max_price INTEGER NOT NULL DEFAULT 0,
  overall_limit INTEGER NOT NULL DEFAULT 0,
  supply_limit  INTEGER NOT NULL DEFAULT 0,
  window TEXT NOT NULL DEFAULT ""
);

CREATE TABLE IF NOT EXISTS purchases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  gift_id TEXT NOT NULL,
  title TEXT,
  stars INTEGER NOT NULL,
  auto INTEGER NOT NULL DEFAULT 0,
  ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS spend_day(
  user_id INTEGER NOT NULL,
  ymd TEXT NOT NULL,
  spent INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, ymd)
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  level TEXT NOT NULL,
  msg TEXT NOT NULL
);
"""

REQUIRED_USERS = {
    "credit":     "INTEGER NOT NULL DEFAULT 0",
    "auto_on":    "INTEGER NOT NULL DEFAULT 0",
    "notify_on":  "INTEGER NOT NULL DEFAULT 1",
}
REQUIRED_SETTINGS = {
    "cycles":        "INTEGER NOT NULL DEFAULT 1",
    "min_price":     "INTEGER NOT NULL DEFAULT 0",
    "max_price":     "INTEGER NOT NULL DEFAULT 0",
    "overall_limit": "INTEGER NOT NULL DEFAULT 0",
    "supply_limit":  "INTEGER NOT NULL DEFAULT 0",
    "window":        "TEXT NOT NULL DEFAULT ''",
}

async def migrate(db: aiosqlite.Connection):
    async def ensure_cols(table: str, spec: Dict[str,str]):
        have = set()
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            async for r in cur:  # cid|name|type|notnull|dflt|pk
                have.add(r[1])
        for col, sql in spec.items():
            if col not in have:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sql}")
    await ensure_cols("users", REQUIRED_USERS)
    await ensure_cols("settings", REQUIRED_SETTINGS)
    await db.commit()

@asynccontextmanager
async def open_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        await db.executescript(CREATE_SQL)
        await migrate(db)
        yield db
    finally:
        await db.close()

# ───────── KV helpers (per user keys)
async def kv_set(key: str, value: dict):
    async with open_db() as db:
        await db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
        await db.commit()

async def kv_get(key: str) -> Optional[dict]:
    async with open_db() as db:
        async with db.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return json.loads(row["value"]) if row else None

# ───────── logging
async def log(level, msg):
    async with open_db() as db:
        await db.execute("INSERT INTO events(ts,level,msg) VALUES(?,?,?)",
                         (int(datetime.now().timestamp()), level, msg))
        await db.commit()

# ───────── user state
async def ensure_user(uid:int):
    async with open_db() as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)",(uid,))
        await db.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)",(uid,))
        await db.commit()

async def guser(uid:int)->dict:
    await ensure_user(uid)
    async with open_db() as db:
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
            u = dict(row) if row else {}
    u.setdefault("credit", 0)
    u.setdefault("auto_on", 0)
    u.setdefault("notify_on", 1)
    return u

async def credit_of(uid:int)->int:
    async with open_db() as db:
        async with db.execute("SELECT credit FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
            return int(row["credit"] if row else 0)

async def add_credit(uid:int, delta:int):
    async with open_db() as db:
        await db.execute("UPDATE users SET credit=MAX(0,credit+?) WHERE user_id=?", (delta,uid))
        await db.commit()

async def set_auto(uid:int, flag:bool):
    async with open_db() as db:
        await db.execute("UPDATE users SET auto_on=? WHERE user_id=?", (1 if flag else 0, uid))
        await db.commit()

async def set_notify(uid:int, flag:bool):
    async with open_db() as db:
        await db.execute("UPDATE users SET notify_on=? WHERE user_id=?", (1 if flag else 0, uid))
        await db.commit()

async def settings_of(uid:int)->dict:
    async with open_db() as db:
        async with db.execute("SELECT * FROM settings WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
            return dict(row)

async def set_setting(uid:int, key:str, val:int|str):
    async with open_db() as db:
        await db.execute(f"UPDATE settings SET {key}=? WHERE user_id=?", (val, uid))
        await db.commit()

def today_ymd()->str: return datetime.utcnow().strftime("%Y-%m-%d")

async def spent_today(uid:int)->int:
    async with open_db() as db:
        async with db.execute("SELECT spent FROM spend_day WHERE user_id=? AND ymd=?",(uid,today_ymd())) as cur:
            row = await cur.fetchone()
            return int(row["spent"] if row else 0)

async def add_spent(uid:int, delta:int):
    async with open_db() as db:
        await db.execute(
            "INSERT INTO spend_day(user_id, ymd, spent) VALUES (?,?,?) "
            "ON CONFLICT(user_id, ymd) DO UPDATE SET spent=spent+excluded.spent",
            (uid, today_ymd(), delta))
        await db.commit()

# ───────── Telegram raw calls
class TG:
    def __init__(self, token:str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.sess: Optional[aiohttp.ClientSession] = None
    async def _ensure(self):
        if not self.sess or self.sess.closed:
            self.sess = aiohttp.ClientSession()
    async def call(self, method:str, payload:Dict[str,Any]|None=None)->Any:
        await self._ensure()
        async with self.sess.post(f"{self.base}/{method}", json=payload or {}) as r:
            data = await r.json()
            if not data.get("ok"):
                raise RuntimeError(f"{method} failed: {data}")
            return data["result"]
    async def get_available_gifts(self)->List[Dict[str,Any]]:
        res = await self.call("getAvailableGifts"); return res.get("gifts", [])
    async def send_gift(self, user_id:int, gift_id:str, text:str="")->bool:
        await self.call("sendGift", {"user_id": user_id, "gift_id": gift_id, "text": text, "is_private": True})
        return True
    async def get_my_star_balance(self)->int:
        res = await self.call("getMyStarBalance"); return int(res.get("star_count", 0))

TGAPI = TG(BOT_TOKEN)

def normalize_g(g:Dict[str,Any])->Dict[str,Any]:
    gid = str(g.get("id") or g.get("gift_id") or "")
    title = str(g.get("title") or g.get("name") or "Gift")
    price = int(g.get("star_count") or (g.get("price") or {}).get("star_count") or g.get("price") or 0)
    emoji = g.get("emoji") or (g.get("sticker") or {}).get("emoji") or "🎁"
    rem = g.get("remaining_count")
    return {"gift_id": gid, "title": title, "star_count": price, "emoji": emoji, "remaining": rem}

async def gifts_from_api()->List[Dict[str,Any]]:
    try:
        raw = await TGAPI.get_available_gifts()
        return [normalize_g(x) for x in raw]
    except Exception as e:
        await log("ERROR", f"getAvailableGifts: {e}")
        return []

# ───────── Helpers
def guard(uid:int)->bool: return uid in ADMIN_IDS

# ───────── UI
router = Router()

def main_menu(auto_on: bool, notify_on: bool)->InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{E['spark']} Auto-Buy: {'ON '+E['ok'] if auto_on else 'OFF '+E['bad']}", callback_data="auto:toggle"),
         InlineKeyboardButton(text=f"{E['bell']} Notifier: {'ON '+E['ok'] if notify_on else 'OFF '+E['bad']}", callback_data="notify:toggle")],
        [InlineKeyboardButton(text=f"{E['health']} Health", callback_data="health:run")],
        [InlineKeyboardButton(text=f"{E['gear']} Auto-Purchase Settings", callback_data="settings:menu")],
        [InlineKeyboardButton(text=f"{E['gift']} Gift Catalog", callback_data="cata:0")],
        [InlineKeyboardButton(text=f"{E['profile']} Profile", callback_data="profile:open"),
         InlineKeyboardButton(text="⭐ Deposit", callback_data="topup:menu")],
        [InlineKeyboardButton(text=f"{E['rocket']} Test Event", callback_data="test:menu"),
         InlineKeyboardButton(text=f"{E['logs']} Logs", callback_data="logs:open")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_home()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="home")]])

async def deny(obj)->bool:
    uid = obj.from_user.id
    if not guard(uid):
        try:
            if isinstance(obj, Message): await obj.answer("🔒 Private bot.")
            else: await obj.answer("🔒 Private bot.", show_alert=True)
        except: pass
        return True
    return False

# ───────── START & HOME
@router.message(CommandStart())
async def start(m: Message):
    if await deny(m): return
    await ensure_user(m.from_user.id)
    u = await guser(m.from_user.id)
    await m.answer("Menu", reply_markup=main_menu(bool(u.get("auto_on",0)), bool(u.get("notify_on",1))))

@router.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    if await deny(c): return
    u = await guser(c.from_user.id)
    await c.message.edit_text("Menu", reply_markup=main_menu(bool(u.get("auto_on",0)), bool(u.get("notify_on",1)))); await c.answer()

# ───────── TOGGLES
@router.callback_query(F.data == "auto:toggle")
async def auto_toggle(c: CallbackQuery):
    if await deny(c): return
    u = await guser(c.from_user.id)
    await set_auto(c.from_user.id, not bool(u.get("auto_on",0)))
    u = await guser(c.from_user.id)
    await c.message.edit_text("Menu", reply_markup=main_menu(bool(u.get("auto_on",0)), bool(u.get("notify_on",1))))
    await c.answer("Auto-Buy " + ("ON" if u.get("auto_on",0) else "OFF"))

@router.callback_query(F.data == "notify:toggle")
async def notify_toggle(c: CallbackQuery):
    if await deny(c): return
    u = await guser(c.from_user.id)
    await set_notify(c.from_user.id, not bool(u.get("notify_on",1)))
    u = await guser(c.from_user.id)
    await c.message.edit_text("Menu", reply_markup=main_menu(bool(u.get("auto_on",0)), bool(u.get("notify_on",1))))
    await c.answer("Notifier " + ("ON" if u.get("notify_on",1) else "OFF"))

# ───────── PROFILE & REFUNDS
@router.callback_query(F.data == "profile:open")
async def profile(c: CallbackQuery):
    if await deny(c): return
    uid = c.from_user.id
    credit = await credit_of(uid)
    try: bot_stars = await TGAPI.get_my_star_balance()
    except: bot_stars = 0
    s = await settings_of(uid)
    spent = await spent_today(uid)
    txt = (f"{E['profile']} Profile\n"
           f"{E['wallet']} Internal credit: {credit}{E['star']}\n"
           f"🤖 Bot Stars: {bot_stars}{E['star']}\n\n"
           f"{E['recycle']} Auto-Purchase\n"
           f"— Cycles: {s['cycles']}\n"
           f"— Daily budget: {s['overall_limit'] or '∞'}{E['star']}  (spent today: {spent}{E['star']})\n"
           f"{E['spark']} Auto: {'on' if (await guser(uid)).get('auto_on',0) else 'off'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Refunds / Credits", callback_data="refund:menu")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data == "refund:menu")
async def refund_menu(c: CallbackQuery):
    if await deny(c): return
    txt = "Refunds / Credits — internal only (Telegram Stars are final)."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⤴️ Refund last purchase (credit)", callback_data="refund:last")],
        [InlineKeyboardButton(text="🧾 Refund by TxID (credit)", callback_data="refund:byid")],
        [InlineKeyboardButton(text="➕ Add manual credit", callback_data="credit:add")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data == "refund:last")
async def refund_last(c: CallbackQuery):
    if await deny(c): return
    uid=c.from_user.id
    async with open_db() as db:
        async with db.execute("SELECT id,stars FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 1",(uid,)) as cur:
            row=await cur.fetchone()
    if not row:
        await c.answer("Nothing to refund.", show_alert=True); return
    stars=int(row["stars"])
    await add_credit(uid, stars)
    async with open_db() as db:
        await db.execute("DELETE FROM purchases WHERE id=?", (row["id"],))
        await db.commit()
    await c.message.answer(f"Credited {stars}{E['star']} back. Internal credit: {await credit_of(uid)}{E['star']}.")
    await c.answer()

@router.callback_query(F.data == "refund:byid")
async def refund_byid(c: CallbackQuery):
    if await deny(c): return
    await kv_set(f"pending:{c.from_user.id}", {"user": c.from_user.id, "key":"refund_tx"})
    await c.message.edit_text("Send TxID to credit back that amount.", reply_markup=back_home()); await c.answer()

@router.callback_query(F.data == "credit:add")
async def credit_add(c: CallbackQuery):
    if await deny(c): return
    await kv_set(f"pending:{c.from_user.id}", {"user": c.from_user.id, "key":"credit_add"})
    await c.message.edit_text("Send amount of **internal** credit to add.", reply_markup=back_home()); await c.answer()

# ───────── SETTINGS
@router.callback_query(F.data == "settings:menu")
async def settings_menu(c: CallbackQuery):
    if await deny(c): return
    s = await settings_of(c.from_user.id)
    txt = (f"{E['gear']} Auto-Purchase Settings\n\n"
           f"Cycles: {s['cycles']}\n"
           f"Lower limit: {s['min_price']}{E['star']}\n"
           f"Upper limit: {s['max_price'] or '∞'}{E['star']}\n"
           f"Daily budget: {s['overall_limit'] or '∞'}{E['star']}\n"
           f"Supply limit: {s['supply_limit'] or '∞'}\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cycles", callback_data="set:cycles")],
        [InlineKeyboardButton(text="Lower limit", callback_data="set:min"),
         InlineKeyboardButton(text="Upper limit", callback_data="set:max")],
        [InlineKeyboardButton(text="Daily budget", callback_data="set:overall"),
         InlineKeyboardButton(text="Supply limit", callback_data="set:supply")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data.in_(["set:cycles","set:min","set:max","set:overall","set:supply"]))
async def set_value_prompt(c: CallbackQuery):
    if await deny(c): return
    prompts = {
        "set:cycles":"Send number of cycles (0=∞).",
        "set:min":"Send lower price in Stars (0 = none).",
        "set:max":"Send upper price in Stars (0 = none).",
        "set:overall":"Send DAILY budget in Stars for auto-buy (0 = ∞).",
        "set:supply":"Send supply limit in pieces (0 = ∞).",
    }
    await kv_set(f"pending:{c.from_user.id}", {"user": c.from_user.id, "key": c.data})
    await c.message.edit_text(prompts[c.data], reply_markup=back_home()); await c.answer()

# ───────── CATALOG (💝 only)
@router.callback_query(F.data.startswith("cata:"))
async def cata(c: CallbackQuery):
    if await deny(c): return
    page = int(c.data.split(":")[1])
    gifts = await gifts_from_api()
    gifts = [g for g in gifts if g.get("emoji") == E["heart"]]
    gifts.sort(key=lambda x: (x["star_count"], x["gift_id"]))
    per=6
    total=max(1, math.ceil(len(gifts)/per))
    page=max(0, min(page,total-1))
    view=gifts[page*per:page*per+per]
    lines=[f"{E['gift']} Gift Catalog page {page+1}/{total} (💝 only)"]
    kb=[]
    for g in view:
        title=g["title"]; price=g["star_count"]
        lines.append(f"• {E['heart']} {title} — {price}{E['star']}")
        kb.append([InlineKeyboardButton(text=f"Buy {price}{E['star']}", callback_data=f"cata:buy:{g['gift_id']}:{price}:{title[:40]}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text=f"{E['back']} Prev", callback_data=f"cata:{page-1}"))
    if page+1<total: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"cata:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="Back", callback_data="home")])
    await c.message.edit_text("\n".join(lines) if view else "No 💝 gifts right now.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@router.callback_query(F.data.startswith("cata:buy:"))
async def cata_buy(c: CallbackQuery):
    if await deny(c): return
    _,_,gid,price,title = c.data.split(":",4)
    price=int(price); uid=c.from_user.id
    credit = await credit_of(uid)
    if credit < price:
        await c.answer(f"Not enough internal credit ({credit}{E['star']}).", show_alert=True); return
    try:
        await TGAPI.send_gift(uid, gid, text="Manual")
    except Exception as e:
        await log("ERROR", f"send_gift: {e}")
        await c.answer("Telegram API refused gift.", show_alert=True); return
    await add_credit(uid, -price)
    async with open_db() as db:
        await db.execute("INSERT INTO purchases(user_id,gift_id,title,stars,auto,ts) VALUES (?,?,?,?,?,?)",
                         (uid,gid,title,price,0,int(datetime.now().timestamp())))
        await db.commit()
    await c.message.answer(f"{E['gift']} Sent {title} for {price}{E['star']}")
    await c.answer()

# ───────── TOP-UP (fixed + custom)
@router.callback_query(F.data == "topup:menu")
async def topup_menu(c: CallbackQuery):
    if await deny(c): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Add 50"+E["star"], callback_data="topup:fixed:50"),
         InlineKeyboardButton(text="Add 1000"+E["star"], callback_data="topup:fixed:1000"),
         InlineKeyboardButton(text="Add 3000"+E["star"], callback_data="topup:fixed:3000")],
        [InlineKeyboardButton(text="Custom amount…", callback_data="topup:custom")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])
    await c.message.edit_text("Top up Stars", reply_markup=kb); await c.answer()

@router.callback_query(F.data.startswith("topup:fixed:"))
async def topup_fixed(c: CallbackQuery):
    if await deny(c): return
    amount=int(c.data.split(":")[2])
    await c.message.answer(f"Top up Stars\nAdd {amount}{E['star']} to bot balance")
    await c.answer()

@router.callback_query(F.data == "topup:custom")
async def topup_custom(c: CallbackQuery):
    if await deny(c): return
    await kv_set(f"pending:{c.from_user.id}", {"user": c.from_user.id, "key":"topup_custom"})
    await c.message.edit_text("Send the Stars amount (e.g., 3300). Min 15, Max 200000.", reply_markup=back_home()); await c.answer()

# Catch number replies (custom top-up, refund tx, settings, add credit)
@router.message(F.text.regexp(r"^\d{1,6}$"))
async def on_number_reply(m: Message):
    if await deny(m): return
    p = await kv_get(f"pending:{m.from_user.id}")
    if not p or p.get("user") != m.from_user.id:
        return
    key = p.get("key"); n = int(m.text)
    if key == "topup_custom":
        if n < 15 or n > 200000:
            await m.answer("Out of range. Min 15, Max 200000 ⭐.")
            return
        await m.answer(f"Top up Stars\nAdd {n}{E['star']} to bot balance")
        await kv_set(f"pending:{m.from_user.id}", {})
        return
    if key == "refund_tx":
        await add_credit(m.from_user.id, n)
        new_credit = await credit_of(m.from_user.id)
        await m.answer(f"Credited {n}{E['star']}. Internal: {new_credit}{E['star']}.")
        await kv_set(f"pending:{m.from_user.id}", {})
        return
    if key in ("set:cycles","set:min","set:max","set:overall","set:supply"):
        value=int(m.text)
        keymap={"set:cycles":"cycles","set:min":"min_price","set:max":"max_price","set:overall":"overall_limit","set:supply":"supply_limit"}
        await set_setting(m.from_user.id, keymap[key], value)
        await m.answer("Updated.")
        await kv_set(f"pending:{m.from_user.id}", {})
        return
    if key == "credit_add":
        await add_credit(m.from_user.id, n)
        new_credit = await credit_of(m.from_user.id)
        await m.answer(f"Credit updated. Internal: {new_credit}{E['star']}.")
        await kv_set(f"pending:{m.from_user.id}", {})
        return

# Parse Telegram transfer confirmations to update internal credit
@router.message()
async def any_msg(m: Message):
    if await deny(m): return
    if not m.text: return
    m1 = re.search(r"You successfully transferred\s*⭐?\s*(\d+)", m.text)
    if m1:
        amount = int(m1.group(1))
        bonus = 0
        b1 = re.search(r"\(\+(\d+)\s*⭐\s*bonus\)", m.text)
        if b1: bonus = int(b1.group(1))
        total = amount + bonus
        await add_credit(m.from_user.id, total)
        new_credit = await credit_of(m.from_user.id)
        msg = f"Received {amount}{E['star']}"
        if bonus: msg += f" (+{bonus}{E['star']} bonus)"
        msg += f". Internal credit: {new_credit}{E['star']}."
        await m.answer(msg)

# ───────── HEALTH / LOGS / TEST
@router.callback_query(F.data == "health:run")
async def health(c: CallbackQuery):
    if await deny(c): return
    ok_api = False
    try:
        gifts = await gifts_from_api()
        ok_api = gifts is not None
    except: pass
    await c.message.edit_text(f"Health\n— API: {'ok '+E['ok'] if ok_api else 'fail '+E['bad']}\n— Loop: running {E['ok']}", reply_markup=back_home()); await c.answer()

@router.callback_query(F.data == "logs:open")
async def logs(c: CallbackQuery):
    if await deny(c): return
    async with open_db() as db:
        async with db.execute("SELECT ts,level,msg FROM events ORDER BY id DESC LIMIT 10") as cur:
            rows=await cur.fetchall()
    lines=[f"{datetime.fromtimestamp(r['ts']).strftime('%H:%M:%S')} [{r['level']}] {r['msg']}" for r in rows]
    await c.message.edit_text("Logs\n"+"\n".join(lines) if lines else "No logs.", reply_markup=back_home()); await c.answer()

@router.callback_query(F.data == "test:menu")
async def test_menu(c: CallbackQuery):
    if await deny(c): return
    await c.message.edit_text("Test Event\n— Simulate a limited drop.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Simulate drop (notify)", callback_data="test:drop")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])); await c.answer()

@router.callback_query(F.data == "test:drop")
async def test_drop(c: CallbackQuery):
    if await deny(c): return
    await c.message.answer("🚨 Limited gift detected (test) — will try to buy if Auto-Buy is ON.")
    await c.answer()

# ───────── AUTOBUY & NOTIFIER
async def autobuy_loop(bot: Bot):
    await asyncio.sleep(2)
    while True:
        try:
            for uid in ADMIN_IDS:
                await ensure_user(uid)
                u = await guser(uid)
                if not u.get("auto_on",0): continue
                s = await settings_of(uid)
                gifts = await gifts_from_api()
                # only limited (remaining_count present and > 0)
                gifts = [g for g in gifts if g.get("remaining") is not None and int(g.get("remaining") or 0) > 0]
                lo, hi = s["min_price"], s["max_price"] or 10**9
                gifts = [g for g in gifts if lo <= g["star_count"] <= hi]
                if not gifts: continue
                # prefer 💝 and cheaper
                gifts.sort(key=lambda x: (x["emoji"] != E["heart"], x["star_count"]))
                if s["overall_limit"] and (await spent_today(uid)) >= s["overall_limit"]:
                    continue
                for g in gifts:
                    price=g["star_count"]
                    if s["overall_limit"] and (await spent_today(uid)) + price > s["overall_limit"]:
                        continue
                    if await credit_of(uid) < price:
                        continue
                    try:
                        await TGAPI.send_gift(uid, g["gift_id"], text="Auto")
                    except Exception as e:
                        await log("ERROR", f"AUTO send_gift: {e}")
                        continue
                    await add_credit(uid, -price)
                    await add_spent(uid, price)
                    async with open_db() as db:
                        await db.execute("INSERT INTO purchases(user_id,gift_id,title,stars,auto,ts) VALUES (?,?,?,?,?,?)",
                                         (uid,g["gift_id"],g["title"],price,1,int(datetime.now().timestamp())))
                        await db.commit()
                    await asyncio.sleep(0.05)
        except Exception as e:
            await log("ERROR", f"autobuy_loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(AUTO_LOOP_SEC)

async def notifier_loop(bot: Bot):
    await asyncio.sleep(2)
    prev_ids=set()
    while True:
        try:
            gifts = await gifts_from_api()
            limited = [g for g in gifts if g.get("remaining") is not None and int(g.get("remaining") or 0) > 0]
            curr_ids={g["gift_id"] for g in limited}
            new = [g for g in limited if g["gift_id"] not in prev_ids]
            if new:
                for uid in ADMIN_IDS:
                    if not (await guser(uid)).get("notify_on",1): continue
                    lines=["🚨 Limited gifts:"]
                    for g in new:
                        lines.append(f"• {g['emoji']} {g['title']} — {g['star_count']}{E['star']}")
                    try: await bot.send_message(uid, "\n".join(lines))
                    except: pass
            prev_ids=curr_ids
        except Exception as e:
            await log("ERROR", f"notifier_loop: {e}")
        await asyncio.sleep(NOTIFY_LOOP_SEC)

# ───────── RUN
async def main():
    bot = Bot(BOT_TOKEN)
    dp  = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(autobuy_loop(bot))
    asyncio.create_task(notifier_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
