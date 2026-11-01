-- 0001_init: the local app's first schema. SQLite, one user per install.
--
-- SQL holds DDL, CHECKs and indexes only. Normalization, classification and scoring live in Python and
-- arrive here as stored results. The claim / compare-and-set UPDATEs are the atomic-guard
-- carve-out and are not defined in this file.
--
-- Column names follow the earlier hosted app's tables. JSONB and ARRAY columns are TEXT holding JSON,
-- booleans are INTEGER 0/1, timestamps are TEXT in UTC ISO-8601. migrate() runs this file in one
-- transaction together with PRAGMA user_version = 1.

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT                       -- JSON text
);

-- users.py minus auth_user_id, stripe_customer_id, subscription_*, apply_credits,
-- forward_outage_notified_at; resume_url renamed resume_path. Exactly one row (id = 1).
CREATE TABLE profile (
    id                            INTEGER PRIMARY KEY CHECK (id = 1),
    email                         TEXT NOT NULL,
    full_name                     TEXT NOT NULL,
    phone                         TEXT,
    summary                       TEXT,
    target_roles                  TEXT,
    us_citizenship_status         TEXT,
    security_clearance_info       TEXT,
    github_url                    TEXT,
    linkedin_url                  TEXT,
    resume_path                   TEXT,
    created_at                    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at                    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    preferred_name                TEXT,
    first_name                    TEXT,
    last_name                     TEXT,
    dob                           TEXT,
    address_line1                 TEXT,
    address_line2                 TEXT,
    city                          TEXT,
    state                         TEXT,
    zip_code                      TEXT,
    country                       TEXT,
    current_location              TEXT,
    pronouns                      TEXT,
    ethnicity                     TEXT,
    lgbtq_status                  TEXT,
    gender                        TEXT,
    authorized_to_work_us         INTEGER CHECK (authorized_to_work_us IN (0, 1)),
    requires_sponsorship          INTEGER CHECK (requires_sponsorship IN (0, 1)),
    veteran_status                INTEGER CHECK (veteran_status IN (0, 1)),
    portfolio_links               TEXT NOT NULL DEFAULT '[]',
    skills                        TEXT NOT NULL DEFAULT '[]',
    education                     TEXT NOT NULL DEFAULT '[]',
    work_experience               TEXT NOT NULL DEFAULT '[]',
    certifications                TEXT NOT NULL DEFAULT '[]',
    languages                     TEXT NOT NULL DEFAULT '[]',
    projects                      TEXT NOT NULL DEFAULT '[]',
    work_preferences              TEXT NOT NULL DEFAULT '{}',
    salary_expectation            TEXT,
    onboarding_completed_at       TEXT,
    auto_consent_authorized       INTEGER NOT NULL DEFAULT 0 CHECK (auto_consent_authorized IN (0, 1)),
    consent_tos_version           TEXT,
    consent_authorized_at         TEXT,
    background_check_authorized   INTEGER NOT NULL DEFAULT 0 CHECK (background_check_authorized IN (0, 1)),
    background_check_tos_version  TEXT,
    background_check_authorized_at TEXT,
    availability_notice           TEXT,
    arbitration_authorized        INTEGER NOT NULL DEFAULT 0 CHECK (arbitration_authorized IN (0, 1)),
    arbitration_tos_version       TEXT,
    arbitration_authorized_at     TEXT,
    referral_source               TEXT,
    screener_answers              TEXT NOT NULL DEFAULT '[]',
    restrictive_covenants         TEXT,
    prior_gov_employment          TEXT,
    post_gov_restrictions         TEXT,
    relocation_willingness        TEXT,
    relocation_radius_miles       INTEGER,
    years_experience              INTEGER
);

-- One Apify run per row, committed before the app waits on it so a restart can reattach.
CREATE TABLE sourcing_runs (
    apify_run_id TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status       TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed', 'timed_out')),
    capped       INTEGER NOT NULL DEFAULT 0 CHECK (capped IN (0, 1)),
    cost_usd     REAL,
    rows_fetched INTEGER,
    ingested_at  TEXT
);

-- job_listings.py minus description and ats (ats is the same as source); plus repost_key and
-- sourcing_run_id.
CREATE TABLE listings (
    id                      INTEGER PRIMARY KEY,
    source                  TEXT NOT NULL CHECK (source IN ('greenhouse', 'lever', 'ashby')),
    external_id             TEXT NOT NULL,
    url                     TEXT NOT NULL,
    title                   TEXT,
    normalized_title        TEXT,
    company                 TEXT,
    location                TEXT,
    is_remote               INTEGER CHECK (is_remote IN (0, 1)),
    employment_type         TEXT,
    experience_level        TEXT,
    posted_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    first_seen_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    is_active               INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    team                    TEXT,
    content_key             TEXT,
    company_key             TEXT NOT NULL,
    locations               TEXT NOT NULL DEFAULT '[]',
    work_arrangement        TEXT,
    remote_regions          TEXT,
    salary_min              REAL,
    salary_max              REAL,
    salary_currency         TEXT,
    experience_years        TEXT,
    key_skills              TEXT,
    visa_sponsorship        INTEGER CHECK (visa_sponsorship IN (0, 1)),
    responsibilities_summary TEXT,
    requirements_summary    TEXT,
    education               TEXT,
    repost_key              TEXT,
    sourcing_run_id         TEXT REFERENCES sourcing_runs (apify_run_id) ON DELETE SET NULL,
    UNIQUE (source, external_id)
);

