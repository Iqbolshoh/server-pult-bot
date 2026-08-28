#!/usr/bin/env python3
"""Server Pult -- entrypoint. The bot itself lives in pult/."""

import os
import signal
import sys
import threading

from pult.core import CONFIG_PATH, log, shutdown, version
from pult.config import CFG, config_problems, ensure_pairing_code, save_config
from pult.i18n import available_languages, t
from pult.telegram import api_try, send, sender
from pult.engines import ENGINE_ORDER, refresh_catalogue
from pult.projects import current_project
from pult.keyboards import main_menu, main_reply_kb
from pult.jobs import do_cancel, recover_interrupted_jobs, worker
from pult.handlers import poller
from pult.localapi import local_api_server
from pult.maintenance import housekeeping

# Command name -> locale key for its description. Published per language.
BOT_COMMANDS = [
    ("start", "cmd.start"), ("status", "cmd.status"), ("jobs", "cmd.jobs"),
    ("history", "cmd.history"), ("engine", "cmd.engine"), ("both", "cmd.both"),
    ("model", "cmd.model"), ("effort", "cmd.effort"), ("limit", "cmd.limit"),
    ("fallback", "cmd.fallback"), ("projects", "cmd.projects"), ("cd", "cmd.cd"),
    ("ls", "cmd.ls"), ("pwd", "cmd.pwd"), ("file", "cmd.file"), ("get", "cmd.get"),
    ("sh", "cmd.sh"), ("server", "cmd.server"), ("doctor", "cmd.doctor"),
    ("new", "cmd.new"), ("stop", "cmd.stop"), ("confirm", "cmd.confirm"),
    ("safe", "cmd.safe"), ("mode", "cmd.mode"), ("language", "cmd.language"),
    ("settings", "cmd.settings"), ("menu", "cmd.menu"), ("keyboard", "cmd.keyboard"),
    ("ping", "cmd.ping"), ("update", "cmd.update"), ("restart", "cmd.restart"),
    ("help", "cmd.help"),
]
def publish_commands():
    """Tell Telegram the command list -- once per language it knows about."""
    for lang in available_languages() or [None]:
        params = {"commands": [{"command": name, "description": t(key, lang=lang)}
                               for name, key in BOT_COMMANDS]}
        if lang:
            params["language_code"] = lang
        if not api_try("setMyCommands", params, timeout=15):
            log(f"could not publish the command list ({lang})")
            return
    log("command list published")
def refresh_models():
    try:
        refresh_catalogue()
    except Exception as e:
        log(f"catalogue refresh failed: {e}")
def main():
    # A first run should not have to be hand-fixed: if the secrets are there, write
    # the config file it is missing and carry on.
    if not os.path.exists(CONFIG_PATH) and CFG["bot_token"]:
        save_config()
        log(f"created {CONFIG_PATH}")
    problems = config_problems()
    if problems:
        for problem in problems:
            log(f"cannot start: {problem}")
        log("run ./install.sh to fix this, or edit the files above by hand")
        sys.exit(1)

    def on_signal(signum, _frame):
        log(f"signal {signum} -- shutting down")
        shutdown.set()
        do_cancel()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    code = ensure_pairing_code()
    if code:
        log(f"nobody is paired yet -- send this code to the bot to claim it: {code}")

    requeued = recover_interrupted_jobs()
    if requeued:
        log(f"requeued {requeued} interrupted job(s)")
    log(f"starting {version()}, workdir={current_project()}, engines={ENGINE_ORDER}, "
        f"default={CFG['engine']}, lang={CFG['language']}, allowed={CFG['allowed_user_ids']}")

    if CFG["notify_on_start"]:
        note = t("boot.started")
        if requeued:
            note += " " + t("boot.requeued", count=requeued)
        for uid in CFG["allowed_user_ids"]:
            send(uid, note, markup=main_reply_kb())
            send(uid, t("menu.prompt"), markup=main_menu())

    threading.Thread(target=publish_commands, daemon=True).start()
    threading.Thread(target=refresh_models, name="catalogue", daemon=True).start()

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
    for t_ in threads:
        t_.start()
    while not shutdown.is_set():
        shutdown.wait(1)
    log("stopped")


if __name__ == "__main__":
    main()
