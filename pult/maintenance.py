"""Periodic disk, database and catalogue upkeep, plus the boot notice."""

import glob
import os
import sqlite3
import time

from .core import AUDIT_PATH, UPLOAD_DIR, log, shutdown
from .config import CFG
from .db import db, db_lock, meta_del, meta_get, meta_keys, meta_set
from .engines import ENGINE_ORDER, refresh_catalogue
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
# Written once by a version that kept one session for the whole bot, and by the
# onboarding wizard, which only ever wrote its step and never read it back.
LEGACY_META_KEYS = ("session_id", "onboard:step")
SESSION_SUFFIXES = (":last", ":jobs")
def stale_meta_keys(now=None):
    """Rows in `meta` that no longer describe anything real.

    `meta` is the one table nothing ever deleted from. A session leaves three
    rows behind per (engine, project), `clear_session` only blanks them rather
    than dropping them, and a project deleted from the server keeps its rows for
    good -- so the table only ever grows, with entries pointing at directories
    that stopped existing.
    """
    now = time.time() if now is None else now
    idle_limit = CFG["session_idle_reset_sec"]
    doomed, sessions = [], {}
    for key in meta_keys():
        if key in LEGACY_META_KEYS:
            doomed.append(key)
        elif key.startswith("cooldown:"):
            try:
                until = float(meta_get(key) or 0)
            except ValueError:
                until = 0.0
            if until <= now:
                doomed.append(key)          # the window it named has long refilled
        elif key.startswith("session:"):
            base = key
            for suffix in SESSION_SUFFIXES:
                if key.endswith(suffix):
                    base = key[: -len(suffix)]
            sessions.setdefault(base, []).append(key)

    for base, keys in sessions.items():
        parts = base.split(":", 2)
        if len(parts) != 3 or parts[1] not in ENGINE_ORDER:
            doomed += keys                  # written before sessions were per engine
            continue
        if not os.path.isdir(parts[2]):
            doomed += keys                  # the project is gone from the disk
            continue
        if not meta_get(base):
            doomed += keys                  # cleared: kept as empty strings until now
            continue
        try:
            last = float(meta_get(base + ":last") or 0)
        except ValueError:
            last = 0.0
        # take_session() would retire this context the moment it was asked for;
        # there is nothing left to resume, so the rows can go now.
        if idle_limit and last and now - last > idle_limit:
            doomed += keys
    return doomed
def prune_meta():
    """Drop the stale rows. Returns how many went."""
    keys = stale_meta_keys()
    for key in keys:
        meta_del(key)
    if keys:
        log(f"housekeeping: dropped {len(keys)} stale meta row(s)")
    return len(keys)
def expire_pending(now=None):
    """Cancel confirm cards nobody ever pressed. Returns how many.

    Without this a `pending` row is immortal: the housekeeping delete only takes
    finished jobs, so an unanswered task from a fortnight ago keeps its ▶️ button
    and stays at the top of /jobs.
    """
    window = CFG["pending_expiry_sec"]
    if not window:
        return 0
    now = time.time() if now is None else now
    with db_lock:
        cur = db.execute(
            "UPDATE jobs SET state='cancelled', finished=? WHERE state='pending' "
            "AND created < ?",
            (now, now - window),
        )
        db.commit()
    if cur.rowcount:
        log(f"housekeeping: expired {cur.rowcount} unapproved job(s)")
    return cur.rowcount
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
            expire_pending()
            prune_meta()
            with db_lock:
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
