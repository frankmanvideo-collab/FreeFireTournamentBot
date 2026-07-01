import os
import re
import base64
import random
import asyncio
import logging
import requests
import threading
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
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

IST = pytz.timezone('Asia/Kolkata')
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

join_locks = {}
pyro_clients = {}
auth_cache = {}

ENTRY_FEE = 50.0
PRIZE_MONEY = 300.0
MATCH_LIVE_MINS = 10
REFUND_WINDOW_MINS = 8

(WAIT_IGN, WAIT_ADD_AMT, WAIT_PAY_PROOF, WAIT_WITHDRAW_QR, WAIT_WIN_PROOF,
 WAIT_WORKER_PHONE, WAIT_WORKER_OTP, WAIT_WORKER_PASS) = range(8)

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

async def analyze_image(b64_image, prompt, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}]}]}
    try:
        resp = await asyncio.to_thread(requests.post, url, json=payload)
        return resp.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "AI_FAILED"

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
            logger.info(f"Worker Started: {phone}")
        except Exception as e:
            logger.error(f"Worker Failed: {e}")

def get_main_menu():
    kbd = [[KeyboardButton("🎮 PLAY FREE FIRE"), KeyboardButton("🎯 MY MATCHES")],
           [KeyboardButton("💰 ADD FUNDS"), KeyboardButton("💸 WITHDRAW CASH")],
           [KeyboardButton("🎁 DAILY REWARD"), KeyboardButton("🤝 HELP / SUPPORT")]]
    return ReplyKeyboardMarkup(kbd, resize_keyboard=True)

