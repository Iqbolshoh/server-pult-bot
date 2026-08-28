"""The HTTPS transport: address order, connection reuse and error mapping.

Every stall the operator felt as "the bot is slow" came from here, so the two
things that fixed it -- IPv4 first and a socket that stays open -- are pinned by
tests rather than left to the next person to rediscover.
"""

import socket
import unittest

from . import context
from pult import telegram


class FakeSocket:
    def __init__(self, family, kind, proto):
        self.family = family
        self.closed = False

    def settimeout(self, _t):
        pass

    def connect(self, _addr):
        pass

    def close(self):
        self.closed = True
class AddressOrderTest(unittest.TestCase):
    """IPv6 to api.telegram.org drops about one handshake in seven on this
    uplink, and a dropped handshake costs the whole request timeout."""

    def setUp(self):
        self.made = []
        self.sockets = []
        self.fail_families = set()

        def getaddrinfo(host, port, family, kind):
            address = "::1" if family == socket.AF_INET6 else "127.0.0.1"
            return [(family, kind, 0, "", (address, port))]

        def make_socket(family, kind, proto):
            self.made.append(family)
            sock = FakeSocket(family, kind, proto)
            self.sockets.append(sock)
            return sock

        def wrap_socket(sock, server_hostname=None):
            if sock.family in self.fail_families:
                raise TimeoutError("timed out")
            return sock

        self.saved = (telegram.socket.getaddrinfo, telegram.socket.socket,
                      telegram._ssl_context)
        telegram.socket.getaddrinfo = getaddrinfo
        telegram.socket.socket = make_socket
        telegram._ssl_context = type("Ctx", (), {"wrap_socket": staticmethod(wrap_socket)})()

    def tearDown(self):
        (telegram.socket.getaddrinfo, telegram.socket.socket,
         telegram._ssl_context) = self.saved

    def test_ipv4_is_tried_first(self):
        telegram.open_socket("api.telegram.org", 5)
        self.assertEqual(self.made, [socket.AF_INET])

    def test_ipv6_is_the_fallback_when_ipv4_fails(self):
        self.fail_families = {socket.AF_INET}
        sock = telegram.open_socket("api.telegram.org", 5)
        self.assertEqual(self.made, [socket.AF_INET, socket.AF_INET6])
        self.assertEqual(sock.family, socket.AF_INET6)

    def test_a_failed_handshake_closes_its_socket(self):
        # A leaked half-open socket per stalled attempt would exhaust the file
        # descriptors long before anyone noticed the bot was slow.
        self.fail_families = {socket.AF_INET}
        telegram.open_socket("api.telegram.org", 5)
        self.assertEqual([s.closed for s in self.sockets], [True, False])

    def test_no_route_at_all_raises(self):
        self.fail_families = {socket.AF_INET, socket.AF_INET6}
        with self.assertRaises(OSError):
            telegram.open_socket("api.telegram.org", 5)
class FakeConnection:
    """Stands in for one pooled HTTPSConnection."""

    def __init__(self, host, timeout=None):
        self.host = host
        self.timeout = timeout
        self.sock = None
        self.closed = False
        self.requests = []
        self.fail_next = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path))
        if self.fail_next:
            self.fail_next = False
            raise ConnectionResetError("peer hung up")

    def getresponse(self):
        return type("Resp", (), {"status": 200, "read": staticmethod(lambda: b'{"ok":true,"result":1}')})()

    def close(self):
        self.closed = True
class PoolTest(unittest.TestCase):
    def setUp(self):
        telegram.close_connections()
        self.saved = telegram.Connection
        self.built = []

        def build(host, timeout=None):
            conn = FakeConnection(host, timeout)
            self.built.append(conn)
            return conn

        telegram.Connection = build

    def tearDown(self):
        telegram.Connection = self.saved
        telegram.close_connections()

    def test_a_second_call_reuses_the_first_connection(self):
        telegram.http_call("h", "POST", "/a", b"{}", {}, 5)
        telegram.http_call("h", "POST", "/b", b"{}", {}, 5)
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.built[0].requests, [("POST", "/a"), ("POST", "/b")])

    def test_a_connection_the_server_dropped_is_retried_once(self):
        telegram.http_call("h", "POST", "/a", b"{}", {}, 5)
        self.built[0].fail_next = True
        status, _body = telegram.http_call("h", "POST", "/b", b"{}", {}, 5)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.built), 2)
        self.assertTrue(self.built[0].closed)

    def test_a_fresh_connection_that_fails_is_not_retried(self):
        # Retrying a request the server may already have read would run a job twice.
        def build(host, timeout=None):
            conn = FakeConnection(host, timeout)
            conn.fail_next = True
            self.built.append(conn)
            return conn

        telegram.Connection = build
        with self.assertRaises(ConnectionResetError):
            telegram.http_call("h", "POST", "/a", b"{}", {}, 5)
        self.assertEqual(len(self.built), 1)

    def test_a_stale_connection_is_dropped_rather_than_handed_out(self):
        telegram.http_call("h", "POST", "/a", b"{}", {}, 5)
        conn, host, _idle = telegram._pool[0]
        telegram._pool[0] = (conn, host, 0.0)     # idle since the epoch
        telegram.http_call("h", "POST", "/b", b"{}", {}, 5)
        self.assertTrue(conn.closed)
        self.assertEqual(len(self.built), 2)

    def test_the_pool_does_not_grow_without_bound(self):
        for i in range(telegram.POOL_SIZE + 3):
            telegram.give_connection(FakeConnection("h"), "h")
        self.assertLessEqual(len(telegram._pool), telegram.POOL_SIZE)
class ErrorMappingTest(unittest.TestCase):
    def setUp(self):
        self.saved = telegram.http_call

    def tearDown(self):
        telegram.http_call = self.saved

    def reply(self, status, body):
        telegram.http_call = lambda *a, **kw: (status, body)

    def test_a_rate_limit_carries_its_retry_after(self):
        self.reply(429, b'{"ok":false,"description":"Too Many Requests",'
                        b'"parameters":{"retry_after":7}}')
        with self.assertRaises(telegram.TelegramError) as caught:
            telegram.api_call("sendMessage")
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(caught.exception.retry_after, 7)

    def test_a_gateway_page_is_reported_as_its_status(self):
        self.reply(502, b"<html>Bad Gateway</html>")
        with self.assertRaises(telegram.TelegramError) as caught:
            telegram.api_call("getUpdates")
        self.assertEqual(caught.exception.code, 502)
        self.assertIn("Bad Gateway", caught.exception.description)

    def test_a_transport_fault_is_code_zero_so_the_sender_keeps_retrying(self):
        def boom(*_a, **_kw):
            raise TimeoutError("timed out")

        telegram.http_call = boom
        with self.assertRaises(telegram.TelegramError) as caught:
            telegram.api_call("sendMessage")
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("timed out", str(caught.exception))

    def test_api_try_swallows_the_failure(self):
        self.reply(400, b'{"ok":false,"description":"nope"}')
        self.assertIsNone(telegram.api_try("editMessageText"))
