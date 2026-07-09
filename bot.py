import os, re, io, json, base64, random, asyncio, logging, hashlib, traceback
import requests
from datetime import datetime, timedelta
from supabase import create_client, Client
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, KeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes,
                          ConversationHandler)
from aiohttp import web
from pyrogram import Client as PyroClient, filters as pyfilters
from pyrogram.errors import SessionPasswordNeeded
import pytz

# Optional heavy imports with graceful fallback
try:
    from PIL import Image; HAS_PILLOW = True
except ImportError: HAS_PILLOW = False
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors; HAS_REPORTLAB = True
except ImportError: HAS_REPORTLAB = False
try:
    from bs4 import BeautifulSoup; HAS_BS4 = True
except ImportError: HAS_BS4 = False
try:
    import feedparser; HAS_FEEDPARSER = True
except ImportError: HAS_FEEDPARSER = False
try:
    import aiohttp; HAS_AIOHTTP = True
except ImportError: HAS_AIOHTTP = False
try:
    from fake_useragent import UserAgent; UA = UserAgent()
except Exception: UA = None

# ==============================================================================
# 1. ENTERPRISE CONFIG & ENVIRONMENT VARIABLES
# ==============================================================================
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN          = os.environ.get("BOT_TOKEN")
SUPABASE_URL       = os.environ.get("SUPABASE_URL")
SUPABASE_KEY       = os.environ.get("SUPABASE_KEY")
ADMIN_GROUP_ID     = int(os.environ.get("ADMIN_GROUP_ID", "0"))
ADMIN_USER_ID      = int(os.environ.get("ADMIN_USER_ID", "0"))
PUBLIC_CHANNEL_ID  = os.environ.get("PUBLIC_CHANNEL_ID",
                                    os.environ.get("CHANNEL_ID", ""))
API_ID             = int(os.environ.get("API_ID", "1234567"))
API_HASH           = os.environ.get("API_HASH", "placeholder")
AICREDITS_API_KEY  = os.environ.get("AICREDITS_API_KEY")
AICREDITS_BASE_URL = os.environ.get("AICREDITS_BASE_URL",
                                    "https://api.aicredits.in/v1")
YOUTUBE_API_KEY    = os.environ.get("YOUTUBE_API_KEY", "")

IST = pytz.timezone('Asia/Kolkata')

# Default pricing (used for admin-created matches when args not given)
DEFAULT_ENTRY_FEE  = 50.0
DEFAULT_PRIZE      = 300.0
MATCH_LIVE_MINS    = 15
REFUND_WINDOW_MINS = 8
MAX_PLAYERS        = 10
ENTRY_OPTIONS      = [30, 40, 50]   # Random pricing tiers

# Supabase init
if SUPABASE_URL and SUPABASE_KEY:
    try: db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e: logger.error(f"Supabase init error: {e}"); db = None
else: db = None

join_locks      = {}
pyro_clients    = {}
user_throttle   = {}
user_cache      = {}
pending_workers = {}
dead_sources    = set()       # Sources that returned 404/403 repeatedly
scraper_stats   = {}          # {source_name: {"last_ok": datetime, "failures": int}}

# Conversation states
(WAIT_IGN, WAIT_ADD_AMT, WAIT_PAY_PROOF, WAIT_WITHDRAW_QR, WAIT_WIN_PROOF,
 WAIT_SUPPORT_CHAT, WAIT_WORKER_PHONE, WAIT_WORKER_OTP, WAIT_WORKER_PASS,
 WAIT_REPORT_ACCUSED, WAIT_REPORT_DESC, WAIT_REPORT_PROOF) = range(12)

# ==============================================================================
# 2. UNIVERSAL SAFE MARKDOWN SHIELD (Enhanced)
# ==============================================================================
_MD_ESCAPE = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')

def safe_md(text):
    """Escape every character Telegram Markdown-v1 treats as special.
    Safe for user-provided strings like usernames, passwords, room IDs,
    UTR numbers, amounts, etc."""
    if text is None: return ""
    return _MD_ESCAPE.sub(r'\\\1', str(text))

def compress_image_to_b64(byte_array, max_width=800, quality=80):
    if not HAS_PILLOW:
        return base64.b64encode(byte_array).decode('utf-8')
    try:
        img = Image.open(io.BytesIO(byte_array))
        if img.mode != 'RGB': img = img.convert('RGB')
        if img.width > max_width:
            ratio = max_width / float(img.width)
            height = int(float(img.height) * ratio)
            img = img.resize((max_width, height), Image.Resampling.LANCZOS)
        out = io.BytesIO(); img.save(out, format='JPEG', quality=quality)
        return base64.b64encode(out.getvalue()).decode('utf-8')
    except Exception as e:
        logger.warning(f"Image compression fallback: {e}")
        return base64.b64encode(byte_array).decode('utf-8')

# ==============================================================================
# 3. UTR HELPERS (Indestructible 3-stage saving + same-day prefix check)
# ==============================================================================
def save_utr_safely(utr, user_id=None, amount=None):
    if not db or not utr or utr.startswith("MANUAL"): return
    ts = datetime.now(IST).isoformat()
    for payload in (
        {"utr": utr, "user_id": user_id, "amount": amount, "created_at": ts},
        {"utr": utr, "user_id": user_id},
        {"utr": utr},
    ):
        try:
            db.table("used_utrs").insert(payload).execute()
            logger.info(f"Saved UTR {utr} with {len(payload)} cols.")
            return
        except Exception as e:
            logger.warning(f"UTR insert {len(payload)} cols failed: {e}")
    logger.error(f"CRITICAL: Could not save UTR {utr} at all.")

def is_utr_used(utr):
    if not db or not utr or utr.startswith("MANUAL"): return False
    try:
        res = db.table("used_utrs").select("*").eq("utr", utr).execute()
        return bool(res.data)
    except Exception as e: logger.error(f"UTR check err: {e}"); return False

def get_utr_prefixes():
    now = datetime.now(IST); yest = now - timedelta(days=1)
    return [str(now.year)[-1] + now.strftime("%j"),
            str(yest.year)[-1] + yest.strftime("%j")]

# ==============================================================================
# 4. 19-KEY ROUND-ROBIN AI SUPERPOOL ENGINE
# ==============================================================================
class AIPoolManager:
    def __init__(self):
        self.keys = []; self._load_keys(); self.current_idx = 0

    def _load_keys(self):
        pool_str = os.environ.get("AI_POOL_KEYS", "")
        if pool_str:
            for k in pool_str.split(","):
                k = k.strip()
                if k and k not in self.keys: self.keys.append(k)
        for name, val in os.environ.items():
            if any(name.startswith(p) for p in
                   ["GROQ_", "GEMINI_", "CLOUDFLARE_", "CF_",
                    "MISTRAL_", "SAMBANOVA_", "CEREBRAS_", "OPENROUTER_"]):
                for sub_k in val.split(","):
                    sub_k = sub_k.strip()
                    if sub_k and sub_k not in self.keys: self.keys.append(sub_k)
        logger.info(f"AI SuperPool: {len(self.keys)} keys loaded.")

    def get_next_key(self):
        if not self.keys: return None
        k = self.keys[self.current_idx]
        self.current_idx = (self.current_idx + 1) % len(self.keys)
        return k

ai_pool = AIPoolManager()

async def call_ai_unified(prompt, image_b64=None, system_context=""):
    # Tier-1: aicredits.in
    if AICREDITS_API_KEY:
        try:
            url = f"{AICREDITS_BASE_URL.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {AICREDITS_API_KEY}",
                       "Content-Type": "application/json"}
            if image_b64:
                messages = [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]}]
            else:
                messages = [
                    {"role": "system",
                     "content": system_context or "You are an AI assistant."},
                    {"role": "user", "content": prompt}]
            resp = await asyncio.to_thread(
                requests.post, url, headers=headers,
                json={"model": "gpt-4o-mini", "messages": messages}, timeout=20)
            if resp.status_code == 200:
                return resp.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.warning(f"AICREDITS fail: {e}. Pool fallback...")

    # Tier-2: 19-key pool
    for _ in range(max(1, len(ai_pool.keys))):
        key = ai_pool.get_next_key()
        if not key: break
        try:
            # Gemini
            if key.startswith("AIzaSy"):
                url = (f"https://generativelanguage.googleapis.com/v1beta/"
                       f"models/gemini-1.5-flash:generateContent?key={key}")
                parts = [{"text": f"{system_context}\n\n{prompt}"}]
                if image_b64:
                    parts.append({"inline_data": {
                        "mime_type": "image/jpeg", "data": image_b64}})
                resp = await asyncio.to_thread(requests.post, url,
                    json={"contents": [{"parts": parts}]}, timeout=20)
                if resp.status_code == 200:
                    return resp.json()['candidates'][0]['content']['parts'][0]['text']
                continue
            if image_b64: continue  # text-only APIs below

            # Cloudflare
            if ":" in key and len(key.split(":")[0]) == 32:
                cf_acc, cf_tok = key.split(":", 1)
                url = (f"https://api.cloudflare.com/client/v4/accounts/"
                       f"{cf_acc}/ai/run/@cf/meta/llama-3.1-8b-instruct")
                headers = {"Authorization": f"Bearer {cf_tok}",
                           "Content-Type": "application/json"}
                payload = {"messages": [
                    {"role": "system",
                     "content": system_context or "You are an AI assistant."},
                    {"role": "user", "content": prompt}]}
                resp = await asyncio.to_thread(requests.post, url,
                    headers=headers, json=payload, timeout=8)
                if resp.status_code == 200:
                    return resp.json()['result']['response']
                continue

            # Groq / Cerebras / Mistral / OpenRouter
            if key.startswith("gsk_"):
                ep = "https://api.groq.com/openai/v1/chat/completions"
                mdl = "llama-3.1-8b-instant"
            elif key.startswith("csk-"):
                ep = "https://api.cerebras.ai/v1/chat/completions"
                mdl = "llama3.1-8b"
            elif len(key) == 32:
                ep = "https://api.mistral.ai/v1/chat/completions"
                mdl = "mistral-small-latest"
            else:
                ep = "https://openrouter.ai/api/v1/chat/completions"
                mdl = "meta-llama/llama-3.1-8b-instruct:free"
            headers = {"Authorization": f"Bearer {key}",
                       "Content-Type": "application/json"}
            messages = [
                {"role": "system",
                 "content": system_context or "You are an AI assistant."},
                {"role": "user", "content": prompt}]
            resp = await asyncio.to_thread(requests.post, ep,
                headers=headers, json={"model": mdl, "messages": messages},
                timeout=8)
            if resp.status_code == 200:
                return resp.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.warning(f"Pool key fail: {e}"); continue
    return "AI_FAILED"

# ==============================================================================
# 5. CORE DATABASE MANAGERS & RATE LIMIT SHIELD
# ==============================================================================
def is_throttled(user_id):
    now = datetime.now()
    last = user_throttle.get(user_id)
    if last and (now - last).total_seconds() < 1.0: return True
    user_throttle[user_id] = now
    return False

def _is_admin(uid):
    return (ADMIN_USER_ID != 0 and uid == ADMIN_USER_ID)

