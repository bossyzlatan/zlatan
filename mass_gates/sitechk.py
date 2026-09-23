import asyncio
import random
import aiohttp
import os
import time
import re
import logging
import io
import json

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIogram Imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from aiogram import types, F, Router, Bot
from aiogram.filters import Command
from aiogram.types import FSInputFile, BufferedInputFile
from database import (
    get_db_connection, get_user_sites_db, add_user_sites_db, clear_user_sites_db,
    get_setting, set_setting,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ADMIN_IDS = [6962534443, 8428369446]

TEST_CARD = "4023961988369649|09|27|335"

try:
    from shopify_api import get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from shopify_api import get_active_server, SHOPIFY_API_KEY, ALT_API_KEY, API_SERVERS

API_TIMEOUT = 90

PROXY_LIST = []
BAD_PROXIES = set()

DEFAULT_MAX_PRICE = 5.0


def _get_max_price() -> float:
    """Read the current max price from settings (falls back to 5.0)."""
    try:
        return float(get_setting("sitechk_max_price", DEFAULT_MAX_PRICE))
    except Exception:
        return DEFAULT_MAX_PRICE


def _price_range_label() -> str:
    """Human-friendly label like '0–5.00' for use in status text."""
    return f"$0-{_get_max_price():.2f}"


DEAD_ERRORS = [
    'site error! status: 404', 'site error! status: 500', 'site error! status: 402',
    'site error! status: 502', 'site error! 503', 'site error! status: 503',
    'site not supported for now!', 'site not supported', 'connection error', 'connection error!',
    'error processing card', 'failed to get token', 'failed to get checkout',
    'failed to add to cart', 'site overloaded', 'site rate limited',
    'failed to get session token', 'unable to get payment token', 'no valid products',
    'site error! status: 403', 'payment method is not shopify!', 'not shopify!',
    'site error! status: 401', 'site requires login!',
    'site error! status: 429', 'cart failed with status 429', 'returned status 429',
    'too many requests', 'http 429', '429',
    'validation_custom', 'payments_payment_flexibility_terms_id_mismatch',
    'timeout', 'http error', 'json', 'proxy', 'curl error', 'could not resolve',
    'connect tunnel failed', 'max retries', 'GENERIC_ERROR',
    'invalid json in submit response', 'invalid json response', 'unknown result', 'payments_credit_card_generic',
    'payments_positive_amount_expected', 'inventoryreservationfailure', 'MERCHANDISE_EXPECTED_PRICE_MISMATCH',
    'Unknown Response: MERCHANDISE_EXPECTED_PRICE_MISMATCH',
    'step 1 failed', 'step 0 failed', 'step 2 failed', 'step 3 failed', 'step 4 failed',
    'step 5 failed', 'step 6 failed', 'step 7 failed', 'step 9 failed', 'step 10 failed',
    'missing stableid', 'missing buildid', 'missing sourcetoken',
    'could not extract private_access_token',
    'could not find actions js url',
    'missing proposal', 'missing submit id',
    'retryable: inventory reservation failure',
    'exceeded 30 poll attempts',
    'could not extract queuetoken',
    'could not extract identification signature',
    'could not extract session id',
    'could not extract delivery handle',
    'could not extract signedhandles',
    'could not extract shipping amount',
    'could not extract total amount',
    'could not extract receiptid',
    'could not extract sessiontoken',
    'errstoreincompatible', 'errmissingreceiptid', 'fetch products',
    'payments_credit_card_brand_not_supported', 'delivery_delivery_line_detail_changed',
    'delivery_no_delivery_strategy_available_for_mercha', 'delivery_address',
    'Unknown Response: Cart failed with status 400', 'Cart failed with status 400',
    'Unknown Response: No valid payment method found', 'No valid payment method found',
    'Response: Unknown Response: PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT',
    'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT',
    'Unknown Response: no shipping handle obtained', 'no shipping handle obtained',
    'Unknown Response: NO_PRODUCT_FOUND', 'NO_PRODUCT_FOUND',
    'Unknown Response: session token not found in HTML', 'session token not found in HTML',
    'TAX_NEW_TAX_MUST_BE_ACCEPTED',
    'Unknown Response: TAX_NEW_TAX_MUST_BE_ACCEPTED',
    'Unknown Response: DELIVERY_INVALID_POSTAL_CODE_FOR_COUNTRY', 'DELIVERY_INVALID_POSTAL_CODE_FOR_COUNTRY',
    'Unknown Response: Error parsing submit: value', 'Error parsing submit: value',
    'Unknown Response: FRAUD_SUSPECTED', 'FRAUD_SUSPECTED',
    'DECISION_RULE_BLOCK', 'Unknown Response: Cart failed with status 422',
    'Unknown Response: DECISION_RULE_BLOCK',
    'Unknown Response: ARTIFACT_DISSATISFACTION', 'ARTIFACT_DISSATISFACTION'
]

SUCCESS_RESPONSES = [
    'CARD_DECLINED', 'INVALID_CVC', 'INCORRECT_CVV', 'INSUFFICIENT_FUNDS',
    '3DS_REQUIRED', 'AMOUNT_TOO_SMALL',
    'ORDER_PAID', 'OTP_REQUIRED', 'ORDER_PLACED',
    'insufficient_funds', 'invalid_cvc'
]

router = Router()

SITES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites.txt")
BANNED_SITES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banned_sites.json")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DB-FIRST SITE MANAGEMENT (txt fallback only if DB is down)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _db_read_global_sites():
    try:
        from database import get_global_sites
        return get_global_sites()
    except Exception as e:
        logging.warning(f"[SITECHK] DB read failed, will fallback to txt: {e}")
        return None


def _db_write_global_sites(sites_list):
    try:
        from database import set_global_sites
        set_global_sites(sites_list)
        return True
    except Exception as e:
        logging.warning(f"[SITECHK] DB write failed, will fallback to txt: {e}")
        return False


def _db_add_global_sites(new_sites):
    try:
        from database import add_global_sites
        return add_global_sites(new_sites)
    except Exception as e:
        logging.warning(f"[SITECHK] DB add failed, will fallback to txt: {e}")
        return None


def _db_remove_global_site(site_url):
    try:
        from database import _get_db
        db = _get_db()
        normalized = normalize_url(site_url)
        result = db.global_sites.delete_one({"url": normalized})
        return result.deleted_count > 0
    except Exception as e:
        logging.warning(f"[SITECHK] DB remove failed, will fallback to txt: {e}")
        return None


def _db_clear_global_sites():
    try:
        from database import clear_global_sites
        return clear_global_sites()
    except Exception as e:
        logging.warning(f"[SITECHK] DB clear failed, will fallback to txt: {e}")
        return None


# ── txt file helpers (fallback only) ────────────────────────────────────────

def _txt_read_sites():
    if not os.path.exists(SITES_FILE):
        return []
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        return list(set([line.strip() for line in f if line.strip()]))


def _txt_write_sites(sites_list):
    unique = list(set(sites_list))
    with open(SITES_FILE, "w", encoding="utf-8") as f:
        for s in unique:
            f.write(f"{s}\n")
    return len(unique)


def _txt_add_sites(new_sites):
    existing = set(_txt_read_sites())
    added = 0
    for s in new_sites:
        s = s.strip()
        if not s:
            continue
        if not s.startswith(('http://', 'https://')):
            s = 'https://' + s
        s = s.rstrip('/').lower()
        if s not in existing:
            existing.add(s)
            added += 1
    _txt_write_sites(list(existing))
    return added


# ── Unified API (used by all commands) ──────────────────────────────────────

def read_sites():
    db_sites = _db_read_global_sites()
    if db_sites is not None:
        if db_sites:
            return db_sites
        legacy = _txt_read_sites()
        if legacy:
            logging.info(f"[SITECHK] Migrating {len(legacy)} legacy sites from txt → DB")
            _db_write_global_sites(legacy)
            return legacy
        return []
    logging.warning("[SITECHK] Using txt fallback for read_sites()")
    return _txt_read_sites()


def write_sites(sites_list):
    if _db_write_global_sites(sites_list):
        return len(set(sites_list))
    logging.warning("[SITECHK] Using txt fallback for write_sites()")
    return _txt_write_sites(sites_list)


def add_sites(new_sites):
    db_added = _db_add_global_sites(new_sites)
    if db_added is not None:
        return db_added
    logging.warning("[SITECHK] Using txt fallback for add_sites()")
    return _txt_add_sites(new_sites)


def remove_site(site_url):
    normalized = normalize_url(site_url)

    db_result = _db_remove_global_site(site_url)
    if db_result is not None:
        try:
            txt_sites = _txt_read_sites()
            filtered = [s for s in txt_sites if normalize_url(s) != normalized]
            if len(filtered) != len(txt_sites):
                _txt_write_sites(filtered)
        except Exception:
            pass
        return db_result

    logging.warning("[SITECHK] Using txt fallback for remove_site()")
    txt_sites = _txt_read_sites()
    filtered = [s for s in txt_sites if normalize_url(s) != normalized]
    if len(filtered) != len(txt_sites):
        _txt_write_sites(filtered)
        return True
    return False


def clear_all_sites():
    db_count = _db_clear_global_sites()
    if db_count is not None:
        try:
            _txt_write_sites([])
        except Exception:
            pass
        return db_count

    logging.warning("[SITECHK] Using txt fallback for clear_all_sites()")
    txt_sites = _txt_read_sites()
    _txt_write_sites([])
    return len(txt_sites)


# ── Proxy helpers ───────────────────────────────────────────────────────────

def load_user_proxies(user_id):
    global PROXY_LIST, BAD_PROXIES
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT proxy FROM proxies WHERE user_id = %s", (user_id,))
        PROXY_LIST = [str(row[0]).strip() for row in cursor.fetchall() if row[0] and str(row[0]).strip()]
        conn.close()
    except Exception as e:
        logging.error(f"Error loading proxies from db: {e}")
        PROXY_LIST = []
    if not PROXY_LIST:
        from gates.st import DEFAULT_PROXIES
        PROXY_LIST = DEFAULT_PROXIES.copy()
    BAD_PROXIES.clear()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def normalize_url(url: str) -> str:
    url = url.strip().lower().rstrip('/')
    if url.startswith('www.'):
        url = url[4:]
    return url


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


def get_random_proxy():
    global BAD_PROXIES
    if not PROXY_LIST:
        return ""
    available = [p for p in PROXY_LIST if p not in BAD_PROXIES]
    if not available:
        BAD_PROXIES.clear()
        available = PROXY_LIST
    return random.choice(available) if available else ""


def mark_proxy_bad(proxy):
    global BAD_PROXIES
    BAD_PROXIES.add(proxy)


_SITECHK_HTTP_SESSION = None

def _get_sitechk_http_session():
    global _SITECHK_HTTP_SESSION
    if _SITECHK_HTTP_SESSION is None or _SITECHK_HTTP_SESSION.closed:
        _SITECHK_HTTP_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            connector=aiohttp.TCPConnector(limit=1000, ssl=False)
        )
    return _SITECHK_HTTP_SESSION


