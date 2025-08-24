import asyncio, json, logging, os
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp, aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)

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

# ---------------- DB ----------------
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
-- ledger: internal credits/debits we control (refund-like credits, topups, spends)
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,   -- +credit, -debit
  note TEXT
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

async def ledger_add(user_id:int, amount:int, note:str):
    async with open_db() as db:
        await db.execute("INSERT INTO ledger(ts,user_id,amount,note) VALUES (?,?,?,?)",
                         (int(datetime.now(tz=timezone.utc).timestamp()), user_id, amount, note))
        await db.commit()

async def ledger_balance(user_id:int)->int:
    async with open_db() as db:
        async with db.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user_id=?", (user_id,)) as cur:
            (s,) = await cur.fetchone()
            return int(s or 0)

# -------------- Bot API (thin) --------------
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

TGAPI = TG(BOT_TOKEN)

# -------------- owner/admin --------------
async def get_owner_id() -> Optional[int]:
    return await kv_get("owner_id")

def is_admin(uid: int, owner: Optional[int]) -> bool:
    return (owner is not None and uid == owner) or (ADMIN_IDS and uid in ADMIN_IDS) or (not ADMIN_IDS and owner is None)

# -------------- state --------------
AUTOBUY_TASK: Optional[asyncio.Task] = None
RUN = {"cycles_left": None, "spent": 0, "bought": 0}  # per-run counts

def interval_for(speed:str)->float:
    s = (speed or "FAST").upper()
    return INTERVAL_INSANE if s=="INSANE" else INTERVAL_NORMAL if s=="NORMAL" else INTERVAL_FAST

def parse_window(window: str) -> Optional[Tuple[time,time]]:
    if not window: return None
    try:
        s,e = window.split("-",1); h1,m1 = map(int,s.split(":")); h2,m2 = map(int,e.split(":"))
        return time(h1,m1), time(h2,m2)
    except: return None

def within_window(now: datetime, window: str) -> bool:
    tpair = parse_window(window)
    if not tpair: return True
    t1,t2 = tpair; nt = now.time()
    return (t1<=nt<=t2) if (t1<=t2) else (nt>=t1 or nt<=t2)

def normalize_gift_dict(x: Dict[str,Any]) -> Dict[str,Any]:
    gid = str(x.get("id") or x.get("gift_id") or "")
    title = str(x.get("title") or x.get("name") or x.get("base_name") or "Gift")
    price = int(x.get("star_count") or (x.get("price") or {}).get("star_count") or x.get("price") or 0)
    return {"gift_id": gid, "title": title, "star_count": price}

async def gifts_from_notifier(path: str) -> List[Dict[str,Any]]:
    try:
        if not path or not os.path.exists(path): return []
        raw = await asyncio.to_thread(lambda: open(path,"r",encoding="utf-8").read())
        data = json.loads(raw)
        items = data["gifts"] if isinstance(data,dict) and "gifts" in data else data
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
        return ("api-fallback", await gifts_from_api())
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

# -------------- UI --------------
def main_menu(st: dict):
    kb = [
      [InlineKeyboardButton(text=("Auto-Buy: ON" if st.get("running") else "Auto-Buy: OFF"), callback_data="auto:toggle"),
       InlineKeyboardButton(text=f"Source: {st.get('feed_mode','api').upper()}", callback_data="source:menu")],
      [InlineKeyboardButton(text="Auto-Purchase Settings", callback_data="settings:menu")],
      [InlineKeyboardButton(text="Gift Catalog", callback_data="cata:0")],
      [InlineKeyboardButton(text="Profile", callback_data="profile:open"),
       InlineKeyboardButton(text="Deposit", callback_data="topup:menu")],
      [InlineKeyboardButton(text="Health", callback_data="health:run"),
       InlineKeyboardButton(text="Logs", callback_data="logs:open")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_home(): 
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="home")]])

