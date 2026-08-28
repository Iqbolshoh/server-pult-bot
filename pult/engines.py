"""The two AI engines: how a run is built, what its output means, and what it costs.

Everything engine-specific lives here. Adding a third engine is one entry in
ENGINES plus a build/events pair; nothing else in the bot knows their names.
"""

import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from .core import fmt_duration, h, log, signal_group
from .config import CFG
from .i18n import t
from .db import meta_get, meta_get_json, meta_set, meta_set_json

# Ordered weakest to strongest. Every engine supports a prefix of this list.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
_EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")
def clamp_effort(engine, effort, model=None):
    """The nearest effort this engine (and model) actually accepts, or None.

    Claude reaches xhigh and max; agy stops at high, and some of its models have
    no medium tier at all. One dial in the UI, clamped here rather than in the
    screens.
    """
    if not effort:
        return None
    allowed = list(ENGINES[engine].get("efforts") or [])
    info = model_info(engine, model) if model else {}
    if info.get("known") and info.get("efforts") is not None:
        # A model with an empty effort list takes no --effort at all.
        allowed = [e for e in allowed if e in info["efforts"]]
    if not allowed:
        return None
    if effort in allowed:
        return effort
    want = EFFORTS.index(effort) if effort in EFFORTS else 0
    return min(allowed, key=lambda e: (abs(EFFORTS.index(e) - want), EFFORTS.index(e)))
def _claude_build(prompt, session_id, mode, system_prompt, model, effort, workdir=None):
    cmd = [CFG["claude_bin"], "-p", "--output-format", "stream-json", "--verbose"]
    cmd += ["--resume", session_id] if session_id else ["--session-id", str(uuid.uuid4())]
    if model:
        cmd += ["--model", model]
    level = clamp_effort("claude", effort, model)
    if level:
        cmd += ["--effort", level]
    permission_mode = "plan" if mode == "plan" else CFG["permission_mode"]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if CFG["safe_mode"]:
        # Removes every command-running tool and confines the file tools to the
        # working directory: the tier that makes pointing this at production sane.
        cmd += ["--restricted"]
    if CFG["autocompact"]:
        # The CLI's own answer to a long context, instead of throwing the
        # conversation away when it gets expensive.
        cmd += ["--autocompact", str(CFG["autocompact"])]
    if CFG["stream_words"]:
        cmd += ["--include-partial-messages"]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    return cmd, prompt + "\n"
LIMIT_PHRASES = ("usage limit", "rate limit", "limit reached", "limit exceeded",
                 "quota", "resets at", "try again later")
def _limit_info(engine, info):
    """Normalise one rate_limit_event into the shape the rest of the bot stores."""
    windows = {}
    for name, window in (info.get("unifiedWindows") or {}).items():
        if not isinstance(window, dict):
            continue
        windows[name] = {
            "utilization": float(window.get("utilization") or 0.0),
            "resets_at": int(window.get("resetsAt") or 0),
        }
    return {
        "engine": engine,
        "status": info.get("status") or "allowed",
        "type": info.get("rateLimitType") or "",
        "resets_at": int(info.get("resetsAt") or 0),
        "windows": windows,
        "at": time.time(),
    }
def _claude_result_limited(event):
    """True only for a run that stopped because a window ran out.

    A cancel, a timeout, the turn cap or an ordinary failure must never look like
    this: those walk no chain, because hopping on them would burn every engine
    the operator owns on one broken prompt.
    """
    if not event.get("is_error"):
        return False
    if "429" in str(event.get("api_error_status") or ""):
        return True
    text = str(event.get("result") or "").lower()
    return any(phrase in text for phrase in LIMIT_PHRASES)
