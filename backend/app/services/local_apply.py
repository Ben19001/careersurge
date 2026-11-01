"""The local app's apply worker: claim, fill, submit and recover for Greenhouse, Lever, Ashby.

Used by the launcher (`local_main`: `recover()` once at startup, then `local_queues.run_all`, which paces
`claim` and `apply_one` per ATS) and by the approval and tray screens, which read the
statuses written here; `local_answers` moves the blocked rows back out. One headless
Chromium context per application, on the user's own machine. Flask-free, Supabase-free; storage is
`app.store.db`, the form is driven by `local_formfill`.

Status machine over `job_applications` (one row per listing, retries reuse it). Every write after
the claim is a compare-and-set (CAS) on `status` and `claim_token`: zero rows updated means the row
moved under us (the user cancelled it, a test changed it) and the worker stops without writing.

    pending --claim--> in_progress --+--> blocked_needs_input/question   (required field unfilled;
                                     |      local_answers.save_blocked_question_answer ->
                                     |      pending when all answered)
                                     +--> failed/unsupported_form | listing_closed | timeout
                                     +--> pending                       (exception, attempts < 3;
                                     |      a block page or a stop, attempt refunded)
                                     +--> blocked_needs_input/attempts_exhausted   (third exception)
                                     +--> blocked_needs_input/captcha  (a visible widget, before the fence; the
                                     |      user solves it in a headed window, `local_handoff.open_in_browser`)
                                     +--fence--> submitting --+--> applied
                                                              +--> blocked_needs_input/verify_submit
                                                              |      (also error='email_link': the user clicks the mailed link)
                                                              +--> blocked_needs_input/captcha  (a challenge after the click)
                                                              +--> blocked_needs_input/otp  (a code prompt; the live context is
                                                                     parked in `local_handoff` until `POST /api/otp`)
    recover() at startup: in_progress -> pending, submitting -> verify_submit, blocked(otp) -> pending
    local_answers.expire_blocked(), in recover() and local_queues.run_all(): blocked question / attempts_exhausted
        older than 72 h -> failed/timeout

`submitting` is one-way: a click may have gone out, so nothing ever sends it again.
"""
import asyncio
import datetime
import json
import logging
import re
import time
import urllib.parse
import uuid

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from app.services import local_answers, local_batch, local_formfill, local_handoff
from app.store import db

logger = logging.getLogger("careersurge.services.local_apply")

# The whole row, from claim to outcome. The earlier hosted worker's value.
APPLY_MAX_ROW_SECONDS = 360
# An exception in `in_progress` re-queues the row until this many claims have been spent on it.
MAX_ATTEMPTS = 3
GOTO_TIMEOUT_MS = 30000

# A goto slower than this (a third of GOTO_TIMEOUT_MS) tells the queue to double the next gap.
SLOW_PAGE_SECONDS = 10

# The URL is actor-supplied text and the browser must open only ATS application forms, so the host
# must be one of these AND belong to the row's ATS.
_ATS_HOSTS = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io",
                   "boards.eu.greenhouse.io"),
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
    "ashby": ("jobs.ashbyhq.com",),
}
# Where the form lives, relative to the listing URL: Greenhouse on the listing itself, Lever behind
# "/apply", Ashby behind "/application".
_FORM_SUFFIX = {"greenhouse": "", "lever": "/apply", "ashby": "/application"}
# A posting that is gone says so. Checked only on a page with no form, so a job description that
# happens to contain one of these phrases cannot close a live posting.
_CLOSED_TEXT = re.compile(
    r"no longer accepting|position has been filled|job is closed|no longer open", re.I)
# A challenge page stands in for the form. Checked only on a page with no form fields, so a real
# form that embeds a captcha widget (handed to the user by `local_handoff`) is never read as a block.
_CHALLENGE_TEXT = re.compile(
    r"verify you are human|are you a robot|unusual traffic|checking your browser|access denied", re.I)


