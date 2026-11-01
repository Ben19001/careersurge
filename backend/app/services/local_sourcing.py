"""Local-first Apify sourcing: run lifecycle, price and budget guard, ingest into SQLite.

Used by the local app's scheduler and Settings screen. The field map below was
copied from the earlier hosted `sourcing.py` and adapted.

A "run" is one actor execution on the user's own Apify account; its "dataset" is the rows it
produced, billed per row. `start_run` starts a run and commits its id to `sourcing_runs` before
returning, `ingest_run` waits on it and writes the dataset into `listings`, and `fail_run` closes a run whose
ingest can never succeed. `local_queues` re-attaches to runs a restart interrupted. Everything blocks on the network, so callers run these
on a worker thread (`asyncio.to_thread`). Flask-free and Supabase-free.
"""
import datetime
import hashlib
import json
import logging
import math
import re
import sqlite3
import ssl
import threading
import time
import urllib.parse
from decimal import Decimal

import impit
from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from app.services import us_states
from app.store import db, secrets

logger = logging.getLogger("careersurge.services.local_sourcing")

ACTOR = "fantastic-jobs/career-site-job-listing-api"
# The actor version tag to run. None runs the actor's default build; pin one once a real run has checked it.
ACTOR_BUILD = None
# Above this many prepared + pending applications at run start the user is asked first.
QUEUE_ASK_ABOVE = 25
# The actor's names for the three ATS the apply workers know. The actor spells Lever `lever.co`;
# `listings.source` stores `lever`.
ACTOR_ATS = ("greenhouse", "lever.co", "ashby")
_ACTOR_MIN_LIMIT = 10
# Unmeasured against a full run.
_RUN_TIMEOUT = datetime.timedelta(minutes=20)
_POLL_WAIT = datetime.timedelta(seconds=10)
_TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED")
# `wait_for_finish` returning None means Apify no longer has the run; it is asked again this
# many seconds apart before the row is given up as failed.
_LOST_RUN_RETRY_DELAYS = (2, 10, 30)
# Pause after a network failure: this many seconds, doubling to the cap, reset by a lap that succeeds.
_NETWORK_BACKOFF_START = 2
_NETWORK_BACKOFF_CAP = 30
# More than this share of dataset rows failing validation means the actor's schema changed, not
# that one hour had a bad posting.
_MAX_SKIPPED_SHARE = 0.20

# Periods per year for the actor's `ai_salary_unit_text`. Salaries are stored annual: 40 h x 52 wk
# is the US full-time year and the actor is US-scoped. A unit outside this map cannot be
# annualized, so the row's salary is stored NULL rather than a number in an unknown unit.
_SALARY_UNIT_PER_YEAR = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}
_WORK_ARRANGEMENTS = frozenset({"On-site", "Hybrid", "Remote OK", "Remote Solely"})
_EXPERIENCE_YEARS = frozenset({"0-2", "2-5", "5-10", "10+"})
_EMPLOYMENT_TYPES = ("FULL_TIME", "PART_TIME", "CONTRACTOR", "INTERN", "TEMPORARY", "VOLUNTEER",
                     "PER_DIEM", "OTHER")
# Spellings the board's own employment-type text uses (lever's is wild: "Full Time Reg | F").
_EMPLOYMENT_TYPE_ALIASES = {
    "full time": "FULL_TIME", "fulltime": "FULL_TIME", "ft": "FULL_TIME",
    "part time": "PART_TIME", "parttime": "PART_TIME", "pt": "PART_TIME",
    "intern": "INTERN", "internship": "INTERN", "intern or coop": "INTERN", "co op": "INTERN",
    "contract": "CONTRACTOR", "contractor": "CONTRACTOR", "contract type": "CONTRACTOR",
    "temp": "TEMPORARY", "per diem": "PER_DIEM",
}

# settings.sourcing_filters key -> actor input name, for the filters sent only when the user set one.
_OPTIONAL_FILTERS = (
    ("title_exclusions", "titleExclusionSearch"),
    ("work_arrangements", "aiWorkArrangementFilter"),
    ("experience_years", "aiExperienceLevelFilter"),
    ("visa_sponsorship", "aiVisaSponsorshipFilter"),
    ("only_with_pay", "hasSalary"),
    ("companies", "organizationSearch"),
    ("company_exclusions", "organizationExclusionSearch"),
)

# Filters whose values must be one of a fixed set (the actor rejects anything else) and the two yes/no ones.
_FILTER_CHOICES = {"employment_types": _EMPLOYMENT_TYPES, "work_arrangements": _WORK_ARRANGEMENTS,
                   "experience_years": _EXPERIENCE_YEARS}
_FILTER_FLAGS = ("visa_sponsorship", "only_with_pay")

# The actor phrase-matches `locationSearch` against "City, State, Country" with every name spelled
# out, so "Los Angeles, CA" matched nothing and every run on it fetched 0 jobs. `start_run` spells
# out a US state code (`us_states.US_STATES`) and these country codes before sending.
_COUNTRY_CODES = {"US": "United States", "USA": "United States", "U.S.": "United States",
                  "U.S.A.": "United States", "UK": "United Kingdom", "U.K.": "United Kingdom"}


