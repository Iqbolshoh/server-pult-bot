"""Dispatch: the private-chat guard, engine prefixes and keyboard labels."""

import unittest

from . import context
from pult import handlers, keyboards, i18n
from pult.config import CFG
from pult.db import db, db_lock


def outbox_count():
    with db_lock:
        return db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
def job_rows():
    with db_lock:
        return db.execute("SELECT chat_id,prompt,engine,state FROM jobs ORDER BY id").fetchall()
def message(text, user_id=42, chat_id=None):
    chat_id = user_id if chat_id is None else chat_id
    return {"update_id": 1, "message": {"message_id": 1, "from": {"id": user_id},
                                        "chat": {"id": chat_id}, "text": text}}
class PrefixTest(unittest.TestCase):
    def test_each_prefix_picks_its_engine(self):
        self.assertEqual(handlers.split_engine_prefix("c: build it"), ("claude", "build it"))
        self.assertEqual(handlers.split_engine_prefix("a: build it"), ("agy", "build it"))
        self.assertEqual(handlers.split_engine_prefix("b: build it"), ("both", "build it"))

    def test_an_ordinary_colon_is_left_alone(self):
        self.assertEqual(handlers.split_engine_prefix("note: fix this"),
                         (None, "note: fix this"))

    def test_the_prefix_is_case_insensitive_and_ignores_spacing(self):
        self.assertEqual(handlers.split_engine_prefix(" C : go"), ("claude", "go"))
class GuardTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        CFG["allowed_user_ids"] = [42]
        CFG["confirm_before_run"] = False

    def test_a_group_message_is_ignored_even_from_the_owner(self):
        # chat_id != user_id means a group: answering there would leak server state.
        handlers.handle_update(message("hello", user_id=42, chat_id=-100123))
        self.assertEqual(outbox_count(), 0)
        self.assertEqual(job_rows(), [])

    def test_a_stranger_gets_silence_not_a_refusal(self):
        handlers.handle_update(message("hello", user_id=999))
        self.assertEqual(outbox_count(), 0)
        self.assertEqual(job_rows(), [])

    def test_the_owner_gets_a_job(self):
        handlers.handle_update(message("do the thing"))
        rows = job_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1], rows[0][2]), (42, "do the thing", "claude"))

    def test_a_prefix_routes_one_message_to_the_other_engine(self):
        handlers.handle_update(message("a: do it there"))
        self.assertEqual(job_rows()[0][2], "agy")

    def test_both_queues_one_job_per_engine(self):
        handlers.handle_update(message("b: do it twice"))
        self.assertEqual(sorted(row[2] for row in job_rows()), ["agy", "claude"])

    def test_an_empty_task_after_a_prefix_starts_nothing(self):
        handlers.handle_update(message("c:"))
        self.assertEqual(job_rows(), [])
        self.assertEqual(outbox_count(), 1)
class LabelTest(unittest.TestCase):
    def tearDown(self):
        CFG["language"] = "uz"
        i18n.reset_cache()

    def test_every_bottom_label_maps_to_a_command(self):
        for label, command in keyboards.label_commands().items():
            self.assertTrue(command.startswith("/"), label)

    def test_labels_from_another_language_still_resolve(self):
        # Telegram keeps showing the keyboard it was last sent, so a label tapped
        # right after a language switch must not be treated as a task.
        CFG["language"] = "en"
        i18n.reset_cache()
        russian_status = i18n.t("btn.status", lang="ru")
        self.assertEqual(keyboards.label_commands()[russian_status], "/status")

    def test_a_label_never_becomes_a_job(self):
        context.reset_db()
        CFG["allowed_user_ids"] = [42]
        handlers.handle_update(message(i18n.t("btn.status")))
        self.assertEqual(job_rows(), [])
        self.assertGreaterEqual(outbox_count(), 1)


if __name__ == "__main__":
    unittest.main()
