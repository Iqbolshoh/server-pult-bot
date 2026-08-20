"""Telegram updates -> commands, callbacks and jobs."""

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

from .core import MEDIA_GROUP_WINDOW, POLL_TIMEOUT, START_TIME, fmt_duration, h, log, shutdown
from .config import CFG, save_config
from .db import db, db_lock, meta_get, meta_set
from .telegram import api_call, api_try, download_telegram_file, send
from .engines import other_engine, engine_label, ENGINES, ENGINE_ORDER, clear_all_sessions, engine_choice_label, engine_model_label, resolve_model
from .projects import current_project, list_projects, project_label
from .keyboards import LABEL_COMMANDS, back_menu, engine_menu, job_menu, main_menu, main_reply_kb, model_menu, projects_menu, settings_menu
from .screens import HELP_TEXT, engine_text, history_text, jobs_text, limit_text, ls_text, model_text, projects_text, server_text, settings_text, start_text, status_text
from .jobs import append_to_job, approve_job, deliver_full_result, do_cancel, run_shell, send_user_file, start_job

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
            target, chosen = resolve_model(arg)
            CFG[ENGINES[target]["model_key"]] = chosen
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
                    send(chat_id, "Foydalanish: <code>/stop</code> · "
                                  "<code>/stop claude</code> · <code>/stop 12</code>",
                         parse_mode="HTML")
                    return
        send(chat_id, do_cancel(target, only_engine))

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
        with db_lock:
            row = db.execute("SELECT engine FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            answer("Topilmadi")
        else:
            answer("Bajarilmoqda")
            # The plan lives in that engine's conversation and nowhere else.
            start_job(chat_id,
                      "Yuqorida tuzgan rejangni tasdiqlayman — endi to'liq bajar.",
                      note=f"reja #{job_id}", mode="auto", approved=True,
                      engine=row[0])
    elif data.startswith("cancel:"):
        note = do_cancel(int(data.split(":", 1)[1]))
        answer(note[:180])
        screen(note, main_menu())
    elif data.startswith("other:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            row = db.execute("SELECT prompt,engine FROM jobs WHERE id=?", (job_id,)).fetchone()
        target = other_engine(row[1]) if row else None
        if not target:
            answer("Topilmadi")
        else:
            answer(f"{engine_label(target)}ga yuborildi")
            start_job(chat_id, row[0], note=f"#{job_id} bilan solishtirish",
                      approved=True, engine=target)
    elif data.startswith("again:"):
        job_id = int(data.split(":", 1)[1])
        with db_lock:
            row = db.execute("SELECT prompt,engine FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            answer("Qayta yuborildi")
            start_job(chat_id, row[0], engine=row[1])
        else:
            answer("Topilmadi")
    elif data.startswith("full:"):
        answer("Yuborilmoqda…")
        deliver_full_result(chat_id, int(data.split(":", 1)[1]))
    else:
        answer()
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
