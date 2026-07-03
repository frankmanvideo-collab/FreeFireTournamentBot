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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from aiohttp import web
from pyrogram import Client as PyroClient, filters as pyfilters
import pytz

# ==========================================
# 1. ENTERPRISE CONFIG & ENV VARIABLES
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "placeholder")
GEMINI_PAYMENT_KEY = os.environ.get("GEMINI_PAYMENT_KEY")
GEMINI_MATCH_KEY = os.environ.get("GEMINI_MATCH_KEY")
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

IST = pytz.timezone('Asia/Kolkata')
db = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

join_locks, pyro_clients, auth_cache = {}, {}, {}
ENTRY_FEE, PRIZE_MONEY, MATCH_LIVE_MINS, REFUND_WINDOW_MINS = 50.0, 300.0, 10, 8

(WAIT_IGN, WAIT_ADD_AMT, WAIT_PAY_PROOF, WAIT_WITHDRAW_QR, WAIT_WIN_PROOF,
 WAIT_WORKER_PHONE, WAIT_WORKER_OTP, WAIT_WORKER_PASS) = range(8)

# ==========================================
# 2. CORE DATABASE MANAGERS
# ==========================================
def get_user(user_id):
    res = db.table("users").select("*").eq("user_id", user_id).execute()
    if not res.data:
        new_user = {"user_id": user_id, "deposit_balance": 0.0, "winning_balance": 0.0, "bonus_balance": 0.0, "locked_balance": 0.0, "ff_ign": "", "last_login": "", "is_18_plus": False, "is_restricted": False}
        db.table("users").insert(new_user).execute()
        return new_user
    return res.data[0]

def deduct_balance(user_id, amount):
    user, rem = get_user(user_id), amount
    b, d, w = user['bonus_balance'], user['deposit_balance'], user['winning_balance']
    if min(b, rem) > 0: ded = min(b, rem); rem -= ded; b -= ded
    if min(d, rem) > 0: ded = min(d, rem); rem -= ded; d -= ded
    if min(w, rem) > 0: ded = min(w, rem); rem -= ded; w -= ded
    if rem > 0: return False
    db.table("users").update({"bonus_balance": b, "deposit_balance": d, "winning_balance": w}).eq("user_id", user_id).execute()
    return True

def get_utr_prefixes():
    now = datetime.now(IST); yest = now - timedelta(days=1)
    return [str(now.year)[-1] + now.strftime("%j"), str(yest.year)[-1] + yest.strftime("%j")]

# ==========================================
# 3. AI DUAL-CORE ENGINE (Anti-Freeze)
# ==========================================
async def analyze_image(b64_image, prompt, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}"
    payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}]}]}
    try:
        resp = await asyncio.to_thread(requests.post, url, json=payload, timeout=12)
        if resp.status_code != 200: return "AI_FAILED"
        return resp.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "AI_FAILED"

# ==========================================
# 4. HYDRA SCRAPER ENGINE (Pyrogram)
# ==========================================
async def start_all_workers():
    for w in db.table("workers").select("*").execute().data:
        phone = w['phone']
        client = PyroClient(f"worker_{phone}", api_id=API_ID, api_hash=API_HASH, session_string=w['session_string'], in_memory=True)
        
        @client.on_message(pyfilters.channel & pyfilters.text)
        async def scrape_room(c, message):
            text = message.text.upper()
            if "ID" in text and ("PASS" in text or "PWD" in text):
                id_m, pass_m = re.search(r'ID\s*[:\-]?\s*(\d{6,10})', text), re.search(r'PASS(?:WORD)?\s*[:\-]?\s*([A-Z0-9]+)', text)
                if id_m and pass_m:
                    r_id, r_pass = id_m.group(1), pass_m.group(1)
                    if not db.table("matches").select("*").eq("room_id", r_id).gt("created_at", (datetime.now(IST) - timedelta(hours=1)).isoformat()).execute().data:
                        db.table("matches").insert({"match_id": f"FF{random.randint(10000,99999)}", "room_id": r_id, "room_pass": r_pass, "tickets_left": 10}).execute()
        try: await client.start()
        except Exception as e: logger.error(f"Worker {phone} failed: {e}")

# ==========================================
# 5. UX MENUS & ESCAPE HATCHES
# ==========================================
def get_main_menu():
    return ReplyKeyboardMarkup([["🎮 PLAY FREE FIRE", "🎯 MY MATCHES"], ["💰 ADD FUNDS", "💸 WITHDRAW CASH"], ["🎁 DAILY REWARD", "🤝 HELP / SUPPORT"]], resize_keyboard=True)

