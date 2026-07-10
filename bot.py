"""
Free Fire Tournament Bot - Ultimate Enhanced Version
Features: Multi-platform scraping, self-learning, god-level verification
Version: 2.0.0
"""

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
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from collections import deque

from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
                          filters, ContextTypes, ConversationHandler)
from aiohttp import web
from pyrogram import Client as PyroClient, filters as pyfilters
from pyrogram.errors import SessionPasswordNeeded
import pytz

# Optional imports with fallbacks
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

# ============================================================================
# CONFIGURATION
# ============================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
PUBLIC_CHANNEL_ID = os.environ.get("PUBLIC_CHANNEL_ID", "")
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "placeholder")
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

# Constants
ENTRY_FEE_OPTIONS = [30, 40, 50]
MATCH_LIVE_MINS = 15
REFUND_WINDOW_MINS = 8
MAX_PLAYERS = 10

# Conversation States
(WAIT_IGN, WAIT_ADD_AMT, WAIT_PAY_PROOF, WAIT_WITHDRAW_QR, WAIT_WIN_PROOF,
 WAIT_SUPPORT_CHAT, WAIT_WORKER_PHONE, WAIT_WORKER_OTP, WAIT_WORKER_PASS,
 WAIT_REPORT_ACCUSED, WAIT_REPORT_DESC, WAIT_REPORT_PROOF) = range(12)

# Global Variables
join_locks = {}
pyro_clients = {}
user_throttle = {}
user_cache = {}
pending_workers = {}
error_memory = deque(maxlen=100)  # Store last 100 errors
success_patterns = deque(maxlen=100)  # Store last 100 successes

# Pre-configured Sources (Directly Added)
TELEGRAM_CHANNELS = [
    "indiaofficialfreefire", "qulishtech", "Free_Fire_Gaming",
    "dktech_hindi", "TechProfitChannel", "freefirepanel_free",
    "FFCustomRooms", "FreeFireTournament", "GarenaCustomRoom",
    "FFMaxCustomRoom", "FreeFireGiveaways", "IndianFFPlayers"
]

REDDIT_SUBREDDITS = [
    "FreeFireIndia", "freefire", "FreeFireMax", "GarenaFreeFire",
    "FreeFireEsports", "IndianGaming", "MobileGamingIndia"
]

YOUTUBE_CHANNELS = [
    "UCUcCOOEBp6MK99MvJMRoFEQ",  # Total Gaming
    "UCAheXRvVYFhGpYdYJMRoFEQ",  # Desi Gamers
    "UCnY8YFgHyEZFkPq5GkMFejw",  # AS Gaming
    "UCKZb7G7M9Bm5FoFbrTilE2g",  # Gyan Gaming
    "UCJXni8KE7TkMYP3-fvS3XQ"   # Two Side Gamers
]

DISCORD_SERVERS = [
    # Pre-configured public FF Discord server IDs
    "freefire-india", "ff-tournaments", "custom-rooms-ff",
    "garena-freefire", "ff-max-india"
]

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def safe_md(text: str) -> str:
    """Escape markdown special characters"""
    if not text:
        return ""
    text = str(text)
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def compress_image_to_b64(byte_array, max_width=800, quality=80):
    """Compress image and convert to base64"""
    if not HAS_PILLOW:
        return base64.b64encode(byte_array).decode('utf-8')
    try:
        img = Image.open(io.BytesIO(byte_array))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        if img.width > max_width:
            ratio = max_width / float(img.width)
            height = int(float(img.height) * ratio)
            img = img.resize((max_width, height), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=quality)
        return base64.b64encode(out.getvalue()).decode('utf-8')
    except Exception as e:
        logger.warning(f"Image compression fallback: {e}")
        return base64.b64encode(byte_array).decode('utf-8')