def get_user(user_id):
    now = datetime.now()
    if user_id in user_cache:
        data, ts = user_cache[user_id]
        if (now - ts).total_seconds() < 10.0: return data
    if not db:
        dummy = {"user_id": user_id, "deposit_balance": 100.0,
                 "winning_balance": 0.0, "bonus_balance": 10.0,
                 "locked_balance": 0.0, "ff_ign": "TEST_USER",
                 "last_login": "", "is_18_plus": True,
                 "is_restricted": False, "is_banned": False,
                 "referrer_id": None}
        user_cache[user_id] = (dummy, now); return dummy
    try:
        res = db.table("users").select("*").eq("user_id", user_id).execute()
        if not res.data:
            new = {"user_id": user_id, "deposit_balance": 0.0,
                   "winning_balance": 0.0, "bonus_balance": 0.0,
                   "locked_balance": 0.0, "ff_ign": "", "last_login": "",
                   "is_18_plus": False, "is_restricted": False,
                   "is_banned": False, "referrer_id": None}
            try: db.table("users").insert(new).execute()
            except Exception: pass
            user_cache[user_id] = (new, now); return new
        user_cache[user_id] = (res.data[0], now)
        return res.data[0]
    except Exception as e:
        logger.error(f"get_user err: {e}")
        return {"user_id": user_id, "deposit_balance": 0.0,
                "winning_balance": 0.0, "bonus_balance": 0.0,
                "locked_balance": 0.0, "ff_ign": "", "last_login": "",
                "is_18_plus": False, "is_restricted": False,
                "is_banned": False}

def invalidate_user_cache(uid): user_cache.pop(uid, None)

def deduct_balance(user_id, amount):
    if not db: return True
    invalidate_user_cache(user_id)
    u = get_user(user_id)
    rem = amount
    b, d, w = u['bonus_balance'], u['deposit_balance'], u['winning_balance']
    db_ = min(b, rem); rem -= db_; b -= db_
    dd_ = min(d, rem); rem -= dd_; d -= dd_
    dw_ = min(w, rem); rem -= dw_; w -= dw_
    if rem > 0: return False
    try:
        db.table("users").update(
            {"bonus_balance": b, "deposit_balance": d,
             "winning_balance": w}).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id); return True
    except Exception as e: logger.error(f"deduct err: {e}"); return False

def credit_balance(user_id, field, amount):
    if not db: return
    invalidate_user_cache(user_id)
    u = get_user(user_id)
    try:
        db.table("users").update(
            {field: u.get(field, 0.0) + amount}
        ).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id)
    except Exception as e: logger.error(f"credit err: {e}")

def random_price():
    fee = random.choice(ENTRY_OPTIONS)
    return fee, fee * 10

def get_match_fee(match):
    """Get entry fee for a match (variable pricing)."""
    fee = match.get('entry_fee') or match.get('fee')
    if fee: return float(fee)
    return DEFAULT_ENTRY_FEE

def get_match_prize(match):
    """Get prize money for a match."""
    prize = match.get('prize_money') or match.get('prize')
    if prize: return float(prize)
    return get_match_fee(match) * 10

# ==============================================================================
# 6. BAN SYSTEM
# ==============================================================================
def is_user_banned(user_id):
    try:
        u = get_user(user_id)
        return bool(u.get('is_banned', False))
    except: return False

def ban_user(user_id, reason=""):
    if not db: return
    try:
        db.table("users").update(
            {"is_banned": True}
        ).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id)
        logger.info(f"User {user_id} BANNED. Reason: {reason}")
    except Exception as e: logger.error(f"ban err: {e}")

def unban_user(user_id):
    if not db: return
    try:
        db.table("users").update(
            {"is_banned": False}
        ).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id)
        logger.info(f"User {user_id} UNBANNED.")
    except Exception as e: logger.error(f"unban err: {e}")

# ==============================================================================
# 7. UX MENUS & KEYBOARDS
# ==============================================================================
def get_main_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎮 PLAY FREE FIRE"),
         KeyboardButton("🎯 MY MATCHES")],
        [KeyboardButton("💰 ADD FUNDS"),
         KeyboardButton("💸 WITHDRAW CASH")],
        [KeyboardButton("🎁 DAILY REWARD"),
         KeyboardButton("🤝 HELP / SUPPORT")]], resize_keyboard=True)

def get_cancel_kbd():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Cancel & Go Back")]], resize_keyboard=True)

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.callback_query:
            await update.callback_query.message.delete()
        else:
            await update.message.reply_text(
                "🚫 Action Cancelled.",
                reply_markup=get_main_menu())
    except: pass
    return ConversationHandler.END

# Motivational quotes for winners
WINNER_QUOTES = [
    "Asli champion wahi hai jo girke uthe aur dobara jeete! 🔥",
    "Ek BOOYAH se kahani nahi banti — aur matches khelein, aur jeetein! 💪",
    "Aaj aap jeete, kal aur bhi bada prize aapka intezaar kar raha hai! 🏆",
    "Har match ek nayi jung hai — taiyar raho aur duniya hila do! ⚡",
    "Winner banna aasan nahi, par aapne kar dikhaya! Aage bhi aise hi khelo! 🎮",
    "Aap is match ke winner hain! Aur bhi matches hain — try karo aur jeeto! 👑",
]

