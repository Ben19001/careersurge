"""The local app's batch approval: what the approval screen shows, and the frozen snapshot.

Used by `GET /api/batch` and `POST /api/batch/approve` (`app.local_api`) and by `local_apply._attempt`,
which fills every form from `snapshot()` instead of the live profile. Approval freezes the INPUTS the
filler may use - the profile row (its consent flags are the auto-agree policy), the resume file and
the answered global questions - not per-form answers, because a form's fields are only known when its
page loads. The Q&A bank stays live: every bank answer was typed by the user, and a tray answer given
after approval must unblock a job of the batch. Flask-free, Supabase-free; storage is `app.store.db`.
"""
import datetime
import hashlib
import json
from pathlib import Path

from app.services import local_profile
from app.store import db

# Seconds one application is assumed to take, before its cooldown. Unmeasured; to be tuned on real runs.
SECONDS_PER_APPLICATION = 60
# Statuses whose application is still on its way to an outcome, so the queue still owes it work.
_UNFINISHED = ("pending", "in_progress", "submitting")


def _inputs(conn) -> dict | None:
    """The frozen inputs as they are right now, or None when there is no profile yet.

    Used by `prepared_batch` (to show them) and `approve` (to store them), so the screen and the
    snapshot cannot disagree. `profile` is the raw row (JSON text columns stay text: the worker
    decodes them with `local_formfill.profile_for_fill`), `resume` is the file's name and sha256,
    `global_answers` are the answered global bank rows, `consent` the three auto-agree flags, each 1 only
    when set under the current wording (`local_profile.consent_is_current`), as the filler reads them.
    """
    profile_row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if profile_row is None:
        return None
    profile = dict(profile_row)
    resume_path = Path(profile["resume_path"]) if profile["resume_path"] else None
    resume = None
    if resume_path is not None and resume_path.is_file():
        resume = {"path": str(resume_path), "name": resume_path.name,
                  "sha256": hashlib.sha256(resume_path.read_bytes()).hexdigest()}
    answers = conn.execute(
        "SELECT original_question, answer_value FROM application_qna_bank "
        "WHERE scope = 'global' AND is_answered = 1 ORDER BY id").fetchall()
    global_answers = []
    for answer in answers:
        global_answers.append({"question": answer["original_question"], "answer": answer["answer_value"]})
    consent = {}
    for flag in local_profile._CONSENT_FLAGS:
        consent[flag] = 1 if local_profile.consent_is_current(profile, flag) else 0
    return {"profile": profile, "resume": resume, "global_answers": global_answers, "consent": consent}


def prepared_batch() -> dict:
    """`{jobs, inputs, eta}` for the approval screen.

    `jobs` are the `prepared` applications, oldest first, each with `id`, `ats`, `title`, `company`,
    `location`, `url`, `pay` (`{min, max, currency}` or None) and `repost_match`: None, or `{kind,
    title, company, url, first_seen_at}` naming the earlier listing the match points at. `inputs` is
    `_inputs` (None before onboarding). `eta` is `{seconds_per_application, queued}`: one application
    plus the mean cooldown, and how many applications each ATS already owes. The ATS queues run in
    parallel, so the screen finishes in `max over ats of (queued + checked) * seconds_per_application`.
    """
    conn = db.get_db()
    try:
        # Plain SQL: each prepared application with its listing, and the earlier listing its repost
        # match points at (LEFT JOIN, so a row with no match keeps a NULL earlier listing).
        #   job_applications JOIN listings ON listings.id = listing_id
        #                    LEFT JOIN listings AS earlier ON earlier.id = repost_of_listing_id
        rows = conn.execute(
            "SELECT a.id, a.ats, a.repost_match, l.title, l.company, l.location, l.url, "
            "       l.salary_min, l.salary_max, l.salary_currency, "
            "       e.title AS earlier_title, e.company AS earlier_company, e.url AS earlier_url, "
            "       e.first_seen_at AS earlier_first_seen_at "
            "FROM job_applications a JOIN listings l ON l.id = a.listing_id "
            "LEFT JOIN listings e ON e.id = a.repost_of_listing_id "
            "WHERE a.status = 'prepared' ORDER BY a.id").fetchall()
        inputs = _inputs(conn)
        unfinished = conn.execute(
            "SELECT ats, COUNT(*) AS n FROM job_applications "
            "WHERE status IN (?, ?, ?) GROUP BY ats", _UNFINISHED).fetchall()
    finally:
        conn.close()

    jobs = []
    for row in rows:
        pay = None
        if row["salary_min"] is not None or row["salary_max"] is not None:
            pay = {"min": row["salary_min"], "max": row["salary_max"], "currency": row["salary_currency"]}
        repost_match = None
        if row["repost_match"]:
            repost_match = {"kind": row["repost_match"], "title": row["earlier_title"],
                            "company": row["earlier_company"], "url": row["earlier_url"],
                            "first_seen_at": row["earlier_first_seen_at"]}
        jobs.append({"id": row["id"], "ats": row["ats"], "title": row["title"], "company": row["company"],
                     "location": row["location"], "url": row["url"], "pay": pay, "repost_match": repost_match})

    # Imported here, not at the top: local_queues imports local_apply, and local_apply imports this
    # module for `snapshot`.
    from app.services import local_queues

    floor = db.get_setting("apply_cooldown_floor_s")
    if not isinstance(floor, (int, float)) or floor <= 0:
        floor = local_queues.DEFAULT_COOLDOWN_FLOOR_S
    floor = max(local_queues.MIN_COOLDOWN_FLOOR_S, floor)
    mean_cooldown = floor + local_queues.COOLDOWN_SPREAD_S / 2
    queued = {ats: 0 for ats in local_queues.SUPPORTED_ATS}
    for row in unfinished:
        queued[row["ats"]] = row["n"]
    eta = {"seconds_per_application": SECONDS_PER_APPLICATION + mean_cooldown, "queued": queued}
    return {"jobs": jobs, "inputs": inputs, "eta": eta}


