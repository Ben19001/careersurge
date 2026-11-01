-- 0004_label_mappings_source: drop 'alias' from label_mappings.source.
--
-- source vocabulary (writer / reader):
--   jev  - written by local_answers.map_labels when Jev picks a profile field at confidence >= 0.9;
--          read by map_labels on every later form.
--   user - written by a later correction action (not built yet); read by map_labels.
--   alias - dropped. _LABEL_ALIASES is checked in code before this table, so an 'alias' row could
--          never be read, and nothing wrote one.
--
-- SQLite cannot alter a CHECK in place, so the table is rebuilt. It is empty: label_mappings had
-- no writer until `local_answers.map_labels`.
DROP TABLE label_mappings;

CREATE TABLE label_mappings (
    normalized_label TEXT PRIMARY KEY,
    profile_key      TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('jev', 'user')),
    confidence       REAL
);
