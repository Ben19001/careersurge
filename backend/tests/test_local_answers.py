"""Saved answers, the label cache, the Jev mapper and classifier, and unblocking.

Runs against a throwaway data dir (`CAREERSURGE_DATA_DIR`, via test_local_apply's base class). Jev is
faked by rebinding `jev.choose`, the services' one seam; no request leaves the machine. The last
class drives a REAL Chromium over in-memory pages, like test_local_apply, for the block, answer,
requeue and fill round trip.
"""
import datetime
import json
import os
import sqlite3
import stat
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import test_local_apply as tla  # noqa: E402 - its store-backed base class and routed-browser helpers

from app.services import jev, local_answers, local_apply, local_formfill  # noqa: E402
from app.store import db  # noqa: E402


def fake_choose(picks, calls=None):
    """A `jev.choose` that answers `picks[question_id] = (option substring, confidence)`.

    An id with no entry answers `none`. `calls` collects each request's question ids, so a test can
    assert that Jev was (or was not) asked.
    """
    def choose(state, questions):
        if calls is not None:
            calls.append(sorted(questions))
        result = {}
        for question_id, (_, options) in questions.items():
            if question_id not in picks:
                result[question_id] = (None, 0.95)
                continue
            wanted, confidence = picks[question_id]
            result[question_id] = (next(o for o in options if wanted in o), confidence)
        return result
    return choose


def unavailable(state, questions):
    raise jev.JevUnavailable("network")


class _AnswersCase(tla._StoreTestCase):
    def setUp(self):
        super().setUp()
        self.jev = mock.patch.object(jev, "choose", unavailable)
        self.jev.start()
        self.addCleanup(self.jev.stop)

    def add_listing(self, company_key="acme", company="Acme"):
        self._next += 1
        return self.conn.execute(
            "INSERT INTO listings (source, external_id, url, company, company_key) "
            "VALUES ('greenhouse', ?, ?, ?, ?)", (str(self._next), tla.GH_URL, company, company_key)).lastrowid

    def add_answer(self, question, answer, scope="global", listing_id=None, company_key=None, answered=1):
        return self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, "
            "company_key, listing_id, answer_value, is_answered) VALUES (?, ?, 'stable_fact', ?, ?, ?, ?, ?)",
            (question, local_answers.local_profile._normalize_question(question), scope, company_key, listing_id,
             answer, answered)).lastrowid

    def block(self, app_id, *qna_ids, status_reason="question"):
        self.conn.execute("UPDATE job_applications SET status = 'blocked_needs_input', blocked_reason = ? "
                          "WHERE id = ?", (status_reason, app_id))
        for qna_id in qna_ids:
            self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id) VALUES (?, ?)", (app_id, qna_id))

    def record(self, app_id, classified, shapes=None, whole_form=False):
        """Run `record_block` the way `local_apply` does: in a transaction of its own."""
        conn = db.get_db()
        conn.execute("BEGIN IMMEDIATE")
        local_answers.record_block(conn, {"id": app_id}, self.listing_of(app_id), classified, shapes,
                                   whole_form=whole_form)
        conn.execute("COMMIT")
        conn.close()

    def listing_of(self, app_id):
        return dict(self.conn.execute(
            "SELECT l.* FROM listings l JOIN job_applications a ON a.listing_id = l.id WHERE a.id = ?",
            (app_id,)).fetchone())


class FindAnswerTests(_AnswersCase):
    def find(self, question, listing_id, company_key="acme", company="Acme"):
        return local_answers.find_answer(question, company, company_key, listing_id)

    def test_the_narrowest_scope_wins(self):
        listing = self.add_listing()
        self.add_answer("Why this role?", "global answer")
        self.assertEqual(self.find("Why this role?", listing), "global answer")
        self.add_answer("Why this role?", "company answer", scope="company", company_key="acme")
        self.assertEqual(self.find("Why this role?", listing), "company answer")
        self.add_answer("Why this role?", "job answer", scope="job", listing_id=listing)
        self.assertEqual(self.find("Why this role?", listing), "job answer")

    def test_a_job_or_company_answer_is_not_used_for_another_listing_or_company(self):
        mine, other_job, other_company = self.add_listing(), self.add_listing(), self.add_listing("globex", "Globex")
        self.add_answer("Why this role?", "job only", scope="job", listing_id=mine)
        self.add_answer("Describe a project", "acme only", scope="company", company_key="acme")
        self.assertIsNone(self.find("Why this role?", other_job))
        self.assertEqual(self.find("Describe a project", other_job), "acme only")
        self.assertIsNone(self.find("Describe a project", other_company, "globex", "Globex"))

    def test_an_unanswered_row_is_not_an_answer(self):
        listing = self.add_listing()
        self.add_answer("Why this role?", None, answered=0)
        self.assertIsNone(self.find("Why this role?", listing))

    def test_the_near_match_reuses_a_reworded_work_authorization_question(self):
        listing = self.add_listing()
        self.add_answer("Are you legally authorized to work in the U.S.?", "Yes")
        for asked in ("Are you authorized to work in the US?", "Are you authorized to work in the United States?",
                      "Are you legally authorized to work in the U.S.?*"):
            with self.subTest(asked=asked):
                self.assertEqual(self.find(asked, listing), "Yes")

    def test_a_different_country_number_or_office_is_a_different_question(self):
        listing = self.add_listing()
        self.add_answer("Are you authorized to work in Canada?", "Yes")
        self.add_answer("Do you have 3 years of Python experience?", "Yes")
        self.add_answer("Can you work from our London office 5 days a week?", "Yes")
        for asked in ("Are you authorized to work in the US?", "Are you authorized to work in the UK?",
                      "Do you have 5 years of Python experience?",
                      "Can you work from our US office three days a week?"):
            with self.subTest(asked=asked):
                self.assertIsNone(self.find(asked, listing))
        self.add_answer("Are you authorized to work in the UK?", "No")
        self.assertIsNone(self.find("Are you authorized to work in the US?", listing))

    def test_a_hit_stamps_last_used_at(self):
        listing = self.add_listing()
        qna_id = self.add_answer("Why this role?", "Because")
        self.find("Why this role?", listing)
        used = self.conn.execute("SELECT last_used_at FROM application_qna_bank WHERE id = ?", (qna_id,)).fetchone()[0]
        self.assertIsNotNone(used)