def approve(approve_ids: list[int], skip_ids: list[int]) -> dict:
    """Approve and skip `prepared` applications in one transaction; returns `{batch_id, approved, skipped}`.

    Called by `POST /api/batch/approve` when the user clicks Approve. The approved rows become
    `pending` with `approved_at` and one new `approval_batches` row whose snapshot is `_inputs` read
    inside the same transaction; the skipped rows become `cancelled/user_skipped`. Each update is a
    compare-and-set on `status = 'prepared'`, so an id that is not `prepared` any more (approved in
    another tab, already running) is not counted and not touched. `batch_id` is None when nothing was
    approved (no batch row is kept). ValueError before anything is written: an id in both lists, no
    profile, or no resume file.
    """
    for app_id in approve_ids:
        if app_id in skip_ids:
            raise ValueError(f"application {app_id} cannot be both approved and skipped")
    # UTC timestamp in the schema's own format (`2026-10-13T09:30:00.123Z`).
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_id = None
        approved = 0
        if approve_ids:
            inputs = _inputs(conn)
            if inputs is None:
                raise ValueError("no profile yet; finish onboarding before approving")
            if inputs["resume"] is None:
                raise ValueError("the profile's resume file is missing; upload it again")
            batch_id = conn.execute("INSERT INTO approval_batches (approved_at, snapshot) VALUES (?, ?)",
                                    (now, json.dumps(inputs))).lastrowid
            for app_id in approve_ids:
                approved += conn.execute(
                    "UPDATE job_applications SET status = 'pending', approved_at = ?, approval_batch_id = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'prepared'",
                    (now, batch_id, now, app_id)).rowcount
            if approved == 0:
                conn.execute("DELETE FROM approval_batches WHERE id = ?", (batch_id,))
                batch_id = None
        skipped = 0
        for app_id in skip_ids:
            skipped += conn.execute(
                "UPDATE job_applications SET status = 'cancelled', skip_reason = 'user_skipped', updated_at = ? "
                "WHERE id = ? AND status = 'prepared'", (now, app_id)).rowcount
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"batch_id": batch_id, "approved": approved, "skipped": skipped}


def snapshot(batch_id: int | None) -> dict:
    """The inputs frozen by one Approve click, as stored by `approve` (see `_inputs`).

    Called by `local_apply._attempt` before every fill. Raises LookupError for a missing batch id or
    an unknown one: a row that reached `pending` without approval must not be filled from the live
    profile.
    """
    if batch_id is None:
        raise LookupError("application has no approval batch")
    conn = db.get_db()
    try:
        row = conn.execute("SELECT snapshot FROM approval_batches WHERE id = ?", (batch_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError(f"no approval batch {batch_id}")
    return json.loads(row["snapshot"])
