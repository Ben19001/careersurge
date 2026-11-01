"""Hand a form to the user when it needs a person: a captcha window and an emailed code.

Used by `local_apply` (`park_for_code`, `drop_parked`, `is_parked`), `local_queues` (the context slots,
`parked_count`, `stop`) and the routes in `app.local_api` (`submit_code`, `open_in_browser`). The app never
solves a captcha or reads the user's mail: it stops, shows a card in the Needs you tray and finishes
the form when the user has done the one thing only they can. Flask-free, Supabase-free; the form is driven
by `local_formfill`, rows are written through `local_apply`'s compare-and-set (`local_apply` imports this
module as well, so every call into it goes through the module object at call time).

An emailed code (`blocked_needs_input/otp`). The worker found a code box after the click, so the ATS has NOT
accepted the application yet. It moves the row `submitting -> blocked/otp`, keeps the live browser context
open under a Future keyed by the application id, and gives its queue slot back. `submit_code` resolves the
Future, the context types the code, `blocked/otp -> submitting` fences the confirm click, and the outcome is
written like any other. No code within `CODE_WAIT_S` closes the context and requeues the row once
(`pending`, `error='otp_timeout'`; the old code is dead, a retry sends a new one); a second timeout on the
same row is `failed/timeout`.

A captcha (`blocked_needs_input/captcha`). A visible widget stops the headless run before the click; an
invisible reCAPTCHA shows its popup after it. Either way the headless context is closed.
`open_in_browser` claims the row (`blocked/captcha -> in_progress`), opens a HEADED Chromium on the user's
desktop at the form and refills it. With a visible widget it waits for the widget's response token, then
fences and clicks. With none it fences and clicks first, and the user solves the popup. `submitting` is
written before any click, either way. A closed window, or `CAPTCHA_WAIT_S` with nothing done, returns the
row to `blocked/captcha`.

Apply in the user's own browser. Some sites, Ashby among them, reject a form filled by an
automated window as spam, so a captcha card also offers `apply_manually`: the row moves to
`blocked/verify_submit` with `error='manual_apply'`, the posting opens in the user's usual browser, and
the user applies there and answers "I applied" or "I didn't apply" on the card. Nothing about the
automated window is hidden or changed.

Open browser contexts are capped at `MAX_CONTEXTS` (3 queues plus parked codes, about 1 GB of RAM): a queue
takes a slot before it claims a row and gives it back when its context closes, a parked context keeps it.
"""
import asyncio
import logging
import uuid

from playwright.async_api import async_playwright

from app.services import local_answers, local_apply, local_batch, local_formfill
from app.store import db

logger = logging.getLogger("careersurge.services.local_handoff")

# At most five open browser contexts: one per active queue (three) plus two parked one-time codes.
MAX_CONTEXTS = 5
# Emailed codes commonly live 10 minutes; waiting 5 leaves time to retry a dead code while it is fresh.
CODE_WAIT_S = 5 * 60
# The longest the headed window waits for the user to solve a captcha.
CAPTCHA_WAIT_S = 10 * 60
# The longest a clicked Open in browser waits for a free context slot before the row goes back to the tray.
SLOT_WAIT_S = 60
# How often the headed window looks for a solved widget.
TOKEN_POLL_S = 0.5
# The longest code the route accepts; real ones are 4 to 8 characters.
CODE_MAX_LENGTH = 64

# A solved reCAPTCHA, hCaptcha or Turnstile widget writes its response token into a hidden field.
_SOLVED_JS = """() => ['g-recaptcha-response', 'h-captcha-response', 'cf-turnstile-response'].some(
    name => [...document.querySelectorAll('[name="' + name + '"]')].some(el => el.value))"""

# application id -> {"future", "task", "phase"}; phase is "waiting" (for the code) or "submitting" (past the fence).
_parked: dict = {}
# application id -> the task running its headed window.
_headed: dict = {}
# (event loop, semaphore): one semaphore per loop, because a semaphore remembers the loop it first waited on.
_slots = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(MAX_CONTEXTS))
    return _slots[1]


