import asyncio, json, logging, os
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections import deque

import aiohttp, aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    BOT_TOKEN, ADMIN_IDS, DB_PATH,
    INTERVAL_NORMAL, INTERVAL_FAST, INTERVAL_INSANE,
    DEFAULT_MAX_PRICE, DEFAULT_ALLOW_IDS, DEFAULT_ALLOW_KEYS,
    DEFAULT_RECIPS, BUY_COOLDOWN_SEC, DEFAULT_WINDOW, DEFAULT_NOTIFY_ONLY,
    FEED_MODE, NOTIFIER_JSON, NOTIFIER_POLL_SEC
)

logging.basicConfig(level=logging.INFO)
router = Router()

# ------------------- Low-level Bot API -------------------
class TG:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.session: Optional[aiohttp.ClientSession] = None

    async def _ensure(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def call(self, method: str, payload: Dict[str, Any] | None = None) -> Any:
        await self._ensure()
        async with self.session.post(f"{self.base}/{method}", json=payload or {}) as r:
            data = await r.json()
            if not data.get("ok"):
                raise RuntimeError(f"{method} failed: {data}")
            return data["result"]

    async def get_available_gifts(self) -> List[Dict[str, Any]]:
        res = await self.call("getAvailableGifts")
        return res.get("gifts", [])

    async def send_gift(self, chat_id: int, gift_id: str, text: str = "", is_private: bool = True) -> Any:
        return await self.call("sendGift", {"chat_id": chat_id, "gift_id": gift_id, "text": text, "is_private": is_private})

    async def get_my_star_balance(self) -> int:
        res = await self.call("getMyStarBalance")
        return int(res.get("star_count", 0))

    async def get_star_transactions(self, limit: int = 10) -> List[Dict[str, Any]]:
        res = await self.call("getStarTransactions", {"limit": limit})
        return res.get("transactions", [])

TGAPI = TG(BOT_TOKEN)

# ------------------- DB / KV / Logs -------------------
CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gift_id TEXT NOT NULL,
  title   TEXT,
  stars   INTEGER NOT NULL,
  recipient_id INTEGER NOT NULL,
  status  TEXT NOT NULL,
  ts      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  level TEXT NOT NULL,
  msg TEXT NOT NULL
);
"""

async def open_db():
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(CREATE_SQL)
    await db.commit()
    return db

async def kv_get(db, key, default=None):
    async with db.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
        r = await cur.fetchone()
        return json.loads(r[0]) if r else default

async def kv_set(db, key, value):
    s = json.dumps(value)
    await db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,s))
    await db.commit()

async def log_event(level: str, msg: str):
    async with open_db() as db:
        await db.execute("INSERT INTO events(ts,level,msg) VALUES (?,?,?)",
                         (int(datetime.now(tz=timezone.utc).timestamp()), level, msg))
        await db.commit()

def is_admin(uid:int)->bool:
    return (not ADMIN_IDS) or uid in ADMIN_IDS

# ------------------- State / helpers -------------------
AUTOBUY_TASK: Optional[asyncio.Task] = None
RECENT_TS: dict[str, int] = {}
RECENT_QUEUE = deque(maxlen=200)

def interval_for(speed:str)->float:
    s = (speed or "FAST").upper()
    return INTERVAL_INSANE if s=="INSANE" else INTERVAL_NORMAL if s=="NORMAL" else INTERVAL_FAST

def within_window(now: datetime, window: str) -> bool:
    if not window: return True
    try:
        s,e = window.split("-",1); h1,m1 = map(int,s.split(":")); h2,m2 = map(int,e.split(":"))
        t1,t2 = time(h1,m1), time(h2,m2); nt = now.time()
        return (t1<=nt<=t2) if (t1<=t2) else (nt>=t1 or nt<=t2)
    except Exception: return True

def normalize_gift_dict(x: Dict[str,Any]) -> Dict[str,Any]:
    gid = str(x.get("id") or x.get("gift_id") or "")
    title = str(x.get("title") or x.get("name") or x.get("base_name") or "Gift")
    price = int(x.get("star_count") or (x.get("price") or {}).get("star_count") or x.get("price") or 0)
    return {"gift_id": gid, "title": title, "star_count": price}

async def gifts_from_notifier(path: str) -> List[Dict[str,Any]]:
    try:
        if not path or not os.path.exists(path): return []
        raw = (await asyncio.to_thread(open, path, "r", encoding="utf-8")).read()
        data = json.loads(raw)
        if isinstance(data, dict) and "gifts" in data: items = data["gifts"]
        elif isinstance(data, dict): items = [{"gift_id": k, **v} for k,v in data.items()]
        elif isinstance(data, list): items = data
        else: items = []
        return [normalize_gift_dict(i) for i in items if isinstance(i, dict)]
    except Exception as e:
        await log_event("ERROR", f"notifier parse error: {e}")
        return []

async def gifts_from_api() -> List[Dict[str,Any]]:
    try:
        return [normalize_gift_dict(g) for g in await TGAPI.get_available_gifts()]
    except Exception as e:
        await log_event("ERROR", f"getAvailableGifts: {e}")
        return []

async def get_gifts(st: dict) -> Tuple[str, List[Dict[str,Any]]]:
    mode = (st.get("feed_mode") or "api").lower()
    if mode == "notifier":
        items = await gifts_from_notifier(st.get("notifier_json") or "")
        if items: return ("notifier", items)
        api = await gifts_from_api()
        return ("api-fallback", api)
    return ("api", await gifts_from_api())

async def get_state():
    async with open_db() as db:
        st = await kv_get(db, "state", {}) or {}
        st.setdefault("running", False)
        st.setdefault("speed", "FAST")
        st.setdefault("max_price", DEFAULT_MAX_PRICE)
        st.setdefault("allow_ids", DEFAULT_ALLOW_IDS)
        st.setdefault("allow_keys", DEFAULT_ALLOW_KEYS)
        st.setdefault("recips", DEFAULT_RECIPS or (ADMIN_IDS[:1] if ADMIN_IDS else []))
        st.setdefault("window", DEFAULT_WINDOW)
        st.setdefault("notify_only", DEFAULT_NOTIFY_ONLY)
        st.setdefault("feed_mode", FEED_MODE)
        st.setdefault("notifier_json", NOTIFIER_JSON)
        return st

async def save_state(st): 
    async with open_db() as db: await kv_set(db, "state", st)

def matches(g: Dict[str,Any], allow_ids: List[str], allow_keys: List[str], max_price: int) -> Tuple[bool, int, str, str]:
    gid, title, price = g["gift_id"], g["title"], int(g["star_count"])
    ok_id   = (not allow_ids)  or (gid in allow_ids)
    ok_key  = (not allow_keys) or any(k.lower() in title.lower() for k in allow_keys)
    ok_price= (not max_price)  or (price <= max_price)
    return (ok_id and ok_key and ok_price), price, gid, title

# ------------------- UI -------------------
def main_menu(st: dict):
    running = st.get("running", False); speed = st.get("speed", "FAST")
    kb = InlineKeyboardBuilder()
    kb.button(text=("🚀 Start" if not running else "🛑 Stop"), callback_data="auto:toggle")
    kb.button(text=f"⚡ Speed: {speed}", callback_data="speed:menu")
    kb.button(text=f"📡 Source: {st.get('feed_mode','api').upper()}", callback_data="source:menu")
    kb.button(text="🎯 Limits", callback_data="limits:menu")
    kb.button(text="👥 Recipients", callback_data="recips:menu")
    kb.button(text="🗂 Catalogue", callback_data="cata:0")
    kb.button(text="💫 Balance", callback_data="bal:menu")
    kb.button(text="🩺 Health", callback_data="health:run")
    kb.button(text="🧾 Logs", callback_data="logs:open")
    kb.button(text="💸 Top up", callback_data="topup:menu")
    kb.adjust(2,2,2,1,2)
    return kb.as_markup()

def back_home(): 
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Home", callback_data="home")]])

# ------------------- Worker -------------------
async def autobuy_loop(bot: Bot):
    await log_event("INFO", "autobuy loop started")
    while True:
        try:
            st = await get_state()
            if not st.get("running"): await asyncio.sleep(1.0); continue
            if not within_window(datetime.now(), st.get("window","")): await asyncio.sleep(1.0); continue

            source, items = await get_gifts(st)
            allow_ids  = st.get("allow_ids") or []
            allow_keys = st.get("allow_keys") or []
            max_price  = int(st.get("max_price") or 0)
            notify_only= bool(st.get("notify_only"))
            recips     = list(st.get("recips") or [])
            if not recips: await log_event("WARN","No recipients configured."); await asyncio.sleep(1.0); continue

            for g in items:
                ok, price, gid, title = matches(g, allow_ids, allow_keys, max_price or 0)
                id_key_ok = ((not allow_ids) or (gid in allow_ids)) and ((not allow_keys) or any(k.lower() in title.lower() for k in allow_keys))
                ts_now = int(datetime.now(tz=timezone.utc).timestamp())
                last = RECENT_TS.get(gid, 0)
                if last and (ts_now - last) < BUY_COOLDOWN_SEC: continue

                if not ok and not (notify_only and id_key_ok):
                    continue

                if notify_only and not ((not max_price) or price <= max_price):
                    try:
                        target = (ADMIN_IDS[0] if ADMIN_IDS else (recips[0] if recips else None))
                        if target:
                            kb = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text=f"BUY {price}⭐", callback_data=f"ovbuy:{gid}:{price}:{title[:40]}")],
                                [InlineKeyboardButton(text="Dismiss", callback_data="noop")]
                            ])
                            await bot.send_message(target, f"⚠️ Over cap from {source}: {title} ({price}⭐)\n`{gid}`", reply_markup=kb)
                        await log_event("INFO", f"notify-only {gid} {price}⭐ {source}")
                    except Exception as e:
                        await log_event("ERROR", f"notify send failed: {e}")
                else:
                    rid = recips[0]
                    try:
                        await TGAPI.send_gift(rid, gid, text="Auto 🎁", is_private=True)
                        async with open_db() as db:
                            await db.execute(
                                "INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                                (gid, title, int(price), rid, "sent", ts_now)
                            ); await db.commit()
                        await log_event("INFO", f"auto-sent {title} ({price}⭐) → {rid} [{source}]")
                        RECENT_TS[gid] = ts_now; RECENT_QUEUE.append((ts_now, gid, title, price, rid))
                    except Exception as e:
                        await log_event("ERROR", f"sendGift {gid} → {rid} failed: {e}")
            await asyncio.sleep(interval_for(st.get("speed")) if st.get("feed_mode","api")=="api" else NOTIFIER_POLL_SEC)
        except Exception as e:
            await log_event("ERROR", f"loop crash: {e}")
            await asyncio.sleep(0.5)

# ------------------- Handlers -------------------
@router.message(CommandStart())
async def start(m: Message):
    st = await get_state()
    await m.answer("⭐ **Auto Gifts Sniper**\n• source switch • limits • buy window • alerts\n• live catalogue • logs • health • stars", reply_markup=main_menu(st))

@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    st = await get_state()
    await c.message.edit_text("Main menu:", reply_markup=main_menu(st)); await c.answer()

# start/stop
@router.callback_query(F.data == "auto:toggle")
async def cb_toggle(c: CallbackQuery, bot: Bot):
    st = await get_state(); st["running"] = not st.get("running"); await save_state(st)
    global AUTOBUY_TASK
    if st["running"]:
        if AUTOBUY_TASK is None or AUTOBUY_TASK.done(): AUTOBUY_TASK = asyncio.create_task(autobuy_loop(bot))
        await c.answer("AutoBuy ON")
    else:
        await c.answer("AutoBuy OFF")
    await c.message.edit_reply_markup(reply_markup=main_menu(st))

# speed menu
@router.callback_query(F.data == "speed:menu")
async def cb_speed_menu(c: CallbackQuery):
    st = await get_state()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="NORMAL", callback_data="speed:set:NORMAL"),
         InlineKeyboardButton(text="FAST",   callback_data="speed:set:FAST"),
         InlineKeyboardButton(text="INSANE", callback_data="speed:set:INSANE")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")]
    ])
    await c.message.edit_text(f"Speed: {st.get('speed')}", reply_markup=kb); await c.answer()

@router.callback_query(F.data.startswith("speed:set:"))
async def cb_speed_set(c: CallbackQuery):
    _,_,v = c.data.split(":"); st = await get_state(); st["speed"]=v; await save_state(st)
    await c.answer(f"Speed → {v}"); await c.message.edit_reply_markup(reply_markup=main_menu(st))

# source menu
@router.callback_query(F.data == "source:menu")
async def cb_source_menu(c: CallbackQuery):
    st = await get_state()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Use API",      callback_data="source:set:api"),
         InlineKeyboardButton(text="Use Notifier", callback_data="source:set:notifier")],
        [InlineKeyboardButton(text="Set JSON path", callback_data="source:set:path")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")]
    ])
    cur = st.get("feed_mode","api").upper()
    await c.message.edit_text(f"Current source: {cur}\nPath: {st.get('notifier_json') or '(none)'}", reply_markup=kb); await c.answer()

@router.callback_query(F.data.startswith("source:set:"))
async def cb_source_set(c: CallbackQuery):
    _,_,what = c.data.split(":")
    st = await get_state()
    if what in ("api","notifier"):
        st["feed_mode"] = what; await save_state(st); await c.answer(f"Source → {what.upper()}")
        await c.message.edit_reply_markup(reply_markup=main_menu(st))
    elif what=="path":
        async with open_db() as db: await kv_set(db, "pending", {"user": c.from_user.id, "key": "source:path"})
        await c.message.edit_text("Send full filesystem path to `gifts.json`.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Home", callback_data="home")]]
        )); await c.answer()

# limits
@router.callback_query(F.data == "limits:menu")
async def cb_limits(c: CallbackQuery):
    st = await get_state()
    txt = (f"🎯 **Limits**\n"
           f"• Price cap: {st.get('max_price') or '∞'}⭐\n"
           f"• Allow IDs: {', '.join(st.get('allow_ids') or ['(any)'])}\n"
           f"• Keywords : {', '.join(st.get('allow_keys') or ['(any)'])}\n"
           f"• Window   : {st.get('window') or '(always)'}\n"
           f"• Alert>cap: {'ON ✅' if st.get('notify_only') else 'OFF ❌'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Set price cap", callback_data="lim:set:cap")],
        [InlineKeyboardButton(text="Set allow IDs", callback_data="lim:set:ids")],
        [InlineKeyboardButton(text="Set keywords",  callback_data="lim:set:keys")],
        [InlineKeyboardButton(text="Set window",    callback_data="lim:set:window")],
        [InlineKeyboardButton(text=("Alert>cap OFF" if st.get("notify_only") else "Alert>cap ON"), callback_data="lim:toggle:notify")],
        [InlineKeyboardButton(text="Clear IDs", callback_data="lim:clear:ids"),
         InlineKeyboardButton(text="Clear keys", callback_data="lim:clear:keys")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data == "lim:toggle:notify")
async def cb_toggle_notify(c: CallbackQuery):
    st = await get_state(); st["notify_only"] = not st.get("notify_only"); await save_state(st)
    await c.answer("Toggled."); await c.message.edit_reply_markup(reply_markup=main_menu(st))

@router.callback_query(F.data.in_(["lim:set:cap","lim:set:ids","lim:set:keys","lim:set:window",
                                   "recips:add","recips:del","source:path"]))
async def cb_prompts(c: CallbackQuery):
    hints = {
        "lim:set:cap":        "Send ⭐ cap number (0 = no cap).",
        "lim:set:ids":        "Send comma-separated Gift IDs.",
        "lim:set:keys":       "Send comma-separated keywords (e.g., teddy, ring).",
        "lim:set:window":     "Send window as HH:MM-HH:MM (empty = always).",
        "recips:add":         "Send numeric user ID to add.",
        "recips:del":         "Send numeric user ID to remove.",
        "source:path":        "Send filesystem path to tg_gifts_notifier JSON file."
    }
    key = c.data
    async with open_db() as db: await kv_set(db, "pending", {"user": c.from_user.id, "key": key})
    await c.message.edit_text(hints[key], reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Home", callback_data="home")]]
    )); await c.answer()

@router.callback_query(F.data == "lim:clear:ids")
async def cb_clear_ids(c: CallbackQuery):
    st = await get_state(); st["allow_ids"] = []; await save_state(st)
    await c.answer("IDs cleared."); await c.message.edit_reply_markup(reply_markup=main_menu(st))

@router.callback_query(F.data == "lim:clear:keys")
async def cb_clear_keys(c: CallbackQuery):
    st = await get_state(); st["allow_keys"] = []; await save_state(st)
    await c.answer("Keywords cleared."); await c.message.edit_reply_markup(reply_markup=main_menu(st))

# recipients
@router.callback_query(F.data == "recips:menu")
async def cb_recips(c: CallbackQuery):
    st = await get_state()
    lst = ", ".join(str(x) for x in (st.get("recips") or [])) or "(none)"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add me", callback_data="recips:addme"),
         InlineKeyboardButton(text="➕ Add ID", callback_data="recips:add")],
        [InlineKeyboardButton(text="➖ Remove ID", callback_data="recips:del")],
        [InlineKeyboardButton(text="🧹 Clear all", callback_data="recips:clear")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")]
    ])
    await c.message.edit_text(f"👥 Recipients: {lst}", reply_markup=kb); await c.answer()

@router.callback_query(F.data == "recips:addme")
async def cb_add_me(c: CallbackQuery):
    st = await get_state(); s=set(st.get("recips") or []); s.add(c.from_user.id); st["recips"]=list(s)
    await save_state(st); await c.answer("Added you."); await c.message.edit_reply_markup(reply_markup=main_menu(st))

@router.callback_query(F.data == "recips:clear")
async def cb_clear_recips(c: CallbackQuery):
    st = await get_state(); st["recips"] = []; await save_state(st)
    await c.answer("Cleared."); await c.message.edit_reply_markup(reply_markup=main_menu(st))

# single text handler (no collisions)
@router.message(F.text)
async def on_text(m: Message):
    async with open_db() as db:
        pend = await kv_get(db, "pending")
        if not pend or pend.get("user") != m.from_user.id: return
        key = pend["key"]; t = (m.text or "").strip(); st = await get_state()
        try:
            if key == "lim:set:cap": st["max_price"] = max(0, int(t)) if t else 0; await m.reply(f"Cap → {st['max_price'] or '∞'}⭐")
            elif key == "lim:set:ids": st["allow_ids"] = [x.strip() for x in t.split(",") if x.strip()]; await m.reply(f"IDs → {', '.join(st['allow_ids'] or ['(any)'])}")
            elif key == "lim:set:keys": st["allow_keys"] = [x.strip() for x in t.split(",") if x.strip()]; await m.reply(f"Keywords → {', '.join(st['allow_keys'] or ['(any)'])}")
            elif key == "lim:set:window": st["window"] = t; await m.reply(f"Window → {st['window'] or '(always)'}")
            elif key == "recips:add": rid = int(t); s=set(st.get("recips") or []); s.add(rid); st["recips"]=list(s); await m.reply(f"Recipient added: {rid}")
            elif key == "recips:del": rid = int(t); st["recips"]=[x for x in (st.get("recips") or []) if x!=rid]; await m.reply(f"Recipient removed: {rid}")
            elif key == "source:path": st["notifier_json"] = t; await m.reply(f"Notifier path set: {t}")
            await save_state(st)
        except Exception as e:
            await m.reply(f"Input error: {e}")
        await kv_set(db, "pending", None)

# catalogue (source-aware)
async def build_catalogue(page: int = 0, per_page: int = 6) -> Tuple[str, InlineKeyboardMarkup]:
    st = await get_state()
    source, gifts = await get_gifts(st)
    total = len(gifts); start = page*per_page; end = min(total, start+per_page); view = gifts[start:end]
    lines = [f"🗂 **Available Gifts** ({source})  page {page+1}/{(total+per_page-1)//per_page or 1}"]
    kb = InlineKeyboardBuilder()
    for g in view:
        gid, title, price = g["gift_id"], g["title"], int(g["star_count"])
        lines.append(f"• {title} — {price}⭐")
        kb.button(text=f"Buy {price}⭐ · {title[:16]}", callback_data=f"cata:buy:{gid}:{price}:{title[:40]}")
        kb.button(text=f"+Allow {title[:8]}", callback_data=f"cata:allow:{gid}")
    nav=[]
    if start>0: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"cata:{page-1}"))
    if end<total: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"cata:{page+1}"))
    if nav: kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🏠 Home", callback_data="home"))
    return ("\n".join(lines) if view else "_No gifts right now._"), kb.as_markup()

@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery): await c.answer("ok")

@router.callback_query(F.data.startswith("cata:"))
async def cb_cata(c: CallbackQuery):
    p = c.data.split(":")
    if p[1] in ("buy","allow"): return
    page = int(p[1]); text, kb = await build_catalogue(page)
    await c.message.edit_text(text, reply_markup=kb); await c.answer()

@router.callback_query(F.data.startswith("cata:allow:"))
async def cb_cata_allow(c: CallbackQuery):
    gid = c.data.split(":")[2]; st = await get_state()
    s=set(st.get("allow_ids") or []); s.add(gid); st["allow_ids"]=list(s); await save_state(st)
    await c.answer("Added to allowlist.")

@router.callback_query(F.data.startswith("cata:buy:"))
async def cb_cata_buy(c: CallbackQuery, bot: Bot):
    _,_,gid,price,title = c.data.split(":",3); st = await get_state()
    rid = (st.get("recips") or [c.from_user.id])[0]
    try:
        await TGAPI.send_gift(rid, gid, text="Manual check 🎁", is_private=True)
        ts = int(datetime.now(tz=timezone.utc).timestamp())
        async with open_db() as db:
            await db.execute("INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                             (gid, title, int(price), rid, "manual", ts)); await db.commit()
        await c.answer("Sent!")
    except Exception as e:
        await c.answer(f"Send failed: {e}", show_alert=True)

# override-buy from alert
@router.callback_query(F.data.startswith("ovbuy:"))
async def cb_ovbuy(c: CallbackQuery, bot: Bot):
    _, gid, price, title = c.data.split(":",3); st = await get_state()
    rid = (st.get("recips") or [c.from_user.id])[0]
    try:
        await TGAPI.send_gift(rid, gid, text="Override buy 🎁", is_private=True)
        ts = int(datetime.now(tz=timezone.utc).timestamp())
        async with open_db() as db:
            await db.execute("INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                             (gid, title, int(price), rid, "override", ts)); await db.commit()
        await c.answer("Bought and sent!")
    except Exception as e:
        await c.answer(f"Override failed: {e}", show_alert=True)

# balance & tx
@router.callback_query(F.data == "bal:menu")
async def cb_bal(c: CallbackQuery):
    try: stars = await TGAPI.get_my_star_balance()
    except Exception as e:
        await c.message.edit_text(f"Balance error: {e}", reply_markup=back_home()); await c.answer(); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📒 Last 10 tx", callback_data="bal:tx")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")]
    ])
    await c.message.edit_text(f"💫 Bot balance: **{stars}⭐**", reply_markup=kb); await c.answer()

@router.callback_query(F.data == "bal:tx")
async def cb_tx(c: CallbackQuery):
    try: items = await TGAPI.get_star_transactions(limit=10)
    except Exception as e:
        await c.message.edit_text(f"Tx error: {e}", reply_markup=back_home()); await c.answer(); return
    lines = [f"{i.get('date','')} — {i.get('amount',0):+}⭐" for i in items]
    await c.message.edit_text("🧾 **Transactions**\n" + ("\n".join(lines) if lines else "_None_"), reply_markup=back_home()); await c.answer()

# top up (Stars/XTR)
def topup_kb():
    kb = InlineKeyboardBuilder()
    for amt in (50, 200, 500, 1000, 3000): kb.button(text=f"Add {amt}⭐", callback_data=f"top:go:{amt}")
    kb.button(text="🏠 Home", callback_data="home"); kb.adjust(3,2,1); return kb.as_markup()

@router.callback_query(F.data == "topup:menu")
async def cb_topup_menu(c: CallbackQuery):
    await c.message.edit_text("Add Stars to bot balance:", reply_markup=topup_kb()); await c.answer()

@router.callback_query(F.data.startswith("top:go:"))
async def cb_topup_go(c: CallbackQuery, bot: Bot):
    amt = int(c.data.split(":")[-1])
    await bot.send_invoice(chat_id=c.message.chat.id, title="Top up Stars", description=f"Add {amt}⭐ to bot balance",
                           payload=f"topup:{amt}", currency="XTR", prices=[LabeledPrice(label=f"{amt}⭐", amount=amt)])
    await c.answer("Invoice created")

@router.pre_checkout_query()
async def pre(q: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query_id=q.id, ok=True)

@router.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment; amt = getattr(sp,"total_amount",0) or getattr(sp,"total_star_amount",0) or 0
    await m.reply(f"✅ Received **{amt}⭐**.")

# health / logs
@router.callback_query(F.data == "health:run")
async def cb_health(c: CallbackQuery, bot: Bot):
    try:
        me = await bot.get_me()
        stars = await TGAPI.get_my_star_balance()
        st = await get_state()
        source, gifts = await get_gifts(st)
        weather = "n/a"
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get("https://wttr.in/?format=3", timeout=5) as r:
                    weather = await r.text() if r.status==200 else f"http {r.status}"
        except Exception: weather = "fail"
        path = st.get("notifier_json") or "(none)"
        txt = (f"🩺 **Health**\n"
               f"Bot: @{getattr(me,'username','?')} (ok)\n"
               f"Stars: {stars}⭐ (ok)\n"
               f"Source: {source} · file: {path}\n"
               f"Gifts available: {len(gifts)}\n"
               f"Weather ping: {weather}")
        await c.message.edit_text(txt, reply_markup=back_home())
    except Exception as e:
        await c.message.edit_text(f"Health error: {e}", reply_markup=back_home())
    await c.answer()

@router.callback_query(F.data == "logs:open")
async def cb_logs(c: CallbackQuery):
    async with open_db() as db:
        async with db.execute("SELECT ts,level,msg FROM events ORDER BY id DESC LIMIT 20") as cur:
            rows = await cur.fetchall()
    lines = [(datetime.fromtimestamp(ts).strftime("%H:%M:%S")+" ["+lvl+"] "+msg) for ts,lvl,msg in rows]
    await c.message.edit_text("🧾 **Recent Logs**\n" + ("\n".join(lines) if lines else "_Empty_"), reply_markup=back_home()); await c.answer()

# bootstrap
async def main():
    if not BOT_TOKEN: raise SystemExit("BOT_TOKEN missing.")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(); dp.include_router(router)
    async with open_db() as db:
        await db.executescript(CREATE_SQL); await db.commit()
        if await kv_get(db,"state") is None:
            await kv_set(db,"state",{
                "running": False, "speed": "FAST",
                "max_price": DEFAULT_MAX_PRICE,
                "allow_ids": DEFAULT_ALLOW_IDS, "allow_keys": DEFAULT_ALLOW_KEYS,
                "recips": DEFAULT_RECIPS or (ADMIN_IDS[:1] if ADMIN_IDS else []),
                "window": DEFAULT_WINDOW, "notify_only": DEFAULT_NOTIFY_ONLY,
                "feed_mode": FEED_MODE, "notifier_json": NOTIFIER_JSON
            })
    await dp.start_polling(bot, allowed_updates=["message","callback_query","pre_checkout_query"])

if __name__ == "__main__":
    asyncio.run(main())