def _ats_url_allowed(url: str, ats: str) -> bool:
    """Whether a listing URL may be opened at all: `https`, on a host of the row's ATS, and read the same way by every parser.

    Used by `_open_form` (the headless and headed browsers) and `local_handoff.blocked_posting_url` (the
    user's own browser). Ingest does not use it: a listing on another host is still stored and shown,
    and fails here when applied to.

    The URL is actor text. A browser reads a backslash as a slash and Python does not, so
    `https://evil.example\\@boards.greenhouse.io/` has the hostname `boards.greenhouse.io` here and
    opens evil.example there.
    So a backslash, whitespace or control character is refused, and so is any userinfo or port: the
    netloc must be exactly the host, so both parsers see the same one.
    """
    url = url or ""
    parts = urllib.parse.urlsplit(url)
    odd_characters = any(char == "\\" or char.isspace() or ord(char) < 32 for char in url)
    plain_host = parts.netloc.lower() == (parts.hostname or "")
    allowed_host = (parts.hostname or "") in _ATS_HOSTS.get(ats, ())
    return parts.scheme == "https" and plain_host and allowed_host and not odd_characters


def _still_on_ats(page, ats: str) -> bool:
    """Whether the page is still over `https` on a host of the row's ATS; checked before the fill and again before each click.

    Used by `_open_form`, by both submit fences (`_attempt`, `local_handoff._run_headed`) and before the
    emailed code is typed (`local_handoff._hold_for_code`). The check right after `goto` misses a script
    or meta-refresh redirect that fires later, and the profile must not be typed into, or submitted on,
    a host outside the list, nor over plain `http`.
    """
    parts = urllib.parse.urlsplit(page.url)
    return parts.scheme == "https" and (parts.hostname or "") in _ATS_HOSTS.get(ats, ())


def _now() -> str:
    """UTC timestamp in the schema's own format (`2026-10-11T09:30:00.123Z`)."""
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def claim(ats: str, avoid: tuple = ()) -> dict | None:
    """Claim the oldest approved `pending` row for `ats`, or None when nothing is pending.

    One UPDATE ... RETURNING statement, so two workers can never take the same row (an atomic
    guard, the one kind of logic kept in SQL). Oldest approval first, so the user's batch goes in the order they
    approved it. Stamps a fresh `claim_token`, counts the attempt, and clears the last error, except
    `otp_timeout`, which stays so a second code timeout on the row is told from the first.
    `avoid` is application ids the queue wants tried last (rows that just met a block page),
    so the rows behind a blocked head get their turn; an avoided row is still claimed when it is
    the only one pending.
    """
    now = _now()
    conn = db.get_db()
    try:
        # Plain SQL: pick the id of the oldest approved pending row for this ats (the subquery),
        # with the ids in `avoid` (a JSON list) sorted after all the others, then flip exactly
        # that one row to in_progress and hand it back. The error is cleared (set to NULL) except
        # `otp_timeout`, which stays so a second code timeout is told from the first.
        row = conn.execute(
            "UPDATE job_applications "
            "SET status = 'in_progress', claim_token = ?, claimed_at = ?, updated_at = ?, "
            "    attempt_count = attempt_count + 1, "
            "    error = CASE WHEN error = 'otp_timeout' THEN error ELSE NULL END "
            "WHERE id = (SELECT id FROM job_applications WHERE status = 'pending' AND ats = ? "
            "            ORDER BY id IN (SELECT value FROM json_each(?)), approved_at, id LIMIT 1) "
            "RETURNING *",
            (uuid.uuid4().hex, now, now, ats, json.dumps(list(avoid)))).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _transition_application(app_id: int, token: str, from_status: str, to_status: str, *,
                            blocked_reason=None, skip_reason=None, error=None, fields_used=None,
                            screenshot_path=None, stamp=None, deactivate_listing_id=None,
                            inside_transaction=None, refund_attempt=False) -> bool:
    """CAS one row from `from_status` to `to_status`; False means the row is no longer ours.

    Written for every transition after the claim. `blocked_reason` is always part of the UPDATE
    (NULL unless given) because the table's CHECK ties it to `blocked_needs_input`, so every exit
    from blocked clears it in the same statement. The other optional columns are only touched when
    given. `stamp` names a timestamp column (`submit_attempted_at`, `confirmed_at`) set to now.
    `deactivate_listing_id` also sets `listings.is_active = 0` in the same transaction.
    `inside_transaction(conn)` runs in that transaction only when the CAS took the row, so the
    question rows of a block commit together with the move to `blocked_needs_input`.
    `refund_attempt` takes the claim's `attempt_count` increment back off in the same UPDATE, for a
    block page or a stop: neither is the job's fault, so neither may spend one of its three tries.
    """
    sets = {"status": to_status, "blocked_reason": blocked_reason, "updated_at": _now()}
    for column, value in (("skip_reason", skip_reason), ("error", error),
                          ("screenshot_path", screenshot_path)):
        if value is not None:
            sets[column] = value
    if to_status == "applied":
        sets["error"] = None    # an applied row carries no error; this drops the `otp_timeout` left by a first, timed-out try
    if fields_used is not None:
        sets["fields_used"] = json.dumps(fields_used)
    if stamp:
        sets[stamp] = _now()
    assignments = ", ".join(f"{column} = ?" for column in sets)
    if refund_attempt:
        assignments += ", attempt_count = MAX(attempt_count - 1, 0)"

    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            f"UPDATE job_applications SET {assignments} "
            "WHERE id = ? AND status = ? AND claim_token = ?",
            (*sets.values(), app_id, from_status, token)).rowcount
        if updated and deactivate_listing_id is not None:
            conn.execute("UPDATE listings SET is_active = 0, updated_at = ? WHERE id = ?",
                         (_now(), deactivate_listing_id))
        if updated and inside_transaction is not None:
            inside_transaction(conn)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return updated == 1