async def acquire_context_slot(timeout: float | None = None) -> bool:
    """Wait for room to open one more browser context; False when `timeout` seconds pass first.

    Called by each ATS queue before it claims a row (with a short timeout, so a Pause or Stop still gets
    through while the queue waits) and by the headed window. The caller gives the slot back with
    `release_context_slot` when its context closes; a context parked for a code keeps it.
    """
    try:
        await asyncio.wait_for(_semaphore().acquire(), timeout)
    except asyncio.TimeoutError:
        return False
    return True


def release_context_slot() -> None:
    """Give back a slot taken by `acquire_context_slot`."""
    _semaphore().release()


def parked_count() -> int:
    """How many browser contexts wait for an emailed code; the shared browser must not close while any do."""
    return len(_parked)


def is_parked(app_id: int) -> bool:
    """Whether application `app_id` has a context parked for a code (the queue reads this after `apply_one`)."""
    return app_id in _parked


def drop_parked() -> None:
    """Cancel every parked wait; called by `local_apply.recover`, since no parked context survives a restart."""
    for entry in list(_parked.values()):
        _cancel_wait(entry, everything=True)


def _cancel_wait(entry: dict, everything: bool) -> None:
    """End a parked wait. A wait still on its Future is ended by cancelling the Future, not the task.

    A task cancelled before its first step never runs its `finally`, so its context and slot would
    leak; a cancelled Future makes the task raise inside its own wait, wherever it has got to. A task
    already past the code (typing, confirming) is cancelled directly, and only when `everything` is
    set (a restart), because a Stop lets it finish. If `submit_code` resolved the Future a moment
    ago and the task has not woken yet, `cancel()` does nothing: the code was accepted, so the task
    goes on to type and confirm, which is what a Stop allows anyway.
    """
    if entry["phase"] == "waiting":
        entry["future"].cancel()
    elif everything:
        entry["task"].cancel()


async def stop() -> None:
    """Wind down for a quit or a Stop; returns when no parked context or headed window is left.

    Called by `local_queues.stop_all` and when `run_all` ends. A context still waiting for a code is
    cancelled (its row goes back to `pending`: nothing was accepted, and the code dies with the process).
    One past the fence finishes on its own, bounded by the 45 s confirmation window, as `stop_all` does
    for an application in `submitting`. A headed window is cancelled.
    """
    for entry in list(_parked.values()):
        _cancel_wait(entry, everything=False)
    for task in list(_headed.values()):
        task.cancel()
    parked_tasks = [entry["task"] for entry in list(_parked.values())]
    headed_tasks = list(_headed.values())
    await asyncio.gather(*parked_tasks, *headed_tasks, return_exceptions=True)


async def park_for_code(row: dict, context, page, ats: str, cleanup=None) -> bool:
    """Keep the live context open for the code the page asks for; False when the row is no longer ours.

    Called by `local_apply._attempt` (and the headed window) when `wait_for_outcome` found a code box.
    Moves the row `submitting -> blocked/otp` and starts the wait. On True the parked wait owns the
    `context`, the context slot and `cleanup` (an async callable run after the context closes, for a
    headed browser); the caller must not close them. On False nothing was parked and the caller
    closes its context as usual.
    """
    moved = local_apply._transition_application(row["id"], row["claim_token"], "submitting",
                                                "blocked_needs_input", blocked_reason="otp")
    if not moved:
        return False
    entry = {"future": asyncio.get_running_loop().create_future(), "phase": "waiting"}
    entry["task"] = asyncio.ensure_future(_hold_for_code(row, context, page, ats, cleanup, entry))
    _parked[row["id"]] = entry
    return True


def submit_code(app_id: int, code: str) -> bool:
    """Hand the user's code to the context parked for `app_id`; False when none is waiting for one.

    Called by `POST /api/otp/{id}` on the event loop (a Future is not thread-safe), which answers 404
    on False: the code expired, the app restarted, or the code was already sent.
    """
    entry = _parked.get(app_id)
    if entry is None or entry["phase"] != "waiting" or entry["future"].done():
        return False
    entry["future"].set_result(code)
    return True