def calculate_similarity(str1: str, str2: str) -> float:
    """Calculate string similarity (0-1)"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()

# ============================================================================
# UTR MANAGEMENT
# ============================================================================

def save_utr_safely(utr, user_id=None, amount=None):
    """Save UTR with fallback strategies"""
    if not db or not utr or utr.startswith("MANUAL"):
        return
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
    """Check if UTR is already used"""
    if not db or not utr or utr.startswith("MANUAL"):
        return False
    try:
        res = db.table("used_utrs").select("*").eq("utr", utr).execute()
        return bool(res.data)
    except Exception as e:
        logger.error(f"UTR check err: {e}")
        return False

def get_utr_prefixes():
    """Get today and yesterday UTR prefixes"""
    now = datetime.now(IST)
    yest = now - timedelta(days=1)
    return [str(now.year)[-1] + now.strftime("%j"),
            str(yest.year)[-1] + yest.strftime("%j")]

# ============================================================================
# AI POOL MANAGER
# ============================================================================

class AIPoolManager:
    """Manages multiple AI API keys with round-robin"""
    
    def __init__(self):
        self.keys = []
        self._load_keys()
        self.current_idx = 0

    def _load_keys(self):
        pool_str = os.environ.get("AI_POOL_KEYS", "")
        if pool_str:
            for k in pool_str.split(","):
                k = k.strip()
                if k and k not in self.keys:
                    self.keys.append(k)
        for name, val in os.environ.items():
            if any(name.startswith(p) for p in
                   ["GROQ_", "GEMINI_", "CLOUDFLARE_", "CF_",
                    "MISTRAL_", "SAMBANOVA_", "CEREBRAS_", "OPENROUTER_"]):
                for sub_k in val.split(","):
                    sub_k = sub_k.strip()
                    if sub_k and sub_k not in self.keys:
                        self.keys.append(sub_k)
        logger.info(f"AI SuperPool: {len(self.keys)} keys loaded.")

    def get_next_key(self):
        if not self.keys:
            return None
        k = self.keys[self.current_idx]
        self.current_idx = (self.current_idx + 1) % len(self.keys)
        return k

ai_pool = AIPoolManager()

async def call_ai_unified(prompt, image_b64=None, system_context=""):
    """Call AI with fallback to multiple providers"""
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

    # Tier-2: Pool
    for _ in range(max(1, len(ai_pool.keys))):
        key = ai_pool.get_next_key()
        if not key:
            break
        try:
            if key.startswith("AIzaSy"):  # Gemini
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
            if image_b64:
                continue
            # Other providers (Groq, Cerebras, etc.)
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
            logger.warning(f"Pool key fail: {e}")
            continue
    return "AI_FAILED"

# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def is_throttled(user_id):
    """Rate limiting"""
    now = datetime.now()
    last = user_throttle.get(user_id)
    if last and (now - last).total_seconds() < 1.0:
        return True
    user_throttle[user_id] = now
    return False

def _is_admin(uid):
    """Check if user is admin"""
    return (ADMIN_USER_ID != 0 and uid == ADMIN_USER_ID)

def get_user(user_id):
    """Get or create user"""
    now = datetime.now()
    if user_id in user_cache:
        data, ts = user_cache[user_id]
        if (now - ts).total_seconds() < 10.0:
            return data
    if not db:
        dummy = {"user_id": user_id, "deposit_balance": 100.0,
                 "winning_balance": 0.0, "bonus_balance": 10.0,
                 "locked_balance": 0.0, "ff_ign": "TEST_USER",
                 "last_login": "", "is_18_plus": True,
                 "is_restricted": False, "is_banned": False,
                 "referrer_id": None}
        user_cache[user_id] = (dummy, now)
        return dummy
    try:
        res = db.table("users").select("*").eq("user_id", user_id).execute()
        if not res.data:
            new = {"user_id": user_id, "deposit_balance": 0.0,
                   "winning_balance": 0.0, "bonus_balance": 0.0,
                   "locked_balance": 0.0, "ff_ign": "", "last_login": "",
                   "is_18_plus": False, "is_restricted": False,
                   "is_banned": False, "referrer_id": None}
            try:
                db.table("users").insert(new).execute()
            except Exception:
                pass
            user_cache[user_id] = (new, now)
            return new
        user_cache[user_id] = (res.data[0], now)
        return res.data[0]
    except Exception as e:
        logger.error(f"get_user err: {e}")
        return {"user_id": user_id, "deposit_balance": 0.0,
                "winning_balance": 0.0, "bonus_balance": 0.0,
                "locked_balance": 0.0, "ff_ign": "", "last_login": "",
                "is_18_plus": False, "is_restricted": False,
                "is_banned": False}

def invalidate_user_cache(uid):
    """Invalidate user cache"""
    user_cache.pop(uid, None)

def deduct_balance(user_id, amount):
    """Deduct balance from user"""
    if not db:
        return True
    invalidate_user_cache(user_id)
    u = get_user(user_id)
    rem = amount
    b, d, w = u['bonus_balance'], u['deposit_balance'], u['winning_balance']
    db_ = min(b, rem)
    rem -= db_
    b -= db_
    dd_ = min(d, rem)
    rem -= dd_
    d -= dd_
    dw_ = min(w, rem)
    rem -= dw_
    w -= dw_
    if rem > 0:
        return False
    try:
        db.table("users").update(
            {"bonus_balance": b, "deposit_balance": d,
             "winning_balance": w}).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id)
        return True
    except Exception as e:
        logger.error(f"deduct err: {e}")
        return False

def credit_balance(user_id, field, amount):
    """Credit balance to user"""
    if not db:
        return
    invalidate_user_cache(user_id)
    u = get_user(user_id)
    try:
        db.table("users").update(
            {field: u.get(field, 0.0) + amount}
        ).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id)
    except Exception as e:
        logger.error(f"credit err: {e}")

def random_price():
    """Generate random entry fee and prize"""
    fee = random.choice(ENTRY_FEE_OPTIONS)
    return fee, fee * 10

def get_match_fee(match):
    """Get match entry fee"""
    fee = match.get('entry_fee') or match.get('fee')
    if fee:
        return float(fee)
    return 50.0  # Default

def get_match_prize(match):
    """Get match prize"""
    prize = match.get('prize_money') or match.get('prize')
    if prize:
        return float(prize)
    return get_match_fee(match) * 10

# ============================================================================
# BAN SYSTEM
# ============================================================================

def is_user_banned(user_id):
    """Check if user is banned"""
    try:
        u = get_user(user_id)
        return bool(u.get('is_banned', False))
    except:
        return False

def ban_user(user_id, reason=""):
    """Ban user permanently"""
    if not db:
        return
    try:
        db.table("users").update(
            {"is_banned": True}
        ).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id)
        logger.info(f"User {user_id} BANNED. Reason: {reason}")
    except Exception as e:
        logger.error(f"ban err: {e}")

def unban_user(user_id):
    """Unban user"""
    if not db:
        return
    try:
        db.table("users").update(
            {"is_banned": False}
        ).eq("user_id", user_id).execute()
        invalidate_user_cache(user_id)
        logger.info(f"User {user_id} UNBANNED.")
    except Exception as e:
        logger.error(f"unban err: {e}")

# ============================================================================
# UI FUNCTIONS
# ============================================================================

def get_main_menu():
    """Get main menu keyboard"""
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎮 PLAY FREE FIRE"),
         KeyboardButton("🎯 MY MATCHES")],
        [KeyboardButton("💰 ADD FUNDS"),
         KeyboardButton("💸 WITHDRAW CASH")],
        [KeyboardButton("🎁 DAILY REWARD"),
         KeyboardButton("🤝 HELP / SUPPORT")]], resize_keyboard=True)

def get_cancel_kbd():
    """Get cancel keyboard"""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Cancel & Go Back")]], resize_keyboard=True)

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current action"""
    try:
        if update.callback_query:
            await update.callback_query.message.delete()
        else:
            await update.message.reply_text(
                "🚫 Action Cancelled.",
                reply_markup=get_main_menu())
    except:
        pass
    return ConversationHandler.END

