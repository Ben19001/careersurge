"""Resume parse, profile save, global Q&A seed and the prepared batch.

Runs against a throwaway data dir. Jev is faked by rebinding `jev.choose` (the services' one-seam
pattern); the fixture PDFs in `fixtures/resumes/` are synthetic.
"""
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports

from app.services import jev, local_formfill, local_profile  # noqa: E402
from app.store import db  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "resumes"
POSIX = os.name == "posix"


def _fake_choose(preferred=None, none_for=()):
    """A `jev.choose` that picks `preferred[question_id]` when it is an option, else the first one."""
    preferred = preferred or {}

    def choose(state, questions):
        answers = {}
        for question_id, (_, options) in questions.items():
            wanted = preferred.get(question_id)
            if question_id in none_for:
                answers[question_id] = (None, 0.3)
            else:
                answers[question_id] = (wanted if wanted in options else options[0], 0.9)
        return answers
    return choose


class _DataDirTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "data"
        env = mock.patch.dict(os.environ, {"CAREERSURGE_DATA_DIR": str(self.data_dir)})
        env.start()
        self.addCleanup(env.stop)
        db.migrate()
        self.conn = db.get_db()
        self.addCleanup(self.conn.close)


class ParseResumeTests(_DataDirTestCase):
    def parse(self, name, choose=None):
        fake = mock.patch.object(jev, "choose", choose or _fake_choose({"exp0_company": "Acme Corp"}))
        fake.start()
        self.addCleanup(fake.stop)
        return local_profile.parse_resume((FIXTURES / name).read_bytes())

    def test_one_column_with_jev_picks(self):
        draft = self.parse("one_column.pdf", _fake_choose({
            "exp0_company": "Acme Corp", "exp1_company": "Beta Industries"}))

        for field, expected in (("full_name", "Jane Q. Doe"), ("email", "jane.doe@example.com"),
                                ("phone", "(408) 555-0142"), ("linkedin_url", "linkedin.com/in/janedoe"),
                                ("github_url", "github.com/janedoe"), ("city", "San Jose"), ("state", "CA")):
            self.assertEqual(draft[field]["value"], expected, field)
            self.assertEqual(draft[field]["source"], "jev", field)
        self.assertEqual(draft["portfolio_links"]["value"], ["janedoe.dev"])
        first, second = draft["work_experience"]
        self.assertEqual((first["title"], first["company"], first["location"]),
                         ("Senior Software Engineer", "Acme Corp", "San Francisco, CA"))
        self.assertEqual((first["start_month"], first["start_year"], first["end_year"], first["current"]),
                         ("01", "2021", "", True))
        self.assertEqual(first["description"],
                         "Built the billing pipeline serving 2M invoices a month.\nCut p99 latency from 900 ms to 240 ms.")
        self.assertEqual((second["title"], second["company"], second["start_month"], second["end_month"],
                          second["end_year"], second["current"]),
                         ("Software Engineer", "Beta Industries", "06", "12", "2020", False))
        self.assertEqual(first["picks"]["title"]["source"], "jev")
        self.assertEqual(set(first) - {"picks"}, {
            "title", "company", "location", "employment_type", "start_month", "start_year",
            "end_month", "end_year", "current", "description"})
        school = draft["education"][0]
        self.assertEqual((school["institution"], school["degree"], school["field_of_study"], school["gpa"],
                          school["start_year"], school["end_year"]),
                         ("University of Texas at Austin", "Bachelor of Science", "Computer Science", "3.8",
                          "2014", "2018"))
        self.assertEqual(draft["skills"], ["Python", "Go", "TypeScript", "SQL", "Docker", "Kubernetes", "PostgreSQL"])

    def test_two_column_still_splits_sections(self):
        draft = self.parse("two_column.pdf")

        self.assertEqual(draft["skills"][:3], ["Python", "Rust", "React"])
        self.assertIn("Cloud Infrastructure", draft["skills"])
        self.assertEqual([entry["title"] for entry in draft["work_experience"]], ["Staff Engineer", "Developer"])
        self.assertEqual(draft["work_experience"][0]["description"],
                         "Led the platform migration to Kubernetes.\nMentored six engineers.")
        self.assertEqual(draft["education"][0]["institution"], "Oregon State University")
        self.assertEqual(draft["full_name"]["value"], "Marcus Lee")

    def test_no_email_resume_has_no_email_candidates(self):
        draft = self.parse("no_email.pdf")

        self.assertIsNone(draft["email"]["value"])
        self.assertEqual(draft["email"]["candidates"], [])
        self.assertEqual(draft["email"]["source"], "none")  # nothing to choose from, so Jev was not asked
        self.assertEqual(draft["phone"]["value"], "(408) 555-0142")

    def test_jev_unavailable_falls_back_to_regex_candidates(self):
        def raise_unavailable(reason):
            def choose(state, questions):
                raise jev.JevUnavailable(reason)
            return choose

        for reason in ("network", "no_key", "rate_limited"):
            with self.subTest(reason=reason):
                draft = self.parse("one_column.pdf", raise_unavailable(reason))
                self.assertEqual(draft["email"]["value"], "jane.doe@example.com")
                for field in ("full_name", "email", "phone", "city"):
                    self.assertEqual(draft[field]["source"], "regex_fallback", field)
                    self.assertEqual(draft[field]["confidence"], 0, field)
                first = draft["work_experience"][0]
                self.assertEqual((first["title"], first["company"]), ("Senior Software Engineer", "Acme Corp"))
                self.assertEqual(first["picks"]["title"]["source"], "regex_fallback")

    def test_a_401_from_jev_degrades_like_a_missing_key(self):
        response = mock.Mock(status_code=401)
        with mock.patch.object(jev.secrets, "get_secret", return_value="wrong-key"), \
                mock.patch.object(jev.httpx, "post", create=True, return_value=response):
            draft = local_profile.parse_resume((FIXTURES / "one_column.pdf").read_bytes())

        self.assertEqual(draft["email"]["source"], "regex_fallback")
        self.assertEqual(draft["email"]["value"], "jane.doe@example.com")
        # Recorded, so Settings can say the key was refused instead of reading "Saved".
        self.assertIs(db.get_setting(jev.KEY_REJECTED_SETTING), True)

    def test_an_accepted_jev_call_clears_the_refused_flag(self):
        db.set_setting(jev.KEY_REJECTED_SETTING, True)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"answers": {"q": {"choice": "A", "confidence": 0.9}}}
        with (
            mock.patch.object(jev.secrets, "get_secret", return_value="good-key"),
            mock.patch.object(jev.httpx, "post", create=True, return_value=response),
        ):
            self.assertEqual(jev.choose({}, {"q": ("pick one", ["A", "B"])}), {"q": ("A", 0.9)})

        self.assertIs(db.get_setting(jev.KEY_REJECTED_SETTING), False)

    def test_none_answer_keeps_the_candidates_for_the_review_screen(self):
        draft = self.parse("one_column.pdf", _fake_choose(none_for=("email",)))

        self.assertIsNone(draft["email"]["value"])
        self.assertEqual(draft["email"]["candidates"], ["jane.doe@example.com"])
        self.assertEqual(draft["email"]["source"], "jev")
        self.assertEqual(draft["phone"]["value"], "(408) 555-0142")

    def test_draft_survives_a_new_connection(self):
        draft = self.parse("one_column.pdf")

        self.assertEqual(db.get_setting("profile_draft"), json.loads(json.dumps(draft)))

    def test_save_draft_overwrites_the_draft_but_keeps_the_stored_resume_path(self):
        draft = self.parse("one_column.pdf")
        local_profile.save_draft({"email": {"value": "edited@example.com"}, "resume_path": "/etc/passwd"})

        self.assertEqual(db.get_setting("profile_draft"),
                         {"email": {"value": "edited@example.com"}, "resume_path": draft["resume_path"]})
        self.assertIsNone(self.conn.execute("SELECT 1 FROM profile").fetchone())

    def test_the_parse_stores_the_pdf_and_the_draft_records_its_path(self):
        data = (FIXTURES / "one_column.pdf").read_bytes()
        draft = self.parse("one_column.pdf")

        stored = Path(draft["resume_path"])
        self.assertEqual(stored, self.data_dir / "resumes" / f"{hashlib.sha256(data).hexdigest()}.pdf")
        self.assertEqual(stored.read_bytes(), data)
        self.assertEqual(db.get_setting("profile_draft")["resume_path"], str(stored))
        if POSIX:
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(list((self.data_dir / "resumes").glob("*.tmp")), [])

    def test_a_pdf_the_parse_refuses_is_not_stored(self):
        with self.assertRaises(ValueError):
            local_profile.parse_resume(b"not a pdf")
        self.assertFalse((self.data_dir / "resumes").exists())

    def test_unreadable_pdf_is_a_value_error(self):
        with self.assertRaises(ValueError):
            local_profile.parse_resume(b"not a pdf")


