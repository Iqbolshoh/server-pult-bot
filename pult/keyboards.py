"""Inline and reply keyboards."""

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

from .config import CFG
from .engines import other_engine, engine_label, ENGINES, ENGINE_ORDER, engine_model
from .projects import current_project, list_projects, project_label

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
def result_menu(job_id, full=False, planned=False, engine=None):
    rows = []
    if planned:
        rows.append([("✅ Rejani bajar", f"exec:{job_id}")])
    other = other_engine(engine) if engine else None
    if other:
        # Same task, other engine -- the cheapest way to compare the two.
        rows.append([(f"🔀 {engine_label(other)}da ham sina", f"other:{job_id}")])
    row = [("🔁 Qayta", f"again:{job_id}"), ("🆕 Yangi suhbat", "new"),
           ("⬅️ Menyu", "menu")]
    if full:
        row.insert(0, ("📄 To'liq matn", f"full:{job_id}"))
    rows.append(row)
    return kb(*rows)
def engine_menu():
    rows = []
    for eng in ENGINE_ORDER:
        mark = "✅ " if CFG["engine"] == eng else ""
        rows.append([(f"{mark}{ENGINES[eng]['label']}", f"setengine:{eng}")])
    mark = "✅ " if CFG["engine"] == "both" else ""
    rows.append([(f"{mark}🤝 Ikkalasi (parallel)", "setengine:both")])
    rows.append([("⬅️ Menyu", "menu")])
    return kb(*rows)
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
def settings_menu():
    toggle = "🛡 Tasdiq: YOQILGAN" if CFG["confirm_before_run"] else "⚡ Tasdiq: O'CHIQ"
    return kb(
        [(toggle, "toggle_confirm")],
        [("🧩 Dvigatel", "engine"), ("🤖 Model", "model")],
        [("📈 Limit", "limit")],
        [("🗂 Loyihalar", "projects"), ("⬅️ Menyu", "menu")],
    )
