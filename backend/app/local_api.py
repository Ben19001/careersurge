"""The local app's HTTP API: the approval, progress, answer, tray, onboarding and
Settings routes behind a per-run token, and the built UI.

`create_app(token, static_dir)` is called by `app.local_main`, which picks a random token and a random
port on 127.0.0.1, serves this app with uvicorn, and passes the token to the pywebview window in the
URL fragment. The UI is served by this same app, so requests are same-origin and no CORS is configured:
CORS only widens what other origins may READ, and it never stops a cross-site POST from being sent. What
stops that is the Origin check below. Flask-free and Supabase-free; the work is in
`app.services.local_answers`, `local_batch`, `local_handoff`, `local_profile`, `local_queues` and `local_sourcing`.
"""
import asyncio
import datetime
import hmac
import json
import logging
import math
import re
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from app.services import jev, local_answers, local_batch, local_handoff, local_profile, local_queues, local_sourcing
from app.store import db, secrets

logger = logging.getLogger("careersurge.local_api")

TOKEN_HEADER = "X-CareerSurge-Token"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
# What a multipart request may weigh beyond its file: field names, boundaries, the other parts.
_MULTIPART_SLACK = 64 * 1024
# An upload without a declared length is refused (411) rather than spooled to disk unmeasured.
_NO_LENGTH = "send the upload with a Content-Length header"
# The settings the Settings screen reads and writes, with what GET returns while one was never saved.
_SETTING_DEFAULTS = {
    "sourcing_filters": {},
    "sourcing_time_range": "auto",
    "sourcing_monthly_budget_usd": 5,
    "sourcing_confirmed_price_usd": None,
    "sourcing_interval": local_queues.DEFAULT_SOURCING_INTERVAL,
    "apply_cooldown_floor_s": local_queues.DEFAULT_COOLDOWN_FLOOR_S,
}


class AnswerBody(BaseModel):
    """`POST /api/answer`: `application_id` is the tray card the user answered on; `value` is a string,
    or a list of strings for a checkbox question.

    An upload sends no `value`; its bytes are the multipart `file` part.
    """
    qna_id: int
    application_id: int
    value: str | list[str] | None = None
    scope: str | None = None


class VerifySubmitBody(BaseModel):
    action: str


class OtpBody(BaseModel):
    """`POST /api/otp/{id}`: the code the user read in their email."""
    code: str


class ApproveBody(BaseModel):
    """`POST /api/batch/approve`: the `prepared` application ids to approve and to skip."""
    approve_ids: list[int] = []
    skip_ids: list[int] = []


class KeyBody(BaseModel):
    """`PUT /api/keys/{name}`: the key to store; it never comes back, only `set` and `last4` do."""
    value: str


class DraftBody(BaseModel):
    """`PUT /api/profile/draft`: the review screen's whole state, saved on every edit."""
    draft: dict


class GlobalAnswerBody(BaseModel):
    """`POST /api/global-questions/answer`: `normalized_question` is the row's id from `GET /api/global-questions`."""
    normalized_question: str
    value: str


class SettingsBody(BaseModel):
    """`PUT /api/settings`: the settings to change, by key; keys not sent keep their value."""
    settings: dict


class RunBody(BaseModel):
    """`POST /api/sourcing/run`: `confirm_backlog` is true after the user agreed to fetch with jobs already waiting."""
    confirm_backlog: bool = False


class PriceBody(BaseModel):
    """`POST /api/sourcing/confirm-price`: the per-job price (USD) the user accepted."""
    price: float


class QueueBody(BaseModel):
    """`POST /api/queues/pause` and `/resume`: one ATS, or all three when `ats` is omitted."""
    ats: str | None = None


def _error(message: str, status: int) -> JSONResponse:
    """The error envelope every route and guard returns: `{"ok": false, "error": message}`."""
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _declared_length(request: Request) -> int | None:
    """The request's `Content-Length` as a number, or None when it is missing or not a whole number.

    Used by the three upload routes, which refuse an oversize body before the multipart parser spools
    it to disk. A chunked request carries no length, so without this it would be spooled whole before
    any limit applied; the window's `fetch` always sends one.
    """
    raw = request.headers.get("content-length")
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw)