def get_cancel_kbd():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel & Go Back")]], resize_keyboard=True)

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=get_main_menu())
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.message.from_user.id)
    if user['is_restricted']:
        await update.message.reply_text("🚨 **ACCOUNT SUSPENDED**")
        return ConversationHandler.END

    if not user['is_18_plus']:
        msg = ("⚖️ **LEGAL VERIFICATION**\nAre you 18+ to play real money games?")
        kbd = [[InlineKeyboardButton("✅ YES, I AM 18+", callback_data="legal_yes")],
               [InlineKeyboardButton("❌ NO", callback_data="legal_no")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
        return ConversationHandler.END

    await update.message.reply_text("🔥 **Welcome back!**", reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

async def legal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "legal_yes":
        db.table("users").update({"is_18_plus": True}).eq("user_id", query.from_user.id).execute()
        await query.message.delete()
        await query.message.reply_text("✅ Verified! Welcome!", reply_markup=get_main_menu())
    else:
        await query.message.edit_text("❌ You must be 18+.")

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if user['is_restricted']: return await update.message.reply_text("🚨 Suspended.")

    if text == "🎮 PLAY FREE FIRE":
        if not user['ff_ign']:
            await update.message.reply_text("⚠️ Apna Free Fire Nickname type karein:", reply_markup=get_cancel_kbd())
            return WAIT_IGN
            
        exp_time = (datetime.now(IST) - timedelta(minutes=MATCH_LIVE_MINS)).isoformat()
        db.table("matches").delete().lt("created_at", exp_time).execute()
        
        matches = db.table("matches").select("*").gt("tickets_left", 0).execute().data
        if not matches:
            await update.message.reply_text("🟡 Abhi match Live nahi hai. Scraper dhoondh raha hai!")
            return ConversationHandler.END
            
        msg = "🔴 **LIVE BATTLE BOARD**\n\n"
        kbd = []
        for m in matches:
            msg += f"🔥 **Match #{m['match_id']}** | Tickets: {m['tickets_left']}/10\n💰 Entry: ₹{ENTRY_FEE} | Prize: ₹{PRIZE_MONEY}\n\n"
            kbd.append([InlineKeyboardButton(f"🔒 JOIN #{m['match_id']}", callback_data=f"confjoin_{m['match_id']}")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

    elif text == "💰 ADD FUNDS":
        await update.message.reply_text("Kitne Rupaye add karne hain? (Min: 30)", reply_markup=get_cancel_kbd())
        return WAIT_ADD_AMT

    elif text == "💸 WITHDRAW CASH":
        tot = user['deposit_balance'] + user['winning_balance'] + user['bonus_balance']
        msg = f"💰 **Total:** ₹{tot}\n🟢 **Winnings:** ₹{user['winning_balance']}\n🔵 **Deposit:** ₹{user['deposit_balance']}\n🎁 **Bonus:** ₹{user['bonus_balance']}\n🔒 **Locked:** ₹{user['locked_balance']}"
        if user['winning_balance'] < 200:
            await update.message.reply_text(msg + "\n❌ Minimum ₹200 Winnings needed.", parse_mode='Markdown')
            return ConversationHandler.END
        await update.message.reply_text(msg + "\n\nUPI QR Code bhejein:", reply_markup=get_cancel_kbd(), parse_mode='Markdown')
        return WAIT_WITHDRAW_QR

    elif text == "🎁 DAILY REWARD":
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if user['last_login'] == today:
            await update.message.reply_text("❌ Aaj ka mil chuka hai. Kal aana!")
        else:
            reward = random.randint(2, 5)
            db.table("users").update({"bonus_balance": user['bonus_balance'] + reward, "last_login": today}).eq("user_id", user_id).execute()
            await update.message.reply_text(f"🎉 **JACKPOT!** ₹{reward} Bonus Cash mila!")

    elif text == "🎯 MY MATCHES":
        ums = db.table("user_matches").select("*").eq("user_id", user_id).execute().data
        if not ums: return await update.message.reply_text("Koi match nahi khela.")
            
        msg = "🎯 **YOUR MATCHES**\n\n"
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
                    kbd.append([InlineKeyboardButton(f"⚠️ ROOM FULL? (Refund #{um['match_id']})", callback_data=f"askref_{um['match_id']}")])
                else:
                    kbd.append([InlineKeyboardButton(f"🏆 I WON! (Claim #{um['match_id']})", callback_data=f"up_proof_{um['match_id']}")])
            else: msg += "\n"
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd) if kbd else None)

    elif text == "🤝 HELP / SUPPORT":
        await update.message.reply_text("📞 Telegram: @AdminHandle")

    return ConversationHandler.END

async def conf_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    kbd = [[InlineKeyboardButton("✅ YES, JOIN (Pay ₹50)", callback_data=f"dojoin_{match_id}")],
           [InlineKeyboardButton("🔙 CANCEL", callback_data="delete_msg")]]
    await query.message.reply_text(f"Pay ₹50 for Match #{match_id}?", reply_markup=InlineKeyboardMarkup(kbd))

async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    user_id = query.from_user.id
    exists = db.table("user_matches").select("*").eq("user_id", user_id).eq("match_id", match_id).execute().data
    if exists: return await query.message.edit_text("❌ Pehle se joined.")

    lock_key = f"join_{match_id}"
    if lock_key not in join_locks: join_locks[lock_key] = asyncio.Lock()
    
    async with join_locks[lock_key]:
        match = db.table("matches").select("*").eq("match_id", match_id).execute().data[0]
        if match['tickets_left'] <= 0: return await query.message.edit_text("❌ Match Full!")
            
        if deduct_balance(user_id, ENTRY_FEE):
            db.table("matches").update({"tickets_left": match['tickets_left'] - 1}).eq("match_id", match_id).execute()
            db.table("user_matches").insert({"user_id": user_id, "match_id": match_id, "status": "JOINED", "joined_at": datetime.now(IST).isoformat()}).execute()
            await query.message.edit_text(f"🔥 **ENTRY CONFIRMED!**\n🔑 ID: `{match['room_id']}`\n🔐 Pass: `{match['room_pass']}`\n*(Refund 8 min tak available hai)*", parse_mode='Markdown')
        else:
            await query.message.edit_text("❌ Balance kam hai!")

async def ask_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    kbd = [[InlineKeyboardButton("✅ YES, REFUND ₹50", callback_data=f"doref_{match_id}")],
           [InlineKeyboardButton("🔙 NO", callback_data="delete_msg")]]
    await query.message.edit_text("Refund chahiye? Prize nahi milega.", reply_markup=InlineKeyboardMarkup(kbd))

async def do_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = query.data.split("_")[1]
    user_id = query.from_user.id
    um = db.table("user_matches").select("*").eq("user_id", user_id).eq("match_id", match_id).execute().data[0]
    if um['status'] == 'REFUNDED': return
    db.table("users").update({"deposit_balance": get_user(user_id)['deposit_balance'] + ENTRY_FEE}).eq("user_id", user_id).execute()
    db.table("user_matches").update({"status": "REFUNDED"}).eq("id", um['id']).execute()
    await query.message.edit_text("✅ Refund Successful!")

async def cancel_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.delete()

async def save_ign_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.table("users").update({"ff_ign": update.message.text.strip()}).eq("user_id", update.message.from_user.id).execute()
    await update.message.reply_text("✅ IGN Locked!", reply_markup=get_main_menu())
    return ConversationHandler.END

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    try:
        amt = float(text)
        if amt < 30: raise ValueError
    except:
        await update.message.reply_text("❌ Minimum ₹30 allowed.")
        return WAIT_ADD_AMT
    context.user_data['dep_amt'] = amt
    await update.message.reply_text(f"QR par **₹{amt}** bhejein:\n`yourfampay@fam`\n\nPayment Screenshot yahan bhejein.", parse_mode='Markdown', reply_markup=get_cancel_kbd())
    return WAIT_PAY_PROOF

async def process_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    user_id = update.message.from_user.id
    claimed_amt = context.user_data.get('dep_amt')
    msg = await update.message.reply_text("⏳ Verifying...", reply_markup=get_main_menu())
    
    photo_file = await update.message.photo[-1].get_file()
    b64_image = base64.b64encode(await photo_file.download_as_bytearray()).decode('utf-8')
    prompt = "Extract 12-digit UTR and Amount. Format: UTR: <12-digits> | AMOUNT: <number>"
    ai_text = await analyze_image(b64_image, prompt, GEMINI_PAYMENT_KEY)
    
    utr_m = re.search(r'UTR:\s*(\d{12})', ai_text)
    amt_m = re.search(r'AMOUNT:\s*(\d+)', ai_text)
    if not utr_m: return await msg.edit_text("⚠️ Manual Check needed.")
    utr = utr_m.group(1)
    ai_amt = float(amt_m.group(1)) if amt_m else claimed_amt
    
    if db.table("used_utrs").select("*").eq("utr", utr).execute().data or not any(utr.startswith(p) for p in get_utr_prefixes()):
        return await msg.edit_text("🚫 SYSTEM ALERT: Rejected.")

    kbd = [[InlineKeyboardButton(f"✅ APPROVE ₹{ai_amt}", callback_data=f"admdep_{user_id}_{utr}_{ai_amt}")],
           [InlineKeyboardButton("❌ REJECT", callback_data=f"admrej_{user_id}")]]
    dossier = f"🚨 **DEPOSIT REQUEST**\n👤 {user_id}\n💰 Claimed: ₹{claimed_amt}\n🤖 AI: **₹{ai_amt}**\n🔢 UTR: `{utr}`"
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=photo_file.file_id, caption=dossier, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd))
    await msg.edit_text("✅ Saved! Balance will be added soon.")
    return ConversationHandler.END

async def process_withdraw_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    user_id = update.message.from_user.id
    user = get_user(user_id)
    amt = user['winning_balance']
    db.table("users").update({"winning_balance": 0, "locked_balance": user['locked_balance'] + amt}).eq("user_id", user_id).execute()
    kbd = [[InlineKeyboardButton(f"✅ PAID QR", callback_data=f"admpaid_{user_id}_{amt}")],
           [InlineKeyboardButton("❌ REJECT", callback_data=f"admrejwd_{user_id}_{amt}")]]
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=update.message.photo[-1].file_id, caption=f"💸 **WITHDRAWAL**\nUser: {user_id}\nAmt: ₹{amt}", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
    await update.message.reply_text("✅ Requested!", reply_markup=get_main_menu())
    return ConversationHandler.END

async def up_proof_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['win_match'] = query.data.split("_")[2]
    await query.message.reply_text("🎉 Send your Screenshot:", reply_markup=get_cancel_kbd())
    return WAIT_WIN_PROOF

async def process_win_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancel & Go Back": return await cancel_action(update, context)
    user_id = update.message.from_user.id
    match_id = context.user_data['win_match']
    msg = await update.message.reply_text("⏳ Verifying...", reply_markup=get_main_menu())
    
    photo_file = await update.message.photo[-1].get_file()
    b64_image = base64.b64encode(await photo_file.download_as_bytearray()).decode('utf-8')
    prompt = "Read In-Game Name at Rank 1. Format: [UNCROPPED/CROPPED] | Rank 1: <Name>"
    ai_text = await analyze_image(b64_image, prompt, GEMINI_MATCH_KEY)
    
    db.table("user_matches").update({"status": "PENDING"}).eq("user_id", user_id).eq("match_id", match_id).execute()
    kbd = [[InlineKeyboardButton(f"✅ APPROVE ₹{PRIZE_MONEY}", callback_data=f"admprize_{user_id}_{match_id}")],
           [InlineKeyboardButton("❌ REJECT", callback_data=f"admrej_{user_id}")]]
    dossier = f"🚨 **VERIFICATION**\n👤 {user_id}\n🎮 Locked IGN: `{get_user(user_id)['ff_ign']}`\n🤖 AI: {ai_text}"
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=photo_file.file_id, caption=dossier, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kbd))
    await msg.edit_text("✅ Checking...")
    return ConversationHandler.END

