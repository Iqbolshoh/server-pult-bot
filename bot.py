#!/usr/bin/env python3
"""Vexa Pult -- one Telegram bridge for two AI coding engines.

Runs as a systemd service. Three independent threads:

  poller  -- long-polls Telegram getUpdates, turns messages into jobs
  worker  -- executes jobs with the `claude` CLI, one at a time
  sender  -- drains the outbox to Telegram, retrying until it gets through

Job execution is fully decoupled from Telegram connectivity: once a command is
stored, it runs to completion even if the phone loses signal, and the result
waits in the outbox until delivery succeeds.

Standard library only -- no pip dependencies.
"""

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
# Set whenever something lands in the outbox, so the sender reacts at once
# instead of waiting out its poll interval.
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


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

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


ENV_PATH = os.path.join(BASE_DIR, ".env")


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


# --------------------------------------------------------------------------
# Engines
#
# Two CLIs, one bot. Each engine knows how to build its argv and how to turn its
# own stream-json dialect into the same small set of normalised events, so the
# worker below is engine-agnostic and both can run at the same time.
# --------------------------------------------------------------------------


def _claude_build(prompt, session_id, mode, system_prompt):
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    cmd += ["--resume", session_id] if session_id else ["--session-id", str(uuid.uuid4())]
    if CFG["model"]:
        cmd += ["--model", CFG["model"]]
    permission_mode = "plan" if mode == "plan" else CFG["permission_mode"]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    return cmd, prompt + "\n"


def _claude_events(event):
    out = []
    if event.get("session_id"):
        out.append(("session", event["session_id"]))
    etype = event.get("type")
    if etype == "assistant":
        out.append(("turn", None))
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                out.append(("tool", (block.get("name", "tool"), block.get("input") or {})))
    elif etype == "result":
        out.append(("result", {
            "text": event.get("result") or "",
            "cost": event.get("total_cost_usd"),
            "turns": event.get("num_turns"),
            # input + output + both cache buckets; nested dicts are skipped.
            "tokens": sum(v for v in (event.get("usage") or {}).values()
                          if isinstance(v, int)),
            "error": bool(event.get("is_error")),
        }))
    return out


def _agy_build(prompt, session_id, mode, system_prompt):
    model = CFG["agy_model"]
    cmd = [CFG["agy_bin"], "--model", model]
    effort = agy_model_info(model)["effort"]
    if effort:
        cmd += ["--effort", effort]
    if session_id:
        cmd += ["--conversation", session_id]
    cmd += list(CFG["agy_flags"])
    cmd += ["--output-format", "stream-json", "--print-timeout", CFG["agy_print_timeout"]]
    # agy takes the prompt as an argument, not on stdin.
    cmd += ["-p", (system_prompt + "\n\n" if system_prompt else "") + prompt]
    return cmd, None


def _agy_events(event):
    out = []
    name = event.get("event")
    if name == "step_update":
        su = event.get("step_update") or {}
        if su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
            out.append(("turn", None))
            out.append(("tool", (su.get("tool_name", "tool"),
                                 (su.get("tool_info") or {}).get("parameters") or {})))
    elif name == "result":
        r = event.get("result") or {}
        if r.get("conversation_id"):
            out.append(("session", r["conversation_id"]))
        status = r.get("status")
        text = (r.get("response") or "").strip()
        if status and status != "SUCCESS":
            text = f"[{status}] " + (r.get("error") or text)
        out.append(("result", {
            "text": text,
            "cost": None,
            "turns": r.get("num_turns"),
            "tokens": (r.get("usage") or {}).get("total_tokens"),
            "error": bool(status and status != "SUCCESS"),
        }))
    return out


# effort=None means the model rejects --effort (the Claude models on agy do).
AGY_MODELS = [
    ("gemini-3.7-flash", "⚡ Gemini 3.7 Flash", "tez va aqlli (odatiy)", "high"),
    ("gemini-3.6-flash", "🔥 Gemini 3.6 Flash", "oldingi avlod, tez", "high"),
    ("gemini-3.5-flash", "🌤 Gemini 3.5 Flash", "eng yengil va arzon", "medium"),
    ("gemini-3.1-pro", "🧠 Gemini 3.1 Pro", "kuchli Gemini", "high"),
    ("claude-sonnet-4-6", "🤖 Claude Sonnet 4.6", "Anthropic thinking", None),
    ("claude-opus-4-6-thinking", "🦾 Claude Opus 4.6", "Anthropic opus thinking", None),
    ("gpt-oss-120b", "🟢 GPT-OSS 120B", "ochiq kodli 120B", "medium"),
]

CLAUDE_MODELS_LIST = [
    ("sonnet", "⚡ Sonnet", "tez va tejamli — kunlik ish uchun", None),
    ("opus", "🦾 Opus", "eng kuchli, eng ko\'p limit yeydi", None),
    ("haiku", "🌤 Haiku", "eng yengil va arzon", None),
    ("", "🎛 Odatiy", "config.json dagi standart", None),
]


def agy_model_info(model_id):
    for mid, label, desc, effort in AGY_MODELS:
        if mid == model_id:
            return {"id": mid, "label": label, "desc": desc, "effort": effort}
    return {"id": model_id, "label": model_id, "desc": "?", "effort": None}


ENGINES = {
    "claude": {
        "label": "🤖 Claude",
        "build": _claude_build,
        "events": _claude_events,
        "models": CLAUDE_MODELS_LIST,
        "model_key": "model",
        "plan_hint": (" Hozir REJA rejimidasan: hech narsani o\'zgartirma, faqat nima "
                      "qilishingni qisqa qadamlar bilan tushuntir va xavfli joylarni ayt."),
    },
    "agy": {
        "label": "🛸 Antigravity",
        "build": _agy_build,
        "events": _agy_events,
        "models": AGY_MODELS,
        "model_key": "agy_model",
        "plan_hint": (" REJA REJIMI: hech narsani o\'zgartirma, fayl yozma, buyruq bajarma. "
                      "Faqat nima qilishingni qisqa qadamlar bilan tushuntir."),
    },
}

ENGINE_ORDER = ["claude", "agy"]


def engine_label(engine):
    return ENGINES.get(engine, {}).get("label", engine)