def get_cancel_kbd():
    return ReplyKeyboardMarkup([["❌ Cancel & Go Back"]], resize_keyboard=True)

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Request Cancelled. Returning to Main Menu.", reply_markup=get_main_menu())
    return ConversationHandler.END

# ==========================================
# 6. ONBOARDING & LEGAL SHIELD
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.message.from_user.id)
    if user['is_restricted']: return await update.message.reply_text("🚨 ACCOUNT SUSPENDED")

    if not user['is_18_plus']:
        kbd = [[InlineKeyboardButton("✅ YES, I AM 18+", callback_data="legal_yes")], [InlineKeyboardButton("❌ NO", callback_data="legal_no")]]
        return await update.message.reply_text("⚖️ **LEGAL VERIFICATION**\nAre you 18+ to play?", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

    await update.message.reply_text("🔥 Welcome back!", reply_markup=get_main_menu())

async def legal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if update.callback_query.data == "legal_yes":
        db.table("users").update({"is_18_plus": True}).eq("user_id", update.callback_query.from_user.id).execute()
        await update.callback_query.message.edit_text("✅ Verified!")
        await update.callback_query.message.reply_text("Welcome!", reply_markup=get_main_menu())
    else:
        await update.callback_query.message.edit_text("❌ You must be 18+.")

# ==========================================
# 7. MAIN ENGINE (100% Emoji-Proof Router)
# ==========================================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.upper()
    user_id = update.message.from_user.id
    user = get_user(user_id)
    
    if "CANCEL" in text: return await cancel_action(update, context)
    if user['is_restricted']: return await update.message.reply_text("🚨 Suspended.")

    if "PLAY" in text:
        if not user['ff_ign']:
            await update.message.reply_text("⚠️ Apna exact Free Fire Nickname type karein:", reply_markup=get_cancel_kbd())
            return WAIT_IGN
        db.table("matches").delete().lt("created_at", (datetime.now(IST) - timedelta(minutes=MATCH_LIVE_MINS)).isoformat()).execute()
        matches = db.table("matches").select("*").gt("tickets_left", 0).execute().data
        if not matches: return await update.message.reply_text("🟡 Abhi match Live nahi hai.")
        msg = "🔴 **LIVE BATTLE BOARD**\n\n"
        kbd = [[InlineKeyboardButton(f"🔒 JOIN #{m['match_id']} (₹50)", callback_data=f"confjoin_{m['match_id']}")] for m in matches]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

    elif "ADD FUNDS" in text:
        await update.message.reply_text("💸 Kitne Rupaye add karne hain? (Min: 30)", reply_markup=get_cancel_kbd())
        return WAIT_ADD_AMT

    elif "WITHDRAW" in text:
        if user['winning_balance'] < 200:
            return await update.message.reply_text("❌ Minimum ₹200 Winnings needed.")
        await update.message.reply_text("📸 Apna UPI QR Code bhejein:", reply_markup=get_cancel_kbd())
        return WAIT_WITHDRAW_QR

    elif "DAILY REWARD" in text:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if user['last_login'] == today: return await update.message.reply_text("❌ Aaj ka mil gaya hai. Kal aaiye!")
        reward = random.randint(2, 5)
        db.table("users").update({"bonus_balance": user['bonus_balance'] + reward, "last_login": today}).eq("user_id", user_id).execute()
        await update.message.reply_text(f"🎉 Aapko ₹{reward} Bonus mila!")

    elif "MY MATCHES" in text:
        ums = db.table("user_matches").select("*").eq("user_id", user_id).execute().data
        if not ums: return await update.message.reply_text("Aapne koi match nahi khela.")
        msg = "🎯 **YOUR MATCHES**\n\n"
        kbd = []
        for um in ums[-5:]:
            m = db.table("matches").select("*").eq("match_id", um['match_id']).execute().data
            if not m: continue
            msg += f"🔥 Match #{um['match_id']} - Status: {um['status']}\n"
            if um['status'] == 'JOINED':
                if (datetime.now(IST) - datetime.fromisoformat(um['joined_at'])).total_seconds() / 60 < REFUND_WINDOW_MINS:
                    kbd.append([InlineKeyboardButton(f"⚠️ Refund #{um['match_id']}", callback_data=f"askref_{um['match_id']}")])
                else:
                    kbd.append([InlineKeyboardButton(f"🏆 Claim #{um['match_id']}", callback_data=f"up_proof_{um['match_id']}")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kbd) if kbd else None)

    elif "HELP" in text or "SUPPORT" in text:
        await update.message.reply_text("📞 **Support**\nEmail: frankmanvideo@gmail.com\nTelegram: @Tughh_456", parse_mode='Markdown')

    return ConversationHandler.END

# ==========================================
# 8. JOIN MATCH & REFUND
# ==========================================
async def conf_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    m_id = update.callback_query.data.split("_")[1]
    await update.callback_query.message.reply_text(f"Pay ₹50 for #{m_id}?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ YES", callback_data=f"dojoin_{m_id}"), InlineKeyboardButton("🔙 CANCEL", callback_data="delete_msg")]]))