# -------------- worker --------------
async def autobuy_loop(bot: Bot):
    while True:
        try:
            st = await get_state()
            if not st.get("running"): await asyncio.sleep(1.0); continue
            if not within_window(datetime.now(), st.get("window","")): await asyncio.sleep(1.0); continue

            if RUN["cycles_left"] is not None:
                if RUN["cycles_left"] <= 0:
                    st["running"]=False; await save_state(st); await log_event("INFO","stopped: cycles"); continue
                RUN["cycles_left"] -= 1
            if st.get("overall_limit",0)>0 and RUN["spent"] >= st["overall_limit"]:
                st["running"]=False; await save_state(st); await log_event("INFO","stopped: overall limit"); continue
            if st.get("supply_limit",0)>0 and RUN["bought"] >= st["supply_limit"]:
                st["running"]=False; await save_state(st); await log_event("INFO","stopped: supply limit"); continue

            source, items = await get_gifts(st)
            allow_ids  = st.get("allow_ids") or []
            allow_keys = st.get("allow_keys") or []
            mn  = int(st.get("min_price") or 0)
            mx  = int(st.get("max_price") or 0)
            recips = list(st.get("recips") or [])
            if not recips:
                owner = await get_owner_id()
                if owner: recips=[owner]
                else: await asyncio.sleep(0.5); continue
            rid = recips[0]

            for g in items:
                gid, title, price = g["gift_id"], g["title"], int(g["star_count"])
                ok_id  = (not allow_ids)  or (gid in allow_ids)
                ok_kw  = (not allow_keys) or any(k.lower() in title.lower() for k in allow_keys)
                ok_min = (mn<=0) or (price>=mn)
                ok_max = (mx<=0) or (price<=mx)
                if not (ok_id and ok_kw and ok_min and ok_max):
                    continue
                if st.get("overall_limit",0)>0 and (RUN["spent"] + price) > st["overall_limit"]: continue
                if st.get("supply_limit",0)>0 and (RUN["bought"] + 1) > st["supply_limit"]: continue

                try:
                    await TGAPI.send_gift(rid, gid, text="Auto", is_private=True)
                    ts = int(datetime.now(tz=timezone.utc).timestamp())
                    async with open_db() as db:
                        await db.execute("INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                                         (gid, title, price, rid, "sent", ts)); await db.commit()
                    RUN["bought"] += 1; RUN["spent"] += price
                    await ledger_add(rid, -price, f"Auto buy: {title}")
                    await log_event("INFO", f"auto-sent {title} ({price}⭐) -> {rid} [{source}]")
                    if st.get("notify",True):
                        await bot.send_message(rid, f"Bought {title} for {price}⭐")
                except Exception as e:
                    await log_event("ERROR", f"sendGift {gid} -> {rid} failed: {e}")
            await asyncio.sleep(INTERVAL_FAST if st.get("speed","FAST")=="FAST" else INTERVAL_INSANE if st.get("speed")=="INSANE" else INTERVAL_NORMAL if st.get("speed")=="NORMAL" else INTERVAL_FAST)
        except Exception as e:
            await log_event("ERROR", f"loop crash: {e}")
            await asyncio.sleep(0.5)

# -------------- handlers --------------
@router.message(CommandStart())
async def start(m: Message):
    owner = await get_owner_id()
    if owner is None:
        await kv_set("owner_id", m.from_user.id)
        owner = m.from_user.id
        await log_event("INFO", f"Owner set {owner}")
    st = await get_state()
    if not st.get("recips"):
        st["recips"] = [owner]; await save_state(st)
    await m.answer("Autogifts is ready. Use the buttons below.", reply_markup=main_menu(st))

@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    st = await get_state()
    await c.message.edit_text("Menu", reply_markup=main_menu(st)); await c.answer()

# profile
@router.callback_query(F.data == "profile:open")
async def cb_profile(c: CallbackQuery):
    try: stars = await TGAPI.get_my_star_balance()
    except Exception: stars = 0
    owner = await get_owner_id()
    credit = await ledger_balance(owner or c.from_user.id)
    st = await get_state()
    txt = (f"Profile\n"
           f"Balance (wallet): {stars}⭐\n"
           f"Internal credit: {credit}⭐\n\n"
           f"Auto-Purchase\n"
           f"Cycles: {st.get('cycles') or '∞'}\n"
           f"Notifications: {'on' if st.get('notify') else 'off'}\n"
           f"Auto-Buy: {'on' if st.get('running') else 'off'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Notifications: {'on' if st.get('notify') else 'off'}", callback_data="notify:toggle"),
         InlineKeyboardButton(text=f"Auto-Buy: {'on' if st.get('running') else 'off'}", callback_data="auto:toggle")],
        [InlineKeyboardButton(text="Refunds / Credits", callback_data="refund:menu")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data == "notify:toggle")
