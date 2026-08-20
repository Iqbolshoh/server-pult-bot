"""The two AI engines and their per-project conversation state."""

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

from .core import fmt_duration, h, log
from .config import CFG
from .db import meta_get, meta_set

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
def _agy_build(prompt, session_id, mode, system_prompt):
    model = CFG["agy_model"]
    cmd = [CFG["agy_bin"], "--model", model]
    effort = agy_model_info(model)["effort"]
    if effort:
        cmd += ["--effort", effort]
    if session_id:
        cmd += ["--conversation", session_id]
    flags = list(CFG["agy_flags"])
    if mode == "plan":
        # agy has a real plan mode; use it instead of merely asking nicely, and
        # drop whatever --mode the operator's flags set so this one wins.
        flags = _strip_flag_pair(flags, "--mode") + ["--mode", "plan"]
    cmd += flags
    cmd += ["--output-format", "stream-json",
            "--print-timeout", agy_print_timeout()]
    # agy takes the prompt as an argument, not on stdin.
    cmd += ["-p", (system_prompt + "\n\n" if system_prompt else "") + prompt]
    return cmd, None
def agy_print_timeout():
    """Keep agy's own deadline just inside the bot's, so the bot reports first."""
    seconds = max(60, int(CFG["job_timeout_sec"]) - 30)
    return f"{seconds}s"
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
        "label": "🟠 Claude",
        "build": _claude_build,
        "events": _claude_events,
        "models": CLAUDE_MODELS_LIST,
        "model_key": "model",
        "plan_hint": (" Hozir REJA rejimidasan: hech narsani o\'zgartirma, faqat nima "
                      "qilishingni qisqa qadamlar bilan tushuntir va xavfli joylarni ayt."),
    },
    "agy": {
        "label": "🔵 Antigravity",
        "build": _agy_build,
        "events": _agy_events,
        "models": AGY_MODELS,
        "model_key": "agy_model",
        "plan_hint": (" REJA REJIMI: hech narsani o\'zgartirma, fayl yozma, buyruq bajarma. "
                      "Faqat nima qilishingni qisqa qadamlar bilan tushuntir."),
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
    label = ENGINES.get(engine, {}).get("label", engine)
    icon, _, name = label.partition(" ")
    return f"{icon} <b>{h(name.upper())}</b>"
def engine_model(engine):
    return CFG[ENGINES[engine]["model_key"]]
def resolve_model(name):
    """(engine, model id) for a name typed after /model.

    Looked up in each engine's own list -- "claude-sonnet-4-6" is an Antigravity
    model despite the name, so guessing from the prefix got it wrong.
    """
    if name in ("-", "default", "odatiy"):
        return "claude", ""
    for match in (lambda mid: mid == name, lambda mid: mid.startswith(name)):
        for eng in ENGINE_ORDER:
            for mid, _label, _desc, _effort in ENGINES[eng]["models"]:
                if mid and match(mid):
                    return eng, mid
    # Unknown name: the claude CLI accepts aliases we do not list, agy does not.
    return "claude", name
def engine_choice_label():
    return "🤝 Ikkalasi" if CFG["engine"] == "both" else engine_label(CFG["engine"])
def engine_model_label(engine):
    if engine == "agy":
        return agy_model_info(CFG["agy_model"])["label"]
    return CFG["model"] or "odatiy"
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
