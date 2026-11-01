"""API-key storage for the local app: OS keychain first, `secrets.json` as the fallback.

Used by the Settings screen (set, status) and by sourcing (get). A local user has no `.env`, so
the Apify and Jev keys live in the OS keychain via `keyring`. Headless Linux often has no
keychain backend; there the value goes to `secrets.json` in the data dir, mode 0600. An env var
of the same name always wins, so dev and CI keep working unchanged. Values are never logged.
"""
import json
import logging
import os
import threading

import keyring
from keyring.errors import InitError, NoKeyringError

from app.store.db import APP_NAME, data_dir

logger = logging.getLogger(__name__)

# The secret names double as their env var names.
KNOWN_SECRETS = ("APIFY_TOKEN", "JEV_API_KEY")

_FILENAME = "secrets.json"

# set_secret reads the whole file, adds one key and writes it back. Two threads doing that at once
# (the services run on `asyncio.to_thread` worker threads) would each start from the same contents and
# the later rename would drop the other's key. One process per data dir is guaranteed by the
# instance lock, so a thread lock is enough.
_file_lock = threading.Lock()


def _read_file() -> dict:
    """The `secrets.json` contents, or `{}` when it is missing or unreadable.

    A truncated file (power loss mid-write) must not lock the user out of saving a key, so it
    reads as empty; the log line names the file, never its contents.
    """
    path = data_dir() / _FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        logger.warning("ignoring unreadable %s", path)
        return {}


def _write_file(values: dict) -> None:
    """Write `secrets.json` atomically with mode 0600 from its first byte."""
    path = data_dir() / _FILENAME
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(values, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def get_secret(name: str) -> str | None:
    """The secret's value, or None when unset: env var, then keychain, then `secrets.json`."""
    if name not in KNOWN_SECRETS:
        raise ValueError(f"unknown secret {name!r}; expected one of {KNOWN_SECRETS}")
    from_env = os.environ.get(name)
    if from_env:
        return from_env
    try:
        from_keychain = keyring.get_password(APP_NAME, name)
    except (NoKeyringError, InitError):
        from_keychain = None
    return from_keychain or _read_file().get(name)


def set_secret(name: str, value: str) -> None:
    """Store `value` in the keychain, or in `secrets.json` when this machine has no keychain.

    A value that reaches the keychain is removed from the file, so it lives in one place. The
    read-modify-write of `secrets.json` runs under `_file_lock` so concurrent saves keep both keys.
    """
    if name not in KNOWN_SECRETS:
        raise ValueError(f"unknown secret {name!r}; expected one of {KNOWN_SECRETS}")
    with _file_lock:
        file_values = _read_file()
        try:
            keyring.set_password(APP_NAME, name, value)
        except (NoKeyringError, InitError):
            logger.warning("no OS keychain available; storing %s in %s", name, data_dir() / _FILENAME)
            _write_file({**file_values, name: value})
            return
        if name in file_values:
            del file_values[name]
            _write_file(file_values)


def secret_status(name: str) -> dict:
    """`{"set": bool, "last4": str | None}` for the Settings screen; never the whole value."""
    value = get_secret(name)
    return {"set": bool(value), "last4": value[-4:] if value else None}
