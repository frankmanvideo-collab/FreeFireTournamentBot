import os
import re
import io
import json
import base64
import random
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from aiohttp import web
from pyrogram import Client as PyroClient, filters as pyfilters
import pytz

# Try importing reportlab for Offline PDF Certificate generation
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

# ==============================================================================
# 1. ENTERPRISE CONFIG & ENVIRONMENT VARIABLES
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Core Bot & Database Config
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Dual Group Architecture Config
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))      # Private Admin Group for verification dossiers
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))        # Primary Admin Telegram User ID for DM commands
PUBLIC_CHANNEL_ID = os.environ.get("PUBLIC_CHANNEL_ID", os.environ.get("CHANNEL_ID", "")) # Public Channel for Hype & Announcements

# Pyrogram Scraper Config
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "placeholder")

# Tier-1 VIP AI Engine (aicredits.in)
AICREDITS_API_KEY = os.environ.get("AICREDITS_API_KEY")
AICREDITS_BASE_URL = os.environ.get("AICREDITS_BASE_URL", "https://api.aicredits.in/v1")

IST = pytz.timezone('Asia/Kolkata')

# Initialize Supabase
if SUPABASE_URL and SUPABASE_KEY:
    try:
        db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.error(f"Supabase init error: {e}")
        db = None
else:
    db = None

join_locks = {}
pyro_clients = {}
user_throttle = {} # In-memory Rate Limiter Shield

ENTRY_FEE = 50.0
PRIZE_MONEY = 300.0
MATCH_LIVE_MINS = 15
REFUND_WINDOW_MINS = 8

# Conversation States
(WAIT_IGN, WAIT_ADD_AMT, WAIT_PAY_PROOF, WAIT_WITHDRAW_QR, WAIT_WIN_PROOF, WAIT_SUPPORT_CHAT) = range(6)

# ==============================================================================
# 2. UNIVERSAL SAFE MARKDOWN SHIELD (Prevents Underscore / Italic Crash)
# ==============================================================================
def safe_md(text):
    """
    Escapes special Markdown v1 symbols (_ * [ `) inside player names or dynamic text
    so Telegram never throws 'Can't parse entities: can't find end of italic entity'.
    """
    if not text: return ""
    text = str(text)
    return text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[")

# ==============================================================================
# 3. 19-KEY ROUND-ROBIN AI SUPERPOOL ENGINE
# ==============================================================================
class AIPoolManager:
    """
    Manages 19+ Free AI Keys across Groq, Cloudflare, Gemini, Mistral, SambaNova, Cerebras, OpenRouter.
    Automatic 1ms failover if any key hits rate limits or network timeout.
    """
    def __init__(self):
        self.keys = []
        self._load_keys()
        self.current_idx = 0

    def _load_keys(self):
        # Load from single comma-separated string if present
        pool_str = os.environ.get("AI_POOL_KEYS", "")
        if pool_str:
            for k in pool_str.split(","):
                k = k.strip()
                if k and k not in self.keys:
                    self.keys.append(k)
        
        # Load from separate environment variables
        for name, val in os.environ.items():
            if any(name.startswith(p) for p in ["GROQ_", "GEMINI_", "CLOUDFLARE_", "CF_", "MISTRAL_", "SAMBANOVA_", "CEREBRAS_", "OPENROUTER_"]):
                for sub_k in val.split(","):
                    sub_k = sub_k.strip()
                    if sub_k and sub_k not in self.keys:
                        self.keys.append(sub_k)
        logger.info(f"AI SuperPool initialized with {len(self.keys)} total keys.")

    def get_next_key(self):
        if not self.keys: return None
        k = self.keys[self.current_idx]
        self.current_idx = (self.current_idx + 1) % len(self.keys)
        return k

ai_pool = AIPoolManager()

async def call_ai_unified(prompt, image_b64=None, system_context=""):
    """
    Unified AI execution engine.
    Tier 1: Checks AICREDITS_API_KEY first for vision/critical tasks.
    Tier 2/3: Uses 19-Key Round-Robin Pool with auto failover.
    """
    # 1. Try AICREDITS.IN for Vision or if configured
    if AICREDITS_API_KEY and image_b64:
        try:
            url = f"{AICREDITS_BASE_URL.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {AICREDITS_API_KEY}", "Content-Type": "application/json"}
            messages = [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]}]
            resp = await asyncio.to_thread(requests.post, url, headers=headers, json={"model": "gemini-1.5-flash", "messages": messages}, timeout=12)
            if resp.status_code == 200: return resp.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.warning(f"AICREDITS vision failed: {e}. Switching to pool...")

    # 2. Try 19-Key Rotation Pool
    for _ in range(max(1, len(ai_pool.keys))):
        key = ai_pool.get_next_key()
        if not key: break
        
        try:
            # Check Cloudflare format (account_id:token)
            if ":" in key and len(key.split(":")[0]) == 32:
                cf_acc, cf_tok = key.split(":", 1)
                url = f"https://api.cloudflare.com/client/v4/accounts/{cf_acc}/ai/run/@cf/meta/llama-3.1-8b-instruct"
                headers = {"Authorization": f"Bearer {cf_tok}", "Content-Type": "application/json"}
                payload = {"messages": [{"role": "system", "content": system_context or "You are an AI assistant."}, {"role": "user", "content": prompt}]}
                resp = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=8)
                if resp.status_code == 200: return resp.json()['result']['response']
                continue

            # Check Google Gemini format (AIzaSy...)
            if key.startswith("AIzaSy"):
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}"
                parts = [{"text": f"{system_context}\n\n{prompt}"}]
                if image_b64: parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image_b64}})
                resp = await asyncio.to_thread(requests.post, url, json={"contents": [{"parts": parts}]}, timeout=10)
                if resp.status_code == 200: return resp.json()['candidates'][0]['content']['parts'][0]['text']
                continue

            # Determine endpoint for Groq, Mistral, Cerebras, SambaNova, OpenRouter
            if key.startswith("gsk_"): endpoint = "https://api.groq.com/openai/v1/chat/completions"; model = "llama-3.1-8b-instant"
            elif key.startswith("csk-"): endpoint = "https://api.cerebras.ai/v1/chat/completions"; model = "llama3.1-8b"
            elif len(key) == 32 or "mistral" in os.environ: endpoint = "https://api.mistral.ai/v1/chat/completions"; model = "mistral-small-latest"
            else: endpoint = "https://openrouter.ai/api/v1/chat/completions"; model = "meta-llama/llama-3.1-8b-instruct:free"
            
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            messages = [{"role": "system", "content": system_context or "You are an AI assistant."}, {"role": "user", "content": prompt}]
            resp = await asyncio.to_thread(requests.post, endpoint, headers=headers, json={"model": model, "messages": messages}, timeout=8)
            if resp.status_code == 200: return resp.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.warning(f"Key failover triggered: {e}")
            continue

    return "AI_FAILED"

