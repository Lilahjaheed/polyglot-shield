BOT_VERSION = "3.0"

import os
import json
import random
import logging
import asyncio
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.async_api import async_playwright
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

BOT_TOKEN = config.get("bot_token", os.environ.get("BOT_TOKEN", ""))
GEMINI_API_KEY = config.get("gemini_api_key", os.environ.get("GEMINI_API_KEY", ""))
OWNER_ID = config.get("owner_id", 6416848944)
WEBSITE_URL = config.get("website_url", "")
REFERRAL_CODE = config.get("referral_code", "0fc8fbfb")
NAME_POOL = config.get("name_pool", ["John Doe"])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

def load_users():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_users(users):
    with open(DATA_FILE, "w") as f:
        json.dump(users, f, indent=2)

users = load_users()

def ensure_chromium():
    """Make sure Chromium is installed."""
    try:
        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium"],
            capture_output=True,
            timeout=120
        )
        logger.info("Chromium ensured")
    except Exception as e:
        logger.error(f"Chromium install error: {e}")

def generate_full_name():
    return random.choice(NAME_POOL)

def generate_phone():
    prefixes = ["0803", "0806", "0813", "0816", "0903", "0906", "0703", "0706"]
    prefix = random.choice(prefixes)
    number = random.randint(10000000, 99999999)
    return f"{prefix} {number}"

def generate_email():
    first_names = [
        "muhammad", "john", "david", "sarah", "ahmed", "mary",
        "ali", "grace", "daniel", "esther", "michael", "rachel",
        "ibrahim", "fatima", "chinedu", "amara", "yusuf", "zainab",
        "emeka", "ngozi", "tunde", "kemi", "musa", "ada",
        "sadiq", "benita", "glory", "blessing", "victor", "hauwa"
    ]
    last_names = [
        "muhammad", "smith", "johnson", "williams", "brown", "jones",
        "garcia", "miller", "davis", "wilson", "anderson", "taylor",
        "thomas", "moore", "martin", "jackson", "white", "harris",
        "clark", "lewis", "okonkwo", "adeyemi", "obinna", "nwosu",
        "ibrahim", "ali", "hassan", "osei", "mensah", "kamara"
    ]
    domains = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]
    first = random.choice(first_names)
    last = random.choice(last_names)
    domain = random.choice(domains)
    separator = random.choice(["", ".", "_", str(random.randint(1, 99))])
    return f"{first}{separator}{last}@{domain}"

def generate_password():
    chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%"
    length = random.randint(10, 14)
    return "".join(random.choice(chars) for _ in range(length))

async def signup_on_website(full_name, phone, email, password):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            page = await browser.new_page()
            await page.goto(WEBSITE_URL, timeout=30000)
            await page.wait_for_timeout(2000)
            
            await page.fill(f"input[placeholder*='Full name'], input[name*='name'], input#name", full_name)
            await page.fill(f"input[placeholder*='Phone'], input[name*='phone'], input[type='tel']", phone)
            await page.fill(f"input[placeholder*='Email'], input[name*='email'], input[type='email']", email)
            await page.fill(f"input[placeholder*='Password'], input[name*='password'], input[type='password']", password)
            
            if REFERRAL_CODE:
                try:
                    await page.fill(f"input[placeholder*='Referral'], input[name*='referral']", REFERRAL_CODE)
                except Exception:
                    pass
            
            await page.click(f"button:has-text('Create account'), button[type='submit']")
            await page.wait_for_timeout(5000)
            await browser.close()
            return True, "Account created!"
            
    except Exception as e:
        return False, f"Error: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Access denied.")
        return
    
    await update.message.reply_text(
        f"🤖 *Signup Bot v{BOT_VERSION}*\n\n"
        "Create accounts automatically on your website.\n\n"
        "Commands:\n"
        "/newuser - Auto create new account\n"
        "/list - View all created accounts\n"
        "/test - Test connection to website\n"
        "/seturl <url> - Change website URL\n"
        "/config - View current settings",
        parse_mode="Markdown"
    )

async def new_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    await update.message.reply_text(f"🔄 Creating account... (v{BOT_VERSION})")
    
    full_name = generate_full_name()
    phone = generate_phone()
    email = generate_email()
    password = generate_password()
    
    success, message = await signup_on_website(full_name, phone, email, password)
    
    if success:
        users[email] = {
            "full_name": full_name,
            "phone": phone,
            "password": password,
            "status": "active"
        }
        save_users(users)
        await update.message.reply_text(
            f"✅ *Account Created! (v{BOT_VERSION})*\n\n"
            f"Name: {full_name}\n"
            f"Phone: {phone}\n"
            f"Email: `{email}`\n"
            f"Password: `{password}`\n"
            f"Referral: `{REFERRAL_CODE}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ {message}")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    if not users:
        await update.message.reply_text("No accounts yet.")
        return
    
    msg = f"📋 *Accounts (v{BOT_VERSION})*\n\n"
    for email, data in users.items():
        msg += f"• {data.get('full_name', 'Unknown')}\n"
        msg += f"  📧 {email}\n"
        msg += f"  📱 {data.get('phone', 'N/A')}\n\n"
    
    msg += f"Total: {len(users)} accounts"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def test_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    await update.message.reply_text(f"🔄 Testing... (v{BOT_VERSION})")
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            page = await browser.new_page()
            await page.goto(WEBSITE_URL, timeout=15000)
            title = await page.title()
            await browser.close()
            await update.message.reply_text(
                f"✅ Connected! (v{BOT_VERSION})\n\nWebsite: {WEBSITE_URL}\nTitle: {title}"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")

async def set_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /seturl <url>")
        return
    
    global WEBSITE_URL
    WEBSITE_URL = context.args[0]
    config["website_url"] = WEBSITE_URL
    
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    
    await update.message.reply_text(f"✅ URL updated: {WEBSITE_URL}")

async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    await update.message.reply_text(
        f"⚙️ *Settings (v{BOT_VERSION})*\n\n"
        f"Website: {WEBSITE_URL}\n"
        f"Referral: {REFERRAL_CODE}\n"
        f"Name pool: {len(NAME_POOL)} names\n"
        f"Accounts created: {len(users)}",
        parse_mode="Markdown"
    )

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

async def post_init(app):
    """Set bot commands menu."""
    await app.bot.set_my_commands([
        BotCommand("newuser", "Auto create new account"),
        BotCommand("list", "View all created accounts"),
        BotCommand("test", "Test connection to website"),
        BotCommand("seturl", "Change website URL"),
        BotCommand("config", "View current settings"),
    ])

def main():
    # Ensure Chromium is installed before starting
    ensure_chromium()
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newuser", new_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("test", test_connection))
    app.add_handler(CommandHandler("seturl", set_url))
    app.add_handler(CommandHandler("config", show_config))

    threading.Thread(target=run_health_server, daemon=True).start()

    print(f"🤖 Signup Bot v{BOT_VERSION} is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
