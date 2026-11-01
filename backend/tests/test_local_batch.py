"""Batch approval, the frozen snapshot the worker fills from, and resume retention.

Runs against a throwaway data dir. The fill test drives a REAL Chromium that answers every request from
memory (`test_local_apply._RoutedBrowser`); nothing leaves the machine.
"""
import hashlib
import json
import os
import sys
import unittest

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import test_local_apply as tla  # noqa: E402 - the store-backed base class and routed-browser helpers

# conftest stubs `pydantic`, and `apify_client` (imported by local_queues, which prepared_batch uses
# for the cooldown) needs the real one. Swap the stub out for the import and put it back, as
# test_local_queues does.
_pydantic_stub = sys.modules.pop("pydantic", None)
try:
    from app.services import local_apply, local_batch, local_profile, local_queues  # noqa: E402
finally:
    if _pydantic_stub is not None:
        sys.modules["pydantic"] = _pydantic_stub
from app.store import db  # noqa: E402

PDF_ONE = b"%PDF-1.4 resume one"
PDF_TWO = b"%PDF-1.4 resume two"


class _BatchCase(tla._StoreTestCase):
    def add_prepared(self, count, **columns):
        return [self.add_row(status="prepared", **columns) for _ in range(count)]

    def status(self, app_id):
        row = self.row(app_id)
        return row["status"], row["skip_reason"]


class ApproveTests(_BatchCase):
    def setUp(self):
        super().setUp()
        self.add_profile(phone="555-0100", auto_consent_authorized=1,
                         consent_tos_version=local_profile.CONSENT_TOS_VERSION)

    def test_approve_three_and_skip_one_of_four_prepared(self):
        ids = self.add_prepared(4)

        result = local_batch.approve(ids[:3], ids[3:])

        self.assertEqual((result["approved"], result["skipped"]), (3, 1))
        rows = [self.row(app_id) for app_id in ids[:3]]
        self.assertEqual({row["status"] for row in rows}, {"pending"})
        self.assertEqual({row["approval_batch_id"] for row in rows}, {result["batch_id"]})
        self.assertTrue(all(row["approved_at"] for row in rows))
        self.assertEqual(self.status(ids[3]), ("cancelled", "user_skipped"))
        self.assertIsNone(self.row(ids[3])["approval_batch_id"])

    def test_an_id_that_is_running_is_not_counted_or_touched(self):
        (prepared,) = self.add_prepared(1)
        running = self.add_row(status="in_progress", claim_token="t", approved_at="2026-10-01T00:00:00.000Z")

        result = local_batch.approve([prepared, running], [])

        self.assertEqual(result["approved"], 1)
        self.assertEqual(self.row(running)["status"], "in_progress")
        self.assertEqual(self.row(running)["approval_batch_id"], 1)

    def test_an_id_already_pending_is_not_counted_and_keeps_its_batch(self):
        (prepared,) = self.add_prepared(1)
        pending = self.add_row(status="pending", approved_at="2026-10-01T00:00:00.000Z")

        result = local_batch.approve([prepared, pending], [])

        self.assertEqual(result["approved"], 1)
        self.assertEqual(self.row(pending)["approved_at"], "2026-10-01T00:00:00.000Z")
        self.assertEqual(self.row(pending)["approval_batch_id"], 1)    # the fixture's batch, not the new one
        self.assertEqual(self.row(prepared)["approval_batch_id"], result["batch_id"])

    def test_approving_nothing_keeps_no_batch_row(self):
        (prepared,) = self.add_prepared(1)
        batches_before = self.conn.execute("SELECT COUNT(*) FROM approval_batches").fetchone()[0]

        result = local_batch.approve([], [prepared])
        self.assertEqual(result, {"batch_id": None, "approved": 0, "skipped": 1})
        again = local_batch.approve([prepared], [])     # already cancelled: nothing to approve
        self.assertEqual(again, {"batch_id": None, "approved": 0, "skipped": 0})

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approval_batches").fetchone()[0], batches_before)

    def test_an_id_in_both_lists_is_refused_before_any_write(self):
        first, second = self.add_prepared(2)
        with self.assertRaises(ValueError):
            local_batch.approve([first, second], [second])
        self.assertEqual({self.row(first)["status"], self.row(second)["status"]}, {"prepared"})

    def test_no_profile_or_no_resume_file_refuses_and_changes_nothing(self):
        (prepared,) = self.add_prepared(1)
        self.resume.unlink()
        with self.assertRaisesRegex(ValueError, "resume"):
            local_batch.approve([prepared], [])
        self.conn.execute("DELETE FROM profile")
        with self.assertRaisesRegex(ValueError, "profile"):
            local_batch.approve([prepared], [])
        self.assertEqual(self.status(prepared), ("prepared", None))

    def test_the_snapshot_holds_the_profile_resume_global_answers_and_consent_as_approved(self):
        self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, "
            "answer_value, is_answered) VALUES ('Notice period?', 'notice period', 'stable_fact', 'global', "
            "'2 weeks', 1), ('Unanswered?', 'unanswered', 'stable_fact', 'global', NULL, 0)")
        (prepared,) = self.add_prepared(1)

        batch_id = local_batch.approve([prepared], [])["batch_id"]
        self.conn.execute("UPDATE profile SET phone = '555-9999', auto_consent_authorized = 0")
        snapshot = local_batch.snapshot(batch_id)

        self.assertEqual(snapshot["profile"]["phone"], "555-0100")
        self.assertEqual(snapshot["consent"]["auto_consent_authorized"], 1)
        self.assertEqual(snapshot["global_answers"], [{"question": "Notice period?", "answer": "2 weeks"}])
        self.assertEqual(snapshot["resume"], {"path": str(self.resume), "name": "resume.pdf",
                                              "sha256": hashlib.sha256(self.resume.read_bytes()).hexdigest()})

    def test_a_flag_agreed_under_older_wording_shows_as_off_in_the_frozen_consent(self):
        self.conn.execute("UPDATE profile SET consent_tos_version = '2026-10-17'")
        (prepared,) = self.add_prepared(1)

        batch_id = local_batch.approve([prepared], [])["batch_id"]

        self.assertEqual(local_batch.snapshot(batch_id)["consent"]["auto_consent_authorized"], 0)

    def test_snapshot_of_a_missing_batch_raises(self):
        with self.assertRaises(LookupError):
            local_batch.snapshot(None)
        with self.assertRaises(LookupError):
            local_batch.snapshot(999)


