import os
import re
import base64
import random
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from aiohttp import web
from pyrogram import Client as PyroClient, filters as pyfilters
import pytz

# ==========================================
# 1. ENTERPRISE CONFIG & ENV VARIABLES
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
API_ID = int(os.getenv("API_ID", "1234567"))
API_HASH = os.getenv("API_HASH", "placeholder")
GEMINI_PAYMENT_KEY = os.getenv("GEMINI_PAYMENT_KEY")
GEMINI_MATCH_KEY = os.getenv("GEMINI_MATCH_KEY")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))

IST = pytz.timezone('Asia/Kolkata')
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Locks & Cache
join_locks = {}
pyro_clients = {}
auth_cache = {}

ENTRY_FEE = 50.0
PRIZE_MONEY = 300.0
MATCH_LIVE_MINS = 10
REFUND_WINDOW_MINS = 8

# States
(WAIT_IGN, WAIT_ADD_AMT, WAIT_PAY_PROOF, WAIT_WITHDRAW_QR, WAIT_WIN_PROOF,
 WAIT_WORKER_PHONE, WAIT_WORKER_OTP, WAIT_WORKER_PASS) = range(8)

# ==========================================
# 2. CORE DATABASE MANAGERS
# ==========================================
def get_user(user_id):
    res = db.table("users").select("*").eq("user_id", user_id).execute()
    if not res.data:
        new_user = {
            "user_id": user_id, "deposit_balance": 0.0, "winning_balance": 0.0,
            "bonus_balance": 0.0, "locked_balance": 0.0, "ff_ign": "", 
            "last_login": "", "is_18_plus": False, "is_restricted": False
        }
        db.table("users").insert(new_user).execute()
        return new_user
    return res.data[0]

def deduct_balance(user_id, amount):
    user = get_user(user_id)
    rem = amount
    b_bal, d_bal, w_bal = user['bonus_balance'], user['deposit_balance'], user['winning_balance']
    
    ded_b = min(b_bal, rem); rem -= ded_b; b_bal -= ded_b
    ded_d = min(d_bal, rem); rem -= ded_d; d_bal -= ded_d
    ded_w = min(w_bal, rem); rem -= ded_w; w_bal -= ded_w
    
    if rem > 0: return False
    db.table("users").update({"bonus_balance": b_bal, "deposit_balance": d_bal, "winning_balance": w_bal}).eq("user_id", user_id).execute()
    return True

def get_utr_prefixes():
    now = datetime.now(IST)
    yest = now - timedelta(days=1)
    return [str(now.year)[-1] + now.strftime("%j"), str(yest.year)[-1] + yest.strftime("%j")]

# ==========================================
# 3. AI DUAL-CORE ENGINE
# ==========================================
async def analyze_image(b64_image, prompt, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}]}]}
    try:
        resp = await asyncio.to_thread(requests.post, url, json=payload)
        return resp.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "AI_FAILED"

# ==========================================
# 4. HYDRA SCRAPER ENGINE (Pyrogram)
# ==========================================
async def start_all_workers():
    workers = db.table("workers").select("*").execute().data
    for w in workers:
        phone = w['phone']
        client = PyroClient(f"worker_{phone}", api_id=API_ID, api_hash=API_HASH, session_string=w['session_string'], in_memory=True)
        
        @client.on_message(pyfilters.channel & pyfilters.text)
        async def scrape_room(client, message):
            text = message.text.upper()
            if "ID" in text and ("PASS" in text or "PWD" in text):
                id_match = re.search(r'ID\s*[:\-]?\s*(\d{6,10})', text)
                pass_match = re.search(r'PASS(?:WORD)?\s*[:\-]?\s*([A-Z0-9]+)', text)
                
                if id_match and pass_match:
                    r_id, r_pass = id_match.group(1), pass_match.group(1)
                    one_hr_ago = (datetime.now(IST) - timedelta(hours=1)).isoformat()
                    exists = db.table("matches").select("*").eq("room_id", r_id).gt("created_at", one_hr_ago).execute().data
                    if not exists:
                        match_id = f"FF{random.randint(10000,99999)}"
                        db.table("matches").insert({"match_id": match_id, "room_id": r_id, "room_pass": r_pass, "tickets_left": 10}).execute()

        try:
            await client.start()
            pyro_clients[phone] = client
            logger.info(f"Hydra Worker Started: {phone}")
        except Exception as e:
            logger.error(f"Worker {phone} failed: {e}")