async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    m_id, u_id = update.callback_query.data.split("_")[1], update.callback_query.from_user.id
    if db.table("user_matches").select("*").eq("user_id", u_id).eq("match_id", m_id).execute().data: return await update.callback_query.message.edit_text("❌ Already joined.")
    
    if m_id not in join_locks: join_locks[m_id] = asyncio.Lock()
    async with join_locks[m_id]:
        m = db.table("matches").select("*").eq("match_id", m_id).execute().data[0]
        if m['tickets_left'] <= 0: return await update.callback_query.message.edit_text("❌ Full!")
        if deduct_balance(u_id, ENTRY_FEE):
            db.table("matches").update({"tickets_left": m['tickets_left'] - 1}).eq("match_id", m_id).execute()
            db.table("user_matches").insert({"user_id": u_id, "match_id": m_id, "status": "JOINED", "joined_at": datetime.now(IST).isoformat()}).execute()
            await update.callback_query.message.edit_text(f"🔥 ENTRY CONFIRMED!\nID: `{m['room_id']}`\nPass: `{m['room_pass']}`", parse_mode='Markdown')
        else: await update.callback_query.message.edit_text("❌ Insufficient Funds!")

async def ask_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    m_id = update.callback_query.data.split("_")[1]
    await update.callback_query.message.edit_text("Refund chahiye?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ YES", callback_data=f"doref_{m_id}"), InlineKeyboardButton("🔙 NO", callback_data="delete_msg")]]))

async def do_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    m_id, u_id = update.callback_query.data.split("_")[1], update.callback_query.from_user.id
    um = db.table("user_matches").select("*").eq("user_id", u_id).eq("match_id", m_id).execute().data[0]
    if um['status'] == 'REFUNDED': return
    db.table("users").update({"deposit_balance": get_user(u_id)['deposit_balance'] + ENTRY_FEE}).eq("user_id", u_id).execute()
    db.table("user_matches").update({"status": "REFUNDED"}).eq("id", um['id']).execute()
    await update.callback_query.message.edit_text("✅ Refund Successful!")