# ==============================================================================
# 8. /start — ONBOARDING + BAN CHECK + REFERRAL
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    # BAN CHECK — permanent, survives /start and delete+restart
    if is_user_banned(user_id):
        await update.message.reply_text(
            "🚫 **PERMANENTLY BANNED**\n\n"
            "Aapka account permanently ban ho chuka hai cheating/"
            "violation ke kaaran.\n"
            "Contact support: @Tughh\\_456",
            parse_mode='Markdown')
        return ConversationHandler.END

    args = context.args
    referrer_id = None
    if args:
        param = args[0]
        if param.startswith("ref_"):
            try:
                rid = int(param.split("_")[1])
                if rid != user_id: referrer_id = rid
            except: pass
        elif param.startswith("match_"):
            mid = param.replace("match_", "")
            kbd = [[InlineKeyboardButton(
                f"🔒 JOIN #{safe_md(mid)}",
                callback_data=f"confjoin_{mid}")]]
            await update.message.reply_text(
                f"🔥 **INVITE TO MATCH #{safe_md(mid)}**\n"
                f"Aapke dost ne aapko bulaya hai!",
                reply_markup=InlineKeyboardMarkup(kbd),
                parse_mode='Markdown')
            return ConversationHandler.END

    user = get_user(user_id)
    if db and referrer_id and not user.get('referrer_id'):
        try:
            db.table("users").update(
                {"referrer_id": referrer_id}
            ).eq("user_id", user_id).execute()
            invalidate_user_cache(user_id)
        except: pass

    if user.get('is_restricted'):
        await update.message.reply_text(
            "🚨 Account Suspended.", parse_mode='Markdown')
        return ConversationHandler.END

    if not user.get('is_18_plus'):
        msg = ("⚖️ **LEGAL & AGE VERIFICATION** ⚖️\n\n"
               "Khelne ke liye:\n"
               "1. Umar **18+** honi chahiye\n"
               "2. Restricted states se nahi hone chahiye\n\n"
               "**Kya aap 18+ hain?**")
        kbd = [[InlineKeyboardButton(
            "✅ YES, I AM 18+", callback_data="legal_yes")],
               [InlineKeyboardButton(
            "❌ NO, UNDER 18", callback_data="legal_no")]]
        await update.message.reply_text(
            msg, reply_markup=InlineKeyboardMarkup(kbd),
            parse_mode='Markdown')
        return ConversationHandler.END

    ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    await update.message.reply_text(
        f"🔥 **Welcome back!**\n\n"
        f"🤝 Refer & Earn ₹10:\n👉 `{safe_md(ref_link)}`",
        reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

async def legal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "legal_yes":
        if db:
            db.table("users").update(
                {"is_18_plus": True}
            ).eq("user_id", query.from_user.id).execute()
            invalidate_user_cache(query.from_user.id)
        await query.message.delete()
        await query.message.reply_text(
            "✅ **Verification Done! Welcome!** 🔥",
            reply_markup=get_main_menu(), parse_mode='Markdown')
    else:
        await query.message.edit_text(
            "❌ You must be 18+ to play.")

# ==============================================================================
# 9. MAIN MENU HANDLER
# ==============================================================================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return ConversationHandler.END
    uid = update.message.from_user.id
    if is_throttled(uid): return ConversationHandler.END
    if is_user_banned(uid):
        await update.message.reply_text("🚫 You are banned.")
        return ConversationHandler.END

    text = update.message.text.upper()
    if "CANCEL" in text: return await cancel_action(update, context)

    user = get_user(uid)
    if user.get('is_restricted'):
        await update.message.reply_text("🚨 Account suspended.")
        return ConversationHandler.END

    # ─── PLAY FREE FIRE ───
    if "PLAY" in text:
        if not user.get('ff_ign', '').strip():
            await update.message.reply_text(
                "⚠️ Pehle apna FF Nickname type karein:",
                reply_markup=get_cancel_kbd())
            return WAIT_IGN

        if db:
            exp = (datetime.now(IST) -
                   timedelta(minutes=MATCH_LIVE_MINS)).isoformat()
            # Cleanup expired scraped matches (scraped=1, older than 10 min)
            try:
                db.table("matches").delete().lt(
                    "created_at", exp).eq("scraped", True).execute()
            except: pass
            matches = db.table("matches").select("*").gt(
                "tickets_left", 0).execute().data
        else:
            matches = [{"match_id": "FF8899", "room_id": "123456",
                         "room_pass": "pass123", "tickets_left": 7,
                         "entry_fee": 50, "prize_money": 500}]

        if not matches:
            await update.message.reply_text(
                "🟡 Koi match abhi Live nahi! "
                "5 min mein dobara check karein ⏳")
            return ConversationHandler.END

        msg = "🔴 **LIVE BATTLE BOARD** 🔴\n\n"
        kbd = []
        for m in matches:
            fee = get_match_fee(m)
            prize = get_match_prize(m)
            seats_taken = MAX_PLAYERS - m['tickets_left']
            bars = int(seats_taken / MAX_PLAYERS * 10)
            progress = "█" * bars + "░" * (10 - bars)
            is_scraped = m.get('scraped', False)
            status = ("🟢 READY" if m['room_id'] != "TBD"
                      else "⏰ SCHEDULED")
            if is_scraped: status += " 🌐"

            msg += (f"🔥 **Match #{safe_md(m['match_id'])}** | "
                    f"{status}\n"
                    f"🎟 `[{progress}] {seats_taken}/{MAX_PLAYERS}`\n"
                    f"💰 Entry: ₹{fee} | Prize: ₹{prize}\n\n")

            share_url = (f"https://t.me/{context.bot.username}"
                         f"?start=match_{m['match_id']}")
            kbd.append([
                InlineKeyboardButton(
                    f"🔒 JOIN #{m['match_id']} (₹{fee})",
                    callback_data=f"confjoin_{m['match_id']}"),
                InlineKeyboardButton(
                    "📢 INVITE",
                    url=f"https://t.me/share/url?url={share_url}"
                        f"&text=Ajao FF Tournament!")])

        await update.message.reply_text(
            msg, reply_markup=InlineKeyboardMarkup(kbd),
            parse_mode='Markdown')
        return ConversationHandler.END

    # ─── ADD FUNDS ───
    elif "ADD FUNDS" in text:
        await update.message.reply_text(
            "💸 Kitne Rupaye add karne hain? (Min: ₹30)",
            reply_markup=get_cancel_kbd())
        return WAIT_ADD_AMT

    # ─── WITHDRAW ───
    elif "WITHDRAW" in text:
        tot = round(user['deposit_balance'] + user['winning_balance']
                     + user['bonus_balance'], 2)
        msg = (f"💰 Total: ₹{tot}\n"
               f"🟢 Winnings: ₹{user['winning_balance']}\n"
               f"🔵 Deposit: ₹{user['deposit_balance']}\n"
               f"🎁 Bonus: ₹{user['bonus_balance']}\n\n"
               f"(Min withdraw: ₹200 Winnings)")
        if user['winning_balance'] < 200:
            await update.message.reply_text(
                msg + "\n❌ Minimum ₹200 Winnings needed.")
            return ConversationHandler.END
        await update.message.reply_text(
            msg + "\n📸 UPI QR Code bhejein:",
            reply_markup=get_cancel_kbd())
        return WAIT_WITHDRAW_QR

    # ─── DAILY REWARD ───
    elif "DAILY REWARD" in text:
        invalidate_user_cache(uid)
        user = get_user(uid)
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if user.get('last_login') == today:
            await update.message.reply_text(
                "❌ Aaj ka reward already claimed. Kal aaiye!")
        else:
            await update.message.reply_text(
                "🎰 BOOYAH JACKPOT! Lever ghum raha hai...")
            dice_msg = await context.bot.send_dice(
                chat_id=uid, emoji='🎰')
            await asyncio.sleep(3.5)
            val = dice_msg.dice.value
            if val == 64: reward = 10.0
            elif val in (1, 22, 43): reward = 5.0
            elif val % 5 == 0: reward = 3.0
            else: reward = random.choice([1.0, 2.0])
            credit_balance(uid, 'bonus_balance', reward)
            if db:
                db.table("users").update(
                    {"last_login": today}
                ).eq("user_id", uid).execute()
                invalidate_user_cache(uid)
            await update.message.reply_text(
                f"🎉 **₹{reward} Bonus Cash mila!** 🎁")
        return ConversationHandler.END

    # ─── MY MATCHES ───
    elif "MATCHES" in text:
        try:
            if not db:
                await update.message.reply_text("Koi match nahi mila.")
                return ConversationHandler.END
            ums = db.table("user_matches").select("*").eq(
                "user_id", uid).execute().data
            if not ums:
                await update.message.reply_text(
                    "Koi match join nahi kiya abhi tak.")
                return ConversationHandler.END

            msg = "🎯 **YOUR MATCHES** 🎯\n\n"
            kbd = []
            now_ist = datetime.now(IST)
            for um in ums[-5:]:
                mid = um['match_id']
                mdata = db.table("matches").select("*").eq(
                    "match_id", mid).execute().data
                if not mdata: continue
                m = mdata[0]
                fee = get_match_fee(m)
                prize = get_match_prize(m)

                msg += (f"🔥 **#{safe_md(mid)}** | "
                        f"Entry ₹{fee} | Prize ₹{prize}\n"
                        f"Status: **{um['status']}**\n")

                if um['status'] == 'JOINED':
                    if m['room_id'] != "TBD":
                        msg += (f"🔑 `{safe_md(m['room_id'])}` | "
                                f"🔐 `{safe_md(m['room_pass'])}`\n")
                    else:
                        msg += "⏰ Room jaldi aayega\n"

                    joined_at = um.get('joined_at', '')
                    if joined_at:
                        try:
                            jt = datetime.fromisoformat(joined_at)
                            if jt.tzinfo is None: jt = IST.localize(jt)
                            mins = (now_ist - jt).total_seconds() / 60
                        except: mins = 999
                    else:
                        mins = 999

                    if mins < REFUND_WINDOW_MINS:
                        kbd.append([InlineKeyboardButton(
                            f"⚠️ REFUND #{mid}",
                            callback_data=f"askref_{mid}")])
                    elif mins < 60:
                        kbd.append([InlineKeyboardButton(
                            f"🏆 I WON #{mid}",
                            callback_data=f"up_proof_{mid}")])
                        kbd.append([InlineKeyboardButton(
                            f"🛡️ REPORT HACKER #{mid}",
                            callback_data=f"repthack_{mid}")])
                    else:
                        kbd.append([InlineKeyboardButton(
                            f"🏆 I WON #{mid}",
                            callback_data=f"up_proof_{mid}")])
                elif um['status'] == 'PENDING':
                    msg += "⏳ Verification pending...\n"
                elif um['status'] == 'WON':
                    msg += "🏆 Winner!\n"
                msg += "\n"

            # Balance summary (text only, no chart)
            msg += (f"💰 **Balance:**\n"
                    f"🔵 Deposit: ₹{user.get('deposit_balance', 0)}\n"
                    f"🟢 Winnings: ₹{user.get('winning_balance', 0)}\n"
                    f"🎁 Bonus: ₹{user.get('bonus_balance', 0)}")

            await update.message.reply_text(
                msg, parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(kbd) if kbd else None)
        except Exception as e:
            logger.error(f"MY MATCHES error: {e}")
            await update.message.reply_text(
                f"⚠️ MY MATCHES load nahi ho paya. Dobara try karein.")
        return ConversationHandler.END

    # ─── HELP / SUPPORT ───
    elif "HELP" in text or "SUPPORT" in text:
        safe_name = safe_md(user.get('ff_ign') or 'Unconfigured')
        await update.message.reply_text(
            f"🟢 **AI SUPPORT ONLINE**\n"
            f"👤 Profile: `{safe_name}`\n\n"
            f"Apni problem niche type karein:",
            reply_markup=get_cancel_kbd(), parse_mode='Markdown')
        return WAIT_SUPPORT_CHAT

    return ConversationHandler.END

# ==============================================================================
# 10. AI SUPPORT CHATBOT
# ==============================================================================
async def handle_support_chat(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text and "CANCEL" in text.upper():
        return await cancel_action(update, context)
    uid = update.message.from_user.id
    await context.bot.send_chat_action(chat_id=uid, action='typing')
    user = get_user(uid)
    sys_p = (f"You are Free Fire Tournament AI Support. "
             f"Player: ID={uid}, IGN={user.get('ff_ign','')}, "
             f"Deposit=₹{user.get('deposit_balance',0)}, "
             f"Winnings=₹{user.get('winning_balance',0)}. "
             f"Answer in Hinglish. Avoid markdown special chars.")
    ai_reply = await call_ai_unified(text, system_context=sys_p)
    try:
        await update.message.reply_text(
            ai_reply, reply_markup=get_cancel_kbd(),
            parse_mode='Markdown')
    except:
        await update.message.reply_text(
            ai_reply, reply_markup=get_cancel_kbd())
    return WAIT_SUPPORT_CHAT

# ==============================================================================
# 11. MATCH JOIN & REFUND
# ==============================================================================
async def conf_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mid = query.data.split("_")[1]
    # Get match-specific fee
    fee = DEFAULT_ENTRY_FEE
    if db:
        mdata = db.table("matches").select("*").eq(
            "match_id", mid).execute().data
        if mdata: fee = get_match_fee(mdata[0])
    kbd = [[InlineKeyboardButton(
        f"✅ YES, JOIN (₹{fee})",
        callback_data=f"dojoin_{mid}")],
           [InlineKeyboardButton("🔙 CANCEL",
        callback_data="delete_msg")]]
    await query.message.reply_text(
        f"Match #{safe_md(mid)} join — ₹{fee} pay karna hai?",
        reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mid = query.data.split("_")[1]
    uid = query.from_user.id
    if db:
        exists = db.table("user_matches").select("*").eq(
            "user_id", uid).eq("match_id", mid).execute().data
        if exists:
            await query.message.edit_text("Already joined.")
            return
        mdata = db.table("matches").select("*").eq(
            "match_id", mid).execute().data
        if not mdata:
            await query.message.edit_text("Match not found.")
            return
        m = mdata[0]
        if m['tickets_left'] <= 0:
            await query.message.edit_text("Match full!")
            return
    else:
        m = {"room_id": "123456", "room_pass": "pass", "tickets_left": 5}

    fee = get_match_fee(m)
    lock_key = f"join_{mid}"
    if lock_key not in join_locks: join_locks[lock_key] = asyncio.Lock()
    async with join_locks[lock_key]:
        if deduct_balance(uid, fee):
            if db:
                db.table("matches").update(
                    {"tickets_left": m['tickets_left'] - 1}
                ).eq("match_id", mid).execute()
                db.table("user_matches").insert({
                    "user_id": uid, "match_id": mid,
                    "status": "JOINED",
                    "joined_at": datetime.now(IST).isoformat()
                }).execute()
            room_info = ("⏰ Room jaldi aayega"
                         if m['room_id'] == "TBD"
                         else f"🔑 `{safe_md(m['room_id'])}` | "
                              f"🔐 `{safe_md(m['room_pass'])}`")
            await query.message.edit_text(
                f"🔥 **JOINED!** 🎮\n{room_info}\n"
                f"Refund {REFUND_WINDOW_MINS} min tak available.",
                parse_mode='Markdown')
        else:
            await query.message.edit_text(
                "❌ Insufficient Funds! Recharge karo.")

async def ask_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mid = query.data.split("_")[1]
    fee = DEFAULT_ENTRY_FEE
    if db:
        mdata = db.table("matches").select("*").eq(
            "match_id", mid).execute().data
        if mdata: fee = get_match_fee(mdata[0])
    kbd = [[InlineKeyboardButton(
        f"✅ YES, REFUND ₹{fee}",
        callback_data=f"doref_{mid}")],
           [InlineKeyboardButton("🔙 NO",
        callback_data="delete_msg")]]
    await query.message.edit_text(
        f"Refund ₹{fee} from Match #{safe_md(mid)}?",
        reply_markup=InlineKeyboardMarkup(kbd))

async def do_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mid = query.data.split("_")[1]
    uid = query.from_user.id
    if not db: return
    um = db.table("user_matches").select("*").eq(
        "user_id", uid).eq("match_id", mid).execute().data
    if not um: return
    if um[0]['status'] == 'REFUNDED':
        await query.message.edit_text("Already refunded.")
        return
    mdata = db.table("matches").select("*").eq(
        "match_id", mid).execute().data
    fee = get_match_fee(mdata[0]) if mdata else DEFAULT_ENTRY_FEE
    credit_balance(uid, 'deposit_balance', fee)
    db.table("user_matches").update(
        {"status": "REFUNDED"}).eq("id", um[0]['id']).execute()
    if mdata:
        db.table("matches").update(
            {"tickets_left": min(MAX_PLAYERS,
                                 mdata[0]['tickets_left'] + 1)}
        ).eq("match_id", mid).execute()
    await query.message.edit_text(
        f"✅ Refund ₹{fee} done!")

async def cancel_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: await update.callback_query.message.delete()
    except: pass

# ==============================================================================
# 12. IGN, DEPOSIT, WITHDRAW FLOWS
# ==============================================================================
async def save_ign_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return WAIT_IGN
    if "CANCEL" in update.message.text.upper():
        return await cancel_action(update, context)
    ign = update.message.text.strip()
    uid = update.message.from_user.id
    if db:
        db.table("users").update({"ff_ign": ign}).eq(
            "user_id", uid).execute()
        invalidate_user_cache(uid)
    await update.message.reply_text(
        f"✅ IGN `{safe_md(ign)}` saved!",
        reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return WAIT_ADD_AMT
    if "CANCEL" in update.message.text.upper():
        return await cancel_action(update, context)
    try:
        amt = float(update.message.text)
        if amt < 30: raise ValueError
    except:
        await update.message.reply_text("❌ Min ₹30. Sahi number likhein.")
        return WAIT_ADD_AMT
    context.user_data['dep_amt'] = amt
    upi_id = "dipanshu153@fam"
    qr_url = (f"https://api.qrserver.com/v1/create-qr-code/"
              f"?size=300x300&data=upi://pay?pa={upi_id}"
              f"%26pn=ArenaEsports%26am={amt}%26cu=INR")
    await update.message.reply_photo(
        photo=qr_url,
        caption=f"💳 PAY ₹{amt} to `{upi_id}`\nScreenshot bhejein:",
        parse_mode='Markdown', reply_markup=get_cancel_kbd())
    return WAIT_PAY_PROOF

async def animate_progress(msg, base_text):
    for s in ["40% [████░░░░░░]", "60% [██████░░░░]",
              "80% [████████░░]"]:
        await asyncio.sleep(3.2)
        try: await msg.edit_text(f"{base_text} {s}")
        except: break

async def process_payment_proof(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    if (update.message and update.message.text
            and "CANCEL" in update.message.text.upper()):
        return await cancel_action(update, context)
    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "❌ Sirf screenshot bhejein.",
            reply_markup=get_cancel_kbd())
        return WAIT_PAY_PROOF

    uid = update.message.from_user.id
    claimed = context.user_data.get('dep_amt', 50.0)
    msg = await update.message.reply_text(
        "⏳ Verifying... 20% [██░░░░░░░░]")
    await context.bot.send_chat_action(chat_id=uid, action='typing')
    anim = asyncio.create_task(
        animate_progress(msg, "⏳ Verifying..."))

    try:
        pf = await update.message.photo[-1].get_file()
        ba = await pf.download_as_bytearray()
        b64 = compress_image_to_b64(ba)
        ai = await call_ai_unified(
            "Extract 12-digit UTR and Amount. "
            "Format: UTR: <12digits> | AMOUNT: <number>",
            image_b64=b64)
        anim.cancel()

        if ai == "AI_FAILED":
            await msg.edit_text(
                "⚠️ **SERVER BUSY!** 30 sec baad dobara try karein.")
            return ConversationHandler.END

        utr_m = re.search(r'UTR:\s*(\d{12})', ai)
        amt_m = re.search(r'AMOUNT:\s*(\d+)', ai)
        if not utr_m:
            await msg.edit_text(
                "🚫 **SCREENSHOT ERROR:** Valid 12-digit UTR nahi "
                "mila. Clear uncropped screenshot bhejein!")
            return ConversationHandler.END

        utr = utr_m.group(1)
        ai_amt = float(amt_m.group(1)) if amt_m else claimed

        if not any(utr.startswith(p) for p in get_utr_prefixes()):
            await msg.edit_text(
                f"🚫 **OLD DATE REJECTED:** UTR `{safe_md(utr)}` "
                f"aaj/kal ka nahi hai!")
            return ConversationHandler.END

        if is_utr_used(utr):
            await msg.edit_text(
                f"🚫 **DUPLICATE UTR:** `{safe_md(utr)}` "
                f"already used!")
            return ConversationHandler.END

        save_utr_safely(utr, uid, ai_amt)

        kbd = [[InlineKeyboardButton(
            f"✅ APPROVE ₹{ai_amt}",
            callback_data=f"admdep_{uid}_{utr}_{ai_amt}")],
               [InlineKeyboardButton(
            "❌ REJECT",
            callback_data=f"admrej_{uid}")]]
        dossier = (f"🚨 **DEPOSIT REQUEST**\n"
                   f"👤 `{uid}` | Claimed: ₹{claimed} | "
                   f"AI: **₹{ai_amt}**\n🔢 UTR: `{safe_md(utr)}`")
        if ADMIN_GROUP_ID:
            await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID, photo=pf.file_id,
                caption=dossier, parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(kbd))
        await msg.edit_text(
            "✅ Submitted! 2-5 min mein balance add hoga.")
    except Exception as e:
        anim.cancel()
        logger.error(f"Payment proof err: {e}")
        await msg.edit_text("⚠️ Error. Dobara try karein.")
        return WAIT_PAY_PROOF
    return ConversationHandler.END

async def process_withdraw_qr(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if (update.message and update.message.text
            and "CANCEL" in update.message.text.upper()):
        return await cancel_action(update, context)
    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "❌ UPI QR Code photo bhejein.",
            reply_markup=get_cancel_kbd())
        return WAIT_WITHDRAW_QR
    uid = update.message.from_user.id
    user = get_user(uid)
    amt = user['winning_balance']
    if db:
        db.table("users").update({
            "winning_balance": 0,
            "locked_balance": user['locked_balance'] + amt
        }).eq("user_id", uid).execute()
        invalidate_user_cache(uid)
    kbd = [[InlineKeyboardButton(
        f"✅ PAID ₹{amt}",
        callback_data=f"admpaid_{uid}_{amt}")],
           [InlineKeyboardButton(
        "❌ REJECT",
        callback_data=f"admrejwd_{uid}_{amt}")]]
    if ADMIN_GROUP_ID:
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=update.message.photo[-1].file_id,
            caption=f"💸 **WITHDRAWAL**\n`{uid}` | ₹{amt}",
            reply_markup=InlineKeyboardMarkup(kbd),
            parse_mode='Markdown')
    await update.message.reply_text(
        f"✅ Withdraw request submitted. ₹{amt} locked.",
        reply_markup=get_main_menu())
    return ConversationHandler.END

# ==============================================================================
# 13. WINNER VERIFICATION
# ==============================================================================
async def up_proof_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mid = query.data.split("_")[2]
    context.user_data['win_match'] = mid
    await query.message.reply_text(
        f"🎉 Match #{safe_md(mid)} ka scoreboard screenshot bhejein:",
        reply_markup=get_cancel_kbd(), parse_mode='Markdown')
    return WAIT_WIN_PROOF

async def process_win_proof(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    if (update.message and update.message.text
            and "CANCEL" in update.message.text.upper()):
        return await cancel_action(update, context)
    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "❌ Scoreboard photo bhejein.",
            reply_markup=get_cancel_kbd())
        return WAIT_WIN_PROOF

    uid = update.message.from_user.id
    mid = context.user_data['win_match']
    msg = await update.message.reply_text(
        "⏳ Verifying Winner... 20% [██░░░░░░░░]")
    await context.bot.send_chat_action(chat_id=uid, action='typing')
    anim = asyncio.create_task(
        animate_progress(msg, "⏳ Verifying Winner..."))

    try:
        pf = await update.message.photo[-1].get_file()
        ba = await pf.download_as_bytearray()
        b64 = compress_image_to_b64(ba)
        ai = await call_ai_unified(
            "Read Rank #1 IGN. Is image cropped? "
            "Format: [UNCROPPED/CROPPED] | Rank 1: <Name>",
            image_b64=b64)
        anim.cancel()

        # Get match-specific prize
        prize = DEFAULT_PRIZE
        if db:
            mdata = db.table("matches").select("*").eq(
                "match_id", mid).execute().data
            if mdata: prize = get_match_prize(mdata[0])
            db.table("user_matches").update(
                {"status": "PENDING"}
            ).eq("user_id", uid).eq("match_id", mid).execute()

        user = get_user(uid)
        safe_ign = safe_md(user.get('ff_ign', f'Player_{uid}'))

        # Enhanced admin message with match number + prize + full info
        kbd = [[InlineKeyboardButton(
            f"✅ APPROVE (₹{prize})",
            callback_data=f"admprize_{uid}_{mid}")],
               [InlineKeyboardButton(
            "❌ REJECT",
            callback_data=f"admrejprize_{uid}_{mid}")]]
        dossier = (
            f"🏆 **WINNER CLAIM**\n\n"
            f"📌 Match: **#{safe_md(mid)}**\n"
            f"💰 Is match ka Prize: **₹{prize}**\n"
            f"👤 User: `{uid}`\n"
            f"🎮 IGN: `{safe_ign}`\n"
            f"📝 Ye user is specific match ke liye "
            f"bol raha hai 'main jeeta hoon'\n"
            f"🤖 AI Read: `{safe_md(ai)}`")

        if ADMIN_GROUP_ID:
            await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID, photo=pf.file_id,
                caption=dossier, parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(kbd))
        await msg.edit_text(
            "✅ Scoreboard submitted! "
            "Approve hote hi prize + public hype hoga.")
    except Exception as e:
        anim.cancel()
        logger.error(f"Win proof err: {e}")
        await msg.edit_text("⚠️ Error. Try again.")
        return WAIT_WIN_PROOF
    return ConversationHandler.END

# ==============================================================================
# 14. ANTI-CHEAT REPORT SYSTEM (3-step flow)
# ==============================================================================
async def report_hacker_btn(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    mid = query.data.split("_")[1]
    context.user_data['report_match'] = mid
    await query.message.reply_text(
        f"🛡️ **Hacker Report — Match #{safe_md(mid)}**\n\n"
        f"1️⃣ Accused ka naam/username kya hai?\n"
        f"(Jo player hack kar raha tha uska IGN likho)",
        reply_markup=get_cancel_kbd(), parse_mode='Markdown')
    return WAIT_REPORT_ACCUSED

async def report_accused_name(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if (update.message and update.message.text
            and "CANCEL" in update.message.text.upper()):
        return await cancel_action(update, context)
    if not update.message or not update.message.text:
        return WAIT_REPORT_ACCUSED
    accused = update.message.text.strip()
    context.user_data['report_accused'] = accused

    # Auto-search in database
    found_info = "⚠️ Ye hamara bot ka user NAHI hai (external player)"
    accused_uid = None
    if db:
        try:
            res = db.table("users").select("*").ilike(
                "ff_ign", f"%{accused}%").execute()
            if res.data:
                u = res.data[0]
                accused_uid = u['user_id']
                found_info = (
                    f"✅ **Ye hamara user hai!**\n"
                    f"👤 IGN: `{safe_md(u.get('ff_ign',''))}`\n"
                    f"🆔 ID: `{accused_uid}`")
        except: pass

    context.user_data['report_accused_uid'] = accused_uid
    await update.message.reply_text(
        f"🔍 **Search Result:**\n{found_info}\n\n"
        f"2️⃣ Ab batao — usne kya kiya?\n"
        f"(Kaise cheat ki, detail mein likho)",
        reply_markup=get_cancel_kbd(), parse_mode='Markdown')
    return WAIT_REPORT_DESC

async def report_description(update: Update,
                             context: ContextTypes.DEFAULT_TYPE):
    if (update.message and update.message.text
            and "CANCEL" in update.message.text.upper()):
        return await cancel_action(update, context)
    if not update.message or not update.message.text:
        return WAIT_REPORT_DESC
    context.user_data['report_desc'] = update.message.text.strip()
    await update.message.reply_text(
        "3️⃣ Ab proof bhejo — **Screenshot ya Video** 📸🎥\n"
        "(Bina proof ke report submit nahi hogi)",
        reply_markup=get_cancel_kbd(), parse_mode='Markdown')
    return WAIT_REPORT_PROOF

async def report_proof_submit(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if (update.message and update.message.text
            and "CANCEL" in update.message.text.upper()):
        return await cancel_action(update, context)
    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "❌ Screenshot/Video proof bhejein.",
            reply_markup=get_cancel_kbd())
        return WAIT_REPORT_PROOF

    uid = update.message.from_user.id
    mid = context.user_data.get('report_match', 'UNKNOWN')
    accused = context.user_data.get('report_accused', 'Unknown')
    accused_uid = context.user_data.get('report_accused_uid')
    desc = context.user_data.get('report_desc', '')
    reporter = get_user(uid)
    reporter_ign = reporter.get('ff_ign', f'User_{uid}')

    # Store report in DB
    report_id = f"RPT{random.randint(10000,99999)}"
    if db:
        try:
            db.table("cheat_reports").insert({
                "report_id": report_id,
                "match_id": mid,
                "reporter_id": uid,
                "accused_name": accused,
                "accused_id": accused_uid,
                "description": desc,
                "created_at": datetime.now(IST).isoformat(),
                "status": "PENDING"
            }).execute()
        except Exception as e:
            logger.warning(f"Report save err (table may not exist): {e}")

    # Build admin buttons based on whether accused is our user
    if accused_uid:
        kbd = [[InlineKeyboardButton(
            "⚠️ BAN + REFUND ALL",
            callback_data=f"banref_{mid}_{accused_uid}_{report_id}")],
               [InlineKeyboardButton(
            "💰 SIRF REFUND ALL",
            callback_data=f"refall_{mid}_{report_id}")],
               [InlineKeyboardButton(
            "❌ DISMISS",
            callback_data=f"disreport_{report_id}_{uid}")]]
    else:
        kbd = [[InlineKeyboardButton(
            "💰 REFUND ALL",
            callback_data=f"refall_{mid}_{report_id}")],
               [InlineKeyboardButton(
            "❌ DISMISS",
            callback_data=f"disreport_{report_id}_{uid}")]]

    # Get match info for admin message
    match_fee = DEFAULT_ENTRY_FEE
    match_prize = DEFAULT_PRIZE
    if db:
        mdata = db.table("matches").select("*").eq(
            "match_id", mid).execute().data
        if mdata:
            match_fee = get_match_fee(mdata[0])
            match_prize = get_match_prize(mdata[0])

    dossier = (
        f"🛡️ **ANTI-CHEAT REPORT!**\n\n"
        f"📌 Match: **#{safe_md(mid)}** "
        f"(Entry ₹{match_fee}, Prize ₹{match_prize})\n"
        f"👤 Reporter: {safe_md(reporter_ign)} (`{uid}`)\n"
        f"🎯 Accused: {safe_md(accused)}")
    if accused_uid:
        dossier += f"\n🆔 Accused ID: `{accused_uid}` ✅ HAMARA USER"
    else:
        dossier += "\n⚠️ EXTERNAL PLAYER (bot ka user nahi)"
    dossier += (f"\n📝 Complaint: {safe_md(desc)}\n"
                f"⏰ {datetime.now(IST).strftime('%d-%b %I:%M %p')}")

    if ADMIN_GROUP_ID:
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=update.message.photo[-1].file_id,
            caption=dossier, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(kbd))

    await update.message.reply_text(
        f"✅ **Report Submitted!**\n"
        f"Match #{safe_md(mid)} | Accused: {safe_md(accused)}\n"
        f"Admin review karega aur action lega.\n"
        f"Fair play ke liye shukriya! 🙏",
        reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

# ==============================================================================
# 15. ADMIN COMMANDS
# ==============================================================================
async def cmd_creatematch(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID: return
    fee = float(context.args[0]) if len(context.args) > 0 \
          else random.choice(ENTRY_OPTIONS)
    prize = float(context.args[1]) if len(context.args) > 1 else fee * 10
    tickets = int(context.args[2]) if len(context.args) > 2 \
              else MAX_PLAYERS
    mid = f"FF{random.randint(10000,99999)}"
    if db:
        try:
            db.table("matches").insert({
                "match_id": mid, "room_id": "TBD",
                "room_pass": "TBD", "tickets_left": tickets,
                "entry_fee": fee, "prize_money": prize,
                "scraped": False,
                "created_at": datetime.now(IST).isoformat()
            }).execute()
        except Exception:
            # Fallback if new columns don't exist yet
            db.table("matches").insert({
                "match_id": mid, "room_id": "TBD",
                "room_pass": "TBD", "tickets_left": tickets,
                "created_at": datetime.now(IST).isoformat()
            }).execute()
    await update.message.reply_text(
        f"✅ **Match #{safe_md(mid)} Created!**\n"
        f"Entry ₹{fee} | Prize ₹{prize} | Seats {tickets}\n"
        f"`/setroom {mid} <id> <pass>`",
        parse_mode='Markdown')

async def cmd_setroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID: return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /setroom <match_id> <room_id> <pass>")
        return
    mid = context.args[0].replace("#", "")
    rid, rpass = context.args[1], context.args[2]
    if db:
        db.table("matches").update(
            {"room_id": rid, "room_pass": rpass}
        ).eq("match_id", mid).execute()
        joined = db.table("user_matches").select("user_id").eq(
            "match_id", mid).eq("status", "JOINED").execute().data
    else: joined = []
    for u in joined:
        try:
            await context.bot.send_message(
                chat_id=u['user_id'],
                text=f"🚨 **ROOM READY! #{safe_md(mid)}** 🎮\n"
                     f"🔑 `{safe_md(rid)}` | 🔐 `{safe_md(rpass)}`\n"
                     f"⚡ Jaldi join karo!",
                parse_mode='Markdown')
        except: pass
    if PUBLIC_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=PUBLIC_CHANNEL_ID,
                text=f"🔥 **ROOM LIVE! #{safe_md(mid)}** 🎮\n"
                     f"🔑 `{safe_md(rid)}`\n"
                     f"👉 @FreeFireCustomRoom_Bot 🚀",
                parse_mode='Markdown')
        except: pass
    await update.message.reply_text(
        f"✅ Room updated! Notified {len(joined)} players.")

async def cmd_hype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID: return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /hype <message>")
        return
    if PUBLIC_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=PUBLIC_CHANNEL_ID,
                text=f"📢 **UPDATE** 🔥\n\n{text}\n\n"
                     f"👉 @FreeFireCustomRoom_Bot 🎮",
                parse_mode='Markdown')
            await update.message.reply_text("✅ Posted!")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID: return
    if not context.args:
        await update.message.reply_text("Usage: /status <match_id>")
        return
    mid = context.args[0].replace("#", "")
    if db:
        m = db.table("matches").select("*").eq(
            "match_id", mid).execute().data
        if not m:
            await update.message.reply_text("Match not found.")
            return
        m = m[0]
        fee = get_match_fee(m); prize = get_match_prize(m)
        taken = MAX_PLAYERS - m['tickets_left']
        txt = (f"🔴 **Match #{safe_md(mid)}**\n"
               f"Seats: {taken}/{MAX_PLAYERS}\n"
               f"Entry ₹{fee} | Prize ₹{prize}")
        await update.message.reply_text(txt, parse_mode='Markdown')

# ─── BAN / UNBAN / BANLIST / FINDUSER ───
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try: target = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    ban_user(target, f"Banned by admin {update.message.from_user.id}")
    u = get_user(target)
    ign = u.get('ff_ign', 'Unknown')
    await update.message.reply_text(
        f"🚫 **USER BANNED!**\n"
        f"👤 {safe_md(ign)} (`{target}`)\n"
        f"Permanent ban — delete/restart se bhi nahi hatega.",
        parse_mode='Markdown')
    try:
        await context.bot.send_message(
            chat_id=target,
            text="🚫 **PERMANENTLY BANNED**\n"
                 "Aapka account permanently ban ho gaya hai.\n"
                 "Contact: @Tughh\\_456",
            parse_mode='Markdown')
    except: pass

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try: target = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    unban_user(target)
    await update.message.reply_text(
        f"✅ **USER UNBANNED** (`{target}`)",
        parse_mode='Markdown')

async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id): return
    if not db:
        await update.message.reply_text("DB not available.")
        return
    try:
        res = db.table("users").select("user_id,ff_ign").eq(
            "is_banned", True).execute()
        if not res.data:
            await update.message.reply_text(
                "✅ No banned users.")
            return
        msg = "🚫 **BANNED USERS:**\n\n"
        for i, u in enumerate(res.data[:20], 1):
            msg += (f"{i}. {safe_md(u.get('ff_ign','?'))} "
                    f"(`{u['user_id']}`)\n")
        msg += f"\nTotal: {len(res.data)} banned"
        await update.message.reply_text(
            msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_finduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id): return
    if not context.args:
        await update.message.reply_text(
            "Usage: /finduser <name or ID>")
        return
    query = " ".join(context.args)
    if not db:
        await update.message.reply_text("DB not available.")
        return
    try:
        # Try as user_id first
        try:
            uid = int(query)
            res = db.table("users").select("*").eq(
                "user_id", uid).execute()
        except:
            res = db.table("users").select("*").ilike(
                "ff_ign", f"%{query}%").execute()
        if not res.data:
            await update.message.reply_text(
                f"❌ No user found for: {safe_md(query)}")
            return
        for u in res.data[:5]:
            banned = "🚫 BANNED" if u.get('is_banned') else "✅ Active"
            await update.message.reply_text(
                f"🔍 **User Found!**\n"
                f"👤 {safe_md(u.get('ff_ign',''))}\n"
                f"🆔 `{u['user_id']}`\n"
                f"💰 Dep ₹{u.get('deposit_balance',0)} | "
                f"Win ₹{u.get('winning_balance',0)}\n"
                f"🚫 {banned}",
                parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_scraperstatus(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id): return
    msg = "🔍 **SCRAPER STATUS**\n\n"
    if not scraper_stats:
        msg += "No scrapers running yet. Wait 5 min."
    else:
        for src, info in scraper_stats.items():
            status = "✅" if info.get('last_ok') else "⏳"
            fails = info.get('failures', 0)
            last = info.get('last_ok')
            last_str = (last.strftime('%H:%M')
                        if last else "Never")
            msg += f"{status} {src}: Last OK {last_str}, Fails {fails}\n"
    msg += f"\n🚫 Dead sources: {len(dead_sources)}"
    await update.message.reply_text(msg)

async def cmd_addworker(update: Update,
                        context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id):
        return ConversationHandler.END
    if not context.args:
        await update.message.reply_text(
            "Usage: /addworker +919876543210")
        return ConversationHandler.END
    phone = context.args[0]
    client = PyroClient(
        f"temp_{phone}", api_id=API_ID, api_hash=API_HASH,
        in_memory=True,
        device_model="Samsung Galaxy S24 Ultra",
        system_version="Android 15",
        app_version="10.14.0", lang_code="en")
    try:
        await client.connect()
        ci = await client.send_code(phone)
        pending_workers[update.message.from_user.id] = {
            "client": client, "phone": phone,
            "hash": ci.phone_code_hash}
        await update.message.reply_text(
            f"📱 OTP sent to `{safe_md(phone)}`!\nOTP type karein:",
            parse_mode='Markdown')
        return WAIT_WORKER_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ OTP failed: {e}")
        return ConversationHandler.END

async def enter_worker_otp(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in pending_workers: return ConversationHandler.END
    otp = update.message.text.strip()
    data = pending_workers[uid]
    try:
        await data['client'].sign_in(data['phone'], data['hash'], otp)
        sess = await data['client'].export_session_string()
        if db:
            db.table("workers").insert(
                {"phone": data['phone'],
                 "session_string": sess}).execute()
        await update.message.reply_text(
            f"✅ Worker `{safe_md(data['phone'])}` added!")
        await data['client'].disconnect()
        pending_workers.pop(uid, None)
    except SessionPasswordNeeded:
        await update.message.reply_text(
            "🔐 2FA Password type karein:")
        return WAIT_WORKER_PASS
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        pending_workers.pop(uid, None)
    return ConversationHandler.END

async def enter_worker_pass(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in pending_workers: return ConversationHandler.END
    data = pending_workers[uid]
    try:
        await data['client'].check_password(
            update.message.text.strip())
        sess = await data['client'].export_session_string()
        if db:
            db.table("workers").insert(
                {"phone": data['phone'],
                 "session_string": sess}).execute()
        await update.message.reply_text(
            f"✅ Worker `{safe_md(data['phone'])}` added!")
        await data['client'].disconnect()
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
    pending_workers.pop(uid, None)
    return ConversationHandler.END

async def cmd_delworker(update: Update,
                        context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.message.from_user.id): return
    if not context.args:
        await update.message.reply_text(
            "Usage: /delworker +919876543210")
        return
    phone = context.args[0]
    if db: db.table("workers").delete().eq("phone", phone).execute()
    if phone in pyro_clients:
        try: await pyro_clients[phone].stop()
        except: pass
        pyro_clients.pop(phone, None)
    await update.message.reply_text(
        f"🗑️ Worker `{safe_md(phone)}` deleted!")

# ==============================================================================
# 16. ADMIN CALLBACK HANDLER (Enhanced with all new buttons)
# ==============================================================================
async def generate_pdf_cert(ign, match_id, prize):
    if not HAS_REPORTLAB: return None
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setStrokeColor(colors.gold); c.setLineWidth(4)
    c.rect(30, 30, 552, 732)
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(306, 680, "CERTIFICATE OF ACHIEVEMENT")
    c.setFont("Helvetica", 16)
    c.drawCentredString(306, 620,
                        "Official Free Fire Tournament Champion")
    c.setFont("Helvetica-Bold", 32)
    c.drawCentredString(306, 530, ign)
    c.setFont("Helvetica", 14)
    c.drawCentredString(306, 460,
        f"For dominating Match #{match_id} — Won ₹{prize}!")
    c.setFont("Helvetica-Oblique", 12)
    c.drawCentredString(306, 100,
        f"Issued {datetime.now(IST).strftime('%Y-%m-%d')}")
    c.save(); buf.seek(0)
    return buf

async def admin_callback_handler(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try: await query.edit_message_reply_markup(reply_markup=None)
    except: pass

    parts = query.data.split("_")
    action = parts[0]

    try:
        # ─── DEPOSIT APPROVE ───
        if action == "admdep":
            tuid = int(parts[1])
            amount = float(parts[-1])
            utr = "_".join(parts[2:-1])
            if is_utr_used(utr):
                try:
                    await query.message.edit_caption(
                        caption=(query.message.caption or "") +
                        "\n\n⚠️ DUPLICATE UTR!")
                except: pass
                return
            save_utr_safely(utr, tuid, amount)
            credit_balance(tuid, 'deposit_balance', amount)
            try:
                await context.bot.send_message(
                    chat_id=tuid,
                    text=f"🎉 **PAYMENT APPROVED!**\n"
                         f"₹{amount} deposit mein add!",
                    parse_mode='Markdown')
            except:
                try:
                    await context.bot.send_message(
                        chat_id=tuid,
                        text=f"Payment Approved! ₹{amount} added.")
                except: pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n✅ APPROVED ₹{amount} by "
                    f"{query.from_user.first_name}")
            except: pass

        # ─── PRIZE APPROVE (Enhanced: public + motivational DM) ───
        elif action == "admprize":
            tuid = int(parts[1])
            mid = "_".join(parts[2:])
            user = get_user(tuid)
            safe_ign = safe_md(
                user.get('ff_ign', f'Player_{tuid}'))

            # Get match-specific prize
            prize = DEFAULT_PRIZE
            if db:
                mdata = db.table("matches").select("*").eq(
                    "match_id", mid).execute().data
                if mdata: prize = get_match_prize(mdata[0])

            # Credit prize
            credit_balance(tuid, 'winning_balance', prize)
            if db:
                try:
                    db.table("user_matches").update(
                        {"status": "WON"}
                    ).eq("user_id", tuid).eq(
                        "match_id", mid).execute()
                except: pass

            # Referral bonus
            ref_id = user.get('referrer_id')
            if ref_id and db:
                try:
                    credit_balance(ref_id, 'bonus_balance', 10.0)
                    await context.bot.send_message(
                        chat_id=ref_id,
                        text=f"🎉 Referral reward! "
                             f"`{safe_ign}` won! ₹10 bonus!",
                        parse_mode='Markdown')
                except: pass

            # PDF Certificate
            pdf = await generate_pdf_cert(
                user.get('ff_ign', 'Champion'), mid, prize)

            # DM to winner with motivational quote
            quote = random.choice(WINNER_QUOTES)
            try:
                await context.bot.send_message(
                    chat_id=tuid,
                    text=f"🏆 **WINNER VERIFIED!**\n\n"
                         f"Match #{safe_md(mid)} — "
                         f"Prize ₹{prize} credited!\n\n"
                         f"💬 {quote}\n\n"
                         f"🎮 Aur matches khelein aur jeetein!",
                    parse_mode='Markdown')
                if pdf:
                    await context.bot.send_document(
                        chat_id=tuid, document=pdf,
                        filename=f"Winner_{mid}.pdf",
                        caption="🏆 Official Certificate!")
            except: pass

            # Public channel hype
            if PUBLIC_CHANNEL_ID:
                hype = (
                    f"🏆👑 **CHAMPION!** 👑🏆\n\n"
                    f"🔥 Match #{safe_md(mid)}\n"
                    f"🎮 {safe_ign}\n"
                    f"💰 Won **₹{prize}** Cash!\n\n"
                    f"⚡ Bahut badhai ho! Agle match ke liye "
                    f"@FreeFireCustomRoom_Bot 🚀")
                try:
                    if query.message.photo:
                        await context.bot.send_photo(
                            chat_id=PUBLIC_CHANNEL_ID,
                            photo=query.message.photo[-1].file_id,
                            caption=hype, parse_mode='Markdown')
                    else:
                        await context.bot.send_message(
                            chat_id=PUBLIC_CHANNEL_ID,
                            text=hype, parse_mode='Markdown')
                except: pass

            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n🏆 APPROVED by "
                    f"{query.from_user.first_name}")
            except: pass

        # ─── REJECT (Deposit) ───
        elif action == "admrej":
            tuid = int(parts[1])
            try:
                await context.bot.send_message(
                    chat_id=tuid,
                    text="❌ **REQUEST REJECTED**\n"
                         "Aapki request reject ho gayi. "
                         "Support: @Tughh\\_456",
                    parse_mode='Markdown')
            except:
                try:
                    await context.bot.send_message(
                        chat_id=tuid,
                        text="Request rejected. Contact support.")
                except: pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n❌ REJECTED by "
                    f"{query.from_user.first_name}")
            except: pass

        # ─── REJECT PRIZE (Enhanced: fake screenshot DM) ───
        elif action == "admrejprize":
            tuid = int(parts[1])
            mid = "_".join(parts[2:])
            try:
                await context.bot.send_message(
                    chat_id=tuid,
                    text="❌ **WINNER CLAIM REJECTED**\n\n"
                         "Aapne fake screenshot dala hai — "
                         "aap winner nahi hain.\n\n"
                         "Agar aapko lagta hai hum galat hain, "
                         "toh Help Centre (@Tughh\\_456) par "
                         "apni pareshani batayein.",
                    parse_mode='Markdown')
            except:
                try:
                    await context.bot.send_message(
                        chat_id=tuid,
                        text="Winner claim rejected. "
                             "Contact @Tughh_456 if mistake.")
                except: pass
            if db:
                try:
                    db.table("user_matches").update(
                        {"status": "JOINED"}
                    ).eq("user_id", tuid).eq(
                        "match_id", mid).execute()
                except: pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n❌ REJECTED (fake screenshot) by "
                    f"{query.from_user.first_name}")
            except: pass

        # ─── BAN + REFUND ALL ───
        elif action == "banref":
            mid = parts[1]
            accused_uid = int(parts[2])
            report_id = parts[3] if len(parts) > 3 else ""
            # Ban the accused
            ban_user(accused_uid, f"Anti-cheat: Match #{mid}")
            try:
                await context.bot.send_message(
                    chat_id=accused_uid,
                    text="🚫 **PERMANENTLY BANNED**\n"
                         f"Match #{safe_md(mid)} mein cheating "
                         f"detect hui.\n"
                         "Aap kabhi ye bot use nahi kar payenge.",
                    parse_mode='Markdown')
            except: pass
            # Refund all players in this match
            if db:
                players = db.table("user_matches").select("*").eq(
                    "match_id", mid).eq("status", "JOINED").execute()
                mdata = db.table("matches").select("*").eq(
                    "match_id", mid).execute().data
                fee = get_match_fee(mdata[0]) if mdata \
                      else DEFAULT_ENTRY_FEE
                count = 0
                for p in (players.data or []):
                    puid = p['user_id']
                    if puid != accused_uid:
                        credit_balance(puid, 'deposit_balance', fee)
                        try:
                            await context.bot.send_message(
                                chat_id=puid,
                                text=f"⚠️ **Match #{safe_md(mid)}**\n\n"
                                     f"Bhai, is match mein ek hacker "
                                     f"detect hua hai.\n"
                                     f"Usko PERMANENTLY BAN kar diya.\n\n"
                                     f"Aapki entry fee ₹{fee} refund "
                                     f"ho gayi hai.\n"
                                     f"Sorry for inconvenience 🙏",
                                parse_mode='Markdown')
                        except: pass
                        count += 1
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n🚫 BANNED {accused_uid} + "
                    f"REFUNDED {count} players by "
                    f"{query.from_user.first_name}")

        # ─── REFUND ALL (no ban) ───
        elif action == "refall":
            mid = parts[1]
            if db:
                players = db.table("user_matches").select("*").eq(
                    "match_id", mid).eq("status", "JOINED").execute()
                mdata = db.table("matches").select("*").eq(
                    "match_id", mid).execute().data
                fee = get_match_fee(mdata[0]) if mdata \
                      else DEFAULT_ENTRY_FEE
                count = 0
                for p in (players.data or []):
                    credit_balance(
                        p['user_id'], 'deposit_balance', fee)
                    try:
                        await context.bot.send_message(
                            chat_id=p['user_id'],
                            text=f"⚠️ **Match #{safe_md(mid)}**\n\n"
                                 f"Is match mein hacker detect hua.\n"
                                 f"Entry fee ₹{fee} refund ho gayi.\n"
                                 f"Sorry 🙏",
                            parse_mode='Markdown')
                    except: pass
                    count += 1
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n💰 REFUNDED {count} players by "
                    f"{query.from_user.first_name}")

        # ─── DISMISS REPORT ───
        elif action == "disreport":
            report_id = parts[1]
            reporter_uid = int(parts[2]) if len(parts) > 2 else 0
            if db:
                try:
                    db.table("cheat_reports").update(
                        {"status": "DISMISSED"}
                    ).eq("report_id", report_id).execute()
                except: pass
            if reporter_uid:
                try:
                    await context.bot.send_message(
                        chat_id=reporter_uid,
                        text="ℹ️ Aapki report review ki gayi — "
                             "hume cheating ka proof nahi mila. "
                             "Report dismiss ki gayi hai.",
                        parse_mode='Markdown')
                except: pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n❌ DISMISSED by "
                    f"{query.from_user.first_name}")
            except: pass

        # ─── WITHDRAW PAID / REJECTED ───
        elif action in ("admpaid", "admrejwd"):
            tuid = int(parts[1])
            amt = float(parts[-1])
            if action == "admpaid":
                if db:
                    u = get_user(tuid)
                    db.table("users").update({
                        "locked_balance": max(
                            0, u['locked_balance'] - amt)
                    }).eq("user_id", tuid).execute()
                    invalidate_user_cache(tuid)
                try:
                    await context.bot.send_message(
                        chat_id=tuid,
                        text=f"✅ Withdrawal ₹{amt} done!")
                except: pass
            else:
                u = get_user(tuid)
                if db:
                    db.table("users").update({
                        "locked_balance": max(
                            0, u['locked_balance'] - amt),
                        "winning_balance": u['winning_balance'] + amt
                    }).eq("user_id", tuid).execute()
                    invalidate_user_cache(tuid)
                try:
                    await context.bot.send_message(
                        chat_id=tuid,
                        text=f"❌ Withdraw rejected. "
                             f"₹{amt} back to Winnings.")
                except: pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\nDone by {query.from_user.first_name}")
            except: pass

    except Exception as e:
        logger.error(f"Admin callback error: {e}\n"
                     f"{traceback.format_exc()}")

