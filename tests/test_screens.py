"""Every screen, in every language, must be valid Telegram HTML.

The locale tests keep the strings honest; these keep the *rendered* screen
honest, because a screen is assembled from several strings plus live values.
"""

import re
import unittest

from . import context  # noqa: F401 -- must run before pult is imported
from pult import i18n, screens
from pult.config import CFG
from pult.core import RULE

SCREENS = ["start_text", "status_text", "jobs_text", "history_text", "limit_text",
           "engine_text", "model_text", "effort_text", "fallback_text", "settings_text",
           "projects_text", "language_text", "help_text"]
TAGS = ("b", "i", "u", "s", "code", "pre", "blockquote")
def rendered():
    for lang in i18n.available_languages():
        CFG["language"] = lang
        i18n.reset_cache()
        for name in SCREENS:
            yield lang, name, getattr(screens, name)()
class ScreenTest(unittest.TestCase):
    def tearDown(self):
        CFG["language"] = "uz"
        i18n.reset_cache()

    def test_no_screen_shows_a_missing_key(self):
        for lang, name, text in rendered():
            self.assertNotIn("⟪", text, f"{name} in {lang}")

    def test_every_tag_is_closed(self):
        for lang, name, text in rendered():
            for tag in TAGS:
                opened = len(re.findall(f"<{tag}(?: [^>]*)?>", text))
                closed = len(re.findall(f"</{tag}>", text))
                self.assertEqual(opened, closed, f"<{tag}> unbalanced in {name} ({lang})")

    def test_only_telegram_tags_are_used(self):
        allowed = set(TAGS) | {f"/{tag}" for tag in TAGS}
        for lang, name, text in rendered():
            for tag in re.findall(r"<(/?[a-zA-Z][^ >]*)", text):
                self.assertIn(tag, allowed, f"unsupported <{tag}> in {name} ({lang})")

    def test_every_screen_leads_with_a_title_and_a_rule(self):
        # One visual language: a heading, a rule, then content.
        for lang, name, text in rendered():
            if name in ("language_text",):
                continue
            self.assertIn(RULE, text, f"{name} ({lang}) has no rule")

    def test_no_screen_is_too_long_for_one_message(self):
        from pult.core import TELEGRAM_MAX_CHARS
        for lang, name, text in rendered():
            self.assertLess(len(text), TELEGRAM_MAX_CHARS, f"{name} ({lang}) needs splitting")
class CardTest(unittest.TestCase):
    def test_a_blank_line_survives_but_a_dropped_row_does_not(self):
        from pult.core import card
        self.assertEqual(card("a", "", None, "b"), "a\n\nb")

    def test_quote_marks_a_long_body_expandable(self):
        from pult.core import quote
        self.assertIn("expandable", quote("x", expandable=True))
        self.assertNotIn("expandable", quote("x"))


if __name__ == "__main__":
    unittest.main()
