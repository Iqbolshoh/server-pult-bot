"""The job queue: one worker per engine, plus result delivery."""

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

from .core import INLINE_RESULT_LIMIT, PREVIEW_CHARS, RUNNING, UPLOAD_DIR, audit, fmt_duration, fmt_tokens, h, log, run_lock, shutdown
from .config import CFG, local_api_hint
from .db import db, db_lock
from .telegram import api_try, send, send_document
from .engines import ENGINES, ENGINE_ORDER, clear_session, engine_banner, engine_label, remember_session, take_session
from .projects import current_project, project_label
from .keyboards import confirm_menu, job_menu, result_menu

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
    audit(f"job#{job_id} [{state}] [{engine}] [{workdir}] {prompt[:200]!r}")

    banner = engine_banner(engine)
    if needs_ok:
        send(chat_id,
             f"{banner} · ❓ <b>#{job_id}</b> — tasdiqlaysizmi?\n"
             f"🗂 {h(project_label(workdir))}\n\n"
             f"<i>{h(prompt[:400])}</i>",
             markup=confirm_menu(job_id), parse_mode="HTML")
        return job_id

    with db_lock:
        ahead = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='queued' AND engine=? AND id<?",
            (engine, job_id),
        ).fetchone()[0]
    head = f"{banner} · 📥 <b>#{job_id}</b> qabul qilindi"
    if note:
        head += f" · {h(note)}"
    lines = [head, f"🗂 {h(project_label(workdir))}"]
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
            send(chat_id, f"{engine_banner(engine)} · ❌ <b>#{job_id}</b> xato: {h(e)}",
                 markup=result_menu(job_id, engine=engine), parse_mode="HTML")
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
        env=dict(os.environ, CLAUDE_CODE_ENTRYPOINT="server-pult",
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
             f"{engine_banner(engine)} · 🧯 <b>#{job_id}</b> {max_turns} qadam chegarasidan "
             f"oshgani uchun to'xtatildi ({elapsed}). Vazifani bo'laklarga bo'ling.",
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return
    if proc.returncode == -signal.SIGTERM:
        finish_job(job_id, "cancelled", final_text, proc.returncode, cost, turns, tokens)
        send(chat_id, f"{engine_banner(engine)} · 🛑 <b>#{job_id}</b> to'xtatildi ({elapsed}).",
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return
    if proc.returncode == -signal.SIGKILL:
        finish_job(job_id, "failed", final_text, proc.returncode, cost, turns, tokens)
        send(chat_id, f"{engine_banner(engine)} · ⏰ <b>#{job_id}</b> vaqt tugadi ({elapsed}).",
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return

    if final_text is None:
        detail = stderr_text[-1000:] or f"exit code {proc.returncode}"
        finish_job(job_id, "failed", detail, proc.returncode, cost, turns, tokens)
        send(chat_id, f"{engine_banner(engine)} · ❌ <b>#{job_id}</b> bajarilmadi "
                      f"({elapsed}):\n<pre>{h(detail)}</pre>",
             markup=result_menu(job_id, engine=engine), parse_mode="HTML")
        return

    finish_job(job_id, "done", final_text, proc.returncode, cost, turns, tokens)
    deliver_result(chat_id, job_id, final_text, elapsed, progress, turns, tokens, mode, engine)
    log(f"job #{job_id} [{engine}] done in {elapsed}")
def deliver_result(chat_id, job_id, text, elapsed, progress, turns, tokens, mode=None,
                   engine="claude"):
    planned = mode == "plan"
    meta = [f"⏱ {elapsed}"]
    if turns:
        meta.append(f"🔄 {turns}")
    if tokens:
        meta.append(f"🧮 {fmt_tokens(tokens)} token")
    state_tag = ("🧭 <b>#%d</b> REJA" % job_id if planned else f"✅ <b>#{job_id}</b>")
    header = f"{engine_banner(engine)} · {state_tag}\n" + " · ".join(meta)
    tools = progress.summary()
    if tools:
        header += f"\n🔧 {h(tools)}"

    if len(text) <= INLINE_RESULT_LIMIT:
        send(chat_id, f"{header}\n\n{h(text)}",
             markup=result_menu(job_id, planned=planned, engine=engine), parse_mode="HTML")
        return

    # Long answers go out as a file so nothing is lost, with a readable preview.
    path = os.path.join(UPLOAD_DIR, f"job-{job_id}.md")
    with open(path, "w") as f:
        f.write(f"# Ish #{job_id}\n\n{text}\n")
    preview = text[:PREVIEW_CHARS].rsplit("\n", 1)[0]
    send(chat_id,
         f"{header}\n\n{h(preview)}\n\n… javob uzun ({len(text)} belgi), to'lig'i faylda ↓",
         markup=result_menu(job_id, full=True, planned=planned, engine=engine),
         parse_mode="HTML")
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
            f"{engine_banner(self.engine)} · ⚙️ <b>#{self.job_id}</b> ishlamoqda\n"
            f"🗂 {h(project_label(self.workdir))} · "
            f"{fmt_duration(time.time() - self.started)}\n"
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
def recover_interrupted_jobs():
    """A restart mid-job leaves rows in 'running'. Put them back in the queue."""
    with db_lock:
        cur = db.execute("UPDATE jobs SET state='queued', started=NULL WHERE state='running'")
        db.commit()
    return cur.rowcount