class MapLabelsTests(_AnswersCase):
    LABEL = "Where do you reside?"

    def map(self, picks, calls=None, labels=None):
        with mock.patch.object(jev, "choose", fake_choose(picks, calls)):
            return local_answers.map_labels(labels or [self.LABEL])

    def cached(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT normalized_label, profile_key, source, confidence FROM label_mappings")]

    def test_a_confident_pick_is_used_and_cached_as_jev(self):
        mapped = self.map({"0": ("current_location", 0.95)})
        self.assertEqual(mapped, {self.LABEL: "current_location"})
        self.assertEqual(self.cached(), [("where do you reside", "current_location", "jev", 0.95)])

    def test_a_pick_below_the_floor_none_or_an_unavailable_jev_leaves_the_label_missing(self):
        self.assertEqual(self.map({"0": ("current_location", 0.5)}), {})
        self.assertEqual(self.map({}), {})                   # Jev answers "none"
        self.assertEqual(local_answers.map_labels([self.LABEL]), {})   # Jev unavailable
        self.assertEqual(self.cached(), [])

    def test_a_cached_label_needs_no_second_jev_call(self):
        calls = []
        self.map({"0": ("current_location", 0.95)}, calls)
        again = self.map({}, calls, labels=["where do you RESIDE"])
        self.assertEqual(again, {"where do you RESIDE": "current_location"})
        self.assertEqual(len(calls), 1)

    def test_the_options_are_the_scalar_columns_the_alias_table_targets(self):
        scalar = {key for _, key in local_formfill._LABEL_ALIASES} - {"portfolio_links"}
        self.assertEqual(set(local_answers._PROFILE_KEYS), scalar)


class ClassifyAndRecordBlockTests(_AnswersCase):
    def test_jev_picks_type_and_scope_and_a_wide_scope_needs_confidence(self):
        picks = {"type0": ("rarely changes", 0.7), "scope0": ("every application", 0.95),
                 "type1": ("written for one employer", 0.7), "scope1": ("any job at this company", 0.95),
                 "type2": ("certifies", 0.7), "scope2": ("every application", 0.6)}
        with mock.patch.object(jev, "choose", fake_choose(picks)):
            classified = local_answers.classify_questions(["Q0", "Q1", "Q2"], "Acme")
        self.assertEqual(classified, {"Q0": ("stable_fact", "global"), "Q1": ("open_ended", "company"),
                                      "Q2": ("attestation", "job")})

    def test_an_attestation_is_job_scope_even_when_jev_is_sure_it_applies_to_every_application(self):
        picks = {"type0": ("certifies", 0.9), "scope0": ("every application", 0.95)}
        with mock.patch.object(jev, "choose", fake_choose(picks)):
            classified = local_answers.classify_questions(["I certify this is accurate"], "Acme")
        self.assertEqual(classified, {"I certify this is accurate": ("attestation", "job")})

    def test_an_unavailable_jev_gives_job_scope_and_open_ended(self):
        self.assertEqual(local_answers.classify_questions(["Q0"], "Acme"), {"Q0": ("open_ended", "job")})

    def test_a_repeat_block_of_the_whole_form_drops_the_questions_it_no_longer_misses(self):
        # Seen on a real Ashby form: a Yes/No the app now fills kept its first card (the question text as its only
        # option), and that unanswered row held the job blocked after the real questions were answered.
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="question")
        yes_no = "Can you work on-site five days a week?"
        self.record(app_id, {yes_no: ("open_ended", "job"), "Start date?": ("open_ended", "job")},
                    {yes_no: {"kind": "single", "options": [yes_no]}})
        self.record(app_id, {"Start date?": ("open_ended", "job")}, whole_form=True)
        linked = self.conn.execute(
            "SELECT q.original_question FROM application_blocking_questions b "
            "JOIN application_qna_bank q ON q.id = b.qna_id WHERE b.application_id = ?", (app_id,)).fetchall()
        self.assertEqual([row[0] for row in linked], ["Start date?"])
        self.assertEqual([q["question"] for q in local_answers.list_blocked()[0]["questions"]], ["Start date?"])

    def test_a_single_question_record_keeps_the_jobs_other_links(self):
        app_id = self.add_row()
        self.record(app_id, {"Why us?": ("open_ended", "job"), "Start date?": ("open_ended", "job")})
        self.record(app_id, {"Why us?": ("open_ended", "job")})          # the rejected-answer path
        count = self.conn.execute("SELECT COUNT(*) FROM application_blocking_questions WHERE application_id = ?",
                                  (app_id,)).fetchone()[0]
        self.assertEqual(count, 2)

    def test_a_block_links_one_row_per_question_and_reuses_the_same_question_at_its_scope(self):
        first, second = self.add_row(), self.add_row()
        classified = {"Why us?": ("open_ended", "job"), "Visa status?": ("stable_fact", "global")}
        for app_id in (first, second):
            conn = db.get_db()
            conn.execute("BEGIN IMMEDIATE")
            local_answers.record_block(conn, {"id": app_id}, self.listing_of(app_id), classified)
            conn.execute("COMMIT")
            conn.close()

        rows = self.conn.execute("SELECT normalized_question, scope, question_type, listing_id, company_key "
                                 "FROM application_qna_bank ORDER BY id").fetchall()
        # "Why us?" is job scope, so each listing gets its own row; "Visa status?" is global, so one.
        self.assertEqual([r["normalized_question"] for r in rows], ["why us", "visa status", "why us"])
        self.assertEqual({r["scope"] for r in rows}, {"job", "global"})
        links = self.conn.execute("SELECT application_id, COUNT(*) FROM application_blocking_questions "
                                  "GROUP BY application_id").fetchall()
        self.assertEqual([tuple(r) for r in links], [(first, 2), (second, 2)])

    def test_an_answered_row_that_blocks_again_is_kept_and_only_this_listing_gets_a_new_answer(self):
        mine, other = self.add_row(), self.add_row()
        qna_id = self.add_answer("Visa status?", "Yes")
        classified = {"Visa status?": ("stable_fact", "global")}

        self.record(mine, classified)

        # The global answer is untouched, and this listing is linked to a row of its own.
        self.assertEqual(tuple(self.conn.execute("SELECT is_answered, answer_value FROM application_qna_bank "
                                                 "WHERE id = ?", (qna_id,)).fetchone()), (1, "Yes"))
        (job,) = self.conn.execute("SELECT id, listing_id, is_answered FROM application_qna_bank "
                                   "WHERE scope = 'job'").fetchall()
        self.assertEqual((job["listing_id"], job["is_answered"]), (self.listing_of(mine)["id"], 0))
        links = self.conn.execute("SELECT qna_id FROM application_blocking_questions WHERE application_id = ?",
                                  (mine,)).fetchall()
        self.assertEqual([r["qna_id"] for r in links], [job["id"]])

        # Blocking again reuses that job row instead of adding another.
        self.record(mine, classified)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_qna_bank WHERE scope = 'job'").fetchone()[0], 1)

        # Only this listing uses the new answer: the other listing still gets the global one.
        self.conn.execute("UPDATE job_applications SET status = 'blocked_needs_input', "
                          "blocked_reason = 'question' WHERE id = ?", (mine,))
        local_answers.save_blocked_question_answer(job["id"], mine, "No")
        listing_mine, listing_other = self.listing_of(mine), self.listing_of(other)
        self.assertEqual(local_answers.find_answer("Visa status?", "Acme", listing_mine["company_key"],
                                                   listing_mine["id"]), "No")
        self.assertEqual(local_answers.find_answer("Visa status?", "Acme", listing_other["company_key"],
                                                   listing_other["id"]), "Yes")

    def test_a_company_answer_that_blocks_again_is_kept_too(self):
        app_id = self.add_row()
        listing = self.listing_of(app_id)
        qna_id = self.add_answer("Why us?", "Because", scope="company", company_key=listing["company_key"])
        self.record(app_id, {"Why us?": ("open_ended", "company")})
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (qna_id,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT scope FROM application_qna_bank WHERE is_answered = 0").fetchone()[0], "job")

    def test_an_answered_job_row_that_blocks_again_is_the_only_one_reopened(self):
        # Nothing else uses a job row and the unique index forbids a second one, so it is reopened.
        app_id = self.add_row()
        qna_id = self.add_answer("Why us?", "Because", scope="job", listing_id=self.listing_of(app_id)["id"])
        self.record(app_id, {"Why us?": ("open_ended", "job")})
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (qna_id,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_qna_bank").fetchone()[0], 1)

    def test_each_blocked_question_carries_the_shape_of_its_own_link(self):
        shapes = {
            "Anything else?": {"kind": "text", "options": [], "max_length": 40},
            "Team size?": {"kind": "single", "options": ["1-10", "11-50"], "max_length": None},
            "Which tools?": {"kind": "multi", "options": ["Python", "SQL", "Go"], "max_length": None},
            "Cover letter": {"kind": "file", "options": [], "max_length": None},
        }
        first, second = self.add_row(), self.add_row()
        classified = {q: ("open_ended", "global") for q in (*shapes, "No shape recorded")}
        self.record(first, classified, shapes)
        # A second form asks one of the same questions with a different control.
        self.record(second, {"Team size?": ("open_ended", "global")},
                    {"Team size?": {"kind": "text", "options": [], "max_length": 5}})
        for app_id in (first, second):
            self.conn.execute("UPDATE job_applications SET status = 'blocked_needs_input', "
                              "blocked_reason = 'question' WHERE id = ?", (app_id,))

        shape_of = {item["id"]: {q["question"]: q["field_shape"] for q in item["questions"]}
                    for item in local_answers.list_blocked()}
        self.assertEqual(shape_of[first], {**shapes, "No shape recorded": local_answers.DEFAULT_SHAPE})
        self.assertEqual(shape_of[second], {"Team size?": {"kind": "text", "options": [], "max_length": 5}})

    def test_each_jev_scope_decides_where_the_answer_is_reused(self):
        cases = {"only this job": (True, False, False), "any job at this company": (True, True, False),
                 "every application": (True, True, True)}
        for scope_text, expected in cases.items():
            with self.subTest(scope=scope_text):
                app_id = self.add_row()
                same_company, other_company = self.add_listing(), self.add_listing("globex", "Globex")
                with mock.patch.object(jev, "choose", fake_choose({"scope0": (scope_text, 0.95)})):
                    classified = local_answers.classify_questions([f"Why {scope_text}?"], "Acme")
                conn = db.get_db()
                conn.execute("BEGIN IMMEDIATE")
                local_answers.record_block(conn, {"id": app_id}, self.listing_of(app_id), classified)
                conn.execute("COMMIT")
                conn.close()
                qna_id = self.conn.execute("SELECT MAX(id) FROM application_qna_bank").fetchone()[0]
                self.conn.execute("UPDATE job_applications SET status = 'blocked_needs_input', "
                                  "blocked_reason = 'question' WHERE id = ?", (app_id,))
                local_answers.save_blocked_question_answer(qna_id, app_id, "my answer")

                question = f"Why {scope_text}?"
                found = (local_answers.find_answer(question, "Acme", "acme", self.listing_of(app_id)["id"]),
                         local_answers.find_answer(question, "Acme", "acme", same_company),
                         local_answers.find_answer(question, "Globex", "globex", other_company))
                self.assertEqual(tuple(f == "my answer" for f in found), expected)


class UnblockTests(_AnswersCase):
    def blocked_app(self, *questions, shot=None, reason="question", **columns):
        app_id = self.add_row(status="pending", screenshot_path=str(shot) if shot else None, **columns)
        qna_ids = [self.add_answer(q, None, scope="job", listing_id=self.listing_of(app_id)["id"], answered=0)
                   for q in questions]
        self.block(app_id, *qna_ids, status_reason=reason)
        return app_id, qna_ids

    def shot(self, name="1-1.png"):
        folder = db.data_dir() / "screenshots"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(b"png")
        return path

    def test_answering_one_of_two_stays_blocked_and_the_last_requeues(self):
        shot = self.shot()
        app_id, (first, second) = self.blocked_app("Q one?", "Q two?", shot=shot)

        self.assertEqual(local_answers.save_blocked_question_answer(first, app_id, "A"), [])
        self.assertEqual(self.row(app_id)["status"], "blocked_needs_input")
        self.assertEqual(local_answers.save_blocked_question_answer(second, app_id, " B "), [app_id])

        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"], row["screenshot_path"]), ("pending", None, None))
        self.assertEqual(self.conn.execute("SELECT answer_value, is_answered FROM application_qna_bank WHERE id = ?",
                                           (second,)).fetchone()[:], ("B", 1))
        self.assertFalse(shot.exists())

    def test_one_answer_releases_every_application_waiting_on_it(self):
        one, (qna_id,) = self.blocked_app("Shared?")
        other = self.add_row(status="pending")
        self.block(other, qna_id)
        self.assertEqual(sorted(local_answers.save_blocked_question_answer(qna_id, one, "A")), sorted([one, other]))

    def test_an_answer_does_not_move_a_row_blocked_for_another_reason(self):
        waiting, (qna_id,) = self.blocked_app("Q?")
        exhausted = self.add_row(status="pending")
        self.block(exhausted, qna_id, status_reason="attempts_exhausted")
        self.assertEqual(local_answers.save_blocked_question_answer(qna_id, waiting, "A"), [waiting])
        self.assertEqual(self.row(exhausted)["blocked_reason"], "attempts_exhausted")

    def test_an_application_that_is_not_waiting_on_the_question_is_refused_and_nothing_is_saved(self):
        waiting, (qna_id,) = self.blocked_app("Q?")
        exhausted = self.add_row(status="pending")
        self.block(exhausted, qna_id, status_reason="attempts_exhausted")
        unlinked = self.add_row(status="pending")
        for application_id in (exhausted, unlinked, 999):
            with self.subTest(application_id=application_id):
                with self.assertRaises(ValueError) as caught:
                    local_answers.save_blocked_question_answer(qna_id, application_id, "A")
                self.assertIn("is not waiting on question", str(caught.exception))
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (qna_id,)).fetchone()[0], 0)
        self.assertEqual(self.row(waiting)["status"], "blocked_needs_input")

    def test_an_empty_or_unknown_answer_is_refused(self):
        with self.assertRaises(ValueError):
            local_answers.save_blocked_question_answer(1, 1, "  ")
        with self.assertRaises(LookupError):
            local_answers.save_blocked_question_answer(999, 1, "x")

    def test_verify_submit_decisions(self):
        cases = {"confirmed": ("applied", None), "retry": ("pending", None), "skip": ("cancelled", "user_skipped")}
        for action, (status, skip_reason) in cases.items():
            with self.subTest(action=action):
                shot = self.shot(f"{action}.png")
                app_id, _ = self.blocked_app(shot=shot, reason="verify_submit", error="form_rejected")
                self.assertTrue(local_answers.resolve_verify_submit(app_id, action))
                row = self.row(app_id)
                self.assertEqual((row["status"], row["blocked_reason"], row["skip_reason"], row["error"]),
                                 (status, None, skip_reason, None))
                self.assertEqual(bool(row["confirmed_at"]), action == "confirmed")
                self.assertIsNone(row["screenshot_path"])
                self.assertFalse(shot.exists())
                self.assertFalse(local_answers.resolve_verify_submit(app_id, action))   # no longer blocked

    def test_an_unknown_action_and_a_row_blocked_for_a_question_are_refused(self):
        app_id, _ = self.blocked_app("Q?")
        with self.assertRaises(ValueError):
            local_answers.resolve_verify_submit(app_id, "maybe")
        self.assertFalse(local_answers.resolve_verify_submit(app_id, "confirmed"))
        self.assertEqual(self.row(app_id)["status"], "blocked_needs_input")

    def test_retry_exhausted_grants_exactly_one_more_attempt(self):
        app_id, _ = self.blocked_app(reason="attempts_exhausted", attempt_count=3)
        self.assertTrue(local_answers.retry_exhausted(app_id))
        row = self.row(app_id)
        self.assertEqual((row["status"], row["blocked_reason"], row["attempt_count"]), ("pending", None, 2))
        self.assertFalse(local_answers.retry_exhausted(app_id))

    def test_a_blocked_question_expires_after_72_hours_but_verify_submit_never_does(self):
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=73)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        shot = self.shot()
        question, _ = self.blocked_app("Q?", shot=shot, updated_at=old)
        verify, _ = self.blocked_app(reason="verify_submit", updated_at=old)
        fresh, _ = self.blocked_app("Q?")

        self.assertEqual(local_answers.expire_blocked(), 1)

        row = self.row(question)
        self.assertEqual((row["status"], row["blocked_reason"], row["skip_reason"], row["error"]),
                         ("failed", None, "timeout", "user_input_timeout"))
        self.assertFalse(shot.exists())
        self.assertEqual(self.row(verify)["blocked_reason"], "verify_submit")
        self.assertEqual(self.row(fresh)["blocked_reason"], "question")

    def manual_card(self, **columns):
        return self.add_row(status="blocked_needs_input", blocked_reason="verify_submit", error="manual_apply", **columns)

    def test_a_captcha_and_a_manual_card_list_the_answers_with_a_file_as_its_name(self):
        self.add_profile()
        resume = db.data_dir() / "resumes" / "Jane_Resume.pdf"
        resume.parent.mkdir(exist_ok=True)
        resume.write_bytes(b"%PDF-1.4")
        filled = json.dumps({"filled": {"First name": "Ana", "Resume": str(resume), "Notes": "LICENSE"}})
        captcha = self.add_row(status="blocked_needs_input", blocked_reason="captcha", fields_used=filled)
        manual = self.manual_card(fields_used=filled)
        ordinary = self.add_row(status="blocked_needs_input", blocked_reason="verify_submit", fields_used=filled)
        bare = self.add_row(status="blocked_needs_input", blocked_reason="captcha")

        cards = {card["id"]: card for card in local_answers.list_blocked()}
        expected = [{"label": "First name", "value": "Ana", "is_file": False},
                    {"label": "Resume", "value": "Jane_Resume.pdf", "is_file": True},
                    {"label": "Notes", "value": "LICENSE", "is_file": False}]
        self.assertEqual(cards[captcha]["answers"], expected)
        self.assertEqual(cards[manual]["answers"], expected)
        self.assertEqual(cards[ordinary]["answers"], [])
        self.assertEqual(cards[bare]["answers"], [])

    def test_a_network_path_in_the_answers_is_shown_as_text_and_never_looked_up(self):
        # Option text comes from the employer's form, and on Windows asking whether
        # `\\host\share\x` is a file connects to that host with the user's login hash. Only a path directly in
        # the app's attachments or resumes folder is looked up, so a real local file elsewhere is text too.
        self.add_profile()
        elsewhere = self.tmp / "private" / "Jane_Resume.pdf"
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(b"%PDF-1.4")
        network_paths = ["\\\\example.invalid\\share\\Decline", "//example.invalid/share/Decline",
                         "\\/example.invalid/share/Decline", "\\??\\UNC\\example.invalid\\share\\Decline",
                         "\\\\?\\UNC\\example.invalid\\share\\Decline", "\\??\\C:\\Windows\\win.ini",
                         "C:\\Windows\\win.ini", "/net/example.invalid/share/Decline", str(elsewhere),
                         str(db.data_dir() / "resumes" / ".." / "careersurge.db")]
        filled = {f"Q{index}": value for index, value in enumerate(network_paths)}
        captcha = self.add_row(status="blocked_needs_input", blocked_reason="captcha",
                               fields_used=json.dumps({"filled": filled}))

        with mock.patch.object(local_answers.os.path, "isfile", side_effect=AssertionError("looked up")):
            cards = {card["id"]: card for card in local_answers.list_blocked()}

        self.assertEqual(cards[captcha]["answers"],
                         [{"label": label, "value": value, "is_file": False} for label, value in filled.items()])

    def test_confirmed_on_a_manual_card_applies_it(self):
        app_id = self.manual_card()
        self.assertTrue(local_answers.resolve_verify_submit(app_id, "confirmed"))
        row = self.row(app_id)
        self.assertEqual((row["status"], row["error"]), ("applied", None))
        self.assertTrue(row["confirmed_at"])

    def test_not_applied_returns_a_manual_card_to_captcha_and_refuses_an_ordinary_one(self):
        manual = self.manual_card(screenshot_path="shot.png")
        ordinary = self.add_row(status="blocked_needs_input", blocked_reason="verify_submit", error="unknown")
        captcha = self.add_row(status="blocked_needs_input", blocked_reason="captcha")

        self.assertTrue(local_answers.resolve_verify_submit(manual, "not_applied"))
        row = self.row(manual)
        self.assertEqual((row["status"], row["blocked_reason"], row["error"], row["screenshot_path"]),
                         ("blocked_needs_input", "captcha", None, "shot.png"))
        with self.assertRaises(ValueError):
            local_answers.resolve_verify_submit(ordinary, "not_applied")
        self.assertEqual((self.row(ordinary)["blocked_reason"], self.row(ordinary)["error"]), ("verify_submit", "unknown"))
        self.assertFalse(local_answers.resolve_verify_submit(captcha, "not_applied"))
        self.assertFalse(local_answers.resolve_verify_submit(manual, "not_applied"))

    def test_a_manual_card_older_than_the_timeout_is_not_expired(self):
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=100)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        app_id = self.manual_card(updated_at=old)
        self.assertEqual(local_answers.expire_blocked(), 0)
        self.assertEqual(self.row(app_id)["blocked_reason"], "verify_submit")

    def test_recover_expires_blocked_rows_first(self):
        with mock.patch.object(local_answers, "expire_blocked") as expire:
            local_apply.recover()
        expire.assert_called_once_with()

    def test_list_blocked_shows_open_questions_only(self):
        shot = self.shot()
        app_id, (first, second) = self.blocked_app("Q one?", "Q two?", shot=shot, error="e")
        local_answers.save_blocked_question_answer(first, app_id, "A")
        (item,) = local_answers.list_blocked()
        self.assertEqual((item["id"], item["blocked_reason"], item["error"], item["has_screenshot"]),
                         (app_id, "question", "e", True))
        self.assertEqual([(q["qna_id"], q["question"], q["scope"]) for q in item["questions"]],
                         [(second, "Q two?", "job")])

    def test_a_stored_path_outside_the_screenshots_folder_is_never_served_or_deleted(self):
        outside = self.tmp / "secret.txt"
        outside.write_text("x")
        self.assertIsNone(local_answers.screenshot_file(str(outside)))
        self.assertIsNone(local_answers.screenshot_file(str(db.data_dir() / "screenshots" / ".." / "careersurge.db")))
        app_id, _ = self.blocked_app(reason="verify_submit", shot=outside)
        local_answers.resolve_verify_submit(app_id, "skip")
        self.assertTrue(outside.exists())