# ==============================================================================
# 17. PYROGRAM WORKERS (Private group scraper)
# ==============================================================================
async def start_all_workers():
    if not db: return
    try:
        workers = db.table("workers").select("*").execute().data
        for w in workers:
            phone = w['phone']
            if not w.get('session_string'): continue
            client = PyroClient(
                f"worker_{phone}", api_id=API_ID,
                api_hash=API_HASH,
                session_string=w['session_string'],
                in_memory=True,
                device_model="Samsung Galaxy S24 Ultra",
                system_version="Android 15",
                app_version="10.14.0", lang_code="en")

            @client.on_message(pyfilters.channel | pyfilters.group)
            async def scrape_room_smart(client, message):
                try:
                    text = (message.text or message.caption
                            or "").strip()
                    if not text and not message.photo: return
                    has_num = bool(re.search(r'\d{6,10}', text))
                    kws = ["id", "password", "pass", "pwd",
                           "room", "custom", "freefire", "ff",
                           "match", "join", "booyah"]
                    has_kw = any(k in text.lower() for k in kws)
                    if not has_num and not has_kw: return
                    id_m = re.search(
                        r'(?:ID|ROOM|RM)\s*[:\-=]?\s*(\d{6,10})',
                        text, re.I)
                    pw_m = re.search(
                        r'(?:PASS|PWD|PW)\s*[:\-=]?\s*'
                        r'([A-Za-z0-9@#$!]+)', text, re.I)
                    r_id = id_m.group(1) if id_m else None
                    r_pw = pw_m.group(1) if pw_m else None
                    if not r_id or not r_pw:
                        ai = await call_ai_unified(
                            f"Extract FF room ID (6-10 digits) "
                            f"and password. Reply ID:xxx|PASS:yyy "
                            f"or NONE. Msg: {text[:200]}")
                        if "ID:" in ai and "PASS:" in ai:
                            m1 = re.search(r'ID:(\d+)', ai)
                            m2 = re.search(r'PASS:([^\s|]+)', ai)
                            if m1 and m2:
                                r_id, r_pw = m1.group(1), m2.group(1)
                    if r_id and r_pw:
                        await _add_scraped_match(r_id, r_pw,
                                                 "telegram_worker")
                except Exception as e:
                    logger.warning(f"Worker scrape err: {e}")

            await client.start()
            pyro_clients[phone] = client
            logger.info(f"Worker started: {phone}")
    except Exception as e:
        logger.error(f"start_all_workers err: {e}")