BUSY_PHRASES = ("overloaded", "capacity", "try again", "temporarily unavailable")
def _claude_result_busy(event):
    """The model, not the account, is unavailable -- worth another model, not a wait.

    This is the failure that makes a within-engine chain step real: opus is
    overloaded, sonnet is not, and the conversation survives the swap.
    """
    if not event.get("is_error"):
        return False
    status = str(event.get("api_error_status") or "")
    if any(code in status for code in ("529", "503", "500")):
        return True
    text = str(event.get("result") or "").lower()
    return any(phrase in text for phrase in BUSY_PHRASES)
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
    elif etype == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                out.append(("say", delta["text"]))
            elif delta.get("type") == "thinking_delta":
                out.append(("think", None))
    elif etype == "system" and event.get("subtype") == "status":
        out.append(("status", event.get("status") or ""))
    elif etype == "rate_limit_event":
        out.append(("limit", _limit_info("claude", event.get("rate_limit_info") or {})))
    elif etype == "result":
        out.append(("result", {
            "text": event.get("result") or "",
            "cost": event.get("total_cost_usd"),
            "turns": event.get("num_turns"),
            # input + output + both cache buckets; nested dicts are skipped.
            "tokens": sum(v for v in (event.get("usage") or {}).values()
                          if isinstance(v, int)),
            "error": bool(event.get("is_error")),
            "limited": _claude_result_limited(event),
            "busy": _claude_result_busy(event),
        }))
    return out
def _strip_flag_pair(flags, name):
    """Remove '--name value' from a flag list so a later --name can win."""
    out, skip = [], False
    for item in flags:
        if skip:
            skip = False
            continue
        if item == name:
            skip = True
            continue
        if item.startswith(name + "="):
            continue
        out.append(item)
    return out
def agy_effort_for(model, effort):
    """agy *requires* --effort for any model that has effort tiers.

    Measured: `agy --model gemini-3.7-flash` with no --effort exits with
    "invalid model selection ... requires --effort (available: low, medium, high)".
    So an empty dial cannot mean "leave it to the engine" here -- it means high,
    or the strongest tier the model actually offers.
    """
    level = clamp_effort("agy", effort, model)
    if level:
        return level
    offered = model_info("agy", model).get("efforts")
    if not offered:
        return None
    return "high" if "high" in offered else offered[-1]
def _agy_build(prompt, session_id, mode, system_prompt, model, effort, workdir=None):
    cmd = [CFG["agy_bin"], "--model", model]
    level = agy_effort_for(model, effort)
    if level:
        cmd += ["--effort", level]
    if workdir:
        # Measured the hard way: agy ignores the process working directory and
        # writes into its own default project scratch unless the directory is
        # explicitly added to the workspace. Without this the bot's project
        # picker did nothing at all on this engine.
        cmd += ["--add-dir", workdir]
    if session_id:
        cmd += ["--conversation", session_id]
    flags = list(CFG["agy_flags"])
    if mode == "plan":
        # agy has a real plan mode; use it instead of merely asking nicely, and
        # drop whatever --mode the operator's flags set so this one wins.
        flags = _strip_flag_pair(flags, "--mode") + ["--mode", "plan"]
    cmd += flags
    if CFG["safe_mode"]:
        cmd += ["--sandbox"]
    cmd += ["--output-format", "stream-json",
            "--print-timeout", agy_print_timeout()]
    # agy takes the prompt as an argument, not on stdin.
    cmd += ["-p", (system_prompt + "\n\n" if system_prompt else "") + prompt]
    return cmd, None
def agy_print_timeout():
    """Keep agy's own deadline just inside the bot's, so the bot reports first."""
    seconds = max(60, int(CFG["job_timeout_sec"]) - 30)
    return f"{seconds}s"
# Only these say "the account is out of fuel". Everything else non-SUCCESS is an
# ordinary failure -- no real quota sample has been captured from agy yet, and
# guessing wide would hide real errors behind an endless engine shuffle.
AGY_LIMIT_STATUSES = {"RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"}
def _agy_events(event):
    out = []
    name = event.get("event")
    if name == "init":
        # The conversation id arrives on the first line as well as in the result.
        # Take it here too: a run that is killed or times out never reaches a
        # result event, and without this its conversation could not be resumed.
        if event.get("conversation_id"):
            out.append(("session", event["conversation_id"]))
    elif name == "step_update":
        su = event.get("step_update") or {}
        if su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
            out.append(("turn", None))
            out.append(("tool", (su.get("tool_name", "tool"),
                                 (su.get("tool_info") or {}).get("parameters") or {})))
        elif su.get("step_type") == "agent_response" and su.get("usage"):
            # agy reports usage per step; keep the running total so a killed run
            # still accounts for what it spent.
            out.append(("usage", int((su["usage"] or {}).get("total_tokens") or 0)))
    elif name == "result":
        r = event.get("result") or {}
        if r.get("conversation_id"):
            out.append(("session", r["conversation_id"]))
        status = r.get("status")
        text = (r.get("response") or "").strip()
        error = str(r.get("error") or "")
        if status and status != "SUCCESS":
            text = f"[{status}] " + (error or text)
        limited = bool(status in AGY_LIMIT_STATUSES
                       or any(s in error.upper() for s in AGY_LIMIT_STATUSES))
        out.append(("result", {
            "text": text,
            "cost": None,
            "turns": r.get("num_turns"),
            "tokens": (r.get("usage") or {}).get("total_tokens"),
            "error": bool(status and status != "SUCCESS"),
            "limited": limited,
            "busy": False,
        }))
    return out
