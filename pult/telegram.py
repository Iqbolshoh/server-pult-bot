"""Telegram API client and the durable outbox."""

import html
import http.client
import json
import mimetypes
import os
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.parse
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
# ---------------------------------------------------------------------------
# HTTPS transport
#
# Two choices here are not the obvious ones, and both were measured on this
# server rather than guessed:
#
# 1. IPv4 first. api.telegram.org resolves to both families and getaddrinfo puts
#    the IPv6 address first, so that is the one Python picks. On this uplink
#    roughly one IPv6 TLS handshake in seven never completes (2 of 14 in a
#    sample; IPv4 scored 0 of 14) -- and a stalled handshake costs the whole
#    request timeout, so a single button press could sit spinning for twenty
#    seconds. IPv6 is still tried, but only when IPv4 is the one that fails.
#
# 2. Connections are kept and reused. A fresh TCP+TLS handshake costs 30-50 ms
#    before Telegram has read a byte; the same call on an open connection
#    answers in 15-25 ms. Every progress tick, screen edit and button press is
#    one call, so the handshake was most of the wait.
# ---------------------------------------------------------------------------

HOST = urllib.parse.urlsplit(API_BASE).hostname or "api.telegram.org"
API_PATH = urllib.parse.urlsplit(API_BASE).path
FILE_HOST = urllib.parse.urlsplit(FILE_BASE).hostname or HOST
FILE_PATH = urllib.parse.urlsplit(FILE_BASE).path
HANDSHAKE_TIMEOUT = 10   # per address, so a dead route is abandoned quickly
IDLE_TTL = 55            # Telegram hangs up on idle sockets; do not hand out a stale one
POOL_SIZE = 4

_ssl_context = ssl.create_default_context()
def open_socket(host, timeout, families=(socket.AF_INET, socket.AF_INET6)):
    """A connected TLS socket, trying each family in turn. Raises OSError."""
    problem = None
    for family in families:
        try:
            addresses = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)
        except OSError as e:
            problem = e
            continue
        for af, kind, proto, _canonical, address in addresses:
            sock = socket.socket(af, kind, proto)
            sock.settimeout(min(timeout or HANDSHAKE_TIMEOUT, HANDSHAKE_TIMEOUT))
            try:
                sock.connect(address)
                wrapped = _ssl_context.wrap_socket(sock, server_hostname=host)
            except OSError as e:
                sock.close()
                problem = e
                continue
            wrapped.settimeout(timeout)
            return wrapped
    raise OSError(f"cannot reach {host}: {problem or 'no address'}")
class Connection(http.client.HTTPSConnection):
    """HTTPS to Telegram, on a socket chosen by open_socket and then kept open."""

    def connect(self):
        self.sock = open_socket(self.host, self.timeout)
_pool = []          # [(connection, host, idle since)] -- newest last
_pool_lock = threading.Lock()
def take_connection(host, timeout):
    """An idle pooled connection, or a new one. Returns (connection, reused)."""
    now = time.time()
    with _pool_lock:
        for i in range(len(_pool) - 1, -1, -1):
            conn, conn_host, idle_since = _pool[i]
            if conn_host != host:
                continue
            _pool.pop(i)
            if now - idle_since > IDLE_TTL:
                conn.close()
                continue
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            return conn, True
    return Connection(host, timeout=timeout), False
def give_connection(conn, host):
    with _pool_lock:
        if len(_pool) >= POOL_SIZE:
            conn.close()
            return
        _pool.append((conn, host, time.time()))
def close_connections():
    """Drop every pooled socket -- used on shutdown and by the tests."""
    with _pool_lock:
        for conn, _host, _idle in _pool:
            conn.close()
        _pool.clear()
def http_call(host, method, path, body, headers, timeout):
    """One request over a pooled connection. Returns (status, body bytes)."""
    for attempt in (0, 1):
        conn, reused = take_connection(host, timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            status, payload = resp.status, resp.read()
        except (http.client.HTTPException, OSError):
            conn.close()
            # A pooled socket Telegram closed while it sat idle fails on the
            # first write, before the request was ever read -- safe to repeat.
            # A fresh connection failing is a real fault and must not be hidden.
            if reused and attempt == 0:
                continue
            raise
        give_connection(conn, host)
        return status, payload
class TelegramError(RuntimeError):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"HTTP {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after
def _request(host, path, body, headers, timeout):
    try:
        status, payload = http_call(host, "POST", path, body, headers, timeout)
    except (http.client.HTTPException, OSError) as e:
        # Code 0 marks a transport fault: the sender retries those forever,
        # while a 4xx from Telegram itself is a message it will never accept.
        raise TelegramError(0, str(e) or type(e).__name__) from None
    try:
        parsed = json.loads(payload)
    except ValueError:
        raise TelegramError(
            status, http.client.responses.get(status, "unexpected reply")) from None
    if not parsed.get("ok"):
        raise TelegramError(
            status if status >= 400 else 0,
            parsed.get("description", str(parsed)),
            (parsed.get("parameters") or {}).get("retry_after"),
        )
    return parsed["result"]
def api_call(method, params=None, timeout=30):
    """Call the Telegram API. Raises TelegramError on failure."""
    body = json.dumps(params or {}).encode("utf-8")
    return _request(HOST, API_PATH + method, body,
                    {"Content-Type": "application/json"}, timeout)
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
    return _request(HOST, API_PATH + method, bytes(body),
                    {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout)
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
    # Not pooled: a download holds the socket for as long as the file takes, and
    # there is nothing to gain from keeping it afterwards.
    conn = Connection(FILE_HOST, timeout=120)
    try:
        conn.request("GET", FILE_PATH + remote)
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"download failed: HTTP {resp.status}")
        with open(local, "wb") as out:
            shutil.copyfileobj(resp, out)
    finally:
        conn.close()
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
