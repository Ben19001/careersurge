"""End to end: the built UI (`frontend/dist`) driven by headless Chromium against the real launcher.

`local_main.build_app` and `start_server` run in this process on a real 127.0.0.1 port, with the real
queues and apply worker. Two things are replaced so nothing leaves the machine: the shared Chromium the
queues apply with answers every ATS request from memory (`test_local_apply._RoutedBrowser`), and the
queue cooldown is zero. The UI is opened the way pywebview opens it, with the token in the fragment.
Skipped when `frontend/dist` has not been built (`cd frontend && npm run build`).
"""
import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import test_local_apply as tla  # noqa: E402 - the store-backed base class and routed-browser helpers
import test_local_handoff as tlh  # noqa: E402 - the captcha, code and email-link fixture pages

from app.store import db  # noqa: E402

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
TOKEN = "e2e-token"
# A 1x1 PNG: what the worker's screenshot would be.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=")

JOB_ONE = "https://boards.greenhouse.io/acme/jobs/1"
JOB_TWO = "https://boards.greenhouse.io/acme/jobs/2"
QUESTION = '<label for="why">Why us? *</label><textarea id="why" name="why" required></textarea>'
HANG = object()

_STUBBED = {}
local_main = local_queues = local_apply = local_handoff = None


def setUpModule():
    """Real FastAPI and pydantic for the launcher, which `conftest` stubs suite-wide (see test_local_api)."""
    global local_main, local_queues, local_apply, local_handoff
    if not (DIST / "index.html").exists():
        raise unittest.SkipTest("frontend/dist is not built: cd frontend && npm run build")
    for name in ("httpx", "pydantic"):
        if name in sys.modules:
            _STUBBED[name] = sys.modules.pop(name)
    try:
        from app import local_main as launcher
        from app.services import local_apply as apply_module, local_handoff as handoff, local_queues as queues
    except ImportError as exc:
        sys.modules.update(_STUBBED)
        raise unittest.SkipTest(f"the launcher's dependencies are not installed: {exc}")
    local_main, local_queues, local_apply, local_handoff = launcher, queues, apply_module, handoff


def tearDownModule():
    sys.modules.update(_STUBBED)


def form_for(number, extra=""):
    """The Greenhouse form of `tla`, posting to job `number`'s own confirmation page."""
    return (200, tla.GH_FORM.format(extra=extra, script="").replace("/acme/jobs/1/", f"/acme/jobs/{number}/"))


class _HangingBrowser(tla._RoutedBrowser):
    """A routed browser where a page mapped to `HANG` never answers, so its application stays in_progress."""

    async def new_context(self):
        context = await self.browser.new_context()

        async def handle(route):
            request = route.request
            self.requests.append((request.method, request.url))
            hit = self.pages.get(request.url)
            if hit is HANG:
                await asyncio.sleep(120)
            elif hit is None:
                await route.abort()
            else:
                await route.fulfill(status=hit[0], body=hit[1], content_type="text/html")
        await context.route("**/*", handle)
        return context