def recover() -> dict:
    """Settle rows a crash or quit left mid-flight; called once at startup, before any worker starts.

    `local_main` holds the instance lock first, so no other process owns a claim - that is why
    the claim needs no lease or heartbeat. `in_progress` never reached the click and goes back to
    `pending`; `submitting` may have clicked, so it becomes `blocked_needs_input/verify_submit` for
    the user to check; `blocked_needs_input/otp` goes back to `pending` (the code expired with the
    process, and so did the parked browser context behind it: `local_handoff.drop_parked` makes sure no
    waiter survives). Blocked rows left unanswered for 72 h expire first (`local_answers.expire_blocked`);
    every other blocked row stays. Returns the count moved per old state.
    """
    local_handoff.drop_parked()
    local_answers.expire_blocked()
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = _now()
        requeued = conn.execute(
            "UPDATE job_applications SET status = 'pending', claim_token = NULL, claimed_at = NULL, "
            "updated_at = ? WHERE status = 'in_progress'", (now,)).rowcount
        to_verify = conn.execute(
            "UPDATE job_applications SET status = 'blocked_needs_input', blocked_reason = 'verify_submit', "
            "error = 'interrupted_during_submit', updated_at = ? WHERE status = 'submitting'",
            (now,)).rowcount
        otp = conn.execute(
            "UPDATE job_applications SET status = 'pending', blocked_reason = NULL, claim_token = NULL, "
            "claimed_at = NULL, updated_at = ? "
            "WHERE status = 'blocked_needs_input' AND blocked_reason = 'otp'", (now,)).rowcount
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"in_progress": requeued, "submitting": to_verify, "otp": otp}


