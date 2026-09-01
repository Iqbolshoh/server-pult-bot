"""The loopback API a running agent calls through curl.

It is the only port this bot opens, and until now the only module with no test
at all. Anything else on the box can reach 127.0.0.1, so what the key does and
does not gate is worth pinning: /health is public on purpose, everything that
speaks to Telegram is not.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from . import context  # noqa: F401 -- must run before pult is imported

import http.server

from pult import localapi
from pult.config import CFG, LOCAL_API_KEY


def get(url):
    """(status, parsed body) -- a 4xx is an answer here, not an exception."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read())


class LocalAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sent = []
        cls.files = []
        cls.real = (localapi.send, localapi.send_user_file)
        localapi.send = lambda chat_id, text: cls.sent.append((chat_id, text))
        localapi.send_user_file = lambda chat_id, path: cls.files.append((chat_id, path))
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), localapi.LocalAPIHandler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        localapi.send, localapi.send_user_file = cls.real

    def setUp(self):
        self.sent.clear()
        self.files.clear()
        localapi._hits.clear()

    def test_health_needs_no_key(self):
        status, body = get(f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("running", body)

    def test_a_message_without_the_key_is_refused(self):
        status, _ = get(f"{self.base}/send-msg?text=hello")
        self.assertEqual(status, 403)
        self.assertEqual(self.sent, [])

    def test_a_wrong_key_is_refused(self):
        status, _ = get(f"{self.base}/send-msg?key=nope&text=hello")
        self.assertEqual(status, 403)
        self.assertEqual(self.sent, [])

    def test_the_key_carries_a_message_to_the_operator(self):
        status, body = get(f"{self.base}/send-msg?key={LOCAL_API_KEY}&text=hello")
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.sent, [(CFG["allowed_user_ids"][0], "hello")])

    def test_a_file_needs_a_path(self):
        status, body = get(f"{self.base}/send-file?key={LOCAL_API_KEY}")
        self.assertEqual(status, 400)
        self.assertEqual(self.files, [])

    def test_a_file_goes_out_by_path(self):
        path = os.path.join(context.HOME, "report.md")
        status, body = get(f"{self.base}/send-file?key={LOCAL_API_KEY}&file={path}")
        self.assertEqual((status, body["sent"]), (200, path))
        self.assertEqual(self.files, [(CFG["allowed_user_ids"][0], path)])

    def test_an_unknown_endpoint_is_a_404_not_a_traceback(self):
        status, _ = get(f"{self.base}/whatever?key={LOCAL_API_KEY}")
        self.assertEqual(status, 404)

    def test_the_rate_limit_closes_before_the_key_is_even_read(self):
        for _ in range(localapi.RATE_MAX_HITS):
            get(f"{self.base}/health")
        status, _ = get(f"{self.base}/send-msg?key={LOCAL_API_KEY}&text=flood")
        self.assertEqual(status, 429)
        self.assertEqual(self.sent, [])

    def test_the_window_reopens(self):
        for _ in range(localapi.RATE_MAX_HITS):
            get(f"{self.base}/health")
        self.assertEqual(get(f"{self.base}/health")[0], 429)
        localapi._hits[:] = []          # what RATE_WINDOW_SEC does on its own clock
        self.assertEqual(get(f"{self.base}/health")[0], 200)


if __name__ == "__main__":
    unittest.main()