def engine_model(engine):
    return CFG[ENGINES[engine]["model_key"]]


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id   INTEGER NOT NULL,
        prompt    TEXT NOT NULL,
        state     TEXT NOT NULL,          -- pending | queued | running | done | failed | cancelled
        created   REAL NOT NULL,
        started   REAL,
        finished  REAL,
        result    TEXT,
        exit_code INTEGER
    );
    CREATE TABLE IF NOT EXISTS outbox (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id  INTEGER NOT NULL,
        text     TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created  REAL NOT NULL
    );
    """
)
db.commit()


def ensure_columns(table, columns):
    """Additive schema migration -- keeps an older state.db usable."""
    with db_lock:
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log(f"migrated: {table}.{name}")
        db.commit()


ensure_columns("jobs", {"project": "TEXT", "cost": "REAL", "turns": "INTEGER", "mode": "TEXT",
                        "engine": "TEXT NOT NULL DEFAULT 'claude'", "tokens": "INTEGER"})
ensure_columns(
    "outbox",
    {
        "kind": "TEXT NOT NULL DEFAULT 'text'",  # text | doc
        "parse_mode": "TEXT",
        "markup": "TEXT",
        "file_path": "TEXT",
    },
)


def meta_get(key, default=None):
    with db_lock:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(key, value):
    with db_lock:
        db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        db.commit()


def send(chat_id, text, markup=None, parse_mode=None):
    """Queue a message for delivery. Never blocks on the network."""
    chunks = split_message(text)
    with db_lock:
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1  # buttons belong on the final chunk only
            db.execute(
                "INSERT INTO outbox(chat_id,text,created,kind,parse_mode,markup) "
                "VALUES(?,?,?,'text',?,?)",
                (chat_id, chunk, time.time(), parse_mode,
                 json.dumps(markup) if (markup and last) else None),
            )
        db.commit()
    outbox_ready.set()


def send_document(chat_id, file_path, caption=""):
    with db_lock:
        db.execute(
            "INSERT INTO outbox(chat_id,text,created,kind,file_path) VALUES(?,?,?,'doc',?)",
            (chat_id, caption[:1000], time.time(), file_path),
        )
        db.commit()
    outbox_ready.set()


def split_message(text):
    text = text if text.strip() else "(bo'sh javob)"
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MAX_CHARS:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, TELEGRAM_MAX_CHARS)
        if cut < TELEGRAM_MAX_CHARS // 2:
            cut = TELEGRAM_MAX_CHARS
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# --------------------------------------------------------------------------
# Telegram API
# --------------------------------------------------------------------------


class TelegramError(RuntimeError):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"HTTP {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


def _request(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        raise TelegramError(
            e.code,
            body.get("description", str(e)),
            (body.get("parameters") or {}).get("retry_after"),
        ) from None
    if not payload.get("ok"):
        raise TelegramError(0, str(payload))
    return payload["result"]


def api_call(method, params=None, timeout=30):
    """Call the Telegram API. Raises TelegramError on failure."""
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + method, data=data, headers={"Content-Type": "application/json"}
    )
    return _request(req, timeout)


def api_try(method, params=None, timeout=20):
    """Best-effort call: returns None instead of raising. For live UI updates."""
    try:
        return api_call(method, params, timeout)
    except Exception:
        return None


def api_upload(method, fields, file_field, filename, blob, timeout=180):
    """multipart/form-data upload, built by hand to stay dependency-free."""
    boundary = "----claudetg" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        ).encode("utf-8")
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    body += blob
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        API_BASE + method,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return _request(req, timeout)


def download_telegram_file(file_id, suggested_name):
    """Pull a photo/document the user sent into the uploads directory."""
    info = api_call("getFile", {"file_id": file_id})
    size = info.get("file_size") or 0
    if size > CFG["max_download_mb"] * 1024 * 1024:
        raise RuntimeError(f"fayl juda katta ({size // 1024 // 1024} MB)")
    remote = info["file_path"]
    ext = os.path.splitext(remote)[1] or os.path.splitext(suggested_name)[1] or ".bin"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(suggested_name)[0])[:40] or "file"
    local = os.path.join(UPLOAD_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}{ext}")
    with urllib.request.urlopen(FILE_BASE + remote, timeout=120) as resp, open(local, "wb") as out:
        shutil.copyfileobj(resp, out)
    return local


# --------------------------------------------------------------------------
# Inline keyboards
# --------------------------------------------------------------------------


# Both bots show the same bottom keyboard and route the same labels, so muscle
# memory carries over between them. Keep this table in sync with server.js.
LABEL_COMMANDS = {
    "📊 Holat": "/status",
    "🖥 Server": "/server",
    "📋 Ishlar": "/jobs",
    "🗂 Loyiha": "/projects",
    "🤖 Model": "/model",
    "📈 Limit": "/limit",
    "🧩 Dvigatel": "/engine",
    "🆕 Yangi suhbat": "/new",
    "⏹ To'xtat": "/stop",
    "⚙️ Sozlama": "/settings",
    "❓ Yordam": "/help",
}


def main_reply_kb():
    """Persistent bottom keyboard -- same layout as the Antigravity bot."""
    return {
        "keyboard": [
            [{"text": "📊 Holat"}, {"text": "🖥 Server"}, {"text": "📋 Ishlar"}],
            [{"text": "🗂 Loyiha"}, {"text": "🤖 Model"}, {"text": "📈 Limit"}],
            [{"text": "🧩 Dvigatel"}, {"text": "🆕 Yangi suhbat"}, {"text": "⏹ To'xtat"}],
            [{"text": "⚙️ Sozlama"}, {"text": "❓ Yordam"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def kb(*rows):
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }


def main_menu():
    return kb(
        [("📊 Holat", "status"), ("🖥 Server", "server")],
        [("🗂 Loyihalar", "projects"), ("📋 Ishlar", "jobs")],
        [("🧩 Dvigatel", "engine"), ("🤖 Model", "model")],
        [("📈 Limit", "limit"), ("🆕 Yangi suhbat", "new")],
        [("⚙️ Sozlama", "settings")],
        [("❓ Yordam", "help")],
    )


def back_menu():
    return kb([("⬅️ Menyu", "menu")])


def job_menu(job_id, engine="claude"):
    return kb([("⏹ To'xtatish", f"cancel:{job_id}"), ("📊 Holat", "status")])


def confirm_menu(job_id):
    """Shown before anything runs, when confirm_before_run is on."""
    return kb(
        [("▶️ Bajar", f"run:{job_id}"), ("🧭 Avval reja", f"plan:{job_id}")],
        [("❌ Bekor", f"drop:{job_id}")],
    )


def result_menu(job_id, full=False, planned=False):
    rows = []
    if planned:
        rows.append([("✅ Rejani bajar", f"exec:{job_id}")])
    row = [("🔁 Qayta", f"again:{job_id}"), ("🆕 Yangi suhbat", "new"),
           ("⬅️ Menyu", "menu")]
    if full:
        row.insert(0, ("📄 To'liq matn", f"full:{job_id}"))
    rows.append(row)
    return kb(*rows)


def engine_choice_label():
    return "🤝 Ikkalasi" if CFG["engine"] == "both" else engine_label(CFG["engine"])


def engine_model_label(engine):
    if engine == "agy":
        return agy_model_info(CFG["agy_model"])["label"]
    return CFG["model"] or "odatiy"


def engine_menu():
    rows = []
    for eng in ENGINE_ORDER:
        mark = "✅ " if CFG["engine"] == eng else ""
        rows.append([(f"{mark}{ENGINES[eng]['label']}", f"setengine:{eng}")])
    mark = "✅ " if CFG["engine"] == "both" else ""
    rows.append([(f"{mark}🤝 Ikkalasi (parallel)", "setengine:both")])
    rows.append([("⬅️ Menyu", "menu")])
    return kb(*rows)


def engine_text():
    live = running_jobs()
    lines = ["🧩 <b>Dvigatel tanlash</b>", ""]
    for eng in ENGINE_ORDER:
        mark = " ✅" if CFG["engine"] == eng else ""
        busy = f" · ▶️ #{live[eng]} ishlamoqda" if eng in live else " · 💤 bo'sh"
        lines.append(f"<b>{h(ENGINES[eng]['label'])}</b>{mark}")
        lines.append(f"   🤖 {h(engine_model_label(eng))}{busy}")
    lines += [
        "",
        f"Hozirgi tanlov: <b>{h(engine_choice_label())}</b>",
        "",
        "🤝 <b>Ikkalasi</b> — bitta vazifa ikkala dvigatelga bir vaqtda yuboriladi. "
        "Har biriga alohida worker tegishli, shuning uchun ular chinakam parallel ishlaydi.",
        "",
        "Tez yo'l: xabar oldiga <code>c:</code> — faqat Claude, "
        "<code>a:</code> — faqat Antigravity, <code>b:</code> — ikkalasi.",
    ]
    return "\n".join(lines)


def model_menu():
    rows = []
    for eng in ENGINE_ORDER:
        rows.append([(f"— {ENGINES[eng]['label']} —", "noop")])
        row = []
        for mid, label, _desc, _effort in ENGINES[eng]["models"]:
            mark = "✅ " if mid == engine_model(eng) else ""
            row.append((f"{mark}{label}", f"setmodel:{eng}:{mid or '-'}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    rows.append([("⬅️ Menyu", "menu")])
    return kb(*rows)


def model_text():
    lines = ["🤖 <b>Model tanlash</b>", ""]
    for eng in ENGINE_ORDER:
        lines.append(f"<b>{h(ENGINES[eng]['label'])}</b>")
        for mid, label, desc, _effort in ENGINES[eng]["models"]:
            mark = " ✅" if mid == engine_model(eng) else ""
            lines.append(f"• {h(label)}{mark} — <i>{h(desc)}</i>")
        lines.append("")
    lines.append("Har bir dvigatelning modeli alohida saqlanadi.")
    return "\n".join(lines)


def h(text):
    return html.escape(str(text))


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


def list_projects():
    """Directories the user can point Claude at, de-duplicated and sorted."""
    found = []
    for pattern in CFG["project_globs"]:
        for path in sorted(glob.glob(pattern)):
            if os.path.isdir(path) and path not in found:
                found.append(path)
    return found


def current_project():
    return meta_get("workdir", CFG["workdir"])


def project_label(path):
    if path.rstrip("/") == "/var/www":
        return "hammasi"
    return os.path.basename(path.rstrip("/")) or path


def session_key(engine, workdir):
    return f"session:{engine}:{workdir}"


def clear_session(engine, workdir):
    """Forget the resumable conversation for one engine on one project."""
    meta_set(session_key(engine, workdir), "")
    meta_set(session_key(engine, workdir) + ":last", 0)
    meta_set(session_key(engine, workdir) + ":jobs", 0)


def clear_all_sessions(workdir):
    for engine in ENGINE_ORDER:
        clear_session(engine, workdir)


def session_usage(engine, workdir):
    """(jobs already run on this context, seconds since it was last used)."""
    used = int(float(meta_get(session_key(engine, workdir) + ":jobs", 0) or 0))
    last = float(meta_get(session_key(engine, workdir) + ":last", 0) or 0)
    return used, (time.time() - last if last else 0.0)


def take_session(engine, workdir):
    """(session id to resume, reason it was dropped) -- expires a worn context.

    Resuming replays the whole transcript on every message, so an old or long
    conversation quietly gets more expensive each time. Retire it instead.
    """
    sid = meta_get(session_key(engine, workdir), "")
    if not sid:
        return "", ""
    used, idle = session_usage(engine, workdir)
    idle_limit = CFG["session_idle_reset_sec"]
    job_limit = CFG["session_max_jobs"]
    if idle_limit and idle > idle_limit:
        log(f"{engine} session for {workdir} idle {int(idle)}s -- fresh context")
        clear_session(engine, workdir)
        return "", f"{fmt_duration(idle)} tanaffusdan keyin"
    if job_limit and used >= job_limit:
        log(f"{engine} session for {workdir} used {used} jobs -- fresh context")
        clear_session(engine, workdir)
        return "", f"{used} ta ishdan keyin"
    return sid, ""


def remember_session(engine, workdir, session_id):
    meta_set(session_key(engine, workdir), session_id)
    meta_set(session_key(engine, workdir) + ":last", time.time())
    used, _ = session_usage(engine, workdir)
    meta_set(session_key(engine, workdir) + ":jobs", used + 1)


# --------------------------------------------------------------------------
# Poller -- Telegram -> jobs table
# --------------------------------------------------------------------------

HELP_TEXT = """<b>🛰 Server Pult</b>

Bitta bot, ikkita dvigatel: 🤖 <b>Claude</b> va 🛸 <b>Antigravity</b>.
Oddiy matn yozing — u serverda bajariladi.
Rasm/fayl yuborsangiz dvigatel uni ko'radi (izoh = topshiriq).

