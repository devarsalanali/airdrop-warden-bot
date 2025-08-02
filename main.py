import os
import logging
import sqlite3
import requests
import base58
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ContextTypes
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

# --- Configuration --- #
load_dotenv()

# Initialize database
conn = sqlite3.connect('airdrop_users.db')
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        subscribed BOOLEAN,
        sub_end_date DATE
    )
''')
conn.commit()

# --- TRON API Integration --- #
def verify_usdt_payment(tx_hash: str) -> bool:
    """Verify USDT (TRC20) payment using TRON API"""
    YOUR_WALLET = os.getenv("USDT_WALLET")
    API_KEY = os.getenv("TRON_API_KEY")
    
    try:
        # Fetch transaction details
        tx_url = f"https://api.trongrid.io/v1/transactions/{tx_hash}"
        headers = {"TRON-PRO-API-KEY": API_KEY}
        response = requests.get(tx_url, headers=headers).json()
        
        # Check if transaction succeeded
        if not response.get("ret", [{}])[0].get("contractRet") == "SUCCESS":
            return False
        
        # Verify USDT transfer
        contract = response.get("raw_data", {}).get("contract", [{}])[0]
        if contract.get("type") != "TriggerSmartContract":
            return False
            
        # Decode USDT transfer parameters
        parameter = contract.get("parameter", {}).get("value", {})
        if parameter.get("data", "")[:8] != "a9059cbb":  # USDT transfer method
            return False
            
        # Extract recipient and amount
        recipient_hex = parameter.get("to_address", "")
        amount_hex = parameter.get("data", "")[8:72]
        
        # Convert to readable format
        recipient = base58.b58encode_check(bytes.fromhex(recipient_hex)).decode()
        amount = int(amount_hex, 16) / 1_000_000  # USDT has 6 decimals
        
        return (
            recipient == YOUR_WALLET
            and amount >= 0.99  # Minimum payment
        )
        
    except Exception as e:
        logging.error(f"TRON API Error: {e}")
        return False

# --- Airdrop Scraping --- #
def get_airdrops():
    """Fetch airdrops from multiple sources"""
    sources = [
        ("CoinMarketCap", "https://coinmarketcap.com/airdrop/", ".cmc-link"),
        ("Airdrops.io", "https://airdrops.io", ".airdrop-item h3"),
        ("CoinGecko", "https://www.coingecko.com/en/airdrops", ".tw-font-medium")
    ]
    
    airdrops = []
    for name, url, selector in sources:
        try:
            soup = BeautifulSoup(requests.get(url).text, 'html.parser')
            items = [f"• {name}: {x.text.strip()}" for x in soup.select(selector)[:3]]
            airdrops.extend(items)
        except Exception as e:
            logging.error(f"Failed to scrape {name}: {e}")
            airdrops.append(f"• {name}: Update pending")
    
    return airdrops[:10]  # Return top 10 results

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "copy_address":
        await query.edit_message_text(
            text=f"`{os.getenv('USDT_WALLET')}`\n\n"
                 "✅ Address copied to clipboard!\n"
                 "Paste it in your wallet to pay.",
            parse_mode="Markdown"
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "📋 Copy USDT Address", 
                callback_data="copy_address"
            ),
            InlineKeyboardButton(
                "💳 Pay Now", 
                url=f"https://tronscan.org/#/send?to={os.getenv('USDT_WALLET')}&amount=0.99"
            )
        ]
    ]
    await update.message.reply_text(
        "🔐 *Subscription Payment*\n\n"
        "1. Send *0.99 USDT* (TRC20) to:\n"
        f"`{os.getenv('USDT_WALLET')}`\n\n"
        "2. Reply with your TX hash\n\n"
        "👉 Use buttons below for quick payment:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )        

async def airdrops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /airdrops command"""
    user_id = update.effective_user.id
    cursor.execute("SELECT sub_end_date FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    
    all_drops = get_airdrops()
    if result and datetime.strptime(result[0], '%Y-%m-%d') > datetime.now():
        await update.message.reply_text(
            "🔥 *All Airdrops*\n\n" + "\n".join(all_drops),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "🔒 *Free Preview*\n\n" + "\n".join(all_drops[:5]) + 
            "\n\nSubscribe for full access: /start",
            parse_mode="Markdown"
        )

async def process_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify payment transaction"""
    tx_hash = update.message.text.strip()
    if len(tx_hash) != 64:
        await update.message.reply_text("❌ Invalid TX hash format. Must be 64 characters.")
        return
    
    await update.message.reply_text("⏳ Verifying payment... (may take 30 seconds)")
    
    if verify_usdt_payment(tx_hash):
        expiry_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        cursor.execute(
            "INSERT OR REPLACE INTO users VALUES (?, ?, ?)",
            (update.effective_user.id, True, expiry_date)
        )
        conn.commit()
        await update.message.reply_text(
            f"✅ Payment confirmed! Access until: {expiry_date}\n\n"
            "Use /airdrops to see all drops."
        )
    else:
        await update.message.reply_text(
            "❌ Payment verification failed. Check:\n"
            "1. Sent exactly 0.99 USDT (TRC20)\n"
            "2. Transaction is confirmed\n"
            "3. Correct recipient address\n\n"
            "Try again or contact support."
        )

# --- Subscription Reminders --- #
async def check_expiring_subs(context: ContextTypes.DEFAULT_TYPE):
    """Notify users with expiring subscriptions"""
    soon = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
    cursor.execute(
        "SELECT user_id, sub_end_date FROM users "
        "WHERE subscribed = 1 AND sub_end_date <= ?",
        (soon,)
    )
    for user_id, end_date in cursor.fetchall():
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Subscription expires on {end_date}\nRenew now: /start"
            )
        except Exception as e:
            logging.error(f"Failed to notify user {user_id}: {e}")

# --- Main Application --- #
def main():
    """Start the bot"""
    application = ApplicationBuilder() \
        .token(os.getenv("BOT_TOKEN")) \
        .build()
    
    # Command handlers
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("airdrops", airdrops))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_payment))
    
    # Schedule daily reminders
    job_queue = application.job_queue
    job_queue.run_repeating(check_expiring_subs, interval=86400, first=10)  # 24 hours
    
    logging.info("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )
    main()