class AnswerShapeTests(_AnswersCase):
    """`save_blocked_question_answer` refuses an answer the control it was asked with would not take."""
    PDF = b"%PDF-1.4 a cover letter"

    def asking(self, shape):
        """A blocked job waiting on one question asked with `shape`; returns (app id, qna id)."""
        app_id = self.add_row(status="pending")
        qna_id = self.add_answer("Q?", None, scope="job", listing_id=self.listing_of(app_id)["id"], answered=0)
        self.block(app_id, qna_id)
        self.conn.execute("UPDATE application_blocking_questions SET field_shape = ? WHERE qna_id = ?",
                          (json.dumps(shape), qna_id))
        return app_id, qna_id

    def stored(self, qna_id):
        return tuple(self.conn.execute("SELECT answer_value, answer_filename, is_answered "
                                       "FROM application_qna_bank WHERE id = ?", (qna_id,)).fetchone())

    def refused(self, app_id, qna_id, value=None, upload=None, *, says):
        with self.assertRaises(ValueError) as caught:
            local_answers.save_blocked_question_answer(qna_id, app_id, value, upload)
        self.assertIn(says, str(caught.exception))
        self.assertEqual(self.stored(qna_id)[2], 0)          # nothing was saved

    def test_single_takes_one_of_the_options_and_the_refusal_names_them(self):
        app_id, qna_id = self.asking({"kind": "single", "options": ["Yes", "No"], "max_length": None})
        self.refused(app_id, qna_id, "Maybe", says="choose one of: Yes, No")
        self.refused(app_id, qna_id, ["Yes"], says="one answer, not a list")
        self.assertEqual(local_answers.save_blocked_question_answer(qna_id, app_id, " Yes "), [app_id])
        self.assertEqual(self.stored(qna_id), ("Yes", None, 1))

    def test_text_is_limited_to_its_max_length(self):
        app_id, qna_id = self.asking({"kind": "text", "options": [], "max_length": 10})
        self.refused(app_id, qna_id, "x" * 11, says="at most 10 characters; yours is 11")
        local_answers.save_blocked_question_answer(qna_id, app_id, "x" * 10)
        self.assertEqual(self.stored(qna_id)[0], "x" * 10)

    def test_multi_takes_a_list_of_offered_options_and_stores_a_json_list(self):
        app_id, qna_id = self.asking({"kind": "multi", "options": ["Python", "SQL", "Go"], "max_length": None})
        self.refused(app_id, qna_id, "Python", says="choose one or more of: Python, SQL, Go")
        self.refused(app_id, qna_id, ["Python", "Rust"], says="not offered: Rust; choose from: Python, SQL, Go")
        self.refused(app_id, qna_id, [], says="an answer cannot be empty")
        local_answers.save_blocked_question_answer(qna_id, app_id, ["Python", "SQL", "Python"])
        self.assertEqual(json.loads(self.stored(qna_id)[0]), ["Python", "SQL"])

    def test_a_link_with_no_recorded_shape_takes_plain_text_only(self):
        app_id, qna_id = self.asking({})
        self.refused(app_id, qna_id, ["a"], says="not a list")
        self.refused(app_id, qna_id, upload=("cv.pdf", self.PDF), says="not a file")
        local_answers.save_blocked_question_answer(qna_id, app_id, "anything")
        self.assertEqual(self.stored(qna_id)[0], "anything")

    def test_a_file_question_takes_a_pdf_doc_or_docx_up_to_1_mb(self):
        app_id, qna_id = self.asking({"kind": "file", "options": [], "max_length": None})
        self.refused(app_id, qna_id, "typed text", says="upload a file")
        for name, data in (("cv.exe", b"MZ\x90"), ("cv.txt", b"plain"), ("cv.pdf", b"not a pdf at all"),
                           ("cv.docx", b"%PDF-1.4 renamed"), ("", self.PDF)):
            with self.subTest(name=name):
                self.refused(app_id, qna_id, upload=(name, data), says="only PDF, DOC or DOCX files up to 1 MB")
        self.refused(app_id, qna_id, upload=("big.pdf", b"%PDF-" + b"0" * local_answers.UPLOAD_MAX_BYTES),
                     says="up to 1 MB")
        self.assertFalse((db.data_dir() / "attachments").exists())

    def test_an_upload_is_stored_0600_by_hash_with_its_original_name(self):
        import hashlib
        app_id, qna_id = self.asking({"kind": "file", "options": [], "max_length": None})
        upload = ("C:\\Users\\me\\My Letter.PDF", self.PDF)
        self.assertEqual(local_answers.save_blocked_question_answer(qna_id, app_id, upload=upload), [app_id])

        path, name, answered = self.stored(qna_id)
        expected = db.data_dir() / "attachments" / (hashlib.sha256(self.PDF).hexdigest() + ".pdf")
        self.assertEqual((path, name, answered), (str(expected), "My Letter.PDF", 1))
        self.assertEqual(expected.read_bytes(), self.PDF)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(expected.parent.stat().st_mode) & 0o077, 0)
        self.assertEqual(list(expected.parent.glob("*.partial")), [])

    def test_doc_and_docx_are_accepted_by_their_own_signature(self):
        for name, data in (("a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 body"), ("a.docx", b"PK\x03\x04 body")):
            with self.subTest(name=name):
                app_id, qna_id = self.asking({"kind": "file", "options": [], "max_length": None})
                local_answers.save_blocked_question_answer(qna_id, app_id, upload=(name, data))
                self.assertEqual(self.stored(qna_id)[1], name)

    def test_a_file_answer_is_found_only_as_a_file_and_a_text_answer_only_as_text(self):
        app_id, qna_id = self.asking({"kind": "file", "options": [], "max_length": None})
        listing = self.listing_of(app_id)
        local_answers.save_blocked_question_answer(qna_id, app_id, upload=("cv.pdf", self.PDF))
        self.add_answer("Typed?", "hello")
        ask = lambda question, **kw: local_answers.find_answer(  # noqa: E731
            question, "Acme", listing["company_key"], listing["id"], **kw)

        path = ask("Q?", file=True)
        self.assertEqual(path, self.stored(qna_id)[0])
        self.assertIsNone(ask("Q?"))                   # a stored path is never typed into a text box
        self.assertIsNone(ask("Typed?", file=True))
        self.assertEqual(ask("Typed?"), "hello")
        self.assertEqual(local_answers.attachment_payload(path)["name"], "cv.pdf")

    def test_attachment_payload_reads_only_inside_the_attachments_folder(self):
        outside = self.tmp / "passwd.pdf"
        outside.write_bytes(self.PDF)
        self.assertIsNone(local_answers.attachment_payload(str(outside)))
        self.assertIsNone(local_answers.attachment_payload(str(db.data_dir() / "attachments" / "gone.pdf")))


