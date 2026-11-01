"""The local apply worker's claim, status machine, outcomes and recovery.

Runs against a throwaway data dir (`CAREERSURGE_DATA_DIR`). The page tests drive a REAL Chromium,
but no request leaves the machine: `_RoutedBrowser` answers every request from a dict of
`{url: (status, html)}` and aborts anything else, so the ATS hosts are served from memory. A real
submission to a live Greenhouse, Lever or Ashby requisition is outside any test (the closest is a
dry run, `fill_check`).
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports

from app.services import jev, local_apply, local_formfill  # noqa: E402
from app.store import db  # noqa: E402

try:
    from playwright.async_api import async_playwright
    _HAVE_BROWSER = True
except ImportError:  # pragma: no cover - environment without playwright
    _HAVE_BROWSER = False

GH_URL = "https://boards.greenhouse.io/acme/jobs/1"
GH_DONE = "https://boards.greenhouse.io/acme/jobs/1/confirmation"
THANKS = (200, "<h1>Thank you for applying</h1>")

# A fillable Greenhouse form: both required fields have profile columns, the submit button is named.
GH_FORM = """
<form id="application_form" method="post" action="/acme/jobs/1/confirmation">
  <label for="first_name">First Name *</label><input id="first_name" name="first_name" required>
  <label for="email">Email *</label><input id="email" name="email" required>
  {extra}
  <input type="submit" id="submit_app" value="Submit Application">
