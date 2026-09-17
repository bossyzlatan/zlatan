import asyncio
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import random
import string
import logging
import io

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIogram Imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from aiogram import types, F, Router
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

router = Router()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION & IMPORTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    from database import DB_CONFIG, PooledConn
    _USE_POOL = True
except ImportError:
    DB_CONFIG = {"dbname": "your_db", "user": "your_user", "password": "your_pass", "host": "localhost"}
    _USE_POOL = False

ADMIN_IDS = {6962534443, 8761005192, 8428369446}
LOG_CHANNEL_ID = -1004462990283  # @lightXhub1 (Group for chats & logs)

# Regex to detect generated plan keys inside a message (e.g. ZLATAN-ROOT-ABC1234567)
KEY_PATTERN = re.compile(r'ZLATAN-[A-Z]+-[A-Z0-9]+')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATABASE HELPERS (POSTGRESQL)
# All DB functions are synchronous — always call via asyncio.to_thread()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SYNC LOGIC HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _resolve_user_id_sync(target_input):
    """Resolve a target input (ID or username) to a user_id from the DB."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if target_input.isdigit():
            target_id = int(target_input)
            cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (target_id,))
            if cursor.fetchone(): return target_id
            return target_id  # Return anyway for new inserts

        target_username = target_input.lstrip('@')
        cursor.execute("SELECT user_id FROM users WHERE username = %s OR username = %s", (target_username, target_input))
        row = cursor.fetchone()
        if row: return row['user_id']
        return None
    except Exception as e:
        import logging
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
        row = cursor.fetchone()
        return row is not None
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
    return f"ZLATAN-{random_str}-CHK"

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

def _sub_db_sync(target_id, display_name, plan_name, days, credits, amount):
    """Full /sub DB update. Returns receipt_id or raises on error."""
    expiry_date = datetime.now() + timedelta(days=days)
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
    """Add credits to user. Returns new total."""
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
    """Remove premium from user, reset credits to 150. Returns user details dict or None."""
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
    """Fetch receipt + user details. Returns row dict or None."""
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
    """Fetch info row, creating user if missing. Returns dict."""
    get_premium_status(user_id)  # resets expired plans
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
            code = "ZLATAN-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            cursor.execute("INSERT INTO codes (code, credits) VALUES (%s, %s)", (code, 100))
            generated.append(code)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error generating codes: {e}")
    finally:
        conn.close()
    return generated

def _gen_plan_keys_db_sync(plan, plan_name, days, credits, amount):
    generated = []
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for _ in range(amount):
            key = f"ZLATAN-{plan.upper()}-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
            cursor.execute(
                "INSERT INTO plan_keys (key, plan, plan_name, days, credits) VALUES (%s, %s, %s, %s, %s)",
                (key, plan, plan_name, days, 999999999)
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
      ("plan_ok",  (plan_name, days, receipt_id))
      ("error",    None)
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Check plan_keys first
        cursor.execute("SELECT * FROM plan_keys WHERE key = %s", (code,))
        p_row = cursor.fetchone()
        if p_row:
            if p_row['claimed_by'] is not None:
                return "claimed", None

            # First, ensure the user row exists so we can lock it
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, credits, joined_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id, "Unknown", "User", 999999999, datetime.now()))

            # Now, acquire a lock on the user row to prevent race conditions
            cursor.execute("SELECT is_premium, premium_expiry FROM users WHERE user_id = %s", (user_id,))
            u_row = cursor.fetchone()

            if u_row and u_row['is_premium'] == 1 and u_row['premium_expiry'] and datetime.now() < u_row['premium_expiry']:
                return "premium", None

            cursor.execute("UPDATE users SET first_name = %s WHERE user_id = %s", (display_name, user_id))

            expiry_date = datetime.now() + timedelta(days=p_row['days'])
            receipt_id = generate_receipt_id()
            purchased_on = datetime.now()

            cursor.execute(
                "UPDATE users SET is_premium = 1, premium_expiry = %s, credits = 999999999 WHERE user_id = %s",
                (expiry_date, user_id)
            )
            cursor.execute(
                "INSERT INTO receipts (receipt_id, user_id, plan, amount_paid, purchased_on, expires_on) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (receipt_id, user_id, p_row['plan_name'], 0.0, purchased_on, expiry_date)
            )
            cursor.execute(
                "UPDATE plan_keys SET claimed_by = %s, claimed_at = %s WHERE key = %s",
                (user_id, datetime.now(), code)
            )
            conn.commit()
            return "plan_ok", (p_row['plan_name'], p_row['days'], receipt_id)

        # Then check regular codes (disabled)
        return "invalid", None
    except Exception as e:
        conn.rollback()
        logging.error(f"Error claiming code/key: {e}")
        return "error", None
    finally:
        conn.close()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /buy — replies directly to the user's message
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
        InlineKeyboardButton(text='Contact Admin', url="https://t.me/Salluuxx", style="primary")
    ]
])

@router.message(F.text.startswith("/buy"))
async def buy_command(message: types.Message):
    """Replies directly to the user's /buy command with the pricing chart."""
    await message.reply(text=_BUY_TEXT, parse_mode="HTML", reply_markup=_BUY_KB)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /eren (Admin grant)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

    # Unlimited Plan variables: 99999 days premium, 999,999,999 credits
    plan_name = "Superuser"
    days = 99999  # Max Plan Uptime
    credits = 999999999
    MAX_VALID_CARDS = 100000000
    amount = 0.0

    try:
        receipt_id = await asyncio.to_thread(_sub_db_sync, target_id, display_name, plan_name, days, credits, amount)
    except Exception as e:
        logging.error(f"Error executing /eren admin command: {e}")
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗘𝗿𝗿𝗼𝗿.")
        return

    masked_id = mask_receipt_id(receipt_id)

    caption = (
        f"𝐂𝐨𝐧𝐠𝐫𝐚𝐭𝐮𝐥𝐚𝐭𝐢𝐨𝐧𝐬!🎉 𝐲𝐨𝐮𝐫 𝐚𝐜𝐜𝐞𝐬𝐬 𝐡𝐚𝐬 𝐛𝐞𝐞𝐧 𝐚𝐜𝐭𝐢𝐯𝐚𝐭𝐞𝐝.\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ DARKANON <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> (UNLIMITED)\n"
        f"𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛ {days} Days\n"
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> <b>𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛</b> <code>{receipt_id}</code>\n"
        f"𝗽𝗹𝗲𝗮𝘀𝗲 𝘀𝗮𝘃𝗲 𝘁𝗵𝗶𝘀 𝗿𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗."
    )
    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗦𝘂𝗽𝗽𝗼𝗿𝘁", url="https://t.me/darkanonp", style="primary")]
    ])

    try:
        await message.bot.send_message(chat_id=target_id, text=caption, parse_mode="HTML", reply_markup=support_kb)
    except Exception as e:
        logging.error(f"Could not DM user {target_id}: {e}")

    await message.reply(f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗨𝗻𝗹𝗶𝗺𝗶𝘁𝗲𝗱 𝗮𝗰𝗰𝗲𝘀𝘀 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 𝘁𝗼 {user_link}.")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /sub
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/sub"))
async def sub_command(message: types.Message):
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.reply("<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀.")
        return

    args = message.text.split()[1:]
    if len(args) < 2:
        await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗨𝘀𝗮𝗴𝗲: /sub {user_id/username} {plan}\nPlans: core, elite, root\nExample: /sub 123456 elite")
        return

    target_input, plan = args[0], args[1].lower()

    plan_map = {
        "core":  ("𝗖𝗼𝗿𝗲 <tg-emoji emoji-id='5042274086332400375'>🛠️</tg-emoji>",  7,  999999999, 5),
        "elite": ("𝗘𝗹𝗶𝘁𝗲 ⭐", 15, 999999999, 7),
        "root":  ("𝗥𝗼𝗼𝘁 <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>",  30, 999999999, 15),
    }
    if plan not in plan_map:
        await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗽𝗹𝗮𝗻. 𝗣𝗹𝗮𝗻𝘀: <b>Core</b>, <b>Elite</b>, <b>Root</b>", parse_mode="HTML")
        return

    # Resolve target ID in thread
    target_id = await asyncio.to_thread(_resolve_user_id_sync, target_input)
    if not target_id:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗖𝗼𝘂𝗹𝗱 𝗻𝗼𝘁 𝗳𝗶𝗻𝗱 𝘂𝘀𝗲𝗿 𝗶𝗻 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲.")
        return

    plan_name, days, credits, amount = plan_map[plan]

    # Fetch display name from Telegram API (non-blocking)
    display_name = "User"
    try:
        chat = await message.bot.get_chat(target_id)
        display_name = chat.first_name or chat.username or "User"
    except Exception:
        pass

    user_link = f'<a href="tg://user?id={target_id}">{display_name}</a>'

    # DB update in thread
    try:
        receipt_id = await asyncio.to_thread(_sub_db_sync, target_id, display_name, plan_name, days, credits, amount)
    except Exception as e:
        logging.error(f"Error updating subscription: {e}")
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗘𝗿𝗿𝗼𝗿.")
        return

    masked_id = mask_receipt_id(receipt_id)

    caption = (
        f"𝐂𝐨𝐧𝐠𝐫𝐚𝐭𝐮𝐥𝐚𝐭𝐢𝐨𝐧𝐬!🎉 𝐲𝐨𝐮𝐫 𝐚𝐜𝐜𝐞𝐬𝐬 𝐡𝐚𝐬 𝐛𝐞𝐞𝐧 𝐚𝐜𝐭𝐢𝐯𝐚𝐭𝐞𝐝.\n"
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {user_link}\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ {plan_name}\n"
        f"𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛ {days} Days\n"
        f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛ <code>{receipt_id}</code>\n"
        f"𝗽𝗹𝗲𝗮𝘀𝗲 𝘀𝗮𝘃𝗲 𝘁𝗵𝗶𝘀 𝗿𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗."
    )
    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗦𝘂𝗽𝗽𝗼𝗿𝘁", url="https://t.me/darkanonp", style="primary")]
    ])
    log_text = (
        f"<b>🛒 𝗡𝗘𝗪 𝗣𝗟𝗔𝗡 𝗣𝗨𝗥𝗖𝗛𝗔𝗦𝗘𝗗</b>\n"
        f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛</b> {user_link}\n"
        f"<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛</b> {plan_name}\n"
        f"<b><tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> 𝗔𝗺𝗼𝘂𝗻𝘁 ➛</b> <b>{amount} USD</b>\n"

        f"<b><tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛</b> <code>{masked_id}</code>"
    )
    log_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝗕𝘂𝘆 𝗡𝗼𝘄", callback_data="show_buy_plans")]
    ])

    # Send DM to user + log to channel + admin confirm — all concurrently
    async def _dm_user():
        try:
            await message.bot.send_message(chat_id=target_id, text=caption, parse_mode="HTML", reply_markup=support_kb)
        except Exception as e:
            logging.error(f"Could not DM user {target_id}: {e}")

    await _dm_user()
    try:
        await message.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text, parse_mode="HTML", reply_markup=log_kb)
    except Exception:
        pass
    await message.reply(f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 𝘁𝗼 <code>{target_id}</code> 𝗳𝗼𝗿 <b>{days}</b> 𝗱𝗮𝘆𝘀.")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /adcr
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/adcr"))
async def adcr_command(message: types.Message):
    await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗱𝗶𝘀𝗮𝗯𝗹𝗲𝗱.")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /rsub
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
        await message.reply(f"<tg-emoji emoji-id='5039844895779455925'>🍾</tg-emoji> 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗿𝗲𝗺𝗼𝘃𝗲𝗱 𝗳𝗿𝗼𝗺 <code>{target_id}</code> 𝗮𝗻𝗱 𝗰𝗿𝗲𝗱𝗶𝘁𝘀 𝗿𝗲𝘀𝗲𝘁 𝘁𝗼 <b>𝟭𝟱𝟬</b>.")
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

    async def _dm_user():
        try:
            await message.bot.send_message(chat_id=target_id, text=dm_text, parse_mode="HTML", reply_markup=buy_kb)
        except Exception as e:
            logging.error(f"Could not DM user {target_id}: {e}")

    await _dm_user()
    await message.reply(f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗿𝗲𝗺𝗼𝘃𝗲𝗱 𝗳𝗿𝗼𝗺 <code>{target_id}</code> 𝗮𝗻𝗱 𝗰𝗿𝗲𝗱𝗶𝘁𝘀 𝗿𝗲𝘀𝗲𝘁 𝘁𝗼 <b>𝟭𝟱𝟬</b>.")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /rc
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
    expires_str = row['expires_on'].strftime('%Y-%m-%d') if row.get('expires_on') else "N/A"
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
        [InlineKeyboardButton(text="ZLATAN", url="https://t.me/zlatanchecker_bot", style="primary")]
    ])
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /info
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
            ending_str = u_row['premium_expiry'].strftime('%Y-%m-%d')
    elif u_row['is_premium'] == 1:
        access_str = "𝗣𝗿𝗲𝗺𝗶𝘂𝗺"

    joined_disp  = u_row['joined_at'].strftime('%Y-%m-%d') if u_row.get('joined_at') else "N/A"

    text = (
        f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ {username_link}\n"
        f"𝗨𝘀𝗲𝗿 𝗜𝗗 ➛ <code>{user_id}</code>\n"
        f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ <b>{access_str}</b>\n"
        f"𝗣𝘂𝗿𝗰𝗵𝗮𝘀𝗲𝗱 𝗼𝗻 ➛ <b>{purchased_str}</b>\n"
        f"𝗘𝗻𝗱𝗶𝗻𝗴 𝗼𝗻 ➛ <b>{ending_str}</b>\n"
        f"𝗝𝗼𝗶𝗻𝗲𝗱 ➛ <b>{joined_disp}</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ZLATAN", url="https://t.me/zlatanchecker_bot", style="primary")]
    ])
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /suball
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
        expiry_date = row['premium_expiry'].strftime('%Y-%m-%d') if row['premium_expiry'] else "N/A"
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /g_code
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/g_code"))
async def g_code_command(message: types.Message):
    await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗱𝗶𝘀𝗮𝗯𝗹𝗲𝗱.")

