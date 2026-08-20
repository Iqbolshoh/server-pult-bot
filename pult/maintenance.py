"""Periodic disk and database upkeep."""

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

from .core import AUDIT_PATH, UPLOAD_DIR, log, shutdown
from .db import db, db_lock

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
        shutdown.wait(6 * 3600)
