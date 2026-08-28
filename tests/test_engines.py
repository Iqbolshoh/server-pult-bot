"""The engine layer, checked against streams recorded from the real CLIs."""

import unittest

from . import context
from pult import engines
from pult.config import CFG


def collect(events, reader):
    out = []
    for event in events:
        out.extend(reader(event))
    return out
class ClaudeStreamTest(unittest.TestCase):
    """tests/fixtures/claude_stream.jsonl is a real `claude -p` run."""

    @classmethod
    def setUpClass(cls):
        cls.events = collect(context.fixture_events("claude_stream.jsonl"),
                             engines._claude_events)
        cls.kinds = [kind for kind, _ in cls.events]

    def test_session_id_is_read(self):
        sessions = {payload for kind, payload in self.events if kind == "session"}
        self.assertEqual(sessions, {"55405ba4-a2f8-4bd2-b8e3-ae9623584a61"})

    def test_tool_use_is_reported(self):
        tools = [payload for kind, payload in self.events if kind == "tool"]
        self.assertTrue(tools)
        name, params = tools[0]
        self.assertEqual(name, "Write")
        self.assertIn("file_path", params)

    def test_partial_text_is_streamed(self):
        said = "".join(payload for kind, payload in self.events if kind == "say")
        self.assertIn("permission", said.lower())

    def test_thinking_and_status_are_understood(self):
        self.assertIn("think", self.kinds)
        self.assertIn(("status", "requesting"), self.events)

    def test_rate_limit_event_is_read(self):
        limits = [payload for kind, payload in self.events if kind == "limit"]
        self.assertEqual(len(limits), 1)
        info = limits[0]
        self.assertEqual(info["status"], "allowed")
        self.assertEqual(info["type"], "five_hour")
        self.assertAlmostEqual(info["windows"]["five_hour"]["utilization"], 0.02)
        self.assertEqual(info["windows"]["five_hour"]["resets_at"], 1787956800)
        self.assertEqual(info["windows"]["seven_day"]["resets_at"], 1788134400)

    def test_result_carries_tokens_and_no_false_limit(self):
        results = [payload for kind, payload in self.events if kind == "result"]
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertGreater(result["tokens"], 20000)   # cache buckets included
        self.assertEqual(result["turns"], 2)
        self.assertFalse(result["error"])
        self.assertFalse(result["limited"])
        self.assertFalse(result["busy"])
class ClaudeFailureShapeTest(unittest.TestCase):
    """Only a spent window may walk the chain; everything else must not."""

    def limited(self, **fields):
        event = dict({"type": "result", "is_error": True, "result": ""}, **fields)
        payload = dict(engines._claude_events(event))["result"]
        return payload["limited"], payload["busy"]

    def test_429_is_a_limit(self):
        self.assertEqual(self.limited(api_error_status=429), (True, False))

    def test_usage_limit_text_is_a_limit(self):
        self.assertEqual(self.limited(result="Claude usage limit reached")[0], True)

    def test_overload_is_busy_not_a_limit(self):
        limited, busy = self.limited(api_error_status=529, result="Overloaded")
        self.assertFalse(limited)
        self.assertTrue(busy)

    def test_ordinary_failure_is_neither(self):
        self.assertEqual(self.limited(result="TypeError on line 4"), (False, False))

    def test_success_is_never_a_limit(self):
        event = {"type": "result", "is_error": False,
                 "result": "I hit the usage limit in the file I read"}
        payload = dict(engines._claude_events(event))["result"]
        self.assertFalse(payload["limited"])
class AgyStreamTest(unittest.TestCase):
    """tests/fixtures/agy_stream.jsonl is a real `agy -p` run."""

    @classmethod
    def setUpClass(cls):
        cls.events = collect(context.fixture_events("agy_stream.jsonl"),
                             engines._agy_events)

    def test_conversation_id_comes_from_init(self):
        # A killed or timed-out run never reaches a result event, so the id has to
        # be taken from the first line as well.
        first_kind, first_payload = self.events[0]
        self.assertEqual(first_kind, "session")
        self.assertEqual(first_payload, "327f9061-d074-4982-b11d-61539f1aa738")

    def test_tool_step_is_reported(self):
        tools = [payload for kind, payload in self.events if kind == "tool"]
        self.assertEqual(tools[0][0], "run_command")
        self.assertEqual(tools[0][1]["CommandLine"], "pwd")

    def test_usage_arrives_before_the_result(self):
        usage = [payload for kind, payload in self.events if kind == "usage"]
        self.assertEqual(usage[0], 14188)

    def test_cancelled_run_is_an_error_but_not_a_limit(self):
        results = [payload for kind, payload in self.events if kind == "result"]
        cancelled = results[0]
        self.assertTrue(cancelled["error"])
        self.assertFalse(cancelled["limited"])
        self.assertIn("CANCELED", cancelled["text"])

    def test_successful_run_is_clean(self):
        results = [payload for kind, payload in self.events if kind == "result"]
        ok = results[-1]
        self.assertFalse(ok["error"])
        self.assertEqual(ok["text"], "OK")
        self.assertEqual(ok["tokens"], 13856)

    def test_quota_status_is_a_limit(self):
        event = {"event": "result", "result": {"status": "RESOURCE_EXHAUSTED",
                                               "response": "", "error": "out of quota"}}
        payload = dict(engines._agy_events(event))["result"]
        self.assertTrue(payload["limited"])