async def cancel_inline(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.message.delete()

# ==========================================
# 9. DEPOSIT & WITHDRAW STATES
# ==========================================
def is_cancel(text): return bool(text) and "CANCEL" in text.upper()

async def save_ign_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_cancel(update.message.text): return await cancel_action(update, context)
    db.table("users").update({"ff_ign": update.message.text.strip()}).eq("user_id", update.message.from_user.id).execute()
    await update.message.reply_text("✅ IGN Locked!", reply_markup=get_main_menu())
    return ConversationHandler.END

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_cancel(update.message.text): return await cancel_action(update, context)
    try:
        amt = float(update.message.text)
        if amt < 30: raise ValueError
    except: return await update.message.reply_text("❌ Minimum ₹30 allowed.")
    context.user_data['dep_amt'] = amt
    await update.message.reply_photo(photo=f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=upi://pay?pa=dipanshu153@fam%26am={amt}%26cu=INR", caption=f"Pay ₹{amt} & Upload Screenshot.", reply_markup=get_cancel_kbd())
    return WAIT_PAY_PROOF

async def process_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_cancel(update.message.text): return await cancel_action(update, context)
    if not update.message.photo: return await update.message.reply_text("❌ Bhejo Photo.")
    
    u_id, c_amt = update.message.from_user.id, context.user_data.get('dep_amt')
    msg = await update.message.reply_text("⏳ Verifying...", reply_markup=get_main_menu())
    b64 = base64.b64encode(await update.message.photo[-1].get_file().download_as_bytearray()).decode('utf-8')
    
    ai = await analyze_image(b64, "Extract 12-digit UTR and Amount. Format: UTR: <12-digits> | AMOUNT: <number>", GEMINI_PAYMENT_KEY)
    u_m, a_m = re.search(r'UTR:\s*(\d{12})', ai), re.search(r'AMOUNT:\s*(\d+)', ai)
    
    if not u_m:
        utr, ai_amt = "MANUAL", c_amt
        await msg.edit_text("⚠️ AI failed. Sent to Admin.")
    else:
        utr, ai_amt = u_m.group(1), float(a_m.group(1)) if a_m else c_amt
        if db.table("used_utrs").select("*").eq("utr", utr).execute().data or not any(utr.startswith(p) for p in get_utr_prefixes()):
            return await msg.edit_text("🚫 SYSTEM ALERT: Rejected.")
            
    kbd = [[InlineKeyboardButton(f"✅ APPROVE ₹{ai_amt}", callback_data=f"admdep_{u_id}_{utr}_{ai_amt}")], [InlineKeyboardButton("❌ REJECT", callback_data=f"admrej_{u_id}")]]
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=update.message.photo[-1].file_id, caption=f"🚨 **DEPOSIT**\nUser: {u_id}\nUTR: `{utr}`\nAmt: ₹{ai_amt}", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
    await msg.edit_text("✅ Saved!")
    return ConversationHandler.END

async def process_withdraw_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_cancel(update.message.text): return await cancel_action(update, context)
    if not update.message.photo: return await update.message.reply_text("❌ Photo bhejo.")
    u_id, amt = update.message.from_user.id, get_user(update.message.from_user.id)['winning_balance']
    db.table("users").update({"winning_balance": 0, "locked_balance": get_user(u_id)['locked_balance'] + amt}).eq("user_id", u_id).execute()
    kbd = [[InlineKeyboardButton(f"✅ PAID", callback_data=f"admpaid_{u_id}_{amt}")], [InlineKeyboardButton("❌ REJECT", callback_data=f"admrejwd_{u_id}_{amt}")]]
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=update.message.photo[-1].file_id, caption=f"💸 **WITHDRAW**\nUser: {u_id}\nAmt: ₹{amt}", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
    await update.message.reply_text("✅ Requested!", reply_markup=get_main_menu())
    return ConversationHandler.END

async def up_proof_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['w_m'] = update.callback_query.data.split("_")[2]
    await update.callback_query.message.reply_text("🎉 Send Screenshot:", reply_markup=get_cancel_kbd())
    return WAIT_WIN_PROOF

async def process_win_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_cancel(update.message.text): return await cancel_action(update, context)
    if not update.message.photo: return await update.message.reply_text("❌ Photo bhejo.")
    u_id, m_id = update.message.from_user.id, context.user_data['w_m']
    msg = await update.message.reply_text("⏳ Verifying...", reply_markup=get_main_menu())
    
    b64 = base64.b64encode(await update.message.photo[-1].get_file().download_as_bytearray()).decode('utf-8')
    ai = await analyze_image(b64, "Read Name at Rank 1. Format: [UNCROPPED/CROPPED] | Rank 1: <Name>", GEMINI_MATCH_KEY)
    
    db.table("user_matches").update({"status": "PENDING"}).eq("user_id", u_id).eq("match_id", m_id).execute()
    kbd = [[InlineKeyboardButton(f"✅ APPROVE ₹{PRIZE_MONEY}", callback_data=f"admprize_{u_id}_{m_id}")], [InlineKeyboardButton("❌ REJECT", callback_data=f"admrej_{u_id}")]]
    await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=update.message.photo[-1].file_id, caption=f"🚨 **VERIFY**\nUser: {u_id}\nIGN: `{get_user(u_id)['ff_ign']}`\nAI: {ai}", reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')
    await msg.edit_text("✅ Sent to Admin.")
    return ConversationHandler.END

# ==========================================
# 10. ADMIN COMMANDS
# ==========================================
async def admin_btns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    d = query.data.split("_"); a = d[0]
    
    if a == "admdep":
        u, utr, am = int(d[1]), d[2], float(d[3])
        db.table("users").update({"deposit_balance": get_user(u)['deposit_balance'] + am}).eq("user_id", u).execute()
        if utr != "MANUAL": db.table("used_utrs").insert({"utr": utr}).execute()
        await context.bot.send_message(chat_id=u, text=f"✅ ₹{am} added!")
        await query.message.edit_caption(caption="✅ APPROVED")
    elif a == "admpaid":
        u, am = int(d[1]), float(d[2])
        db.table("users").update({"locked_balance": get_user(u)['locked_balance'] - am}).eq("user_id", u).execute()
        await context.bot.send_message(chat_id=u, text="✅ Withdrawal Processed!")
        await query.message.edit_caption(caption="✅ PAID")
    elif a == "admrejwd":
        u, am = int(d[1]), float(d[2])
        db.table("users").update({"locked_balance": get_user(u)['locked_balance'] - am, "winning_balance": get_user(u)['winning_balance'] + am}).eq("user_id", u).execute()
        await query.message.edit_caption(caption="❌ REJECTED")
    elif a == "admprize":
        u, m = int(d[1]), d[2]
        db.table("users").update({"winning_balance": get_user(u)['winning_balance'] + PRIZE_MONEY}).eq("user_id", u).execute()
        db.table("user_matches").update({"status": "WON"}).eq("user_id", u).eq("match_id", m).execute()
        await context.bot.send_message(chat_id=u, text=f"🏆 BOOYAH! ₹{PRIZE_MONEY} added!")
        await query.message.edit_caption(caption="✅ DONE")
        if CHANNEL_ID:
            try: await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🏆 **BOOYAH!** Player **{get_user(u)['ff_ign']}** won ₹{PRIZE_MONEY}!", parse_mode='Markdown')
            except: pass
    elif a == "admrej": await query.message.edit_caption(caption="❌ REJECTED")

async def admin_hype_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🔥 **INSANE WIN!** Player **{random.choice(['❖Rᴀнᴜʟ', 'ProSniper99', 'VIPER_FF'])}** won Match #{random.randint(1000,9999)} & cashed out ₹300! 💸", parse_mode='Markdown')
        await update.message.reply_text("✅ Fake Hype Sent!")
    except Exception as e: await update.message.reply_text(f"Error: {e}")

async def admin_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_GROUP_ID: return
    await update.message.reply_text(f"🟢 **SERVER STATUS: ONLINE**\n🤖 Scrapers: {len(pyro_clients)}", parse_mode='Markdown')

# ==========================================
# 11. MAIN BOOT SEQUENCE
# ==========================================
def run_background_services():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = web.AppRunner(web.Application())
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080))).start())
    loop.run_until_complete(start_all_workers())
    loop.run_forever()