class SaveProfileTests(_DataDirTestCase):
    PDF = b"%PDF-1.4 first resume"
    PDF_2 = b"%PDF-1.4 second resume"

    def resume_files(self):
        folder = self.data_dir / "resumes"
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_missing_email_or_unknown_key_writes_nothing(self):
        db.set_setting("profile_draft", {"keep": True})
        for fields in ({"full_name": "Jane"}, {"email": "j@x.com", "full_name": " "},
                       {"email": "j@x.com", "full_name": "Jane", "favourite_colour": "teal"}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    local_profile.save_profile(fields, self.PDF)
                self.assertIsNone(self.conn.execute("SELECT 1 FROM profile").fetchone())
                self.assertEqual(self.resume_files(), [])
                self.assertEqual(db.get_setting("profile_draft"), {"keep": True})
        with self.assertRaises(ValueError):
            local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "id": 2}, self.PDF)
        with self.assertRaises(ValueError):
            local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"}, b"plain text")
        with self.assertRaises(ValueError) as too_big:    # the 1 MB PDF rule
            local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"},
                                       b"%PDF-1.4" + b"0" * local_profile.RESUME_MAX_BYTES)
        self.assertEqual(str(too_big.exception), local_profile.RESUME_RULE)

    def test_save_writes_row_file_and_deletes_the_draft(self):
        db.set_setting("profile_draft", {"keep": False})

        local_profile.save_profile({
            "email": "j@x.com", "full_name": "Jane Doe", "skills": ["Python"],
            "work_experience": [{"title": "Engineer"}], "requires_sponsorship": False}, self.PDF)

        row = self.conn.execute("SELECT * FROM profile").fetchone()
        self.assertEqual((row["id"], row["email"], row["requires_sponsorship"]), (1, "j@x.com", 0))
        self.assertEqual(json.loads(row["skills"]), ["Python"])
        self.assertEqual(json.loads(row["work_experience"]), [{"title": "Engineer"}])
        self.assertEqual(Path(row["resume_path"]).read_bytes(), self.PDF)
        self.assertIsNone(db.get_setting("profile_draft"))
        if POSIX:
            self.assertEqual(stat.S_IMODE(Path(row["resume_path"]).stat().st_mode), 0o600)

    def test_picks_from_the_parse_draft_are_not_stored_in_the_profile(self):
        draft = {"title": "Engineer", "picks": {"title": {"value": "Engineer"}}}

        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "work_experience": [draft]}, self.PDF)

        stored = self.conn.execute("SELECT work_experience FROM profile").fetchone()[0]
        self.assertEqual(json.loads(stored), [{"title": "Engineer"}])

    def test_unencodable_field_raises_before_any_file_exists(self):
        with self.assertRaises(TypeError):
            local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "skills": [object()]}, self.PDF)

        self.assertEqual(self.resume_files(), [])

    def test_stray_partial_resume_from_a_crash_is_removed(self):
        (self.data_dir / "resumes").mkdir(parents=True)
        stray = self.data_dir / "resumes" / "abc.tmp"
        stray.write_bytes(b"%PDF-partial")
        two_minutes_ago = time.time() - 120
        os.utime(stray, (two_minutes_ago, two_minutes_ago))

        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)

        self.assertFalse(stray.exists())

    def test_a_temp_file_younger_than_a_minute_is_another_saves_and_is_left_alone(self):
        (self.data_dir / "resumes").mkdir(parents=True)
        in_progress = self.data_dir / "resumes" / "being-written.tmp"
        in_progress.write_bytes(b"%PDF-half")

        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)

        self.assertTrue(in_progress.exists())
        self.assertTrue((self.data_dir / "resumes" / f"{hashlib.sha256(self.PDF).hexdigest()}.pdf").is_file())

    def test_second_save_keeps_one_row_and_replaces_the_resume(self):
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane Doe", "phone": "555"}, self.PDF)
        first_path = self.conn.execute("SELECT resume_path FROM profile").fetchone()[0]

        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane Q Doe"}, self.PDF_2)

        rows = self.conn.execute("SELECT * FROM profile").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["full_name"], rows[0]["phone"]), (1, "Jane Q Doe", "555"))
        self.assertNotEqual(rows[0]["resume_path"], first_path)
        self.assertFalse(Path(first_path).exists())
        self.assertEqual(Path(rows[0]["resume_path"]).read_bytes(), self.PDF_2)
        self.assertEqual(len(self.resume_files()), 1)

    def test_same_resume_saved_twice_keeps_its_file(self):
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane D"}, self.PDF)

        path = Path(self.conn.execute("SELECT resume_path FROM profile").fetchone()[0])
        self.assertEqual(path.read_bytes(), self.PDF)

    def test_failed_transaction_keeps_the_draft_and_removes_the_new_file(self):
        db.set_setting("profile_draft", {"keep": True})

        with self.assertRaises(sqlite3.IntegrityError):
            # skills is NOT NULL: the upsert fails after the PDF was written
            local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "skills": None}, self.PDF)

        self.assertEqual(db.get_setting("profile_draft"), {"keep": True})
        self.assertIsNone(self.conn.execute("SELECT 1 FROM profile").fetchone())
        self.assertEqual(self.resume_files(), [])