def create_app(token: str, static_dir: str | Path | None = None) -> FastAPI:
    """The FastAPI app. Every request must pass the guards, in order, before any route runs.

    1. `Host` is `127.0.0.1` or `localhost` (400): refuses DNS rebinding, where a hostile site's own
       domain is pointed at 127.0.0.1 so the browser treats our API as that site's origin.
    2. `Origin`, when the browser sends one, equals this app's own origin (403): a form or fetch
       from another site carries its own Origin.
    3. `X-CareerSurge-Token` equals `token` (401), compared in constant time. Only `/api/` paths
       need it: a page load cannot send a header, and the built UI holds nothing private (it is the
       same files for everyone); every byte of user data is behind `/api/`.

    With `static_dir` the built UI is served from it, `index.html` for any path that is not a file
    (the SPA routes client-side).
    """
    if not token:
        raise ValueError("an empty token would leave the API open")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host.rsplit(":", 1)[0] not in _LOOPBACK_HOSTS:
            return _error("bad host", 400)
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{host}":
            return _error("bad origin", 403)
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            sent = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                return _error("bad token", 401)
        return await call_next(request)

    @app.get("/api/batch")
    def batch():
        return {"ok": True, **local_batch.prepared_batch()}

    @app.post("/api/batch/approve")
    def approve(body: ApproveBody):
        try:
            result = local_batch.approve(body.approve_ids, body.skip_ids)
        except ValueError as exc:
            return _error(str(exc), 400)
        return {"ok": True, **result}

    @app.get("/api/progress")
    def progress():
        """Queue state per ATS, application counts by status, the last sourcing refusal, the newest sourcing run and the server clock.

        `sourcing_run` is `{status, started_at, ingested_at, rows_fetched, rows_skipped, cost_usd}` of the
        newest `sourcing_runs` row, or None: the screen shows "Fetching jobs" while it is `started`,
        and once it ended, what that check found, so a check that found nothing still reads as done.

        `server_time` (epoch seconds) is what the screen subtracts from each queue's `next_at`, so the
        "next application in 0:47" countdown does not depend on the window's clock.
        """
        conn = db.get_db()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM job_applications GROUP BY status").fetchall()
            newest_run = conn.execute(
                "SELECT status, started_at, ingested_at, rows_fetched, rows_skipped, cost_usd FROM sourcing_runs "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        counts = {}
        for row in rows:
            counts[row["status"]] = row["n"]
        sourcing_run = None
        if newest_run is not None:
            sourcing_run = dict(newest_run)
        return {"ok": True, "queues": local_queues.queue_status(), "counts": counts,
                "sourcing_refusal": db.get_setting("sourcing_last_refusal"),
                "sourcing_run": sourcing_run, "server_time": local_queues.now()}

    @app.get("/api/failed")
    def failed():
        """The newest 200 `failed` applications with their listing, for the Progress screen's Failed list.

        `skip_reason` is `listing_closed`, `unsupported_form`, `timeout` or `error`; `error` is the short
        stored code (`user_input_timeout`, `host_not_allowed`) or None. Capped because it is a list to read,
        not a history. `cancelled` rows (`user_skipped`) are not failures and stay out.
        """
        conn = db.get_db()
        try:
            # Plain SQL: each failed application with its listing, newest change first.
            #   job_applications JOIN listings ON listings.id = listing_id
            rows = conn.execute(
                "SELECT a.id, l.title, l.company, l.url, a.skip_reason, a.error, a.attempt_count, a.updated_at "
                "FROM job_applications a JOIN listings l ON l.id = a.listing_id "
                "WHERE a.status = 'failed' ORDER BY a.updated_at DESC, a.id DESC LIMIT 200").fetchall()
        finally:
            conn.close()
        failed_rows = []
        for row in rows:
            failed_rows.append(dict(row))
        return {"ok": True, "failed": failed_rows}

    @app.get("/api/keys/{name}")
    def key_status(name: str):
        if name not in secrets.KNOWN_SECRETS:
            return _error("unknown key", 404)
        # `rejected`: TypeSafe refused the stored Jev key on its last use. Only Jev reports it.
        status = secrets.secret_status(name)
        rejected = name == "JEV_API_KEY" and status["set"] and bool(db.get_setting(jev.KEY_REJECTED_SETTING))
        return {"ok": True, **status, "rejected": rejected}

    @app.put("/api/keys/{name}")
    def set_key(name: str, body: KeyBody):
        if name not in secrets.KNOWN_SECRETS:
            return _error("unknown key", 404)
        # A key copied out of a `.env` line or a shell arrives as `NAME=key`, `export NAME = key`,
        # `=key` or `"key"`, and the box is a password field, so the user never sees the extra
        # characters. A Jev key stored with a leading "=" was refused by TypeSafe on every call.
        # No real key starts with "=" or is wrapped in quotes, so they are dropped.
        value = body.value.strip()
        value = re.sub(rf"^(?:export\s+)?{re.escape(name)}\s*=", "", value, flags=re.IGNORECASE)
        value = value.strip().removeprefix("=")
        value = value.strip().strip("\"'").strip()
        if not value:
            return _error("the key is empty", 400)
        secrets.set_secret(name, value)
        if name == "JEV_API_KEY":
            db.set_setting(jev.KEY_REJECTED_SETTING, False)   # a new key has not been refused yet
        return {"ok": True, **secrets.secret_status(name), "rejected": False}

    @app.post("/api/resume/parse")
    async def parse_resume(request: Request):
        """Multipart `file`: a PDF resume up to 1 MB. Returns the review draft, which is also stored for the next open.

        The PDF is stored too and the draft's `resume_path` names it, so Confirm after a reopen needs no file.

        A rejected Jev key is not an error here: the parse falls back to regex candidates.
        """
        length = _declared_length(request)
        if length is None:
            return _error(_NO_LENGTH, 411)
        if length > local_profile.RESUME_MAX_BYTES + _MULTIPART_SLACK:
            return _error(local_profile.RESUME_RULE, 400)
        form = await request.form()
        try:
            part = form.get("file")
            if part is None or isinstance(part, str):
                return _error("send the resume as the file part", 400)
            data = await part.read(local_profile.RESUME_MAX_BYTES + 1)
        finally:
            await form.close()
        try:
            draft = await asyncio.to_thread(local_profile.parse_resume, data)
        except ValueError as exc:
            return _error(str(exc), 400)
        return {"ok": True, "draft": draft}

    @app.put("/api/profile/draft")
    def save_draft(body: DraftBody):
        local_profile.save_draft(body.draft)
        return {"ok": True}

    @app.get("/api/profile")
    def profile():
        """The saved profile (None before onboarding finishes its confirm step) and the unfinished review draft.

        `consent_versions` is each consent flag's current wording version; the consent block shows a flag
        stamped with any other version as unticked, because that agreement no longer counts.
        """
        consent_versions = {}
        for flag, (_version_column, _stamp_column, version) in local_profile._CONSENT_FLAGS.items():
            consent_versions[flag] = version
        return {"ok": True, "profile": local_profile.get_profile(), "draft": local_profile.get_draft(),
                "consent_versions": consent_versions}

    @app.put("/api/profile")
    async def save_profile(request: Request):
        """Multipart: `fields` (JSON object of `profile` columns) and, optionally, `file` (a new PDF resume).

        Without a file the stored resume stays; a first save then uses the PDF the draft stored at
        `/api/resume/parse`. `first_name`, `last_name` and `current_location` are filled from `full_name`,
        `city` and `state` unless sent (`current_location` only when `city` or `state` is in the request,
        else the stored one stays). Saving also seeds the global questions the onboarding asks next;
        seeding twice changes nothing.
        """
        length = _declared_length(request)
        if length is None:
            return _error(_NO_LENGTH, 411)
        if length > local_profile.RESUME_MAX_BYTES + _MULTIPART_SLACK:
            return _error(local_profile.RESUME_RULE, 400)
        form = await request.form()
        try:
            raw_fields = form.get("fields")
            part = form.get("file")
            data = None
            if part is not None and not isinstance(part, str):
                data = await part.read(local_profile.RESUME_MAX_BYTES + 1)
        finally:
            await form.close()
        try:
            fields = json.loads(raw_fields or "")
        except ValueError:
            return _error("send the profile as the fields part, a JSON object", 400)
        if not isinstance(fields, dict):
            return _error("send the profile as the fields part, a JSON object", 400)
        # The form filler answers "First name", "Last name" and "Location" from these columns, but the
        # screens edit only the full name, city and state; derive them unless the caller sent them.
        name_parts = str(fields.get("full_name") or "").split()
        if name_parts:
            fields.setdefault("first_name", name_parts[0])
            fields.setdefault("last_name", name_parts[-1] if len(name_parts) > 1 else None)
        # A request with neither city nor state (a partial edit) leaves current_location out, so the stored
        # value stays: save_profile upserts only the columns it is given.
        if "city" in fields or "state" in fields:
            place_parts = []
            for piece in (fields.get("city"), fields.get("state")):
                if piece:
                    place_parts.append(str(piece))
            fields.setdefault("current_location", ", ".join(place_parts) or None)
        try:
            await asyncio.to_thread(local_profile.save_profile, fields, data)
        except ValueError as exc:
            return _error(str(exc), 400)
        local_profile.seed_global_questions()
        return {"ok": True, "profile": local_profile.get_profile()}

    @app.put("/api/profile/consent")
    async def save_consent(request: Request):
        """JSON object with the three consent flags as booleans (400 on any other key, a missing flag or a
        non-boolean); a profile must exist first (409).

        `local_profile.save_consent` stamps each flag's version and time on a 0 -> 1 change, or when a flag
        saved true carries an older version (the user agreeing to the new wording).
        """
        try:
            flags = await request.json()
        except ValueError:
            return _error("send the three consent flags as a JSON object", 400)
        wanted = set(local_profile._CONSENT_FLAGS)
        if not isinstance(flags, dict) or set(flags) != wanted:
            return _error("send exactly auto_consent_authorized, background_check_authorized and arbitration_authorized", 400)
        if not all(isinstance(value, bool) for value in flags.values()):
            return _error("each consent flag must be true or false", 400)
        if local_profile.get_profile() is None:
            return _error("save your profile first", 409)
        local_profile.save_consent(flags)
        return {"ok": True, "profile": local_profile.get_profile()}

    @app.get("/api/global-questions")
    def global_questions():
        return {"ok": True, "questions": local_profile.list_global_questions()}

    @app.post("/api/global-questions/answer")
    def global_answer(body: GlobalAnswerBody):
        value = body.value.strip()
        if not value:
            return _error("the answer is empty", 400)
        try:
            local_profile.save_global_answer(body.normalized_question, value)
        except ValueError as exc:
            return _error(str(exc), 400)
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings():
        """The sourcing and pacing settings the Settings screen edits, each with its default when never saved."""
        settings = {}
        for key, default in _SETTING_DEFAULTS.items():
            settings[key] = db.get_setting(key, default)
        # The same tally start_run refuses on, so the screen shows what the guard will see.
        now = datetime.datetime.now(datetime.timezone.utc)
        conn = db.get_db()
        try:
            spent = local_sourcing.month_spend(conn, now)
        finally:
            conn.close()
        # Any day of the 1st plus 32 days lands in next month; pin it back to the 1st.
        this_month_start = now.replace(day=1)
        next_month_start = (this_month_start + datetime.timedelta(days=32)).replace(day=1)
        return {
            "ok": True,
            "settings": settings,
            "spent_this_month_usd": spent,
            "budget_resets_on": next_month_start.strftime("%Y-%m-%d"),
        }

    @app.put("/api/settings")
    def put_settings(body: SettingsBody):
        """Validate every key sent, then store them all; one bad value (400) stores nothing."""
        valid = {}
        for key, value in body.settings.items():
            if key not in _SETTING_DEFAULTS:
                return _error(f"unknown setting {key!r}", 400)
            is_number = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            if key == "sourcing_filters":
                try:
                    valid[key] = local_sourcing.clean_filters(value)
                except ValueError as exc:
                    return _error(str(exc), 400)
            elif key == "sourcing_time_range":
                if value not in ("auto", "1h", "24h", "7d"):
                    return _error("sourcing_time_range must be auto, 1h, 24h or 7d", 400)
                valid[key] = value
            elif key == "sourcing_interval":
                if value not in ("manual", *local_queues.SOURCING_INTERVAL_S):
                    return _error("sourcing_interval must be manual, 4h or 1h", 400)
                valid[key] = value
            elif key == "apply_cooldown_floor_s":
                if not is_number or value < local_queues.MIN_COOLDOWN_FLOOR_S:
                    return _error(f"the cooldown floor must be at least {local_queues.MIN_COOLDOWN_FLOOR_S} seconds", 400)
                valid[key] = value
            else:
                # sourcing_monthly_budget_usd and sourcing_confirmed_price_usd: a money amount.
                if not is_number or value <= 0:
                    return _error(f"{key} must be a number above 0", 400)
                valid[key] = value
        for key, value in valid.items():
            db.set_setting(key, value)
        return {"ok": True}

    @app.post("/api/sourcing/run")
    async def run_now(body: RunBody):
        """Start an Apify run; `202 {run_id}` once it is started, long before it finishes.

        Every `SourcingRefused` is one `409 {ok, error, reason, detail}`: the screen switches on `reason`
        (a confirm for `price_unconfirmed`, `price_rose` and `backlog_unconfirmed`, a notice for the rest).
        """
        try:
            run_id = await local_queues.start_sourcing(body.confirm_backlog)
        except local_sourcing.SourcingRefused as refused:
            return JSONResponse({"ok": False, "error": f"run refused: {refused.reason}",
                                 "reason": refused.reason, "detail": refused.detail}, status_code=409)
        except Exception:  # noqa: BLE001 - a network or Apify error after the price read: tell the user, nothing was charged for a run that did not start
            logger.exception("Run now failed before the run started")
            return _error("Could not start the job check. Check your connection and try again.", 502)
        return JSONResponse({"ok": True, "run_id": run_id}, status_code=202)

    @app.post("/api/sourcing/confirm-price")
    def confirm_price(body: PriceBody):
        if not math.isfinite(body.price) or body.price <= 0:
            return _error("the price must be a number above 0", 400)
        db.set_setting("sourcing_confirmed_price_usd", body.price)
        return {"ok": True}

    @app.post("/api/queues/pause")
    def pause(body: QueueBody):
        try:
            local_queues.pause(body.ats)
        except ValueError as exc:
            return _error(str(exc), 400)
        return {"ok": True}

    @app.post("/api/queues/resume")
    def resume(body: QueueBody):
        try:
            local_queues.resume(body.ats)
        except ValueError as exc:
            return _error(str(exc), 400)
        return {"ok": True}

    @app.get("/api/blocked")
    def blocked():
        """The tray cards (`local_answers.list_blocked`); an `otp` card also carries `code_wait_s`, how long it waits for the code."""
        cards = local_answers.list_blocked()
        for card in cards:
            if card["blocked_reason"] == "otp":
                card["code_wait_s"] = local_handoff.CODE_WAIT_S
        return {"ok": True, "blocked": cards}

    @app.post("/api/otp/{app_id}")
    async def otp(app_id: int, body: OtpBody):
        """Hand the emailed code to the browser context parked for `app_id`.

        `async` so the Future is resolved on the event loop that owns it. 404 when nothing waits for a
        code: it timed out, the app restarted, or a code was already sent for this application.
        """
        code = body.code.strip()
        if not code or len(code) > local_handoff.CODE_MAX_LENGTH:
            return _error("enter the code from the email", 400)
        if not local_handoff.submit_code(app_id, code):
            return _error("that application is not waiting on a code", 404)
        return {"ok": True}

    @app.post("/api/captcha/{app_id}/open")
    async def open_captcha(app_id: int):
        """Open the application in a headed browser window for the user to solve its captcha.

        Returns as soon as the row is claimed; the window runs on. 409 when the row is not
        `blocked/captcha` (it was already opened, or the user skipped it).
        """
        if not await local_handoff.open_in_browser(app_id):
            return _error("that application is not waiting on a captcha", 409)
        return {"ok": True}

    @app.post("/api/captcha/{app_id}/manual")
    def captcha_manual(app_id: int):
        """The user applies in their own browser instead of the automated window.

        Moves the card to a manual apply (`local_handoff.apply_manually`), then opens the posting in
        the system browser. `opened` is false when the machine has no browser to open, and the card then
        shows the URL. 409 when the row is not `blocked/captcha`; 400 when its URL is not on the row's ATS.
        """
        try:
            url = local_handoff.apply_manually(app_id)
        except ValueError as exc:
            return _error(str(exc), 400)
        if url is None:
            return _error("that application is not waiting on a captcha", 409)
        return {"ok": True, "url": url, "opened": webbrowser.open(url)}

    @app.post("/api/posting/{app_id}/open")
    def open_posting(app_id: int):
        """Open the posting of a blocked card in the system browser, which a link inside the app window cannot."""
        try:
            url = local_handoff.blocked_posting_url(app_id)
        except ValueError as exc:
            return _error(str(exc), 400)
        if url is None:
            return _error("that application is not waiting on you", 404)
        return {"ok": True, "url": url, "opened": webbrowser.open(url)}

    @app.post("/api/answer")
    async def answer(request: Request):
        """JSON `{qna_id, application_id, value, scope?}` for typed and picked answers; multipart `qna_id`,
        `application_id`, `scope?` + `file` for an upload. `scope` (`job`, `company`, `global`) is how
        widely the answer is reused; omitted, the row keeps the scope Jev set.

        Both go through `save_blocked_question_answer`, which checks the answer against the control of
        the application the user answered on; a misfit, or an application not waiting on that
        question, is a 400 (a misfit's message names the allowed options or the limit).
        """
        upload = None
        try:
            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                # A file part is spooled to disk by the parser before we see it, so refuse an obviously
                # oversize request first; the exact limit is checked on the bytes in the service.
                length = _declared_length(request)
                if length is None:
                    return _error(_NO_LENGTH, 411)
                if length > local_answers.UPLOAD_MAX_BYTES + _MULTIPART_SLACK:
                    return _error(f"that file is too large; {local_answers.UPLOAD_RULE}", 400)
                form = await request.form()
                try:
                    body = AnswerBody(qna_id=form.get("qna_id"), application_id=form.get("application_id"),
                                      value=form.get("value"), scope=form.get("scope"))
                    part = form.get("file")
                    if part is not None:
                        upload = (part.filename or "", await part.read(local_answers.UPLOAD_MAX_BYTES + 1))
                finally:
                    await form.close()      # drops the spooled temp file
            else:
                body = AnswerBody.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": "bad answer body", "detail": str(exc)}, status_code=422)
        try:
            requeued = local_answers.save_blocked_question_answer(
                body.qna_id, body.application_id, body.value, upload, body.scope)
        except ValueError as exc:
            return _error(str(exc), 400)
        except LookupError as exc:
            return _error(str(exc), 404)
        return {"ok": True, "requeued": requeued}

    @app.post("/api/verify-submit/{app_id}")
    def verify_submit(app_id: int, body: VerifySubmitBody):
        try:
            moved = local_answers.resolve_verify_submit(app_id, body.action)
        except ValueError as exc:
            return _error(str(exc), 400)
        if not moved:
            return _error("that application is not waiting on a submit check", 404)
        return {"ok": True}

    @app.post("/api/retry/{app_id}")
    def retry(app_id: int):
        if not local_answers.retry_exhausted(app_id):
            return _error("that application is not waiting on a retry", 404)
        return {"ok": True}

    @app.get("/api/screenshot/{app_id}")
    def screenshot(app_id: int):
        conn = db.get_db()
        try:
            row = conn.execute("SELECT screenshot_path FROM job_applications "
                               "WHERE id = ? AND status = 'blocked_needs_input'", (app_id,)).fetchone()
        finally:
            conn.close()
        path = local_answers.screenshot_file(row["screenshot_path"]) if row else None
        if path is None:
            return _error("no screenshot", 404)
        return FileResponse(path, media_type="image/png")

    if static_dir is not None:
        root = Path(static_dir).resolve()

        @app.get("/{path:path}")
        def ui(path: str):
            if path == "api" or path.startswith("api/"):
                return _error("not found", 404)
            # This route needs no token, and on Windows `root / "\\\\host\\share\\x"` or `root / "C:/x"`
            # drops `root` for that path: resolving a `\\host` path opens a network share, which sends
            # the user's Windows login hash to that host. So a path that is
            # not plainly relative is never handed to the filesystem. No file in the built UI has a
            # backslash, a colon, a NUL or a `..` segment in its name.
            segments = path.split("/")
            if "\\" in path or ":" in path or "\x00" in path or path.startswith("/") or ".." in segments:
                return FileResponse(root / "index.html")
            wanted = (root / path).resolve()
            if wanted.is_file() and wanted.is_relative_to(root):
                return FileResponse(wanted)
            return FileResponse(root / "index.html")

    return app
