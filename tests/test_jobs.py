"""Job bookkeeping: the progress card, the hop, and what counts as a limit."""

import time
import unittest

from . import context
from pult import failover, jobs
from pult.config import CFG
from pult.db import db, db_lock


def queue_job(engine="claude", state="queued", step=0, prompt="do it"):
    with db_lock:
        cur = db.execute(
            "INSERT INTO jobs(chat_id,prompt,state,created,project,engine,step) "
            "VALUES(?,?,?,?,?,?,?)", (42, prompt, state, time.time(), "/tmp", engine, step))
        db.commit()
        return cur.lastrowid
def job_row(job_id):
    with db_lock:
        return db.execute("SELECT state,engine,step,handover FROM jobs WHERE id=?",
                          (job_id,)).fetchone()
class LimitSniffTest(unittest.TestCase):
    def test_a_quota_message_on_stderr_counts(self):
        self.assertTrue(jobs.looks_like_limit("Error: usage limit reached"))
        self.assertTrue(jobs.looks_like_limit("RESOURCE_EXHAUSTED"))
        self.assertTrue(jobs.looks_like_limit("HTTP 429 too many requests"))

    def test_an_ordinary_crash_does_not(self):
        self.assertFalse(jobs.looks_like_limit("Traceback: KeyError 'x'"))
        self.assertFalse(jobs.looks_like_limit(""))
class HopTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        CFG["fallback_enabled"] = True
        CFG["fallback_chain"] = [
            {"engine": "claude", "model": "opus", "effort": "high"},
            {"engine": "claude", "model": "sonnet", "effort": "high"},
            {"engine": "agy", "model": "gemini-3.7-flash", "effort": "high"},
        ]
        self.sent = []
        self.real_send = jobs.send
        self.real_path = failover.engine_path
        jobs.send = lambda chat_id, text, **kw: self.sent.append(text)
        failover.engine_path = lambda engine: "/usr/bin/" + engine

    def tearDown(self):
        jobs.send = self.real_send
        failover.engine_path = self.real_path
        CFG["fallback_enabled"] = False

    def test_a_limit_hop_crosses_to_the_other_engine_and_announces_it(self):
        job_id = queue_job(state="running")
        self.assertTrue(jobs.hop_job(job_id, 42, "claude", 0, "limit", time.time() + 60))
        state, engine, step, handover = job_row(job_id)
        self.assertEqual((state, engine, step, handover), ("queued", "agy", 2, "claude"))
        self.assertEqual(len(self.sent), 1)

    def test_a_busy_hop_may_stay_on_the_same_engine(self):
        job_id = queue_job(state="running")
        self.assertTrue(jobs.hop_job(job_id, 42, "claude", 0, "busy"))
        state, engine, step, handover = job_row(job_id)
        self.assertEqual((engine, step), ("claude", 1))
        # Same engine, so the conversation survives and no handover note is needed.
        self.assertIsNone(handover)

    def test_the_chain_is_never_walked_backwards(self):
        job_id = queue_job(engine="agy", state="running", step=2)
        self.assertFalse(jobs.hop_job(job_id, 42, "agy", 2, "limit"))
        self.assertEqual(job_row(job_id)[0], "running")

    def test_nothing_hops_while_the_chain_is_off(self):
        CFG["fallback_enabled"] = False
        job_id = queue_job(state="running")
        self.assertFalse(jobs.hop_job(job_id, 42, "claude", 0, "limit"))
        self.assertEqual(self.sent, [])

    def test_a_cooling_engine_is_never_hopped_onto(self):
        from pult import engines
        engines.set_cooldown("agy", time.time() + 600)
        job_id = queue_job(state="running")
        self.assertFalse(jobs.hop_job(job_id, 42, "claude", 1, "limit"))