# Fallback catalogue: what the buyer sees before `agy models` has ever answered.
AGY_MODELS_FALLBACK = [
    {"id": "gemini-3.7-flash", "label": "⚡ Gemini 3.7 Flash",
     "desc_key": "model.agy.flash37", "efforts": ["low", "medium", "high"]},
    {"id": "gemini-3.6-flash", "label": "🔥 Gemini 3.6 Flash",
     "desc_key": "model.agy.flash36", "efforts": ["low", "medium", "high"]},
    {"id": "gemini-3.5-flash", "label": "🌤 Gemini 3.5 Flash",
     "desc_key": "model.agy.flash35", "efforts": ["low", "medium", "high"]},
    {"id": "gemini-3.1-pro", "label": "🧠 Gemini 3.1 Pro",
     "desc_key": "model.agy.pro31", "efforts": ["low", "high"]},
    {"id": "claude-sonnet-4-6", "label": "🤖 Claude Sonnet 4.6",
     "desc_key": "model.agy.sonnet", "efforts": []},
    {"id": "claude-opus-4-6-thinking", "label": "🦾 Claude Opus 4.6",
     "desc_key": "model.agy.opus", "efforts": []},
    {"id": "gpt-oss-120b", "label": "🟢 GPT-OSS 120B",
     "desc_key": "model.agy.oss", "efforts": ["medium"]},
]
CLAUDE_MODELS = [
    {"id": "fable", "label": "🚀 Fable", "desc_key": "model.claude.fable", "efforts": EFFORTS},
    {"id": "opus", "label": "🦾 Opus", "desc_key": "model.claude.opus", "efforts": EFFORTS},
    {"id": "sonnet", "label": "⚡ Sonnet", "desc_key": "model.claude.sonnet", "efforts": EFFORTS},
    {"id": "haiku", "label": "🌤 Haiku", "desc_key": "model.claude.haiku", "efforts": EFFORTS},
    {"id": "", "label": "🎛", "label_key": "model.default",
     "desc_key": "model.claude.default", "efforts": EFFORTS},
]
CATALOGUE_KEY = "catalogue:agy"
_MODEL_ICONS = (("pro", "🧠"), ("opus", "🦾"), ("sonnet", "🤖"), ("gemini", "⚡"),
                ("gpt", "🟢"), ("claude", "🤖"))
def _catalogue_icon(model_id):
    for needle, icon in _MODEL_ICONS:
        if needle in model_id:
            return icon
    return "🔹"
def parse_agy_catalogue(output):
    """`agy models` -> one entry per base model, with the efforts it offers.

    The CLI lists effort-suffixed ids ('gemini-3.7-flash-high'); the bot keeps
    the base id in config and passes --effort separately, so one effort dial can
    drive both engines. Models with no suffix take no effort at all.
    """
    models, seen = [], {}
    for line in (output or "").splitlines():
        if "\t" not in line:
            continue
        model_id, _, label = line.partition("\t")
        model_id, label = model_id.strip(), label.strip()
        if not model_id or " " in model_id:
            continue
        match = _EFFORT_SUFFIX.search(model_id)
        effort = match.group(1) if match else None
        base = model_id[: match.start()] if match else model_id
        label = re.sub(r"\s*\((?:High|Medium|Low|XHigh|Max)\)\s*$", "", label, flags=re.I)
        if base not in seen:
            seen[base] = {"id": base, "label": f"{_catalogue_icon(base)} {label or base}",
                          "desc": "", "efforts": []}
            models.append(seen[base])
        if effort and effort not in seen[base]["efforts"]:
            seen[base]["efforts"].append(effort)
    for entry in models:
        entry["efforts"].sort(key=EFFORTS.index)
    return models