<b>Dvigatel</b>
/engine — qaysi dvigatel ishlashini tanlash
/both vazifa — ikkalasiga bir vaqtda yuborish
<code>c:</code> · <code>a:</code> · <code>b:</code> — bitta xabar uchun tez tanlov

<b>Ish</b>
/status — hozirgi ish va navbat
/jobs — oxirgi ishlar ro'yxati
/history — so'nggi topshiriqlar
/get N — N-ishning to'liq natijasi
/stop — bajarilayotgan ishni to'xtatish
/new — kontekstni tozalash

<b>Loyiha va fayl</b>
/projects — loyihani tanlash
/cd nom — loyihaga o'tish
/pwd — joriy jild
/ls [yo'l] — papka ichi
/file yo'l — faylni Telegramga yuborish

<b>Server</b>
/server — disk, RAM, xizmatlar (token sarflamaydi)
/sh buyruq — shell buyrug'i (Claude'siz, tez)
/limit — sarflar va kontekst holati

<b>Sozlama</b>
/model — har dvigatel uchun model
/mode — Claude ruxsat rejimi
/confirm — tasdiq so'rashni yoqish/o'chirish
/settings — sozlamalar
/menu — inline menyu · /keyboard — pastki tugmalar
/ping — bot tirikligi · /restart — qayta ishga tushirish

<b>Tasdiqlash</b>
Tasdiq yoqilgan bo'lsa har topshiriqda
▶️ Bajar · 🧭 Avval reja · ❌ Bekor chiqadi.
Reja hech narsani o'zgartirmaydi — ko'rib, keyin
✅ Rejani bajar deysiz.

Ish serverda bajariladi: telefonda internet uzilsa ham
to'xtamaydi, natija aloqa tiklanganda yetib keladi."""


def start_text():
    """Short dashboard shown on /start, mirroring the Antigravity bot's."""
    confirm = "yoqilgan" if CFG["confirm_before_run"] else "o'chiq"
    return (
        "🛰 <b>Server Pult</b>\n"
        + "━" * 20 + "\n"
        f"🗂 Loyiha: <code>{h(project_label(current_project()))}</code>\n"
        f"🧩 Dvigatel: <code>{h(engine_choice_label())}</code>\n"
        f"🤖 Claude: <code>{h(CFG['model'] or 'odatiy')}</code>\n"
        f"🛸 Antigravity: <code>{h(agy_model_info(CFG['agy_model'])['label'])}</code>\n"
        f"🛡 Tasdiq: <code>{confirm}</code>\n"
        f"⏱ Bot uptime: <code>{fmt_duration(time.time() - START_TIME)}</code>\n"
        + "━" * 20 + "\n"
        "<i>Matn yozing — server bajaradi. Pastdagi tugmalar tez yo'l.</i>"
    )


def poller():
    offset = int(meta_get("update_offset", 0))
    backoff = 1
    timeouts = 0
    while not shutdown.is_set():
        try:
            updates = api_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=POLL_TIMEOUT + 15,
            )
            backoff = 1
        except Exception as e:
            if not shutdown.is_set():
                # A single long-poll read timeout is routine on a flaky uplink --
                # only log when it keeps failing.
                timeouts = timeouts + 1 if "timed out" in str(e) else 0
                if timeouts == 0 or timeouts % 10 == 1 and timeouts > 1:
                    log(f"poll error: {e} (retry in {backoff}s)")
                shutdown.wait(backoff)
                backoff = min(backoff * 2, 60)
            continue
        timeouts = 0

        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception as e:
                log(f"handle_update error: {e}")
            meta_set("update_offset", offset)


ENGINE_PREFIXES = {"c": "claude", "a": "agy", "b": "both"}


def split_engine_prefix(text):
    """('claude'|'agy'|'both'|None, remaining text) for a leading 'c:' style tag."""
    head, sep, rest = text.partition(":")
    if sep and head.strip().lower() in ENGINE_PREFIXES:
        return ENGINE_PREFIXES[head.strip().lower()], rest.strip()
    return None, text


def authorised(user_id):
    return user_id in CFG["allowed_user_ids"]


def handle_update(update):
    if "callback_query" in update:
        # Tugmani alohida thread'da ishlaymiz: /server kabi sekin ekran poller'ni
        # bloklab qo'ysa, keyingi bosishlar navbatda qolib "yuklanmoqda" bo'lib turardi.
        threading.Thread(
            target=guarded_callback, args=(update["callback_query"],), daemon=True
        ).start()
        return
    msg = update.get("message")
    if not msg:
        return

    user_id = msg.get("from", {}).get("id")
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # In a private chat the chat id equals the user id. Anything else is a group
    # or channel, where a reply would leak server state to whoever else is in it.
    if chat_id != user_id:
        log(f"ignoring non-private chat_id={chat_id} from user_id={user_id}")
        return

    if not authorised(user_id):
        if try_pairing(user_id, chat_id, text):
            return
        log(f"rejected user_id={user_id} chat_id={chat_id}: {text[:80]!r}")
        return

    attachment = extract_attachment(msg)
    if attachment:
        handle_attachment(chat_id, msg, attachment, text)
        return

    if not text:
        send(chat_id, "Bu turdagi xabarni tushunmayman. Matn, rasm yoki fayl yuboring.")
        return

    # Bottom-keyboard labels are commands wearing a nicer coat.
    text = LABEL_COMMANDS.get(text, text)

    if text.startswith("/"):
        handle_command(chat_id, text)
        return

    # "c: ...", "a: ...", "b: ..." pick an engine for this one message only.
    engine, text = split_engine_prefix(text)
    if not text:
        send(chat_id, "Vazifa matni bo'sh.")
        return

    start_job(chat_id, text, engine=engine)


def extract_attachment(msg):
    """Return (file_id, filename, human_kind) for supported attachments."""
    if msg.get("photo"):
        largest = max(msg["photo"], key=lambda p: p.get("file_size") or 0)
        return largest["file_id"], "photo.jpg", "rasm"
    if msg.get("document"):
        doc = msg["document"]
        return doc["file_id"], doc.get("file_name") or "file.bin", "fayl"
    if msg.get("voice"):
        return None  # handled separately below
    return None


# Albums arrive as several messages; only the first carries the caption.
media_groups = {}
media_lock = threading.Lock()


def handle_attachment(chat_id, msg, attachment, caption):
    file_id, filename, kind = attachment
    try:
        path = download_telegram_file(file_id, filename)
    except Exception as e:
        send(chat_id, f"❌ Faylni ololmadim: {e}")
        return

    group_id = msg.get("media_group_id")
    if group_id and not caption:
        with media_lock:
            entry = media_groups.get(group_id)
        if entry and time.time() - entry[1] < MEDIA_GROUP_WINDOW and append_to_job(entry[0], path):
            log(f"attached {path} to queued job #{entry[0]}")
            return

    prompt = f"[Foydalanuvchi {kind} yubordi: {path}]\n" + (
        caption or f"Shu {kind}ga qara va nima qilish kerakligini ayt."
    )
    job_id = start_job(chat_id, prompt, note=f"📎 {os.path.basename(path)}")
    if group_id:
        with media_lock:
            media_groups[group_id] = (job_id, time.time())


def append_to_job(job_id, path):
    """Add another album photo to a job that has not started yet."""
    with db_lock:
        row = db.execute("SELECT prompt,state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row[1] != "queued":
            return False
        db.execute(
            "UPDATE jobs SET prompt=? WHERE id=?",
            (row[0] + f"\n[Yana bir fayl: {path}]", job_id),
        )
        db.commit()
    return True


def try_pairing(user_id, chat_id, text):
    """Claim the bot on first contact. Only possible while no owner is set."""
    if CFG["allowed_user_ids"] or not CFG["pairing_code"]:
        return False
    if text.strip() != CFG["pairing_code"]:
        log(f"wrong pairing code from user_id={user_id}")
        return False
    CFG["allowed_user_ids"] = [user_id]
    CFG["pairing_code"] = ""
    save_config(CFG)
    log(f"paired with user_id={user_id}")
    send(chat_id, f"🔐 Ulandi. Bot endi faqat sizga javob beradi.\n\n{HELP_TEXT}",
         markup=main_menu(), parse_mode="HTML")
    return True


def start_job(chat_id, prompt, note="", mode=None, approved=False, engine=None):
    """Store a task. Unless pre-approved, it waits for a button press.

    engine="both" queues the same task on every engine at once -- they have
    separate workers, so the two really do run side by side.
    """
    engine = engine or CFG["engine"]
    if engine == "both":
        return [start_job(chat_id, prompt, note, mode, approved, eng)
                for eng in ENGINE_ORDER]

    workdir = current_project()
    needs_ok = CFG["confirm_before_run"] and not approved
    state = "pending" if needs_ok else "queued"
    with db_lock:
        cur = db.execute(
            "INSERT INTO jobs(chat_id,prompt,state,created,project,mode,engine) "
            "VALUES(?,?,?,?,?,?,?)",
            (chat_id, prompt, state, time.time(), workdir, mode, engine),
        )
        db.commit()
        job_id = cur.lastrowid
    audit(f"job#{job_id} [{state}] [{engine}] [{workdir}] {prompt[:200]!r}")

    head_line = f"{engine_label(engine)} · 🗂 {h(project_label(workdir))}"
    if needs_ok:
        send(chat_id,
             f"❓ <b>#{job_id}</b> — tasdiqlaysizmi?\n{head_line}\n\n"
             f"<i>{h(prompt[:400])}</i>",
             markup=confirm_menu(job_id), parse_mode="HTML")
        return job_id

    with db_lock:
        ahead = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='queued' AND engine=? AND id<?",
            (engine, job_id),
        ).fetchone()[0]
    head = f"📥 <b>#{job_id}</b> qabul qilindi"
    if note:
        head += f" · {h(note)}"
    lines = [head, head_line]
    if mode == "plan":
        lines.append("🧭 Faqat reja tuziladi, hech narsa o'zgarmaydi")
    if ahead:
        lines.append(f"⏳ Shu dvigatelda oldida {ahead} ta ish bor")
    send(chat_id, "\n".join(lines), markup=job_menu(job_id, engine), parse_mode="HTML")
    return job_id


