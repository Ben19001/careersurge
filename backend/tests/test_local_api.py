"""The local API's guards and routes.

FastAPI, pydantic and httpx are the REAL packages here, while `conftest` stubs two of them suite-wide. `setUpModule` swaps the stubs out for this module's tests and
`tearDownModule` puts them back, so no other module sees the real ones. Runs against a throwaway data
dir; no request leaves the machine (`TestClient` calls the app in process).
"""
import datetime
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import test_local_apply as tla  # noqa: E402 - the store-backed base class

import keyring  # noqa: E402
from keyring.backends import fail  # noqa: E402

from app.services import jev, local_answers, local_handoff, local_profile  # noqa: E402
from app.store import db, secrets  # noqa: E402

TOKEN = "secret-token"
_STUBBED = {}
local_api = TestClient = local_queues = local_sourcing = None


def setUpModule():
    global local_api, TestClient, local_queues, local_sourcing
    for name in ("httpx", "pydantic"):
        if name in sys.modules:
            _STUBBED[name] = sys.modules.pop(name)
    try:
        from fastapi.testclient import TestClient as client
        from app import local_api as api
        from app.services import local_queues as queues, local_sourcing as sourcing
    except ImportError as exc:
        sys.modules.update(_STUBBED)
        raise unittest.SkipTest(f"fastapi is not installed: {exc}")
    local_api, TestClient, local_queues, local_sourcing = api, client, queues, sourcing


def tearDownModule():
    sys.modules.update(_STUBBED)


class _ApiCase(tla._StoreTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(local_api.create_app(TOKEN), base_url="http://127.0.0.1:8123")
        self.addCleanup(self.client.close)
        self.headers = {local_api.TOKEN_HEADER: TOKEN}

    def blocked_question(self, shot=None, shape=None):
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="question",
                              screenshot_path=str(shot) if shot else None)
        listing_id = self.conn.execute("SELECT listing_id FROM job_applications WHERE id = ?", (app_id,)).fetchone()[0]
        qna_id = self.conn.execute(
            "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, scope, listing_id) "
            "VALUES ('Why us?', 'why us', 'open_ended', 'job', ?)", (listing_id,)).lastrowid
        self.conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id, field_shape) "
                          "VALUES (?, ?, ?)", (app_id, qna_id, json.dumps(shape or {})))
        return app_id, qna_id