# Winner quotes
WINNER_QUOTES = [
    "Asli champion wahi hai jo girke uthe aur dobara jeete! 🔥",
    "Ek BOOYAH se kahani nahi banti — aur matches khelein, aur jeetein! 💪",
    "Aaj aap jeete, kal aur bhi bada prize aapka intezaar kar raha hai! 🏆",
    "Har match ek nayi jung hai — taiyar raho aur duniya hila do! ⚡",
    "Winner banna aasan nahi, par aapne kar dikhaya! Aage bhi aise hi khelo! 🎮",
    "Aap is match ke winner hain! Aur bhi matches hain — try karo aur jeeto! 👑",
]

# ============================================================================
# PDF CERTIFICATE GENERATION (Gaming Style)
# ============================================================================

async def generate_gaming_cert(ign, match_id, prize, kills=0, mode="Solo"):
    """Generate gaming-style PDF certificate"""
    if not HAS_REPORTLAB:
        return None
    
    buf = io.BytesIO()
    width, height = 800, 550
    
    c = canvas.Canvas(buf, pagesize=(width, height))
    
    # Dark background
    c.setFillColor(colors.Color(0.05, 0.05, 0.15))
    c.rect(0, 0, width, height, fill=1)
    
    # Neon glow effects
    c.setFillColor(colors.Color(0.1, 0.0, 0.3, 0.5))
    c.rect(0, height - 100, width, 100, fill=1)
    
    c.setFillColor(colors.Color(0.0, 0.1, 0.3, 0.5))
    c.rect(0, 0, width, 80, fill=1)
    
    # Gold border
    c.setStrokeColor(colors.Color(1.0, 0.84, 0.0))
    c.setLineWidth(4)
    c.rect(20, 20, width - 40, height - 40)
    
    # Inner border
    c.setStrokeColor(colors.Color(1.0, 0.84, 0.0, 0.5))
    c.setLineWidth(1)
    c.rect(28, 28, width - 56, height - 56)
    
    # Corner decorations
    c.setFillColor(colors.Color(1.0, 0.84, 0.0))
    c.circle(50, height - 50, 8, fill=1)
    c.circle(width - 50, height - 50, 8, fill=1)
    c.circle(50, 50, 8, fill=1)
    c.circle(width - 50, 50, 8, fill=1)
    
    # Header
    c.setFillColor(colors.Color(1.0, 0.84, 0.0))
    c.setFont("Helvetica-Bold", 48)
    c.drawCentredString(width/2, height - 80, "BOOYAH!")
    
    # Subtitle
    c.setFillColor(colors.Color(0.8, 0.8, 0.9))
    c.setFont("Helvetica", 14)
    c.drawCentredString(width/2, height - 110,
                        "OFFICIAL TOURNAMENT CHAMPION")
    
    # Divider
    c.setStrokeColor(colors.Color(1.0, 0.84, 0.0, 0.5))
    c.setLineWidth(2)
    c.line(100, height - 125, width - 100, height - 125)
    
    # Winner name
    c.setFillColor(colors.Color(1.0, 1.0, 1.0))
    c.setFont("Helvetica", 12)
    c.drawCentredString(width/2, height - 155,
                        "THIS CERTIFICATE IS PRESENTED TO")
    
    c.setFillColor(colors.Color(1.0, 0.84, 0.0))
    c.setFont("Helvetica-Bold", 36)
    c.drawCentredString(width/2, height - 195, ign)
    
    # Achievement details
    c.setFillColor(colors.Color(0.8, 0.8, 0.9))
    c.setFont("Helvetica", 13)
    c.drawCentredString(width/2, height - 230,
        f"For dominating Match #{match_id} in {mode} mode")
    
    if kills > 0:
        c.drawCentredString(width/2, height - 255,
            f"with {kills} kills and winning the championship!")
    else:
        c.drawCentredString(width/2, height - 255,
            "and winning the championship!")
    
    # Prize box
    c.setFillColor(colors.Color(0.1, 0.1, 0.2))
    c.roundRect(width/2 - 120, height - 310, 240, 45, 10, fill=1)
    
    c.setStrokeColor(colors.Color(1.0, 0.84, 0.0))
    c.setLineWidth(2)
    c.roundRect(width/2 - 120, height - 310, 240, 45, 10)
    
    c.setFillColor(colors.Color(1.0, 0.84, 0.0))
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(width/2, height - 293, f"PRIZE: Rs.{prize}")
    
    # Stats section
    y_stats = height - 370
    c.setFillColor(colors.Color(0.7, 0.7, 0.8))
    c.setFont("Helvetica", 11)
    
    c.drawString(80, y_stats, f"Mode: {mode}")
    if kills > 0:
        c.drawString(80, y_stats - 20, f"Kills: {kills}")
    c.drawString(80, y_stats - 40, "Rank: #1")
    
    date_str = datetime.now(IST).strftime("%d-%b-%Y")
    c.drawRightString(width - 80, y_stats, f"Date: {date_str}")
    c.drawRightString(width - 80, y_stats - 20, f"Match: #{match_id}")
    c.drawRightString(width - 80, y_stats - 40, f"IGN: {ign}")
    
    # Divider
    c.setStrokeColor(colors.Color(1.0, 0.84, 0.0, 0.3))
    c.line(80, y_stats - 55, width - 80, y_stats - 55)
    
    # Footer
    c.setFillColor(colors.Color(0.5, 0.5, 0.6))
    c.setFont("Helvetica", 9)
    serial = f"FFC-{match_id}"
    c.drawCentredString(width/2, 55,
        f"Certificate ID: {serial} | Verified by @FreeFireCustomRoom_Bot")
    c.drawCentredString(width/2, 40,
        f"Issued: {date_str} | This is a digitally verified certificate")
    
    # Stamp
    c.setFillColor(colors.Color(1.0, 0.84, 0.0, 0.2))
    c.setFont("Helvetica-Bold", 50)
    c.saveState()
    c.translate(width - 130, 120)
    c.rotate(15)
    c.drawCentredString(0, 0, "VERIFIED")
    c.restoreState()
    
    c.save()
    buf.seek(0)
    return buf