def approve_job(job_id, mode):
    """Move a pending job into the queue. Returns a status line for the user."""
    with db_lock:
        row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return f"#{job_id} topilmadi."
        if row[0] != "pending":
            return f"#{job_id} allaqachon <b>{h(row[0])}</b> holatida."
        db.execute("UPDATE jobs SET state='queued', mode=? WHERE id=?", (mode, job_id))
        db.commit()
    audit(f"job#{job_id} approved mode={mode}")
    if mode == "plan":
        return f"🧭 <b>#{job_id}</b> — reja tuzilmoqda. Hech narsa o'zgartirilmaydi."
    return f"▶️ <b>#{job_id}</b> tasdiqlandi, bajarilmoqda…"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def handle_command(chat_id, text):
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    arg = text[len(parts[0]):].strip()

    if cmd == "/start":
        send(chat_id, start_text(), markup=main_reply_kb(), parse_mode="HTML")
        send(chat_id, "Nima qilamiz?", markup=main_menu())

    elif cmd == "/help":
        send(chat_id, HELP_TEXT, markup=back_menu(), parse_mode="HTML")

    elif cmd == "/menu":
        send(chat_id, "Nima qilamiz?", markup=main_menu())

    elif cmd == "/keyboard":
        send(chat_id, "⌨️ Tugmalar tiklandi.", markup=main_reply_kb())

    elif cmd == "/ping":
        send(chat_id, f"🟢 Tirik. Bot uptime: {fmt_duration(time.time() - START_TIME)}")

    elif cmd == "/new":
        clear_all_sessions(current_project())
        send(chat_id, f"🆕 <b>{h(project_label(current_project()))}</b> uchun yangi suhbat "
                      f"boshlandi (eski kontekst unutildi).",
             markup=main_menu(), parse_mode="HTML")

    elif cmd == "/status":
        send(chat_id, status_text(), markup=back_menu(), parse_mode="HTML")

    elif cmd == "/jobs":
        send(chat_id, jobs_text(), markup=back_menu(), parse_mode="HTML")

    elif cmd in ("/server", "/sys"):
        send(chat_id, server_text(), markup=back_menu(), parse_mode="HTML")

    elif cmd in ("/projects", "/sessions"):
        send(chat_id, projects_text(), markup=projects_menu(), parse_mode="HTML")

    elif cmd in ("/cd", "/setcwd"):
        do_cd_by_name(chat_id, arg)

    elif cmd == "/sh":
        if not arg:
            send(chat_id, "Foydalanish: <code>/sh systemctl status nginx</code>", parse_mode="HTML")
        else:
            run_shell(chat_id, arg)

    elif cmd == "/model":
        if arg:
            # A gemini-*/gpt-* name can only mean agy; everything else is Claude's.
            target = "agy" if any(arg.startswith(p) for p in ("gemini", "gpt")) else "claude"
            CFG[ENGINES[target]["model_key"]] = (
                "" if arg in ("-", "default", "odatiy") else arg)
            save_config(CFG)
        send(chat_id, model_text(), markup=model_menu(), parse_mode="HTML")

    elif cmd == "/mode":
        valid = ["auto", "acceptEdits", "plan", "manual", "dontAsk", "bypassPermissions"]
        if arg:
            if arg not in valid:
                send(chat_id, "Rejimlar: " + ", ".join(valid))
                return
            CFG["permission_mode"] = arg
            save_config(CFG)
        send(chat_id, f"🔓 Rejim: <b>{h(CFG['permission_mode'])}</b>\n"
                      f"<code>auto</code> — so'ramasdan bajaradi\n"
                      f"<code>plan</code> — faqat reja, o'zgartirmaydi\n"
                      f"<code>acceptEdits</code> — fayl tahriri avtomatik, qolgani so'raladi",
             parse_mode="HTML")

    elif cmd == "/get":
        if not arg:
            send(chat_id, "Foydalanish: <code>/get 12</code> — ish natijasi, "
                          "<code>/get storage/logs/laravel.log</code> — fayl",
                 parse_mode="HTML")
        elif arg.isdigit():
            deliver_full_result(chat_id, int(arg))
        else:
            send_user_file(chat_id, arg)

    elif cmd in ("/cancel", "/stop", "/kill"):
        target = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        send(chat_id, do_cancel(target))

    elif cmd == "/settings":
        send(chat_id, settings_text(), markup=settings_menu(), parse_mode="HTML")

    elif cmd == "/confirm":
        CFG["confirm_before_run"] = not CFG["confirm_before_run"]
        save_config(CFG)
        send(chat_id, settings_text(), markup=settings_menu(), parse_mode="HTML")

    elif cmd in ("/engine", "/dvigatel"):
        if arg:
            choice = arg.strip().lower()
            aliases = {"claude": "claude", "c": "claude", "agy": "agy",
                       "antigravity": "agy", "a": "agy", "both": "both",
                       "ikkalasi": "both", "b": "both"}
            if choice not in aliases:
                send(chat_id, "Tanlovlar: <code>claude</code>, <code>agy</code>, "
                              "<code>both</code>", parse_mode="HTML")
                return
            CFG["engine"] = aliases[choice]
            save_config(CFG)
        send(chat_id, engine_text(), markup=engine_menu(), parse_mode="HTML")

    elif cmd == "/both":
        if not arg:
            send(chat_id, "Foydalanish: <code>/both nginx konfigini tekshir</code>",
                 parse_mode="HTML")
        else:
            start_job(chat_id, arg, engine="both")

    elif cmd == "/limit":
        send(chat_id, limit_text(), markup=back_menu(), parse_mode="HTML")

    elif cmd == "/history":
        send(chat_id, history_text(), markup=back_menu(), parse_mode="HTML")

    elif cmd == "/pwd":
        send(chat_id, f"📁 <b>{h(project_label(current_project()))}</b>\n"
                      f"<code>{h(current_project())}</code>", parse_mode="HTML")

    elif cmd == "/ls":
        send(chat_id, ls_text(arg), parse_mode="HTML")

    elif cmd == "/file":
        if not arg:
            send(chat_id, "Foydalanish: <code>/file storage/logs/laravel.log</code>",
                 parse_mode="HTML")
        else:
            send_user_file(chat_id, arg)

    elif cmd == "/restart":
        # Supervisor restarts us (autorestart=true); a running job is requeued on start.
        send(chat_id, "🔄 Bot qayta ishga tushmoqda…")
        threading.Timer(2.0, lambda: (shutdown.set(), do_cancel())).start()

    else:
        send(chat_id, f"Noma'lum buyruq: {h(cmd)}", markup=main_menu(), parse_mode="HTML")


def guarded_callback(query):
    try:
        handle_callback(query)
    except Exception as e:
        log(f"callback error ({(query.get('data') or '')!r}): {e}")


