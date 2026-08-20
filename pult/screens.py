"""Every screen the bot renders, as HTML."""

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

from .core import START_TIME, fmt_duration, fmt_tokens, h, running_jobs
from .config import CFG
from .db import db, db_lock, meta_get
from .engines import ENGINES, ENGINE_ORDER, agy_model_info, engine_choice_label, engine_label, engine_model, engine_model_label, session_key, session_usage
from .projects import current_project, project_label

def engine_text():
    live = running_jobs()
    lines = ["🧩 <b>Dvigatel tanlash</b>", ""]
    for eng in ENGINE_ORDER:
        mark = " ✅" if CFG["engine"] == eng else ""
        busy = f" · ▶️ #{live[eng]} ishlamoqda" if eng in live else " · 💤 bo'sh"
        lines.append(f"<b>{h(ENGINES[eng]['label'])}</b>{mark}")
        lines.append(f"   ⚙️ {h(engine_model_label(eng))}{busy}")
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
        f"{h(engine_label('claude'))}: <code>{h(CFG['model'] or 'odatiy')}</code>\n"
        f"{h(engine_label('agy'))}: <code>{h(agy_model_info(CFG['agy_model'])['label'])}</code>\n"
        f"🛡 Tasdiq: <code>{confirm}</code>\n"
        f"⏱ Bot uptime: <code>{fmt_duration(time.time() - START_TIME)}</code>\n"
        + "━" * 20 + "\n"
        "<i>Matn yozing — server bajaradi. Pastdagi tugmalar tez yo'l.</i>"
    )
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
            "SELECT id, prompt, engine FROM jobs ORDER BY id DESC LIMIT 15"
        ).fetchall()
    if not rows:
        return "📜 Tarix bo'sh."
    lines = ["📜 <b>So'nggi topshiriqlar</b>", ""]
    for jid, prompt, engine in rows:
        lines.append(f"<b>#{jid}</b> {h(engine_label(engine))} · {h(prompt[:100])}")
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
            "SELECT id,state,created,finished,prompt,project,engine FROM jobs "
            "ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return "Hali ish yo'q."
    icons = {"pending": "❓", "queued": "⏳", "running": "▶️",
             "done": "✅", "failed": "❌", "cancelled": "🛑"}
    out = ["📋 <b>Oxirgi ishlar</b>", ""]
    for jid, state, created, finished, prompt, project, engine in rows:
        dur = fmt_duration((finished or time.time()) - created)
        tag = project_label(project or "")
        out.append(f"{icons.get(state, '•')} <b>#{jid}</b> {h(engine_label(engine))} · "
                   f"{dur} · {h(tag)}")
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
