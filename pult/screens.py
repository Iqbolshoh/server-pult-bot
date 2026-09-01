"""Every screen the bot renders, as HTML.

HTML, never MarkdownV2: a session id or a path with a dot or a dash silently
breaks MarkdownV2, and these screens are full of both.
"""

import glob
import os
import shutil
import subprocess
import time
import urllib.request

from .core import (BASE_DIR, CONFIG_PATH, DB_PATH, ENV_PATH, RULE, START_TIME, THIN_RULE, bar,
                   fmt_duration, fmt_tokens, fmt_when, h, quote, running_jobs, version)
from .config import CFG, config_problems
from .i18n import available_languages, current_language, language_name, t
from .db import db, db_lock, meta_get
from .telegram import api_try
from .engines import (ENGINES, ENGINE_ORDER, catalogue_age, cooldown_until, effort_label,
                      engine_choice_label, engine_label, engine_model, engine_model_label,
                      engine_models, engine_path, engine_version, entry_label, limit_info,
                      model_desc, session_key, session_usage)
from . import failover
from .projects import current_project, project_label

def engine_text():
    live = running_jobs()
    lines = [t("engine.title"), RULE]
    for eng in ENGINE_ORDER:
        mark = " ✅" if CFG["engine"] == eng else ""
        busy = (t("engine.busy", id=live[eng]) if eng in live else t("engine.idle"))
        lines.append(f"<b>{h(ENGINES[eng]['label'])}</b>{mark}")
        lines.append(f"   ⚙️ <b>{h(engine_model_label(eng))}</b> · {busy}")
        until = cooldown_until(eng)
        if until:
            lines.append("   " + t("engine.cooling", clock=fmt_when(until)))
        if not engine_path(eng):
            lines.append("   " + t("engine.not_installed", binary=h(CFG[ENGINES[eng]["bin_key"]])))
        lines.append("")
    lines += [t("engine.current", choice=h(engine_choice_label())), THIN_RULE,
              quote(t("engine.both_help")), t("engine.prefix_help")]
    return "\n".join(lines)
def model_text():
    lines = [t("model.title"), RULE]
    for eng in ENGINE_ORDER:
        lines.append(f"<b>{h(ENGINES[eng]['label'])}</b>")
        for entry in engine_models(eng):
            chosen = entry["id"] == engine_model(eng)
            desc = model_desc(entry) or ", ".join(entry.get("efforts") or []) or "—"
            bullet = "▸" if chosen else "·"
            name = f"<b>{h(entry_label(entry))}</b> ✅" if chosen else h(entry_label(entry))
            lines.append(f"{bullet} {name}\n   <i>{h(desc)}</i>")
        lines.append(THIN_RULE)
    age = catalogue_age()
    lines.append(t("model.catalogue_fresh", age=fmt_duration(age)) if age is not None
                 else t("model.catalogue_missing"))
    lines.append(t("model.effort_line", effort=h(effort_label())))
    return "\n".join(lines)
def effort_text():
    lines = [t("effort.title"), RULE]
    for eng in ENGINE_ORDER:
        lines.append(t("effort.engine_line", engine=engine_label(eng),
                       model=h(engine_model_label(eng)), effort=h(effort_label(eng))))
    lines += [THIN_RULE, t("effort.range"), "", quote(t("effort.explain"))]
    return "\n".join(lines)
def help_text():
    return t("help.body")
def start_text():
    confirm = t("word.on") if CFG["confirm_before_run"] else t("word.off")
    safe = t("word.on") if CFG["safe_mode"] else t("word.off")
    return t("start.body",
             project=h(project_label(current_project())),
             engine=h(engine_choice_label()),
             claude_label=h(engine_label("claude")),
             claude_model=h(engine_model_label("claude")),
             agy_label=h(engine_label("agy")),
             agy_model=h(engine_model_label("agy")),
             effort=h(effort_label()),
             confirm=confirm, safe=safe,
             chain=(t("word.on") if failover.enabled() else t("word.off")),
             uptime=fmt_duration(time.time() - START_TIME),
             version=h(version()))
