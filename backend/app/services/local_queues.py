"""The local app's apply queues and sourcing schedule: pacing, block breaker, pause and stop.

Used by the launcher (`local_main`: `run_all` and `run_sourcing_schedule` as tasks on the one asyncio
loop, `stop_all` on window close) and by the progress and Settings screens, which
call `pause`, `resume`, `queue_status` and `start_sourcing`. `local_apply` does one
application; this module decides when. Flask-free, Supabase-free; state that must survive a
restart lives in `settings`, the rest (breaker counts, cooldown clock) is in memory on purpose.

One task per ATS (Greenhouse, Lever, Ashby), one application at a time each. Between applications
an ATS cools down 60 to 180 s; a block page backs it off and, repeated, pauses it. The three tasks
share ONE headless Chromium, one context per application: contexts isolate cookies and storage, and
three browser processes would cost three times the memory.

Settings keys (JSON values via `db.get_setting`): `apply_paused` {ats: "user" | "blocked"},
`apply_cooldown_floor_s`, `sourcing_interval` ("manual", "4h", "1h"), `sourcing_last_refusal`
{reason, detail, at}; `detail` also holds `retry_at` while a failed ingest waits to be retried.
"""
import asyncio
import dataclasses
import datetime
import logging
import math
import random
import sqlite3
import time

from playwright.async_api import async_playwright

from app.services import local_answers, local_apply, local_handoff, local_profile, local_sourcing
from app.store import db

logger = logging.getLogger("careersurge.services.local_queues")

SUPPORTED_ATS = ("greenhouse", "lever", "ashby")
# How often an idle, paused or cooling queue looks again; also the longest a stop waits on a sleeping queue.
POLL_S = 5
# Gap after an application: uniform(floor, floor + COOLDOWN_SPREAD_S). The floor is a setting, never under 30 s.
DEFAULT_COOLDOWN_FLOOR_S = 60
MIN_COOLDOWN_FLOOR_S = 30
COOLDOWN_SPREAD_S = 120
# A slow page doubles the next gap, up to this many times the normal one.
MAX_SLOW_FACTOR = 4
# After the 1st and 2nd consecutive block the ATS claims nothing this long; the 3rd pauses it.
BACKOFF_SECONDS = (15 * 60, 30 * 60)
MAX_BLOCKS = 3
BROWSER_IDLE_S = 5 * 60
EXPIRE_EVERY_S = 10 * 60
SOURCING_INTERVAL_S = {"1h": 60 * 60, "4h": 4 * 60 * 60}
DEFAULT_SOURCING_INTERVAL = "4h"
# Waits between tries of an ingest failure the user can fix: 2, 5, then 15 minutes for every try after.
INGEST_RETRY_WAITS_S = (2 * 60, 5 * 60, 15 * 60)

# Wall clock and sleep are module attributes so tests drive time without waiting.
_clock = time.time
_sleep = asyncio.sleep


@dataclasses.dataclass
class _AtsState:
    """What one ATS queue remembers between applications (in memory; a restart starts clean)."""
    blocks: int = 0               # consecutive block pages
    backoff_until: float = 0.0    # epoch seconds; nothing is claimed before this
    next_at: float = 0.0          # epoch seconds; end of the cooldown after the last application
    slow_factor: int = 1          # 1, 2 or 4: multiplier on the next cooldown
    through: int = 0              # applications that got past the block check (not a block page) so far
    row_blocked_at: dict = dataclasses.field(default_factory=dict)    # application id -> `through` when it was last blocked
    task: asyncio.Task | None = None    # the `apply_one` in flight
    app_id: int | None = None           # its application row


_state = {ats: _AtsState() for ats in SUPPORTED_ATS}
_loops: list[asyncio.Task] = []
_stop_requested = False
_sourcing_tasks: list[asyncio.Task] = []    # the ingest and batch build of every run started by `start_sourcing` or `resume_sourcing`


