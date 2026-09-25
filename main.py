import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import os
import re
import database
import logging
import asyncio
from aiohttp import web
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, types, F, Router, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import get_user, create_user, DB_CONFIG, get_db_connection
from gate import on_command, off_command
from fb import setup_feedback_handler, feedback_cmd, router as fb_router
from stats import stats_command
from stats import router as stats_router
from tools.binn import binn_command
from cmds import cmds_command, router as cmds_router
from broad import broad_command, router as broad_router
from ban import ban_command, unban_command, BanMiddleware, router as ban_router
from status import vps_command, router as status_router

from sub import (
    sub_command, rc_command, suball_command, g_code_command,
    claim_command, info_command, rsub_command, buy_command, adcr_command,
    eren_admin_command, gen_command,
    revokeall_command, revokeall_callback,
    revoke_command, revokeuser_command,
)
from proxy import proxy_command, checkproxy_command, clearproxy_command, rtvproxy_command

from gates.st import st_command
from gates.sh import sh_command, sh_callback_handler, router as sh_router

from mass_gates.msh import (
    router as msh_router, MshStopCallback, MshResultCallback,
    handle_stop_callback as msh_stop_handler,
    handle_result_callback as msh_result_handler,
)
from mass_gates.mst import (
    router as mst_router, MstStopCallback, MstResultCallback,
    handle_stop_callback as mst_stop_handler,
    handle_result_callback as mst_result_handler,
    mst_command
)
from mass_gates.sitechk import (
    sitechk_command, addsite_command, siteall_command,
    removeall_command, dedupe_command, proxyinfo_command, resetproxy_command,
    remsite_command, mysites_command, clearsites_command,
)

try:
    from mass_gates.sitechk import setprice_command
except ImportError:
    setprice_command = None
    logging.warning(
        "[main] setprice_command not found in mass_gates.sitechk — "
        "/setprice will be disabled. Update sitechk.py to enable it."
    )

from whop import whop_command, router as whop_router

import payments as pay_sys
import shopify_api

BOT_TOKEN = "8882512603:AAHPGT5qG1SjjJWHtSTDP0dA9RHfmv3gowg"
WEBHOOK_URL = f""
WEBHOST = "0.0.0.0"
WEBPORT = 8080

LOG_CHANNEL_ID = -1004479507133
BOT_LINK = "https://t.me/zlatanchecker_bot"
CHANNEL_LINK = "https://t.me/+93nNDkmK2PRjZjg8"
GROUP_LINK = "https://t.me/+KrM3-0iNKrRjNWJk"
DEV_LINK = "https://t.me/Lanxo2"

PRICING_TEXT = (
    "<b>┌── <tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗣𝗥𝗜𝗖𝗜𝗡𝗚 𝗣𝗟𝗔𝗡𝗦 ──┐</b>\n\n"
    "<b><tg-emoji emoji-id='5042274086332400375'>🛠️</tg-emoji> 𝗖𝗢𝗥𝗘 𝗣𝗟𝗔𝗡</b>\n"
    "<b>├ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> 𝟳 Days\n"
    "<b>└ 𝗣𝗿𝗶𝗰𝗲 ➛</b> 𝟱$\n\n"
    "<b><tg-emoji emoji-id='5278751923338490157'>⭐</tg-emoji> 𝗘𝗟𝗜𝗧𝗘 𝗣𝗟𝗔𝗡</b>\n"
    "<b>├ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> 𝟭𝟱 Days\n"
    "<b>└ 𝗣𝗿𝗶𝗰𝗲 ➛</b> 𝟳$\n\n"
    "<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> Superuser 𝗣𝗟𝗔𝗡</b>\n"
    "<b>├ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> 𝟯𝟬 Days\n"
    "<b>└ 𝗣𝗿𝗶𝗰𝗲 ➛</b> 𝟭𝟱$\n"
    "<b>└──────────────────┘</b>"
)

import json
from aiogram.client.session.aiohttp import AiohttpSession

def custom_dumps(obj, *args, **kwargs):
    return json.dumps(obj, *args, **kwargs)

session = AiohttpSession(json_dumps=custom_dumps)
bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

dp.include_router(msh_router)
dp.include_router(mst_router)
dp.include_router(sh_router)
dp.include_router(cmds_router)
dp.include_router(fb_router)
dp.include_router(ban_router)
dp.include_router(broad_router)
dp.include_router(status_router)
dp.include_router(stats_router)
dp.include_router(whop_router)

router = Router()
dp.include_router(router)

ADMIN_IDS = {6962534443, 8428369446}