# ==============================================================================
# 18. EXTERNAL SCRAPING ENGINE (Public sources — crash-proof)
# ==============================================================================

# Region filter keywords
GREEN_KW = ["bhai", "ajao", "aao", "india", "indian",
            "inr", "₹", "upi", "paytm", "phonepe", "gpay",
            "nepal", "bangladesh", "bd ", "hindi", "hinglish",
            "custom room", "room id", "password", "ff max"]
RED_KW = ["senha", "sala personalizada", "garena br",
          "brasil", "brazil", "turnamen", "sistem",
          "contraseña", "latam", "mena"]

def is_indian_content(text):
    t = text.lower()
    green = sum(1 for k in GREEN_KW if k in t)
    red = sum(1 for k in RED_KW if k in t)
    return green > red

async def _safe_http_get(url, timeout=10):
    """Crash-proof HTTP GET with error handling."""
    try:
        headers = {}
        if UA:
            try: headers["User-Agent"] = UA.random
            except: headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36")
        else:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36")
        resp = await asyncio.to_thread(
            requests.get, url, headers=headers,
            timeout=timeout, allow_redirects=True)
        return resp
    except Exception as e:
        logger.warning(f"HTTP GET fail {url[:60]}: {e}")
        return None

async def _add_scraped_match(room_id, room_pass, source,
                             entry_fee=None, prize=None):
    """Add a scraped room to the match board with 10-min timer."""
    if not db: return
    if not room_id or not room_pass: return

    # Dedup check (same room in last 1 hour)
    try:
        one_hr = (datetime.now(IST) - timedelta(hours=1)).isoformat()
        exists = db.table("matches").select("*").eq(
            "room_id", room_id).gt("created_at", one_hr).execute()
        if exists.data: return  # already exists
    except: pass

    if not entry_fee:
        entry_fee = random.choice(ENTRY_OPTIONS)
    if not prize:
        prize = entry_fee * 10

    mid = f"SC{random.randint(10000,99999)}"
    try:
        db.table("matches").insert({
            "match_id": mid, "room_id": room_id,
            "room_pass": room_pass,
            "tickets_left": MAX_PLAYERS,
            "entry_fee": entry_fee,
            "prize_money": prize,
            "scraped": True,
            "created_at": datetime.now(IST).isoformat()
        }).execute()
        logger.info(f"Scraped match added: {mid} "
                     f"(Room:{room_id}, Src:{source})")
    except Exception:
        # Fallback without new columns
        try:
            db.table("matches").insert({
                "match_id": mid, "room_id": room_id,
                "room_pass": room_pass,
                "tickets_left": MAX_PLAYERS,
                "created_at": datetime.now(IST).isoformat()
            }).execute()
        except Exception as e:
            logger.error(f"Add scraped match fail: {e}")

