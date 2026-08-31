"""The bot's own Telegram profile: command list, description, language codes.

Every one of these locks a defect that was live until 2026-08-31 and that no
test could see, because all three live on Telegram's side rather than in the
code: the Tajik command list was published under a language code no client
sends, the default command scope was never published at all, and the profile
texts were empty in every language.
"""

import json
import unittest

from . import context  # noqa: F401 -- must run before pult is imported

import bot as entrypoint
from pult import i18n
from pult.telegram import api_try  # noqa: F401 -- patched by name below


class LanguageCodeTest(unittest.TestCase):
    def test_tajik_is_published_as_tg_not_tj(self):
        # Our locale file is tj.json; ISO 639-1 -- and every Tajik client --
        # says tg. Publishing under tj reaches nobody.
        self.assertEqual(i18n.telegram_language_code("tj"), "tg")

    def test_every_other_locale_keeps_its_own_code(self):
        for code in ("uz", "ru", "en"):
            self.assertEqual(i18n.telegram_language_code(code), code)


class ProfileTextTest(unittest.TestCase):
    """Telegram caps both strings; over the cap the call fails and the profile
    silently stays empty, which is exactly the state this replaced."""

    def bundles(self):
        out = {}
        for lang in i18n.available_languages():
            with open(f"{i18n.LOCALES_DIR}/{lang}.json", encoding="utf-8") as fh:
                out[lang] = json.load(fh)
        return out

    def test_every_locale_carries_both_profile_strings(self):
        for lang, bundle in self.bundles().items():
            self.assertIn("bot.description", bundle, lang)
            self.assertIn("bot.short", bundle, lang)

    def test_description_fits_telegram_512_character_limit(self):
        for lang, bundle in self.bundles().items():
            self.assertLessEqual(len(bundle["bot.description"]), 512, lang)

    def test_short_description_fits_telegram_120_character_limit(self):
        for lang, bundle in self.bundles().items():
            self.assertLessEqual(len(bundle["bot.short"]), 120, lang)


class PublishTest(unittest.TestCase):
    """publish_commands()/publish_profile() against a recording stand-in."""

    def setUp(self):
        self.calls = []
        self._real = entrypoint.api_try
        entrypoint.api_try = lambda method, params=None, timeout=20: (
            self.calls.append((method, params)) or {"ok": True})

    def tearDown(self):
        entrypoint.api_try = self._real

    def test_the_default_scope_is_published_as_well_as_each_language(self):
        entrypoint.publish_commands()
        codes = [params.get("language_code") for _m, params in self.calls]
        self.assertIn(None, codes, "the default scope was not published")
        for lang in i18n.available_languages():
            self.assertIn(i18n.telegram_language_code(lang), codes)

    def test_no_command_list_is_published_under_a_locale_file_name(self):
        entrypoint.publish_commands()
        codes = [params.get("language_code") for _m, params in self.calls]
        self.assertNotIn("tj", codes)

    def test_every_published_list_holds_every_command(self):
        entrypoint.publish_commands()
        for _method, params in self.calls:
            self.assertEqual(len(params["commands"]), len(entrypoint.BOT_COMMANDS))
            for entry in params["commands"]:
                self.assertNotIn("⟪", entry["description"])

    def test_profile_publishes_both_texts_and_then_stops_repeating_itself(self):
        from pult.db import meta_del
        meta_del("profile_fingerprint")

        entrypoint.publish_profile()
        methods = {method for method, _params in self.calls}
        self.assertEqual(methods, {"setMyDescription", "setMyShortDescription"})
        self.assertNotIn("setMyName", methods)

        # A restart must not spend calls re-sending text that has not changed.
        self.calls.clear()
        entrypoint.publish_profile()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