class GuardTests(_ApiCase):
    def test_no_token_or_a_wrong_token_is_401(self):
        self.assertEqual(self.client.get("/api/blocked").status_code, 401)
        self.assertEqual(self.client.get("/api/blocked", headers={local_api.TOKEN_HEADER: "nope"}).status_code, 401)
        self.assertEqual(self.client.get("/api/blocked", headers=self.headers).status_code, 200)

    def test_a_foreign_host_is_400_and_localhost_is_allowed(self):
        self.assertEqual(self.client.get("/api/blocked", headers={**self.headers, "Host": "evil.com"}).status_code, 400)
        self.assertEqual(self.client.get("/api/blocked", headers={**self.headers, "Host": "localhost:9"}).status_code, 200)

    def test_a_foreign_origin_is_403_and_the_apps_own_origin_is_allowed(self):
        for origin in ("http://evil.com", "null", "http://127.0.0.1:9999"):
            with self.subTest(origin=origin):
                response = self.client.post("/api/retry/1", headers={**self.headers, "Origin": origin})
                self.assertEqual(response.status_code, 403)
        own = self.client.get("/api/blocked", headers={**self.headers, "Origin": "http://127.0.0.1:8123"})
        self.assertEqual(own.status_code, 200)

    def test_no_response_carries_a_cors_header_and_there_is_no_docs_page(self):
        response = self.client.get("/api/blocked", headers={**self.headers, "Origin": "http://127.0.0.1:8123"})
        self.assertNotIn("access-control-allow-origin", response.headers)
        preflight = self.client.options("/api/answer", headers={"Origin": "http://evil.com",
                                                                "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", preflight.headers)
        self.assertEqual(self.client.get("/docs", headers=self.headers).status_code, 404)

    def test_an_empty_token_is_refused(self):
        with self.assertRaises(ValueError):
            local_api.create_app("")


class RouteTests(_ApiCase):
    def test_blocked_lists_the_open_questions(self):
        app_id, qna_id = self.blocked_question()
        body = self.client.get("/api/blocked", headers=self.headers).json()
        self.assertTrue(body["ok"])
        self.assertEqual([(b["id"], b["questions"][0]["qna_id"]) for b in body["blocked"]], [(app_id, qna_id)])

    def test_blocked_cards_carry_the_profile_email_and_an_otp_card_its_wait(self):
        self.add_profile()
        otp = self.add_row(status="blocked_needs_input", blocked_reason="otp")
        captcha = self.add_row(status="blocked_needs_input", blocked_reason="captcha")
        cards = {c["id"]: c for c in self.client.get("/api/blocked", headers=self.headers).json()["blocked"]}
        self.assertEqual(cards[otp]["email"], "jane@example.com")
        self.assertEqual(cards[otp]["code_wait_s"], local_handoff.CODE_WAIT_S)
        self.assertEqual(cards[captcha]["email"], "jane@example.com")
        self.assertNotIn("code_wait_s", cards[captcha])

    def test_otp_is_404_with_nothing_parked_and_400_for_an_empty_code(self):
        post = lambda app_id, code: self.client.post(f"/api/otp/{app_id}", headers=self.headers, json={"code": code})  # noqa: E731
        self.assertEqual(post(9, "123456").status_code, 404)
        self.assertIn("not waiting on a code", post(9, "123456").json()["error"])
        self.assertEqual(post(9, "  ").status_code, 400)
        self.assertEqual(post(9, "1" * (local_handoff.CODE_MAX_LENGTH + 1)).status_code, 400)
        self.assertEqual(self.client.post("/api/otp/9", json={"code": "1"}).status_code, 401)

    def test_opening_a_row_that_is_not_waiting_on_a_captcha_is_409_and_claims_nothing(self):
        question = self.add_row(status="blocked_needs_input", blocked_reason="question")
        for app_id in (question, 999):
            response = self.client.post(f"/api/captcha/{app_id}/open", headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertIn("not waiting on a captcha", response.json()["error"])
        self.assertEqual(self.row(question)["status"], "blocked_needs_input")

    def test_manual_apply_opens_the_listing_once_and_moves_the_card(self):
        app_id = self.add_row(status="blocked_needs_input", blocked_reason="captcha")
        with mock.patch.object(local_api.webbrowser, "open", return_value=True) as opener:
            response = self.client.post(f"/api/captcha/{app_id}/manual", headers=self.headers)
            again = self.client.post(f"/api/captcha/{app_id}/manual", headers=self.headers)
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True, "url": tla.GH_URL, "opened": True}))
        opener.assert_called_once_with(tla.GH_URL)
        self.assertEqual(again.status_code, 409)
        self.assertIn("not waiting on a captcha", again.json()["error"])
        self.assertEqual((self.row(app_id)["blocked_reason"], self.row(app_id)["error"]), ("verify_submit", "manual_apply"))

    def test_manual_apply_reports_a_machine_with_no_browser_and_refuses_a_foreign_host(self):
        quiet = self.add_row(status="blocked_needs_input", blocked_reason="captcha")
        foreign = self.add_row(url="https://evil.example/acme/jobs/1", status="blocked_needs_input", blocked_reason="captcha")
        with mock.patch.object(local_api.webbrowser, "open", return_value=False) as opener:
            no_browser = self.client.post(f"/api/captcha/{quiet}/manual", headers=self.headers)
            refused = self.client.post(f"/api/captcha/{foreign}/manual", headers=self.headers)
        self.assertEqual((no_browser.status_code, no_browser.json()["opened"]), (200, False))
        self.assertEqual(refused.status_code, 400)
        opener.assert_called_once_with(tla.GH_URL)
        self.assertEqual(self.row(foreign)["blocked_reason"], "captcha")

    def test_open_posting_opens_a_blocked_rows_listing_only(self):
        blocked = self.add_row(status="blocked_needs_input", blocked_reason="question")
        pending = self.add_row(status="pending")
        foreign = self.add_row(url="https://evil.example/x", status="blocked_needs_input", blocked_reason="question")
        backslash = self.add_row(url="https://evil.example\\@boards.greenhouse.io/acme/jobs/1",
                                 status="blocked_needs_input", blocked_reason="question")
        post = lambda app_id: self.client.post(f"/api/posting/{app_id}/open", headers=self.headers)  # noqa: E731
        with mock.patch.object(local_api.webbrowser, "open", return_value=True) as opener:
            self.assertEqual(post(blocked).json(), {"ok": True, "url": tla.GH_URL, "opened": True})
            self.assertEqual((post(pending).status_code, post(999).status_code, post(foreign).status_code,
                              post(backslash).status_code), (404, 404, 400, 400))
        opener.assert_called_once_with(tla.GH_URL)

    def test_not_applied_on_an_ordinary_verify_submit_is_400(self):
        ordinary = self.add_row(status="blocked_needs_input", blocked_reason="verify_submit")
        response = self.client.post(f"/api/verify-submit/{ordinary}", headers=self.headers, json={"action": "not_applied"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.row(ordinary)["blocked_reason"], "verify_submit")

    def test_answer_saves_and_requeues(self):
        app_id, qna_id = self.blocked_question()
        response = self.client.post("/api/answer", headers=self.headers, json={"qna_id": qna_id, "application_id": app_id, "value": "Mission"})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True, "requeued": [app_id]}))
        self.assertEqual(self.row(app_id)["status"], "pending")

    def test_answer_scope_widens_the_row_for_json_and_multipart_and_a_refusal_is_400(self):
        app_id, qna_id = self.blocked_question()
        response = self.client.post("/api/answer", headers=self.headers,
                                    json={"qna_id": qna_id, "application_id": app_id, "value": "Mission", "scope": "company"})
        self.assertEqual((response.status_code, response.json()["requeued"]), (200, [app_id]))
        self.assertEqual(self.conn.execute("SELECT scope, listing_id FROM application_qna_bank WHERE id = ?",
                                           (qna_id,)).fetchone()[:], ("company", None))

        file_app, file_q = self.blocked_question(shape={"kind": "file", "options": [], "max_length": None})
        stored = self.client.post("/api/answer", headers=self.headers, files={"file": ("cv.pdf", b"%PDF-1.4")},
                                  data={"qna_id": str(file_q), "application_id": str(file_app), "scope": "global"})
        self.assertEqual((stored.status_code, stored.json().get("requeued")), (200, [file_app]), stored.text)
        self.assertEqual(self.conn.execute("SELECT scope FROM application_qna_bank WHERE id = ?", (file_q,)).fetchone()[0],
                         "global")

        bad_app, bad_q = self.blocked_question()
        bad = self.client.post("/api/answer", headers=self.headers,
                               json={"qna_id": bad_q, "application_id": bad_app, "value": "x", "scope": "planet"})
        self.assertEqual(bad.status_code, 400)

    def test_answer_rejects_an_empty_value_an_unknown_question_and_a_bad_body(self):
        app_id, qna_id = self.blocked_question()
        post = lambda **body: self.client.post("/api/answer", headers=self.headers, json=body)  # noqa: E731
        self.assertEqual(post(qna_id=qna_id, application_id=app_id, value=" ").status_code, 400)
        self.assertEqual(post(qna_id=999, application_id=app_id, value="x").status_code, 404)
        self.assertEqual(post(value="x").status_code, 422)
        self.assertEqual(post(qna_id=qna_id, value="x").status_code, 422)         # no application_id

    def test_answer_from_an_application_that_is_not_waiting_on_the_question_is_400(self):
        _, qna_id = self.blocked_question()
        elsewhere, _ = self.blocked_question()
        response = self.client.post("/api/answer", headers=self.headers,
                                    json={"qna_id": qna_id, "application_id": elsewhere, "value": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not waiting on question", response.json()["error"])
        files = self.client.post("/api/answer", headers=self.headers, files={"file": ("cv.pdf", b"%PDF-1.4")},
                                 data={"qna_id": str(qna_id), "application_id": str(elsewhere)})
        self.assertEqual(files.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT is_answered FROM application_qna_bank WHERE id = ?",
                                           (qna_id,)).fetchone()[0], 0)

    def test_blocked_cards_carry_the_job_details(self):
        app_id, _ = self.blocked_question()
        self.conn.execute(
            "UPDATE listings SET location = 'Remote', responsibilities_summary = 'Build', requirements_summary = 'Python', "
            "key_skills = '[\"Python\"]' WHERE id = (SELECT listing_id FROM job_applications WHERE id = ?)", (app_id,))
        (card,) = self.client.get("/api/blocked", headers=self.headers).json()["blocked"]
        self.assertEqual((card["location"], card["has_company"], card["responsibilities"], card["requirements"],
                          card["key_skills"]), ("Remote", True, "Build", "Python", ["Python"]))
        self.assertTrue(card["url"].startswith("http"))
        self.assertEqual(card["ats"], "greenhouse")

    def test_blocked_returns_each_questions_field_shape(self):
        shape = {"kind": "multi", "options": ["Python", "SQL"], "max_length": None}
        self.blocked_question(shape=shape)
        self.blocked_question()
        body = self.client.get("/api/blocked", headers=self.headers).json()
        self.assertEqual([b["questions"][0]["field_shape"] for b in body["blocked"]],
                         [shape, {"kind": "text", "options": [], "max_length": None}])

    def test_answer_refuses_a_value_the_control_would_not_take_with_400_and_the_allowed_options(self):
        app_single, single = self.blocked_question(shape={"kind": "single", "options": ["Yes", "No"], "max_length": None})
        app_multi, multi = self.blocked_question(shape={"kind": "multi", "options": ["Python", "SQL"], "max_length": None})
        app_text, text = self.blocked_question(shape={"kind": "text", "options": [], "max_length": 5})
        post = lambda qna_id, app_id, value: self.client.post(  # noqa: E731
            "/api/answer", headers=self.headers, json={"qna_id": qna_id, "application_id": app_id, "value": value})

        for qna_id, app_id, value, says in ((single, app_single, "Maybe", "choose one of: Yes, No"),
                                            (multi, app_multi, ["Python", "Rust"], "not offered: Rust"),
                                            (multi, app_multi, "Python", "choose one or more of: Python, SQL"),
                                            (text, app_text, "too long", "at most 5 characters")):
            with self.subTest(value=value):
                response = post(qna_id, app_id, value)
                self.assertEqual(response.status_code, 400)
                self.assertIn(says, response.json()["error"])
        self.assertEqual(post(single, app_single, "No").status_code, 200)
        self.assertEqual(post(multi, app_multi, ["SQL", "Python"]).status_code, 200)
        self.assertEqual(json.loads(self.conn.execute("SELECT answer_value FROM application_qna_bank WHERE id = ?",
                                                      (multi,)).fetchone()[0]), ["SQL", "Python"])

    def test_answer_takes_a_multipart_upload_for_a_file_question(self):
        import hashlib
        pdf = b"%PDF-1.4 cover letter"
        app_id, qna_id = self.blocked_question(shape={"kind": "file", "options": [], "max_length": None})
        response = self.client.post("/api/answer", headers=self.headers, data={"qna_id": str(qna_id), "application_id": str(app_id)},
                                    files={"file": ("Cover Letter.pdf", pdf, "application/pdf")})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True, "requeued": [app_id]}))
        stored = db.data_dir() / "attachments" / (hashlib.sha256(pdf).hexdigest() + ".pdf")
        self.assertEqual(stored.read_bytes(), pdf)
        self.assertEqual(tuple(self.conn.execute("SELECT answer_value, answer_filename FROM application_qna_bank "
                                                 "WHERE id = ?", (qna_id,)).fetchone()), (str(stored), "Cover Letter.pdf"))

    def test_an_upload_without_a_content_length_is_refused_with_411_before_it_is_read(self):
        # A chunked body declares no size, so the size check could not refuse it
        # before the parser spooled all of it to disk.
        body = (b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"cv.pdf\"\r\n\r\n"
                b"%PDF-1.4\r\n--b--\r\n")
        headers = {**self.headers, "Content-Type": "multipart/form-data; boundary=b"}
        for method, url in (("POST", "/api/resume/parse"), ("PUT", "/api/profile"), ("POST", "/api/answer")):
            with self.subTest(url=url):
                response = self.client.request(method, url, headers=headers, content=iter([body]))
                self.assertEqual((response.status_code, response.json()["error"]), (411, local_api._NO_LENGTH))

    def test_answer_refuses_a_non_pdf_an_oversize_file_and_a_file_for_a_text_question_with_400(self):
        file_app, file_q = self.blocked_question(shape={"kind": "file", "options": [], "max_length": None})
        text_app, text_q = self.blocked_question()
        upload = lambda qna_id, app_id, name, data: self.client.post(  # noqa: E731
            "/api/answer", headers=self.headers, data={"qna_id": str(qna_id), "application_id": str(app_id)},
            files={"file": (name, data)})

        for name, data in (("run.exe", b"MZ"), ("letter.pdf", b"not a pdf"),
                           ("big.pdf", b"%PDF-" + b"0" * local_answers.UPLOAD_MAX_BYTES)):
            with self.subTest(name=name):
                response = upload(file_q, file_app, name, data)
                self.assertEqual(response.status_code, 400)
                self.assertIn("PDF, DOC or DOCX files up to 1 MB", response.json()["error"])
        # So large that the request is refused before the file is read.
        huge = upload(file_q, file_app, "huge.pdf", b"%PDF-" + b"0" * (local_answers.UPLOAD_MAX_BYTES + 100_000))
        self.assertEqual((huge.status_code, "1 MB" in huge.json()["error"]), (400, True))
        self.assertEqual(upload(text_q, text_app, "letter.pdf", b"%PDF-1.4").status_code, 400)
        typed = {"qna_id": file_q, "application_id": file_app, "value": "typed"}
        self.assertEqual(self.client.post("/api/answer", headers=self.headers, json=typed).status_code, 400)
        self.assertFalse((db.data_dir() / "attachments").exists())

    def test_verify_submit_and_retry(self):
        stuck = self.add_row(status="blocked_needs_input", blocked_reason="verify_submit")
        exhausted = self.add_row(status="blocked_needs_input", blocked_reason="attempts_exhausted", attempt_count=3)
        post = lambda path, **body: self.client.post(path, headers=self.headers, json=body or None)  # noqa: E731

        self.assertEqual(post(f"/api/verify-submit/{stuck}", action="maybe").status_code, 400)
        self.assertEqual(post(f"/api/verify-submit/{exhausted}", action="skip").status_code, 404)
        self.assertEqual(post(f"/api/verify-submit/{stuck}", action="confirmed").status_code, 200)
        self.assertEqual(self.row(stuck)["status"], "applied")
        self.assertEqual(post(f"/api/retry/{stuck}").status_code, 404)
        self.assertEqual(post(f"/api/retry/{exhausted}").status_code, 200)
        self.assertEqual(self.row(exhausted)["status"], "pending")

    def test_screenshot_serves_only_a_file_inside_the_screenshots_folder(self):
        folder = db.data_dir() / "screenshots"
        folder.mkdir()
        inside = folder / "1-1.png"
        inside.write_bytes(b"\x89PNG-bytes")
        outside = self.tmp / "careersurge-secret.png"
        outside.write_bytes(b"secret")

        good, _ = self.blocked_question(shot=inside)
        bad, _ = self.blocked_question(shot=outside)
        missing, _ = self.blocked_question()

        served = self.client.get(f"/api/screenshot/{good}", headers=self.headers)
        self.assertEqual((served.status_code, served.content, served.headers["content-type"]),
                         (200, b"\x89PNG-bytes", "image/png"))
        self.assertEqual(self.client.get(f"/api/screenshot/{bad}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/screenshot/{missing}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/api/screenshot/9999", headers=self.headers).status_code, 404)
        self.assertEqual(local_answers.screenshot_file(str(inside)), inside.resolve())


