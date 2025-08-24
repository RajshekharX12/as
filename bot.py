import asyncio, json, logging, os
from contextlib import asynccontextmanager
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
    DEFAULT_MIN_PRICE, DEFAULT_MAX_PRICE, DEFAULT_ALLOW_IDS, DEFAULT_ALLOW_KEYS,
    DEFAULT_RECIPS, BUY_COOLDOWN_SEC, DEFAULT_WINDOW,
    DEFAULT_NOTIFICATIONS, DEFAULT_CYCLES, DEFAULT_OVERALL_LIMIT, DEFAULT_SUPPLY_LIMIT,
    FEED_MODE, NOTIFIER_JSON, NOTIFIER_POLL_SEC
)

logging.basicConfig(level=logging.INFO)
router = Router()

# ------------------- Async DB -------------------
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

@asynccontextmanager
async def open_db():
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.executescript(CREATE_SQL)
        await db.commit()
        yield db
    finally:
        await db.close()

async def kv_get(key: str, default=None):
    async with open_db() as db:
        async with db.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return json.loads(row[0]) if row else default

async def kv_set(key: str, value):
    s = json.dumps(value)
    async with open_db() as db:
        await db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, s)
        )
        await db.commit()

async def log_event(level: str, msg: str):
    async with open_db() as db:
        await db.execute("INSERT INTO events(ts,level,msg) VALUES (?,?,?)",
                         (int(datetime.now(tz=timezone.utc).timestamp()), level, msg))
        await db.commit()

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

# ------------------- Owner / Admin -------------------
async def get_owner_id() -> Optional[int]:
    return await kv_get("owner_id")

def is_admin(uid: int, owner: Optional[int]) -> bool:
    return (owner is not None and uid == owner) or (ADMIN_IDS and uid in ADMIN_IDS) or (not ADMIN_IDS and owner is None)

# ------------------- State / helpers -------------------
AUTOBUY_TASK: Optional[asyncio.Task] = None
RECENT_TS: dict[str, int] = {}
RECENT_QUEUE = deque(maxlen=200)

# per-run counters (reset when you press Start)
RUN = {"cycles_left": None, "spent": 0, "bought": 0}

def interval_for(speed:str)->float:
    s = (speed or "FAST").upper()
    return INTERVAL_INSANE if s=="INSANE" else INTERVAL_NORMAL if s=="NORMAL" else INTERVAL_FAST

def parse_window(window: str) -> Optional[Tuple[time, time]]:
    if not window: return None
    try:
        s,e = window.split("-",1)
        h1,m1 = map(int, s.split(":")); h2,m2 = map(int, e.split(":"))
        return time(h1,m1), time(h2,m2)
    except Exception:
        return None

def within_window(now: datetime, window: str) -> bool:
    tpair = parse_window(window)
    if not tpair: return True
    t1, t2 = tpair; nt = now.time()
    return (t1<=nt<=t2) if (t1<=t2) else (nt>=t1 or nt<=t2)

def normalize_gift_dict(x: Dict[str,Any]) -> Dict[str,Any]:
    gid = str(x.get("id") or x.get("gift_id") or "")
    title = str(x.get("title") or x.get("name") or x.get("base_name") or "Gift")
    price = int(x.get("star_count") or (x.get("price") or {}).get("star_count") or x.get("price") or 0)
    return {"gift_id": gid, "title": title, "star_count": price}

async def gifts_from_notifier(path: str) -> List[Dict[str,Any]]:
    try:
        if not path or not os.path.exists(path): return []
        raw = await asyncio.to_thread(lambda: open(path, "r", encoding="utf-8").read())
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
    st = await kv_get("state") or {}
    st.setdefault("running", False)
    st.setdefault("speed", "FAST")
    st.setdefault("min_price", DEFAULT_MIN_PRICE)
    st.setdefault("max_price", DEFAULT_MAX_PRICE)
    st.setdefault("allow_ids", DEFAULT_ALLOW_IDS)
    st.setdefault("allow_keys", DEFAULT_ALLOW_KEYS)
    st.setdefault("recips", DEFAULT_RECIPS)
    st.setdefault("window", DEFAULT_WINDOW)
    st.setdefault("notify", DEFAULT_NOTIFICATIONS)
    st.setdefault("cycles", DEFAULT_CYCLES)
    st.setdefault("overall_limit", DEFAULT_OVERALL_LIMIT)
    st.setdefault("supply_limit", DEFAULT_SUPPLY_LIMIT)
    st.setdefault("feed_mode", FEED_MODE)
    st.setdefault("notifier_json", NOTIFIER_JSON)
    return st