def projects_text():
    current = current_project()
    live = [engine_label(e) for e in ENGINE_ORDER
            if meta_get(session_key(e, current), "")]
    return t("projects.body", project=h(project_label(current)), path=h(current),
             sessions=h(", ".join(live)) if live else t("projects.no_session"))
def limit_gauges():
    """The real fuel gauges, straight from the CLI's own rate_limit_event."""
    lines = []
    for eng in ENGINE_ORDER:
        lines.append(f"{engine_label(eng)}")
        info = limit_info(eng)
        windows = info.get("windows") or {}
        if not windows:
            lines.append("   " + t("limit.no_signal"))
        for name in ("five_hour", "seven_day"):
            window = windows.get(name)
            if not window:
                continue
            used = float(window.get("utilization") or 0)
            lines.append("   " + t(f"limit.window.{name}", gauge=bar(used),
                                   percent=int(round(used * 100)),
                                   clock=fmt_when(window.get("resets_at"))))
        until = cooldown_until(eng)
        if until:
            lines.append("   " + t("limit.cooling", clock=fmt_when(until)))
        elif info.get("at"):
            lines.append("   " + t("limit.measured", age=fmt_duration(time.time() - info["at"])))
    return lines
def limit_text():
    """Utilization first, token accounting second.

    A Max subscription bills nothing per token, so the number that decides whether
    work can continue is how much of the window is spent, not the spend itself.
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

    lines = [t("limit.title"), RULE] + limit_gauges()
    lines += [THIN_RULE, t("limit.chain", state=(t("word.on") if failover.enabled()
                                                 else t("word.off")))]
    lines += ["", t("limit.today", day=h(today))]
    stats = {eng: (0, 0, 0) for eng in ENGINE_ORDER}
    for eng, count, turns, tokens in per_engine:
        stats[eng] = (count, int(turns), int(tokens))
    day_total = 0
    for eng in ENGINE_ORDER:
        count, turns, tokens = stats[eng]
        day_total += tokens
        lines.append("   " + t("limit.engine_day", engine=engine_label(eng), jobs=count,
                               turns=turns, tokens=fmt_tokens(tokens)))
    lines.append("   " + t("limit.day_total", tokens=fmt_tokens(day_total)))

    lines += ["", t("limit.recent_days")]
    if not per_day:
        lines.append("   " + t("limit.no_jobs"))
    for day, count, turns, tokens, measured in per_day:
        amount = (t("limit.tokens", tokens=fmt_tokens(tokens)) if measured
                  else t("limit.tokens_unknown"))
        lines.append("   " + t("limit.day_line", day=h(day), jobs=count, turns=int(turns),
                               amount=amount))

    lines += ["", t("limit.context")]
    for eng in ENGINE_ORDER:
        used, idle = session_usage(eng, current_project())
        state = (t("limit.context_used", used=used, max=CFG["session_max_jobs"],
                   idle=fmt_duration(idle)) if used else t("limit.context_fresh"))
        lines.append("   " + t("limit.context_line", engine=engine_label(eng), state=state))

    lines += [THIN_RULE, t("limit.caps", turns=CFG["max_turns"],
                           minutes=CFG["job_timeout_sec"] // 60,
                           autocompact=CFG["autocompact"] or t("word.off")),
              t("limit.models", claude=h(engine_model_label("claude")),
                agy=h(engine_model_label("agy")), effort=h(effort_label())),
              "", quote(t("limit.footnote"))]
    return "\n".join(lines)
def fallback_text():
    lines = [t("fallback.title"), RULE,
             t("fallback.state", state=(t("word.on") if failover.enabled()
                                        else t("word.off"))), ""]
    steps = failover.chain()
    if not steps:
        lines.append(t("fallback.empty"))
    for index, step in enumerate(steps):
        lines.append(failover.describe_step(index, step))
    with db_lock:
        hopped = db.execute(
            "SELECT id,engine,step FROM jobs WHERE state IN ('queued','running') AND step>0"
        ).fetchall()
    if hopped:
        lines.append(THIN_RULE)
        for jid, eng, step in hopped:
            lines.append(t("fallback.live", id=jid, engine=engine_label(eng), step=step + 1))
    lines += ["", quote(t("fallback.explain"), expandable=True)]
    return "\n".join(lines)
def language_text():
    return t("language.body", current=language_name(current_language()),
             count=len(available_languages()))
def history_text():
    with db_lock:
        rows = db.execute(
            "SELECT id, prompt, engine FROM jobs ORDER BY id DESC LIMIT 15"
        ).fetchall()
    lines = [t("history.title"), RULE]
    if not rows:
        return "\n".join(lines + [t("history.empty")])
    for jid, prompt, engine in rows:
        lines.append(f"<b>#{jid}</b> {h(engine_label(engine))}\n   <i>{h(prompt[:100])}</i>")
    lines += [THIN_RULE, t("history.hint")]
    return "\n".join(lines)
def ls_text(target):
    """Directory listing -- no model tokens spent."""
    base = current_project()
    path = target if os.path.isabs(target) else os.path.join(base, target)
    path = os.path.abspath(path or base)
    if not os.path.isdir(path):
        return t("ls.not_found", path=h(path))
    try:
        entries = sorted(os.listdir(path))
    except OSError as e:
        return t("ls.unreadable", error=h(e))
    dirs = [f"📁 {n}" for n in entries if os.path.isdir(os.path.join(path, n))]
    files = [f"📄 {n}" for n in entries if not os.path.isdir(os.path.join(path, n))]
    body = "\n".join(dirs + files) or t("ls.empty")
    if len(body) > 3000:
        body = body[:3000] + "\n" + t("text.truncated")
    return f"📁 <b>{h(path)}</b>\n<pre>{h(body)}</pre>"
def settings_text():
    confirm = (t("settings.confirm.explain_on") if CFG["confirm_before_run"]
               else t("settings.confirm.explain_off"))
    safe = (t("settings.safe.explain_on") if CFG["safe_mode"]
            else t("settings.safe.explain_off"))
    return t("settings.body",
             confirm=confirm, safe=safe,
             engine=h(engine_choice_label()),
             claude_model=h(engine_model_label("claude")),
             agy_model=h(engine_model_label("agy")),
             effort=h(effort_label()),
             agy_flags=h(" ".join(CFG["agy_flags"]) or t("word.none")),
             mode=h(CFG["permission_mode"]),
             project=h(project_label(current_project())),
             minutes=CFG["job_timeout_sec"] // 60,
             chain=(t("word.on") if failover.enabled() else t("word.off")),
             language=language_name(current_language()),
             users=len(CFG["allowed_user_ids"]))
def status_text():
    with db_lock:
        live_rows = db.execute(
            "SELECT id,prompt,started,engine,step FROM jobs WHERE state='running' ORDER BY id"
        ).fetchall()
        queued = db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
        awaiting = db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        today = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens),0) FROM jobs WHERE created > ?",
            (time.time() - 86400,),
        ).fetchone()
    lines = [t("status.title"), RULE]
    if live_rows:
        for jid, prompt, started, eng, step in live_rows:
            elapsed = fmt_duration(time.time() - (started or time.time()))
            head = t("status.running", id=jid, engine=engine_label(eng), elapsed=elapsed)
            if step:
                head += " " + t("status.chain_step", step=step + 1)
            lines.append(head)
            lines.append(quote(h(prompt[:150])))
        if len(live_rows) > 1:
            lines.append(t("status.parallel"))
    else:
        lines.append(t("status.idle"))
    lines.append(THIN_RULE)
    with db_lock:
        per_engine = dict(db.execute(
            "SELECT engine, COUNT(*) FROM jobs WHERE state='queued' GROUP BY engine"
        ).fetchall())
    if queued:
        detail = ", ".join(f"{engine_label(e)} {per_engine[e]}" for e in sorted(per_engine))
        lines.append(t("status.queued", count=queued, detail=h(detail)))
    if awaiting:
        lines.append(t("status.awaiting", count=awaiting))
    if pending:
        lines.append(t("status.outbox", count=pending))
    lines.append(t("status.project", project=h(project_label(current_project()))))
    for eng in ENGINE_ORDER:
        if meta_get(session_key(eng, current_project()), ""):
            used, idle = session_usage(eng, current_project())
            lines.append(t("status.context", engine=engine_label(eng), used=used,
                           max=CFG["session_max_jobs"], idle=fmt_duration(idle)))
        else:
            lines.append(t("status.context_fresh", engine=engine_label(eng)))
        until = cooldown_until(eng)
        if until:
            lines.append(t("status.cooling", engine=engine_label(eng), clock=fmt_when(until)))
    lines.append(THIN_RULE)
    lines.append(t("status.day", jobs=today[0], tokens=fmt_tokens(today[1])))
    lines.append(t("status.uptime", uptime=fmt_duration(time.time() - START_TIME)))
    return "\n".join(lines)
def jobs_text():
    with db_lock:
        rows = db.execute(
            "SELECT id,state,created,finished,prompt,project,engine FROM jobs "
            "ORDER BY id DESC LIMIT 10"
        ).fetchall()
    icons = {"pending": "❓", "queued": "⏳", "running": "▶️",
             "done": "✅", "failed": "❌", "cancelled": "🛑"}
    out = [t("jobs.title"), RULE]
    if not rows:
        return "\n".join(out + [t("jobs.empty")])
    for jid, state, created, finished, prompt, project, engine in rows:
        dur = fmt_duration((finished or time.time()) - created)
        tag = project_label(project or "")
        out.append(f"{icons.get(state, '•')} <b>#{jid}</b> {h(engine_label(engine))} · "
                   f"<i>{dur} · {h(tag)}</i>")
        out.append(f"   <i>{h(prompt[:80])}</i>")
    out += [THIN_RULE, t("jobs.hint")]
    return "\n".join(out)
def sh(cmd, default="—", timeout=10):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip() or default
    except (OSError, subprocess.SubprocessError):
        return default
_service_cache = {"at": 0.0, "names": []}
def service_candidates():
    """Units worth asking about. The php-fpm glob is slow and rarely changes."""
    if time.time() - _service_cache["at"] > 600:
        names = ["nginx", "mysql", "mariadb", "postgresql", "redis-server", "supervisor"]
        names += [os.path.basename(p)[:-8]
                  for p in glob.glob("/lib/systemd/system/php*-fpm.service")]
        _service_cache.update(at=time.time(), names=names)
    return _service_cache["names"]
def server_text():
    """Instant server health -- plain shell, no model tokens burned.

    One shell call for the metrics and one systemctl call for every unit: this
    screen sits on the main menu, and it used to fork about a dozen times.
    """
    names = service_candidates()
    raw = sh(
        "echo UP $(uptime -p); "
        "echo LOAD $(cut -d' ' -f1-3 /proc/loadavg); "
        "free -m | awk '/^Mem:/{printf \"MEM %s / %s MB (%.0f%%)\\n\", $3, $2, $3*100/$2}'; "
        "free -m | awk '/^Swap:/{if($2>0) printf \"SWAP %s / %s MB\\n\", $3, $2; "
        "else print \"SWAP -\"}'; "
        "df -h / | awk 'NR==2{printf \"DISK %s / %s (%s)\\n\", $3, $2, $5}'; "
        "df -i / | awk 'NR==2{print \"INODE \" $5}'; "
        "supervisorctl status server-pult-bot 2>/dev/null "
        "| awk '{print \"BOT \" $1 \": \" $2}'",
        default="", timeout=15)
    facts = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        facts.setdefault(key, []).append(value.strip())
    one = lambda key: (facts.get(key) or ["—"])[0]

    states = sh("systemctl is-active " + " ".join(names) + " 2>/dev/null", default="")
    services = []
    for name, state in zip(names, states.splitlines()):
        if state and state != "inactive":
            services.append(f"{'🟢' if state == 'active' else '🔴'} {name}")

    with db_lock:
        per_engine = dict(db.execute(
            "SELECT engine, COUNT(*) FROM jobs WHERE state='running' GROUP BY engine"
        ).fetchall())

    lines = [t("server.title"), RULE,
             t("server.uptime", value=h(one("UP"))),
             t("server.load", value=h(one("LOAD")), cores=os.cpu_count() or 1),
             t("server.ram", value=h(one("MEM"))),
             t("server.swap", value=h(one("SWAP"))),
             t("server.disk", value=h(one("DISK")), inodes=h(one("INODE"))),
             THIN_RULE, t("server.services")]
    lines += ["  " + h(s) for s in services] or ["  —"]
    if facts.get("BOT"):
        lines += [THIN_RULE, t("server.bot")] + ["  " + h(b) for b in facts["BOT"]]
    lines += [THIN_RULE, t("server.jobs")]
    for eng in ENGINE_ORDER:
        count = per_engine.get(eng, 0)
        state = (t("server.jobs_running", count=count) if count else t("server.jobs_idle"))
        lines.append(f"  {h(engine_label(eng))} — {state}")
    return "\n".join(lines)
# Where each engine keeps the login the bot cannot work without.
LOGIN_ARTIFACTS = {
    "claude": ["~/.claude/.credentials.json"],
    "agy": ["~/.gemini/antigravity-cli/antigravity-oauth-token"],
}
def engine_logged_in(engine):
    """True / False / None when the engine keeps its login somewhere we do not know."""
    paths = LOGIN_ARTIFACTS.get(engine)
    if not paths:
        return None
    return any(os.path.exists(os.path.expanduser(p)) for p in paths)
def doctor_lines():
    """Why it is not working, answered without an SSH session."""
    ok, bad, warn = t("doctor.ok"), t("doctor.bad"), t("doctor.warn")
    lines = [t("doctor.title"), RULE, t("doctor.version", version=h(version())), ""]

    for problem in config_problems():
        lines.append(f"{bad} {h(problem)}")

    for eng in ENGINE_ORDER:
        path = engine_path(eng)
        if not path:
            lines.append(f"{bad} {engine_label(eng)}: " +
                         t("doctor.engine_missing", binary=h(CFG[ENGINES[eng]["bin_key"]])))
            continue
        lines.append(f"{ok} {engine_label(eng)}: {h(engine_version(eng) or '?')}")
        logged = engine_logged_in(eng)
        if logged is False:
            lines.append(f"   {bad} " + t("doctor.not_logged_in"))
        elif logged:
            lines.append(f"   {ok} " + t("doctor.logged_in"))
        until = cooldown_until(eng)
        if until:
            lines.append(f"   {warn} " + t("doctor.cooling", clock=fmt_when(until)))

    me = api_try("getMe", timeout=10)
    lines.append((ok if me else bad) + " " +
                 t("doctor.telegram", code=h("@" + me["username"] if me else "—")))

    free_disk = shutil.disk_usage("/").free // (1024 * 1024)
    lines.append((ok if free_disk > 500 else warn) + " " +
                 t("doctor.disk", mb=free_disk))
    inodes = sh("df -i / | awk 'NR==2{print $5}'", "")
    lines.append(f"{ok} " + t("doctor.inodes", value=h(inodes or "—")))

    writable = os.access(DB_PATH, os.W_OK) if os.path.exists(DB_PATH) else os.access(BASE_DIR, os.W_OK)
    lines.append((ok if writable else bad) + " " + t("doctor.db", path=h(DB_PATH)))
    for label, path in (("doctor.env", ENV_PATH), ("doctor.config", CONFIG_PATH)):
        lines.append((ok if os.path.exists(path) else bad) + " " + t(label, path=h(path)))

    port = CFG["local_api_port"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            alive = resp.status == 200
    except (OSError, ValueError):
        alive = False
    lines.append((ok if alive else warn) + " " + t("doctor.local_api", port=port))

    unit = sh("supervisorctl status server-pult-bot 2>/dev/null | awk '{print $2}'", "")
    if not unit:
        unit = sh("systemctl is-active server-pult-bot 2>/dev/null", "")
    lines.append((ok if unit in ("RUNNING", "active") else warn) + " " +
                 t("doctor.service", state=h(unit or "—")))

    lines.append(f"{ok} " + t("doctor.languages", count=len(available_languages()),
                              current=language_name(current_language())))

    age = catalogue_age()
    lines.append((f"{ok} " + t("doctor.catalogue", age=fmt_duration(age))) if age is not None
                 else (f"{warn} " + t("doctor.catalogue_missing")))
    return lines
def doctor_text():
    return "\n".join(doctor_lines())
