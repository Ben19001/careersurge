-- 0008_lone_checkbox_shapes: clear what the Ashby Yes/No card bug already stored.
--
-- Ashby draws a Yes/No question as two buttons over one hidden checkbox. The card offered the
-- question text as its only choice, so picking it saved the question text as the "answer", and the
-- next fill matched no button and blocked the job again. The code now offers the buttons' text
-- (local_formfill.missing_shapes); this removes the answers and shapes the old code left behind.
--
-- application_qna_bank (writer / reader): a row whose answer_value equals its own original_question
--   goes back to unanswered (is_answered = 0, answer_value NULL). The only way to save that value is
--   this bug. One legitimate answer shares that fingerprint: a true acknowledgement box ("I have read
--   the privacy notice") keeps its label as its one choice, and ticking it stores the label. SQL
--   cannot tell the two apart, so those answers are cleared too and the user is asked once more on the
--   next job that needs them. That is the safe direction for a consent: it is never filled without an
--   answer the user gave. Written by local_answers.save_blocked_question_answer,
--   read by local_answers.find_answer, which skips unanswered rows.
-- application_blocking_questions.field_shape (reader only here): a link whose options hold exactly
--   its question's text is the bad shape. It is not rewritten; the job is requeued and
--   local_answers.record_block stores the right shape when the next attempt blocks again.
-- job_applications (writer / reader): a job blocked on a question moves back to 'pending', as
--   local_answers.save_blocked_question_answer does when the last card is answered. The worker reads
--   'pending' and rescrapes the form. The screenshot file stays on disk until the next cleanup; only
--   the column is cleared.

-- Jobs blocked on a question, linked to a bad shape or to a row answered with its own question text:
--   job_applications
--     WHERE blocked on a question
--       AND id IN (links whose field_shape options are exactly [question text],
--                  links to a bank row whose answer_value = original_question)
-- Runs before the bank rows are unanswered below, because it finds them by the bad answer.
UPDATE job_applications
SET status = 'pending', blocked_reason = NULL, screenshot_path = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'blocked_needs_input' AND blocked_reason = 'question'
  AND id IN (
      SELECT b.application_id
      FROM application_blocking_questions b
      JOIN application_qna_bank q ON q.id = b.qna_id
      WHERE (json_array_length(b.field_shape, '$.options') = 1
             AND json_extract(b.field_shape, '$.options[0]') = q.original_question)
         OR q.answer_value = q.original_question);

-- Bank rows answered with their own question text become unanswered again.
UPDATE application_qna_bank
SET answer_value = NULL, is_answered = 0,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE answer_value = original_question;
