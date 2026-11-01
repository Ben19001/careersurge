"""End to end: onboarding, Profile and Settings in the built UI, driven by headless Chromium.

`local_main.build_app` and `start_server` run in this process on a real 127.0.0.1 port, as in
`test_local_ui_e2e`. Nothing leaves the machine: Apify is `FakeApify` (rebound at
`local_sourcing._apify_client`), Jev is unavailable (the resume parse falls back to regex, the path a
rejected key takes), and the OS keychain is replaced by a file in the throwaway data dir. The queues
run for real but have nothing approved, so no application browser starts. The UI is opened the way
pywebview opens it, with the token in the fragment. Skipped when `frontend/dist` has not been built.
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import keyring  # noqa: E402
import test_local_apply as tla  # noqa: E402 - the store-backed base class
from keyring.backends import fail  # noqa: E402

import test_local_sourcing as tls  # noqa: E402 - FakeApify and its run fixtures
from app.store import db, secrets  # noqa: E402

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
RESUMES = Path(__file__).resolve().parent / "fixtures" / "resumes"
TOKEN = "onboarding-token"

_STUBBED = {}
local_main = local_profile = None


def setUpModule():
    """Real FastAPI and pydantic for the launcher, which `conftest` stubs suite-wide (see test_local_api)."""
    global local_main, local_profile
    if not (DIST / "index.html").exists():
        raise unittest.SkipTest("frontend/dist is not built: cd frontend && npm run build")
    for name in ("httpx", "pydantic"):
        if name in sys.modules:
            _STUBBED[name] = sys.modules.pop(name)
    try:
        from app import local_main as launcher
        from app.services import local_profile as profile
    except ImportError as exc:
        sys.modules.update(_STUBBED)
        raise unittest.SkipTest(f"the launcher's dependencies are not installed: {exc}")
    local_main, local_profile = launcher, profile


def tearDownModule():
    sys.modules.update(_STUBBED)


class HeldApify(tls.FakeApify):
    """An Apify whose run stays RUNNING until `release` is set, then SUCCEEDED with the fixture dataset."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def run(self, run_id):
        fake = self

        def wait_for_finish(wait_duration=None):
            if fake.release.wait(0.2):
                return tls.make_run("SUCCEEDED", run_id=run_id)
            return tls.make_run("RUNNING", run_id=run_id)
        return tls.SimpleNamespace(wait_for_finish=wait_for_finish)


