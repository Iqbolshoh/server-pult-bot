"""Paths, shared runtime state and small formatting helpers."""

import html
import os
import subprocess
import threading
import time


# The package sits inside the source tree. State (config, database, uploads,
# audit log) normally lives beside it, but SERVER_PULT_HOME moves it elsewhere:
# that is what lets a test point the whole package at a temp directory, and what
# lets the installer keep state out of a git checkout.
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(PACKAGE_DIR)
BASE_DIR = os.path.abspath(os.environ.get("SERVER_PULT_HOME") or SOURCE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "state.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
AUDIT_PATH = os.path.join(BASE_DIR, "audit.log")
ENV_PATH = os.path.join(BASE_DIR, ".env")
# Translations ship with the code, never with the state directory.
LOCALES_DIR = os.path.join(SOURCE_DIR, "locales")
TELEGRAM_MAX_CHARS = 3800
# One visual language for every card the bot sends. Telegram's HTML is narrow --
# bold, italic, code, pre and blockquote -- so structure has to come from rules,
# quoted bodies and a consistent order of lines.
RULE = "━━━━━━━━━━━━━━━━"
THIN_RULE = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
POLL_TIMEOUT = 50
INLINE_RESULT_LIMIT = 3400  # longer results are delivered as a .md file
PREVIEW_CHARS = 700
MEDIA_GROUP_WINDOW = 90  # seconds an album stays open for extra photos
shutdown = threading.Event()
outbox_ready = threading.Event()
def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
def audit(line):
    """Append-only record of every instruction the bot was given."""
    try:
        with open(AUDIT_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{line}\n")
    except OSError:
        pass
def h(text):
    return html.escape(str(text))
def quote(escaped_text, expandable=False):
    """Wrap already-escaped text in a Telegram blockquote.

    A quoted body is what separates the model's answer from the bot's own words
    at a glance; expandable keeps a long one from swallowing the screen.
    """
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{escaped_text}</blockquote>"
def card(*parts):
    """Join the lines of a card. None drops a line; "" keeps a blank one."""
    return "\n".join(part for part in parts if part is not None)
def kv(icon, label, value):
    """One 'icon label: value' row, the same shape on every screen."""
    return f"{icon} {label}: <b>{value}</b>"
def fmt_tokens(count):
    """1234 -> '1.2K', 4500000 -> '4.5M'. Limits are counted in tokens, not money."""
    count = int(count or 0)
    if count >= 999_500:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1_000:
        return f"{count / 1_000:.1f}K".replace(".0K", "K")
    return str(count)
def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
def fmt_clock(unix_ts):
    """Local HH:MM for a reset timestamp, or an empty string when there is none."""
    if not unix_ts:
        return ""
    return time.strftime("%H:%M", time.localtime(float(unix_ts)))
def fmt_when(unix_ts):
    """HH:MM for today, DD.MM HH:MM for anything further out."""
    if not unix_ts:
        return ""
    when = time.localtime(float(unix_ts))
    if time.strftime("%Y%m%d", when) == time.strftime("%Y%m%d"):
        return time.strftime("%H:%M", when)
    return time.strftime("%d.%m %H:%M", when)
def bar(fraction, width=10):
    """Text gauge for a 0..1 utilization value."""
    fraction = min(1.0, max(0.0, float(fraction or 0)))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)
def signal_group(proc, sig):
    """Signal a child's whole process group, not just the child.

    A bare proc.kill() reaches the CLI and nothing else: a build, an install or a
    dev server the agent started keeps running, holding the working tree and the
    port -- and a grandchild that still holds the stdout pipe can hang the parent
    forever. Every child in this bot is spawned with start_new_session=True so it
    leads its own group and this call can reach all of it.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass
RUNNING = {}                       # engine -> {"proc": Popen, "job_id": int}
run_lock = threading.Lock()
def running_jobs():
    with run_lock:
        return {eng: info["job_id"] for eng, info in RUNNING.items()}
START_TIME = time.time()
def version():
    """Short git revision of the checkout, for /start and /update."""
    try:
        out = subprocess.run(["git", "-C", SOURCE_DIR, "describe", "--always", "--dirty"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"
