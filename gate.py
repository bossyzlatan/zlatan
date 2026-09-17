from aiogram import types, Router
from database import set_gate_status

# 🔴 Replace with your Telegram user ID(s)
ADMINS = [6962534443, 8428369446]

router = Router()

@router.message(lambda message: message.text and message.text.startswith("/on"))
async def on_command(message: types.Message):
    if message.from_user.id not in ADMINS:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> Usage: /on <gate>\nExample: /on au"
        )
        return

    gate = parts[1].lower()
    set_gate_status(gate, True)

    await message.reply(
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> <b>{gate.upper()}</b> gate is now <b>ENABLED</b>",
        parse_mode="HTML"
    )


@router.message(lambda message: message.text and message.text.startswith("/off"))
async def off_command(message: types.Message):
    if message.from_user.id not in ADMINS:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> Usage: /off <gate>\nExample: /off au"
        )
        return

    gate = parts[1].lower()
    set_gate_status(gate, False)

    await message.reply(
        f"<tg-emoji emoji-id='4958926882994127612'>🚧</tg-emoji> <b>{gate.upper()}</b> gate is now <b>UNDER MAINTENANCE</b>",
        parse_mode="HTML"
    )