async def save_state(st): 
    await kv_set("state", st)

def matches(g: Dict[str,Any], allow_ids: List[str], allow_keys: List[str],
            min_price: int, max_price: int) -> Tuple[bool, int, str, str]:
    gid, title, price = g["gift_id"], g["title"], int(g["star_count"])
    ok_id  = (not allow_ids)  or (gid in allow_ids)
    ok_key = (not allow_keys) or any(k.lower() in title.lower() for k in allow_keys)
    ok_min = (min_price<=0) or (price >= min_price)
    ok_max = (max_price<=0) or (price <= max_price)
    return (ok_id and ok_key and ok_min and ok_max), price, gid, title

# ------------------- Inline UI -------------------
def main_menu(st: dict):
    running = st.get("running", False); speed = st.get("speed", "FAST")
    kb = InlineKeyboardBuilder()
    kb.button(text=("⚡ Auto-Buy: ON" if running else "⚡ Auto-Buy: OFF"), callback_data="auto:toggle")
    kb.button(text=f"📡 Source: {st.get('feed_mode','api').upper()}", callback_data="source:menu")
    kb.button(text="🎛 Auto-Purchase Settings", callback_data="settings:menu")
    kb.button(text="🎁 Gift Catalog", callback_data="cata:0")
    kb.button(text="👤 Profile", callback_data="profile:open")
    kb.button(text="💫 Deposit", callback_data="topup:menu")
    kb.button(text="🩺 Health", callback_data="health:run")
    kb.button(text="🧾 Logs", callback_data="logs:open")
    kb.adjust(2,1,2,2)
    return kb.as_markup()

def back_home(): 
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="home")]])

# ------------------- Worker -------------------
async def autobuy_loop(bot: Bot):
    await log_event("INFO", "autobuy loop started")
    while True:
        try:
            st = await get_state()
            if not st.get("running"):
                await asyncio.sleep(1.0); continue
            if not within_window(datetime.now(), st.get("window","")):
                await asyncio.sleep(1.0); continue

            # stop if cycles exhausted
            if RUN["cycles_left"] is not None:
                if RUN["cycles_left"] <= 0:
                    st["running"] = False
                    await save_state(st)
                    await log_event("INFO", "AutoBuy stopped (cycles done)")
                    continue
                RUN["cycles_left"] -= 1

            # stop if overall limit or supply limit hit
            if st.get("overall_limit",0)>0 and RUN["spent"] >= st["overall_limit"]:
                st["running"]=False; await save_state(st); await log_event("INFO","Stopped (overall limit)"); continue
            if st.get("supply_limit",0)>0 and RUN["bought"] >= st["supply_limit"]:
                st["running"]=False; await save_state(st); await log_event("INFO","Stopped (supply limit)"); continue

            source, items = await get_gifts(st)
            allow_ids  = st.get("allow_ids") or []
            allow_keys = st.get("allow_keys") or []
            min_price  = int(st.get("min_price") or 0)
            max_price  = int(st.get("max_price") or 0)
            recips     = list(st.get("recips") or [])
            notify     = bool(st.get("notify", True))

            if not recips:
                owner = await get_owner_id()
                if owner: recips=[owner]
                else:
                    await log_event("WARN","No recipients configured."); await asyncio.sleep(0.5); continue

            for g in items:
                ok, price, gid, title = matches(g, allow_ids, allow_keys, min_price, max_price)
                id_key_ok = ((not allow_ids) or (gid in allow_ids)) and ((not allow_keys) or any(k.lower() in title.lower() for k in allow_keys))

                ts_now = int(datetime.now(tz=timezone.utc).timestamp())
                last = RECENT_TS.get(gid, 0)
                if last and (ts_now - last) < BUY_COOLDOWN_SEC:
                    continue

                # If range not satisfied but title/id matched, we can notify (if enabled)
                if not ok and not (notify and id_key_ok):
                    continue

                rid = recips[0]

                if not ok and notify and id_key_ok:
                    try:
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text=f"BUY {price}⭐", callback_data=f"ovbuy:{gid}:{price}:{title[:40]}")],
                            [InlineKeyboardButton(text="Dismiss", callback_data="noop")]
                        ])
                        await bot.send_message(rid, f"⚠️ Found match over range: {title} ({price}⭐)\n`{gid}`", reply_markup=kb)
                        await log_event("INFO", f"notify {gid} {price}⭐ {source}")
                    except Exception as e:
                        await log_event("ERROR", f"notify send failed: {e}")
                    continue

                # Respect overall/supply limits before buying
                if st.get("overall_limit",0)>0 and (RUN["spent"] + price) > st["overall_limit"]:
                    continue
                if st.get("supply_limit",0)>0 and (RUN["bought"] + 1) > st["supply_limit"]:
                    continue

                try:
                    await TGAPI.send_gift(rid, gid, text="Auto 🎁", is_private=True)
                    RUN["bought"] += 1
                    RUN["spent"]  += price
                    async with open_db() as db:
                        await db.execute(
                            "INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                            (gid, title, int(price), rid, "sent", ts_now)
                        )
                        await db.commit()
                    if notify:
                        await bot.send_message(rid, f"✅ Bought {title} for {price}⭐")
                    await log_event("INFO", f"auto-sent {title} ({price}⭐) → {rid} [{source}]")
                    RECENT_TS[gid] = ts_now; RECENT_QUEUE.append((ts_now, gid, title, price, rid))
                except Exception as e:
                    await log_event("ERROR", f"sendGift {gid} → {rid} failed: {e}")
            await asyncio.sleep(interval_for(st.get("speed","FAST")) if st.get("feed_mode","api")=="api" else NOTIFIER_POLL_SEC)
        except Exception as e:
            await log_event("ERROR", f"loop crash: {e}")
            await asyncio.sleep(0.5)