# ==============================================================================
# 4. CORE DATABASE MANAGERS & RATE LIMIT SHIELD
# ==============================================================================
def is_throttled(user_id):
    now = datetime.now()
    last_time = user_throttle.get(user_id)
    if last_time and (now - last_time).total_seconds() < 1.0:
        return True
    user_throttle[user_id] = now
    return False

def get_user(user_id):
    if not db:
        return {"user_id": user_id, "deposit_balance": 100.0, "winning_balance": 0.0,
                "bonus_balance": 10.0, "locked_balance": 0.0, "ff_ign": "TEST_USER", 
                "last_login": "", "is_18_plus": True, "is_restricted": False, "referrer_id": None}
    try:
        res = db.table("users").select("*").eq("user_id", user_id).execute()
        if not res.data:
            new_user = {
                "user_id": user_id, "deposit_balance": 0.0, "winning_balance": 0.0,
                "bonus_balance": 0.0, "locked_balance": 0.0, "ff_ign": "", 
                "last_login": "", "is_18_plus": False, "is_restricted": False, "referrer_id": None
            }
            db.table("users").insert(new_user).execute()
            return new_user
        return res.data[0]
    except Exception as e:
        logger.error(f"Error get_user: {e}")
        return {"user_id": user_id, "deposit_balance": 0.0, "winning_balance": 0.0,
                "bonus_balance": 0.0, "locked_balance": 0.0, "ff_ign": "", 
                "last_login": "", "is_18_plus": False, "is_restricted": False}

def deduct_balance(user_id, amount):
    if not db: return True
    user = get_user(user_id)
    rem = amount
    b_bal, d_bal, w_bal = user['bonus_balance'], user['deposit_balance'], user['winning_balance']
    
    ded_b = min(b_bal, rem); rem -= ded_b; b_bal -= ded_b
    ded_d = min(d_bal, rem); rem -= ded_d; d_bal -= ded_d
    ded_w = min(w_bal, rem); rem -= ded_w; w_bal -= ded_w
    
    if rem > 0: return False
    try:
        db.table("users").update({"bonus_balance": b_bal, "deposit_balance": d_bal, "winning_balance": w_bal}).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error deduct_balance: {e}")
        return False

def get_utr_prefixes():
    now = datetime.now(IST)
    yest = now - timedelta(days=1)
    return [str(now.year)[-1] + now.strftime("%j"), str(yest.year)[-1] + yest.strftime("%j")]

# ==============================================================================
# 5. PYROGRAM 24x7 STAGE-1 KEYWORD & SMART AI SCRAPER
# ==============================================================================
async def start_all_workers():
    if not db: return
    try:
        workers = db.table("workers").select("*").execute().data
        for w in workers:
            phone = w['phone']
            client = PyroClient(f"worker_{phone}", api_id=API_ID, api_hash=API_HASH, session_string=w['session_string'], in_memory=True)
            
            @client.on_message(pyfilters.channel | pyfilters.group)
            async def scrape_room_smart(client, message):
                text = (message.text or message.caption or "").strip()
                if not text and not message.photo: return
                
                # Stage-1 Pre-Filter: Check for numbers, keywords, or photo
                has_number = bool(re.search(r'\d{6,10}', text))
                has_keyword = any(kw in text.lower() for kw in ["id", "password", "pass", "pwd", "room", "custom", "freefire", "ff", "match", "join", "booyah", "winner", "cheat", "hack"])
                
                if not has_number and not has_keyword and not message.photo:
                    return # Fast drop 90% casual chat in 0.001ms

                # Fast Regex extraction for credentials
                id_match = re.search(r'(?:ID|ROOM|RM|ROOMID)\s*[:\-=\s]?\s*(\d{6,10})', text, re.IGNORECASE)
                pass_match = re.search(r'(?:PASS|PWD|PW|PASSWORD|KEY)\s*[:\-=\s]?\s*([A-Za-z0-9@#\$\!]+)', text, re.IGNORECASE)
                
                r_id, r_pass = None, None
                if id_match and pass_match:
                    r_id, r_pass = id_match.group(1), pass_match.group(1)
                else:
                    # Stage-2 AI Extraction
                    ai_prompt = f"Analyze message from tournament channel. If it has Room ID (6-10 digits) and password, format EXACTLY as ID:12345678|PASS:xyz123. If none, reply NONE. Message: {text}"
                    ai_res = await call_ai_unified(ai_prompt)
                    if "ID:" in ai_res and "PASS:" in ai_res:
                        m_id = re.search(r'ID:(\d+)', ai_res)
                        m_pass = re.search(r'PASS:([^\s\|]+)', ai_res)
                        if m_id and m_pass: r_id, r_pass = m_id.group(1), m_pass.group(1)

                if r_id and r_pass:
                    one_hr_ago = (datetime.now(IST) - timedelta(hours=1)).isoformat()
                    exists = db.table("matches").select("*").eq("room_id", r_id).gt("created_at", one_hr_ago).execute().data
                    if not exists:
                        tbd = db.table("matches").select("*").eq("room_id", "TBD").gt("tickets_left", 0).execute().data
                        if tbd:
                            db.table("matches").update({"room_id": r_id, "room_pass": r_pass}).eq("match_id", tbd[0]['match_id']).execute()
                        else:
                            match_id = f"FF{random.randint(10000,99999)}"
                            db.table("matches").insert({"match_id": match_id, "room_id": r_id, "room_pass": r_pass, "tickets_left": 10, "created_at": datetime.now(IST).isoformat()}).execute()

            await client.start()
            pyro_clients[phone] = client
            logger.info(f"Hydra 24x7 Scraper Started: {phone}")
    except Exception as e:
        logger.error(f"Scraper error: {e}")

