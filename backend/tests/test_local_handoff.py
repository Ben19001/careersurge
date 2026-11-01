"""The captcha, emailed-code and email-link handoffs.

Real Chromium over fixture pages, like `test_local_apply`: `_RoutedBrowser` answers every request from a
dict of `{url: (status, html)}` and aborts the rest, so no request leaves the machine. The headed window
the user would see is the same routed browser launched HEADLESS (this machine has no display);
`_FakeHeaded` records that `open_in_browser` asked for `headless=False`. The test plays the user by
acting on the page the headed window opened (filling the widget's response token, submitting the form,
closing the browser).
"""
import asyncio
import time
import unittest
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import test_local_apply as tla  # noqa: E402 - the store-backed base class and the routed browser
from playwright.async_api import Locator  # noqa: E402

from app.services import local_apply, local_formfill, local_handoff  # noqa: E402

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - environment without playwright
    async_playwright = None

GH_URL, GH_DONE = tla.GH_URL, tla.GH_DONE
CHECK_EMAIL_URL = "https://boards.greenhouse.io/acme/jobs/1/check-email"
CODE_URL = "https://boards.greenhouse.io/acme/jobs/1/code"
OTHER_URL = "https://boards.greenhouse.io/acme/jobs/1/other"

_CHECKBOX = ('<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"'
             ' style="width:300px;height:78px"></iframe>'
             '<textarea name="g-recaptcha-response" style="display:none"></textarea>')
_BADGE = ('<div class="grecaptcha-badge" style="position:fixed;bottom:0;right:0">'
          '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"'
          ' style="width:70px;height:60px"></iframe></div>'
          '<textarea name="g-recaptcha-response" style="display:none"></textarea>')
# The popup an invisible reCAPTCHA shows after the click; the form does not send until it is solved.
_POPUP_ON_CLICK = """<script>document.getElementById('submit_app').addEventListener('click', e => {
  e.preventDefault(); const f = document.createElement('iframe');
  f.src = 'https://www.google.com/recaptcha/api2/bframe?hl=en'; f.style = 'width:400px;height:580px';
  document.body.append(f); });</script>"""
# A code box appears in place after the first click; the second click sends it (the right code is 123456).
_CODE_IN_PLACE = """<script>let step = 0;
document.getElementById('submit_app').addEventListener('click', e => {
  e.preventDefault();
  if (step++ === 0) {
    document.body.insertAdjacentHTML('beforeend',
      '<label for="code">Verification code</label><input id="code" name="code">');
  } else if (document.getElementById('code').value === '123456') {
    location.href = '/acme/jobs/1/confirmation';
  } });</script>"""
_CODE_PAGE = """<form id="application_form" method="post" action="/acme/jobs/1/confirmation">
  <label for="code">Enter the code we emailed you</label><input id="code" name="code">
  <input type="submit" id="submit_app" value="Submit Application"></form>"""


def _form_to(url_path, extra="", script=""):
    """The fillable Greenhouse form, posting to `url_path` instead of the confirmation page."""
    html = tla.GH_FORM.format(extra=extra, script=script).replace("/acme/jobs/1/confirmation", url_path)
    return (200, html)


class _FakeHeaded:
    """Stands in for `async_playwright()` in `local_handoff`: launches the routed browser headless and records what was asked."""

    def __init__(self, pages):
        self.pages, self.browsers, self.launch_kwargs, self.stopped = pages, [], [], False

    def __call__(self):
        return self

    async def start(self):
        self._real = await async_playwright().start()
        outer = self

        class _Chromium:
            async def launch(self, **kwargs):
                outer.launch_kwargs.append(kwargs)
                routed = tla._RoutedBrowser(await outer._real.chromium.launch(headless=True), outer.pages)
                outer.browsers.append(routed)
                return routed
        self.chromium = _Chromium()
        return self

    async def stop(self):
        self.stopped = True
        await self._real.stop()


