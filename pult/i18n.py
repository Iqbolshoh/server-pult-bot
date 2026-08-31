"""Everything a human reads, kept in locales/<lang>.json.

Code, comments and config stay English; only the Telegram-facing text is
translated. Uzbek is the reference locale because it is the one that was written
first, English is what an unknown buyer gets, Russian is the market.
"""

import json
import os

from .core import LOCALES_DIR, log
from .config import CFG

FALLBACK_LANG = "en"
LANGUAGE_NAMES = {"uz": "🇺🇿 O'zbekcha", "ru": "🇷🇺 Русский", "en": "🇬🇧 English", "tj": "🇹🇯 Тоҷикӣ"}
_bundles = {}
_missing = set()
def available_languages():
    """Language codes with a locale file, ordered by LANGUAGE_NAMES then name."""
    try:
        codes = [f[:-5] for f in os.listdir(LOCALES_DIR) if f.endswith(".json")]
    except OSError:
        return []
    known = [c for c in LANGUAGE_NAMES if c in codes]
    return known + sorted(c for c in codes if c not in LANGUAGE_NAMES)
def language_name(code):
    return LANGUAGE_NAMES.get(code, code)
def bundle(lang):
    """Parsed locale file, cached. An unreadable file behaves as an empty one."""
    if lang not in _bundles:
        path = os.path.join(LOCALES_DIR, f"{lang}.json")
        try:
            with open(path, encoding="utf-8") as f:
                _bundles[lang] = json.load(f)
        except (OSError, ValueError) as e:
            log(f"locale {lang} unreadable: {e}")
            _bundles[lang] = {}
    return _bundles[lang]
def current_language():
    return CFG.get("language") or FALLBACK_LANG
def t(key, lang=None, **kw):
    """Translate one key. Falls back to English, then to the key itself.

    A missing key is logged once and rendered visibly rather than silently
    borrowing another language, so a half-translated locale cannot hide.
    """
    lang = lang or current_language()
    text = bundle(lang).get(key)
    if text is None and lang != FALLBACK_LANG:
        text = bundle(FALLBACK_LANG).get(key)
        if text is not None and (lang, key) not in _missing:
            _missing.add((lang, key))
            log(f"locale {lang}: missing key {key!r}")
    if text is None:
        if key not in _missing:
            _missing.add(key)
            log(f"locale: unknown key {key!r}")
        return f"⟪{key}⟫"
    if isinstance(text, list):
        text = "\n".join(text)
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError) as e:
        log(f"locale {lang}: bad placeholder in {key!r}: {e}")
        return text
def reset_cache():
    """Drop parsed locales -- used after a language change and by the tests."""
    _bundles.clear()
    _missing.clear()