async def _verify_with_ai(text):
    """Send scraped text to AI to verify if it's a FF custom room."""
    prompt = (
        "You are a Free Fire custom room detector.\n"
        f"MESSAGE: {text[:300]}\n\n"
        "Is this a Free Fire custom room with room ID and "
        "password? Reply exactly:\n"
        "YES|ROOM_ID|PASSWORD|SERVER\n"
        "or NO|reason\n"
        "Where SERVER is India/Brazil/Other.\n"
        "Room ID is 6-10 digits. Password is alphanumeric.")
    ai = await call_ai_unified(prompt)
    if ai.startswith("YES|"):
        p = ai.split("|")
        if len(p) >= 3:
            rid = p[1].strip()
            rpw = p[2].strip()
            server = p[3].strip() if len(p) > 3 else "India"
            if server.lower() != "india": return None
            if re.match(r'\d{6,10}$', rid) and rpw:
                return (rid, rpw)
    return None

# ─── Telegram Public Channel Scraper ───
async def scrape_telegram_channels():
    """Scrape public Telegram channels for FF rooms."""
    if not HAS_BS4:
        logger.warning("BeautifulSoup not installed. "
                       "pip install beautifulsoup4 lxml")
        return
    channels = [
        "indiaofficialfreefire", "qulishtech",
        "Free_Fire_Gaming", "dktech_hindi",
        "TechProfitChannel", "freefirepanel_free",
    ]
    # Also try to load from DB if available
    if db:
        try:
            extra = db.table("external_sources").select(
                "url").eq("type", "telegram").execute()
            for row in (extra.data or []):
                ch = row['url'].strip("/").split("/")[-1]
                if ch and ch not in channels:
                    channels.append(ch)
        except: pass

    for ch in channels:
        if f"tg_{ch}" in dead_sources: continue
        try:
            resp = await _safe_http_get(f"https://t.me/s/{ch}")
            if not resp:
                scraper_stats.setdefault(
                    f"tg_{ch}", {"failures": 0})
                scraper_stats[f"tg_{ch}"]["failures"] += 1
                if scraper_stats[f"tg_{ch}"]["failures"] >= 5:
                    dead_sources.add(f"tg_{ch}")
                continue
            if resp.status_code in (403, 404):
                dead_sources.add(f"tg_{ch}")
                continue
            soup = BeautifulSoup(resp.text, "lxml"
                                 if HAS_BS4 else "html.parser")
            msgs = soup.find_all(
                "div", class_="tgme_widget_message_text")
            for msg_div in msgs[-10:]:  # last 10 messages
                text = msg_div.get_text(strip=True)
                if not text or len(text) < 20: continue
                if not is_indian_content(text): continue
                # Quick regex check first
                id_m = re.search(
                    r'(?:ID|ROOM|RM)\s*[:\-=]?\s*(\d{6,10})',
                    text, re.I)
                pw_m = re.search(
                    r'(?:PASS|PWD|PW)\s*[:\-=]?\s*'
                    r'([A-Za-z0-9@#$!]+)', text, re.I)
                if id_m and pw_m:
                    await _add_scraped_match(
                        id_m.group(1), pw_m.group(1),
                        f"tg_{ch}")
                elif any(kw in text.lower() for kw in
                         ["room", "custom", "password", "id"]):
                    result = await _verify_with_ai(text)
                    if result:
                        await _add_scraped_match(
                            result[0], result[1], f"tg_{ch}")
            scraper_stats[f"tg_{ch}"] = {
                "last_ok": datetime.now(IST), "failures": 0}
        except Exception as e:
            logger.warning(f"TG scrape {ch} err: {e}")
        await asyncio.sleep(random.uniform(1, 3))