def handle_callback(query):
    user_id = query.get("from", {}).get("id")
    data = query.get("data") or ""
    msg = query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    # Telegram spinner faqat answerCallbackQuery kelganda o'chadi va so'rov bir necha
    # soniyada eskiradi — shuning uchun ishni boshlashdan oldin darhol javob beramiz.
    answered = threading.Event()

    def answer(note=""):
        if answered.is_set():
            return
        answered.set()
        ok = api_try("answerCallbackQuery",
                     {"callback_query_id": query["id"], "text": note}, timeout=8)
        if ok is None:
            log(f"answerCallbackQuery failed for {data!r}")

    if not authorised(user_id) or chat_id != user_id:
        answer("Ruxsat yo'q")
        return

    # Tarmoq sekin bo'lsa ham spinner osilib qolmasin: 1.5 soniyada hech kim javob
    # bermagan bo'lsa, o'zimiz bo'sh javob yuboramiz.
    threading.Timer(1.5, answer).start()

    def screen(text, markup):
        api_try("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "reply_markup": markup,
            "link_preview_options": {"is_disabled": True},
        })

    if data == "menu":
        answer()
        screen("Nima qilamiz?", main_menu())
    elif data == "status":
        answer()
        screen(status_text(), back_menu())
    elif data == "server":
        answer("O'lchanmoqda…")
        screen(server_text(), back_menu())
    elif data == "jobs":
        answer()
        screen(jobs_text(), back_menu())
    elif data == "help":
        answer()
        screen(HELP_TEXT, back_menu())
    elif data == "settings":
        answer()
        screen(settings_text(), settings_menu())
    elif data == "engine":
        answer()
        screen(engine_text(), engine_menu())
    elif data.startswith("setengine:"):
        choice = data.split(":", 1)[1]
        if choice in ENGINE_ORDER or choice == "both":
            CFG["engine"] = choice
            save_config(CFG)
            answer(engine_choice_label())
        screen(engine_text(), engine_menu())
    elif data == "noop":
        answer()
    elif data == "model":
        answer()
        screen(model_text(), model_menu())
    elif data == "limit":
        answer()
        screen(limit_text(), back_menu())
    elif data.startswith("setmodel:"):
        _, eng, choice = data.split(":", 2)
        if eng in ENGINES:
            CFG[ENGINES[eng]["model_key"]] = "" if choice == "-" else choice
            save_config(CFG)
            answer(engine_model_label(eng))
        screen(model_text(), model_menu())
    elif data == "projects":
        answer()
        screen(projects_text(), projects_menu())
    elif data == "new":
        clear_all_sessions(current_project())
        answer("Kontekst tozalandi")
        screen(f"🆕 <b>{h(project_label(current_project()))}</b> uchun yangi suhbat boshlandi.",
               main_menu())
    elif data.startswith("cd:"):
        projects = list_projects()
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(projects):
            meta_set("workdir", projects[idx])
            answer(project_label(projects[idx]))
            screen(projects_text(), projects_menu())
        else:
            answer("Topilmadi")
    elif data == "toggle_confirm":
        CFG["confirm_before_run"] = not CFG["confirm_before_run"]
        save_config(CFG)
        answer("Tasdiq: " + ("yoqildi" if CFG["confirm_before_run"] else "o'chirildi"))
        screen(settings_text(), settings_menu())
    elif data.startswith("run:"):
        job_id = int(data.split(":", 1)[1])
        note = approve_job(job_id, "auto")
        answer("Bajarilmoqda")
        screen(note, job_menu(job_id))
    elif data.startswith("plan:"):
        job_id = int(data.split(":", 1)[1])
        note = approve_job(job_id, "plan")
        answer("Reja tuzilmoqda")
        screen(note, job_menu(job_id))
    elif data.startswith("drop:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            db.execute(
                "UPDATE jobs SET state='cancelled', finished=? WHERE id=? AND state='pending'",
                (time.time(), job_id),
            )
            db.commit()
        answer("Bekor qilindi")
        screen(f"❌ <b>#{job_id}</b> bekor qilindi. Hech narsa bajarilmadi.", main_menu())
    elif data.startswith("exec:"):
        job_id = int(data.split(":", 1)[1])
        answer("Bajarilmoqda")
        start_job(chat_id,
                  "Yuqorida tuzgan rejangni tasdiqlayman — endi to'liq bajar.",
                  note=f"reja #{job_id}", mode="auto", approved=True)
    elif data.startswith("cancel:"):
        note = do_cancel(int(data.split(":", 1)[1]))
        answer(note[:180])
        screen(note, main_menu())
    elif data.startswith("again:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            row = db.execute("SELECT prompt FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            answer("Qayta yuborildi")
            start_job(chat_id, row[0])
        else:
            answer("Topilmadi")
    elif data.startswith("full:"):
        answer("Yuborilmoqda…")
        deliver_full_result(chat_id, int(data.split(":", 1)[1]))
    else:
        answer()


def projects_menu():
    projects = list_projects()
    current = current_project()
    rows, row = [], []
    for i, path in enumerate(projects):
        mark = "✅ " if path == current else ""
        row.append((f"{mark}{project_label(path)}", f"cd:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("⬅️ Menyu", "menu")])
    return kb(*rows)


def projects_text():
    current = current_project()
    live = [engine_label(e) for e in ENGINE_ORDER
            if meta_get(session_key(e, current), "")]
    return (
        f"🗂 <b>Joriy loyiha:</b> {h(project_label(current))}\n"
        f"<code>{h(current)}</code>\n"
        f"🧠 Suhbat: {h(', '.join(live)) if live else 'yangi boshlanadi'}\n\n"
        "Har bir dvigatel har bir loyiha uchun alohida kontekst saqlaydi — "
        "boshqasiga o'tib qaytsangiz, suhbat joyidan davom etadi."
    )


def limit_text():
    """What the limits are actually spent in: tokens, not dollars.

    A Max subscription bills nothing per token, so the only number that means
    anything here is how many tokens were pushed through.
    """
    today = time.strftime("%Y-%m-%d")
    with db_lock:
        per_day = db.execute(
            "SELECT date(created,'unixepoch','localtime') AS day, COUNT(*), "
            "COALESCE(SUM(turns),0), COALESCE(SUM(tokens),0), COUNT(tokens) "
            "FROM jobs WHERE created > ? GROUP BY day ORDER BY day DESC",
            (time.time() - 7 * 86400,),
        ).fetchall()
        per_engine = db.execute(
            "SELECT engine, COUNT(*), COALESCE(SUM(turns),0), COALESCE(SUM(tokens),0) "
            "FROM jobs WHERE date(created,'unixepoch','localtime')=? GROUP BY engine",
            (today,),
        ).fetchall()

    lines = ["📈 <b>Sarflar</b>", "", f"📅 <b>Bugun ({h(today)})</b>"]
    stats = {eng: (0, 0, 0) for eng in ENGINE_ORDER}
    for eng, count, turns, tokens in per_engine:
        stats[eng] = (count, int(turns), int(tokens))
    day_total = 0
    for eng in ENGINE_ORDER:
        count, turns, tokens = stats[eng]
        day_total += tokens
        lines.append(f"   {engine_label(eng)} — {count} ish · {turns} qadam · "
                     f"<b>{fmt_tokens(tokens)}</b> token")
    lines.append(f"   <b>Jami: {fmt_tokens(day_total)} token</b>")

    lines += ["", "📊 <b>Oxirgi kunlar</b>"]
    if not per_day:
        lines.append("   <i>Hali ish yo'q.</i>")
    for day, count, turns, tokens, measured in per_day:
        amount = (f"<b>{fmt_tokens(tokens)}</b> token" if measured
                  else "<i>token yozilmagan</i>")
        lines.append(f"   <b>{h(day)}</b> · {count} ish · {int(turns)} qadam · {amount}")

    lines += ["", "🧠 <b>Kontekst</b>"]
    for eng in ENGINE_ORDER:
        used, idle = session_usage(eng, current_project())
        state = (f"{used}/{CFG['session_max_jobs']} ish · {fmt_duration(idle)} oldin"
                 if used else "yangi suhbat")
        lines.append(f"   {engine_label(eng)} — {state}")

    lines += [
        "",
        f"🎛 Qadam limiti: {CFG['max_turns']} · ⏰ Ish limiti: "
        f"{CFG['job_timeout_sec'] // 60} daq",
        f"🤖 Claude: <b>{h(CFG['model'] or 'odatiy')}</b> · "
        f"🛸 Antigravity: <b>{h(agy_model_info(CFG['agy_model'])['label'])}</b>",
        "",
        "<i>Max obuna: pul yechilmaydi, limit tokenda o'lchanadi. Claude raqamiga "
        "kesh o'qishlari ham kiradi.</i>",
    ]
    return "\n".join(lines)


def history_text():
    with db_lock:
        rows = db.execute(
            "SELECT id, prompt FROM jobs ORDER BY id DESC LIMIT 15"
        ).fetchall()
    if not rows:
        return "📜 Tarix bo'sh."
    lines = ["📜 <b>So'nggi topshiriqlar</b>", ""]
    for jid, prompt in rows:
        lines.append(f"<b>#{jid}</b> · {h(prompt[:110])}")
    lines.append("\nQaytadan yuborish: natija ostidagi 🔁 tugmasi.")
    return "\n".join(lines)


def ls_text(target):
    """Directory listing -- no model tokens, same output as the agy bot's /ls."""
    base = current_project()
    path = target if os.path.isabs(target) else os.path.join(base, target)
    path = os.path.abspath(path or base)
    if not os.path.isdir(path):
        return f"❌ Papka topilmadi:\n<code>{h(path)}</code>"
    try:
        entries = sorted(os.listdir(path))
    except OSError as e:
        return f"❌ O'qib bo'lmadi: {h(e)}"
    dirs = [f"📁 {n}" for n in entries if os.path.isdir(os.path.join(path, n))]
    files = [f"📄 {n}" for n in entries if not os.path.isdir(os.path.join(path, n))]
    body = "\n".join(dirs + files) or "(bo'sh)"
    if len(body) > 3000:
        body = body[:3000] + "\n… (qisqartirildi)"
    return f"📁 <b>{h(path)}</b>\n<pre>{h(body)}</pre>"


def send_user_file(chat_id, target):
    """Push any file on the server to Telegram (the agy bot's /get <file>)."""
    base = current_project()
    path = target if os.path.isabs(target) else os.path.join(base, target)
    path = os.path.abspath(path)
    if not os.path.exists(path):
        send(chat_id, f"❌ Fayl topilmadi:\n<code>{h(path)}</code>", parse_mode="HTML")
        return
    if os.path.isdir(path):
        send(chat_id, "❌ Bu papka, fayl emas. <code>/ls</code> bilan ko'ring.",
             parse_mode="HTML")
        return
    size = os.path.getsize(path)
    if size > 50 * 1024 * 1024:
        send(chat_id, f"❌ Fayl juda katta ({size // 1048576} MB). Telegram 50 MB gacha.")
        return
    audit(f"sent file {path!r}")
    send_document(chat_id, path, caption=path)


def do_cd_by_name(chat_id, name):
    if not name:
        send(chat_id, projects_text(), markup=projects_menu(), parse_mode="HTML")
        return
    projects = list_projects()
    matches = [p for p in projects if name.lower() in project_label(p).lower()]
    if not matches and os.path.isdir(name):
        matches = [os.path.abspath(name)]
    if not matches:
        send(chat_id, f"❌ <b>{h(name)}</b> topilmadi.", markup=projects_menu(), parse_mode="HTML")
        return
    meta_set("workdir", matches[0])
    send(chat_id, projects_text(), markup=projects_menu(), parse_mode="HTML")


def settings_menu():
    toggle = "🛡 Tasdiq: YOQILGAN" if CFG["confirm_before_run"] else "⚡ Tasdiq: O'CHIQ"
    return kb(
        [(toggle, "toggle_confirm")],
        [("🧩 Dvigatel", "engine"), ("🤖 Model", "model")],
        [("📈 Limit", "limit")],
        [("🗂 Loyihalar", "projects"), ("⬅️ Menyu", "menu")],
    )


def settings_text():
    confirm = (
        "🛡 <b>yoqilgan</b> — har topshiriq tugma bilan tasdiqlanadi"
        if CFG["confirm_before_run"]
        else "⚡ <b>o'chiq</b> — topshiriq darhol bajariladi"
    )
    return (
        "⚙️ <b>Sozlamalar</b>\n\n"
        f"Tasdiqlash: {confirm}\n"
        f"🧩 Dvigatel: <b>{h(engine_choice_label())}</b> — <code>/engine</code>\n"
        f"🤖 Claude modeli: <b>{h(CFG['model'] or 'odatiy')}</b>\n"
        f"🛸 Antigravity modeli: <b>{h(agy_model_info(CFG['agy_model'])['label'])}</b>\n"
        f"🎛 agy bayroqlari: <code>{h(' '.join(CFG['agy_flags']) or '(yo\'q)')}</code>\n"
        f"🔓 Claude rejimi: <b>{h(CFG['permission_mode'])}</b> — <code>/mode plan</code>\n"
        f"🗂 Loyiha: <b>{h(project_label(current_project()))}</b> — <code>/cd nom</code>\n"
        f"⏰ Ish limiti: {CFG['job_timeout_sec'] // 60} daqiqa\n"
        f"👤 Ruxsat: {len(CFG['allowed_user_ids'])} ta foydalanuvchi\n\n"
        "Tasdiq yoqilganda har topshiriqda <b>▶️ Bajar</b>, <b>🧭 Avval reja</b> va "
        "<b>❌ Bekor</b> tugmalari chiqadi. Reja tugmasi hech narsani o'zgartirmasdan "
        "faqat nima qilishini aytadi — ko'rib, keyin <b>✅ Rejani bajar</b> deysiz."
    )


# --------------------------------------------------------------------------
# Status / reporting
# --------------------------------------------------------------------------


def status_text():
    with db_lock:
        live_rows = db.execute(
            "SELECT id,prompt,started,engine FROM jobs WHERE state='running' ORDER BY id"
        ).fetchall()
        queued = db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
        awaiting = db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        today = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens),0) FROM jobs WHERE created > ?",
            (time.time() - 86400,),
        ).fetchone()
    lines = ["📊 <b>Holat</b>", ""]
    if live_rows:
        for jid, prompt, started, eng in live_rows:
            elapsed = fmt_duration(time.time() - (started or time.time()))
            lines.append(f"▶️ <b>#{jid}</b> {h(engine_label(eng))} ({elapsed})")
            lines.append(f"   {h(prompt[:110])}")
        if len(live_rows) > 1:
            lines.append("⚡ <i>Ikkala dvigatel bir vaqtda ishlamoqda</i>")
    else:
        lines.append("💤 Bo'sh turibdi")
    with db_lock:
        per_engine = dict(db.execute(
            "SELECT engine, COUNT(*) FROM jobs WHERE state='queued' GROUP BY engine"
        ).fetchall())
    if queued:
        detail = ", ".join(f"{engine_label(e)} {per_engine[e]}" for e in sorted(per_engine))
        lines.append(f"📋 Navbatda: {queued} ({h(detail)})")
    if awaiting:
        lines.append(f"❓ Tasdiq kutmoqda: {awaiting}")
    if pending:
        lines.append(f"📤 Yuborilmagan xabar: {pending}")
    lines.append(f"🗂 Loyiha: <b>{h(project_label(current_project()))}</b>")
    for eng in ENGINE_ORDER:
        if meta_get(session_key(eng, current_project()), ""):
            used, idle = session_usage(eng, current_project())
            lines.append(f"🧠 {engine_label(eng)}: {used}/{CFG['session_max_jobs']} ish · "
                         f"{fmt_duration(idle)} oldin")
        else:
            lines.append(f"🧠 {engine_label(eng)}: yangi suhbat")
    lines.append(f"📅 24 soatda: {today[0]} ta ish · "
                 f"{fmt_tokens(today[1])} token")
    lines.append(f"⏱ Bot uptime: {fmt_duration(time.time() - START_TIME)}")
    return "\n".join(lines)