class SharedQuestionTests(_AnswersCase):
    """One wide-scope row waited on by forms that ask it with different controls (no deadlock)."""
    PDF = b"%PDF-1.4 a cover letter"
    ACME = {"kind": "single", "options": ["Yes", "No"], "max_length": None}
    BETA = {"kind": "single", "options": ["Yes, I have", "No, I have not"], "max_length": None}
    FILE = {"kind": "file", "options": [], "max_length": None}
    TEXT = {"kind": "text", "options": [], "max_length": 50}

    def waiting_on(self, qna_id, shape):
        """A new job blocked on the shared question `qna_id`, asked with `shape`; returns its id."""
        app_id = self.add_row(status="pending")
        self.block(app_id, qna_id)
        self.conn.execute("UPDATE application_blocking_questions SET field_shape = ? "
                          "WHERE application_id = ? AND qna_id = ?", (json.dumps(shape), app_id, qna_id))
        return app_id

    def link_of(self, app_id):
        """(scope, is_answered, answer_value, field_shape) of the one question this job is linked to."""
        row = self.conn.execute(
            "SELECT q.scope, q.is_answered, q.answer_value, b.field_shape, q.listing_id, q.question_type, q.id "
            "FROM application_blocking_questions b JOIN application_qna_bank q ON q.id = b.qna_id "
            "WHERE b.application_id = ?", (app_id,)).fetchone()
        return row

    def test_disjoint_single_options_each_job_is_answered_from_its_own_card(self):
        qna_id = self.add_answer("Have you worked for the government?", None, answered=0)
        acme, beta = self.waiting_on(qna_id, self.ACME), self.waiting_on(qna_id, self.BETA)

        self.assertEqual(local_answers.save_blocked_question_answer(qna_id, acme, "Yes"), [acme])

        self.assertEqual(self.row(acme)["status"], "pending")
        self.assertEqual(self.row(beta)["status"], "blocked_needs_input")
        shared = self.conn.execute("SELECT answer_value, is_answered FROM application_qna_bank WHERE id = ?",
                                   (qna_id,)).fetchone()
        self.assertEqual(tuple(shared), ("Yes", 1))
        # Beta now waits on its own unanswered job question, with its own options and the same wording.
        link = self.link_of(beta)
        self.assertEqual((link["scope"], link["is_answered"], link["listing_id"], link["question_type"]),
                         ("job", 0, self.listing_of(beta)["id"], "stable_fact"))
        self.assertEqual(json.loads(link["field_shape"]), self.BETA)
        (card,) = [item for item in local_answers.list_blocked() if item["id"] == beta]
        self.assertEqual([(q["question"], q["scope"], q["field_shape"]["options"]) for q in card["questions"]],
                         [("Have you worked for the government?", "job", ["Yes, I have", "No, I have not"])])

        # Answering Beta's card with one of ITS options releases it and leaves the shared answer alone.
        self.assertEqual(local_answers.save_blocked_question_answer(link["id"], beta, "No, I have not"), [beta])
        self.assertEqual(self.conn.execute("SELECT answer_value FROM application_qna_bank WHERE id = ?",
                                           (qna_id,)).fetchone()[0], "Yes")
        listing = self.listing_of(beta)
        self.assertEqual(local_answers.find_answer("Have you worked for the government?", "Acme",
                                                   listing["company_key"], listing["id"]), "No, I have not")

    def test_the_card_answered_on_must_take_the_answer_and_nothing_moves_when_it_does_not(self):
        qna_id = self.add_answer("Have you worked for the government?", None, answered=0)
        acme, beta = self.waiting_on(qna_id, self.ACME), self.waiting_on(qna_id, self.BETA)

        with self.assertRaises(ValueError) as caught:
            local_answers.save_blocked_question_answer(qna_id, beta, "Yes")

        self.assertIn("choose one of: Yes, I have, No, I have not", str(caught.exception))
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (qna_id,)).fetchone()[0], 0)
        self.assertEqual([self.link_of(acme)["id"], self.link_of(beta)["id"]], [qna_id, qna_id])

    def test_a_file_input_and_a_text_box_on_one_row_are_answered_separately(self):
        qna_id = self.add_answer("Cover letter?", None, answered=0)
        upload_form, paste_form = self.waiting_on(qna_id, self.FILE), self.waiting_on(qna_id, self.TEXT)

        requeued = local_answers.save_blocked_question_answer(qna_id, upload_form, upload=("cv.pdf", self.PDF))
        self.assertEqual(requeued, [upload_form])
        self.assertTrue(self.conn.execute("SELECT answer_filename FROM application_qna_bank WHERE id = ?",
                                          (qna_id,)).fetchone()[0])
        link = self.link_of(paste_form)
        self.assertEqual((link["scope"], link["is_answered"]), ("job", 0))
        self.assertEqual(json.loads(link["field_shape"]), self.TEXT)

        self.assertEqual(local_answers.save_blocked_question_answer(link["id"], paste_form, "Dear team"),
                         [paste_form])

    def test_a_typed_answer_moves_the_file_form_to_its_own_question(self):
        qna_id = self.add_answer("Cover letter?", None, answered=0)
        upload_form, paste_form = self.waiting_on(qna_id, self.FILE), self.waiting_on(qna_id, self.TEXT)

        self.assertEqual(local_answers.save_blocked_question_answer(qna_id, paste_form, "Dear team"), [paste_form])

        link = self.link_of(upload_form)
        self.assertEqual((link["scope"], link["is_answered"], json.loads(link["field_shape"])),
                         ("job", 0, self.FILE))
        self.assertEqual(self.row(upload_form)["status"], "blocked_needs_input")

    def test_jobs_whose_shapes_all_take_the_answer_are_all_released_and_keep_the_shared_row(self):
        qna_id = self.add_answer("Visa?", None, answered=0)
        one, other = self.waiting_on(qna_id, self.ACME), self.waiting_on(qna_id, self.ACME)
        self.assertEqual(sorted(local_answers.save_blocked_question_answer(qna_id, one, "No")), sorted([one, other]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_qna_bank WHERE scope = 'job'").fetchone()[0], 0)

    def test_a_moved_job_reuses_its_own_job_row_and_reopens_it_when_answered(self):
        qna_id = self.add_answer("Visa?", None, answered=0)
        acme, beta = self.waiting_on(qna_id, self.ACME), self.waiting_on(qna_id, self.BETA)
        mine = self.add_answer("Visa?", "stale", scope="job", listing_id=self.listing_of(beta)["id"], answered=1)

        local_answers.save_blocked_question_answer(qna_id, acme, "Yes")

        self.assertEqual(self.link_of(beta)["id"], mine)
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (mine,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_qna_bank WHERE scope = 'job'").fetchone()[0], 1)

    def test_a_moved_job_with_another_open_question_stays_blocked_on_both(self):
        qna_id = self.add_answer("Visa?", None, answered=0)
        acme, beta = self.waiting_on(qna_id, self.ACME), self.waiting_on(qna_id, self.BETA)
        extra = self.add_answer("Why us?", None, scope="job", listing_id=self.listing_of(beta)["id"], answered=0)
        self.block(beta, extra)

        local_answers.save_blocked_question_answer(qna_id, acme, "Yes")

        questions = self.conn.execute("SELECT q.original_question, q.scope FROM application_blocking_questions b "
                                      "JOIN application_qna_bank q ON q.id = b.qna_id WHERE b.application_id = ? "
                                      "ORDER BY q.id", (beta,)).fetchall()
        self.assertEqual([tuple(r) for r in questions], [("Why us?", "job"), ("Visa?", "job")])


class ScopeChoiceTests(_AnswersCase):
    """The card's "Reuse this answer for" choice re-scopes the row it answers."""
    QUESTION = "Are you authorized to work in the US?"

    def waiting_job(self, company_key="acme", question_type="stable_fact"):
        """A job blocked on its own unanswered `job` row for QUESTION; returns (application id, qna id)."""
        app_id = self.add_row(status="pending")
        listing_id = self.listing_of(app_id)["id"]
        self.conn.execute("UPDATE listings SET company_key = ? WHERE id = ?", (company_key, listing_id))
        qna_id = self.add_answer(self.QUESTION, None, scope="job", listing_id=listing_id, answered=0)
        self.conn.execute("UPDATE application_qna_bank SET question_type = ? WHERE id = ?", (question_type, qna_id))
        self.block(app_id, qna_id)
        return app_id, qna_id

    def shared_row(self, *app_ids, scope="global", company_key=None, keep_job_rows=False):
        """One unanswered wide row that every job in `app_ids` is linked to instead of its job row.

        The job rows are deleted unless `keep_job_rows`.
        """
        qna_id = self.add_answer(self.QUESTION, None, scope=scope, company_key=company_key, answered=0)
        for app_id in app_ids:
            self.conn.execute("DELETE FROM application_blocking_questions WHERE application_id = ?", (app_id,))
            if not keep_job_rows:
                self.conn.execute("DELETE FROM application_qna_bank WHERE scope = 'job' AND listing_id = ?",
                                  (self.listing_of(app_id)["id"],))
            self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id) VALUES (?, ?)",
                              (app_id, qna_id))
        return qna_id

    def bank(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT id, scope, company_key, listing_id, is_answered, answer_value FROM application_qna_bank "
            "ORDER BY id")]

    def test_widening_a_job_row_to_global_changes_it_in_place_and_releases_the_job(self):
        app_id, qna_id = self.waiting_job()

        self.assertEqual(local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="global"), [app_id])

        self.assertEqual(self.bank(), [(qna_id, "global", None, None, 1, "Yes")])
        self.assertEqual(self.row(app_id)["status"], "pending")
        # A second listing's fill now finds the answer, so it never raises a card.
        other = self.add_row(status="pending")
        listing = self.listing_of(other)
        self.assertEqual(local_answers.find_answer(self.QUESTION, "Acme", listing["company_key"], listing["id"]), "Yes")

    def test_widening_to_company_keeps_the_listings_company_key(self):
        app_id, qna_id = self.waiting_job(company_key="globex")
        local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="company")
        self.assertEqual(self.bank(), [(qna_id, "company", "globex", None, 1, "Yes")])

    def test_an_unanswered_row_at_the_target_takes_the_links_and_the_job_row_goes(self):
        app_id, qna_id = self.waiting_job()
        elsewhere, _ = self.waiting_job(company_key="globex")
        target = self.shared_row(elsewhere)
        self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id) VALUES (?, ?)",
                          (app_id, target))

        released = local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="global")

        self.assertCountEqual(released, [app_id, elsewhere])
        self.assertEqual([r[0] for r in self.bank() if r[1] == "global"], [target])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_qna_bank WHERE scope = 'job' "
                                           "AND listing_id = ?", (self.listing_of(app_id)["id"],)).fetchone()[0], 0)
        self.assertEqual(self.row(elsewhere)["status"], "pending")

    def test_a_job_waiting_on_its_own_row_is_not_released_by_a_widened_answer(self):
        app_id, qna_id = self.waiting_job()
        other, other_qna = self.waiting_job()

        local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="global")

        self.assertEqual(self.row(other)["status"], "blocked_needs_input")
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (other_qna,)).fetchone()[0], 0)

    def test_an_answered_row_at_the_target_refuses_and_names_the_saved_value(self):
        app_id, qna_id = self.waiting_job()
        self.add_answer(self.QUESTION, "No", scope="global")
        before = self.bank()

        with self.assertRaisesRegex(ValueError, r'"No".*This job only'):
            local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="global")

        self.assertEqual(self.bank(), before)
        self.assertEqual(self.row(app_id)["status"], "blocked_needs_input")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_blocking_questions "
                                           "WHERE qna_id = ?", (qna_id,)).fetchone()[0], 1)

    def test_company_on_a_listing_without_a_company_key_is_refused(self):
        app_id, qna_id = self.waiting_job(company_key="")
        with self.assertRaisesRegex(ValueError, "no company"):
            local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="company")
        self.assertEqual(self.bank()[0][1:5], ("job", None, self.listing_of(app_id)["id"], 0))

    def test_an_attestation_cannot_be_widened_but_can_name_its_own_scope(self):
        app_id, qna_id = self.waiting_job(question_type="attestation")
        for scope in ("company", "global"):
            with self.assertRaisesRegex(ValueError, "one employer"):
                local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope=scope)
        local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="job")
        self.assertEqual(self.bank()[0][1:], ("job", None, self.listing_of(app_id)["id"], 1, "Yes"))

    def test_an_unknown_scope_is_refused(self):
        app_id, qna_id = self.waiting_job()
        with self.assertRaisesRegex(ValueError, "scope must be one of"):
            local_answers.save_blocked_question_answer(qna_id, app_id, "Yes", scope="planet")

    def test_a_scope_change_from_a_card_that_is_not_waiting_is_refused_and_writes_nothing(self):
        _, qna_id = self.waiting_job()
        elsewhere, _ = self.waiting_job()
        before = self.bank()
        with self.assertRaisesRegex(ValueError, "is not waiting on question"):
            local_answers.save_blocked_question_answer(qna_id, elsewhere, "Yes", scope="global")
        self.assertEqual(self.bank(), before)

    def test_narrowing_a_row_only_this_card_uses_changes_it_in_place(self):
        app_id, _ = self.waiting_job()
        shared = self.shared_row(app_id)

        self.assertEqual(local_answers.save_blocked_question_answer(shared, app_id, "Yes", scope="job"), [app_id])

        self.assertEqual(self.bank()[-1], (shared, "job", None, self.listing_of(app_id)["id"], 1, "Yes"))

    def test_narrowing_with_other_jobs_linked_moves_only_this_cards_link(self):
        app_a, _ = self.waiting_job()
        app_b, _ = self.waiting_job()
        shared = self.shared_row(app_a, app_b)

        self.assertEqual(local_answers.save_blocked_question_answer(shared, app_a, "Yes", scope="job"), [app_a])

        mine = self.conn.execute("SELECT q.id, q.scope, q.listing_id, q.answer_value FROM application_blocking_questions b "
                                 "JOIN application_qna_bank q ON q.id = b.qna_id WHERE b.application_id = ?",
                                 (app_a,)).fetchone()
        self.assertEqual(tuple(mine)[1:], ("job", self.listing_of(app_a)["id"], "Yes"))
        self.assertNotEqual(mine["id"], shared)
        # B did not choose "this job only": it still waits on the shared row, which stays unanswered.
        self.assertEqual(self.row(app_b)["status"], "blocked_needs_input")
        self.assertEqual(self.conn.execute("SELECT qna_id FROM application_blocking_questions WHERE application_id = ?",
                                           (app_b,)).fetchone()[0], shared)
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (shared,)).fetchone()[0], 0)

    def test_a_global_row_narrowed_to_company_changes_in_place_when_only_this_card_uses_it(self):
        app_id, _ = self.waiting_job()
        shared = self.shared_row(app_id)

        self.assertEqual(local_answers.save_blocked_question_answer(shared, app_id, "Yes", scope="company"), [app_id])

        self.assertEqual(self.bank()[-1], (shared, "company", "acme", None, 1, "Yes"))

    def test_a_global_row_narrowed_to_company_with_other_jobs_linked_moves_only_this_cards_link(self):
        app_a, _ = self.waiting_job()
        app_b, _ = self.waiting_job()
        shared = self.shared_row(app_a, app_b)

        self.assertEqual(local_answers.save_blocked_question_answer(shared, app_a, "Yes", scope="company"), [app_a])

        mine = self.conn.execute("SELECT q.id, q.scope, q.company_key, q.answer_value FROM application_blocking_questions b "
                                 "JOIN application_qna_bank q ON q.id = b.qna_id WHERE b.application_id = ?",
                                 (app_a,)).fetchone()
        self.assertEqual(tuple(mine)[1:], ("company", "acme", "Yes"))
        self.assertEqual(self.row(app_b)["status"], "blocked_needs_input")
        self.assertEqual(self.conn.execute("SELECT qna_id FROM application_blocking_questions WHERE application_id = ?",
                                           (app_b,)).fetchone()[0], shared)
        self.assertEqual(self.conn.execute("SELECT scope, is_answered FROM application_qna_bank WHERE id = ?",
                                           (shared,)).fetchone()[:], ("global", 0))

    def test_a_global_row_narrowed_to_company_joins_an_unanswered_company_row_and_refuses_an_answered_one(self):
        app_a, _ = self.waiting_job()
        app_b, _ = self.waiting_job()
        shared = self.shared_row(app_a, app_b)
        company = self.add_answer(self.QUESTION, None, scope="company", company_key="acme", answered=0)

        self.assertEqual(local_answers.save_blocked_question_answer(shared, app_a, "Yes", scope="company"), [app_a])

        self.assertEqual(self.conn.execute("SELECT qna_id FROM application_blocking_questions WHERE application_id = ?",
                                           (app_a,)).fetchone()[0], company)
        self.assertEqual(self.conn.execute("SELECT answer_value FROM application_qna_bank WHERE id = ?",
                                           (company,)).fetchone()[0], "Yes")

        with self.assertRaisesRegex(ValueError, r'"Yes".*This job only'):
            local_answers.save_blocked_question_answer(shared, app_b, "No", scope="company")
        self.assertEqual(self.row(app_b)["status"], "blocked_needs_input")

    def test_a_wide_attestation_row_is_narrowed_to_this_job_when_answered(self):
        app_id, _ = self.waiting_job(question_type="attestation")
        shared = self.shared_row(app_id)
        self.conn.execute("UPDATE application_qna_bank SET question_type = 'attestation' WHERE id = ?", (shared,))

        local_answers.save_blocked_question_answer(shared, app_id, "Yes", scope="job")

        self.assertEqual(self.bank()[-1], (shared, "job", None, self.listing_of(app_id)["id"], 1, "Yes"))

    def test_narrowing_onto_an_unanswered_row_drops_the_source_row_this_card_alone_used(self):
        app_id, _ = self.waiting_job()
        shared = self.shared_row(app_id)
        company = self.add_answer(self.QUESTION, None, scope="company", company_key="acme", answered=0)

        local_answers.save_blocked_question_answer(shared, app_id, "Yes", scope="company")

        self.assertEqual(self.bank(), [(company, "company", "acme", None, 1, "Yes")])

    def test_narrowing_reopens_and_reuses_an_answered_job_row_of_the_same_listing(self):
        app_id, job_row = self.waiting_job()
        shared = self.shared_row(app_id, keep_job_rows=True)
        self.conn.execute("UPDATE application_qna_bank SET is_answered = 1, answer_value = 'old' WHERE id = ?", (job_row,))

        local_answers.save_blocked_question_answer(shared, app_id, "new", scope="job")

        self.assertEqual(self.conn.execute("SELECT answer_value, is_answered FROM application_qna_bank WHERE id = ?",
                                           (job_row,)).fetchone()[:], ("new", 1))
        self.assertEqual(self.row(app_id)["status"], "pending")

    def test_omitting_scope_keeps_the_scope_jev_set(self):
        app_id, qna_id = self.waiting_job()
        local_answers.save_blocked_question_answer(qna_id, app_id, "Yes")
        self.assertEqual(self.bank()[0][1:4], ("job", None, self.listing_of(app_id)["id"]))

    def test_list_blocked_carries_the_job_details_the_card_shows(self):
        app_id, _ = self.waiting_job()
        bare, _ = self.waiting_job(company_key="")
        self.conn.execute(
            "UPDATE listings SET location = 'Remote', responsibilities_summary = 'Build things', "
            "requirements_summary = '5 years', key_skills = '[\"Python\", \"SQL\"]' "
            "WHERE id = (SELECT listing_id FROM job_applications WHERE id = ?)", (app_id,))

        cards = {c["id"]: c for c in local_answers.list_blocked()}

        full = cards[app_id]
        self.assertEqual((full["url"], full["location"], full["ats"], full["has_company"]),
                         (tla.GH_URL, "Remote", "greenhouse", True))
        self.assertEqual((full["responsibilities"], full["requirements"], full["key_skills"]),
                         ("Build things", "5 years", ["Python", "SQL"]))
        self.assertEqual((cards[bare]["has_company"], cards[bare]["key_skills"], cards[bare]["responsibilities"]),
                         (False, [], None))