class _HandoffCase(tla._StoreTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.real = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.real.close)
        self.add_profile()
        for name, value in (("CODE_WAIT_S", 5), ("CAPTCHA_WAIT_S", 6), ("TOKEN_POLL_S", 0.05)):
            patch = mock.patch.object(local_handoff, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    async def apply(self, pages, **row_kwargs):
        """Seed one pending row, claim it and apply it against `pages`; returns (id, routed browser)."""
        app_id = self.add_row(**row_kwargs)
        browser = tla._RoutedBrowser(self.real, pages)
        await local_apply.apply_one(browser, local_apply.claim(row_kwargs.get("ats", "greenhouse")))
        return app_id, browser

    async def until(self, condition, what, seconds=15):
        """Wait for `condition()` to hold; the page and the worker move on their own clocks."""
        deadline = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            await asyncio.sleep(0.05)

    def states(self, app_id):
        row = self.row(app_id)
        return row["status"], row["blocked_reason"], row["error"]

    async def headed_page(self, fake):
        """The page the headed window opened, once the refill has put the form on it."""
        await self.until(lambda: fake.browsers and fake.browsers[0].browser.contexts
                         and fake.browsers[0].browser.contexts[0].pages, "the headed window")
        return fake.browsers[0].browser.contexts[0].pages[0]

    async def open_headed(self, app_id, pages):
        fake = _FakeHeaded(pages)
        patch = mock.patch.object(local_handoff, "async_playwright", fake)
        patch.start()
        self.addCleanup(patch.stop)
        self.assertTrue(await local_handoff.open_in_browser(app_id))
        return fake


class CaptchaTests(_HandoffCase):
    async def test_a_visible_widget_blocks_as_captcha_before_any_click(self):
        app_id, browser = await self.apply({GH_URL: tla._gh(_CHECKBOX), GH_DONE: tla.THANKS})
        row = self.row(app_id)
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "captcha", "recaptcha"))
        self.assertIsNone(row["submit_attempted_at"])
        self.assertEqual(browser.posts(), [])

    async def test_hcaptcha_and_turnstile_widgets_block_too(self):
        for vendor, src in (("hcaptcha", "https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html#frame=checkbox"),
                            ("turnstile", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/x")):
            with self.subTest(vendor=vendor):
                widget = f'<iframe src="{src}" style="width:300px;height:70px"></iframe>'
                app_id, _ = await self.apply({GH_URL: tla._gh(widget), GH_DONE: tla.THANKS})
                self.assertEqual(self.states(app_id), ("blocked_needs_input", "captcha", vendor))

    async def test_an_invisible_badge_with_no_challenge_is_applied(self):
        app_id, _ = await self.apply({GH_URL: tla._gh(_BADGE), GH_DONE: tla.THANKS})
        self.assertEqual(self.row(app_id)["status"], "applied")

    async def test_a_popup_after_the_click_blocks_as_captcha_and_the_user_solves_it_in_the_headed_window(self):
        pages = {GH_URL: tla._gh(_BADGE, _POPUP_ON_CLICK), GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        row = self.row(app_id)
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "captcha", "captcha_challenge"))
        self.assertTrue(row["submit_attempted_at"])

        fake = await self.open_headed(app_id, pages)
        self.assertEqual(self.states(app_id)[0], "in_progress")
        page = await self.headed_page(fake)
        await self.until(lambda: self.row(app_id)["status"] == "submitting", "the click")
        await page.wait_for_selector('iframe[src*="bframe"]')
        self.assertNotEqual(self.row(app_id)["status"], "applied")
        # The user solves the popup, and the form sends itself.
        await page.evaluate("document.getElementById('application_form').submit()")
        await local_handoff._headed[app_id]
        self.assertEqual(self.row(app_id)["status"], "applied")
        self.assertEqual(fake.launch_kwargs, [{"headless": False}])
        self.assertTrue(fake.stopped)
        self.assertEqual(local_handoff._headed, {})

    async def test_a_solved_turnstile_that_stays_on_screen_is_not_a_popup(self):
        widget = ('<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/x"'
                  ' style="width:300px;height:70px"></iframe>'
                  '<textarea name="cf-turnstile-response" style="display:none"></textarea>')
        pages = {GH_URL: tla._gh(widget, tla._SILENT), GH_DONE: tla.THANKS}    # the board answers in place with nothing
        app_id, _ = await self.apply(pages)
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "captcha"))
        fake = await self.open_headed(app_id, pages)
        page = await self.headed_page(fake)
        await page.wait_for_function("document.getElementById('email').value")    # refilled
        await page.evaluate("document.querySelector('textarea[name=cf-turnstile-response]').value = 'token'")
        await local_handoff._headed[app_id]
        # The click went out and nothing proves what happened: the user checks, it is not offered again as a captcha.
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "verify_submit", "submit_outcome_unknown"))

    async def test_a_popup_that_goes_away_with_no_recognised_answer_is_verify_submit_not_captcha(self):
        pages = {GH_URL: tla._gh(_BADGE, _POPUP_ON_CLICK), GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        fake = await self.open_headed(app_id, pages)
        page = await self.headed_page(fake)
        await page.wait_for_selector('iframe[src*="bframe"]')
        # The user solves it and the board answers in place with text nothing here recognises: the form was
        # probably sent, so it must not come back as a captcha card where Open in browser would send it again.
        await page.evaluate("""document.querySelector('iframe[src*="bframe"]').remove();
                               document.body.insertAdjacentHTML('beforeend', '<p>One moment please.</p>')""")
        await local_handoff._headed[app_id]
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "verify_submit", "submit_outcome_unknown"))

    async def test_closing_the_window_after_the_click_is_verify_submit_not_captcha(self):
        # Once Submit has been clicked, a close can follow a form that went out: a captcha card (and a second
        # Open in browser) would send it again. That holds whatever the page was doing when the window closed.
        solved_in_place = ("""document.querySelector('iframe[src*="bframe"]').remove();
                               document.body.insertAdjacentHTML('beforeend', '<p>One moment please.</p>')""")
        solved_to_other_page = "document.getElementById('application_form').submit()"
        for label, act in (("popup untouched", None), ("answers in place", solved_in_place),
                           ("moves to an unmatched page", solved_to_other_page)):
            with self.subTest(label):
                pages = {GH_URL: _form_to("/acme/jobs/1/other", _BADGE, _POPUP_ON_CLICK),
                         OTHER_URL: (200, "<p>Hello</p>"), GH_DONE: tla.THANKS}
                app_id, _ = await self.apply(pages)
                fake = await self.open_headed(app_id, pages)
                page = await self.headed_page(fake)
                await page.wait_for_selector('iframe[src*="bframe"]')
                if act:
                    await page.evaluate(act)
                    await asyncio.sleep(0.3)
                task = local_handoff._headed[app_id]
                await fake.browsers[0].browser.close()
                await task
                self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "verify_submit"))
                self.conn.execute("UPDATE job_applications SET status = 'cancelled', blocked_reason = NULL WHERE id = ?",
                                  (app_id,))

    async def test_a_popup_left_untouched_until_the_wait_ends_is_verify_submit(self):
        pages = {GH_URL: tla._gh(_BADGE, _POPUP_ON_CLICK), GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        with mock.patch.object(local_handoff, "CAPTCHA_WAIT_S", 0.8):
            await self.open_headed(app_id, pages)
            await local_handoff._headed[app_id]
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "verify_submit"))

    async def test_no_free_slot_sends_the_row_back_to_the_tray_with_a_reason(self):
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="captcha")
        for _ in range(local_handoff.MAX_CONTEXTS):
            await local_handoff.acquire_context_slot()
        fake = _FakeHeaded({GH_URL: tla._gh(_CHECKBOX)})
        try:
            with mock.patch.object(local_handoff, "async_playwright", fake), \
                    mock.patch.object(local_handoff, "SLOT_WAIT_S", 0.2):
                self.assertTrue(await local_handoff.open_in_browser(app_id))
                await local_handoff._headed[app_id]
        finally:
            for _ in range(local_handoff.MAX_CONTEXTS):
                local_handoff.release_context_slot()
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "captcha", "no_free_slot"))
        self.assertEqual(fake.launch_kwargs, [])

    async def test_applied_clears_the_otp_timeout_left_by_an_earlier_try(self):
        app_id, _ = await self.apply({GH_URL: tla._gh(), GH_DONE: tla.THANKS}, error="otp_timeout")
        self.assertEqual(self.states(app_id), ("applied", None, None))

    async def test_a_code_step_after_the_popup_parks_the_headed_window_and_closes_it_with_the_code(self):
        pages = {GH_URL: _form_to("/acme/jobs/1/code", _BADGE, _POPUP_ON_CLICK), CODE_URL: (200, _CODE_PAGE),
                 GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        fake = await self.open_headed(app_id, pages)
        page = await self.headed_page(fake)
        await page.wait_for_selector('iframe[src*="bframe"]')
        await page.evaluate("document.getElementById('application_form').submit()")
        await self.until(lambda: local_handoff.is_parked(app_id), "the code step")
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "otp"))
        self.assertFalse(fake.stopped)     # the window stays open for the code
        task = local_handoff._parked[app_id]["task"]
        self.assertTrue(local_handoff.submit_code(app_id, "123456"))
        await task
        self.assertEqual(self.row(app_id)["status"], "applied")
        self.assertTrue(fake.stopped)

    async def test_a_visible_widget_waits_for_its_token_then_clicks(self):
        pages = {GH_URL: tla._gh(_CHECKBOX), GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        fake = await self.open_headed(app_id, pages)
        page = await self.headed_page(fake)
        await page.wait_for_function("document.getElementById('email').value")    # refilled
        await asyncio.sleep(0.5)
        # Unsolved: no click and no fence.
        self.assertEqual(self.states(app_id)[0], "in_progress")
        self.assertEqual(fake.browsers[0].posts(), [])
        await page.evaluate("document.querySelector('textarea[name=g-recaptcha-response]').value = 'token'")
        await local_handoff._headed[app_id]
        self.assertEqual(self.row(app_id)["status"], "applied")

    async def test_closing_the_window_before_the_click_returns_the_row_to_the_tray(self):
        pages = {GH_URL: tla._gh(_CHECKBOX), GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        fake = await self.open_headed(app_id, pages)
        page = await self.headed_page(fake)
        await page.wait_for_function("document.getElementById('email').value")    # refilled
        await asyncio.sleep(0.2)
        task = local_handoff._headed[app_id]
        await fake.browsers[0].browser.close()
        await task
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "captcha"))
        self.assertIsNone(self.row(app_id)["submit_attempted_at"])

    async def test_a_window_nobody_touches_returns_to_the_tray_after_the_wait(self):
        pages = {GH_URL: tla._gh(_CHECKBOX), GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        with mock.patch.object(local_handoff, "CAPTCHA_WAIT_S", 0.4):
            await self.open_headed(app_id, pages)
            await local_handoff._headed[app_id]
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "captcha"))

    async def test_a_headed_window_that_left_the_ats_host_is_not_clicked(self):
        # The headed fence checks the host too, immediately before the click.
        app_id, _ = await self.apply({GH_URL: tla._gh(_CHECKBOX), GH_DONE: tla.THANKS})
        off_host = "https://jobs.lever.co/acme/1"
        real_fill = local_formfill.fill_form_fields

        async def fill_then_leave(page, *args, **kwargs):
            report = await real_fill(page, *args, **kwargs)
            await page.goto(off_host)    # the same form, now on a host outside the row's ATS
            return report
        with mock.patch.object(local_formfill, "fill_form_fields", fill_then_leave):
            fake = await self.open_headed(app_id, {GH_URL: tla._gh(), off_host: tla._gh(), GH_DONE: tla.THANKS})
            await local_handoff._headed[app_id]
        row = self.row(app_id)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                         ("failed", "unsupported_form", "redirected_off_host"))
        self.assertIsNone(row["submit_attempted_at"])
        self.assertEqual(fake.browsers[0].posts(), [])

    async def test_open_in_browser_only_claims_a_captcha_row_once(self):
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="captcha")
        other = self.add_row(status="blocked_needs_input", blocked_reason="question")
        self.assertFalse(await local_handoff.open_in_browser(other))
        self.assertFalse(await local_handoff.open_in_browser(999))
        fake = _FakeHeaded({GH_URL: tla._gh(_CHECKBOX)})
        with mock.patch.object(local_handoff, "async_playwright", fake), \
                mock.patch.object(local_handoff, "CAPTCHA_WAIT_S", 0.3):
            self.assertTrue(await local_handoff.open_in_browser(app_id))
            self.assertFalse(await local_handoff.open_in_browser(app_id))
            await local_handoff._headed[app_id]
        self.assertEqual(len(fake.launch_kwargs), 1)