def jobs_text():
    with db_lock:
        rows = db.execute(
            "SELECT id,state,created,finished,prompt,project FROM jobs ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return "Hali ish yo'q."
    icons = {"pending": "❓", "queued": "⏳", "running": "▶️",
             "done": "✅", "failed": "❌", "cancelled": "🛑"}
    out = ["📋 <b>Oxirgi ishlar</b>", ""]
    for jid, state, created, finished, prompt, project in rows:
        dur = fmt_duration((finished or time.time()) - created)
        tag = project_label(project or "")
        out.append(f"{icons.get(state, '•')} <b>#{jid}</b> · {dur} · {h(tag)}")
        out.append(f"   {h(prompt[:80])}")
    out.append("\nTo'liq natija: <code>/get N</code>")
    return "\n".join(out)


def server_text():
    """Instant server health -- plain shell, no model tokens burned."""
    def sh(cmd, default="—"):
        try:
            return subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=10
            ).stdout.strip() or default
        except Exception:
            return default

    load = sh("cut -d' ' -f1-3 /proc/loadavg")
    cores = os.cpu_count() or 1
    up = sh("uptime -p")
    mem = sh("free -m | awk '/^Mem:/{printf \"%s / %s MB (%.0f%%)\", $3, $2, $3*100/$2}'")
    swap = sh("free -m | awk '/^Swap:/{if($2>0) printf \"%s / %s MB\", $3, $2; else print \"yo'\\''q\"}'")
    disk = sh("df -h / | awk 'NR==2{printf \"%s / %s (%s)\", $3, $2, $5}'")
    inodes = sh("df -i / | awk 'NR==2{print $5}'")

    services = []
    candidates = ["nginx", "mysql", "mariadb", "postgresql", "redis-server", "supervisor"]
    candidates += [os.path.basename(p)[:-8] for p in glob.glob("/lib/systemd/system/php*-fpm.service")]
    for svc in candidates:
        state = sh(f"systemctl is-active {svc} 2>/dev/null", "")
        if state and state != "inactive":
            services.append(f"{'🟢' if state == 'active' else '🔴'} {svc}")

    with db_lock:
        running = db.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]

    lines = [
        "🖥 <b>Server holati</b>",
        "",
        f"⏱ Uptime: {h(up)}",
        f"⚡ Load: {h(load)} ({cores} yadro)",
        f"🧠 RAM: {h(mem)}",
        f"💾 Swap: {h(swap)}",
        f"📀 Disk /: {h(disk)} · inode {h(inodes)}",
        "",
        "<b>Xizmatlar</b>",
    ]
    lines += ["  " + h(s) for s in services] or ["  —"]
    bots = sh("supervisorctl status server-pult-bot 2>/dev/null | awk '{print $1\": \"$2}'", "")
    if bots:
        lines.append("")
        lines.append("<b>Botlar (supervisor)</b>")
        lines += ["  " + h(b) for b in bots.splitlines()]
    lines.append("")
    lines.append(f"🤖 Claude ishlari: {running} ta bajarilmoqda")
    return "\n".join(lines)


def run_shell(chat_id, command):
    """Direct shell escape hatch -- fast answers without invoking the model."""
    audit(f"shell {command!r}")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=current_project(),
            capture_output=True,
            text=True,
            timeout=CFG["shell_timeout_sec"],
        )
        out = (proc.stdout + proc.stderr).strip() or "(chiqish yo'q)"
        status = "✅" if proc.returncode == 0 else f"❌ exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        out, status = "(vaqt tugadi)", "⏰"
    if len(out) > 3000:
        out = out[:3000] + "\n… (qisqartirildi)"
    send(chat_id, f"{status} <code>{h(command[:120])}</code>\n<pre>{h(out)}</pre>",
         parse_mode="HTML")


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