def run_engine_command(argv, timeout):
    """Run a short engine command safely. (stdout, ok) -- never hangs the bot.

    subprocess.run(timeout=...) kills the child but keeps waiting on a pipe a
    grandchild still holds, which is how `agy models` wedged the bot for minutes
    under supervisor. Own process group, killed as a group, stdin closed.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        log(f"{argv[0]}: {e}")
        return "", False
    try:
        out, _err = proc.communicate(timeout=timeout)
        return out, proc.returncode == 0
    except subprocess.TimeoutExpired:
        signal_group(proc, signal.SIGKILL)
        try:
            out, _err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        log(f"{' '.join(argv)} timed out after {timeout}s")
        return out, False
def fetch_agy_catalogue(timeout=90):
    """Ask the engine what the buyer's own account can reach. None on failure."""
    binary = shutil.which(CFG["agy_bin"])
    if not binary:
        return None
    out, ok = run_engine_command([binary, "models"], timeout)
    models = parse_agy_catalogue(out)
    if not models:
        log(f"agy models returned nothing usable (ok={ok})")
        return None
    return models
_catalogue_lock = threading.Lock()
def refresh_catalogue(force=False):
    """Re-read the live model list at most once per catalogue_refresh_sec.

    Serialised: start-up and housekeeping both ask, and two `agy models` calls at
    boot is one more than anybody needs.
    """
    with _catalogue_lock:
        cached = meta_get_json(CATALOGUE_KEY) or {}
        age = time.time() - float(cached.get("at") or 0)
        if not force and cached.get("models") and age < CFG["catalogue_refresh_sec"]:
            return False
        models = fetch_agy_catalogue()
        if not models:
            return False
        meta_set_json(CATALOGUE_KEY, {"at": time.time(), "models": models})
        log(f"model catalogue: {len(models)} agy models")
        return True
def agy_models():
    cached = (meta_get_json(CATALOGUE_KEY) or {}).get("models")
    return cached or AGY_MODELS_FALLBACK
def catalogue_age():
    """Seconds since the live catalogue was last read, or None if never."""
    at = (meta_get_json(CATALOGUE_KEY) or {}).get("at")
    return time.time() - float(at) if at else None
def engine_models(engine):
    getter = ENGINES[engine]["models"]
    return getter() if callable(getter) else getter
def model_info(engine, model_id):
    for entry in engine_models(engine):
        if entry["id"] == model_id:
            return dict(entry, known=True)
    return {"id": model_id, "label": model_id or "?", "desc": "", "efforts": None,
            "known": False}
def entry_label(entry):
    """A catalogue entry's button text; only the 'engine default' row is translated."""
    return t(entry["label_key"]) if entry.get("label_key") else entry["label"]
def model_label(engine, model_id):
    return entry_label(model_info(engine, model_id))
def model_desc(entry):
    return t(entry["desc_key"]) if entry.get("desc_key") else (entry.get("desc") or "")
ENGINES = {
    "claude": {
        "label": "🟠 Claude",
        "bin_key": "claude_bin",
        "build": _claude_build,
        "events": _claude_events,
        "models": lambda: CLAUDE_MODELS,
        "model_key": "model",
        "efforts": EFFORTS,
        "plan_hint_key": "prompt.plan_hint.claude",
    },
    "agy": {
        "label": "🔵 Antigravity",
        "bin_key": "agy_bin",
        "build": _agy_build,
        "events": _agy_events,
        "models": agy_models,
        "model_key": "agy_model",
        "efforts": ["low", "medium", "high"],
        "plan_hint_key": "prompt.plan_hint.agy",
    },
}
ENGINE_ORDER = ["claude", "agy"]
def other_engine(engine):
    """The engine a result was NOT run on -- for the compare-with button."""
    rest = [e for e in ENGINE_ORDER if e != engine]
    return rest[0] if rest else None
def engine_label(engine):
    return ENGINES.get(engine, {}).get("label", engine)
def engine_banner(engine):
    """Loud first line for every job message, so two results never blur together."""
    label = engine_label(engine)
    icon, _, name = label.partition(" ")
    return f"{icon} <b>{h(name.upper())}</b>"