def clean_filters(raw) -> dict:
    """The user's search filters with each list stripped of blanks and repeats; ValueError names what is wrong.

    Called by the Settings route before `settings['sourcing_filters']` is stored, so `start_run`
    only ever reads values the actor accepts. The names allowed are titles, locations, employment_types
    and every key of `_OPTIONAL_FILTERS`, so a filter added there is accepted here without another edit.
    Lists hold text; `employment_types`, `work_arrangements` and `experience_years` may hold only their
    fixed choices; `visa_sponsorship` and `only_with_pay` are true or false.
    """
    if not isinstance(raw, dict):
        raise ValueError("sourcing_filters must be an object")
    # Names that must hold a list: titles, locations, every optional filter and every fixed-choice filter...
    list_names = {"titles", "locations"} | {name for name, _ in _OPTIONAL_FILTERS} | set(_FILTER_CHOICES)
    # ...except the two flags, which are true or false.
    list_names -= set(_FILTER_FLAGS)
    cleaned = {}
    for name, value in raw.items():
        if name in _FILTER_FLAGS:
            if not isinstance(value, bool):
                raise ValueError(f"filter {name} must be true or false")
            cleaned[name] = value
            continue
        if name not in list_names:
            raise ValueError(f"unknown filter {name!r}")
        if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
            raise ValueError(f"filter {name} must be a list of text")
        entries = []
        for entry in value:
            entry = entry.strip()
            if entry and entry not in entries:
                entries.append(entry)
        choices = _FILTER_CHOICES.get(name)
        if choices is not None:
            for entry in entries:
                if entry not in choices:
                    raise ValueError(f"filter {name} has {entry!r}, which is not one of {sorted(choices)}")
        cleaned[name] = entries
    return cleaned


_start_lock = threading.Lock()

# Shown by `RefusalNotice` when `ingest_run` raises `SourcingRefused("tls")`.
_TLS_MESSAGE = ("Secure connection to Apify failed: check proxy/antivirus HTTPS inspection, "
                "system clock, or Wi-Fi login page.")


class SourcingRefused(Exception):
    """A run was refused before any charge. `reason` says why, `detail` carries the numbers.

    Reasons: no_key, no_titles, run_in_progress, backlog_unconfirmed, price_unreadable, price_unconfirmed,
    price_rose, budget, and `tls` (raised by `ingest_run`, run row left `started`; `detail["message"]`
    is the text to show). `ingest_failed` is not raised: `local_queues` stores it as the notice for any
    other ingest error. The UI turns each into a notice or a question for the user.
    """

    def __init__(self, reason: str, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _apify_client() -> ApifyClient:
    """The Apify client for the stored token; the one seam tests rebind to a fake."""
    token = secrets.get_secret("APIFY_TOKEN")
    if not token:
        raise SourcingRefused("no_key")
    return ApifyClient(token)


def _is_transient(exc: Exception) -> bool:
    """True when `exc` is the network or Apify being down, so `ingest_run` should wait and ask again.

    A transport error, timeout, HTTP 429 or 5xx is transient because the run may have finished
    and been billed, so giving up would let a restart skip rows already paid for. Anything
    else (a 401 for a rejected token, a bug) is permanent and `ingest_run` raises it.
    """
    if isinstance(exc, ApifyApiError):
        return exc.status_code == 429 or exc.status_code >= 500
    # apify-client sends requests with `impit`, whose connection, timeout and protocol errors
    # are not OSError subclasses. Two of its transport errors are permanent (a request it
    # refuses to send), so those are raised, not waited out.
    impit_down = isinstance(exc, impit.TransportError) and not isinstance(
        exc, (impit.LocalProtocolError, impit.UnsupportedProtocol))
    return impit_down or isinstance(exc, OSError)


def is_fixable_by_user(exc: Exception) -> bool:
    """True when the user can fix the cause of an `ingest_run` error, so a later try may work.

    Used by `local_queues._ingest_and_prepare` to choose between waiting to retry and failing the run.
    Fixable: `SourcingRefused` `tls` (proxy, clock, Wi-Fi login page) or `no_key`, an Apify 401 or 403
    (the token was rejected), and `sqlite3.OperationalError` for a full disk or a locked database.
    Everything else (our own bug such as `_map_item` crashing, a request impit refuses to send, a
    corrupt database, any other Apify 4xx) fails the same way every time, so retrying would only hide it.
    """
    if isinstance(exc, SourcingRefused):
        return exc.reason in ("tls", "no_key")
    if isinstance(exc, ApifyApiError):
        return exc.status_code in (401, 403)
    if isinstance(exc, sqlite3.OperationalError):
        # The low byte is the primary result code; Python reports the extended one (BUSY_SNAPSHOT, ...).
        primary_code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        return primary_code in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return False


def _price_per_job(client, now: datetime.datetime) -> tuple[float, float | None]:
    """The actor's price per job for this account's tier, and its minimal cap (None if it lists none).

    Used by `start_run` before any charge, so a pricing shape it cannot read refuses the run
    instead of billing blind. `pricing_infos` is the actor's price history, scheduled future
    changes included, so only entries with `started_at <= now` count and the newest of those
    (max by `started_at`) is the price in force. A PAY_PER_EVENT actor's price is the sum of its
    charge events, each at the account tier's price (so a per-run start event is counted per job:
    an overstatement, never an understatement). A PRICE_PER_DATASET_ITEM actor's is its flat or
    tiered unit price. Any other shape, a missing tier or a price of zero or less refuses
    `price_unreadable`.
    """
    try:
        actor = client.actor(ACTOR).get()
    except ValueError as exc:
        # pydantic.ValidationError: the client could not parse a pricing model it does not know.
        raise SourcingRefused("price_unreadable", detail=str(exc)[:200]) from exc
    in_force = []
    for info in actor.pricing_infos or []:
        if info.started_at <= now:
            in_force.append(info)
    if not in_force:
        raise SourcingRefused("price_unreadable")
    pricing = max(in_force, key=lambda info: info.started_at)

    account = client.user().get()
    plan = getattr(account, "plan", None)
    plan_tier = plan.tier if plan is not None else None
    tier = (plan_tier or "").upper()

    price = 0.0
    minimal_cap = None
    if pricing.pricing_model == "PAY_PER_EVENT":
        charge_events = pricing.pricing_per_event.actor_charge_events or {}
        for event in charge_events.values():
            tiered = {}
            for name, entry in (event.event_tiered_pricing_usd or {}).items():
                tiered[name.upper()] = entry
            if event.event_price_usd is not None:
                price += event.event_price_usd
            elif tier in tiered:
                price += tiered[tier].tiered_event_price_usd
            else:
                raise SourcingRefused("price_unreadable", tier=tier)
        minimal_cap = pricing.minimal_max_total_charge_usd
    elif pricing.pricing_model == "PRICE_PER_DATASET_ITEM":
        tiered = {}
        for name, entry in (pricing.tiered_pricing or {}).items():
            tiered[name.upper()] = entry
        if pricing.price_per_unit_usd is not None:
            price = pricing.price_per_unit_usd
        elif tier in tiered:
            price = tiered[tier].tiered_price_per_unit_usd
        else:
            raise SourcingRefused("price_unreadable", tier=tier)
    else:
        raise SourcingRefused("price_unreadable", pricing_model=pricing.pricing_model)
    if not math.isfinite(price) or price <= 0:
        raise SourcingRefused("price_unreadable", price=price)
    return round(price, 6), minimal_cap


def month_spend(conn: sqlite3.Connection, now: datetime.datetime) -> float:
    """Dollars of Apify spend recorded on runs started since the first of `now`'s UTC month.

    Used by `start_run` to size a run against what the budget has left, and by `GET /api/settings`
    to show the same figure on the Settings screen, so the guard and the screen cannot drift apart.
    A run still `started` has a NULL `cost_usd` and counts as 0 until it ends.
    """
    month_start = now.strftime("%Y-%m-01T00:00:00.000Z")
    return conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM sourcing_runs WHERE started_at >= ?",
        (month_start,),
    ).fetchone()[0]


