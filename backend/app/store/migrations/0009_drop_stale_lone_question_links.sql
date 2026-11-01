-- 0009_drop_stale_lone_question_links: clear the Yes/No cards 0008 meant to clear but could not.
--
-- 0008 requeued every job linked to a card whose only option was its own question text, expecting
-- the next block to store the right shape. Where the next attempt FILLED that question (Ashby's
-- Yes/No buttons now scrape as options), it was not among the questions missed, so its old
-- link was never rewritten: the card kept showing one choice equal to the question, and its
-- unanswered row held the job blocked after the user answered every real question.
-- local_answers.record_block now drops such links on each block
-- (`whole_form`); this removes the ones already stored.
--
-- application_blocking_questions (writer / reader): a link whose field_shape options are exactly its
--   question's text, to a bank row still unanswered, is deleted. If the form still asks it, the next
--   block links it again with the shape that attempt scraped. Written by local_answers.record_block,
--   read by local_answers.list_blocked and save_blocked_question_answer. A true acknowledgement box
--   has the same fingerprint; deleting its unanswered link only means the next attempt asks again.
-- application_qna_bank: not changed. The unanswered rows stay; find_answer skips unanswered rows.
-- job_applications (writer / reader): a job blocked on a question with no unanswered linked question
--   left goes back to 'pending', as local_answers.save_blocked_question_answer does when the last card
--   is answered. The worker reads 'pending' and fills the form again. The screenshot file stays on
--   disk until the next cleanup; only the column is cleared.

-- Links whose only option is their own question text, to an unanswered row:
--   application_blocking_questions
--     WHERE field_shape.options = [the linked row's original_question]
--       AND the linked row is unanswered
DELETE FROM application_blocking_questions
WHERE rowid IN (
    SELECT b.rowid
    FROM application_blocking_questions b
    JOIN application_qna_bank q ON q.id = b.qna_id
    WHERE json_array_length(b.field_shape, '$.options') = 1
      AND json_extract(b.field_shape, '$.options[0]') = q.original_question
      AND q.is_answered = 0);

-- Jobs blocked on a question that no longer link to any unanswered question:
--   job_applications
--     WHERE blocked on a question
--       AND NOT EXISTS (a link to an unanswered row)
UPDATE job_applications
SET status = 'pending', blocked_reason = NULL, screenshot_path = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'blocked_needs_input' AND blocked_reason = 'question'
  AND NOT EXISTS (
      SELECT 1
      FROM application_blocking_questions b
      JOIN application_qna_bank q ON q.id = b.qna_id
      WHERE b.application_id = job_applications.id AND q.is_answered = 0);
