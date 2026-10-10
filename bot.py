import os
import logging
import threading
from datetime import datetime, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "6416848944"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== FILTERS ====================

BANNED_WORDS = [
    # Insults / offensive (English)
    "idiot", "stupid", "dumb", "shut up", "shutup", "loser", "trash",
    "damn", "hell", "crap", "wtf", "stfu", "f off", "f***",
    # Sexual / porn
    "sex", "porn", "nude", "naked", "xxx", "nsfw", "onlyfans",
    "hookup", "sugar daddy", "sugar mommy",
    # Scam / spam
    "free money", "click here now", "limited offer", "act now",
    "congratulations you won", "send me money", "investment opportunity",
    # Insults (French)
    "connard", "connasse", "putain", "merde", "salope", "encule",
    "imbécile", "crétin", "débile", "espèce de",
]

BANNED_EMOJIS = ["💦", "🍑", "🍆", "🍒", "🍌", "🫦", "🔞", "💩"]

# Non-coding keywords for GM (placeholder - can customize)
GM_MESSAGES = [
    "☀️ Good Morning Polyglot Group! Hope you all have a wonderful day learning languages! 🌍",
    "🌅 Good Morning! Another day to practice and improve. Let's go! 💪",
    "☀️ GM Polyglots! Remember: every conversation makes you better. 🗣️",
]

GN_MESSAGES = [
    "🌙 Goodnight Polyglot Group! Sweet dreams and see you tomorrow! 💫",
    "🌙 GN everyone! Rest well, tomorrow we learn again. 🌟",
]

DISCUSSION_PROMPTS = [
    "💬 What's the hardest word to pronounce in your target language?",
    "🌍 What language do you want to learn next and why?",
    "📖 Share a useful phrase you learned today!",
    "🗣️ What's your favorite language-learning app or method?",
    "🎌 What cultural difference surprised you the most?",
]

# ==================== STATE ====================

# Per-group data: {chat_id: {"warnings": {user_id: count}, "new_members": [user_ids]}}
group_data = {}

# Welcome batching: collect new members, send at end of day
pending_welcomes = {}  # {chat_id: [user_ids]}

# ==================== HELPERS ====================

def get_group(chat_id):
    if chat_id not in group_data:
        group_data[chat_id] = {"warnings": {}, "new_members": []}
    return group_data[chat_id]

def is_admin(user_id, chat_id, context):
    try:
        member = context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

def contains_banned_content(text):
    """Check if text contains banned words or emojis. Returns (True, reason) or (False, None)"""
    if not text:
        return False, None

    lower = text.lower()

    for word in BANNED_WORDS:
        if word in lower:
            return True, f"banned word: {word}"

    for emoji in BANNED_EMOJIS:
        if emoji in text:
            return True, f"banned emoji: {emoji}"

    return False, None

# ==================== HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message."""
    if update.message.chat.type == "private":
        await update.message.reply_text(
            "🛡️ *Polyglot Shield Bot*\n\n"
            "I protect Telegram groups from:\n"
            "• Porn & sexual content\n"
            "• Insults & offensive language\n"
            "• Spam & scams\n"
            "• Misused emojis\n\n"
            "Add me to your group and make me admin to start protecting your community!\n\n"
            "Commands:\n"
            "/warn - Warn a user\n"
            "/unwarn - Remove a warning\n"
            "/warnings - View user warnings\n"
            "/ban - Ban a user\n"
            "/unban - Unban a user\n"
            "/rules - Show group rules\n"
            "/setrules - Set group rules\n"
            "/stats - View moderation stats",
            parse_mode="Markdown"
        )