async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    for chat_target in ["@zlatanchatxlogs", "@Zlatanchannel"]:
        try:
            m = await bot.get_chat_member(chat_target, user_id)
            if m.status in ["left", "kicked"]:
                return False
        except Exception:
            continue
    return True

class JoinCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if not user or user.id in ADMIN_IDS:
            return await handler(event, data)
        if chat and chat.type != "private":
            return await handler(event, data)
        if isinstance(event, types.CallbackQuery) and event.data == "verify_fsub":
            return await handler(event, data)

        bot = data["bot"]
        if not await is_subscribed(bot, user.id):
            if isinstance(event, types.Message):
                await event.reply(FSUB_TEXT, parse_mode="HTML", reply_markup=_FSUB_KB)
            elif isinstance(event, types.CallbackQuery):
                await event.answer("⚠️ Please join our Channel and Group first!", show_alert=True)
                try:
                    await event.message.edit_text(FSUB_TEXT, parse_mode="HTML", reply_markup=_FSUB_KB)
                except Exception:
                    pass
            return
        return await handler(event, data)

dp.message.middleware(BanMiddleware())
dp.message.middleware(JoinCheckMiddleware())
dp.callback_query.middleware(JoinCheckMiddleware())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DB HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _db_conn():
    return get_db_connection()

def _ensure_user_sync(user_id, username):
    try:
        if not get_user(user_id):
            create_user(user_id, username or "unknown")
    except Exception as e:
        logging.error(f"ensure_user {user_id}: {e}")

async def ensure_user(user_id, username="unknown"):
    await asyncio.to_thread(_ensure_user_sync, user_id, username)

