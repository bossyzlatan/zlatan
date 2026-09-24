import asyncio
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import random
import string
import logging
import io

from aiogram import types, F, Router
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

router = Router()

try:
    from database import DB_CONFIG, PooledConn
    _USE_POOL = True
except ImportError:
    DB_CONFIG = {"dbname": "your_db", "user": "your_user", "password": "your_pass", "host": "localhost"}
    _USE_POOL = False

ADMIN_IDS = {6962534443, 8761005192, 8428369446}
LOG_CHANNEL_ID = -1004462990283

KEY_PATTERN = re.compile(r'CARDERX-[A-Z]+-[A-Z0-9]+')

# ═══════════════════════════════════════════════════════════════
# PLAN PRESETS + DURATION HELPERS
# ═══════════════════════════════════════════════════════════════

# format: (display_name, duration_hours, credits, amount_usd)
PLAN_PRESETS = {
    "trial": ("Trail 🛠️",       1 * 24,  999999999, 0),
    "core":  ("Core 🛠️",        7 * 24,  999999999, 5),
    "elite": ("Elite ⭐",        15 * 24, 999999999, 7),
    "root":  ("Root 👑",        30 * 24, 999999999, 15),
}

def parse_duration(s: str):
    """
    Parse a duration string into hours.
    Accepts: 1h, 6h, 12h, 24h, 1d, 3d, 7d, 30d
    Returns int (hours) or None if invalid.
    """
    if not s:
        return None
    s = s.strip().lower()
    m = re.match(r'^(\d+)([hd])$', s)
    if not m:
        return None
    num = int(m.group(1))
    if num <= 0 or num > 3650 * 24:
        return None
    unit = m.group(2)
    if unit == 'h':
        return num
    if unit == 'd':
        return num * 24
    return None


def format_duration(hours: int) -> str:
    """Human-readable duration string."""
    if hours < 24:
        return f"{hours} Hour{'s' if hours != 1 else ''}"
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} Day{'s' if days != 1 else ''}"
    days = hours // 24
    rem = hours % 24
    return f"{days}d {rem}h"


def resolve_plan_arg(plan_arg: str, custom_name: str = None):
    """
    Given a plan argument, return (display_name, duration_hours, credits, amount)
    or None if invalid.
    - If plan_arg is a preset name (core/elite/root/trial), use the preset.
    - Otherwise try to parse it as a duration (e.g. 6h, 3d).
    """
    if not plan_arg:
        return None
    key = plan_arg.lower().strip()

    if key in PLAN_PRESETS:
        name, hours, credits, amount = PLAN_PRESETS[key]
        return (name, hours, credits, amount)

    hours = parse_duration(key)
    if hours is None:
        return None

    if custom_name:
        name = custom_name
    else:
        name = format_duration(hours)

    return (name, hours, 999999999, 0)


# ═══════════════════════════════════════════════════════════════
# DB CONNECTION
# ═══════════════════════════════════════════════════════════════