async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome new members (batched at end of day)."""
    chat_id = update.message.chat.id

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        # Add to pending welcomes
        if chat_id not in pending_welcomes:
            pending_welcomes[chat_id] = []
        pending_welcomes[chat_id].append(member.id)

        # Send immediate welcome
        welcome_text = (
            f"👋 Welcome to the group, {member.first_name}!\n\n"
            f"Please introduce yourself:\n"
            f"• Your name\n"
            f"• Country\n"
            f"• Languages you speak\n\n"
            f"📋 *Rules:*\n"
            f"• No porn or sexual content\n"
            f"• No insults or offensive language\n"
            f"• No spam or scams\n"
            f"• Respect everyone\n\n"
            f"⚠️ 3 warnings = auto-ban\n\n"
            f"Enjoy your stay! 🌍"
        )
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def moderate_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Moderate messages for banned content."""
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    text = update.message.text

    # Skip admins
    if is_admin(user_id, chat_id, context):
        return

    # Check for banned content
    has_banned, reason = contains_banned_content(text)

    if has_banned:
        # Delete the message
        try:
            await update.message.delete()
        except Exception:
            pass

        # Warn the user
        data = get_group(chat_id)
        current_warnings = data["warnings"].get(user_id, 0) + 1
        data["warnings"][user_id] = current_warnings

        username = update.message.from_user.first_name

        if current_warnings >= 3:
            # Ban the user
            try:
                await context.bot.ban_chat_member(chat_id, user_id)
                await update.message.reply_text(
                    f"⛔ {username} has been banned (3 warnings reached).\n"
                    f"Reason: {reason}",
                    parse_mode="Markdown"
                )
            except Exception:
                await update.message.reply_text(
                    f"⚠️ {username} reached 3 warnings but I couldn't ban them. "
                    f"Please ban manually.",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text(
                f"⚠️ {username}, your message was removed.\n"
                f"Reason: {reason}\n"
                f"Warning {current_warnings}/3",
                parse_mode="Markdown"
            )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check photos for NSFW (simplified - checks caption)."""
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id

    if is_admin(user_id, chat_id, context):
        return

    caption = update.message.caption or ""
    has_banned, reason = contains_banned_content(caption)

    if has_banned:
        try:
            await update.message.delete()
        except Exception:
            pass

        data = get_group(chat_id)
        current_warnings = data["warnings"].get(user_id, 0) + 1
        data["warnings"][user_id] = current_warnings

        if current_warnings >= 3:
            try:
                await context.bot.ban_chat_member(chat_id, user_id)
                await update.message.reply_text(
                    f"⛔ User banned (3 warnings). Reason: {reason}"
                )
            except Exception:
                pass
        else:
            await update.message.reply_text(
                f"⚠️ Photo removed. Reason: {reason}. Warning {current_warnings}/3"
            )

# ==================== ADMIN COMMANDS ====================

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Warn a user: /warn @username or /warn user_id"""
    chat_id = update.message.chat.id
    admin_id = update.message.from_user.id

    if not is_admin(admin_id, chat_id, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /warn @username or /warn user_id")
        return

    # Get target user
    target = context.args[0]
    if target.startswith("@"):
        # Try to get user ID from username
        await update.message.reply_text(
            "Please reply to the user's message with /warn to warn them."
        )
        return

    try:
        user_id = int(target)
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    data = get_group(chat_id)
    current_warnings = data["warnings"].get(user_id, 0) + 1
    data["warnings"][user_id] = current_warnings

    await update.message.reply_text(
        f"⚠️ User warned. Warning {current_warnings}/3"
    )

async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a warning: /unwarn user_id"""
    chat_id = update.message.chat.id
    admin_id = update.message.from_user.id

    if not is_admin(admin_id, chat_id, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unwarn user_id")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    data = get_group(chat_id)
    if user_id in data["warnings"] and data["warnings"][user_id] > 0:
        data["warnings"][user_id] -= 1
        await update.message.reply_text(
            f"✅ Warning removed. Now: {data['warnings'][user_id]}/3"
        )
    else:
        await update.message.reply_text("User has no warnings.")

async def view_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View user warnings: /warnings user_id"""
    chat_id = update.message.chat.id

    if not context.args:
        await update.message.reply_text("Usage: /warnings user_id")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    data = get_group(chat_id)
    warnings = data["warnings"].get(user_id, 0)
    await update.message.reply_text(f"⚠️ User {user_id} has {warnings}/3 warnings.")

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user: /ban user_id"""
    chat_id = update.message.chat.id
    admin_id = update.message.from_user.id

    if not is_admin(admin_id, chat_id, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /ban user_id")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await update.message.reply_text(f"⛔ User {user_id} banned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to ban: {e}")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unban a user: /unban user_id"""
    chat_id = update.message.chat.id
    admin_id = update.message.from_user.id

    if not is_admin(admin_id, chat_id, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unban user_id")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    try:
        await context.bot.unban_chat_member(chat_id, user_id)
        await update.message.reply_text(f"✅ User {user_id} unbanned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unban: {e}")

async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set group rules: /setrules <rules text>"""
    chat_id = update.message.chat.id
    admin_id = update.message.from_user.id

    if not is_admin(admin_id, chat_id, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /setrules <rules text>\n\n"
            "Example: /setrules Be respectful. No spam. No porn."
        )
        return

    rules = " ".join(context.args)
    data = get_group(chat_id)
    data["rules"] = rules

    await update.message.reply_text(
        f"✅ Rules updated:\n\n{rules}",
        parse_mode="Markdown"
    )

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show group rules."""
    chat_id = update.message.chat.id
    data = get_group(chat_id)

    rules = data.get("rules", "No rules set yet. Admin can set rules with /setrules")
    await update.message.reply_text(
        f"📋 *Group Rules:*\n\n{rules}",
        parse_mode="Markdown"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View moderation stats."""
    chat_id = update.message.chat.id

    if not is_admin(update.message.from_user.id, chat_id, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    data = get_group(chat_id)
    total_warned = len(data["warnings"])
    total_banned = sum(1 for w in data["warnings"].values() if w >= 3)

    await update.message.reply_text(
        f"📊 *Moderation Stats*\n\n"
        f"👥 Total users warned: {total_warned}\n"
        f"⛔ Total banned: {total_banned}\n"
        f"📝 Total messages moderated: Check logs",
        parse_mode="Markdown"
    )

# ==================== SCHEDULED TASKS ====================

async def send_gm(context: ContextTypes.DEFAULT_TYPE):
    """Send GM message to all groups."""
    import random
    for chat_id in group_data:
        try:
            msg = random.choice(GM_MESSAGES)
            await context.bot.send_message(chat_id, msg)
        except Exception:
            pass

async def send_gn(context: ContextTypes.DEFAULT_TYPE):
    """Send GN message to all groups."""
    import random
    for chat_id in group_data:
        try:
            msg = random.choice(GN_MESSAGES)
            await context.bot.send_message(chat_id, msg)
        except Exception:
            pass

async def send_discussion(context: ContextTypes.DEFAULT_TYPE):
    """Send discussion prompt to all groups."""
    import random
    for chat_id in group_data:
        try:
            msg = random.choice(DISCUSSION_PROMPTS)
            await context.bot.send_message(chat_id, msg)
        except Exception:
            pass

# ==================== MAIN ====================

async def post_init(app):
    """Set bot commands."""
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("warn", "Warn a user"),
        BotCommand("unwarn", "Remove a warning"),
        BotCommand("warnings", "View warnings"),
        BotCommand("ban", "Ban a user"),
        BotCommand("unban", "Unban a user"),
        BotCommand("rules", "Show rules"),
        BotCommand("setrules", "Set rules"),
        BotCommand("stats", "View stats"),
    ])

    # Schedule GM at 8:00 AM daily
    app.job_queue.run_daily(send_gm, time=time(8, 0))

    # Schedule GN at 10:00 PM daily
    app.job_queue.run_daily(send_gn, time=time(22, 0))

    # Schedule discussion prompt at 2:00 PM daily
    app.job_queue.run_daily(send_discussion, time=time(14, 0))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server on :{port}")
    server.serve_forever()


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("unwarn", unwarn_user))
    app.add_handler(CommandHandler("warnings", view_warnings))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("setrules", set_rules))
    app.add_handler(CommandHandler("stats", stats))

    # Welcome new members
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))

    # Content moderation
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, moderate_content))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    threading.Thread(target=run_health_server, daemon=True).start()

    print("🛡️ Polyglot Shield Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