def _status_sync(user_id):
    conn = _db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    plan, joined_str = "𝗧𝗿𝗶𝗮𝗹", "N/A"
    try:
        cur.execute("SELECT is_premium, premium_expiry, joined_at FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            if row['joined_at']:
                joined_str = row['joined_at'].strftime('%Y-%m-%d')
            if row['is_premium'] == 1 and row['premium_expiry'] and datetime.now() < row['premium_expiry']:
                cur.execute("SELECT plan FROM receipts WHERE user_id = %s ORDER BY purchased_on DESC LIMIT 1", (user_id,))
                r = cur.fetchone()
                plan = r['plan'] if r else "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"
    except Exception as e:
        logging.error(f"status {user_id}: {e}")
    finally:
        conn.close()
    bold_map = {
        'a':'𝗮','b':'𝗯','c':'𝗰','d':'𝗱','e':'𝗲','f':'𝗳','g':'𝗴','h':'𝗵','i':'𝗶','j':'𝗷','k':'𝗸','l':'𝗹','m':'𝗺','n':'𝗻','o':'𝗼','p':'𝗽','q':'𝗾','r':'𝗿','s':'𝘀','t':'𝘁','u':'𝘂','v':'𝘃','w':'𝘄','x':'𝗅','y':'𝘆','z':'𝘇',
        'A':'𝗔','B':'𝗕','C':'𝗖','D':'𝗗','E':'𝗘','F':'𝗙','G':'𝗚','H':'𝗛','I':'𝗜','J':'𝗝','K':'𝗞','L':'𝗟','M':'𝗠','N':'𝗡','O':'𝗢','P':'𝗣','Q':'𝗤','R':'𝗥','S':'𝗦','T':'𝗧','U':'𝗨','V':'𝗩','W':'𝗪','X':'𝗫','Y':'𝗬','Z':'𝗭'
    }
    if plan.lower() in ("kashim", "chirag", "darkanon"):
        plan_formatted = "Carder X <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>"
    else:
        plan_formatted = "".join(bold_map.get(c, c) for c in plan)
    return plan_formatted, joined_str

async def _get_caption(user) -> str:
    access_str, joined_str = await asyncio.to_thread(_status_sync, user.id)
    ul = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'
    dl = '<a href="https://t.me/Lanxo2">Carder X</a>'
    return (
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {ul}\n"
        f"<tg-emoji emoji-id='6237822905128851025'>🆔</tg-emoji> 𝗨𝘀𝗲𝗿 𝗜𝗗 ➛ <code>{user.id}</code>\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ <b>{access_str}</b>\n"
        f"<tg-emoji emoji-id='6147637448135414816'>📅</tg-emoji> 𝗝𝗼𝗶𝗻𝗲𝗱 ➛ <b>{joined_str}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> 𝗗𝗲𝘃 ➛ {dl}"
    )

def _loading_caption(user) -> str:
    ul = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'
    dl = '<a href="https://t.me/Lanxo2">Carder X</a>'
    return (
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {ul}\n"
        f"<tg-emoji emoji-id='6237822905128851025'>🆔</tg-emoji> 𝗨𝘀𝗲𝗿 𝗜𝗗 ➛ <code>{user.id}</code>\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ <b>Loading…</b>\n"
        f"<tg-emoji emoji-id='6147637448135414816'>📅</tg-emoji> 𝗝𝗼𝗶𝗻𝗲𝗱 ➛ <b>Loading…</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id='5039653765439816618'>🐈‍⬛</tg-emoji> 𝗗𝗲𝘃 ➛ {dl}"
    )

def mask_receipt_id(receipt_id):
    parts = receipt_id.split('-')
    if len(parts) == 3:
        m = parts[1]
        if len(m) >= 2:
            return f"{parts[0]}-{m[:2]}XX{m[4:]}-{parts[2]}"
    return receipt_id

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRE-BUILT KEYBOARDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_MAIN_KB = {
    "inline_keyboard": [
        [{"text": " 𝗖𝗵𝗲𝗰𝗸𝗲𝗿", "callback_data": "menu_gates", "icon_custom_emoji_id": "5039623284056917259", "style": "primary"},
         {"text": " 𝗕𝘂𝘆 𝗡𝗼𝘄", "callback_data": "menu_payment_methods", "icon_custom_emoji_id": "5039727497143387500", "style": "success"}],
        [{"text": " 𝗖𝗵𝗮𝗻𝗻𝗲𝗹", "url": CHANNEL_LINK, "icon_custom_emoji_id": "5424818078833715060", "style": "primary"},
         {"text": " 𝗚𝗿𝗼𝘂𝗽", "url": GROUP_LINK, "icon_custom_emoji_id": "6237927637906364256", "style": "success"}],
        [{"text": " 𝗦𝘂𝗽𝗽𝗼𝗿𝘁", "url": "https://t.me/Lanxo2", "icon_custom_emoji_id": "5040030395416969985", "style": "danger"},
         {"text": " 𝗣𝗿𝗼𝘅𝘆", "callback_data": "menu_proxy", "icon_custom_emoji_id": "5039895103947146186", "style": "primary"}]
    ]
}

_FSUB_KB = {
    "inline_keyboard": [
        [{"text": " 𝗖𝗵𝗮𝗻𝗻𝗲𝗹", "url": CHANNEL_LINK, "icon_custom_emoji_id": "5424818078833715060", "style": "primary"},
         {"text": " 𝗚𝗿𝗼𝘂𝗽", "url": GROUP_LINK, "icon_custom_emoji_id": "6237927637906364256", "style": "success"}],
        [{"text": " 𝗩𝗲𝗿𝗶𝗳𝘆 𝗠𝗲𝗺𝗯𝗲𝗿𝘀𝗵𝗶𝗽", "callback_data": "verify_fsub", "icon_custom_emoji_id": "5341715473882955310", "style": "success"}]
    ]
}

FSUB_TEXT = (
    "<b><tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗠𝗲𝗺𝗯𝗲𝗿𝘀𝗵𝗶𝗽 𝗥𝗲𝗾𝘂𝗶𝗿𝗲𝗱</b>\n\n"
    "To access <b>Carder X</b>, you must join our official Channel and Group:\n\n"
    "<b><tg-emoji emoji-id='5424818078833715060'>📢</tg-emoji> 𝗖𝗵𝗮𝗻𝗻𝗲𝗹 ➛</b> <a href='https://t.me/+93nNDkmK2PRjZjg8'>Carder X Channel</a>\n"
    "<b><tg-emoji emoji-id='6237927637906364256'>👥</tg-emoji> 𝗚𝗿𝗼𝘂𝗽 ➛</b> <a href='https://t.me/+KrM3-0iNKrRjNWJk'>Carder X Chats</a>\n\n"
    "<i>Join both links above, then tap <b>Verify Membership</b> below!</i>"
)

def _back(target):
    return {
        "inline_keyboard": [
            [{"text": "𝗕𝗮𝗰𝗸", "callback_data": target, "icon_custom_emoji_id": "5456140674028019486", "style": "danger"}]
        ]
    }

_KB_BACK_MAIN   = _back("back_main")
_KB_BACK_GATES  = _back("menu_gates")
_KB_BACK_MASS   = _back("menu_mass_in_gates")
_KB_BACK_AUTH   = _back("menu_auth")
_KB_BACK_CHARGE = _back("menu_charge")

_KB_PRICING = {
    "inline_keyboard": [
        [{"text": " 𝗣𝗮𝘆 𝗩𝗶𝗮", "callback_data": "menu_payment_methods", "icon_custom_emoji_id": "5039539210072097557", "style": "success"},
         {"text": "Contact Admin", "url": "https://t.me/Lanxo2", "icon_custom_emoji_id": "5042329873662609701", "style": "primary"}],
        _KB_BACK_MAIN["inline_keyboard"][0]
    ]
}

_KB_GATES = {
    "inline_keyboard": [
        [{"text": " 𝗖𝗵𝗮𝗿𝗴𝗲", "callback_data": "menu_charge", "icon_custom_emoji_id": "5042050649248760772", "style": "success"},
         {"text": " 𝗠𝗮𝘀𝘀", "callback_data": "menu_mass_in_gates", "icon_custom_emoji_id": "5041975203853239332", "style": "primary"}],
        _KB_BACK_MAIN["inline_keyboard"][0]
    ]
}

_KB_MASS = {
    "inline_keyboard": [
        [{"text": " 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗠𝗮𝘀𝘀", "callback_data": "info_msh_gate", "icon_custom_emoji_id": "5039531487720899631", "style": "success"},
         {"text": " 𝗦𝘁𝗿𝗶𝗽𝗲 𝗠𝗮𝘀𝘀 𝟭$", "callback_data": "info_mst_gate", "icon_custom_emoji_id": "5042297717242463211", "style": "primary"}],
        _KB_BACK_GATES["inline_keyboard"][0]
    ]
}

_KB_CHARGE = {
    "inline_keyboard": [
        [{"text": " 𝗦𝗵𝗼𝗽𝗶𝗳𝘆 𝗦𝗶𝗻𝗴𝗹𝗲", "callback_data": "info_charge_shopify", "icon_custom_emoji_id": "5039544445637231745", "style": "success"},
         {"text": " 𝗦𝘁𝗿𝗶𝗽𝗲 𝟭$", "callback_data": "info_charge_stripe", "icon_custom_emoji_id": "5042334757040423886", "style": "primary"}],
        _KB_BACK_GATES["inline_keyboard"][0]
    ]
}

_SEP = "━━━━━━━━━━━━━━━━"

_PAYMENT_SELECT_TEXT = "<b><tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗦𝗲𝗹𝗲𝗰𝘁 𝗬𝗼𝘂𝗿 𝗣𝗹𝗮𝗻\n\nChoose a plan to proceed with\nsecure crypto payment</b>"

STATIC_MENU_MAP: dict = {
    "menu_pricing": (PRICING_TEXT, _KB_PRICING),
    "menu_gates": (
        "<b>┌── <tg-emoji emoji-id='5424818078833715060'>📢</tg-emoji> 𝗚𝗔𝗧𝗘𝗦 𝗦𝗧𝗔𝗧𝗨𝗦 ──┐</b>\n"
        "<b>├ 𝗖𝗵𝗮𝗿𝗴𝗲 𝗚𝗮𝘁𝗲𝘀 ➛</b> <b>2</b>\n"
        "<b>├ 𝗠𝗮𝘀𝘀 𝗚𝗮𝘁𝗲𝘀 ➛</b> <b>2</b>\n"
        "<b>└──────────────────┘</b>\n"
        "<b>𝗦𝗲𝗹𝗲𝗰𝘁 𝗮 𝗚𝗮𝘁𝗲 𝗖𝗮𝘁𝗲𝗴𝗼𝗿𝘆 𝗯𝗲𝗹𝗼𝘄:</b>",
        _KB_GATES
    ),
    "menu_mass_in_gates": ("<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗦𝗲𝗹𝗲𝗰𝘁 𝗮 𝗠𝗮𝘀𝘀 𝗚𝗮𝘁𝗲</b>", _KB_MASS),
    "menu_charge":        ("<b><tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗦𝗲𝗹𝗲𝗰𝘁 𝗖𝗵𝗮𝗿𝗴𝗲 𝗠𝗲𝘁𝗵𝗼𝗱</b>", _KB_CHARGE),
    "menu_proxy": (
        "<b>┌── <tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗣𝗥𝗢𝘅𝗬 𝗠𝗔𝗡𝗔𝗚𝗘𝗠𝗘𝗡𝗧 ──┐</b>\n\n"
        "<b><tg-emoji emoji-id='5271604874419647061'>🔧</tg-emoji> 𝗦𝗲𝘁 𝗣𝗿𝗼𝘅𝘆</b>\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/proxy</code>\n"
        "<b>└ 𝗧𝘆𝗽𝗲 ➛</b> 𝗙𝗿𝗲𝗲\n\n"
        "<b><tg-emoji emoji-id='5388632425314140043'>🔍</tg-emoji> 𝗖𝗵𝗲𝗰𝗸 𝗣𝗿𝗼𝘅𝘆</b>\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/checkproxy</code>\n"
        "<b>└ 𝗧𝘆𝗽𝗲 ➛</b> 𝗙𝗿𝗲𝗲\n\n"
        "<b><tg-emoji emoji-id='5042329873662609701'>🗑️</tg-emoji> 𝗖𝗹𝗲𝗮𝗿 𝗣𝗿𝗼𝘅𝘆</b>\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/clearproxy</code>\n"
        "<b>└ 𝗧𝘆𝗽𝗲 ➛</b> 𝗙𝗿𝗲𝗲\n"
        "<b>└──────────────────────┘</b>",
        _KB_BACK_MAIN
    ),
    "menu_payment_methods": (_PAYMENT_SELECT_TEXT, None),
    "info_msh_gate": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Shopify Mass\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/msh</code>\n"
        "<b>├ 𝗟𝗶𝗺𝗶𝘁 ➛</b> 10,000 Cards\n"
        "<b>├ 𝗧𝘆𝗽𝗲 ➛</b> Mass Checker\n"
        "<b>└ 𝗦𝘁𝗼𝗽 ➛</b> <tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> Button\n"
        "<b>└────────────────┘</b>", _KB_BACK_MASS),
    "info_mst_gate": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Stripe Mass 1$ (Atoti)\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/mst</code> or <code>/mffc</code>\n"
        "<b>├ 𝗟𝗶𝗺𝗶𝘁 ➛</b> 2,000 Cards\n"
        "<b>├ 𝗧𝘆𝗽𝗲 ➛</b> Mass Checker\n"
        "<b>└ 𝗦𝘁𝗼𝗽 ➛</b> <tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> Button\n"
        "<b>└────────────────┘</b>", _KB_BACK_MASS),
    "info_mstr_gate": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Stripe Multi Mass\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/mstr</code>\n"
        "<b>├ 𝗟𝗶𝗺𝗶𝘁 ➛</b> 2,000\n"
        "<b>├ 𝗧𝘆𝗽𝗲 ➛</b> Mass Checker\n"
        "<b>└ 𝗦𝘁𝗼𝗽 ➛</b> <tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> Button\n"
        "<b>└────────────────┘</b>", _KB_BACK_MASS),
    "info_stco_gate": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Stripe Hitter\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/stco</code>\n"
        "<b>├ 𝗧𝘆𝗽𝗲 ➛</b> Auto Hitter\n"
        "<b>└ 𝗦𝘁𝗼𝗽 ➛</b> <tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> Button\n"
        "<b>└────────────────┘</b>", _KB_BACK_MASS),
    "info_mrz_gate": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Razorpay Mass\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/mrz</code>\n"
        "<b>├ 𝗟𝗶𝗺𝗶𝘁 ➛</b> 5,000\n"
        "<b>├ 𝗧𝘆𝗽𝗲 ➛</b> Mass Checker\n"
        "<b>└ 𝗦𝘁𝗼𝗽 ➛</b> <tg-emoji emoji-id='5456140674028019486'>🛑</tg-emoji> Button\n"
        "<b>└────────────────┘</b>\n"
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Razorpay\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/rz</code>\n"
        "<b>└ 𝗟𝗶𝗺𝗶𝘁 ➛</b> 50 Cards\n"
        "<b>└────────────────┘</b>", _KB_BACK_MASS),
    "info_auth_stripe": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Stripe 0$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/chk</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 16\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_AUTH),
    "info_auth_braintree": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Braintree 0$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/b3</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 2\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_AUTH),
    "info_auth_braintree_vbv": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Braintree VBV\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/vbv</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 1\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_AUTH),
    "info_charge_stripe": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Stripe 1$ Charge (Atoti)\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/st</code> or <code>/ffc</code>\n"
        "<b>├ 𝗟𝗶𝗺𝗶𝘁 ➛</b> 1 Card\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_str": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Stripe Multi\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/str</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 6\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_paypal": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> PayPal 0.10$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/pp</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 7\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_shopify": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧𝗘 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Shopify Single\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/sh</code>\n"
        "<b>├ 𝗟𝗶𝗺𝗶𝘁 ➛</b> 1 Card\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_payfast": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> PayFast 0.30$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/pf</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 1\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_fatzebra": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> FatZebra 4$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/ft</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 1\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_nmi": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> NMI 1$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/nmi</code>\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>\n"
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> NMI2 1$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/nmi2</code>\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_bluepay": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> BluePay 20$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/bl</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 1\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_authnet": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Authorize.net 1$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/at</code>\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_payway": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> PayWay 1$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/pw</code>\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_razorpay": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> Razorpay 1₹\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/rz</code>\n"
        "<b>├ 𝗦𝗶𝘁𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱 ➛</b> 5\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
    "info_charge_payu": (
        "<b>┌── <tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> 𝗚𝗔𝗧Ｅ 𝗜𝗡𝗙𝗢 ──┐</b>\n"
        "<b>├ 𝗚𝗮𝘁𝗲 ➛</b> PayU 1$\n"
        "<b>├ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱 ➛</b> <code>/pyu</code>\n"
        "<b>└ 𝗚𝗮𝘁𝗲 𝗛𝗲𝗮𝗹𝘁𝗵 ➛</b> 100%\n"
        "<b>└────────────────┘</b>", _KB_BACK_CHARGE),
}

