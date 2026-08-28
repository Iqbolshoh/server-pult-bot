"""Loopback helper a running agent calls through curl."""

import http.server
import json
import threading
import time
import urllib.parse

from .core import log, running_jobs
from .config import CFG, LOCAL_API_KEY
from .telegram import send
from .jobs import send_user_file

# Any local process can reach the port. The key guards the write endpoints; this
# guards everything else -- a compromised site user on a shared box should not be
# able to hammer the port or enumerate it for free.
RATE_WINDOW_SEC = 10
RATE_MAX_HITS = 20
_hits = []
_hits_lock = threading.Lock()
def rate_ok():
    now = time.time()
    with _hits_lock:
        _hits[:] = [at for at in _hits if now - at < RATE_WINDOW_SEC]
        if len(_hits) >= RATE_MAX_HITS:
            return False
        _hits.append(now)
    return True
class LocalAPIHandler(http.server.BaseHTTPRequestHandler):
    """Loopback-only helper the running agents call through curl."""

    def log_message(self, *_args):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not rate_ok():
            return self._reply(429, {"ok": False, "error": "too many requests"})
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if parsed.path == "/health":
            return self._reply(200, {"ok": True, "running": running_jobs()})
        if params.get("key") != LOCAL_API_KEY:
            return self._reply(403, {"ok": False, "error": "forbidden"})
        if not CFG["allowed_user_ids"]:
            return self._reply(503, {"ok": False, "error": "no recipient"})
        chat_id = CFG["allowed_user_ids"][0]
        if parsed.path == "/send-msg":
            send(chat_id, (params.get("text") or "…")[:3000])
            return self._reply(200, {"ok": True})
        if parsed.path == "/send-file":
            target = params.get("file")
            if not target:
                return self._reply(400, {"ok": False, "error": "file parameter missing"})
            send_user_file(chat_id, target)
            return self._reply(200, {"ok": True, "sent": target})
        return self._reply(404, {"ok": False, "error": "unknown endpoint"})
def local_api_server():
    try:
        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", CFG["local_api_port"]), LocalAPIHandler)
    except OSError as e:
        log(f"local API disabled: {e}")
        return
    log(f"local API: http://127.0.0.1:{CFG['local_api_port']}")
    srv.serve_forever(poll_interval=1)
