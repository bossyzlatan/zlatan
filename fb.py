import asyncio
import logging
import uuid
import html
from typing import Any, Dict, List, Optional, Tuple

from aiogram import types, Router, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument,
)

from database import (
    save_pending_feedback,
    get_pending_feedback,
    delete_pending_feedback,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADMIN_IDS = {6962534443, 8428369446}
FEEDBACK_CHANNEL = -1003905461082  # @Zlatan (Main Channel)

router = Router()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MEDIA GROUP COLLECTOR MIDDLEWARE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_MEDIA_GROUPS: Dict[str, List[types.Message]] = {}

class MediaGroupCollectorMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Message, data: Dict[str, Any]):
        gid = getattr(event, "media_group_id", None)
        if gid:
            bucket = _MEDIA_GROUPS.setdefault(gid, [])
            existing_ids = {m.message_id for m in bucket}
            if event.message_id not in existing_ids:
                bucket.append(event)
        return await handler(event, data)

router.message.middleware(MediaGroupCollectorMiddleware())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MEDIA DETECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _detect_media(msg: types.Message) -> str:
    """Returns media type string for a single message."""
    if msg.photo:       return "photo"
    if msg.video:       return "video"
    if msg.animation:   return "animation"
    if msg.document:    return "document"
    return "text"

def _get_file_id(msg: types.Message) -> Optional[str]:
    """Returns the primary file_id from a message, or None."""
    if msg.photo:       return msg.photo[-1].file_id
    if msg.video:       return msg.video.file_id
    if msg.animation:   return msg.animation.file_id
    if msg.document:    return msg.document.file_id
    return None

def _serialize_msg(msg: types.Message) -> dict:
    """Extract only serializable data from a Message."""
    return {
        "chat_id": msg.chat.id,
        "message_id": msg.message_id,
        "media_type": _detect_media(msg),
        "file_id": _get_file_id(msg),
        "caption": (msg.caption or msg.text or "").strip(),
    }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _admin_info_text(user: types.User, media_type: str, count: int) -> str:
    username_part = f"@{user.username}" if user.username else "N/A"
    user_link     = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'
    media_label   = f"{count}× {media_type}" if count > 1 else media_type
    return (
        "━━━━━━━━━━━━━━━━\n"
        f"<b><i>User      ➛ {user_link}</i></b>\n"
        f"<b><i>UID       ➛ {user.id}</i></b>\n"
        f"<b><i>Username  ➛ {username_part}</i></b>\n"
        f"<b><i>Media     ➛ {media_label}</i></b>\n"
        "━━━━━━━━━━━━━━━━"
    )

def _approve_keyboard(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Approve", callback_data=f"fb_approve_{pid}"),
        InlineKeyboardButton(text="Reject", callback_data=f"fb_reject_{pid}"),
    ]])

def _channel_caption(user_id: int, user_first_name: str, hit_text: str) -> str:
    user_link = f'<a href="tg://user?id={user_id}">{user_first_name}</a>'
    note_part = hit_text if hit_text else "—"
    return (
        f"<b>𝗙𝗘𝗘𝗗𝗕𝗔𝗖𝗞</b> __PREMIUM_<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji>__\n"
        f"<b>𝗨𝗦𝗘𝗥 ➛</b> {user_link}\n"
        f"<b>𝗨𝗦𝗘𝗥 𝗜𝗗 ➛</b> {user_id}\n"
        f"━━━━━━━━\n"
        f" {note_part} "
    )

def _channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="𝗕𝘂𝘆 𝗡𝗼𝘄", url="https://t.me/zlatanchecker_bot?start=buy")
    ]])