def get_db_connection():
    if _USE_POOL:
        from database import get_db_connection as _pc
        conn = _pc()
        conn.cursor_factory = RealDictCursor
        return conn
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("ALTER TABLE users ALTER COLUMN user_id TYPE BIGINT")
        conn.commit()
    except Exception:
        conn.rollback()

    columns = [
        ("first_name",      "TEXT"),
        ("username",        "TEXT"),
        ("credits",         "INTEGER DEFAULT 0"),
        ("is_premium",      "INTEGER DEFAULT 0"),
        ("premium_expiry",  "TIMESTAMP"),
        ("joined_at",       "TIMESTAMP DEFAULT NOW()"),
    ]
    for col_name, col_type in columns:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logging.warning(f"Column check {col_name}: {e}")

    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS receipts (
                id SERIAL PRIMARY KEY,
                receipt_id TEXT UNIQUE,
                user_id BIGINT,
                plan TEXT,
                amount_paid REAL,
                purchased_on TIMESTAMP,
                expires_on TIMESTAMP
            )
        ''')
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        cursor.execute("ALTER TABLE receipts ADD COLUMN IF NOT EXISTS expires_on TIMESTAMP")
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        cursor.execute("SELECT amount_paid FROM receipts LIMIT 1")
        conn.rollback()
    except Exception:
        conn.rollback()
        try:
            cursor.execute("ALTER TABLE receipts ADD COLUMN IF NOT EXISTS amount_paid REAL")
            conn.commit()
            try:
                cursor.execute("UPDATE receipts SET amount_paid = amount WHERE amount_paid IS NULL AND amount IS NOT NULL")
                conn.commit()
            except Exception:
                conn.rollback()
        except Exception as e:
            conn.rollback()
            logging.warning(f"Migration amount_paid check: {e}")

    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                credits INTEGER,
                claimed_by BIGINT,
                claimed_at TIMESTAMP
            )
        ''')
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plan_keys (
                key TEXT PRIMARY KEY,
                plan TEXT,
                plan_name TEXT,
                days INTEGER,
                duration_hours INTEGER,
                credits INTEGER,
                claimed_by BIGINT,
                claimed_at TIMESTAMP
            )
        ''')
        conn.commit()
    except Exception:
        conn.rollback()

    # Idempotent add-column for existing deployments
    try:
        cursor.execute("ALTER TABLE plan_keys ADD COLUMN IF NOT EXISTS duration_hours INTEGER")
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS eren_generators (
                user_id BIGINT PRIMARY KEY,
                granted_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        conn.commit()
    except Exception:
        conn.rollback()

    conn.close()


try:
    init_db()
except Exception as e:
    logging.error(f"DB Init Error: {e}")


# ═══════════════════════════════════════════════════════════════
# SYNC HELPERS
# ═══════════════════════════════════════════════════════════════

def _resolve_user_id_sync(target_input):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if target_input.isdigit():
            target_id = int(target_input)
            cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (target_id,))
            if cursor.fetchone():
                return target_id
            return target_id

        target_username = target_input.lstrip('@')
        cursor.execute("SELECT user_id FROM users WHERE username = %s OR username = %s", (target_username, target_input))
        row = cursor.fetchone()
        if row:
            return row['user_id']
        return None
    except Exception as e:
        logging.error(f"Resolve user error: {e}")
        return None
    finally:
        conn.close()


def is_authorized_generator(user_id):
    if user_id in ADMIN_IDS:
        return True
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM eren_generators WHERE user_id = %s", (user_id,))
        return cursor.fetchone() is not None
    except Exception as e:
        logging.error(f"Error checking generator rights: {e}")
        return False
    finally:
        conn.close()


def grant_generator_rights(user_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO eren_generators (user_id)
            VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id,))
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"Error granting generator rights: {e}")
        return False
    finally:
        conn.close()


def revoke_generator_rights(user_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM eren_generators WHERE user_id = %s", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"Error revoking generator rights: {e}")
        return False
    finally:
        conn.close()


def generate_receipt_id():
    random_str = "".join(random.choices(string.digits, k=6))
    return f"CARDERX-{random_str}-CHK"


def mask_receipt_id(receipt_id):
    parts = receipt_id.split('-')
    if len(parts) == 3:
        middle = parts[1]
        if len(middle) >= 2:
            masked_middle = middle[:2] + "XX" + middle[4:]
            return f"{parts[0]}-{masked_middle}-{parts[2]}"
    return receipt_id


def get_premium_status(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium, premium_expiry FROM users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, None
    is_premium_flag = row['is_premium']
    expiry = row['premium_expiry']
    result = (False, None)
    if is_premium_flag == 1:
        if expiry:
            if datetime.now() < expiry:
                result = (True, expiry)
            else:
                cursor.execute(
                    "UPDATE users SET is_premium = 0, premium_expiry = NULL, credits = 150 WHERE user_id = %s",
                    (user_id,)
                )
                conn.commit()
                result = (False, None)
        else:
            cursor.execute("UPDATE users SET is_premium = 0 WHERE user_id = %s", (user_id,))
            conn.commit()
            result = (False, None)
    elif expiry:
        if datetime.now() > expiry:
            cursor.execute(
                "UPDATE users SET premium_expiry = NULL, credits = 150 WHERE user_id = %s",
                (user_id,)
            )
            conn.commit()
            result = (False, None)
    conn.close()
    return result


def _sub_db_sync(target_id, display_name, plan_name, duration_hours, credits, amount):
    """
    Grants premium to a user for the given duration (in hours).
    Returns the new receipt_id.
    """
    expiry_date = datetime.now() + timedelta(hours=duration_hours)
    receipt_id = generate_receipt_id()
    purchased_on = datetime.now()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, credits, joined_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (target_id, "Unknown", "User", 0, datetime.now()))
        cursor.execute("UPDATE users SET first_name = %s WHERE user_id = %s", (display_name, target_id))
        cursor.execute(
            "UPDATE users SET is_premium = 1, premium_expiry = %s WHERE user_id = %s",
            (expiry_date, target_id)
        )
        cursor.execute(
            "UPDATE users SET credits = credits + %s WHERE user_id = %s",
            (credits, target_id)
        )
        cursor.execute(
            "INSERT INTO receipts (receipt_id, user_id, plan, amount_paid, purchased_on, expires_on) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (receipt_id, target_id, plan_name, amount, purchased_on, expiry_date)
        )
        conn.commit()
        return receipt_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _adcr_db_sync(target_id, display_name, add_credits):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, credits, joined_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (target_id, "Unknown", "User", 0, datetime.now()))
        cursor.execute(
            "UPDATE users SET credits = credits + %s WHERE user_id = %s",
            (add_credits, target_id)
        )
        cursor.execute("SELECT credits FROM users WHERE user_id = %s", (target_id,))
        updated = cursor.fetchone()
        new_total = updated['credits'] if updated else 0
        conn.commit()
        return new_total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rsub_db_sync(target_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_premium = 0, premium_expiry = NULL, credits = 150 WHERE user_id = %s",
            (target_id,)
        )
        conn.commit()
        cursor.execute("SELECT first_name, username FROM users WHERE user_id = %s", (target_id,))
        return cursor.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rc_db_sync(receipt_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT r.*, u.first_name, u.is_premium, u.credits
            FROM receipts r
            JOIN users u ON r.user_id = u.user_id
            WHERE r.receipt_id = %s
        ''', (receipt_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def _info_db_sync(user_id, username, first_name):
    get_premium_status(user_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    u_row = cursor.fetchone()
    if not u_row:
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, credits, joined_at) VALUES (%s, %s, %s, %s, %s)",
            (user_id, username, first_name, 0, datetime.now())
        )
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        u_row = cursor.fetchone()
    receipt_row = None
    if u_row['is_premium'] == 1:
        cursor.execute(
            "SELECT plan, purchased_on FROM receipts WHERE user_id = %s ORDER BY purchased_on DESC LIMIT 1",
            (user_id,)
        )
        receipt_row = cursor.fetchone()
    conn.close()
    return dict(u_row), dict(receipt_row) if receipt_row else None