async def _hold_for_code(row: dict, context, page, ats: str, cleanup, entry: dict) -> None:
    """The parked wait: for the code, then type it, fence, confirm and write the outcome.

    Run as a task by `park_for_code`. `status` is where the row is, so an exception or a cancellation
    is settled the way `local_apply.apply_one` settles it: before the fence the row goes back to
    `pending` with the attempt refunded (nothing was accepted), after it to `verify_submit`.
    """
    app_id, token = row["id"], row["claim_token"]
    status = "blocked_needs_input"
    try:
        try:
            code = await asyncio.wait_for(entry["future"], CODE_WAIT_S)
        except asyncio.TimeoutError:
            if row["error"] == "otp_timeout":
                local_apply._transition_application(app_id, token, status, "failed", skip_reason="timeout",
                                                    error="otp_timeout")
            else:
                local_apply._transition_application(app_id, token, status, "pending", error="otp_timeout",
                                                    refund_attempt=True)
            return
        entry["phase"] = "submitting"
        # The page waited for the code for minutes and may have moved off the ATS meanwhile; the code is
        # not typed there. The form itself was already sent, so the user checks it,
        # with a picture of where the tab ended up, as `settle_submit` keeps one.
        if not local_apply._still_on_ats(page, ats):
            screenshot = await local_apply._screenshot(page, app_id)
            local_apply._transition_application(app_id, token, status, "blocked_needs_input",
                                                blocked_reason="verify_submit", error="redirected_off_host",
                                                screenshot_path=screenshot)
            return
        box = page.locator("[data-cs-code]").first
        await box.fill(code, timeout=local_formfill.PLAYWRIGHT_ACTION_TIMEOUT)
        # The fence for the confirm click: from here a code may have been accepted.
        if not local_apply._transition_application(app_id, token, status, "submitting"):
            return
        status = "submitting"
        control, _ = await local_formfill.find_submit_control(page, ats)
        before_url = page.url
        body_before = await local_formfill.read_body(page)
        if control is not None:
            await control.evaluate("el => el.click()")
        else:
            await box.press("Enter")
        result = await local_formfill.wait_for_outcome(page, ats, before_url, body_before)
        if result["outcome"] == "code":
            result = {"outcome": "unknown", "reason": "second_code_prompt"}
        await local_apply.settle_submit(page, row, result)
    except asyncio.CancelledError:
        if status == "submitting":
            local_apply._transition_application(app_id, token, status, "blocked_needs_input",
                                                blocked_reason="verify_submit", error="stopped_during_submit")
        else:
            local_apply._transition_application(app_id, token, status, "pending", refund_attempt=True)
        raise
    except Exception as exc:  # noqa: BLE001 - any failure is settled into a status, never escapes
        logger.exception("application %s raised while waiting on its code", app_id)
        error = f"{type(exc).__name__}: {exc}"[:500]
        if status == "submitting":
            local_apply._transition_application(app_id, token, status, "blocked_needs_input",
                                                blocked_reason="verify_submit", error=error)
        else:
            local_apply._transition_application(app_id, token, status, "pending", error=error,
                                                refund_attempt=True)
    finally:
        _parked.pop(app_id, None)
        try:
            await context.close()
        except Exception:  # noqa: BLE001 - the browser may already be gone
            logger.warning("closing the context of application %s failed", app_id, exc_info=True)
        if cleanup is not None:
            await cleanup()
        release_context_slot()


async def open_in_browser(app_id: int) -> bool:
    """Claim a `blocked/captcha` row and open it in a headed window; False when it is not waiting on a captcha.

    Called by `POST /api/captcha/{id}/open`, which answers 409 on False. The claim is one
    compare-and-set (`blocked/captcha -> in_progress`, a fresh `claim_token`, the old screenshot gone),
    so a second click cannot open a second window. The window runs as a task, so this returns as soon as
    the row is claimed; the card leaves the tray and comes back if the window closes without a result.
    """
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT screenshot_path FROM job_applications WHERE id = ? "
                           "AND status = 'blocked_needs_input' AND blocked_reason = 'captcha'",
                           (app_id,)).fetchone()
        if old is None:
            conn.execute("ROLLBACK")
            return False
        now = local_apply._now()
        row = conn.execute(
            "UPDATE job_applications SET status = 'in_progress', blocked_reason = NULL, claim_token = ?, "
            "claimed_at = ?, screenshot_path = NULL, updated_at = ? WHERE id = ? RETURNING *",
            (uuid.uuid4().hex, now, now, app_id)).fetchone()
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    local_answers.delete_screenshot(old["screenshot_path"])
    _headed[app_id] = asyncio.ensure_future(_run_headed(dict(row)))
    await asyncio.sleep(0)    # let the task start, so a Stop right now still settles the row (see `park_for_code`)
    return True


