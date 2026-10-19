BOT_VERSION = "5.9"

import os
import re
import json
import random
import logging
import asyncio
import subprocess
import threading
import string
import time
import hmac
import hashlib
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Patchright = hardened Playwright fork (harder for sites to detect bots)
try:
    from patchright.async_api import async_playwright
    BROWSER_ENGINE = "patchright"
except ImportError:
    from playwright.async_api import async_playwright
    BROWSER_ENGINE = "playwright"

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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
# Fixed email for OTP sites (codes must reach an inbox you control)
OVERRIDE_EMAIL = config.get("override_email", "")
# Region the browser pretends to be in (for geo-blocked sites)
ACTIVE_REGION = config.get("region", "nigeria")
# Auto-OTP mode: True = temp email (mail.tm), reads OTP automatically.
# False = your own email (OVERRIDE_EMAIL), you send me the code.
AUTO_OTP = config.get("auto_otp", False)

# Web dashboard token (generated once, stored in config). Only you have the link.
WEB_TOKEN = config.get("web_token", "")
if not WEB_TOKEN:
    WEB_TOKEN = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    config["web_token"] = WEB_TOKEN
    with open(CONFIG_PATH, "w") as _f:
        json.dump(config, _f, indent=2)

# Filled in main() — lets the HTTP thread schedule work on the bot's event loop
BOT_LOOP = None

REGIONS = {
    "nigeria":  {"label": "🇳🇬 Nigeria",     "tz": "Africa/Lagos",        "locale": "en-NG", "ua_loc": "NG", "geo": (6.5244, 3.3792)},
    "usa":      {"label": "🇺🇸 USA",         "tz": "America/New_York",    "locale": "en-US", "ua_loc": "US", "geo": (40.7128, -74.0060)},
    "uk":       {"label": "🇬🇧 UK",          "tz": "Europe/London",       "locale": "en-GB", "ua_loc": "GB", "geo": (51.5074, -0.1278)},
    "ghana":    {"label": "🇬🇭 Ghana",       "tz": "Africa/Accra",        "locale": "en-GH", "ua_loc": "GH", "geo": (5.6037, -0.1870)},
    "kenya":    {"label": "🇰🇪 Kenya",       "tz": "Africa/Nairobi",      "locale": "en-KE", "ua_loc": "KE", "geo": (-1.2921, 36.8219)},
    "south_africa": {"label": "🇿🇦 South Africa", "tz": "Africa/Johannesburg", "locale": "en-ZA", "ua_loc": "ZA", "geo": (-26.2041, 28.0473)},
    "canada":   {"label": "🇨🇦 Canada",      "tz": "America/Toronto",     "locale": "en-CA", "ua_loc": "CA", "geo": (43.6532, -79.3832)},
    "india":    {"label": "🇮🇳 India",       "tz": "Asia/Kolkata",        "locale": "en-IN", "ua_loc": "IN", "geo": (28.6139, 77.2090)},
    "uae":      {"label": "🇦🇪 UAE",         "tz": "Asia/Dubai",          "locale": "en-AE", "ua_loc": "AE", "geo": (25.2048, 55.2708)},
    "germany":  {"label": "🇩🇪 Germany",     "tz": "Europe/Berlin",       "locale": "de-DE", "ua_loc": "DE", "geo": (52.5200, 13.4050)},
}

def region_options():
    """Playwright context options for the active region (stealth-ish)."""
    r = REGIONS.get(ACTIVE_REGION, REGIONS["nigeria"])
    return {
        "timezone_id": r["tz"],
        "locale": r["locale"],
        "geolocation": {"latitude": r["geo"][0], "longitude": r["geo"][1]},
        "permissions": ["geolocation"],
        "user_agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1366, "height": 768},
        "extra_http_headers": {"Accept-Language": f"{r['locale']},en;q=0.9"},
    }

# ---------- mail.tm temp inbox (real email, auto OTP reading) ----------
MAILTM_BASE = "https://api.mail.tm"

async def create_temp_inbox():
    """Create a real temporary email on mail.tm. Returns (email, token) or (None, None)."""
    def _create():
        try:
            r = requests.get(f"{MAILTM_BASE}/domains", timeout=20)
            domain = r.json()["hydra:member"][0]["domain"]
            local = "sbx" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            email = f"{local}@{domain}"
            pw = "Sbx!" + "".join(random.choices(string.ascii_letters + string.digits, k=12))
            r2 = requests.post(f"{MAILTM_BASE}/accounts",
                              json={"address": email, "password": pw}, timeout=20)
            if r2.status_code not in (200, 201):
                return None, None
            r3 = requests.post(f"{MAILTM_BASE}/token",
                              json={"address": email, "password": pw}, timeout=20)
            return email, r3.json().get("token")
        except Exception as e:
            logger.error(f"mail.tm create error: {e}")
            return None, None
    return await asyncio.get_event_loop().run_in_executor(None, _create)

async def wait_for_otp_email(token, timeout_sec=180, notify=None):
    """Poll the temp inbox for an OTP code. Returns code string or None."""
    def _poll():
        deadline = time.time() + timeout_sec
        seen = set()
        while time.time() < deadline:
            try:
                r = requests.get(f"{MAILTM_BASE}/messages",
                                headers={"Authorization": f"Bearer {token}"}, timeout=20)
                for msg in r.json().get("hydra:member", []):
                    mid = msg.get("id")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    # Fetch full message body
                    r2 = requests.get(f"{MAILTM_BASE}/messages/{mid}",
                                     headers={"Authorization": f"Bearer {token}"}, timeout=20)
                    body = r2.json().get("text", "") or ""
                    html = r2.json().get("html", "")
                    if isinstance(html, list):
                        html = " ".join(html)
                    # Look for a 4-8 digit/char verification code
                    for pattern in (r'\b(\d{4,8})\b', r'\b([A-Z0-9]{4,8})\b',
                                    r'code[:\s]+([A-Za-z0-9]{4,8})',
                                    r'verification[:\s]+([A-Za-z0-9]{4,8})'):
                        m = re.search(pattern, body + " " + html, re.I)
                        if m:
                            return m.group(1)
            except Exception as e:
                logger.error(f"mail.tm poll error: {e}")
            time.sleep(5)
        return None
    code = await asyncio.get_event_loop().run_in_executor(None, _poll)
    return code

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

