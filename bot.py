BOT_VERSION = "4.7"

import os
import re
import json
import random
import logging
import asyncio
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.async_api import async_playwright
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

BOT_TOKEN = config.get("bot_token", os.environ.get("BOT_TOKEN", ""))
GEMINI_API_KEY = config.get("gemini_api_key", os.environ.get("GEMINI_API_KEY", ""))
OWNER_ID = config.get("owner_id", 6416848944)
WEBSITE_URL = config.get("website_url", "https://readersbuck.com/register?invite=0fc8fbfb")
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

# Current batch job state (for parallel creation + stop button)
JOB = {"active": False, "event": None}

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
    """Generate 11-digit Nigerian phone number starting with 09, 07, or 08."""
    prefixes = ["090", "091", "080", "081", "070", "071"]
    prefix = random.choice(prefixes)
    # 11 digits total: 3 prefix + 8 random digits
    number = random.randint(10000000, 99999999)
    return f"{prefix}{number}"

def generate_matching_name():
    """Generate matching username and email (no dots or underscores)."""
    first_names = [
        "muhammad", "john", "david", "sarah", "ahmed", "mary",
        "ali", "grace", "daniel", "esther", "michael", "rachel",
        "ibrahim", "fatima", "chinedu", "amara", "yusuf", "zainab",
        "emeka", "ngozi", "tunde", "kemi", "musa", "ada",
        "sadiq", "benita", "glory", "blessing", "victor", "hauwa",
        "peter", "ruth", "samuel", "hannah", "isaac", "deborah",
        "joseph", "ruth", "david", "grace", "solomon", "esther"
    ]
    last_names = [
        "muhammad", "smith", "johnson", "williams", "brown", "jones",
        "garcia", "miller", "davis", "wilson", "anderson", "taylor",
        "thomas", "moore", "martin", "jackson", "white", "harris",
        "clark", "lewis", "okonkwo", "adeyemi", "obinna", "nwosu",
        "ibrahim", "ali", "hassan", "osei", "mensah", "kamara",
        "obi", "eze", "okafor", "bello", "usman", "yusuf"
    ]
    domains = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]
    
    first = random.choice(first_names)
    last = random.choice(last_names)
    domain = random.choice(domains)
    
    # No dots or underscores - just concatenate
    name = f"{first}{last}"
    email = f"{name}@{domain}"
    
    return name, email

def generate_password():
    chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%"
    length = random.randint(10, 14)
    return "".join(random.choice(chars) for _ in range(length))

def generate_unique_name():
    """Generate email not already used by a created account."""
    for _ in range(50):
        username, email = generate_matching_name()
        if email not in users:
            return username, email
    # Fallback: append random digits
    username, email = generate_matching_name()
    local, domain = email.split("@")
    return username, f"{local}{random.randint(100, 999)}@{domain}"

