import re
import logging
import aiohttp
import asyncio
from datetime import datetime
from urllib.parse import quote
import io

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

ADMIN_IDS = {6962534443, 8761005192, 8428369446}

MAX_CONCURRENT_CHECKS = 5
PROXY_TIMEOUT = 8
IPIFY_API_URL = "https://api.ipify.org?format=json"

def parse_proxy_input(proxy_input):
    s = proxy_input.strip()
    protocol = 'http'
    protocol_match = re.match(r'^(?P<p>http|https|socks4|socks5)://', s, re.IGNORECASE)
    if protocol_match:
        protocol = protocol_match.group('p').lower()
        s = s[len(protocol_match.group('p'))+3:]

    def is_valid(ip, port):
        return port and port.isdigit()

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

    match = re.match(r'^([^:@]+)\s+([^:@]+)\s+([^:@]+)\s+(\d+)$', s)
    if match:
        user, password, ip, port = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    match = re.match(r'^([^:@]+):([^:@]+):([^:@]+):(\d+)$', s)
    if match:
        user, password, ip, port = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    match = re.match(r'^([^:@]+):(\d+):([^:@]+):([^:@]+)$', s)
    if match:
        ip, port, user, password = match.groups()
        if is_valid(ip, port): return build_dict(user, password, ip, port, protocol, proxy_input)

    return None

def build_dict(user, password, ip, port, protocol, original_input):
    user = user.strip()
    password = password.strip()
    ip = ip.strip()
    port = port.strip()
    encoded_user = quote(user, safe='')
    encoded_pass = quote(password, safe='')
    return {
        "user": user,
        "password": password,
        "ip": ip,
        "port": port,
        "original_format": original_input,
        "url_format": f"{protocol}://{encoded_user}:{encoded_pass}@{ip}:{port}",
        "db_format": f"{user} {password} {ip} {port}",
        "http_format": f"http://{user}:{password}@{ip}:{port}"
    }

async def check_proxy_live(proxy_url, session=None, timeout=PROXY_TIMEOUT):
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=timeout/2)
    close_session = False
    if session is None:
        session = aiohttp.ClientSession(timeout=client_timeout, headers=headers)
        close_session = True
    try:
        async with session.get(IPIFY_API_URL, proxy=proxy_url, ssl=False) as resp:
            if resp.status == 200:
                try:
                    data = await resp.json()
                    ip = data.get('ip')
                    if ip:
                        return True, {"ip": ip}
                    return False, {"error": "No IP in response"}
                except Exception:
                    text_data = await resp.text()
                    if text_data.strip():
                        return True, {"ip": text_data.strip()}
                    return False, {"error": "Empty response"}
            elif resp.status == 429:
                return False, {"error": "Rate limited"}
            else:
                return False, {"error": f"HTTP {resp.status}"}
    except asyncio.TimeoutError:
        return False, {"error": "Timeout"}
    except aiohttp.ClientProxyConnectionError:
        return False, {"error": "Connection failed"}
    except aiohttp.ClientConnectorError:
        return False, {"error": "DNS/Connection error"}
    except aiohttp.ClientError as e:
        return False, {"error": f"Client error: {str(e)[:50]}"}
    except Exception as e:
        return False, {"error": str(e)[:80]}
    finally:
        if close_session:
            await session.close()