# Pending OTP requests: chat_id -> asyncio.Future resolved when owner sends the code
OTP_WAIT = {}

# Waiting for the owner to type a value (url/email): chat_id -> "url" | "email"
PENDING_INPUT = {}

def chromium_ready():
    """Check if Playwright's Chromium binary already exists."""
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                           os.path.expanduser("~/.cache/ms-playwright"))
    if not os.path.isdir(cache):
        return False
    for entry in os.listdir(cache):
        if entry.startswith("chromium"):
            for exe in ("chrome-linux/chrome", "chrome-linux/headless_shell",
                        "chrome-headless-shell-linux64/chrome-headless-shell"):
                if os.path.exists(os.path.join(cache, entry, exe)):
                    return True
    return False

def ensure_chromium():
    """Install Chromium only if missing (build usually does this already)."""
    if chromium_ready():
        logger.info("Chromium already present - skipping install")
        return
    try:
        module = "patchright" if BROWSER_ENGINE == "patchright" else "playwright"
        subprocess.run(
            ["python", "-m", module, "install", "chromium"],
            capture_output=True,
            timeout=300
        )
        logger.info(f"Chromium installed via {module}")
    except Exception as e:
        logger.error(f"Chromium install error: {e}")

async def launch_chromium(p):
    """Launch headless Chromium with retry on ETXTBSY (binary busy).
    Memory-safe flags for Render free tier (512MB)."""
    args = [
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
        # Memory savers for the 512MB free instance
        "--disable-gpu", "--disable-software-rasterizer",
        "--disable-extensions", "--disable-background-networking",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=site-per-process,Translate,BackForwardCache",
        "--js-flags=--max-old-space-size=128",
        "--disk-cache-size=1",
        "--media-cache-size=1",
        "--no-first-run", "--no-default-browser-check",
    ]
    if BROWSER_ENGINE != "patchright":
        # patchright handles anti-detection itself; these flags can hurt it
        args.append("--disable-blink-features=AutomationControlled")
    last_err = None
    for i in range(4):
        try:
            return await p.chromium.launch(headless=True, args=args)
        except Exception as e:
            last_err = e
            if "ETXTBSY" in str(e) and i < 3:
                await asyncio.sleep(5)
                continue
            raise
    raise last_err

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

# ---------- Auto-learn form detection ----------
LEARNED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned_form.json")

