# ═══════════════════════════════════════════════════════════════════════════════
# payments.py - Direct USDT Crypto Payment (BEP20 / TRC20 + Admin Verification)
# ═══════════════════════════════════════════════════════════════════════════════

import logging
from typing import Optional, Dict, Any

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN & WALLET CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

ADMIN_CONTACT_USERNAME = "Lanxo2"
ADMIN_CONTACT_URL = "https://t.me/Lanxo2"

USDT_ADDRESSES: Dict[str, Dict[str, str]] = {
    "BEP20": {
        "name": "USDT (BEP20)",
        "network": "BSC BNB Smart Chain (BEP20)",
        "currency": "USDT",
        "address": "0x905b510ecca97f903edb7455d250f2ecc1854be1"
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# PLAN CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

PLANS: Dict[str, Dict[str, Any]] = {
    "CORE":   {"price": 5, "days": 7,  "display": "𝗖𝗢𝗥𝗘 <tg-emoji emoji-id='5042274086332400375'>🛠️</tg-emoji>"},
    "ELITE":  {"price": 7, "days": 15, "display": "𝗘𝗟𝗜𝗧𝗘 ⭐"},
    "ROOT":   {"price": 15, "days": 30, "display": "𝗥𝗢𝗢𝗧 <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>"}
}

# In-memory user plan selections
user_sessions: Dict[int, Dict[str, str]] = {}
_bot = None

def set_bot(bot):
    global _bot
    _bot = bot

def get_bot():
    return _bot

def set_user_session(user_id: int, plan: str, currency: str = None):
    user_sessions[user_id] = {"plan": plan, "currency": currency}

def get_user_session(user_id: int) -> Optional[Dict]:
    return user_sessions.get(user_id)

def clear_user_session(user_id: int):
    if user_id in user_sessions:
        del user_sessions[user_id]

# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_plan_selection_keyboard():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='𝗖𝗢𝗥𝗘 - $5', callback_data="pay_plan_CORE", icon_custom_emoji_id="5039649904264217620", style="primary")],
        [InlineKeyboardButton(text='𝗘𝗟𝗜𝗧𝗘 - $7', callback_data="pay_plan_ELITE", icon_custom_emoji_id="5039653765439816618", style="success")],
        [InlineKeyboardButton(text='𝗥𝗢𝗢𝗧 - $15', callback_data="pay_plan_ROOT", icon_custom_emoji_id="5039539210072097557", style="danger")],
        [InlineKeyboardButton(text="Contact Admin", url=ADMIN_CONTACT_URL, icon_custom_emoji_id="5042329873662609701", style="primary")],
        [InlineKeyboardButton(text="𝗕𝗮𝗰𝗸", callback_data="menu_pricing", icon_custom_emoji_id="5042020176455795565", style="danger")]
    ])

def get_network_selection_keyboard(user_id: int):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="USDT (BEP20)", callback_data="pay_direct_BEP20", icon_custom_emoji_id="5039727497143387500", style="primary")
        ],
        [
            InlineKeyboardButton(text="Contact Admin", url=ADMIN_CONTACT_URL, icon_custom_emoji_id="5042329873662609701", style="success")
        ],
        [
            InlineKeyboardButton(text="𝗕𝗮𝗰𝗸", callback_data=f"pay_back_plans_{user_id}", icon_custom_emoji_id="5042020176455795565", style="danger")
        ]
    ])

def format_deposit_caption(plan: str, net_key: str) -> str:
    plan_info = PLANS.get(plan, {})
    net_info = USDT_ADDRESSES.get(net_key, {})
    price = plan_info.get("price", 0)
    days = plan_info.get("days", 0)
    display = plan_info.get("display", plan)
    address = net_info.get("address", "")
    network = net_info.get("network", "")

    return (
        f"<b>┌── <tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗣𝗔𝗬𝗠𝗘𝗡𝗧 𝗜𝗡𝗩𝗢𝗜𝗖𝗘 ──┐</b>\n\n"
        f"<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗣𝗹𝗮𝗻 ➛</b> {display}\n"
        f"<b><tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> 𝗣𝗿𝗶𝗰𝗲 ➛</b> ${price} USD\n"
        f"<b><tg-emoji emoji-id='6147637448135414816'>⏳</tg-emoji> 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> {days} Days\n"
        f"<b><tg-emoji emoji-id='5039895103947146186'>🌐</tg-emoji> 𝗡𝗲𝘁𝘄𝗼𝗿𝗸 ➛</b> {network}\n\n"
        f"<b><tg-emoji emoji-id='5042050649248760772'>📥</tg-emoji> 𝗗𝗲𝗽𝗼𝘀𝗶𝘁 𝗔𝗱𝗱𝗿𝗲𝘀𝘀 ➛</b>\n"
        f"<code>{address}</code>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>After paying, send the transaction screenshot to <a href='{ADMIN_CONTACT_URL}'>@{ADMIN_CONTACT_USERNAME}</a> to activate your plan!</b>\n"
        f"<b>└────────────────────────┘</b>"
    )

def get_deposit_keyboard(user_id: int, plan: str):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Contact Admin", url=ADMIN_CONTACT_URL, icon_custom_emoji_id="5042329873662609701", style="success")],
        [InlineKeyboardButton(text="𝗕𝗮𝗰𝗸", callback_data=f"pay_plan_{plan}", icon_custom_emoji_id="5042020176455795565", style="danger")]
    ])