async def _open_form(browser, ats: str, url: str):
    """Open the application form for a listing URL; returns `(context, page, fields, failure, goto_seconds)`.

    Used by `apply_one` and `fill_check`. `failure` is None, or `(skip_reason, error, listing_gone)`
    for a posting we cannot apply to: a URL `_ats_url_allowed` refuses (no browser context is
    opened, nothing is navigated), a page that left the ATS's hosts (checked after the load and
    again after the wait for the form), a 404 or 410, a closed-posting page, or no form within
    `FORM_RENDER_TIMEOUT_MS`. A block comes back as `("blocked", error, False)` and is not
    a skip reason: HTTP 403 or 429 on the form page, or no form fields plus challenge text. It is
    temporary, so `_attempt` requeues the row instead of failing it. `goto_seconds` is how long the
    navigation took (0.0 when none was made), for the queue's slow-page cooldown. The caller closes
    `context` when it is not None.
    """
    if not _ats_url_allowed(url, ats):
        return None, None, [], ("unsupported_form", "host_not_allowed", False), 0.0
    parts = urllib.parse.urlsplit(url)
    # The browser is handed a URL rebuilt from the checked host, never the actor's text, so it opens
    # exactly what was checked.
    if ats == "greenhouse":
        # Greenhouse keeps the job id in the query (`?gh_jid=`), so the query stays.
        query = f"?{parts.query}" if parts.query else ""
        form_url = f"https://{parts.hostname}{parts.path}{query}"
    else:
        # Lever and Ashby carry tracking parameters on the listing URL; the form is the path plus a suffix.
        path = parts.path.rstrip("/")
        suffix = _FORM_SUFFIX[ats]
        form_url = f"https://{parts.hostname}{path if path.endswith(suffix) else path + suffix}"

    context = await browser.new_context()
    try:
        page = await context.new_page()
        goto_started = time.monotonic()
        response = await page.goto(form_url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        goto_seconds = time.monotonic() - goto_started
        # A redirect (an open redirect on an ATS host, a board that bounces to the company's site)
        # must not carry the profile onto a host outside the list.
        if not _still_on_ats(page, ats):
            return context, page, [], ("unsupported_form", "redirected_off_host", False), goto_seconds
        if response is not None and response.status in (404, 410):
            return context, page, [], ("listing_closed", f"http_{response.status}", True), goto_seconds
        if response is not None and response.status in (403, 429):
            return context, page, [], ("blocked", f"http_{response.status}", False), goto_seconds
        fields = await local_formfill.grab_page_fields(page)
        # The wait for the form is long enough for a late redirect; the fill must not follow it.
        if not _still_on_ats(page, ats):
            return context, page, [], ("unsupported_form", "redirected_off_host", False), goto_seconds
        if not fields:
            body = await page.inner_text("body")
            if _CHALLENGE_TEXT.search(body or ""):
                return context, page, [], ("blocked", "challenge_page", False), goto_seconds
            if _CLOSED_TEXT.search(body or ""):
                return context, page, [], ("listing_closed", "posting_closed", True), goto_seconds
            return context, page, [], ("unsupported_form", "no_form", False), goto_seconds
    except BaseException:
        await context.close()    # the caller never receives the context, so it cannot close it
        raise
    return context, page, fields, None, goto_seconds


async def _screenshot(page, app_id: int) -> str | None:
    """Save a full-page screenshot to `data_dir()/screenshots/<id>-<unix>.png`; None if it fails."""
    folder = db.data_dir() / "screenshots"
    folder.mkdir(exist_ok=True)
    path = folder / f"{app_id}-{int(time.time())}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort, never the reason a row fails
        logger.warning("screenshot for application %s failed: %s", app_id, exc)
        return None
    return str(path)


async def settle_submit(page, row: dict, result: dict) -> None:
    """Write what a click came to, from `submitting`; the `outcome` is one `wait_for_outcome` returns.

    Called by `_attempt`, by `local_handoff` for the headed window and for a code typed into a parked
    context. `submitted` is `applied`. A `challenge` (a captcha popup the user must solve) is
    `blocked/captcha`. Everything else is `blocked/verify_submit` for the user to look at, with the
    screenshot kept for the card: `email_link` (the ATS mailed a link; `error='email_link'`),
    `rejected_by_form` and `unknown`. Never `applied` on anything the page did not prove.
    """
    app_id, token = row["id"], row["claim_token"]
    outcome = result["outcome"]
    if outcome == "submitted":
        _transition_application(app_id, token, "submitting", "applied", stamp="confirmed_at")
        return
    screenshot = await _screenshot(page, app_id)
    blocked_reason = "captcha" if outcome == "challenge" else "verify_submit"
    _transition_application(app_id, token, "submitting", "blocked_needs_input", blocked_reason=blocked_reason,
                            error=result["reason"], screenshot_path=screenshot)


async def _attempt(browser, row: dict, progress: dict) -> None:
    """One pass over a claimed row; raises on anything unexpected, `apply_one` settles that.

    `progress["status"]` is `in_progress` until the fence moves the row to `submitting`, so the
    caller knows which state a timeout or an exception happened in. It also carries `blocked` and
    `slow` back to `apply_one`.
    """
    app_id, token, ats = row["id"], row["claim_token"], row["ats"]
    conn = db.get_db()
    try:
        listing = dict(conn.execute("SELECT * FROM listings WHERE id = ?", (row["listing_id"],)).fetchone())
    finally:
        conn.close()
    # The profile the user approved, not the live row: an edit after approval is not sent. A
    # row with no batch raises (LookupError) and is settled like any other exception.
    profile = local_formfill.profile_for_fill(local_batch.snapshot(row["approval_batch_id"])["profile"])

    context, page, fields, failure, goto_seconds = await _open_form(browser, ats, listing["url"])
    progress["slow"] = goto_seconds > SLOW_PAGE_SECONDS
    try:
        if failure:
            skip_reason, error, listing_gone = failure
            if skip_reason == "blocked":
                # A block page is the ATS rate-limiting this IP, not a verdict on the job: back to
                # pending with the attempt refunded, and the queue backs this ATS off.
                progress["blocked"] = True
                _transition_application(app_id, token, "in_progress", "pending", error=f"blocked_{error}",
                                        refund_attempt=True)
                return
            _transition_application(app_id, token, "in_progress", "failed", skip_reason=skip_reason, error=error,
                  deactivate_listing_id=listing["id"] if listing_gone else None)
            return

        report = await local_formfill.fill_form_fields(page, fields, profile, listing)
        fields_used = {key: report[key] for key in ("filled", "missing", "consented", "declined")}
        if not report["ok"]:
            # A required field no profile value, saved answer, consent or decline answers. Nothing is
            # clicked. Jev classifies the questions BEFORE the transaction, so no network call holds
            # the write lock; the question rows then commit with the move to blocked.
            screenshot = await _screenshot(page, app_id)
            classified = await asyncio.to_thread(local_answers.classify_questions, report["missing"],
                                                 listing["company"])
            shapes = dict(zip(report["missing"], report["missing_shapes"]))
            _transition_application(app_id, token, "in_progress", "blocked_needs_input", blocked_reason="question",
                  fields_used=fields_used, screenshot_path=screenshot,
                  inside_transaction=lambda conn: local_answers.record_block(conn, row, listing, classified, shapes, whole_form=True))
            return

        left_host = False

        def mark_attempted() -> bool:
            """The fence: committed (synchronous=FULL) before the click, so a crash after it reads `submitting`.

            A page that left the ATS's hosts during the fill is not clicked: False, as for a lost row.
            """
            nonlocal left_host
            if not _still_on_ats(page, ats):
                left_host = True
                return False
            moved = _transition_application(app_id, token, "in_progress", "submitting", stamp="submit_attempted_at",
                          fields_used=fields_used)
            if moved:
                progress["status"] = "submitting"
            return moved

        result = await local_formfill.submit_form(page, fields, ats, mark_attempted)
        outcome = result["outcome"]
        if left_host:
            _transition_application(app_id, token, "in_progress", "failed", skip_reason="unsupported_form",
                                    error="redirected_off_host", fields_used=fields_used)
        elif outcome == "no_control":
            _transition_application(app_id, token, "in_progress", "failed", skip_reason="unsupported_form",
                  error=result["reason"], fields_used=fields_used)
        elif outcome == "captcha":
            # A visible widget, found before the fence: nothing was clicked, so the user can solve it in
            # a headed window. The app never solves one.
            screenshot = await _screenshot(page, app_id)
            _transition_application(app_id, token, "in_progress", "blocked_needs_input", blocked_reason="captcha",
                  error=result["reason"], fields_used=fields_used, screenshot_path=screenshot)
        elif outcome == "code":
            # The ATS wants the code it just emailed. The context stays open under the user's code, so
            # `park_for_code` takes it, and the queue slot is free for the next application.
            if await local_handoff.park_for_code(row, context, page, ats):
                context = None
        elif outcome != "lost_row":
            await settle_submit(page, row, result)
        # "lost_row": the fence found the row moved; nothing was clicked and nothing is written.
    finally:
        if context is not None:
            await context.close()


async def apply_one(browser, row: dict) -> dict:
    """Apply to one claimed row and write its outcome; returns `{blocked, slow}` for the queue.

    Called by `local_queues.run_all` per claim. `blocked` is True when the form page was a block
    page (the row is back to `pending`, attempt refunded) and `slow` when the page took over
    `SLOW_PAGE_SECONDS` to load or the row timed out, so the queue can back off. The row is capped at
    `APPLY_MAX_ROW_SECONDS`. An exception or timeout is settled by the state it happened in: before
    the fence the row goes back to `pending` (third exception: `attempts_exhausted`; timeout:
    `failed/timeout`), after the fence it is `blocked_needs_input/verify_submit`, because a click
    may have gone out. A failure in the attempt is settled, not raised, except a cancellation
    (`local_queues.stop_all`): before the fence nothing was clicked, so the row goes back to
    `pending` with the attempt refunded; after it, to `verify_submit`. Then the cancellation re-raises.
    """
    app_id, token = row["id"], row["claim_token"]
    progress = {"status": "in_progress", "blocked": False, "slow": False}
    cancelled = False
    try:
        await asyncio.wait_for(_attempt(browser, row, progress), APPLY_MAX_ROW_SECONDS)
        return {"blocked": progress["blocked"], "slow": progress["slow"]}
    except asyncio.TimeoutError:
        progress["slow"] = True
        if progress["status"] == "submitting":
            settle = ("submitting", "blocked_needs_input", {"blocked_reason": "verify_submit", "error": "row_timeout"})
        else:
            settle = ("in_progress", "failed", {"skip_reason": "timeout", "error": "row_timeout"})
    except asyncio.CancelledError:
        cancelled = True
        if progress["status"] == "submitting":
            settle = ("submitting", "blocked_needs_input",
                      {"blocked_reason": "verify_submit", "error": "stopped_during_submit"})
        else:
            settle = ("in_progress", "pending", {"refund_attempt": True})
    except Exception as exc:  # noqa: BLE001 - any failure is settled into a status, never escapes
        logger.exception("application %s raised", app_id)
        error = f"{type(exc).__name__}: {exc}"[:500]
        progress["slow"] = isinstance(exc, PlaywrightTimeout)    # a goto that hit GOTO_TIMEOUT_MS
        if progress["status"] == "submitting":
            settle = ("submitting", "blocked_needs_input", {"blocked_reason": "verify_submit", "error": error})
        elif row["attempt_count"] >= MAX_ATTEMPTS:
            settle = ("in_progress", "blocked_needs_input",
                      {"blocked_reason": "attempts_exhausted", "error": error})
        else:
            settle = ("in_progress", "pending", {"error": error})
    # The branches above only chose where the row goes; the one write happens here.
    from_status, to_status, columns = settle
    try:
        _transition_application(app_id, token, from_status, to_status, **columns)
    except Exception:  # noqa: BLE001 - e.g. the database stayed busy; the row keeps its state until recover()
        logger.exception("could not settle application %s", app_id)
    if cancelled:
        raise asyncio.CancelledError
    return {"blocked": progress["blocked"], "slow": progress["slow"]}


async def fill_check(application_id: int) -> dict:
    """Dry run: open the form for one application, fill it, click nothing, report coverage.

    Claims nothing and writes no status or `fields_used`; it reads the row whatever its status. Returns
    `{required_total, required_filled, missing, filled, screenshot_path}`, or `{error, skip_reason}`
    when the form cannot be opened (same causes as `_open_form`). The zero-touch rate per ATS is
    `required_filled == required_total` over a sample.
    """
    conn = db.get_db()
    try:
        row = dict(conn.execute("SELECT * FROM job_applications WHERE id = ?", (application_id,)).fetchone())
        listing = dict(conn.execute("SELECT * FROM listings WHERE id = ?", (row["listing_id"],)).fetchone())
        profile_row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    finally:
        conn.close()
    if profile_row is None:
        raise RuntimeError("no profile row")
    profile = local_formfill.profile_for_fill(profile_row)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, fields, failure, _ = await _open_form(browser, row["ats"], listing["url"])
            try:
                if failure:
                    return {"error": failure[1], "skip_reason": failure[0]}
                report = await local_formfill.fill_form_fields(page, fields, profile, listing)
                screenshot = await _screenshot(page, application_id)
            finally:
                if context is not None:
                    await context.close()
        finally:
            await browser.close()
    return {"required_total": report["required_total"], "required_filled": report["required_filled"],
            "missing": report["missing"], "filled": report["filled"], "screenshot_path": screenshot}