def load_learned():
    try:
        with open(LEARNED_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_learned(data):
    try:
        with open(LEARNED_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Save learned form error: {e}")

# Runs inside the page: maps every input to a field by label/name/id/placeholder
DETECT_FORM_JS = """
() => {
  const KEYWORDS = {
    username:         ["username", "user name", "handle"],
    name:             ["full name", "your name", "name", "fullname"],
    phone:            ["phone number", "phone", "mobile", "tel", "whatsapp"],
    email:            ["email address", "email", "e-mail", "mail"],
    confirm_password: ["confirm password", "repeat password", "re-enter password", "retype password", "confirm pass"],
    password:         ["password", "passcode", "pass"],
    referral:         ["referral", "invite", "invite code", "referral code", "ref"],
    gender:           ["gender", "sex"],
    dob:              ["date of birth", "birth date", "dob", "birthday"],
    address:          ["address", "street", "home address"],
    city:             ["city", "town"],
    state:            ["state", "province"],
    country:          ["country", "nation"]
  };
  const labels = {};
  document.querySelectorAll("label[for]").forEach(l => {
    labels[l.getAttribute("for")] = (l.innerText || "").trim().toLowerCase();
  });
  const fields = {};
  const describe = el => {
    const id = el.id ? "#" + el.id : "";
    const name = el.name ? `${el.tagName.toLowerCase()}[name="${el.name}"]` : "";
    const placeholder = el.placeholder ? `[placeholder*="${el.placeholder}"]` : "";
    return id || name || placeholder || "";
  };
  document.querySelectorAll("input, textarea, select").forEach(el => {
    const type = (el.type || el.tagName.toLowerCase()).toLowerCase();
    if (["hidden", "submit", "button", "checkbox", "radio", "image"].includes(type)) return;
    const id = el.id || "";
    const text = [
      labels[id] || "",
      (el.name || "").toLowerCase(),
      id.toLowerCase(),
      (el.placeholder || "").toLowerCase(),
      (el.getAttribute("aria-label") || "").toLowerCase()
    ].join(" ");
    for (const [field, kws] of Object.entries(KEYWORDS)) {
      if (fields[field]) continue;
      if (type === field || kws.some(k => text.includes(k))) {
        fields[field] = describe(el);
        break;
      }
    }
  });
  // submit/next button
  let submit = "";
  const btns = [...document.querySelectorAll("button, input[type=submit], a[role=button], .submit")];
  for (const b of btns) {
    const t = ((b.innerText || b.value || "") + " " + (b.className || "") + " " + (b.id || "")).toLowerCase();
    if (t.includes("create account") || t.includes("sign up") || t.includes("register") || t.includes("submit") || t.includes("next") || t.includes("continue") || t.includes("proceed") || b.id === "submitbtn" || (b.className && b.className.includes("submit"))) {
      submit = b.id ? "#" + b.id : (b.className && b.className.includes("submit") ? "." + b.className.trim().split(/\\s+/)[0] : "");
      if (!submit && b.tagName === "BUTTON") submit = "button[type=submit]";
      break;
    }
  }
  if (!submit) submit = "button[type=submit], .submit, button";
  return { fields, submit };
}
"""

# What filled data each field expects
FIELD_VALUES = {
    "name": "full_name", "phone": "phone", "email": "email",
    "password": "password", "referral": "referral_code",
    "confirm_password": "password", "username": "username",
    "gender": "gender", "dob": "dob", "address": "address",
    "city": "city", "state": "state", "country": "country"
}

def extra_signup_values(username):
    """Plausible values for common extra wizard-step fields."""
    return {
        "username": username,
        "gender": random.choice(["Male", "Female"]),
        "dob": f"{random.randint(1995, 2003)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
        "address": f"{random.randint(1, 99)} {random.choice(['Aba Road', 'Ikoyi Street', 'Ahmadu Bello Way', 'Zik Avenue', 'Ogui Road'])}",
        "city": random.choice(["Lagos", "Abuja", "Port Harcourt", "Kano", "Ibadan"]),
        "state": random.choice(["Lagos", "Abuja FCT", "Rivers", "Kano", "Oyo"]),
        "country": "Nigeria"
    }

# Runs inside the page: returns selector of OTP input, or null
OTP_DETECT_JS = """
() => {
  const otpInput = document.querySelector(
    'input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i],' +
    ' input[placeholder*="otp" i], input[placeholder*="verification" i], input[placeholder*="code sent" i]'
  );
  if (otpInput && otpInput.offsetParent !== null) {
    return otpInput.id ? "#" + otpInput.id : (otpInput.name ? 'input[name="' + otpInput.name + '"]' : "input");
  }
  const body = (document.body.innerText || "").toLowerCase();
  if (/verification code|one[- ]time|code sent|enter (the )?code/.test(body)) {
    // OTP asked for but we could not pin an input - ask human to look
    return "TEXT_PROMPT";
  }
  return null;
}
"""

async def detect_form(page):
    """Detect form fields in the open page. Returns {fields:{...}, submit:str}."""
    return await page.evaluate(DETECT_FORM_JS)

def mapping_report(mapping):
    """Human-readable report of a detected mapping."""
    fields = mapping.get("fields", {})
    lines = []
    labels = {"name": "Full name", "phone": "Phone", "email": "Email", "password": "Password", "referral": "Referral"}
    for key in ["name", "phone", "email", "password", "referral"]:
        if key in fields:
            lines.append(f"  ✅ {labels[key]} → `{fields[key]}`")
        else:
            lines.append(f"  ⚠️ {labels[key]} → not found")
    lines.append(f"  🔘 Submit → `{mapping.get('submit', '?')}`")
    return "\n".join(lines)

async def wait_for_form_ready(page, timeout_ms=30000):
    """Wait through loading animations/spinners until the signup form appears."""
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        try:
            mapping = await detect_form(page)
            if mapping.get("fields") and len(mapping["fields"]) >= 2:
                # Also ensure at least one field is actually visible (not behind overlay)
                any_visible = False
                for sel in mapping["fields"].values():
                    try:
                        if sel and await page.is_visible(sel, timeout=500):
                            any_visible = True
                            break
                    except Exception:
                        continue
                if any_visible:
                    return mapping
        except Exception:
            pass
        # Nudge common loading spinners out of the way
        try:
            await page.evaluate("""
                () => {
                  document.querySelectorAll(
                    '.loading, .spinner, .loader, [class*="loader"], [class*="spinner"], [id*="loading"]'
                  ).forEach(el => el.style.display = 'none');
                }
            """)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    # Timed out - return whatever we last saw (diagnostics use it)
    try:
        return await detect_form(page)
    except Exception:
        return {"fields": {}, "submit": ""}

async def learn_form(url):
    """Visit url, screenshot the page, detect the signup form.
    Returns (screenshot_path_or_None, mapping, error_or_None)."""
    async with async_playwright() as p:
        browser = await launch_chromium(p)
        context = await browser.new_context(**region_options())
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        # Wait through loading animations for the real form
        mapping = await wait_for_form_ready(page, timeout_ms=20000)
        
        shot = None
        try:
            shot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learn_shot.png")
            await page.screenshot(path=shot, full_page=True)
        except Exception:
            pass
        
        await browser.close()
        
        if not mapping.get("fields"):
            return shot, mapping, "No form fields found on this page."
        return shot, mapping, None

async def request_otp(bot, chat_id, screenshot_path=None, url=""):
    """Ask the owner for an OTP code and wait for their reply (3 min timeout)."""
    fut = asyncio.get_running_loop().create_future()
    OTP_WAIT[chat_id] = fut
    try:
        caption = (
            f"🔐 *OTP required (v{BOT_VERSION})*\n\n"
            f"Site: `{url}`\n\n"
            "Send me the verification code from your email/phone.\n"
            "You have 3 minutes."
        )
        if screenshot_path and os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as photo:
                await bot.send_photo(chat_id, photo, caption=caption, parse_mode="Markdown")
        else:
            await bot.send_message(chat_id, caption, parse_mode="Markdown")
        code = await asyncio.wait_for(fut, timeout=180)
        return code
    except asyncio.TimeoutError:
        try:
            await bot.send_message(chat_id, "⏱ OTP wait timed out.")
        except Exception:
            pass
        return None
    finally:
        OTP_WAIT.pop(chat_id, None)

async def signup_on_website(full_name, phone, email, password, max_attempts=3,
                            otp_chat_id=None, bot=None, otp_token=None):
    # otp_token = mail.tm bearer token -> read OTP from temp inbox automatically
    last_error = "Unknown error"
    # Use learned mapping for this URL, or auto-detect live
    learned = load_learned().get(WEBSITE_URL)
    for attempt in range(max_attempts):
        # Fresh data each attempt (keep same email if override set - OTP must reach it)
        if attempt > 0:
            phone = generate_phone()
            if not OVERRIDE_EMAIL:
                _, email = generate_unique_name()
        browser = None
        try:
            async with async_playwright() as p:
                browser = await launch_chromium(p)
                context = await browser.new_context(**region_options())
                # Hide automation flags from the site
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = await context.new_page()
                
                # domcontentloaded is reliable; networkidle hangs on analytics beacons
                await page.goto(WEBSITE_URL, timeout=45000, wait_until="domcontentloaded")
                # Wait through loading animations/spinners until form appears
                wait_mapping = await wait_for_form_ready(page, timeout_ms=25000)
                
                values = {
                    "full_name": full_name, "phone": phone,
                    "email": email, "password": password,
                    "referral_code": REFERRAL_CODE
                }
                values.update(extra_signup_values(email.split("@")[0]))
                
                attempt_failed = False
                
                # Multi-step wizard: fill -> click next/submit -> repeat (max 6 steps)
                for step in range(6):
                    mapping = (learned if step == 0 else None) or wait_mapping or await detect_form(page)
                    fields = mapping.get("fields", {})
                    submit_sel = mapping.get("submit", "#submitBtn")
                    
                    if step == 0 and not (fields.get("name") or fields.get("username")):
                        try:
                            title = await page.title()
                            body_text = (await page.inner_text("body"))[:300]
                            last_error = f"Form fields not found. Page title: '{title}'. Body: {body_text}"
                        except Exception:
                            last_error = "Form fields not found and page unreadable"
                        attempt_failed = True
                        break
                    
                    # Fill every field this step exposes
                    for field, selector in fields.items():
                        value = values.get(FIELD_VALUES.get(field, field))
                        if not value or not selector:
                            continue
                        try:
                            await page.fill(selector, value)
                        except Exception:
                            try:
                                await page.select_option(selector, label=value)
                            except Exception:
                                try:
                                    await page.select_option(selector, value)
                                except Exception:
                                    pass  # optional/unknown field
                    
                    await page.wait_for_timeout(500)
                    
                    # Click Next / Continue / Submit (whichever exists)
                    clicked = False
                    for sel in (submit_sel, "button:has-text('Next')", "button:has-text('Continue')",
                                "button:has-text('Proceed')", "button:has-text('Next Step')",
                                "input[type=submit]"):
                        try:
                            await page.click(sel, timeout=3000)
                            clicked = True
                            break
                        except Exception:
                            continue
                    if not clicked:
                        last_error = f"Step {step + 1}: no Next/Submit button found"
                        attempt_failed = True
                        break
                    
                    await page.wait_for_timeout(4000)
                    
                    # OTP step? (interactive only)
                    otp_sel = None
                    try:
                        otp_sel = await page.evaluate(OTP_DETECT_JS)
                    except Exception:
                        pass
                    
                    if otp_sel and otp_token:
                        # Auto-OTP: read the code from the mail.tm temp inbox
                        code = await wait_for_otp_email(otp_token, timeout_sec=180)
                        if not code:
                            await browser.close()
                            browser = None
                            return False, "Auto-OTP: no verification email received in 3 min", email, phone
                        try:
                            if otp_sel == "TEXT_PROMPT":
                                for sel in ['input[autocomplete="one-time-code"]', 'input[name*="otp" i]',
                                            'input[placeholder*="code" i]', 'input[type="tel"]', 'input[type="text"]']:
                                    try:
                                        await page.fill(sel, code)
                                        otp_sel = sel
                                        break
                                    except Exception:
                                        continue
                            else:
                                await page.fill(otp_sel, code)
                            try:
                                await page.click(submit_sel)
                            except Exception:
                                await page.keyboard.press("Enter")
                            await page.wait_for_timeout(5000)
                        except Exception as e:
                            await browser.close()
                            browser = None
                            return False, f"OTP fill failed: {str(e)}", email, phone
                    elif otp_sel and bot and otp_chat_id:
                        otp_shot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otp_shot.png")
                        try:
                            await page.screenshot(path=otp_shot)
                        except Exception:
                            otp_shot = None
                        code = await request_otp(bot, otp_chat_id, otp_shot, page.url)
                        if not code:
                            await browser.close()
                            browser = None
                            return False, "OTP not provided (cancelled or timed out)", email, phone
                        try:
                            if otp_sel == "TEXT_PROMPT":
                                for sel in ['input[autocomplete="one-time-code"]', 'input[name*="otp" i]',
                                            'input[placeholder*="code" i]', 'input[type="tel"]', 'input[type="text"]']:
                                    try:
                                        await page.fill(sel, code)
                                        otp_sel = sel
                                        break
                                    except Exception:
                                        continue
                            else:
                                await page.fill(otp_sel, code)
                            try:
                                await page.click(submit_sel)
                            except Exception:
                                await page.keyboard.press("Enter")
                            await page.wait_for_timeout(5000)
                        except Exception as e:
                            await browser.close()
                            browser = None
                            return False, f"OTP fill failed: {str(e)}", email, phone
                    elif otp_sel:
                        await browser.close()
                        browser = None
                        return False, "Site requires OTP — use /newuser (interactive) or /setemail", email, phone
                    
                    # Success check: left the form flow, or landed on a done-ish page
                    page_content = await page.content()
                    current_url = page.url.lower()
                    body_l = ""
                    try:
                        body_l = (await page.inner_text("body")).lower()
                    except Exception:
                        pass
                    
                    success_markers = ("dashboard", "/home", "welcome", "success", "congrat",
                                       "verify", "logout", "account created", "check your email")
                    if (any(k in current_url for k in success_markers)
                            or "register" not in current_url and "login" not in current_url
                            or any(k in body_l for k in ("welcome", "congratulations", "account created", "verify your email"))):
                        return True, "Account created!", email, phone
                    
                    # Error box on this step?
                    err_match = re.search(r'class="error"[^>]*>(.*?)</div>', page_content, re.S)
                    if err_match:
                        err_text = re.sub(r'<[^>]+>', '', err_match.group(1)).strip()
                        last_error = err_text
                        attempt_failed = True
                        break
                    
                    # Otherwise assume another form step appeared - loop continues
                
                if attempt_failed:
                    # Duplicate data - retry with fresh email/phone
                    if last_error and "already" in last_error.lower():
                        continue
                    if last_error and last_error.startswith("Step"):
                        return False, last_error, email, phone
                    if "Form fields not found" not in (last_error or ""):
                        return False, last_error or "Unknown step failure", email, phone
                    continue
                
                # Loop exhausted without success or a clear error
                last_error = "Signup wizard did not finish in 6 steps (page may need manual handling)"
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

def main_menu_keyboard():
    """Main menu with color-coded emoji buttons (🟢 go/on, 🔴 off, 🟡 config)."""
    return [
        [
            InlineKeyboardButton("🟢 1 Account", callback_data="create_1"),
            InlineKeyboardButton("🟢 5 Accounts", callback_data="create_5"),
        ],
        [
            InlineKeyboardButton("🟢 10 Accounts", callback_data="create_10"),
            InlineKeyboardButton("🟢 20 Accounts", callback_data="create_20"),
        ],
        [
            InlineKeyboardButton("🟢 50 Accounts", callback_data="create_50"),
            InlineKeyboardButton("🟢 100 Accounts", callback_data="create_100"),
        ],
        [
            InlineKeyboardButton("📋 View Accounts", callback_data="view_accounts"),
            InlineKeyboardButton("⚙️ Settings", callback_data="show_settings"),
        ],
        [
            InlineKeyboardButton("🟡 Set URL", callback_data="ask_url"),
            InlineKeyboardButton("🟡 Set Email", callback_data="ask_email"),
        ],
        [
            InlineKeyboardButton("🟡 Learn Form", callback_data="ask_learn"),
            InlineKeyboardButton(f"🌍 {REGIONS.get(ACTIVE_REGION, REGIONS['nigeria'])['label']}", callback_data="ask_region"),
        ],
        [
            InlineKeyboardButton(
                f"{'🟢 Auto-OTP: ON' if AUTO_OTP else '🔴 Auto-OTP: OFF'}",
                callback_data="toggle_auto_otp"
            ),
        ],
        [
            InlineKeyboardButton("🌐 Open App", web_app=WebAppInfo(url=_app_url())),
        ],
    ]

def main_menu_text():
    status_on = "🟢 *ON* — I create a temp email & read codes automatically"
    status_off = "🔴 *OFF* — I use your email & ask you for codes"
    return (
        f"🤖 *Signup Bot v{BOT_VERSION}*\n\n"
        f"Website: {WEBSITE_URL}\n"
        f"Accounts created: {len(users)}\n"
        f"Auto-OTP: {status_on if AUTO_OTP else status_off}\n"
        f"Browser: {BROWSER_ENGINE}\n\n"
        f"🟢 = create accounts\n🟡 = settings to change\n\n"
        f"Select how many accounts to create:"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Access denied.")
        return
    
    # Cancel any waiting input (url/email/OTP prompt)
    PENDING_INPUT.pop(update.message.chat_id, None)
    
    await update.message.reply_text(
        main_menu_text(),
        reply_markup=InlineKeyboardMarkup(main_menu_keyboard()),
        parse_mode="Markdown"
    )

async def start_from_callback(query):
    """Re-render the main menu from a callback query."""
    PENDING_INPUT.pop(query.message.chat_id, None)
    await query.edit_message_text(
        main_menu_text(),
        reply_markup=InlineKeyboardMarkup(main_menu_keyboard()),
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
        
        # Render free tier = 512MB. One Chromium at a time to avoid OOM restarts.
        sem = asyncio.Semaphore(1)
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
                otp_token = None
                if AUTO_OTP:
                    # Real temp inbox so OTP emails arrive in a box we can read
                    temp_email, otp_token = await create_temp_inbox()
                    if not temp_email:
                        status["failed"] += 1
                        status["errors"].append(f"{email}: temp inbox (mail.tm) unavailable")
                        status["done"] += 1
                        await live_update(force=True)
                        return
                    email = temp_email
                
                success, message, final_email, final_phone = await signup_on_website(
                    full_name, phone, email, password, otp_token=otp_token)
                
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
    
    elif data == "toggle_auto_otp":
        global AUTO_OTP
        AUTO_OTP = not AUTO_OTP
        config["auto_otp"] = AUTO_OTP
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        if AUTO_OTP:
            await query.answer("🟢 Auto-OTP ON — temp email + automatic code reading")
            await query.edit_message_text(
                "🟢 *Auto-OTP: ON*\n\n"
                "I'll create a real temp email (mail.tm) for each account\n"
                "and read the verification code automatically.\n"
                "No action needed from you.\n\n"
                "Tap 🟢 below to go back.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« Back to menu", callback_data="back_to_menu")]]
                ),
                parse_mode="Markdown"
            )
        else:
            await query.answer("🔴 Auto-OTP OFF — I'll ask you for codes")
            await query.edit_message_text(
                "🔴 *Auto-OTP: OFF*\n\n"
                "I'll use your own email (via Set Email) or a random one.\n"
                "If the site asks for a code, I'll send it to you here.\n\n"
                "Tap below to go back.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« Back to menu", callback_data="back_to_menu")]]
                ),
                parse_mode="Markdown"
            )
        return

    elif data == "ask_region":
        rows = []
        row = []
        for key, r in REGIONS.items():
            row.append(InlineKeyboardButton(r["label"], callback_data=f"set_region:{key}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("« Back", callback_data="back_to_menu")])
        await query.edit_message_text(
            "🌍 *Select your region*\n\n"
            "The browser pretends to be from this country\n"
            "(timezone, language, GPS location).\n"
            "Use this if a site says \"not available in your country\".",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown"
        )
        return
    
    elif data.startswith("set_region:"):
        global ACTIVE_REGION
        key = data.split(":", 1)[1]
        if key in REGIONS:
            ACTIVE_REGION = key
            config["region"] = key
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
            await query.answer(f"Region set to {REGIONS[key]['label']}")
            await query.edit_message_text(
                f"✅ Region set to {REGIONS[key]['label']}\n\n"
                f"Timezone: {REGIONS[key]['tz']}\n"
                f"Language: {REGIONS[key]['locale']}\n\n"
                f"Signups will now look like they come from this country.",
                parse_mode="Markdown"
            )
        else:
            await query.answer("Unknown region")
        return
    
    elif data == "back_to_menu":
        await start_from_callback(query)
        return
    
    elif data == "ask_learn":
        PENDING_INPUT[query.message.chat_id] = "learn_url"
        await query.edit_message_text(
            "🔍 *Send me the page to learn*\n\n"
            "Type/paste the signup page URL in your next message.\n"
            "I'll screenshot it, detect the form, and save the mapping.\n\n"
            "Or send `here` to learn the current URL:\n"
            f"`{WEBSITE_URL}`\n\n"
            "(Send /start to cancel)",
            parse_mode="Markdown"
        )
        return
    
    elif data == "ask_url":
        PENDING_INPUT[query.message.chat_id] = "url"
        await query.edit_message_text(
            "🔗 *Send me the new URL*\n\n"
            "Just type/paste the signup page link in your next message.\n"
            "Example: `https://example.com/register`\n\n"
            "(Send /start to cancel)",
            parse_mode="Markdown"
        )
        return
    
    elif data == "ask_email":
        PENDING_INPUT[query.message.chat_id] = "email"
        await query.edit_message_text(
            "✉️ *Send me the email*\n\n"
            "Type the email to use for signups in your next message.\n"
            "Example: `you@gmail.com`\n\n"
            "Send the word `off` to go back to random emails.\n"
            "(Send /start to cancel)",
            parse_mode="Markdown"
        )
        return
    
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
    if OVERRIDE_EMAIL:
        username = OVERRIDE_EMAIL.split("@")[0]
        email = OVERRIDE_EMAIL
    else:
        username, email = generate_unique_name()
    password = generate_password()
    
    otp_token = None
    otp_chat = None
    otp_bot = None
    if AUTO_OTP:
        temp_email, otp_token = await create_temp_inbox()
        if not temp_email:
            await update.message.reply_text("❌ Temp inbox (mail.tm) unavailable — try again or toggle Auto-OTP off.")
            return
        email = temp_email
        await update.message.reply_text(f"📧 Using temp inbox: `{email}`\nOTP will be read automatically.", parse_mode="Markdown")
    else:
        otp_chat = update.message.chat_id
        otp_bot = context.bot
    
    success, message, final_email, final_phone = await signup_on_website(
        full_name, phone, email, password,
        otp_chat_id=otp_chat, bot=otp_bot, otp_token=otp_token
    )
    
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
            browser = await launch_chromium(p)
            context = await browser.new_context(**region_options())
            page = await context.new_page()
            await page.goto(WEBSITE_URL, timeout=30000, wait_until="domcontentloaded")
            title = await page.title()
            mapping = await wait_for_form_ready(page, timeout_ms=10000)
            await browser.close()
            r = REGIONS.get(ACTIVE_REGION, REGIONS["nigeria"])
            await update.message.reply_text(
                f"✅ Connected! (v{BOT_VERSION})\n\n"
                f"Website: {WEBSITE_URL}\n"
                f"Title: {title}\n"
                f"Form fields found: {len(mapping.get('fields', {}))}\n"
                f"Region: {r['label']}"
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

async def set_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a fixed email (for OTP sites) or 'off' to go back to random emails."""
    global OVERRIDE_EMAIL
    if update.message.from_user.id != OWNER_ID:
        return
    
    if not context.args:
        await update.message.reply_text(
            f"Current email mode: {OVERRIDE_EMAIL if OVERRIDE_EMAIL else 'random (auto-generated)'}\n\n"
            "Usage:\n"
            "/setemail you@gmail.com  — use this email for signups (OTP goes here)\n"
            "/setemail off  — back to random emails"
        )
        return
    
    arg = context.args[0]
    if arg.lower() in ("off", "none", "random"):
        OVERRIDE_EMAIL = ""
    elif "@" in arg and "." in arg.split("@")[-1]:
        OVERRIDE_EMAIL = arg
    else:
        await update.message.reply_text("❌ Give a valid email or 'off'")
        return
    
    config["override_email"] = OVERRIDE_EMAIL
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    
    if OVERRIDE_EMAIL:
        await update.message.reply_text(
            f"✅ Fixed email set: `{OVERRIDE_EMAIL}`\n\n"
            "OTP codes for signups will be sent to this inbox.\n"
            "Note: the same email can usually only register once — "
            "for multiple OTP accounts, change it with /setemail before each /newuser.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("✅ Back to random auto-generated emails.")

async def otp_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text: pending URL/email input, then OTP codes."""
    if update.message.from_user.id != OWNER_ID:
        return
    
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    
    # 1) Waiting for a URL or email (button flow - no slash needed)
    pending = PENDING_INPUT.get(chat_id)
    if pending:
        PENDING_INPUT.pop(chat_id, None)
        global WEBSITE_URL, OVERRIDE_EMAIL
        if pending == "learn_url":
            url = WEBSITE_URL if text.lower() == "here" else text
            if not url.startswith("http"):
                await update.message.reply_text("❌ Send a URL (or `here`). /start to cancel.", parse_mode="Markdown")
                return
            await update.message.reply_text(
                f"🔍 *Learning form...*\n\nVisiting:\n`{url}`", parse_mode="Markdown")
            try:
                shot, mapping, error = await learn_form(url)
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to open page: {str(e)}")
                return
            if shot and os.path.exists(shot):
                try:
                    with open(shot, "rb") as photo:
                        await update.message.reply_photo(photo, caption=f"📸 What the bot sees:\n{url}")
                except Exception:
                    pass
            if error and not mapping.get("fields"):
                await update.message.reply_text(f"❌ *Learn failed*\n\n{error}", parse_mode="Markdown")
                return
            report = mapping_report(mapping)
            detected = len(mapping.get("fields", {}))
            if detected >= 3:
                data = load_learned()
                data[url] = mapping
                save_learned(data)
                await update.message.reply_text(
                    f"✅ *Form learned!*\n\nDetected {detected}/5 fields:\n{report}\n\nSaved. Signups on this URL will use these selectors.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    f"⚠️ *Weak detection*\n\n{report}\n\nOnly {detected} fields found — not saved.",
                    parse_mode="Markdown"
                )
            return
        if pending == "url":
            if not text.startswith("http"):
                await update.message.reply_text("❌ That doesn't look like a URL. Send /start and tap 🔗 Set URL again.")
                return
            WEBSITE_URL = text
            config["website_url"] = WEBSITE_URL
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
            await update.message.reply_text(
                f"✅ URL saved:\n`{WEBSITE_URL}`\n\n"
                f"Tip: tap 🔍 Learn on /start (or send /learn) to auto-detect the form.",
                parse_mode="Markdown"
            )
        elif pending == "email":
            if text.lower() in ("off", "none", "random"):
                OVERRIDE_EMAIL = ""
            elif "@" in text and "." in text.split("@")[-1]:
                OVERRIDE_EMAIL = text
            else:
                await update.message.reply_text("❌ Send a valid email (or `off`). /start to cancel.", parse_mode="Markdown")
                return
            config["override_email"] = OVERRIDE_EMAIL
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
            if OVERRIDE_EMAIL:
                await update.message.reply_text(f"✅ Email saved: `{OVERRIDE_EMAIL}`", parse_mode="Markdown")
            else:
                await update.message.reply_text("✅ Back to random auto-generated emails.")
        return
    
    # 2) Waiting for an OTP code
    fut = OTP_WAIT.get(chat_id)
    if fut is None or fut.done():
        return
    m = re.search(r'\b([A-Za-z0-9]{4,8})\b', text)
    fut.set_result(m.group(1) if m else text)
    try:
        await update.message.reply_text(f"📩 Code received: `{m.group(1) if m else text}` — submitting...", parse_mode="Markdown")
    except Exception:
        pass

async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    
    learned = load_learned().get(WEBSITE_URL)
    learned_line = "✅ Learned" if learned else "❌ Not learned (using live detection)"
    r = REGIONS.get(ACTIVE_REGION, REGIONS["nigeria"])
    
    await update.message.reply_text(
        f"⚙️ *Settings (v{BOT_VERSION})*\n\n"
        f"Website: {WEBSITE_URL}\n"
        f"Form: {learned_line}\n"
        f"Auto-OTP: {'ON (mail.tm temp email)' if AUTO_OTP else 'OFF (you send codes)'}\n"
        f"Email: {OVERRIDE_EMAIL if OVERRIDE_EMAIL else 'random (auto)'}\n"
        f"Region: {r['label']} ({r['tz']})\n"
        f"Browser: {BROWSER_ENGINE}\n"
        f"Referral: `{REFERRAL_CODE}`\n"
        f"Name pool: {len(NAME_POOL)} names\n"
        f"Accounts created: {len(users)}\n\n"
        f"🌐 Web App:\n`{_app_url()}`",
        parse_mode="Markdown"
    )

async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Visit a signup page, screenshot it, detect the form, save the mapping."""
    if update.message.from_user.id != OWNER_ID:
        return
    
    url = context.args[0] if context.args else WEBSITE_URL
    await update.message.reply_text(
        f"🔍 *Learning form... (v{BOT_VERSION})*\n\nVisiting:\n`{url}`\n\n"
        "Reading labels, inputs and buttons...", parse_mode="Markdown")
    
    try:
        shot, mapping, error = await learn_form(url)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to open page: {str(e)}")
        return
    
    # Send the screenshot so you can see what the bot saw
    if shot and os.path.exists(shot):
        try:
            with open(shot, "rb") as photo:
                await update.message.reply_photo(
                    photo, caption=f"📸 What the bot sees on:\n{url}"
                )
        except Exception:
            pass
    
    if error and not mapping.get("fields"):
        await update.message.reply_text(f"❌ *Learn failed*\n\n{error}", parse_mode="Markdown")
        return
    
    report = mapping_report(mapping)
    detected = len(mapping.get("fields", {}))
    
    if detected >= 3:
        # Save mapping for this URL
        data = load_learned()
        data[url] = mapping
        save_learned(data)
        await update.message.reply_text(
            f"✅ *Form learned! (v{BOT_VERSION})*\n\n"
            f"Detected {detected}/5 fields:\n{report}\n\n"
            f"Saved. Signups on this URL will use these selectors.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"⚠️ *Weak detection (v{BOT_VERSION})*\n\n{report}\n\n"
            f"Only {detected} fields found — not saved. "
            f"Site may need JavaScript or a login step.",
            parse_mode="Markdown"
        )

# ---------- Web dashboard API (Telegram Mini App) ----------
WEB_STATUS = {"done": 0, "created": [], "failed": 0, "total": 0, "errors": [], "active": False}
DASHBOARD_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

def _render_base_url():
    return os.environ.get("RENDER_EXTERNAL_URL", "https://polyglot-shield.onrender.com")

def _app_url():
    return f"{_render_base_url()}/?t={WEB_TOKEN}"

async def web_run_batch(count):
    """Run an account batch for the web dashboard (no Telegram messages)."""
    if JOB.get("active") or WEB_STATUS.get("active"):
        return False
    JOB["active"] = True
    WEB_STATUS.update({"done": 0, "created": [], "failed": 0, "total": count, "errors": [], "active": True})
    JOB["event"] = asyncio.Event()
    sem = asyncio.Semaphore(1)
    
    async def one(i):
        if JOB["event"].is_set():
            return
        async with sem:
            if JOB["event"].is_set():
                return
            full_name = generate_full_name()
            phone = generate_phone()
            username, email = generate_unique_name()
            password = generate_password()
            otp_token = None
            if AUTO_OTP:
                temp_email, otp_token = await create_temp_inbox()
                if not temp_email:
                    WEB_STATUS["failed"] += 1
                    WEB_STATUS["errors"].append(f"{email}: temp inbox unavailable")
                    WEB_STATUS["done"] += 1
                    return
                email = temp_email
            success, message, final_email, final_phone = await signup_on_website(
                full_name, phone, email, password, otp_token=otp_token)
            if success:
                users[final_email] = {
                    "full_name": full_name, "username": username,
                    "phone": final_phone, "password": password, "status": "active"
                }
                WEB_STATUS["created"].append(final_email)
                save_users(users)
            else:
                WEB_STATUS["failed"] += 1
                WEB_STATUS["errors"].append(f"{final_email}: {message}")
            WEB_STATUS["done"] += 1
    
    try:
        await asyncio.gather(*[one(i) for i in range(count)])
    finally:
        JOB["active"] = False
        JOB["event"] = None
        WEB_STATUS["active"] = False
    return True

def _json_response(handler, code, obj):
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

def _authorized(handler):
    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    token = (qs.get("t") or [""])[0]
    return hmac.compare_digest(token, WEB_TOKEN)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        # Health check (UptimeRobot) — no auth
        if path in ("/", "/health", "/healthz"):
            if path == "/" and _authorized(self):
                return self._serve_dashboard()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        
        if path == "/app":
            return self._serve_dashboard()
        
        if not _authorized(self):
            return _json_response(self, 401, {"error": "unauthorized"})
        
        if path == "/api/status":
            return _json_response(self, 200, {
                "version": BOT_VERSION,
                "engine": BROWSER_ENGINE,
                "url": WEBSITE_URL,
                "region": ACTIVE_REGION,
                "auto_otp": AUTO_OTP,
                "total_accounts": len(users),
                "job": WEB_STATUS,
            })
        
        if path == "/api/accounts":
            return _json_response(self, 200, {"accounts": users})
        
        return _json_response(self, 404, {"error": "not found"})
    
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        if not _authorized(self):
            return _json_response(self, 401, {"error": "unauthorized"})
        
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}
        
        global AUTO_OTP, ACTIVE_REGION, WEBSITE_URL
        
        if path == "/api/start":
            count = int(data.get("count", 1))
            count = max(1, min(count, 100))
            if BOT_LOOP is None:
                return _json_response(self, 503, {"message": "Bot loop not ready"})
            if JOB.get("active") or WEB_STATUS.get("active"):
                return _json_response(self, 409, {"message": "⚠️ A batch is already running"})
            # Fire and forget — batch runs in background on the bot loop
            asyncio.run_coroutine_threadsafe(web_run_batch(count), BOT_LOOP)
            return _json_response(self, 200, {"message": f"🟢 Started creating {count} account(s)", "started": True})
        
        if path == "/api/stop":
            if JOB.get("event"):
                JOB["event"].set()
            return _json_response(self, 200, {"message": "🛑 Stop requested"})
        
        if path == "/api/settings":
            if "auto_otp" in data:
                AUTO_OTP = bool(data["auto_otp"])
                config["auto_otp"] = AUTO_OTP
            if "region" in data and data["region"] in REGIONS:
                ACTIVE_REGION = data["region"]
                config["region"] = ACTIVE_REGION
            if "url" in data and str(data["url"]).startswith("http"):
                WEBSITE_URL = str(data["url"]).strip()
                config["website_url"] = WEBSITE_URL
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
            return _json_response(self, 200, {"message": "💾 Saved", "auto_otp": AUTO_OTP, "region": ACTIVE_REGION, "url": WEBSITE_URL})
        
        return _json_response(self, 404, {"error": "not found"})
    
    def _serve_dashboard(self):
        try:
            with open(DASHBOARD_HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_response(500)
            self.end_headers()
    
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
    global BOT_LOOP
    BOT_LOOP = asyncio.get_running_loop()
    from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators
    
    new_commands = [
        BotCommand("start", "Show bot info"),
        BotCommand("learn", "Teach bot a signup page (auto-detect form)"),
        BotCommand("newuser", "Auto create new account (interactive OTP)"),
        BotCommand("list", "View all created accounts"),
        BotCommand("setemail", "Fixed email for OTP sites"),
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
    global BOT_LOOP
    # Ensure Chromium is installed before starting
    ensure_chromium()
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("learn", learn_cmd))
    app.add_handler(CommandHandler("newuser", new_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("test", test_connection))
    app.add_handler(CommandHandler("seturl", set_url))
    app.add_handler(CommandHandler("setemail", set_email))
    app.add_handler(CommandHandler("config", show_config))
    # Catches OTP codes the owner sends while a signup waits (after commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, otp_message))

    threading.Thread(target=run_health_server, daemon=True).start()

    print(f"🤖 Signup Bot v{BOT_VERSION} is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