@unittest.skipUnless(tla._HAVE_BROWSER, "playwright not installed")
class UiE2E(tla._StoreTestCase):
    async def asyncSetUp(self):
        self.playwright = await tla.async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.chromium = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.chromium.close)
        self.add_profile(phone="555-0100", current_location="Austin, TX")
        db.set_setting("sourcing_filters", {"titles": ["software engineer"]})    # without a title the app opens onboarding
        self.pages = {}

    def start_app(self, pages):
        """Serve the built UI with the real queues, applying against `pages`; returns the window URL."""
        self.pages.update(pages)
        browser_class = _HangingBrowser
        queue_browser = {}

        async def get(shared):
            if shared.browser is None:
                shared.playwright = await tla.async_playwright().start()
                real = await shared.playwright.chromium.launch(headless=True)
                queue_browser["real"] = browser_class(real, self.pages)
                shared.browser = queue_browser["real"]
            return shared.browser

        for patch in (mock.patch.object(local_queues._SharedBrowser, "get", get),
                      mock.patch.object(local_queues, "POLL_S", 0.2),
                      mock.patch.object(local_queues, "COOLDOWN_SPREAD_S", 0),
                      mock.patch.object(local_queues, "MIN_COOLDOWN_FLOOR_S", 0),
                      mock.patch.object(local_queues, "DEFAULT_COOLDOWN_FLOOR_S", 0)):
            patch.start()
            self.addCleanup(patch.stop)
        db.set_setting("sourcing_interval", "manual")

        app = local_main.build_app(TOKEN, DIST)
        server, thread, port = local_main.start_server(app)
        self.app = app

        def stop():
            server.should_exit = True
            thread.join(30)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{port}/#token={TOKEN}"

    async def open_ui(self, url, width=1280):
        page = await self.chromium.new_page(viewport={"width": width, "height": 900})
        self.addAsyncCleanup(page.close)
        await page.goto(url)
        return page

    async def wait_for_status(self, app_id, status, seconds=40):
        for _ in range(int(seconds / 0.2)):
            if self.row(app_id)["status"] == status:
                return
            await asyncio.sleep(0.2)
        self.fail(f"application {app_id} is {self.row(app_id)['status']!r}, never {status!r}")

    def add_job(self, url, title, **columns):
        app_id = self.add_row(url=url, status="prepared", **columns)
        self.conn.execute("UPDATE listings SET title = ?, company = 'Acme', location = 'Austin, TX' "
                          "WHERE id = (SELECT listing_id FROM job_applications WHERE id = ?)", (title, app_id))
        return app_id

    async def test_approve_screen_flags_reposts_and_skips_the_unticked(self):
        # The earlier listing is closed: launch now prepares every active listing that has no application.
        earlier = self.conn.execute(
            "INSERT INTO listings (source, external_id, url, title, company, company_key, first_seen_at, is_active) "
            "VALUES ('greenhouse', 'old', ?, 'Platform Engineer', 'Acme', 'acme', '2026-09-01T00:00:00.000Z', 0)",
            (JOB_ONE,)).lastrowid
        plain = self.add_job(JOB_ONE, "Backend Engineer")
        strong = self.add_job(JOB_TWO, "Platform Engineer")
        self.conn.execute("UPDATE job_applications SET repost_match = 'strong', repost_of_listing_id = ? WHERE id = ?",
                          (earlier, strong))
        weak = self.add_job("https://boards.greenhouse.io/acme/jobs/3", "Data Engineer")
        self.conn.execute("UPDATE job_applications SET repost_match = 'weak', repost_of_listing_id = ? WHERE id = ?",
                          (earlier, weak))
        page = await self.open_ui(self.start_app({}))

        rows = page.get_by_test_id("job-row")
        await rows.first.wait_for()
        self.assertEqual(await rows.count(), 3)
        self.assertFalse(await page.get_by_role("checkbox", name="Include Platform Engineer at Acme").is_checked())
        self.assertTrue(await page.get_by_role("checkbox", name="Include Backend Engineer at Acme").is_checked())
        self.assertTrue(await page.get_by_role("checkbox", name="Include Data Engineer at Acme").is_checked())
        self.assertIn("Same posting as Platform Engineer", await page.get_by_test_id("strong-repost").inner_text())
        self.assertIn("Possible repost", await page.get_by_test_id("weak-repost").inner_text())
        self.assertIn("Jane Doe", await page.get_by_test_id("what-will-be-sent").inner_text())
        self.assertIn("About", await page.get_by_test_id("eta").inner_text())
        # The token left the address bar and the page still works after a reload.
        self.assertNotIn("token", page.url)
        await page.reload()
        await page.get_by_test_id("job-row").first.wait_for()

        await page.get_by_role("button", name="Skip unticked (1)").click()
        await page.get_by_test_id("approve-notice").wait_for()
        self.assertEqual(self.row(strong)["skip_reason"], "user_skipped")
        self.assertEqual({self.row(plain)["status"], self.row(weak)["status"]}, {"prepared"})

    async def test_approve_summary_shows_the_consent_flags_set_in_profile_and_links_back_to_it(self):
        self.add_job(JOB_ONE, "Backend Engineer")
        page = await self.open_ui(self.start_app({}))
        summary = page.get_by_test_id("what-will-be-sent")
        await summary.wait_for()
        self.assertIn("✗ Agree to routine consent questions", await summary.inner_text())    # all three start off

        await summary.get_by_role("link", name="Change in Profile").click()
        await page.get_by_test_id("consent-general").click()
        await page.get_by_test_id("consent-arbitration").click()
        await page.get_by_role("button", name="Save permissions").click()
        await page.get_by_test_id("consent-saved").wait_for()
        await page.get_by_role("link", name="Approve", exact=True).click()

        summary = page.get_by_test_id("what-will-be-sent")
        await summary.wait_for()
        text = await summary.inner_text()
        self.assertIn("✓ Agree to routine consent questions", text)
        self.assertIn("✗ Agree to background checks", text)
        self.assertIn("✓ Agree to arbitration clauses", text)

    async def test_approve_then_a_question_blocks_is_answered_in_needs_you_and_the_job_is_filled(self):
        ok = self.add_job(JOB_ONE, "Backend Engineer")
        asks = self.add_job(JOB_TWO, "Platform Engineer")
        page = await self.open_ui(self.start_app({
            JOB_ONE: form_for(1), "https://boards.greenhouse.io/acme/jobs/1/confirmation": tla.THANKS,
            JOB_TWO: form_for(2, QUESTION), "https://boards.greenhouse.io/acme/jobs/2/confirmation": tla.THANKS}))

        await page.get_by_role("button", name="Approve 2 jobs").click()
        await page.get_by_test_id("approve-notice").wait_for()
        await self.wait_for_status(ok, "applied")
        await self.wait_for_status(asks, "blocked_needs_input")

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-question")
        await card.wait_for()
        self.assertTrue(await card.get_by_role("radio", name="This job only").is_checked())
        await card.get_by_label("Why us?").fill("Great mission")
        await card.get_by_role("button", name="Save answer").click()

        await self.wait_for_status(asks, "applied")
        self.assertIn("Nothing needs you", await page.get_by_role("alert").inner_text())
        await page.get_by_role("link", name="Progress").click()
        await page.get_by_test_id("count-applied").wait_for()
        self.assertEqual(await page.get_by_test_id("count-applied").inner_text(), "2")

    def seed_question_card(self, question_type="open_ended", summaries=True, scope="job"):
        """A blocked job with one unanswered `job` question; returns (application id, qna id)."""
        app_id = self.add_row(url=JOB_ONE, status="blocked_needs_input", blocked_reason="question")
        listing_id = self.row(app_id)["listing_id"]
        self.conn.execute("UPDATE listings SET title = 'Backend Engineer', company = 'Acme', location = 'Austin, TX' "
                          "WHERE id = ?", (listing_id,))
        if summaries:
            self.conn.execute("UPDATE listings SET responsibilities_summary = 'Build the API', "
                              "requirements_summary = '5 years Python', key_skills = '[\"Python\", \"SQL\"]' WHERE id = ?",
                              (listing_id,))
        qna_id = self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, listing_id) "
            "VALUES ('Why us?', 'why us', ?, 'job', ?)", (question_type, listing_id)).lastrowid
        if scope != "job":
            self.conn.execute("UPDATE application_qna_bank SET scope = ?, listing_id = NULL WHERE id = ?", (scope, qna_id))
        self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id) VALUES (?, ?)",
                          (app_id, qna_id))
        return app_id, qna_id

    async def test_a_question_card_shows_the_job_and_every_job_moves_it_off_the_tray(self):
        app_id, qna_id = self.seed_question_card()
        page = await self.open_ui(self.start_app({}), width=420)

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-question")
        await card.wait_for()
        self.assertEqual(await card.get_by_test_id("job-meta").inner_text(), "Austin, TX · Greenhouse · View posting")
        # A button, not a link: pywebview cannot open a link in the system browser.
        self.assertEqual(await card.get_by_role("button", name="View posting").count(), 1)
        self.assertEqual(await card.get_by_role("link", name="View posting").count(), 0)
        about = card.get_by_test_id("about-job")
        self.assertFalse(await about.evaluate("el => el.open"))           # collapsed by default
        await about.get_by_text("About this job").click()
        text = await about.inner_text()
        for expected in ("Responsibilities", "Build the API", "Requirements", "5 years Python", "Key skills"):
            self.assertIn(expected, text)
        for skill in ("Python", "SQL"):                                   # one chip per skill
            self.assertEqual(await about.get_by_text(skill, exact=True).count(), 1)

        self.assertTrue(await card.get_by_role("radio", name="This job only").is_checked())
        await card.get_by_role("radio", name="Every job").click()
        await card.get_by_label("Why us?").fill("Great mission")
        await card.get_by_role("button", name="Save answer").click()

        await page.get_by_text("Nothing needs you").wait_for()
        self.assertEqual(self.row(app_id)["status"], "pending")
        saved = self.conn.execute("SELECT scope, listing_id, answer_value FROM application_qna_bank WHERE id = ?",
                                  (qna_id,)).fetchone()
        self.assertEqual(tuple(saved), ("global", None, "Great mission"))

    async def test_an_attestation_card_has_no_picker_and_a_card_without_summaries_or_a_web_link_has_neither(self):
        self.seed_question_card(question_type="attestation", summaries=False)
        self.conn.execute("UPDATE listings SET url = 'javascript:alert(1)'")
        page = await self.open_ui(self.start_app({}))

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-question")
        await card.wait_for()
        self.assertEqual(await card.get_by_role("radio").count(), 0)
        self.assertIn("Used for this job only", await card.inner_text())
        self.assertEqual(await card.get_by_test_id("about-job").count(), 0)
        self.assertEqual(await card.get_by_role("link", name="View posting").count(), 0)

    async def test_a_single_choice_with_one_option_is_a_checkbox_not_a_lone_radio(self):
        # Seen in a real run: a one-option card drew one radio with nothing to choose between.
        app_id, qna_id = self.seed_question_card(question_type="attestation")
        self.conn.execute("UPDATE application_blocking_questions SET field_shape = ?",
                          ('{"kind": "single", "options": ["I have read the privacy notice"]}',))
        page = await self.open_ui(self.start_app({}))

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-question")
        await card.wait_for()
        self.assertEqual(await card.get_by_role("radio").count(), 0)
        await card.get_by_role("checkbox", name="I have read the privacy notice").click()
        await card.get_by_role("button", name="Save answer").click()

        await page.get_by_text("Nothing needs you").wait_for()
        self.assertEqual(self.row(app_id)["status"], "pending")
        saved = self.conn.execute("SELECT answer_value FROM application_qna_bank WHERE id = ?", (qna_id,)).fetchone()
        self.assertEqual(saved[0], "I have read the privacy notice")

    async def test_a_wide_attestation_card_says_this_job_only_and_is_saved_that_way(self):
        app_id, qna_id = self.seed_question_card(question_type="attestation", scope="global")
        page = await self.open_ui(self.start_app({}))

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-question")
        await card.wait_for()
        self.assertIn("Used for this job only", await card.inner_text())
        await card.get_by_label("Why us?").fill("Yes")
        await card.get_by_role("button", name="Save answer").click()

        await page.get_by_text("Nothing needs you").wait_for()
        saved = self.conn.execute("SELECT scope, listing_id FROM application_qna_bank WHERE id = ?", (qna_id,)).fetchone()
        self.assertEqual(tuple(saved), ("job", self.row(app_id)["listing_id"]))

    async def test_a_bad_answer_shows_its_message_inline(self):
        asks = self.add_job(JOB_ONE, "Backend Engineer")
        page = await self.open_ui(self.start_app({
            JOB_ONE: form_for(1, '<label for="note">Anything else? *</label><input id="note" name="note" '
                                 'maxlength="5" required>')}))
        await page.get_by_role("button", name="Approve 1 job").click()
        await self.wait_for_status(asks, "blocked_needs_input")
        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-question")
        await card.get_by_role("button", name="Save answer").click()

        self.assertIn("cannot be empty", await card.get_by_test_id("answer-problem").inner_text())
        self.assertEqual(self.row(asks)["status"], "blocked_needs_input")

    async def test_verify_submit_card_shows_the_last_page_and_a_confirmation_closes_it(self):
        url = self.start_app({})
        screenshots = db.data_dir() / "screenshots"
        screenshots.mkdir(exist_ok=True)
        shot = screenshots / "1-1.png"
        shot.write_bytes(PNG)
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="verify_submit", screenshot_path=str(shot))
        page = await self.open_ui(url, width=420)

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-verify_submit")
        await card.wait_for()
        self.assertIn("Not sure", await card.inner_text())
        await card.get_by_role("button", name="View last page").click()
        image = page.get_by_test_id("last-page")
        await image.wait_for()
        self.assertGreater(await image.evaluate("img => img.naturalWidth"), 0)
        await page.keyboard.press("Escape")
        await image.wait_for(state="detached")

        await card.get_by_role("button", name="I got a confirmation").click()
        await self.wait_for_status(app_id, "applied")
        # The file is deleted after the status commit, so wait for it rather than assert at once.
        for _ in range(50):
            if not shot.exists():
                break
            await asyncio.sleep(0.1)
        self.assertFalse(shot.exists())

    async def test_a_code_step_waits_in_needs_you_and_the_typed_code_finishes_the_application(self):
        app_id = self.add_job(JOB_ONE, "Backend Engineer")
        page = await self.open_ui(self.start_app({
            JOB_ONE: (200, tla.GH_FORM.format(extra="", script=tlh._CODE_IN_PLACE)),
            "https://boards.greenhouse.io/acme/jobs/1/confirmation": tla.THANKS}))
        await page.get_by_role("button", name="Approve 1 job").click()
        await self.wait_for_status(app_id, "blocked_needs_input")

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-otp")
        await card.wait_for()
        self.assertIn("Enter the code Acme sent to jane@example.com", await card.inner_text())
        self.assertRegex(await card.get_by_test_id("otp-countdown").inner_text(), r"^[3-5]:\d\d left")
        self.assertEqual(self.row(app_id)["blocked_reason"], "otp")    # never applied before the code
        await card.get_by_label("Enter the code").fill("123456")
        await card.get_by_role("button", name="Send code").click()

        await self.wait_for_status(app_id, "applied")
        self.assertIn("Nothing needs you", await page.get_by_role("alert").inner_text())

    async def test_a_captcha_card_opens_the_headed_window_and_leaves_the_tray(self):
        app_id = self.add_job(JOB_ONE, "Backend Engineer")
        # The "user" solves the widget two seconds after the page loads. The headless pass is stopped by the
        # widget before it clicks, so the timer only matters in the headed window. The window runs on the
        # server's event loop, so the test can only watch it from outside, never drive it.
        solve = ("<script>setTimeout(() => document.querySelector('textarea[name=g-recaptcha-response]')"
                 ".value = 'token', 2000)</script>")
        pages = {JOB_ONE: (200, tla.GH_FORM.format(extra=tlh._CHECKBOX, script=solve)),
                 "https://boards.greenhouse.io/acme/jobs/1/confirmation": tla.THANKS}
        page = await self.open_ui(self.start_app(pages))
        await page.get_by_role("button", name="Approve 1 job").click()
        await self.wait_for_status(app_id, "blocked_needs_input")

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-captcha")
        await card.wait_for()
        self.assertIn("Acme needs you to solve a captcha", await card.inner_text())
        fake = tlh._FakeHeaded(pages)
        with mock.patch.object(local_handoff, "async_playwright", fake):
            await card.get_by_role("button", name="Open in browser").click()
            await page.get_by_text("Nothing needs you right now.").wait_for()
            await self.wait_for_status(app_id, "applied")
        self.assertEqual(fake.launch_kwargs, [{"headless": False}])

    async def test_a_captcha_card_offers_apply_in_my_browser_and_the_manual_card_has_copy_buttons(self):
        # A value reads as an upload only directly inside the app's resumes or attachments folder (security audit).
        resume = db.data_dir() / "resumes" / "resume.pdf"
        resume.parent.mkdir(exist_ok=True)
        resume.write_bytes(b"%PDF-1.4 fake")
        filled = json.dumps({"filled": {"First name": "Ana", "Resume": str(resume)}})
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="captcha", fields_used=filled)
        page = await self.open_ui(self.start_app({}), width=420)
        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-captcha")
        await card.wait_for()
        text = await card.inner_text()
        self.assertIn("Ashby among them", text)
        await card.get_by_role("button", name="Open in browser").wait_for()

        with mock.patch("webbrowser.open", return_value=True) as opener:
            await card.get_by_role("button", name="Apply in my browser").click()
            manual = page.get_by_test_id("card-verify_submit")
            await manual.get_by_text("Apply in the browser that opened").wait_for()
        opener.assert_called_once_with(tla.GH_URL)
        self.assertEqual((self.row(app_id)["blocked_reason"], self.row(app_id)["error"]), ("verify_submit", "manual_apply"))
        answers = await manual.get_by_test_id("manual-answers").inner_text()
        self.assertIn("Ana", answers)
        self.assertIn("Upload resume.pdf", answers)
        self.assertEqual(await manual.get_by_role("button", name="Copy").count(), 1)    # the file row has none
        self.assertEqual(await manual.get_by_role("button", name="No confirmation, try again").count(), 0)

        await manual.get_by_role("button", name="I didn't apply").click()
        await page.get_by_test_id("card-captcha").wait_for()
        self.assertEqual(self.row(app_id)["blocked_reason"], "captcha")
        with mock.patch("webbrowser.open", return_value=False):
            await page.get_by_role("button", name="Apply in my browser").click()
            await page.get_by_test_id("unopened-url").wait_for()
        await page.get_by_test_id("card-verify_submit").get_by_role("button", name="I applied").click()
        await self.wait_for_status(app_id, "applied")

    async def test_view_posting_opens_the_system_browser(self):
        self.add_row(status="blocked_needs_input", blocked_reason="attempts_exhausted", attempt_count=3)
        page = await self.open_ui(self.start_app({}))
        await page.get_by_role("link", name="Needs you (1)").click()
        with mock.patch("webbrowser.open", return_value=True) as opener:
            await page.get_by_role("button", name="View posting").click()
            for _ in range(50):
                if opener.called:
                    break
                await asyncio.sleep(0.1)
        opener.assert_called_once_with(tla.GH_URL)

    async def test_an_email_link_card_says_where_it_went_and_done_applies_it(self):
        app_id = self.add_job(JOB_ONE, "Backend Engineer")
        mail = (200, "<h1>Check your email</h1><p>We sent you a confirmation link.</p>")
        page = await self.open_ui(self.start_app({
            JOB_ONE: tlh._form_to("/acme/jobs/1/check-email"),
            "https://boards.greenhouse.io/acme/jobs/1/check-email": mail}), width=420)
        await page.get_by_role("button", name="Approve 1 job").click()
        await self.wait_for_status(app_id, "blocked_needs_input")

        await page.get_by_role("link", name="Needs you (1)").click()
        card = page.get_by_test_id("card-verify_submit")
        await card.wait_for()
        self.assertIn("Acme emailed a link to jane@example.com. Click it, then press Done.", await card.inner_text())
        await card.get_by_role("button", name="Done").click()
        await self.wait_for_status(app_id, "applied")

    async def test_an_exhausted_row_has_retry(self):
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="attempts_exhausted", attempt_count=3)
        page = await self.open_ui(self.start_app({}))
        await page.get_by_role("link", name="Needs you (1)").click()
        await page.get_by_test_id("card-attempts_exhausted").get_by_role("button", name="Retry").click()
        await self.wait_for_status(app_id, "pending")
        self.assertEqual(self.row(app_id)["attempt_count"], 2)

    async def test_progress_shows_queues_pauses_and_the_last_sourcing_refusal(self):
        db.set_setting("sourcing_last_refusal", {"reason": "no_key", "detail": {}, "at": "2026-10-13T00:00:00.000Z"})
        page = await self.open_ui(self.start_app({}))
        await page.get_by_role("link", name="Progress").click()

        self.assertIn("Add your Apify key", await page.get_by_test_id("sourcing-refusal").inner_text())
        await page.get_by_test_id("queue-lever").get_by_role("button", name="Pause").click()
        await page.get_by_text("Paused", exact=True).wait_for()
        self.assertEqual(db.get_setting("apply_paused"), {"lever": "user"})
        await page.get_by_test_id("queue-lever").get_by_role("button", name="Resume").click()
        await page.get_by_test_id("queue-state-lever").filter(has_text="Idle").wait_for()

    async def test_a_cooling_queue_counts_down_only_while_something_waits_and_a_backed_off_one_says_when_it_resumes(self):
        before = local_queues._state["greenhouse"]
        page = await self.open_ui(self.start_app({}))
        while local_queues._state["greenhouse"] is before:    # `run_all` swaps in fresh state when it starts
            await asyncio.sleep(0.05)
        await page.get_by_role("link", name="Progress").click()
        ends = local_queues._clock() + 300
        for ats in ("greenhouse", "lever"):
            local_queues._state[ats].next_at = ends       # the cooldown after an application, one queue with a job waiting
        local_queues._state["ashby"].backoff_until = ends
        waiting = self.add_row(url=JOB_ONE, ats="greenhouse", status="pending", approved_at="2026-10-18T00:00:00.000Z")

        await page.get_by_test_id("queue-state-greenhouse").filter(has_text="Next application in").wait_for()
        self.assertRegex(await page.get_by_test_id("queue-state-greenhouse").inner_text(), r"^Next application in [45]:\d\d$")
        self.assertIn("1 waiting", await page.get_by_test_id("queue-greenhouse").inner_text())
        self.assertEqual((await page.get_by_test_id("queue-state-lever").inner_text()), "Idle")    # cooling, nothing waiting
        self.assertRegex(await page.get_by_test_id("queue-state-ashby").inner_text(),
                         r"^Backing off after a block page - resumes in [45]:\d\d$")
        self.assertEqual(self.row(waiting)["status"], "pending")

    async def test_the_failed_count_opens_a_list_newest_first_and_a_row_opens_its_reason_error_and_posting(self):
        old = self.add_row(url="https://boards.greenhouse.io/acme/jobs/1", status="failed", skip_reason="listing_closed",
                           attempt_count=1, updated_at="2026-10-17T10:00:00.000Z")
        new = self.add_row(url="not a link", status="failed", skip_reason="error", error="host_not_allowed",
                           attempt_count=2, updated_at="2026-10-18T10:00:00.000Z")
        self.add_row(status="applied")
        for app_id, title in ((old, "Backend Engineer"), (new, "Data Engineer")):
            self.conn.execute("UPDATE listings SET title = ?, company = 'Acme' "
                              "WHERE id = (SELECT listing_id FROM job_applications WHERE id = ?)", (title, app_id))
        page = await self.open_ui(self.start_app({}))
        await page.get_by_role("link", name="Progress").click()

        await page.get_by_test_id("count-failed").get_by_role("button").click()
        rows = page.get_by_test_id("failed-row")
        await rows.first.wait_for()
        self.assertEqual(await rows.count(), 2)
        self.assertIn("Data Engineer at Acme", await rows.nth(0).inner_text())
        self.assertIn("Backend Engineer at Acme", await rows.nth(1).inner_text())

        await rows.nth(0).click()
        detail = page.get_by_test_id("failed-detail")
        await detail.wait_for()
        self.assertIn("The application hit an error", await detail.inner_text())
        self.assertEqual(await detail.get_by_test_id("failed-error").inner_text(), "host_not_allowed")
        self.assertEqual(await detail.get_by_role("link", name="View posting").count(), 0)    # not an http(s) URL
        await page.keyboard.press("Escape")

        await rows.nth(1).click()
        link = page.get_by_test_id("failed-detail").get_by_role("link", name="View posting")
        self.assertEqual(await link.get_attribute("href"), "https://boards.greenhouse.io/acme/jobs/1")
        self.assertIn("The posting was closed", await page.get_by_test_id("failed-detail").inner_text())

    async def test_closing_the_window_with_one_in_progress_warns_and_the_quit_settles_it_pending(self):
        app_id = self.add_job(JOB_ONE, "Backend Engineer")
        page = await self.open_ui(self.start_app({JOB_ONE: HANG}))
        await page.get_by_role("button", name="Approve 1 job").click()
        await self.wait_for_status(app_id, "in_progress")
        asked = []

        closes = await asyncio.to_thread(
            local_main.close_guard, lambda message: asked.append(message) or True, self.app.state.stop_queues)

        self.assertTrue(closes)
        self.assertEqual(asked, ["1 application in progress. Quit when they reach a safe point?"])
        row = self.row(app_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("pending", 0))


if __name__ == "__main__":
    unittest.main()