class MigrationTests(tla._StoreTestCase):
    def database_at(self, version):
        """Replace the migrated database with one built from the first `version` migration files."""
        self.conn.close()
        db.db_path().unlink()
        for leftover in db.db_path().parent.glob("careersurge.db-*"):
            leftover.unlink()
        raw = sqlite3.connect(db.db_path())
        for path in sorted((Path(db.__file__).parent / "migrations").glob("*.sql")):
            if int(path.name[:4]) <= version:
                raw.executescript(path.read_text())
        raw.execute(f"PRAGMA user_version = {version}")
        raw.close()
        self.conn = db.get_db()
        self.addCleanup(self.conn.close)

    def test_0004_on_a_v3_database_rebuilds_label_mappings_without_alias(self):
        self.database_at(3)
        self.assertEqual(db.migrate(), 9)
        self.conn.execute("INSERT INTO label_mappings VALUES ('a', 'email', 'jev', 0.9)")
        with self.assertRaises(Exception):
            self.conn.execute("INSERT INTO label_mappings VALUES ('b', 'email', 'alias', NULL)")

    def test_0005_takes_a_v4_database_to_v5_and_old_links_read_as_a_plain_text_box(self):
        self.database_at(4)
        listing_id = self.conn.execute(
            "INSERT INTO listings (source, external_id, url, company_key) "
            "VALUES ('greenhouse', '1', 'https://boards.greenhouse.io/a/jobs/1', 'acme')").lastrowid
        app_id = self.conn.execute(
            "INSERT INTO job_applications (listing_id, ats, status, blocked_reason) "
            "VALUES (?, 'greenhouse', 'blocked_needs_input', 'question')", (listing_id,)).lastrowid
        qna_id = self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope) "
            "VALUES ('Old?', 'old', 'open_ended', 'global')").lastrowid
        self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id) VALUES (?, ?)",
                          (app_id, qna_id))

        self.assertEqual(db.migrate(), 9)

        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 9)
        self.assertEqual(self.conn.execute("SELECT field_shape FROM application_blocking_questions").fetchone()[0], "{}")
        self.assertIsNone(self.conn.execute("SELECT answer_filename FROM application_qna_bank").fetchone()[0])
        (item,) = local_answers.list_blocked()
        self.assertEqual(item["questions"][0]["field_shape"], local_answers.DEFAULT_SHAPE)


    def test_0007_drops_the_hear_about_us_row_and_requeues_only_jobs_it_alone_blocked(self):
        self.database_at(6)
        hear = self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, "
            "answer_value, is_answered) VALUES ('How did you hear about us?', 'how did you hear about us', "
            "'stable_fact', 'global', 'LinkedIn', 1)").lastrowid
        other = self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope) "
            "VALUES ('Why us?', 'why us', 'open_ended', 'global')").lastrowid

        def blocked_job(external_id, *qna_ids, reason="question"):
            listing_id = self.conn.execute(
                "INSERT INTO listings (source, external_id, url, company_key) "
                "VALUES ('greenhouse', ?, 'https://boards.greenhouse.io/a/jobs/1', 'acme')", (external_id,)).lastrowid
            app_id = self.conn.execute(
                "INSERT INTO job_applications (listing_id, ats, status, blocked_reason) "
                "VALUES (?, 'greenhouse', 'blocked_needs_input', ?)", (listing_id, reason)).lastrowid
            for qna_id in qna_ids:
                self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id) VALUES (?, ?)",
                                  (app_id, qna_id))
            return app_id

        only_hear = blocked_job("1", hear)
        hear_and_other = blocked_job("2", hear, other)
        captcha = blocked_job("3", reason="captcha")

        self.assertEqual(db.migrate(), 9)

        status = {row["id"]: (row["status"], row["blocked_reason"])
                  for row in self.conn.execute("SELECT id, status, blocked_reason FROM job_applications")}
        self.assertEqual(status[only_hear], ("pending", None))
        self.assertEqual(status[hear_and_other], ("blocked_needs_input", "question"))
        self.assertEqual(status[captcha], ("blocked_needs_input", "captcha"))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM application_qna_bank WHERE id = ?", (hear,)).fetchone())
        links = self.conn.execute("SELECT application_id, qna_id FROM application_blocking_questions").fetchall()
        self.assertEqual([tuple(row) for row in links], [(hear_and_other, other)])
        listing = self.conn.execute("SELECT id FROM listings LIMIT 1").fetchone()[0]
        self.assertIsNone(local_answers.find_answer("How did you hear about Acme?", "Acme", "acme", listing))

    def test_0008_unanswers_a_question_answered_with_itself_and_requeues_the_jobs_it_blocked(self):
        self.database_at(7)
        question = "Are you related to any current employees?"

        def bank_row(text, answer, answered):
            return self.conn.execute(
                "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, "
                "answer_value, is_answered) VALUES (?, ?, 'stable_fact', 'global', ?, ?)",
                (text, text.lower(), answer, answered)).lastrowid

        def blocked_job(external_id, qna_id, options=None, reason="question"):
            listing_id = self.conn.execute(
                "INSERT INTO listings (source, external_id, url, company_key) "
                "VALUES ('ashby', ?, 'https://jobs.ashbyhq.com/a/1', 'acme')", (external_id,)).lastrowid
            app_id = self.conn.execute(
                "INSERT INTO job_applications (listing_id, ats, status, blocked_reason) "
                "VALUES (?, 'ashby', 'blocked_needs_input', ?)", (listing_id, reason)).lastrowid
            shape = json.dumps({"kind": "single", "options": options or [], "max_length": None})
            self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id, field_shape) "
                              "VALUES (?, ?, ?)", (app_id, qna_id, shape))
            return app_id

        bad_answer = bank_row(question, question, 1)
        bad_shape = bank_row("Do you need sponsorship?", None, 0)
        good_answer = bank_row("Notice period?", "Two weeks", 1)
        # Accepted cost: an acknowledgement box is answered with its own label too, so it is re-asked once.
        acknowledged = bank_row("I have read the privacy notice *", "I have read the privacy notice *", 1)
        two_options = bank_row("Remote only?", None, 0)
        by_answer = blocked_job("1", bad_answer)
        by_shape = blocked_job("2", bad_shape, ["Do you need sponsorship?"])
        normal_block = blocked_job("3", two_options, ["Yes", "No"])
        other_reason = blocked_job("4", bad_answer, reason="captcha")

        self.assertEqual(db.migrate(), 9)

        status = {row["id"]: (row["status"], row["blocked_reason"])
                  for row in self.conn.execute("SELECT id, status, blocked_reason FROM job_applications")}
        self.assertEqual(status[by_answer], ("pending", None))
        self.assertEqual(status[by_shape], ("pending", None))
        self.assertEqual(status[normal_block], ("blocked_needs_input", "question"))
        self.assertEqual(status[other_reason], ("blocked_needs_input", "captcha"))
        rows = {row["id"]: (row["is_answered"], row["answer_value"])
                for row in self.conn.execute("SELECT id, is_answered, answer_value FROM application_qna_bank")}
        self.assertEqual(rows[bad_answer], (0, None))
        self.assertEqual(rows[good_answer], (1, "Two weeks"))
        self.assertEqual(rows[acknowledged], (0, None))

    def test_0009_drops_lone_question_links_and_requeues_jobs_left_with_nothing_to_answer(self):
        # Seen on real Ashby jobs: a Yes/No card stored before the scrape read its buttons, which the fill now answers.
        self.database_at(8)
        question = "Are you able to work on-site five days per week?"

        def bank_row(text, answered=0):
            return self.conn.execute(
                "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, "
                "answer_value, is_answered) VALUES (?, ?, 'open_ended', 'global', ?, ?)",
                (text, text.lower(), "x" if answered else None, answered)).lastrowid

        def blocked_job(external_id, *links):
            listing_id = self.conn.execute(
                "INSERT INTO listings (source, external_id, url, company_key) "
                "VALUES ('ashby', ?, 'https://jobs.ashbyhq.com/a/1', 'acme')", (external_id,)).lastrowid
            app_id = self.conn.execute(
                "INSERT INTO job_applications (listing_id, ats, status, blocked_reason) "
                "VALUES (?, 'ashby', 'blocked_needs_input', 'question')", (listing_id,)).lastrowid
            for qna_id, options in links:
                shape = json.dumps({"kind": "single", "options": options, "max_length": None})
                self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id, field_shape) "
                                  "VALUES (?, ?, ?)", (app_id, qna_id, shape))
            return app_id

        stale = bank_row(question)
        start_date = bank_row("Earliest start date?")
        why_us = bank_row("Why us?", answered=1)
        yes_no = bank_row("Remote only?")
        still_asked = blocked_job("1", (stale, [question]), (start_date, []))
        only_stale = blocked_job("2", (stale, [question]), (why_us, []))
        good_card = blocked_job("3", (yes_no, ["Yes", "No"]))

        self.assertEqual(db.migrate(), 9)

        links = self.conn.execute(
            "SELECT application_id, qna_id FROM application_blocking_questions ORDER BY application_id, qna_id")
        self.assertEqual([tuple(row) for row in links],
                         [(still_asked, start_date), (only_stale, why_us), (good_card, yes_no)])
        status = {row["id"]: row["status"] for row in self.conn.execute("SELECT id, status FROM job_applications")}
        self.assertEqual(status, {still_asked: "blocked_needs_input", only_stale: "pending",
                                  good_card: "blocked_needs_input"})