async def _edit(msg: types.Message, text: str, kb: InlineKeyboardMarkup):
    try:
        if msg.caption is not None:
            await msg.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    except TypeError:
        pass
    except Exception as e:
        if "Message is not modified" not in str(e):
            logging.warning(f"_edit: {e}")

async def _safe_answer(cb: types.CallbackQuery, text: str = "", **kw):
    try:
        await cb.answer(text, **kw)
    except Exception:
        pass

@router.message(Command("start"))
async def start(message: types.Message):
    user = message.from_user
    asyncio.create_task(ensure_user(user.id, user.username))

    if message.text and len(message.text.split()) > 1 and message.text.split()[1] == "buy":
        await buy_command(message)
        return

    quick = _loading_caption(user)
    caption_task = asyncio.create_task(_get_caption(user))

    sent = await message.reply(text=quick, reply_markup=_MAIN_KB)

    try:
        full = await caption_task
        await sent.edit_text(text=full, reply_markup=_MAIN_KB)
    except Exception:
        pass

DOT_COMMAND_MAP = {
    "sh": sh_command, "msh": None, "st": st_command, "ffc": st_command,
    "mst": mst_command, "mffc": mst_command, "bin": binn_command, "binn": binn_command,
    "sub": sub_command, "rc": rc_command, "suball": suball_command,
    "g_code": g_code_command, "claim": claim_command, "info": info_command,
    "rsub": rsub_command, "buy": buy_command, "adcr": adcr_command,
    "gen": gen_command,
    "on": on_command, "off": off_command, "stats": stats_command,
    "proxy": proxy_command, "checkproxy": checkproxy_command,
    "clearproxy": clearproxy_command, "rtvproxy": rtvproxy_command,
    "sitechk": sitechk_command, "addsite": addsite_command, "siteall": siteall_command,
    "removeall": removeall_command, "dedupe": dedupe_command,
    "proxyinfo": proxyinfo_command, "resetproxy": resetproxy_command,
    "remsite": remsite_command,
    "mysites": mysites_command, "mysite": mysites_command,
    "clearsites": clearsites_command, "clearsite": clearsites_command,
    "setprice": setprice_command,
    "whop": whop_command, "whophit": whop_command,
    "revokeall": revokeall_command,
    "revoke": revoke_command,
    "revokeuser": revokeuser_command,
    "cmds": cmds_command, "fb": feedback_cmd, "broad": broad_command,
    "ban": ban_command, "unban": unban_command, "vps": vps_command,
    "api": None,
}

