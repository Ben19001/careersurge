"""The local SQLite store, the secret store and the single-instance lock.

Every test runs against a throwaway data dir via `CAREERSURGE_DATA_DIR`; nothing touches the real
OS data dir or keychain.
"""
import logging
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.backends import fail  # noqa: E402

from app.store import db, instance_lock, secrets  # noqa: E402

POSIX = os.name == "posix"


class _DataDirTestCase(unittest.TestCase):
    """Points the store at a fresh data dir and a clean secret environment for one test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "data"  # does not exist yet: data_dir() must create it
        env = mock.patch.dict(os.environ, {"CAREERSURGE_DATA_DIR": str(self.data_dir)})
        env.start()
        self.addCleanup(env.stop)
        for name in secrets.KNOWN_SECRETS:
            os.environ.pop(name, None)


class _MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        del self.store[(service, username)]


class _StoreTestCase(_DataDirTestCase):
    def setUp(self):
        super().setUp()
        db.migrate()
        self.conn = db.get_db()
        self.addCleanup(self.conn.close)

    def insert_listing(self, external_id="1", company_key="acme"):
        cursor = self.conn.execute(
            "INSERT INTO listings (source, external_id, url, company_key) "
            "VALUES ('greenhouse', ?, 'https://example.com/job', ?)",
            (external_id, company_key),
        )
        return cursor.lastrowid

    def insert_application(self, listing_id, status="prepared", blocked_reason=None):
        cursor = self.conn.execute(
            "INSERT INTO job_applications (listing_id, ats, status, blocked_reason) "
            "VALUES (?, 'greenhouse', ?, ?)",
            (listing_id, status, blocked_reason),
        )
        return cursor.lastrowid

    def insert_qna(self, normalized_question="q", scope="global", company_key=None, listing_id=None):
        cursor = self.conn.execute(
            "INSERT INTO application_qna_bank "
            "(original_question, normalized_question, question_type, scope, company_key, listing_id) "
            "VALUES ('Q?', ?, 'stable_fact', ?, ?, ?)",
            (normalized_question, scope, company_key, listing_id),
        )
        return cursor.lastrowid


class MigrateTests(_DataDirTestCase):
    def test_empty_data_dir_gets_modes_version_and_nine_tables(self):
        self.assertFalse(self.data_dir.exists())
        self.assertEqual(db.migrate(), 9)

        conn = db.get_db()
        self.addCleanup(conn.close)
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(tables, {
            "settings", "profile", "listings", "sourcing_runs", "job_applications",
            "application_qna_bank", "application_blocking_questions", "label_mappings", "approval_batches"})
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
        if POSIX:
            self.assertEqual(stat.S_IMODE(self.data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.data_dir / "careersurge.db").stat().st_mode), 0o600)

    def test_second_migrate_changes_nothing(self):
        db.migrate()
        db.set_setting("keep", {"me": 1})
        conn = db.get_db()
        self.addCleanup(conn.close)
        schema_before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()

        self.assertEqual(db.migrate(), 9)

        schema_after = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        self.assertEqual([tuple(r) for r in schema_before], [tuple(r) for r in schema_after])
        self.assertEqual(db.get_setting("keep"), {"me": 1})

    def test_newer_schema_on_disk_raises_and_leaves_the_file_untouched(self):
        raw = sqlite3.connect(db.db_path())
        raw.execute("PRAGMA user_version = 99")
        raw.close()
        before = (self.data_dir / "careersurge.db").read_bytes()

        with self.assertRaises(db.SchemaTooNew):
            db.migrate()

        self.assertEqual((self.data_dir / "careersurge.db").read_bytes(), before)

    def test_every_connection_sets_the_pragmas(self):
        db.migrate()
        for _ in range(2):
            conn = db.connect()
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_settings_round_trip_json(self):
        db.migrate()
        self.assertIsNone(db.get_setting("missing"))
        self.assertEqual(db.get_setting("missing", 7), 7)
        db.set_setting("filters", {"roles": ["swe"], "min": 3})
        db.set_setting("filters", {"roles": []})
        self.assertEqual(db.get_setting("filters"), {"roles": []})


class SchemaConstraintTests(_StoreTestCase):
    def test_unknown_status_fails_the_check(self):
        listing = self.insert_listing()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_application(listing, status="done")

    def test_blocked_reason_must_match_blocked_status(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_application(self.insert_listing("1"), "pending", blocked_reason="otp")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_application(self.insert_listing("2"), "blocked_needs_input")
        self.insert_application(self.insert_listing("3"), "blocked_needs_input", "otp")

    def test_one_application_per_listing(self):
        listing = self.insert_listing()
        self.insert_application(listing)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_application(listing, status="pending")

    def test_deleting_an_application_cascades_its_blocking_questions(self):
        listing = self.insert_listing()
        application = self.insert_application(listing, "blocked_needs_input", "question")
        qna = self.insert_qna()
        self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id) VALUES (?, ?)",
                          (application, qna))

        self.conn.execute("DELETE FROM job_applications WHERE id = ?", (application,))

        left = self.conn.execute("SELECT COUNT(*) FROM application_blocking_questions").fetchone()[0]
        self.assertEqual(left, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_qna_bank").fetchone()[0], 1)

    def test_profile_holds_exactly_one_row(self):
        self.conn.execute("INSERT INTO profile (id, email, full_name) VALUES (1, 'a@b.c', 'A B')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO profile (id, email, full_name) VALUES (2, 'a@b.c', 'A B')")

    def test_qna_scope_must_match_its_column(self):
        listing = self.insert_listing()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_qna("a", scope="company", company_key=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_qna("b", scope="global", company_key="acme")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_qna("c", scope="job", company_key="acme", listing_id=listing)
        self.insert_qna("d", scope="company", company_key="acme")
        self.insert_qna("e", scope="job", listing_id=listing)

    def test_qna_uniqueness_is_per_scope(self):
        listing = self.insert_listing()
        self.insert_qna("same", scope="global")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_qna("same", scope="global")
        # The same question is legal at every other scope and per company / listing.
        self.insert_qna("same", scope="company", company_key="acme")
        self.insert_qna("same", scope="company", company_key="other")
        self.insert_qna("same", scope="job", listing_id=listing)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_qna("same", scope="company", company_key="acme")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_qna("same", scope="job", listing_id=listing)

    def test_deleting_a_listing_cascades_its_job_scoped_answers(self):
        listing = self.insert_listing()
        self.insert_qna("job-q", scope="job", listing_id=listing)
        self.insert_qna("global-q", scope="global")

        self.conn.execute("DELETE FROM listings WHERE id = ?", (listing,))

        remaining = [r[0] for r in self.conn.execute("SELECT normalized_question FROM application_qna_bank")]
        self.assertEqual(remaining, ["global-q"])


class SecretStoreTests(_DataDirTestCase):
    def setUp(self):
        super().setUp()
        quiet = logging.NullHandler()  # keeps the fallback warning off stderr
        logging.getLogger("app.store.secrets").addHandler(quiet)
        self.addCleanup(logging.getLogger("app.store.secrets").removeHandler, quiet)

    def use_keyring(self, backend):
        previous = keyring.get_keyring()
        keyring.set_keyring(backend)
        self.addCleanup(keyring.set_keyring, previous)
        return backend

    def test_keychain_available_keeps_the_secret_out_of_files(self):
        backend = self.use_keyring(_MemoryKeyring())

        secrets.set_secret("APIFY_TOKEN", "apify_secret_value_1234")

        self.assertEqual(backend.store[(db.APP_NAME, "APIFY_TOKEN")], "apify_secret_value_1234")
        self.assertEqual(secrets.get_secret("APIFY_TOKEN"), "apify_secret_value_1234")
        self.assertFalse((self.data_dir / "secrets.json").exists())
        status = secrets.secret_status("APIFY_TOKEN")
        self.assertEqual(status, {"set": True, "last4": "1234"})
        self.assertNotIn("apify_secret", repr(status))

    def test_no_keychain_falls_back_to_a_0600_file(self):
        self.use_keyring(fail.Keyring())

        secrets.set_secret("JEV_API_KEY", "jev_secret_value_9876")

        path = self.data_dir / "secrets.json"
        self.assertTrue(path.exists())
        if POSIX:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(secrets.get_secret("JEV_API_KEY"), "jev_secret_value_9876")
        self.assertEqual(secrets.secret_status("JEV_API_KEY"), {"set": True, "last4": "9876"})

    def test_env_var_wins_over_the_stored_value(self):
        self.use_keyring(_MemoryKeyring())
        secrets.set_secret("APIFY_TOKEN", "stored_value_0000")

        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "env_value_1111"}):
            self.assertEqual(secrets.get_secret("APIFY_TOKEN"), "env_value_1111")
            self.assertEqual(secrets.secret_status("APIFY_TOKEN")["last4"], "1111")

    def test_unset_secret_reports_unset(self):
        self.use_keyring(_MemoryKeyring())
        self.assertIsNone(secrets.get_secret("APIFY_TOKEN"))
        self.assertEqual(secrets.secret_status("APIFY_TOKEN"), {"set": False, "last4": None})

    def test_a_keychain_write_removes_the_old_file_copy(self):
        self.use_keyring(fail.Keyring())
        secrets.set_secret("APIFY_TOKEN", "old_file_value_1111")
        self.use_keyring(_MemoryKeyring())

        secrets.set_secret("APIFY_TOKEN", "new_keychain_value_2222")

        self.assertNotIn("old_file_value", (self.data_dir / "secrets.json").read_text())
        self.assertEqual(secrets.get_secret("APIFY_TOKEN"), "new_keychain_value_2222")

    def test_concurrent_saves_in_the_file_fallback_keep_both_keys(self):
        self.use_keyring(fail.Keyring())
        real_read = secrets._read_file

        def slow_read():
            contents = real_read()
            time.sleep(0.2)  # widens the read-modify-write window so an unlocked race shows
            return contents

        threads = [threading.Thread(target=secrets.set_secret, args=(name, f"{name}_value_0000"))
                   for name in secrets.KNOWN_SECRETS]
        with mock.patch.object(secrets, "_read_file", slow_read):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        for name in secrets.KNOWN_SECRETS:
            self.assertEqual(secrets.get_secret(name), f"{name}_value_0000")

    def test_truncated_secrets_file_does_not_block_saving(self):
        self.use_keyring(_MemoryKeyring())
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "secrets.json").write_text("")

        secrets.set_secret("APIFY_TOKEN", "after_crash_value_5555")

        self.assertEqual(secrets.get_secret("APIFY_TOKEN"), "after_crash_value_5555")

    def test_unknown_secret_name_is_rejected(self):
        with self.assertRaises(ValueError):
            secrets.set_secret("PATH", "x")
        with self.assertRaises(ValueError):
            secrets.get_secret("PATH")

    def test_no_secret_operation_logs_the_value(self):
        value = "logged_secret_value_7777"
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = _Capture(level=logging.DEBUG)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, previous_level)

        for backend in (_MemoryKeyring(), fail.Keyring()):
            self.use_keyring(backend)
            secrets.set_secret("APIFY_TOKEN", value)
            secrets.get_secret("APIFY_TOKEN")
            secrets.secret_status("APIFY_TOKEN")

        self.assertTrue(captured, "the no-keychain fallback should log that it fell back")
        self.assertFalse([m for m in captured if value in m])


_HOLDER = """
import sys, time
from app.store import instance_lock
instance_lock.acquire()
print("held", flush=True)
time.sleep(60)
"""


class InstanceLockTests(_DataDirTestCase):
    def tearDown(self):
        instance_lock._held = None

    def test_second_acquire_while_held_raises_naming_the_data_dir(self):
        first = instance_lock.acquire()
        self.addCleanup(first.release)

        with self.assertRaises(instance_lock.AlreadyRunning) as raised:
            instance_lock.acquire()

        self.assertIn(str(self.data_dir), str(raised.exception))

    @unittest.skipUnless(POSIX, "SIGKILL is POSIX-only")
    def test_lock_is_free_after_the_holder_is_killed(self):
        backend_dir = Path(__file__).resolve().parents[1]
        holder = subprocess.Popen([sys.executable, "-c", _HOLDER], cwd=backend_dir,
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")

        with self.assertRaises(instance_lock.AlreadyRunning):
            instance_lock.acquire()

        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)

        lock = instance_lock.acquire()
        self.addCleanup(lock.release)


if __name__ == "__main__":
    unittest.main()
