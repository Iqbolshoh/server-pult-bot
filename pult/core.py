"""Paths, shared runtime state and small formatting helpers."""

import glob
import html
import http.server
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


# core.py lives inside the package, so the project root is one level up.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "state.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
AUDIT_PATH = os.path.join(BASE_DIR, "audit.log")
TELEGRAM_MAX_CHARS = 3800
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
ENV_PATH = os.path.join(BASE_DIR, ".env")
def h(text):
    return html.escape(str(text))
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
RUNNING = {}                       # engine -> {"proc": Popen, "job_id": int}
run_lock = threading.Lock()
def running_jobs():
    with run_lock:
        return {eng: info["job_id"] for eng, info in RUNNING.items()}
START_TIME = time.time()