CLAIM_LOCKS = {}

@router.message(F.text.startswith("/claim"))
async def claim_command(message: types.Message):
    user = message.from_user
    user_id = user.id

    async with CLAIM_LOCKS.setdefault(user_id, asyncio.Lock()):
        display_name = user.first_name or user.username or "User"

        args = message.text.split()[1:]
        codes_to_try = []

        # Determine the list of codes to try
        if args:
            # User passed a code explicitly
            codes_to_try = [args[0].upper()]
        elif message.reply_to_message:
            # Extract keys from the replied-to message
            reply_text = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            )
            # Find all key patterns (dedupe while preserving order)
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

        # Try each code sequentially until one succeeds
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
                # User already has an active plan — stop immediately
                await message.reply(
                    "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> "
                    "𝗨𝘀𝗲𝗿𝘀 𝘄𝗶𝘁𝗵 𝗮𝗻 𝗮𝗰𝘁𝗶𝘃𝗲 𝗽𝗹𝗮𝗻 𝗰𝗮𝗻𝗻𝗼𝘁 𝗿𝗲𝗱𝗲𝗲𝗺 𝗸𝗲𝘆𝘀."
                )
                return
            else:
                # "claimed" / "invalid" / "error" — move to next code
                skipped_count += 1
                continue

        # Handle the outcome
        if final_status == "plan_ok":
            plan_name, days, receipt_id = final_data
            caption = (
                f"𝐂𝐨𝐧𝐠𝐫𝐚𝐭𝐮𝐥𝐚𝐭𝐢𝐨𝐧𝐬!🎉 𝐲𝐨𝐮𝐫 𝐚𝐜𝐜𝐞𝐬𝘀 𝐡𝐚𝐬 𝐛𝐞𝐞𝐧 𝐚𝐜𝐭𝐢𝐯𝐚𝐭𝐞𝐝.\n"
                f"<tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛ "
                f"<a href=\"tg://user?id={user_id}\">{display_name}</a>\n"
                f"<tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛ {plan_name}\n"
                f"𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 ➛ {days} Days\n"
                f"<tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗶𝗱 ➛ "
                f"<code>{receipt_id}</code>\n"
                f"𝗽𝗹𝗲𝗮𝘀𝗲 𝘀𝗮𝘃𝗲 𝘁𝗵𝗶𝘀 𝗿𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗."
            )
            support_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="𝗦𝘂𝗽𝗽𝗼𝗿𝘁", url="https://t.me/darkanonp", style="primary")]
            ])
            await message.reply(caption, parse_mode="HTML", reply_markup=support_kb)

            # Log to channel
            log_text = (
                f"<b>🛒 𝗡𝗘𝗪 𝗣𝗟𝗔𝗡 𝗖𝗟𝗔𝗜𝗠𝗘𝗗 𝗩𝗜𝗔 𝗞𝗘𝗬</b>\n"
                f"<b><tg-emoji emoji-id='6237927637906364256'>👤</tg-emoji> 𝗨𝘀𝗲𝗿 ➛</b> "
                f"<a href=\"tg://user?id={user_id}\">{display_name}</a>\n"
                f"<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗔𝗰𝗰𝗲𝘀𝘀 ➛</b> {plan_name}\n"
                f"<b><tg-emoji emoji-id='5042050649248760772'>💎</tg-emoji> 𝗥𝗲𝗰𝗲𝗶𝗽𝘁 𝗜𝗗 ➛</b> "
                f"<code>{mask_receipt_id(receipt_id)}</code>"
            )
            try:
                await message.bot.send_message(
                    chat_id=LOG_CHANNEL_ID, text=log_text, parse_mode="HTML"
                )
            except Exception:
                pass

        elif skipped_count > 0:
            await message.reply(
                "<tg-emoji emoji-id='4915853119839011973'>⚠️</tg-emoji> "
                "𝗔𝗹𝗹 𝗸𝗲𝘆𝘀 𝗶𝗻 𝘁𝗵𝗶𝘀 𝗺𝗲𝘀𝘀𝗮𝗴𝗲 𝗵𝗮𝘃𝗲 𝗮𝗹𝗿𝗲𝗮𝗱𝘆 𝗯𝗲𝗲𝗻 "
                "𝗰𝗹𝗮𝗶𝗺𝗲𝗱 𝗼𝗿 𝗮𝗿𝗲 𝗶𝗻𝘃𝗮𝗹𝗶𝗱."
            )
        else:
            await message.reply(
                "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗖𝗼𝗱𝗲."
            )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /gen
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.text.startswith("/gen"))
async def gen_command(message: types.Message):
    user = message.from_user
    if not await asyncio.to_thread(is_authorized_generator, user.id):
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.")
        return

    args = message.text.split()[1:]
    if len(args) < 2:
        await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗨𝘀𝗮𝗴𝗲: /gen {plan} {quantity}\nPlans: core, elite, root")
        return

    plan, quantity_str = args[0].lower(), args[1]
    plan_map = {
        "trial": ("trail <tg-emoji emoji-id='5042274086332400375'>🛠️</tg-emoji>",  1,  999999999),
        "core":  ("𝗖𝗼𝗿𝗲 <tg-emoji emoji-id='5042274086332400375'>🛠️</tg-emoji>",  7,  999999999),
        "elite": ("𝗘𝗹𝗶𝘁𝗲 ⭐", 15, 999999999),
        "root":  ("𝗥𝗼𝗼𝘁 <tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji>",  30, 999999999),
    }

    if plan not in plan_map:
        await message.reply("<tg-emoji emoji-id='5040030395416969985'>🚫</tg-emoji> 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗽𝗹𝗮𝗻. 𝗣𝗹𝗮𝗻𝘀: <b>Core</b>, <b>Elite</b>, <b>Root</b>", parse_mode="HTML")
        return

    try:
        amount = int(quantity_str)
        if amount <= 0 or amount > 50:
            raise ValueError()
    except ValueError:
        await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗾𝘂𝗮𝗻𝘁𝗶𝘁𝘆 (max 50).")
        return

    plan_name, days, credits = plan_map[plan]
    generated_keys = await asyncio.to_thread(_gen_plan_keys_db_sync, plan, plan_name, days, credits, amount)

    formatted_keys = "\n".join([f"<code>{k}</code>" for k in generated_keys])
    response = (
        f"<b><tg-emoji emoji-id='5039727497143387500'>👑</tg-emoji> 𝗚𝗲𝗻𝗲𝗿𝗮𝘁𝗲𝗱 𝗞𝗲𝘆𝘀 𝗳𝗼𝗿 {plan_name}</b>\n"
        f"<b>𝗤𝘂𝗮𝗻𝘁𝗶𝘁𝘆:</b> {amount}\n"
        f"<b>𝗗𝗮𝘆𝘀:</b> {days}\n\n"
        f"{formatted_keys}\n\n"
        f"𝗨𝘀𝗲 /𝗰𝗹𝗮𝗶𝗺 𝘁𝗼 𝗿𝗲𝗱𝗲𝗲𝗺."
    )
    await message.reply(response, parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND: /eren (generator rights grant/revoke)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
            await message.reply(f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 generator rights to user <code>{target_id}</code>.", parse_mode="HTML")
        else:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> Error granting generator rights.")
    else:
        success = await asyncio.to_thread(revoke_generator_rights, target_id)
        if success:
            await message.reply(f"<tg-emoji emoji-id='5341715473882955310'>✅</tg-emoji> 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𝗿𝗲𝘃𝗼𝗸𝗲𝗱 generator rights from user <code>{target_id}</code>.", parse_mode="HTML")
        else:
            await message.reply("<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> Error revoking generator rights.")