class FailedRouteTests(_ApiCase):
    def test_failed_lists_only_failed_rows_newest_first_with_their_listing(self):
        old = self.add_row(status="failed", skip_reason="listing_closed", attempt_count=1, updated_at="2026-10-17T10:00:00.000Z")
        new = self.add_row(status="failed", skip_reason="error", error="host_not_allowed", attempt_count=2,
                           updated_at="2026-10-18T10:00:00.000Z")
        self.add_row(status="applied")
        self.add_row(status="cancelled", skip_reason="user_skipped")
        self.conn.execute("UPDATE listings SET title = 'Backend Engineer', company = 'Acme' "
                          "WHERE id = (SELECT listing_id FROM job_applications WHERE id = ?)", (new,))

        response = self.client.get("/api/failed", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        rows = response.json()["failed"]
        self.assertEqual([row["id"] for row in rows], [new, old])
        self.assertEqual(rows[0], {"id": new, "title": "Backend Engineer", "company": "Acme", "url": tla.GH_URL,
                                   "skip_reason": "error", "error": "host_not_allowed", "attempt_count": 2,
                                   "updated_at": "2026-10-18T10:00:00.000Z"})
        self.assertEqual((rows[1]["skip_reason"], rows[1]["error"], rows[1]["title"]), ("listing_closed", None, None))

    def test_failed_returns_at_most_200_rows(self):
        for _ in range(201):
            self.add_row(status="failed", skip_reason="timeout")
        self.assertEqual(len(self.client.get("/api/failed", headers=self.headers).json()["failed"]), 200)

    def test_failed_needs_the_token(self):
        self.assertEqual(self.client.get("/api/failed").status_code, 401)


class OnboardingRouteTests(_ApiCase):
    """The keys, resume, profile and global-question routes behind the onboarding and Profile screens."""
    PDF = b"%PDF-1.4 first resume"

    def setUp(self):
        super().setUp()
        previous = keyring.get_keyring()
        keyring.set_keyring(fail.Keyring())     # no OS keychain: keys go to secrets.json in the temp data dir
        self.addCleanup(keyring.set_keyring, previous)
        for name in secrets.KNOWN_SECRETS:      # a developer's own exported key must not leak into the case
            env = mock.patch.dict(os.environ)
            env.start()
            self.addCleanup(env.stop)
            os.environ.pop(name, None)

    def put(self, path, **body):
        return self.client.put(path, headers=self.headers, json=body)

    def save_profile(self, fields, file=None):
        files = {"file": ("cv.pdf", file, "application/pdf")} if file is not None else None
        return self.client.put("/api/profile", headers=self.headers, data={"fields": json.dumps(fields)}, files=files)

    CONSENT = {"auto_consent_authorized": True, "background_check_authorized": False,
               "arbitration_authorized": False}

    def test_consent_is_saved_stamped_and_returned_with_the_profile(self):
        self.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)

        response = self.put("/api/profile/consent", **self.CONSENT)

        profile = response.json()["profile"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual((profile["auto_consent_authorized"], profile["background_check_authorized"]), (1, 0))
        self.assertEqual(profile["consent_tos_version"], local_profile.CONSENT_TOS_VERSION)
        self.assertTrue(profile["consent_authorized_at"])
        self.assertEqual(self.client.get("/api/profile", headers=self.headers).json()["profile"], profile)

    def test_get_profile_lists_each_consent_flags_current_wording_version(self):
        body = self.client.get("/api/profile", headers=self.headers).json()

        self.assertEqual(body["consent_versions"], {"auto_consent_authorized": "2026-10-28",
                                                    "background_check_authorized": "2026-10-28",
                                                    "arbitration_authorized": "2026-10-28"})

    def test_consent_with_an_extra_or_missing_key_or_a_non_boolean_is_400_and_writes_nothing(self):
        self.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)
        missing = {key: value for key, value in self.CONSENT.items() if key != "arbitration_authorized"}
        for body in ({**self.CONSENT, "phone": True}, missing, {**self.CONSENT, "auto_consent_authorized": "yes"}):
            with self.subTest(body=body):
                self.assertEqual(self.put("/api/profile/consent", **body).status_code, 400)
        not_an_object = self.client.put("/api/profile/consent", headers=self.headers, json=[True])
        self.assertEqual(not_an_object.status_code, 400)
        self.assertEqual(self.client.get("/api/profile", headers=self.headers).json()["profile"]["auto_consent_authorized"], 0)

    def test_consent_before_a_profile_exists_is_409(self):
        self.assertEqual(self.put("/api/profile/consent", **self.CONSENT).status_code, 409)

    def test_the_profile_route_refuses_consent_columns(self):
        self.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)

        response = self.save_profile({"email": "j@x.com", "full_name": "Jane", "auto_consent_authorized": 1})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/profile", headers=self.headers).json()["profile"]["auto_consent_authorized"], 0)

    def test_a_key_is_stored_and_reported_by_its_last_four_and_never_returned(self):
        self.assertEqual(self.client.get("/api/keys/JEV_API_KEY", headers=self.headers).json(),
                         {"ok": True, "set": False, "last4": None, "rejected": False})

        response = self.put("/api/keys/JEV_API_KEY", value="  jev_secret_key_9876 ")

        self.assertEqual((response.status_code, response.json()), (200, {"ok": True, "set": True, "last4": "9876", "rejected": False}))
        again = self.client.get("/api/keys/JEV_API_KEY", headers=self.headers)
        self.assertEqual(again.json(), {"ok": True, "set": True, "last4": "9876", "rejected": False})
        self.assertNotIn("jev_secret", response.text + again.text)
        self.assertEqual(secrets.get_secret("JEV_API_KEY"), "jev_secret_key_9876")

    def test_a_key_pasted_with_its_env_line_prefix_is_stored_without_it(self):
        # A stored Jev key that began with "=" got a 401 from TypeSafe on every call
        # and the app ran without Jev (regex resume parse, no label mapping) while Settings said "set".
        # The key is fake; gitleaks:allow tells the secret scan these two lines are meant to look like keys.
        pasted_forms = ("=jev_secret_key_9876", "JEV_API_KEY=jev_secret_key_9876", '"jev_secret_key_9876"',  # gitleaks:allow
                        "export JEV_API_KEY = 'jev_secret_key_9876'", "jev_api_key=jev_secret_key_9876")  # gitleaks:allow
        for pasted in pasted_forms:
            with self.subTest(pasted=pasted):
                self.put("/api/keys/JEV_API_KEY", value=pasted)
                self.assertEqual(secrets.get_secret("JEV_API_KEY"), "jev_secret_key_9876")

    def test_a_refused_jev_key_is_reported_until_a_new_key_is_saved(self):
        # jev.choose sets the flag on a 401/403 (test_local_profile pins that); Settings reads it here.
        db.set_setting(jev.KEY_REJECTED_SETTING, True)
        self.assertFalse(self.client.get("/api/keys/JEV_API_KEY", headers=self.headers).json()["rejected"])  # no key

        self.put("/api/keys/JEV_API_KEY", value="jev_secret_key_9876")
        db.set_setting(jev.KEY_REJECTED_SETTING, True)

        self.assertTrue(self.client.get("/api/keys/JEV_API_KEY", headers=self.headers).json()["rejected"])
        self.assertFalse(self.client.get("/api/keys/APIFY_TOKEN", headers=self.headers).json()["rejected"])

        self.assertFalse(self.put("/api/keys/JEV_API_KEY", value="jev_new_key_1234").json()["rejected"])
        self.assertFalse(self.client.get("/api/keys/JEV_API_KEY", headers=self.headers).json()["rejected"])

    def test_an_unknown_key_name_is_404_and_an_empty_key_is_400(self):
        self.assertEqual(self.put("/api/keys/OTHER", value="x").status_code, 404)
        self.assertEqual(self.client.get("/api/keys/OTHER", headers=self.headers).status_code, 404)
        self.assertEqual(self.put("/api/keys/APIFY_TOKEN", value="  ").status_code, 400)
        self.assertFalse(secrets.secret_status("APIFY_TOKEN")["set"])

    def test_parse_refuses_a_text_field_sent_as_the_file_part_with_400(self):
        response = self.client.post("/api/resume/parse", headers=self.headers, data={"file": "not a file"})
        self.assertEqual(response.status_code, 400)

    def test_parse_returns_the_draft_stored_for_the_next_open(self):
        draft = {"full_name": {"value": "Jane Doe", "candidates": ["Jane Doe"], "confidence": 0, "source": "regex_fallback"}}
        with mock.patch.object(local_profile, "parse_resume", return_value=draft) as parse:
            response = self.client.post("/api/resume/parse", headers=self.headers,
                                        files={"file": ("cv.pdf", self.PDF, "application/pdf")})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True, "draft": draft}))
        parse.assert_called_once_with(self.PDF)

    def test_parse_then_confirm_with_no_file_saves_the_profile_with_the_parsed_resume(self):
        fixtures = Path(__file__).resolve().parent / "fixtures" / "resumes"
        data = (fixtures / "one_column.pdf").read_bytes()
        with mock.patch.object(local_profile.jev, "choose", side_effect=local_profile.jev.JevUnavailable("rejected")):
            parsed = self.client.post("/api/resume/parse", headers=self.headers,
                                      files={"file": ("one_column.pdf", data, "application/pdf")}).json()["draft"]
        self.assertTrue(Path(parsed["resume_path"]).is_file())

        # The screen autosaves its copy of the draft (and may send a path of its own); then the app is "reopened".
        self.put("/api/profile/draft", draft={**parsed, "resume_path": "/etc/passwd"})
        reopened = self.client.get("/api/profile", headers=self.headers).json()["draft"]
        self.assertEqual(reopened["resume_path"], parsed["resume_path"])

        saved = self.save_profile({"email": "jane.doe@example.com", "full_name": "Jane Q. Doe"})

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(Path(saved.json()["profile"]["resume_path"]).read_bytes(), data)
        self.assertIsNone(self.client.get("/api/profile", headers=self.headers).json()["draft"])

    def test_parse_refuses_a_big_pdf_and_a_docx_with_the_1_mb_rule(self):
        for name, data in (("big.pdf", b"%PDF-" + b"0" * local_profile.RESUME_MAX_BYTES),
                           ("huge.pdf", b"%PDF-" + b"0" * (local_profile.RESUME_MAX_BYTES + 100_000)),
                           ("cv.docx", b"PK\x03\x04 word")):
            with self.subTest(name=name):
                response = self.client.post("/api/resume/parse", headers=self.headers, files={"file": (name, data)})
                self.assertEqual((response.status_code, response.json()["error"]), (400, local_profile.RESUME_RULE))
        self.assertEqual(self.client.post("/api/resume/parse", headers=self.headers,
                                          files={"other": ("cv.pdf", self.PDF)}).status_code, 400)

    def test_parse_with_a_rejected_jev_key_falls_back_to_regex_and_still_answers_200(self):
        fixtures = Path(__file__).resolve().parent / "fixtures" / "resumes"
        name = sorted(os.listdir(fixtures))[0]
        data = (fixtures / name).read_bytes()
        with mock.patch.object(local_profile.jev, "choose", side_effect=local_profile.jev.JevUnavailable("rejected")):
            response = self.client.post("/api/resume/parse", headers=self.headers, files={"file": (name, data)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({field["source"] for key, field in response.json()["draft"].items()
                          if key in ("full_name", "email")} - {"regex_fallback", "none"}, set())

    def test_draft_round_trips_through_the_profile_route_until_the_profile_is_saved(self):
        empty = self.client.get("/api/profile", headers=self.headers).json()
        self.assertEqual((empty["ok"], empty["profile"], empty["draft"]), (True, None, None))
        self.assertEqual(self.put("/api/profile/draft", draft={"phone": {"value": "555"}}).status_code, 200)
        self.assertEqual(self.client.get("/api/profile", headers=self.headers).json()["draft"], {"phone": {"value": "555"}})

        saved = self.save_profile({"email": "j@x.com", "full_name": "Jane", "phone": "555"}, self.PDF)

        self.assertEqual((saved.status_code, saved.json()["profile"]["phone"]), (200, "555"))
        reread = self.client.get("/api/profile", headers=self.headers).json()
        self.assertEqual((reread["profile"]["email"], reread["draft"]), ("j@x.com", None))

    def test_profile_save_without_email_or_name_is_400_and_writes_nothing(self):
        for fields in ({"full_name": "Jane"}, {"email": "j@x.com", "full_name": " "}):
            with self.subTest(fields=fields):
                response = self.save_profile(fields, self.PDF)
                self.assertEqual(response.status_code, 400)
                self.assertRegex(response.json()["error"], "email is required|full_name is required")
                self.assertIsNone(local_profile.get_profile())

    def test_profile_edit_without_a_new_resume_changes_the_phone_and_keeps_resume_path(self):
        self.save_profile({"email": "j@x.com", "full_name": "Jane", "phone": "555-0100"}, self.PDF)
        resume_path = local_profile.get_profile()["resume_path"]

        response = self.save_profile({"email": "j@x.com", "full_name": "Jane", "phone": "555-0199"})

        profile = response.json()["profile"]
        self.assertEqual((response.status_code, profile["phone"], profile["resume_path"]), (200, "555-0199", resume_path))

    def test_names_and_location_are_derived_for_the_form_filler_unless_sent(self):
        self.save_profile({"email": "j@x.com", "full_name": "Jane Q Doe", "city": "Austin", "state": "TX"}, self.PDF)
        profile = local_profile.get_profile()
        self.assertEqual((profile["first_name"], profile["last_name"], profile["current_location"]),
                         ("Jane", "Doe", "Austin, TX"))

        self.save_profile({"email": "j@x.com", "full_name": "Cher", "first_name": "Cherilyn"})
        profile = local_profile.get_profile()
        self.assertEqual((profile["first_name"], profile["last_name"]), ("Cherilyn", None))
        self.assertEqual(profile["current_location"], "Austin, TX")    # a request without city or state leaves it

    def test_a_partial_save_keeps_current_location_and_only_clearing_city_and_state_clears_it(self):
        self.save_profile({"email": "j@x.com", "full_name": "Jane", "city": "Austin", "state": "TX"}, self.PDF)

        self.save_profile({"email": "j@x.com", "full_name": "Jane", "phone": "555-0199"})    # the Profile phone edit
        profile = local_profile.get_profile()
        self.assertEqual((profile["phone"], profile["city"], profile["current_location"]), ("555-0199", "Austin", "Austin, TX"))

        self.save_profile({"email": "j@x.com", "full_name": "Jane", "city": None, "state": None})
        self.assertIsNone(local_profile.get_profile()["current_location"])

    def test_the_first_profile_save_needs_a_resume_and_a_bad_fields_part_is_400(self):
        self.assertEqual(self.save_profile({"email": "j@x.com", "full_name": "Jane"}).status_code, 400)
        for raw in ("not json", "[1]", ""):
            with self.subTest(raw=raw):
                response = self.client.put("/api/profile", headers=self.headers, data={"fields": raw},
                                           files={"file": ("cv.pdf", self.PDF)})
                self.assertEqual(response.status_code, 400)

    def test_saving_the_profile_seeds_the_global_questions_and_each_can_be_answered(self):
        self.assertEqual(self.client.get("/api/global-questions", headers=self.headers).json()["questions"], [])
        self.save_profile({"email": "j@x.com", "full_name": "Jane"}, self.PDF)
        self.save_profile({"email": "j@x.com", "full_name": "Jane"})          # a second save seeds nothing new
        questions = self.client.get("/api/global-questions", headers=self.headers).json()["questions"]
        self.assertEqual(len(questions), len(local_profile.GLOBAL_SEED))

        key = questions[0]["normalized_question"]
        post = lambda **body: self.client.post("/api/global-questions/answer", headers=self.headers, json=body)  # noqa: E731
        self.assertEqual(post(normalized_question=key, value=" Yes ").status_code, 200)
        self.assertEqual(post(normalized_question=key, value=" ").status_code, 400)
        self.assertEqual(post(normalized_question="never seeded", value="x").status_code, 400)
        answered = self.client.get("/api/global-questions", headers=self.headers).json()["questions"]
        self.assertEqual(answered[0]["answer"], "Yes")

        # Every question names its control, and an answer outside it is a 400 that says what fits.
        self.assertEqual(answered[0]["field_shape"], {"kind": "single", "options": ["Yes", "No"], "max_length": None})
        self.assertNotIn("how did you hear about us", [q["normalized_question"] for q in answered])
        refused = post(normalized_question=key, value="sure")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Yes, No", refused.json()["error"])
        salary = next(q["normalized_question"] for q in answered if q["field_shape"]["kind"] == "number")
        self.assertEqual(post(normalized_question=salary, value="$120k").status_code, 400)
        self.assertEqual(post(normalized_question=salary, value="120000").status_code, 200)


class SettingsRouteTests(_ApiCase):
    """`GET`/`PUT /api/settings` and the Run now routes."""

    def put(self, **settings):
        return self.client.put("/api/settings", headers=self.headers, json={"settings": settings})

    def test_defaults_before_anything_is_saved(self):
        settings = self.client.get("/api/settings", headers=self.headers).json()["settings"]
        self.assertEqual(settings, {
            "sourcing_filters": {}, "sourcing_time_range": "auto", "sourcing_monthly_budget_usd": 5,
            "sourcing_confirmed_price_usd": None, "sourcing_interval": "4h", "apply_cooldown_floor_s": 60})

    def test_the_budget_figures_come_with_the_settings(self):
        body = self.client.get("/api/settings", headers=self.headers).json()
        self.assertEqual(body["spent_this_month_usd"], 0)
        self.assertRegex(body["budget_resets_on"], r"^\d{4}-\d{2}-01$")

        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, status, cost_usd) VALUES ('r1', 'succeeded', 1.2)")
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, status, cost_usd) VALUES ('r2', 'started', NULL)")
        body = self.client.get("/api/settings", headers=self.headers).json()
        self.assertEqual(body["spent_this_month_usd"], 1.2)

    def test_the_reset_date_rolls_over_the_year(self):
        december = datetime.datetime(2026, 12, 20, tzinfo=datetime.timezone.utc)
        clock = mock.Mock(wraps=datetime)
        clock.datetime = mock.Mock()
        clock.datetime.now.return_value = december
        with mock.patch.object(local_api, "datetime", clock):
            body = self.client.get("/api/settings", headers=self.headers).json()
        self.assertEqual(body["budget_resets_on"], "2027-01-01")

    def test_valid_settings_are_stored_cleaned_and_read_back(self):
        filters = {"titles": [" Backend Engineer ", "", "Backend Engineer"], "locations": ["Austin, TX"],
                   "employment_types": ["FULL_TIME"], "work_arrangements": ["Remote OK"], "experience_years": ["2-5"],
                   "title_exclusions": ["intern"], "only_with_pay": True, "visa_sponsorship": False,
                   "companies": [], "company_exclusions": ["Bad Co"]}
        response = self.put(sourcing_filters=filters, sourcing_time_range="24h", sourcing_monthly_budget_usd=7.5,
                            sourcing_interval="1h", apply_cooldown_floor_s=30, sourcing_confirmed_price_usd=0.012)
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))

        settings = self.client.get("/api/settings", headers=self.headers).json()["settings"]
        self.assertEqual(settings["sourcing_filters"], {**filters, "titles": ["Backend Engineer"]})
        self.assertEqual((settings["sourcing_time_range"], settings["sourcing_monthly_budget_usd"],
                          settings["sourcing_interval"], settings["apply_cooldown_floor_s"],
                          settings["sourcing_confirmed_price_usd"]), ("24h", 7.5, "1h", 30, 0.012))

    def test_an_unknown_key_or_a_bad_value_is_400_and_nothing_is_stored(self):
        bad = ({"profile_draft": {}}, {"apply_cooldown_floor_s": 10}, {"apply_cooldown_floor_s": True},
               {"sourcing_interval": "daily"}, {"sourcing_time_range": "6m"}, {"sourcing_monthly_budget_usd": 0},
               {"sourcing_monthly_budget_usd": "5"}, {"sourcing_filters": []},
               {"sourcing_filters": {"titles": "backend"}}, {"sourcing_filters": {"surprise": ["x"]}},
               {"sourcing_filters": {"employment_types": ["WEEKENDS"]}},
               {"sourcing_filters": {"only_with_pay": "yes"}})
        for settings in bad:
            with self.subTest(settings=settings):
                self.assertEqual(self.put(**{"sourcing_time_range": "7d", **settings}).status_code, 400)
        self.assertIsNone(db.get_setting("sourcing_time_range"))     # the valid key sent beside each bad one was not stored

    def test_nan_infinity_and_a_list_where_text_belongs_are_400_not_500(self):
        for settings in ({"sourcing_monthly_budget_usd": float("inf")}, {"apply_cooldown_floor_s": float("nan")},
                         {"sourcing_confirmed_price_usd": float("nan")}, {"sourcing_interval": []},
                         {"sourcing_interval": {}}, {"sourcing_time_range": ["24h"]}):
            with self.subTest(settings=settings):
                response = self.client.put("/api/settings", headers={**self.headers, "Content-Type": "application/json"},
                                           content=json.dumps({"settings": settings}))
                self.assertEqual(response.status_code, 400)
        price = self.client.post("/api/sourcing/confirm-price", headers={**self.headers, "Content-Type": "application/json"},
                                 content='{"price": NaN}')
        self.assertIn(price.status_code, (400, 422))
        self.assertIsNone(db.get_setting("sourcing_confirmed_price_usd"))

    def test_the_floor_error_names_the_30_second_minimum(self):
        response = self.put(apply_cooldown_floor_s=10)
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 30 seconds", response.json()["error"])

    def test_run_now_is_202_with_the_run_id_and_passes_confirm_backlog_on(self):
        with mock.patch.object(local_queues, "start_sourcing", mock.AsyncMock(return_value="run-9")) as start:
            response = self.client.post("/api/sourcing/run", headers=self.headers, json={"confirm_backlog": True})
        self.assertEqual((response.status_code, response.json()), (202, {"ok": True, "run_id": "run-9"}))
        start.assert_awaited_once_with(True)

    def test_every_refusal_is_one_409_with_its_reason_and_detail(self):
        for reason, detail in (("price_unconfirmed", {"price": 0.012}), ("backlog_unconfirmed", {"waiting": 30}),
                               ("budget", {"remaining": 1.5, "minimal": 2.0}), ("run_in_progress", {"runs": 1})):
            with self.subTest(reason=reason):
                refused = local_sourcing.SourcingRefused(reason, **detail)
                with mock.patch.object(local_queues, "start_sourcing", mock.AsyncMock(side_effect=refused)):
                    response = self.client.post("/api/sourcing/run", headers=self.headers, json={})
                body = response.json()
                self.assertEqual((response.status_code, body["ok"], body["reason"], body["detail"]),
                                 (409, False, reason, detail))
                self.assertIn(reason, body["error"])

    def test_a_failure_before_the_run_starts_is_a_502_with_a_message_not_a_bare_500(self):
        with mock.patch.object(local_queues, "start_sourcing", mock.AsyncMock(side_effect=OSError("down"))), \
                self.assertLogs("careersurge.local_api", "ERROR"):
            response = self.client.post("/api/sourcing/run", headers=self.headers, json={})
        self.assertEqual((response.status_code, response.json()["ok"]), (502, False))
        self.assertIn("Could not start", response.json()["error"])

    def test_confirm_price_stores_the_accepted_price_and_refuses_a_bad_one(self):
        post = lambda price: self.client.post("/api/sourcing/confirm-price", headers=self.headers, json={"price": price})  # noqa: E731
        self.assertEqual(post(0.012).status_code, 200)
        self.assertEqual(db.get_setting("sourcing_confirmed_price_usd"), 0.012)
        self.assertEqual(post(0).status_code, 400)
        self.assertEqual(db.get_setting("sourcing_confirmed_price_usd"), 0.012)

    def test_progress_carries_the_newest_sourcing_run_or_none(self):
        self.assertIsNone(self.client.get("/api/progress", headers=self.headers).json()["sourcing_run"])
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, started_at, status) VALUES ('old', '2026-10-01T00:00:00.000Z', 'succeeded')")
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, started_at, status) VALUES ('new', '2026-10-13T00:00:00.000Z', 'started')")
        self.assertEqual(self.client.get("/api/progress", headers=self.headers).json()["sourcing_run"],
                         {"status": "started", "started_at": "2026-10-13T00:00:00.000Z", "ingested_at": None,
                          "rows_fetched": None, "rows_skipped": None, "cost_usd": None})
        # Once it ended, what the check found, so the screen can say "0 jobs" rather than nothing.
        self.conn.execute("UPDATE sourcing_runs SET status = 'succeeded', ingested_at = '2026-10-13T00:00:02.000Z', "
                          "rows_fetched = 0, rows_skipped = 0, cost_usd = 0 WHERE apify_run_id = 'new'")
        run = self.client.get("/api/progress", headers=self.headers).json()["sourcing_run"]
        self.assertEqual((run["status"], run["rows_fetched"], run["ingested_at"]),
                         ("succeeded", 0, "2026-10-13T00:00:02.000Z"))