class CodeTests(_HandoffCase):
    async def finish_parked(self, app_id):
        await local_handoff._parked[app_id]["task"]

    async def test_a_code_box_after_the_click_parks_the_context_and_the_code_finishes_it(self):
        cases = (("in place", {GH_URL: tla._gh(script=_CODE_IN_PLACE), GH_DONE: tla.THANKS}),
                 ("after a URL change", {GH_URL: _form_to("/acme/jobs/1/code"), CODE_URL: (200, _CODE_PAGE),
                                         GH_DONE: tla.THANKS}))
        for label, pages in cases:
            with self.subTest(label):
                app_id, browser = await self.apply(pages)
                row = self.row(app_id)
                self.assertEqual((row["status"], row["blocked_reason"]), ("blocked_needs_input", "otp"))
                self.assertIsNone(row["confirmed_at"])
                self.assertTrue(local_handoff.is_parked(app_id))
                self.assertEqual(local_handoff.parked_count(), 1)
                self.assertNotIn(GH_DONE, [url for _, url in browser.requests])
                task = local_handoff._parked[app_id]["task"]

                self.assertTrue(local_handoff.submit_code(app_id, "123456"))
                self.assertFalse(local_handoff.submit_code(app_id, "again"))    # already sent
                await task
                row = self.row(app_id)
                self.assertEqual((row["status"], row["blocked_reason"]), ("applied", None))
                self.assertTrue(row["confirmed_at"])
                self.assertEqual(local_handoff.parked_count(), 0)

    async def test_a_parked_page_that_left_the_ats_host_gets_no_code(self):
        # The code waits for minutes, so the host is checked again before it is typed.
        off_host = "https://jobs.lever.co/acme/code"
        pages = {GH_URL: _form_to("/acme/jobs/1/code"), CODE_URL: (200, _CODE_PAGE), off_host: (200, _CODE_PAGE),
                 GH_DONE: tla.THANKS}
        app_id, browser = await self.apply(pages)
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "otp"))
        page = browser.browser.contexts[0].pages[0]
        await page.goto(off_host)
        task = local_handoff._parked[app_id]["task"]
        typed = []
        real_fill = Locator.fill

        async def record_fill(locator, value, **kwargs):
            typed.append(value)    # a script on the page could read a typed code without ever POSTing it
            return await real_fill(locator, value, **kwargs)
        with mock.patch.object(Locator, "fill", record_fill):
            self.assertTrue(local_handoff.submit_code(app_id, "123456"))
            await task
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "verify_submit", "redirected_off_host"))
        self.assertEqual(typed, [])
        self.assertIsNotNone(self.row(app_id)["screenshot_path"])
        self.assertNotIn(off_host, browser.posts())
        self.assertNotIn(GH_DONE, [url for _, url in browser.requests])

    async def test_no_code_closes_the_context_and_requeues_once_then_fails(self):
        pages = {GH_URL: tla._gh(script=_CODE_IN_PLACE), GH_DONE: tla.THANKS}
        with mock.patch.object(local_handoff, "CODE_WAIT_S", 0.3):
            app_id, _ = await self.apply(pages)
            await self.finish_parked(app_id)
            row = self.row(app_id)
            self.assertEqual((row["status"], row["error"], row["attempt_count"]), ("pending", "otp_timeout", 0))
            self.assertEqual(local_handoff.parked_count(), 0)

            browser = tla._RoutedBrowser(self.real, pages)
            await local_apply.apply_one(browser, local_apply.claim("greenhouse"))
            await self.finish_parked(app_id)
            row = self.row(app_id)
            self.assertEqual((row["status"], row["skip_reason"], row["error"]), ("failed", "timeout", "otp_timeout"))

    async def test_a_wrong_code_ends_in_verify_submit_not_applied(self):
        pages = {GH_URL: tla._gh(script=_CODE_IN_PLACE), GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        local_handoff.submit_code(app_id, "000000")
        await self.finish_parked(app_id)
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "verify_submit", "submit_outcome_unknown"))

    async def test_a_code_step_on_the_confirmation_url_does_not_count_a_wrong_code_as_applied(self):
        stuck = ('<form id="application_form" onsubmit="return false">'
                 '<label for="code">Enter the code we emailed you</label><input id="code">'
                 '<input type="submit" id="submit_app" value="Submit Application"></form>')
        app_id, _ = await self.apply({GH_URL: tla._gh(), GH_DONE: (200, stuck)})
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "otp"))
        local_handoff.submit_code(app_id, "000000")
        await self.finish_parked(app_id)
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "verify_submit"))
        self.assertIsNone(self.row(app_id)["confirmed_at"])

    async def test_a_second_code_prompt_after_the_code_is_verify_submit(self):
        again = _CODE_IN_PLACE.replace(
            "location.href = '/acme/jobs/1/confirmation';",
            "document.body.insertAdjacentHTML('beforeend', '<label>One-time passcode <input id=\"c2\"></label>');")
        app_id, _ = await self.apply({GH_URL: tla._gh(script=again), GH_DONE: tla.THANKS})
        local_handoff.submit_code(app_id, "123456")
        await self.finish_parked(app_id)
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "verify_submit", "second_code_prompt"))

    async def test_a_restart_drops_the_parked_wait_and_requeues_the_row(self):
        app_id, _ = await self.apply({GH_URL: tla._gh(script=_CODE_IN_PLACE), GH_DONE: tla.THANKS})
        task = local_handoff._parked[app_id]["task"]
        local_apply.recover()
        self.assertEqual(self.row(app_id)["status"], "pending")
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(local_handoff.parked_count(), 0)
        self.assertFalse(local_handoff.submit_code(app_id, "123456"))

    async def test_stop_requeues_a_waiting_context_and_leaves_none_open(self):
        app_id, _ = await self.apply({GH_URL: tla._gh(script=_CODE_IN_PLACE), GH_DONE: tla.THANKS})
        await local_handoff.stop()
        row = self.row(app_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("pending", 0))
        self.assertEqual(local_handoff.parked_count(), 0)

    async def test_nothing_waiting_means_no_code_is_taken(self):
        self.assertFalse(local_handoff.submit_code(9, "123456"))

    async def test_a_parked_context_keeps_its_slot_until_it_closes(self):
        with mock.patch.object(local_handoff, "MAX_CONTEXTS", 2):
            self.assertTrue(await local_handoff.acquire_context_slot(0.1))     # the queue's slot for the row that parks
            pages = {GH_URL: tla._gh(script=_CODE_IN_PLACE), GH_DONE: tla.THANKS}
            app_id, _ = await self.apply(pages)                                # parks, and now owns that slot
            self.assertTrue(await local_handoff.acquire_context_slot(0.1))     # another queue's job
            self.assertFalse(await local_handoff.acquire_context_slot(0.2))    # the next job waits for a slot
            local_handoff.submit_code(app_id, "123456")
            await self.finish_parked(app_id)
            self.assertTrue(await local_handoff.acquire_context_slot(0.2))