async def call_site_check_api(site_url: str, cc_formatted: str, proxy: str) -> dict:
    try:
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        api_proxy = normalize_proxy(proxy)
        api_proxy_clean = api_proxy
        if api_proxy_clean.startswith("http://"):
            api_proxy_clean = api_proxy_clean[7:]
        elif api_proxy_clean.startswith("https://"):
            api_proxy_clean = api_proxy_clean[8:]
        elif api_proxy_clean.startswith("socks5://"):
            api_proxy_clean = api_proxy_clean[9:]

        servers_to_try = [get_active_server()]
        for s in API_SERVERS:
            if s not in servers_to_try:
                servers_to_try.append(s)

        _http = _get_sitechk_http_session()
        last_error = "UNKNOWN_ERROR"
        last_resp_msg = "Unknown"

        for server in servers_to_try:
            current_api = f"{server}/shopify"
            for key in [SHOPIFY_API_KEY, ALT_API_KEY]:
                params = {"site": site_url, "cc": cc_formatted, "proxy": api_proxy_clean, "key": key}
                try:
                    async with _http.get(current_api, params=params, timeout=timeout, ssl=False) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            response_msg = data.get("Response", "Unknown")
                            price_str = data.get("Price", "-1.0")
                            proxy_raw = data.get("Proxy", "Live")
                            gateway = data.get("Gateway", "")
                            status = data.get("Status", False)
                            status_bool = status.lower() == "true" if isinstance(status, str) else bool(status)
                            proxy_status = "Live" if "live" in str(proxy_raw).lower() else "Dead"
                            return {
                                "success": True, "response": response_msg, "price": price_str,
                                "proxy_status": proxy_status, "gateway": gateway,
                                "status": status_bool, "error": None
                            }
                        elif resp.status in (401, 403):
                            last_error = f"HTTP_{resp.status}"
                            last_resp_msg = f"HTTP Error {resp.status}"
                            continue
                        else:
                            last_error = f"HTTP_{resp.status}"
                            last_resp_msg = f"HTTP Error {resp.status}"
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    last_error = "CLIENT_ERROR"
                    last_resp_msg = str(ex)
                    break

        return {"success": False, "response": last_resp_msg, "price": "-1.0",
                "proxy_status": "Live", "gateway": "", "error": last_error}

    except asyncio.TimeoutError:
        return {"success": False, "response": "Timeout Error", "price": "-1.0",
                "proxy_status": "Dead", "gateway": "", "error": "TIMEOUT"}
    except aiohttp.ClientConnectorError as e:
        error_str = str(e).lower()
        if "proxy" in error_str or "tunnel" in error_str:
            return {"success": False, "response": f"Proxy Error: {str(e)[:60]}", "price": "-1.0",
                    "proxy_status": "Dead", "gateway": "", "error": "PROXY_ERROR"}
        return {"success": False, "response": f"Connection Error: {str(e)[:60]}", "price": "-1.0",
                "proxy_status": "Dead", "gateway": "", "error": "CONNECTION_ERROR"}
    except aiohttp.ClientError as e:
        return {"success": False, "response": f"Client Error: {str(e)[:60]}", "price": "-1.0",
                "proxy_status": "Dead", "gateway": "", "error": "CLIENT_ERROR"}
    except Exception as e:
        return {"success": False, "response": f"Error: {str(e)[:60]}", "price": "-1.0",
                "proxy_status": "Dead", "gateway": "", "error": "UNKNOWN_ERROR"}


