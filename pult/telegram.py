"""Telegram API client and the durable outbox."""

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

from .core import TELEGRAM_MAX_CHARS, UPLOAD_DIR, log, outbox_ready, shutdown
from .config import API_BASE, CFG, FILE_BASE
from .db import db, db_lock

def send(chat_id, text, markup=None, parse_mode=None):
    """Queue a message for delivery. Never blocks on the network."""
    chunks = split_message(text)
    with db_lock:
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1  # buttons belong on the final chunk only
            db.execute(
                "INSERT INTO outbox(chat_id,text,created,kind,parse_mode,markup) "
                "VALUES(?,?,?,'text',?,?)",
                (chat_id, chunk, time.time(), parse_mode,
                 json.dumps(markup) if (markup and last) else None),
            )
        db.commit()
    outbox_ready.set()
def send_document(chat_id, file_path, caption=""):
    with db_lock:
        db.execute(
            "INSERT INTO outbox(chat_id,text,created,kind,file_path) VALUES(?,?,?,'doc',?)",
            (chat_id, caption[:1000], time.time(), file_path),
        )
        db.commit()
    outbox_ready.set()
def split_message(text):
    text = text if text.strip() else "(bo'sh javob)"
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MAX_CHARS:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, TELEGRAM_MAX_CHARS)
        if cut < TELEGRAM_MAX_CHARS // 2:
            cut = TELEGRAM_MAX_CHARS
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
class TelegramError(RuntimeError):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"HTTP {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after
def _request(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        raise TelegramError(
            e.code,
            body.get("description", str(e)),
            (body.get("parameters") or {}).get("retry_after"),
        ) from None
    if not payload.get("ok"):
        raise TelegramError(0, str(payload))
    return payload["result"]
def api_call(method, params=None, timeout=30):
    """Call the Telegram API. Raises TelegramError on failure."""
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + method, data=data, headers={"Content-Type": "application/json"}
    )
    return _request(req, timeout)
def api_try(method, params=None, timeout=20):
    """Best-effort call: returns None instead of raising. For live UI updates."""
    try:
        return api_call(method, params, timeout)
    except Exception:
        return None
def api_upload(method, fields, file_field, filename, blob, timeout=180):
    """multipart/form-data upload, built by hand to stay dependency-free."""
    boundary = "----claudetg" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        ).encode("utf-8")
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    body += blob
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        API_BASE + method,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return _request(req, timeout)
def download_telegram_file(file_id, suggested_name):
    """Pull a photo/document the user sent into the uploads directory."""
    info = api_call("getFile", {"file_id": file_id})
    size = info.get("file_size") or 0
    if size > CFG["max_download_mb"] * 1024 * 1024:
        raise RuntimeError(f"fayl juda katta ({size // 1024 // 1024} MB)")
    remote = info["file_path"]
    ext = os.path.splitext(remote)[1] or os.path.splitext(suggested_name)[1] or ".bin"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(suggested_name)[0])[:40] or "file"
    local = os.path.join(UPLOAD_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}{ext}")
    with urllib.request.urlopen(FILE_BASE + remote, timeout=120) as resp, open(local, "wb") as out:
        shutil.copyfileobj(resp, out)
    return local
def sender():
    backoff = 1
    while not shutdown.is_set():
        # Clear before reading: anything queued after this point sets the event
        # again, so no message can slip through the gap.
        outbox_ready.clear()
        with db_lock:
            row = db.execute(
                "SELECT id,chat_id,text,attempts,kind,parse_mode,markup,file_path "
                "FROM outbox ORDER BY id LIMIT 1"
            ).fetchone()
        if not row:
            outbox_ready.wait(5)
            continue
        msg_id, chat_id, text, attempts, kind, parse_mode, markup, file_path = row
        try:
            deliver_outbox(chat_id, text, kind, parse_mode, markup, file_path)
            drop_outbox(msg_id)
            backoff = 1
        except TelegramError as e:
            if e.retry_after:
                log(f"rate limited, waiting {e.retry_after}s")
                shutdown.wait(e.retry_after + 1)
                continue
            # 4xx means Telegram will never accept this message. Retry once without
            # HTML (a stray tag is the usual culprit), then give up on it.
            if 400 <= e.code < 500:
                # Until the user presses /start the bot may not write to them at
                # all; retrying that forever just fills the log.
                if "chat not found" in (e.description or "").lower():
                    log(f"dropping outbox #{msg_id}: chat not started yet")
                    drop_outbox(msg_id)
                    continue
                if parse_mode and attempts == 0:
                    log(f"outbox #{msg_id}: {e.description} -- retrying as plain text")
                    with db_lock:
                        db.execute(
                            "UPDATE outbox SET parse_mode=NULL, text=?, attempts=1 WHERE id=?",
                            (strip_tags(text), msg_id),
                        )
                        db.commit()
                    continue
                if attempts >= 2:
                    log(f"dropping outbox #{msg_id}: {e.description}")
                    drop_outbox(msg_id)
                    continue
            bump_attempts(msg_id)
            log(f"send error {e} (retry in {backoff}s)")
            shutdown.wait(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as e:
            bump_attempts(msg_id)
            log(f"send error: {e} (retry in {backoff}s)")
            shutdown.wait(backoff)
            backoff = min(backoff * 2, 30)
def deliver_outbox(chat_id, text, kind, parse_mode, markup, file_path):
    if kind == "doc":
        if not file_path or not os.path.exists(file_path):
            raise TelegramError(400, "file missing")
        with open(file_path, "rb") as f:
            blob = f.read()
        fields = {"chat_id": str(chat_id)}
        if text:
            fields["caption"] = text
        api_upload("sendDocument", fields, "document", os.path.basename(file_path), blob)
        return
    params = {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    if markup:
        params["reply_markup"] = json.loads(markup)
    api_call("sendMessage", params, timeout=30)
def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))
def drop_outbox(msg_id):
    with db_lock:
        db.execute("DELETE FROM outbox WHERE id=?", (msg_id,))
        db.commit()
def bump_attempts(msg_id):
    with db_lock:
        db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (msg_id,))
        db.commit()
