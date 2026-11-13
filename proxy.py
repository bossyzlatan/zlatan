import re
import logging
import aiohttp
import asyncio
import ipaddress
import json
from datetime import datetime
from urllib.parse import quote, unquote
from typing import Optional
from html import escape as html_escape

from aiogram import types, F, Router
from aiogram.types import BufferedInputFile

from database import (
    create_user,
    get_proxies,
    add_proxy,
    remove_proxy,
    clear_proxies,
    count_proxies,
    get_premium_status,
    get_all_proxies_with_users,
)

router = Router()

ADMIN_IDS = {6962534443, 8428369446}

# ─── PERF TUNING ────────────────────────────────────────────
MAX_CONCURRENT_CHECKS = 50
PROXY_TIMEOUT = 8
PROGRESS_EDIT_INTERVAL = 8

# ─── MULTI-ENDPOINT IP LOOKUP ──────────────────────────────
IP_ENDPOINTS = [
    "https://api.ipify.org?format=json",
    "https://api64.ipify.org?format=json",
    "https://ipinfo.io/ip",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PROXY PARSING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Fix 2: include socks5h:// and socks4a:// so they're caught here rather
# than slipping through and being mangled into a bogus HTTP proxy by the
# generic `user:pass@host:port` regex below.
_SOCKS_RE = re.compile(r'^socks[45][ah]?://', re.IGNORECASE)
_SCHEME_RE = re.compile(
    r'^(?P<p>http|https|socks4[ah]?|socks5[ah]?)://', re.IGNORECASE
)


def is_socks_proxy_input(line: str) -> bool:
    """Fix 2: detect every SOCKS scheme (incl. socks5h://, socks4a://)."""
    return bool(_SOCKS_RE.match((line or "").strip()))


# Fix 10: reject garbage hostnames/ports before they land in the DB.
def _is_plausible_host(h: str) -> bool:
    h = (h or "").strip()
    if not h or len(h) > 253:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(re.match(
            r'^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$', h
        ))


def parse_proxy_input(proxy_input):
    s = proxy_input.strip()
    protocol = 'http'

    protocol_match = _SCHEME_RE.match(s)
    if protocol_match:
        p = protocol_match.group('p').lower()
        s = s[len(protocol_match.group('p')) + 3:]

        # Fix 2: reject every SOCKS variant, including 5h / 4a.
        if p.startswith('socks4') or p.startswith('socks5'):
            return None

        # Fix 4: normalize https:// → http://. Users mean "HTTP proxy that
        # tunnels HTTPS traffic", not TLS-to-the-proxy. Also makes db_format
        # round-trips lossless (previously scheme was discarded).
        protocol = 'http'

    def is_valid(ip, port):
        # Fix 10: real host + port validation (was only port.isdigit()).
        if not _is_plausible_host(ip):
            return False
        if not (port and port.isdigit()):
            return False
        try:
            pnum = int(port)
        except ValueError:
            return False
        return 0 < pnum <= 65535

    match = re.match(r'^([^:@]+):([^:@]+)@([^:@]+):(\d+)$', s)
    if match:
        user, password, ip, port = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    match = re.match(r'^([^:@]+):([^:@]+)\s+([^:@]+):(\d+)$', s)
    if match:
        user, password, ip, port = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    match = re.match(r'^([^:@]+):(\d+)\s+([^:@]+)\s+([^:@]+)$', s)
    if match:
        ip, port, user, password = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    match = re.match(r'^([^:@]+)\s+([^:@]+)\s+([^:@]+):(\d+)$', s)
    if match:
        user, password, ip, port = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    # db_format round-trips via quote/unquote.
    match = re.match(r'^([^:@\s]+)\s+([^:@\s]+)\s+([^:@\s]+)\s+(\d+)$', s)
    if match:
        user, password, ip, port = match.groups()
        if user == "-" and password == "-":
            if is_valid(ip, port):
                return build_dict(None, None, ip, port, protocol, proxy_input)
        else:
            if is_valid(ip, port):
                return build_dict(unquote(user), unquote(password),
                                  ip, port, protocol, proxy_input)

    match = re.match(r'^([^:@]+):([^:@]+):([^:@]+):(\d+)$', s)
    if match:
        user, password, ip, port = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    match = re.match(r'^([^:@]+):(\d+):([^:@]+):([^:@]+)$', s)
    if match:
        ip, port, user, password = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    # plain ip:port (no auth)
    match = re.match(r'^([^:@\s]+):(\d+)$', s)
    if match:
        ip, port = match.groups()
        if is_valid(ip, port):
            return build_dict(None, None, ip, port, protocol, proxy_input)

    return None


def build_dict(user, password, ip, port, protocol, original_input):
    user = (user or "").strip()
    password = (password or "").strip()
    ip = ip.strip()
    port = port.strip()

    if not user or not password:
        return {
            "user": None,
            "password": None,
            "ip": ip,
            "port": port,
            "original_format": original_input,
            "url_format": f"{protocol}://{ip}:{port}",
            "db_format": f"- - {ip} {port}",
            "http_format": f"http://{ip}:{port}",
        }

    encoded_user = quote(user, safe='')
    encoded_pass = quote(password, safe='')
    return {
        "user": user,
        "password": password,
        "ip": ip,
        "port": port,
        "original_format": original_input,
        "url_format": f"{protocol}://{encoded_user}:{encoded_pass}@{ip}:{port}",
        "db_format": f"{encoded_user} {encoded_pass} {ip} {port}",
        "http_format": f"http://{encoded_user}:{encoded_pass}@{ip}:{port}",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PROXY LIVENESS CHECK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _extract_ip(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return data.get("ip") or data.get("IP") or data.get("query")
        except Exception:
            pass
    parts = text.split()
    return parts[0][:64] if parts else None


async def check_proxy_live(proxy_url: str, session: aiohttp.ClientSession,
                           timeout: float = PROXY_TIMEOUT):
    """
    Returns (status, info):
      (True,  {...}) → proxy is live
      (False, {...}) → proxy is dead (proxy-level connection failure only)
      (None,  {...}) → status unknown (could not verify) — caller MUST keep it
    """
    rate_limit_hits = 0
    last_error = "unknown"

    for endpoint in IP_ENDPOINTS:
        try:
            async with session.get(
                endpoint,
                proxy=proxy_url,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=timeout, connect=timeout / 2),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    ip = _extract_ip(text)
                    return True, {"ip": ip or "unknown", "endpoint": endpoint}
                elif resp.status == 429:
                    rate_limit_hits += 1
                    last_error = "endpoint rate limited"
                    continue
                elif resp.status == 403:
                    # Endpoint blocked this specific proxy IP — proxy works.
                    return True, {"ip": "unknown", "endpoint": endpoint, "http": 403}
                elif resp.status == 503:
                    # Endpoint is down — don't vouch for the proxy.
                    last_error = f"endpoint 503 ({endpoint})"
                    continue
                else:
                    last_error = f"HTTP {resp.status} from {endpoint}"
                    continue
        except asyncio.TimeoutError:
            last_error = "timeout"
            continue
        except aiohttp.ClientProxyConnectionError:
            return False, {"error": "proxy connection failed"}
        except aiohttp.ClientConnectorError:
            return False, {"error": "proxy DNS/connect failed"}
        except aiohttp.ClientHttpProxyError as e:
            return False, {"error": f"proxy HTTP error: {str(e)[:50]}"}
        except aiohttp.ClientError as e:
            last_error = f"{type(e).__name__}: {str(e)[:40]}"
            continue
        except Exception as e:
            # Fix 9: unexpected exceptions must be visible.
            last_error = str(e)[:60]
            logging.debug(
                "[check_proxy_live] unexpected %s on %s: %s",
                type(e).__name__, endpoint, e,
            )
            continue

    # No proxy-level error observed → unknown, never False.
    if rate_limit_hits >= len(IP_ENDPOINTS) - 1:
        return None, {"error": "all IP endpoints rate-limited — try again later"}
    return None, {"error": last_error or "all endpoints failed (non-proxy)"}


async def check_proxies_parallel(proxies_list, max_concurrent: int = MAX_CONCURRENT_CHECKS,
                                 on_progress=None):
    semaphore = asyncio.Semaphore(max_concurrent)
    timeout = aiohttp.ClientTimeout(total=PROXY_TIMEOUT, connect=PROXY_TIMEOUT / 2)
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json,text/plain,*/*'}

    done_count = 0
    live_count = 0
    # Fix 6: seed with -interval so the first progress edit fires immediately.
    last_edit_time = -PROGRESS_EDIT_INTERVAL
    lock = asyncio.Lock()

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async def check_with_semaphore(proxy_data):
            nonlocal done_count, live_count, last_edit_time
            async with semaphore:
                status, info = await check_proxy_live(proxy_data['url_format'], session)

                # Fix 11: snapshot under lock, await on_progress outside.
                should_edit = False
                done_snapshot = 0
                live_snapshot = 0
                async with lock:
                    done_count += 1
                    if status is True:
                        live_count += 1
                    if on_progress:
                        now = asyncio.get_event_loop().time()
                        if (now - last_edit_time >= PROGRESS_EDIT_INTERVAL
                                or done_count == len(proxies_list)):
                            last_edit_time = now
                            should_edit = True
                            done_snapshot = done_count
                            live_snapshot = live_count

                if should_edit and on_progress:
                    try:
                        await on_progress(done_snapshot, len(proxies_list), live_snapshot)
                    except Exception:
                        pass

                return (proxy_data, status, info)

        tasks = [check_with_semaphore(p) for p in proxies_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            # An exception is "couldn't verify", not "dead".
            processed.append((proxies_list[i], None, {"error": str(res)[:60]}))
        else:
            processed.append(res)
    return processed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADD-PROXY RESULT CLASSIFIER (fix 5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _classify_add_result(r):
    """
    Fix 5: normalise add_proxy() return value.

    Accepts True/False/None/Exception. None is treated as success (common
    Python idiom: function side-effects and returns nothing). Anything
    else is logged and treated as an error.
    """
    if isinstance(r, BaseException):
        return "error"
    if r is True:
        return "added"
    if r is False:
        return "clone"
    if r is None:
        return "added"
    logging.warning(
        "[proxy] add_proxy() returned unexpected type %s: %r",
        type(r).__name__, r,
    )
    return "error"


# Fix 8: bound DB fan-out so a 5000-proxy upload doesn't stall the executor.
_DB_WRITE_CONCURRENCY = 20


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /proxy — background add pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def process_proxies_background(bot, message: types.Message, valid_proxies: list):
    user_id = message.from_user.id
    total_count = len(valid_proxies)

    status_msg = await message.reply(
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>Processing...</b>\n\n"
        f"<b>Checking ➛</b> <code>0/{total_count}</code>\n"
        f"<b>Live ➛</b> <b>0</b> ✅",
        parse_mode="HTML"
    )

    async def _progress(done, total, live):
        pct = int(done * 100 / total) if total else 0
        try:
            await bot.edit_message_text(
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                text=(
                    f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>Processing...</b>\n\n"
                    f"<b>Checking ➛</b> <code>{done}/{total}</code> ({pct}%)\n"
                    f"<b>Live ➛</b> <b>{live}</b> ✅"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

    results_list = await check_proxies_parallel(valid_proxies, on_progress=_progress)

    live_count    = sum(1 for _, is_live, _ in results_list if is_live is True)
    dead_count    = sum(1 for _, is_live, _ in results_list if is_live is False)
    unknown_count = sum(1 for _, is_live, _ in results_list if is_live is None)

    live_entries = [p for (p, is_live, _) in results_list if is_live is True]

    if live_entries:
        # Fix 8: bounded concurrency around the DB writes.
        _add_sem = asyncio.Semaphore(_DB_WRITE_CONCURRENCY)

        async def _safe_add(db_format):
            async with _add_sem:
                return await asyncio.to_thread(add_proxy, user_id, db_format)

        add_results = await asyncio.gather(
            *[_safe_add(p['db_format']) for p in live_entries],
            return_exceptions=True,
        )

        classifications = [_classify_add_result(r) for r in add_results]
        added_count   = sum(1 for c in classifications if c == "added")
        clone_count   = sum(1 for c in classifications if c == "clone")
        errored_count = sum(1 for c in classifications if c == "error")
    else:
        add_results = []
        classifications = []
        added_count = clone_count = errored_count = 0

    is_bulk = total_count > 1
    if is_bulk:
        caption = (
            f"<b><tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> Complete!</b>\n\n"
            f"<b>Total ➛</b> <code>{total_count}</code>\n"
            f"<b>Live ➛</b> <b>{live_count}</b> ✅\n"
            f"<b>Added ➛</b> <b>{added_count}</b> 💾\n"
            f"<b>Clones ➛</b> <b>{clone_count}</b> 🔁\n"
            f"<b>Dead ➛</b> <b>{dead_count}</b> ⚠️"
        )
        if errored_count:
            caption += f"\n<b>DB Errors ➛</b> <b>{errored_count}</b> 🛑"
        if unknown_count:
            caption += f"\n<b>Unknown ➛</b> <b>{unknown_count}</b> ❔ (endpoints throttled)"
    else:
        if results_list:
            proxy_data, is_live, info = results_list[0]
        else:
            proxy_data, is_live, info = valid_proxies[0], False, {}

        single_result = add_results[0] if add_results else None
        single_class = _classify_add_result(single_result) if add_results else "error"

        # Fix 7: escape every user / exception-derived string before HTML.
        ip_raw = info.get("ip") or proxy_data['ip']
        ip_esc = html_escape(str(ip_raw))

        if is_live is True:
            if single_class == "error":
                detail = (
                    str(single_result)[:80]
                    if isinstance(single_result, BaseException)
                    else "unknown error"
                )
                caption = (
                    f"<b>❌ DB Error</b>\n\n"
                    f"<b>Status ➛ Live ✅</b>\n"
                    f"<b>IP ➛</b> <code>{ip_esc}</code>\n\n"
                    f"<b>⚠️ {html_escape(detail)}</b>"
                )
            elif single_class == "added":
                caption = (
                    f"<b>✅ Success!</b>\n\n"
                    f"<b>Status ➛ Live ✅</b>\n"
                    f"<b>IP ➛</b> <code>{ip_esc}</code>\n\n"
                    f"<b>💾 Saved to database</b>"
                )
            else:  # clone
                caption = (
                    f"<b>⚠️ Warning!</b>\n\n"
                    f"<b>Status ➛ Live ✅</b>\n"
                    f"<b>IP ➛</b> <code>{ip_esc}</code>\n\n"
                    f"<b>🔁 Already exists in DB</b>"
                )
        elif is_live is False:
            error_msg = html_escape(str(info.get("error", "Unknown")))
            caption = (
                f"<b>❌ Failed!</b>\n\n"
                f"<b>Status ➛ Dead 🛑</b>\n"
                f"<b>Reason ➛</b> <code>{error_msg}</code>\n\n"
                f"<b>❌ Not added.</b>"
            )
        else:  # None — unknown
            error_msg = html_escape(str(info.get("error", "unknown")))
            caption = (
                f"<b>❔ Uncertain</b>\n\n"
                f"<b>Status ➛ Unknown</b>\n"
                f"<b>Reason ➛</b> <code>{error_msg}</code>\n\n"
                f"<b>⏳ Try again in a minute.</b>"
            )

    try:
        await bot.edit_message_text(
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            text=caption,
            parse_mode="HTML"
        )
    except Exception:
        await message.reply(caption, parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /checkproxy — background verify-and-prune
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def check_db_proxies_background(bot, message: types.Message):
    user_id = message.from_user.id
    proxies_str_list = await asyncio.to_thread(get_proxies, user_id)
    if not proxies_str_list:
        await message.reply("<b>📭 No proxies saved.</b>", parse_mode="HTML")
        return

    total_count = len(proxies_str_list)

    status_msg = await message.reply(
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>Checking...</b>\n\n"
        f"<b>Saved ➛</b> <code>{total_count}</code>\n"
        f"<b>Progress ➛</b> <code>0/{total_count}</code>",
        parse_mode="HTML"
    )

    proxies_to_check = []
    skipped_count = 0
    # Fix 2 (follow-up): catches socks5h://, socks4a:// too.
    socks_to_remove = []
    for p_str in proxies_str_list:
        if is_socks_proxy_input(p_str):
            socks_to_remove.append(p_str)
            continue
        parsed = parse_proxy_input(p_str)
        if parsed:
            proxies_to_check.append(parsed)
        else:
            skipped_count += 1

    if not proxies_to_check:
        msg = "<b>⚠️ None of the saved proxies could be parsed.</b>"
        if skipped_count:
            msg += f"\n<b>Skipped ➛</b> <b>{skipped_count}</b>"
        if socks_to_remove:
            msg += f"\n<b>SOCKS (unsupported) ➛</b> <b>{len(socks_to_remove)}</b>"
            # Fix 8: bounded remove concurrency.
            _del_sem = asyncio.Semaphore(_DB_WRITE_CONCURRENCY)
            async def _safe_del(d):
                async with _del_sem:
                    return await asyncio.to_thread(remove_proxy, user_id, d)
            await asyncio.gather(
                *[_safe_del(s) for s in socks_to_remove],
                return_exceptions=True,
            )
        await status_msg.edit_text(msg, parse_mode="HTML")
        return

    async def _progress(done, total, live):
        pct = int(done * 100 / total) if total else 0
        try:
            await bot.edit_message_text(
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                text=(
                    f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>Checking...</b>\n\n"
                    f"<b>Progress ➛</b> <code>{done}/{total}</code> ({pct}%)\n"
                    f"<b>Live ➛</b> <b>{live}</b> ✅"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

    results_list = await check_proxies_parallel(proxies_to_check, on_progress=_progress)

    live_proxies    = []
    dead_proxies    = []
    unknown_proxies = []
    for proxy_data, is_live, info in results_list:
        if is_live is True:
            live_proxies.append(proxy_data['db_format'])
        elif is_live is False:
            dead_proxies.append(proxy_data['db_format'])
        else:
            unknown_proxies.append(proxy_data['db_format'])

    to_remove = dead_proxies + socks_to_remove
    if to_remove:
        # Fix 8: bounded remove concurrency.
        _del_sem = asyncio.Semaphore(_DB_WRITE_CONCURRENCY)
        async def _safe_del2(d):
            async with _del_sem:
                return await asyncio.to_thread(remove_proxy, user_id, d)
        await asyncio.gather(
            *[_safe_del2(d) for d in to_remove],
            return_exceptions=True,
        )

    caption = (
        f"<b>✅ Check Complete!</b>\n\n"
        f"<b>Total ➛</b> <code>{total_count}</code>\n"
        f"<b>Live ➛</b> <b>{len(live_proxies)}</b> ✅\n"
        f"<b>Removed ➛</b> <b>{len(dead_proxies)}</b> 🗑️"
    )
    if socks_to_remove:
        caption += f"\n<b>SOCKS Removed ➛</b> <b>{len(socks_to_remove)}</b> (unsupported)"
    if skipped_count:
        caption += f"\n<b>Skipped ➛</b> <b>{skipped_count}</b> (unparseable)"
    if unknown_proxies:
        caption += f"\n<b>Unknown ➛</b> <b>{len(unknown_proxies)}</b> ❔ (kept)"

    if not live_proxies:
        caption += "\n\n<b>⚠️ No live proxies remaining.</b>"
        try:
            await bot.edit_message_text(
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                text=caption,
                parse_mode="HTML"
            )
        except Exception:
            await message.reply(caption, parse_mode="HTML")
        return

    file_content = ""
    for p_str in live_proxies:
        parsed = parse_proxy_input(p_str)
        if parsed:
            file_content += parsed['http_format'] + "\n"
        else:
            file_content += p_str + "\n"

    txt_file = BufferedInputFile(
        file=file_content.encode('utf-8'),
        filename=f"live_proxies_{len(live_proxies)}.txt"
    )

    try:
        await bot.delete_message(
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id
        )
    except Exception:
        pass

    try:
        await message.reply_document(
            document=txt_file,
            caption=caption,
            parse_mode="HTML",
            reply_to_message_id=message.message_id
        )
    except Exception as e:
        logging.error(f"File send error: {e}")
        await message.reply(caption, parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(
    F.text.regexp(r'^/proxy(?:\s|$)') | F.caption.regexp(r'^/proxy(?:\s|$)')
)
async def proxy_command(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    first_name = message.from_user.first_name or "User"
    await asyncio.to_thread(create_user, user_id, username)

    full_text = message.text or message.caption or ""

    raw_text = ""
    parts = full_text.split(maxsplit=1)
    if len(parts) > 1:
        raw_text += parts[1].strip() + "\n"
    if message.reply_to_message and message.reply_to_message.text:
        raw_text += message.reply_to_message.text + "\n"
    if message.reply_to_message and message.reply_to_message.caption:
        raw_text += message.reply_to_message.caption + "\n"

    document = message.document
    if not document and message.reply_to_message and message.reply_to_message.document:
        document = message.reply_to_message.document

    if document:
        if document.file_size and document.file_size > 2 * 1024 * 1024:
            await message.reply("<b>⚠️ File too large. Max 2MB.</b>", parse_mode="HTML")
            return
        try:
            file = await message.bot.get_file(document.file_id)
            downloaded = await message.bot.download_file(file.file_path)
            byte_content = downloaded.read()
            raw_text += byte_content.decode('utf-8', errors='ignore')
        except Exception as e:
            await message.reply(f"<b>🛑 Error reading file: {e}</b>", parse_mode="HTML")
            return

    if not raw_text.strip():
        await message.reply(
            "<b>❌ Invalid Usage!</b>\n\n"
            "<b>How to use:</b>\n"
            "• <code>/proxy user:pass@host:port</code>\n"
            "• <code>/proxy host:port</code> (no-auth)\n"
            "• Reply to a message containing proxies with <code>/proxy</code>\n"
            "• Or upload a .txt file with proxies and caption it <code>/proxy</code>",
            parse_mode="HTML"
        )
        return

    lines = raw_text.strip().split('\n')
    valid_proxies = []
    socks_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if is_socks_proxy_input(stripped):
            socks_count += 1
            continue
        proxy_data = parse_proxy_input(stripped)
        if proxy_data:
            valid_proxies.append(proxy_data)

    if not valid_proxies:
        msg = "<b>⚠️ No valid proxies found.</b>"
        if socks_count:
            msg += (
                f"\n\n<b>❌ {socks_count} SOCKS proxies rejected.</b>\n"
                f"<i>SOCKS is not supported — use HTTP(S) proxies.</i>"
            )
        await message.reply(msg, parse_mode="HTML")
        return

    if socks_count:
        await message.reply(
            f"<b>⚠️ {socks_count} SOCKS proxies skipped.</b>\n"
            f"<i>Only HTTP(S) proxies are supported.</i>",
            parse_mode="HTML"
        )

    asyncio.create_task(process_proxies_background(message.bot, message, valid_proxies))


@router.message(
    F.text.regexp(r'^/checkproxy(?:\s|$)') | F.caption.regexp(r'^/checkproxy(?:\s|$)')
)
async def checkproxy_command(message: types.Message):
    asyncio.create_task(check_db_proxies_background(message.bot, message))


@router.message(
    F.text.regexp(r'^/clearproxy(?:\s|$)') | F.caption.regexp(r'^/clearproxy(?:\s|$)')
)
async def clearproxy_command(message: types.Message):
    user_id = message.from_user.id
    deleted_count = await asyncio.to_thread(clear_proxies, user_id)
    if deleted_count > 0:
        msg = f"<b>✅ Success!</b>\n\n<b>Deleted {deleted_count} proxies.</b>"
    else:
        msg = "<b>📭 Database is already empty.</b>"
    await message.reply(msg, parse_mode="HTML")


@router.message(
    F.text.regexp(r'^/myproxies(?:\s|$)') | F.caption.regexp(r'^/myproxies(?:\s|$)')
)
async def myproxies_command(message: types.Message):
    user_id = message.from_user.id
    count = await asyncio.to_thread(count_proxies, user_id)
    if count > 0:
        msg = (
            f"<b>📊 Your Proxies</b>\n\n"
            f"<b>Total Saved:</b> <b>{count}</b> proxies\n\n"
            f"Use <code>/checkproxy</code> to test them\n"
            f"Use <code>/clearproxy</code> to remove all"
        )
    else:
        msg = (
            f"<b>📭 No Proxies</b>\n\n"
            f"You haven't saved any proxies yet.\n\n"
            f"Use <code>/proxy</code> to add some!"
        )
    await message.reply(msg, parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADMIN: /rtvproxy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(
    F.text.regexp(r'^/rtvproxy(?:\s|$)') | F.caption.regexp(r'^/rtvproxy(?:\s|$)')
)
async def rtvproxy_command(message: types.Message):
    """Admin-only: download every proxy in the database, grouped by user."""
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply(
            "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
            "<b>𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.</b>",
            parse_mode="HTML"
        )
        return

    status_msg = await message.reply(
        "<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> "
        "<b>Collecting proxies from all users...</b>",
        parse_mode="HTML"
    )

    try:
        rows = await asyncio.to_thread(get_all_proxies_with_users)

        if not rows:
            await status_msg.edit_text(
                "<b>📭 No proxies found in the database.</b>",
                parse_mode="HTML"
            )
            return

        by_user = {}
        for r in rows:
            uid = r.get("user_id")
            proxy = r.get("proxy")
            if uid is None or not proxy:
                continue
            by_user.setdefault(uid, []).append(proxy)

        total_proxies = sum(len(v) for v in by_user.values())
        total_users = len(by_user)

        lines = []
        lines.append("════════════════════════════════════════")
        lines.append("         ALL PROXIES — STARKZ")
        lines.append("════════════════════════════════════════")
        lines.append(f"Total Users   : {total_users}")
        lines.append(f"Total Proxies : {total_proxies}")
        lines.append(f"Generated At  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("════════════════════════════════════════")
        lines.append("")

        for uid, proxies in by_user.items():
            lines.append(f"── User ID: {uid}  ({len(proxies)} proxies) ──")
            for p in proxies:
                lines.append(p)
            lines.append("")

        file_content = "\n".join(lines).encode("utf-8")
        document = BufferedInputFile(
            file=file_content,
            filename=f"all_proxies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )

        await status_msg.delete()
        await message.reply_document(
            document=document,
            caption=(
                f"<tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> "
                f"<b>All Proxies Retrieved</b>\n"
                f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> Total Users ➛</b> <code>{total_users}</code>\n"
                f"<b>📡 Total Proxies ➛</b> <code>{total_proxies}</code>"
            ),
            parse_mode="HTML"
        )
        logging.info(f"[rtvproxy] Admin {user.id} retrieved {total_proxies} proxies from {total_users} users")

    except Exception as e:
        logging.error(f"Error in /rtvproxy command: {e}", exc_info=True)
        try:
            await status_msg.edit_text(
                f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
                f"<b>Error:</b> <code>{html_escape(str(e)[:80])}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EXPORT HELPER (fix 1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# main.py can replace its per-handler imports with:
#
#     from proxy import get_proxy_commands
#     for _cmd, _fn in get_proxy_commands().items():
#         dp.message.register(_fn, Command(_cmd))
#
# or just add ("myproxies", myproxies_command) to the existing register
# loop — either way is fine.

def get_proxy_commands() -> dict:
    """Return every proxy command name → handler for dispatcher registration."""
    return {
        "proxy":      proxy_command,
        "checkproxy": checkproxy_command,
        "clearproxy": clearproxy_command,
        "myproxies":  myproxies_command,
        "rtvproxy":   rtvproxy_command,
    }