@unittest.skipUnless(tla._HAVE_BROWSER, "playwright not installed")
class BlockAnswerFillRoundTripTests(_AnswersCase):
    """Against a real page: a block, the answer, the requeue and the fill that now succeeds."""
    EXTRA = '<label for="why">Why us? *</label><input id="why" name="why" required>'

    async def asyncSetUp(self):
        self.playwright = await tla.async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.real = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.real.close)
        self.add_profile()

    async def apply(self, pages):
        """Seed one pending row, claim it and apply it against `pages`; returns (id, routed browser)."""
        app_id = self.add_row()
        browser = tla._RoutedBrowser(self.real, pages)
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))
        return app_id, browser

    async def test_block_answer_requeue_and_fill(self):
        pages = {tla.GH_URL: tla._gh(self.EXTRA), tla.GH_DONE: tla.THANKS}
        picks = {"type0": ("written for one employer", 0.9), "scope0": ("only this job", 0.95)}
        with mock.patch.object(jev, "choose", fake_choose(picks)):
            app_id, _ = await self.apply(pages)
        self.assertEqual(self.row(app_id)["blocked_reason"], "question")
        (item,) = local_answers.list_blocked()
        self.assertEqual((item["questions"][0]["question"], item["questions"][0]["question_type"],
                          item["questions"][0]["scope"]), ("Why us? *", "open_ended", "job"))

        self.assertEqual(local_answers.save_blocked_question_answer(item["questions"][0]["qna_id"], app_id, "Great mission"), [app_id])
        self.assertEqual(self.row(app_id)["status"], "pending")

        browser = tla._RoutedBrowser(self.real, pages)
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))
        row = self.row(app_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("applied", 2))
        self.assertEqual(json.loads(row["fields_used"])["filled"]["Why us? *"], "Great mission")

    async def test_an_unknown_label_is_mapped_by_jev_then_served_from_the_cache(self):
        self.conn.execute("UPDATE profile SET current_location = 'Austin, TX' WHERE id = 1")
        self.freeze_profile()
        extra = '<label for="loc">Where do you reside? *</label><input id="loc" name="loc" required>'
        pages = {tla.GH_URL: tla._gh(extra), tla.GH_DONE: tla.THANKS}
        calls = []
        with mock.patch.object(jev, "choose", fake_choose({"0": ("current_location", 0.95)}, calls)):
            first, _ = await self.apply(pages)
            second, _ = await self.apply(pages)
        self.assertEqual([self.row(i)["status"] for i in (first, second)], ["applied", "applied"])
        self.assertEqual(calls, [["0"]])      # the second form was served from label_mappings
        self.assertEqual(json.loads(self.row(first)["fields_used"])["filled"]["Where do you reside? *"], "Austin, TX")

    async def test_a_low_confidence_mapping_leaves_the_field_missing_and_blocks(self):
        self.conn.execute("UPDATE profile SET current_location = 'Austin, TX' WHERE id = 1")
        self.freeze_profile()
        extra = '<label for="loc">Where do you reside? *</label><input id="loc" name="loc" required>'
        with mock.patch.object(jev, "choose", fake_choose({"0": ("current_location", 0.5)})):
            app_id, browser = await self.apply({tla.GH_URL: tla._gh(extra), tla.GH_DONE: tla.THANKS})
        self.assertEqual(self.row(app_id)["blocked_reason"], "question")
        self.assertEqual(browser.posts(), [])

    # One question per control kind, as a Greenhouse-style board renders them.
    SHAPES_FORM = """
      <label for="note">Anything else? *</label><input id="note" name="note" maxlength="40" required>
      <label for="size">Team size? *</label>
      <select id="size" name="size" required><option value=""></option><option>1-10</option><option>11-50</option></select>
      <fieldset><legend>Remote only? *</legend>
        <label><input type="radio" name="remote" value="y" required> Yes</label>
        <label><input type="radio" name="remote" value="n" required> No</label></fieldset>
      <fieldset><legend>Which tools? *</legend>
        <label><input type="checkbox" name="tools" value="py" required> Python</label>
        <label><input type="checkbox" name="tools" value="sql" required> SQL</label>
        <label><input type="checkbox" name="tools" value="go" required> Go</label></fieldset>
      <label for="cover">Cover letter *</label><input type="file" id="cover" name="cover" required>"""

    async def test_every_control_kind_is_scraped_into_its_field_shape(self):
        pages = {tla.GH_URL: tla._gh(self.SHAPES_FORM), tla.GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        self.assertEqual(self.row(app_id)["blocked_reason"], "question")

        (item,) = local_answers.list_blocked()
        shapes = {q["question"]: q["field_shape"] for q in item["questions"]}
        self.assertEqual(shapes, {
            "Anything else? *": {"kind": "text", "options": [], "max_length": 40},
            "Team size? *": {"kind": "single", "options": ["1-10", "11-50"], "max_length": None},
            "Remote only? *": {"kind": "single", "options": ["Yes", "No"], "max_length": None},
            "Which tools? *": {"kind": "multi", "options": ["Python", "SQL", "Go"], "max_length": None},
            "Cover letter *": {"kind": "file", "options": [], "max_length": None},
        })

    async def test_an_ashby_yes_no_card_offers_yes_and_no_and_the_answer_clicks_the_button(self):
        # Ashby: two toggle buttons over one hidden checkbox. The submit is refused unless the box is
        # ticked, so "applied" proves the Yes button was clicked and the box followed it.
        extra = """<label for="rel">Are you related to any current employees? *</label>
            <div><input id="rel" type="checkbox" style="display:none">
              <button type="button" aria-pressed="false">Yes</button>
              <button type="button" aria-pressed="false">No</button></div>
            <script>
              const box = document.getElementById('rel');
              document.querySelectorAll('button[aria-pressed]').forEach(b => b.addEventListener('click', () => {
                box.checked = b.innerText === 'Yes'; }));
              document.getElementById('submit_app').addEventListener('click', e => {
                if (!box.checked) { e.preventDefault(); const d = document.createElement('div');
                  d.setAttribute('role', 'alert'); d.textContent = 'not related?'; document.body.prepend(d); } });
            </script>"""
        pages = {tla.GH_URL: tla._gh(extra), tla.GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        (item,) = local_answers.list_blocked()
        question = item["questions"][0]
        self.assertEqual(question["field_shape"], {"kind": "single", "options": ["Yes", "No"], "max_length": None})

        self.assertEqual(local_answers.save_blocked_question_answer(question["qna_id"], app_id, "Yes"), [app_id])
        browser = tla._RoutedBrowser(self.real, pages)
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))

        row = self.row(app_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("applied", 2),
                         (row["blocked_reason"], row["error"], row["fields_used"]))

    async def test_a_multi_answer_ticks_each_row_it_names_and_only_those(self):
        # Reject the submit unless exactly Python and SQL are ticked, so "applied" proves the ticks.
        script = """<script>document.getElementById('submit_app').addEventListener('click', e => {
          const ticked = [...document.querySelectorAll('input[name=tools]:checked')].map(c => c.value).join();
          if (ticked !== 'py,sql') { e.preventDefault(); const d = document.createElement('div');
            d.setAttribute('role', 'alert'); d.textContent = 'wrong tools: ' + ticked; document.body.prepend(d); } });</script>"""
        # `required` on each box would make the browser demand all three; the fieldset carries it.
        extra = """<fieldset aria-required="true"><legend>Which tools? *</legend>
            <label><input type="checkbox" name="tools" value="py"> Python</label>
            <label><input type="checkbox" name="tools" value="sql"> SQL</label>
            <label><input type="checkbox" name="tools" value="go"> Go</label></fieldset>"""
        pages = {tla.GH_URL: tla._gh(extra, script), tla.GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        (item,) = local_answers.list_blocked()
        question = item["questions"][0]
        self.assertEqual(question["field_shape"]["kind"], "multi")

        self.assertEqual(local_answers.save_blocked_question_answer(question["qna_id"], app_id, ["Python", "SQL"]), [app_id])
        browser = tla._RoutedBrowser(self.real, pages)
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))

        row = self.row(app_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("applied", 2),
                         (row["blocked_reason"], row["error"], row["fields_used"]))
        filled = json.loads(row["fields_used"])["filled"]
        self.assertEqual({k: v for k, v in filled.items() if k in ("Python", "SQL", "Go")},
                         {"Python": "Python", "SQL": "SQL"})

    async def test_a_saved_upload_is_attached_to_that_file_input_on_the_rerun(self):
        # Reject the submit unless the input holds the uploaded file under its original name.
        script = """<script>document.getElementById('submit_app').addEventListener('click', e => {
          const files = document.getElementById('cover').files;
          if (files.length !== 1 || files[0].name !== 'My Letter.pdf') { e.preventDefault();
            const d = document.createElement('div'); d.setAttribute('role', 'alert');
            d.textContent = 'no file'; document.body.prepend(d); } });</script>"""
        extra = '<label for="cover">Cover letter *</label><input type="file" id="cover" name="cover" required>'
        pages = {tla.GH_URL: tla._gh(extra, script), tla.GH_DONE: tla.THANKS}
        app_id, _ = await self.apply(pages)
        (item,) = local_answers.list_blocked()
        question = item["questions"][0]
        self.assertEqual(question["field_shape"]["kind"], "file")

        local_answers.save_blocked_question_answer(question["qna_id"], app_id,
                                                   upload=("My Letter.pdf", b"%PDF-1.4 my letter"))
        browser = tla._RoutedBrowser(self.real, pages)
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))

        row = self.row(app_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("applied", 2))
        self.assertEqual(json.loads(row["fields_used"])["filled"]["Cover letter *"], "My Letter.pdf")

    async def test_a_saved_upload_is_not_typed_into_a_text_box_asking_the_same_question(self):
        # "Cover letter" was saved as a file; another form asks it as a text box.
        app_id = self.add_row()
        qna_id = self.add_answer("Cover letter", "/somewhere/secret.pdf", scope="global")
        self.conn.execute("UPDATE application_qna_bank SET answer_filename = 'secret.pdf' WHERE id = ?", (qna_id,))
        extra = '<label for="cl">Cover letter *</label><input id="cl" name="cl" required>'
        browser = tla._RoutedBrowser(self.real, {tla.GH_URL: tla._gh(extra), tla.GH_DONE: tla.THANKS})
        await local_apply.apply_one(browser, local_apply.claim("greenhouse"))
        self.assertEqual(self.row(app_id)["blocked_reason"], "question")

    async def test_the_screener_answers_column_no_longer_reaches_the_filler(self):
        self.assertNotIn("screener_answers", local_formfill._PROFILE_JSON_COLUMNS)


if __name__ == "__main__":
    unittest.main()
