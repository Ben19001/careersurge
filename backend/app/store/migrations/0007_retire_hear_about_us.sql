-- 0007_retire_hear_about_us: "How did you hear about us?" stops being a global answer.
--
-- One global answer ("LinkedIn") was typed into every company's form, which is untrue for most of
-- them. The seed no longer creates the row (local_profile.GLOBAL_SEED); this removes the one already
-- there. A form that asks the question again lands in Needs you as a job-scope card through
-- local_answers.classify_questions.
--
-- application_qna_bank (writer / reader): the global row 'how did you hear about us' is deleted;
--   its application_blocking_questions links go with it (ON DELETE CASCADE, 0001_init.sql).
-- job_applications (writer / reader): a job blocked on a question whose ONLY unanswered linked
--   question is that row goes back to 'pending', the same move local_answers.save_blocked_question_answer
--   makes when the last card is answered. The worker reads 'pending' and fills the form again.
--   The screenshot file of the block stays on disk until the next cleanup; only the column is cleared.

-- Jobs that are blocked on a question and linked to the row, with no other unanswered linked question:
--   job_applications
--     WHERE blocked on a question
--       AND id IN (links to the global 'how did you hear about us' row)
--       AND NOT EXISTS (a link to any other unanswered row)
UPDATE job_applications
SET status = 'pending', blocked_reason = NULL, screenshot_path = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'blocked_needs_input' AND blocked_reason = 'question'
  AND id IN (
      SELECT b.application_id
      FROM application_blocking_questions b
      JOIN application_qna_bank q ON q.id = b.qna_id
      WHERE q.scope = 'global' AND q.normalized_question = 'how did you hear about us')
  AND NOT EXISTS (
      SELECT 1
      FROM application_blocking_questions other_link
      JOIN application_qna_bank other_row ON other_row.id = other_link.qna_id
      WHERE other_link.application_id = job_applications.id
        AND other_row.is_answered = 0
        AND NOT (other_row.scope = 'global' AND other_row.normalized_question = 'how did you hear about us'));

DELETE FROM application_qna_bank
WHERE scope = 'global' AND normalized_question = 'how did you hear about us';