async def admin_btns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action = data[0]
    
    if action == "admdep":
        user_id, utr, amt = int(data[1]), data[2], float(data[3])
        db.table("users").update({"deposit_balance": get_user(user_id)['deposit_balance'] + amt}).eq("user_id", user_id).execute()
        if utr != "MANUAL_CHECK": db.table("used_utrs").insert({"utr": utr}).execute()
        await context.bot.send_message(chat_id=user_id, text=f"✅ **PAYMENT SUCCESS!** ₹{amt} added!", parse_mode='Markdown')
        await query.message.edit_caption(caption="✅ APPROVED")
    elif action == "admpaid":
        user_id, amt = int(data[1]), float(data[2])
        db.table("users").update({"locked_balance": get_user(user_id)['locked_balance'] - amt}).eq("user_id", user_id).execute()
        await context.bot.send_message(chat_id=user_id, text=f"✅ **WITHDRAWAL PROCESSED!**", parse_mode='Markdown')
        await query.message.edit_caption(caption="✅ PAID & CLEARED")
    elif action == "admrejwd":
        user_id, amt = int(data[1]), float(data[2])
        user = get_user(user_id)
        db.table("users").update({"locked_balance": user['locked_balance'] - amt, "winning_balance": user['winning_balance'] + amt}).eq("user_id", user_id).execute()
        await query.message.edit_caption(caption="❌ REJECTED. Unlocked.")
    elif action == "admprize":
        user_id, match_id = int(data[1]), data[2]
        db.table("users").update({"winning_balance": get_user(user_id)['winning_balance'] + PRIZE_MONEY}).eq("user_id", user_id).execute()
        db.table("user_matches").update({"status": "WON"}).eq("user_id", user_id).eq("match_id", match_id).execute()
        await context.bot.send_message(chat_id=user_id, text=f"🏆 **BOOYAH!** ₹{PRIZE_MONEY} added!", parse_mode='Markdown')
        await query.message.edit_caption(caption="✅ PAYOUT DONE")
        if CHANNEL_ID:
            try: await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🏆 **BOOYAH!** Player {get_user(user_id)['ff_ign']} won ₹{PRIZE_MONEY}!")
            except: pass
    elif action == "admrej":
        await query.message.edit_caption(caption="❌ REJECTED")
    elif action == "delete":
        await query.message.delete()