class CatalogueTest(unittest.TestCase):
    def setUp(self):
        self.models = engines.parse_agy_catalogue(context.fixture("agy_models.txt"))
        self.by_id = {m["id"]: m for m in self.models}

    def test_effort_suffixes_collapse_into_one_model(self):
        self.assertIn("gemini-3.7-flash", self.by_id)
        self.assertEqual(self.by_id["gemini-3.7-flash"]["efforts"],
                         ["low", "medium", "high"])
        self.assertNotIn("gemini-3.7-flash-high", self.by_id)

    def test_a_model_without_a_medium_tier_says_so(self):
        self.assertEqual(self.by_id["gemini-3.1-pro"]["efforts"], ["low", "high"])

    def test_unsuffixed_models_take_no_effort(self):
        self.assertEqual(self.by_id["claude-sonnet-4-6"]["efforts"], [])

    def test_labels_lose_the_effort_parenthesis(self):
        self.assertNotIn("(High)", self.by_id["gemini-3.7-flash"]["label"])

    def test_noise_lines_are_ignored(self):
        self.assertNotIn("Fetching", " ".join(self.by_id))
class EffortTest(unittest.TestCase):
    def test_claude_reaches_max(self):
        self.assertEqual(engines.clamp_effort("claude", "max", "opus"), "max")

    def test_agy_stops_at_high(self):
        self.assertEqual(engines.clamp_effort("agy", "max", "gemini-3.7-flash"), "high")

    def test_a_model_without_medium_gets_the_nearest_tier(self):
        self.assertIn(engines.clamp_effort("agy", "medium", "gemini-3.1-pro"),
                      ("low", "high"))

    def test_a_model_with_no_effort_support_gets_none(self):
        self.assertIsNone(engines.clamp_effort("agy", "high", "claude-sonnet-4-6"))

    def test_empty_effort_stays_empty(self):
        self.assertIsNone(engines.clamp_effort("claude", "", "opus"))
class ModelResolutionTest(unittest.TestCase):
    def test_an_agy_model_named_claude_resolves_to_agy(self):
        self.assertEqual(engines.resolve_model("claude-sonnet-4-6")[0], "agy")

    def test_a_claude_alias_resolves_to_claude(self):
        self.assertEqual(engines.resolve_model("opus"), ("claude", "opus"))

    def test_fable_is_offered(self):
        self.assertIn("fable", [m["id"] for m in engines.CLAUDE_MODELS])

    def test_a_prefix_is_enough(self):
        self.assertEqual(engines.resolve_model("gemini-3.7"), ("agy", "gemini-3.7-flash"))

    def test_default_clears_the_claude_model(self):
        self.assertEqual(engines.resolve_model("-"), ("claude", ""))
class BuildTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: CFG[k] for k in ("safe_mode", "autocompact", "stream_words",
                                          "permission_mode", "agy_flags", "effort")}

    def tearDown(self):
        CFG.update(self.saved)

    def claude(self, **cfg):
        CFG.update(cfg)
        cmd, stdin = engines._claude_build("do it", "", None, "sys", "opus", "high")
        return cmd, stdin

    def test_a_fresh_run_gets_a_session_id_and_the_prompt_on_stdin(self):
        cmd, stdin = self.claude()
        self.assertIn("--session-id", cmd)
        self.assertEqual(stdin, "do it\n")

    def test_resume_replaces_session_id(self):
        cmd, _ = engines._claude_build("x", "abc", None, "", "", "")
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_effort_is_passed(self):
        cmd, _ = self.claude()
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_safe_mode_restricts_claude(self):
        self.assertIn("--restricted", self.claude(safe_mode=True)[0])
        self.assertNotIn("--restricted", self.claude(safe_mode=False)[0])

    def test_autocompact_and_streaming_are_configurable(self):
        cmd, _ = self.claude(autocompact="auto", stream_words=True)
        self.assertEqual(cmd[cmd.index("--autocompact") + 1], "auto")
        self.assertIn("--include-partial-messages", cmd)
        cmd, _ = self.claude(autocompact="", stream_words=False)
        self.assertNotIn("--autocompact", cmd)
        self.assertNotIn("--include-partial-messages", cmd)

    def test_plan_mode_overrides_the_permission_mode(self):
        CFG["permission_mode"] = "auto"
        cmd, _ = engines._claude_build("x", "", "plan", "", "", "")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")

    def test_agy_plan_mode_wins_over_operator_flags(self):
        CFG["agy_flags"] = ["--mode", "accept-edits"]
        cmd, stdin = engines._agy_build("x", "sid", "plan", "sys", "gemini-3.7-flash", "high")
        self.assertEqual(cmd.count("--mode"), 1)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")
        self.assertIsNone(stdin)
        self.assertEqual(cmd[cmd.index("--conversation") + 1], "sid")

    def test_agy_safe_mode_uses_the_sandbox(self):
        CFG["agy_flags"] = []
        cmd, _ = engines._agy_build("x", "", None, "", "gemini-3.7-flash", "low")
        self.assertNotIn("--sandbox", cmd)
        CFG["safe_mode"] = True
        cmd, _ = engines._agy_build("x", "", None, "", "gemini-3.7-flash", "low")
        self.assertIn("--sandbox", cmd)

    def test_agy_print_timeout_tracks_the_job_timeout(self):
        CFG["job_timeout_sec"] = 3600
        self.assertEqual(engines.agy_print_timeout(), "3570s")
        CFG["job_timeout_sec"] = 60
        self.assertEqual(engines.agy_print_timeout(), "60s")
        CFG["job_timeout_sec"] = 3600
class SessionTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        self.workdir = "/tmp/session-test"

    def test_a_fresh_project_has_no_session(self):
        self.assertEqual(engines.take_session("claude", self.workdir), ("", ""))

    def test_a_session_survives_until_the_job_limit(self):
        CFG["session_max_jobs"] = 2
        engines.remember_session("claude", self.workdir, "sid-1")
        self.assertEqual(engines.take_session("claude", self.workdir)[0], "sid-1")
        engines.remember_session("claude", self.workdir, "sid-1")
        sid, reason = engines.take_session("claude", self.workdir)
        self.assertEqual(sid, "")
        self.assertTrue(reason)

    def test_an_idle_session_is_retired(self):
        from pult.db import meta_set
        CFG["session_idle_reset_sec"] = 10
        engines.remember_session("agy", self.workdir, "sid-2")
        meta_set(engines.session_key("agy", self.workdir) + ":last", 1)
        sid, reason = engines.take_session("agy", self.workdir)
        self.assertEqual(sid, "")
        self.assertTrue(reason)
class CooldownTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()

    def test_a_cooldown_expires_on_its_own(self):
        import time
        engines.set_cooldown("claude", time.time() + 60)
        self.assertTrue(engines.cooldown_until("claude"))
        engines.set_cooldown("claude", time.time() - 60)
        self.assertFalse(engines.cooldown_until("claude"))

    def test_a_blocked_limit_event_parks_the_engine(self):
        import time
        resets = int(time.time() + 300)
        engines.remember_limit("claude", {"status": "blocked", "resets_at": resets,
                                          "windows": {}})
        self.assertEqual(engines.cooldown_until("claude"), resets)

    def test_an_allowed_limit_event_parks_nothing(self):
        engines.remember_limit("agy", {"status": "allowed", "resets_at": 0, "windows": {}})
        self.assertFalse(engines.cooldown_until("agy"))

    def test_nearly_dry_uses_the_five_hour_window(self):
        engines.remember_limit("claude", {"status": "allowed", "windows": {
            "five_hour": {"utilization": 0.96, "resets_at": 0}}})
        self.assertTrue(engines.engine_nearly_dry("claude"))
        engines.remember_limit("claude", {"status": "allowed", "windows": {
            "five_hour": {"utilization": 0.5, "resets_at": 0}}})
        self.assertFalse(engines.engine_nearly_dry("claude"))


if __name__ == "__main__":
    unittest.main()
