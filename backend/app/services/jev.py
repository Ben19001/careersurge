"""Jev (TypeSafe System One) client for the local app.

Used by `local_profile.parse_resume` and by `local_answers`' label mapper and question classifier. Jev only ever
*selects* among options the caller supplies; every value that ends up in the profile or on a
form is copied from the caller's own text, never generated. Flask-free, and it imports no
Supabase and not `app.config`: the key comes from the keychain through `app.store.secrets`.

The `typesafe-sdk` package is not used because it reads `TYPESAFE_API_KEY` from the environment
and documents no key parameter, while the key lives in the keychain.
"""
import logging
import math
import sqlite3

import httpx

from app.store import db, secrets

logger = logging.getLogger(__name__)

# `settings` key: true once TypeSafe refused the stored key (401 or 403), false again after a new key
# is saved or a call is accepted. Read by `GET /api/keys/JEV_API_KEY` for the Settings screen.
KEY_REJECTED_SETTING = "jev_key_rejected"

_URL = "https://api.typesafe.ai/v1/systemone"
# Pinned rather than `jev-latest`: a per-field hit rate measured on real forms holds for one model
# version only.
_MODEL = "jev-1.13.0"
# The user is waiting on the review screen, so a slow Jev must fall back, not stall it. httpx applies
# this to each of connect, read and write, not to the whole call.
_TIMEOUT_SECONDS = 10
# A choice question allows 255 criteria; one of them is the `none` option added here.
MAX_OPTIONS = 254
# The extra criteria key meaning "the document has no such value". Jev's Choice criteria are a map
# of option name to optional description, the names are what the model reads, and `choice` returns
# the name, so the option text itself is the key.
_NONE_TEXT = "None of these"


class JevUnavailable(Exception):
    """Jev could not answer; callers degrade instead of failing.

    `reason` is one of no_key, network, rate_limited, server, bad_response.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def choose(state: dict, questions: dict[str, tuple[str, list[str]]]) -> dict[str, tuple[str | None, float]]:
    """One Jev request answering every question; returns `{question_id: (chosen option, confidence)}`.

    `questions` maps our id to `(instructions, options)`. Every question also gets a `none`
    option, so a field the document does not contain is an answer instead of a forced guess;
    `none` comes back as `(None, confidence)`. The options themselves are the criteria keys
    (descriptions null) and `choice` returns the chosen key, so no index mapping exists. No retry:
    the caller degrades.

    Raises ValueError for more than `MAX_OPTIONS` options, a duplicate option, or an option equal
    to the none text (all caller bugs, because the keys would collide), and `JevUnavailable` for a
    missing or rejected key (401/403), network failure, 429, 5xx or an unreadable answer.
    """
    key = secrets.get_secret("JEV_API_KEY")
    if not key:
        raise JevUnavailable("no_key")

    body_questions = {}
    for question_id, (instructions, options) in questions.items():
        if len(options) > MAX_OPTIONS:
            raise ValueError(f"question {question_id!r} has {len(options)} options; the limit is {MAX_OPTIONS}")
        if len(set(options)) != len(options):
            raise ValueError(f"question {question_id!r} has duplicate options")
        if _NONE_TEXT in options:
            raise ValueError(f"question {question_id!r} has an option equal to {_NONE_TEXT!r}")
        criteria = {option: None for option in options}
        criteria[_NONE_TEXT] = None
        body_questions[question_id] = {"type": "choice", "instructions": instructions, "criteria": criteria}

    try:
        response = httpx.post(
            _URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": _MODEL, "state": state, "questions": body_questions},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        # Timeouts are a TransportError subclass. The message can carry the URL, never the key.
        logger.warning("jev request failed: %s", type(exc).__name__)
        raise JevUnavailable("network") from exc

    status = response.status_code
    if status in (401, 403):
        # Every caller degrades on JevUnavailable, so a refused key used to be invisible: Settings
        # said "Saved" while each call fell back to regex. The flag is what Settings shows instead;
        # saving a new key or the next accepted call clears it. A database error only costs the
        # flag: callers catch JevUnavailable alone, and the resume parse must never fail because of Jev.
        try:
            db.set_setting(KEY_REJECTED_SETTING, True)
        except sqlite3.Error as exc:
            logger.warning("could not record the refused jev key: %s", type(exc).__name__)
        raise JevUnavailable("no_key")
    if status == 429:
        raise JevUnavailable("rate_limited")
    if status >= 500:
        raise JevUnavailable("server")
    if status != 200:
        raise JevUnavailable("bad_response")

    try:
        answers = response.json()["answers"]
        result = {}
        for question_id, (_, options) in questions.items():
            answer = answers[question_id]
            choice = answer["choice"]
            confidence = float(answer["confidence"])
            # `float()` accepts "nan", and NaN fails every comparison, so `confidence < floor` would let it
            # through as confident; an infinity is no confidence either.
            if not math.isfinite(confidence):
                raise ValueError(confidence)
            if choice != _NONE_TEXT and choice not in options:
                raise KeyError(choice)
            result[question_id] = (None if choice == _NONE_TEXT else choice, confidence)
    except (ValueError, KeyError, TypeError) as exc:
        raise JevUnavailable("bad_response") from exc
    try:
        if db.get_setting(KEY_REJECTED_SETTING):
            db.set_setting(KEY_REJECTED_SETTING, False)
    except sqlite3.Error as exc:
        logger.warning("could not clear the refused jev key flag: %s", type(exc).__name__)
    return result
