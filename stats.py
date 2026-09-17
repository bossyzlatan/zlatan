import asyncio
import logging

from aiogram import types, F, Router

from database import (
    get_charge_leaderboard,
    get_user_charge_rank,
    clear_all_charge_stats,
)

router = Router()

# Admin IDs allowed to run /clearstats
ADMIN_IDS = {6962534443, 8761005192, 8428369446}

MEDALS = ["🥇", "🥈", "🥉"]

# Premium emojis
CROWN = "<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>"
USER = "<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji>"


def _display_name(entry: dict) -> str:
    """Return the best display name (no @ prefix, plain text for link label)."""
    first_name = entry.get("first_name")
    if first_name and str(first_name).lower() not in ("unknown", "user", "none", ""):
        return str(first_name)
    username = entry.get("username")
    if username and str(username).lower() not in ("unknown", "user", "none", ""):
        return f"@{username}"
    return "Unknown"


def _user_link(entry: dict) -> str:
    """Return an HTML link to the user's Telegram profile (click-to-chat)."""
    user_id = entry.get("user_id")
    label = _display_name(entry)
    if user_id:
        return f'<a href="tg://user?id={user_id}">{label}</a>'
    return label


def _build_leaderboard_text(top: list, your_rank: dict | None) -> str:
    """Build the full HTML message body for the leaderboard."""
    if not top:
        header_block = (
            f"{CROWN} <b>LEADERBOARD — TOP CHARGED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>No charged hits recorded yet.</i>\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        lines = [f"{CROWN} <b>LEADERBOARD — TOP CHARGED</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for idx, u in enumerate(top):
            handle = _user_link(u)
            charged = u["cc_charged"]
            hits = u["cc_checked"]
            rate = (charged / hits * 100) if hits > 0 else 0.0
            prefix = MEDALS[idx] if idx < 3 else f"{idx + 1}."
            lines.append(f"{prefix} {handle} — {charged} charged ({hits} hits · {rate:.1f}%)")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        header_block = "\n".join(lines)

    # Footer: your own stats
    if your_rank:
        if your_rank.get("rank") is None:
            # User has hits but 0 charged → show without ranking
            footer = (
                f"{USER} You have <b>0</b> charged · "
                f"{your_rank['hits']} hits · {your_rank['rate']:.1f}% rate\n"
                f"<i>(Not ranked — you need at least 1 charged to enter the leaderboard)</i>"
            )
        else:
            handle = _user_link(your_rank)
            footer = (
                f"{USER} Your rank: #{your_rank['rank']} of {your_rank['total']} | "
                f"{your_rank['charged']} charged · {your_rank['hits']} hits · "
                f"{your_rank['rate']:.1f}% rate"
            )
    else:
        footer = f"{USER} You haven't recorded any hits yet."

    return header_block + "\n" + footer


@router.message(F.text.startswith("/stats"))
@router.message(F.text.startswith("/leaderboard"))
@router.message(F.text.startswith("/top"))
async def stats_command(message: types.Message):
    """Show the top 10 users by charged hits + the caller's own stats."""

    status_msg = await message.answer(
        "<tg-emoji emoji-id='5039579582764680065'>⏳</tg-emoji> "
        "<b>Fetching Leaderboard...</b>",
        parse_mode="HTML"
    )

    try:
        user_id = message.from_user.id

        top, your_rank = await asyncio.gather(
            asyncio.to_thread(get_charge_leaderboard, 10),
            asyncio.to_thread(get_user_charge_rank, user_id),
        )

        text = _build_leaderboard_text(top, your_rank)

        await status_msg.edit_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        logging.error(f"Error in /stats command: {e}")
        try:
            await status_msg.edit_text(
                "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
                "<b>Error fetching stats.</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /clearstats — admin only
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/clearstats"))
async def clearstats_command(message: types.Message):
    """Admin-only: reset every user's cc_checked and cc_charged to 0."""
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
        "<b>Clearing all charge stats...</b>",
        parse_mode="HTML"
    )

    try:
        affected = await asyncio.to_thread(clear_all_charge_stats)

        await status_msg.edit_text(
            f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> "
            f"<b>𝗖𝗵𝗮𝗿𝗴𝗲 𝘀𝘁𝗮𝘁𝘀 𝗰𝗹𝗲𝗮𝗿𝗲𝗱!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿𝘀 𝗿𝗲𝘀𝗲𝘁 ➛</b> <code>{affected}</code>\n"
            f"<i>Every user's charged count and total hits have been set to 0. The leaderboard will now start recording fresh data.</i>",
            parse_mode="HTML"
        )
        logging.info(f"[clearstats] Reset charge stats for {affected} users (by admin {user.id})")

    except Exception as e:
        logging.error(f"Error in /clearstats command: {e}")
        try:
            await status_msg.edit_text(
                f"<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
                f"<b>Error clearing stats:</b> <code>{str(e)[:80]}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
