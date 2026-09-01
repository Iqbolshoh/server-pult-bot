"""Settings from config.json plus secrets from .env.

Nothing here exits the process. A missing token or an unwritten config is
reported by config_problems() so the entrypoint can print one clear instruction
and /doctor can show the same list inside Telegram -- and so the whole package
stays importable from a test with no config.json at all.
"""

import json
import os
import uuid

from .core import CONFIG_PATH, ENV_PATH, UPLOAD_DIR, log

DEFAULT_CONFIG = {
    "bot_token": "",
    "allowed_user_ids": [],
    # Claimed by whoever sends this code first, once, while allowed_user_ids is empty.
    "pairing_code": "",
    # Language of everything the operator reads. See locales/.
    "language": "uz",
    "workdir": "/var/www",
    # Directories offered by the /projects picker. Globs are expanded.
    "project_globs": ["/var/www/*", "/var/www"],
    # Engine a bare message runs on. "both" fans the task out to all engines.
    "engine": "claude",
    "model": "",
    "agy_bin": "agy",
    "claude_bin": "claude",
    "agy_model": "gemini-3.7-flash",
    # Permission flags for agy. Read from AGY_FLAGS in .env, never hard-coded here,
    # so the operator alone decides how autonomous that engine is.
    "agy_flags": [],
    # Reasoning effort for both engines, clamped per engine at build time.
    # "" leaves each CLI on its own default.
    "effort": "",
    # Read-only tier: --restricted (claude) / --sandbox (agy).
    "safe_mode": False,
    # Claude's own answer to a context that grew too long. "" disables it.
    "autocompact": "auto",
    # Stream the model's prose into the progress card instead of tool names only.
    "stream_words": True,
    "local_api_port": 7799,
    # bypassPermissions is refused by the CLI when running as root; "auto" is the
    # most autonomous mode that works here.
    "permission_mode": "auto",
    # "" means: use the current locale's default prompt (locales/<lang>.json).
    "system_prompt": "",
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
    # An apt security upgrade makes needrestart bounce supervisor once per
    # batch, so the bot comes up four or five times in a row. Only the first
    # start inside this window announces itself; see maintenance.boot_notice.
    "boot_notice_cooldown_sec": 900,
    # Automatic failover. Off until the operator turns it on, because a hop
    # spends their *other* subscription.
    "fallback_enabled": False,
    "fallback_chain": [
        {"engine": "claude", "model": "opus", "effort": "high"},
        {"engine": "claude", "model": "sonnet", "effort": "high"},
        {"engine": "agy", "model": "gemini-3.7-flash", "effort": "high"},
        {"engine": "agy", "model": "gpt-oss-120b", "effort": "medium"},
    ],
    # How often the live model catalogue is re-read from the engine.
    "catalogue_refresh_sec": 86400,
    # Utilization at which an engine is treated as nearly dry (0..1).
    "limit_warn_utilization": 0.95,
    "onboarded": False,
}
def load_env(path=None):
    """Secrets and the agy permission policy live in .env, never in config.json.

    Keeping them out of config.json is what makes this directory safe to commit.
    """
    env = {}
    try:
        with open(path or ENV_PATH) as f:
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
    """Settings, always. Never raises, never exits -- see config_problems()."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log(f"{CONFIG_PATH} unreadable ({e}) -- falling back to defaults")
    env = load_env()
    if env.get("BOT_TOKEN"):
        cfg["bot_token"] = env["BOT_TOKEN"]
    if env.get("ADMIN_CHAT_ID") and not cfg["allowed_user_ids"]:
        cfg["allowed_user_ids"] = [int(x) for x in env["ADMIN_CHAT_ID"].split(",")
                                   if x.strip().lstrip("-").isdigit()]
    if not cfg["agy_flags"] and env.get("AGY_FLAGS"):
        cfg["agy_flags"] = env["AGY_FLAGS"].split()
    if env.get("LOCAL_API_PORT"):
        cfg["local_api_port"] = int(env["LOCAL_API_PORT"])
    return cfg
def config_problems(cfg=None):
    """Reasons the bot cannot serve anybody yet, worst first. Empty means ready."""
    cfg = CFG if cfg is None else cfg
    problems = []
    if not os.path.exists(CONFIG_PATH):
        problems.append(f"config.json is missing -- run install.sh or start the bot once "
                        f"to create {CONFIG_PATH}")
    if not cfg["bot_token"]:
        problems.append(f"BOT_TOKEN is missing -- set it in {ENV_PATH}")
    if not cfg["allowed_user_ids"] and not cfg["pairing_code"]:
        problems.append("allowed_user_ids and pairing_code are both empty -- refusing to "
                        "run open to everyone; set ADMIN_CHAT_ID in .env")
    return problems
def ensure_pairing_code(cfg=None):
    """Give a fresh install something to be claimed with, and persist it."""
    cfg = CFG if cfg is None else cfg
    if not cfg["allowed_user_ids"] and not cfg["pairing_code"]:
        cfg["pairing_code"] = uuid.uuid4().hex[:10]
        save_config(cfg)
    return cfg["pairing_code"]
def save_config(cfg=None):
    """Persist settings, minus anything that belongs in .env."""
    cfg = CFG if cfg is None else cfg
    public = {k: v for k, v in cfg.items() if k not in ("bot_token", "agy_flags")}
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
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