# ------------------- Handlers -------------------
@router.message(CommandStart())
async def start(m: Message):
    owner = await get_owner_id()
    if owner is None:
        await kv_set("owner_id", m.from_user.id)
        owner = m.from_user.id
        await log_event("INFO", f"Owner set to {owner}")
    st = await get_state()
    if not st.get("recips"):
        st["recips"] = [owner]
        await save_state(st)
    await m.answer("🎄 **Autogifts**\nAuto-purchase with cycles & limits.\nUse the buttons below.",
                   reply_markup=main_menu(st))

@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    st = await get_state()
    await c.message.edit_text("Main menu:", reply_markup=main_menu(st)); await c.answer()

# Profile screen
@router.callback_query(F.data == "profile:open")
async def cb_profile(c: CallbackQuery):
    try:
        me = await c.bot.get_me()
        stars = await TGAPI.get_my_star_balance()
    except Exception:
        stars = 0
    st = await get_state()
    txt = (f"👤 **Profile**\n"
           f"Owner: `{await get_owner_id()}`\n"
           f"Balance: **{stars}⭐**\n\n"
           f"⚙️ Auto-Purchase:\n"
           f"• Cycles: {st.get('cycles') or '∞'}\n"
           f"• Notifications: {'on' if st.get('notify') else 'off'}\n"
           f"• Auto-Buy: {'on' if st.get('running') else 'off'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Notifications: {'on' if st.get('notify') else 'off'}", callback_data="notify:toggle"),
         InlineKeyboardButton(text=f"Auto-Buy: {'on' if st.get('running') else 'off'}", callback_data="auto:toggle")],
        [InlineKeyboardButton(text="🎛 Auto-Purchase Settings", callback_data="settings:menu")],
        [InlineKeyboardButton(text="🏠 Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data == "notify:toggle")
async def cb_notify_toggle(c: CallbackQuery):
    st = await get_state(); st["notify"] = not st.get("notify"); await save_state(st)
    await c.answer("Toggled."); await cb_profile(c)

# Auto toggle
@router.callback_query(F.data == "auto:toggle")
async def cb_toggle(c: CallbackQuery, bot: Bot):
    st = await get_state(); st["running"] = not st.get("running")
    # reset per-run counters when starting
    if st["running"]:
        RUN["spent"] = 0; RUN["bought"] = 0
        RUN["cycles_left"] = (st.get("cycles") or 0) if st.get("cycles") else None
        # spin loop if not alive
        global AUTOBUY_TASK
        if AUTOBUY_TASK is None or AUTOBUY_TASK.done():
            AUTOBUY_TASK = asyncio.create_task(autobuy_loop(bot))
        await c.answer("Auto-Buy ON")
    else:
        await c.answer("Auto-Buy OFF")
    await save_state(st)
    await c.message.edit_reply_markup(reply_markup=main_menu(st))

# Source switch
@router.callback_query(F.data == "source:menu")
async def cb_source_menu(c: CallbackQuery):
    st = await get_state()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Use API",      callback_data="source:set:api"),
         InlineKeyboardButton(text="Use Notifier", callback_data="source:set:notifier")],
        [InlineKeyboardButton(text="Set JSON path", callback_data="source:set:path")],
        [InlineKeyboardButton(text="🏠 Back", callback_data="home")]
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
        await kv_set("pending", {"user": c.from_user.id, "key": "source:path"})
        await c.message.edit_text("Send filesystem path to `gifts.json` written by the notifier.",
                                  reply_markup=back_home()); await c.answer()

# Settings screen
@router.callback_query(F.data == "settings:menu")
async def cb_settings(c: CallbackQuery):
    st = await get_state()
    txt = (f"⚙️ **Auto-Purchase Settings**\n\n"
           f"Current Parameters: ✅\n"
           f"— Cycles: {st.get('cycles') or '∞'}\n"
           f"— Lower Limit: {st.get('min_price') or 0} ⭐\n"
           f"— Upper Limit: {st.get('max_price') or '∞'} ⭐\n"
           f"— Overall Limit: {st.get('overall_limit') or '∞'} ⭐\n"
           f"— Supply Limit: {st.get('supply_limit') or '∞'}\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cycles ↻", callback_data="set:cycles")],
        [InlineKeyboardButton(text="Lower Limit 🔻", callback_data="set:min"),
         InlineKeyboardButton(text="Upper Limit 🔺", callback_data="set:max")],
        [InlineKeyboardButton(text="Overall Limit ⭐", callback_data="set:overall"),
         InlineKeyboardButton(text="Supply Limit 📦", callback_data="set:supply")],
        [InlineKeyboardButton(text="🏠 Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data.in_(["set:cycles","set:min","set:max","set:overall","set:supply",
                                   "lim:set:ids","lim:set:keys","lim:set:window",
                                   "recips:add","recips:del","source:path"]))
