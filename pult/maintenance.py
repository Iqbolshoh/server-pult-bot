"""Periodic disk, database and catalogue upkeep, plus the boot notice."""

import glob
import os
import sqlite3
import time

from .core import AUDIT_PATH, UPLOAD_DIR, log, shutdown
from .config import CFG
from .db import db, db_lock, meta_del, meta_get, meta_set
from .engines import refresh_catalogue
from .i18n import t

# When the last boot notice went out, and whether the restart was asked for.
BOOT_NOTICE_KEY = "boot.last_notice"
BOOT_ASKED_KEY = "boot.asked"
def request_boot_notice():
    """Mark the restart that is about to happen as one the operator ordered."""
    meta_set(BOOT_ASKED_KEY, 1)
def boot_notice(requeued=0, now=None):
    """The message to send on start, or None to come up quietly.

    needrestart restarts supervisor once per apt batch, so a single unattended
    upgrade bounces this bot four or five times inside a minute and the operator
    gets that many identical "the bot is up" cards. The first one is worth
    sending; the rest are noise. A restart the operator asked for (/restart,
    /update) always speaks, and so does one that had to requeue a job -- those
    carry news. Everything else inside the cooldown stays quiet.
    """
    if not CFG["notify_on_start"]:
        return None
    now = time.time() if now is None else now
    asked = bool(meta_get(BOOT_ASKED_KEY))
    if asked:
        meta_del(BOOT_ASKED_KEY)
    try:
        last = float(meta_get(BOOT_NOTICE_KEY) or 0)
    except ValueError:
        last = 0.0
    since = now - last
    if not asked and not requeued and since < CFG["boot_notice_cooldown_sec"]:
        log(f"boot notice suppressed -- the last one was {int(since)}s ago")
        return None
    meta_set(BOOT_NOTICE_KEY, now)
    note = t("boot.started")
    if requeued:
        note += " " + t("boot.requeued", count=requeued)
    return note
def housekeeping():
    """Keep uploads, the jobs table and the WAL from creeping up on disk."""
    while not shutdown.is_set():
        cutoff = time.time() - 7 * 86400
        for path in glob.glob(os.path.join(UPLOAD_DIR, "*")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
        try:
            if os.path.getsize(AUDIT_PATH) > 5 * 1024 * 1024:
                os.replace(AUDIT_PATH, AUDIT_PATH + ".1")
                log("rotated audit.log")
        except OSError:
            pass
        try:
            with db_lock:
                db.execute(
                    "DELETE FROM jobs WHERE state IN ('done','failed','cancelled') "
                    "AND created < ?",
                    (time.time() - 30 * 86400,),
                )
                db.commit()
                # WAL never shrinks on its own while the connection stays open.
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:
            log(f"housekeeping: {e}")
        # The model catalogue ages out on its own clock; this only pokes it.
        try:
            refresh_catalogue()
        except Exception as e:
            log(f"catalogue refresh: {e}")
        shutdown.wait(6 * 3600)