class SaveConsentTests(_DataDirTestCase):
    """The one writer of the three consent flags and their evidence columns."""
    FLAG = "auto_consent_authorized"

    def setUp(self):
        super().setUp()
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane Doe"}, b"%PDF-1.4 resume")

    def row(self):
        return self.conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()

    def test_a_new_flag_is_stamped_with_its_version_and_time(self):
        local_profile.save_consent({self.FLAG: True})

        row = self.row()
        self.assertEqual((row[self.FLAG], row["consent_tos_version"]), (1, local_profile.CONSENT_TOS_VERSION))
        self.assertTrue(row["consent_authorized_at"])
        self.assertEqual((row["background_check_authorized"], row["background_check_tos_version"],
                          row["arbitration_authorized"], row["arbitration_authorized_at"]), (0, None, 0, None))

    def test_each_flag_is_stamped_with_its_own_columns(self):
        local_profile.save_consent({"background_check_authorized": True, "arbitration_authorized": True})

        row = self.row()
        self.assertEqual((row["background_check_tos_version"], row["arbitration_tos_version"]),
                         (local_profile.BACKGROUND_TOS_VERSION, local_profile.ARBITRATION_TOS_VERSION))
        self.assertTrue(row["background_check_authorized_at"] and row["arbitration_authorized_at"])
        self.assertEqual((row[self.FLAG], row["consent_authorized_at"]), (0, None))

    def test_saving_a_flag_at_its_current_value_keeps_the_first_timestamp(self):
        local_profile.save_consent({self.FLAG: True})
        self.conn.execute("UPDATE profile SET consent_authorized_at = '2026-01-01T00:00:00.000Z'")

        local_profile.save_consent({self.FLAG: True})

        self.assertEqual(self.row()["consent_authorized_at"], "2026-01-01T00:00:00.000Z")

    def test_clearing_a_flag_keeps_its_version_and_time_as_the_record(self):
        local_profile.save_consent({self.FLAG: True})
        stamped = self.row()["consent_authorized_at"]

        local_profile.save_consent({self.FLAG: False})

        row = self.row()
        self.assertEqual((row[self.FLAG], row["consent_tos_version"], row["consent_authorized_at"]),
                         (0, local_profile.CONSENT_TOS_VERSION, stamped))

    def test_a_flag_ticked_again_after_clearing_is_stamped_afresh(self):
        local_profile.save_consent({self.FLAG: True})
        self.conn.execute("UPDATE profile SET consent_authorized_at = '2026-01-01T00:00:00.000Z'")
        local_profile.save_consent({self.FLAG: False})

        local_profile.save_consent({self.FLAG: True})

        self.assertNotEqual(self.row()["consent_authorized_at"], "2026-01-01T00:00:00.000Z")

    def test_an_unknown_key_or_no_profile_raises_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            local_profile.save_consent({"phone": "1"})
        with self.assertRaises(ValueError):
            local_profile.save_consent({self.FLAG: True, "phone": "1"})
        self.assertEqual(self.row()[self.FLAG], 0)

        self.conn.execute("DELETE FROM profile")
        with self.assertRaises(ValueError):
            local_profile.save_consent({self.FLAG: True})

    def test_save_profile_refuses_every_consent_column_and_writes_nothing(self):
        for column in sorted(local_profile._CONSENT_COLUMNS):
            with self.subTest(column=column):
                with self.assertRaises(ValueError):
                    local_profile.save_profile({"email": "j@x.com", "full_name": "Changed", column: 1})
                self.assertEqual(self.row()["full_name"], "Jane Doe")

    def test_the_refused_columns_cover_every_flag_with_its_version_and_time(self):
        for flag, (version_column, stamp_column, _) in local_profile._CONSENT_FLAGS.items():
            with self.subTest(flag=flag):
                self.assertLessEqual({flag, version_column, stamp_column}, local_profile._CONSENT_COLUMNS)
        self.assertEqual(len(local_profile._CONSENT_COLUMNS), 3 * len(local_profile._CONSENT_FLAGS))

    def test_a_set_flag_ticks_the_certify_box_and_the_audit_names_tier_and_version(self):
        certify = {"label": "I certify that the information above is true and accurate", "input_id": "cert",
                   "input_type": "checkbox"}
        flags = ({self.FLAG: True}, {self.FLAG: False})
        for wanted, ticked in zip(flags, (True, False)):
            local_profile.save_consent(wanted)
            values, audit = local_formfill._consent_values([certify], local_profile.get_profile())
            with self.subTest(wanted=wanted):
                self.assertEqual(values, {"cert": "Yes"} if ticked else {})
                if ticked:
                    self.assertEqual((audit[0]["authority"], audit[0]["tos_version"]),
                                     ("standard", local_profile.CONSENT_TOS_VERSION))

    def test_a_flag_agreed_under_older_wording_ticks_nothing_until_ticked_again_then_is_stamped_afresh(self):
        certify = {"label": "I certify that the information above is true and accurate", "input_id": "cert",
                   "input_type": "checkbox"}
        self.conn.execute("UPDATE profile SET auto_consent_authorized = 1, consent_tos_version = '2026-10-17', "
                          "consent_authorized_at = '2026-10-17T00:00:00.000Z'")
        self.assertEqual(local_formfill._consent_values([certify], local_profile.get_profile())[0], {})

        local_profile.save_consent({self.FLAG: True})

        row = self.row()
        self.assertEqual(row["consent_tos_version"], "2026-10-28")
        self.assertNotEqual(row["consent_authorized_at"], "2026-10-17T00:00:00.000Z")
        # The live row, as `local_apply.fill_check` reads it, now ticks the box under the new wording.
        values, audit = local_formfill._consent_values([certify], local_profile.get_profile())
        self.assertEqual(values, {"cert": "Yes"})
        self.assertEqual((audit[0]["authority"], audit[0]["tos_version"]), ("standard", "2026-10-28"))


