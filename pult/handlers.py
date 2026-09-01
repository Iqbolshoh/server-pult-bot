"""Telegram updates -> commands, callbacks and jobs."""

import os
import subprocess
import threading
import time

from .core import (MEDIA_GROUP_WINDOW, POLL_TIMEOUT, SOURCE_DIR, START_TIME, fmt_duration, h,
                   log, shutdown, version)
from .config import CFG, save_config
from .i18n import available_languages, language_name, reset_cache, t
from .db import db, db_lock, meta_get, meta_set
from .telegram import TelegramError, api_call, api_try, download_telegram_file, send
from .engines import (ENGINES, ENGINE_ORDER, EFFORTS, clear_all_sessions, clear_cooldown,
                      engine_choice_label, engine_label, engine_model_label, other_engine,
                      refresh_catalogue, resolve_model, set_engine_model)
from . import failover
from .projects import current_project, list_projects, project_label
from .keyboards import (back_to_settings, doctor_menu, effort_menu, engine_menu,
                        fallback_menu, job_menu, label_commands, language_menu,
                        main_reply_kb, model_menu, onboarding_confirm_menu,
                        onboarding_engine_menu, onboarding_language_menu,
                        onboarding_projects_menu, projects_menu, settings_menu)
from .screens import (doctor_text, effort_text, engine_text, fallback_text, help_text,
                      history_text, jobs_text, language_text, limit_text, ls_text, model_text,
                      projects_text, server_text, settings_text, start_text, status_text)
from .maintenance import request_boot_notice
from .jobs import (append_to_job, approve_job, deliver_full_result, do_cancel, run_shell,
                   send_user_file, start_job)

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
def in_background(fn, *args):
    """Run a slow screen off the poller thread, so the next update is not stuck."""
    threading.Thread(target=fn, args=args, daemon=True).start()
def handle_update(update):
    if "callback_query" in update:
        # Buttons run on their own thread: a slow screen like /server used to block
        # the poller, leaving every later tap spinning.
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
        send(chat_id, t("error.unsupported_message"), parse_mode="HTML")
        return

    # Bottom-keyboard labels are commands wearing a nicer coat.
    text = label_commands().get(text, text)

    if text.startswith("/"):
        handle_command(chat_id, text)
        return

    # "c: ...", "a: ...", "b: ..." pick an engine for this one message only.
    engine, text = split_engine_prefix(text)
    if not text:
        send(chat_id, t("error.empty_task"), parse_mode="HTML")
        return

    start_job(chat_id, text, engine=engine)
def extract_attachment(msg):
    """Return (file_id, filename, human kind key) for supported attachments."""
    if msg.get("photo"):
        largest = max(msg["photo"], key=lambda p: p.get("file_size") or 0)
        return largest["file_id"], "photo.jpg", "word.photo"
    if msg.get("document"):
        doc = msg["document"]
        return doc["file_id"], doc.get("file_name") or "file.bin", "word.file"
    return None
media_groups = {}
media_lock = threading.Lock()
def handle_attachment(chat_id, msg, attachment, caption):
    file_id, filename, kind_key = attachment
    try:
        path = download_telegram_file(file_id, filename)
    except Exception as e:
        send(chat_id, t("error.download_failed", error=h(e)), parse_mode="HTML")
        return

    group_id = msg.get("media_group_id")
    if group_id and not caption:
        with media_lock:
            entry = media_groups.get(group_id)
        if entry and time.time() - entry[1] < MEDIA_GROUP_WINDOW and append_to_job(entry[0], path):
            log(f"attached {path} to queued job #{entry[0]}")
            return

    kind = t(kind_key)
    prompt = t("job.attachment_prompt", kind=kind, path=path) + "\n" + (
        caption or t("job.attachment_default_task", kind=kind))
    job_id = start_job(chat_id, prompt, note=f"📎 {os.path.basename(path)}")
    if group_id:
        with media_lock:
            media_groups[group_id] = (job_id, time.time())
def try_pairing(user_id, chat_id, text):
    """Claim the bot on first contact. Only possible while no owner is set."""
    if CFG["allowed_user_ids"] or not CFG["pairing_code"]:
        return False
    if text.strip() != CFG["pairing_code"]:
        log(f"wrong pairing code from user_id={user_id}")
        return False
    CFG["allowed_user_ids"] = [user_id]
    CFG["pairing_code"] = ""
    save_config()
    log(f"paired with user_id={user_id}")
    send(chat_id, t("onboard.paired"), parse_mode="HTML")
    begin_onboarding(chat_id)
    return True
