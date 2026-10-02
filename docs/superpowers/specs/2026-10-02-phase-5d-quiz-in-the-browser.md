# Phase 5d — the gate's actions and the quiz, in the browser

*2026-10-02. Built; this records what and why, so the next change starts from
the rules rather than from the code.*

## What it does

- **Read and Skip** on the default screen, at equal weight, under the button
  that opens the material. Skip is one tap and logged on that tap; an optional
  reason is offered before it. Both call `gate/actions.py`, which the bot's
  buttons call too.
- **The quiz in the browser.** One question at a time; a flag button on every
  question (it leaves the denominator and retires the set); a result in counts
  ("4 of 6", and how many were needed) with each missed question linking to its
  source page in the reader (`/read/<drive_id>?page=N`).
- **One open attempt per item, shared with Telegram.** Whichever surface started
  it, the other resumes it at the next unanswered question.
- **A Quizzes screen:** ready to sit, not ready yet (with why and when), and
  every attempt sat.

## The rules, and what pins each

| rule | pinned by |
|---|---|
| `verified` has one writer; the API reaches it only via `quiz.settle` | `test_settle_is_called_in_exactly_one_place`, `test_no_write_route_reaches_verified_without_a_passed_quiz`, `test_a_passed_quiz_verifies_through_settle` |
| the API never names `verify_study_item` or `advance_study_item` | `FORBIDDEN_NAMES` in `test_api_guards.py` |
| the API process never loads `quizgen` or the provider | `MUST_NOT_LOAD`, plus the control that it does load `agent.gate.quiz` |
| the grading half imports no provider | `test_quiz_split.py` (static and in a clean interpreter) |
| no answer or explanation before an answer is given | `QuizQuestionOut` has no field for either; `test_an_open_attempt_never_carries_the_answers`; the render test greps the page for the explanation |
| Read and Skip are one path for both surfaces | `test_the_bot_read_and_skip_buttons_go_through_actions` |
| untranscribed items stay unquizzable and say why | `readiness` orders `not-readable` before `not-generated`; request refuses with the reason |

## Generation, out of process

The API cannot write a question set. When one is missing:

1. `POST /api/study-items/{id}/quiz/request` inserts a row in `quiz_requests`
   (one open row per item, by a partial unique index).
2. `agent bot` calls `quizgen.serve_requests(limit=1)` between long polls and
   stamps a heartbeat in `bot_state`. After a failure that would repeat (quota,
   key, model) it pauses for half an hour.
3. The `quizzes` stage of `agent run` serves open requests first, then the next
   item of each subject for the next seven days in gate order, then anything read
   and unverified -- at most `quiz.prepare_limit` requests per run.
4. The status the screen shows is measured: a heartbeat under three minutes old
   says "within a few minutes"; otherwise it names the next scheduled run.
   A request that closed without a set keeps the refusal's sentence.

Quota, per day: OCR 12, ahead-of-time sets 4, on demand ~4.

## Not built

- **Snooze** stays in Telegram. It postpones the evening prompt, which the web
  app does not send.
- **Deliver** as a separate action. In the browser, the files are on the screen;
  Read from `pending` passes through `delivered` on the way to `reviewed`, which
  is the same claim the bot's Read makes after it has sent the files.
