import asyncio
import logging
import psycopg2
import os
import sys
from typing import Callable, Dict, Any, Awaitable
from aiogram import Router, types, BaseMiddleware
from aiogram.filters import Command
from database import DB_CONFIG, get_db_connection

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADMIN_IDS = {7549544641, 8428369446, 6962534443}

router = Router()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SCHEMA — auto-create banned_users table on import
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _ensure_banned_table():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id   BIGINT PRIMARY KEY,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("[DB] banned_users table checked.")
    except Exception as e:
        print(f"[DB] Error creating banned_users table: {e}")

_ensure_banned_table()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SYNC DB HELPERS (run via asyncio.to_thread)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _is_banned_sync(user_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM banned_users WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result is not None

def _ban_user_sync(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO banned_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,)
    )
    conn.commit()
    cur.close()
    conn.close()

def _unban_user_sync(user_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM banned_users WHERE user_id = %s RETURNING user_id",
        (user_id,)
    )
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return result is not None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /ban COMMAND   —  /ban 123456789
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.message(Command("ban"))
async def ban_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Admin only.", parse_mode="HTML")
        return

    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply("Usage: /ban [user_id]")
        return

    target_id = int(parts[1])
    if target_id in ADMIN_IDS:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> Cannot ban the admin.")
        return

    await asyncio.to_thread(_ban_user_sync, target_id)
    await message.reply(
        f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> User <code>{target_id}</code> has been <b>banned</b>.",
        parse_mode="HTML"
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /unban COMMAND  —  /unban 123456789
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.message(Command("unban"))
async def unban_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Admin only.", parse_mode="HTML")
        return

    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply("Usage: /unban [user_id]")
        return

    target_id = int(parts[1])
    removed = await asyncio.to_thread(_unban_user_sync, target_id)

    if removed:
        await message.reply(
            f"<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> User <code>{target_id}</code> has been <b>unbanned</b>.",
            parse_mode="HTML"
        )
    else:
        await message.reply(
            f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> User <code>{target_id}</code> is <b>not</b> in the ban list.",
            parse_mode="HTML"
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /reload COMMAND
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.message(Command("reload"))
async def reload_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Admin only.", parse_mode="HTML")
        return

    await message.reply("🔄 <b>𝗥𝗲𝗯𝗼𝗼𝘁𝗶𝗻𝗴 𝗕𝗼𝘁...</b>", parse_mode="HTML")
    # This restarts the current python process
    os.execv(sys.executable, ['python'] + sys.argv)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BAN MIDDLEWARE
# Runs BEFORE every message handler.  Blocked users get a
# one-line warning and the handler chain is stopped.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BAN_WARNING = (
    "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 𝗯𝗲𝗲𝗻 𝗯𝗮𝗻𝗻𝗲𝗱 𝗳𝗿𝗼𝗺 𝘂𝘀𝗶𝗻𝗴 𝘁𝗵𝗶𝘀 𝗯𝗼𝘁.</b>\n\n"
    "𝗜𝗳 𝘆𝗼𝘂 𝗯𝗲𝗹𝗶𝗲𝘃𝗲 𝘁𝗵𝗶𝘀 𝗶𝘀 𝗮 𝗺𝗶𝘀𝘁𝗮𝗸𝗲, 𝗰𝗼𝗻𝘁𝗮𝗰𝘁 𝘀𝘂𝗽𝗽𝗼𝗿𝘁."
)

class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        user = event.from_user
        if not user or user.id in ADMIN_IDS:
            return await handler(event, data)

        banned = await asyncio.to_thread(_is_banned_sync, user.id)
        if banned:
            try:
                await event.reply(BAN_WARNING, parse_mode="HTML")
            except Exception:
                pass
            return  # stop processing — do NOT call handler

        return await handler(event, data)