class StepSettingsTest(unittest.TestCase):
    def setUp(self):
        CFG["fallback_chain"] = [
            {"engine": "claude", "model": "opus", "effort": "high"},
            {"engine": "claude", "model": "sonnet", "effort": "low"},
        ]
        CFG["model"] = "haiku"
        CFG["effort"] = ""

    def tearDown(self):
        CFG["model"] = ""

    def test_an_unhopped_job_keeps_the_operators_own_model(self):
        self.assertEqual(jobs.step_settings("claude", 0), ("haiku", ""))

    def test_a_hopped_job_uses_the_chain_step(self):
        self.assertEqual(jobs.step_settings("claude", 1), ("sonnet", "low"))

    def test_a_step_for_another_engine_is_ignored(self):
        self.assertEqual(jobs.step_settings("agy", 1)[0], CFG["agy_model"])
class ProgressTest(unittest.TestCase):
    def reporter(self):
        return jobs.ProgressReporter(42, 7, time.time(), "/tmp", "claude")

    def test_it_describes_both_engines_parameter_names(self):
        describe = jobs.ProgressReporter._describe
        self.assertIn("app.py", describe("Edit", {"file_path": "/var/www/app.py"}))
        self.assertIn("ls -la", describe("run_command", {"CommandLine": "ls -la"}))
        self.assertIn("main.go", describe("view_file", {"AbsolutePath": "/srv/main.go"}))

    def test_an_unknown_tool_keeps_its_own_name(self):
        self.assertEqual(jobs.ProgressReporter._describe("Weird", {}), "Weird")

    def test_streamed_words_keep_only_the_tail(self):
        card = self.reporter()
        card.note_words("x" * 900)
        self.assertLessEqual(len(card.words), 400)
        self.assertLessEqual(len(card._tail()), 180)

    def test_words_replace_the_thinking_marker(self):
        card = self.reporter()
        card.note_thinking()
        self.assertTrue(card.thinking)
        card.note_words("hello")
        self.assertFalse(card.thinking)

    def test_a_summary_counts_repeated_tools(self):
        card = self.reporter()
        card.tools = ["Read", "Read", "Edit"]
        summary = card.summary()
        self.assertIn("×2", summary)

    def test_an_empty_summary_stays_empty(self):
        self.assertEqual(self.reporter().summary(), "")
class SystemPromptTest(unittest.TestCase):
    def test_the_local_api_hint_carries_the_port_and_key(self):
        from pult.config import LOCAL_API_KEY
        hint = jobs.local_api_hint()
        self.assertIn(str(CFG["local_api_port"]), hint)
        self.assertIn(LOCAL_API_KEY, hint)

    def test_the_operators_own_prompt_wins_over_the_locale_default(self):
        CFG["system_prompt"] = "MY PROMPT"
        try:
            self.assertTrue(jobs.system_prompt().startswith("MY PROMPT"))
        finally:
            CFG["system_prompt"] = ""
        self.assertNotIn("MY PROMPT", jobs.system_prompt())


if __name__ == "__main__":
    unittest.main()
class ProcessGroupTest(unittest.TestCase):
    """A killed job must take the tree it started with it."""

    def test_signalling_the_group_reaches_a_grandchild(self):
        import os
        import signal
        import subprocess
        from pult.core import signal_group

        marker = "server-pult-group-test"
        proc = subprocess.Popen(
            f"sleep 60 & sleep 60 & wait   # {marker}",
            shell=True, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            group = os.getpgid(proc.pid)
            alive = subprocess.run(["pgrep", "-g", str(group)], capture_output=True,
                                   text=True).stdout.split()
            self.assertGreaterEqual(len(alive), 3)  # shell plus its two sleeps

            signal_group(proc, signal.SIGKILL)
            proc.wait(timeout=10)
            time.sleep(0.5)
            left = subprocess.run(["pgrep", "-g", str(group)], capture_output=True,
                                  text=True).stdout.split()
            self.assertEqual(left, [], "a grandchild outlived the job")
        finally:
            if proc.poll() is None:
                signal_group(proc, signal.SIGKILL)