# ==========================================
# 5. UX MENUS & ESCAPE HATCHES
# ==========================================
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
    await update.message.reply_text("🚫 Request Cancelled. Returning to Main Menu.", reply_markup=get_main_menu())
    return ConversationHandler.END

# ==========================================
# 6. ONBOARDING & LEGAL SHIELD
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.message.from_user.id)
    if user['is_restricted']:
        await update.message.reply_text("🚨 **ACCOUNT SUSPENDED:** Aapka account rules todne ki wajah se restricted hai.")
        return ConversationHandler.END

    if not user['is_18_plus']:
        msg = ("⚖️ **LEGAL & AGE VERIFICATION** ⚖️\n\n"
               "Govt of India ke niyamon ke anusaar, khelne ke liye:\n"
               "1. Aapki umar **18 saal (18+)** honi chahiye.\n"
               "2. Aap restricted states se nahi hone chahiye.\n\n"
               "**Kya aap 18+ hain aur rules accept karte hain?**")
        kbd = [[InlineKeyboardButton("✅ YES, I AM 18+ (Play Now)", callback_data="legal_yes")],
               [InlineKeyboardButton("❌ NO, I AM UNDER 18", callback_data="legal_no")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
        return ConversationHandler.END

    await update.message.reply_text("🔥 **Welcome back to Free Fire Tournaments!**", reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

async def legal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "legal_yes":
        db.table("users").update({"is_18_plus": True}).eq("user_id", query.from_user.id).execute()
        await query.message.delete()
        await query.message.reply_text("✅ Verification Successful! Welcome!", reply_markup=get_main_menu())
    else:
        await query.message.edit_text("❌ Sorry! You must be 18+ to play on this platform.")

# ==========================================
# 7. MAIN ENGINE (Play, Balances, Daily)
# ==========================================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if user['is_restricted']:
        return await update.message.reply_text("🚨 Aapka account suspended hai.")

    if text == "🎮 PLAY FREE FIRE":
        if not user['ff_ign']:
            await update.message.reply_text("⚠️ **PROFILE CONFIGURATION** ⚠️\nApna exact Free Fire Nickname type karein:", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
            return WAIT_IGN
            
        exp_time = (datetime.now(IST) - timedelta(minutes=MATCH_LIVE_MINS)).isoformat()
        db.table("matches").delete().lt("created_at", exp_time).execute()
        
        matches = db.table("matches").select("*").gt("tickets_left", 0).execute().data
        if not matches:
            await update.message.reply_text("🟡 Abhi koi match Live nahi hai. Scraper match dhoondh raha hai, thodi der mein aaiye!")
            return ConversationHandler.END
            
        msg = "🔴 **LIVE BATTLE BOARD** 🔴\n*Make sure all maps (Bermuda, Kalahari) are downloaded!*\n\n"
        kbd = []
        for m in matches:
            msg += f"🔥 **Match #{m['match_id']}** | Tickets Left: {m['tickets_left']}/10\n💰 Entry: ₹{ENTRY_FEE} | Prize: ₹{PRIZE_MONEY}\n\n"
            kbd.append([InlineKeyboardButton(f"🔒 JOIN #{m['match_id']} (₹50)", callback_data=f"confjoin_{m['match_id']}")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

    elif text == "💰 ADD FUNDS":
        await update.message.reply_text("💸 Kitne Rupaye add karne hain? (Min: ₹30)\n*Type amount below:*", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
        return WAIT_ADD_AMT

    elif text == "💸 WITHDRAW CASH":
        tot = user['deposit_balance'] + user['winning_balance'] + user['bonus_balance']
        msg = (f"💰 **Total Balance:** ₹{tot}\n🟢 **Winnings:** ₹{user['winning_balance']} (Withdrawable)\n"
               f"🔵 **Deposit:** ₹{user['deposit_balance']}\n🎁 **Bonus Cash:** ₹{user['bonus_balance']}\n🔒 **Locked:** ₹{user['locked_balance']}\n\n"
               f"*(Min withdrawal: ₹200 Winnings)*")
        if user['winning_balance'] < 200:
            await update.message.reply_text(msg + "\n\n❌ Minimum ₹200 Winnings needed to withdraw.", parse_mode='Markdown')
            return ConversationHandler.END
        
        await update.message.reply_text(msg + "\n\nApna UPI QR Code ki photo bhejein:", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
        return WAIT_WITHDRAW_QR

    elif text == "🎁 DAILY REWARD":
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if user['last_login'] == today:
            await update.message.reply_text("❌ Aap aaj ka Bonus claim kar chuke hain. Kal aaiye!")
        else:
            reward = random.randint(2, 5)
            db.table("users").update({"bonus_balance": user['bonus_balance'] + reward, "last_login": today}).eq("user_id", user_id).execute()
            await update.message.reply_text(f"🎉 **JACKPOT!** Aapko ₹{reward} Bonus Cash mila hai! 🎁\nCome back tomorrow for more!", parse_mode='Markdown')

    elif text == "🎯 MY MATCHES":
        ums = db.table("user_matches").select("*").eq("user_id", user_id).execute().data
        if not ums: return await update.message.reply_text("Aapne abhi tak koi match nahi khela.")
            
        msg = "🎯 **YOUR MATCHES** 🎯\n\n"
        kbd = []
        for um in ums[-5:]:
            m = db.table("matches").select("*").eq("match_id", um['match_id']).execute().data
            if not m: continue
            
            msg += f"**Match #{um['match_id']}**\nStatus: {um['status']}\n"
            if um['status'] == 'JOINED':
                join_time = datetime.fromisoformat(um['joined_at'])
                mins_passed = (datetime.now(IST) - join_time).total_seconds() / 60
                msg += f"🔑 ID: `{m[0]['room_id']}` | 🔐 Pass: `{m[0]['room_pass']}`\n\n"
                
                if mins_passed < REFUND_WINDOW_MINS:
                    kbd.append([InlineKeyboardButton(f"⚠️ ROOM FULL? (Get Refund #{um['match_id']})", callback_data=f"askref_{um['match_id']}")])
                else:
                    kbd.append([InlineKeyboardButton(f"🏆 I WON! (Claim #{um['match_id']})", callback_data=f"up_proof_{um['match_id']}")])
            else: msg += "\n"
                
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd) if kbd else None)

    elif text == "🤝 HELP / SUPPORT":
        await update.message.reply_text("📞 **Support Center**\nEmail: support@fftourney.com\nTelegram: @AdminHandle", parse_mode='Markdown')

    return ConversationHandler.END

# ==========================================
# 8. JOIN MATCH & REFUND (Safety Locks)
# ==========================================
async def conf_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    kbd = [[InlineKeyboardButton("✅ YES, JOIN (Pay ₹50)", callback_data=f"dojoin_{match_id}")],
           [InlineKeyboardButton("🔙 CANCEL", callback_data="delete_msg")]]
    await query.message.reply_text(f"Are you sure you want to pay ₹50 for Match #{match_id}?", reply_markup=InlineKeyboardMarkup(kbd))

async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    user_id = query.from_user.id
    
    exists = db.table("user_matches").select("*").eq("user_id", user_id).eq("match_id", match_id).execute().data
    if exists: return await query.message.edit_text("❌ Aap is match mein pehle se hain.")

    lock_key = f"join_{match_id}"
    if lock_key not in join_locks: join_locks[lock_key] = asyncio.Lock()
    
    async with join_locks[lock_key]:
        match = db.table("matches").select("*").eq("match_id", match_id).execute().data[0]
        if match['tickets_left'] <= 0: return await query.message.edit_text("❌ Oops! Ye match just abhi full ho gaya!")
            
        if deduct_balance(user_id, ENTRY_FEE):
            db.table("matches").update({"tickets_left": match['tickets_left'] - 1}).eq("match_id", match_id).execute()
            db.table("user_matches").insert({"user_id": user_id, "match_id": match_id, "status": "JOINED", "joined_at": datetime.now(IST).isoformat()}).execute()
            await query.message.edit_text(f"🔥 **ENTRY CONFIRMED!** 🎮\n🔑 ID: `{match['room_id']}`\n🔐 Pass: `{match['room_pass']}`\n\n*(Jaldi room join karein! Refund 8 min tak available hai)*", parse_mode='Markdown')
        else:
            await query.message.edit_text("❌ Insufficient Funds! Kripaya recharge karein.")

async def ask_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    msg = ("🚨 **REFUND CONFIRMATION** 🚨\n\nKya aap sach mein is match se bahar aana chahte hain?\n"
           "1. Aapke ₹50 turant wapas mil jayenge.\n2. Iske baad aap Proof Upload NAHI kar payenge.\n3. Agar aapne refund lekar jeeta, toh Prize NAHI milega.\n\nConfirm karte hain?")
    kbd = [[InlineKeyboardButton("✅ YES, REFUND ₹50", callback_data=f"doref_{match_id}")],
           [InlineKeyboardButton("🔙 NO, TAKE ME BACK", callback_data="delete_msg")]]
    await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

async def do_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    user_id = query.from_user.id
    
    um = db.table("user_matches").select("*").eq("user_id", user_id).eq("match_id", match_id).execute().data[0]
    if um['status'] == 'REFUNDED': return
    
    db.table("users").update({"deposit_balance": get_user(user_id)['deposit_balance'] + ENTRY_FEE}).eq("user_id", user_id).execute()
    db.table("user_matches").update({"status": "REFUNDED"}).eq("id", um['id']).execute()
    await query.message.edit_text("✅ **Refund Successful!** ₹50 aapke wallet mein add ho gaye hain.", parse_mode='Markdown')

async def cancel_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.delete()

# ==========================================
# 9. DEPOSIT ENGINE (AI Amount Check)
# ==========================================
async def save_ign_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.table("users").update({"ff_ign": update.message.text.strip()}).eq("user_id", update.message.from_user.id).execute()
    await update.message.reply_text("✅ IGN Locked successfully!", reply_markup=get_main_menu())
    return ConversationHandler.END

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    try:
        amt = float(text)
        if amt < 30: raise ValueError
    except:
        await update.message.reply_text("❌ Minimum ₹30 allowed. Sahi number likhein.")
        return WAIT_ADD_AMT
        
    context.user_data['dep_amt'] = amt
    upi_id = "yourfampay@fam" # PUT REAL UPI HERE
    await update.message.reply_text(f"Kripaya is QR par exact **₹{amt}** bhejein:\n`{upi_id}`\n\nUske baad **Payment Success ka Screenshot** yahan bhejein.", parse_mode='Markdown', reply_markup=get_cancel_kbd())
    return WAIT_PAY_PROOF

async def process_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    user_id = update.message.from_user.id
    claimed_amt = context.user_data.get('dep_amt')
    
    msg = await update.message.reply_text("⏳ Verifying with AI... (Please wait)", reply_markup=get_main_menu())
    await context.bot.send_chat_action(chat_id=user_id, action='typing')
    
    photo_file = await update.message.photo[-1].get_file()
    b64_image = base64.b64encode(await photo_file.download_as_bytearray()).decode('utf-8')
    
    prompt = "Extract 12-digit UTR and the Amount paid. Format: UTR: <12-digits> | AMOUNT: <number>"
    ai_text = await analyze_image(b64_image, prompt, GEMINI_PAYMENT_KEY)
    
    utr_m = re.search(r'UTR:\s*(\d{12})', ai_text)
    amt_m = re.search(r'AMOUNT:\s*(\d+)', ai_text)
    
    if not utr_m:
        return await msg.edit_text("⚠️ AI couldn't read the UTR clearly. Request sent to Admin for Manual Check.")
        
    utr = utr_m.group(1)
    ai_amt = float(amt_m.group(1)) if amt_m else claimed_amt
    
    if db.table("used_utrs").select("*").eq("utr", utr).execute().data:
        return await msg.edit_text("🚫 SYSTEM ALERT: Duplicate UTR detected. Scammer request Rejected.")
        
    if not any(utr.startswith(p) for p in get_utr_prefixes()):
        return await msg.edit_text("🚫 SYSTEM ALERT: Ye UTR aaj/kal ka nahi hai. Request Auto-Rejected.")

    # Admin Panel Dossier
    kbd = [[InlineKeyboardButton(f"✅ APPROVE ₹{ai_amt}", callback_data=f"admdep_{user_id}_{utr}_{ai_amt}")],
           [InlineKeyboardButton("❌ REJECT", callback_data=f"admrej_{user_id}")]]
    dossier = f"🚨 **DEPOSIT REQUEST** 🚨\n👤 User: {user_id}\n💰 Claimed: ₹{claimed_amt}\n🤖 AI Found: **₹{ai_amt}**\n🔢 UTR: `{utr}`"
    
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=photo_file.file_id, caption=dossier, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd))
    await msg.edit_text("✅ Screenshot Saved! Admin checking... Balance will be added in 2-5 mins.")
    return ConversationHandler.END

# ==========================================
# 10. WITHDRAW ENGINE (Locked Balance)
# ==========================================
async def process_withdraw_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    user_id = update.message.from_user.id
    user = get_user(user_id)
    amt = user['winning_balance']
    
    db.table("users").update({"winning_balance": 0, "locked_balance": user['locked_balance'] + amt}).eq("user_id", user_id).execute()
    
    kbd = [[InlineKeyboardButton(f"✅ PAID QR (Clear Lock)", callback_data=f"admpaid_{user_id}_{amt}")],
           [InlineKeyboardButton("❌ REJECT (Refund User)", callback_data=f"admrejwd_{user_id}_{amt}")]]
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=update.message.photo[-1].file_id, caption=f"💸 **WITHDRAWAL**\nUser: {user_id}\nAmt: ₹{amt}", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
    await update.message.reply_text(f"✅ Withdraw requested! ₹{amt} safely locked.", reply_markup=get_main_menu())
    return ConversationHandler.END

# ==========================================
# 11. WINNER VERIFICATION
# ==========================================
async def up_proof_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['win_match'] = query.data.split("_")[2]
    await query.message.reply_text(
        "🎉 **You Won!** Send your 'Match Results' screenshot below.\n*(Do NOT crop the photo)*", 
        parse_mode='Markdown', reply_markup=get_cancel_kbd()
    )
    return WAIT_WIN_PROOF

async def process_win_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    user_id = update.message.from_user.id
    match_id = context.user_data['win_match']
    
    msg = await update.message.reply_text("⏳ Checking Photo with AI... (Please wait)", reply_markup=get_main_menu())
    await context.bot.send_chat_action(chat_id=user_id, action='typing')
    
    photo_file = await update.message.photo[-1].get_file()
    b64_image = base64.b64encode(await photo_file.download_as_bytearray()).decode('utf-8')
    
    prompt = "Read the In-Game Name at Rank #1 exactly. Is the image cropped? Format: [UNCROPPED/CROPPED] | Rank 1: <Name>"
    ai_text = await analyze_image(b64_image, prompt, GEMINI_MATCH_KEY)
    
    db.table("user_matches").update({"status": "PENDING"}).eq("user_id", user_id).eq("match_id", match_id).execute()
    
    kbd = [[InlineKeyboardButton(f"✅ APPROVE ₹{PRIZE_MONEY}", callback_data=f"admprize_{user_id}_{match_id}")],
           [InlineKeyboardButton("❌ REJECT (Scam)", callback_data=f"admrej_{user_id}")]]
    dossier = f"🚨 **VERIFICATION DOSSIER** 🚨\n👤 User: {user_id}\n🎮 Locked IGN: `{get_user(user_id)['ff_ign']}`\n🤖 AI Read: {ai_text}"
    
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=photo_file.file_id, caption=dossier, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd))
    await msg.edit_text("✅ Photo received! Admin checking... Amount will be credited soon.")
    return ConversationHandler.END

# ==========================================
# 12. ADMIN MAGIC BUTTONS
# ==========================================
async def admin_btns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action = data[0]
    
    if action == "admdep":
        user_id, utr, amt = int(data[1]), data[2], float(data[3])
        db.table("users").update({"deposit_balance": get_user(user_id)['deposit_balance'] + amt}).eq("user_id", user_id).execute()
        if utr != "MANUAL_CHECK": db.table("used_utrs").insert({"utr": utr}).execute()
        await context.bot.send_message(chat_id=user_id, text=f"✅ **PAYMENT SUCCESS!** 💰 ₹{amt} added!", parse_mode='Markdown')
        await query.message.edit_caption(caption=f"✅ APPROVED ₹{amt}")
        
    elif action == "admpaid":
        user_id, amt = int(data[1]), float(data[2])
        db.table("users").update({"locked_balance": get_user(user_id)['locked_balance'] - amt}).eq("user_id", user_id).execute()
        await context.bot.send_message(chat_id=user_id, text=f"✅ **WITHDRAWAL PROCESSED!** ₹{amt} sent to your QR.", parse_mode='Markdown')
        await query.message.edit_caption(caption="✅ PAID & CLEARED")
        
    elif action == "admrejwd":
        user_id, amt = int(data[1]), float(data[2])
        user = get_user(user_id)
        db.table("users").update({"locked_balance": user['locked_balance'] - amt, "winning_balance": user['winning_balance'] + amt}).eq("user_id", user_id).execute()
        await query.message.edit_caption(caption="❌ REJECTED. Funds Unlocked.")
        
    elif action == "admprize":
        user_id, match_id = int(data[1]), data[2]
        db.table("users").update({"winning_balance": get_user(user_id)['winning_balance'] + PRIZE_MONEY}).eq("user_id", user_id).execute()
        db.table("user_matches").update({"status": "WON"}).eq("user_id", user_id).eq("match_id", match_id).execute()
        await context.bot.send_message(chat_id=user_id, text=f"🏆 **BOOYAH!** Payout verified! ₹{PRIZE_MONEY} added to Winnings!", parse_mode='Markdown')
        await query.message.edit_caption(caption="✅ PAYOUT DONE")
        
        CHANNEL_ID = os.getenv("CHANNEL_ID")
        if CHANNEL_ID:
            blast_msg = f"🏆 **BOOYAH!** 🏆\n\n🎉 Player **{get_user(user_id)['ff_ign']}** just won Match #{match_id} and cashed out **₹{PRIZE_MONEY}**!\n\n💸 Khelo aur Jeeto! Start the bot now!"
            try: await context.bot.send_message(chat_id=CHANNEL_ID, text=blast_msg, parse_mode='Markdown')
            except: pass
        
    elif action == "admrej":
        await query.message.edit_caption(caption="❌ REJECTED / SCAM")
        
    elif action == "delete":
        await query.message.delete()

# ==========================================
# 13. ADMIN SCRAPER COMMANDS
# ==========================================
async def add_worker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    phone = context.args[0]
    client = PyroClient(f"worker_{phone}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    await client.connect()
    sent_code = await client.send_code(phone)
    auth_cache[phone] = {'client': client, 'hash': sent_code.phone_code_hash}
    context.user_data['auth_phone'] = phone
    await update.message.reply_text(f"OTP sent to {phone}. Reply with OTP:")
    return WAIT_WORKER_OTP

async def worker_otp_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone, otp = context.user_data['auth_phone'], update.message.text
    data = auth_cache[phone]
    try:
        await data['client'].sign_in(phone, data['hash'], otp)
        db.table("workers").insert({"phone": phone, "session_string": await data['client'].export_session_string()}).execute()
        await update.message.reply_text(f"✅ Worker {phone} Active!")
        return ConversationHandler.END
    except Exception as e:
        if "SessionPasswordNeeded" in str(e):
            await update.message.reply_text("2FA Password Required. Send password:")
            return WAIT_WORKER_PASS
        await update.message.reply_text(f"Error: {e}"); return ConversationHandler.END

async def worker_pass_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone, pwd = context.user_data['auth_phone'], update.message.text
    data = auth_cache[phone]
    await data['client'].check_password(pwd)
    db.table("workers").insert({"phone": phone, "session_string": await data['client'].export_session_string()}).execute()
    await update.message.reply_text(f"✅ Worker {phone} Active!")
    return ConversationHandler.END

async def join_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    phone, channel = context.args[0], context.args[1]
    client = pyro_clients.get(phone)
    if client:
        await client.join_chat(channel)
        await update.message.reply_text(f"✅ {phone} joined {channel} & listening for matches!")

# ==========================================
# 14. WEB SERVER (UptimeRobot Anti-Sleep)
# ==========================================
async def handle_ping(request): return web.Response(text="Esports Bot Engine Running 100%")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080))).start()