class PreparedBatchTests(_BatchCase):
    def test_jobs_carry_repost_evidence_pay_and_the_eta_inputs(self):
        self.add_profile()
        earlier = self.conn.execute(
            "INSERT INTO listings (source, external_id, url, title, company, company_key, first_seen_at) "
            "VALUES ('greenhouse', 'old', 'https://boards.greenhouse.io/acme/jobs/9', 'Engineer', 'Acme', 'acme', "
            "'2026-09-01T00:00:00.000Z')").lastrowid
        plain, flagged = self.add_prepared(2)
        self.conn.execute("UPDATE listings SET title = 'Backend Engineer', company = 'Acme', location = 'Austin, TX', "
                          "salary_min = 120000, salary_max = 150000, salary_currency = 'USD' "
                          "WHERE id = (SELECT listing_id FROM job_applications WHERE id = ?)", (plain,))
        self.conn.execute("UPDATE job_applications SET repost_match = 'strong', repost_of_listing_id = ? WHERE id = ?",
                          (earlier, flagged))
        self.add_row(status="pending", approved_at="2026-10-01T00:00:00.000Z")

        batch = local_batch.prepared_batch()

        first, second = batch["jobs"]
        self.assertEqual((first["id"], first["title"], first["location"], first["repost_match"]),
                         (plain, "Backend Engineer", "Austin, TX", None))
        self.assertEqual(first["pay"], {"min": 120000.0, "max": 150000.0, "currency": "USD"})
        self.assertIsNone(second["pay"])
        self.assertEqual(second["repost_match"], {
            "kind": "strong", "title": "Engineer", "company": "Acme",
            "url": "https://boards.greenhouse.io/acme/jobs/9", "first_seen_at": "2026-09-01T00:00:00.000Z"})
        self.assertEqual(batch["inputs"]["profile"]["email"], "jane@example.com")
        self.assertEqual(batch["eta"]["queued"], {"greenhouse": 1, "lever": 0, "ashby": 0})
        # one application (60 s) plus the mean cooldown: floor 60 + half of the 120 s spread
        self.assertEqual(batch["eta"]["seconds_per_application"], 60 + 60 + 60)

    def test_inputs_are_none_before_a_profile_exists(self):
        self.assertIsNone(local_batch.prepared_batch()["inputs"])