async def signup_on_website(full_name, phone, email, password, max_attempts=3):
    last_error = "Unknown error"
    for attempt in range(max_attempts):
        # Fresh data each attempt
        if attempt > 0:
            phone = generate_phone()
            _, email = generate_unique_name()
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled"
                    ]
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 768},
                    locale="en-US"
                )
                # Hide automation flags from the site
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = await context.new_page()
                
                # domcontentloaded is reliable; networkidle hangs on analytics beacons
                await page.goto(WEBSITE_URL, timeout=45000, wait_until="domcontentloaded")
                
                try:
                    await page.wait_for_selector("#name", state="attached", timeout=30000)
                except Exception:
                    # Form not there - reload once and wait again
                    try:
                        await page.reload(timeout=45000, wait_until="domcontentloaded")
                        await page.wait_for_selector("#name", state="attached", timeout=30000)
                    except Exception:
                        # Diagnostic: what did the browser actually see?
                        try:
                            title = await page.title()
                            body_text = (await page.inner_text("body"))[:300]
                            last_error = f"Form not found. Page title: '{title}'. Body: {body_text}"
                        except Exception:
                            last_error = "Form not found and page unreadable"
                        continue
                
                await page.fill("#name", full_name)
                await page.fill("#phone", phone)
                await page.fill("#email", email)
                await page.fill("#password", password)
                
                await page.wait_for_timeout(500)
                
                await page.click("#submitBtn")
                await page.wait_for_timeout(5000)
                
                page_content = await page.content()
                current_url = page.url
                await browser.close()
                browser = None
                
                # Success: redirected away from register page
                if "register" not in current_url.lower():
                    return True, "Account created!", email, phone
                
                # Extract actual error message from the error box
                err_match = re.search(r'class="error"[^>]*>(.*?)</div>', page_content, re.S)
                if err_match:
                    err_text = re.sub(r'<[^>]+>', '', err_match.group(1)).strip()
                    last_error = err_text
                    # Duplicate data - retry with fresh email/phone
                    if "already" in err_text.lower():
                        continue
                    return False, err_text, email, phone
                
                last_error = "No redirect, no error box (page may have changed)"
        except Exception as e:
            last_error = f"Error: {str(e)}"
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
        # Brief pause between retries so parallel workers don't hammer the site
        if attempt < max_attempts - 1:
            await asyncio.sleep(3)
    
    return False, last_error, email, phone

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Access denied.")
        return
    
    keyboard = [
        [
            InlineKeyboardButton("1 Account", callback_data="create_1"),
            InlineKeyboardButton("5 Accounts", callback_data="create_5"),
        ],
        [
            InlineKeyboardButton("10 Accounts", callback_data="create_10"),
            InlineKeyboardButton("20 Accounts", callback_data="create_20"),
        ],
        [
            InlineKeyboardButton("50 Accounts", callback_data="create_50"),
            InlineKeyboardButton("100 Accounts", callback_data="create_100"),
        ],
        [
            InlineKeyboardButton("📋 View Accounts", callback_data="view_accounts"),
            InlineKeyboardButton("⚙️ Settings", callback_data="show_settings"),
        ],
    ]
    
    await update.message.reply_text(
        f"🤖 *Signup Bot v{BOT_VERSION}*\n\n"
        f"Website: readersbuck.com\n"
        f"Accounts created: {len(users)}\n\n"
        f"Select how many accounts to create:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses."""
    query = update.callback_query
    
    if query.from_user.id != OWNER_ID:
        await query.answer("⛔ Access denied.")
        return
    
    await query.answer()
    
    data = query.data
    
    if data.startswith("create_"):
        count = int(data.split("_")[1])
        
        if JOB.get("active"):
            await query.answer("⚠️ A batch is already running!", show_alert=True)
            return
        
        JOB["active"] = True
        JOB["event"] = asyncio.Event()
        
        stop_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data="stop_job")]])
        progress_lock = asyncio.Lock()
        
        def progress_text():
            stopping = JOB["event"] is not None and JOB["event"].is_set()
            header = f"⏹ *STOPPED — finishing up... (v{BOT_VERSION})*" if stopping else f"🔄 *Creating accounts... (v{BOT_VERSION})*"
            txt = f"{header}\n\n"
            txt += f"Progress: {status['done']}/{status['total']}\n"
            txt += f"✅ Created: {len(status['created'])}   ❌ Failed: {status['failed']}\n"
            if status["created"]:
                txt += "\n*Latest:*\n"
                for e in status["created"][-3:]:
                    txt += f"  ✅ `{e}`\n"
            if status.get("errors"):
                txt += "\n*Errors:*\n"
                for e in status["errors"][-3:]:
                    txt += f"  ❌ {e}\n"
            return txt
        
        def safe_text(t):
            # Strip Markdown-dangerous chars so Telegram never rejects the message
            return re.sub(r'([_*`\[\]])', r'\\\1', t)
        
        status = {"done": 0, "created": [], "failed": 0, "total": count, "errors": []}
        
        await query.edit_message_text(progress_text(), reply_markup=stop_btn, parse_mode="Markdown")
        
        sem = asyncio.Semaphore(2)
        last_edit = [0.0]
        
        async def live_update(force=False):
            async with progress_lock:
                now = asyncio.get_event_loop().time()
                if force or (now - last_edit[0]) >= 2.0:
                    last_edit[0] = now
                    stopping = JOB["event"] is not None and JOB["event"].is_set()
                    markup = None if stopping else stop_btn
                    try:
                        await query.edit_message_text(progress_text(), reply_markup=markup, parse_mode="Markdown")
                    except Exception:
                        try:
                            await query.edit_message_text(safe_text(progress_text()), reply_markup=markup)
                        except Exception:
                            pass
        
        async def make_one(i):
            if JOB["event"].is_set():
                return
            async with sem:
                if JOB["event"].is_set():
                    return
                full_name = generate_full_name()
                phone = generate_phone()
                username, email = generate_unique_name()
                password = generate_password()
                
                success, message, final_email, final_phone = await signup_on_website(full_name, phone, email, password)
                
                if success:
                    users[final_email] = {
                        "full_name": full_name,
                        "username": username,
                        "phone": final_phone,
                        "password": password,
                        "status": "active"
                    }
                    status["created"].append(final_email)
                    save_users(users)
                else:
                    status["failed"] += 1
                    status["errors"].append(f"{final_email}: {message}")
                status["done"] += 1
                await live_update(force=True)
        
        await asyncio.gather(*[make_one(i) for i in range(count)])
        
        cancelled = JOB["event"].is_set()
        JOB["active"] = False
        JOB["event"] = None
        
        result = f"{'⏹ STOPPED' if cancelled else '✅ Done!'} *({BOT_VERSION})*\n\n"
        result += f"Created: {len(status['created'])}\n"
        if status["failed"] > 0:
            result += f"Failed: {status['failed']}\n"
        if cancelled:
            result += f"Skipped: {status['total'] - status['done']}\n"
        result += f"Total accounts: {len(users)}\n\n"
        
        if status["created"]:
            result += "*Accounts:*\n"
            for e in status["created"][:15]:
                result += f"  • `{e}`\n"
            if len(status["created"]) > 15:
                result += f"  ... and {len(status['created']) - 15} more"
        
        if status.get("errors"):
            result += "\n\n*Failure reasons:*\n"
            for e in status["errors"][:10]:
                result += f"  ❌ {e}\n"
        
        # Fail-safe: never lose the final report
        try:
            await query.edit_message_text(result, parse_mode="Markdown")
        except Exception:
            try:
                await query.edit_message_text(safe_text(result))
            except Exception:
                try:
                    await query.message.reply_text(re.sub(r'\\[*_`\[\]]', '', result))
                except Exception:
                    pass
    
    elif data == "stop_job":
        if JOB.get("event") and not JOB["event"].is_set():
            JOB["event"].set()
            await query.answer("⏹ STOPPED! Queued accounts cancelled.", show_alert=True)
            try:
                await query.edit_message_text(
                    f"⏹ *STOPPED (v{BOT_VERSION})*\n\n"
                    "Waiting for the 1-2 in-flight accounts to finish...\n"
                    "Remaining accounts were cancelled.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        else:
            await query.answer("No running batch.")
        return
    
    elif data == "view_accounts":
        if not users:
            await query.edit_message_text("No accounts yet.")
            return
        
        entries = []
        for email, user_data in users.items():
            entries.append(
                f"• {user_data.get('full_name', 'Unknown')}\n"
                f"  📧 {email}\n"
                f"  📱 {user_data.get('phone', 'N/A')}\n"
                f"  🔑 {user_data.get('password', 'N/A')}\n"
            )
        
        # Send as multiple messages if too long for one Telegram message (4096 limit)
        chunks = []
        current = f"📋 *Accounts (v{BOT_VERSION})*\n\n"
        for e in entries:
            if len(current) + len(e) > 3900:
                chunks.append(current)
                current = ""
            current += e
        current += f"\nTotal: {len(users)} accounts"
        chunks.append(current)
        
        await query.edit_message_text(chunks[0], parse_mode="Markdown")
        for chunk in chunks[1:]:
            await query.message.reply_text(chunk, parse_mode="Markdown")
    
    elif data == "show_settings":
        await query.edit_message_text(
            f"⚙️ *Settings (v{BOT_VERSION})*\n\n"
            f"Website: {WEBSITE_URL}\n"
            f"Referral: {REFERRAL_CODE}\n"
            f"Name pool: {len(NAME_POOL)} names\n"
            f"Accounts created: {len(users)}\n\n"
            f"Commands:\n"
            f"/seturl <url> - Change URL\n"
            f"/test - Test connection",
            parse_mode="Markdown"
        )

async def new_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    await update.message.reply_text(f"🔄 Creating account... (v{BOT_VERSION})")
    
    full_name = generate_full_name()
    phone = generate_phone()
    username, email = generate_unique_name()
    password = generate_password()
    
    success, message, final_email, final_phone = await signup_on_website(full_name, phone, email, password)
    
    if success:
        users[final_email] = {
            "full_name": full_name,
            "username": username,
            "phone": final_phone,
            "password": password,
            "status": "active"
        }
        save_users(users)
        await update.message.reply_text(
            f"✅ *Account Created! (v{BOT_VERSION})*\n\n"
            f"Name: {full_name}\n"
            f"Phone: {final_phone}\n"
            f"Email: `{final_email}`\n"
            f"Password: `{password}`\n"
            f"Referral: `{REFERRAL_CODE}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ *Failed (v{BOT_VERSION})*\n\nReason: {message}", parse_mode="Markdown")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    if not users:
        await update.message.reply_text("No accounts yet.")
        return
    
    entries = []
    for email, data in users.items():
        entries.append(
            f"• {data.get('full_name', 'Unknown')}\n"
            f"  📧 {email}\n"
            f"  📱 {data.get('phone', 'N/A')}\n"
            f"  🔑 {data.get('password', 'N/A')}\n"
        )
    
    chunks = []
    current = f"📋 *Accounts (v{BOT_VERSION})*\n\n"
    for e in entries:
        if len(current) + len(e) > 3900:
            chunks.append(current)
            current = ""
        current += e
    current += f"\nTotal: {len(users)} accounts"
    chunks.append(current)
    
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="Markdown")

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
    """Set bot commands menu - clear ALL old scopes from moderation bot."""
    from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators
    
    new_commands = [
        BotCommand("start", "Show bot info"),
        BotCommand("newuser", "Auto create new account"),
        BotCommand("list", "View all created accounts"),
        BotCommand("test", "Test connection to website"),
        BotCommand("seturl", "Change website URL"),
        BotCommand("config", "View current settings"),
    ]
    
    try:
        # Delete default scope
        await app.bot.delete_my_commands()
        # Delete old scoped commands (from moderation bot)
        await app.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
        await app.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
        await app.bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
        # Set new commands
        await app.bot.set_my_commands(new_commands)
        
        # Also clear/set owner private chat (old bot used per-chat scopes)
        from telegram import BotCommandScopeChat
        try:
            await app.bot.delete_my_commands(scope=BotCommandScopeChat(OWNER_ID))
        except Exception:
            pass
        await app.bot.set_my_commands(new_commands, scope=BotCommandScopeChat(OWNER_ID))
        
        # Clear/set any registered groups from old moderation bot
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.json"), "r") as f:
                old_data = json.load(f)
            group_ids = old_data.get("registered_groups", old_data.get("groups", []))
            if isinstance(group_ids, dict):
                group_ids = list(group_ids.keys())
            for gid in group_ids:
                try:
                    await app.bot.delete_my_commands(scope=BotCommandScopeChat(int(gid)))
                except Exception:
                    pass
        except Exception:
            pass
        
        print("✅ Command menu updated")
    except Exception as e:
        print(f"⚠️ Menu update error: {e}")

def main():
    # Ensure Chromium is installed before starting
    ensure_chromium()
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
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