class ProfileReaderTests(_DataDirTestCase):
    """The readers behind the Profile screen and a resumed review, and a save without a new resume."""
    PDF = b"%PDF-1.4 first resume"

    def test_no_profile_and_no_draft_read_as_none(self):
        self.assertIsNone(local_profile.get_profile())
        self.assertIsNone(local_profile.get_draft())

    def test_get_profile_decodes_the_json_columns(self):
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "skills": ["Python"],
                                    "work_experience": [{"title": "Engineer"}]}, self.PDF)

        profile = local_profile.get_profile()

        self.assertEqual((profile["email"], profile["skills"], profile["work_experience"]),
                         ("j@x.com", ["Python"], [{"title": "Engineer"}]))
        self.assertEqual((profile["projects"], profile["work_preferences"]), ([], {}))

    def test_get_draft_returns_what_save_draft_stored_until_the_profile_is_saved(self):
        local_profile.save_draft({"full_name": {"value": "Jane"}})
        self.assertEqual(local_profile.get_draft(), {"full_name": {"value": "Jane"}})

        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)

        self.assertIsNone(local_profile.get_draft())

    def test_a_save_without_a_resume_keeps_the_resume_and_the_other_columns(self):
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "phone": "555-0100"}, self.PDF)
        before = local_profile.get_profile()["resume_path"]

        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "phone": "555-0199",
                                    "resume_path": "/elsewhere.pdf"})

        after = local_profile.get_profile()
        self.assertEqual((after["phone"], after["resume_path"]), ("555-0199", before))
        self.assertEqual(Path(before).read_bytes(), self.PDF)

    def test_a_first_save_without_a_resume_is_refused_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"})
        self.assertIsNone(local_profile.get_profile())

    def test_a_reopened_review_confirms_with_the_resume_the_parse_stored(self):
        data = (FIXTURES / "one_column.pdf").read_bytes()
        with mock.patch.object(jev, "choose", _fake_choose()):
            draft = local_profile.parse_resume(data)
        local_profile.save_draft(draft)                 # the review screen's autosave
        # "Close the app": nothing of the upload is held in memory, only the stored draft.
        reopened = local_profile.get_draft()

        local_profile.save_profile({"email": reopened["email"]["value"], "full_name": reopened["full_name"]["value"]})

        profile = local_profile.get_profile()
        self.assertEqual(Path(profile["resume_path"]).read_bytes(), data)
        self.assertEqual(profile["resume_path"], draft["resume_path"])
        self.assertIsNone(local_profile.get_draft())
        self.assertEqual(len(list((self.data_dir / "resumes").glob("*.pdf"))), 1)    # the save's own sweep spared it

    def test_a_draft_pointing_outside_the_resumes_folder_or_at_a_missing_file_is_not_a_resume(self):
        outside = self.data_dir / "elsewhere.pdf"
        outside.write_bytes(b"%PDF-1.4 other")
        for path in (str(outside), str(self.data_dir / "resumes" / "gone.pdf"), ""):
            with self.subTest(path=path):
                db.set_setting("profile_draft", {"resume_path": path})
                with self.assertRaises(ValueError) as refused:
                    local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"})
                self.assertEqual(str(refused.exception), "a resume is required")
                self.assertIsNone(local_profile.get_profile())

    def test_the_draft_resume_is_used_only_for_a_first_save(self):
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"}, b"%PDF-1.4 saved resume")
        saved = local_profile.get_profile()["resume_path"]
        with mock.patch.object(jev, "choose", _fake_choose()):
            local_profile.parse_resume((FIXTURES / "one_column.pdf").read_bytes())

        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane", "phone": "555"})

        self.assertEqual(local_profile.get_profile()["resume_path"], saved)

    def test_the_sweep_keeps_the_file_the_current_draft_points_at(self):
        local_profile.save_profile({"email": "j@x.com", "full_name": "Jane"}, b"%PDF-1.4 saved resume")
        with mock.patch.object(jev, "choose", _fake_choose()):
            draft = local_profile.parse_resume((FIXTURES / "one_column.pdf").read_bytes())
        orphan = self.data_dir / "resumes" / ("0" * 64 + ".pdf")
        orphan.write_bytes(b"%PDF-1.4 nobody needs me")

        deleted = local_profile.sweep_resumes()

        self.assertEqual(deleted, 1)
        self.assertTrue(Path(draft["resume_path"]).is_file())
        self.assertFalse(orphan.exists())
        self.assertTrue(Path(local_profile.get_profile()["resume_path"]).is_file())

    def test_a_resume_over_1_mb_or_not_a_pdf_is_refused_by_the_parse(self):
        for data in (b"%PDF-1.4" + b"0" * local_profile.RESUME_MAX_BYTES, b"PK\x03\x04 a docx"):
            with self.subTest(size=len(data)):
                with self.assertRaises(ValueError) as refused:
                    local_profile.parse_resume(data)
                self.assertEqual(str(refused.exception), local_profile.RESUME_RULE)

    def test_list_global_questions_shows_the_seed_in_order_and_each_answer(self):
        self.assertEqual(local_profile.list_global_questions(), [])
        local_profile.seed_global_questions()
        start = local_profile._normalize_question("When can you start?")
        local_profile.save_global_answer(start, "Immediately")

        questions = local_profile.list_global_questions()

        self.assertEqual([q["question"] for q in questions], [question for question, _, _ in local_profile.GLOBAL_SEED])
        answers = {q["normalized_question"]: q["answer"] for q in questions}
        self.assertEqual(answers[start], "Immediately")
        self.assertEqual(sum(answer is not None for answer in answers.values()), 1)


