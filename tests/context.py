"""Point the package at a throwaway state directory, then import it.

Every test module imports this first. Without it the package would read the
operator's real config.json and write to the live state.db -- which is exactly
what SERVER_PULT_HOME exists to prevent.
"""

import atexit
import json
import os
import shutil
import tempfile

HOME = tempfile.mkdtemp(prefix="server-pult-test-")
os.environ["SERVER_PULT_HOME"] = HOME
atexit.register(lambda: shutil.rmtree(HOME, ignore_errors=True))

CONFIG = {
    "allowed_user_ids": [42],
    "language": "uz",
    "workdir": HOME,
    "project_globs": [os.path.join(HOME, "*")],
    "engine": "claude",
    "model": "",
    "agy_model": "gemini-3.7-flash",
    "confirm_before_run": False,
    "notify_on_start": False,
}
with open(os.path.join(HOME, "config.json"), "w") as _f:
    json.dump(CONFIG, _f)
with open(os.path.join(HOME, ".env"), "w") as _f:
    _f.write("BOT_TOKEN=123:test-token\nADMIN_CHAT_ID=42\n")
os.makedirs(os.path.join(HOME, "project-one"), exist_ok=True)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()
def fixture_events(name):
    """Every JSON line of a recorded stream, in order."""
    return [json.loads(line) for line in fixture(name).splitlines() if line.strip()]
def reset_db():
    from pult.db import db, db_lock
    with db_lock:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM outbox")
        db.execute("DELETE FROM meta")
        db.commit()