# ============================================================================
# START COMMAND
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user_id = update.message.from_user.id
    
    # Ban check
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
                if rid != user_id:
                    referrer_id = rid
            except:
                pass
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
        except:
            pass
    
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
        f"🤝 Refer & Earn Rs.10:\n👉 `{safe_md(ref_link)}`",
        reply_markup=get_main_menu(), parse_mode='Markdown')
    return ConversationHandler.END

async def legal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle legal verification"""
    query = update.callback_query
    await query.answer()
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

# ============================================================================
# MAIN MENU HANDLER
# ============================================================================

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu buttons"""
    if not update.message or not update.message.text:
        return ConversationHandler.END
    uid = update.message.from_user.id
    if is_throttled(uid):
        return ConversationHandler.END
    if is_user_banned(uid):
        await update.message.reply_text("🚫 You are banned.")
        return ConversationHandler.END
    
    text = update.message.text.upper()
    if "CANCEL" in text:
        return await cancel_action(update, context)
    
    user = get_user(uid)
    if user.get('is_restricted'):
        await update.message.reply_text("🚨 Account suspended.")
        return ConversationHandler.END
    
    # PLAY FREE FIRE
    if "PLAY" in text:
        if not user.get('ff_ign', '').strip():
            await update.message.reply_text(
                "⚠️ Pehle apna FF Nickname type karein:",
                reply_markup=get_cancel_kbd())
            return WAIT_IGN
        
        if db:
            exp = (datetime.now(IST) -
                   timedelta(minutes=MATCH_LIVE_MINS)).isoformat()
            try:
                db.table("matches").delete().lt(
                    "created_at", exp).eq("scraped", True).execute()
            except:
                pass
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
            if is_scraped:
                status += " 🌐"
            
            msg += (f"🔥 **Match #{safe_md(m['match_id'])}** | "
                    f"{status}\n"
                    f"🎟 `[{progress}] {seats_taken}/{MAX_PLAYERS}`\n"
                    f"💰 Entry: Rs.{fee} | Prize: Rs.{prize}\n\n")
            
            share_url = (f"https://t.me/{context.bot.username}"
                         f"?start=match_{m['match_id']}")
            kbd.append([
                InlineKeyboardButton(
                    f"🔒 JOIN #{m['match_id']} (Rs.{fee})",
                    callback_data=f"confjoin_{m['match_id']}"),
                InlineKeyboardButton(
                    "📢 INVITE",
                    url=f"https://t.me/share/url?url={share_url}"
                        f"&text=Ajao FF Tournament!")])
        
        await update.message.reply_text(
            msg, reply_markup=InlineKeyboardMarkup(kbd),
            parse_mode='Markdown')
        return ConversationHandler.END
    
    # ADD FUNDS
    elif "ADD FUNDS" in text:
        await update.message.reply_text(
            "💸 Kitne Rupaye add karne hain? (Min: Rs.30)",
            reply_markup=get_cancel_kbd())
        return WAIT_ADD_AMT
    
    # WITHDRAW
    elif "WITHDRAW" in text:
        tot = round(user['deposit_balance'] + user['winning_balance']
                     + user['bonus_balance'], 2)
        msg = (f"💰 Total: Rs.{tot}\n"
               f"🟢 Winnings: Rs.{user['winning_balance']}\n"
               f"🔵 Deposit: Rs.{user['deposit_balance']}\n"
               f"🎁 Bonus: Rs.{user['bonus_balance']}\n\n"
               f"(Min withdraw: Rs.200 Winnings)")
        if user['winning_balance'] < 200:
            await update.message.reply_text(
                msg + "\n❌ Minimum Rs.200 Winnings needed.")
            return ConversationHandler.END
        await update.message.reply_text(
            msg + "\n📸 UPI QR Code bhejein:",
            reply_markup=get_cancel_kbd())
        return WAIT_WITHDRAW_QR
    
    # DAILY REWARD
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
            if val == 64:
                reward = 10.0
            elif val in (1, 22, 43):
                reward = 5.0
            elif val % 5 == 0:
                reward = 3.0
            else:
                reward = random.choice([1.0, 2.0])
            credit_balance(uid, 'bonus_balance', reward)
            if db:
                db.table("users").update(
                    {"last_login": today}
                ).eq("user_id", uid).execute()
                invalidate_user_cache(uid)
            await update.message.reply_text(
                f"🎉 **Rs.{reward} Bonus Cash mila!** 🎁")
        return ConversationHandler.END
    
    # MY MATCHES
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
                if not mdata:
                    continue
                m = mdata[0]
                fee = get_match_fee(m)
                prize = get_match_prize(m)
                
                msg += (f"🔥 **#{safe_md(mid)}** | "
                        f"Entry Rs.{fee} | Prize Rs.{prize}\n"
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
                            if jt.tzinfo is None:
                                jt = IST.localize(jt)
                            mins = (now_ist - jt).total_seconds() / 60
                        except:
                            mins = 999
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
            
            msg += (f"💰 **Balance:**\n"
                    f"🔵 Deposit: Rs.{user.get('deposit_balance', 0)}\n"
                    f"🟢 Winnings: Rs.{user.get('winning_balance', 0)}\n"
                    f"🎁 Bonus: Rs.{user.get('bonus_balance', 0)}")
            
            await update.message.reply_text(
                msg, parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(kbd) if kbd else None)
        except Exception as e:
            logger.error(f"MY MATCHES error: {e}")
            await update.message.reply_text(
                f"⚠️ MY MATCHES load nahi ho paya. Dobara try karein.")
        return ConversationHandler.END
    
    # HELP / SUPPORT
    elif "HELP" in text or "SUPPORT" in text:
        safe_name = safe_md(user.get('ff_ign') or 'Unconfigured')
        await update.message.reply_text(
            f"🟢 **AI SUPPORT ONLINE**\n"
            f"👤 Profile: `{safe_name}`\n\n"
            f"Apni problem niche type karein:",
            reply_markup=get_cancel_kbd(), parse_mode='Markdown')
        return WAIT_SUPPORT_CHAT
    
    return ConversationHandler.END

# ============================================================================
# AI SUPPORT CHAT
# ============================================================================

async def handle_support_chat(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Handle support chat messages"""
    text = update.message.text
    if text and "CANCEL" in text.upper():
        return await cancel_action(update, context)
    uid = update.message.from_user.id
    await context.bot.send_chat_action(chat_id=uid, action='typing')
    user = get_user(uid)
    sys_p = (f"You are Free Fire Tournament AI Support. "
             f"Player: ID={uid}, IGN={user.get('ff_ign','')}, "
             f"Deposit=Rs.{user.get('deposit_balance',0)}, "
             f"Winnings=Rs.{user.get('winning_balance',0)}. "
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

# ============================================================================
# IGN SAVE FLOW
# ============================================================================

async def save_ign_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save user's FF IGN"""
    if not update.message or not update.message.text:
        return WAIT_IGN
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

# ============================================================================
# DEPOSIT FLOW
# ============================================================================

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enter deposit amount"""
    if not update.message or not update.message.text:
        return WAIT_ADD_AMT
    if "CANCEL" in update.message.text.upper():
        return await cancel_action(update, context)
    try:
        amt = float(update.message.text)
        if amt < 30:
            raise ValueError
    except:
        await update.message.reply_text("❌ Min Rs.30. Sahi number likhein.")
        return WAIT_ADD_AMT
    context.user_data['dep_amt'] = amt
    upi_id = "dipanshu153@fam"
    qr_url = (f"https://api.qrserver.com/v1/create-qr-code/"
              f"?size=300x300&data=upi://pay?pa={upi_id}"
              f"%26pn=ArenaEsports%26am={amt}%26cu=INR")
    await update.message.reply_photo(
        photo=qr_url,
        caption=f"💳 PAY Rs.{amt} to `{upi_id}`\nScreenshot bhejein:",
        parse_mode='Markdown', reply_markup=get_cancel_kbd())
    return WAIT_PAY_PROOF

async def animate_progress(msg, base_text):
    """Animate progress bar"""
    for s in ["40% [████░░░░░░]", "60% [██████░░░░]",
              "80% [████████░░]"]:
        await asyncio.sleep(3.2)
        try:
            await msg.edit_text(f"{base_text} {s}")
        except:
            break

async def process_payment_proof(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    """Process payment proof screenshot"""
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
            f"✅ APPROVE Rs.{ai_amt}",
            callback_data=f"admdep_{uid}_{utr}_{ai_amt}")],
               [InlineKeyboardButton(
            "❌ REJECT",
            callback_data=f"admrej_{uid}")]]
        dossier = (f"🚨 **DEPOSIT REQUEST**\n"
                   f"👤 `{uid}` | Claimed: Rs.{claimed} | "
                   f"AI: **Rs.{ai_amt}**\n🔢 UTR: `{safe_md(utr)}`")
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

# ============================================================================
# WITHDRAW FLOW
# ============================================================================

async def process_withdraw_qr(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Process withdraw QR code"""
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
        f"✅ PAID Rs.{amt}",
        callback_data=f"admpaid_{uid}_{amt}")],
           [InlineKeyboardButton(
        "❌ REJECT",
        callback_data=f"admrejwd_{uid}_{amt}")]]
    if ADMIN_GROUP_ID:
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=update.message.photo[-1].file_id,
            caption=f"💸 **WITHDRAWAL**\n`{uid}` | Rs.{amt}",
            reply_markup=InlineKeyboardMarkup(kbd),
            parse_mode='Markdown')
    await update.message.reply_text(
        f"✅ Withdraw request submitted. Rs.{amt} locked.",
        reply_markup=get_main_menu())
    return ConversationHandler.END