@router.message(Command("eid"))
async def eid_command(message: types.Message):
    if not message.entities:
        return await message.reply("𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝗮 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗲𝗺𝗼𝗷𝗶 𝘄𝗶𝘁𝗵 𝘁𝗵𝗲 𝗰𝗼𝗺𝗺𝗮𝗻𝗱.\n𝗘𝘅𝗮𝗺𝗽𝗹𝗲: /eid <tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji>")
    for entity in message.entities:
        if entity.type == "custom_emoji":
            return await message.reply(f"𝗖𝘂𝘀𝘁𝗼𝗺 𝗘𝗺𝗼𝗷𝗶 𝗜𝗗: <code>{entity.custom_emoji_id}</code>\n\n<i>Give this ID to me (Antigravity) so I can add it to your buttons!</i>", parse_mode="HTML")
    await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗡𝗼 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗰𝘂𝘀𝘁𝗼𝗺 𝗲𝗺𝗼𝗷𝗶 𝗳𝗼𝘂𝗻𝗱 𝗶𝗻 𝘆𝗼𝘂𝗿 𝗺𝗲𝘀𝘀𝗮𝗴𝗲!")

@router.message(Command("api"))
@router.message(Command("apis"))
async def api_health_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    text = shopify_api.get_health_summary()
    await message.reply(text, parse_mode="HTML")