def deliver_full_result(chat_id, job_id):
    with db_lock:
        row = db.execute(
            "SELECT state,result,prompt FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    if not row:
        send(chat_id, f"#{job_id} topilmadi.")
        return
    state, result, prompt = row
    body = result or "(natija yo'q)"
    if len(body) <= INLINE_RESULT_LIMIT:
        send(chat_id, f"<b>#{job_id}</b> [{h(state)}]\n\n{h(body)}", parse_mode="HTML")
        return
    path = os.path.join(UPLOAD_DIR, f"job-{job_id}.md")
    with open(path, "w") as f:
        f.write(f"# Ish #{job_id} [{state}]\n\n## Topshiriq\n\n{prompt}\n\n## Natija\n\n{body}\n")
    send_document(chat_id, path, caption=f"#{job_id} — to'liq natija ({len(body)} belgi)")


# --------------------------------------------------------------------------
# Workers -- jobs table -> engine CLI -> outbox
#
# One worker per engine, so a Claude job and an Antigravity job genuinely run at
# the same time. Within one engine jobs stay serialised: two runs of the same CLI
# over the same tree would fight over the files.
# --------------------------------------------------------------------------

RUNNING = {}                       # engine -> {"proc": Popen, "job_id": int}
run_lock = threading.Lock()


def running_jobs():
    with run_lock:
        return {eng: info["job_id"] for eng, info in RUNNING.items()}


def do_cancel(job_id=None, engine=None):
    """Stop running jobs (optionally one engine or one id), or drop a queued one."""
    stopped = []
    with run_lock:
        targets = list(RUNNING.items())
    for eng, info in targets:
        if engine and eng != engine:
            continue
        if job_id is not None and info["job_id"] != job_id:
            continue
        proc = info["proc"]
        if proc.poll() is None:
            proc.terminate()
            threading.Timer(5, lambda pr=proc: pr.poll() is None and pr.kill()).start()
            stopped.append(f"#{info['job_id']} {engine_label(eng)}")
    if stopped:
        return "🛑 To'xtatilmoqda: " + ", ".join(stopped)
    if job_id is not None:
        with db_lock:
            cur = db.execute(
                "UPDATE jobs SET state='cancelled', finished=? "
                "WHERE id=? AND state IN ('queued','pending')",
                (time.time(), job_id),
            )
            db.commit()
        if cur.rowcount:
            return f"🛑 #{job_id} bekor qilindi."
        return f"#{job_id} allaqachon tugagan."
    return "Hozir bajarilayotgan ish yo'q."


def worker(engine):
    while not shutdown.is_set():
        with db_lock:
            row = db.execute(
                "SELECT id,chat_id,prompt,project,mode FROM jobs "
                "WHERE state='queued' AND engine=? ORDER BY id LIMIT 1",
                (engine,),
            ).fetchone()
        if not row:
            shutdown.wait(1)
            continue
        job_id, chat_id, prompt, project, mode = row
        with db_lock:
            db.execute(
                "UPDATE jobs SET state='running', started=? WHERE id=?", (time.time(), job_id)
            )
            db.commit()
        try:
            run_job(job_id, chat_id, prompt, project or CFG["workdir"], engine, mode=mode)
        except Exception as e:
            log(f"job #{job_id} ({engine}) crashed: {e}")
            finish_job(job_id, "failed", f"Ichki xato: {e}", -1)
            send(chat_id, f"❌ #{job_id} xato: {h(e)}", markup=result_menu(job_id),
                 parse_mode="HTML")


def finish_job(job_id, state, result, exit_code, cost=None, turns=None, tokens=None):
    with db_lock:
        db.execute(
            "UPDATE jobs SET state=?, finished=?, result=?, exit_code=?, cost=?, turns=?, "
            "tokens=? WHERE id=?",
            (state, time.time(), result, exit_code, cost, turns, tokens, job_id),
        )
        db.commit()


def run_job(job_id, chat_id, prompt, workdir, engine, mode=None, attempt=0):
    spec = ENGINES[engine]
    if not os.path.isdir(workdir):
        workdir = CFG["workdir"]
    session_id, reset_reason = take_session(engine, workdir)
    if reset_reason:
        send(chat_id, f"🧠 {engine_label(engine)}: {reset_reason} kontekst yangilandi "
                      f"(token tejash uchun).")
    started = time.time()

    system_prompt = CFG["system_prompt"] + local_api_hint()
    if mode == "plan":
        system_prompt += spec["plan_hint"]
    cmd, stdin_text = spec["build"](prompt, session_id, mode, system_prompt)
    log(f"job #{job_id} [{engine}] start in {workdir}: {cmd[0]} {' '.join(cmd[1:4])}…")

    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=dict(os.environ, CLAUDE_CODE_ENTRYPOINT="vexa-pult",
                 HOME=os.environ.get("HOME", "/root")),
    )
    with run_lock:
        RUNNING[engine] = {"proc": proc, "job_id": job_id}

    killer = threading.Timer(CFG["job_timeout_sec"], proc.kill)
    killer.daemon = True
    killer.start()

    stderr_lines = []
    stderr_thread = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
    stderr_thread.start()

    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    progress = ProgressReporter(chat_id, job_id, started, workdir, engine)
    final_text = None
    new_session_id = None
    cost = turns = tokens = None
    max_turns = CFG["max_turns"]
    assistant_turns = 0
    capped = False

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for kind, payload in spec["events"](event):
            if kind == "session":
                new_session_id = payload
            elif kind == "turn":
                assistant_turns += 1
                if max_turns and assistant_turns > max_turns:
                    capped = True
            elif kind == "tool":
                progress.note_tool(payload[0], payload[1])
            elif kind == "result":
                final_text = payload["text"]
                cost, turns, tokens = payload["cost"], payload["turns"], payload["tokens"]
                if payload["error"] and not final_text.startswith("["):
                    final_text = f"(xatolik) {final_text}"
        if capped:
            log(f"job #{job_id} [{engine}]: over {max_turns} turns -- stopping it")
            proc.kill()
            break

    proc.wait()
    killer.cancel()
    stderr_thread.join(timeout=5)
    with run_lock:
        RUNNING.pop(engine, None)
    progress.clear()

    stderr_text = "".join(stderr_lines).strip()
    elapsed = fmt_duration(time.time() - started)

    # A stale session id makes the CLI exit immediately -- start fresh and retry once.
    if (proc.returncode != 0 and session_id and final_text is None
            and attempt == 0 and not capped):
        log(f"job #{job_id} [{engine}]: resume failed, retrying with a fresh session")
        clear_session(engine, workdir)
        send(chat_id, f"ℹ️ #{job_id}: eski suhbat topilmadi, yangisi boshlandi.")
        return run_job(job_id, chat_id, prompt, workdir, engine, mode=mode, attempt=1)

    if new_session_id:
        remember_session(engine, workdir, new_session_id)

    if capped:
        note = final_text or f"{max_turns} qadamdan oshdi."
        finish_job(job_id, "failed", note, proc.returncode, cost, turns, tokens)
        send(chat_id,
             f"🧯 <b>#{job_id}</b> {max_turns} qadam chegarasidan oshgani uchun to'xtatildi "
             f"({elapsed}). Vazifani kichikroq bo'laklarga bo'ling.",
             markup=result_menu(job_id), parse_mode="HTML")
        return
    if proc.returncode == -signal.SIGTERM:
        finish_job(job_id, "cancelled", final_text, proc.returncode, cost, turns, tokens)
        send(chat_id, f"🛑 #{job_id} to'xtatildi ({elapsed}).", markup=result_menu(job_id))
        return
    if proc.returncode == -signal.SIGKILL:
        finish_job(job_id, "failed", final_text, proc.returncode, cost, turns, tokens)
        send(chat_id, f"⏰ #{job_id} vaqt tugadi ({elapsed}).", markup=result_menu(job_id))
        return

    if final_text is None:
        detail = stderr_text[-1000:] or f"exit code {proc.returncode}"
        finish_job(job_id, "failed", detail, proc.returncode, cost, turns, tokens)
        send(chat_id, f"❌ <b>#{job_id}</b> bajarilmadi ({elapsed}):\n<pre>{h(detail)}</pre>",
             markup=result_menu(job_id), parse_mode="HTML")
        return

    finish_job(job_id, "done", final_text, proc.returncode, cost, turns, tokens)
    deliver_result(chat_id, job_id, final_text, elapsed, progress, turns, tokens, mode, engine)
    log(f"job #{job_id} [{engine}] done in {elapsed}")


def deliver_result(chat_id, job_id, text, elapsed, progress, turns, tokens, mode=None,
                   engine="claude"):
    planned = mode == "plan"
    meta = [f"⏱ {elapsed}", engine_label(engine)]
    if turns:
        meta.append(f"🔄 {turns}")
    if tokens:
        meta.append(f"🧮 {fmt_tokens(tokens)} token")
    header = ("🧭 <b>#%d</b> REJA · " % job_id if planned else f"✅ <b>#{job_id}</b> · ")
    header += " · ".join(meta)
    tools = progress.summary()
    if tools:
        header += f"\n🔧 {h(tools)}"

    if len(text) <= INLINE_RESULT_LIMIT:
        send(chat_id, f"{header}\n\n{h(text)}",
             markup=result_menu(job_id, planned=planned), parse_mode="HTML")
        return

    # Long answers go out as a file so nothing is lost, with a readable preview.
    path = os.path.join(UPLOAD_DIR, f"job-{job_id}.md")
    with open(path, "w") as f:
        f.write(f"# Ish #{job_id}\n\n{text}\n")
    preview = text[:PREVIEW_CHARS].rsplit("\n", 1)[0]
    send(chat_id,
         f"{header}\n\n{h(preview)}\n\n… javob uzun ({len(text)} belgi), to'lig'i faylda ↓",
         markup=result_menu(job_id, full=True, planned=planned), parse_mode="HTML")
    send_document(chat_id, path, caption=f"#{job_id} — to'liq natija")


TOOL_LABELS = {
    # Claude Code
    "Read": "o'qish", "Edit": "tahrir", "Write": "yozish", "Bash": "buyruq",
    "Grep": "qidiruv", "Glob": "fayl qidiruv", "WebFetch": "veb", "WebSearch": "qidiruv",
    "Task": "agent", "TodoWrite": "reja",
    # Antigravity (agy)
    "run_command": "buyruq", "read_file": "o'qish", "view_file": "o'qish",
    "write_to_file": "yozish", "replace_file_content": "tahrir",
    "multi_replace_file_content": "tahrir", "list_dir": "papka",
    "grep_search": "qidiruv", "find_by_name": "fayl qidiruv",
    "search_web": "veb", "read_url_content": "veb", "task_boundary": "reja",
}