class GlobalQuestionTests(_DataDirTestCase):
    def test_seed_twice_then_answer(self):
        local_profile.seed_global_questions()
        local_profile.seed_global_questions()
        sponsorship = local_profile._normalize_question("Will you now or in the future require visa sponsorship?")
        local_profile.save_global_answer(sponsorship, "No")

        rows = self.conn.execute(
            "SELECT normalized_question, question_type, is_answered, answer_value, scope, company_key, listing_id "
            "FROM application_qna_bank").fetchall()
        self.assertEqual(len(rows), 6)
        types = {row["normalized_question"]: row["question_type"] for row in rows}
        self.assertEqual({question for question, kind in types.items() if kind == "job_dependent"},
                         {"what are your salary expectations", "are you willing to relocate"})
        self.assertEqual({row["question_type"] for row in rows if row["normalized_question"] not in (
            "what are your salary expectations", "are you willing to relocate")}, {"stable_fact"})
        self.assertEqual({(row["scope"], row["company_key"], row["listing_id"]) for row in rows},
                         {("global", None, None)})
        answered = [row for row in rows if row["is_answered"]]
        self.assertEqual([(row["normalized_question"], row["answer_value"]) for row in answered],
                         [("will you now or in the future require visa sponsorship", "No")])

    def test_the_seed_has_six_questions_each_with_a_shape_and_none_asks_how_you_heard(self):
        local_profile.seed_global_questions()

        questions = local_profile.list_global_questions()

        self.assertEqual(len(questions), 6)
        self.assertNotIn("how did you hear about us", [q["normalized_question"] for q in questions])
        shapes = {q["question"]: q["field_shape"] for q in questions}
        self.assertEqual(shapes["Are you legally authorized to work in the United States?"],
                         {"kind": "single", "options": ["Yes", "No"], "max_length": None})
        self.assertEqual(shapes["What are your salary expectations?"]["kind"], "number")
        self.assertEqual(shapes["When can you start?"]["options"][0], "Immediately")

    def test_an_answer_must_fit_the_questions_shape(self):
        local_profile.seed_global_questions()
        authorized = local_profile._normalize_question("Are you legally authorized to work in the United States?")
        salary = local_profile._normalize_question("What are your salary expectations?")

        with self.assertRaisesRegex(ValueError, "Yes, No"):
            local_profile.save_global_answer(authorized, "sure")
        local_profile.save_global_answer(authorized, "Yes")
        for bad in ("$120k", "120,000", "12.5", "-5", ""):
            with self.subTest(salary=bad), self.assertRaises(ValueError):
                local_profile.save_global_answer(salary, bad)
        local_profile.save_global_answer(salary, "120000")

        answers = {q["normalized_question"]: q["answer"] for q in local_profile.list_global_questions()}
        self.assertEqual((answers[authorized], answers[salary]), ("Yes", "120000"))

    def test_a_row_the_tray_created_is_a_text_box_of_up_to_500_characters(self):
        local_profile.seed_global_questions()
        self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope) "
            "VALUES ('Favourite colour?', 'favourite colour', 'open_ended', 'global')")

        (tray_row,) = [q for q in local_profile.list_global_questions() if q["normalized_question"] == "favourite colour"]
        self.assertEqual(tray_row["field_shape"], {"kind": "text", "options": [], "max_length": 500})
        local_profile.save_global_answer("favourite colour", "x" * 500)
        with self.assertRaises(ValueError):
            local_profile.save_global_answer("favourite colour", "x" * 501)

    def test_seed_keeps_an_existing_answer(self):
        local_profile.seed_global_questions()
        key = local_profile._normalize_question("When can you start?")
        local_profile.save_global_answer(key, "Immediately")
        local_profile.seed_global_questions()

        row = self.conn.execute("SELECT answer_value, is_answered FROM application_qna_bank "
                                "WHERE normalized_question = ?", (key,)).fetchone()
        self.assertEqual((row["answer_value"], row["is_answered"]), ("Immediately", 1))

    def test_unknown_question_is_a_value_error(self):
        with self.assertRaises(ValueError):
            local_profile.save_global_answer("not seeded", "x")

    def test_normalize_question(self):
        self.assertEqual(local_profile._normalize_question("  Are you  AUTHORIZED, to work?? "),
                         "are you authorized to work")


