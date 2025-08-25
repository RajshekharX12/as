import asyncio, os, time
from dataclasses import dataclass
from typing import Optional, List, Set, Dict

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.types import LabeledPrice, ForceReply, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
RECIPIENT_DEFAULT = os.getenv("RECIPIENT", "")
PRICE_MIN_DEFAULT = int(os.getenv("PRICE_MIN", "100"))
PRICE_MAX_DEFAULT = int(os.getenv("PRICE_MAX", "1000"))
SCAN_INTERVAL_MS = int(os.getenv("SCAN_INTERVAL_MS", "300"))
DB_PATH = "giftbot.sqlite3"

if not BOT_TOKEN or not ADMIN_IDS or not RECIPIENT_DEFAULT:
    raise SystemExit("Configure BOT_TOKEN, ADMIN_IDS, RECIPIENT in .env")

# ---------- state ----------
@dataclass
class GiftInfo:
    id: str
    star_count: int
    total_count: Optional[int]
    remaining_count: Optional[int]
    upgrade_star_count: Optional[int]

class Store:
    def __init__(self):
        self.last_seen_ids: Set[str] = set()
        self.recipient: str = RECIPIENT_DEFAULT
        self.min_price: int = PRICE_MIN_DEFAULT
        self.max_price: int = PRICE_MAX_DEFAULT
        self.autobuy: bool = True
        self.multi_buy: bool = True      # buy multiple gifts per tick
        self.blocked_ids: Set[str] = set()
        self.admins: Set[int] = ADMIN_IDS
        self.panel_msg_id: Optional[int] = None
        self.panel_chat_id: Optional[int] = None
        self.waiting: Dict[int, str] = {}  # user_id -> "set_recipient" | "refund"

store = Store()
router = Router()

