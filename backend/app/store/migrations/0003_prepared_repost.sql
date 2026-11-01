-- 0003_prepared_repost: the evidence build_prepared() stores on a prepared application.
--
-- repost_match: 'strong' = an earlier listing has the same repost_key (same apply URL), 'weak' =
--   same company and same listing fingerprint within 12 months, NULL = no earlier match.
--   Written by build_prepared(); read by the approval screen (strong starts unchecked, weak
--   is a badge).
-- repost_of_listing_id: the earlier listing the match points at, NULL when repost_match is NULL.
--   ON DELETE SET NULL because a purged listing must not take the application with it.
--
-- The fingerprint and both comparisons are Python; this file only stores the results. Both
-- ADD COLUMNs default to NULL, which SQLite requires for a column with a foreign key.

ALTER TABLE job_applications ADD COLUMN repost_match TEXT CHECK (repost_match IN ('strong', 'weak'));
ALTER TABLE job_applications ADD COLUMN repost_of_listing_id INTEGER REFERENCES listings (id) ON DELETE SET NULL;
