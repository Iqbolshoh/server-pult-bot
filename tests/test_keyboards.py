"""The keyboards: nothing repeats the bottom row, and no button is a dead end."""

import inspect
import re
import unittest

from . import context
from pult import handlers, keyboards
from pult.i18n import available_languages, t


def inline_makers():
    """Every zero-argument function in keyboards that builds an inline keyboard."""
    made = {}
    for name, fn in vars(keyboards).items():
        if not callable(fn) or not name.endswith("_menu"):
            continue
        params = inspect.signature(fn).parameters
        if any(p.default is inspect.Parameter.empty for p in params.values()):
            continue
        markup = fn()
        if "inline_keyboard" in markup:
            made[name] = markup
    made["back_to_settings"] = keyboards.back_to_settings()
    # The ones that need a job id are the cards the operator sees most often, so
    # they are exactly the ones a duplicate button must not creep back into.
    made["job_menu"] = keyboards.job_menu(7)
    made["confirm_menu"] = keyboards.confirm_menu(7)
    made["result_menu"] = keyboards.result_menu(7, full=True, planned=True, engine="claude")
    return made
def buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]
class NoDuplicateMenuTest(unittest.TestCase):
    """The bottom keyboard is the menu. An inline copy of it under every message
    was the one piece of the UI the operator asked to have taken away."""

    def test_no_inline_keyboard_repeats_a_bottom_row_label(self):
        bottom = set()
        for lang in available_languages():
            for row in keyboards.KEYBOARD_ROWS:
                for key, _cmd in row:
                    bottom.add(t(key, lang=lang))
        for name, markup in inline_makers().items():
            for button in buttons(markup):
                self.assertNotIn(
                    button["text"], bottom,
                    f"{name} repeats the bottom-keyboard button {button['text']!r}")

    def test_the_inline_menu_is_gone(self):
        self.assertFalse(hasattr(keyboards, "main_menu"))
        self.assertFalse(hasattr(keyboards, "back_menu"))

    def test_the_only_back_button_left_leads_to_settings(self):
        for name, markup in inline_makers().items():
            for button in buttons(markup):
                if button["text"] == t("btn.back"):
                    self.assertEqual(button["callback_data"], "settings",
                                     f"{name} has a back button pointing elsewhere")

    def test_back_is_labelled_back_and_not_menu(self):
        # It used to read "Menu" in all three locales, which is what made it look
        # like a second menu on every screen.
        for lang in available_languages():
            self.assertNotIn("men", t("btn.back", lang=lang).lower())
class NoDeadButtonTest(unittest.TestCase):
    """A callback_data with no branch in handle_callback is a button that spins
    and then does nothing. The source is the only place that can prove it."""

    def test_every_callback_data_has_a_branch(self):
        source = inspect.getsource(handlers.handle_callback)
        handled = set(re.findall(r'data == "([^"]+)"', source))
        prefixes = set(re.findall(r'data\.startswith\("([^"]+)"\)', source))
        for name, markup in inline_makers().items():
            for button in buttons(markup):
                data = button["callback_data"]
                if data == "noop":
                    continue
                ok = data in handled or any(data.startswith(p) for p in prefixes)
                self.assertTrue(ok, f"{name}: nothing handles {data!r}")
class JobButtonTest(unittest.TestCase):
    def test_a_result_card_offers_actions_but_no_navigation(self):
        texts = [b["text"] for b in buttons(keyboards.result_menu(7, full=True))]
        self.assertIn(t("btn.again"), texts)
        self.assertIn(t("btn.full_text"), texts)
        self.assertNotIn(t("btn.back"), texts)
