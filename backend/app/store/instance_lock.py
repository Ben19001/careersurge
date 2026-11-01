"""Single-instance guard: one running copy of the local app per data dir.

Startup recovery resets every `in_progress` row to `pending`. A second copy would reset
the first copy's live job while its worker is mid-form, re-queue it, and submit it twice. The OS
releases a file lock when its holder dies, so a crash leaves no stale lock; a port bind is ruled
out because the web port is random.
"""
from filelock import FileLock, Timeout

from app.store.db import APP_NAME, data_dir

# Holds the lock for the process lifetime; dropping the last reference would release it.
_held = None


class AlreadyRunning(RuntimeError):
    """Another copy of the app already holds the lock on this data dir."""


def acquire() -> FileLock:
    """Take the non-blocking lock on `app.lock`; raise `AlreadyRunning` if another copy has it.

    Called first thing at app startup (`local_main`). Returns the lock so a caller that wants to release
    early (tests) can.
    """
    global _held
    directory = data_dir()
    lock = FileLock(directory / "app.lock", timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise AlreadyRunning(f"{APP_NAME} is already running (data dir {directory})") from None
    _held = lock
    return lock