class OutcomeTests(_HandoffCase):
    async def test_an_email_link_page_blocks_as_verify_submit_and_done_applies_it(self):
        check = (200, "<h1>Check your email</h1><p>We sent you a confirmation link.</p>")
        app_id, _ = await self.apply({GH_URL: _form_to("/acme/jobs/1/check-email"), CHECK_EMAIL_URL: check})
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "verify_submit", "email_link"))
        from app.services import local_answers
        self.assertTrue(local_answers.resolve_verify_submit(app_id, "confirmed"))
        self.assertEqual(self.row(app_id)["status"], "applied")

    async def test_a_confirmation_page_that_also_asks_for_the_mailed_link_is_not_applied(self):
        both = (200, "<h1>Thank you for applying</h1><p>Check your inbox to confirm.</p>")
        app_id, _ = await self.apply({GH_URL: tla._gh(), GH_DONE: both})
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "verify_submit"))

    async def test_an_unrecognized_page_after_the_click_is_verify_submit(self):
        other = (200, "<h1>Welcome back</h1>")
        app_id, _ = await self.apply({GH_URL: _form_to("/acme/jobs/1/other"), OTHER_URL: other})
        self.assertEqual(self.states(app_id), ("blocked_needs_input", "verify_submit", "unrecognized_page_after_submit"))

    async def test_a_url_change_to_a_lever_page_with_thanks_text_is_applied_but_a_bare_one_is_not(self):
        form = ('<form method="post" action="/acme/abc/done"><label for="n">Full name *</label>'
                '<input id="n" name="name" required><button type="submit" data-qa="btn-submit">Submit</button></form>')
        url = "https://jobs.lever.co/acme/abc/apply"
        for body, status in (("<h1>Thanks for applying</h1>", "applied"), ("<h1>Hi</h1>", "blocked_needs_input")):
            with self.subTest(status=status):
                app_id, _ = await self.apply({url: (200, form), "https://jobs.lever.co/acme/abc/done": (200, body)},
                                             url="https://jobs.lever.co/acme/abc", ats="lever")
                self.assertEqual(self.row(app_id)["status"], status)

    async def test_the_confirmation_url_alone_proves_a_greenhouse_application(self):
        app_id, _ = await self.apply({GH_URL: tla._gh(), GH_DONE: (200, "<h1>Done</h1>")})
        self.assertEqual(self.row(app_id)["status"], "applied")

    async def test_an_old_acknowledgement_already_on_the_page_is_not_a_new_one(self):
        extra = "<p>Once we have received your application we will email you.</p>"
        app_id, _ = await self.apply({GH_URL: tla._gh(extra, tla._SILENT)})
        self.assertEqual(self.states(app_id)[:2], ("blocked_needs_input", "verify_submit"))


