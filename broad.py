import asyncio
import logging
import time
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from database import get_all_user_ids  # <-- MongoDB function

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADMIN_IDS = {6962534443, 8428369446}
UPDATE_INTERVAL = 10
router = Router()
_broadcast_running = False

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LIVE STATUS BUILDER  <-- MUST BE DEFINED
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _status_text(total: int, done: int, sent: int, blocked: int, failed: int, finished: bool = False) -> str:
    header = "<tg-emoji emoji-id='6242135305697106689'>🎁</tg-emoji> <b>Broadcast Complete</b>" if finished else "<tg-emoji emoji-id='5399913388845322366'>📡</tg-emoji> <b>Broadcasting…</b>"
    bar_filled = int((done / total) * 20) if total else 20
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    return (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total</b>   ➛ <b>{total}</b>\n"
        f"📨 <b>Sent</b>    ➛ <b>{sent}</b>\n"
        f"<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> <b>Blocked</b> ➛ <b>{blocked}</b>\n"
        f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>Failed</b>  ➛ <b>{failed}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<code>[{bar}]</code> {done}/{total}"
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /broad COMMAND
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.message(Command("broad"))
async def broad_command(message: types.Message):
    global _broadcast_running

    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Admin only.", parse_mode="HTML")
        return

    if not message.reply_to_message:
        await message.reply(
            "↩️ Reply to a message with <b>/broad</b> to broadcast it to all users.\n\n"
            "<i>The message is sent as a native bot message — no 'Forwarded from' header.</i>",
            parse_mode="HTML"
        )
        return

    if _broadcast_running:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> A broadcast is already in progress. Please wait for it to finish.")
        return

    _broadcast_running = True
    try:
        # Use MongoDB function instead of raw SQL
        user_ids = await asyncio.to_thread(get_all_user_ids)
        total = len(user_ids)
        target = message.reply_to_message

        if total == 0:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> No users found in database to broadcast.")
            return

        # Initial status message
        status_msg = await message.reply(
            _status_text(total, 0, 0, 0, 0),
            parse_mode="HTML"
        )

        sent = blocked = failed = 0
        last_update = time.monotonic()

        for idx, uid in enumerate(user_ids, start=1):
            try:
                await target.copy_to(chat_id=uid)
                sent += 1
            except TelegramForbiddenError:
                blocked += 1
                logging.debug(f"[broad] blocked by {uid}")
            except TelegramBadRequest as e:
                failed += 1
                logging.debug(f"[broad] bad request for {uid}: {e}")
            except Exception as e:
                failed += 1
                logging.debug(f"[broad] error for {uid}: {e}")

            # Update progress at most once every UPDATE_INTERVAL seconds
            now = time.monotonic()
            if now - last_update >= UPDATE_INTERVAL:
                try:
                    await status_msg.edit_text(
                        _status_text(total, idx, sent, blocked, failed),
                        parse_mode="HTML"
                    )
                    last_update = now
                except Exception:
                    pass

            # ~20 messages/sec — well within Telegram flood limit
            await asyncio.sleep(0.05)

        # Final update
        await status_msg.edit_text(
            _status_text(total, total, sent, blocked, failed, finished=True),
            parse_mode="HTML"
        )

    finally:
        _broadcast_running = False
