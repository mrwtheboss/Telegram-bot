import os
import logging
import aiosqlite
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    ContextTypes, filters
)
import requests

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
# Optional: IPN secret later for automatic confirmation
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = "shop.db"

# ---------- Database helpers ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                language TEXT DEFAULT 'en'
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

# ---------- Keyboards ----------
def main_keyboard():
    keyboard = [
        [KeyboardButton("🛒 Proxies"), KeyboardButton("🎮 Game Keys")],
        [KeyboardButton("💰 My Balance"), KeyboardButton("➕ Top-up")],
        [KeyboardButton("📦 My Orders"), KeyboardButton("🆘 Support")],
        [KeyboardButton("中文 / English")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------- NowPayments ----------
def create_nowpayments_invoice(amount_usd: float, order_id: str, description: str):
    url = "https://api.nowpayments.io/v1/invoice"
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "price_amount": amount_usd,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": description,
        # "ipn_callback_url": "https://your-public-url/ipn",  # add later for auto-confirm
        "success_url": "https://t.me/your_bot_username",
        "cancel_url": "https://t.me/your_bot_username"
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await get_balance(user.id)  # ensure user exists
    await update.message.reply_text(
        f"Welcome {user.first_name}!\n\n"
        "Legal Proxies & Game Keys shop – 24/7 automated.\n"
        "Choose an option below:",
        reply_markup=main_keyboard()
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = await get_balance(update.effective_user.id)
    await update.message.reply_text(f"💰 Your balance: **${bal:.2f}**", parse_mode="Markdown")

async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simple fixed amounts for Phase 1. Later make it custom.
    text = (
        "Choose top-up amount:\n\n"
        "Send one of these numbers:\n"
        "`10`  → $10\n"
        "`25`  → $25\n"
        "`50`  → $50\n"
        "`100` → $100"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data["awaiting_topup"] = True

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if context.user_data.get("awaiting_topup"):
        if text in ["10", "25", "50", "100"]:
            amount = float(text)
            order_id = f"topup_{user_id}_{int(update.message.date.timestamp())}"
            try:
                invoice = create_nowpayments_invoice(
                    amount_usd=amount,
                    order_id=order_id,
                    description=f"Balance top-up ${amount}"
                )
                pay_url = invoice.get("invoice_url")
                await update.message.reply_text(
                    f"✅ Invoice created for **${amount}**\n\n"
                    f"Pay here:\n{pay_url}\n\n"
                    "After payment is confirmed, balance will be added automatically "
                    "(or contact support if IPN is not set yet).",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(e)
                await update.message.reply_text("❌ Failed to create payment. Try again later.")
            context.user_data["awaiting_topup"] = False
            return

    # Placeholder buttons
    if text == "🛒 Proxies":
        await update.message.reply_text("Proxy catalog coming soon…")
    elif text == "🎮 Game Keys":
        await update.message.reply_text("Game Keys catalog coming soon…")
    elif text == "💰 My Balance":
        await balance(update, context)
    elif text == "➕ Top-up":
        await topup(update, context)
    elif text == "🆘 Support":
        await update.message.reply_text("Contact @YourSupportUsername")
    else:
        await update.message.reply_text("Please use the menu buttons.")

async def post_init(app: Application):
    await init_db()

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