CREATE INDEX listings_company_title_idx ON listings (company_key, normalized_title);
CREATE INDEX listings_repost_key_idx ON listings (repost_key);

-- The hosted applications table minus user_id, posted_at, next_retry_at, dispatched_at,
-- capture_source; plus ats, blocked_reason, approved_at. One row per listing: retries reuse the row.
-- status vocabulary, with its writers:
--   prepared             local_profile.build_prepared
--   pending              local_batch.approve; local_answers (unblock, retry); local_apply and
--                        local_handoff (a refunded attempt, recovery, a code timeout)
--   in_progress          local_apply.claim; local_handoff.open_in_browser (a captcha the user
--                        opens in a headed window)
--   submitting           local_apply, local_handoff
--   applied              local_apply, local_handoff, local_answers.resolve_verify_submit (confirmed)
--   failed               local_apply, local_handoff, local_answers.expire_blocked, and the
--                        local_queues breaker (a row blocked twice)
--   blocked_needs_input  local_apply, local_handoff
--   cancelled            local_batch.approve (skip), local_answers.resolve_verify_submit (skip)
--   Readers: pending by local_apply.claim; in_progress and submitting by local_apply.recover;
--   blocked_needs_input by local_answers.list_blocked (Needs you); prepared by local_batch (Approve)
--   and the local_sourcing backlog check; applied, with the live statuses, by the local_profile
--   repost check; failed by GET /api/failed. GET /api/progress counts every value, cancelled only there.
-- blocked_reason (local_apply, local_handoff, local_answers): question, otp, captcha, verify_submit,
--   attempts_exhausted.
-- skip_reason (written with failed and cancelled): the failed / cancelled reason.
CREATE TABLE job_applications (
    id                  INTEGER PRIMARY KEY,
    listing_id          INTEGER NOT NULL UNIQUE REFERENCES listings (id) ON DELETE CASCADE,
    ats                 TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN (
                            'prepared', 'pending', 'in_progress', 'submitting', 'applied',
                            'blocked_needs_input', 'failed', 'cancelled')),
    blocked_reason      TEXT CHECK (blocked_reason IN (
                            'question', 'otp', 'captcha', 'verify_submit', 'attempts_exhausted')),
    skip_reason         TEXT CHECK (skip_reason IN (
                            'listing_closed', 'unsupported_form', 'timeout', 'error', 'user_skipped')),
    screenshot_path     TEXT,
    fields_used         TEXT NOT NULL DEFAULT '{}',
    error               TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    approved_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    claimed_at          TEXT,
    claim_expires_at    TEXT,
    claim_token         TEXT,
    confirmed_at        TEXT,
    submit_attempted_at TEXT,
    CHECK ((status = 'blocked_needs_input') = (blocked_reason IS NOT NULL))
);

CREATE INDEX job_applications_status_ats_idx ON job_applications (status, ats);

-- Answers the user (or the classifier) supplied, scoped to everyone (global), one company, or
-- one listing. The typed scope columns make the FK real; the CHECK ties scope to its column.
CREATE TABLE application_qna_bank (
    id                  INTEGER PRIMARY KEY,
    original_question   TEXT NOT NULL,
    normalized_question TEXT NOT NULL,
    question_type       TEXT NOT NULL CHECK (question_type IN (
                            'stable_fact', 'open_ended', 'job_dependent', 'attestation')),
    scope               TEXT NOT NULL CHECK (scope IN ('global', 'company', 'job')),
    company_key         TEXT,
    listing_id          INTEGER REFERENCES listings (id) ON DELETE CASCADE,
    answer_value        TEXT,
    is_answered         INTEGER NOT NULL DEFAULT 0 CHECK (is_answered IN (0, 1)),
    last_used_at        TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (
        (scope = 'global'  AND company_key IS NULL     AND listing_id IS NULL) OR
        (scope = 'company' AND company_key IS NOT NULL AND listing_id IS NULL) OR
        (scope = 'job'     AND company_key IS NULL     AND listing_id IS NOT NULL)
    )
);

-- A plain UNIQUE treats NULLs as distinct, so global rows would duplicate; one partial index per
-- scope instead.
CREATE UNIQUE INDEX application_qna_bank_global_uq
    ON application_qna_bank (normalized_question) WHERE scope = 'global';
CREATE UNIQUE INDEX application_qna_bank_company_uq
    ON application_qna_bank (normalized_question, company_key) WHERE scope = 'company';
CREATE UNIQUE INDEX application_qna_bank_job_uq
    ON application_qna_bank (normalized_question, listing_id) WHERE scope = 'job';

CREATE TABLE application_blocking_questions (
    application_id INTEGER NOT NULL REFERENCES job_applications (id) ON DELETE CASCADE,
    qna_id         INTEGER NOT NULL REFERENCES application_qna_bank (id) ON DELETE CASCADE,
    PRIMARY KEY (application_id, qna_id)
);

CREATE TABLE label_mappings (
    normalized_label TEXT PRIMARY KEY,
    profile_key      TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('alias', 'jev', 'user')),
    confidence       REAL
);