@unittest.skipUnless(tla._HAVE_BROWSER, "playwright not installed")
class FillFromSnapshotTests(_BatchCase):
    async def asyncSetUp(self):
        self.playwright = await tla.async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.real = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.real.close)

    PHONE = '<label for="phone">Phone *</label><input id="phone" name="phone" required>'

    async def test_a_profile_edit_after_approval_is_not_sent(self):
        self.add_profile(phone="555-0100")
        (prepared,) = self.add_prepared(1)
        local_batch.approve([prepared], [])
        self.conn.execute("UPDATE profile SET phone = '555-9999' WHERE id = 1")

        browser = tla._RoutedBrowser(self.real, {tla.GH_URL: tla._gh(self.PHONE), tla.GH_DONE: tla.THANKS})
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))

        row = self.row(prepared)
        self.assertEqual(row["status"], "applied")
        self.assertEqual(json.loads(row["fields_used"])["filled"]["Phone *"], "555-0100")

    async def test_a_pending_row_without_a_batch_is_never_filled(self):
        self.add_profile()
        app_id = self.add_row(status="pending", approved_at="2026-10-01T00:00:00.000Z", approval_batch_id=None)

        browser = tla._RoutedBrowser(self.real, {tla.GH_URL: tla._gh(), tla.GH_DONE: tla.THANKS})
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))

        row = self.row(app_id)
        self.assertEqual(row["status"], "pending")      # settled like any exception: back for another try
        self.assertIn("LookupError", row["error"])
        self.assertEqual(browser.requests, [])


class ResumeRetentionTests(_BatchCase):
    FIELDS = {"email": "jane@example.com", "full_name": "Jane Doe"}

    def resume_files(self):
        return sorted(path.name for path in (db.data_dir() / "resumes").glob("*.pdf"))

    def age_resumes(self):
        """Back-date every resume file: the sweep leaves files under a minute old alone."""
        for path in (db.data_dir() / "resumes").glob("*.pdf"):
            os.utime(path, (1_700_000_000, 1_700_000_000))

    def test_an_old_resume_is_kept_while_a_batch_needs_it_and_deleted_after(self):
        local_profile.save_profile(self.FIELDS, PDF_ONE)
        old_name = f"{hashlib.sha256(PDF_ONE).hexdigest()}.pdf"
        (prepared,) = self.add_prepared(1)
        local_batch.approve([prepared], [])

        local_profile.save_profile(self.FIELDS, PDF_TWO)
        new_name = f"{hashlib.sha256(PDF_TWO).hexdigest()}.pdf"
        self.assertEqual(self.resume_files(), sorted([old_name, new_name]))

        self.conn.execute("UPDATE job_applications SET status = 'applied' WHERE id = ?", (prepared,))
        self.assertEqual(local_profile.sweep_resumes(min_age_s=60), 0)      # the periodic sweep spares a fresh file
        self.age_resumes()
        self.assertEqual(local_profile.sweep_resumes(min_age_s=60), 1)
        self.assertEqual(self.resume_files(), [new_name])

    def test_a_blocked_application_still_needs_its_resume(self):
        local_profile.save_profile(self.FIELDS, PDF_ONE)
        (prepared,) = self.add_prepared(1)
        local_batch.approve([prepared], [])
        self.conn.execute("UPDATE job_applications SET status = 'blocked_needs_input', blocked_reason = 'question' "
                          "WHERE id = ?", (prepared,))

        local_profile.save_profile(self.FIELDS, PDF_TWO)
        local_profile.sweep_resumes()

        self.assertEqual(len(self.resume_files()), 2)

    def test_with_no_batch_the_previous_resume_goes_at_once(self):
        local_profile.save_profile(self.FIELDS, PDF_ONE)
        local_profile.save_profile(self.FIELDS, PDF_TWO)
        self.assertEqual(self.resume_files(), [f"{hashlib.sha256(PDF_TWO).hexdigest()}.pdf"])


if __name__ == "__main__":
    unittest.main()