@unittest.skipUnless(tla._HAVE_BROWSER, "playwright not installed")
class OnboardingE2E(tla._StoreTestCase):
    async def asyncSetUp(self):
        previous = keyring.get_keyring()
        keyring.set_keyring(fail.Keyring())     # keys go to secrets.json in the temp data dir, not a real keychain
        self.addCleanup(keyring.set_keyring, previous)
        for name in ("APIFY_TOKEN", "JEV_API_KEY"):
            env = mock.patch.dict(os.environ)
            env.start()
            self.addCleanup(env.stop)
            os.environ.pop(name, None)
        self.fake = HeldApify()
        for target, name, value in ((tls.local_sourcing, "_apify_client", lambda: self.fake),
                                    (local_main.local_queues, "POLL_S", 0.2)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.playwright = await tla.async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.chromium = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.chromium.close)
        self.addCleanup(self.fake.release.set)    # never leave an ingest thread waiting

    def start_app(self):
        """Serve the built UI with the real queues; returns the window URL."""
        db.set_setting("sourcing_interval", "manual")
        app = local_main.build_app(TOKEN, DIST)
        server, thread, port = local_main.start_server(app)
        self.base = f"http://127.0.0.1:{port}"

        def stop():
            server.should_exit = True
            thread.join(30)
        self.addCleanup(stop)
        return f"{self.base}/#token={TOKEN}"

    async def open_ui(self, url, width=1280):
        page = await self.chromium.new_page(viewport={"width": width, "height": 900})
        self.addAsyncCleanup(page.close)
        await page.goto(url)
        return page

    def call(self, method, path, body=None):
        """An API call with the token, as the UI makes it: `(status, json)`, timing out after 10 s."""
        request = urllib.request.Request(self.base + path, method=method, headers={
            "X-CareerSurge-Token": TOKEN, "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    async def wait_for(self, check, seconds=30):
        for _ in range(int(seconds / 0.1)):
            if check():
                return
            await tla.asyncio.sleep(0.1)
        self.fail("timed out waiting for the condition")

    def runs(self):
        return [tuple(row) for row in self.conn.execute("SELECT apify_run_id, status FROM sourcing_runs")]

    def prepared(self):
        return self.conn.execute("SELECT COUNT(*) FROM job_applications WHERE status = 'prepared'").fetchone()[0]

    async def save_keys(self, page):
        await page.get_by_label("Apify key").fill("apify_api_E2EKEY1111")
        await page.get_by_role("button", name="Save key").first.click()
        await page.get_by_test_id("key-status-APIFY_TOKEN").wait_for()
        await page.get_by_label("Jev key").fill("jev_e2e_key_2222")
        await page.get_by_role("button", name="Save key").first.click()
        await page.get_by_test_id("key-status-JEV_API_KEY").wait_for()
        self.assertIn("ends in 2222", await page.get_by_test_id("key-status-JEV_API_KEY").inner_text())

    def add_onboarded_profile(self, **extra):
        """A confirmed profile plus a job title: what the app needs to skip onboarding."""
        self.add_profile(**extra)
        db.set_setting("sourcing_filters", {"titles": ["software engineer"]})

    async def upload_resume(self, page):
        await page.set_input_files("#resume", str(RESUMES / "one_column.pdf"))
        await page.get_by_label("Full name").wait_for()

    async def test_an_empty_data_dir_opens_onboarding_at_keys_and_the_jev_key_never_comes_back(self):
        page = await self.open_ui(self.start_app())

        await page.get_by_test_id("step-title").wait_for()
        self.assertEqual(await page.get_by_test_id("step-title").inner_text(), "Connect your accounts")
        self.assertEqual((await page.get_by_test_id("step-count").inner_text()).lower(), "step 1 of 7")
        self.assertTrue(await page.get_by_role("button", name="Continue").is_disabled())    # both keys are needed
        await self.save_keys(page)
        await page.get_by_role("button", name="Continue").click()
        self.assertEqual(await page.get_by_test_id("step-title").inner_text(), "Upload your resume")

        status, body = self.call("GET", "/api/keys/JEV_API_KEY")
        self.assertEqual((status, body), (200, {"ok": True, "set": True, "last4": "2222", "rejected": False}))
        self.assertEqual(self.call("GET", "/api/keys/OTHER")[0], 404)

    async def test_a_resume_the_rules_refuse_is_refused_with_the_1_mb_pdf_rule(self):
        db.set_setting("sourcing_filters", {})
        page = await self.open_ui(self.start_app())
        await self.save_keys(page)
        await page.get_by_role("button", name="Continue").click()

        await page.set_input_files("#resume", files=[{"name": "big.pdf", "mimeType": "application/pdf",
                                                     "buffer": b"%PDF-" + b"0" * 1_300_000}])
        self.assertEqual(await page.get_by_test_id("step-problem").inner_text(), local_profile.RESUME_RULE)
        await page.set_input_files("#resume", files=[{"name": "cv.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                                     "buffer": b"PK\x03\x04 word"}])
        self.assertEqual(await page.get_by_test_id("step-problem").inner_text(), local_profile.RESUME_RULE)
        self.assertEqual(await page.get_by_test_id("step-title").inner_text(), "Upload your resume")

    async def test_a_rejected_jev_key_still_gives_a_filled_review_and_an_edit_survives_a_reopen(self):
        page = await self.open_ui(self.start_app())
        await self.save_keys(page)
        await page.get_by_role("button", name="Continue").click()

        await self.upload_resume(page)    # Jev is unavailable in this test: the regex candidates fill the form
        self.assertEqual(await page.get_by_label("Full name").input_value(), "Jane Q. Doe")
        self.assertEqual(await page.get_by_label("Email").input_value(), "jane.doe@example.com")
        self.assertEqual(await page.get_by_test_id("step-title").inner_text(), "Review what we found")
        await page.get_by_label("Phone").fill("555-0199")
        await page.get_by_test_id("draft-saved").wait_for()

        reopened = await self.open_ui(self.base + f"/#token={TOKEN}")    # "close the app, open it again"
        await reopened.get_by_label("Phone").wait_for()
        self.assertEqual(await reopened.get_by_test_id("step-title").inner_text(), "Review what we found")
        self.assertEqual(await reopened.get_by_label("Phone").input_value(), "555-0199")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM profile").fetchone())    # nothing is the profile until confirmed

    async def test_a_reopened_review_confirms_without_choosing_the_resume_again(self):
        page = await self.open_ui(self.start_app())
        await self.save_keys(page)
        await page.get_by_role("button", name="Continue").click()
        await self.upload_resume(page)
        await page.get_by_label("Phone").fill("555-0199")
        await page.get_by_test_id("draft-saved").wait_for()

        reopened = await self.open_ui(self.base + f"/#token={TOKEN}")    # "close the app, open it again": the File is gone
        await reopened.get_by_label("Phone").wait_for()
        self.assertEqual(await reopened.get_by_label("Attach your resume again to confirm").count(), 0)
        await reopened.get_by_role("button", name="Confirm profile").click()
        await reopened.get_by_test_id("step-title").filter(has_text="Common application questions").wait_for()

        profile = local_profile.get_profile()
        self.assertEqual(profile["phone"], "555-0199")
        self.assertEqual(Path(profile["resume_path"]).read_bytes(), (RESUMES / "one_column.pdf").read_bytes())
        self.assertIsNone(local_profile.get_draft())

    async def test_a_reopened_review_whose_stored_resume_is_gone_offers_the_attach_input(self):
        page = await self.open_ui(self.start_app())
        await self.save_keys(page)
        await page.get_by_role("button", name="Continue").click()
        await self.upload_resume(page)
        await page.get_by_label("Phone").fill("555-0199")
        await page.get_by_test_id("draft-saved").wait_for()
        Path(local_profile.get_draft()["resume_path"]).unlink()    # the PDF the parse stored is deleted

        reopened = await self.open_ui(self.base + f"/#token={TOKEN}")
        await reopened.get_by_label("Phone").wait_for()
        await reopened.get_by_role("button", name="Confirm profile").click()
        attach = reopened.get_by_label("Attach your resume again to confirm")
        await attach.wait_for()
        self.assertIn("is gone", await reopened.get_by_test_id("error-resume-again").inner_text())

        await attach.set_input_files(RESUMES / "one_column.pdf")
        await reopened.get_by_role("button", name="Confirm profile").click()
        await reopened.get_by_test_id("step-title").filter(has_text="Common application questions").wait_for()
        self.assertEqual(Path(local_profile.get_profile()["resume_path"]).read_bytes(),
                         (RESUMES / "one_column.pdf").read_bytes())

    async def test_confirming_without_a_name_or_email_marks_the_field_and_saves_nothing(self):
        page = await self.open_ui(self.start_app())
        await self.save_keys(page)
        await page.get_by_role("button", name="Continue").click()
        await self.upload_resume(page)

        await page.get_by_label("Full name").fill("")
        await page.get_by_label("Email").fill("")
        await page.get_by_role("button", name="Confirm profile").click()

        self.assertIn("full name is required", (await page.get_by_test_id("error-full_name").inner_text()).lower())
        self.assertIn("email is required", (await page.get_by_test_id("error-email").inner_text()).lower())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM profile").fetchone())

    async def test_the_whole_first_run_from_keys_to_a_prepared_batch(self):
        page = await self.open_ui(self.start_app())
        await self.save_keys(page)
        await page.get_by_role("button", name="Continue").click()
        await self.upload_resume(page)

        # A comma still separates skills.
        await page.get_by_label("Add a skill").press_sequentially("Rust, Zig")
        await page.get_by_label("Add a skill").press("Enter")
        self.assertEqual(await page.get_by_role("button", name="Remove Rust", exact=True).count(), 1)
        self.assertEqual(await page.get_by_role("button", name="Remove Zig", exact=True).count(), 1)

        await page.get_by_role("button", name="Confirm profile").click()
        await page.get_by_test_id("step-title").filter(has_text="Common application questions").wait_for()
        profile = local_profile.get_profile()
        self.assertEqual((profile["full_name"], profile["first_name"], profile["last_name"], profile["current_location"]),
                         ("Jane Q. Doe", "Jane", "Doe", "San Jose, CA"))
        self.assertTrue(Path(profile["resume_path"]).is_file())

        # Yes/No and start-date questions are dropdowns, salary is whole dollars per year, no "hear about us".
        self.assertEqual(await page.get_by_text("How did you hear about us?").count(), 0)
        work_auth = page.get_by_label("Are you legally authorized to work in the United States?")
        self.assertEqual(await work_auth.evaluate("element => element.tagName"), "SELECT")
        self.assertEqual(await work_auth.locator("option").all_inner_texts(), ["Pick one", "Yes", "No"])
        salary = page.get_by_label("What are your salary expectations?")
        self.assertEqual(await salary.get_attribute("type"), "number")
        self.assertEqual(await page.get_by_text("per year", exact=True).count(), 1)

        await work_auth.select_option("Yes")
        await salary.fill("120000")
        await page.get_by_label("When can you start?").select_option("Within 2 weeks")
        await page.get_by_role("button", name="Save answers and continue").click()
        await page.get_by_test_id("step-title").filter(has_text="Let CareerSurge confirm").wait_for()
        answers = {q["question"]: q["answer"] for q in local_profile.list_global_questions()}
        self.assertEqual(answers["When can you start?"], "Within 2 weeks")
        self.assertEqual(answers["Are you legally authorized to work in the United States?"], "Yes")
        self.assertEqual(answers["What are your salary expectations?"], "120000")

        # Every permission starts unticked; the general one warns, the other two cannot be ticked without it.
        self.assertIn("each of those jobs waits for you in Needs you", await page.get_by_test_id("consent-warning").inner_text())
        self.assertTrue(await page.get_by_test_id("consent-background").is_disabled())
        self.assertTrue(await page.get_by_test_id("consent-arbitration").is_disabled())
        await page.get_by_test_id("consent-general").click()
        self.assertEqual(await page.get_by_test_id("consent-warning").count(), 0)
        self.assertTrue(await page.get_by_test_id("consent-background").is_enabled())
        await page.get_by_role("button", name="Save and continue").click()
        await page.get_by_test_id("step-title").filter(has_text="What jobs").wait_for()
        saved = local_profile.get_profile()
        self.assertEqual((saved["auto_consent_authorized"], saved["background_check_authorized"],
                          saved["arbitration_authorized"]), (1, 0, 0))
        self.assertEqual(saved["consent_tos_version"], local_profile.CONSENT_TOS_VERSION)

        await page.get_by_role("button", name="Save and continue").click()    # titles are required
        self.assertIn("at least one job title", await page.get_by_test_id("error-titles").inner_text())
        await page.get_by_label("Add a job title").fill("Backend Engineer")
        await page.get_by_label("Add a job title").press("Enter")
        await page.get_by_label("Add a location").press_sequentially("Austin, Texas")      # one entry, comma kept
        await page.get_by_label("Add a location").press("Enter")
        await page.get_by_role("button", name="Save and continue").click()
        await page.get_by_test_id("step-title").filter(has_text="Find your first jobs").wait_for()
        self.assertEqual(db.get_setting("sourcing_filters")["titles"], ["Backend Engineer"])
        self.assertEqual(db.get_setting("sourcing_filters")["locations"], ["Austin, Texas"])

        # The price was never accepted: Run now asks, and nothing started yet.
        await page.get_by_role("button", name="Run now").click()
        notice = page.get_by_test_id("sourcing-refusal")
        await notice.wait_for()
        self.assertEqual(await notice.get_attribute("data-reason"), "price_unconfirmed")
        self.assertIn("$0.0120", await notice.inner_text())
        self.assertEqual(self.runs(), [])

        # Accepting saves the price and starts the run, and lands on Progress; Apify is still running.
        started = time.monotonic()
        await notice.get_by_role("button", name="Accept $0.0120 per job and run").click()
        await page.get_by_test_id("fetching-jobs").wait_for()
        self.assertTrue(page.url.endswith("/progress"))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(db.get_setting("sourcing_confirmed_price_usd"), 0.012)
        self.assertEqual([status for _, status in self.runs()], ["started"])
        self.assertEqual(self.prepared(), 0)

        self.fake.release.set()
        await self.wait_for(lambda: [status for _, status in self.runs()] == ["succeeded"] and self.prepared() > 0)
        await page.get_by_role("link", name="Approve", exact=True).click()
        await page.get_by_test_id("job-row").first.wait_for()

    async def open_run_step(self, page):
        """From a saved profile with a title: onboarding reopens at the questions, so skip to the Run step."""
        await page.goto(self.base + "/onboarding")
        await page.get_by_test_id("step-title").filter(has_text="Common application questions").wait_for()
        await page.get_by_role("button", name="Skip").click()
        await page.get_by_test_id("step-title").filter(has_text="Let CareerSurge confirm").wait_for()
        await page.get_by_role("button", name="Skip").click()
        await page.get_by_test_id("step-title").filter(has_text="What jobs").wait_for()
        await page.get_by_role("button", name="Save and continue").click()
        await page.get_by_test_id("step-title").filter(has_text="Find your first jobs").wait_for()

    async def test_run_now_with_the_price_confirmed_opens_progress_while_apify_fetches(self):
        self.add_onboarded_profile()
        db.set_setting("sourcing_confirmed_price_usd", 0.012)
        page = await self.open_ui(self.start_app())
        await self.open_run_step(page)

        await page.get_by_role("button", name="Run now").click()
        await page.get_by_test_id("fetching-jobs").wait_for()
        self.assertTrue(page.url.endswith("/progress"))
        self.assertEqual([status for _, status in self.runs()], ["started"])

    async def test_the_skip_button_goes_to_approve_and_starts_nothing(self):
        self.add_onboarded_profile()
        page = await self.open_ui(self.start_app())
        await self.open_run_step(page)

        await page.get_by_role("button", name="Skip, I'll run it later").click()
        await page.get_by_role("link", name="Settings", exact=True).wait_for()
        self.assertFalse(page.url.endswith("/progress"))
        self.assertEqual(self.runs(), [])

    async def test_a_refused_run_now_stays_on_the_step_and_shows_the_refusal(self):
        self.add_onboarded_profile()
        db.set_setting("sourcing_confirmed_price_usd", 0.012)
        no_key = mock.patch.object(tls.local_sourcing, "_apify_client",
                                   side_effect=tls.local_sourcing.SourcingRefused("no_key"))
        no_key.start()
        self.addCleanup(no_key.stop)
        page = await self.open_ui(self.start_app())
        await self.open_run_step(page)

        await page.get_by_role("button", name="Run now").click()
        notice = page.get_by_test_id("sourcing-refusal")
        await notice.wait_for()
        self.assertEqual(await notice.get_attribute("data-reason"), "no_key")
        self.assertEqual(await page.get_by_test_id("step-title").inner_text(), "Find your first jobs")
        self.assertTrue(page.url.endswith("/onboarding"))

    async def test_accepting_the_price_on_the_approve_screen_runs_and_stays_on_approve(self):
        self.add_onboarded_profile()
        secrets.set_secret("APIFY_TOKEN", "apify_api_E2EKEY1111")
        db.set_setting("sourcing_last_refusal", {"reason": "price_unconfirmed", "detail": {"price": 0.012},
                                                 "at": "2026-10-18T00:00:00.000Z"})
        page = await self.open_ui(self.start_app())

        await page.get_by_role("button", name="Accept $0.0120 per job and run").click()
        await page.get_by_test_id("fetching-jobs").wait_for()
        self.assertFalse(page.url.endswith("/progress"))
        self.assertEqual([status for _, status in self.runs()], ["started"])

    async def test_skipping_permissions_leaves_every_flag_off_and_the_profile_block_sets_them_later(self):
        self.add_profile()
        db.set_setting("sourcing_filters", {"titles": ["software engineer"]})
        db.set_setting("sourcing_confirmed_price_usd", 0.012)
        page = await self.open_ui(self.start_app())
        await page.goto(self.base + "/onboarding")    # a saved profile with a title reopens at the questions
        await page.get_by_test_id("step-title").filter(has_text="Common application questions").wait_for()
        await page.get_by_role("button", name="Skip").click()
        await page.get_by_test_id("step-title").filter(has_text="Let CareerSurge confirm").wait_for()
        await page.get_by_role("button", name="Skip").click()
        await page.get_by_test_id("step-title").filter(has_text="What jobs").wait_for()
        self.assertEqual(local_profile.get_profile()["auto_consent_authorized"], 0)

        await page.goto(self.base + "/profile")
        await page.get_by_test_id("consent-general").click()
        await page.get_by_test_id("consent-arbitration").click()
        await page.get_by_role("button", name="Save permissions").click()
        await page.get_by_test_id("consent-saved").wait_for()
        saved = local_profile.get_profile()
        self.assertEqual((saved["auto_consent_authorized"], saved["background_check_authorized"],
                          saved["arbitration_authorized"]), (1, 0, 1))

        await page.get_by_test_id("consent-general").click()    # off again: the two it widens go off with it
        await page.get_by_role("button", name="Save permissions").click()
        await page.get_by_test_id("consent-saved").wait_for()
        saved = local_profile.get_profile()
        self.assertEqual((saved["auto_consent_authorized"], saved["arbitration_authorized"]), (0, 0))
        # The record of what was once agreed stays.
        self.assertEqual(saved["arbitration_tos_version"], local_profile.ARBITRATION_TOS_VERSION)

    async def test_a_flag_agreed_under_older_wording_shows_unticked_with_a_note_until_ticked_and_saved(self):
        self.add_onboarded_profile(auto_consent_authorized=1, consent_tos_version="2026-10-17")
        page = await self.open_ui(self.start_app())
        await page.goto(self.base + "/profile")

        note = page.get_by_test_id("consent-stale-general")
        await note.wait_for()
        self.assertIn("The wording changed since you agreed", await note.inner_text())
        self.assertFalse(await page.get_by_test_id("consent-general").is_checked())
        copy = await page.get_by_test_id("consent-block").inner_text()
        self.assertNotIn("CareerSurge", copy)
        self.assertNotRegex(copy, r"\bWe\b")

        await page.get_by_test_id("consent-general").click()
        self.assertEqual(await note.count(), 0)
        await page.get_by_role("button", name="Save permissions").click()
        await page.get_by_test_id("consent-saved").wait_for()
        self.assertEqual(local_profile.get_profile()["consent_tos_version"], local_profile.CONSENT_TOS_VERSION)

        await page.reload()
        await page.get_by_test_id("consent-general").wait_for()
        self.assertTrue(await page.get_by_test_id("consent-general").is_checked())
        self.assertEqual(await note.count(), 0)

    async def test_a_start_answer_saved_before_the_list_existed_shows_pick_one_and_stays_saved(self):
        self.add_onboarded_profile()
        db.set_setting("sourcing_confirmed_price_usd", 0.012)
        local_profile.seed_global_questions()
        local_profile.save_global_answer(local_profile._normalize_question("When can you start?"), "Immediately")
        with self.conn:
            self.conn.execute("UPDATE application_qna_bank SET answer_value = 'ASAP' WHERE normalized_question = 'when can you start'")
        page = await self.open_ui(self.start_app())
        await page.goto(self.base + "/onboarding")
        await page.get_by_test_id("step-title").filter(has_text="Common application questions").wait_for()

        start = page.get_by_label("When can you start?")
        self.assertEqual(await start.input_value(), "")
        self.assertEqual(await start.locator("option:checked").inner_text(), "Pick one")
        await page.get_by_role("button", name="Save answers and continue").click()
        await page.get_by_test_id("step-title").filter(has_text="Let CareerSurge confirm").wait_for()
        answers = {q["question"]: q["answer"] for q in local_profile.list_global_questions()}
        self.assertEqual(answers["When can you start?"], "ASAP")

    async def test_a_profile_without_a_job_title_reopens_onboarding_at_the_filters_until_a_title_is_saved(self):
        self.add_profile()      # the app was closed after Confirm, before the filters
        page = await self.open_ui(self.start_app())

        await page.get_by_test_id("step-title").filter(has_text="What jobs").wait_for()
        self.assertEqual(await page.get_by_role("link", name="Settings", exact=True).count(), 0)    # no nav yet

        await page.get_by_label("Add a job title").fill("Backend Engineer")
        await page.get_by_label("Add a job title").press("Enter")
        await page.get_by_role("button", name="Save and continue").click()
        await page.get_by_test_id("step-title").filter(has_text="Find your first jobs").wait_for()
        await page.get_by_role("link", name="Settings", exact=True).wait_for()    # onboarding is done: the nav opens

        reopened = await self.open_ui(self.base + f"/#token={TOKEN}")
        await reopened.get_by_role("link", name="Settings", exact=True).wait_for()
        self.assertEqual(await reopened.get_by_test_id("step-title").count(), 0)    # straight into the normal app

    async def test_run_now_answers_at_once_while_apify_runs_and_a_second_run_is_refused(self):
        self.add_profile()
        db.set_setting("sourcing_filters", {"titles": ["software engineer"]})
        db.set_setting("sourcing_confirmed_price_usd", 0.012)
        self.start_app()

        started = time.monotonic()
        status, body = self.call("POST", "/api/sourcing/run", {})
        self.assertEqual((status, body["ok"]), (202, True))
        self.assertLess(time.monotonic() - started, 2)
        _, progress = self.call("GET", "/api/progress")
        self.assertEqual(progress["sourcing_run"]["status"], "started")

        status, body = self.call("POST", "/api/sourcing/run", {})
        self.assertEqual((status, body["reason"]), (409, "run_in_progress"))

        self.fake.release.set()
        await self.wait_for(lambda: self.call("GET", "/api/progress")[1]["counts"].get("prepared", 0) > 0)
        _, progress = self.call("GET", "/api/progress")
        self.assertEqual(progress["sourcing_run"]["status"], "succeeded")

    async def test_a_backlog_is_confirmed_in_settings_and_the_run_goes_ahead_with_confirm_backlog(self):
        self.add_profile()
        db.set_setting("sourcing_filters", {"titles": ["software engineer"]})
        db.set_setting("sourcing_confirmed_price_usd", 0.012)
        for index in range(30):
            listing = self.conn.execute(
                "INSERT INTO listings (source, external_id, url, company_key) "
                "VALUES ('greenhouse', ?, 'https://x.test/j', 'acme')", (f"q{index}",)).lastrowid
            self.conn.execute("INSERT INTO job_applications (listing_id, ats, status) VALUES (?, 'greenhouse', 'prepared')",
                              (listing,))
        page = await self.open_ui(self.start_app())
        await page.get_by_role("link", name="Settings", exact=True).click()
        await page.get_by_role("button", name="Run now").click()

        notice = page.get_by_test_id("sourcing-refusal")
        await notice.wait_for()
        self.assertEqual(await notice.get_attribute("data-reason"), "backlog_unconfirmed")
        self.assertIn("30 applications", await notice.inner_text())
        self.assertEqual(self.runs(), [])

        await notice.get_by_role("button", name="Fetch more anyway").click()
        await page.get_by_test_id("fetching-jobs").wait_for()
        self.assertEqual([status for _, status in self.runs()], ["started"])

    async def test_settings_run_now_opens_progress_and_says_what_the_check_found(self):
        # Seen in a real run: Run now in Settings stayed put, and a check that found 0 jobs ended in a second
        # with nothing on Progress, so it read as a button that does nothing.
        self.add_onboarded_profile()
        db.set_setting("sourcing_filters", {"titles": ["software engineer"]})
        db.set_setting("sourcing_confirmed_price_usd", 0.012)
        page = await self.open_ui(self.start_app())
        await page.get_by_role("link", name="Settings", exact=True).click()
        await page.get_by_role("button", name="Run now").click()

        await page.get_by_test_id("fetching-jobs").wait_for()
        self.assertTrue(page.url.endswith("/progress"))
        self.fake.release.set()
        last_check = page.get_by_test_id("last-check")
        await last_check.wait_for()
        self.assertRegex(await last_check.inner_text(), r"^Last job check, at .+: (found \d+ jobs?|no jobs matched)")

    async def test_settings_says_a_refused_jev_key_was_refused_until_a_new_one_is_saved(self):
        # A Jev key TypeSafe refused on every call still showed "Saved" here.
        self.add_onboarded_profile()
        secrets.set_secret("JEV_API_KEY", "jev_old_key_1111")
        db.set_setting("jev_key_rejected", True)
        page = await self.open_ui(self.start_app())
        await page.get_by_role("link", name="Settings", exact=True).click()

        refused = page.get_by_test_id("error-JEV_API_KEY")
        await refused.wait_for()
        self.assertIn("refused the last time it was used", await refused.inner_text())

        await page.get_by_label("Jev key").fill("jev_new_key_2222")
        await page.get_by_role("button", name="Replace key").last.click()
        await page.get_by_text("ends in 2222").wait_for()
        self.assertEqual(await refused.count(), 0)

    async def test_a_budget_refusal_the_scheduler_stored_shows_with_the_numbers_on_the_next_open(self):
        self.add_onboarded_profile()
        db.set_setting("sourcing_last_refusal", {"reason": "budget", "detail": {"remaining": 1.5, "minimal": 2.0},
                                                 "at": "2026-10-13T00:00:00.000Z"})
        page = await self.open_ui(self.start_app())

        notice = page.get_by_test_id("sourcing-refusal")
        await notice.wait_for()
        text = await notice.inner_text()
        self.assertIn("$1.50", text)
        self.assertIn("$2.00", text)

    def break_the_poll(self, error):
        """Make Apify's answer to the ingest's poll raise `error`."""
        def wait_for_finish(wait_duration=None):
            raise error
        self.fake.run = lambda run_id: tls.SimpleNamespace(
            wait_for_finish=wait_for_finish, get=lambda: tls.make_run(usage=0.5, run_id=run_id))

    async def test_a_failed_ingest_the_server_is_retrying_shows_why_and_when_on_progress(self):
        self.add_onboarded_profile()
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, status) VALUES ('run1', 'started')")
        self.break_the_poll(tls.api_error(403))    # Apify rejects the key: the user can fix it, so the server retries
        page = await self.open_ui(self.start_app())

        await page.get_by_role("link", name="Progress", exact=True).click()
        notice = page.get_by_test_id("sourcing-refusal")
        await notice.wait_for()
        self.assertEqual(await notice.get_attribute("data-reason"), "ingest_failed")
        self.assertRegex(await notice.inner_text(), r"Fetching jobs failed: .+\. Retrying at \d{1,2}:\d{2}")
        self.assertEqual(self.runs(), [("run1", "started")])

    async def test_a_failed_ingest_that_cannot_be_fixed_shows_the_error_without_a_retry_time(self):
        self.add_onboarded_profile()
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, status) VALUES ('run1', 'started')")
        self.break_the_poll(RuntimeError("could not map the listing"))
        page = await self.open_ui(self.start_app())

        await page.get_by_role("link", name="Progress", exact=True).click()
        notice = page.get_by_test_id("sourcing-refusal")
        await notice.wait_for()
        text = await notice.inner_text()
        self.assertIn("Fetching jobs failed: could not map the listing.", text)
        self.assertNotIn("Retrying", text)
        self.assertEqual(self.runs(), [("run1", "failed")])    # Run now works again

    async def test_a_run_left_started_is_ingested_and_prepared_on_relaunch(self):
        self.add_profile()
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, status) VALUES ('run1', 'started')")
        self.fake.release.set()
        self.start_app()

        await self.wait_for(lambda: self.runs() == [("run1", "succeeded")] and self.prepared() > 0)

    async def test_the_profile_screen_saves_a_new_phone_without_a_new_resume(self):
        self.add_onboarded_profile(phone="555-0100")
        resume_path = self.conn.execute("SELECT resume_path FROM profile").fetchone()[0]
        page = await self.open_ui(self.start_app())

        await page.get_by_role("link", name="Profile", exact=True).click()
        await page.get_by_label("Phone").fill("555-0199")
        await page.get_by_role("button", name="Save profile").click()
        await page.get_by_test_id("profile-saved").wait_for()

        row = self.conn.execute("SELECT phone, resume_path FROM profile").fetchone()
        self.assertEqual(tuple(row), ("555-0199", resume_path))

    async def test_settings_save_valid_values_and_name_the_30_second_floor(self):
        self.add_onboarded_profile()
        page = await self.open_ui(self.start_app())
        await page.get_by_role("link", name="Settings", exact=True).click()

        await page.locator("#floor").fill("10")
        await page.get_by_role("button", name="Save settings").click()
        self.assertIn("at least 30 seconds", await page.get_by_role("alert").first.inner_text())
        self.assertIsNone(db.get_setting("apply_cooldown_floor_s"))

        await page.locator("#floor").fill("45")
        await page.locator("#interval").select_option("1h")
        await page.get_by_label("Add a job title").fill("Data Engineer")
        await page.get_by_label("Add a job title").press("Enter")
        await page.get_by_role("button", name="Save settings").click()
        await page.get_by_test_id("settings-saved").wait_for()
        self.assertEqual((db.get_setting("apply_cooldown_floor_s"), db.get_setting("sourcing_interval")), (45, "1h"))
        self.assertEqual(db.get_setting("sourcing_filters")["titles"], ["software engineer", "Data Engineer"])

    async def test_settings_shows_the_month_spend_and_explains_the_hints(self):
        self.add_onboarded_profile()
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, status, cost_usd) VALUES ('r1', 'succeeded', 1.2)")
        page = await self.open_ui(self.start_app())
        await page.get_by_role("link", name="Settings", exact=True).click()

        await page.get_by_text("$1.20 of $5.00 used this month. Resets on").wait_for()
        for hint in ("Automatic covers the time since the last search",
                     "Each search is charged to your Apify account",
                     "plus a random 0 to 2 minutes"):
            self.assertTrue(await page.get_by_text(hint).is_visible(), hint)

        self.conn.execute("UPDATE sourcing_runs SET cost_usd = 5 WHERE apify_run_id = 'r1'")
        await page.locator("#budget").fill("5")
        await page.get_by_role("button", name="Save settings").click()
        used_up = page.get_by_text("Budget used up. No searches until")
        await used_up.wait_for()
        self.assertIn("text-destructive", await used_up.get_attribute("class"))


if __name__ == "__main__":
    unittest.main()