def blocked_posting_url(app_id: int) -> str | None:
    """The posting URL of a blocked row, safe to hand to the user's own browser; None when the row is not blocked.

    Called by `apply_manually` and `POST /api/posting/{id}/open`. The URL came from Apify, so it is
    opened only when `local_apply._ats_url_allowed` passes it (`https` on a host of the row's ATS, the
    check `_open_form` uses, with no userinfo, port, backslash or whitespace); anything else is a ValueError.
    """
    conn = db.get_db()
    try:
        row = conn.execute("SELECT a.ats, l.url FROM job_applications a JOIN listings l ON l.id = a.listing_id "
                           "WHERE a.id = ? AND a.status = 'blocked_needs_input'", (app_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    url = row["url"] or ""
    if not local_apply._ats_url_allowed(url, row["ats"]):
        raise ValueError("that posting is not on a site this app opens")
    return url


def apply_manually(app_id: int) -> str | None:
    """Move a `blocked/captcha` row to the manual-apply card and return its posting URL; None when it is not waiting on a captcha.

    Called by `POST /api/captcha/{id}/manual`, which opens the URL in the user's browser and answers
    409 on None. One compare-and-set, `blocked/captcha -> blocked/verify_submit` with
    `error='manual_apply'`; the screenshot stays so "View last page" still works. A URL
    `blocked_posting_url` refuses raises ValueError before anything is written. The row never expires
    (`expire_blocked` skips `verify_submit`): only the user knows whether they applied.
    """
    url = blocked_posting_url(app_id)
    if url is None:
        return None
    conn = db.get_db()
    try:
        moved = conn.execute(
            "UPDATE job_applications SET blocked_reason = 'verify_submit', error = 'manual_apply', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ? AND status = 'blocked_needs_input' AND blocked_reason = 'captcha'", (app_id,)).rowcount
    finally:
        conn.close()
    return url if moved else None


async def _return_to_captcha(row: dict, from_status: str, page=None, error=None) -> None:
    """Put the row back in `blocked/captcha` for the user to try again; the screenshot is taken if the page is still open."""
    screenshot = None
    if page is not None and not page.is_closed():
        screenshot = await local_apply._screenshot(page, row["id"])
    local_apply._transition_application(row["id"], row["claim_token"], from_status, "blocked_needs_input",
                                        blocked_reason="captcha", error=error, screenshot_path=screenshot)


async def _shutdown_headed(browser, playwright) -> None:
    """Close the headed browser and its Playwright driver; best effort, the user may already have closed the window."""
    if browser is not None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            logger.warning("closing the headed browser failed", exc_info=True)
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:  # noqa: BLE001
            logger.warning("stopping the Playwright driver failed", exc_info=True)


async def _run_headed(row: dict) -> None:
    """The headed window for one claimed `captcha` row: refill, wait for the user, click, write the outcome.

    Run as a task by `open_in_browser`. `status` is where the row is. A visible widget makes the window
    wait for its response token before the fence; otherwise the fence and click come first and the
    user solves the popup that follows. Before the click (`in_progress`), a close or `CAPTCHA_WAIT_S`
    with nothing done returns the row to `blocked/captcha`: nothing was sent. Once the click has gone
    out (`submitting`), every outcome but a confirmed success is `verify_submit` (a close, a timeout, a
    popup that vanished, an answer nothing recognises), never a state that can click Submit again. No
    free context slot within `SLOT_WAIT_S` returns the row to the tray with `error='no_free_slot'`.
    """
    app_id, token, ats = row["id"], row["claim_token"], row["ats"]
    status = "in_progress"
    playwright = browser = context = page = None
    parked = slot_held = False
    try:
        if not await acquire_context_slot(SLOT_WAIT_S):
            await _return_to_captcha(row, status, error="no_free_slot")    # the card says why (needs-you.tsx)
            return
        slot_held = True
        conn = db.get_db()
        try:
            listing = dict(conn.execute("SELECT * FROM listings WHERE id = ?", (row["listing_id"],)).fetchone())
        finally:
            conn.close()
        profile = local_formfill.profile_for_fill(local_batch.snapshot(row["approval_batch_id"])["profile"])
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=False)
        context, page, fields, failure, _ = await local_apply._open_form(browser, ats, listing["url"])
        if failure:
            skip_reason, error, listing_gone = failure
            if skip_reason == "blocked":
                await _return_to_captcha(row, status, page, error=f"blocked_{error}")
            else:
                local_apply._transition_application(app_id, token, status, "failed", skip_reason=skip_reason,
                      error=error, deactivate_listing_id=listing["id"] if listing_gone else None)
            return
        await page.bring_to_front()
        report = await local_formfill.fill_form_fields(page, fields, profile, listing)
        if not report["ok"]:
            await _return_to_captcha(row, status, page, error="form_needs_answer")
            return

        if await local_formfill.captcha_widget(page):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CAPTCHA_WAIT_S
            solved = False
            while loop.time() < deadline and not page.is_closed():
                if await page.evaluate(_SOLVED_JS):
                    solved = True
                    break
                await asyncio.sleep(TOKEN_POLL_S)
            if not solved:
                await _return_to_captcha(row, status, page)
                return

        left_host = False

        def mark_attempted() -> bool:
            """The fence, again: this click is a new submission attempt, so `submitting` is written first.

            A page that left the ATS's hosts while the window was open is not clicked: False, as for a lost row.
            """
            nonlocal status, left_host
            if not local_apply._still_on_ats(page, ats):
                left_host = True
                return False
            moved = local_apply._transition_application(app_id, token, "in_progress", "submitting",
                                                        stamp="submit_attempted_at")
            if moved:
                status = "submitting"
            return moved

        form_url = page.url
        form_text = await local_formfill.read_body(page)
        result = await local_formfill.submit_form(page, fields, ats, mark_attempted, allow_captcha=True)
        if left_host:
            local_apply._transition_application(app_id, token, status, "failed",
                                                skip_reason="unsupported_form", error="redirected_off_host")
            return
        if result["outcome"] in ("no_control", "lost_row"):
            if result["outcome"] == "no_control":
                local_apply._transition_application(app_id, token, status, "failed",
                                                    skip_reason="unsupported_form", error=result["reason"])
            return
        if result["outcome"] == "challenge":
            # The popup is on screen: the user solves it and the form sends itself. The click has gone out, so
            # whatever comes of the wait (a close, a timeout, an answer nothing here recognises) is settled
            # below as `verify_submit`, never as a captcha card: Open in browser would submit the form again.
            result = await local_formfill.wait_for_outcome(page, ats, form_url, form_text,
                                                           timeout_ms=CAPTCHA_WAIT_S * 1000, watch_challenge=False)
        if result["outcome"] == "code":
            parked = await park_for_code(row, context, page, ats, cleanup=lambda: _shutdown_headed(browser, playwright))
            return
        await local_apply.settle_submit(page, row, result)
    except asyncio.CancelledError:
        if status == "submitting":
            local_apply._transition_application(app_id, token, status, "blocked_needs_input",
                                                blocked_reason="verify_submit", error="stopped_during_submit")
        else:
            await _return_to_captcha(row, status)
        raise
    except Exception as exc:  # noqa: BLE001 - any failure is settled into a status, never escapes
        window_closed = page is not None and page.is_closed()
        if window_closed:
            logger.info("the user closed the window of application %s", app_id)
        else:
            logger.exception("application %s raised in the headed window", app_id)
        error = f"{type(exc).__name__}: {exc}"[:500]
        if status == "submitting":
            local_apply._transition_application(app_id, token, status, "blocked_needs_input",
                                                blocked_reason="verify_submit", error=error)
        else:
            await _return_to_captcha(row, status, page, error=None if window_closed else error)
    finally:
        _headed.pop(app_id, None)
        if not parked:
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001 - the user closed the window
                    pass
            await _shutdown_headed(browser, playwright)
            if slot_held:
                release_context_slot()