class BuildPreparedTests(_DataDirTestCase):
    def listing(self, external_id, company_key="acme", url=None, posted_at="2026-09-01T00:00:00.000Z",
                skills=("python", "sql"), title="backend engineer", is_active=1, **extra):
        columns = {
            "source": "greenhouse", "external_id": external_id,
            "url": url or f"https://boards.greenhouse.io/{company_key}/jobs/{external_id}",
            "company_key": company_key, "normalized_title": title, "location": "Austin, TX",
            "posted_at": posted_at, "is_active": is_active,
            "repost_key": f"key-{external_id}", "key_skills": json.dumps(list(skills)) if skills else None,
        }
        columns.update(extra)
        cursor = self.conn.execute(
            f"INSERT INTO listings ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            list(columns.values()))
        return cursor.lastrowid

    def apply_to(self, listing_id, status="applied"):
        self.conn.execute("INSERT INTO job_applications (listing_id, ats, status) VALUES (?, 'greenhouse', ?)",
                          (listing_id, status))

    def applications(self):
        rows = self.conn.execute(
            "SELECT listing_id, ats, status, repost_match, repost_of_listing_id FROM job_applications "
            "ORDER BY listing_id").fetchall()
        return {row["listing_id"]: row for row in rows}

    def test_creates_prepared_rows_for_active_listings_without_an_application(self):
        a = self.listing("1", company_key="a")
        b = self.listing("2", company_key="b")
        c = self.listing("3", company_key="c")
        self.listing("4", company_key="d", is_active=0)
        self.apply_to(a, status="prepared")

        created = local_profile.build_prepared()

        self.assertEqual(created, 2)
        rows = self.applications()
        self.assertEqual(sorted(rows), [a, b, c])
        self.assertEqual({(rows[b]["status"], rows[b]["ats"]), (rows[c]["status"], rows[c]["ats"])},
                         {("prepared", "greenhouse")})
        self.assertEqual(local_profile.build_prepared(), 0)

    def test_same_repost_key_as_an_applied_listing_is_strong(self):
        old = self.listing("1", repost_key="same-url")
        self.apply_to(old)
        new = self.listing("2", repost_key="same-url", posted_at="2026-10-01T00:00:00.000Z")

        local_profile.build_prepared()

        row = self.applications()[new]
        self.assertEqual((row["repost_match"], row["repost_of_listing_id"]), ("strong", old))

    def test_same_company_and_fingerprint_with_another_url_is_weak(self):
        old = self.listing("1")
        self.apply_to(old)
        new = self.listing("2", posted_at="2026-10-01T00:00:00.000Z")

        local_profile.build_prepared()

        row = self.applications()[new]
        self.assertEqual((row["repost_match"], row["repost_of_listing_id"]), ("weak", old))

    def test_key_skills_decide_the_weak_match(self):
        old = self.listing("1", skills=("python", "sql"))
        self.apply_to(old)
        different = self.listing("2", skills=("rust", "c++"))
        empty = self.listing("3", skills=())
        reordered = self.listing("4", skills=("SQL", "Python"))

        local_profile.build_prepared()

        rows = self.applications()
        self.assertEqual(rows[different]["repost_match"], None)
        self.assertEqual(rows[empty]["repost_match"], None)
        self.assertEqual((rows[reordered]["repost_match"], rows[reordered]["repost_of_listing_id"]), ("weak", old))

    def test_other_company_or_outside_twelve_months_is_not_weak(self):
        old = self.listing("1", posted_at="2025-01-01T00:00:00.000Z")
        self.apply_to(old)
        stale = self.listing("2", posted_at="2026-10-01T00:00:00.000Z")
        other_company = self.listing("3", company_key="globex", posted_at="2025-01-02T00:00:00.000Z")

        local_profile.build_prepared()

        rows = self.applications()
        self.assertIsNone(rows[stale]["repost_match"])
        self.assertIsNone(rows[other_company]["repost_match"])

    def test_cancelled_or_failed_earlier_application_is_not_a_repost(self):
        for status, external_id in (("cancelled", "1"), ("failed", "2"), ("prepared", "3")):
            self.apply_to(self.listing(external_id, repost_key="same-url"), status=status)
        new = self.listing("4", repost_key="same-url", posted_at="2026-10-01T00:00:00.000Z")

        local_profile.build_prepared()

        self.assertIsNone(self.applications()[new]["repost_match"])

    def test_two_copies_in_one_call_are_not_both_unflagged(self):
        older = self.listing("1", repost_key="same-url", posted_at="2026-09-01T00:00:00.000Z")
        newer = self.listing("2", repost_key="same-url", posted_at="2026-09-05T00:00:00.000Z")

        created = local_profile.build_prepared()

        rows = self.applications()
        self.assertEqual(created, 2)
        self.assertEqual((rows[older]["repost_match"], rows[older]["repost_of_listing_id"]), (None, None))
        self.assertEqual((rows[newer]["repost_match"], rows[newer]["repost_of_listing_id"]), ("strong", older))


class MigrationTests(_DataDirTestCase):
    def test_0003_on_a_v2_database_adds_two_null_columns(self):
        # Rebuild a v2 database by hand: migrate() from setUp already ran, so start over. setUp's
        # connection is closed first: Windows refuses to delete a file that is still open (WinError 32).
        self.conn.close()
        db.db_path().unlink()
        for leftover in db.db_path().parent.glob("careersurge.db-*"):
            leftover.unlink()
        raw = sqlite3.connect(db.db_path())
        migrations = Path(db.__file__).parent / "migrations"
        raw.executescript((migrations / "0001_init.sql").read_text())
        raw.executescript((migrations / "0002_local_sourcing_schema.sql").read_text())
        raw.execute("PRAGMA user_version = 2")
        raw.execute("INSERT INTO listings (source, external_id, url, company_key) "
                    "VALUES ('greenhouse', '1', 'https://x', 'acme')")
        raw.execute("INSERT INTO job_applications (listing_id, ats, status) VALUES (1, 'greenhouse', 'prepared')")
        raw.commit()
        raw.close()

        self.assertEqual(db.migrate(), 9)

        conn = db.get_db()
        self.addCleanup(conn.close)
        row = conn.execute("SELECT repost_match, repost_of_listing_id FROM job_applications").fetchone()
        self.assertEqual((row["repost_match"], row["repost_of_listing_id"]), (None, None))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE job_applications SET repost_match = 'maybe'")


if __name__ == "__main__":
    unittest.main()