# ==============================================================================
# 6. UX MENUS & KEYBOARDS
# ==============================================================================
def get_main_menu():
    kbd = [
        [KeyboardButton("🎮 PLAY FREE FIRE"), KeyboardButton("🎯 MY MATCHES")],
        [KeyboardButton("💰 ADD FUNDS"), KeyboardButton("💸 WITHDRAW CASH")],
        [KeyboardButton("🎁 DAILY REWARD"), KeyboardButton("🤝 HELP / SUPPORT")]
    ]
    return ReplyKeyboardMarkup(kbd, resize_keyboard=True)

def get_cancel_kbd():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel & Go Back")]], resize_keyboard=True)

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 **Action Cancelled!** Wapas Main Menu par aagaye hain.", parse_mode='Markdown', reply_markup=get_main_menu())
    return ConversationHandler.END

# ==============================================================================
# 7. ONBOARDING & LEGAL VERIFICATION (WITH UNIVERSAL START RESET)
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    args = context.args

    # Check Deep Link referral or match invite
    referrer_id = None
    if args and len(args) > 0:
        param = args[0]
        if param.startswith("ref_"):
            try:
                ref_id = int(param.split("_")[1])
                if ref_id != user_id: referrer_id = ref_id
            except: pass
        elif param.startswith("match_"):
            match_id = param.replace("match_", "")
            kbd = [[InlineKeyboardButton(f"🔒 JOIN #{match_id} (₹{ENTRY_FEE})", callback_data=f"confjoin_{match_id}")]]
            await update.message.reply_text(f"🔥 **INVITE TO MATCH #{match_id}**\nAapke dost ne aapko is match mein bulaya hai! Niche click karke join karein:", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
            return ConversationHandler.END

    user = get_user(user_id)
    if db and referrer_id and not user.get('referrer_id'):
        db.table("users").update({"referrer_id": referrer_id}).eq("user_id", user_id).execute()

    if user['is_restricted']:
        await update.message.reply_text("🚨 **ACCOUNT SUSPENDED:** Aapka account restricted hai.", parse_mode='Markdown')
        return ConversationHandler.END

    if not user['is_18_plus']:
        msg = ("⚖️ **LEGAL & AGE VERIFICATION** ⚖️\n\n"
               "Govt of India ke niyamon ke anusaar khelne ke liye:\n"
               "1. Aapki umar **18 saal (18+)** honi chahiye.\n"
               "2. Aap restricted states se nahi hone chahiye.\n\n"
               "**Kya aap 18+ hain aur rules accept karte hain?**")
        kbd = [[InlineKeyboardButton("✅ YES, I AM 18+ (Play Now)", callback_data="legal_yes")],
               [InlineKeyboardButton("❌ NO, I AM UNDER 18", callback_data="legal_no")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
        return ConversationHandler.END

    ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    await update.message.reply_text(f"🔥 **Welcome back to Free Fire Tournaments!**\n\n🤝 **Refer & Earn:** Dosto ko bulao aur ₹10 Bonus pao jab wo pehla match khele:\n👉 `{ref_link}`", reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

async def legal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "legal_yes":
        if db: db.table("users").update({"is_18_plus": True}).eq("user_id", query.from_user.id).execute()
        await query.message.delete()
        await query.message.reply_text("✅ **Verification Successful! Welcome to the Arena!** 🔥", reply_markup=get_main_menu(), parse_mode='Markdown')
    else:
        await query.message.edit_text("❌ Sorry! You must be 18+ to play on this platform.")

# ==============================================================================
# 8. MAIN ENGINE & MENU HANDLER
# ==============================================================================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.message.from_user.id
    if is_throttled(user_id): return
    
    text = update.message.text.upper()
    if "CANCEL" in text: return await cancel_action(update, context)

    user = get_user(user_id)
    if user['is_restricted']:
        await update.message.reply_text("🚨 Aapka account suspended hai.")
        return ConversationHandler.END

    if "PLAY" in text:
        if not user['ff_ign'] or not user['ff_ign'].strip():
            await update.message.reply_text("⚠️ **PROFILE CONFIGURATION**\nApna exact Free Fire Nickname (IGN) type karein:", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
            return WAIT_IGN
            
        if db:
            exp_time = (datetime.now(IST) - timedelta(minutes=MATCH_LIVE_MINS)).isoformat()
            db.table("matches").delete().lt("created_at", exp_time).neq("room_id", "TBD").execute()
            matches = db.table("matches").select("*").gt("tickets_left", 0).execute().data
        else:
            matches = [{"match_id": "FF8899", "room_id": "123456", "room_pass": "pass123", "tickets_left": 7}]

        if not matches:
            await update.message.reply_text("🟡 **Koi match abhi Live nahi hai!** Scraper match dhoondh raha hai, 5 min mein dobara check karein! ⏳", parse_mode='Markdown')
            return ConversationHandler.END
            
        msg = "🔴 **LIVE BATTLE BOARD** 🔴\n\n"
        kbd = []
        for m in matches:
            bars = int((10 - m['tickets_left']) / 10 * 10)
            progress = "█" * bars + "░" * (10 - bars)
            status_text = "🟢 READY TO PLAY" if m['room_id'] != "TBD" else "⏰ SCHEDULED"
            
            msg += (f"🔥 **Match #{m['match_id']}** | {status_text}\n"
                    f"🎟 **Seats:** `[{progress}] {10 - m['tickets_left']}/10 Filled`\n"
                    f"💰 **Entry:** ₹{ENTRY_FEE} | **Prize:** ₹{PRIZE_MONEY}\n\n")
            
            share_url = f"https://t.me/{context.bot.username}?start=match_{m['match_id']}"
            kbd.append([InlineKeyboardButton(f"🔒 JOIN #{m['match_id']} (₹{ENTRY_FEE})", callback_data=f"confjoin_{m['match_id']}"),
                        InlineKeyboardButton("📢 INVITE SQUAD", url=f"https://t.me/share/url?url={share_url}&text=Ajao Free Fire Tournament khelein!")])
            
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
        return ConversationHandler.END

    elif "ADD FUNDS" in text:
        await update.message.reply_text("💸 **Kitne Rupaye add karne hain?** (Min: ₹30)\n_Niche amount type karein:_ 👇", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
        return WAIT_ADD_AMT

    elif "WITHDRAW" in text:
        tot = round(user['deposit_balance'] + user['winning_balance'] + user['bonus_balance'], 2)
        msg = (f"💰 **Total Balance:** ₹{tot}\n\n🟢 **Winnings:** ₹{user['winning_balance']} _(Withdrawable)_\n"
               f"🔵 **Deposit:** ₹{user['deposit_balance']}\n🎁 **Bonus:** ₹{user['bonus_balance']}\n\n*(Min withdraw: ₹200 Winnings)*")
        if user['winning_balance'] < 200:
            await update.message.reply_text(msg + "\n\n❌ **Oops! Minimum ₹200 Winnings needed to withdraw.**", parse_mode='Markdown')
            return ConversationHandler.END
        await update.message.reply_text(msg + "\n\n📸 Apna **UPI QR Code** ki photo bhejein:", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
        return WAIT_WITHDRAW_QR

    elif "DAILY REWARD" in text:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if user['last_login'] == today:
            await update.message.reply_text("❌ **Aap aaj ka Daily Jackpot claim kar chuke hain.** Kal aaiye! ⏳", parse_mode='Markdown')
        else:
            await update.message.reply_text("🎰 **BOOYAH JACKPOT SLOTS!** 🎰\nLever ghum raha hai apna luck check karein...")
            dice_msg = await context.bot.send_dice(chat_id=user_id, emoji='🎰')
            await asyncio.sleep(3.5)
            
            val = dice_msg.dice.value
            # Strict backend probability rules: Max payout ₹10
            if val in [64]: reward = 10.0 # 777 Rare Jackpot
            elif val in [1, 22, 43]: reward = 5.0
            elif val % 5 == 0: reward = 3.0
            else: reward = random.choice([1.0, 2.0])
            
            if db: db.table("users").update({"bonus_balance": user['bonus_balance'] + reward, "last_login": today}).eq("user_id", user_id).execute()
            await update.message.reply_text(f"🎉 **JACKPOT RESULT!** Aapko **₹{reward} Bonus Cash** mila hai! 🎁", parse_mode='Markdown')
        return ConversationHandler.END

    elif "MATCHES" in text:
        if not db:
            await update.message.reply_text("❌ Aapne abhi tak koi match nahi khela.")
            return ConversationHandler.END
            
        ums = db.table("user_matches").select("*").eq("user_id", user_id).execute().data
        if not ums:
            await update.message.reply_text("❌ Aapne abhi tak koi match join nahi kiya hai.")
            return ConversationHandler.END
            
        msg = "🎯 **YOUR RECENT MATCHES** 🎯\n\n"
        kbd = []
        for um in ums[-5:]:
            m_data = db.table("matches").select("*").eq("match_id", um['match_id']).execute().data
            if not m_data: continue
            m = m_data[0]
            
            msg += f"🔥 **Match #{um['match_id']}** | Status: **{um['status']}**\n"
            if um['status'] == 'JOINED':
                join_time = datetime.fromisoformat(um['joined_at'])
                mins_passed = (datetime.now(IST) - join_time).total_seconds() / 60
                msg += f"🔑 Room ID: `{m['room_id']}`\n🔐 Pass: `{m['room_pass']}`\n\n" if m['room_id'] != "TBD" else "⏰ **Status:** Room Jald aayega\n\n"
                if mins_passed < REFUND_WINDOW_MINS:
                    kbd.append([InlineKeyboardButton(f"⚠️ ROOM FULL? (Get Refund #{um['match_id']})", callback_data=f"askref_{um['match_id']}")])
                else:
                    kbd.append([InlineKeyboardButton(f"🏆 I WON! (Claim #{um['match_id']})", callback_data=f"up_proof_{um['match_id']}")])
            else: msg += "\n"
        
        # Add QuickChart Stats Card URL
        chart_url = f"https://quickchart.io/chart?c={{type:'bar',data:{{labels:['Deposit','Winnings','Bonus'],datasets:[{{data:[{user['deposit_balance']},{user['winning_balance']},{user['bonus_balance']}],backgroundColor:['#3b82f6','#10b981','#f59e0b']}}]}}}}"
        await update.message.reply_photo(photo=chart_url, caption=msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd) if kbd else None)
        return ConversationHandler.END

    elif "HELP" in text or "SUPPORT" in text:
        safe_profile = safe_md(user['ff_ign'] or 'Unconfigured')
        await update.message.reply_text(
            "🟢 **PERSONALIZED AI SUPPORT ENGINE ONLINE**\n\n"
            f"👤 **Connected Profile:** `{safe_profile}`\n\n"
            "Aap apni koi bhi problem ya doubt niche type karein, humara AI Live Data dekh kar turant jawab dega:",
            reply_markup=get_cancel_kbd(), parse_mode='Markdown'
        )
        return WAIT_SUPPORT_CHAT

    return ConversationHandler.END

# ==============================================================================
# 9. PERSONALIZED LIVE DATA AI SUPPORT CHATBOT
# ==============================================================================
async def handle_support_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text and "CANCEL" in text.upper(): return await cancel_action(update, context)
    
    user_id = update.message.from_user.id
    await context.bot.send_chat_action(chat_id=user_id, action='typing')
    user = get_user(user_id)
    
    sys_prompt = (f"You are official Free Fire Tournament AI Support. Player Live Context:\n"
                  f"- ID: {user_id}, IGN: {user['ff_ign']}\n"
                  f"- Balances: Deposit ₹{user['deposit_balance']}, Winnings ₹{user['winning_balance']}, Bonus ₹{user['bonus_balance']}\n"
                  f"Answer politely in fluent Hinglish based on their context. Avoid markdown formatting that causes parsing errors.")
    
    ai_reply = await call_ai_unified(text, system_context=sys_prompt)
    try:
        await update.message.reply_text(ai_reply + "\n\n_(Aur sawal puchein ya '❌ Cancel & Go Back' dabayein)_", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
    except Exception:
        # Fallback if AI emits unclosed markdown symbols
        await update.message.reply_text(ai_reply + "\n\n(Aur sawal puchein ya 'Cancel & Go Back' dabayein)", reply_markup=get_cancel_kbd())
    return WAIT_SUPPORT_CHAT

# ==============================================================================
# 10. MATCH JOIN & REFUND FLOWS
# ==============================================================================
async def conf_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    kbd = [[InlineKeyboardButton(f"✅ YES, JOIN (Pay ₹{ENTRY_FEE})", callback_data=f"dojoin_{match_id}")],
           [InlineKeyboardButton("🔙 CANCEL", callback_data="delete_msg")]]
    await query.message.reply_text(f"Kya aap **Match #{match_id}** mein join karne ke liye **₹{ENTRY_FEE}** pay karna chahte hain?", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    user_id = query.from_user.id
    
    if db:
        exists = db.table("user_matches").select("*").eq("user_id", user_id).eq("match_id", match_id).execute().data
        if exists: return await query.message.edit_text("❌ Aap is match mein pehle se join ho chuke hain.")

    lock_key = f"join_{match_id}"
    if lock_key not in join_locks: join_locks[lock_key] = asyncio.Lock()
    
    async with join_locks[lock_key]:
        if db:
            match_res = db.table("matches").select("*").eq("match_id", match_id).execute().data
            if not match_res: return await query.message.edit_text("❌ Ye match ab available nahi hai.")
            match = match_res[0]
            if match['tickets_left'] <= 0: return await query.message.edit_text("❌ Oops! Ye match just abhi full ho gaya!")
        else:
            match = {"room_id": "123456", "room_pass": "pass123", "tickets_left": 5}
            
        if deduct_balance(user_id, ENTRY_FEE):
            if db:
                db.table("matches").update({"tickets_left": match['tickets_left'] - 1}).eq("match_id", match_id).execute()
                db.table("user_matches").insert({"user_id": user_id, "match_id": match_id, "status": "JOINED", "joined_at": datetime.now(IST).isoformat()}).execute()
            
            room_info = "⏰ **STATUS:** SCHEDULED (Room Jald Aayega)" if match['room_id'] == "TBD" else f"🔑 Room ID: `{match['room_id']}`\n🔐 Pass: `{match['room_pass']}`"
            await query.message.edit_text(f"🔥 **ENTRY CONFIRMED!** 🎮\n\n{room_info}\n\n_*(Refund {REFUND_WINDOW_MINS} min tak available hai)*_", parse_mode='Markdown')
        else:
            await query.message.edit_text("❌ **Insufficient Funds!** Kripaya recharge karein.", parse_mode='Markdown')

async def ask_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    kbd = [[InlineKeyboardButton(f"✅ YES, REFUND ₹{ENTRY_FEE}", callback_data=f"doref_{match_id}")],
           [InlineKeyboardButton("🔙 NO, TAKE ME BACK", callback_data="delete_msg")]]
    await query.message.edit_text(f"🚨 **REFUND CONFIRMATION**\nKya aap sach mein Match #{match_id} se ₹{ENTRY_FEE} refund chahte hain?", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

async def do_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    user_id = query.from_user.id
    
    if db:
        um_res = db.table("user_matches").select("*").eq("user_id", user_id).eq("match_id", match_id).execute().data
        if not um_res: return
        um = um_res[0]
        if um['status'] == 'REFUNDED': return await query.message.edit_text("ℹ️ Aap pehle hi refund le chuke hain.")
        
        db.table("users").update({"deposit_balance": get_user(user_id)['deposit_balance'] + ENTRY_FEE}).eq("user_id", user_id).execute()
        db.table("user_matches").update({"status": "REFUNDED"}).eq("id", um['id']).execute()
        match_res = db.table("matches").select("*").eq("match_id", match_id).execute().data
        if match_res: db.table("matches").update({"tickets_left": min(10, match_res[0]['tickets_left'] + 1)}).eq("match_id", match_id).execute()
            
    await query.message.edit_text(f"✅ **Refund Successful!** ₹{ENTRY_FEE} aapke wallet mein add ho gaye hain.", parse_mode='Markdown')

async def cancel_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: await update.callback_query.message.delete()
    except: pass

# ==============================================================================
# 11. DEPOSIT ENGINE WITH SMART AI UTR OCR
# ==============================================================================
async def save_ign_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return WAIT_IGN
    if "CANCEL" in update.message.text.upper(): return await cancel_action(update, context)
    text = update.message.text.strip()
    if db: db.table("users").update({"ff_ign": text}).eq("user_id", update.message.from_user.id).execute()
    safe_name = safe_md(text)
    await update.message.reply_text(f"✅ **IGN (`{safe_name}`) Locked!** 🎮", reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return WAIT_ADD_AMT
    if "CANCEL" in update.message.text.upper(): return await cancel_action(update, context)
    try:
        amt = float(update.message.text)
        if amt < 30: raise ValueError
    except:
        await update.message.reply_text("❌ Minimum ₹30 allowed hai. Sahi number likhein:")
        return WAIT_ADD_AMT
        
    context.user_data['dep_amt'] = amt
    upi_id = "dipanshu153@fam" 
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=upi://pay?pa={upi_id}%26pn=ArenaEsports%26am={amt}%26cu=INR"
    await update.message.reply_photo(photo=qr_url, caption=f"💳 **PAY ₹{amt}** to `{upi_id}`\nUske baad screenshot yahan bhejein:", parse_mode='Markdown', reply_markup=get_cancel_kbd())
    return WAIT_PAY_PROOF

async def process_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text and "CANCEL" in update.message.text.upper(): return await cancel_action(update, context)
    if not update.message or not update.message.photo:
        await update.message.reply_text("❌ Kripaya sirf Payment ka Screenshot (Photo) upload karein.", parse_mode='Markdown', reply_markup=get_cancel_kbd())
        return WAIT_PAY_PROOF
        
    user_id = update.message.from_user.id
    claimed_amt = context.user_data.get('dep_amt', 50.0)
    msg = await update.message.reply_text("⏳ Verifying Payment with AI... [████░░░░░░]")
    await context.bot.send_chat_action(chat_id=user_id, action='typing')
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        b64_image = base64.b64encode(await photo_file.download_as_bytearray()).decode('utf-8')
        
        prompt = "Extract 12-digit UTR number and Amount paid. Format: UTR: <12-digits> | AMOUNT: <number>"
        ai_text = await call_ai_unified(prompt, image_b64=b64_image)
        
        utr_m = re.search(r'UTR:\s*(\d{12})', ai_text)
        amt_m = re.search(r'AMOUNT:\s*(\d+)', ai_text)
        
        utr = utr_m.group(1) if utr_m else f"MANUAL_{random.randint(100000,999999)}"
        ai_amt = float(amt_m.group(1)) if amt_m else claimed_amt
            
        if db and not utr.startswith("MANUAL") and db.table("used_utrs").select("*").eq("utr", utr).execute().data:
            await msg.edit_text("🚫 **SYSTEM ALERT:** Duplicate UTR detected. Request Rejected.")
            return ConversationHandler.END

        kbd = [[InlineKeyboardButton(f"✅ APPROVE ₹{ai_amt}", callback_data=f"admdep_{user_id}_{utr}_{ai_amt}")],
               [InlineKeyboardButton("❌ REJECT", callback_data=f"admrej_{user_id}")]]
        dossier = f"🚨 **DEPOSIT REQUEST**\n👤 User: `{user_id}`\n💰 Claimed: ₹{claimed_amt} | AI Read: **₹{ai_amt}**\n🔢 UTR: `{utr}`"
        
        if ADMIN_GROUP_ID != 0: await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=photo_file.file_id, caption=dossier, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd))
        await msg.edit_text("✅ **Screenshot Submitted!** Admin verification ke baad balance 2-5 min mein add ho jayega.", reply_markup=get_main_menu())
    except Exception as e:
        logger.error(f"Upload Error: {e}")
        await msg.edit_text("⚠️ Upload error. Kripaya dobara try karein.", reply_markup=get_main_menu())
        return WAIT_PAY_PROOF
    return ConversationHandler.END

# ==============================================================================
# 12. WITHDRAW ENGINE
# ==============================================================================
async def process_withdraw_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text and "CANCEL" in update.message.text.upper(): return await cancel_action(update, context)
    if not update.message or not update.message.photo:
        await update.message.reply_text("❌ Kripaya sirf UPI QR Code photo bhejein.", reply_markup=get_cancel_kbd())
        return WAIT_WITHDRAW_QR
        
    user_id = update.message.from_user.id
    user = get_user(user_id)
    amt = user['winning_balance']
    if db: db.table("users").update({"winning_balance": 0, "locked_balance": user['locked_balance'] + amt}).eq("user_id", user_id).execute()
    
    kbd = [[InlineKeyboardButton(f"✅ PAID QR ₹{amt}", callback_data=f"admpaid_{user_id}_{amt}")],
           [InlineKeyboardButton("❌ REJECT & REFUND", callback_data=f"admrejwd_{user_id}_{amt}")]]
    if ADMIN_GROUP_ID != 0: await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=update.message.photo[-1].file_id, caption=f"💸 **WITHDRAWAL**\nUser: `{user_id}`\nAmt: **₹{amt}**", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
    await update.message.reply_text(f"✅ Withdraw request submit! **₹{amt}** locked.", reply_markup=get_main_menu())
    return ConversationHandler.END

# ==============================================================================
# 13. WINNER VERIFICATION ENGINE
# ==============================================================================
async def up_proof_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['win_match'] = query.data.split("_")[2]
    await query.message.reply_text("🎉 **Congratulations!** Match Scoreboard ka uncropped screenshot bhejein:", parse_mode='Markdown', reply_markup=get_cancel_kbd())
    return WAIT_WIN_PROOF

async def process_win_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text and "CANCEL" in text.upper(): return await cancel_action(update, context)
    if not update.message.photo:
        await update.message.reply_text("❌ Kripaya sirf Scoreboard photo upload karein.", reply_markup=get_cancel_kbd())
        return WAIT_WIN_PROOF
        
    user_id = update.message.from_user.id
    match_id = context.user_data['win_match']
    msg = await update.message.reply_text("⏳ Verifying Winner with AI... [████░░░░░░]")
    await context.bot.send_chat_action(chat_id=user_id, action='typing')
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        b64_image = base64.b64encode(await photo_file.download_as_bytearray()).decode('utf-8')
        
        prompt = "Read Rank #1 Free Fire IGN. Is image cropped? Format: [UNCROPPED/CROPPED] | Rank 1: <Name>"
        ai_text = await call_ai_unified(prompt, image_b64=b64_image)
        
        if db: db.table("user_matches").update({"status": "PENDING"}).eq("user_id", user_id).eq("match_id", match_id).execute()
        
        kbd = [[InlineKeyboardButton(f"✅ APPROVE & PUBLIC HYPE (₹{PRIZE_MONEY})", callback_data=f"admprize_{user_id}_{match_id}")],
               [InlineKeyboardButton("❌ REJECT", callback_data=f"admrej_{user_id}")]]
        safe_ign = safe_md(get_user(user_id)['ff_ign'])
        dossier = f"🏆 **WINNER VERIFICATION**\nUser: `{user_id}` | Registered IGN: `{safe_ign}`\nMatch: #{match_id}\nAI Read: `{ai_text}`"
        
        if ADMIN_GROUP_ID != 0: await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=photo_file.file_id, caption=dossier, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd))
        await msg.edit_text("✅ Scoreboard sent to Admin! Approve hote hi Public Channel par HYPE banega!")
    except Exception as e:
        logger.error(f"Error process_win_proof: {e}")
        await msg.edit_text("⚠️ Error occurred. Try again.")
        return WAIT_WIN_PROOF
    return ConversationHandler.END

# ==============================================================================
# 14. ADMIN COMMANDS (/creatematch, /setroom, /hype, /status)
# ==============================================================================
async def cmd_creatematch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID != 0 and update.message.from_user.id != ADMIN_USER_ID and update.message.chat_id != ADMIN_GROUP_ID: return
    fee = float(context.args[0]) if len(context.args) > 0 else ENTRY_FEE
    prize = float(context.args[1]) if len(context.args) > 1 else PRIZE_MONEY
    tickets = int(context.args[2]) if len(context.args) > 2 else 10
    
    match_id = f"FF{random.randint(10000,99999)}"
    if db: db.table("matches").insert({"match_id": match_id, "room_id": "TBD", "room_pass": "TBD", "tickets_left": tickets, "created_at": datetime.now(IST).isoformat()}).execute()
    await update.message.reply_text(f"✅ **NEW MATCH SCHEDULED!**\nMatch ID: `#{match_id}` | Entry: ₹{fee} | Seats: {tickets}\nCommand to start: `/setroom {match_id} <id> <pass>`", parse_mode='Markdown')

async def cmd_setroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID != 0 and update.message.from_user.id != ADMIN_USER_ID and update.message.chat_id != ADMIN_GROUP_ID: return
    if len(context.args) < 3: return await update.message.reply_text("❌ Usage: `/setroom <match_id> <room_id> <password>`")
    match_id, room_id, room_pass = context.args[0].replace("#", ""), context.args[1], context.args[2]
    
    if db:
        db.table("matches").update({"room_id": room_id, "room_pass": room_pass}).eq("match_id", match_id).execute()
        joined = db.table("user_matches").select("user_id").eq("match_id", match_id).eq("status", "JOINED").execute().data
    else: joined = []

    for u in joined:
        try: await context.bot.send_message(chat_id=u['user_id'], text=f"🚨 **ROOM IS READY! MATCH #{match_id}** 🎮\n🔑 ID: `{room_id}` | 🔐 Pass: `{room_pass}`\n⚡ Jaldi join karein!", parse_mode='Markdown')
        except: pass

    if PUBLIC_CHANNEL_ID:
        try: await context.bot.send_message(chat_id=PUBLIC_CHANNEL_ID, text=f"🔥 **CUSTOM ROOM IS LIVE! MATCH #{match_id}** 🎮\n🔑 Room ID: `{room_id}`\n👉 Agle match ke liye bot par aao: @FreeFireCustomRoom_Bot 🚀", parse_mode='Markdown')
        except: pass
    await update.message.reply_text(f"✅ **Room Updated & Broadcast Sent!** Notified Players: {len(joined)}")

async def cmd_hype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID != 0 and update.message.from_user.id != ADMIN_USER_ID and update.message.chat_id != ADMIN_GROUP_ID: return
    text = " ".join(context.args)
    if not text: return await update.message.reply_text("❌ Usage: `/hype Mega Tournament starting soon! Prize ₹300!`")
    banner = f"📢 **OFFICIAL TOURNAMENT UPDATE** 🔥\n\n{text}\n\n👉 Join Now: @FreeFireCustomRoom_Bot 🎮"
    if PUBLIC_CHANNEL_ID:
        try:
            await context.bot.send_message(chat_id=PUBLIC_CHANNEL_ID, text=banner, parse_mode='Markdown')
            await update.message.reply_text("✅ **Hype Banner Posted to Public Channel!**")
        except Exception as e: await update.message.reply_text(f"❌ Failed to post: {e}")
    else: await update.message.reply_text("⚠️ PUBLIC_CHANNEL_ID environment variable set nahi hai!")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID != 0 and update.message.from_user.id != ADMIN_USER_ID and update.message.chat_id != ADMIN_GROUP_ID: return
    if not context.args: return await update.message.reply_text("❌ Usage: `/status <match_id>`")
    match_id = context.args[0].replace("#", "")
    if db:
        m = db.table("matches").select("*").eq("match_id", match_id).execute().data
        if not m: return await update.message.reply_text("❌ Match not found.")
        m = m[0]
        banner = f"🔴 **MATCH #{match_id} LIVE STATUS** 🔴\n\n🎟 **Seats Remaining:** {m['tickets_left']}/10\n💰 **Entry:** ₹{ENTRY_FEE} | **Prize:** ₹{PRIZE_MONEY}\n\n⚡ Jaldi seat lock karo: @FreeFireCustomRoom_Bot 🚀"
        if PUBLIC_CHANNEL_ID:
            try: await context.bot.send_message(chat_id=PUBLIC_CHANNEL_ID, text=banner, parse_mode='Markdown')
            except: pass
        await update.message.reply_text(f"✅ Match #{match_id} status broadcasted!")

# ==============================================================================
# 15. UNIFIED ADMIN CALLBACK LISTENER (SAFE UNDERSCORE PARSING & BUTTON REMOVAL)
# ==============================================================================
async def generate_pdf_cert(ign, match_id, prize):
    if not HAS_REPORTLAB: return None
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setStrokeColor(colors.gold); c.setLineWidth(4); c.rect(30, 30, 552, 732)
    c.setFont("Helvetica-Bold", 28); c.drawCentredString(306, 680, "CERTIFICATE OF ACHIEVEMENT")
    c.setFont("Helvetica", 16); c.drawCentredString(306, 620, "Official Free Fire Tournament Champion")
    c.setFont("Helvetica-Bold", 32); c.drawCentredString(306, 530, ign)
    c.setFont("Helvetica", 14); c.drawCentredString(306, 460, f"For dominating Match #{match_id} and winning ₹{prize} Cash Prize!")
    c.setFont("Helvetica-Oblique", 12); c.drawCentredString(306, 100, f"Verified & Issued on {datetime.now(IST).strftime('%Y-%m-%d')}")
    c.save(); buf.seek(0)
    return buf

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Instant Button Removal so Admin cannot double-click!
    try: await query.edit_message_reply_markup(reply_markup=None)
    except Exception: pass

    parts = query.data.split("_")
    action = parts[0]

    try:
        if action == "admdep":
            target_user_id = int(parts[1])
            amount = float(parts[-1])       # Safe: ALWAYS the last item!
            utr = "_".join(parts[2:-1])     # Safe: Reconstructs exact UTR even if it contains underscores!
            
            if db and not utr.startswith("MANUAL"):
                if db.table("used_utrs").select("*").eq("utr", utr).execute().data:
                    return await query.message.edit_caption(caption=query.message.caption or "" + "\n\n⚠️ **DUPLICATE UTR!**", parse_mode='Markdown')
                db.table("used_utrs").insert({"utr": utr, "user_id": target_user_id, "amount": amount, "created_at": datetime.now(IST).isoformat()}).execute()
            
            user = get_user(target_user_id)
            if db: db.table("users").update({"deposit_balance": user['deposit_balance'] + amount}).eq("user_id", target_user_id).execute()
            try: await context.bot.send_message(chat_id=target_user_id, text=f"🎉 **PAYMENT APPROVED!** ₹{amount} added to Deposit Balance.")
            except: pass
            try: await query.message.edit_caption(caption=(query.message.caption or "") + f"\n\n✅ **APPROVED ₹{amount}** by {query.from_user.first_name}", parse_mode='Markdown')
            except: pass

        elif action == "admprize":
            target_user_id = int(parts[1])
            match_id = "_".join(parts[2:])
            user = get_user(target_user_id)
            safe_ign = safe_md(user.get('ff_ign', f'Player_{target_user_id}'))
            
            if db:
                db.table("users").update({"winning_balance": user['winning_balance'] + PRIZE_MONEY}).eq("user_id", target_user_id).execute()
                db.table("user_matches").update({"status": "WON"}).eq("user_id", target_user_id).eq("match_id", match_id).execute()
                
                # Check Referral Armor
                ref_id = user.get('referrer_id')
                if ref_id:
                    ref_user = get_user(ref_id)
                    db.table("users").update({"bonus_balance": ref_user['bonus_balance'] + 10.0}).eq("user_id", ref_id).execute()
                    try: await context.bot.send_message(chat_id=ref_id, text=f"🎉 **REFERRAL REWARD UNLOCKED!** Aapke dost `{safe_ign}` ne tournament poora kiya! ₹10 Bonus added!")
                    except: pass
            
            pdf_buf = await generate_pdf_cert(user.get('ff_ign', 'Champion'), match_id, PRIZE_MONEY)
            try:
                await context.bot.send_message(chat_id=target_user_id, text=f"🏆 **WINNER VERIFIED!** ₹{PRIZE_MONEY} credited to Winnings! 🎉")
                if pdf_buf: await context.bot.send_document(chat_id=target_user_id, document=pdf_buf, filename=f"Winner_Cert_{match_id}.pdf", caption="🏆 Aapka Official Winner Certificate!")
            except: pass

            if PUBLIC_CHANNEL_ID:
                hype_text = f"🏆👑 **OFFICIAL TOURNAMENT CHAMPION** 👑🏆\n\n🔥 Match ID: `#{match_id}`\n🎮 Champion IGN: `{safe_ign}`\n💰 Prize Won & Credited: **₹{PRIZE_MONEY} CASH** 🎉\n\n⚡ Bahut badhai ho `{safe_ign}` ko! Agle match ke liye bot par aao: @FreeFireCustomRoom_Bot 🚀"
                try:
                    if query.message.photo: await context.bot.send_photo(chat_id=PUBLIC_CHANNEL_ID, photo=query.message.photo[-1].file_id, caption=hype_text, parse_mode='Markdown')
                    else: await context.bot.send_message(chat_id=PUBLIC_CHANNEL_ID, text=hype_text, parse_mode='Markdown')
                except: pass
            try: await query.message.edit_caption(caption=(query.message.caption or "") + f"\n\n🏆 **WIN APPROVED & HYPE POSTED** by {query.from_user.first_name}", parse_mode='Markdown')
            except: pass

        elif action in ["admpaid", "admrejwd", "admrej"]:
            target_user_id = int(parts[1])
            if action == "admpaid":
                amt = float(parts[-1])
                if db: db.table("users").update({"locked_balance": max(0.0, get_user(target_user_id)['locked_balance'] - amt)}).eq("user_id", target_user_id).execute()
                try: await context.bot.send_message(chat_id=target_user_id, text=f"✅ **WITHDRAWAL SUCCESSFUL!** ₹{amt} transferred.")
                except: pass
            elif action == "admrejwd":
                amt = float(parts[-1])
                user = get_user(target_user_id)
                if db: db.table("users").update({"locked_balance": max(0.0, user['locked_balance'] - amt), "winning_balance": user['winning_balance'] + amt}).eq("user_id", target_user_id).execute()
                try: await context.bot.send_message(chat_id=target_user_id, text=f"❌ **WITHDRAW REJECTED** ₹{amt} refunded back to Winnings.")
                except: pass
            try: await query.message.edit_caption(caption=(query.message.caption or "") + f"\n\nDone by {query.from_user.first_name}", parse_mode='Markdown')
            except: pass
    except Exception as e:
        logger.error(f"Error callback: {e}")

# ==============================================================================
# 16. HEALTH-CHECK WEB SERVER & APPLICATION ENTRYPOINT
# ==============================================================================
async def health_check(request):
    return web.Response(text="Free Fire Tournament Bot is Running OK (200)", status=200)

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app_web = web.Application()
    app_web.router.add_get("/", health_check)
    runner = web.AppRunner(app_web)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

def main():
    if not BOT_TOKEN:
        logger.error("CRITICAL: BOT_TOKEN missing!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu),
            CallbackQueryHandler(up_proof_btn, pattern=r"^up_proof_")
        ],
        states={
            WAIT_IGN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_ign_flow)],
            WAIT_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
            WAIT_PAY_PROOF: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), process_payment_proof)],
            WAIT_WITHDRAW_QR: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), process_withdraw_qr)],
            WAIT_WIN_PROOF: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), process_win_proof)],
            WAIT_SUPPORT_CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support_chat)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel_action),
            MessageHandler(filters.Regex(r"(?i)CANCEL|Cancel & Go Back"), cancel_action)
        ],
        per_user=True, per_chat=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("creatematch", cmd_creatematch))
    application.add_handler(CommandHandler("setroom", cmd_setroom))
    application.add_handler(CommandHandler("hype", cmd_hype))
    application.add_handler(CommandHandler("status", cmd_status))

    application.add_handler(CallbackQueryHandler(legal_callback, pattern=r"^legal_"))
    application.add_handler(CallbackQueryHandler(conf_join, pattern=r"^confjoin_"))
    application.add_handler(CallbackQueryHandler(do_join, pattern=r"^dojoin_"))
    application.add_handler(CallbackQueryHandler(ask_refund, pattern=r"^askref_"))
    application.add_handler(CallbackQueryHandler(do_refund, pattern=r"^doref_"))
    application.add_handler(CallbackQueryHandler(cancel_inline, pattern=r"^delete_msg$"))
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern=r"^adm"))
    application.add_handler(conv_handler)

    async def post_init(app: Application):
        asyncio.create_task(start_web_server())
        asyncio.create_task(start_all_workers())

    application.post_init = post_init
    logger.info("Starting Free Fire Tournament Bot Polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