class DetectionTests(_HandoffCase):
    async def test_a_code_field_the_form_already_had_is_not_a_new_prompt(self):
        page = await self.real.new_page()
        self.addAsyncCleanup(page.close)
        await page.set_content('<label for="c">Security code</label><input id="c" data-cs-field="1">'
                               '<label for="d">Phone</label><input id="d">')
        self.assertFalse(await local_formfill.code_prompt(page))
        await page.set_content('<label for="c">Security code</label><input id="c">')
        self.assertTrue(await local_formfill.code_prompt(page))
        self.assertFalse(await local_formfill.code_prompt(page))    # reported once; it is the same prompt
        await page.set_content('<input placeholder="Enter the code we sent" type="hidden"><input aria-label="Your one-time code">')
        self.assertTrue(await local_formfill.code_prompt(page))

    async def test_only_a_visible_challenge_frame_counts(self):
        page = await self.real.new_page()
        self.addAsyncCleanup(page.close)
        await page.set_content('<iframe src="about:blank" title="recaptcha challenge expires in two minutes"'
                               ' style="display:none"></iframe>')
        self.assertFalse(await local_formfill.challenge_shown(page))
        await page.set_content('<iframe src="about:blank" title="recaptcha challenge expires in two minutes"'
                               ' style="width:300px;height:300px"></iframe>')
        self.assertTrue(await local_formfill.challenge_shown(page))

    async def test_the_invisible_badge_is_not_a_widget(self):
        page = await self.real.new_page()
        self.addAsyncCleanup(page.close)
        await page.set_content(_BADGE)
        self.assertIsNone(await local_formfill.captcha_widget(page))
        await page.set_content(_CHECKBOX)
        self.assertEqual(await local_formfill.captcha_widget(page), "recaptcha")