async def cb_notify_toggle(c: CallbackQuery):
    st = await get_state(); st["notify"] = not st.get("notify"); await save_state(st)
    await cb_profile(c)

@router.callback_query(F.data == "auto:toggle")
async def cb_toggle(c: CallbackQuery, bot: Bot):
    st = await get_state(); st["running"] = not st.get("running")
    if st["running"]:
        RUN["spent"]=0; RUN["bought"]=0
        RUN["cycles_left"] = (st.get("cycles") or 0) if st.get("cycles") else None
        global AUTOBUY_TASK
        if AUTOBUY_TASK is None or AUTOBUY_TASK.done():
            AUTOBUY_TASK = asyncio.create_task(autobuy_loop(bot))
    await save_state(st)
    await c.answer("Toggled")
    await c.message.edit_reply_markup(reply_markup=main_menu(st))

# settings
@router.callback_query(F.data == "settings:menu")
async def cb_settings(c: CallbackQuery):
    st = await get_state()
    txt = (f"Auto-Purchase Settings\n"
           f"Cycles: {st.get('cycles') or '∞'}\n"
           f"Lower limit: {st.get('min_price') or 0}⭐\n"
           f"Upper limit: {st.get('max_price') or '∞'}⭐\n"
           f"Overall limit: {st.get('overall_limit') or '∞'}⭐\n"
           f"Supply limit: {st.get('supply_limit') or '∞'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cycles", callback_data="set:cycles")],
        [InlineKeyboardButton(text="Lower limit", callback_data="set:min"),
         InlineKeyboardButton(text="Upper limit", callback_data="set:max")],
        [InlineKeyboardButton(text="Overall limit", callback_data="set:overall"),
         InlineKeyboardButton(text="Supply limit", callback_data="set:supply")],
        [InlineKeyboardButton(text="Filters", callback_data="filters:menu")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data == "filters:menu")
async def cb_filters(c: CallbackQuery):
    st = await get_state()
    txt = (f"Filters\n"
           f"IDs: {', '.join(st.get('allow_ids') or ['(any)'])}\n"
           f"Keywords: {', '.join(st.get('allow_keys') or ['(any)'])}\n"
           f"Window: {st.get('window') or '(always)'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Allow IDs", callback_data="lim:set:ids"),
         InlineKeyboardButton(text="Keywords", callback_data="lim:set:keys")],
        [InlineKeyboardButton(text="Window", callback_data="lim:set:window")],
        [InlineKeyboardButton(text="Back", callback_data="settings:menu")]
    ])
    await c.message.edit_text(txt, reply_markup=kb); await c.answer()

@router.callback_query(F.data.in_(["set:cycles","set:min","set:max","set:overall","set:supply",
                                   "lim:set:ids","lim:set:keys","lim:set:window","source:path"]))
async def cb_prompts(c: CallbackQuery):
    hints = {
        "set:cycles":"Send number of cycles (0 = ∞).",
        "set:min":"Send lower price limit in stars (0 = none).",
        "set:max":"Send upper price limit in stars (0 = none).",
        "set:overall":"Send overall stars budget for this run (0 = unlimited).",
        "set:supply":"Send max pieces to buy this run (0 = unlimited).",
        "lim:set:ids":"Send comma-separated Gift IDs (empty = any).",
        "lim:set:keys":"Send comma-separated keywords (e.g., teddy, ring).",
        "lim:set:window":"Send HH:MM-HH:MM (empty = always).",
        "source:path":"Send full path to notifier JSON."
    }
    await kv_set("pending", {"user": c.from_user.id, "key": c.data})
    await c.message.edit_text(hints[c.data], reply_markup=back_home()); await c.answer()