# ============================================================================
# WINNER VERIFICATION (GOD-LEVEL with IGN Matching)
# ============================================================================

async def up_proof_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle I WON button"""
    query = update.callback_query
    await query.answer()
    mid = query.data.split("_")[2]
    context.user_data['win_match'] = mid
    await query.message.reply_text(
        f"🎉 Match #{safe_md(mid)} ka scoreboard screenshot bhejein:",
        reply_markup=get_cancel_kbd(), parse_mode='Markdown')
    return WAIT_WIN_PROOF

async def process_win_proof(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    """Process winner proof with GOD-LEVEL verification"""
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
        # Get user's registered IGN
        user = get_user(uid)
        registered_ign = user.get('ff_ign', '').strip().lower()
        
        if not registered_ign:
            anim.cancel()
            await msg.edit_text(
                "❌ **IGN NOT SET!**\n"
                "Pehle /start karo aur apna FF IGN set karo!")
            return ConversationHandler.END
        
        # Get screenshot
        pf = await update.message.photo[-1].get_file()
        ba = await pf.download_as_bytearray()
        b64 = compress_image_to_b64(ba)
        
        # AI Analysis
        prompt = """Analyze this Free Fire match result screenshot.
Extract:
1. Winner's IGN (in-game name) — EXACT as shown
2. Is this a BOOYAH/Winner screen?
3. Kill count
4. Rank/Position
5. Match mode (Solo/Duo/Squad)
6. Any suspicious signs (edited, cropped, fake?)

