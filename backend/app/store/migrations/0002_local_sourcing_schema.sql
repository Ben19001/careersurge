-- 0002_local_sourcing_schema: three changes for the local sourcing path.
--
-- 1. profile.target_roles is the hosted onboarding list. Local search titles live in
--    settings.sourcing_filters, so the column has no writer and no reader. SQLite (3.35+) can
--    drop it because it is not a key, index, CHECK or foreign key target in 0001.
-- 2. sourcing_runs.status loses 'timed_out': a TIMED-OUT or ABORTED Apify run is 'failed'
--    locally. SQLite cannot alter a CHECK, so the table is rebuilt (create, copy, drop, rename).
--    The table is empty when this ships (nothing wrote it yet), which matters because
--    connect() turns foreign keys on and dropping a parent table deletes its rows first, so
--    listings.sourcing_run_id (ON DELETE SET NULL) would be nulled for any run that had listings.
--
-- 3. sourcing_runs gains rows_skipped and skip_reasons (JSON counts per reason, e.g.
--    {"missing_url": 10, "unsupported_ats": 2}): the rebuild above is the cheapest place, and the
--    Settings screen tells the user why some dataset rows could not be saved.
--
-- 0001 has shipped and migrations are forward-only, so this is a new file.

ALTER TABLE profile DROP COLUMN target_roles;

CREATE TABLE sourcing_runs_new (
    apify_run_id TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status       TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    capped       INTEGER NOT NULL DEFAULT 0 CHECK (capped IN (0, 1)),
    cost_usd     REAL,
    rows_fetched INTEGER,
    rows_skipped INTEGER,
    skip_reasons TEXT,
    ingested_at  TEXT
);

INSERT INTO sourcing_runs_new (apify_run_id, started_at, status, capped, cost_usd, rows_fetched, ingested_at)
SELECT apify_run_id, started_at,
       CASE WHEN status = 'timed_out' THEN 'failed' ELSE status END,
       capped, cost_usd, rows_fetched, ingested_at
FROM sourcing_runs;

DROP TABLE sourcing_runs;
ALTER TABLE sourcing_runs_new RENAME TO sourcing_runs;
