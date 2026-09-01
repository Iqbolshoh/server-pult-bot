"""The job queue: one worker per engine, the chain walk, and result delivery."""

import json
import os
import signal
import subprocess
import threading
import time

from .core import (PREVIEW_CHARS, RULE, RUNNING, TELEGRAM_MAX_CHARS, UPLOAD_DIR, audit, card,
                   fmt_clock, fmt_duration, fmt_tokens, h, log, queue_ready, quote, run_lock,
                   shutdown, signal_group)
from .config import CFG, LOCAL_API_KEY
from .i18n import t
from .db import db, db_lock
from .telegram import api_try, send, send_document
from .engines import (ENGINES, ENGINE_ORDER, clear_session, cooldown_until, engine_banner,
                      engine_bin, engine_label, engine_model, engine_path, model_label,
                      remember_limit, remember_session, set_cooldown, take_session)
from . import failover
from .projects import current_project, project_label
from .keyboards import confirm_menu, job_menu, result_menu

def local_api_hint():
    """Told to every agent: how to reach the operator while a job is still running."""
    base = f"http://127.0.0.1:{CFG['local_api_port']}"
    return t("prompt.local_api", base=base, api_key=LOCAL_API_KEY)
def system_prompt():
    """The operator's own prompt, or the locale's default, plus the local API hint."""
    return (CFG["system_prompt"] or t("prompt.system")) + local_api_hint()
def append_to_job(job_id, path):
    """Add another album photo to a job that has not started yet."""
    with db_lock:
        row = db.execute("SELECT prompt,state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row[1] != "queued":
            return False
        db.execute(
            "UPDATE jobs SET prompt=? WHERE id=?",
            (row[0] + "\n" + t("job.extra_file", path=path), job_id),
        )
        db.commit()
    return True
def start_job(chat_id, prompt, note="", mode=None, approved=False, engine=None):
    """Store a task. Unless pre-approved, it waits for a button press.

    engine="both" queues the same task on every engine at once -- they have
    separate workers, so the two really do run side by side.
    """
    engine = engine or CFG["engine"]
    if engine == "both":
        return [start_job(chat_id, prompt, f"🤝 {i}/{len(ENGINE_ORDER)}" if not note
                          else f"{note} · 🤝 {i}/{len(ENGINE_ORDER)}",
                          mode, approved, eng)
                for i, eng in enumerate(ENGINE_ORDER, 1)]

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
    if state == "queued":
        queue_ready(engine).set()
    audit(f"job#{job_id} [{state}] [{engine}] [{workdir}] {prompt[:200]!r}")

    banner = engine_banner(engine)
    if needs_ok:
        send(chat_id, card(
            t("job.confirm.head", banner=banner, id=job_id),
            RULE,
            t("job.project_line", project=h(project_label(workdir))),
            "",
            quote(h(prompt[:900]), expandable=len(prompt) > 220),
            "",
            t("job.confirm.ask"),
        ), markup=confirm_menu(job_id), parse_mode="HTML")
        return job_id

    with db_lock:
        ahead = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='queued' AND engine=? AND id<?",
            (engine, job_id),
        ).fetchone()[0]
    head = t("job.accepted", banner=banner, id=job_id)
    if note:
        head += f" · <i>{h(note)}</i>"
    send(chat_id, card(
        head,
        RULE,
        t("job.project_line", project=h(project_label(workdir))),
        t("job.plan_only") if mode == "plan" else None,
        t("job.ahead", count=ahead) if ahead else None,
        "",
        quote(h(prompt[:300]) + ("…" if len(prompt) > 300 else "")),
    ), markup=job_menu(job_id), parse_mode="HTML")
    return job_id