DOT_COMMAND_MAP["api"] = api_health_command
DOT_COMMAND_MAP["apis"] = api_health_command

@router.message(F.text.regexp(r'^\.\w+'))
async def dot_command_handler(message: types.Message):
    cmd = message.text.strip().split()[0][1:].lower()
    h = DOT_COMMAND_MAP.get(cmd)
    if h:
        await h(message)

_MASS_PREFIXES = ("mshs_", "mshr_", "msts_", "mstr_", "mstsr_", "mstss_", "fb_", "cmds_")

@router.callback_query()
async def button_handler(callback: types.CallbackQuery):
    data = callback.data or ""

    if data.startswith(_MASS_PREFIXES):
        return

    if data.startswith("revokeall_"):
        return

    user_id = callback.from_user.id
    msg = callback.message
    if not msg:
        return

    if msg.chat.type in ("group", "supergroup"):
        owner_id = None
        entities = msg.entities or msg.caption_entities or []
        for entity in entities:
            if entity.type == "text_link" and entity.url and entity.url.startswith("tg://user?id="):
                try:
                    owner_id = int(entity.url.split("id=")[1])
                    break
                except (ValueError, IndexError):
                    pass
            elif entity.type == "text_mention" and entity.user:
                owner_id = entity.user.id
                break

        if owner_id is None:
            msg_text = msg.text or msg.caption or ""
            owner_id_match = re.search(r'(?:𝗨𝘀𝗲𝗿 𝗜𝗗|User ID)\s*➛\s*(\d+)', msg_text)
            if owner_id_match:
                owner_id = int(owner_id_match.group(1))

        if owner_id is not None and user_id != owner_id:
            await _safe_answer(callback, "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗬𝗼𝘂 𝗰𝗮𝗻𝗻𝗼𝘁 𝗶𝗻𝘁𝗲𝗿𝗮𝗰𝘁 𝘄𝗶𝘁𝗵 𝘁𝗵𝗶𝘀 𝗺𝗲𝗻𝘂.", show_alert=True)
            return

    if data == "verify_fsub":
        if await is_subscribed(callback.bot, user_id):
            await _safe_answer(callback, "✅ Membership verified! Welcome to Carder X.", show_alert=True)
            user = callback.from_user
            quick = _loading_caption(user)
            caption_task = asyncio.create_task(_get_caption(user))
            try:
                if msg.caption is not None:
                    await msg.edit_caption(caption=quick, reply_markup=_MAIN_KB, parse_mode="HTML")
                else:
                    await msg.edit_text(text=quick, reply_markup=_MAIN_KB, parse_mode="HTML")
            except Exception:
                pass
            try:
                full_caption = await caption_task
                if msg.caption is not None:
                    await msg.edit_caption(caption=full_caption, reply_markup=_MAIN_KB, parse_mode="HTML")
                else:
                    await msg.edit_text(text=full_caption, reply_markup=_MAIN_KB, parse_mode="HTML")
            except Exception:
                pass
        else:
            await _safe_answer(callback, "❌ You have not joined both yet! Join our Channel and Group then tap Verify.", show_alert=True)
        return

    static = STATIC_MENU_MAP.get(data)
    if static is not None:
        text, kb = static
        if kb is None:
            kb = pay_sys.get_plan_selection_keyboard()
        asyncio.create_task(_safe_answer(callback))
        asyncio.create_task(_edit(msg, text, kb))
        return

    if data == "back_main":
        user = callback.from_user
        quick = _loading_caption(user)
        caption_task = asyncio.create_task(_get_caption(user))
        await _safe_answer(callback)
        try:
            if msg.caption is not None:
                await msg.edit_caption(caption=quick, reply_markup=_MAIN_KB, parse_mode="HTML")
            else:
                await msg.edit_text(text=quick, reply_markup=_MAIN_KB, parse_mode="HTML")
        except TypeError:
            pass
        except Exception as e:
            if "Message is not modified" not in str(e):
                logging.warning(f"back_main load: {e}")
        try:
            full = await caption_task
            if msg.caption is not None:
                await msg.edit_caption(caption=full, reply_markup=_MAIN_KB, parse_mode="HTML")
            else:
                await msg.edit_text(text=full, reply_markup=_MAIN_KB, parse_mode="HTML")
        except TypeError:
            pass
        except Exception:
            pass
        return

    if data == "show_buy_plans":
        buy_url_kb = {
            "inline_keyboard": [[
                {"text": " 𝗧𝗮𝗽 𝗛𝗲𝗿𝗲 𝘁𝗼 𝗕𝘂𝘆", "url": f"{BOT_LINK}?start=buy", "icon_custom_emoji_id": "6242135305697106689"}
            ]]
        }
        await _safe_answer(callback)
        try:
            await msg.edit_reply_markup(reply_markup=buy_url_kb)
        except Exception:
            pass
        return

    if data.startswith("pay_plan_"):
        plan = data[9:]
        if plan not in pay_sys.PLANS:
            await _safe_answer(callback)
            await msg.answer("Invalid plan!")
            return
        pi = pay_sys.PLANS[plan]
        pay_sys.set_user_session(user_id, plan)
        text = (
            f"<b>{pi['display']} 𝗣𝗹𝗮𝗻</b>\n"
            f"<b>𝗣𝗿𝗶𝗰𝗲 ➛</b> ${pi['price']}\n"
            f"<b>𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> {pi['days']} Days\n"
            f"<b>𝗦𝗲𝗹𝗲𝗰𝘁 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗠𝗲𝘁𝗵𝗼𝗱:</b>"
        )
        await _safe_answer(callback)
        await _edit(msg, text, pay_sys.get_network_selection_keyboard(user_id))
        return

    if data.startswith("pay_back_plans_"):
        try:
            owner_id = int(data[15:])
        except ValueError:
            await _safe_answer(callback, "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> Error", show_alert=True)
            return
        if user_id != owner_id:
            await _safe_answer(callback, "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> No permission", show_alert=True)
            return
        await _safe_answer(callback)
        await _edit(msg, _PAYMENT_SELECT_TEXT, pay_sys.get_plan_selection_keyboard())
        return

    if data.startswith("pay_direct_"):
        net_key = data[11:]
        session = pay_sys.get_user_session(user_id)
        if not session or not session.get("plan"):
            await _safe_answer(callback)
            await msg.answer("Session expired! Please select a plan again with /buy.")
            return
        plan = session["plan"]
        if net_key not in pay_sys.USDT_ADDRESSES:
            await _safe_answer(callback)
            await msg.answer("Invalid payment network!")
            return
        text = pay_sys.format_deposit_caption(plan, net_key)
        kb = pay_sys.get_deposit_keyboard(user_id, plan)
        await _safe_answer(callback)
        await _edit(msg, text, kb)
        return