class _SharedBrowser:
    """The one headless Chromium all queues open their contexts in.

    Launched on first use, relaunched when it has disconnected (a crash), closed by the supervisor
    after `BROWSER_IDLE_S` with no application running. `busy` counts applications in flight.
    """

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.busy = 0
        self.idle_since = _clock()
        self._lock = asyncio.Lock()

    async def get(self):
        async with self._lock:
            if self.browser is not None and not self.browser.is_connected():
                self.browser = None
            if self.browser is None:
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(headless=True)
            return self.browser

    async def close(self, idle_for: float = 0.0):
        """Close the browser unless an application is running, a context waits for an emailed code, or it was used within `idle_for` seconds."""
        async with self._lock:
            if self.busy or local_handoff.parked_count() or _clock() - self.idle_since < idle_for:
                return
            if self.browser is not None:
                await self.browser.close()
                self.browser = None
            if self.playwright is not None:
                await self.playwright.stop()
                self.playwright = None


def now() -> float:
    """Epoch seconds on the queue's clock, the one `queue_status` `next_at` is measured on (the progress route)."""
    return _clock()


def stop_requested() -> bool:
    """Whether `stop_all` has been called since `run_all` last started."""
    return _stop_requested


def _stopping(should_stop) -> bool:
    return _stop_requested or bool(should_stop())


def _pending(ats: str) -> int:
    """How many approved applications wait for `ats`."""
    conn = db.get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM job_applications WHERE status = 'pending' AND ats = ?",
                            (ats,)).fetchone()[0]
    finally:
        conn.close()


def pause(ats: str | None = None) -> None:
    """Pause one ATS queue, or all three; a running application finishes, nothing new is claimed.

    Called by the Progress screen's Pause button. Stored in `settings['apply_paused']` as `{ats: "user"}`, read at
    every turn of the queue, so a restart honors it. An ATS the breaker already paused as
    `blocked` keeps that reason.
    """
    targets = SUPPORTED_ATS if ats is None else (ats,)
    unknown = [name for name in targets if name not in SUPPORTED_ATS]
    if unknown:
        raise ValueError(f"unknown ats: {unknown[0]}")
    paused = db.get_setting("apply_paused", {})
    for name in targets:
        paused.setdefault(name, "user")
    db.set_setting("apply_paused", paused)


def resume(ats: str | None = None) -> None:
    """Un-pause one ATS queue, or all three, and clear its block breaker (count and back-off)."""
    targets = SUPPORTED_ATS if ats is None else (ats,)
    unknown = [name for name in targets if name not in SUPPORTED_ATS]
    if unknown:
        raise ValueError(f"unknown ats: {unknown[0]}")
    paused = db.get_setting("apply_paused", {})
    for name in targets:
        paused.pop(name, None)
        _state[name].blocks = 0
        _state[name].backoff_until = 0.0
    db.set_setting("apply_paused", paused)


def queue_status() -> dict:
    """`{ats: {state, paused_reason, next_at, pending}}` for the Progress screen.

    `state` is `paused`, `running` (an application in flight), `backed_off` (after a block),
    `cooling` (the gap between applications) or `idle`. `next_at` is epoch seconds when the queue may
    claim again (back-off or cooldown end), else None; the screen shows "next application in 0:47".
    """
    now = _clock()
    paused = db.get_setting("apply_paused", {})
    status = {}
    for ats in SUPPORTED_ATS:
        state = _state[ats]
        next_at = None
        if ats in paused:
            queue_state = "paused"
        elif state.task is not None:
            queue_state = "running"
        elif state.backoff_until > now:
            queue_state, next_at = "backed_off", state.backoff_until
        elif state.next_at > now:
            queue_state, next_at = "cooling", state.next_at
        else:
            queue_state = "idle"
        status[ats] = {"state": queue_state, "paused_reason": paused.get(ats), "next_at": next_at,
                       "pending": _pending(ats)}
    return status