# ---------- DB ----------
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER, charge_id TEXT, amount INTEGER, ts INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS purchases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          gift_id TEXT, star_count INTEGER, recipient TEXT, result_ok INTEGER, ts INTEGER
        )""")
        await db.commit()

async def db_add_payment(user_id: int, charge_id: str, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO payments(user_id, charge_id, amount, ts) VALUES(?,?,?,?)",
                         (user_id, charge_id, amount, int(time.time())))
        await db.commit()

async def db_add_purchase(gift_id: str, star: int, recipient: str, ok: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO purchases(gift_id, star_count, recipient, result_ok, ts) VALUES(?,?,?,?,?)",
                         (gift_id, star, recipient, 1 if ok else 0, int(time.time())))
        await db.commit()

# ---------- helpers ----------
def is_admin(uid: int) -> bool: return uid in store.admins
def is_limited(g: GiftInfo) -> bool:
    return g.total_count is not None and (g.remaining_count or 0) > 0
def in_band(g: GiftInfo) -> bool:
    return store.min_price <= g.star_count <= store.max_price

def panel_text(balance: int) -> str:
    return (
        "<b>Auto Gift Buyer</b>\n"
        f"Recipient: <code>{store.recipient}</code>\n"
        f"Band: <b>{store.min_price}→{store.max_price}⭐</b>\n"
        f"Auto-buy: {'🟢 ON' if store.autobuy else '🔴 OFF'} | Multi-buy: {'YES' if store.multi_buy else 'NO'}\n"
        f"Scan: {SCAN_INTERVAL_MS} ms\n"
        f"Bot Stars: <b>{balance}⭐</b>"
    )

def panel_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Auto-buy ON/OFF", callback_data="p:toggle")
    kb.button(text="–50 min", callback_data="p:min:-50")
    kb.button(text="+50 min", callback_data="p:min:+50")
    kb.button(text="–50 max", callback_data="p:max:-50")
    kb.button(text="+50 max", callback_data="p:max:+50")
    kb.button(text="Multi-buy ON/OFF", callback_data="p:multi")
    kb.button(text="Set Recipient", callback_data="p:set_recipient")
    kb.button(text="Balance ⟳", callback_data="p:balance")
    kb.button(text="Deposit 100⭐", callback_data="p:dep:100")
    kb.button(text="Deposit 500⭐", callback_data="p:dep:500")
    kb.button(text="Deposit 1000⭐", callback_data="p:dep:1000")
    kb.button(text="Refund…", callback_data="p:refund")
    kb.adjust(3, 4, 2, 3)
    return kb.as_markup()

# ---------- Bot API wrappers ----------
async def fetch_available_gifts(bot: Bot) -> List[GiftInfo]:
    data = await bot.get_available_gifts()
    gifts = []
    for g in getattr(data, "gifts", []):
        gifts.append(GiftInfo(
            id=str(g.id),
            star_count=int(g.star_count),
            total_count=int(g.total_count) if getattr(g, "total_count", None) is not None else None,
            remaining_count=int(g.remaining_count) if getattr(g, "remaining_count", None) is not None else None,
            upgrade_star_count=int(g.upgrade_star_count) if getattr(g, "upgrade_star_count", None) is not None else None,
        ))
    return gifts

async def get_balance(bot: Bot) -> int:
    amt = await bot.get_my_star_balance()
    return int(getattr(amt, "amount", 0))

async def send_gift(bot: Bot, gift: GiftInfo, recipient: str, text: str = "") -> bool:
    try:
        if recipient.startswith("@"):
            await bot.send_gift(chat_id=recipient, gift_id=gift.id, text=text)
        else:
            await bot.send_gift(user_id=int(recipient), gift_id=gift.id, text=text)
        await db_add_purchase(gift.id, gift.star_count, recipient, True)
        return True
    except Exception:
        await db_add_purchase(gift.id, gift.star_count, recipient, False)
        return False

# ---------- notifier + sniper loop (diff + greedy buy) ----------
async def sniper_loop(bot: Bot):
    await db_init()
    while True:
        try:
            gifts = await fetch_available_gifts(bot)
            current_ids = {g.id for g in gifts}
            appeared = [g for g in gifts if g.id not in store.last_seen_ids]
            store.last_seen_ids = current_ids

            # notify on newly appeared limited gifts in-band
            for g in appeared:
                if is_limited(g) and in_band(g):
                    try:
                        await bot.send_message(list(store.admins)[0],
                                               f"🆕 Limited gift: {g.star_count}⭐ (id={g.id})")
                    except Exception:
                        pass

            if not store.autobuy:
                await asyncio.sleep(SCAN_INTERVAL_MS / 1000)
                continue

            # candidates: limited + price band
            candidates = [g for g in gifts if is_limited(g) and in_band(g)]
            # cheaper first, then scarcer first (lower remaining)
            candidates.sort(key=lambda x: (x.star_count, (x.remaining_count or 10**9)))

            bal = await get_balance(bot)
            for g in candidates:
                if g.id in store.blocked_ids:
                    continue
                if g.star_count > bal:
                    continue
                ok = await send_gift(bot, g, store.recipient, text=f"Auto-bought {g.star_count}⭐")
                if ok:
                    bal -= g.star_count
                    if not store.multi_buy:
                        break
            await asyncio.sleep(SCAN_INTERVAL_MS / 1000)
        except Exception:
            # never die on transient errors
            await asyncio.sleep(0.7)

# ---------- Commands ----------
@router.message(Command("start"))
async def on_start(m: types.Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return await m.answer("Access denied.")
    bal = await get_balance(bot)
    await m.answer("Bot online ✅")
    text = panel_text(bal)
    msg = await m.answer(text, reply_markup=panel_kb())
    store.panel_chat_id, store.panel_msg_id = m.chat.id, msg.message_id

@router.message(Command("panel"))
async def on_panel(m: types.Message, bot: Bot):
    if not is_admin(m.from_user.id): return
    bal = await get_balance(bot)
    msg = await m.answer(panel_text(bal), reply_markup=panel_kb())
    store.panel_chat_id, store.panel_msg_id = m.chat.id, msg.message_id

@router.message(Command("band"))
async def on_band(m: types.Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.strip().split()
    if len(parts) == 3:
        store.min_price = max(1, int(parts[1]))
        store.max_price = max(store.min_price, int(parts[2]))
        return await m.answer(f"✅ Band set to {store.min_price}→{store.max_price}⭐")
    await m.answer("Usage: /band <min> <max>")

@router.message(Command("recipient"))
async def on_recipient_cmd(m: types.Message):
    if not is_admin(m.from_user.id): return
    store.waiting[m.from_user.id] = "set_recipient"
    await m.answer("Reply with <user_id> or @channel", reply_markup=ForceReply(selective=True))

@router.message(Command("balance"))
async def on_balance(m: types.Message, bot: Bot):
    if not is_admin(m.from_user.id): return
    bal = await get_balance(bot)
    await m.answer(f"💰 Bot Stars: {bal}⭐")

@router.message(Command("deposit"))
async def on_deposit_cmd(m: types.Message, bot: Bot):
    if not is_admin(m.from_user.id): return
    parts = m.text.strip().split()
    if len(parts) != 2:
        return await m.answer("Usage: /deposit <stars>")
    stars = int(parts[1])
    if stars <= 0:
        return await m.answer("Amount must be > 0.")
    await bot.send_invoice(
        chat_id=m.chat.id,
        title="Deposit",
        description=f"Top-up {stars}⭐",
        payload=f"deposit:{stars}",
        currency="XTR",
        prices=[LabeledPrice(label="Deposit", amount=stars)],
        provider_token=""  # XTR doesn’t need provider token (digital goods)
    )

@router.message(Command("refund"))
async def on_refund_cmd(m: types.Message):
    if not is_admin(m.from_user.id): return
    store.waiting[m.from_user.id] = "refund"
    await m.answer("Reply with <telegram_payment_charge_id>", reply_markup=ForceReply(selective=True))

# ---------- Replies for ForceReply prompts ----------
@router.message(F.reply_to_message, F.from_user)
async def on_force_reply(m: types.Message, bot: Bot):
    uid = m.from_user.id
    if uid not in store.waiting:
        return
    action = store.waiting.pop(uid)

    if action == "set_recipient":
        store.recipient = m.text.strip()
        await m.answer(f"📦 Recipient set to: <code>{store.recipient}</code>", parse_mode="HTML")
    elif action == "refund":
        try:
            ok = await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=m.text.strip())
            await m.answer("♻️ Refund OK." if ok else "❌ Refund failed.")
        except Exception:
            await m.answer("❌ Refund failed.")

# ---------- Payments (Stars) ----------
@router.pre_checkout_query()
async def on_pre_checkout(pcq: types.PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)

@router.message(F.successful_payment)
async def on_successful_payment(m: types.Message):
    sp = m.successful_payment
    stars = int(sp.total_amount)
    await db_add_payment(m.from_user.id, sp.telegram_payment_charge_id, stars)
    await m.answer(f"✅ Deposit confirmed: {stars}⭐\n<code>{sp.telegram_payment_charge_id}</code>", parse_mode="HTML")

# ---------- Inline panel callbacks ----------
@router.callback_query(F.data.startswith("p:"))
async def on_panel_cb(q: CallbackQuery, bot: Bot):
    if not is_admin(q.from_user.id):
        return await q.answer("No access", show_alert=True)

    _, action, *rest = q.data.split(":")

    if action == "toggle":
        store.autobuy = not store.autobuy
        await q.answer(f"Auto-buy {'ON' if store.autobuy else 'OFF'}")
    elif action == "multi":
        store.multi_buy = not store.multi_buy
        await q.answer(f"Multi-buy {'ON' if store.multi_buy else 'OFF'}")
    elif action in ("min", "max"):
        delta = int(rest[0])
        if action == "min":
            store.min_price = max(1, store.min_price + delta)
            if store.min_price > store.max_price:
                store.max_price = store.min_price
        else:
            store.max_price = max(store.min_price, store.max_price + delta)
        await q.answer(f"Band {store.min_price}→{store.max_price}⭐")
    elif action == "set_recipient":
        store.waiting[q.from_user.id] = "set_recipient"
        await q.message.answer("Reply with <user_id> or @channel", reply_markup=ForceReply(selective=True))
        await q.answer()
        return
    elif action == "balance":
        bal = await get_balance(bot)
        try:
            await q.message.edit_text(panel_text(bal), reply_markup=panel_kb(), parse_mode="HTML")
        except Exception:
            pass
        return
    elif action == "dep":
        stars = int(rest[0])
        await bot.send_invoice(
            chat_id=q.message.chat.id,
            title="Deposit",
            description=f"Top-up {stars}⭐",
            payload=f"deposit:{stars}",
            currency="XTR",
            prices=[LabeledPrice(label="Deposit", amount=stars)],
            provider_token=""
        )
        await q.answer("Invoice sent")
        return
    elif action == "refund":
        store.waiting[q.from_user.id] = "refund"
        await q.message.answer("Reply with <telegram_payment_charge_id>", reply_markup=ForceReply(selective=True))
        await q.answer()
        return

    # refresh panel after toggles
    bal = await get_balance(bot)
    try:
        await q.message.edit_text(panel_text(bal), reply_markup=panel_kb(), parse_mode="HTML")
    except Exception:
        pass

async def main():
    bot = Bot(BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(sniper_loop(bot))
    print("Auto Gift Buyer running…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
