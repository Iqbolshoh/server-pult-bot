"""The start-up notice.

On 2026-09-01 one unattended-upgrade run made needrestart bounce supervisor four
times in fifty seconds, and the operator got four identical "the bot is up"
cards. The bot was healthy every time -- the message just had no way to tell a
restart it was ordered to do from one the package manager did behind its back.
"""

import unittest

from . import context  # noqa: F401 -- must run before pult is imported

from pult.config import CFG
from pult.maintenance import BOOT_NOTICE_KEY, boot_notice, request_boot_notice
from pult.db import meta_get


class BootNoticeTest(unittest.TestCase):
    def setUp(self):
        context.reset_db()
        self.notify = CFG["notify_on_start"]
        self.cooldown = CFG["boot_notice_cooldown_sec"]
        CFG["notify_on_start"] = True
        CFG["boot_notice_cooldown_sec"] = 900

    def tearDown(self):
        CFG["notify_on_start"] = self.notify
        CFG["boot_notice_cooldown_sec"] = self.cooldown

    def test_first_start_speaks(self):
        self.assertIsNotNone(boot_notice(now=1000))
        self.assertEqual(float(meta_get(BOOT_NOTICE_KEY)), 1000)

    def test_a_restart_storm_speaks_once(self):
        self.assertIsNotNone(boot_notice(now=1000))
        for seconds in (14, 28, 41):
            self.assertIsNone(boot_notice(now=1000 + seconds))

    def test_it_speaks_again_after_the_cooldown(self):
        boot_notice(now=1000)
        self.assertIsNotNone(boot_notice(now=1000 + 901))

    def test_a_requeued_job_is_news_even_inside_the_cooldown(self):
        boot_notice(now=1000)
        note = boot_notice(requeued=2, now=1010)
        self.assertIsNotNone(note)
        self.assertIn("2", note)

    def test_an_ordered_restart_always_confirms(self):
        boot_notice(now=1000)
        request_boot_notice()
        self.assertIsNotNone(boot_notice(now=1005))
        # The flag is spent, so the next restart falls back to the cooldown.
        self.assertIsNone(boot_notice(now=1010))

    def test_notify_on_start_off_still_wins(self):
        CFG["notify_on_start"] = False
        request_boot_notice()
        self.assertIsNone(boot_notice(requeued=3, now=1000))


if __name__ == "__main__":
    unittest.main()
