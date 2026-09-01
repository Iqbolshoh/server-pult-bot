"""What housekeeping throws away.

Two rows were immortal until 2026-09-01. A `pending` job -- a confirm card the
operator never pressed -- was never touched by the jobs delete, so its ▶️ button
still worked days later, against a server that had moved on. And nothing had
ever deleted from `meta`: sessions are blanked rather than dropped, so a project
removed from the disk kept its three rows for good.
"""

import os
import time
import unittest

from . import context  # noqa: F401 -- must run before pult is imported

from pult.config import CFG
from pult.db import db, db_lock, meta_get, meta_keys, meta_set
from pult.jobs import approve_job
from pult.maintenance import expire_pending, prune_meta, stale_meta_keys

DAY = 86400


def add_job(state="pending", age=0, engine="claude"):
    with db_lock:
        cur = db.execute(
            "INSERT INTO jobs(chat_id,prompt,state,created,engine) VALUES(?,?,?,?,?)",
            (42, "task", state, time.time() - age, engine),
        )
        db.commit()
        return cur.lastrowid


def state_of(job_id):
    with db_lock:
        return db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]


class PendingExpiryTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        self.window = CFG["pending_expiry_sec"]
        CFG["pending_expiry_sec"] = DAY

    def tearDown(self):
        CFG["pending_expiry_sec"] = self.window

    def test_a_fresh_card_still_runs(self):
        job_id = add_job(age=60)
        approve_job(job_id, None)
        self.assertEqual(state_of(job_id), "queued")

    def test_an_old_card_is_spent_not_armed(self):
        job_id = add_job(age=3 * DAY)
        note = approve_job(job_id, None)
        self.assertEqual(state_of(job_id), "cancelled")
        self.assertIn(str(job_id), note)

    def test_housekeeping_clears_the_backlog(self):
        old, fresh = add_job(age=3 * DAY), add_job(age=60)
        running = add_job(state="running", age=3 * DAY)
        self.assertEqual(expire_pending(), 1)
        self.assertEqual(state_of(old), "cancelled")
        self.assertEqual(state_of(fresh), "pending")
        self.assertEqual(state_of(running), "running")

    def test_the_window_can_be_turned_off(self):
        CFG["pending_expiry_sec"] = 0
        job_id = add_job(age=30 * DAY)
        self.assertEqual(expire_pending(), 0)
        approve_job(job_id, None)
        self.assertEqual(state_of(job_id), "queued")


class StaleMetaTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        self.live = os.path.join(context.HOME, "project-one")

    def session(self, engine, workdir, sid="abc", last=None):
        base = f"session:{engine}:{workdir}"
        meta_set(base, sid)
        meta_set(base + ":last", time.time() if last is None else last)
        meta_set(base + ":jobs", 1)
        return base

    def test_a_live_session_survives(self):
        base = self.session("claude", self.live)
        self.assertEqual(stale_meta_keys(), [])
        self.assertEqual(meta_get(base), "abc")

    def test_a_deleted_project_takes_its_three_rows_with_it(self):
        self.session("claude", os.path.join(context.HOME, "gone-last-week"))
        self.assertEqual(len(stale_meta_keys()), 3)
        self.assertEqual(prune_meta(), 3)
        self.assertEqual(meta_keys("session:"), [])

    def test_a_cleared_session_stops_being_three_empty_rows(self):
        self.session("claude", self.live, sid="")
        self.assertEqual(len(stale_meta_keys()), 3)

    def test_a_context_too_idle_to_resume_goes(self):
        idle = CFG["session_idle_reset_sec"]
        self.session("claude", self.live, last=time.time() - idle - 60)
        self.assertEqual(len(stale_meta_keys()), 3)

    def test_a_pre_engine_session_key_goes(self):
        # Written before sessions were keyed per engine; nothing reads it now.
        meta_set(f"session:{self.live}", "old")
        meta_set("session_id", "older")
        self.assertEqual(sorted(stale_meta_keys()),
                         sorted([f"session:{self.live}", "session_id"]))

    def test_a_spent_cooldown_goes_and_a_live_one_stays(self):
        meta_set("cooldown:claude", int(time.time()) - 3600)
        meta_set("cooldown:agy", int(time.time()) + 3600)
        self.assertEqual(stale_meta_keys(), ["cooldown:claude"])

    def test_pruning_leaves_everything_it_does_not_understand_alone(self):
        meta_set("update_offset", 12345)
        meta_set("profile_fingerprint", "x")
        prune_meta()
        self.assertEqual(meta_get("update_offset"), "12345")
        self.assertEqual(meta_get("profile_fingerprint"), "x")


if __name__ == "__main__":
    unittest.main()