def begin_onboarding(chat_id):
    """A first run must not require a hand-edited config.json."""
    meta_set("onboard:step", "lang")
    send(chat_id, t("onboard.language"), markup=onboarding_language_menu(), parse_mode="HTML")
def finish_onboarding(chat_id):
    CFG["onboarded"] = True
    save_config()
    meta_set("onboard:step", "done")
    send(chat_id, t("onboard.done", project=h(project_label(current_project())),
                    engine=h(engine_choice_label())),
         markup=main_reply_kb(), parse_mode="HTML")
    send(chat_id, help_text(), parse_mode="HTML")
def handle_command(chat_id, text):
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    arg = text[len(parts[0]):].strip()

    if cmd == "/start":
        send(chat_id, start_text(), markup=main_reply_kb(), parse_mode="HTML")
        if not CFG["onboarded"]:
            begin_onboarding(chat_id)

    elif cmd in ("/setup", "/onboard"):
        begin_onboarding(chat_id)

    elif cmd == "/help":
        send(chat_id, help_text(), parse_mode="HTML")

    elif cmd in ("/menu", "/keyboard"):
        # One menu, not two. The bottom keyboard is it; this re-sends it for a
        # client that lost it, and there is no inline twin to fall back on.
        send(chat_id, t("keyboard.restored"), markup=main_reply_kb(), parse_mode="HTML")

    elif cmd == "/ping":
        send(chat_id, t("ping", uptime=fmt_duration(time.time() - START_TIME)),
             parse_mode="HTML")

    elif cmd == "/new":
        clear_all_sessions(current_project())
        send(chat_id, t("session.cleared", project=h(project_label(current_project()))),
             parse_mode="HTML")

    elif cmd == "/status":
        send(chat_id, status_text(), parse_mode="HTML")

    elif cmd == "/jobs":
        send(chat_id, jobs_text(), parse_mode="HTML")

    elif cmd in ("/server", "/sys"):
        in_background(lambda: send(chat_id, server_text(), parse_mode="HTML"))

    elif cmd == "/doctor":
        in_background(lambda: send(chat_id, doctor_text(), markup=doctor_menu(),
                                   parse_mode="HTML"))

    elif cmd in ("/projects", "/sessions"):
        send(chat_id, projects_text(), markup=projects_menu(), parse_mode="HTML")

    elif cmd in ("/cd", "/setcwd"):
        do_cd_by_name(chat_id, arg)

    elif cmd == "/sh":
        if not arg:
            send(chat_id, t("shell.usage"), parse_mode="HTML")
        else:
            in_background(run_shell, chat_id, arg)

    elif cmd in ("/model", "/models"):
        if arg:
            target, chosen = resolve_model(arg)
            set_engine_model(target, chosen)
            save_config()
        send(chat_id, model_text(), markup=model_menu(), parse_mode="HTML")

    elif cmd == "/effort":
        if arg:
            level = arg.strip().lower()
            if level in ("-", "default", "odatiy"):
                CFG["effort"] = ""
            elif level in EFFORTS:
                CFG["effort"] = level
            else:
                send(chat_id, t("effort.usage", levels=", ".join(EFFORTS)), parse_mode="HTML")
                return
            save_config()
        send(chat_id, effort_text(), markup=effort_menu(), parse_mode="HTML")

    elif cmd == "/safe":
        CFG["safe_mode"] = not CFG["safe_mode"]
        save_config()
        send(chat_id, settings_text(), markup=settings_menu(), parse_mode="HTML")

    elif cmd in ("/fallback", "/chain"):
        if arg.strip().lower() in ("on", "off"):
            CFG["fallback_enabled"] = arg.strip().lower() == "on"
            save_config()
        send(chat_id, fallback_text(), markup=fallback_menu(), parse_mode="HTML")

    elif cmd in ("/language", "/lang", "/til"):
        code = arg.strip().lower()
        if code in available_languages():
            set_language(code)
        send(chat_id, language_text(), markup=language_menu(), parse_mode="HTML")

    elif cmd == "/mode":
        valid = ["auto", "acceptEdits", "plan", "manual", "dontAsk", "bypassPermissions"]
        if arg:
            if arg not in valid:
                send(chat_id, t("mode.usage", modes=", ".join(valid)), parse_mode="HTML")
                return
            CFG["permission_mode"] = arg
            save_config()
        send(chat_id, t("mode.body", mode=h(CFG["permission_mode"])), parse_mode="HTML")

    elif cmd == "/get":
        if not arg:
            send(chat_id, t("get.usage"), parse_mode="HTML")
        elif arg.isdigit():
            deliver_full_result(chat_id, int(arg))
        else:
            send_user_file(chat_id, arg)

    elif cmd in ("/cancel", "/stop", "/kill"):
        target, only_engine = None, None
        if arg:
            if arg.isdigit():
                target = int(arg)
            else:
                only_engine, _ = split_engine_prefix(arg + ":")
                if only_engine in (None, "both"):
                    only_engine = {"claude": "claude", "agy": "agy",
                                   "antigravity": "agy"}.get(arg.lower())
                if only_engine is None:
                    send(chat_id, t("cancel.usage"), parse_mode="HTML")
                    return
        send(chat_id, do_cancel(target, only_engine), parse_mode="HTML")

    elif cmd == "/settings":
        send(chat_id, settings_text(), markup=settings_menu(), parse_mode="HTML")

    elif cmd == "/confirm":
        CFG["confirm_before_run"] = not CFG["confirm_before_run"]
        save_config()
        send(chat_id, settings_text(), markup=settings_menu(), parse_mode="HTML")

    elif cmd in ("/engine", "/dvigatel"):
        if arg:
            choice = arg.strip().lower()
            aliases = {"claude": "claude", "c": "claude", "agy": "agy",
                       "antigravity": "agy", "a": "agy", "both": "both",
                       "ikkalasi": "both", "b": "both"}
            if choice not in aliases:
                send(chat_id, t("engine.usage"), parse_mode="HTML")
                return
            CFG["engine"] = aliases[choice]
            save_config()
        send(chat_id, engine_text(), markup=engine_menu(), parse_mode="HTML")

    elif cmd == "/both":
        if not arg:
            send(chat_id, t("both.usage"), parse_mode="HTML")
        else:
            start_job(chat_id, arg, engine="both")

    elif cmd == "/limit":
        send(chat_id, limit_text(), parse_mode="HTML")

    elif cmd == "/history":
        send(chat_id, history_text(), parse_mode="HTML")

    elif cmd == "/pwd":
        send(chat_id, t("pwd", project=h(project_label(current_project())),
                        path=h(current_project())), parse_mode="HTML")

    elif cmd == "/ls":
        send(chat_id, ls_text(arg), parse_mode="HTML")

    elif cmd == "/file":
        if not arg:
            send(chat_id, t("file.usage"), parse_mode="HTML")
        else:
            send_user_file(chat_id, arg)

    elif cmd == "/update":
        in_background(do_update, chat_id)

    elif cmd == "/restart":
        # Supervisor restarts us (autorestart=true); a running job is requeued on start.
        send(chat_id, t("restart.notice"), parse_mode="HTML")
        request_boot_notice()
        threading.Timer(2.0, lambda: (shutdown.set(), do_cancel())).start()

    else:
        send(chat_id, t("error.unknown_command", cmd=h(cmd)), parse_mode="HTML")
