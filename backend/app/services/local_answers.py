"""The local app's blocking flows: saved answers, the label cache, and unblocking.

Used by `local_formfill` (`find_answer`, `map_labels` while filling), by `local_apply` (`classify_questions`
and `record_block` when a form needs the user, `expire_blocked` at startup and before each queue run),
and by `app.local_api` (`list_blocked`, `save_blocked_question_answer`, `resolve_verify_submit`, `retry_exhausted`),
which the Needs you tray calls. Flask-free, Supabase-free; storage is `app.store.db`.

A job that blocks on the user is `blocked_needs_input/question` with one unanswered
`application_qna_bank` row per missing question, linked through `application_blocking_questions`.
Answering the last one moves the job back to `pending`. Each link records the control the form asked
with (`field_shape`), and an answer must fit it; a file answer is stored under `data_dir()/attachments`. Jev only ever *selects* here: which profile
field a form label means, and what kind of question and how widely reusable an answer is. It never
writes an answer, and it never sees a profile value.
"""
import datetime
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from app.services import jev, local_profile
from app.store import db

logger = logging.getLogger(__name__)

# A Jev pick is acted on only at this confidence. High because a wrong profile field sends a real
# but wrong fact, and a wrong `global` scope reuses an answer on every application. Starting value,
# to be tuned against real forms.
JEV_CONFIDENCE_FLOOR = 0.9
# Content-word overlap required before two question wordings are the same question, once their
# numbers and proper nouns already match exactly. The value the earlier hosted app used.
QUESTION_MATCH_FLOOR = 0.8
# How long a question or exhausted-attempts block waits for the user, as in the earlier
# hosted app; `verify_submit` never expires.
USER_INPUT_TIMEOUT_HOURS = 72
# An uploaded answer: PDF, DOC or DOCX, at most 1 MB. Each type is checked by its
# extension AND its first bytes, so a renamed executable is not stored: extension -> (magic, MIME type).
UPLOAD_MAX_BYTES = 1024 * 1024
UPLOAD_TYPES = {
    ".pdf": (b"%PDF-", "application/pdf"),
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/msword"),
    ".docx": (b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}
UPLOAD_RULE = "only PDF, DOC or DOCX files up to 1 MB are accepted"
# The field shape of a blocked question that recorded none (see migration 0005): a plain text box.
DEFAULT_SHAPE = {"kind": "text", "options": [], "max_length": None}

# Profile columns a form label can mean, each with the plain description Jev reads. These are the
# distinct scalar columns `local_formfill._LABEL_ALIASES` targets (`portfolio_links` is a list and
# is left out); a test keeps the two in step.
_PROFILE_KEYS = {
    "first_name": "the applicant's first name",
    "last_name": "the applicant's last name",
    "preferred_name": "the name the applicant likes to be called",
    "full_name": "the applicant's full legal name",
    "pronouns": "the applicant's pronouns",
    "email": "the applicant's email address",
    "phone": "the applicant's phone number",
    "linkedin_url": "the applicant's LinkedIn profile URL",
    "github_url": "the applicant's GitHub profile URL",
    "relocation_willingness": "whether the applicant is willing to relocate",
    "years_experience": "the applicant's years of professional experience",
    "restrictive_covenants": "whether the applicant is bound by a non-compete or similar agreement",
    "post_gov_restrictions": "whether post-government-employment restrictions apply to the applicant",
    "prior_gov_employment": "whether the applicant has been a government employee",
    "referral_source": "how the applicant heard about the job",
    "authorized_to_work_us": "whether the applicant is legally authorized to work in the United States",
    "requires_sponsorship": "whether the applicant needs visa sponsorship",
    "us_citizenship_status": "the applicant's US citizenship status",
    "security_clearance_info": "the applicant's security clearance",
    "veteran_status": "whether the applicant is a protected veteran",
    "gender": "the applicant's gender",
    "ethnicity": "the applicant's race or ethnicity",
    "lgbtq_status": "whether the applicant identifies as LGBTQ+",
    "salary_expectation": "the applicant's desired salary",
    "availability_notice": "when the applicant can start or their notice period",
    "address_line1": "the first line of the applicant's street address",
    "address_line2": "the second line of the applicant's street address",
    "city": "the applicant's city",
    "state": "the applicant's state",
    "zip_code": "the applicant's postal code",
    "country": "the applicant's country",
    "current_location": "where the applicant is currently located",
}
# Jev picks by option text, so the text carries the description and maps back to the column.
_PROFILE_OPTIONS = {f"{key}: {description}": key for key, description in _PROFILE_KEYS.items()}

# question_type and scope as Jev sees them: option text -> the value stored in the bank.
_TYPE_OPTIONS = {
    "a fact about the applicant that rarely changes, such as work authorization": "stable_fact",
    "a free-text answer written for one employer, such as why this role": "open_ended",
    "an answer that depends on this particular job, such as the salary for this role": "job_dependent",
    "a statement the applicant confirms or certifies, such as that the application is accurate": "attestation",
}
_SCOPE_OPTIONS = {
    "only this job": "job",
    "any job at this company": "company",
    "every application": "global",
}

# Words too common to carry meaning when deciding whether two questions are the same question.
# "legally" is here because "legally authorized to work" and "authorized to work" are one question.
_QUESTION_STOPWORDS = frozenset(
    "a an the you your do does did are is was were have has had of to in for at on our we us they "
    "it that this with and or as be been will would can could please if any other legally".split())
# Digit-words, so "three days per week" and "3 days per week" compare as the same discriminator.
_NUMBER_TOKENS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                  "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
# Every spelling of the country, so "the United States", "U.S." and "USA" are one proper noun.
_US_SPELLINGS = re.compile(r"\bUnited States(?: of America)?\b|\bU\.S\.(?:A\.)?|\bUSA\b")


def _question_signature(text, company: str = "") -> tuple[frozenset, frozenset, frozenset]:
    """(numbers, proper nouns, content words): what decides whether two questions are the same.

    Used by `find_answer`. Ported from the earlier hosted app's `_question_signature`.
    The first two are the safety gate, not a similarity score: "work from our London office 5 days
    a week" and "...our US office three days a week" overlap 0.88 on content words and have opposite
    answers, so the numbers and proper nouns must be IDENTICAL before overlap is consulted.

    Two changes from the hosted copy, both because a wrong reuse sends a wrong fact under the user's
    name: a country spelled "United States", "U.S." or "USA" reads as "US", and a two-letter
    capitalised token ("US", "UK", "EU") counts as a proper noun, which the hosted three-letter floor
    dropped, so "work in the UK" matched "work in the US". The asking company's own name is dropped,
    which collapses "How did you hear about Twilio?" and "...about us?" to one question.
    """
    drop = {word for word in re.split(r"\W+", str(company).lower()) if len(word) > 2}
    text = _US_SPELLINGS.sub("US", str(text))
    tokens = [_NUMBER_TOKENS.get(word.lower(), word)
              for word in re.sub(r"[^A-Za-z0-9 ]", " ", text).split()]
    numbers = {token for token in tokens if token.isdigit()}
    proper = {token.lower() for token in tokens
              if (token[:1].isupper() and len(token) > 2 and token.lower() not in _QUESTION_STOPWORDS
                  and token.lower() not in drop)
              or (len(token) == 2 and token.isupper() and token.isalpha())}
    content = {token.lower() for token in tokens
               if len(token) > 2 and token.lower() not in _QUESTION_STOPWORDS
               and token.lower() not in drop}
    return frozenset(numbers), frozenset(proper), frozenset(content)


def screenshot_file(path: str | None) -> Path | None:
    """The screenshot file for a stored path, only when it lies inside `data_dir()/screenshots`.

    Used by `list_blocked` (`has_screenshot`), the screenshot route, and `delete_screenshot`. A path
    outside the folder, or one that no longer exists, is None, so a bad `screenshot_path` can never
    make the API read or delete another file.
    """
    if not path:
        return None
    folder = (db.data_dir() / "screenshots").resolve()
    candidate = Path(path).resolve()
    if candidate.parent != folder or not candidate.is_file():
        return None
    return candidate


def delete_screenshot(path: str | None) -> None:
    """Delete the screenshot file a stored path names, only when `screenshot_file` accepts it.

    Called by `save_blocked_question_answer`, `_transition_blocked_application` and `expire_blocked`,
    after the transaction that dropped the row's `screenshot_path` commits: the picture only exists
    for the user to look at while blocked.
    """
    stale = screenshot_file(path)
    if stale is not None:
        stale.unlink(missing_ok=True)


def find_answer(question: str, company: str, company_key: str | None, listing_id: int,
                *, file: bool = False) -> str | None:
    """The saved answer for a form question, or None. Called by `fill_form_fields` per unanswered field.

    Only answered rows count, and only rows of the kind asked for: with `file=True` the answer is an
    upload (its `answer_value` is a stored path), otherwise a typed or picked one. The same question
    can be a file on one form and a text box on another, and a stored path must never be typed into a
    text field. The scope is tried narrowest first (this job, then this company, then
    every application) because a narrower answer was given for this exact case. Inside a scope an
    exact normalized match wins, else the best near-match: numbers and proper nouns identical, and
    content-word overlap of at least `QUESTION_MATCH_FLOOR` (see `_question_signature`). A hit
    stamps `last_used_at`.
    """
    normalized = local_profile._normalize_question(question)
    if not normalized:
        return None
    asked_numbers, asked_proper, asked_content = _question_signature(question, company)

    conn = db.get_db()
    try:
        # Plain SQL: every answered row that could apply to this listing - its own job rows, its
        # company's rows, and the global rows.
        rows = conn.execute(
            "SELECT id, scope, original_question, normalized_question, answer_value "
            "FROM application_qna_bank "
            "WHERE is_answered = 1 AND (answer_filename IS NOT NULL) = ? AND ((scope = 'job' AND listing_id = ?) "
            "OR (scope = 'company' AND company_key = ?) OR scope = 'global')",
            (int(file), listing_id, company_key)).fetchall()
        for scope in ("job", "company", "global"):
            best, best_overlap = None, 0.0
            for row in rows:
                if row["scope"] != scope:
                    continue
                if row["normalized_question"] == normalized:
                    best = row
                    break
                numbers, proper, content = _question_signature(row["original_question"], company)
                if numbers != asked_numbers or proper != asked_proper:
                    continue
                overlap = len(content & asked_content) / max(len(content | asked_content), 1)
                if overlap >= QUESTION_MATCH_FLOOR and overlap > best_overlap:
                    best, best_overlap = row, overlap
            if best is not None:
                conn.execute("UPDATE application_qna_bank "
                             "SET last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                             (best["id"],))
                return best["answer_value"]
    finally:
        conn.close()
    return None


def attachment_payload(path: str) -> dict | None:
    """`{name, mimeType, buffer}` for a stored upload, ready for Playwright's `set_input_files`, or None.

    Called by `fill_form_fields` for a file field whose saved answer `find_answer(file=True)` found.
    Only a file inside `data_dir()/attachments` is read, so a bad `answer_value` can never make the
    filler attach another file. The name is the one the user uploaded (`answer_filename`), not the
    stored `<sha256>.<ext>`.
    """
    folder = (db.data_dir() / "attachments").resolve()
    candidate = Path(path).resolve()
    if candidate.parent != folder or not candidate.is_file():
        return None
    conn = db.get_db()
    try:
        row = conn.execute("SELECT answer_filename FROM application_qna_bank "
                           "WHERE answer_value = ? AND answer_filename IS NOT NULL LIMIT 1", (path,)).fetchone()
    finally:
        conn.close()
    return {"name": row["answer_filename"] if row else candidate.name,
            "mimeType": UPLOAD_TYPES.get(candidate.suffix.lower(), (b"", "application/octet-stream"))[1],
            "buffer": candidate.read_bytes()}


def map_labels(labels: list[str]) -> dict[str, str]:
    """`{label: profile column}` for form labels the alias table does not know.

    Called by `fill_form_fields` with the labels of required fields still unanswered. Cached
    `label_mappings` rows answer first; the rest go to Jev in ONE request, whose options are the
    profile columns in `_PROFILE_KEYS` (names and descriptions, never values). A pick at confidence
    `JEV_CONFIDENCE_FLOOR` or more is stored as `source='jev'` and returned; anything lower, "none",
    or Jev being unavailable leaves the label out, so the field stays missing and the job blocks.
    """
    normalized_of = {}
    for label in labels:
        normalized = local_profile._normalize_question(label)
        if normalized:
            normalized_of[label] = normalized
    if not normalized_of:
        return {}

    conn = db.get_db()
    try:
        cached = {}
        for normalized in set(normalized_of.values()):
            row = conn.execute("SELECT profile_key FROM label_mappings WHERE normalized_label = ?",
                               (normalized,)).fetchone()
            if row is not None:
                cached[normalized] = row["profile_key"]

        unknown = sorted(set(normalized_of.values()) - set(cached))
        if unknown:
            asked = {}
            for index, normalized in enumerate(unknown):
                asked[str(index)] = (
                    f'A job application form field is labelled "{normalized}". Which applicant '
                    "profile field does it ask for?", list(_PROFILE_OPTIONS))
            try:
                answers = jev.choose({"task": "match form field labels to applicant profile fields"}, asked)
            except jev.JevUnavailable as exc:
                logger.info("label mapping skipped, jev unavailable: %s", exc.reason)
                answers = {}
            for index, normalized in enumerate(unknown):
                if str(index) not in answers:
                    continue
                picked, confidence = answers[str(index)]
                if picked is None or confidence < JEV_CONFIDENCE_FLOOR:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO label_mappings (normalized_label, profile_key, source, confidence) "
                    "VALUES (?, ?, 'jev', ?)", (normalized, _PROFILE_OPTIONS[picked], confidence))
                cached[normalized] = _PROFILE_OPTIONS[picked]
    finally:
        conn.close()

    mapped = {}
    for label, normalized in normalized_of.items():
        if normalized in cached:
            mapped[label] = cached[normalized]
    return mapped


def classify_questions(questions: list[str], company: str) -> dict[str, tuple[str, str]]:
    """`{question: (question_type, scope)}` for the questions a form left unanswered.

    Called by `local_apply` BEFORE the block transaction, because this is a network call and must not
    hold the write lock. One `jev.choose` request, two questions per form question: the type among the
    four in `_TYPE_OPTIONS`, and the scope among `_SCOPE_OPTIONS`. The user only types the answer, so
    a wrong wider scope would reuse it on forms it does not fit: a scope wider than `job` is kept only
    at `JEV_CONFIDENCE_FLOOR`. Below it, or with Jev unavailable, the row is `job` scope, and
    `open_ended` when the type is unknown. An `attestation` is always `job`, whatever scope Jev picked:
    a certification is made to one employer, and the profile's consent flags are the only recorded
    way to agree for every form.
    """
    if not questions:
        return {}
    asked = {}
    for index, question in enumerate(questions):
        asked[f"type{index}"] = (f'Which kind of question is this job application question? "{question}"',
                                 list(_TYPE_OPTIONS))
        asked[f"scope{index}"] = (
            f'An applicant types one answer to this question: "{question}". Should that answer be reused '
            "for only this job, for any job at this company, or for every application?",
            list(_SCOPE_OPTIONS))
    try:
        answers = jev.choose({"company": company or ""}, asked)
    except jev.JevUnavailable as exc:
        logger.info("question classification skipped, jev unavailable: %s", exc.reason)
        answers = None

    classified = {}
    for index, question in enumerate(questions):
        question_type, scope = "open_ended", "job"
        if answers is not None:
            picked_type, _ = answers[f"type{index}"]
            if picked_type is not None:
                question_type = _TYPE_OPTIONS[picked_type]
            picked_scope, confidence = answers[f"scope{index}"]
            if picked_scope is not None and confidence >= JEV_CONFIDENCE_FLOOR:
                scope = _SCOPE_OPTIONS[picked_scope]
        if question_type == "attestation":
            scope = "job"
        classified[question] = (question_type, scope)
    return classified


def record_block(conn, app_row: dict, listing: dict, classified: dict[str, tuple[str, str]],
                 shapes: dict[str, dict] | None = None, *, whole_form: bool = False) -> None:
    """Link `app_row` to one unanswered bank row per classified question, reusing existing rows.

    Called by `local_apply` on the connection of the block transaction, so the rows and the move to
    `blocked_needs_input` commit together. A row for the same normalized question at the same scope
    is reused, not duplicated (the partial unique indexes in 0001_init.sql). An answered row is NEVER
    reopened: if the row at Jev's scope is answered, that answer stays for every other listing and
    this listing gets (or reuses) its own unanswered `job` row, which `find_answer` prefers over the
    wider scopes, so only this listing uses the new answer. The one exception is a `job` row that is
    itself answered: nothing else uses it, and it cannot be duplicated, so it is reopened. A `company`
    scope on a listing with no `company_key` falls back to `job`. `shapes` is `{question: field
    shape}` from the form scrape; each link stores its own (migration 0005).

    `whole_form` says `classified` is every question this attempt left unmet (local_apply's block),
    so the application's links from earlier attempts that are not among them are dropped. Without
    that, a question the app has since filled kept its old card, with the shape it was first asked
    with, and its unanswered row held the job blocked even after every real question was answered.
    """
    linked = []
    for question, (question_type, scope) in classified.items():
        normalized = local_profile._normalize_question(question)
        if not normalized:
            continue
        if scope == "company" and not listing.get("company_key"):
            scope = "job"

        # Jev's scope first, then this listing's own row when Jev's is already answered.
        candidates = [(scope, listing["company_key"] if scope == "company" else None,
                       listing["id"] if scope == "job" else None)]
        if scope != "job":
            candidates.append(("job", None, listing["id"]))
        for row_scope, row_company_key, row_listing_id in candidates:
            # IS compares NULLs as equal, so one query covers all three scopes.
            existing = conn.execute(
                "SELECT id, is_answered FROM application_qna_bank "
                "WHERE normalized_question = ? AND scope = ? AND company_key IS ? AND listing_id IS ?",
                (normalized, row_scope, row_company_key, row_listing_id)).fetchone()
            if existing is None or not existing["is_answered"]:
                break

        if existing is None:
            qna_id = conn.execute(
                "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, "
                "scope, company_key, listing_id, is_answered) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (question, normalized, question_type, row_scope, row_company_key, row_listing_id)).lastrowid
        else:
            qna_id = existing["id"]
            if existing["is_answered"]:
                conn.execute("UPDATE application_qna_bank SET is_answered = 0, "
                             "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (qna_id,))
        # A repeat block of the same job refreshes the shape: the form may have changed.
        shape = json.dumps((shapes or {}).get(question) or {})
        conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id, field_shape) "
                     "VALUES (?, ?, ?) ON CONFLICT (application_id, qna_id) DO UPDATE SET "
                     "field_shape = excluded.field_shape", (app_row["id"], qna_id, shape))
        linked.append(qna_id)

    # Kept only when something was linked, so a block never leaves its job with no question to answer.
    if whole_form and linked:
        # Plain SQL: DELETE this application's links WHERE qna_id NOT IN (the ids linked above).
        # Only the "?, ?, ?" placeholders, one per id, go into the f-string; the ids are bound values.
        placeholders = ", ".join(["?"] * len(linked))
        conn.execute(f"DELETE FROM application_blocking_questions WHERE application_id = ? "
                     f"AND qna_id NOT IN ({placeholders})", (app_row["id"], *linked))


def save_blocked_question_answer(qna_id: int, application_id: int, value: str | list | None = None,
                                 upload: tuple[str, bytes] | None = None, scope: str | None = None) -> list[int]:
    """Store the answer the user gave on one application's card and requeue every job it releases.

    Called by `POST /api/answer`. `application_id` is the tray card the user answered on, `value` the
    typed or picked answer (a list for a checkbox question), `upload` `(file name, bytes)` for a file
    question. The answer must fit the control THAT application's form asked with: the `field_shape`
    on its link to `qna_id`. `single` takes one of the options, `multi` a list of them (stored as a
    JSON list), `text` at most `max_length` characters, `file` a PDF, DOC or DOCX of at most 1 MB,
    stored 0600 as `data_dir()/attachments/<sha256>.<ext>` with that path in `answer_value`. The
    type stays as Jev set it; so does the scope unless the card sends `scope`.

    `scope` (`job`, `company` or `global`) is how widely the user wants the answer reused. When it
    differs from the row's, the row is re-scoped first, in the same transaction, to the key
    `(normalized_question, scope, company_key, listing_id)` of the card's listing, and the rest of
    this function runs on the row that results. Widening: the row is updated in place when that key
    is free; when an unanswered row holds the key, this row's links move onto it and this row is
    deleted, so one question has one row; when an answered row holds it, ValueError, because
    overwriting it would change what every other job is sent. Narrowing (global to company or job,
    company to job): the row is updated in place when this card is its only link and nothing holds the
    key, else only this card's link moves to the row at the key (created if none; a `job` row that is
    answered is reopened, an answered `company` row is refused like a widening) and the shared row
    keeps its other jobs. ValueError also for an unknown scope, `company` on a listing with no
    `company_key`, and a wider scope for an `attestation` (a certification is made to one employer).
    A scope change never fans the answer out to jobs waiting on other rows.

    A wide-scope row can be shared by jobs whose forms ask it differently (Yes/No on one, "Yes, I
    have"/"No, I have not" on another; a file input on one and a text box on another), so the other
    jobs waiting on it are judged by their own link's shape. In one transaction, each waiting job
    whose shape accepts the answer keeps its link, and each whose shape rejects it has its link moved
    to its own unanswered `job` row for its listing (found or created by `record_block`, shape kept),
    so it stays blocked and its card shows its own options. Then each application still linked to
    this question that is `blocked_needs_input/question` with no unanswered linked question left goes
    to `pending` (compare-and-set on status and reason, `blocked_reason` cleared, screenshot
    dropped). Returns the requeued application ids. ValueError for an empty or misfit answer, its
    message naming the allowed options or the limit, or for an application that is not waiting on
    this question; LookupError for an unknown `qna_id`.
    """
    if upload is None:
        if isinstance(value, str):
            value = value.strip()
        if not value:
            raise ValueError("an answer cannot be empty")
    else:
        # The upload is judged here, before the lock: a name outside the supported types, a body that
        # does not start like that type, or one over the limit is refused with the rule in the message.
        filename = Path(str(upload[0]).replace("\\", "/")).name
        filename = "".join(ch for ch in filename if ch.isprintable()).strip()[:255]
        extension = Path(filename).suffix.lower()
        data = upload[1]
        if extension not in UPLOAD_TYPES or not data.startswith(UPLOAD_TYPES[extension][0]):
            raise ValueError(f"{filename or 'that file'} is not a supported file; {UPLOAD_RULE}")
        if len(data) > UPLOAD_MAX_BYTES:
            raise ValueError(f"{filename} is too large; {UPLOAD_RULE}")

    scopes = ("job", "company", "global")      # narrowest first: a scope's index says how wide it is
    if scope is not None and scope not in scopes:
        raise ValueError(f"scope must be one of: {', '.join(scopes)}")

    requeued, screenshots = [], []
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        question = conn.execute("SELECT * FROM application_qna_bank WHERE id = ?", (qna_id,)).fetchone()
        if question is None:
            raise LookupError(f"no question {qna_id}")

        if scope is not None and scope != question["scope"]:
            card = conn.execute(
                "SELECT a.listing_id, l.company_key, b.field_shape FROM application_blocking_questions b "
                "JOIN job_applications a ON a.id = b.application_id JOIN listings l ON l.id = a.listing_id "
                "WHERE b.qna_id = ? AND b.application_id = ? AND a.status = 'blocked_needs_input' "
                "AND a.blocked_reason = 'question'", (qna_id, application_id)).fetchone()
            if card is None:
                raise ValueError(f"application {application_id} is not waiting on question {qna_id}")
            widening = scopes.index(scope) > scopes.index(question["scope"])
            if widening and question["question_type"] == "attestation":
                raise ValueError("a certification is made to one employer, so it can only be saved for this job")
            if scope == "company" and not card["company_key"]:
                raise ValueError("this job has no company name to reuse the answer for; "
                                 "choose this job only or every job")

            # The key the re-scoped row would have, and the row already holding it, if any.
            target_company = card["company_key"] if scope == "company" else None
            target_listing = card["listing_id"] if scope == "job" else None
            target = conn.execute(
                "SELECT id, is_answered, answer_value, answer_filename FROM application_qna_bank "
                "WHERE normalized_question = ? AND scope = ? AND company_key IS ? AND listing_id IS ?",
                (question["normalized_question"], scope, target_company, target_listing)).fetchone()
            other_links = conn.execute(
                "SELECT COUNT(*) FROM application_blocking_questions WHERE qna_id = ? AND application_id != ?",
                (qna_id, application_id)).fetchone()[0]

            # An answered row at a wider key is shared by other jobs, so it is never overwritten. An
            # answered `job` row is this listing's alone, so narrowing to it reopens it (as record_block does).
            if target is not None and target["is_answered"] and scope != "job":
                saved = target["answer_filename"] or target["answer_value"]
                raise ValueError(f'"{saved}" is already saved for this question at that scope; changing it would '
                                 f"change what every other job is sent. Choose \"This job only\" instead")
            if target is None and (widening or other_links == 0):
                # Nothing holds the key and no other job is left behind, so the row itself is updated in place.
                conn.execute("UPDATE application_qna_bank SET scope = ?, company_key = ?, listing_id = ?, "
                             "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                             (scope, target_company, target_listing, qna_id))
            elif widening:
                # Widening onto an unanswered row: it takes over this row's links (a job may
                # already link to both) and this row goes.
                conn.execute("INSERT OR IGNORE INTO application_blocking_questions (application_id, qna_id, field_shape) "
                             "SELECT application_id, ?, field_shape FROM application_blocking_questions "
                             "WHERE qna_id = ?", (target["id"], qna_id))
                conn.execute("DELETE FROM application_qna_bank WHERE id = ?", (qna_id,))
                qna_id = target["id"]
            else:
                # Narrowing, and the row is shared or the key is held: other jobs did not choose the narrower
                # scope, so only this card's link moves, to the row at the key (created when none holds it).
                if target is None:
                    target_id = conn.execute(
                        "INSERT INTO application_qna_bank (original_question, normalized_question, question_type, "
                        "scope, company_key, listing_id, is_answered) VALUES (?, ?, ?, ?, ?, ?, 0)",
                        (question["original_question"], question["normalized_question"], question["question_type"],
                         scope, target_company, target_listing)).lastrowid
                else:
                    target_id = target["id"]
                    conn.execute("UPDATE application_qna_bank SET is_answered = 0, "
                                 "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (target_id,))
                conn.execute("INSERT INTO application_blocking_questions (application_id, qna_id, field_shape) "
                             "VALUES (?, ?, ?) ON CONFLICT (application_id, qna_id) DO UPDATE SET "
                             "field_shape = excluded.field_shape", (application_id, target_id, card["field_shape"]))
                conn.execute("DELETE FROM application_blocking_questions WHERE application_id = ? AND qna_id = ?",
                             (application_id, qna_id))
                if other_links == 0:
                    # This card was its only link: the old row would be an unanswered orphan, and a
                    # global one would still be listed by onboarding's Questions step.
                    conn.execute("DELETE FROM application_qna_bank WHERE id = ?", (qna_id,))
                qna_id = target_id
            question = conn.execute("SELECT * FROM application_qna_bank WHERE id = ?", (qna_id,)).fetchone()

        # Plain SQL: every application waiting on this question, with the control its form asked it
        # with. The waiting ones are `blocked_needs_input/question`; a link of any other application
        # is stale and ignored.
        waiting = conn.execute(
            "SELECT b.application_id, b.field_shape, a.listing_id FROM application_blocking_questions b "
            "JOIN job_applications a ON a.id = b.application_id "
            "WHERE b.qna_id = ? AND a.status = 'blocked_needs_input' AND a.blocked_reason = 'question'",
            (qna_id,)).fetchall()

        answer, answer_filename = None, None
        answered_on_card, rejected = False, []
        for link in waiting:
            shape = {**DEFAULT_SHAPE, **json.loads(link["field_shape"])}
            kind, options, limit = shape["kind"], shape["options"], shape["max_length"]
            on_card = link["application_id"] == application_id
            answered_on_card = answered_on_card or on_card
            try:
                fitted = None
                if kind == "file" and upload is None:
                    raise ValueError(f"upload a file: {UPLOAD_RULE}")
                if kind != "file" and upload is not None:
                    raise ValueError("this question takes typed text or a choice, not a file")
                if kind == "multi":
                    if not isinstance(value, list):
                        raise ValueError(f"choose one or more of: {', '.join(options)}")
                    picked = list(dict.fromkeys(str(item).strip() for item in value))
                    not_offered = [item for item in picked if item not in options]
                    if not picked or not_offered:
                        raise ValueError(f"not offered: {', '.join(not_offered) or 'nothing picked'}; "
                                         f"choose from: {', '.join(options)}")
                    fitted = json.dumps(picked)
                elif kind != "file":
                    if isinstance(value, list):
                        raise ValueError("this question takes one answer, not a list")
                    if kind == "single" and value not in options:
                        raise ValueError(f"choose one of: {', '.join(options)}")
                    if kind == "text" and limit and len(value) > limit:
                        raise ValueError(f"answers to this question can be at most {limit} characters; "
                                         f"yours is {len(value)}")
                    fitted = value
            except ValueError:
                # The card the user answered on must take the answer; any other job just stays blocked.
                if on_card:
                    raise
                rejected.append(link)
                continue
            if on_card:
                answer = fitted
        if not answered_on_card:
            raise ValueError(f"application {application_id} is not waiting on question {qna_id}")

        if upload is not None:
            digest = hashlib.sha256(data).hexdigest()
            folder = db.data_dir() / "attachments"
            folder.mkdir(exist_ok=True)
            if os.name == "posix":
                os.chmod(folder, 0o700)
            stored = folder / f"{digest}{extension}"
            # Content-addressed, so the same bytes are the same file: write once, atomically, 0600 from
            # the first byte, so the filler never reads a half-written upload.
            if not stored.exists():
                partial = folder / f"{digest}.partial"
                descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                os.replace(partial, stored)
            answer, answer_filename = str(stored), filename

        conn.execute(
            "UPDATE application_qna_bank SET answer_value = ?, answer_filename = ?, is_answered = 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (answer, answer_filename, qna_id))

        # A job whose form cannot take this answer leaves the shared row for its own `job` row, the
        # same rule as a re-block: reused when it exists (reopened if answered), else inserted. The
        # new link keeps this job's shape; the old link is dropped, so the shared answer never
        # reaches it.
        for link in rejected:
            record_block(conn, {"id": link["application_id"]}, {"id": link["listing_id"]},
                         {question["original_question"]: (question["question_type"], "job")},
                         {question["original_question"]: json.loads(link["field_shape"])})
            conn.execute("DELETE FROM application_blocking_questions WHERE application_id = ? AND qna_id = ?",
                         (link["application_id"], qna_id))

        # Plain SQL: the applications linked to this question that wait on a question answer, and
        # that have no linked question still unanswered (the NOT EXISTS subquery). A job moved off
        # above is no longer linked here, so it is not in this list.
        releasable = conn.execute(
            "SELECT a.id, a.screenshot_path FROM job_applications a "
            "JOIN application_blocking_questions b ON b.application_id = a.id "
            "WHERE b.qna_id = ? AND a.status = 'blocked_needs_input' AND a.blocked_reason = 'question' "
            "AND NOT EXISTS (SELECT 1 FROM application_blocking_questions o "
            "                JOIN application_qna_bank q ON q.id = o.qna_id "
            "                WHERE o.application_id = a.id AND q.is_answered = 0)",
            (qna_id,)).fetchall()
        for row in releasable:
            moved = conn.execute(
                "UPDATE job_applications SET status = 'pending', blocked_reason = NULL, "
                "screenshot_path = NULL, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND status = 'blocked_needs_input' AND blocked_reason = 'question'",
                (row["id"],)).rowcount
            if moved:
                requeued.append(row["id"])
                screenshots.append(row["screenshot_path"])
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    for path in screenshots:
        delete_screenshot(path)
    return requeued


def list_blocked() -> list[dict]:
    """Every `blocked_needs_input` application for the tray, oldest first.

    Called by `GET /api/blocked`. Each dict: `id`, `title`, `company`, `blocked_reason`, `questions`
    (the unanswered ones: `qna_id`, `question`, `question_type`, `scope`), `error`, `has_screenshot`,
    `updated_at`. The card's own `scope` preselects the "Reuse this answer for" choice.
    Each question also carries `field_shape`, `{kind, options, max_length}`, the control THIS form
    asked it with: `text` (a box, limited to `max_length` when set), `single` (pick one of `options`),
    `multi` (pick any of `options`) or `file` (an upload button). A link with no recorded shape is
    `text`.

    Every card also names its job: `url`, `location`, `ats`, `has_company` (the listing has a
    `company_key`, so "any job at <company>" can be offered), and the actor's `responsibilities`,
    `requirements` (text, possibly None) and `key_skills` (a list, `[]` when empty).

    Every card also carries `email`, the address on the profile: the one an `otp` card says the code
    was sent to and a `verify_submit` card with `error='email_link'` says the link was sent to.

    Every card carries `answers`, `[{label, value, is_file}]`. A `captcha` card and a `verify_submit`
    card with `error='manual_apply'` list what the app would have sent (`fields_used.filled`), so the user
    can paste it into the form in their own browser; any other card has `[]`. A value that is a
    path to an existing file is sent as its file name with `is_file` true: the path stays on this machine.
    """
    conn = db.get_db()
    try:
        profile = conn.execute("SELECT email FROM profile WHERE id = 1").fetchone()
        applications = conn.execute(
            "SELECT a.id, a.blocked_reason, a.error, a.screenshot_path, a.updated_at, a.ats, a.fields_used, l.title, l.company, "
            "l.url, l.location, l.company_key, l.responsibilities_summary, l.requirements_summary, l.key_skills "
            "FROM job_applications a JOIN listings l ON l.id = a.listing_id "
            "WHERE a.status = 'blocked_needs_input' ORDER BY a.updated_at, a.id").fetchall()
        # Plain SQL: the unanswered questions linked to any blocked application.
        open_questions = conn.execute(
            "SELECT b.application_id, b.field_shape, q.id, q.original_question, q.question_type, q.scope "
            "FROM application_blocking_questions b JOIN application_qna_bank q ON q.id = b.qna_id "
            "WHERE q.is_answered = 0 ORDER BY q.id").fetchall()
    finally:
        conn.close()

    questions_of = {}
    for row in open_questions:
        questions_of.setdefault(row["application_id"], []).append(
            {"qna_id": row["id"], "question": row["original_question"],
             "question_type": row["question_type"], "scope": row["scope"],
             "field_shape": {**DEFAULT_SHAPE, **json.loads(row["field_shape"])}})

    # Where the app itself keeps uploads (`save_blocked_question_answer`) and resumes (`local_profile`); see the
    # file check below.
    own_folders = (db.data_dir() / "attachments", db.data_dir() / "resumes")
    blocked = []
    for row in applications:
        key_skills = json.loads(row["key_skills"] or "[]")
        answers = []
        is_manual_card = row["blocked_reason"] == "verify_submit" and row["error"] == "manual_apply"
        if row["blocked_reason"] == "captcha" or is_manual_card:
            fields_used = json.loads(row["fields_used"] or "{}")
            filled = fields_used.get("filled", {})
            for label, value in filled.items():
                # Only a file directly inside the app's own attachments or resumes folder is looked up, decided
                # on the text alone. Some values are option text copied from the employer's form, and asking
                # the disk about a path the employer wrote reaches out: on Windows `\\host\share\x` (or
                # `\??\UNC\host\x`) connects to that host with the user's login hash, and on macOS `/net/host/x`
                # makes the automounter contact it, on every poll of this list.
                # A relative answer such as "LICENSE" never matches either, so it cannot hit the working directory.
                is_file = (isinstance(value, str) and Path(value).parent in own_folders
                           and os.path.isfile(value))
                answers.append({"label": label, "value": Path(value).name if is_file else value, "is_file": is_file})
        blocked.append({
            "id": row["id"], "title": row["title"], "company": row["company"],
            "url": row["url"], "location": row["location"], "ats": row["ats"],
            "has_company": bool(row["company_key"]), "responsibilities": row["responsibilities_summary"],
            "requirements": row["requirements_summary"], "key_skills": key_skills,
            "blocked_reason": row["blocked_reason"], "questions": questions_of.get(row["id"], []),
            "error": row["error"], "has_screenshot": screenshot_file(row["screenshot_path"]) is not None,
            "updated_at": row["updated_at"], "email": profile["email"] if profile else None, "answers": answers})
    return blocked


def _transition_blocked_application(app_id: int, from_reason: str, to_status: str, *, stamp_confirmed=False,
                                    skip_reason=None, attempt_adjust=0) -> bool:
    """Move one blocked application out of `from_reason` to `to_status`; False when it is not in that state.

    Shared by `resolve_verify_submit` and `retry_exhausted`. A compare-and-set on status and reason,
    `blocked_reason` cleared in the same statement (the table's CHECK), the block's `error` cleared
    with it, and the row's screenshot file deleted once the move commits: the picture only exists for
    the user to look at while blocked. `attempt_adjust` is added to `attempt_count` (never below 0).
    """
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT screenshot_path FROM job_applications "
                           "WHERE id = ? AND status = 'blocked_needs_input' AND blocked_reason = ?",
                           (app_id, from_reason)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return False
        # The move itself. error and screenshot belong to the block, so they go with it.
        conn.execute(
            "UPDATE job_applications SET status = ?, blocked_reason = NULL, error = NULL, "
            "screenshot_path = NULL, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (to_status, app_id))
        # Then the extras this particular exit asks for, one plain statement each.
        if skip_reason is not None:
            conn.execute("UPDATE job_applications SET skip_reason = ? WHERE id = ?", (skip_reason, app_id))
        if stamp_confirmed:
            conn.execute("UPDATE job_applications SET confirmed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                         "WHERE id = ?", (app_id,))
        if attempt_adjust:
            # MAX(..., 0) keeps the count from going negative.
            conn.execute("UPDATE job_applications SET attempt_count = MAX(attempt_count + ?, 0) WHERE id = ?",
                         (attempt_adjust, app_id))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    delete_screenshot(row["screenshot_path"])
    return True


def resolve_verify_submit(app_id: int, action: str) -> bool:
    """Apply the user's decision on a `verify_submit` card; False when the row is not waiting on one.

    Called by `POST /api/verify-submit/{id}`. `confirmed` -> `applied` (the user saw the
    confirmation, `confirmed_at` set), `retry` -> `pending` (the user checked that nothing arrived,
    so a second submit is their call), `skip` -> `cancelled/user_skipped`. `not_applied` is
    for a card made by `local_handoff.apply_manually` (`error='manual_apply'`): the user did not apply
    in their own browser after all, so it goes back to `blocked/captcha` with the screenshot kept. It
    has its own statement because `_transition_blocked_application` always leaves `blocked_needs_input`.
    ValueError for any other action, and for `not_applied` on a card that is not a manual apply.
    """
    if action == "confirmed":
        return _transition_blocked_application(app_id, "verify_submit", "applied", stamp_confirmed=True)
    if action == "retry":
        return _transition_blocked_application(app_id, "verify_submit", "pending")
    if action == "skip":
        return _transition_blocked_application(app_id, "verify_submit", "cancelled",
                                                skip_reason="user_skipped")
    if action == "not_applied":
        conn = db.get_db()
        try:
            moved = conn.execute(
                "UPDATE job_applications SET blocked_reason = 'captcha', error = NULL, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND status = 'blocked_needs_input' AND blocked_reason = 'verify_submit' "
                "AND error = 'manual_apply'", (app_id,)).rowcount
            if moved:
                return True
            # Decides 400 (a verify_submit card that is not a manual apply) vs 404 (not waiting on a submit check).
            other_verify_card = conn.execute("SELECT 1 FROM job_applications WHERE id = ? "
                                             "AND status = 'blocked_needs_input' AND blocked_reason = 'verify_submit'",
                                             (app_id,)).fetchone()
        finally:
            conn.close()
        if other_verify_card:
            raise ValueError("only an application opened in your own browser can be marked as not applied")
        return False
    raise ValueError(f"unknown action {action!r}")


def retry_exhausted(app_id: int) -> bool:
    """`blocked_needs_input/attempts_exhausted` -> `pending` with one more attempt; False if not in that state.

    Called by `POST /api/retry/{id}`. The claim adds one to `attempt_count`, so taking one off here
    leaves exactly one attempt before the row is exhausted again.
    """
    return _transition_blocked_application(app_id, "attempts_exhausted", "pending", attempt_adjust=-1)


def expire_blocked() -> int:
    """Fail blocked rows the user left unanswered past `USER_INPUT_TIMEOUT_HOURS`; returns how many.

    Called by `local_apply.recover` and `local_queues.run_all`. `verify_submit` never expires: only the user
    knows whether the application went out. Expired rows become `failed/timeout` with
    `error='user_input_timeout'` and lose their screenshot file.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=USER_INPUT_TIMEOUT_HOURS)
    cutoff_text = cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            "SELECT id, screenshot_path FROM job_applications "
            "WHERE status = 'blocked_needs_input' AND blocked_reason != 'verify_submit' AND updated_at < ?",
            (cutoff_text,)).fetchall()
        for row in stale:
            conn.execute(
                "UPDATE job_applications SET status = 'failed', blocked_reason = NULL, skip_reason = 'timeout', "
                "error = 'user_input_timeout', screenshot_path = NULL, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (row["id"],))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    for row in stale:
        delete_screenshot(row["screenshot_path"])
    return len(stale)