async def check_site_status(site_url: str) -> tuple:
    MAX_RETRIES = 3
    # Snapshot the max price once per site so it can't change mid-run
    max_price = _get_max_price()
    for attempt in range(MAX_RETRIES):
        proxy = get_random_proxy()
        result = await call_site_check_api(site_url=site_url, cc_formatted=TEST_CARD, proxy=proxy)
        response_msg = result.get("response", "Unknown")
        price_str = result.get("price", "-1.0")
        error_type = result.get("error")

        if error_type in ["PROXY_ERROR", "TIMEOUT"]:
            mark_proxy_bad(proxy)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(0.5)
                continue

        is_dead = False
        response_lower = response_msg.lower()
        for err in DEAD_ERRORS:
            if err.lower() in response_lower:
                is_dead = True
                break

        if not result.get("success"):
            if error_type in ["JSON_PARSE_ERROR", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_404"]:
                is_dead = True

        if is_dead:
            return site_url, "REMOVE", {"Price": -1.0}, response_msg

        if any(x in response_msg.upper() for x in SUCCESS_RESPONSES):
            actual_price = -1.0
            if price_str and price_str != "-1.0":
                clean_price = re.sub(r'[^\d.]', '', str(price_str))
                if clean_price:
                    try:
                        actual_price = float(clean_price)
                    except ValueError:
                        actual_price = -1.0

            if not (0.00 <= actual_price <= max_price):
                return site_url, "REMOVE", {"Price": actual_price}, f"Price ${actual_price:.2f} (> ${max_price:.2f} Rejected) | {response_msg}"

            FAKE_CARDS = ["4003035140199121|11|29|470", "4400666318254873|03|27|336"]
            fake_charged = 0
            for fake_cc in FAKE_CARDS:
                try:
                    f_result = await call_site_check_api(site_url=site_url, cc_formatted=fake_cc, proxy=proxy)
                    f_resp = f_result.get("response", "").lower()
                    if any(k in f_resp for k in ["thank you", "order_placed", "charged", "order_paid"]):
                        fake_charged += 1
                        break
                except Exception:
                    pass

            if fake_charged >= 1:
                return site_url, "REMOVE", {"Price": actual_price}, f"Fake Charge Detected (Fake Card Approved) | {response_msg}"

            return site_url, "KEEP", {"Price": actual_price}, f"${actual_price:.2f} | {response_msg}"

        if result.get("success"):
            return site_url, "KEEP", {"Price": 0.0}, f"Unknown Response: {response_msg}"

        return site_url, "REMOVE", {"Price": -1.0}, response_msg

    return site_url, "ERROR", {"Price": -1.0}, "Max Retries Reached"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND WORKER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def run_site_checker(bot: Bot, chat_id: int, sites_to_check, command_name="Audit", status_message_id=None, user_id=None):
    global BAD_PROXIES
    BAD_PROXIES.clear()

    # Snapshot the price label once — every message in this run uses the same value
    price_label = _price_range_label()

    total_sites = len(sites_to_check)
    valid_sites = []
    working_sites_content = []
    checked_count = 0
    live_count = 0
    dead_count = 0
    duplicate_count = 0

    last_edit_time = 0
    MIN_EDIT_INTERVAL = 2.0
    CHECKS_PER_UPDATE = 20
    sem = asyncio.Semaphore(30)

    existing_sites = set()
    if command_name == "Adding":
        existing_sites = set(await asyncio.to_thread(read_sites))
        print(f"[SITECHK] Found {len(existing_sites)} existing sites for duplicate check")

    async def worker(site):
        async with sem:
            return await check_site_status(site)

    tasks = [worker(site) for site in sites_to_check]

    for future in asyncio.as_completed(tasks):
        try:
            site, status, data, resp_msg = await future
        except Exception as e:
            checked_count += 1
            dead_count += 1
            print(f"[LOG] {site} | Error: {e}")
            continue

        checked_count += 1
        print(f"[LOG] {site} | {resp_msg}")

        if status == "KEEP":
            normalized_site = normalize_url(site)
            if command_name == "Adding":
                normalized_existing = {normalize_url(s) for s in existing_sites}
                if normalized_site in normalized_existing:
                    duplicate_count += 1
                    print(f"[DUPLICATE SKIPPED] {site} already exists!")
                    continue
                normalized_valid = {normalize_url(s) for s in valid_sites}
                if normalized_site in normalized_valid:
                    duplicate_count += 1
                    print(f"[DUPLICATE SKIPPED] {site} duplicate in batch!")
                    continue

            live_count += 1
            valid_sites.append(site)
            price = data.get("Price", "0.00") if isinstance(data, dict) else "0.00"
            if isinstance(price, float):
                price = f"${price:.2f}"
            working_sites_content.append(f"{site} | Price: {price} | Response: {resp_msg}")
        else:
            dead_count += 1

        current_time = time.time()
        if (current_time - MIN_EDIT_INTERVAL > last_edit_time) or (checked_count % CHECKS_PER_UPDATE == 0):
            try:
                if status_message_id:
                    dup_text = ""
                    if duplicate_count > 0:
                        dup_text = f"\n🔄 <b>Duplicates Skipped:</b> <code>{duplicate_count}</code>"
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_message_id,
                        text=f"🔄 <b>{command_name}ing {total_sites} Sites...</b>\n"
                             f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
                             f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> <b>Kept ({price_label}):</b> <code>{live_count}</code>\n"
                             f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>Rejected:</b> <code>{dead_count}</code>\n"
                             f"🔄 <b>Checked:</b> <code>{checked_count}/{total_sites}</code>\n"
                             f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> <b>Proxies Available:</b> <code>{len(PROXY_LIST) - len(BAD_PROXIES)}/{len(PROXY_LIST)}</code>"
                             f"{dup_text}",
                        parse_mode="HTML"
                    )
                    last_edit_time = current_time
            except Exception:
                pass

    # Final dedup
    final_unique_sites = []
    seen_normalized = set()
    for site in valid_sites:
        normalized = normalize_url(site)
        if normalized not in seen_normalized:
            seen_normalized.add(normalized)
            final_unique_sites.append(site)

    removed_dupes = len(valid_sites) - len(final_unique_sites)
    if removed_dupes > 0:
        print(f"[SITECHK] Removed {removed_dupes} internal duplicates before saving")

    # ── Save results ────────────────────────────────────────────────────────
    if command_name == "Audit":
        await asyncio.to_thread(write_sites, final_unique_sites)
    elif command_name == "Adding":
        await asyncio.to_thread(add_sites, final_unique_sites)
        if user_id:
            try:
                await asyncio.to_thread(add_user_sites_db, user_id, final_unique_sites)
                print(f"[SITECHK] Saved {len(final_unique_sites)} sites to custom pool for user {user_id}")
            except Exception as ue:
                logging.error(f"Error saving to user_sites DB: {ue}")

    # Report file
    filename = f"report_{command_name.lower()}_{int(time.time())}.txt"
    file_content = "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    file_content += f"TOTAL CHECKED: {total_sites}\n"
    file_content += f"WORKING SITES (Price {price_label}): {len(final_unique_sites)}\n"
    file_content += f"REJECTED (Dead/High Price): {dead_count}\n"
    if duplicate_count > 0 or removed_dupes > 0:
        file_content += f"DUPLICATES SKIPPED: {duplicate_count + removed_dupes}\n"
    file_content += f"PROXIES USED: {len(PROXY_LIST)} | BAD: {len(BAD_PROXIES)}\n"
    file_content += "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    file_content += "\n".join(working_sites_content) if working_sites_content else "No valid sites found within price range!"

    try:
        def _write_report():
            with open(filename, "w", encoding="utf-8") as f:
                f.write(file_content)
        await asyncio.to_thread(_write_report)

        if status_message_id:
            try:
                dup_final = ""
                if (duplicate_count + removed_dupes) > 0:
                    dup_final = f"\n<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>Duplicates Blocked:</b> <code>{duplicate_count + removed_dupes}</code>"
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=f"<tg-emoji emoji-id='6242135305697106689'>🎁</tg-emoji> <b>{command_name} Complete!</b>\n\n"
                         f"<b>Total Checked:</b> {total_sites}\n"
                         f"<b>Valid ({price_label}):</b> {len(final_unique_sites)} <tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji>\n"
                         f"<b>Rejected:</b> {dead_count} <tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji>\n"
                         f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
                         f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> <b>Proxies Used:</b> {len(PROXY_LIST)} | <b>Bad:</b> {len(BAD_PROXIES)}"
                         f"{dup_final}",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        await bot.send_document(chat_id=chat_id, document=FSInputFile(filename),
                                caption=f"📜 <b>{command_name} Report (Deduplicated)</b>",
                                parse_mode="HTML")
        try:
            os.remove(filename)
        except:
            pass
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>Error:</b> {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /sitechk — Audit & Clean (DB first, txt fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("sitechk"))
async def sitechk_command(message: types.Message):
    user_id = message.from_user.id
    from sub import get_premium_status
    is_premium, _ = get_premium_status(user_id)
    if not is_premium:
        await message.reply("<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝗽𝗴𝗿𝗮𝗱𝗲 𝘆𝗼𝘂𝗿 𝗽𝗹𝗮𝗻 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀 𝗳𝗲𝗮𝘁𝘂𝗿𝗲.", parse_mode="HTML")
        return
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    load_user_proxies(user_id)
    bot = message.bot
    chat_id = message.chat.id

    sites = await asyncio.to_thread(read_sites)
    if not sites:
        await message.answer("📭 <b>No sites found in the global pool.</b>", parse_mode="HTML")
        return

    price_label = _price_range_label()

    status_msg = await message.answer(
        f"🔄 <b>Starting Audit on {len(sites)} Sites...</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"🔄 <b>Checked:</b> <code>0/{len(sites)}</code>\n"
        f"<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> <b>Kept ({price_label}):</b> <code>0</code>\n"
        f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>Rejected:</b> <code>0</code>\n"
        f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> <b>Proxies:</b> <code>{len(PROXY_LIST)}</code>",
        parse_mode="HTML"
    )

    asyncio.create_task(run_site_checker(bot, chat_id, sites,
                                          command_name="Audit",
                                          status_message_id=status_msg.message_id))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /addsite — Add & Verify New Sites
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("addsite"))
async def addsite_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    load_user_proxies(user_id)
    bot = message.bot
    chat_id = message.chat.id

    doc = message.document
    if not doc and message.reply_to_message:
        doc = message.reply_to_message.document

    if not doc:
        await message.answer(
            "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>Please reply to a file or upload a file containing sites with /addsite.</b>",
            parse_mode="HTML"
        )
        return

    try:
        file_info = await bot.get_file(doc.file_id)
        destination = io.BytesIO()
        await bot.download_file(file_info.file_path, destination)
        destination.seek(0)
        text = destination.read().decode('utf-8', errors='ignore')

        new_sites = []
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            match = re.search(r'(https?://\S+)', line)
            if match:
                url = match.group(1)
            else:
                first_word = line.split()[0]
                if '.' in first_word and not first_word.startswith(('http://', 'https://')):
                    url = f"https://{first_word}"
                else:
                    continue
            url = url.rstrip('.,;:!?)\'"')
            new_sites.append(url)

        new_sites = list(set(new_sites))
        if not new_sites:
            await message.answer("<tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> <b>No valid sites found in file.</b>", parse_mode="HTML")
            return

    except Exception as e:
        logging.error(f"Error downloading file: {e}", exc_info=True)
        await message.answer(f"<tg-emoji emoji-id='6234166879663987'>❌</tg-emoji> <b>Error reading file:</b> {e}", parse_mode="HTML")
        return

    price_label = _price_range_label()

    status_msg = await message.answer(
        f"🔄 <b>Starting Addition of {len(new_sites)} Sites...</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"🔄 <b>Checked:</b> <code>0/{len(new_sites)}</code>\n"
        f"<tg-emoji emoji-id='6242135305697106689'>🎁</tg-emoji> <b>Added ({price_label}):</b> <code>0</code>\n"
        f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>Rejected:</b> <code>0</code>\n"
        f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>Duplicates:</b> <code>0</code>\n"
        f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> <b>Proxies:</b> <code>{len(PROXY_LIST)}</code>",
        parse_mode="HTML"
    )

    asyncio.create_task(run_site_checker(bot, chat_id, new_sites,
                                          command_name="Adding",
                                          status_message_id=status_msg.message_id,
                                          user_id=user_id))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /mysites — Custom user pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("mysites"))