# ==========================================
# 15. MAIN BOOT SEQUENCE
# ==========================================
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🎮 PLAY FREE FIRE$"), handle_menu),
            MessageHandler(filters.Regex("^💰 ADD FUNDS$"), handle_menu),
            MessageHandler(filters.Regex("^💸 WITHDRAW CASH$"), handle_menu),
            CallbackQueryHandler(up_proof_btn, pattern="^up_proof_"),
            CommandHandler("add_worker", add_worker_cmd)
        ],
        states={
            WAIT_IGN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_ign_flow)],
            WAIT_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
            WAIT_PAY_PROOF: [MessageHandler(filters.PHOTO | filters.TEXT, process_payment_proof)],
            WAIT_WITHDRAW_QR: [MessageHandler(filters.PHOTO | filters.TEXT, process_withdraw_qr)],
            WAIT_WIN_PROOF: [MessageHandler(filters.PHOTO | filters.TEXT, process_win_proof)],
            WAIT_WORKER_OTP: [MessageHandler(filters.TEXT, worker_otp_recv)],
            WAIT_WORKER_PASS: [MessageHandler(filters.TEXT, worker_pass_recv)]
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^(❌ Cancel & Go Back)$"), cancel_action)
        ]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join_channel_cmd))
    app.add_handler(MessageHandler(filters.Regex("^(📊 MY WALLET|🎁 DAILY REWARD|🤝 HELP / SUPPORT|🎯 MY MATCHES)$"), handle_menu))
    app.add_handler(CallbackQueryHandler(legal_callback, pattern="^legal_"))
    app.add_handler(CallbackQueryHandler(conf_join, pattern="^confjoin_"))
    app.add_handler(CallbackQueryHandler(do_join, pattern="^dojoin_"))
    app.add_handler(CallbackQueryHandler(ask_refund, pattern="^askref_"))
    app.add_handler(CallbackQueryHandler(do_refund, pattern="^doref_"))
    app.add_handler(CallbackQueryHandler(cancel_inline, pattern="^delete_msg$"))
    app.add_handler(CallbackQueryHandler(admin_btns, pattern="^adm"))
    app.add_handler(conv_handler)
    
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())
    loop.create_task(start_all_workers())
    
    logger.info("🔥 Arena Platform is Live!")
    app.run_polling()

if __name__ == "__main__":
    main()
