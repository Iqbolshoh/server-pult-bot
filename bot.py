#!/usr/bin/env python3
"""Server Pult -- entrypoint. The bot itself lives in pult/."""

import os
import signal
import sys
import threading

from pult.core import CONFIG_PATH, log, shutdown, version
from pult.config import CFG, config_problems, ensure_pairing_code, save_config
from pult.db import meta_get, meta_set
from pult.i18n import available_languages, current_language, t,\
    telegram_language_code
from pult.telegram import api_try, close_connections, send, sender
from pult.engines import ENGINE_ORDER, refresh_catalogue
from pult.projects import current_project
from pult.keyboards import main_reply_kb
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
def command_list(lang):
    return [{"command": name, "description": t(key, lang=lang)}
            for name, key in BOT_COMMANDS]
def publish_commands():
    """Tell Telegram the command list -- per language, and for everyone else.

    The default scope (no language_code) is not optional: it is what a user
    whose Telegram is set to German or Turkish sees, and without it that list
    keeps whatever was last written to it -- which is how this bot ended up
    offering 26 commands to most of the world and 32 to the four locales.
    """
    scopes = [(None, current_language())]
    scopes += [(telegram_language_code(lang), lang) for lang in available_languages()]

    for code, lang in scopes:
        params = {"commands": command_list(lang)}
        if code:
            params["language_code"] = code
        if not api_try("setMyCommands", params, timeout=15):
            log(f"could not publish the command list ({code or 'default'})")
            return
    log(f"command list published ({len(scopes)} scopes)")
def publish_profile():
    """The description and the one-liner above it, in every language.

    Both were empty until now, in every language -- so the profile card, the
    thing a new operator reads before pressing Start, said nothing at all.
    Telegram caps the description at 512 characters and the short one at 120;
    the locale test holds both to that.

    Deliberately not setMyName: the name is the product's, not a translation,
    and Telegram rate-limits name changes far harder than these two.
    """
    fingerprint = str([(lang, t("bot.description", lang=lang), t("bot.short", lang=lang))
                       for lang in available_languages()] + [current_language()])
    if meta_get("profile_fingerprint") == fingerprint:
        return

    scopes = [(None, current_language())]
    scopes += [(telegram_language_code(lang), lang) for lang in available_languages()]

    for code, lang in scopes:
        for method, field, key in (("setMyDescription", "description", "bot.description"),
                                   ("setMyShortDescription", "short_description", "bot.short")):
            params = {field: t(key, lang=lang)}
            if code:
                params["language_code"] = code
            if not api_try(method, params, timeout=15):
                log(f"could not publish {field} ({code or 'default'})")
                return

    meta_set("profile_fingerprint", fingerprint)
    log(f"profile published ({len(scopes)} scopes)")
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
            send(uid, note, markup=main_reply_kb(), parse_mode="HTML")

    threading.Thread(target=publish_commands, daemon=True).start()
    threading.Thread(target=publish_profile, daemon=True).start()
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
    close_connections()
    log("stopped")


if __name__ == "__main__":
    main()
