-- 0005_blocking_field_shape: what kind of control each blocked question was asked with.
--
-- application_blocking_questions.field_shape (writer / reader):
--   Written by local_answers.record_block from the form scrape: a JSON object
--   {"kind": "text" | "single" | "multi" | "file", "options": [...], "max_length": int | null}.
--   It lives on the LINK, not the question row, because two forms can ask one saved question with
--   different controls (a dropdown on one board, a text box on another).
--   Read by local_answers.list_blocked (so the tray draws a text box, a picker, checkboxes or an
--   upload button) and by local_answers.save_blocked_question_answer (which refuses a value the
--   control would not take). '{}' means no shape was recorded; readers treat it as a plain text box.
--
-- application_qna_bank.answer_filename (writer / reader):
--   Written by local_answers.save_blocked_question_answer for an uploaded answer: the file name the
--   user uploaded.
--   answer_value holds the stored path (attachments/<sha256>.<ext>), which has no readable name, and
--   the filler attaches the file under the original name. NULL for every non-file answer, which is
--   also how find_answer tells a file answer from a text one.
ALTER TABLE application_blocking_questions ADD COLUMN field_shape TEXT NOT NULL DEFAULT '{}';
ALTER TABLE application_qna_bank ADD COLUMN answer_filename TEXT;
