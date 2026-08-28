"""The durable outbox and the message splitter."""

import json
import unittest

from . import context
from pult import telegram
from pult.db import db, db_lock
from pult.core import TELEGRAM_MAX_CHARS


def rows():
    with db_lock:
        return db.execute("SELECT chat_id,text,kind,parse_mode,markup,file_path "
                          "FROM outbox ORDER BY id").fetchall()
class SplitTest(unittest.TestCase):
    def test_a_short_message_is_one_chunk(self):
        self.assertEqual(telegram.split_message("hello"), ["hello"])

    def test_an_empty_message_never_disappears(self):
        self.assertEqual(len(telegram.split_message("   ")), 1)

    def test_a_long_message_is_split_under_the_limit(self):
        chunks = telegram.split_message("x" * (TELEGRAM_MAX_CHARS * 3 + 17))
        self.assertEqual(len(chunks), 4)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), TELEGRAM_MAX_CHARS)
        self.assertEqual(len("".join(chunks)), TELEGRAM_MAX_CHARS * 3 + 17)

    def test_it_prefers_to_break_on_a_line_end(self):
        text = ("line\n" * 2000)
        chunks = telegram.split_message(text)
        self.assertTrue(chunks[0].endswith("line"))

    def test_a_line_longer_than_the_limit_is_still_cut(self):
        chunks = telegram.split_message("a" * 100 + "\n" + "b" * (TELEGRAM_MAX_CHARS * 2))
        self.assertTrue(all(len(c) <= TELEGRAM_MAX_CHARS for c in chunks))
class OutboxTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()

    def test_a_message_is_queued_before_any_network_call(self):
        telegram.send(42, "hello", parse_mode="HTML")
        queued = rows()
        self.assertEqual(len(queued), 1)
        self.assertEqual((queued[0][0], queued[0][1], queued[0][3]), (42, "hello", "HTML"))

    def test_buttons_ride_on_the_last_chunk_only(self):
        telegram.send(42, "y" * (TELEGRAM_MAX_CHARS + 10), markup={"inline_keyboard": []})
        queued = rows()
        self.assertEqual(len(queued), 2)
        self.assertIsNone(queued[0][4])
        self.assertEqual(json.loads(queued[1][4]), {"inline_keyboard": []})

    def test_a_document_is_queued_as_a_document(self):
        telegram.send_document(42, "/tmp/x.md", caption="cap")
        kind, path = rows()[0][2], rows()[0][5]
        self.assertEqual((kind, path), ("doc", "/tmp/x.md"))

    def test_stripping_tags_leaves_readable_text(self):
        self.assertEqual(telegram.strip_tags("<b>bold</b> &amp; <i>it</i>"), "bold & it")

    def test_dropping_and_bumping_touch_only_one_row(self):
        telegram.send(42, "one")
        telegram.send(42, "two")
        with db_lock:
            first = db.execute("SELECT id FROM outbox ORDER BY id").fetchone()[0]
        telegram.bump_attempts(first)
        with db_lock:
            attempts = db.execute("SELECT attempts FROM outbox ORDER BY id").fetchall()
        self.assertEqual([a[0] for a in attempts], [1, 0])
        telegram.drop_outbox(first)
        self.assertEqual(len(rows()), 1)


if __name__ == "__main__":
    unittest.main()
