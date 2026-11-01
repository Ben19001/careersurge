-- 0006_approval_batches: one row per Approve click, holding the inputs the user approved.
--
-- snapshot: JSON text, the profile row, resume file (path, name, sha256), answered global
--   questions and consent flags as they were when the user clicked Approve. Stored once per
--   batch, shared by every job in it; the worker fills forms from it, not from the live profile,
--   so a profile edit after approval is never sent unapproved. job_applications.fields_used is
--   not reused for it because the worker overwrites that column with per-field fill results.
-- job_applications.approval_batch_id: nullable because a `prepared` row has no batch until it is
--   approved, and because SQLite's ADD COLUMN cannot add a NOT NULL foreign key. Written by
--   local_batch.approve, read by local_apply._attempt.
--
-- Pure DDL: the snapshot is built in Python.

CREATE TABLE approval_batches (
    id          INTEGER PRIMARY KEY,
    approved_at TEXT NOT NULL,
    snapshot    TEXT NOT NULL
);

ALTER TABLE job_applications ADD COLUMN approval_batch_id INTEGER REFERENCES approval_batches (id);
