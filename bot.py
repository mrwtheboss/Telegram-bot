import os
import json
import hmac
import hashlib
import logging
import aiosqlite
import requests
import asyncio
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
PORT = int(os.getenv("PORT", 8080))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = "shop.db"
AMOUNT, COIN = range(2)

COINS = {
    "usdttrc20": "USDT TRC20 (Tron) – Recommended",
    "usdterc20": "USDT ERC20 (Ethereum)",
    "usdtbsc": "USDT BEP20 (BNB Chain)",
    "usdtsol": "USDT Solana",
    "btc": "Bitcoin (BTC)",
    "eth": "Ethereum (ETH)",
    "ltc": "Litecoin (LTC)",
}

# ==================== DATABASE ====================
async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")


def get_estimate(amount_usd: float, pay_currency: str) -> float | None:
    """Get estimated crypto amount"""
    url = "https://api.nowpayments.io/v1/estimate"
    headers = {"x-api-key": NOWPAYMENTS_API_KEY}
    params = {
        "amount": amount_usd,
        "currency_from": "usd",
        "currency_to": pay_currency
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("estimated_amount", 0))
    except Exception as e:
        logger.error(f"Estimate error: {e}")
        return None

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER,
                amount REAL,
                status TEXT DEFAULT 'waiting'
            )
        """)
        await db.commit()

async def get_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            await db.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
            await db.commit()
            return 0.0

async def add_balance(user_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
            (user_id, amount, amount)
        )
        await db.commit()

# ==================== KEYBOARDS ====================
def main_keyboard():
    keyboard = [
        [KeyboardButton("🛒 Proxies"), KeyboardButton("🎮 Game Keys")],
        [KeyboardButton("💰 My Balance"), KeyboardButton("➕ Top-up")],
        [KeyboardButton("📦 My Orders"), KeyboardButton("🆘 Support")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def coin_keyboard(last_coin: str = None):
    buttons = []
    for code, name in COINS.items():
        prefix = "✅ " if code == last_coin else ""
        buttons.append([InlineKeyboardButton(f"{prefix}{name}", callback_data=f"coin_{code}")])
    
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_topup")])
    return InlineKeyboardMarkup(buttons)

# ==================== NOWPAYMENTS ====================
def create_payment(amount_usd: float, pay_currency: str, order_id: str, ipn_url: str):
    url = "https://api.nowpayments.io/v1/payment"
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "price_amount": amount_usd,
        "price_currency": "usd",
        "pay_currency": pay_currency,
        "order_id": order_id,
        "order_description": f"Balance top-up ${amount_usd}",
        "ipn_callback_url": ipn_url,
        "is_fixed_rate": True
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

def generate_qr_url(address: str) -> str:
    return f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={address}"

def verify_ipn_signature(payload: dict, signature: str) -> bool:
    if not signature or not NOWPAYMENTS_IPN_SECRET:
        return False
    sorted_msg = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    digest = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode(),
        sorted_msg.encode(),
        hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(digest, signature)

##  minimum fixed

def get_min_amount(pay_currency: str, is_fixed_rate: bool = True) -> float:
    """Returns the minimum amount in USD for the selected currency"""
    url = "https://api.nowpayments.io/v1/min-amount"
    headers = {"x-api-key": NOWPAYMENTS_API_KEY}
    params = {
        "currency_from": "usd",
        "currency_to": pay_currency,
        "fiat_equivalent": "usd",
        "is_fixed_rate": str(is_fixed_rate).lower()
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("fiat_equivalent", 5))
    except Exception as e:
        logger.error(f"Min amount check failed: {e}")
        return 20.0  # safe fallback

# ==================== TELEGRAM HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_balance(update.effective_user.id)
    await update.message.reply_text(
        "Welcome to Legal Proxies & Game Keys shop!\n\nChoose an option:",
        reply_markup=main_keyboard()
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = await get_balance(update.effective_user.id)
    await update.message.reply_text(f"💰 Your current balance: **${bal:.2f}**", parse_mode="Markdown")

async def topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Enter the amount you want to deposit (minimum **$5**).\nExample: `12.5` or `25`",
        parse_mode="Markdown"
    )
    return AMOUNT

async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount < 5:
            await update.message.reply_text("Minimum deposit is **$5**. Please try again.")
            return AMOUNT
                context.user_data["topup_amount"] = amount
        
        # Get last used coin
        last_coin = context.user_data.get("last_coin")
        
        await update.message.reply_text(
            f"Amount: **${amount:.2f}**\n\nSelect the cryptocurrency:",
            reply_markup=coin_keyboard(last_coin),
            parse_mode="Markdown"
        )
        return COIN
    except ValueError:
        await update.message.reply_text("Please enter a valid number (e.g. 10 or 25.5)")
        return AMOUNT

async def receive_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_topup":
        await query.message.reply_text("Top-up cancelled.", reply_markup=main_keyboard())
        return ConversationHandler.END

    coin_code = query.data.replace("coin_", "")
    amount = context.user_data.get("topup_amount")
    user_id = query.from_user.id

    # Remember this coin
    context.user_data["last_coin"] = coin_code

    # Check minimum
    min_amount = get_min_amount(coin_code, is_fixed_rate=True)
    if amount < min_amount:
        await query.message.reply_text(
            f"❌ Minimum for **{COINS.get(coin_code)}** is **${min_amount:.2f}**\n"
            f"You entered: **${amount:.2f}**\n\nPlease start again.",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END

    # Get estimate
    estimated = get_estimate(amount, coin_code)
    estimate_text = f"≈ **{round(estimated, 6)} {coin_code.upper()}**" if estimated else "Calculating..."

    # Ask for confirmation
    confirm_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm & Get Address", callback_data=f"confirm_{coin_code}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_topup")
        ]
    ])

    await query.message.edit_text(
        f"Confirm top-up:\n\n"
        f"Amount: **${amount:.2f}**\n"
        f"Coin: **{COINS.get(coin_code)}**\n"
        f"You will pay: {estimate_text}\n\n"
        f"Do you want to continue?",
        parse_mode="Markdown",
        reply_markup=confirm_keyboard
    )
    return COIN   # stay in the same state
    
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=main_keyboard())
    return ConversationHandler.END

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "💰 My Balance":
        await balance(update, context)
    elif text == "🛒 Proxies":
        await update.message.reply_text("🛒 Proxy catalog coming soon…")
    elif text == "🎮 Game Keys":
        await update.message.reply_text("🎮 Game Keys catalog coming soon…")
    elif text == "🆘 Support":
        await update.message.reply_text("Contact support: @YourSupportUsername")
    else:
        await update.message.reply_text("Please use the menu buttons.")
        
async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_topup":
        await query.message.reply_text("Top-up cancelled.", reply_markup=main_keyboard())
        return ConversationHandler.END

    coin_code = query.data.replace("confirm_", "")
    amount = context.user_data.get("topup_amount")
    user_id = query.from_user.id

    order_id = f"topup_{user_id}_{int(query.message.date.timestamp())}"
    ipn_url = os.getenv("IPN_URL", "https://your-service.up.railway.app/ipn")

    try:
        payment = create_payment(amount, coin_code, order_id, ipn_url)
        pay_address = payment["pay_address"]
        pay_amount = payment["pay_amount"]
        payment_id = str(payment["payment_id"])
        currency = payment["pay_currency"].upper()

        display_amount = round(float(pay_amount), 2)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO pending_payments (payment_id, user_id, amount) VALUES (?, ?, ?)",
                (payment_id, user_id, amount)
            )
            await db.commit()

        qr_url = generate_qr_url(pay_address)

        text = (
            f"✅ Payment created\n\n"
            f"Amount to send: **{display_amount} {currency}**\n"
            f"Network: {COINS.get(coin_code)}\n\n"
            f"**Address (tap to copy):**\n`{pay_address}`\n\n"
            f"Send exactly the amount above.\n"
            f"Balance will be credited automatically after confirmation."
        )

        await query.message.reply_photo(photo=qr_url, caption=text, parse_mode="Markdown")
        await query.message.reply_text("You can return to the main menu.", reply_markup=main_keyboard())

    except Exception as e:
        logger.error(f"Payment creation error: {e}")
        await query.message.reply_text("❌ Failed to create payment. Please try again.")

    return ConversationHandler.END

# ==================== IPN WEBHOOK ====================
async def ipn_handler(request: web.Request):
    try:
        payload = await request.json()
        signature = request.headers.get("x-nowpayments-sig", "")

        if not verify_ipn_signature(payload, signature):
            logger.warning("Invalid IPN signature")
            return web.Response(status=400, text="Invalid signature")

        payment_id = str(payload.get("payment_id"))
        status = payload.get("payment_status")
        order_id = payload.get("order_id", "")

        logger.info(f"IPN received: {payment_id} → {status}")

        if status in ["finished", "confirmed"]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, amount, status FROM pending_payments WHERE payment_id = ?",
            (payment_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row and row[2] != "finished":
            user_id, amount, _ = row
            await add_balance(user_id, amount)
            await db.execute(
                "UPDATE pending_payments SET status = 'finished' WHERE payment_id = ?",
                (payment_id,)
            )
            await db.commit()
            logger.info(f"Credited ${amount} to user {user_id}")

            # === ADMIN NOTIFICATION ===
            try:
                from telegram import Bot
                bot = Bot(token=BOT_TOKEN)
                for admin_id in ADMIN_IDS:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"💰 *New Top-up Received!*\n\n"
                            f"User ID: `{user_id}`\n"
                            f"Amount: *${amount:.2f}*\n"
                            f"Payment ID: `{payment_id}`"
                        ),
                        parse_mode="Markdown"
                    )
            except Exception as e:
             logger.error(f"Failed to send admin notification: {e}")
                
# ==================== MAIN ====================
async def main():
    await init_db()

    # Telegram bot
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Top-up$"), topup_start)],
        states={
    AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)],
    COIN: [
        CallbackQueryHandler(receive_coin, pattern="^coin_"),
        CallbackQueryHandler(confirm_payment, pattern="^confirm_"),
        CallbackQueryHandler(receive_coin, pattern="^cancel_topup$"),
    ],
},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Web server for IPN
    web_app = web.Application()
    web_app.router.add_post("/ipn", ipn_handler)
    web_app.router.add_get("/", lambda r: web.Response(text="Bot is running"))

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"IPN webhook listening on port {PORT}")

    # Start polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Telegram bot started (polling)")

    # Keep running
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