class ManualApplyTests(tla._StoreTestCase):
    """A captcha card handed to the user's own browser."""

    def test_a_captcha_row_becomes_a_manual_apply_card_and_keeps_its_screenshot(self):
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="captcha", error="recaptcha",
                              screenshot_path="shot.png")
        self.assertEqual(local_handoff.apply_manually(app_id), GH_URL)
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"], row["error"], row["screenshot_path"]),
                         ("blocked_needs_input", "verify_submit", "manual_apply", "shot.png"))

    def test_any_other_state_and_a_second_call_return_none_and_change_nothing(self):
        question = self.add_row(status="blocked_needs_input", blocked_reason="question")
        pending = self.add_row(status="pending")
        captcha = self.add_row(status="blocked_needs_input", blocked_reason="captcha")
        self.assertIsNotNone(local_handoff.apply_manually(captcha))
        for app_id in (question, pending, captcha, 999):
            with self.subTest(app_id=app_id):
                self.assertIsNone(local_handoff.apply_manually(app_id))
        self.assertEqual(self.states(question), ("blocked_needs_input", "question", None))
        self.assertEqual(self.states(pending), ("pending", None, None))
        self.assertEqual(self.states(captcha), ("blocked_needs_input", "verify_submit", "manual_apply"))

    def test_a_url_outside_the_rows_ats_is_refused_before_anything_is_written(self):
        for url in ("https://evil.example/acme/jobs/1", "http://boards.greenhouse.io/acme/jobs/1",
                    "https://jobs.lever.co/acme/1", "https://evil.example\\@boards.greenhouse.io/acme/jobs/1",
                    "https://boards.greenhouse.io@evil.example/", "https://boards.greenhouse.io:443@evil.example/",
                    "https://boards.greenhouse.io:443/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/1 x"):
            with self.subTest(url=url):
                app_id = self.add_row(url=url, status="blocked_needs_input", blocked_reason="captcha")
                with self.assertRaises(ValueError):
                    local_handoff.apply_manually(app_id)
                self.assertEqual(self.states(app_id), ("blocked_needs_input", "captcha", None))

    def states(self, app_id):
        row = self.row(app_id)
        return row["status"], row["blocked_reason"], row["error"]


if __name__ == "__main__":
    unittest.main()