def _build_input_media(msg: dict, caption: str = "", parse_mode: str = "HTML") -> Optional[Any]:
    """Build InputMedia* from serialized msg dict."""
    fid = msg.get("file_id")
    if not fid:
        return None
    mtype = msg.get("media_type", "document")
    cap = caption or None
    pm  = parse_mode if caption else None
    if mtype == "photo":
        return InputMediaPhoto(media=fid, caption=cap, parse_mode=pm)
    if mtype == "video":
        return InputMediaVideo(media=fid, caption=cap, parse_mode=pm)
    if mtype in ("animation", "document"):
        return InputMediaDocument(media=fid, caption=cap, parse_mode=pm)
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /fb COMMAND HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.message(Command("fb"))
async def feedback_cmd(message: types.Message):
    """Reply to any gate hit with /fb to submit it as feedback.
    Supports: single photo, video, GIF/animation, document, or a photo album (up to 10)."""

    if not message.reply_to_message:
        await message.reply(
            "<b><i>Usage ➛ Reply to your hit message with /fb</i></b>",
            parse_mode="HTML"
        )
        return

    replied = message.reply_to_message
    user    = message.from_user

    # ── Confirm to user INSTANTLY ──────────────────────────────────────────
    try:
        await message.reply(
            "━━━━━━━━━━━━━━━━\n"
            "<b><i>Feedback Submitted ✓</i></b>\n"
            "━━━━━━━━━━━━━━━━",
            parse_mode="HTML"
        )
    except Exception:
        pass

    # ── Collect media group if applicable ───────────────────────────────────
    gid = getattr(replied, "media_group_id", None)
    if gid:
        await asyncio.sleep(0.35)
        group_msgs = list(_MEDIA_GROUPS.get(gid, [replied]))
        group_msgs.sort(key=lambda m: m.message_id)
        group_msgs = group_msgs[:10]
        media_type = "photo_album"
        hit_text   = (replied.caption or replied.text or "").strip()
    else:
        group_msgs = [replied]
        media_type = _detect_media(replied)
        hit_text   = (replied.caption or replied.text or "").strip()

    # ── Serialize messages ──────────────────────────────────────────────────
    serialized = [_serialize_msg(m) for m in group_msgs]

    pid = uuid.uuid4().hex[:10]
    pending_data = {
        "messages": serialized,
        "media_type": media_type,
        "hit_text": hit_text,
        "user_id": user.id,
        "user_first_name": user.first_name or "User",
        "user_username": user.username or "",
        "admin_cards": [],
        "approved": False,
    }

    # ── Persist to MongoDB ──────────────────────────────────────────────────
    saved = await asyncio.to_thread(save_pending_feedback, pid, pending_data)
    if not saved:
        logging.error(f"[fb] Failed to save pending feedback {pid}")
        try:
            await message.reply(
                "<b><i>Internal error — please try again later.</i></b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    admin_text = _admin_info_text(user, media_type, len(group_msgs))

    admin_cards: List[Tuple[int, int]] = []

    async def _forward_all():
        for admin_id in ADMIN_IDS:
            for m in group_msgs:
                try:
                    await message.bot.forward_message(
                        chat_id=admin_id,
                        from_chat_id=m.chat.id,
                        message_id=m.message_id
                    )
                except Exception as e:
                    logging.warning(f"[fb] forward to admin {admin_id} failed: {e}")

    async def _send_admin_info():
        for admin_id in ADMIN_IDS:
            try:
                sent = await message.bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    parse_mode="HTML",
                    reply_markup=_approve_keyboard(pid),
                    disable_web_page_preview=True
                )
                admin_cards.append((admin_id, sent.message_id))
            except Exception as e:
                logging.error(f"[fb] admin notify error for {admin_id}: {e}")
        # Persist admin card message IDs so we can update the other admin later
        if admin_cards:
            try:
                from database import _get_db
                db = _get_db()
                db.pending_feedback.update_one(
                    {"_id": pid},
                    {"$set": {"admin_cards": admin_cards}}
                )
            except Exception as e:
                logging.error(f"[fb] failed to store admin_cards: {e}")

    await asyncio.gather(_forward_all(), _send_admin_info())
    logging.info(f"[fb] Pending {pid} stored for user {user.id} ({media_type}, {len(group_msgs)} item(s))")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# APPROVE CALLBACK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.callback_query(F.data.startswith("fb_approve_"))