@router.message(Command("mysite"))
async def mysites_command(message: types.Message):
    user_id = message.from_user.id
    user_sites = await asyncio.to_thread(get_user_sites_db, user_id)
    if not user_sites:
        await message.reply(
            "<b>🌐 𝗬𝗼𝘂𝗿 𝗖𝘂𝘀𝘁𝗼𝗺 𝗦𝗶𝘁𝗲𝘀:</b> <code>0</code>\n\n"
            "You are currently using the <b>Global Sites Pool</b>.\n"
            "Use <code>/addsite</code> with a .txt file to add your own sites!",
            parse_mode="HTML"
        )
        return

    text = f"<b>🌐 𝗬𝗼𝘂𝗿 𝗖𝘂𝘀𝘁𝗼𝗺 𝗦𝗶𝘁𝗲𝘀 𝗣𝗼𝗼𝗹:</b> <code>{len(user_sites)} sites</code>\n\n"
    text += "Your checks will automatically use <b>ONLY your sites</b>!\n"
    text += "Use <code>/clearsites</code> to revert back to global sites.\n\n"
    preview = "\n".join(f"• <code>{s}</code>" for s in user_sites[:10])
    text += preview
    if len(user_sites) > 10:
        text += f"\n<i>...and {len(user_sites) - 10} more</i>"

    content = "\n".join(user_sites).encode('utf-8')
    file = BufferedInputFile(content, filename=f"my_sites_{user_id}.txt")
    await message.reply_document(file, caption=text, parse_mode="HTML")