async def check_proxies_parallel(proxies_list, max_concurrent=MAX_CONCURRENT_CHECKS):
    semaphore = asyncio.Semaphore(max_concurrent)
    async def check_with_semaphore(proxy_data):
        async with semaphore:
            is_live, info = await check_proxy_live(proxy_data['url_format'])
            return (proxy_data, is_live, info)
    tasks = [check_with_semaphore(p) for p in proxies_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    processed = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            processed.append((proxies_list[i], False, {"error": str(res)}))
        else:
            processed.append(res)
    return processed

async def run_db_operation(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)

async def process_proxies_background(bot, message: types.Message, valid_proxies: list, is_premium: bool):
    user_id = message.from_user.id
    total_count = len(valid_proxies)
    status_msg = await message.reply(
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>Processing...</b>\n\n"
        f"<code>{total_count}</code> proxies to check\n"
        f"<i>Checking</i>",
        parse_mode="HTML"
    )
    results_list = await check_proxies_parallel(valid_proxies)
    live_count = sum(1 for _, is_live, _ in results_list if is_live)
    dead_count = total_count - live_count
    added_count = 0
    clone_count = 0
    for proxy_data, is_live, _ in results_list:
        if is_live:
            was_added = await asyncio.to_thread(add_proxy, user_id, proxy_data['db_format'])
            if was_added:
                added_count += 1
            else:
                clone_count += 1
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
    else:
        if results_list:
            proxy_data, is_live, info = results_list[0]
        else:
            proxy_data, is_live, info = valid_proxies[0], False, {}
        if is_live:
            ip = info.get("ip") or proxy_data['ip']
            if added_count > 0:
                caption = (
                    f"<b>✅ Success!</b>\n\n"
                    f"<b>Status ➛ Live ✅</b>\n"
                    f"<b>IP ➛</b> <code>{ip}</code>\n\n"
                    f"<b>💾 Saved to database</b>"
                )
            else:
                caption = (
                    f"<b>⚠️ Warning!</b>\n\n"
                    f"<b>Status ➛ Live ✅</b>\n"
                    f"<b>IP ➛</b> <code>{ip}</code>\n\n"
                    f"<b>🔁 Already exists in DB</b>"
                )
        else:
            error_msg = info.get("error", "Unknown")
            caption = (
                f"<b>❌ Failed!</b>\n\n"
                f"<b>Status ➛ Dead 🛑</b>\n"
                f"<b>Reason ➛</b> <code>{error_msg}</code>\n\n"
                f"<b>❌ Not added.</b>"
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

async def check_db_proxies_background(bot, message: types.Message):
    user_id = message.from_user.id
    proxies_str_list = await asyncio.to_thread(get_proxies, user_id)
    if not proxies_str_list:
        await message.reply("<b>📭 No proxies saved.</b>", parse_mode="HTML")
        return
    total_count = len(proxies_str_list)
    status_msg = await message.reply(
        f"<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> <b>Checking...</b>\n\n"
        f"<code>{total_count}</code> saved proxies\n"
        f"<i>Testing {min(MAX_CONCURRENT_CHECKS, total_count)} at a time...</i>",
        parse_mode="HTML"
    )
    proxies_to_check = []
    for p_str in proxies_str_list:
        parsed = parse_proxy_input(p_str)
        if parsed:
            proxies_to_check.append(parsed)
    results_list = await check_proxies_parallel(proxies_to_check)
    live_proxies = []
    dead_proxies = []
    for proxy_data, is_live, info in results_list:
        if is_live:
            live_proxies.append(proxy_data['db_format'])
        else:
            dead_proxies.append(proxy_data['db_format'])
    if dead_proxies:
        for dead_str in dead_proxies:
            await asyncio.to_thread(remove_proxy, user_id, dead_str)
    caption = (
        f"<b>✅ Check Complete!</b>\n\n"
        f"<b>Total ➛</b> <code>{total_count}</code>\n"
        f"<b>Live ➛</b> <b>{len(live_proxies)}</b> ✅\n"
        f"<b>Removed ➛</b> <b>{len(dead_proxies)}</b> 🗑️"
    )
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

@router.message(F.text.startswith("/proxy"))
async def proxy_command(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    first_name = message.from_user.first_name or "User"
    await asyncio.to_thread(create_user, user_id, username)
    is_premium, expiry = await asyncio.to_thread(get_premium_status, user_id)

    raw_text = ""
    parts = message.text.split(maxsplit=1)
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
        if document.file_size > 2 * 1024 * 1024:
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
        await message.reply("<b>❌ Invalid Usage!</b>", parse_mode="HTML")
        return

    lines = raw_text.strip().split('\n')
    valid_proxies = []
    for line in lines:
        proxy_data = parse_proxy_input(line)
        if proxy_data:
            valid_proxies.append(proxy_data)

    if not valid_proxies:
        await message.reply("<b>⚠️ No valid proxies found.</b>", parse_mode="HTML")
        return

    asyncio.create_task(process_proxies_background(message.bot, message, valid_proxies, is_premium))

@router.message(F.text.startswith("/checkproxy"))
async def checkproxy_command(message: types.Message):
    asyncio.create_task(check_db_proxies_background(message.bot, message))

@router.message(F.text.startswith("/clearproxy"))
async def clearproxy_command(message: types.Message):
    user_id = message.from_user.id
    deleted_count = await asyncio.to_thread(clear_proxies, user_id)
    if deleted_count > 0:
        msg = f"<b>✅ Success!</b>\n\n<b>Deleted {deleted_count} proxies.</b>"
    else:
        msg = "<b>📭 Database is already empty.</b>"
    await message.reply(msg, parse_mode="HTML")

@router.message(F.text.startswith("/myproxies"))
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
# ADMIN: /rtvproxy — Retrieve every proxy across all users
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/rtvproxy"))
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

    status_msg = await message.answer(
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
        lines.append("         ALL PROXIES — ZLATAN")
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
                f"<b>Error:</b> <code>{str(e)[:80]}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