async def fb_approve(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text(
            "<b><i>Approved ✓ — Posting to channel...</i></b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    pid   = callback.data.replace("fb_approve_", "")
    entry = await asyncio.to_thread(get_pending_feedback, pid)

    if not entry:
        try:
            await callback.message.edit_text(
                "<b><i>Expired — feedback not found.</i></b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if entry.get("approved"):
        try:
            await callback.message.edit_text(
                "<b><i>Already approved.</i></b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    user_id        = entry["user_id"]
    user_first     = entry["user_first_name"]
    msgs           = entry["messages"]
    media_type     = entry["media_type"]
    hit_text       = entry["hit_text"]
    caption        = _channel_caption(user_id, user_first, hit_text)
    ch_kb          = _channel_keyboard()

    try:
        if media_type == "photo_album" and len(msgs) > 1:
            media_items = []
            for i, m in enumerate(msgs):
                inp = _build_input_media(m, caption=caption if i == 0 else "")
                if inp:
                    media_items.append(inp)
            if media_items:
                await callback.bot.send_media_group(
                    chat_id=FEEDBACK_CHANNEL,
                    media=media_items
                )
                await callback.bot.send_message(
                    chat_id=FEEDBACK_CHANNEL,
                    text="⬆️",
                    reply_markup=ch_kb,
                    disable_web_page_preview=True
                )
            else:
                await callback.bot.forward_message(
                    chat_id=FEEDBACK_CHANNEL,
                    from_chat_id=msgs[0]["chat_id"],
                    message_id=msgs[0]["message_id"]
                )

        elif media_type == "photo":
            await callback.bot.send_photo(
                chat_id=FEEDBACK_CHANNEL,
                photo=msgs[0]["file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=ch_kb
            )
        elif media_type == "video":
            await callback.bot.send_video(
                chat_id=FEEDBACK_CHANNEL,
                video=msgs[0]["file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=ch_kb
            )
        elif media_type == "animation":
            await callback.bot.send_animation(
                chat_id=FEEDBACK_CHANNEL,
                animation=msgs[0]["file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=ch_kb
            )
        elif media_type == "document":
            await callback.bot.send_document(
                chat_id=FEEDBACK_CHANNEL,
                document=msgs[0]["file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=ch_kb
            )
        else:
            await callback.bot.send_message(
                chat_id=FEEDBACK_CHANNEL,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=ch_kb
            )

        # Delete pending entry after successful approval
        await asyncio.to_thread(delete_pending_feedback, pid)

        await callback.message.edit_text(
            "<b><i>Approved ✓ — Posted to channel</i></b>",
            parse_mode="HTML"
        )

        # Update the other admin's card
        for adm_id, msg_id in entry.get("admin_cards", []):
            if adm_id != callback.from_user.id:
                try:
                    await callback.bot.edit_message_text(
                        chat_id=adm_id,
                        message_id=msg_id,
                        text=f"<b><i>Approved ✓ by {html.escape(callback.from_user.first_name or 'Admin')} — Posted to channel</i></b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        logging.info(f"[fb] Approved and posted for user {user_id} ({media_type}, {len(msgs)} item(s))")

    except Exception as e:
        logging.error(f"[fb] Approve post error: {e}")
        try:
            await callback.message.edit_text(
                f"<b><i>Approve failed: {html.escape(str(e))}</i></b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REJECT CALLBACK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.callback_query(F.data.startswith("fb_reject_"))
async def fb_reject(callback: types.CallbackQuery):
    await callback.answer()
    pid   = callback.data.replace("fb_reject_", "")
    entry = await asyncio.to_thread(get_pending_feedback, pid)

    if entry:
        await asyncio.to_thread(delete_pending_feedback, pid)

    try:
        await callback.message.edit_text(
            "<b><i>Rejected ✗</i></b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    if entry:
        for adm_id, msg_id in entry.get("admin_cards", []):
            if adm_id != callback.from_user.id:
                try:
                    await callback.bot.edit_message_text(
                        chat_id=adm_id,
                        message_id=msg_id,
                        text=f"<b><i>Rejected ✗ by {html.escape(callback.from_user.first_name or 'Admin')}</i></b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    logging.info(f"[fb] Rejected feedback {pid}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REGISTRATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def setup_feedback_handler(dispatcher):
    logging.info("[fb] setup_feedback_handler called (no-op — router pre-included).")