@router.message(Command("clearsites"))
@router.message(Command("clearsite"))
async def clearsites_command(message: types.Message):
    user_id = message.from_user.id
    deleted = await asyncio.to_thread(clear_user_sites_db, user_id)
    await message.reply(
        f"🗑️ <b>Cleared {deleted} custom sites from your pool.</b>\n\n"
        "You are now back on the <b>Global Sites Pool</b>.",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /siteall — Download global sites
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("siteall"))
async def siteall_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    sites = await asyncio.to_thread(read_sites)
    if not sites:
        await message.answer("📭 <b>Global site pool is empty.</b>", parse_mode="HTML")
        return

    filename = f"full_sites_list_{int(time.time())}.txt"
    try:
        def _write_file():
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"Total Sites: {len(sites)} (Deduplicated)\n\n")
                f.write("\n".join(sites))
        await asyncio.to_thread(_write_file)

        await message.answer_document(
            document=FSInputFile(filename),
            caption=f"📜 <b>Total Sites:</b> <code>{len(sites)}</code> ✨ (No Duplicates)",
            parse_mode="HTML"
        )
        os.remove(filename)
    except Exception as e:
        await message.answer(f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>Error:</b> {e}", parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /removeall — Clear every global site
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("removeall"))
async def removeall_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    count = await asyncio.to_thread(clear_all_sites)
    if count == 0:
        await message.answer("📭 <b>Global site pool is already empty.</b>", parse_mode="HTML")
        return

    await message.answer(
        f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> <b>All sites have been successfully removed.</b>\n\n"
        f"<b>Removed:</b> <code>{count}</code> sites",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /dedupe — Force deduplicate global pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("dedupe"))