def approve_job(job_id, mode):
    """Move a pending job into the queue. Returns a status line for the user.

    A confirm card that was never pressed keeps its button forever, so a task
    written days ago could still be launched against a server that has changed
    underneath it. Past `pending_expiry_sec` the card is spent, not armed.
    """
    with db_lock:
        row = db.execute(
            "SELECT state,engine,created FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            return t("job.not_found", id=job_id)
        if row[0] != "pending":
            return t("job.already_state", id=job_id, state=h(row[0]))
        waited = time.time() - (row[2] or 0)
        if CFG["pending_expiry_sec"] and waited > CFG["pending_expiry_sec"]:
            db.execute("UPDATE jobs SET state='cancelled', finished=? WHERE id=?",
                       (time.time(), job_id))
            db.commit()
            audit(f"job#{job_id} expired unapproved after {int(waited)}s")
            return t("job.expired", id=job_id, waited=fmt_duration(waited))
        db.execute("UPDATE jobs SET state='queued', mode=? WHERE id=?", (mode, job_id))
        db.commit()
    queue_ready(row[1]).set()
    audit(f"job#{job_id} approved mode={mode}")
    return t("job.approved.plan" if mode == "plan" else "job.approved.run", id=job_id)
def send_user_file(chat_id, target):
    """Push any file on the server to Telegram."""
    base = current_project()
    path = target if os.path.isabs(target) else os.path.join(base, target)
    path = os.path.abspath(path)
    if not os.path.exists(path):
        send(chat_id, t("file.not_found", path=h(path)), parse_mode="HTML")
        return
    if os.path.isdir(path):
        send(chat_id, t("file.is_dir"), parse_mode="HTML")
        return
    size = os.path.getsize(path)
    if size > 50 * 1024 * 1024:
        send(chat_id, t("file.too_big", mb=size // 1048576), parse_mode="HTML")
        return
    audit(f"sent file {path!r}")
    send_document(chat_id, path, caption=path)
def run_shell(chat_id, command):
    """Direct shell escape hatch -- fast answers without invoking the model."""
    audit(f"shell {command!r}")
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=current_project(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out = proc.communicate(timeout=CFG["shell_timeout_sec"])[0]
        status = "✅" if proc.returncode == 0 else f"❌ exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        signal_group(proc, signal.SIGKILL)
        out = (proc.communicate()[0] or "") + "\n" + t("shell.timed_out")
        status = "⏰"
    out = (out or "").strip() or t("shell.no_output")
    if len(out) > 3000:
        out = out[:3000] + "\n" + t("text.truncated")
    send(chat_id, f"{status} <code>{h(command[:120])}</code>\n<pre>{h(out)}</pre>",
         parse_mode="HTML")
def deliver_full_result(chat_id, job_id):
    with db_lock:
        row = db.execute(
            "SELECT state,result,prompt FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    if not row:
        send(chat_id, t("job.not_found", id=job_id))
        return
    state, result, prompt = row
    body = result or t("result.empty")
    inline = card(t("result.stored", id=job_id, state=h(state)), RULE, quote(h(body)))
    if len(inline) <= TELEGRAM_MAX_CHARS:
        send(chat_id, inline, parse_mode="HTML")
        return
    path = os.path.join(UPLOAD_DIR, f"job-{job_id}.md")
    with open(path, "w") as f:
        f.write(t("result.file_body", id=job_id, state=state, prompt=prompt, body=body))
    send_document(chat_id, path,
                  caption=t("result.file_caption_len", id=job_id, chars=len(body)))
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
            signal_group(proc, signal.SIGTERM)
            threading.Timer(5, lambda pr=proc: pr.poll() is None
                            and signal_group(pr, signal.SIGKILL)).start()
            stopped.append(f"#{info['job_id']} {engine_label(eng)}")
    if stopped:
        return t("cancel.stopping", jobs=", ".join(stopped))
    if job_id is not None:
        with db_lock:
            cur = db.execute(
                "UPDATE jobs SET state='cancelled', finished=? "
                "WHERE id=? AND state IN ('queued','pending')",
                (time.time(), job_id),
            )
            db.commit()
        if cur.rowcount:
            return t("cancel.dropped", id=job_id)
        return t("cancel.already_done", id=job_id)
    return t("cancel.nothing")
_waiting_announced = set()
def worker(engine):
    """One thread per engine. Jobs stay serialised within an engine, on purpose."""
    ready = queue_ready(engine)
    while not shutdown.is_set():
        # Cleared before the read, so a job queued from here on sets it again and
        # cannot fall into the gap between the query and the wait.
        ready.clear()
        with db_lock:
            row = db.execute(
                "SELECT id,chat_id,prompt,project,mode,step,handover FROM jobs "
                "WHERE state='queued' AND engine=? ORDER BY id LIMIT 1",
                (engine,),
            ).fetchone()
        if not row:
            ready.wait(5)
            continue
        job_id, chat_id, prompt, project, mode, step, handover = row
        if not preflight(job_id, chat_id, engine, step or 0):
            shutdown.wait(5)
            continue
        with db_lock:
            db.execute(
                "UPDATE jobs SET state='running', started=? WHERE id=?", (time.time(), job_id)
            )
            db.commit()
        _waiting_announced.discard(job_id)
        try:
            run_job(job_id, chat_id, prompt, project or CFG["workdir"], engine, mode=mode,
                    step=step or 0, handover=handover)
        except Exception as e:
            log(f"job #{job_id} ({engine}) crashed: {e}")
            finish_job(job_id, "failed", f"internal error: {e}", -1)
            send(chat_id, t("job.internal_error", banner=engine_banner(engine), id=job_id,
                            error=h(e)),
                 markup=result_menu(job_id, engine=engine), parse_mode="HTML")
def preflight(job_id, chat_id, engine, step):
    """Decide whether this job may start on this engine right now.

    Cheapest hop there is: an engine with a stored reset time in the future is
    skipped before a single token is spent.
    """
    if not engine_path(engine):
        if hop_job(job_id, chat_id, engine, step, "missing"):
            return False
        finish_job(job_id, "failed", f"{engine_bin(engine)} is not installed", -1)
        send(chat_id, t("job.engine_missing", banner=engine_banner(engine), id=job_id,
                        binary=h(engine_bin(engine))), parse_mode="HTML")
        return False
    until = cooldown_until(engine)
    if until:
        if hop_job(job_id, chat_id, engine, step, "cooldown", until):
            return False
        if job_id not in _waiting_announced:
            _waiting_announced.add(job_id)
            send(chat_id, t("failover.waiting", banner=engine_banner(engine), id=job_id,
                            clock=fmt_clock(until)), parse_mode="HTML")
        return False
    if failover.should_step_aside(engine) and hop_job(job_id, chat_id, engine, step,
                                                      "nearly_dry"):
        return False
    return True
def hop_job(job_id, chat_id, engine, step, reason, resets_at=0):
    """Move a job to the next usable step of the chain. True when it moved.

    Walked forward only, once per step: a chain that could revisit a step would
    shuffle a broken prompt between every engine the operator pays for.
    """
    if not failover.enabled():
        return False
    # A quota or a cooldown takes the whole engine out; an overloaded model does
    # not, so a busy hop may land on the same engine with a different model.
    exclude = () if reason == "busy" else (engine,)
    index, step_cfg = failover.next_step((step or 0) + 1, exclude_engines=exclude)
    if index is None:
        return False
    target = step_cfg["engine"]
    crossed = target != engine
    with db_lock:
        db.execute(
            "UPDATE jobs SET state='queued', engine=?, step=?, handover=?, started=NULL "
            "WHERE id=?",
            (target, index, engine if crossed else None, job_id),
        )
        db.commit()
    queue_ready(target).set()
    failover.note_hop(job_id, engine, target, index, reason, resets_at)
    audit(f"job#{job_id} failover {engine} -> {target} step={index + 1} reason={reason}")
    send(chat_id,
         t("failover.hop", id=job_id, reason=failover.hop_reason_text(reason, engine, resets_at),
           to=engine_label(target), model=model_label(target, step_cfg["model"])),
         parse_mode="HTML")
    return True
def finish_job(job_id, state, result, exit_code, cost=None, turns=None, tokens=None):
    with db_lock:
        db.execute(
            "UPDATE jobs SET state=?, finished=?, result=?, exit_code=?, cost=?, turns=?, "
            "tokens=? WHERE id=?",
            (state, time.time(), result, exit_code, cost, turns, tokens, job_id),
        )
        db.commit()
def step_settings(engine, step):
    """(model, effort) for this run: the chain's, once a job has hopped."""
    model, effort = engine_model(engine), CFG["effort"]
    if step:
        step_cfg = failover.step_at(step)
        if step_cfg and step_cfg["engine"] == engine:
            model = step_cfg.get("model") or model
            effort = step_cfg.get("effort") or effort
    return model, effort
def run_job(job_id, chat_id, prompt, workdir, engine, mode=None, attempt=0, step=0,
            handover=None):
    spec = ENGINES[engine]
    if not os.path.isdir(workdir):
        workdir = CFG["workdir"]
    if handover:
        # A cross-engine hop starts cold: the new engine must be told that a
        # working tree it has never seen was already edited by another agent.
        prompt = failover.handover_prompt(prompt, handover)
        session_id, reset_reason = "", ""
    else:
        session_id, reset_reason = take_session(engine, workdir)
    if reset_reason:
        send(chat_id, t("session.reset", engine=engine_label(engine), reason=reset_reason),
             parse_mode="HTML")
    started = time.time()

    prompt_prefix = system_prompt()
    if mode == "plan":
        prompt_prefix += t(spec["plan_hint_key"])
    model, effort = step_settings(engine, step)
    cmd, stdin_text = spec["build"](prompt, session_id, mode, prompt_prefix, model, effort,
                                    workdir)
    log(f"job #{job_id} [{engine}] start in {workdir}: {' '.join(cmd[:6])}…")

    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
        env=dict(os.environ, CLAUDE_CODE_ENTRYPOINT="server-pult",
                 HOME=os.environ.get("HOME", "/root")),
    )
    with run_lock:
        RUNNING[engine] = {"proc": proc, "job_id": job_id}

    killer = threading.Timer(CFG["job_timeout_sec"],
                             lambda: signal_group(proc, signal.SIGKILL))
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
    limited = busy = errored = blocked = False
    resets_at = 0

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
            elif kind == "say":
                progress.note_words(payload)
            elif kind == "think":
                progress.note_thinking()
            elif kind == "status":
                progress.note_status(payload)
            elif kind == "usage":
                tokens = payload or tokens
            elif kind == "limit":
                remember_limit(engine, payload)
                if payload.get("status") not in ("allowed", "", None):
                    blocked = True
                    resets_at = payload.get("resets_at") or resets_at
                progress.note_limit(payload)
            elif kind == "result":
                final_text = payload["text"]
                cost, turns = payload["cost"], payload["turns"]
                tokens = payload["tokens"] or tokens
                errored = bool(payload["error"])
                limited = limited or bool(payload.get("limited"))
                busy = busy or bool(payload.get("busy"))
                if payload["error"] and not final_text.startswith("["):
                    final_text = t("result.error_prefix", text=final_text)
        if capped:
            log(f"job #{job_id} [{engine}]: over {max_turns} turns -- stopping it")
            signal_group(proc, signal.SIGKILL)
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
            and attempt == 0 and not capped and not limited and not blocked):
        log(f"job #{job_id} [{engine}]: resume failed, retrying with a fresh session")
        clear_session(engine, workdir)
        send(chat_id, t("session.stale", id=job_id), parse_mode="HTML")
        return run_job(job_id, chat_id, prompt, workdir, engine, mode=mode, attempt=1,
                       step=step, handover=None)

    if new_session_id:
        remember_session(engine, workdir, new_session_id)

    if capped:
        note = final_text or t("job.capped.note", turns=max_turns)
        finish_job(job_id, "failed", note, proc.returncode, cost, turns, tokens)
        send(chat_id, t("job.capped", banner=engine_banner(engine), id=job_id,
                        turns=max_turns, elapsed=elapsed),
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return
    if proc.returncode == -signal.SIGTERM:
        finish_job(job_id, "cancelled", final_text, proc.returncode, cost, turns, tokens)
        send(chat_id, t("job.cancelled", banner=engine_banner(engine), id=job_id,
                        elapsed=elapsed),
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return
    if proc.returncode == -signal.SIGKILL:
        finish_job(job_id, "failed", final_text, proc.returncode, cost, turns, tokens)
        send(chat_id, t("job.timeout", banner=engine_banner(engine), id=job_id,
                        elapsed=elapsed),
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return

    # Only a run that actually failed may walk the chain, and only for a reason
    # that another engine could survive: a quota, or an overloaded model.
    if final_text is None and looks_like_limit(stderr_text):
        limited = True
    failed = final_text is None or errored
    if failed and (limited or blocked):
        resets_at = set_cooldown(engine, resets_at)
        if hop_job(job_id, chat_id, engine, step, "limit", resets_at):
            return
        # Nowhere left to hop: say so once, with the time the window refills, then
        # report the failure the ordinary way.
        send(chat_id, t("failover.exhausted", banner=engine_banner(engine), id=job_id,
                        clock=fmt_clock(resets_at)), parse_mode="HTML")
    elif failed and busy and hop_job(job_id, chat_id, engine, step, "busy"):
        return

    if final_text is None:
        detail = stderr_text[-1000:] or f"exit code {proc.returncode}"
        finish_job(job_id, "failed", detail, proc.returncode, cost, turns, tokens)
        send(chat_id, t("job.failed", banner=engine_banner(engine), id=job_id,
                        elapsed=elapsed, detail=h(detail)),
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return

    if errored:
        # The engine answered, and the answer is that it failed. Reporting that as
        # a green tick is how a broken run gets mistaken for a finished one.
        finish_job(job_id, "failed", final_text, proc.returncode, cost, turns, tokens)
        send(chat_id, t("job.engine_error", banner=engine_banner(engine), id=job_id,
                        elapsed=elapsed, detail=h(final_text[:800])),
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        log(f"job #{job_id} [{engine}] failed in {elapsed}: {final_text[:120]}")
        return

    finish_job(job_id, "done", final_text, proc.returncode, cost, turns, tokens)
    deliver_result(chat_id, job_id, final_text, elapsed, progress, turns, tokens, mode, engine)
    log(f"job #{job_id} [{engine}] done in {elapsed}")
LIMIT_STDERR_PHRASES = ("usage limit", "rate limit", "429", "quota", "resource_exhausted")
def looks_like_limit(text):
    low = (text or "").lower()
    return any(phrase in low for phrase in LIMIT_STDERR_PHRASES)
def result_header(job_id, engine, elapsed, turns, tokens, tools, planned):
    """The two or three lines above every answer: who ran it, and what it cost."""
    meta = [f"⏱ <b>{elapsed}</b>"]
    if turns:
        meta.append(f"🔄 {turns}")
    if tokens:
        meta.append(f"🧮 {fmt_tokens(tokens)}")
    state_tag = (t("result.plan_tag", id=job_id) if planned
                 else t("result.done_tag", id=job_id))
    return card(
        f"{engine_banner(engine)} · {state_tag}",
        RULE,
        " · ".join(meta),
        f"🔧 <i>{h(tools)}</i>" if tools else None,
    )
def deliver_result(chat_id, job_id, text, elapsed, progress, turns, tokens, mode=None,
                   engine="claude"):
    planned = mode == "plan"
    header = result_header(job_id, engine, elapsed, turns, tokens, progress.summary(), planned)

    # Fit is decided on the rendered message, not on the raw answer: escaping and
    # the quote tags both add length, and a card split in half breaks its own HTML.
    inline = card(header, "", quote(h(text)))
    if len(inline) <= TELEGRAM_MAX_CHARS:
        send(chat_id, inline,
             markup=result_menu(job_id, planned=planned, engine=engine), parse_mode="HTML")
        return

    # Long answers go out as a file so nothing is lost, with a readable preview.
    path = os.path.join(UPLOAD_DIR, f"job-{job_id}.md")
    with open(path, "w") as f:
        f.write(f"# {t('result.file_title', id=job_id)}\n\n{text}\n")
    preview = text[:PREVIEW_CHARS].rsplit("\n", 1)[0]
    send(chat_id, card(
        header,
        "",
        quote(h(preview) + " …", expandable=True),
        "",
        t("result.too_long", chars=len(text)),
    ), markup=result_menu(job_id, full=True, planned=planned, engine=engine),
        parse_mode="HTML")
    send_document(chat_id, path, caption=t("result.file_caption", id=job_id))
TOOL_LABEL_KEYS = {
    # Claude Code
    "Read": "read", "Edit": "edit", "Write": "write", "Bash": "command",
    "Grep": "search", "Glob": "file_search", "WebFetch": "web", "WebSearch": "search",
    "Task": "agent", "TodoWrite": "plan",
    # Antigravity (agy)
    "run_command": "command", "read_file": "read", "view_file": "read",
    "write_to_file": "write", "replace_file_content": "edit",
    "multi_replace_file_content": "edit", "list_dir": "dir",
    "grep_search": "search", "find_by_name": "file_search",
    "search_web": "web", "read_url_content": "web", "task_boundary": "plan",
}
def tool_label(name):
    key = TOOL_LABEL_KEYS.get(name)
    return t(f"tool.{key}") if key else name
# A card that never changes looks stuck; the glyph turns on every edit.
SPINNER = "◐◓◑◒"
class ProgressReporter:
    """Live progress in one edited message. Gives up quietly when Telegram is down.

    Claude streams its prose with --include-partial-messages, so the card can show
    what the model is actually saying instead of a list of tool names. agy has no
    equivalent flag; it keeps the tool view, and the two still look like one card.
    """

    def __init__(self, chat_id, job_id, started, workdir, engine):
        self.chat_id = chat_id
        self.job_id = job_id
        self.started = started
        self.workdir = workdir
        self.engine = engine
        self.tools = []
        self.last_detail = ""
        self.words = ""
        self.thinking = False
        self.status = ""
        self.limit_note = ""
        self.message_id = None
        self.last_update = 0.0
        self.frame = 0

    def _tick(self, force=False):
        now = time.time()
        if not force and now - self.last_update < CFG["progress_interval_sec"]:
            return
        self.last_update = now
        self._render()

    def note_tool(self, name, tool_input):
        self.tools.append(name)
        self.thinking = False
        self.last_detail = self._describe(name, tool_input)
        self._tick()

    def note_words(self, text):
        self.thinking = False
        # Keep the tail: the newest sentence is the one worth showing.
        self.words = (self.words + text)[-400:]
        self._tick()

    def note_thinking(self):
        if not self.thinking:
            self.thinking = True
            self._tick()

    def note_status(self, status):
        self.status = status or ""
        # The very first status is the fastest feedback available -- show the card
        # immediately rather than waiting out the first interval.
        self._tick(force=self.message_id is None and status == "requesting")

    def note_limit(self, info):
        window = (info.get("windows") or {}).get("five_hour") or {}
        used = float(window.get("utilization") or 0)
        if used >= CFG["limit_warn_utilization"]:
            self.limit_note = t("progress.limit_warn", percent=int(used * 100),
                                clock=fmt_clock(window.get("resets_at")))
            self._tick(force=True)

    @staticmethod
    def _describe(name, tool_input):
        label = tool_label(name)
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

    def _tail(self):
        """The last thing the model said, on one line."""
        words = " ".join(self.words.split())
        return words[-180:]

    def _render(self):
        self.frame += 1
        detail = ""
        tail = self._tail()
        if tail:
            detail = f"💬 <i>{h(tail)}</i>"
        elif self.thinking:
            detail = t("progress.thinking")
        elif self.last_detail:
            detail = f"🔧 <i>{h(self.last_detail)}</i>"
        elif self.status:
            detail = t("progress.status", status=h(self.status))
        text = card(
            t("progress.head", banner=engine_banner(self.engine), id=self.job_id,
              spinner=SPINNER[self.frame % len(SPINNER)]),
            t("progress.where", project=h(project_label(self.workdir)),
              elapsed=fmt_duration(time.time() - self.started), count=len(self.tools)),
            f"🔧 <i>{h(self.last_detail)}</i>" if (tail and self.last_detail) else None,
            detail or None,
            self.limit_note or None,
        )
        params = {"text": text, "parse_mode": "HTML",
                  "reply_markup": job_menu(self.job_id)}
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
        return ", ".join(f"{tool_label(n)}×{c}" if c > 1 else tool_label(n)
                         for n, c in top)

    def clear(self):
        if self.message_id is not None:
            api_try("deleteMessage",
                    {"chat_id": self.chat_id, "message_id": self.message_id}, timeout=10)
def recover_interrupted_jobs():
    """A restart mid-job leaves rows in 'running'. Put them back in the queue."""
    with db_lock:
        cur = db.execute("UPDATE jobs SET state='queued', started=NULL WHERE state='running'")
        db.commit()
    for engine in ENGINE_ORDER:
        queue_ready(engine).set()
    return cur.rowcount