async def admin_hype_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    if not CHANNEL_ID: return await update.message.reply_text("❌ CHANNEL_ID missing in Secrets.")
    names = ["❖Rᴀнᴜʟ", "ProSniper99", "VIPER_FF", "SK_SABIR_FAN", "Riya♡Gaming", "GHOST_RIDER", "X-MAN_007"]
    win_name = random.choice(names)
    m_id = random.randint(1000, 9999)
    msgs = [
        f"🔥 **INSANE WIN!** Player **{win_name}** snatched Rank 1 in Match #{m_id} and cashed out ₹300! 💸",
        f"🏆 **BOOYAH!** **{win_name}** dominated Match #{m_id} and took home ₹300 via UPI! ⚡",
        f"🤑 **EASY MONEY!** **{win_name}** just won Match #{m_id}! Join now and earn real cash! 💰"
    ]
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=random.choice(msgs), parse_mode='Markdown')
        await update.message.reply_text("✅ Fake Hype Sent to Public Channel!")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def admin_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    active_workers = len(pyro_clients)
    await update.message.reply_text(f"🟢 **SERVER STATUS: ONLINE**\n🤖 Active Scrapers: {active_workers}\n(Engine running smoothly)", parse_mode='Markdown')


async def add_worker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    phone = context.args[0]
    client = PyroClient(f"worker_{phone}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    await client.connect()
    sent_code = await client.send_code(phone)
    auth_cache[phone] = {'client': client, 'hash': sent_code.phone_code_hash}
    context.user_data['auth_phone'] = phone
    await update.message.reply_text(f"OTP sent to {phone}. Reply OTP:")
    return WAIT_WORKER_OTP

async def worker_otp_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone, otp = context.user_data['auth_phone'], update.message.text
    data = auth_cache[phone]
    try:
        await data['client'].sign_in(phone, data['hash'], otp)
        db.table("workers").insert({"phone": phone, "session_string": await data['client'].export_session_string()}).execute()
        await update.message.reply_text("✅ Worker Active!")
        return ConversationHandler.END
    except Exception as e:
        if "PasswordNeeded" in str(e): return WAIT_WORKER_PASS
        return ConversationHandler.END

async def worker_pass_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone, pwd = context.user_data['auth_phone'], update.message.text
    data = auth_cache[phone]
    await data['client'].check_password(pwd)
    db.table("workers").insert({"phone": phone, "session_string": await data['client'].export_session_string()}).execute()
    await update.message.reply_text("✅ Worker Active!")
    return ConversationHandler.END

async def join_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    phone, channel = context.args[0], context.args[1]
    client = pyro_clients.get(phone)
    if client: await client.join_chat(channel); await update.message.reply_text(f"✅ Joined {channel}")

def run_background_services():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = web.AppRunner(web.Application())
    loop.run_until_complete(runner.setup())
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    loop.run_until_complete(site.start())
    loop.run_until_complete(start_all_workers())
    loop.run_forever()

def main():
    bg_thread = threading.Thread(target=run_background_services, daemon=True)
    bg_thread.start()
    
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
        fallbacks=[CommandHandler("start", start), MessageHandler(filters.Regex("^(❌ Cancel & Go Back)$"), cancel_action)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join_channel_cmd))
    app.add_handler(CommandHandler("hype", admin_hype_cmd))
    app.add_handler(CommandHandler("status", admin_status_cmd))
    app.add_handler(MessageHandler(filters.Regex("^(📊 MY WALLET|🎁 DAILY REWARD|🤝 HELP / SUPPORT|🎯 MY MATCHES)$"), handle_menu))
    app.add_handler(CallbackQueryHandler(legal_callback, pattern="^legal_"))
    app.add_handler(CallbackQueryHandler(conf_join, pattern="^confjoin_"))
    app.add_handler(CallbackQueryHandler(do_join, pattern="^dojoin_"))
    app.add_handler(CallbackQueryHandler(ask_refund, pattern="^askref_"))
    app.add_handler(CallbackQueryHandler(do_refund, pattern="^doref_"))
    app.add_handler(CallbackQueryHandler(cancel_inline, pattern="^delete_msg$"))
    app.add_handler(CallbackQueryHandler(admin_btns, pattern="^adm"))
    app.add_handler(conv_handler)
    
        logger.info("🔥 Arena Platform is Live!")
    
    # FORCED LOOP CREATION (The Nuclear Fix)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling()

if __name__ == "__main__":
    main()