@router.message(F.text)
async def on_text(m: Message):
    pend = await kv_get("pending")
    if not pend or pend.get("user") != m.from_user.id: return
    key = pend["key"]; t = (m.text or "").strip(); st = await get_state()
    try:
        if key=="set:cycles": st["cycles"]=max(0,int(t)) if t else 0; await m.reply(f"Cycles -> {st['cycles'] or '∞'}")
        elif key=="set:min": st["min_price"]=max(0,int(t)) if t else 0; await m.reply(f"Lower -> {st['min_price']}⭐")
        elif key=="set:max": st["max_price"]=max(0,int(t)) if t else 0; await m.reply(f"Upper -> {st['max_price'] or '∞'}⭐")
        elif key=="set:overall": st["overall_limit"]=max(0,int(t)) if t else 0; await m.reply(f"Overall -> {st['overall_limit'] or '∞'}⭐")
        elif key=="set:supply": st["supply_limit"]=max(0,int(t)) if t else 0; await m.reply(f"Supply -> {st['supply_limit'] or '∞'}")
        elif key=="lim:set:ids": st["allow_ids"]=[x.strip() for x in t.split(",") if x.strip()]; await m.reply("IDs updated")
        elif key=="lim:set:keys": st["allow_keys"]=[x.strip() for x in t.split(",") if x.strip()]; await m.reply("Keywords updated")
        elif key=="lim:set:window": st["window"]=t; await m.reply(f"Window -> {st['window'] or '(always)'}")
        elif key=="source:path": st["notifier_json"]=t; await m.reply(f"Notifier path set")
        await save_state(st)
    except Exception as e:
        await m.reply(f"Input error: {e}")
    await kv_set("pending", None)

# catalogue (clean layout: two buttons per row, no markdown **)
@router.callback_query(F.data.startswith("cata:"))
async def cb_cata(c: CallbackQuery):
    p = c.data.split(":")
    if p[1] in ("buy","allow"): return
    page = int(p[1])
    st = await get_state()
    source, gifts = await get_gifts(st)
    per_page = 6
    total = len(gifts)
    start = page*per_page; end = min(total, start+per_page)
    view = gifts[start:end]
    lines = [f"Gift Catalog ({source}) page {page+1}/{(total+per_page-1)//per_page or 1}"]
    kb_rows = []
    for g in view:
        gid, title, price = g["gift_id"], g["title"], int(g["star_count"])
        lines.append(f"• {title} — {price}⭐")
        kb_rows.append([
            InlineKeyboardButton(text=f"Buy {price}⭐", callback_data=f"cata:buy:{gid}:{price}:{title[:40]}"),
            InlineKeyboardButton(text=f"+Allow", callback_data=f"cata:allow:{gid}")
        ])
    nav=[]
    if start>0: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"cata:{page-1}"))
    if end<total: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"cata:{page+1}"))
    if nav: kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="Back", callback_data="home")])
    await c.message.edit_text("\n".join(lines) if view else "No gifts right now.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()

@router.callback_query(F.data.startswith("cata:allow:"))
async def cb_cata_allow(c: CallbackQuery):
    gid = c.data.split(":")[2]; st = await get_state()
    s=set(st.get("allow_ids") or []); s.add(gid); st["allow_ids"]=list(s); await save_state(st)
    await c.answer("Added")

@router.callback_query(F.data.startswith("cata:buy:"))
async def cb_cata_buy(c: CallbackQuery, bot: Bot):
    _,_,gid,price,title = c.data.split(":",3); st = await get_state()
    rid = (st.get("recips") or [c.from_user.id])[0]
    try:
        await TGAPI.send_gift(rid, gid, text="Manual", is_private=True)
        ts = int(datetime.now(tz=timezone.utc).timestamp())
        async with open_db() as db:
            await db.execute("INSERT INTO purchases(gift_id,title,stars,recipient_id,status,ts) VALUES (?,?,?,?,?,?)",
                             (gid, title, int(price), rid, "manual", ts)); await db.commit()
        await ledger_add(rid, -int(price), f"Manual buy: {title}")
        await c.answer("Sent!")
        await bot.send_message(rid, f"Sent {title} for {price}⭐")
    except Exception as e:
        await c.answer(f"Send failed: {e}", show_alert=True)

# refunds/credits (admin)
@router.callback_query(F.data == "refund:menu")
async def cb_refund_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Refund last purchase (credit)", callback_data="refund:last")],
        [InlineKeyboardButton(text="Add manual credit", callback_data="refund:add")],
        [InlineKeyboardButton(text="Back", callback_data="profile:open")]
    ])
    await c.message.edit_text("Refunds / Credits\n— credit adds to internal balance; future sends will still cost Stars on Telegram.", reply_markup=kb); await c.answer()

