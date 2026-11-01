"""The local app's SQLite store: data dir, connections, migrations, settings.

Used by every local-first service through `get_db()`. Stdlib `sqlite3` and plain
SQL on purpose: the workers hop threads, the claim UPDATEs are single statements, and business logic
stays out of SQL, so an ORM adds nothing. Imports no Flask, Supabase or `app.config`.
"""
import json
import os
import re
import sqlite3
from pathlib import Path

from platformdirs import user_data_dir

# One constant because the brand is unresolved; it names the OS data dir and the keychain service.
APP_NAME = "CareerSurge"

_DB_FILENAME = "careersurge.db"
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class SchemaTooNew(RuntimeError):
    """The database on disk was written by a newer app than this one."""


def data_dir() -> Path:
    """The per-user data directory, created 0700 (it holds DOB, work authorization and resumes).

    `CAREERSURGE_DATA_DIR` overrides the OS location (tests, dev only: it is re-moded to 0700,
    so point it at a directory of its own, never a shared one). The directory must be on a
    local disk: WAL does not work on network filesystems. Mode bits are POSIX-only; on Windows
    the per-user profile directory is the protection.
    """
    override = os.environ.get("CAREERSURGE_DATA_DIR")
    path = Path(override) if override else Path(user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def db_path() -> Path:
    """The database file inside `data_dir()`, created empty with mode 0600 if missing.

    Creating it ourselves (rather than letting sqlite3 do it) is what makes the mode 0600 from
    the first byte instead of the umask's guess.
    """
    path = data_dir() / _DB_FILENAME
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    if os.name == "posix":
        os.chmod(path, 0o600)
    return path


def connect() -> sqlite3.Connection:
    """Open a fresh connection with the per-connection pragmas set.

    One connection per call, because `asyncio.to_thread` hops to a different worker thread
    each call and a `sqlite3` connection is single-thread. Autocommit mode (`isolation_level=None`):
    callers open transactions explicitly with `BEGIN IMMEDIATE`, which is what the claim and
    compare-and-set updates of the apply worker need.

    - `foreign_keys=ON`: SQLite defaults it OFF per connection, which silently disables every
      ON DELETE CASCADE in the schema.
    - `journal_mode=WAL`: readers do not block the one writer.
    - `busy_timeout=5000`: writers queue behind WAL's single writer for milliseconds, so 5 s
      absorbs three queued writers and still surfaces a hung one.
    - `synchronous=FULL`: under NORMAL a power loss can drop the last commit, and
      `submit_attempted_at` must survive. Writes are tens per hour, so the fsync is free.
    """
    conn = sqlite3.connect(db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def migrate() -> int:
    """Apply every `migrations/NNNN_*.sql` newer than the file's `PRAGMA user_version`.

    Called once at app startup (`local_main`). Each file runs in one transaction that also
    sets `user_version`, so a failed migration leaves the previous version intact. Raises
    `SchemaTooNew` before touching anything when the file is newer than this app's newest
    migration: an older app would otherwise run against a schema it does not know. Returns the
    resulting version.
    """
    migrations = []
    for path in _MIGRATIONS.glob("*.sql"):
        match = re.match(r"(\d+)_", path.name)
        if match is None:
            continue
        migrations.append((int(match.group(1)), path))
    migrations.sort()
    newest = migrations[-1][0]

    # A bare connection for the version read: connect() would switch the file to WAL first, which
    # rewrites the header of a database we may be about to refuse.
    probe = sqlite3.connect(db_path())
    current = probe.execute("PRAGMA user_version").fetchone()[0]
    probe.close()
    if current > newest:
        raise SchemaTooNew(f"database schema version {current} is newer than this app's {newest}; "
                           f"update the app")

    conn = connect()
    try:
        for version, path in migrations:
            if version <= current:
                continue
            script = (f"BEGIN IMMEDIATE;\n{path.read_text()}\n"
                      f"PRAGMA user_version = {version};\nCOMMIT;")
            try:
                conn.executescript(script)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            current = version
    finally:
        conn.close()
    return current


def get_db() -> sqlite3.Connection:
    """The one seam tests rebind to point the services at a temp database."""
    return connect()


def get_setting(key: str, default=None):
    """The JSON-decoded value stored under `key`, or `default` when the key is absent."""
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return default if row is None else json.loads(row["value"])


def set_setting(key: str, value) -> None:
    """Store `value` (anything `json.dumps` accepts) under `key`, replacing any prior value."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
    finally:
        conn.close()
