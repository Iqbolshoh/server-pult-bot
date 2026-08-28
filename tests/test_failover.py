"""The chain: who it skips, when it hops, and what it tells the next engine."""

import time
import unittest

from . import context
from pult import engines, failover
from pult.config import CFG

CHAIN = [
    {"engine": "claude", "model": "opus", "effort": "high"},
    {"engine": "claude", "model": "sonnet", "effort": "high"},
    {"engine": "agy", "model": "gemini-3.7-flash", "effort": "high"},
    {"engine": "agy", "model": "gpt-oss-120b", "effort": "medium"},
]
class ChainTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        CFG["fallback_chain"] = [dict(step) for step in CHAIN]
        CFG["fallback_enabled"] = True
        # Both binaries "exist" for these tests unless a case says otherwise.
        self.real_path = engines.engine_path
        failover.engine_path = lambda engine: "/usr/bin/" + engine

    def tearDown(self):
        failover.engine_path = self.real_path
        CFG["fallback_enabled"] = False

    def test_an_unknown_engine_is_dropped_from_the_chain(self):
        CFG["fallback_chain"] = CHAIN + [{"engine": "ghost", "model": "x"}]
        self.assertEqual(len(failover.chain()), len(CHAIN))

    def test_the_first_usable_step_is_the_first_step(self):
        index, step = failover.next_step(0)
        self.assertEqual((index, step["model"]), (0, "opus"))

    def test_a_cooling_engine_is_skipped_before_anything_is_spent(self):
        engines.set_cooldown("claude", time.time() + 600)
        index, step = failover.next_step(0)
        self.assertEqual((index, step["engine"]), (2, "agy"))

    def test_the_chain_walks_forward_only(self):
        index, _ = failover.next_step(2)
        self.assertEqual(index, 2)
        index, _ = failover.next_step(4)
        self.assertIsNone(index)

    def test_an_excluded_engine_is_never_chosen(self):
        index, step = failover.next_step(1, exclude_engines=("claude",))
        self.assertEqual(step["engine"], "agy")
        self.assertEqual(index, 2)

    def test_a_missing_binary_is_skipped(self):
        failover.engine_path = lambda engine: None if engine == "claude" else "/usr/bin/agy"
        index, step = failover.next_step(0)
        self.assertEqual(step["engine"], "agy")

    def test_nothing_is_ready_when_every_engine_is_out(self):
        engines.set_cooldown("claude", time.time() + 600)
        engines.set_cooldown("agy", time.time() + 600)
        self.assertEqual(failover.next_step(0), (None, None))
        self.assertEqual(failover.engines_ready(), [])

    def test_reordering_swaps_two_steps(self):
        moved = failover.move_step(2, -1)
        self.assertEqual(moved, 1)
        self.assertEqual(failover.chain()[1]["engine"], "agy")
        self.assertIsNone(failover.move_step(0, -1))

    def test_step_effort_is_clamped_per_engine(self):
        self.assertEqual(failover.step_effort(CHAIN[0], "claude"), "high")
        self.assertEqual(
            failover.step_effort({"engine": "agy", "model": "gemini-3.7-flash",
                                  "effort": "max"}, "agy"), "high")
class StepAsideTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        CFG["fallback_chain"] = [dict(step) for step in CHAIN]
        CFG["fallback_enabled"] = True
        self.real_path = engines.engine_path
        failover.engine_path = lambda engine: "/usr/bin/" + engine

    def tearDown(self):
        failover.engine_path = self.real_path
        CFG["fallback_enabled"] = False

    def test_a_nearly_dry_engine_steps_aside_when_another_is_ready(self):
        engines.remember_limit("claude", {"status": "allowed", "windows": {
            "five_hour": {"utilization": 0.97, "resets_at": 0}}})
        self.assertTrue(failover.should_step_aside("claude"))

    def test_a_healthy_engine_keeps_its_jobs(self):
        engines.remember_limit("claude", {"status": "allowed", "windows": {
            "five_hour": {"utilization": 0.1, "resets_at": 0}}})
        self.assertFalse(failover.should_step_aside("claude"))

    def test_nothing_steps_aside_while_the_chain_is_off(self):
        CFG["fallback_enabled"] = False
        engines.remember_limit("claude", {"status": "allowed", "windows": {
            "five_hour": {"utilization": 0.99, "resets_at": 0}}})
        self.assertFalse(failover.should_step_aside("claude"))

    def test_nothing_steps_aside_when_it_is_the_only_engine_left(self):
        engines.remember_limit("claude", {"status": "allowed", "windows": {
            "five_hour": {"utilization": 0.99, "resets_at": 0}}})
        engines.set_cooldown("agy", time.time() + 600)
        self.assertFalse(failover.should_step_aside("claude"))
class HandoverTest(unittest.TestCase):
    def test_the_next_engine_is_told_to_look_before_it_touches_anything(self):
        prompt = failover.handover_prompt("Fix the nginx config", "claude")
        self.assertIn("git status", prompt)
        self.assertIn("Fix the nginx config", prompt)

    def test_the_handover_names_the_engine_that_stopped(self):
        self.assertIn("Claude", failover.handover_prompt("x", "claude"))

    def test_every_locale_keeps_the_inspect_instruction(self):
        from pult import i18n
        saved = CFG["language"]
        try:
            for lang in i18n.available_languages():
                CFG["language"] = lang
                i18n.reset_cache()
                self.assertIn("git status", failover.handover_prompt("x", "agy"))
        finally:
            CFG["language"] = saved
            i18n.reset_cache()
class HopReasonTest(unittest.TestCase):
    def test_a_limit_hop_names_the_reset_time(self):
        text = failover.hop_reason_text("limit", "claude", time.time() + 3600)
        self.assertTrue(text)
        self.assertNotIn("{", text)

    def test_a_busy_hop_reads_differently(self):
        self.assertNotEqual(failover.hop_reason_text("busy", "claude", 0),
                            failover.hop_reason_text("limit", "claude", 0))


if __name__ == "__main__":
    unittest.main()
