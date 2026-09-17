import asyncio
import platform
import time
from datetime import timedelta
from aiogram import Router, types
from aiogram.filters import Command

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADMIN_IDS = {6962534443, 7549544641, 8428369446}

router = Router()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def _collect_status() -> str:
    if not HAS_PSUTIL:
        return "<tg-emoji emoji-id='6237864166879663987'>❌</tg-emoji> <b>psutil not installed.</b> Run: <code>pip install psutil</code>"

    # ── CPU ──────────────────────────────────────────
    cores_phys  = psutil.cpu_count(logical=False) or "N/A"
    cores_logic = psutil.cpu_count(logical=True)  or "N/A"
    cpu_pct     = psutil.cpu_percent(interval=0.5)
    try:
        freq = psutil.cpu_freq()
        freq_str = f"{freq.current:.0f} MHz" if freq else "N/A"
    except Exception:
        freq_str = "N/A"

    # ── RAM ──────────────────────────────────────────
    ram = psutil.virtual_memory()
    ram_used = _fmt_bytes(ram.used)
    ram_total = _fmt_bytes(ram.total)
    ram_free  = _fmt_bytes(ram.available)
    ram_pct   = ram.percent

    # ── SWAP ─────────────────────────────────────────
    swap = psutil.swap_memory()
    swap_used  = _fmt_bytes(swap.used)
    swap_total = _fmt_bytes(swap.total)
    swap_pct   = swap.percent

    # ── DISK ─────────────────────────────────────────
    disk = psutil.disk_usage("/")
    disk_used  = _fmt_bytes(disk.used)
    disk_total = _fmt_bytes(disk.total)
    disk_free  = _fmt_bytes(disk.free)
    disk_pct   = disk.percent

    # ── NETWORK ──────────────────────────────────────
    net = psutil.net_io_counters()
    net_sent = _fmt_bytes(net.bytes_sent)
    net_recv = _fmt_bytes(net.bytes_recv)

    # ── UPTIME ───────────────────────────────────────
    uptime_sec = int(time.time() - psutil.boot_time())
    uptime_str = str(timedelta(seconds=uptime_sec))

    # ── OS ───────────────────────────────────────────
    os_name = f"{platform.system()} {platform.release()}"

    # ── PROCESSES ────────────────────────────────────
    proc_count = len(psutil.pids())

    return (
        f"<b>VPS STATUS</b>\n"
        f"━━━━━━━━━━━━━━━━\n"

        f"<b>CPU</b>\n"
        f"  <b>Cores</b>     ➛ <b>{cores_phys} physical / {cores_logic} logical</b>\n"
        f"  <b>Freq</b>      ➛ <b>{freq_str}</b>\n"
        f"  <b>Usage</b>     ➛ <b>{cpu_pct:.1f}%</b>\n"
        f"━━━━━━━━━━━━━━━━\n"

        f"<b>RAM</b>\n"
        f"  <b>Used</b>      ➛ <b>{ram_used} / {ram_total}</b>\n"
        f"  <b>Free</b>      ➛ <b>{ram_free}</b>\n"
        f"  <b>Usage</b>     ➛ <b>{ram_pct:.1f}%</b>\n"
        f"━━━━━━━━━━━━━━━━\n"

        f"<b>SWAP</b>\n"
        f"  <b>Used</b>      ➛ <b>{swap_used} / {swap_total}</b>\n"
        f"  <b>Usage</b>     ➛ <b>{swap_pct:.1f}%</b>\n"
        f"━━━━━━━━━━━━━━━━\n"

        f"<b>DISK (/)</b>\n"
        f"  <b>Used</b>      ➛ <b>{disk_used} / {disk_total}</b>\n"
        f"  <b>Free</b>      ➛ <b>{disk_free}</b>\n"
        f"  <b>Usage</b>     ➛ <b>{disk_pct:.1f}%</b>\n"
        f"━━━━━━━━━━━━━━━━\n"

        f"<b>NETWORK</b>\n"
        f"  <b>Sent</b>      ➛ <b>{net_sent}</b>\n"
        f"  <b>Recv</b>      ➛ <b>{net_recv}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"

        f"<b>SYSTEM</b>\n"
        f"  <b>OS</b>        ➛ <b>{os_name}</b>\n"
        f"  <b>Uptime</b>    ➛ <b>{uptime_str}</b>\n"
        f"  <b>Processes</b> ➛ <b>{proc_count}</b>"
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /vps COMMAND
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.message(Command("vps"))
async def vps_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("\u26d4 Admin only.", parse_mode="HTML")
        return

    text = await asyncio.to_thread(_collect_status)
    await message.reply(text, parse_mode="HTML")