async def cb_prompts(c: CallbackQuery):
    hints = {
        "set:cycles":  "Send number of cycles (0 = ∞).",
        "set:min":     "Send lower price limit in ⭐ (0 = no minimum).",
        "set:max":     "Send upper price limit in ⭐ (0 = no maximum).",
        "set:overall": "Send overall Stars budget for THIS RUN (0 = unlimited).",
        "set:supply":  "Send max pieces to buy THIS RUN (0 = unlimited).",
        "lim:set:ids": "Send comma-separated Gift IDs to allow (empty = any).",
        "lim:set:keys":"Send comma-separated keywords (e.g., teddy, ring).",
        "lim:set:window":"Send window as HH:MM-HH:MM (empty = always).",
        "recips:add":  "Send numeric user ID to add as recipient.",
        "recips:del":  "Send numeric user ID to remove.",
        "source:path": "Send full filesystem path to notifier JSON."
    }
    key = c.data
    await kv_set("pending", {"user": c.from_user.id, "key": key})
    await c.message.edit_text(hints[key], reply_markup=back_home()); await c.answer()

# Limits & recipients quick menu from profile
@router.callback_query(F.data == "lim:quick")
async def cb_lim_quick(c: CallbackQuery):
    st = await get_state()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Allow IDs", callback_data="lim:set:ids"),
         InlineKeyboardButton(text="Keywords", callback_data="lim:set:keys")],
        [InlineKeyboardButton(text="Window", callback_data="lim:set:window")],
        [InlineKeyboardButton(text="🏠 Back", callback_data="home")]
    ])
    await c.message.edit_text("Edit filters:", reply_markup=kb); await c.answer()

@router.callback_query(F.data == "recips:menu")
async def cb_recips(c: CallbackQuery):
    st = await get_state()
    lst = ", ".join(str(x) for x in (st.get("recips") or [])) or "(none)"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add me", callback_data="recips:addme"),
         InlineKeyboardButton(text="➕ Add ID", callback_data="recips:add")],
        [InlineKeyboardButton(text="➖ Remove ID", callback_data="recips:del")],
        [InlineKeyboardButton(text="🧹 Clear all", callback_data="recips:clear")],
        [InlineKeyboardButton(text="🏠 Back", callback_data="home")]
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

