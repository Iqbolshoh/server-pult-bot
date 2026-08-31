"""Translations: no missing key, no silent fallback, no drifting placeholder.

The Laravel sites on this machine were bitten by a translation helper that falls
back to another language and reports success. These tests exist so this bot
cannot repeat it.
"""

import ast
import glob
import itertools
import json
import os
import re
import string
import unittest

from . import context  # noqa: F401 -- must run before pult is imported
from pult import i18n
from pult.core import LOCALES_DIR, SOURCE_DIR

REFERENCE = "uz"
def locale_files():
    return sorted(glob.glob(os.path.join(LOCALES_DIR, "*.json")))
def load(lang):
    with open(os.path.join(LOCALES_DIR, f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)
def source_files():
    return [os.path.join(SOURCE_DIR, "bot.py")] + sorted(
        glob.glob(os.path.join(SOURCE_DIR, "pult", "*.py")))
def used_keys():
    """Every literal key passed to t() anywhere in the source."""
    keys = set()
    for path in source_files():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "t" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
    return keys
def placeholders(value):
    if isinstance(value, list):
        value = "\n".join(value)
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(value) if name}
class LocaleTest(unittest.TestCase):
    def setUp(self):
        self.languages = [os.path.basename(p)[:-5] for p in locale_files()]
        self.bundles = {lang: load(lang) for lang in self.languages}

    def test_four_languages_ship(self):
        self.assertEqual(sorted(self.languages), ["en", "ru", "tj", "uz"])

    def test_every_locale_has_exactly_the_same_keys(self):
        reference = set(self.bundles[REFERENCE])
        for lang, bundle in self.bundles.items():
            self.assertEqual(set(bundle), reference,
                             f"{lang}.json differs: "
                             f"missing={sorted(reference - set(bundle))[:5]} "
                             f"extra={sorted(set(bundle) - reference)[:5]}")

    def test_every_key_used_in_the_code_exists_in_every_locale(self):
        for key in used_keys():
            for lang, bundle in self.bundles.items():
                self.assertIn(key, bundle, f"{key} missing from {lang}.json")

    def test_placeholders_match_across_languages(self):
        reference = self.bundles[REFERENCE]
        for lang, bundle in self.bundles.items():
            for key, value in bundle.items():
                self.assertEqual(placeholders(value), placeholders(reference[key]),
                                 f"{key} has different placeholders in {lang}.json")

    def test_no_value_is_empty(self):
        for lang, bundle in self.bundles.items():
            for key, value in bundle.items():
                text = "\n".join(value) if isinstance(value, list) else value
                self.assertTrue(text.strip(), f"{key} is empty in {lang}.json")

    def test_html_tags_are_balanced(self):
        # Telegram rejects a message with a stray tag, and the retry strips the
        # formatting -- so an unbalanced locale string silently loses its layout.
        for lang, bundle in self.bundles.items():
            for key, value in bundle.items():
                text = "\n".join(value) if isinstance(value, list) else value
                for tag in ("b", "i", "code", "pre"):
                    self.assertEqual(len(re.findall(f"<{tag}>", text)),
                                     len(re.findall(f"</{tag}>", text)),
                                     f"{key} has an unbalanced <{tag}> in {lang}.json")
class TranslateTest(unittest.TestCase):
    def tearDown(self):
        from pult.config import CFG
        CFG["language"] = "uz"
        i18n.reset_cache()

    def test_a_known_key_is_translated_per_language(self):
        from pult.config import CFG
        languages = i18n.available_languages()
        seen = set()
        for lang in languages:
            CFG["language"] = lang
            i18n.reset_cache()
            seen.add(i18n.t("word.on"))
        self.assertEqual(len(seen), len(languages))

    def test_an_unknown_key_is_visible_rather_than_silent(self):
        self.assertEqual(i18n.t("no.such.key"), "⟪no.such.key⟫")

    def test_placeholders_are_filled(self):
        self.assertIn("#7", i18n.t("job.not_found", id=7))

    def test_a_bad_placeholder_does_not_crash_the_screen(self):
        self.assertTrue(i18n.t("job.not_found"))

    def test_a_list_value_becomes_lines(self):
        self.assertIn("\n", i18n.t("help.body"))

    def test_language_names_cover_every_installed_locale(self):
        for code in i18n.available_languages():
            self.assertNotEqual(i18n.language_name(code), code)


class PlaceholderNameTest(unittest.TestCase):
    """t(key, lang=None, **kw) owns two names; a locale must not claim them."""

    def test_no_locale_uses_a_reserved_placeholder_name(self):
        for path in locale_files():
            with open(path, encoding="utf-8") as f:
                bundle = json.load(f)
            for locale_key, value in bundle.items():
                names = placeholders(value)
                self.assertNotIn("key", names, f"{locale_key} in {path}")
                self.assertNotIn("lang", names, f"{locale_key} in {path}")


class DistinctTranslationTest(unittest.TestCase):
    """tj.json once shipped as a byte-for-byte copy of ru.json.

    Every test above passed: the keys matched, the placeholders matched, the
    tags balanced. Only the language was wrong. So the check has to be that two
    locales do not say the same thing, not that they are shaped the same.
    """

    def bundles(self):
        return {os.path.basename(p)[:-5]: load(os.path.basename(p)[:-5])
                for p in locale_files()}

    def test_no_locale_is_a_copy_of_another(self):
        bundles = self.bundles()
        for a, b in itertools.combinations(sorted(bundles), 2):
            self.assertNotEqual(bundles[a], bundles[b],
                                f"{a}.json and {b}.json are identical -- "
                                f"{b} was never translated")

    def test_most_values_differ_between_any_two_locales(self):
        # A handful of values are legitimately shared: bare placeholders,
        # emoji-only markers, product names. A locale that shares most of its
        # text with another is a copy with a few strings edited.
        bundles = self.bundles()
        for a, b in itertools.combinations(sorted(bundles), 2):
            shared = sum(1 for k in bundles[a] if bundles[a][k] == bundles[b][k])
            self.assertLess(shared, len(bundles[a]) // 2,
                            f"{a}.json and {b}.json share {shared} values")


if __name__ == "__main__":
    unittest.main()