Reply EXACTLY in this format:
WINNER_IGN: <exact name from screenshot>
IS_WINNER: YES/NO
KILLS: <number>
RANK: <number>
MODE: <Solo/Duo/Squad>
SUSPICIOUS: YES/NO
REASON: <if suspicious, why>"""
        
        ai_result = await call_ai_unified(prompt, image_b64=b64)
        anim.cancel()
        
        # Parse AI response
        def extract_field(text, field):
            pattern = rf'{field}:\s*(.+?)(?:\n|$)'
            match = re.search(pattern, text, re.IGNORECASE)
            return match.group(1).strip() if match else ""
        
        winner_ign = extract_field(ai_result, "WINNER_IGN").strip().lower()
        is_winner = extract_field(ai_result, "IS_WINNER").upper() == "YES"
        suspicious = extract_field(ai_result, "SUSPICIOUS").upper() == "YES"
        kills = extract_field(ai_result, "KILLS")
        mode = extract_field(ai_result, "MODE")
        
        # Check 1: Is this a winner screen?
        if not is_winner:
            await msg.edit_text(
                "❌ **NOT A WINNER SCREEN!**\n"
                "Ye BOOYAH/Winner ka screenshot nahi lag raha. "
                "Kripaya sahi winner screenshot bhejein!")
            return ConversationHandler.END
        
        # Check 2: Suspicious screenshot?
        if suspicious:
            reason = extract_field(ai_result, "REASON")
            await msg.edit_text(
                f"⚠️ **SUSPICIOUS SCREENSHOT!**\n"
                f"Reason: {reason}\n"
                f"Kripaya original, uncropped screenshot bhejein!")
            return ConversationHandler.END
        
        # Check 3: IGN MATCH (Most Important!)
        similarity = calculate_similarity(registered_ign, winner_ign)
        
        if similarity < 0.8:  # 80% match required
            await msg.edit_text(
                f"❌ **IGN MISMATCH!**\n\n"
                f"📝 Aapka registered IGN: `{safe_md(registered_ign)}`\n"
                f"🖼️ Screenshot mein winner: `{safe_md(winner_ign)}`\n\n"
                f"Ye dono match nahi kar rahe! "
                f"Kripaya apne registered IGN se match karta hua "
                f"screenshot bhejein.\n\n"
                f"Agar IGN change karna hai toh /start karo aur "
                f"naya IGN set karo.",
                parse_mode='Markdown')
            return ConversationHandler.END
        
        # Get match prize
        prize = DEFAULT_PRIZE
        if db:
            mdata = db.table("matches").select("*").eq(
                "match_id", mid).execute().data
            if mdata:
                prize = get_match_prize(mdata[0])
            db.table("user_matches").update(
                {"status": "PENDING"}
            ).eq("user_id", uid).eq("match_id", mid).execute()
        
        # Prepare admin message
        safe_ign = safe_md(registered_ign)
        dossier = (
            f"🏆 **WINNER CLAIM — VERIFIED** ✅\n\n"
            f"📌 Match: **#{safe_md(mid)}**\n"
            f"👤 User: `{uid}`\n"
            f"🎮 Registered IGN: `{safe_ign}`\n"
            f"🖼️ Screenshot IGN: `{safe_md(winner_ign)}`\n"
            f"✅ IGN Match: **{similarity*100:.0f}%**\n"
            f"🎯 Kills: **{kills}**\n"
            f"🎮 Mode: **{mode}**\n"
            f"🔍 Suspicious: **NO**\n\n"
            f"💰 Is match ka Prize: **Rs.{prize}**\n"
            f"📝 Ye user is specific match ke liye "
            f"bol raha hai 'main jeeta hoon'")
        
        kbd = [[InlineKeyboardButton(
            f"✅ APPROVE (Rs.{prize})",
            callback_data=f"admprize_{uid}_{mid}")],
               [InlineKeyboardButton(
            "❌ REJECT",
            callback_data=f"admrejprize_{uid}_{mid}")]]
        
        if ADMIN_GROUP_ID:
            await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID, photo=pf.file_id,
                caption=dossier, parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(kbd))
        
        await msg.edit_text(
            "✅ **Winner Screenshot Verified!**\n"
            f"IGN Match: {similarity*100:.0f}% ✅\n"
            "Admin review ke baad prize milega!")
    except Exception as e:
        anim.cancel()
        logger.error(f"Win proof err: {e}")
        await msg.edit_text("⚠️ Error. Try again.")
        return WAIT_WIN_PROOF
    return ConversationHandler.END

# ============================================================================
# MATCH JOIN/CONFIRM/REFUND
# ============================================================================

async def conf_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm join match"""
    query = update.callback_query
    await query.answer()
    mid = query.data.split("_")[1]
    fee = DEFAULT_ENTRY_FEE
    if db:
        mdata = db.table("matches").select("*").eq(
            "match_id", mid).execute().data
        if mdata:
            fee = get_match_fee(mdata[0])
    kbd = [[InlineKeyboardButton(
        f"✅ YES, JOIN (Rs.{fee})",
        callback_data=f"dojoin_{mid}")],
           [InlineKeyboardButton("🔙 CANCEL",
        callback_data="delete_msg")]]
    await query.message.reply_text(
        f"Match #{safe_md(mid)} join — Rs.{fee} pay karna hai?",
        reply_markup=InlineKeyboardMarkup(kbd), parse_mode='Markdown')

async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actually join match"""
    query = update.callback_query
    await query.answer()
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
    if lock_key not in join_locks:
        join_locks[lock_key] = asyncio.Lock()
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
    """Ask for refund confirmation"""
    query = update.callback_query
    await query.answer()
    mid = query.data.split("_")[1]
    fee = DEFAULT_ENTRY_FEE
    if db:
        mdata = db.table("matches").select("*").eq(
            "match_id", mid).execute().data
        if mdata:
            fee = get_match_fee(mdata[0])
    kbd = [[InlineKeyboardButton(
        f"✅ YES, REFUND Rs.{fee}",
        callback_data=f"doref_{mid}")],
           [InlineKeyboardButton("🔙 NO",
        callback_data="delete_msg")]]
    await query.message.edit_text(
        f"Refund Rs.{fee} from Match #{safe_md(mid)}?",
        reply_markup=InlineKeyboardMarkup(kbd))

async def do_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process refund"""
    query = update.callback_query
    await query.answer()
    mid = query.data.split("_")[1]
    uid = query.from_user.id
    if not db:
        return
    um = db.table("user_matches").select("*").eq(
        "user_id", uid).eq("match_id", mid).execute().data
    if not um:
        return
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
        f"✅ Refund Rs.{fee} done!")