</form>
{script}"""

_REJECT = """<script>document.getElementById('submit_app').addEventListener('click', e => {
  e.preventDefault(); const d = document.createElement('div'); d.setAttribute('role', 'alert');
  d.textContent = 'Email is invalid'; document.body.prepend(d); });</script>"""
_SILENT = """<script>document.getElementById('submit_app').addEventListener('click',
  e => e.preventDefault());</script>"""


def _gh(extra="", script=""):
    return (200, GH_FORM.format(extra=extra, script=script))


class _RoutedBrowser:
    """A browser whose contexts answer from `pages` and record every request they see."""

    def __init__(self, browser, pages):
        self.browser, self.pages, self.requests = browser, pages, []

    async def close(self):
        await self.browser.close()

    async def new_context(self):
        context = await self.browser.new_context()

        async def handle(route):
            request = route.request
            self.requests.append((request.method, request.url))
            hit = self.pages.get(request.url)
            if hit is None:
                await route.abort()
            else:
                await route.fulfill(status=hit[0], body=hit[1], content_type="text/html",
                                    headers=hit[2] if len(hit) > 2 else None)
        await context.route("**/*", handle)
        return context

    def posts(self):
        return [url for method, url in self.requests if method == "POST"]


class _RoutedPlaywright:
    """Stands in for `async_playwright()` where the code under test launches its own browser."""

    def __init__(self, pages):
        self.pages, self.browsers = pages, []

    async def __aenter__(self):
        self._real = async_playwright()
        playwright = await self._real.__aenter__()
        outer = self

        class _Chromium:
            async def launch(self, **kwargs):
                routed = _RoutedBrowser(await playwright.chromium.launch(**kwargs), outer.pages)
                outer.browsers.append(routed)
                return routed
        self.chromium = _Chromium()
        return self

    async def __aexit__(self, *exc):
        return await self._real.__aexit__(*exc)


class _NoBrowser:
    """A browser that fails the test if anything asks it for a context."""

    async def new_context(self):
        raise AssertionError("the browser must not be touched")


class _StoreTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # On Windows a server thread can still hold careersurge.db open while the folder is deleted
        # (WinError 32). Each test has its own folder, so a leftover file never reaches another test.
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"CAREERSURGE_DATA_DIR": str(self.tmp / "data")})
        env.start()
        self.addCleanup(env.stop)
        db.migrate()
        self.conn = db.get_db()
        self.addCleanup(self.conn.close)
        # The batch `add_profile` fills in; the worker reads the profile from it.
        self.conn.execute("INSERT INTO approval_batches (id, approved_at, snapshot) "
                          "VALUES (1, '2026-10-01T00:00:00.000Z', '{\"profile\": null}')")
        # A blocked form asks Jev to classify its questions; no test reaches the network.
        no_jev = mock.patch.object(jev, "choose", side_effect=jev.JevUnavailable("no_key"))
        no_jev.start()
        self.addCleanup(no_jev.stop)
        self.resume = self.tmp / "resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4 fake")
        self._next = 0
        for name, value in (("SUBMIT_CONFIRM_TIMEOUT_MS", 300), ("FORM_RENDER_TIMEOUT_MS", 300),
                            ("PLAYWRIGHT_ACTION_TIMEOUT", 2000)):
            patch = mock.patch.object(local_formfill, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def add_profile(self, **extra):
        """Insert the profile row and freeze it as approval batch 1, which every non-`prepared`
        row made by `add_row` belongs to, so the worker fills from it as it does after a real Approve."""
        columns = {"id": 1, "email": "jane@example.com", "full_name": "Jane Doe", "first_name": "Jane",
                   "last_name": "Doe", "resume_path": str(self.resume), **extra}
        self.conn.execute(f"INSERT INTO profile ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                          list(columns.values()))
        self.freeze_profile()

    def freeze_profile(self):
        """Copy the profile row as it is now into approval batch 1 (what Approve would have frozen)."""
        frozen = dict(self.conn.execute("SELECT * FROM profile WHERE id = 1").fetchone())
        self.conn.execute("UPDATE approval_batches SET snapshot = ? WHERE id = 1", (json.dumps({"profile": frozen}),))

    def add_row(self, url=GH_URL, ats="greenhouse", status="pending", approved_at=None, **columns):
        """One listing plus its application row; returns the application id."""
        self._next += 1
        listing_id = self.conn.execute(
            "INSERT INTO listings (source, external_id, url, company_key) VALUES (?, ?, ?, 'acme')",
            (ats, str(self._next), url)).lastrowid
        batch = None if status in ("prepared", "cancelled") else 1
        columns = {"listing_id": listing_id, "ats": ats, "status": status, "approved_at": approved_at,
                   "approval_batch_id": batch, **columns}
        return self.conn.execute(
            f"INSERT INTO job_applications ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            list(columns.values())).lastrowid

    def row(self, app_id):
        return dict(self.conn.execute("SELECT * FROM job_applications WHERE id = ?", (app_id,)).fetchone())

    def listing_active(self, app_id):
        return self.conn.execute(
            "SELECT is_active FROM listings WHERE id = (SELECT listing_id FROM job_applications WHERE id = ?)",
            (app_id,)).fetchone()[0]


class ClaimTests(_StoreTestCase):
    def test_each_pending_row_is_claimed_once_oldest_approval_first(self):
        later = self.add_row(approved_at="2026-10-02T00:00:00.000Z")
        earlier = self.add_row(approved_at="2026-10-01T00:00:00.000Z")
        other_ats = self.add_row(ats="lever", url="https://jobs.lever.co/a/b", approved_at="2026-09-01T00:00:00.000Z")

        first, second = local_apply.claim("greenhouse"), local_apply.claim("greenhouse")

        self.assertEqual((first["id"], second["id"]), (earlier, later))
        self.assertEqual((first["attempt_count"], first["status"]), (1, "in_progress"))
        self.assertTrue(first["claim_token"])
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertIsNone(local_apply.claim("greenhouse"))
        self.assertEqual(self.row(other_ats)["status"], "pending")

    def test_avoided_rows_are_claimed_last_but_still_claimed_when_alone(self):
        head = self.add_row(approved_at="2026-10-01T00:00:00.000Z")
        behind = self.add_row(approved_at="2026-10-02T00:00:00.000Z")

        first = local_apply.claim("greenhouse", avoid=(head,))
        second = local_apply.claim("greenhouse", avoid=(head,))

        self.assertEqual((first["id"], second["id"]), (behind, head))

    def test_a_blocked_exit_clears_blocked_reason_in_the_same_update(self):
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="question", claim_token="t1")

        self.assertFalse(local_apply._transition_application(app_id, "stale", "blocked_needs_input", "pending"))
        self.assertEqual(self.row(app_id)["blocked_reason"], "question")
        self.assertTrue(local_apply._transition_application(app_id, "t1", "blocked_needs_input", "pending"))
        self.assertEqual((self.row(app_id)["status"], self.row(app_id)["blocked_reason"]), ("pending", None))


class RecoverTests(_StoreTestCase):
    def test_recover_settles_each_state_and_leaves_other_blocked_rows(self):
        in_progress = self.add_row(status="in_progress", claim_token="a", attempt_count=1)
        submitting = self.add_row(status="submitting", claim_token="b", attempt_count=1)
        otp = self.add_row(status="blocked_needs_input", blocked_reason="otp", claim_token="c")
        question = self.add_row(status="blocked_needs_input", blocked_reason="question", claim_token="d")

        counts = local_apply.recover()

        self.assertEqual(counts, {"in_progress": 1, "submitting": 1, "otp": 1})
        self.assertEqual((self.row(in_progress)["status"], self.row(in_progress)["attempt_count"]), ("pending", 1))
        self.assertEqual((self.row(submitting)["status"], self.row(submitting)["blocked_reason"]),
                         ("blocked_needs_input", "verify_submit"))
        self.assertEqual((self.row(otp)["status"], self.row(otp)["blocked_reason"]), ("pending", None))
        self.assertEqual((self.row(question)["status"], self.row(question)["blocked_reason"]),
                         ("blocked_needs_input", "question"))


class NoBrowserOutcomeTests(_StoreTestCase):
    async def test_a_host_outside_the_allowlist_or_the_rows_ats_fails_without_a_context(self):
        self.add_profile()
        for ats, url in (("greenhouse", "https://example.com/acme/jobs/1"),
                         ("greenhouse", "https://jobs.lever.co/acme/1"),
                         ("lever", "https://boards.greenhouse.io/acme/jobs/1"),
                         ("greenhouse", "http://boards.greenhouse.io/acme/jobs/1"),
                         ("greenhouse", "https://boards.greenhouse.io.evil.example/acme/jobs/1")):
            with self.subTest(ats=ats, url=url):
                app_id = self.add_row(url=url, ats=ats)
                await local_apply.apply_one(_NoBrowser(), local_apply.claim(ats))
                row = self.row(app_id)
                self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                                 ("failed", "unsupported_form", "host_not_allowed"))
                self.assertEqual(self.listing_active(app_id), 1)

    async def test_a_url_two_parsers_read_differently_fails_without_a_context(self):
        # Python reads the host after "\@" or "@", a browser may read the one before it.
        self.add_profile()
        for url in ("https://evil.example\\@boards.greenhouse.io/acme/jobs/1",
                    "https://evil.example@boards.greenhouse.io/acme/jobs/1",
                    "https://boards.greenhouse.io:8443/acme/jobs/1",
                    "https://boards.greenhouse.io/acme/jobs/1 x",
                    "https://boards.greenhouse.io/acme/jobs/1\n"):
            with self.subTest(url=url):
                app_id = self.add_row(url=url)
                await local_apply.apply_one(_NoBrowser(), local_apply.claim("greenhouse"))
                row = self.row(app_id)
                self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                                 ("failed", "unsupported_form", "host_not_allowed"))

    async def test_a_row_that_moved_after_the_claim_is_not_written(self):
        self.add_profile()
        app_id = self.add_row(url="https://example.com/x")
        claimed = local_apply.claim("greenhouse")
        self.conn.execute("UPDATE job_applications SET status = 'cancelled', skip_reason = 'user_skipped' "
                          "WHERE id = ?", (app_id,))

        await local_apply.apply_one(_NoBrowser(), claimed)

        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]), ("cancelled", "user_skipped", None))

    async def test_an_exception_requeues_twice_then_exhausts_the_attempts(self):
        self.add_profile()
        app_id = self.add_row()
        boom = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with mock.patch.object(local_apply, "_open_form", boom):
            seen = []
            for _ in range(3):
                await local_apply.apply_one(_NoBrowser(), local_apply.claim("greenhouse"))
                row = self.row(app_id)
                seen.append((row["attempt_count"], row["status"], row["blocked_reason"]))
        self.assertEqual(seen, [(1, "pending", None), (2, "pending", None),
                                (3, "blocked_needs_input", "attempts_exhausted")])
        self.assertIn("boom", self.row(app_id)["error"])

    async def test_a_timeout_before_the_fence_fails_as_timeout(self):
        self.add_profile()
        app_id = self.add_row()

        async def hang(*args, **kwargs):
            await asyncio.sleep(30)
        with mock.patch.object(local_apply, "APPLY_MAX_ROW_SECONDS", 0.3), \
                mock.patch.object(local_apply, "_open_form", hang):
            await local_apply.apply_one(_NoBrowser(), local_apply.claim("greenhouse"))
        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"]), ("failed", "timeout"))


    async def test_eu_hosts_are_opened_and_a_company_domain_path_is_refused(self):
        self.add_profile()
        eu = self.add_row(url="https://jobs.eu.lever.co/x/1", ats="lever")
        elsewhere = self.add_row(url="https://jobs.acme.com/lever/1", ats="lever")
        asked = []

        class _CountingBrowser:
            async def new_context(self):
                asked.append("context")
                raise RuntimeError("stop here: the host was allowed")
        await local_apply.apply_one(_CountingBrowser(), local_apply.claim("lever"))
        self.assertEqual(self.row(eu)["status"], "pending")    # the stub's error requeued it
        self.conn.execute("UPDATE job_applications SET status = 'cancelled' WHERE id = ?", (eu,))
        await local_apply.apply_one(_CountingBrowser(), local_apply.claim("lever"))

        self.assertEqual(asked, ["context"])
        row = self.row(elsewhere)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                         ("failed", "unsupported_form", "host_not_allowed"))
        for host in ("job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io"):
            self.assertIn(host, local_apply._ATS_HOSTS["greenhouse"])

    async def test_a_refunded_transition_takes_the_claims_attempt_back_off(self):
        app_id = self.add_row(status="in_progress", claim_token="t", attempt_count=2)
        self.assertTrue(local_apply._transition_application(app_id, "t", "in_progress", "pending",
                                                            refund_attempt=True))
        self.assertEqual((self.row(app_id)["status"], self.row(app_id)["attempt_count"]), ("pending", 1))

    async def test_cancelled_before_the_fence_requeues_with_the_attempt_refunded_and_reraises(self):
        self.add_profile()
        app_id = self.add_row()

        async def hang(*args, **kwargs):
            await asyncio.sleep(30)
        with mock.patch.object(local_apply, "_open_form", hang):
            task = asyncio.ensure_future(local_apply.apply_one(_NoBrowser(), local_apply.claim("greenhouse")))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row = self.row(app_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("pending", 0))

    async def test_a_slow_goto_is_reported_to_the_queue(self):
        self.add_profile()
        self.add_row()
        slow = mock.AsyncMock(return_value=(None, None, [], ("unsupported_form", "no_form", False), 12.0))
        with mock.patch.object(local_apply, "_open_form", slow):
            outcome = await local_apply.apply_one(_NoBrowser(), local_apply.claim("greenhouse"))
        self.assertEqual(outcome, {"blocked": False, "slow": True})

@unittest.skipUnless(_HAVE_BROWSER, "playwright not installed")
class PageOutcomeTests(_StoreTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.real = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.real.close)
        self.add_profile()

    async def apply(self, pages, **row_kwargs):
        """Seed one pending row, claim it and apply it against `pages`; returns (id, routed browser)."""
        app_id = self.add_row(**row_kwargs)
        browser = _RoutedBrowser(self.real, pages)
        await local_apply.apply_one(browser, local_apply.claim(row_kwargs.get("ats", "greenhouse")))
        return app_id, browser

    async def test_all_required_filled_and_a_thank_you_page_is_applied_on_each_ats(self):
        cases = (
            ("greenhouse", GH_URL, GH_URL, GH_FORM.format(extra="", script="")),
            ("lever", "https://jobs.lever.co/acme/abc?lever-source=x", "https://jobs.lever.co/acme/abc/apply",
             '<form method="post" action="/acme/abc/done"><label for="n">Full name *</label>'
             '<input id="n" name="name" required><button type="submit" data-qa="btn-submit">Submit</button></form>'),
            ("ashby", "https://jobs.ashbyhq.com/acme/abc", "https://jobs.ashbyhq.com/acme/abc/application",
             '<form method="post" action="/acme/abc/done"><label for="e">Email *</label>'
             '<input id="e" name="email" required><button type="submit"'
             ' data-testid="application-form-submit-button">Submit</button></form>'),
        )
        for ats, listing_url, form_url, html in cases:
            with self.subTest(ats=ats):
                done = form_url.rsplit("/", 1)[0] + "/done" if ats != "greenhouse" else GH_DONE
                pages = {form_url: (200, html), done: THANKS}
                app_id, browser = await self.apply(pages, url=listing_url, ats=ats)
                row = self.row(app_id)
                self.assertEqual((row["status"], row["blocked_reason"], row["attempt_count"]), ("applied", None, 1))
                self.assertTrue(row["submit_attempted_at"] and row["confirmed_at"])
                self.assertEqual(browser.posts(), [done])
                self.assertTrue(json.loads(row["fields_used"])["filled"])

    async def test_a_required_field_nothing_answers_blocks_as_question_without_a_click(self):
        extra = '<label for="why">Why us? *</label><input id="why" name="why" required>'
        app_id, browser = await self.apply({GH_URL: _gh(extra), GH_DONE: THANKS})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"]), ("blocked_needs_input", "question"))
        self.assertEqual(json.loads(row["fields_used"])["missing"], ["Why us? *"])
        self.assertIsNone(row["submit_attempted_at"])
        self.assertEqual(browser.posts(), [])

    async def test_a_reblock_drops_the_card_of_a_question_this_attempt_did_not_miss(self):
        # Seen on a real form: a Yes/No the fill had since learned kept its first card and held the job blocked.
        app_id = self.add_row()
        stale = self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, "
            "listing_id) SELECT 'Old Yes/No?', 'old yes no', 'open_ended', 'job', listing_id "
            "FROM job_applications WHERE id = ?", (app_id,)).lastrowid
        self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id, field_shape) "
                          "VALUES (?, ?, '{\"kind\": \"single\", \"options\": [\"Old Yes/No?\"]}')", (app_id, stale))
        extra = '<label for="why">Why us? *</label><input id="why" name="why" required>'
        await local_apply.apply_one(_RoutedBrowser(self.real, {GH_URL: _gh(extra), GH_DONE: THANKS}),
                                    local_apply.claim("greenhouse"))

        self.assertEqual(self.row(app_id)["status"], "blocked_needs_input")
        linked = self.conn.execute(
            "SELECT q.original_question FROM application_blocking_questions b "
            "JOIN application_qna_bank q ON q.id = b.qna_id WHERE b.application_id = ?", (app_id,)).fetchall()
        self.assertEqual([row[0] for row in linked], ["Why us? *"])

    async def test_404_and_410_close_the_listing(self):
        for status in (404, 410):
            with self.subTest(status=status):
                app_id, _ = await self.apply({GH_URL: (status, "<h1>gone</h1>")})
                row = self.row(app_id)
                self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                                 ("failed", "listing_closed", f"http_{status}"))
                self.assertEqual(self.listing_active(app_id), 0)

    async def test_a_closed_posting_message_closes_the_listing(self):
        app_id, _ = await self.apply({GH_URL: (200, "<p>This job is no longer accepting applications.</p>")})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"]), ("failed", "listing_closed"))
        self.assertEqual(self.listing_active(app_id), 0)

    async def test_no_form_within_the_render_budget_is_unsupported_and_the_listing_stays_active(self):
        app_id, _ = await self.apply({GH_URL: (200, "<p>hello</p>")})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]), ("failed", "unsupported_form", "no_form"))
        self.assertEqual(self.listing_active(app_id), 1)

    async def test_validation_errors_after_the_click_block_as_verify_submit_with_a_screenshot(self):
        app_id, browser = await self.apply({GH_URL: _gh(script=_REJECT), GH_DONE: THANKS})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"], row["error"]),
                         ("blocked_needs_input", "verify_submit", "form_rejected"))
        self.assertTrue(row["submit_attempted_at"])
        self.assertTrue(Path(row["screenshot_path"]).is_file())
        self.assertTrue(Path(row["screenshot_path"]).is_relative_to(db.data_dir() / "screenshots"))

    async def test_no_response_within_the_confirmation_wait_blocks_as_verify_submit(self):
        app_id, _ = await self.apply({GH_URL: _gh(script=_SILENT), GH_DONE: THANKS})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"], row["error"]),
                         ("blocked_needs_input", "verify_submit", "submit_outcome_unknown"))
        self.assertTrue(Path(row["screenshot_path"]).is_file())

    async def test_a_redirect_off_the_ats_host_fills_nothing(self):
        pages = {GH_URL: (302, "", {"Location": "https://jobs.lever.co/acme/1"}),
                 "https://jobs.lever.co/acme/1": _gh(), GH_DONE: THANKS}
        app_id, browser = await self.apply(pages)
        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                         ("failed", "unsupported_form", "redirected_off_host"))
        self.assertEqual(browser.posts(), [])

    async def test_a_redirect_while_the_form_renders_fills_nothing(self):
        # The host is checked again after the wait for the form, before the fill.
        off_host = "https://jobs.lever.co/acme/1"
        real_grab = local_formfill.grab_page_fields

        async def grab_then_leave(page):
            fields = await real_grab(page)
            await page.goto(off_host)    # stands in for a script redirect that fires after the load
            return fields
        fill = mock.AsyncMock()
        with mock.patch.object(local_formfill, "grab_page_fields", grab_then_leave), \
                mock.patch.object(local_formfill, "fill_form_fields", fill):
            app_id, browser = await self.apply({GH_URL: _gh(), off_host: _gh(), GH_DONE: THANKS})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                         ("failed", "unsupported_form", "redirected_off_host"))
        fill.assert_not_called()
        self.assertEqual(browser.posts(), [])

    async def test_a_redirect_during_the_fill_is_not_clicked(self):
        # The fence checks the host once more, immediately before the click. Plain
        # http on the row's own ATS host counts as off it too.
        real_fill = local_formfill.fill_form_fields
        for off_host in ("https://jobs.lever.co/acme/1", GH_URL.replace("https://", "http://", 1)):
            with self.subTest(off_host=off_host):
                async def fill_then_leave(page, *args, **kwargs):
                    report = await real_fill(page, *args, **kwargs)
                    await page.goto(off_host)    # the same form, now somewhere the row's ATS check refuses
                    return report
                with mock.patch.object(local_formfill, "fill_form_fields", fill_then_leave):
                    app_id, browser = await self.apply({GH_URL: _gh(), off_host: _gh(), GH_DONE: THANKS})
                row = self.row(app_id)
                self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                                 ("failed", "unsupported_form", "redirected_off_host"))
                self.assertIsNone(row["submit_attempted_at"])
                self.assertEqual(browser.posts(), [])

    async def test_a_greenhouse_url_is_opened_as_rebuilt_from_its_checked_parts(self):
        # The browser gets the checked host, path and query; a fragment is dropped.
        url = GH_URL + "?gh_jid=1#apply"
        app_id, browser = await self.apply({GH_URL + "?gh_jid=1": _gh(), GH_DONE: THANKS}, url=url)
        self.assertEqual(self.row(app_id)["status"], "applied")
        self.assertEqual(browser.requests[0], ("GET", GH_URL + "?gh_jid=1"))

    async def test_a_row_that_moved_before_the_fence_sends_nothing(self):
        app_id = self.add_row()
        claimed = local_apply.claim("greenhouse")
        self.conn.execute("UPDATE job_applications SET status = 'cancelled', skip_reason = 'user_skipped' "
                          "WHERE id = ?", (app_id,))
        browser = _RoutedBrowser(self.real, {GH_URL: _gh(), GH_DONE: THANKS})

        await local_apply.apply_one(browser, claimed)

        row = self.row(app_id)
        self.assertEqual((row["status"], row["submit_attempted_at"]), ("cancelled", None))
        self.assertEqual(browser.posts(), [])

    async def test_an_ambiguous_or_missing_submit_control_fails_as_unsupported_without_a_click(self):
        two_buttons = ('<form><label for="n">Full name *</label><input id="n" name="name" required>'
                       '<button type="submit">Submit application</button>'
                       '<button type="submit">Submit and create profile</button></form>')
        no_button = ('<form><label for="n">Full name *</label><input id="n" name="name" required></form>')
        for name, html, reason in (("ambiguous", two_buttons, "submit_ambiguous"),
                                   ("missing", no_button, "submit_control_not_found")):
            with self.subTest(name):
                app_id, browser = await self.apply({"https://jobs.lever.co/acme/abc/apply": (200, html)},
                                                   url="https://jobs.lever.co/acme/abc", ats="lever")
                row = self.row(app_id)
                self.assertEqual((row["status"], row["skip_reason"], row["error"]), ("failed", "unsupported_form", reason))
                self.assertIsNone(row["submit_attempted_at"])
                self.assertEqual(browser.posts(), [])

    async def test_an_exception_after_the_fence_blocks_as_verify_submit_and_never_requeues(self):
        async def click_then_die(page, fields, ats, mark_attempted):
            mark_attempted()
            raise RuntimeError("page crashed")
        with mock.patch.object(local_formfill, "submit_form", click_then_die):
            app_id, _ = await self.apply({GH_URL: _gh(), GH_DONE: THANKS})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"]), ("blocked_needs_input", "verify_submit"))
        self.assertIn("page crashed", row["error"])

    async def test_a_timeout_after_the_fence_blocks_as_verify_submit(self):
        async def click_then_hang(page, fields, ats, mark_attempted):
            mark_attempted()
            await asyncio.sleep(30)
        with mock.patch.object(local_apply, "APPLY_MAX_ROW_SECONDS", 3), \
                mock.patch.object(local_formfill, "submit_form", click_then_hang):
            app_id, _ = await self.apply({GH_URL: _gh(), GH_DONE: THANKS})
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"], row["error"]),
                         ("blocked_needs_input", "verify_submit", "row_timeout"))

    async def test_403_429_and_a_challenge_page_requeue_the_row_and_report_a_block(self):
        challenge = "<h1>Please verify you are human</h1>"
        for name, page, error in (("403", (403, "<h1>Forbidden</h1>"), "blocked_http_403"),
                                  ("429", (429, "<h1>Slow down</h1>"), "blocked_http_429"),
                                  ("challenge", (200, challenge), "blocked_challenge_page")):
            with self.subTest(name):
                app_id = self.add_row()
                browser = _RoutedBrowser(self.real, {GH_URL: page})
                outcome = await local_apply.apply_one(browser, local_apply.claim("greenhouse"))
                row = self.row(app_id)
                self.assertEqual(outcome["blocked"], True)
                self.assertEqual((row["status"], row["attempt_count"], row["error"], row["skip_reason"]),
                                 ("pending", 0, error, None))
                self.assertEqual(self.listing_active(app_id), 1)
                self.conn.execute("UPDATE job_applications SET status = 'cancelled' WHERE id = ?", (app_id,))

    async def test_a_real_form_with_a_recaptcha_iframe_is_filled_not_blocked(self):
        captcha = '<iframe title="reCAPTCHA" src="about:blank"></iframe><p>access denied is not on this page</p>'
        app_id, _ = await self.apply({GH_URL: _gh(captcha), GH_DONE: THANKS})
        self.assertEqual(self.row(app_id)["status"], "applied")

    async def test_400_stays_no_form_and_a_normal_page_is_not_slow_or_blocked(self):
        app_id = self.add_row()
        browser = _RoutedBrowser(self.real, {GH_URL: (400, "<p>bad request</p>")})
        outcome = await local_apply.apply_one(browser, local_apply.claim("greenhouse"))
        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]), ("failed", "unsupported_form", "no_form"))
        self.assertEqual(outcome, {"blocked": False, "slow": False})

    async def test_cancelled_after_the_fence_blocks_as_verify_submit(self):
        async def click_then_wait(page, fields, ats, mark_attempted):
            mark_attempted()
            await asyncio.sleep(30)
        app_id = self.add_row()
        browser = _RoutedBrowser(self.real, {GH_URL: _gh(), GH_DONE: THANKS})
        with mock.patch.object(local_formfill, "submit_form", click_then_wait):
            task = asyncio.ensure_future(local_apply.apply_one(browser, local_apply.claim("greenhouse")))
            while self.row(app_id)["status"] != "submitting":
                await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"], row["error"], row["attempt_count"]),
                         ("blocked_needs_input", "verify_submit", "stopped_during_submit", 1))

    async def test_fill_check_reports_coverage_and_changes_no_row(self):
        extra = '<label for="why">Why us? *</label><input id="why" name="why" required>'
        app_id = self.add_row()
        before = self.row(app_id)
        routed = _RoutedPlaywright({GH_URL: _gh(extra), GH_DONE: THANKS})

        with mock.patch.object(local_apply, "async_playwright", lambda: routed):
            report = await local_apply.fill_check(app_id)

        self.assertEqual((report["required_total"], report["required_filled"]), (3, 2))
        self.assertEqual(report["missing"], ["Why us? *"])
        self.assertEqual(report["filled"], {"First Name *": "Jane", "Email *": "jane@example.com"})
        self.assertTrue(Path(report["screenshot_path"]).is_file())
        self.assertEqual(self.row(app_id), before)
        self.assertEqual(routed.browsers[0].posts(), [])

    async def test_fill_check_on_a_closed_posting_returns_the_reason(self):
        app_id = self.add_row()
        routed = _RoutedPlaywright({GH_URL: (404, "gone")})
        with mock.patch.object(local_apply, "async_playwright", lambda: routed):
            report = await local_apply.fill_check(app_id)
        self.assertEqual(report, {"error": "http_404", "skip_reason": "listing_closed"})
        self.assertEqual(self.row(app_id)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