def _suball_db_sync():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT ON (u.user_id)
            u.user_id, u.username, r.receipt_id, r.purchased_on, r.plan, u.premium_expiry, u.credits
        FROM users u
        LEFT JOIN receipts r ON u.user_id = r.user_id
        WHERE u.is_premium = 1 AND u.premium_expiry > NOW()
        ORDER BY u.user_id, r.purchased_on DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows


def _g_code_db_sync(amount):
    generated = []
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for _ in range(amount):
            code = "CARDERX-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            cursor.execute("INSERT INTO codes (code, credits) VALUES (%s, %s)", (code, 100))
            generated.append(code)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error generating codes: {e}")
    finally:
        conn.close()
    return generated


def _gen_plan_keys_db_sync(plan, plan_name, duration_hours, amount):
    """
    Generate `amount` plan keys with the given duration.
    `days` field is kept for backwards compatibility (0 if <24h).
    """
    generated = []
    days_val = duration_hours // 24 if duration_hours >= 24 else 0
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for _ in range(amount):
            key = f"CARDERX-{plan.upper()}-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
            cursor.execute(
                "INSERT INTO plan_keys (key, plan, plan_name, days, duration_hours, credits) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (key, plan, plan_name, days_val, duration_hours, 999999999)
            )
            generated.append(key)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error generating plan keys: {e}")
    finally:
        conn.close()
    return generated