# ─── Reddit Scraper ───
async def scrape_reddit():
    subreddits = ("FreeFireIndia+freefire+FreeFireMax+"
                  "GarenaFreeFire+FreeFireEsports+"
                  "IndianGaming+MobileGamingIndia")
    url = f"https://www.reddit.com/r/{subreddits}.json?limit=30"
    resp = await _safe_http_get(url, timeout=15)
    if not resp or resp.status_code != 200:
        scraper_stats.setdefault("reddit", {"failures": 0})
        scraper_stats["reddit"]["failures"] += 1
        return
    try:
        data = resp.json()
        for child in data.get('data', {}).get('children', []):
            post = child.get('data', {})
            text = (post.get('title', '') + " " +
                    post.get('selftext', ''))
            if not text or len(text) < 20: continue
            if not is_indian_content(text): continue
            id_m = re.search(r'(?:ID|ROOM)\s*[:\-]?\s*(\d{6,10})',
                             text, re.I)
            pw_m = re.search(
                r'(?:PASS|PWD|Password)\s*[:\-]?\s*'
                r'([A-Za-z0-9]+)', text, re.I)
            if id_m and pw_m:
                await _add_scraped_match(
                    id_m.group(1), pw_m.group(1), "reddit")
            elif "custom room" in text.lower() or \
                 "room id" in text.lower():
                result = await _verify_with_ai(text)
                if result:
                    await _add_scraped_match(
                        result[0], result[1], "reddit")
        scraper_stats["reddit"] = {
            "last_ok": datetime.now(IST), "failures": 0}
    except Exception as e:
        logger.warning(f"Reddit parse err: {e}")

