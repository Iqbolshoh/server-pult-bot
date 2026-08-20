"""Settings from config.json plus secrets from .env."""

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

from .core import CONFIG_PATH, ENV_PATH, UPLOAD_DIR, log

DEFAULT_SYSTEM_PROMPT = (
    "Seni Telegram orqali telefondan boshqarishmoqda. Shuning uchun: "
    "javobing qisqa va aniq bo'lsin (iloji boricha 15 qatordan oshmasin), "
    "o'zbek tilida yoz, keng markdown jadval ishlatma (telefon ekranida buziladi), "
    "uzun kod bloklarini nusxalama. Ish tugagach natijani bir-ikki jumlada xulosa qil "
    "va nima o'zgarganini ayt."
)
DEFAULT_CONFIG = {
    "bot_token": "",
    "allowed_user_ids": [],
    # Claimed by whoever sends this code first, once, while allowed_user_ids is empty.
    "pairing_code": "",
    "workdir": "/var/www",
    # Directories offered by the /projects picker. Globs are expanded.
    "project_globs": ["/var/www/*", "/var/www"],
    # Engine a bare message runs on. "both" fans the task out to all engines.
    "engine": "claude",
    "model": "",
    "agy_bin": "agy",
    "agy_model": "gemini-3.7-flash",
    "agy_print_timeout": "15m",
    # Permission flags for agy. Read from AGY_FLAGS in .env, never hard-coded here,
    # so the operator alone decides how autonomous that engine is.
    "agy_flags": [],
    "local_api_port": 7799,
    # bypassPermissions is refused by the CLI when running as root; "auto" is the
    # most autonomous mode that works here.
    "permission_mode": "auto",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    # When true, every task waits for a button press before anything runs.
    "confirm_before_run": True,
    "job_timeout_sec": 3600,
    # Hard stop for a run that keeps looping. Counted from assistant turns in the
    # stream, because this CLI has no --max-turns flag.
    "max_turns": 60,
    # A --resume chain replays its whole transcript, so it gets more expensive with
    # every message. Drop the context once it is stale or long enough.
    "session_idle_reset_sec": 14400,
    "session_max_jobs": 15,
    "shell_timeout_sec": 60,
    "progress_interval_sec": 15,
    "max_download_mb": 20,
    "notify_on_start": True,
}
def load_env():
    """Secrets and the agy permission policy live in .env, never in config.json.

    Keeping them out of config.json is what makes this directory safe to commit.
    """
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    except OSError:
        pass
    return env
def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = dict(DEFAULT_CONFIG, pairing_code=uuid.uuid4().hex[:10])
        save_config(cfg)
        log(f"created {CONFIG_PATH} -- fill in bot_token")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = dict(DEFAULT_CONFIG, **json.load(f))
    env = load_env()
    if env.get("BOT_TOKEN"):
        cfg["bot_token"] = env["BOT_TOKEN"]
    if env.get("ADMIN_CHAT_ID") and not cfg["allowed_user_ids"]:
        cfg["allowed_user_ids"] = [int(x) for x in env["ADMIN_CHAT_ID"].split(",") if x.strip()]
    if not cfg["agy_flags"] and env.get("AGY_FLAGS"):
        cfg["agy_flags"] = env["AGY_FLAGS"].split()
    if env.get("LOCAL_API_PORT"):
        cfg["local_api_port"] = int(env["LOCAL_API_PORT"])
    if not cfg["bot_token"]:
        log("BOT_TOKEN is missing -- set it in .env")
        sys.exit(1)
    if not cfg["allowed_user_ids"] and not cfg["pairing_code"]:
        log("both allowed_user_ids and pairing_code are empty -- refusing to run open to everyone")
        sys.exit(1)
    return cfg
def save_config(cfg):
    """Persist settings, minus anything that belongs in .env."""
    public = {k: v for k, v in cfg.items() if k not in ("bot_token", "agy_flags")}
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(public, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
CFG = load_config()
API_BASE = f"https://api.telegram.org/bot{CFG['bot_token']}/"
FILE_BASE = f"https://api.telegram.org/file/bot{CFG['bot_token']}/"

os.makedirs(UPLOAD_DIR, exist_ok=True)
LOCAL_API_KEY = uuid.uuid4().hex[:24]
def local_api_hint():
    port = CFG["local_api_port"]
    base = f"http://127.0.0.1:{port}"
    return (
        " Foydalanuvchiga fayl yuborish kerak bo'lsa: "
        f"curl -s '{base}/send-file?key={LOCAL_API_KEY}' -G --data-urlencode "
        "'file=/to/liq/yol'. Oraliq xabar yuborish: "
        f"curl -s '{base}/send-msg?key={LOCAL_API_KEY}' -G --data-urlencode 'text=XABAR'."
    )
