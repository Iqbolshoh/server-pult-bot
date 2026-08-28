"""An old state.db must keep working after an update."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from . import context  # noqa: F401 -- must run before pult is imported
from pult.core import SOURCE_DIR

# The schema as it shipped before engines, tokens and the failover columns existed.
OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, prompt TEXT NOT NULL,
    state TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
    result TEXT, exit_code INTEGER);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
"""
class MigrationTest(unittest.TestCase):
    def test_an_old_database_gains_every_new_column_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory(prefix="server-pult-old-") as home:
            db_path = os.path.join(home, "state.db")
            old = sqlite3.connect(db_path)
            old.executescript(OLD_SCHEMA)
            old.execute("INSERT INTO jobs(chat_id,prompt,state,created) VALUES(1,'old','done',1)")
            old.commit()
            old.close()
            with open(os.path.join(home, ".env"), "w") as f:
                f.write("BOT_TOKEN=1:x\nADMIN_CHAT_ID=1\n")

            env = dict(os.environ, SERVER_PULT_HOME=home, PYTHONPATH=SOURCE_DIR)
            proc = subprocess.run([sys.executable, "-c", "import pult.db"], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("migrated: jobs.engine", proc.stdout)

            after = sqlite3.connect(db_path)
            columns = {row[1] for row in after.execute("PRAGMA table_info(jobs)")}
            self.assertLessEqual({"engine", "tokens", "project", "cost", "turns", "mode",
                                  "step", "handover"}, columns)
            row = after.execute("SELECT prompt,engine,step FROM jobs").fetchone()
            self.assertEqual(row, ("old", "claude", 0))
            outbox = {r[1] for r in after.execute("PRAGMA table_info(outbox)")}
            self.assertLessEqual({"kind", "parse_mode", "markup", "file_path"}, outbox)
            after.close()

    def test_the_package_imports_with_no_config_at_all(self):
        # config.py used to sys.exit(1) at import time, which made every module
        # untestable and every failure invisible until the process died.
        with tempfile.TemporaryDirectory(prefix="server-pult-bare-") as home:
            env = dict(os.environ, SERVER_PULT_HOME=home, PYTHONPATH=SOURCE_DIR)
            proc = subprocess.run(
                [sys.executable, "-c",
                 "from pult.config import config_problems; "
                 "import pult.screens, pult.handlers, pult.jobs; "
                 "print('PROBLEMS', len(config_problems()))"],
                env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PROBLEMS 3", proc.stdout)


if __name__ == "__main__":
    unittest.main()