# ─── YouTube RSS Scraper ───
async def scrape_youtube_rss():
    if not HAS_FEEDPARSER: return
    # FF YouTuber channel IDs (big + medium + small)
    channel_ids = [
        "UCUcCOOEBp6MK99MvJljfLig",  # Total Gaming
        "UCAheXRvVYFhGpYdYJMRoFEQ",  # Desi Gamers
        "UCnY8YFgHyEZFkPq5GkMFejw",  # AS Gaming
        "UCKZb7G7M9Bm5FoFbrTilE2g",  # Gyan Gaming
        "UCkJXni8KE7TkMYP3-fvS3XQ",  # Two Side Gamers
        "UCw4YcOMN4bM2Yhb2MR4oGJg",  # Lokesh Gamer
    ]
    for cid in channel_ids:
        if f"yt_{cid[:8]}" in dead_sources: continue
        try:
            url = (f"https://www.youtube.com/feeds/videos.xml"
                   f"?channel_id={cid}")
            resp = await _safe_http_get(url, timeout=10)
            if not resp or resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.text)
            for entry in feed.entries[:5]:
                desc = entry.get('summary', '') + " " + \
                       entry.get('title', '')
                if not is_indian_content(desc.lower()): continue
                id_m = re.search(
                    r'(?:ID|ROOM)\s*[:\-]?\s*(\d{6,10})',
                    desc, re.I)
                pw_m = re.search(
                    r'(?:PASS|PWD)\s*[:\-]?\s*([A-Za-z0-9]+)',
                    desc, re.I)
                if id_m and pw_m:
                    await _add_scraped_match(
                        id_m.group(1), pw_m.group(1),
                        f"yt_{cid[:8]}")
            scraper_stats[f"yt_{cid[:8]}"] = {
                "last_ok": datetime.now(IST), "failures": 0}
        except Exception as e:
            logger.warning(f"YT RSS {cid[:8]} err: {e}")
        await asyncio.sleep(1)

# ─── Rooter Scraper ───
async def scrape_rooter():
    try:
        resp = await _safe_http_get(
            "https://rooter.gg/api/discover/streams"
            "?game=free-fire&limit=20", timeout=10)
        if not resp or resp.status_code != 200:
            # Fallback: scrape main page
            resp = await _safe_http_get(
                "https://rooter.gg", timeout=10)
            if not resp: return
        # Try to parse stream titles
        try:
            data = resp.json()
            streams = data.get('data', data.get('streams', []))
            for s in streams:
                title = s.get('title', '') + " " + \
                        s.get('description', '')
                if not is_indian_content(title.lower()): continue
                id_m = re.search(
                    r'(?:ID|ROOM)\s*[:\-]?\s*(\d{6,10})',
                    title, re.I)
                pw_m = re.search(
                    r'(?:PASS|PWD)\s*[:\-]?\s*([A-Za-z0-9]+)',
                    title, re.I)
                if id_m and pw_m:
                    await _add_scraped_match(
                        id_m.group(1), pw_m.group(1), "rooter")
        except:
            pass
        scraper_stats["rooter"] = {
            "last_ok": datetime.now(IST), "failures": 0}
    except Exception as e:
        logger.warning(f"Rooter err: {e}")

# ─── Loco Scraper ───
async def scrape_loco():
    try:
        resp = await _safe_http_get(
            "https://loco.gg/api/streams?game=free-fire",
            timeout=10)
        if not resp or resp.status_code != 200:
            resp = await _safe_http_get(
                "https://loco.gg/streams/free-fire",
                timeout=10)
            if not resp: return
        try:
            data = resp.json()
            streams = data.get('data', data.get('streams', []))
            for s in streams:
                title = s.get('title', '') + " " + \
                        s.get('description', '')
                if not is_indian_content(title.lower()): continue
                id_m = re.search(
                    r'(?:ID|ROOM)\s*[:\-]?\s*(\d{6,10})',
                    title, re.I)
                pw_m = re.search(
                    r'(?:PASS|PWD)\s*[:\-]?\s*([A-Za-z0-9]+)',
                    title, re.I)
                if id_m and pw_m:
                    await _add_scraped_match(
                        id_m.group(1), pw_m.group(1), "loco")
        except:
            pass
        scraper_stats["loco"] = {
            "last_ok": datetime.now(IST), "failures": 0}
    except Exception as e:
        logger.warning(f"Loco err: {e}")

# ─── Match Auto-Start (even if <10 players) ───
async def auto_start_matches():
    """After 10 min, mark matches as started (no refund after)."""
    if not db: return
    try:
        cutoff = (datetime.now(IST) -
                  timedelta(minutes=10)).isoformat()
        # Find matches older than 10 min that are still active
        old_matches = db.table("matches").select("*").lt(
            "created_at", cutoff).neq("room_id", "TBD").execute()
        for m in (old_matches.data or []):
            mid = m['match_id']
            # Move joined players to MY MATCHES state
            # (they can still see room info but no refund)
            # This is handled by the refund window check
            # in MY MATCHES display
            pass
    except Exception as e:
        logger.warning(f"auto_start err: {e}")

# ─── Match Reminder (2 min before) ───
async def match_reminder_job():
    """Send DM reminders 2 min before scheduled matches."""
    if not db: return
    try:
        # Find matches where room was set recently
        # and has joined players
        matches = db.table("matches").select("*").neq(
            "room_id", "TBD").gt("tickets_left", 0).execute()
        for m in (matches.data or []):
            mid = m['match_id']
            created = m.get('created_at', '')
            if not created: continue
            try:
                ct = datetime.fromisoformat(created)
                if ct.tzinfo is None: ct = IST.localize(ct)
                age_mins = (datetime.now(IST) - ct
                            ).total_seconds() / 60
            except: continue
            # Send reminder at ~8 min mark (once)
            if 7.5 < age_mins < 9:
                players = db.table("user_matches").select(
                    "user_id").eq("match_id", mid).eq(
                    "status", "JOINED").execute()
                for p in (players.data or []):
                    try:
                        await asyncio.sleep(0.2)
                        # Already sent check (use context flag)
                        flag_key = f"reminded_{mid}_{p['user_id']}"
                        if flag_key in user_cache: continue
                        user_cache[flag_key] = (True,
                                                datetime.now())
                        await _send_reminder(
                            p['user_id'], m)
                    except: pass
    except Exception as e:
        logger.warning(f"Reminder err: {e}")

async def _send_reminder(uid, match):
    """Send match reminder DM (bot instance needed)."""
    mid = match['match_id']
    rid = match['room_id']
    rpw = match['room_pass']
    fee = get_match_fee(match)
    # We'll store the bot app reference globally
    if _bot_app:
        try:
            await _bot_app.bot.send_message(
                chat_id=uid,
                text=f"🔥 **MATCH REMINDER!**\n\n"
                     f"Match #{safe_md(mid)} start hone wala hai!\n"
                     f"🔑 `{safe_md(rid)}` | "
                     f"🔐 `{safe_md(rpw)}`\n"
                     f"⚡ Jaldi join karo!",
                parse_mode='Markdown')
        except: pass

_bot_app = None  # Will be set in main()

# ─── Master Scraper Loop ───
async def scraper_master_loop():
    """Run all scrapers in a cycle every 5 minutes."""
    logger.info("Scraper engine started. First cycle in 30s...")
    await asyncio.sleep(30)
    while True:
        try:
            logger.info("Scraper cycle starting...")
            # Run all scrapers concurrently
            tasks = [
                scrape_telegram_channels(),
                scrape_reddit(),
                scrape_youtube_rss(),
                scrape_rooter(),
                scrape_loco(),
                auto_start_matches(),
                match_reminder_job(),
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            active = sum(1 for s in scraper_stats.values()
                         if s.get('last_ok'))
            logger.info(f"Scraper cycle done. "
                        f"Active sources: {active}, "
                        f"Dead: {len(dead_sources)}")
        except Exception as e:
            logger.error(f"Scraper master err: {e}")
        await asyncio.sleep(300)  # 5 minutes

# ==============================================================================
# 19. HEALTH CHECK WEB SERVER
# ==============================================================================
async def health_check(request):
    return web.Response(
        text="Free Fire Tournament Bot OK", status=200)

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app_web = web.Application()
    app_web.router.add_get("/", health_check)
    runner = web.AppRunner(app_web)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

# ==============================================================================
# 20. MAIN ENTRYPOINT — All handlers wired
# ==============================================================================
def main():
    global _bot_app
    if not BOT_TOKEN:
        logger.error("CRITICAL: BOT_TOKEN missing!"); return

    application = Application.builder().token(BOT_TOKEN).build()
    _bot_app = application

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("addworker", cmd_addworker),
            MessageHandler(filters.TEXT & ~filters.COMMAND,
                           handle_menu),
            CallbackQueryHandler(up_proof_btn,
                                 pattern=r"^up_proof_"),
            CallbackQueryHandler(report_hacker_btn,
                                 pattern=r"^repthack_"),
        ],
        states={
            WAIT_IGN: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, save_ign_flow)],
            WAIT_ADD_AMT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, enter_amount)],
            WAIT_PAY_PROOF: [MessageHandler(
                filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                process_payment_proof)],
            WAIT_WITHDRAW_QR: [MessageHandler(
                filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                process_withdraw_qr)],
            WAIT_WIN_PROOF: [MessageHandler(
                filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                process_win_proof)],
            WAIT_SUPPORT_CHAT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_support_chat)],
            WAIT_WORKER_OTP: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                enter_worker_otp)],
            WAIT_WORKER_PASS: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                enter_worker_pass)],
            WAIT_REPORT_ACCUSED: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                report_accused_name)],
            WAIT_REPORT_DESC: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                report_description)],
            WAIT_REPORT_PROOF: [MessageHandler(
                filters.PHOTO | filters.VIDEO |
                (filters.TEXT & ~filters.COMMAND),
                report_proof_submit)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel_action),
            MessageHandler(
                filters.Regex(r"(?i)CANCEL|Cancel & Go Back"),
                cancel_action)],
        per_user=True, per_chat=True)

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CommandHandler("creatematch", cmd_creatematch))
    application.add_handler(
        CommandHandler("setroom", cmd_setroom))
    application.add_handler(CommandHandler("hype", cmd_hype))
    application.add_handler(
        CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("ban", cmd_ban))
    application.add_handler(CommandHandler("unban", cmd_unban))
    application.add_handler(
        CommandHandler("banlist", cmd_banlist))
    application.add_handler(
        CommandHandler("finduser", cmd_finduser))
    application.add_handler(
        CommandHandler("scraperstatus", cmd_scraperstatus))
    application.add_handler(
        CommandHandler("delworker", cmd_delworker))

    # Callback queries
    application.add_handler(
        CallbackQueryHandler(legal_callback,
                             pattern=r"^legal_"))
    application.add_handler(
        CallbackQueryHandler(conf_join,
                             pattern=r"^confjoin_"))
    application.add_handler(
        CallbackQueryHandler(do_join,
                             pattern=r"^dojoin_"))
    application.add_handler(
        CallbackQueryHandler(ask_refund,
                             pattern=r"^askref_"))
    application.add_handler(
        CallbackQueryHandler(do_refund,
                             pattern=r"^doref_"))
    application.add_handler(
        CallbackQueryHandler(cancel_inline,
                             pattern=r"^delete_msg$"))
    # Anti-cheat admin buttons
    application.add_handler(
        CallbackQueryHandler(admin_callback_handler,
                             pattern=r"^(adm|banref|refall|disreport)"))

    # Conversation handler (MUST be last)
    application.add_handler(conv_handler)

    async def post_init(app: Application):
        asyncio.create_task(start_web_server())
        asyncio.create_task(start_all_workers())
        asyncio.create_task(scraper_master_loop())

    application.post_init = post_init
    logger.info("Starting Free Fire Tournament Bot...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
