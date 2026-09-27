import sys
import os
import re
import asyncio
import logging
import psycopg2.extras
import aiohttp

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Aiogram Imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from aiogram import Router, F, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PATH FIX (To import database from root folder)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL IMPORTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from database import get_db_connection
from sub import get_premium_status

# Initialize Router
router = Router()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPER: FETCH BIN FROM API DIRECTLY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def fetch_bin_from_api(bin_number: str) -> dict:
    url = f"https://bins.antipublic.cc/bins/{bin_number}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 404:
                    return {"error": "BIN not found."}
                elif resp.status == 429:
                    return {"error": "Rate limit exceeded."}
                else:
                    return {"error": f"API Error: HTTP {resp.status}"}
        except asyncio.TimeoutError:
            return {"error": "Request Timed Out."}
        except Exception as e:
            return {"error": f"Connection Error: {str(e)}"}

async def get_plan_name_from_db(user_id) -> str:
    """Fetches the plan name from receipts DB (only called when user is premium)."""
    def _sync_fetch():
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT plan FROM receipts WHERE user_id = %s ORDER BY purchased_on DESC LIMIT 1",
            (user_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row['plan'].upper() if row else "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"
    try:
        return await asyncio.to_thread(_sync_fetch)
    except Exception as e:
        logging.error(f"Error fetching plan name: {e}")
        return "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND HANDLER (Aiogram 3.x)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/binn"))
async def binn_command(message: types.Message):
    """
    Fetches and displays BIN info using the direct API.
    Works with Args or Reply to a message.
    Deducts 1 credit on successful lookup.
    """

    user = message.from_user
    user_id = user.id

    # 1. EXTRACT TEXT (Check Arguments, then Reply)
    raw_text = ""

    parts = message.text.split()
    if len(parts) > 1:
        raw_text = " ".join(parts[1:])
    elif message.reply_to_message:
        replied_msg = message.reply_to_message
        if replied_msg.text:
            raw_text = replied_msg.text
        elif replied_msg.caption:
            raw_text = replied_msg.caption

    # 2. Validate that we have some text
    if not raw_text:
        await message.reply(
            "𝗘𝗿𝗿𝗼𝗿 ➛ <b>𝗠𝗶𝘀𝘀𝗶𝗻𝗴 𝗔𝗿𝗴𝘂𝗺𝗲𝗻𝘁<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji></b>\n"
            "Usage: <code>/bin 456789</code>\n",
            parse_mode="HTML"
        )
        return

    # 3. Extract and Clean Input (Get first 6 digits)
    digits_only = re.sub(r'\D', '', raw_text)

    if len(digits_only) < 6:
        await message.reply(
            "𝗘𝗿𝗿𝗼𝗿 ➛ <b>𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗜𝗻𝗽𝘂𝘁<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji></b>\n"
            "Please provide at least 6 digits in the argument or replied message.",
            parse_mode="HTML"
        )
        return

    bin_6 = digits_only[:6]

    # 4. Run premium check AND BIN fetch simultaneously
    (is_premium, _), data = await asyncio.gather(
        asyncio.to_thread(get_premium_status, user_id),
        fetch_bin_from_api(bin_6)
    )


    # 7. Prepare UI Variables (get plan name in parallel with nothing — just fetch it)
    plan_name = await get_plan_name_from_db(user_id) if is_premium else "𝗧𝗿𝗶𝗮𝗹"
    user_link = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'
    user_display = f"{user_link} ({plan_name})"

    dev_link = '<a href="https://t.me/Lanxo2">Carder X</a>'
    button = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Carder x", url="https://t.me/zlatanchecker_bot", style="primary")]
    ])

    # 8. Format Response
    if "error" in data:
        final_text = (
            f"𝗦𝘁𝗮𝘁𝘂𝘀 ➛ <b>𝗘𝗿𝗿𝗼𝗿</b>\n"
            f"𝗠𝗲𝘀𝘀𝗮𝗴𝗲 ➛ <code>{data['error']}</code>\n"
            f"𝗗𝗲𝘃 ➛ {dev_link}"
        )
    else:
        api_bin = data.get("bin", bin_6)
        brand = data.get("brand", "N/A")
        level = data.get("level", "N/A")
        bank = data.get("bank", "N/A")
        country = data.get("country_name", "N/A")
        flag = data.get("country_flag", "")
        card_type = data.get("type", "N/A")
        currencies = data.get("country_currencies", [])

        country_display = f"{flag} {country}" if flag else country
        currency_display = currencies[0] if currencies else "N/A"

        final_text = (
            f"𝗕𝗶𝗻 ➛ <code>{api_bin}</code>\n"
            f"𝗕𝗿𝗮𝗻𝗱 ➛ <b>{brand}</b>\n"
            f"𝗟𝗲𝘃𝗲𝗹 ➛ <b>{level}</b>\n"
            f"𝗕𝗮𝗻𝗸 ➛ <b>{bank}</b>\n"
            f"𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➛ <b>{country_display}</b>\n"
            f"𝗧𝘆𝗽𝗲 ➛ <b>{card_type}</b>\n"
            f"𝗖𝘂𝗿𝗿𝗲𝗻𝗰𝘆 ➛ <b>{currency_display}</b>\n"
            f"𝗨𝘀𝗲𝗿 ➛ {user_display}\n"
            f"𝗗𝗲𝘃 ➛ {dev_link}"
        )

    # 9. Send Direct Reply
    await message.reply(
        text=final_text,
        parse_mode="HTML",
        reply_markup=button,
        disable_web_page_preview=True
    )