async def dedupe_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    sites = await asyncio.to_thread(read_sites)
    if not sites:
        await message.answer("📭 <b>Global site pool is empty.</b>", parse_mode="HTML")
        return

    original_count = len(sites)
    unique = {}
    for s in sites:
        n = normalize_url(s)
        if n not in unique:
            unique[n] = s
    deduped = list(unique.values())

    final_count = await asyncio.to_thread(write_sites, deduped)
    removed = original_count - final_count

    if removed > 0:
        await message.answer(
            f"✨ <b>Deduplication Complete!</b>\n\n"
            f"<b>Original:</b> <code>{original_count}</code>\n"
            f"<b>Removed:</b> <code>{removed}</code> duplicates\n"
            f"<b>Final:</b> <code>{final_count}</code> unique sites",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> <b>No duplicates found!</b>\n\n"
            f"<b>Total Sites:</b> <code>{final_count}</code> (All Unique)",
            parse_mode="HTML"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /proxyinfo — Proxy diagnostics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("proxyinfo"))
async def proxyinfo_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    available = len(PROXY_LIST) - len(BAD_PROXIES)
    text = (
        f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> <b>Proxy Information</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"📊 <b>Total Proxies:</b> <code>{len(PROXY_LIST)}</code>\n"
        f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> <b>Available:</b> <code>{available}</code>\n"
        f"<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>Bad/Dead:</b> <code>{len(BAD_PROXIES)}</code>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
    )
    for i, proxy in enumerate(PROXY_LIST, 1):
        status = "❌ Dead" if proxy in BAD_PROXIES else "✅ Live"
        if "@" in proxy:
            parts = proxy.split("@")
            host_part = parts[1] if len(parts) > 1 else proxy
            text += f"<code>{i}.</code> {host_part} - {status}\n"
        else:
            text += f"<code>{i}.</code> {proxy[:30]}... - {status}\n"
    await message.answer(text, parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /resetproxy — Clear bad list
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("resetproxy"))
async def resetproxy_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    global BAD_PROXIES
    cleared_count = len(BAD_PROXIES)
    BAD_PROXIES.clear()

    await message.answer(
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> <b>Proxy List Reset!</b>\n\n"
        f"<b>Cleared:</b> <code>{cleared_count}</code> bad proxies\n"
        f"<b>Available Now:</b> <code>{len(PROXY_LIST)}</code>",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /remsite — Remove & ban a single site
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("remsite"))
async def remsite_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    args = message.text.split()[1:]
    if not args:
        await message.answer("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>Usage:</b> /remsite {site_url}\nExample: /remsite example.com", parse_mode="HTML")
        return

    target_site = normalize_url(args[0])
    removed_from_active = await asyncio.to_thread(remove_site, target_site)

    banned_sites = []
    if os.path.exists(BANNED_SITES_FILE):
        try:
            with open(BANNED_SITES_FILE, "r", encoding="utf-8") as f:
                banned_sites = json.load(f)
        except Exception:
            banned_sites = []

    if target_site not in banned_sites:
        banned_sites.append(target_site)
        try:
            with open(BANNED_SITES_FILE, "w", encoding="utf-8") as f:
                json.dump(banned_sites, f, indent=4)
        except Exception as e:
            await message.answer(f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>Error writing to banned_sites.json:</b> <code>{str(e)}</code>", parse_mode="HTML")
            return

    active_removed_str = "and removed from active list" if removed_from_active else "(was not in active list)"
    await message.answer(
        f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> <b>Site Banned successfully!</b>\n\n"
        f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> <b>Site:</b> <code>{target_site}</code>\n"
        f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Site is now added to <code>banned_sites.json</code> {active_removed_str} and will never be used again.",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /setprice — Change the max accepted price for site checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(Command("setprice"))
async def setprice_command(message: types.Message):
    """Admin-only: set the maximum accepted price for /sitechk and /addsite."""
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ <b>You are not authorized.</b>", parse_mode="HTML")
        return

    args = message.text.split()[1:]
    if not args:
        current = _get_max_price()
        await message.answer(
            f"<b>💵 Current max price ➛</b> <code>${current:.2f}</code>\n\n"
            f"<b>Usage:</b> <code>/setprice 10</code>\n"
            f"<i>Sets the highest price a site is allowed to have for it to be kept.</i>",
            parse_mode="HTML"
        )
        return

    raw = args[0].replace("$", "").strip()
    try:
        new_price = float(raw)
        if new_price < 0:
            raise ValueError("Must be non-negative")
    except ValueError:
        await message.answer(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
            "<b>Invalid amount.</b> Example: <code>/setprice 7.5</code>",
            parse_mode="HTML"
        )
        return

    old_price = _get_max_price()
    ok = await asyncio.to_thread(set_setting, "sitechk_max_price", new_price)

    if ok:
        await message.answer(
            f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> "
            f"<b>Max price updated</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"<b>Before ➛</b> <code>${old_price:.2f}</code>\n"
            f"<b>After  ➛</b> <code>${new_price:.2f}</code>\n\n"
            f"<i>Applies immediately to the next /sitechk or /addsite run.</i>",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
            "<b>Failed to save setting.</b>",
            parse_mode="HTML"
        )