class ProgressReporter:
    """Live progress in one edited message. Gives up quietly when Telegram is down."""

    def __init__(self, chat_id, job_id, started, workdir, engine):
        self.chat_id = chat_id
        self.job_id = job_id
        self.started = started
        self.workdir = workdir
        self.engine = engine
        self.tools = []
        self.last_detail = ""
        self.message_id = None
        self.last_update = 0.0

    def note_tool(self, name, tool_input):
        self.tools.append(name)
        self.last_detail = self._describe(name, tool_input)
        now = time.time()
        if now - self.last_update < CFG["progress_interval_sec"]:
            return
        self.last_update = now
        self._render()

    @staticmethod
    def _describe(name, tool_input):
        label = TOOL_LABELS.get(name, name)
        for key in ("file_path", "command", "pattern", "path", "url", "description",
                    "CommandLine", "AbsolutePath", "TargetFile", "File", "Query",
                    "DirectoryPath", "Url", "Pattern"):
            value = tool_input.get(key)
            if value:
                value = str(value).replace("\n", " ")
                if key == "file_path":
                    value = os.path.basename(value)
                return f"{label}: {value[:60]}"
        return label

    def _render(self):
        text = (
            f"⚙️ <b>#{self.job_id}</b> {h(engine_label(self.engine))} · "
            f"{fmt_duration(time.time() - self.started)}\n"
            f"🗂 {h(project_label(self.workdir))}\n"
            f"🔧 {h(self.last_detail)}\n"
            f"📊 {len(self.tools)} ta amal"
        )
        params = {"text": text, "parse_mode": "HTML",
                  "reply_markup": job_menu(self.job_id, self.engine)}
        if self.message_id is None:
            res = api_try("sendMessage", dict(params, chat_id=self.chat_id), timeout=10)
            if res:
                self.message_id = res["message_id"]
        else:
            api_try("editMessageText",
                    dict(params, chat_id=self.chat_id, message_id=self.message_id), timeout=10)

    def summary(self):
        if not self.tools:
            return ""
        counts = {}
        for name in self.tools:
            counts[name] = counts.get(name, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        return ", ".join(f"{TOOL_LABELS.get(n, n)}×{c}" if c > 1 else TOOL_LABELS.get(n, n)
                         for n, c in top)

    def clear(self):
        if self.message_id is not None:
            api_try("deleteMessage",
                    {"chat_id": self.chat_id, "message_id": self.message_id}, timeout=10)


# --------------------------------------------------------------------------
# Sender -- outbox -> Telegram, retrying until the network comes back
# --------------------------------------------------------------------------


def sender():
    backoff = 1
    while not shutdown.is_set():
        # Clear before reading: anything queued after this point sets the event
        # again, so no message can slip through the gap.
        outbox_ready.clear()
        with db_lock:
            row = db.execute(
                "SELECT id,chat_id,text,attempts,kind,parse_mode,markup,file_path "
                "FROM outbox ORDER BY id LIMIT 1"
            ).fetchone()
        if not row:
            outbox_ready.wait(5)
            continue
        msg_id, chat_id, text, attempts, kind, parse_mode, markup, file_path = row
        try:
            deliver_outbox(chat_id, text, kind, parse_mode, markup, file_path)
            drop_outbox(msg_id)
            backoff = 1
        except TelegramError as e:
            if e.retry_after:
                log(f"rate limited, waiting {e.retry_after}s")
                shutdown.wait(e.retry_after + 1)
                continue
            # 4xx means Telegram will never accept this message. Retry once without
            # HTML (a stray tag is the usual culprit), then give up on it.
            if 400 <= e.code < 500:
                # Until the user presses /start the bot may not write to them at
                # all; retrying that forever just fills the log.
                if "chat not found" in (e.description or "").lower():
                    log(f"dropping outbox #{msg_id}: chat not started yet")
                    drop_outbox(msg_id)
                    continue
                if parse_mode and attempts == 0:
                    log(f"outbox #{msg_id}: {e.description} -- retrying as plain text")
                    with db_lock:
                        db.execute(
                            "UPDATE outbox SET parse_mode=NULL, text=?, attempts=1 WHERE id=?",
                            (strip_tags(text), msg_id),
                        )
                        db.commit()
                    continue
                if attempts >= 2:
                    log(f"dropping outbox #{msg_id}: {e.description}")
                    drop_outbox(msg_id)
                    continue
            bump_attempts(msg_id)
            log(f"send error {e} (retry in {backoff}s)")
            shutdown.wait(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as e:
            bump_attempts(msg_id)
            log(f"send error: {e} (retry in {backoff}s)")
            shutdown.wait(backoff)
            backoff = min(backoff * 2, 30)


def deliver_outbox(chat_id, text, kind, parse_mode, markup, file_path):
    if kind == "doc":
        if not file_path or not os.path.exists(file_path):
            raise TelegramError(400, "file missing")
        with open(file_path, "rb") as f:
            blob = f.read()
        fields = {"chat_id": str(chat_id)}
        if text:
            fields["caption"] = text
        api_upload("sendDocument", fields, "document", os.path.basename(file_path), blob)
        return
    params = {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    if markup:
        params["reply_markup"] = json.loads(markup)
    api_call("sendMessage", params, timeout=30)


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def drop_outbox(msg_id):
    with db_lock:
        db.execute("DELETE FROM outbox WHERE id=?", (msg_id,))
        db.commit()


def bump_attempts(msg_id):
    with db_lock:
        db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (msg_id,))
        db.commit()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

START_TIME = time.time()


def recover_interrupted_jobs():
    """A restart mid-job leaves rows in 'running'. Put them back in the queue."""
    with db_lock:
        cur = db.execute("UPDATE jobs SET state='queued', started=NULL WHERE state='running'")
        db.commit()
    return cur.rowcount


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


class LocalAPIHandler(http.server.BaseHTTPRequestHandler):
    """Loopback-only helper the running agents call through curl.

    Any local process can reach the port, so the write endpoints need the
    per-boot key that only the agents are told about.
    """

    def log_message(self, *_args):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if parsed.path == "/health":
            return self._reply(200, {"ok": True, "running": running_jobs()})
        if params.get("key") != LOCAL_API_KEY:
            return self._reply(403, {"ok": False, "error": "forbidden"})
        if not CFG["allowed_user_ids"]:
            return self._reply(503, {"ok": False, "error": "no recipient"})
        chat_id = CFG["allowed_user_ids"][0]
        if parsed.path == "/send-msg":
            send(chat_id, (params.get("text") or "(bo'sh xabar)")[:3000])
            return self._reply(200, {"ok": True})
        if parsed.path == "/send-file":
            target = params.get("file")
            if not target:
                return self._reply(400, {"ok": False, "error": "file parameter missing"})
            send_user_file(chat_id, target)
            return self._reply(200, {"ok": True, "sent": target})
        return self._reply(404, {"ok": False, "error": "unknown endpoint"})


def local_api_server():
    try:
        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", CFG["local_api_port"]), LocalAPIHandler)
    except OSError as e:
        log(f"local API disabled: {e}")
        return
    log(f"local API: http://127.0.0.1:{CFG['local_api_port']}")
    srv.serve_forever(poll_interval=1)


def housekeeping():
    """Keep uploads, the jobs table and the WAL from creeping up on disk."""
    while not shutdown.is_set():
        cutoff = time.time() - 7 * 86400
        for path in glob.glob(os.path.join(UPLOAD_DIR, "*")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
        try:
            with db_lock:
                db.execute(
                    "DELETE FROM jobs WHERE state IN ('done','failed','cancelled') "
                    "AND created < ?",
                    (time.time() - 30 * 86400,),
                )
                db.commit()
                # WAL never shrinks on its own while the connection stays open.
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:
            log(f"housekeeping: {e}")
        shutdown.wait(6 * 3600)


def main():
    def on_signal(signum, _frame):
        log(f"signal {signum} -- shutting down")
        shutdown.set()
        do_cancel()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    requeued = recover_interrupted_jobs()
    if requeued:
        log(f"requeued {requeued} interrupted job(s)")
    log(f"starting, workdir={current_project()}, engines={ENGINE_ORDER}, "
        f"default={CFG['engine']}, allowed={CFG['allowed_user_ids']}")

    if CFG["notify_on_start"]:
        note = "🤖 Bot ishga tushdi."
        if requeued:
            note += f" {requeued} ta uzilib qolgan ish navbatga qaytarildi."
        for uid in CFG["allowed_user_ids"]:
            send(uid, note, markup=main_reply_kb())
            send(uid, "Nima qilamiz?", markup=main_menu())

    threads = [
        threading.Thread(target=poller, name="poller", daemon=True),
        threading.Thread(target=sender, name="sender", daemon=True),
        threading.Thread(target=housekeeping, name="housekeeping", daemon=True),
        threading.Thread(target=local_api_server, name="localapi", daemon=True),
    ]
    # One worker per engine: that is what lets both run at the same time.
    for engine in ENGINE_ORDER:
        threads.append(
            threading.Thread(target=worker, args=(engine,), name=f"worker-{engine}",
                             daemon=True))
    for t in threads:
        t.start()
    while not shutdown.is_set():
        shutdown.wait(1)
    log("stopped")


if __name__ == "__main__":
    main()