async def _ats_loop(ats: str, shared: _SharedBrowser, should_stop) -> None:
    """One ATS's queue: wait while paused, backed off or cooling; claim; apply; record the outcome.

    With nothing `pending` (or no profile yet) it polls every `POLL_S`. The browser is obtained
    before the claim, so `stop_all` never meets a claimed row that has no task yet. After a block
    page the breaker backs the ATS off 15 min, then 30, then pauses it as `blocked`; any other
    outcome resets the count and starts the cooldown. A row blocked a second time is failed as
    `unsupported_form/blocked_repeatedly` only if another row on the same ATS got through between its
    two blocks (then the URL is the problem, not the IP); otherwise it stays `pending` and the
    breaker counts the block as usual, so an IP-wide block never loses a job. A failed repeat row
    does not count toward the breaker. Never retries around a block.
    """
    state = _state[ats]
    while not _stopping(should_stop):
        try:
            now = _clock()
            paused = db.get_setting("apply_paused", {})
            conn = db.get_db()
            try:
                has_profile = conn.execute("SELECT 1 FROM profile WHERE id = 1").fetchone() is not None
            finally:
                conn.close()
            if ats in paused or not has_profile:
                wait = POLL_S
            elif state.backoff_until > now:
                wait = min(state.backoff_until - now, POLL_S)
            elif state.next_at > now:
                wait = min(state.next_at - now, POLL_S)
            elif _pending(ats) == 0:
                wait = POLL_S
            else:
                wait = 0
            if wait:
                await _sleep(wait)
                continue

            # Room for one more browser context: contexts parked for an emailed code count, and
            # a queue waits here, before it claims, so a waiting row never burns its time budget. The short
            # timeout keeps Pause and Stop working.
            if not await local_handoff.acquire_context_slot(POLL_S):
                continue
            parked = False
            try:
                browser = await shared.get()
                # Launching Chromium can take seconds; a Pause or Stop pressed meanwhile must still hold.
                if _stopping(should_stop) or ats in db.get_setting("apply_paused", {}):
                    continue
                # Rows that just met a block page go last, so a bad URL at the head cannot keep the rows behind it from trying.
                row = local_apply.claim(ats, avoid=tuple(state.row_blocked_at))
                if row is None:
                    continue

                # Run the application as a task so stop_all can cancel it; this loop waits for it.
                shared.busy += 1
                state.app_id = row["id"]
                state.task = asyncio.ensure_future(local_apply.apply_one(browser, row))
                try:
                    await asyncio.wait({state.task})
                finally:
                    task = state.task
                    state.task = None
                    state.app_id = None
                    shared.busy -= 1
                    shared.idle_since = _clock()
                    if not task.done():
                        # This loop itself was cancelled: settle the application before leaving.
                        task.cancel()
                        await asyncio.wait({task})
                # A context parked for a code keeps this slot; `local_handoff` gives it back when it closes.
                parked = local_handoff.is_parked(row["id"])
            finally:
                if not parked:
                    local_handoff.release_context_slot()
            if task.cancelled():
                break    # stop_all cancelled it; apply_one already settled the row
            outcome = task.result()

            if outcome["blocked"]:
                # Was this row blocked before, with some other row getting through since? If so the
                # URL is the problem; if not, the IP (or the ATS) is blocking everything.
                blocked_before_at = state.row_blocked_at.get(row["id"])
                state.row_blocked_at[row["id"]] = state.through
                sibling_got_through = blocked_before_at is not None and state.through > blocked_before_at
                if not sibling_got_through:
                    # The row stays pending (apply_one refunded the attempt); back the ATS off or pause it.
                    state.blocks += 1
                    if state.blocks >= MAX_BLOCKS:
                        paused = db.get_setting("apply_paused", {})
                        paused[ats] = "blocked"
                        db.set_setting("apply_paused", paused)
                        logger.warning("%s paused after %d block pages in a row", ats, state.blocks)
                    else:
                        backoff = BACKOFF_SECONDS[state.blocks - 1]
                        state.backoff_until = _clock() + backoff
                        logger.warning("%s hit a block page; backed off %d min", ats, backoff // 60)
                    continue
                # Blocked twice with a sibling through in between: a bad URL, not a rate limit. Fail it
                # and do not count it against the ATS, so the rows behind it still run. It still gets
                # the cooldown below.
                local_apply._transition_application(row["id"], row["claim_token"], "pending", "failed",
                                                    skip_reason="unsupported_form", error="blocked_repeatedly")
                del state.row_blocked_at[row["id"]]
                logger.warning("%s application %s failed: blocked again after another row got through",
                               ats, row["id"])
            else:
                # A normal outcome (applied, failed, blocked on a question) resets the breaker.
                state.blocks = 0
                state.through += 1
                state.row_blocked_at.pop(row["id"], None)

            # The cooldown, doubled per consecutive slow page.
            if outcome["slow"]:
                state.slow_factor = min(state.slow_factor * 2, MAX_SLOW_FACTOR)
            else:
                state.slow_factor = 1
            floor = db.get_setting("apply_cooldown_floor_s")
            if not isinstance(floor, (int, float)) or not math.isfinite(floor) or floor <= 0:
                floor = DEFAULT_COOLDOWN_FLOOR_S
            floor = max(MIN_COOLDOWN_FLOOR_S, floor)
            state.next_at = _clock() + random.uniform(floor, floor + COOLDOWN_SPREAD_S) * state.slow_factor
        except Exception:  # noqa: BLE001 - one bad turn (a locked database, a failed launch) must not end the queue
            logger.exception("%s queue turn failed", ats)
            await _sleep(POLL_S)


async def run_all(should_stop=lambda: False) -> None:
    """Run the three ATS queues until `should_stop()` is true or `stop_all` is called.

    Started once by the launcher (`local_main`) after `local_apply.recover()`. Also expires 72 h-old blocked
    rows every `EXPIRE_EVERY_S` (`local_answers.expire_blocked` is a write transaction, so not on
    every poll), deletes resume files no approved batch needs (`local_profile.sweep_resumes`) and
    closes the shared Chromium after `BROWSER_IDLE_S` with no application running.
    """
    global _stop_requested
    _stop_requested = False
    _state.update({ats: _AtsState() for ats in SUPPORTED_ATS})
    shared = _SharedBrowser()
    _loops[:] = [asyncio.ensure_future(_ats_loop(ats, shared, should_stop)) for ats in SUPPORTED_ATS]
    next_expiry = 0.0
    try:
        while not all(loop.done() for loop in _loops):
            try:
                if _clock() >= next_expiry:
                    next_expiry = _clock() + EXPIRE_EVERY_S
                    local_answers.expire_blocked()
                    local_profile.sweep_resumes(min_age_s=60)    # a resume kept for an approved batch goes once it settles
                await shared.close(idle_for=BROWSER_IDLE_S)
            except Exception:  # noqa: BLE001 - a locked database must not take the three queues down with it
                logger.exception("queue supervisor turn failed")
            await _sleep(POLL_S)
    finally:
        for loop in _loops:
            loop.cancel()
        await asyncio.gather(*_loops, return_exceptions=True)
        await local_handoff.stop()
        await shared.close()


async def stop_all() -> None:
    """Stop claiming and wind the queues down safely; returns when every queue is idle.

    Called by the launcher (`local_main`) on window close and at server shutdown. An application still `in_progress` is
    cancelled and goes back to `pending` with its attempt refunded (nothing was clicked). One in
    `submitting` is left to reach its outcome, bounded by the 45 s confirmation wait in
    `local_formfill`. A context parked for an emailed code goes back to `pending` (the code dies
    with the process) unless its code is already typed, and an open captcha window is closed. A queue
    that is sleeping exits within `POLL_S`.
    """
    global _stop_requested
    _stop_requested = True
    for ats in SUPPORTED_ATS:
        state = _state[ats]
        if state.task is None or state.task.done():
            continue
        conn = db.get_db()
        try:
            status = conn.execute("SELECT status FROM job_applications WHERE id = ?",
                                  (state.app_id,)).fetchone()["status"]
        finally:
            conn.close()
        if status == "in_progress":
            state.task.cancel()
    await asyncio.gather(*_loops, return_exceptions=True)
    # Contexts parked for a code: the ones still waiting go back to `pending`, the ones past the fence finish.
    await local_handoff.stop()
    # Each ends at its ingest's next 10 s lap, or within POLL_S when it is waiting to retry a failed ingest.
    await asyncio.gather(*_sourcing_tasks, return_exceptions=True)
    _sourcing_tasks.clear()    # a task belongs to its loop; a later run of the app starts its own


async def _ingest_and_prepare(run_id: str) -> None:
    """Read a started run's dataset into `listings`, then build the prepared rows; shared by Run now and the launch.

    Run as a task by `start_sourcing` (a run just started) and by `resume_sourcing` (a run a quit left
    `started`), so a failed ingest behaves the same either way. When `local_sourcing.ingest_run` raises,
    `local_sourcing.is_fixable_by_user` splits the failure in two:

    - Fixable (a `tls` or `no_key` refusal, an Apify 401/403, a full disk or locked database): the task
      stores `sourcing_last_refusal` {reason, detail {message, retry_at}, at}, waits `INGEST_RETRY_WAITS_S`
      (2, 5, then 15 minutes per try) and calls `ingest_run` again until it returns or the app quits.
      The row stays `started`, so `start_run`'s `run_in_progress` guard keeps refusing a second paid run.
    - Anything else (our own bug, a request impit refuses to send, a corrupt database) will fail the same
      way next time: the task marks the row `failed` through `local_sourcing.fail_run` (with the run's
      Apify bill, so the monthly tally stays right), stores {reason `ingest_failed`, detail {message}, at}
      with no `retry_at`, and stops. Run now works again.

    Success clears the notice and builds the prepared rows. A wait wakes on `stop_all` within `POLL_S`; a
    quit leaves the row `started` for the next launch's `resume_sourcing`.
    """
    failures = 0
    while True:
        try:
            await asyncio.to_thread(local_sourcing.ingest_run, run_id, lambda: _stop_requested)
            break
        except Exception as exc:  # noqa: BLE001 - every failure is shown, then retried or ends the run
            fixable = local_sourcing.is_fixable_by_user(exc)
            if fixable and isinstance(exc, local_sourcing.SourcingRefused):
                refused = exc    # tls or no_key: its own notice text
            else:
                # The message is shown to the user; client and driver errors can run long.
                refused = local_sourcing.SourcingRefused(
                    "ingest_failed", message=(str(exc) or type(exc).__name__)[:200])
            if not fixable:
                logger.error("sourcing run %s: ingest failed for good (%s)", run_id, exc, exc_info=True)
                try:
                    # The notice first: if closing the row fails, the user still sees why.
                    _store_refusal(refused)
                    await asyncio.to_thread(local_sourcing.fail_run, run_id)
                except Exception:  # noqa: BLE001 - a corrupt database cannot record anything; the log has it
                    logger.exception("sourcing run %s: recording the failure failed", run_id)
                return
            # After the third failure every try keeps the last wait, 15 minutes.
            wait_s = INGEST_RETRY_WAITS_S[min(failures, len(INGEST_RETRY_WAITS_S) - 1)]
            failures += 1
            logger.warning("sourcing run %s: ingest failed (%s); retrying in %d s", run_id, exc, wait_s,
                           exc_info=True)
            _store_refusal(refused, retry_in_s=wait_s)
            retry_at = _clock() + wait_s
            while _clock() < retry_at and not _stop_requested:
                await _sleep(POLL_S)
            if _stop_requested:
                return
    if _stop_requested:
        return
    db.set_setting("sourcing_last_refusal", None)
    try:
        prepared = await asyncio.to_thread(local_profile.build_prepared)
        logger.info("sourcing run %s prepared %d applications", run_id, prepared)
    except Exception:  # noqa: BLE001 - the next launch's build_prepared picks the listings up
        logger.exception("sourcing run %s: building the prepared applications failed", run_id)


async def start_sourcing(confirm_backlog: bool = False) -> str:
    """Start an Apify run and return its id once it is started; the ingest and the batch build go on in a task.

    Called by `run_sourcing_schedule` and by the Run now route. `start_run` runs in a worker
    thread and takes seconds, because it reads Apify's price before any charge. Its refusals
    (no key, over budget, backlog needs confirming, ...) raise `local_sourcing.SourcingRefused`
    for the caller to show, and nothing has been charged. Once the run is started the stored
    refusal notice is cleared and a `_ingest_and_prepare` task waits on Apify (minutes), ingests the
    dataset and builds the `prepared` rows. The route has long since answered; the screen follows the
    run through `sourcing_runs.status`.

    An ingest failure the user can fix is retried every 2, 5, then 15 minutes with the row left `started`;
    any other failure marks the row `failed` and is shown once, with no retry (see `_ingest_and_prepare`).
    """
    run_id = await asyncio.to_thread(local_sourcing.start_run, confirm_backlog)
    # Drop finished tasks so the list does not grow for as long as the app stays open.
    still_running = [task for task in _sourcing_tasks if not task.done()]
    _sourcing_tasks.clear()
    _sourcing_tasks.extend(still_running)
    _sourcing_tasks.append(asyncio.ensure_future(_ingest_and_prepare(run_id)))
    # The run is paid for and its task is going, so a locked database here must not turn into "could not
    # start". The task does not run before the next await, and it clears the notice itself on success.
    try:
        db.set_setting("sourcing_last_refusal", None)
    except sqlite3.OperationalError:
        logger.warning("sourcing run %s started, but the old notice could not be cleared", run_id, exc_info=True)
    return run_id


async def resume_sourcing() -> None:
    """Give every run a quit left `started` the same ingest as Run now; never starts a run.

    Called once by the launcher (`local_main`), so a run paid for before the app closed is read instead
    of billed again, with the same retries and failure notice as `start_sourcing`, and a notice the quit
    left behind is cleared when the retry succeeds. With no such run it only builds the prepared rows, so
    listings whose build failed last time reach the approval screen.
    """
    conn = db.get_db()
    try:
        started = conn.execute(
            "SELECT apify_run_id FROM sourcing_runs WHERE status = 'started' ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()
    for row in started:
        _sourcing_tasks.append(asyncio.ensure_future(_ingest_and_prepare(row["apify_run_id"])))
    if not started:
        try:
            await asyncio.to_thread(local_profile.build_prepared)
        except Exception:  # noqa: BLE001 - the next launch tries again
            logger.exception("building the prepared applications at launch failed")


def _store_refusal(refused: local_sourcing.SourcingRefused, retry_in_s: float | None = None) -> None:
    """Store a refusal as `settings['sourcing_last_refusal'] = {reason, detail, at}` for the notice on the next open.

    Used by the scheduler (a refused `start_run`) and by `_ingest_and_prepare` (`tls`, `no_key`,
    `ingest_failed`), which passes `retry_in_s` for a failure it will retry so `detail.retry_at` tells the
    notice when the next try is.
    """
    at = datetime.datetime.fromtimestamp(_clock(), datetime.timezone.utc)
    detail = dict(refused.detail)
    if retry_in_s is not None:
        retry_at = at + datetime.timedelta(seconds=retry_in_s)
        detail["retry_at"] = retry_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db.set_setting("sourcing_last_refusal", {
        "reason": refused.reason, "detail": detail,
        "at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z")})


async def run_sourcing_schedule(should_stop=lambda: False) -> None:
    """Check for new jobs every `settings['sourcing_interval']` (`manual`, `4h`, `1h`; default `4h`).

    Started once by the launcher next to `run_all`. A run is due when the interval has passed
    since the newest of: the last `sourcing_runs.started_at`, the last stored refusal, and this
    loop's last attempt. The last two stop a refused or failing run from being retried (and its
    price re-read from Apify) every tick. A `SourcingRefused` is stored as
    `settings['sourcing_last_refusal'] = {reason, detail, at}` for the `RefusalNotice` banner and is not raised,
    except `run_in_progress` while a notice with a `retry_at` is stored, which is dropped so it cannot
    hide the notice of a run retrying its ingest.
    """
    last_attempt = 0.0
    while not _stopping(should_stop):
        interval = SOURCING_INTERVAL_S.get(db.get_setting("sourcing_interval", DEFAULT_SOURCING_INTERVAL))
        if interval is not None:
            conn = db.get_db()
            try:
                newest_run = conn.execute("SELECT MAX(started_at) FROM sourcing_runs").fetchone()[0]
            finally:
                conn.close()
            refusal = db.get_setting("sourcing_last_refusal")
            marks = [last_attempt]
            if newest_run:
                marks.append(datetime.datetime.fromisoformat(newest_run).timestamp())
            if refusal:
                marks.append(datetime.datetime.fromisoformat(refusal["at"]).timestamp())
            if _clock() - max(marks) >= interval:
                last_attempt = _clock()
                try:
                    run_id = await start_sourcing()
                    logger.info("scheduled sourcing run %s started", run_id)
                except local_sourcing.SourcingRefused as refused:
                    # A run still retrying its ingest is not news: storing `run_in_progress` here would
                    # replace the "Fetching jobs failed ... Retrying at" notice that explains the wait.
                    current = db.get_setting("sourcing_last_refusal")
                    retrying = current is not None and "retry_at" in current["detail"]
                    if refused.reason != "run_in_progress" or not retrying:
                        _store_refusal(refused)
                except Exception:  # noqa: BLE001 - a network failure waits for the next interval, it does not end the schedule
                    logger.exception("scheduled sourcing run failed")
        await _sleep(POLL_S)
