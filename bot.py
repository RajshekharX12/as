import asyncio, json, os, math
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp, aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)

# ── ENV ──────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

DB_PATH = "autogifts.db"

# ── EMOJI / TEXT ─────────────────────────────────────────────────────────────
E = {
    "gift": "🎁",
    "star": "⭐",
    "ok": "✅",
    "bad": "❌",
    "back": "⬅️",
    "spark": "⚡",
    "tools": "🧰",
    "wallet": "👛",
    "profile": "👤",
    "logs": "📄",
    "health": "🩺",
    "gear": "⚙️",
    "bell": "🔔",
    "recycle": "♻️",
    "credit": "💳",
    "doc": "🧾",
}

# ── DB LAYER ─────────────────────────────────────────────────────────────────
CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  credit  INTEGER NOT NULL DEFAULT 0,
  auto_on INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings(
  user_id INTEGER PRIMARY KEY,
  cycles INTEGER NOT NULL DEFAULT 500,
  min_price INTEGER NOT NULL DEFAULT 0,
  max_price INTEGER NOT NULL DEFAULT 0,          -- 0 = ∞
  overall_limit INTEGER NOT NULL DEFAULT 0,      -- 0 = ∞
  supply_limit  INTEGER NOT NULL DEFAULT 0,      -- 0 = ∞
  window TEXT NOT NULL DEFAULT ""                -- "HH:MM-HH:MM" or empty
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
  title TEXT,
  stars INTEGER NOT NULL,
  ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tx_credits(
  tx_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  level TEXT NOT NULL,
  msg TEXT NOT NULL
);
"""

@asynccontextmanager
async def open_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        await db.executescript(CREATE_SQL)
        await db.commit()
        yield db
    finally:
        await db.close()

async def kv_get(k, d=None):
    async with open_db() as db:
        async with db.execute("SELECT value FROM kv WHERE key=?", (k,)) as cur:
            row = await cur.fetchone()
            return json.loads(row[0]) if row else d

async def kv_set(k, v):
    s = json.dumps(v)
    async with open_db() as db:
        await db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,s))
        await db.commit()

async def log(level, msg):
    async with open_db() as db:
        await db.execute("INSERT INTO events(ts,level,msg) VALUES(?,?,?)",(int(datetime.now().timestamp()), level, msg))
        await db.commit()

async def ensure_user(user_id: int):
    async with open_db() as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)",(user_id,))
        await db.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)",(user_id,))
        await db.commit()

async def credit_of(user_id: int) -> int:
    async with open_db() as db:
        async with db.execute("SELECT credit FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return int(row["credit"] if row else 0)

async def add_credit(user_id:int, delta:int, note:str=""):
    async with open_db() as db:
        await db.execute("UPDATE users SET credit=credit+? WHERE user_id=?", (delta, user_id))
        await db.commit()
    if note: await log("INFO", f"credit {user_id} {delta}: {note}")

async def auto_on(user_id:int)->bool:
    async with open_db() as db:
        async with db.execute("SELECT auto_on FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return bool(row and row["auto_on"])

async def set_auto(user_id:int, flag:bool):
    async with open_db() as db:
        await db.execute("UPDATE users SET auto_on=? WHERE user_id=?", (1 if flag else 0, user_id)); await db.commit()

async def settings_of(user_id:int)->Dict[str,int|str]:
    async with open_db() as db:
        async with db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}

async def set_setting(user_id:int, key:str, val:int|str):
    async with open_db() as db:
        await db.execute(f"UPDATE settings SET {key}=? WHERE user_id=?", (val, user_id)); await db.commit()

async def allow_toggle(user_id:int, gift_id:str)->bool:
    try:
        async with open_db() as db:
            await db.execute("INSERT INTO allowlist(user_id,gift_id) VALUES(?,?)", (user_id,gift_id))
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        async with open_db() as db:
            await db.execute("DELETE FROM allowlist WHERE user_id=? AND gift_id=?", (user_id,gift_id))
            await db.commit()
        return False

async def allowed(user_id:int, gift_id:str)->bool:
    async with open_db() as db:
        async with db.execute("SELECT 1 FROM allowlist WHERE user_id=? AND gift_id=?", (user_id,gift_id)) as cur:
            return (await cur.fetchone()) is not None

# ── RAW BOT API for Stars/Gifts ───────────────────────────────────────────────
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
        res = await self.call("getAvailableGifts")
        return res.get("gifts", [])

    async def send_gift(self, user_id:int, gift_id:str, text:str="")->bool:
        await self.call("sendGift", {"user_id": user_id, "gift_id": gift_id, "text": text, "is_private": True})
        return True

    async def get_my_star_balance(self)->int:
        res = await self.call("getMyStarBalance")
        return int(res.get("star_count", 0))

    async def get_star_transactions(self, limit:int=200)->List[Dict[str,Any]]:
        res = await self.call("getStarTransactions", {"limit": limit})
        return res.get("transactions", [])

TGAPI = TG(BOT_TOKEN)

# ── UTIL ──────────────────────────────────────────────────────────────────────
def parse_window(s: str)->Optional[Tuple[time,time]]:
    if not s: return None
    try:
        a,b = s.split("-",1); ha,ma = map(int,a.split(":")); hb,mb = map(int,b.split(":"))
        return time(ha,ma), time(hb,mb)
    except: return None

def in_window(now:datetime, win:str)->bool:
    t = parse_window(win)
    if not t: return True
    t1,t2 = t; nt = now.time()
    return (t1<=nt<=t2) if t1<=t2 else (nt>=t1 or nt<=t2)

def normalize_gift(g:Dict[str,Any])->Dict[str,Any]:
    gid = str(g.get("id") or g.get("gift_id") or g.get("gid") or "")
    title = str(g.get("title") or g.get("name") or g.get("base_name") or "Gift")
    price = int(g.get("star_count") or (g.get("price") or {}).get("star_count") or g.get("price") or 0)
    emoji = g.get("emoji") or (g.get("sticker") or {}).get("emoji") or "🎁"
    rem = g.get("remaining_count")
    return {"gift_id": gid, "title": title, "star_count": price, "emoji": emoji, "remaining": rem}

async def fetch_gifts_from_api()->List[Dict[str,Any]]:
    try:
        raw = await TGAPI.get_available_gifts()
        return [normalize_gift(x) for x in raw]
    except Exception as e:
        await log("ERROR", f"getAvailableGifts: {e}")
        return []

# ── APP (AIOGRAM) ────────────────────────────────────────────────────────────
router = Router()

def main_menu(auto_on_flag: bool)->InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{E['spark']} Auto-Buy: {'ON '+E['ok'] if auto_on_flag else 'OFF '+E['bad']}", callback_data="auto:toggle"),
         InlineKeyboardButton(f"🔎 Source", callback_data="source:menu")],
        [InlineKeyboardButton(f"{E['gear']} Auto-Purchase Settings", callback_data="settings:menu")],
        [InlineKeyboardButton(f"{E['gift']} Gift Catalog", callback_data="cata:0")],
        [InlineKeyboardButton(f"{E['profile']} Profile", callback_data="profile:open"),
         InlineKeyboardButton(f"⭐ Deposit", callback_data="topup:menu")],
        [InlineKeyboardButton(f"{E['health']} Health", callback_data="health:run"),
         InlineKeyboardButton(f"{E['logs']} Logs", callback_data="logs:open")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_home()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="home")]])

@router.message(CommandStart())
async def on_start(m: Message):
    await ensure_user(m.from_user.id)
    flag = await auto_on(m.from_user.id)
    await m.answer("Menu", reply_markup=main_menu(flag))

@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    flag = await auto_on(c.from_user.id)
    await c.message.edit_text("Menu", reply_markup=main_menu(flag)); await c.answer()

# ── PROFILE / BALANCE ────────────────────────────────────────────────────────
@router.callback_query(F.data == "profile:open")
async def cb_profile(c: CallbackQuery):
    uid = c.from_user.id
    credit = await credit_of(uid)
    try:
        bot_balance = await TGAPI.get_my_star_balance()
    except Exception:
        bot_balance = 0
    s = await settings_of(uid)
    auto = await auto_on(uid)
    txt = (
        f"{E['profile']} Profile\n"
        f"{E['wallet']} Internal credit: {credit}{E['star']}\n"
        f"🤖 Bot Stars: {bot_balance}{E['star']}\n\n"
        f"{E['recycle']} Auto-Purchase\n"
        f"— Cycles: {s['cycles']}\n"
        f"{E['spark']} Auto: {'on' if auto else 'off'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Refunds / Credits", callback_data="refund:menu")],
        [InlineKeyboardButton("Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

# ── SOURCE / NOTIFIER ────────────────────────────────────────────────────────
@router.callback_query(F.data == "source:menu")
async def cb_source_menu(c: CallbackQuery):
    path = (await kv_get("notifier_path")) or "(none)"
    # quick test
    count = len(await kv_get("last_notifier_items") or [])
    txt = f"Source: NOTIFIER\nJSON path: {path}\nLast parsed: {count} gifts"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Set JSON path", callback_data="source:path")],
        [InlineKeyboardButton("Test Notifier", callback_data="source:test")],
        [InlineKeyboardButton("Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data == "source:test")
async def cb_source_test(c: CallbackQuery):
    path = (await kv_get("notifier_path")) or ""
    if not path or not os.path.exists(path):
        await c.answer("Path missing or file not found.", show_alert=True); return
    try:
        data = json.loads(open(path,"r",encoding="utf-8").read())
        items = data.get("gifts", data)
        if isinstance(items, dict): items = [{"gift_id":k, **v} for k,v in items.items()]
        norm = [normalize_gift(x) for x in items if isinstance(x, dict)]
        await kv_set("last_notifier_items", norm)
        await c.answer(f"OK: {len(norm)} gifts", show_alert=True)
    except Exception as e:
        await c.answer(f"Parse error: {e}", show_alert=True)

@router.callback_query(F.data == "source:path")
async def cb_source_path(c: CallbackQuery):
    await kv_set("pending", {"user": c.from_user.id, "key": "source:path"})
    await c.message.edit_text("Send full path to notifier JSON.", reply_markup=back_home()); await c.answer()

# ── SETTINGS ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "settings:menu")
async def cb_settings(c: CallbackQuery):
    s = await settings_of(c.from_user.id)
    txt = (
        f"{E['gear']} Auto-Purchase Settings\n\n"
        f"Cycles: {s['cycles']}\n"
        f"Lower limit: {s['min_price']}{E['star']}\n"
        f"Upper limit: {s['max_price'] or '∞'}{E['star']}\n"
        f"Overall limit: {s['overall_limit'] or '∞'}{E['star']}\n"
        f"Supply limit: {s['supply_limit'] or '∞'}\n"
        f"Window: {s['window'] or '(always)'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Cycles", callback_data="set:cycles")],
        [InlineKeyboardButton("Lower limit", callback_data="set:min"),
         InlineKeyboardButton("Upper limit", callback_data="set:max")],
        [InlineKeyboardButton("Overall limit", callback_data="set:overall"),
         InlineKeyboardButton("Supply limit", callback_data="set:supply")],
        [InlineKeyboardButton("Allow IDs", callback_data="lim:set:ids"),
         InlineKeyboardButton("Window", callback_data="lim:set:window")],
        [InlineKeyboardButton("Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data.in_(["set:cycles","set:min","set:max","set:overall","set:supply","lim:set:ids","lim:set:window"]))
async def cb_setting_prompts(c: CallbackQuery):
    prompts = {
        "set:cycles":"Send number of cycles (0=∞).",
        "set:min":"Send lower price in Stars (0 = none).",
        "set:max":"Send upper price in Stars (0 = none).",
        "set:overall":"Send overall budget in Stars (0 = ∞).",
        "set:supply":"Send supply limit in pieces (0 = ∞).",
        "lim:set:ids":"Send comma-separated Gift IDs to allow (empty = any).",
        "lim:set:window":"Send HH:MM-HH:MM (empty = always).",
    }
    await kv_set("pending", {"user": c.from_user.id, "key": c.data})
    await c.message.edit_text(prompts[c.data], reply_markup=back_home()); await c.answer()

# ── CATALOG ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("cata:"))
async def cb_cata(c: CallbackQuery):
    p = c.data.split(":")
    if len(p)==2:   # cata:0
        page = int(p[1])
        gifts = await fetch_gifts_from_api()
        gifts.sort(key=lambda x: (x["star_count"], x["gift_id"]))
        per = 6
        total = max(1, math.ceil(len(gifts)/per))
        page = max(0, min(page, total-1))
        start = page*per; view = gifts[start:start+per]
        lines = [f"{E['gift']} Gift Catalog (api-fallback) page {page+1}/{total}"]
        kb_rows = []
        for g in view:
            gid, title, price, emoji = g["gift_id"], g["title"], g["star_count"], g["emoji"]
            lines.append(f"• {emoji} {title} — {price}{E['star']} (id {gid})")
            kb_rows.append([
                InlineKeyboardButton(f"Buy {price}{E['star']}", callback_data=f"cata:buy:{gid}:{price}:{title[:40]}"),
                InlineKeyboardButton("+Allow", callback_data=f"cata:allow:{gid}"),
            ])
        nav=[]
        if page>0: nav.append(InlineKeyboardButton(f"{E['back']} Prev", callback_data=f"cata:{page-1}"))
        if page+1<total: nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cata:{page+1}"))
        if nav: kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("Back", callback_data="home")])
        await c.message.edit_text("\n".join(lines) if view else "No gifts now.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)); await c.answer()
        return

    if p[1]=="allow":
        gid = p[2]
        flag = await allow_toggle(c.from_user.id, gid)
        await c.answer(("Allowed" if flag else "Removed"), show_alert=False)
        return

    if p[1]=="buy":
        gid, price, title = p[2], int(p[3]), p[4]
        uid = c.from_user.id
        # check internal credit
        credit = await credit_of(uid)
        if credit < price:
            await c.answer(f"Not enough internal credit ({credit}{E['star']}). Top up first.", show_alert=True); return
        try:
            await TGAPI.send_gift(uid, gid, text="Manual")
        except Exception as e:
            await c.answer(f"Send failed: {e}", show_alert=True); return
        await add_credit(uid, -price, f"Manual buy {title}")
        async with open_db() as db:
            await db.execute("INSERT INTO purchases(user_id,gift_id,title,stars,ts) VALUES (?,?,?,?,strftime('%s','now'))",
                             (uid, gid, title, price))
            await db.commit()
        await c.answer("Sent!", show_alert=False)
        return

# ── REFUNDS / CREDITS ────────────────────────────────────────────────────────
@router.callback_query(F.data == "refund:menu")
async def cb_refunds(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("↩️ Refund last purchase (credit)", callback_data="refund:last")],
        [InlineKeyboardButton("🧾 Refund by TxID (credit)", callback_data="refund:txid")],
        [InlineKeyboardButton("➕ Add manual credit", callback_data="refund:add")],
        [InlineKeyboardButton("Back", callback_data="home")]
    ])
    await c.message.edit_text("Refunds / Credits — internal only (Telegram Stars are final).", reply_markup=kb); await c.answer()

@router.callback_query(F.data == "refund:last")
async def cb_refund_last(c: CallbackQuery):
    uid = c.from_user.id
    async with open_db() as db:
        async with db.execute("SELECT stars FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)) as cur:
            row = await cur.fetchone()
    if not row:
        await c.answer("Nothing to refund", show_alert=True); return
    amt = int(row["stars"])
    await add_credit(uid, amt, "Refund last purchase")
    bal = await credit_of(uid)
    await c.message.edit_text(f"Credited {amt}{E['star']} back. Internal credit: {bal}{E['star']}.", reply_markup=back_home()); await c.answer()

@router.callback_query(F.data == "refund:add")
async def cb_refund_add(c: CallbackQuery):
    await kv_set("pending", {"user": c.from_user.id, "key": "refund:add"})
    await c.message.edit_text("Send amount to credit/debit (positive/negative integer).", reply_markup=back_home()); await c.answer()

@router.callback_query(F.data == "refund:txid")
async def cb_refund_txid(c: CallbackQuery):
    await kv_set("pending", {"user": c.from_user.id, "key": "refund:txid"})
    await c.message.edit_text("Send the **Star Transaction ID** now.", reply_markup=back_home()); await c.answer()

# ── TOP UP (STARS / XTR) ─────────────────────────────────────────────────────
@router.callback_query(F.data == "topup:menu")
async def cb_topup(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Add 50⭐", callback_data="top:go:50"),
         InlineKeyboardButton("Add 1000⭐", callback_data="top:go:1000"),
         InlineKeyboardButton("Add 3000⭐", callback_data="top:go:3000")],
        [InlineKeyboardButton("Back", callback_data="home")]
    ])
    await c.message.edit_text("Top up Stars", reply_markup=kb); await c.answer()

@router.callback_query(F.data.startswith("top:go:"))
async def cb_topup_go(c: CallbackQuery, bot: Bot):
    amt = int(c.data.split(":")[-1])
    await bot.send_invoice(chat_id=c.message.chat.id, title="Top up Stars", description=f"Add {amt}⭐ to bot balance",
                           payload=f"topup:{amt}", currency="XTR",
                           prices=[LabeledPrice(label=f"{amt}⭐", amount=amt)])
    await c.answer("Invoice sent")

@router.pre_checkout_query()
async def pre(q: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query_id=q.id, ok=True)

@router.message(F.successful_payment)
async def on_paid(m: Message):
    amt = getattr(m.successful_payment, "total_amount", 0) or getattr(m.successful_payment, "total_star_amount", 0) or 0
    bonus = math.floor(amt * 0.3)  # optional promo
    await add_credit(m.from_user.id, amt + bonus, "Top up")
    bal = await credit_of(m.from_user.id)
    await m.answer(f"Received {amt}⭐ (+{bonus}⭐ bonus). Internal credit: {bal}⭐.")

# ── HEALTH / LOGS ────────────────────────────────────────────────────────────
@router.callback_query(F.data == "health:run")
async def cb_health(c: CallbackQuery):
    try:
        stars = await TGAPI.get_my_star_balance()
    except Exception:
        stars = 0
    gifts = await fetch_gifts_from_api()
    await c.message.edit_text(f"Health\nBot wallet: {stars}⭐\nGifts available: {len(gifts)}", reply_markup=back_home()); await c.answer()

@router.callback_query(F.data == "logs:open")
async def cb_logs(c: CallbackQuery):
    async with open_db() as db:
        async with db.execute("SELECT ts,level,msg FROM events ORDER BY id DESC LIMIT 20") as cur:
            rows = await cur.fetchall()
    lines = [(datetime.fromtimestamp(ts).strftime('%H:%M:%S') + f" [{lvl}] {msg}") for ts,lvl,msg in rows]
    await c.message.edit_text("Recent logs\n" + ("\n".join(lines) if lines else "Empty"), reply_markup=back_home()); await c.answer()

# ── TEXT INPUTS (pending prompts) ────────────────────────────────────────────
@router.message(F.text)
async def on_text(m: Message):
    pend = await kv_get("pending")
    if not pend or pend.get("user") != m.from_user.id: return
    key = pend["key"]; txt = (m.text or "").strip()
    uid = m.from_user.id

    try:
        if key=="source:path":
            await kv_set("notifier_path", txt); await m.answer("Notifier path set.")
        elif key=="set:cycles":
            await set_setting(uid, "cycles", max(0,int(txt))); await m.answer("Cycles updated.")
        elif key=="set:min":
            await set_setting(uid, "min_price", max(0,int(txt))); await m.answer("Lower limit updated.")
        elif key=="set:max":
            await set_setting(uid, "max_price", max(0,int(txt))); await m.answer("Upper limit updated (0=∞).")
        elif key=="set:overall":
            await set_setting(uid, "overall_limit", max(0,int(txt))); await m.answer("Overall limit updated (0=∞).")
        elif key=="set:supply":
            await set_setting(uid, "supply_limit", max(0,int(txt))); await m.answer("Supply limit updated (0=∞).")
        elif key=="lim:set:ids":
            ids = [x.strip() for x in txt.split(",") if x.strip()]
            # wipe + add
            async with open_db() as db:
                await db.execute("DELETE FROM allowlist WHERE user_id=?", (uid,))
                for gid in ids:
                    await db.execute("INSERT OR IGNORE INTO allowlist(user_id,gift_id) VALUES(?,?)",(uid,gid))
                await db.commit()
            await m.answer("Allowlist updated.")
        elif key=="lim:set:window":
            await set_setting(uid, "window", txt); await m.answer("Window updated.")
        elif key=="refund:add":
            await add_credit(uid, int(txt), "Manual credit"); bal = await credit_of(uid)
            await m.answer(f"Credit updated. Internal: {bal}⭐.")
        elif key=="refund:txid":
            txid = txt
            try:
                txs = await TGAPI.get_star_transactions(limit=200)
            except Exception as e:
                await m.answer(f"Could not fetch transactions: {e}"); await kv_set("pending", None); return
            hit = next((t for t in txs if str(t.get('id') or '')==txid), None)
            if not hit:
                await m.answer("TxID not found."); await kv_set("pending", None); return
            amt = int(hit.get("amount") or hit.get("star_count") or 0)
            async with open_db() as db:
                async with db.execute("SELECT 1 FROM tx_credits WHERE tx_id=?", (txid,)) as cur:
                    if await cur.fetchone():
                        await m.answer("Already credited for this TxID."); await kv_set("pending", None); return
                await db.execute("INSERT INTO tx_credits(tx_id,user_id,amount,ts) VALUES (?,?,?,strftime('%s','now'))", (txid, uid, amt))
                await db.commit()
            await add_credit(uid, amt, "TxID credit")
            bal = await credit_of(uid)
            await m.answer(f"Credited {amt}⭐ from TxID. Internal credit: {bal}⭐.")
        else:
            await m.answer("Ignored.")
    except Exception as e:
        await m.answer(f"Input error: {e}")
    await kv_set("pending", None)

# ── AUTO-BUY BACKGROUND ──────────────────────────────────────────────────────
RUN = {"spent":0, "bought":0, "cycles":None}

async def autobuy_loop(bot: Bot):
    while True:
        try:
            # find users with auto_on = 1
            async with open_db() as db:
                async with db.execute("SELECT user_id FROM users WHERE auto_on=1") as cur:
                    active = [r["user_id"] for r in await cur.fetchall()]
            if not active:
                await asyncio.sleep(1.5); continue

            gifts = await fetch_gifts_from_api()
            for uid in active:
                s = await settings_of(uid)
                winok = in_window(datetime.now(), s.get("window",""))
                if not winok: continue
                credit = await credit_of(uid)

                # stop by overall/supply/cycles if needed
                if RUN["cycles"] is None:
                    RUN["cycles"] = s.get("cycles") or 0
                if RUN["cycles"]>0:
                    RUN["cycles"] -= 1

                for g in gifts:
                    price = int(g["star_count"])
                    if s["min_price"] and price < s["min_price"]: continue
                    if s["max_price"] and price > s["max_price"]: continue
                    if credit < price: continue
                    if not await allowed(uid, g["gift_id"]): continue
                    try:
                        await TGAPI.send_gift(uid, g["gift_id"], text="Auto")
                    except Exception as e:
                        await log("ERROR", f"sendGift fail {uid}/{g['gift_id']}: {e}")
                        continue
                    await add_credit(uid, -price, f"Auto buy {g['title']}")
                    async with open_db() as db:
                        await db.execute("INSERT INTO purchases(user_id,gift_id,title,stars,ts) VALUES (?,?,?,?,strftime('%s','now'))",
                                         (uid, g["gift_id"], g["title"], price))
                        await db.commit()
                    await bot.send_message(chat_id=uid, text=f"{E['spark']} Bought {g['emoji']} {g['title']} for {price}{E['star']}")
        except Exception as e:
            await log("ERROR", f"loop crash: {e}")
        await asyncio.sleep(2)

# ── BOOTSTRAP ────────────────────────────────────────────────────────────────
async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # start background loop
    asyncio.create_task(autobuy_loop(bot))

    print("Bot running…")
    await dp.start_polling(bot, allowed_updates=["message","callback_query","pre_checkout_query"])

if __name__ == "__main__":
    asyncio.run(main())