async def cancel_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel inline callback"""
    try:
        await update.callback_query.message.delete()
    except:
        pass

# ============================================================================
# ADMIN COMMANDS
# ============================================================================

async def cmd_creatematch(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """Create new match"""
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID:
        return
    fee = float(context.args[0]) if len(context.args) > 0 \
          else random.choice(ENTRY_FEE_OPTIONS)
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
            db.table("matches").insert({
                "match_id": mid, "room_id": "TBD",
                "room_pass": "TBD", "tickets_left": tickets,
                "created_at": datetime.now(IST).isoformat()
            }).execute()
    await update.message.reply_text(
        f"✅ **Match #{safe_md(mid)} Created!**\n"
        f"Entry Rs.{fee} | Prize Rs.{prize} | Seats {tickets}\n"
        f"`/setroom {mid} <id> <pass>`",
        parse_mode='Markdown')

async def cmd_setroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set room ID and password"""
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID:
        return
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
    else:
        joined = []
    for u in joined:
        try:
            await context.bot.send_message(
                chat_id=u['user_id'],
                text=f"🚨 **ROOM READY! #{safe_md(mid)}** 🎮\n"
                     f"🔑 `{safe_md(rid)}` | 🔐 `{safe_md(rpass)}`\n"
                     f"⚡ Jaldi join karo!",
                parse_mode='Markdown')
        except:
            pass
    if PUBLIC_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=PUBLIC_CHANNEL_ID,
                text=f"🔥 **ROOM LIVE! #{safe_md(mid)}** 🎮\n"
                     f"🔑 `{safe_md(rid)}`\n"
                     f"👉 @FreeFireCustomRoom_Bot 🚀",
                parse_mode='Markdown')
        except:
            pass
    await update.message.reply_text(
        f"✅ Room updated! Notified {len(joined)} players.")

async def cmd_hype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send hype message to public channel"""
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID:
        return
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
    """Show match status"""
    if not _is_admin(update.message.from_user.id) and \
       update.message.chat_id != ADMIN_GROUP_ID:
        return
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
        fee = get_match_fee(m)
        prize = get_match_prize(m)
        taken = MAX_PLAYERS - m['tickets_left']
        txt = (f"🔴 **Match #{safe_md(mid)}**\n"
               f"Seats: {taken}/{MAX_PLAYERS}\n"
               f"Entry Rs.{fee} | Prize Rs.{prize}")
        await update.message.reply_text(txt, parse_mode='Markdown')

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban user permanently"""
    if not _is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        target = int(context.args[0])
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
    except:
        pass

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unban user"""
    if not _is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        target = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    unban_user(target)
    await update.message.reply_text(
        f"✅ **USER UNBANNED** (`{target}`)",
        parse_mode='Markdown')

async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show banned users list"""
    if not _is_admin(update.message.from_user.id):
        return
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
    """Find user by IGN or ID"""
    if not _is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /finduser <name or ID>")
        return
    query = " ".join(context.args)
    if not db:
        await update.message.reply_text("DB not available.")
        return
    try:
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
                f"💰 Dep Rs.{u.get('deposit_balance',0)} | "
                f"Win Rs.{u.get('winning_balance',0)}\n"
                f"🚫 {banned}",
                parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_scraperstatus(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    """Show scraper status"""
    if not _is_admin(update.message.from_user.id):
        return
    msg = "🔍 **SCRAPER STATUS**\n\n"
    # This would be implemented with actual scraper tracking
    msg += "Scrapers are running in background.\n"
    msg += "Check logs for detailed status."
    await update.message.reply_text(msg)

# ============================================================================
# WORKER MANAGEMENT (Pyrogram)
# ============================================================================

async def cmd_addworker(update: Update,
                        context: ContextTypes.DEFAULT_TYPE):
    """Add Pyrogram worker"""
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
    """Enter worker OTP"""
    uid = update.message.from_user.id
    if uid not in pending_workers:
        return ConversationHandler.END
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
    """Enter worker 2FA password"""
    uid = update.message.from_user.id
    if uid not in pending_workers:
        return ConversationHandler.END
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
    """Delete worker"""
    if not _is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /delworker +919876543210")
        return
    phone = context.args[0]
    if db:
        db.table("workers").delete().eq("phone", phone).execute()
    if phone in pyro_clients:
        try:
            await pyro_clients[phone].stop()
        except:
            pass
        pyro_clients.pop(phone, None)
    await update.message.reply_text(
        f"🗑️ Worker `{safe_md(phone)}` deleted!")

# ============================================================================
# ADMIN CALLBACK HANDLER
# ============================================================================

async def admin_callback_handler(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    """Handle admin callback queries"""
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    
    parts = query.data.split("_")
    action = parts[0]
    
    try:
        # DEPOSIT APPROVE
        if action == "admdep":
            tuid = int(parts[1])
            amount = float(parts[-1])
            utr = "_".join(parts[2:-1])
            if is_utr_used(utr):
                try:
                    await query.message.edit_caption(
                        caption=(query.message.caption or "") +
                        "\n\n⚠️ DUPLICATE UTR!")
                except:
                    pass
                return
            save_utr_safely(utr, tuid, amount)
            credit_balance(tuid, 'deposit_balance', amount)
            try:
                await context.bot.send_message(
                    chat_id=tuid,
                    text=f"🎉 **PAYMENT APPROVED!**\n"
                         f"Rs.{amount} deposit mein add!",
                    parse_mode='Markdown')
            except:
                try:
                    await context.bot.send_message(
                        chat_id=tuid,
                        text=f"Payment Approved! Rs.{amount} added.")
                except:
                    pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n✅ APPROVED Rs.{amount} by "
                    f"{query.from_user.first_name}")
            except:
                pass
        
        # PRIZE APPROVE
        elif action == "admprize":
            tuid = int(parts[1])
            mid = "_".join(parts[2:])
            user = get_user(tuid)
            safe_ign = safe_md(
                user.get('ff_ign', f'Player_{tuid}'))
            
            prize = DEFAULT_PRIZE
            if db:
                mdata = db.table("matches").select("*").eq(
                    "match_id", mid).execute().data
                if mdata:
                    prize = get_match_prize(mdata[0])
            
            credit_balance(tuid, 'winning_balance', prize)
            if db:
                try:
                    db.table("user_matches").update(
                        {"status": "WON"}
                    ).eq("user_id", tuid).eq(
                        "match_id", mid).execute()
                except:
                    pass
            
            # Referral bonus
            ref_id = user.get('referrer_id')
            if ref_id and db:
                try:
                    credit_balance(ref_id, 'bonus_balance', 10.0)
                    await context.bot.send_message(
                        chat_id=ref_id,
                        text=f"🎉 Referral reward! "
                             f"`{safe_ign}` won! Rs.10 bonus!",
                        parse_mode='Markdown')
                except:
                    pass
            
            # PDF Certificate
            pdf = await generate_gaming_cert(
                user.get('ff_ign', 'Champion'), mid, prize)
            
            # DM to winner
            quote = random.choice(WINNER_QUOTES)
            try:
                await context.bot.send_message(
                    chat_id=tuid,
                    text=f"🏆 **WINNER VERIFIED!**\n\n"
                         f"Match #{safe_md(mid)} — "
                         f"Prize Rs.{prize} credited!\n\n"
                         f"💬 {quote}\n\n"
                         f"🎮 Aur matches khelein aur jeetein!",
                    parse_mode='Markdown')
                if pdf:
                    await context.bot.send_document(
                        chat_id=tuid, document=pdf,
                        filename=f"Winner_{mid}.pdf",
                        caption="🏆 Official Certificate!")
            except:
                pass
            
            # Public channel hype
            if PUBLIC_CHANNEL_ID:
                hype = (
                    f"🏆👑 **CHAMPION!** 👑🏆\n\n"
                    f"🔥 Match #{safe_md(mid)}\n"
                    f"🎮 {safe_ign}\n"
                    f"💰 Won **Rs.{prize}** Cash!\n\n"
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
                except:
                    pass
            
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n🏆 APPROVED by "
                    f"{query.from_user.first_name}")
            except:
                pass
        
        # REJECT (Deposit)
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
                except:
                    pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n❌ REJECTED by "
                    f"{query.from_user.first_name}")
            except:
                pass
        
        # REJECT PRIZE
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
                except:
                    pass
            if db:
                try:
                    db.table("user_matches").update(
                        {"status": "JOINED"}
                    ).eq("user_id", tuid).eq(
                        "match_id", mid).execute()
                except:
                    pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\n❌ REJECTED (fake screenshot) by "
                    f"{query.from_user.first_name}")
            except:
                pass
        
        # WITHDRAW PAID/REJECTED
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
                        text=f"✅ Withdrawal Rs.{amt} done!")
                except:
                    pass
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
                             f"Rs.{amt} back to Winnings.")
                except:
                    pass
            try:
                await query.message.edit_caption(
                    caption=(query.message.caption or "") +
                    f"\n\nDone by {query.from_user.first_name}")
            except:
                pass
    
    except Exception as e:
        logger.error(f"Admin callback error: {e}")