def main():
    threading.Thread(target=run_background_services, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    
    # EKDUM SIMPLE ROUTING - NO COMPLEX REGEX CLASHES
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
        states={
            WAIT_IGN: [MessageHandler(filters.ALL, save_ign_flow)],
            WAIT_ADD_AMT: [MessageHandler(filters.ALL, enter_amount)],
            WAIT_PAY_PROOF: [MessageHandler(filters.ALL, process_payment_proof)],
            WAIT_WITHDRAW_QR: [MessageHandler(filters.ALL, process_withdraw_qr)],
            WAIT_WIN_PROOF: [MessageHandler(filters.ALL, process_win_proof)]
        },
        fallbacks=[MessageHandler(filters.Regex(re.compile(r".*(CANCEL).*", re.IGNORECASE)), cancel_action)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hype", admin_hype_cmd))
    app.add_handler(CommandHandler("status", admin_status_cmd))
    app.add_handler(CallbackQueryHandler(legal_callback, pattern="^legal_"))
    app.add_handler(CallbackQueryHandler(conf_join, pattern="^confjoin_"))
    app.add_handler(CallbackQueryHandler(do_join, pattern="^dojoin_"))
    app.add_handler(CallbackQueryHandler(ask_refund, pattern="^askref_"))
    app.add_handler(CallbackQueryHandler(do_refund, pattern="^doref_"))
    app.add_handler(CallbackQueryHandler(cancel_inline, pattern="^delete_msg$"))
    app.add_handler(CallbackQueryHandler(admin_btns, pattern="^adm"))
    app.add_handler(CallbackQueryHandler(up_proof_btn, pattern="^up_proof_"))
    app.add_handler(conv_handler)
    
    logger.info("🔥 Arena Platform is Live!")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling()

if __name__ == "__main__":
    main()
