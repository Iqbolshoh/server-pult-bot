#!/usr/bin/env python3
"""Server Pult — entrypoint. The bot itself lives in pult/."""

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

from pult.core import log, shutdown
from pult.config import CFG
from pult.telegram import api_try, send, sender
from pult.engines import ENGINE_ORDER
from pult.projects import current_project
from pult.keyboards import main_menu, main_reply_kb
from pult.jobs import do_cancel, recover_interrupted_jobs, worker
from pult.handlers import poller
from pult.localapi import local_api_server
from pult.maintenance import housekeeping

BOT_COMMANDS = [
    ("start", "Bosh ekran va tugmalar"),
    ("status", "Hozirgi ish va navbat"),
    ("jobs", "Oxirgi ishlar"),
    ("history", "So'nggi topshiriqlar"),
    ("engine", "Dvigatel: Claude / Antigravity / ikkalasi"),
    ("both", "Vazifani ikkala dvigatelga birdan"),
    ("model", "Model tanlash"),
    ("limit", "Token sarfi"),
    ("projects", "Loyihani tanlash"),
    ("cd", "Loyihaga o'tish"),
    ("ls", "Papka ichi"),
    ("pwd", "Joriy jild"),
    ("file", "Faylni Telegramga yuborish"),
    ("get", "Ish natijasi yoki fayl"),
    ("sh", "Shell buyrug'i (dvigatelsiz)"),
    ("server", "Server holati"),
    ("new", "Kontekstni tozalash"),
    ("stop", "Ishni to'xtatish"),
    ("confirm", "Tasdiq so'rashni yoqish/o'chirish"),
    ("mode", "Claude ruxsat rejimi"),
    ("settings", "Sozlamalar"),
    ("menu", "Inline menyu"),
    ("keyboard", "Pastki tugmalarni tiklash"),
    ("ping", "Bot tirikmi"),
    ("restart", "Botni qayta ishga tushirish"),
    ("help", "Yordam"),
]
def publish_commands():
    """Tell Telegram the command list so typing '/' offers autocomplete."""
    ok = api_try("setMyCommands", {"commands": [
        {"command": name, "description": desc} for name, desc in BOT_COMMANDS
    ]}, timeout=15)
    log("command list published" if ok else "could not publish the command list")
def main():
    def on_signal(signum, _frame):
        log(f"signal {signum} -- shutting down")
        shutdown.set()
        do_cancel()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    requeued = recover_interrupted_jobs()
    if requeued:
        log(f"requeued {requeued} interrupted job(s)")
    log(f"starting, workdir={current_project()}, engines={ENGINE_ORDER}, "
        f"default={CFG['engine']}, allowed={CFG['allowed_user_ids']}")

    if CFG["notify_on_start"]:
        note = "🤖 Bot ishga tushdi."
        if requeued:
            note += f" {requeued} ta uzilib qolgan ish navbatga qaytarildi."
        for uid in CFG["allowed_user_ids"]:
            send(uid, note, markup=main_reply_kb())
            send(uid, "Nima qilamiz?", markup=main_menu())

    threading.Thread(target=publish_commands, daemon=True).start()

    threads = [
        threading.Thread(target=poller, name="poller", daemon=True),
        threading.Thread(target=sender, name="sender", daemon=True),
        threading.Thread(target=housekeeping, name="housekeeping", daemon=True),
        threading.Thread(target=local_api_server, name="localapi", daemon=True),
    ]
    # One worker per engine: that is what lets both run at the same time.
    for engine in ENGINE_ORDER:
        threads.append(
            threading.Thread(target=worker, args=(engine,), name=f"worker-{engine}",
                             daemon=True))
    for t in threads:
        t.start()
    while not shutdown.is_set():
        shutdown.wait(1)
    log("stopped")


if __name__ == "__main__":
    main()