class ShellTests(_ApiCase):
    """The static UI, the approval, progress and queue routes."""

    def setUp(self):
        super().setUp()
        self.static = self.tmp / "dist"
        (self.static / "assets").mkdir(parents=True)
        (self.static / "index.html").write_text("<div id=root></div>")
        (self.static / "assets" / "app.js").write_text("console.log(1)")
        (self.tmp / "secret.txt").write_text("not for the web")
        self.ui = TestClient(local_api.create_app(TOKEN, self.static), base_url="http://127.0.0.1:8123")
        self.addCleanup(self.ui.close)

    def test_the_page_needs_no_token_but_the_api_does_and_a_foreign_host_is_refused(self):
        self.assertEqual(self.ui.get("/").status_code, 200)
        self.assertEqual(self.ui.get("/api/batch").status_code, 401)
        self.assertEqual(self.ui.get("/", headers={"Host": "evil.com"}).status_code, 400)
        self.assertEqual(self.ui.get("/", headers={"Origin": "http://evil.com"}).status_code, 403)

    def test_files_are_served_and_any_other_path_gets_the_index_for_the_spa(self):
        self.assertEqual(self.ui.get("/assets/app.js").text, "console.log(1)")
        self.assertEqual(self.ui.get("/progress").text, "<div id=root></div>")

    def test_a_path_cannot_climb_out_of_the_static_dir_and_unknown_api_paths_are_404(self):
        self.assertEqual(self.ui.get("/..%2fsecret.txt").text, "<div id=root></div>")
        self.assertEqual(self.ui.get("/api/nope", headers=self.headers).status_code, 404)

    def test_a_network_drive_or_absolute_path_gets_the_index_without_touching_the_filesystem(self):
        # On Windows `root / "\\\\host\\share"` is that share, and resolving it
        # connects to the host with the user's login hash. The route needs no token, so it must never get there.
        seen = []
        real_resolve = Path.resolve

        def spy(path, *args, **kwargs):
            seen.append(str(path))
            return real_resolve(path, *args, **kwargs)
        with mock.patch.object(Path, "resolve", spy):
            for url in ("/%5C%5Cexample.invalid%5Cshare%5Cx", "/%2F%2Fexample.invalid/share/x",
                        "/C:/Windows/win.ini", "/assets/..%2F..%2Fsecret.txt", "/a%5C..%5Csecret.txt"):
                with self.subTest(url=url):
                    self.assertEqual(self.ui.get(url).text, "<div id=root></div>")
        self.assertEqual([path for path in seen if "example.invalid" in path or "secret" in path], [])

    def test_without_a_static_dir_a_page_is_404(self):
        self.assertEqual(self.client.get("/").status_code, 404)

    def test_batch_lists_prepared_jobs_and_approve_moves_them(self):
        self.add_profile()
        keep = self.add_row(status="prepared")
        skip = self.add_row(status="prepared")

        listed = self.client.get("/api/batch", headers=self.headers).json()
        self.assertEqual([job["id"] for job in listed["jobs"]], [keep, skip])
        self.assertEqual(listed["inputs"]["profile"]["full_name"], "Jane Doe")

        response = self.client.post("/api/batch/approve", headers=self.headers,
                                    json={"approve_ids": [keep], "skip_ids": [skip]})
        body = response.json()
        self.assertEqual((response.status_code, body["approved"], body["skipped"]), (200, 1, 1))
        self.assertEqual((self.row(keep)["status"], self.row(skip)["status"]), ("pending", "cancelled"))

    def test_approve_without_a_profile_is_a_400_with_the_reason(self):
        keep = self.add_row(status="prepared")
        response = self.client.post("/api/batch/approve", headers=self.headers, json={"approve_ids": [keep]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("profile", response.json()["error"])
        self.assertEqual(self.row(keep)["status"], "prepared")

    def test_progress_reports_queues_counts_and_the_last_refusal(self):
        self.add_row(status="pending", approved_at="2026-10-01T00:00:00.000Z")
        self.add_row(status="applied")
        db.set_setting("sourcing_last_refusal", {"reason": "no_key", "detail": "", "at": "2026-10-13T00:00:00.000Z"})

        body = self.client.get("/api/progress", headers=self.headers).json()

        self.assertEqual(body["counts"], {"pending": 1, "applied": 1})
        self.assertEqual(body["queues"]["greenhouse"]["pending"], 1)
        self.assertEqual(body["queues"]["lever"]["state"], "idle")
        self.assertEqual(body["sourcing_refusal"]["reason"], "no_key")
        self.assertIsInstance(body["server_time"], float)

    def test_pause_and_resume_one_ats_or_all_and_an_unknown_ats_is_400(self):
        post = lambda path, **body: self.client.post(path, headers=self.headers, json=body)  # noqa: E731
        self.assertEqual(post("/api/queues/pause", ats="lever").status_code, 200)
        self.assertEqual(db.get_setting("apply_paused"), {"lever": "user"})
        self.assertEqual(post("/api/queues/pause").status_code, 200)
        self.assertEqual(set(db.get_setting("apply_paused")), {"greenhouse", "lever", "ashby"})
        self.assertEqual(post("/api/queues/resume", ats="lever").status_code, 200)
        self.assertEqual(set(db.get_setting("apply_paused")), {"greenhouse", "ashby"})
        self.assertEqual(post("/api/queues/resume").status_code, 200)
        self.assertEqual(db.get_setting("apply_paused"), {})
        self.assertEqual(post("/api/queues/pause", ats="workday").status_code, 400)


if __name__ == "__main__":
    unittest.main()