dp.callback_query.register(msh_stop_handler, MshStopCallback.filter())
dp.callback_query.register(msh_result_handler, MshResultCallback.filter())
dp.callback_query.register(mst_stop_handler, MstStopCallback.filter())
dp.callback_query.register(mst_result_handler, MstResultCallback.filter())
dp.callback_query.register(sh_callback_handler, F.data.startswith("sh_"))
dp.callback_query.register(revokeall_callback, F.data.startswith("revokeall_"))

for _cmd, _fn in [
    ("sh", sh_command), ("st", st_command), ("ffc", st_command),
    ("mst", mst_command), ("mffc", mst_command),
    ("sub", sub_command), ("rc", rc_command),
    ("suball", suball_command), ("g_code", g_code_command), ("claim", claim_command),
    ("info", info_command), ("rsub", rsub_command), ("buy", buy_command),
    ("adcr", adcr_command), ("on", on_command), ("off", off_command),
    ("revokeall", revokeall_command),
    ("revoke", revoke_command),
    ("revokeuser", revokeuser_command),
    ("sitechk", sitechk_command), ("addsite", addsite_command),
    ("mysites", mysites_command), ("mysite", mysites_command),
    ("clearsites", clearsites_command), ("clearsite", clearsites_command),
    ("siteall", siteall_command), ("removeall", removeall_command), ("dedupe", dedupe_command),
    ("proxyinfo", proxyinfo_command), ("resetproxy", resetproxy_command),
    ("setprice", setprice_command),
    ("whop", whop_command), ("whophit", whop_command),
    ("stats", stats_command), ("proxy", proxy_command), ("checkproxy", checkproxy_command),
    ("clearproxy", clearproxy_command), ("rtvproxy", rtvproxy_command),
    ("bin", binn_command), ("binn", binn_command),
    ("eren", eren_admin_command), ("remsite", remsite_command),
    ("gen", gen_command)
]:
    if _fn is None:
        continue
    dp.message.register(_fn, Command(_cmd))

setup_feedback_handler(dp)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POLLING STARTUP & WEB SERVER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_dummy_server():
    try:
        app = web.Application()
        app.router.add_get('/', handle_ping)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logging.info(f"Dummy web server started on port {port} to keep Render alive")
    except OSError as e:
        logging.warning(f"Could not bind to dummy web server port (already in use?): {e}. Continuing bot polling anyway.")
    except Exception as e:
        logging.warning(f"Failed to start dummy web server: {e}. Continuing bot polling anyway.")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    pay_sys.set_bot(bot)

    await start_dummy_server()

    asyncio.create_task(shopify_api.auto_health_check())

    logging.info("Bot starting via Long Polling…")
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except Exception as e:
            logging.error(f"Polling error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped.")