def _claim_db_sync(user_id, display_name, code):
    """
    Returns one of:
      ("invalid",  None)
      ("claimed",  None)
      ("premium",  None)
      ("plan_ok",  (plan_name, duration_hours, receipt_id))
      ("error",    None)
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM plan_keys WHERE key = %s", (code,))
        p_row = cursor.fetchone()
        if p_row:
            if p_row.get('claimed_by') is not None:
                return "claimed", None

            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, credits, joined_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id, "Unknown", "User", 999999999, datetime.now()))

            cursor.execute("SELECT is_premium, premium_expiry FROM users WHERE user_id = %s", (user_id,))
            u_row = cursor.fetchone()

            if u_row and u_row['is_premium'] == 1 and u_row['premium_expiry'] and datetime.now() < u_row['premium_expiry']:
                return "premium", None

            cursor.execute("UPDATE users SET first_name = %s WHERE user_id = %s", (display_name, user_id))

            # Determine duration: prefer duration_hours, fall back to days
            duration_hours = p_row.get('duration_hours')
            if not duration_hours:
                duration_hours = (p_row.get('days') or 0) * 24
            if duration_hours <= 0:
                duration_hours = 24  # safety fallback: 1 day

            expiry_date = datetime.now() + timedelta(hours=duration_hours)
            receipt_id = generate_receipt_id()
            purchased_on = datetime.now()

            cursor.execute(
                "UPDATE users SET is_premium = 1, premium_expiry = %s, credits = 999999999 WHERE user_id = %s",
                (expiry_date, user_id)
            )
            cursor.execute(
                "INSERT INTO receipts (receipt_id, user_id, plan, amount_paid, purchased_on, expires_on) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (receipt_id, user_id, p_row.get('plan_name') or "Plan", 0.0, purchased_on, expiry_date)
            )
            cursor.execute(
                "UPDATE plan_keys SET claimed_by = %s, claimed_at = %s WHERE key = %s",
                (user_id, datetime.now(), code)
            )
            conn.commit()
            return "plan_ok", (p_row.get('plan_name') or "Plan", duration_hours, receipt_id)

        return "invalid", None
    except Exception as e:
        conn.rollback()
        logging.error(f"Error claiming code/key: {e}")
        return "error", None
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# COMMAND: /buy
# ═══════════════════════════════════════════════════════════════

_BUY_TEXT = (
    "<b>┌── <tg-emoji emoji-id='5039623284056917259'>💳</tg-emoji> 𝗣𝗥𝗜𝗖𝗜𝗡𝗚 𝗣𝗟𝗔𝗡𝗦 ──┐</b>\n\n"
    "<b><tg-emoji emoji-id='5042274086332400375'>🛠️</tg-emoji> 𝗖𝗢𝗥𝗘 𝗣𝗟𝗔𝗡</b>\n"
    "<b>├ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> 𝟳 Days\n"
    "<b>├ 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 ➛</b> Unlimited (∞)\n"
    "<b>└ 𝗣𝗿𝗶𝗰𝗲 ➛</b> 𝟱$\n\n"
    "<b><tg-emoji emoji-id='5278751923338490157'>⭐</tg-emoji> 𝗘𝗟𝗜𝗧𝗘 𝗣𝗟𝗔𝗡</b>\n"
    "<b>├ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> 𝟭𝟱 Days\n"
    "<b>├ 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 ➛</b> Unlimited (∞)\n"
    "<b>└ 𝗣𝗿𝗶𝗰𝗲 ➛</b> 𝟳$\n\n"
    "<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗥𝗢𝗢𝗧 𝗣𝗟𝗔𝗡</b>\n"
    "<b>├ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> 𝟯𝟬 Days\n"
    "<b>├ 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 ➛</b> Unlimited (∞)\n"
    "<b>└ 𝗣𝗿𝗶𝗰𝗲 ➛</b> 𝟭𝟱$\n"
    "<b>└──────────────────┘</b>"
)
_BUY_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text='𝗕𝘂𝘆 𝗡𝗼𝘄', callback_data="menu_payment_methods", style="success"),
        InlineKeyboardButton(text='Contact Admin', url="https://t.me/Lanxo2", style="primary")
    ]
])


@router.message(F.text.startswith("/buy"))
async def buy_command(message: types.Message):
    await message.reply(text=_BUY_TEXT, parse_mode="HTML", reply_markup=_BUY_KB)


# ═══════════════════════════════════════════════════════════════
# COMMAND: /eren — grant unlimited access
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.lower().startswith("/eren"))
async def eren_admin_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀.")
        return

    cmd_name = "/eren"

    args = message.text.split()[1:]
    if not args:
        await message.reply(f"<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗨𝘀𝗮𝗴𝗲: {cmd_name} {{user_id/username}}")
        return

    target_input = args[0]
    target_id = await asyncio.to_thread(_resolve_user_id_sync, target_input)
    if not target_id:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗖𝗼𝘂𝗹𝗱 𝗻𝗼𝘁 𝗳𝗶𝗻𝗱 𝘂𝘀𝗲𝗿 𝗶𝗻 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲.")
        return

    display_name = "User"
    try:
        chat = await message.bot.get_chat(target_id)
        display_name = chat.first_name or chat.username or "User"
    except Exception:
        pass

    user_link = f'<a href="tg://user?id={target_id}">{display_name}</a>'

    plan_name = "Superuser"
    duration_hours = 99999 * 24
    credits = 999999999
    amount = 0.0

    try:
        receipt_id = await asyncio.to_thread(
            _sub_db_sync, target_id, display_name, plan_name, duration_hours, credits, amount
        )
    except Exception as e:
        logging.error(f"Error executing /eren admin command: {e}")
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗘𝗿𝗿𝗼𝗿.")
        return

    caption = (
        f"𝐂𝐨𝐧𝐠𝐫𝐚𝐭𝐮𝐥𝐚𝐭𝐢𝐨𝐧𝐬!🎉 𝐲𝐨𝐮𝐫 𝐚𝐜𝐜𝐞𝐬𝐬 𝐡𝐚𝐬 𝐛𝐞𝐞𝐧 𝐚𝐜𝐭𝐢𝐯𝐚𝐭𝐞𝐝.\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ Carder X <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> (UNLIMITED)\n"
        f"𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛ {format_duration(duration_hours)}\n"
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> <b>𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛</b> <code>{receipt_id}</code>\n"
        f"𝗽𝗹𝗲𝗮𝘀𝗲 𝘀𝗮𝘃𝗲 𝘁𝗵𝗶𝘀 𝗿𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗."
    )
    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗦𝘂𝗽𝗽𝗼𝗿𝘁", url="https://t.me/Lanxo2", style="primary")]
    ])

    try:
        await message.bot.send_message(chat_id=target_id, text=caption, parse_mode="HTML", reply_markup=support_kb)
    except Exception as e:
        logging.error(f"Could not DM user {target_id}: {e}")

    await message.reply(f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗨𝗻𝗹𝗶𝗺𝗶𝘁𝗲𝗱 𝗮𝗰𝗰𝗲𝘀𝘀 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 𝘁𝗼 {user_link}.")


# ═══════════════════════════════════════════════════════════════
# COMMAND: /sub — grant premium to a user
#
# Usage:
#   /sub <user> <preset>                       → e.g. /sub 123 core
#   /sub <user> <duration>                     → e.g. /sub 123 6h  |  /sub 123 3d
#   /sub <user> <duration> <custom name>       → e.g. /sub 123 12h Starter Pass
#   /sub <user> <preset> <custom name>         → e.g. /sub 123 core Custom Core
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/sub"))
async def sub_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀.")
        return

    args = message.text.split(maxsplit=3)[1:]
    if len(args) < 2:
        await message.reply(
            "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>𝗨𝘀𝗮𝗴𝗲:</b>\n"
            "<code>/sub &lt;user&gt; &lt;preset&gt;</code> — presets: core, elite, root, trial\n"
            "<code>/sub &lt;user&gt; &lt;duration&gt;</code> — durations: 1h, 6h, 12h, 1d, 3d, 7d, 30d\n"
            "<code>/sub &lt;user&gt; &lt;preset|duration&gt; &lt;custom name&gt;</code>\n\n"
            "<b>𝗘𝘅𝗮𝗺𝗽𝗹𝗲𝘀:</b>\n"
            "<code>/sub 123456 core</code>\n"
            "<code>/sub 123456 6h</code>\n"
            "<code>/sub 123456 12h Starter Pass</code>",
            parse_mode="HTML"
        )
        return

    target_input = args[0]
    plan_arg = args[1]
    custom_name = args[2] if len(args) >= 3 else None

    resolved = resolve_plan_arg(plan_arg, custom_name)
    if resolved is None:
        await message.reply(
            "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> "
            "𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗽𝗹𝗮𝗻 𝗼𝗿 𝗱𝘂𝗿𝗮𝘁𝗶𝗼𝗻.\n\n"
            "<b>Presets:</b> <code>core</code>, <code>elite</code>, <code>root</code>, <code>trial</code>\n"
            "<b>Durations:</b> <code>1h</code>, <code>6h</code>, <code>12h</code>, <code>1d</code>, <code>3d</code>, <code>7d</code>, <code>30d</code>",
            parse_mode="HTML"
        )
        return

    plan_name, duration_hours, credits, amount = resolved

    target_id = await asyncio.to_thread(_resolve_user_id_sync, target_input)
    if not target_id:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗖𝗼𝘂𝗹𝗱 𝗻𝗼𝘁 𝗳𝗶𝗻𝗱 𝘂𝘀𝗲𝗿 𝗶𝗻 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲.")
        return

    display_name = "User"
    try:
        chat = await message.bot.get_chat(target_id)
        display_name = chat.first_name or chat.username or "User"
    except Exception:
        pass

    user_link = f'<a href="tg://user?id={target_id}">{display_name}</a>'

    try:
        receipt_id = await asyncio.to_thread(
            _sub_db_sync, target_id, display_name, plan_name, duration_hours, credits, amount
        )
    except Exception as e:
        logging.error(f"Error updating subscription: {e}")
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗘𝗿𝗿𝗼𝗿.")
        return

    masked_id = mask_receipt_id(receipt_id)

    caption = (
        f"𝐂𝐨𝐧𝐠𝐫𝐚𝐭𝐮𝐥𝐚𝐭𝐢𝐨𝐧𝐬!🎉 𝐲𝐨𝐮𝐫 𝐚𝐜𝐜𝐞𝐬𝘀 𝐡𝐚𝐬 𝐛𝐞𝐞𝐧 𝐚𝐜𝐭𝐢𝐯𝐚𝐭𝐞𝐝.\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ {plan_name}\n"
        f"𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛ {format_duration(duration_hours)}\n"
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛ <code>{receipt_id}</code>\n"
        f"𝗽𝗹𝗲𝗮𝘀𝗲 𝘀𝗮𝘃𝗲 𝘁𝗵𝗶𝘀 𝗿𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗."
    )
    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗦𝘂𝗽𝗽𝗼𝗿𝘁", url="https://t.me/Lanxo2", style="primary")]
    ])
    log_text = (
        f"<b>🛒 𝗡𝗘𝗪 𝗣𝗟𝗔𝗡 𝗣𝗨𝗥𝗖𝗛𝗔𝗦𝗘𝗗</b>\n"
        f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛</b> {user_link}\n"
        f"<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛</b> {plan_name}\n"
        f"<b>⏱️ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> {format_duration(duration_hours)}\n"
        f"<b><tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> 𝗔𝗺𝗼𝘂𝗻𝘁 ➛</b> <b>{amount} USD</b>\n"
        f"<b><tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛</b> <code>{masked_id}</code>"
    )
    log_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗕𝘂𝘆 𝗡𝗼𝘄", callback_data="show_buy_plans")]
    ])

    try:
        await message.bot.send_message(chat_id=target_id, text=caption, parse_mode="HTML", reply_markup=support_kb)
    except Exception as e:
        logging.error(f"Could not DM user {target_id}: {e}")

    try:
        await message.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text, parse_mode="HTML", reply_markup=log_kb)
    except Exception:
        pass

    await message.reply(
        f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> "
        f"𝗣𝗿𝗲𝗺𝗶𝘂𝗺 <b>{plan_name}</b> 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 𝘁𝗼 <code>{target_id}</code> 𝗳𝗼𝗿 "
        f"<b>{format_duration(duration_hours)}</b>."
    )


# ═══════════════════════════════════════════════════════════════
# COMMAND: /adcr — disabled
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/adcr"))
async def adcr_command(message: types.Message):
    await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗱𝗶𝘀𝗮𝗯𝗹𝗲𝗱.")


# ═══════════════════════════════════════════════════════════════
# COMMAND: /rsub — remove premium
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/rsub"))
async def rsub_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀.")
        return

    args = message.text.split()[1:]
    if not args:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗨𝘀𝗮𝗴𝗲: /rsub {user_id/username}")
        return

    target_id = await asyncio.to_thread(_resolve_user_id_sync, args[0])
    if not target_id:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗖𝗼𝘂𝗹𝗱 𝗻𝗼𝘁 𝗳𝗶𝗻𝗱 𝘂𝘀𝗲𝗿 𝗶𝗻 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲.")
        return

    try:
        user_details = await asyncio.to_thread(_rsub_db_sync, target_id)
    except Exception as e:
        logging.error(f"Error removing subscription: {e}")
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗘𝗿𝗿𝗼𝗿.")
        return

    if not user_details:
        await message.reply(
            f"<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> "
            f"𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗿𝗲𝗺𝗼𝘃𝗲𝗱 𝗳𝗿𝗼𝗺 <code>{target_id}</code> "
            f"𝗮𝗻𝗱 𝗰𝗿𝗲𝗱𝗶𝘁𝘀 𝗿𝗲𝘀𝗲𝘁 𝘁𝗼 <b>𝟭𝟱𝟬</b>."
        )
        return

    display_name = "User"
    temp_name = user_details['first_name'] if user_details['first_name'] else user_details['username']
    if temp_name and temp_name not in ["Unknown", "User"]:
        display_name = temp_name
    else:
        try:
            chat = await message.bot.get_chat(target_id)
            display_name = chat.first_name or "User"
        except Exception:
            pass

    dm_text = (
        f"𝗬𝗼𝘂𝗿 𝗮𝗰𝗰𝗲𝘀𝘀 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗲𝗻𝗱𝗲𝗱. 𝗧𝗵𝗮𝗻𝗸 𝘆𝗼𝘂 𝗳𝗼𝗿 𝘂𝘀𝗶𝗻𝗴.\n\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {display_name}\n"
        f"𝗨𝘀𝗲𝗿 𝗜𝗗 ➛ <code>{target_id}</code>\n"
        f"𝘀𝘁𝗮𝘁𝘂𝘀 ➛ <b>Trial</b>\n"
        f"𝗖𝗿𝗲𝗱𝗶𝘁𝘀 ➛ <b>150</b>"
    )
    buy_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗕𝘂𝘆 𝗡𝗼𝘄", callback_data="show_buy_plans")]
    ])

    try:
        await message.bot.send_message(chat_id=target_id, text=dm_text, parse_mode="HTML", reply_markup=buy_kb)
    except Exception as e:
        logging.error(f"Could not DM user {target_id}: {e}")

    await message.reply(
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> "
        f"𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗿𝗲𝗺𝗼𝘃𝗲𝗱 𝗳𝗿𝗼𝗺 <code>{target_id}</code> "
        f"𝗮𝗻𝗱 𝗰𝗿𝗲𝗱𝗶𝘁𝘀 𝗿𝗲𝘀𝗲𝘁 𝘁𝗼 <b>𝟭𝟱𝟬</b>."
    )


# ═══════════════════════════════════════════════════════════════
# COMMAND: /rc — receipt lookup
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/rc"))
async def rc_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.")
        return

    args = message.text.split()[1:]
    if not args:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗨𝘀𝗮𝗴𝗲: /rc {receipt_id}")
        return

    receipt_id = args[0]
    row = await asyncio.to_thread(_rc_db_sync, receipt_id)

    if not row:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 𝗻𝗼𝘁 𝗳𝗼𝘂𝗻𝗱.")
        return

    uid = row['user_id']
    fname = row['first_name']
    if not fname or fname in ["User", "Unknown"]:
        try:
            chat = await message.bot.get_chat(uid)
            fname = chat.first_name or "Unknown"
        except Exception:
            fname = "Unknown"

    user_link = f'<a href="tg://user?id={uid}">{fname}</a>'
    amount_val = row.get('amount_paid') if row.get('amount_paid') is not None else row.get('amount', 0)
    date_str    = row['purchased_on'].strftime('%Y-%m-%d') if row['purchased_on'] else "N/A"
    expires_str = row['expires_on'].strftime('%Y-%m-%d %H:%M') if row.get('expires_on') else "N/A"
    text = (
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"𝗨𝘀𝗲𝗿 𝗜𝗗 ➛ <code>{uid}</code>\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ <b>{row['plan']}</b>\n"
        f"𝗣𝘂𝗿𝗰𝗵𝗮𝘀𝗲𝗱 𝗢𝗻 ➛ {date_str}\n"
        f"𝗘𝘅𝗽𝗶𝗿𝗲𝘀 𝗢𝗻 ➛ <b>{expires_str}</b>\n"
        f"<b><tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> 𝗔𝗺𝗼𝘂𝗻𝘁 ➛</b> <b>{amount_val} USD</b>\n"
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛ <code>{row['receipt_id']}</code>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CARDER X", url="https://t.me/zlatanchecker_bot", style="primary")]
    ])
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════
# COMMAND: /info
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/info"))
async def info_command(message: types.Message):
    user = message.from_user
    user_id = user.id

    u_row, r_row = await asyncio.to_thread(_info_db_sync, user_id, user.username, user.first_name)

    raw_name = "N/A"
    if user.username:
        raw_name = f"@{user.username}"
    elif user.first_name:
        raw_name = user.first_name
    elif u_row.get('username') and u_row['username'].lower() not in ['unknown', 'user', 'none']:
        raw_name = u_row['username']
    elif u_row.get('first_name') and u_row['first_name'].lower() not in ['unknown', 'user', 'none']:
        raw_name = u_row['first_name']

    username_link = f'<a href="tg://user?id={user_id}">{raw_name}</a>' if raw_name != "N/A" else raw_name

    access_str    = "𝗧𝗿𝗶𝗮𝗹"
    purchased_str = "N/A"
    ending_str    = "N/A"

    if u_row['is_premium'] == 1 and r_row:
        access_str    = r_row['plan']
        purchased_str = r_row['purchased_on'].strftime('%Y-%m-%d') if r_row['purchased_on'] else "N/A"
        if u_row.get('premium_expiry'):
            ending_str = u_row['premium_expiry'].strftime('%Y-%m-%d %H:%M')
    elif u_row['is_premium'] == 1:
        access_str = "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"

    joined_disp = u_row['joined_at'].strftime('%Y-%m-%d') if u_row.get('joined_at') else "N/A"

    text = (
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {username_link}\n"
        f"𝗨𝘀𝗲𝗿 𝗜𝗗 ➛ <code>{user_id}</code>\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ <b>{access_str}</b>\n"
        f"𝗣𝘂𝗿𝗰𝗵𝗮𝘀𝗲𝗱 𝗼𝗻 ➛ <b>{purchased_str}</b>\n"
        f"𝗘𝗻𝗱𝗶𝗻𝗴 𝗼𝗻 ➛ <b>{ending_str}</b>\n"
        f"𝗝𝗼𝗶𝗻𝗲𝗱 ➛ <b>{joined_disp}</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CARDER X", url="https://t.me/zlatanchecker_bot", style="primary")]
    ])
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════
# COMMAND: /suball
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/suball"))
async def suball_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        return

    rows = await asyncio.to_thread(_suball_db_sync)

    if not rows:
        await message.reply("𝗡𝗼 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝘂𝘀𝗲𝗿𝘀 𝗳𝗼𝘂𝗻𝗱.")
        return

    output = io.BytesIO()
    output.write("𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝘂𝘀𝗲𝗿𝘀 𝗟𝗶𝘀𝘁\n\n".encode('utf-8'))
    for row in rows:
        expiry_date = row['premium_expiry'].strftime('%Y-%m-%d %H:%M') if row['premium_expiry'] else "N/A"
        line = (
            f"ID: {row['user_id']}\n"
            f"Username: {row['username'] or 'N/A'}\n"
            f"Plan: {row['plan'] or 'N/A'}\n"
            f"Expiry: {expiry_date}\n"
            f"{'-'*30}\n"
        ).encode('utf-8')
        output.write(line)

    filename = f"premium_users_{datetime.now().strftime('%Y%m%d')}.txt"
    document = BufferedInputFile(output.getvalue(), filename=filename)
    await message.reply_document(document=document)


# ═══════════════════════════════════════════════════════════════
# COMMAND: /g_code — disabled
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/g_code"))
async def g_code_command(message: types.Message):
    await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗱𝗶𝘀𝗮𝗯𝗹𝗲𝗱.")


# ═══════════════════════════════════════════════════════════════
# COMMAND: /claim
# ═══════════════════════════════════════════════════════════════

CLAIM_LOCKS = {}


@router.message(F.text.startswith("/claim"))
async def claim_command(message: types.Message):
    user = message.from_user
    user_id = user.id

    async with CLAIM_LOCKS.setdefault(user_id, asyncio.Lock()):
        display_name = user.first_name or user.username or "User"

        args = message.text.split()[1:]
        codes_to_try = []

        if args:
            codes_to_try = [args[0].upper()]
        elif message.reply_to_message:
            reply_text = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            )
            found = KEY_PATTERN.findall(reply_text.upper())
            codes_to_try = list(dict.fromkeys(found))
            if not codes_to_try:
                await message.reply(
                    "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> "
                    "𝗡𝗼 𝗸𝗲𝘆𝘀 𝗳𝗼𝘂𝗻𝗱 𝗶𝗻 𝘁𝗵𝗲 𝗿𝗲𝗽𝗹𝗶𝗲𝗱 𝗺𝗲𝘀𝘀𝗮𝗴𝗲.\n"
                    "𝗨𝘀𝗮𝗴𝗲: <code>/claim {code}</code> 𝗼𝗿 𝗿𝗲𝗽𝗹𝘆 𝘁𝗼 𝗮 "
                    "𝗺𝗲𝘀𝘀𝗮𝗴𝗲 𝗰𝗼𝗻𝘁𝗮𝗶𝗻𝗶𝗻𝗴 𝗸𝗲𝘆𝘀."
                )
                return
        else:
            await message.reply(
                "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> "
                "𝗨𝘀𝗮𝗴𝗲: <code>/claim {code}</code>\n"
                "𝗢𝗿 𝗿𝗲𝗽𝗹𝘆 𝘁𝗼 𝗮 𝗺𝗲𝘀𝘀𝗮𝗴𝗲 𝗰𝗼𝗻𝘁𝗮𝗶𝗻𝗶𝗻𝗴 𝗴𝗲𝗻𝗲𝗿𝗮𝘁𝗲𝗱 "
                "𝗸𝗲𝘆𝘀 𝘄𝗶𝘁𝗵 <code>/claim</code>."
            )
            return

        final_status = None
        final_data = None
        skipped_count = 0

        for code in codes_to_try:
            status, result_data = await asyncio.to_thread(
                _claim_db_sync, user_id, display_name, code
            )

            if status == "plan_ok":
                final_status = status
                final_data = result_data
                break
            elif status == "premium":
                await message.reply(
                    "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
                    "𝗨𝘀𝗲𝗿𝘀 𝘄𝗶𝘁𝗵 𝗮𝗻 𝗮𝗰𝘁𝗶𝘃𝗲 𝗽𝗹𝗮𝗻 𝗰𝗮𝗻𝗻𝗼𝘁 𝗿𝗲𝗱𝗲𝗲𝗺 𝗸𝗲𝘆𝘀."
                )
                return
            else:
                skipped_count += 1
                continue

        if final_status == "plan_ok":
            plan_name, duration_hours, receipt_id = final_data
            caption = (
                f"𝐂𝐨𝐧𝐠𝐫𝐚𝐭𝐮𝐥𝐚𝐭𝐢𝐨𝐧𝐬!🎉 𝐲𝐨𝐮𝐫 𝐚𝐜𝐜𝐞𝐬𝘀 𝐡𝐚𝐬 𝐛𝐞𝐞𝐧 𝐚𝐜𝐭𝐢𝐯𝐚𝐭𝐞𝐝.\n"
                f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ "
                f"<a href=\"tg://user?id={user_id}\">{display_name}</a>\n"
                f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ {plan_name}\n"
                f"𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛ {format_duration(duration_hours)}\n"
                f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗶𝗱 ➛ "
                f"<code>{receipt_id}</code>\n"
                f"𝗽𝗹𝗲𝗮𝘀𝗲 𝘀𝗮𝘃𝗲 𝘁𝗵𝗶𝘀 𝗿𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗."
            )
            support_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="𝗦𝘂𝗽𝗽𝗼𝗿𝘁", url="https://t.me/Lanxo2", style="primary")]
            ])
            await message.reply(caption, parse_mode="HTML", reply_markup=support_kb)

            log_text = (
                f"<b>🛒 𝗡𝗘𝗪 𝗣𝗟𝗔𝗡 𝗖𝗟𝗔𝗜𝗠𝗘𝗗 𝗩𝗜𝗔 𝗞𝗘𝗬</b>\n"
                f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛</b> "
                f"<a href=\"tg://user?id={user_id}\">{display_name}</a>\n"
                f"<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛</b> {plan_name}\n"
                f"<b>⏱️ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛</b> {format_duration(duration_hours)}\n"
                f"<b><tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛</b> "
                f"<code>{mask_receipt_id(receipt_id)}</code>"
            )
            try:
                await message.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text, parse_mode="HTML")
            except Exception:
                pass

        elif skipped_count > 0:
            await message.reply(
                "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> "
                "𝗔𝗹𝗹 𝗸𝗲𝘆𝘀 𝗶𝗻 𝘁𝗵𝗶𝘀 𝗺𝗲𝘀𝘀𝗮𝗴𝗲 𝗵𝗮𝘃𝗲 𝗮𝗹𝗿𝗲𝗮𝗱𝘆 𝗯𝗲𝗲𝗻 "
                "𝗰𝗹𝗮𝗶𝗺𝗲𝗱 𝗼𝗿 𝗮𝗿𝗲 𝗶𝗻𝘃𝗮𝗹𝗶𝗱."
            )
        else:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗖𝗼𝗱𝗲.")


# ═══════════════════════════════════════════════════════════════
# COMMAND: /gen — generate plan keys
#
# Usage:
#   /gen <preset> <qty>                       → e.g. /gen core 10
#   /gen <duration> <qty>                     → e.g. /gen 6h 10   |  /gen 3d 5
#   /gen <duration> <qty> <custom name>       → e.g. /gen 12h 5 Starter Pass
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/gen"))
async def gen_command(message: types.Message):
    user = message.from_user
    if not await asyncio.to_thread(is_authorized_generator, user.id):
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.")
        return

    args = message.text.split(maxsplit=3)[1:]
    if len(args) < 2:
        await message.reply(
            "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> <b>𝗨𝘀𝗮𝗴𝗲:</b>\n"
            "<code>/gen &lt;preset&gt; &lt;qty&gt;</code> — presets: trial, core, elite, root\n"
            "<code>/gen &lt;duration&gt; &lt;qty&gt;</code> — durations: 1h, 6h, 12h, 1d, 3d, 7d, 30d\n"
            "<code>/gen &lt;duration&gt; &lt;qty&gt; &lt;custom name&gt;</code>\n\n"
            "<b>𝗘𝘅𝗮𝗺𝗽𝗹𝗲𝘀:</b>\n"
            "<code>/gen core 10</code>\n"
            "<code>/gen 6h 5</code>\n"
            "<code>/gen 12h 5 Starter Pass</code>\n"
            "<code>/gen 3d 20 Weekend Pack</code>",
            parse_mode="HTML"
        )
        return

    plan_arg = args[0]
    quantity_str = args[1]
    custom_name = args[2] if len(args) >= 3 else None

    resolved = resolve_plan_arg(plan_arg, custom_name)
    if resolved is None:
        await message.reply(
            "<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> "
            "𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗽𝗹𝗮𝗻 𝗼𝗿 𝗱𝘂𝗿𝗮𝘁𝗶𝗼𝗻.\n\n"
            "<b>Presets:</b> <code>trial</code>, <code>core</code>, <code>elite</code>, <code>root</code>\n"
            "<b>Durations:</b> <code>1h</code>, <code>6h</code>, <code>12h</code>, <code>1d</code>, <code>3d</code>, <code>7d</code>, <code>30d</code>",
            parse_mode="HTML"
        )
        return

    plan_name, duration_hours, credits, _ = resolved

    # For custom-duration keys we need a stable "plan" identifier for the key prefix
    if plan_arg.lower() in PLAN_PRESETS:
        plan_id = plan_arg.lower()
    else:
        # For custom durations the key prefix is a short code derived from the duration
        if duration_hours < 24:
            plan_id = f"{duration_hours}H"
        else:
            days = duration_hours // 24
            plan_id = f"{days}D"

    try:
        amount = int(quantity_str)
        if amount <= 0 or amount > 50:
            raise ValueError()
    except ValueError:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗾𝘂𝗮𝗻𝘁𝗶𝘁𝘆 (1-50).")
        return

    generated_keys = await asyncio.to_thread(
        _gen_plan_keys_db_sync, plan_id, plan_name, duration_hours, amount
    )

    formatted_keys = "\n".join([f"<code>{k}</code>" for k in generated_keys])
    response = (
        f"<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗚𝗲𝗻𝗲𝗿𝗮𝘁𝗲𝗱 𝗞𝗲𝘆𝘀 𝗳𝗼𝗿 {plan_name}</b>\n"
        f"<b>𝗤𝘂𝗮𝗻𝘁𝗶𝘁𝘆:</b> {amount}\n"
        f"<b>𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻:</b> {format_duration(duration_hours)}\n\n"
        f"{formatted_keys}\n\n"
        f"𝗨𝘀𝗲 /𝗰𝗹𝗮𝗶𝗺 𝘁𝗼 𝗿𝗲𝗱𝗲𝗲𝗺."
    )
    await message.reply(response, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# COMMAND: /eren — grant/revoke generator rights
# ═══════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/eren"))
async def eren_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.")
        return

    args = message.text.split()[1:]
    if not args:
        await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗨𝘀𝗮𝗴𝗲: /eren {userid/username} [off/remove]")
        return

    target_input = args[0]
    target_id = await asyncio.to_thread(_resolve_user_id_sync, target_input)
    if not target_id:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗨𝘀𝗲𝗿 𝗻𝗼𝘁 𝗳𝗼𝘂𝗻𝗱 𝗶𝗻 𝗱𝗮𝘁𝗮𝗯𝗮𝘀𝗲.")
        return

    action = "grant"
    if len(args) > 1 and args[1].lower() in ("remove", "off", "revoke"):
        action = "revoke"

    if action == "grant":
        success = await asyncio.to_thread(grant_generator_rights, target_id)
        if success:
            await message.reply(
                f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> "
                f"𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 generator rights to user <code>{target_id}</code>.",
                parse_mode="HTML"
            )
        else:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> Error granting generator rights.")
    else:
        success = await asyncio.to_thread(revoke_generator_rights, target_id)
        if success:
            await message.reply(
                f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> "
                f"𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𝗿𝗲𝘃𝗼𝗸𝗲𝗱 generator rights from user <code>{target_id}</code>.",
                parse_mode="HTML"
            )
        else:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> Error revoking generator rights.")
