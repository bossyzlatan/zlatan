import asyncio
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

async def safe_send_message(bot: Bot, chat_id: int, **kwargs):
    for attempt in range(6):
        try:
            return await bot.send_message(chat_id=chat_id, **kwargs)
        except TelegramRetryAfter as e:
            logging.warning(f"Flood control exceeded for {chat_id}. Retrying in {e.retry_after} seconds (Attempt {attempt+1}/6).")
            await asyncio.sleep(e.retry_after)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logging.warning(f"Cannot send message to {chat_id}: {e}")
            raise e
        except Exception as e:
            logging.error(f"Error sending message to {chat_id}: {e}")
            raise e
    raise Exception(f"Failed to send message to {chat_id} after 6 attempts.")

async def safe_send_animation(bot: Bot, chat_id: int, **kwargs):
    for attempt in range(6):
        try:
            return await bot.send_animation(chat_id=chat_id, **kwargs)
        except TelegramRetryAfter as e:
            logging.warning(f"Flood control exceeded for {chat_id}. Retrying in {e.retry_after} seconds (Attempt {attempt+1}/6).")
            await asyncio.sleep(e.retry_after)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logging.warning(f"Cannot send animation to {chat_id}: {e}")
            raise e
        except Exception as e:
            logging.error(f"Error sending animation to {chat_id}: {e}")
            raise e
    raise Exception(f"Failed to send animation to {chat_id} after 6 attempts.")