def set_language(code):
    CFG["language"] = code
    save_config()
    reset_cache()
def do_update(chat_id):
    """git pull, migrate, restart -- refused on a dirty tree, so no work is lost."""
    def git(*args):
        return subprocess.run(["git", "-C", SOURCE_DIR, *args], capture_output=True,
                              text=True, timeout=120)

    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        send(chat_id, t("update.dirty", files=h(dirty[:600])), parse_mode="HTML")
        return
    before = version()
    pull = git("pull", "--ff-only")
    output = (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0:
        send(chat_id, t("update.failed", error=h(output[-600:])), parse_mode="HTML")
        return
    after = version()
    if before == after:
        send(chat_id, t("update.current", version=h(after)), parse_mode="HTML")
        return
    send(chat_id, t("update.done", before=h(before), after=h(after),
                    output=h(output[-600:])), parse_mode="HTML")
    request_boot_notice()
    # The new code is only live after a restart: the modules already loaded stay
    # in memory otherwise. Migrations run on the way back up.
    threading.Timer(2.0, lambda: (shutdown.set(), do_cancel())).start()
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

    # The spinner only stops when answerCallbackQuery arrives, and the query goes
    # stale in seconds -- so answer before doing any work, and answer exactly
    # once. Telegram rejects the second answer to the same query, so the guard
    # has to be a lock and not a check-then-set on an Event: the timer below and
    # the branch that does the work would otherwise both get through.
    answer_lock = threading.Lock()
    answered = [False]
    fallback = None

    def answer(note=""):
        with answer_lock:
            if answered[0]:
                return
            answered[0] = True
        if fallback is not None:
            fallback.cancel()
        try:
            api_call("answerCallbackQuery",
                     {"callback_query_id": query["id"], "text": note}, timeout=8)
        except TelegramError as e:
            # A button pressed while the bot was down arrives with a query id
            # Telegram has already retired. Nothing is wrong and nothing can be
            # done about it, so it does not deserve a line in the log.
            if "too old" not in (e.description or "").lower():
                log(f"answerCallbackQuery failed for {data!r}: {e.description}")
        except Exception as e:
            log(f"answerCallbackQuery failed for {data!r}: {e}")

    if not authorised(user_id) or chat_id != user_id:
        answer(t("error.forbidden"))
        return

    # Even on a slow uplink the spinner must not hang: if nothing has answered in
    # 1.5 seconds, send an empty answer.
    fallback = threading.Timer(1.5, answer)
    fallback.daemon = True
    fallback.start()

    def screen(text, markup):
        # An edit that leaves reply_markup out keeps the buttons the message
        # already had, so a screen with no buttons has to say so with an empty
        # keyboard rather than with None.
        api_try("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "reply_markup": markup or {"inline_keyboard": []},
            "link_preview_options": {"is_disabled": True},
        })

    if data == "menu":
        answer()
        send(chat_id, t("keyboard.restored"), markup=main_reply_kb(), parse_mode="HTML")
    elif data == "status":
        answer()
        screen(status_text(), None)
    elif data == "server":
        answer(t("wait.measuring"))
        screen(server_text(), None)
    elif data == "doctor":
        answer(t("wait.checking"))
        screen(doctor_text(), doctor_menu())
    elif data == "jobs":
        answer()
        screen(jobs_text(), None)
    elif data == "help":
        answer()
        screen(help_text(), None)
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
            save_config()
            answer(engine_choice_label())
        screen(engine_text(), engine_menu())
    elif data == "noop":
        answer()
    elif data == "model":
        answer()
        screen(model_text(), model_menu())
    elif data == "models_refresh":
        answer(t("wait.reading"))
        refresh_catalogue(force=True)
        screen(model_text(), model_menu())
    elif data == "limit":
        answer()
        screen(limit_text(), back_to_settings())
    elif data == "effort":
        answer()
        screen(effort_text(), effort_menu())
    elif data.startswith("effort:"):
        level = data.split(":", 1)[1]
        CFG["effort"] = "" if level == "-" else level
        save_config()
        answer(CFG["effort"] or t("effort.default"))
        screen(effort_text(), effort_menu())
    elif data == "language":
        answer()
        screen(language_text(), language_menu())
    elif data.startswith("lang:"):
        code = data.split(":", 1)[1]
        if code in available_languages():
            set_language(code)
            answer(language_name(code))
            send(chat_id, t("keyboard.restored"), markup=main_reply_kb(), parse_mode="HTML")
        screen(language_text(), language_menu())
    elif data == "fallback":
        answer()
        screen(fallback_text(), fallback_menu())
    elif data == "fb:toggle":
        CFG["fallback_enabled"] = not CFG["fallback_enabled"]
        save_config()
        answer(t("word.on") if CFG["fallback_enabled"] else t("word.off"))
        screen(fallback_text(), fallback_menu())
    elif data.startswith("fb:up:") or data.startswith("fb:down:"):
        _, direction, index = data.split(":", 2)
        moved = failover.move_step(int(index), -1 if direction == "up" else 1)
        if moved is not None:
            save_config()
        answer()
        screen(fallback_text(), fallback_menu())
    elif data == "fb:cool":
        for eng in ENGINE_ORDER:
            clear_cooldown(eng)
        answer(t("fallback.cooldowns_cleared"))
        screen(fallback_text(), fallback_menu())
    elif data.startswith("setmodel:"):
        _, eng, choice = data.split(":", 2)
        if eng in ENGINES:
            set_engine_model(eng, "" if choice == "-" else choice)
            save_config()
            answer(engine_model_label(eng))
        screen(model_text(), model_menu())
    elif data == "projects":
        answer()
        screen(projects_text(), projects_menu())
    elif data == "new":
        clear_all_sessions(current_project())
        answer(t("session.cleared_short"))
        screen(t("session.cleared", project=h(project_label(current_project()))), None)
    elif data.startswith("cd:"):
        projects = list_projects()
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(projects):
            meta_set("workdir", projects[idx])
            answer(project_label(projects[idx]))
            screen(projects_text(), projects_menu())
        else:
            answer(t("error.not_found"))
    elif data == "toggle_confirm":
        CFG["confirm_before_run"] = not CFG["confirm_before_run"]
        save_config()
        answer(t("word.on") if CFG["confirm_before_run"] else t("word.off"))
        screen(settings_text(), settings_menu())
    elif data == "toggle_safe":
        CFG["safe_mode"] = not CFG["safe_mode"]
        save_config()
        answer(t("word.on") if CFG["safe_mode"] else t("word.off"))
        screen(settings_text(), settings_menu())
    elif data.startswith("ob:"):
        handle_onboarding(chat_id, data, answer, screen)
    elif data.startswith("run:"):
        job_id = int(data.split(":", 1)[1])
        note = approve_job(job_id, "auto")
        answer(t("wait.running"))
        screen(note, job_menu(job_id))
    elif data.startswith("plan:"):
        job_id = int(data.split(":", 1)[1])
        note = approve_job(job_id, "plan")
        answer(t("wait.planning"))
        screen(note, job_menu(job_id))
    elif data.startswith("drop:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            db.execute(
                "UPDATE jobs SET state='cancelled', finished=? WHERE id=? AND state='pending'",
                (time.time(), job_id),
            )
            db.commit()
        answer(t("word.cancelled"))
        screen(t("job.dropped", id=job_id), None)
    elif data.startswith("exec:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            row = db.execute("SELECT engine FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            answer(t("error.not_found"))
        else:
            answer(t("wait.running"))
            # The plan lives in that engine's conversation and nowhere else.
            start_job(chat_id, t("job.execute_plan"), note=f"#{job_id}", mode="auto",
                      approved=True, engine=row[0])
    elif data.startswith("cancel:"):
        note = do_cancel(int(data.split(":", 1)[1]))
        answer(note[:180])
        screen(note, None)
    elif data.startswith("other:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            row = db.execute("SELECT prompt,engine FROM jobs WHERE id=?", (job_id,)).fetchone()
        target = other_engine(row[1]) if row else None
        if not target:
            answer(t("error.not_found"))
        else:
            answer(engine_label(target))
            start_job(chat_id, row[0], note=t("job.compare_note", id=job_id),
                      approved=True, engine=target)
    elif data.startswith("again:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            row = db.execute("SELECT prompt,engine FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            answer(t("word.resent"))
            start_job(chat_id, row[0], engine=row[1])
        else:
            answer(t("error.not_found"))
    elif data.startswith("full:"):
        answer(t("wait.sending"))
        deliver_full_result(chat_id, int(data.split(":", 1)[1]))
    else:
        answer()
def handle_onboarding(chat_id, data, answer, screen):
    """Four taps: language, project, engine, confirm-before-run."""
    _, field, value = data.split(":", 2)
    if field == "lang":
        if value in available_languages():
            set_language(value)
        answer(language_name(value))
        meta_set("onboard:step", "dir")
        screen(t("onboard.project"), onboarding_projects_menu())
    elif field == "dir":
        if value != "-":
            projects = list_projects()
            index = int(value)
            if 0 <= index < len(projects):
                meta_set("workdir", projects[index])
        answer(project_label(current_project()))
        meta_set("onboard:step", "engine")
        screen(t("onboard.engine"), onboarding_engine_menu())
    elif field == "engine":
        if value in ENGINE_ORDER or value == "both":
            CFG["engine"] = value
            save_config()
        answer(engine_choice_label())
        meta_set("onboard:step", "confirm")
        screen(t("onboard.confirm"), onboarding_confirm_menu())
    elif field == "confirm":
        CFG["confirm_before_run"] = value == "1"
        save_config()
        answer()
        screen(t("onboard.saved"), {"inline_keyboard": []})
        finish_onboarding(chat_id)
def do_cd_by_name(chat_id, name):
    if not name:
        send(chat_id, projects_text(), markup=projects_menu(), parse_mode="HTML")
        return
    projects = list_projects()
    matches = [p for p in projects if name.lower() in project_label(p).lower()]
    if not matches and os.path.isdir(name):
        matches = [os.path.abspath(name)]
    if not matches:
        send(chat_id, t("error.project_not_found", name=h(name)), markup=projects_menu(),
             parse_mode="HTML")
        return
    meta_set("workdir", matches[0])
    send(chat_id, projects_text(), markup=projects_menu(), parse_mode="HTML")