# Single text handler (no collisions)
@router.message(F.text)
async def on_text(m: Message):
    pend = await kv_get("pending")
    if not pend or pend.get("user") != m.from_user.id: return
    key = pend["key"]; t = (m.text or "").strip(); st = await get_state()
    try:
        if key == "set:cycles":  st["cycles"] = max(0, int(t)) if t else 0; await m.reply(f"Cycles → {st['cycles'] or '∞'}")
        elif key == "set:min":   st["min_price"] = max(0, int(t)) if t else 0; await m.reply(f"Lower limit → {st['min_price']}⭐")
        elif key == "set:max":   st["max_price"] = max(0, int(t)) if t else 0; await m.reply(f"Upper limit → {st['max_price'] or '∞'}⭐")
        elif key == "set:overall": st["overall_limit"] = max(0, int(t)) if t else 0; RUN["spent"]=0; await m.reply(f"Overall limit → {st['overall_limit'] or '∞'}⭐")
        elif key == "set:supply":  st["supply_limit"] = max(0, int(t)) if t else 0; RUN["bought"]=0; await m.reply(f"Supply limit → {st['supply_limit'] or '∞'} pcs")
        elif key == "lim:set:ids":  st["allow_ids"] = [x.strip() for x in t.split(",") if x.strip()]; await m.reply(f"IDs → {', '.join(st['allow_ids'] or ['(any)'])}")
        elif key == "lim:set:keys": st["allow_keys"] = [x.strip() for x in t.split(",") if x.strip()]; await m.reply(f"Keywords → {', '.join(st['allow_keys'] or ['(any)'])}")
        elif key == "lim:set:window": st["window"] = t; await m.reply(f"Window → {st['window'] or '(always)'}")
        elif key == "recips:add":
            rid = int(t); s=set(st.get("recips") or []); s.add(rid); st["recips"]=list(s); await m.reply(f"Recipient added: {rid}")
        elif key == "recips:del":
            rid = int(t); st["recips"]=[x for x in (st.get("recips") or []) if x!=rid]; await m.reply(f"Recipient removed: {rid}")
        elif key == "source:path": st["notifier_json"] = t; await m.reply(f"Notifier path set: {t}")
        await save_state(st)
    except Exception as e:
        await m.reply(f"Input error: {e}")
    await kv_set("pending", None)

# Catalogue (source-aware)
async def build_catalogue(page: int = 0, per_page: int = 6) -> Tuple[str, InlineKeyboardMarkup]:
    st = await get_state()
    source, gifts = await get_gifts(st)
    total = len(gifts); start = page*per_page; end = min(total, start+per_page); view = gifts[start:end]
    lines = [f"🎁 **Gift Catalog** ({source})  page {page+1}/{(total+per_page-1)//per_page or 1}"]
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
    kb.row(InlineKeyboardButton(text="🏠 Back", callback_data="home"))
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
        await TGAPI.send_gift(rid, gid, text="Manual 🎁", is_private=True)
        ts = int(datetime.now(tz=timezone.utc).timestamp())
        async with open_db() as db:
            await db.execute("INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                             (gid, title, int(price), rid, "manual", ts)); await db.commit()
        await c.answer("Sent!")
    except Exception as e:
        await c.answer(f"Send failed: {e}", show_alert=True)

# Override-buy from alerts
@router.callback_query(F.data.startswith("ovbuy:"))
async def cb_ovbuy(c: CallbackQuery, bot: Bot):
    _, gid, price, title = c.data.split(":",3); st = await get_state()
    rid = (st.get("recips") or [c.from_user.id])[0]
    try:
        await TGAPI.send_gift(rid, gid, text="Override 🎁", is_private=True)
        ts = int(datetime.now(tz=timezone.utc).timestamp())
        async with open_db() as db:
            await db.execute("INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                             (gid, title, int(price), rid, "override", ts)); await db.commit()
        await c.answer("Bought and sent!")
    except Exception as e:
        await c.answer(f"Override failed: {e}", show_alert=True)

# Balance & tx
@router.callback_query(F.data == "topup:menu")
async def cb_topup_menu(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for amt in (50, 200, 500, 1000, 3000): kb.button(text=f"Add {amt}⭐", callback_data=f"top:go:{amt}")
    kb.button(text="🏠 Back", callback_data="home"); kb.adjust(3,2,1)
    await c.message.edit_text("Add Stars to bot balance:", reply_markup=kb.as_markup()); await c.answer()

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

# Health / logs
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
        txt = (f"🩺 **Health**\n"
               f"Bot: @{getattr(me,'username','?')} (ok)\n"
               f"Stars: {stars}⭐ (ok)\n"
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

# Bootstrap
async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing. Put it in .env or environment.")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(); dp.include_router(router)
    # ensure DB exists & defaults saved
    st = await get_state()
    await kv_set("state", st)
    await dp.start_polling(bot, allowed_updates=["message","callback_query","pre_checkout_query"])

if __name__ == "__main__":
    asyncio.run(main())