def start_run(confirm_backlog: bool = False) -> str:
    """Start one actor run sized to the remaining monthly budget and return its Apify run id.

    Used by the scheduler and the "check now" button. Every guard runs before
    the first charge and refuses with `SourcingRefused`, in this order: no_titles, no_key,
    run_in_progress, backlog_unconfirmed, price_unreadable, price_unconfirmed, price_rose, budget.

    - `limit` is every row the budget still buys (never below the actor's minimum of 10), counted
      in dollars from `sourcing_runs.cost_usd` so a price change or start fee is not hidden. The
      queue never sizes a run; above `QUEUE_ASK_ABOVE` waiting applications it only asks.
    - `max_total_charge_usd` bounds dollars the way `limit` bounds rows. Apify ends a run that
      hits it as SUCCEEDED, so `ingest_run` detects a cap stop from the bill.
    - The price per job comes from `_price_per_job`; any pricing shape it does not recognise
      refuses price_unreadable.

    The `sourcing_runs` row is INSERTed and committed before returning, so a restart can
    reattach with `local_queues.resume_sourcing` instead of starting (and paying for) the same window again.
    """
    # Held from the run_in_progress read to the INSERT: start_run takes seconds on the network, so
    # without it a scheduler tick and a "check now" click both pass the guard and both start a run
    # over the same window. A thread lock is enough because instance_lock allows one process.
    with _start_lock:
        filters = db.get_setting("sourcing_filters", {})
        raw_titles = filters.get("titles") or []
        titles = [title.strip() for title in raw_titles if title.strip()]
        if not titles:
            raise SourcingRefused("no_titles")
        client = _apify_client()

        # Backlog: how many applications are already waiting on the user.
        conn = db.get_db()
        try:
            # A run still `started` has cost_usd NULL, so the budget tally below cannot see it and a
            # second run would size itself against the full budget and re-bill the first one's window.
            in_flight = conn.execute("SELECT COUNT(*) FROM sourcing_runs WHERE status = 'started'").fetchone()[0]
            waiting = conn.execute(
                "SELECT COUNT(*) FROM job_applications WHERE status IN ('prepared', 'pending')"
            ).fetchone()[0]
            now = datetime.datetime.now(datetime.timezone.utc)
            spent = month_spend(conn, now)
            last_success = conn.execute(
                "SELECT started_at FROM sourcing_runs WHERE status = 'succeeded' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if in_flight:
            raise SourcingRefused("run_in_progress", runs=in_flight)
        if waiting > QUEUE_ASK_ABOVE and not confirm_backlog:
            raise SourcingRefused("backlog_unconfirmed", waiting=waiting)

        price, minimal_cap = _price_per_job(client, now)

        confirmed = db.get_setting("sourcing_confirmed_price_usd")
        if confirmed is None:
            raise SourcingRefused("price_unconfirmed", price=price)
        if price > confirmed:
            raise SourcingRefused("price_rose", price=price, confirmed=confirmed)

        # Budget: what is left this calendar month (UTC), and the smallest cap Apify accepts.
        remaining = round(db.get_setting("sourcing_monthly_budget_usd", 5) - spent, 6)
        minimal_cap = minimal_cap or price
        if remaining < minimal_cap:
            raise SourcingRefused("budget", remaining=remaining, minimal=minimal_cap)
        # round to 6 dp before floor/ceil to drop float fuzz, so 2.9999999999 rows is not 2 rows
        # and a cap of 5.0000000001 dollars is not rounded up to 5.01
        limit = max(_ACTOR_MIN_LIMIT, math.floor(round(remaining / price, 6)))
        cap_for_limit = math.ceil(round(limit * price * 100, 6)) / 100
        cap = round(min(remaining, max(cap_for_limit, minimal_cap)), 6)

        # Window: a fixed one the user picked, else the smallest covering the gap since the last
        # successful run (so each job is billed once), `7d` with no earlier run. Never `6m`.
        time_range = db.get_setting("sourcing_time_range", "auto")
        if time_range not in ("1h", "24h", "7d"):
            time_range = "7d"
            if last_success is not None:
                gap = now - datetime.datetime.fromisoformat(last_success["started_at"])
                if gap <= datetime.timedelta(hours=1):
                    time_range = "1h"
                elif gap <= datetime.timedelta(hours=24):
                    time_range = "24h"

        # Locations in the actor's spelled-out form: "Austin, TX" -> "Austin, Texas, United States",
        # "Austin, TX, USA" -> the same, "Remote, US" -> "Remote, United States". A part that is no
        # code is left as typed, and so is a lone code ("CA" could be Canada).
        locations = []
        for location in filters.get("locations") or ["United States"]:
            parts = [part.strip() for part in location.split(",")]
            # A trailing country code first, so the state code before it can be spelled out too.
            country = parts[-1].upper()
            if country in _COUNTRY_CODES:
                parts[-1] = _COUNTRY_CODES[country]
            # A state code: last ("Austin, TX"), or just before "United States" ("Austin, TX, USA").
            if len(parts) >= 2 and parts[-1].upper() in us_states.US_STATES:
                state = us_states.US_STATES[parts[-1].upper()]
                parts = parts[:-1] + [state, "United States"]
            elif len(parts) >= 3 and parts[-1] == "United States" and parts[-2].upper() in us_states.US_STATES:
                parts[-2] = us_states.US_STATES[parts[-2].upper()]
            locations.append(", ".join(parts))

        # Filters sit in actor input because the store page applies them to what is returned and billed.
        run_input = {
            "timeRange": time_range,
            "limit": limit,
            "titleSearch": titles,
            "locationSearch": locations,
            "aiEmploymentTypeFilter": filters.get("employment_types") or ["FULL_TIME"],
            "ats": list(ACTOR_ATS),
            "aiLanguageFilter": ["English"],
        }
        for setting_key, actor_key in _OPTIONAL_FILTERS:
            if filters.get(setting_key):
                run_input[actor_key] = filters[setting_key]

        # `max_items` is Apify's own hard row bound; `ingest_run` reads it back to tell a limit stop
        # from a short dataset. 1 GB above 2,000 rows is the actor docs' ask.
        run = client.actor(ACTOR).start(
            run_input=run_input,
            build=ACTOR_BUILD,
            max_items=limit,
            max_total_charge_usd=Decimal(str(cap)),
            memory_mbytes=512 if limit <= 2000 else 1024,
            run_timeout=_RUN_TIMEOUT,
        )
        conn = db.get_db()
        try:
            conn.execute("INSERT INTO sourcing_runs (apify_run_id, status) VALUES (?, 'started')", (run.id,))
        finally:
            conn.close()
        logger.info("sourcing run %s started: limit=%d cap=%.2f price=%.4f timeRange=%s",
                    run.id, limit, cap, price, time_range)
        return run.id


def ingest_run(run_id: str, should_stop=lambda: False) -> dict:
    """Wait for a started run to end, then write its dataset into `listings`; `{ingested, skipped}`.

    Used by `local_queues` after `start_run` and after a restart (`resume_sourcing`). Polls
    `wait_for_finish` in 10 s laps (Apify holds the request open), logging the dataset's row count
    as progress; `should_stop()` true returns with the row still `started`. The whole write
    (every upsert and the `sourcing_runs` UPDATE) is one transaction with no cursor: a crash
    rolls it all back and the next try re-reads the dataset from its first row.

    - Apify no longer knowing the run (`None`) is retried 3 times, 2 s, 10 s, 30 s apart, then
      the row is marked `failed`.
    - A network or 429/5xx failure (`_is_transient`) in the poll or the dataset read sleeps 2 s,
      doubling to 30 s, and the next lap asks again; it never counts against those retries. A TLS
      certificate failure raises `SourcingRefused("tls")` at once, no sleep, before `_is_transient` is asked.
      Other errors raise, and the row stays `started`. The caller (`local_queues._ingest_and_prepare`)
      asks `is_fixable_by_user`: a fixable error (tls, no key, Apify 401/403, full disk or locked
      database) is retried every 2, 5, then 15 minutes and again on the next launch; any other
      (our own bug, a corrupt database) ends in `fail_run`, which marks the row `failed`.
    - SUCCEEDED -> `succeeded`; FAILED, TIMED-OUT, ABORTED -> `failed`. Any terminal run still
      ingests what its dataset holds, because those rows were billed.
    - A dataset row without `id`, `title`, `url` or a known `source` is skipped, logged at INFO
      with its id (or url) and reason, and counted per reason in `sourcing_runs.rows_skipped` and
      `skip_reasons` (JSON, e.g. {"missing_url": 10}) for the UI to show. More than 20% skipped
      writes nothing and marks the run `failed`.
    - `capped` is set by a cap stop (Apify ends it SUCCEEDED, so the bill is the evidence) or by
      reaching the run's row limit. The `sourcing_runs` row is never deleted: the budget tally,
      the auto time range and the cost shown after each run read it.
    """
    client = _apify_client()
    lost_retries = 0
    backoff = _NETWORK_BACKOFF_START
    while True:
        if should_stop():
            return {"ingested": 0, "skipped": 0}
        try:
            run = client.run(run_id).wait_for_finish(wait_duration=_POLL_WAIT)
            # The dataset read shares the try: a failure there is retried next lap, when
            # wait_for_finish answers at once for a run that has already ended.
            if run is not None:
                status = run.status
                if status in _TERMINAL_STATUSES:
                    items = list(client.dataset(run.default_dataset_id).iterate_items())
                    break
        except Exception as exc:
            # A certificate failure is permanent: waiting will not fix a proxy that inspects HTTPS,
            # a wrong system clock or a Wi-Fi login page. impit is Rust/rustls and has no TLS
            # exception type: a bad certificate and a DNS failure are both `impit.ConnectError`,
            # told apart only by "InvalidCertificate" in rustls's message (impit 0.14.1; pinned in
            # requirements.txt, and `TLS_ERROR_FIXTURE` in the tests fails if an upgrade rewords it).
            # `ssl.SSLCertVerificationError` covers any transport that raises Python's own.
            impit_cert = isinstance(exc, impit.ConnectError) and "InvalidCertificate" in str(exc)
            if impit_cert or isinstance(exc, ssl.SSLCertVerificationError):
                raise SourcingRefused("tls", message=_TLS_MESSAGE, detail=str(exc)[:200]) from exc
            if not _is_transient(exc):
                raise
            logger.warning("apify unreachable (%s); retrying in %ds", type(exc).__name__, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _NETWORK_BACKOFF_CAP)
            continue
        backoff = _NETWORK_BACKOFF_START
        if run is None:
            if lost_retries == len(_LOST_RUN_RETRY_DELAYS):
                conn = db.get_db()
                try:
                    conn.execute("UPDATE sourcing_runs SET status = 'failed' WHERE apify_run_id = ?",
                                 (run_id,))
                finally:
                    conn.close()
                logger.warning("sourcing run %s: Apify no longer has it, marked failed", run_id)
                return {"ingested": 0, "skipped": 0}
            time.sleep(_LOST_RUN_RETRY_DELAYS[lost_retries])
            lost_retries += 1
            continue
        try:
            dataset = client.dataset(run.default_dataset_id).get()
            logger.info("sourcing run %s %s: %d rows so far", run_id, status, dataset.item_count)
        except Exception:  # progress only; the next lap or the dataset read surfaces real faults
            logger.debug("sourcing run %s: progress read failed", run_id)

    mapped = []
    skip_reasons = {}
    for item in items:
        listing, reason = _map_item(item)
        if listing is not None:
            mapped.append(listing)
            continue
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        logger.info("sourcing run %s: skipped row %s (%s)", run_id, item.get("id") or item.get("url"), reason)
    skipped = len(items) - len(mapped)
    too_many_bad = len(items) > 0 and skipped / len(items) > _MAX_SKIPPED_SHARE

    usage = run.usage_total_usd or 0
    cap = run.options.max_total_charge_usd
    max_items = run.options.max_items
    cap_hit = cap is not None and cap > 0 and usage >= cap
    row_limit_hit = max_items is not None and len(items) >= max_items
    capped = int(cap_hit or row_limit_hit)
    final_status = "succeeded" if status == "SUCCEEDED" and not too_many_bad else "failed"
    if too_many_bad:
        logger.warning("sourcing run %s: %d of %d dataset rows invalid, nothing ingested",
                       run_id, skipped, len(items))
        mapped = []

    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for listing in mapped:
                # A listing already stored only has its sighting refreshed: a posting that
                # changed under an approved application must not rewrite what the user approved.
                conn.execute(
                    """
                    INSERT INTO listings (
                        source, external_id, url, title, normalized_title, company, company_key,
                        location, locations, is_remote, employment_type, experience_level,
                        posted_at, work_arrangement, remote_regions, salary_min, salary_max,
                        salary_currency, experience_years, key_skills, visa_sponsorship,
                        responsibilities_summary, requirements_summary, education, content_key,
                        repost_key, sourcing_run_id)
                    VALUES (
                        :source, :external_id, :url, :title, :normalized_title, :company, :company_key,
                        :location, :locations, :is_remote, :employment_type, :experience_level,
                        :posted_at, :work_arrangement, :remote_regions, :salary_min, :salary_max,
                        :salary_currency, :experience_years, :key_skills, :visa_sponsorship,
                        :responsibilities_summary, :requirements_summary, :education, :content_key,
                        :repost_key, :sourcing_run_id)
                    ON CONFLICT (source, external_id) DO UPDATE SET
                        last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        sourcing_run_id = excluded.sourcing_run_id,
                        is_active = 1
                    """,
                    {**listing, "sourcing_run_id": run_id},
                )
            conn.execute(
                "UPDATE sourcing_runs SET status = ?, capped = ?, cost_usd = ?, rows_fetched = ?, "
                "rows_skipped = ?, skip_reasons = ?, "
                "ingested_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE apify_run_id = ?",
                (final_status, capped, usage, len(items), skipped, json.dumps(skip_reasons), run_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    logger.info("sourcing run %s %s: %d ingested, %d skipped, cost %.4f USD, capped=%d",
                run_id, final_status, len(mapped), skipped, usage, capped)
    return {"ingested": len(mapped), "skipped": skipped}


def fail_run(run_id: str) -> None:
    """Mark a run `failed` after an ingest error that will never go away, recording its Apify bill.

    Called by `local_queues._ingest_and_prepare` for an error `is_fixable_by_user` rejects. The run was
    billed, so `cost_usd` takes `usage_total_usd` from Apify when it can be read; a failed row with no
    cost would undercount the monthly budget tally. The error may have come while Apify was still
    running the actor (a bad request in the poll, say), and a run left going would keep billing after
    the row stops guarding against a second run, so a run that has not ended is aborted and its bill is
    read once it has ended. If Apify cannot answer the cost stays empty and the row is still closed,
    because the user must be able to start a new run.
    """
    cost = None
    try:
        run_client = _apify_client().run(run_id)
        run = run_client.get()
        if run is not None and run.status not in _TERMINAL_STATUSES:
            run_client.abort()
            # An aborted run passes through ABORTING; wait for it to end so the bill is final.
            run = run_client.wait_for_finish(wait_duration=_POLL_WAIT)
        # A run still ABORTING after the wait would report a partial bill, so only a final one is recorded.
        if run is not None and run.status in _TERMINAL_STATUSES:
            cost = run.usage_total_usd
    except Exception:  # noqa: BLE001 - closing the row matters more than the cost
        logger.warning("sourcing run %s: its Apify bill could not be read", run_id, exc_info=True)
    conn = db.get_db()
    try:
        conn.execute("UPDATE sourcing_runs SET status = 'failed', cost_usd = ? "
                     "WHERE apify_run_id = ? AND status = 'started'", (cost, run_id))
    finally:
        conn.close()


# Title and company normalization, copied from the hosted `common.py` (since deleted). The local
# app must start without the `supabase` package installed (guard: `test_local_main`).


def normalize_company_key(name: str | None) -> str:
    """Collapse a raw company string to the employer key: strip + lowercase.

    Stored as `listings.company_key` and part of `content_key`. "Old Navy" and "oldnavy" stay
    different keys; that is accepted.
    """
    return (name or "").strip().lower()


def normalize_role(text: str) -> str:
    """MECHANICAL normalization only: lowercase + alnum + single spaces.

    No synonym resolution: the local app has no subscription to join a canonical role to, so
    "pm" stays "pm". Also the base of `_split_seniority`.
    """
    text = (text or "").lower()                 # null-safe + case-fold
    text = re.sub(r"\(required\)", "", text)    # drop literal "(required)" label noise
    text = re.sub(r"[^a-z0-9 ]", " ", text)     # any char not a-z/0-9/space -> space ("s.w.e." -> "s w e ")
    return " ".join(text.split())               # collapse whitespace runs, trim


# Seniority vocabulary, split three ways because POSITION MATTERS: some words are a level in front
# of a title and the job itself behind it.
#
# Unambiguous in any position. `intern` stays distinct from `junior` on purpose - an internship is
# not a junior role, and collapsing them reintroduces the very mismatch this vocabulary exists to
# prevent.
_SENIORITY_TERMS = {
    "senior": "senior", "sr": "senior", "snr": "senior",
    "junior": "junior", "jr": "junior", "jnr": "junior", "entry": "junior",
    "mid": "mid", "midlevel": "mid",
    "staff": "staff", "principal": "principal", "lead": "lead",
    "intern": "intern", "internship": "intern",
    "trainee": "apprentice", "apprentice": "apprentice",
    "journeyman": "journeyman",
}
# PREFIX only: each of these is a base title when it trails. "Master Plumber" is a level;
# "Scrum Master" is a job. "Associate Attorney" is a level; "Retail Sales Associate" is a job the
# synonym seed already maps, and a tail strip would silently canonicalize it to "retail sales".
_SENIORITY_PREFIX_ONLY = {
    "master": "master", "associate": "junior", "grad": "junior", "graduate": "junior",
    "vp": "executive", "svp": "executive", "evp": "executive",
}
# SUFFIX only: "Engineer II" states a level, a leading "II" does not.
_SENIORITY_SUFFIX_ONLY = {"i": "junior", "1": "junior", "ii": "mid", "2": "mid",
                          "iii": "senior", "3": "senior", "iv": "staff", "4": "staff",
                          "v": "principal", "5": "principal"}
_SENIORITY_PHRASES = {"entry level": "junior", "mid level": "mid", "new grad": "junior"}

# Deliberately absent: `director`, `chief`, `head`, `fellow`. Across most of the labour market
# these ARE the job rather than a modifier - Art Director, Funeral Director, Head Chef, Head Nurse,
# Research Fellow - and stripping them yields base titles like "of nursing" (from "Director of
# Nursing"). A corrupt canonical role poisons the poll key for every subscriber to that title,
# whereas a NULL level merely declines to filter. `vp`/`svp`/`evp` are included because they are
# never a base title anywhere.


def _split_seniority(title: str) -> tuple[str | None, str]:
    """Split a title into (seniority level, base title), both normalized.

    `_map_item` stamps the level into `listings.experience_level` and ignores the base title.

    Seniority is not always a prefix - "Engineer II", "Engineer, Senior", "Lead, Engineering" -
    and a prefix-only implementation under-strips silently, which reads as working. A title that
    is ONLY a seniority word ("Principal") is never stripped to the empty string, which would
    collapse every unrelated listing into one canonical role.
    """
    words = normalize_role(title).split()
    level = None
    stripped = True
    while stripped and len(words) > 1:
        stripped = False
        phrase = _SENIORITY_PHRASES.get(" ".join(words[:2]))
        if phrase and len(words) > 2:
            level = level or phrase
            words, stripped = words[2:], True
            continue
        head = _SENIORITY_TERMS.get(words[0]) or _SENIORITY_PREFIX_ONLY.get(words[0])
        if head:
            level = level or head
            words, stripped = words[1:], True
            continue
        tail = _SENIORITY_TERMS.get(words[-1]) or _SENIORITY_SUFFIX_ONLY.get(words[-1])
        if tail:
            level = level or tail
            words, stripped = words[:-1], True
    return level, " ".join(words)


def _map_item(item: dict) -> tuple[dict | None, str | None]:
    """Map one actor dataset row to `(listing, None)`, or `(None, reason)` when it is unusable.

    The reason is one of `missing_id`, `missing_title`, `missing_url`, `unsupported_ats` (a `source`
    outside greenhouse / lever.co / ashby) or `empty_title` (a title that normalizes to nothing);
    `ingest_run` counts them per reason. A field the actor sent with a value this code does not
    recognise (`work_arrangement`, `experience_years`, the salary unit, the employment type) is
    stored NULL and logged at INFO with the value: that is the signal the actor's vocabulary has
    drifted, not something to tell the user. The listing is still saved; those columns are
    nullable with no CHECK in `0001_init.sql`. Copied from `sourcing._fetch_actor` (field probes in
    `tests/fixtures/actor/`) and adapted to the local schema:

    - `source` is the ATS (`lever.co` -> `lever`); there is no `description` or `ats`.
    - `url` is kept, except a greenhouse row pointing at the company's own careers page (35% do,
      and that page has no form) is rebuilt from `?gh_jid=N` + `source_slug`.
    - `locations` is one object per `locations_derived[i]` split on ", " and zipped with
      `lats_derived[i]` / `lngs_derived[i]` (index-aligned; `cities/regions/countries_derived` are not).
    - `employment_type` prefers the board's own text, falls back to the AI layer's, and is INTERN
      when neither says and the title does. `work_arrangement` is "Remote Solely" when the board
      says `TELECOMMUTE`, else the AI layer's.
    - Salary comes from `ai_salary_*` (a lone value is min = max), annualized by
      `_SALARY_UNIT_PER_YEAR` and NULL when the unit is missing or unknown.
    - `repost_key` is sha256 of the canonical apply URL: lowercase host without `www.`, path
      without a trailing slash, no scheme, query or fragment, because reposts of one posting
      differ in tracking parameters.
    - Titles are normalized mechanically (`normalize_role`); there is no subscription to join
      to, so the synonym table has no reader here.
    """
    title = item.get("title") or ""
    url = item.get("url") or ""
    source = item.get("source")
    if item.get("id") is None:
        return None, "missing_id"
    if not title.strip():
        return None, "missing_title"
    if not url:
        return None, "missing_url"
    if source not in ACTOR_ATS:
        return None, "unsupported_ats"
    normalized_title = normalize_role(title)
    if normalized_title == "":
        return None, "empty_title"
    external_id = str(item["id"])

    parts = urllib.parse.urlsplit(url)
    gh_jids = urllib.parse.parse_qs(parts.query).get("gh_jid") or []
    if source == "greenhouse" and "greenhouse.io" not in parts.netloc and gh_jids and item.get("source_slug"):
        # Both parts are actor text: percent-encode each so a stray "/", "?" or "#" stays
        # inside its own path segment instead of reshaping the link.
        slug = urllib.parse.quote(str(item["source_slug"]), safe="")
        job_id = urllib.parse.quote(gh_jids[0], safe="")
        url = f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}"
    apply_parts = urllib.parse.urlsplit(url)
    host = apply_parts.netloc.lower().removeprefix("www.")
    repost_key = hashlib.sha256(f"{host}{apply_parts.path.rstrip('/')}".encode()).hexdigest()

    locations_derived = item.get("locations_derived") or []
    lats = item.get("lats_derived") or []
    lngs = item.get("lngs_derived") or []
    locations = []
    for index, entry in enumerate(locations_derived):
        pieces = entry.split(", ")
        if len(pieces) == 1:
            city, region, country = None, None, pieces[0]
        elif len(pieces) == 2:
            city, region, country = None, pieces[0], pieces[1]
        else:
            city, region, country = pieces[0], ", ".join(pieces[1:-1]), pieces[-1]
        locations.append({
            "city": city, "region": region, "country": country,
            "lat": lats[index] if index < len(lats) else None,
            "lng": lngs[index] if index < len(lngs) else None,
        })

    experience_level, _ = _split_seniority(title)
    board_types = item.get("employment_type") or []
    ai_types = item.get("ai_employment_type") or []
    # The board's own text first ("Full Time Reg | F" on lever), then the AI layer's.
    board_text = str(board_types[0]) if board_types else ""
    board_words = re.sub(r"[^a-z0-9 ]", " ", board_text.lower()).split()
    board_key = " ".join(board_words)
    as_enum = board_key.upper().replace(" ", "_")
    if as_enum in _EMPLOYMENT_TYPES:
        employment_type = as_enum
    else:
        employment_type = _EMPLOYMENT_TYPE_ALIASES.get(board_key)
    employment_type = employment_type or (ai_types[0] if ai_types else None)
    # Derive INTERN from the title when the source reports no employment type. Greenhouse rows
    # carry no employment type at all and the AI layer sometimes leaves a row untyped too; an
    # untyped internship would pass a full-time filter (an unknown type is not excluded). The
    # rule only fills a gap: a type the source states always wins.
    if employment_type is None and experience_level == "intern":
        employment_type = "INTERN"
    if employment_type is None and board_text:
        logger.info("listing %s: unrecognised employment type %r set to NULL", external_id, board_text)

    if item.get("location_type") == "TELECOMMUTE":
        work_arrangement = "Remote Solely"
    else:
        work_arrangement = item.get("ai_work_arrangement")
    if work_arrangement is not None and work_arrangement not in _WORK_ARRANGEMENTS:
        logger.info("listing %s: unrecognised work arrangement %r set to NULL", external_id, work_arrangement)
        work_arrangement = None
    experience_years = item.get("ai_experience_level")
    if experience_years is not None and experience_years not in _EXPERIENCE_YEARS:
        logger.info("listing %s: unrecognised experience level %r set to NULL", external_id, experience_years)
        experience_years = None
    # None stays None: a row the AI layer left unclassified must not masquerade as on-site.
    is_remote = None if work_arrangement is None else int(work_arrangement in ("Remote Solely", "Remote OK"))

    salary_min = item.get("ai_salary_min_value")
    salary_max = item.get("ai_salary_max_value")
    if salary_min is None and salary_max is None and item.get("ai_salary_value") is not None:
        salary_min = salary_max = item.get("ai_salary_value")
    salary_unit = item.get("ai_salary_unit_text")
    periods = _SALARY_UNIT_PER_YEAR.get(salary_unit)
    if periods is None:
        if salary_unit is not None and (salary_min is not None or salary_max is not None):
            logger.info("listing %s: unrecognised salary unit %r, salary set to NULL", external_id, salary_unit)
        salary_min = salary_max = None
    else:
        salary_min = None if salary_min is None else round(salary_min * periods)
        salary_max = None if salary_max is None else round(salary_max * periods)

    # posted_at is NOT NULL: a missing date means "we saw it now", and a future one (publisher or
    # timezone error) is clamped to now so it cannot sort to the front of the queue forever.
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        posted = datetime.datetime.fromisoformat(str(item.get("date_posted")).replace("Z", "+00:00"))
        posted = posted if posted.tzinfo else posted.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        posted = now
    posted_at = min(posted, now).astimezone(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")

    company = item.get("organization")
    company_key = normalize_company_key(company)
    visa = item.get("ai_visa_sponsorship")
    remote_regions = item.get("ai_remote_location_derived")
    key_skills = item.get("ai_key_skills")
    education = item.get("ai_education")
    listing = {
        "source": "lever" if source == "lever.co" else source,
        "external_id": external_id,
        "url": url,
        "title": title,
        "normalized_title": normalized_title,
        "company": company,
        "company_key": company_key,
        "location": locations_derived[0] if locations_derived else None,
        "locations": json.dumps(locations),
        "is_remote": is_remote,
        "employment_type": employment_type,
        "experience_level": experience_level,
        "posted_at": posted_at,
        "work_arrangement": work_arrangement,
        "remote_regions": json.dumps(remote_regions) if remote_regions else None,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": item.get("ai_salary_currency"),
        "experience_years": experience_years,
        "key_skills": json.dumps(key_skills) if key_skills else None,
        "visa_sponsorship": None if visa is None else int(bool(visa)),
        "responsibilities_summary": item.get("ai_core_responsibilities"),
        "requirements_summary": item.get("ai_requirements_summary"),
        "education": json.dumps(education) if education else None,
        "content_key": hashlib.sha256(f"{company_key}|{normalized_title}|{external_id}".encode()).hexdigest(),
        "repost_key": repost_key,
    }
    return listing, None
