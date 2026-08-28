"""Inline and reply keyboards. Every label comes from the active locale."""

from .config import CFG
from .i18n import available_languages, current_language, language_name, t
from .engines import (ENGINES, ENGINE_ORDER, EFFORTS, engine_label, engine_model,
                      engine_models, entry_label, other_engine)
from . import failover
from .projects import current_project, list_projects, project_label

# The persistent bottom keyboard: eleven labels, three per row. Each one is a
# command wearing a nicer coat, so the dispatcher only ever sees commands.
KEYBOARD_ROWS = [
    [("btn.status", "/status"), ("btn.server", "/server"), ("btn.jobs", "/jobs")],
    [("btn.projects", "/projects"), ("btn.model", "/model"), ("btn.limit", "/limit")],
    [("btn.engine", "/engine"), ("btn.new", "/new"), ("btn.stop", "/stop")],
    [("btn.settings", "/settings"), ("btn.help", "/help")],
]
def label_commands():
    """label -> command, in every installed language.

    Telegram keeps showing the keyboard it was last sent, so a label the operator
    taps right after switching language must still resolve.
    """
    mapping = {}
    for lang in available_languages() or [current_language()]:
        for row in KEYBOARD_ROWS:
            for key, command in row:
                mapping[t(key, lang=lang)] = command
    return mapping
def main_reply_kb():
    return {
        "keyboard": [[{"text": t(key)} for key, _cmd in row] for row in KEYBOARD_ROWS],
        "resize_keyboard": True,
        "is_persistent": True,
    }
def kb(*rows):
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }
# There is deliberately no inline copy of the menu here. The bottom keyboard is
# always on screen and already carries these eleven entries, so an inline twin
# under every message was one row of buttons that could only repeat what the
# operator could already see. A "back" button now survives in one place only:
# the screens that hang off /settings, where it leads to a real parent.
def back_to_settings():
    return kb([(t("btn.back"), "settings")])
def job_menu(job_id):
    """The one button a running job needs. The engine is named in the banner
    above it, and everything else is a tap away on the bottom keyboard."""
    return kb([(t("btn.stop_job"), f"cancel:{job_id}")])
def confirm_menu(job_id):
    """Shown before anything runs, when confirm_before_run is on."""
    return kb(
        [(t("btn.run"), f"run:{job_id}"), (t("btn.plan_first"), f"plan:{job_id}")],
        [(t("btn.drop"), f"drop:{job_id}")],
    )
def result_menu(job_id, full=False, planned=False, engine=None):
    rows = []
    if planned:
        rows.append([(t("btn.exec_plan"), f"exec:{job_id}")])
    other = other_engine(engine) if engine else None
    if other:
        # Same task, other engine -- the cheapest way to compare the two.
        rows.append([(t("btn.try_other", engine=engine_label(other)), f"other:{job_id}")])
    row = [(t("btn.again"), f"again:{job_id}")]
    if full:
        row.insert(0, (t("btn.full_text"), f"full:{job_id}"))
    rows.append(row)
    return kb(*rows)
def engine_menu():
    rows = []
    for eng in ENGINE_ORDER:
        mark = "✅ " if CFG["engine"] == eng else ""
        rows.append([(f"{mark}{ENGINES[eng]['label']}", f"setengine:{eng}")])
    mark = "✅ " if CFG["engine"] == "both" else ""
    rows.append([(f"{mark}{t('engine.both')}", "setengine:both")])
    return kb(*rows)
def model_menu():
    rows = []
    for eng in ENGINE_ORDER:
        rows.append([(f"— {ENGINES[eng]['label']} —", "noop")])
        row = []
        for entry in engine_models(eng):
            mark = "✅ " if entry["id"] == engine_model(eng) else ""
            row.append((f"{mark}{entry_label(entry)}", f"setmodel:{eng}:{entry['id'] or '-'}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    rows.append([(t("btn.refresh_models"), "models_refresh")])
    rows.append([(t("btn.effort"), "effort")])
    return kb(*rows)
def effort_menu():
    """One dial for both engines; each engine clamps it to what it accepts."""
    rows, row = [], []
    for level in EFFORTS:
        mark = "✅ " if CFG["effort"] == level else ""
        row.append((f"{mark}{level}", f"effort:{level}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    mark = "✅ " if not CFG["effort"] else ""
    rows.append([(f"{mark}{t('effort.default')}", "effort:-")])
    rows.append([(t("btn.back"), "settings")])
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
    return kb(*rows)
def settings_menu():
    confirm = t("settings.confirm.on") if CFG["confirm_before_run"] else t("settings.confirm.off")
    safe = t("settings.safe.on") if CFG["safe_mode"] else t("settings.safe.off")
    return kb(
        [(confirm, "toggle_confirm")],
        [(safe, "toggle_safe")],
        [(t("btn.effort"), "effort"), (t("btn.fallback"), "fallback")],
        [(t("btn.language"), "language"), (t("btn.doctor"), "doctor")],
    )
def language_menu():
    rows = []
    for code in available_languages():
        mark = "✅ " if code == current_language() else ""
        rows.append([(f"{mark}{language_name(code)}", f"lang:{code}")])
    rows.append([(t("btn.back"), "settings")])
    return kb(*rows)
def fallback_menu():
    """The chain: toggle it, reorder it, and clear a cooldown that has gone stale."""
    toggle = t("fallback.on") if failover.enabled() else t("fallback.off")
    rows = [[(toggle, "fb:toggle")]]
    steps = failover.chain()
    for index, step in enumerate(steps):
        row = [(f"{index + 1}. {engine_label(step['engine'])} "
                f"{step['model'] or '—'}", "noop")]
        if index:
            row.append(("⬆️", f"fb:up:{index}"))
        if index < len(steps) - 1:
            row.append(("⬇️", f"fb:down:{index}"))
        rows.append(row)
    rows.append([(t("btn.clear_cooldowns"), "fb:cool")])
    rows.append([(t("btn.back"), "settings")])
    return kb(*rows)
def doctor_menu():
    return kb([(t("btn.recheck"), "doctor")], [(t("btn.back"), "settings")])
def onboarding_language_menu():
    rows = [[(language_name(code), f"ob:lang:{code}")] for code in available_languages()]
    return kb(*rows)
def onboarding_projects_menu():
    rows, row = [], []
    for i, path in enumerate(list_projects()[:12]):
        row.append((project_label(path), f"ob:dir:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([(t("btn.skip"), "ob:dir:-")])
    return kb(*rows)
def onboarding_engine_menu():
    rows = [[(ENGINES[eng]["label"], f"ob:engine:{eng}")] for eng in ENGINE_ORDER]
    rows.append([(t("engine.both"), "ob:engine:both")])
    return kb(*rows)
def onboarding_confirm_menu():
    return kb(
        [(t("onboard.confirm.keep"), "ob:confirm:1")],
        [(t("onboard.confirm.drop"), "ob:confirm:0")],
    )