def engine_bin(engine):
    return CFG[ENGINES[engine]["bin_key"]]
def engine_path(engine):
    """Absolute path of the engine binary, or None when it is not installed."""
    return shutil.which(engine_bin(engine))
def engine_version(engine, timeout=20):
    path = engine_path(engine)
    if not path:
        return None
    out, _ok = run_engine_command([path, "--version"], timeout)
    lines = (out or "").strip().splitlines()
    return lines[0] if lines else None
def engine_model(engine):
    return CFG[ENGINES[engine]["model_key"]]
def set_engine_model(engine, model_id):
    CFG[ENGINES[engine]["model_key"]] = model_id
def resolve_model(name):
    """(engine, model id) for a name typed after /model.

    Looked up in each engine's own list -- "claude-sonnet-4-6" is an Antigravity
    model despite the name, so guessing from the prefix got it wrong.
    """
    if name in ("-", "default", "odatiy", "standart"):
        return "claude", ""
    for match in (lambda mid: mid == name, lambda mid: mid.startswith(name)):
        for eng in ENGINE_ORDER:
            for entry in engine_models(eng):
                if entry["id"] and match(entry["id"]):
                    return eng, entry["id"]
    # Unknown name: the claude CLI accepts aliases we do not list, agy does not.
    return "claude", name
def engine_choice_label():
    return t("engine.both.short") if CFG["engine"] == "both" else engine_label(CFG["engine"])
def engine_model_label(engine):
    return model_label(engine, engine_model(engine))
def effort_label(engine=None):
    """What the dial reads as -- including the clamp, and agy's forced default.

    Without an engine this is the dial itself. With one it is what that engine
    will actually be given, which is not the same thing: agy has no "let the
    engine decide", so an empty dial still resolves to a real level there.
    """
    effort = CFG["effort"]
    if not engine:
        return effort or t("effort.default")
    model = engine_model(engine)
    level = (agy_effort_for(model, effort) if engine == "agy"
             else clamp_effort(engine, effort, model))
    if not level:
        return t("effort.unsupported") if effort else t("effort.default")
    if not effort:
        return t("effort.auto", level=level)
    return level if level == effort else f"{effort} → {level}"
LIMIT_KEY = "limit:{}"
COOLDOWN_KEY = "cooldown:{}"
def remember_limit(engine, info):
    """Latest fuel gauge for an engine. Drives /limit and the failover chain."""
    meta_set_json(LIMIT_KEY.format(engine), info)
    if info.get("status") not in ("allowed", "", None):
        set_cooldown(engine, info.get("resets_at") or 0)
def limit_info(engine):
    return meta_get_json(LIMIT_KEY.format(engine)) or {}
def set_cooldown(engine, resets_at):
    """Park an engine until its window refills. Shared by every queued job."""
    resets_at = int(resets_at or 0) or int(time.time() + 3600)
    meta_set(COOLDOWN_KEY.format(engine), resets_at)
    log(f"{engine} cooling down until {time.strftime('%H:%M', time.localtime(resets_at))}")
    return resets_at
def cooldown_until(engine):
    """Unix time the engine becomes usable again, or 0 when it is ready now."""
    until = int(float(meta_get(COOLDOWN_KEY.format(engine), 0) or 0))
    return until if until > time.time() else 0
def clear_cooldown(engine):
    meta_set(COOLDOWN_KEY.format(engine), 0)
def engine_nearly_dry(engine):
    """True once the five-hour window is close enough to spent to start elsewhere."""
    info = limit_info(engine)
    window = (info.get("windows") or {}).get("five_hour") or {}
    return float(window.get("utilization") or 0) >= CFG["limit_warn_utilization"]
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
        return "", t("session.reset.idle", idle=fmt_duration(idle))
    if job_limit and used >= job_limit:
        log(f"{engine} session for {workdir} used {used} jobs -- fresh context")
        clear_session(engine, workdir)
        return "", t("session.reset.jobs", used=used)
    return sid, ""
def remember_session(engine, workdir, session_id):
    meta_set(session_key(engine, workdir), session_id)
    meta_set(session_key(engine, workdir) + ":last", time.time())
    used, _ = session_usage(engine, workdir)
    meta_set(session_key(engine, workdir) + ":jobs", used + 1)