# ============================================================================
# PYROGRAM WORKERS STARTUP
# ============================================================================

async def start_all_workers():
    """Start all Pyrogram worker clients"""
    if not db:
        return
    try:
        workers = db.table("workers").select("*").execute().data
        for w in workers:
            phone = w['phone']
            if not w.get('session_string'):
                continue
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
                    if not text and not message.photo:
                        return
                    has_num = bool(re.search(r'\d{6,10}', text))
                    kws = ["id", "password", "pass", "pwd",
                           "room", "custom", "freefire", "ff",
                           "match", "join", "booyah"]
                    has_kw = any(k in text.lower() for k in kws)
                    if not has_num and not has_kw:
                        return
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

async def _add_scraped_match(room_id, room_pass, source,
                             entry_fee=None, prize=None):
    """Add scraped match to database"""
    if not db:
        return
    if not room_id or not room_pass:
        return
    
    # Dedup check
    try:
        one_hr = (datetime.now(IST) - timedelta(hours=1)).isoformat()
        exists = db.table("matches").select("*").eq(
            "room_id", room_id).gt("created_at", one_hr).execute()
        if exists.data:
            return
    except:
        pass
    
    if not entry_fee:
        entry_fee = random.choice(ENTRY_FEE_OPTIONS)
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
        try:
            db.table("matches").insert({
                "match_id": mid, "room_id": room_id,
                "room_pass": room_pass,
                "tickets_left": MAX_PLAYERS,
                "created_at": datetime.now(IST).isoformat()
            }).execute()
        except Exception as e:
            logger.error(f"Add scraped match fail: {e}")

# ============================================================================
# HEALTH CHECK SERVER
# ============================================================================

async def health_check(request):
    """Health check endpoint"""
    return web.Response(
        text="Free Fire Tournament Bot OK", status=200)

async def start_web_server():
    """Start health check web server"""
    port = int(os.environ.get("PORT", 8080))
    app_web = web.Application()
    app_web.router.add_get("/", health_check)
    runner = web.AppRunner(app_web)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

# ============================================================================
# MAIN FUNCTION
# ============================================================================

def main():
    """Main entry point"""
    if not BOT_TOKEN:
        logger.error("CRITICAL: BOT_TOKEN missing!")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("addworker", cmd_addworker),
            MessageHandler(filters.TEXT & ~filters.COMMAND,
                           handle_menu),
            CallbackQueryHandler(up_proof_btn,
                                 pattern=r"^up_proof_"),
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
    application.add_handler(
        CallbackQueryHandler(admin_callback_handler,
                             pattern=r"^(adm|banref|refall|disreport)"))
    
    # Conversation handler
    application.add_handler(conv_handler)
    
    async def post_init(app: Application):
        asyncio.create_task(start_web_server())
        asyncio.create_task(start_all_workers())
    
    application.post_init = post_init
    logger.info("Starting Free Fire Tournament Bot...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