@router.callback_query(F.data == "refund:last")
async def cb_refund_last(c: CallbackQuery):
    owner = await get_owner_id()
    if c.from_user.id != (owner or c.from_user.id): 
        await c.answer("Only owner", show_alert=True); return
    async with open_db() as db:
        async with db.execute("SELECT id,gift_id,title,stars FROM purchases ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    if not row: await c.answer("Nothing to refund", show_alert=True); return
    _,_,title,stars = row
    await ledger_add(owner, int(stars), f"Refund credit: {title}")
    bal = await ledger_balance(owner)
    await c.answer("Credited")
    await c.message.edit_text(f"Refunded (credit) {stars}⭐ for {title}\nInternal credit now: {bal}⭐", reply_markup=back_home())

@router.callback_query(F.data == "refund:add")
async def cb_refund_add(c: CallbackQuery):
    await kv_set("pending", {"user": c.from_user.id, "key": "refund:add"})
    await c.message.edit_text("Send a positive or negative stars amount to credit/debit internally.\nExample: 50", reply_markup=back_home()); await c.answer()

# top up (XTR)
@router.callback_query(F.data == "topup:menu")
async def cb_topup_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Add 50⭐", callback_data="top:go:50"),
         InlineKeyboardButton(text="Add 1000⭐", callback_data="top:go:1000"),
         InlineKeyboardButton(text="Add 3000⭐", callback_data="top:go:3000")],
        [InlineKeyboardButton(text="Back", callback_data="home")]
    ])
    await c.message.edit_text("Top up Stars", reply_markup=kb); await c.answer()

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
    owner = await get_owner_id() or m.from_user.id
    await ledger_add(owner, int(amt), "Top up (Stars)")
    credit = await ledger_balance(owner)
    await m.reply(f"Received {amt}⭐. Internal credit: {credit}⭐.")

# health/logs (trimmed)
@router.callback_query(F.data == "health:run")
async def cb_health(c: CallbackQuery, bot: Bot):
    try:
        stars = await TGAPI.get_my_star_balance()
    except Exception:
        stars = 0
    st = await get_state()
    source, gifts = await get_gifts(st)
    await c.message.edit_text(f"Health\nWallet: {stars}⭐\nSource: {source}\nGifts available: {len(gifts)}", reply_markup=back_home())
    await c.answer()

@router.callback_query(F.data == "logs:open")
async def cb_logs(c: CallbackQuery):
    async with open_db() as db:
        async with db.execute("SELECT ts,level,msg FROM events ORDER BY id DESC LIMIT 20") as cur:
            rows = await cur.fetchall()
    lines = [(datetime.fromtimestamp(ts).strftime('%H:%M:%S')+f" [{lvl}] {msg}") for ts,lvl,msg in rows]
    await c.message.edit_text("Recent logs\n" + ("\n".join(lines) if lines else "Empty"), reply_markup=back_home()); await c.answer()

# extra text hook for manual credit
@router.message(F.text)
async def on_text_credit(m: Message):
    pend = await kv_get("pending")
    if not pend or pend.get("user") != m.from_user.id or pend.get("key")!="refund:add": return
    try:
        amt = int((m.text or "0").strip())
        owner = await get_owner_id() or m.from_user.id
        await ledger_add(owner, amt, "Manual credit")
        credit = await ledger_balance(owner)
        await m.reply(f"Credit updated. Internal credit: {credit}⭐.")
    except Exception as e:
        await m.reply(f"Input error: {e}")
    await kv_set("pending", None)

# bootstrap
async def main():
    if not BOT_TOKEN: raise SystemExit("BOT_TOKEN missing")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(); dp.include_router(router)
    st = await get_state(); await kv_set("state", st)
    await dp.start_polling(bot, allowed_updates=["message","callback_query","pre_checkout_query"])

if __name__ == "__main__":
    asyncio.run(main())
