// The quiz, in the browser. Phase 5d.
//
// What the server will and will not tell this screen is the design:
//
//   While the quiz is open, a question arrives with its options and nothing
//   else -- no correct index, no explanation. They do not exist in the response
//   model, so no bug here can show them early. Grading happens on the server
//   when the last question is answered or flagged.
//
//   The result is counts: "4 of 6", and how many were needed. Never a
//   percentage. What I missed is listed with the right answer and a link to the
//   page it came from, because a wrong answer is worth something only if I can
//   go and look.
//
//   A quiz with no questions written yet is not an error. The server cannot
//   write them -- it never calls a model -- so this screen asks for them and
//   says plainly when they will exist.
//
// One open attempt per item, shared with Telegram. Opening this screen on an
// item already half answered in the chat resumes at the next question.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, describe } from "/api.js";
import { linkProps, navigate } from "/router.js";
import { Icon } from "/icons.js";
import { localTime, plural } from "/format.js";
import { Card, Problem, ScreenHeader, Skeleton, SubjectName, useSubjectNames } from "/ui.js";

const LETTERS = ["A", "B", "C", "D"];

/**
 * What a quiz's status means, said once, for every screen that shows one.
 *
 * Returns { sentence, action } where action is "start", "ask" or null.
 */
export function quizState(status) {
  if (!status) return { sentence: "", action: null };
  switch (status.kind) {
    case "open":
      return { sentence: "A quiz is open on this. It picks up where it stopped.", action: "start" };
    case "ready":
      return { sentence: "The questions are written.", action: "start" };
    case "requested":
      return { sentence: `Asked for. ${status.when}`, action: null };
    case "not-generated":
      return {
        sentence: status.request_outcome
          ? `The last request did not produce a quiz: ${status.request_outcome}.`
          : "The questions have not been written yet.",
        action: "ask",
      };
    case "not-readable":
      return { sentence: `No quiz yet: ${status.reason}.`, action: null };
    case "not-delivered":
      return { sentence: status.reason, action: null };
    default:
      return { sentence: status.reason || "", action: null };
  }
}

/**
 * The quiz's door, wherever an item is shown: Today, a subject, the Quizzes list.
 *
 * Fetches its own status, because the answer changes under it -- a set the bot
 * writes while this is on screen should turn "asked for" into "take the quiz"
 * the next time it is looked at.
 */
export function QuizEntry({ itemId, compact = false }) {
  const [status, setStatus] = useState(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .get(`/api/study-items/${itemId}/quiz`)
      .then((found) => live && setStatus(found))
      .catch((err) => live && setProblem(describe(err)));
    return () => {
      live = false;
    };
  }, [itemId]);

  async function ask() {
    setBusy(true);
    setProblem("");
    try {
      setStatus(await api.post(`/api/study-items/${itemId}/quiz/request`));
    } catch (err) {
      setProblem(describe(err));
    }
    setBusy(false);
  }

  if (problem) return html`<p class="t-meta">${problem}</p>`;
  if (!status) return null;
  const { sentence, action } = quizState(status);

  return html`<div class=${`quiz-entry ${compact ? "compact" : ""}`}>
    ${action === "start"
      ? html`<a class="button primary-button" ...${linkProps(`/quiz/${itemId}`)}>
          <${Icon} name="quiz" />
          ${status.kind === "open" ? "Resume the quiz" : "Take the quiz"}
        </a>`
      : null}
    ${action === "ask"
      ? html`<button class="button secondary-button" type="button" disabled=${busy} onClick=${ask}>
          <${Icon} name="quiz" />
          ${busy ? "Asking…" : "Ask for the quiz"}
        </button>`
      : null}
    ${sentence && !(action === "start" && compact)
      ? html`<p class="t-meta quiz-sentence">${sentence}${action === "ask" && status.when ? ` ${status.when}` : ""}</p>`
      : null}
  </div>`;
}

// ---------------------------------------------------------------- one question

function Question({ attempt, question, onAnswer, onFlag, busy }) {
  return html`<article class="hero quiz-card" aria-labelledby="quiz-q">
    <p class="t-meta num">
      ${`Question ${question.index + 1} of ${attempt.total}`}
    </p>
    <h1 class="t-lead quiz-question" id="quiz-q">${question.question}</h1>
    <ol class="quiz-options">
      ${question.options.map(
        (option, n) => html`<li key=${n}>
          <button
            class="quiz-option"
            type="button"
            disabled=${busy}
            onClick=${() => onAnswer(question.index, n)}
          >
            <span class="quiz-letter" aria-hidden="true">${LETTERS[n]}</span>
            <span>${option}</span>
          </button>
        </li>`
      )}
    </ol>
    <div class="quiz-flag">
      <button class="button quiet-button" type="button" disabled=${busy} onClick=${() => onFlag(question.index)}>
        <${Icon} name="flag" />
        This question is wrong
      </button>
      <p class="t-meta">A flagged question does not count, and the set is rewritten next time.</p>
    </div>
  </article>`;
}

// ---------------------------------------------------------------- the result

function Review({ entry }) {
  const chosen = entry.chosen == null ? null : entry.options[entry.chosen];
  const right = entry.options[entry.correct];
  const page = entry.source_page;
  return html`<li>
    <div class="card review-card">
      <div class="card-body">
        <p class="t-body review-q">${entry.question}</p>
        ${entry.flagged
          ? html`<p class="t-meta">Flagged as wrong, so it did not count.</p>`
          : html`
              <p class="t-state">${chosen ? `You chose: ${chosen}` : "Not answered."}</p>
              <p class="review-right">${`The answer: ${right}`}</p>
            `}
        ${entry.explanation ? html`<p class="t-meta">${entry.explanation}</p>` : null}
        ${entry.drive_id
          ? html`<a
              class="button quiet-button review-link"
              ...${linkProps(
                `/read/${encodeURIComponent(entry.drive_id)}${page ? `?page=${page}` : ""}`
              )}
            >
              <${Icon} name="document" />
              ${page ? `Open page ${page}` : "Open the file"}
            </a>`
          : entry.source_file
          ? html`<p class="t-meta">${`From ${entry.source_file}${page ? `, page ${page}` : ""}.`}</p>`
          : null}
      </div>
    </div>
  </li>`;
}

function Result({ attempt, onRetry }) {
  const names = useSubjectNames();
  const subject = names[attempt.course_id] || attempt.course_name;
  const result = attempt.result;
  const missed = result.review.filter((entry) => !entry.right && !entry.flagged);
  const flagged = result.review.filter((entry) => entry.flagged);
  const headline = result.passed
    ? result.verified
      ? `${result.correct} of ${result.counted}. Verified.`
      : `${result.correct} of ${result.counted}. Passed.`
    : `${result.correct} of ${result.counted}.`;
  const sub = result.passed
    ? "That counts as verified for this lecture."
    : `${result.needed} ${result.needed === 1 ? "was" : "were"} needed. It stays read, not verified — nothing is lost.`;

  return html`<div class="screen quiz-result">
    <article class="hero">
      <p class="t-state"><${SubjectName} name=${subject} />${` · ${attempt.label}`}</p>
      <h1 class="t-hero num">${headline}</h1>
      <p class="t-state">${sub}</p>
      ${result.flagged
        ? html`<p class="t-meta">${`${plural(result.flagged, "question")} flagged and left out of the count.`}</p>`
        : null}
      <div class="hero-actions">
        ${!result.passed
          ? html`<button class="button primary-button" type="button" onClick=${onRetry}>Try again</button>`
          : null}
        <a class="button secondary-button" ...${linkProps("/quizzes")}>All quizzes</a>
      </div>
      ${!result.passed && result.failures >= 3
        ? html`<p class="t-meta">
            Three tries on one lecture. If the questions seem wrong rather than hard,
            flag them next time — a flagged set is rewritten.
          </p>`
        : null}
    </article>

    ${missed.length
      ? html`<section class="section">
          <h2 class="section-title">What you missed</h2>
          <ul class="list">${missed.map((entry) => html`<${Review} key=${entry.index} entry=${entry} />`)}</ul>
        </section>`
      : null}
    ${flagged.length
      ? html`<section class="section">
          <h2 class="section-title">Flagged</h2>
          <ul class="list">${flagged.map((entry) => html`<${Review} key=${entry.index} entry=${entry} />`)}</ul>
        </section>`
      : null}
  </div>`;
}

// ---------------------------------------------------------------- the screen

/** /quiz/:itemId -- start or resume, then one question at a time. */
export function Quiz({ itemId }) {
  const names = useSubjectNames();
  const [attempt, setAttempt] = useState(null);
  const [status, setStatus] = useState(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  async function open() {
    setProblem("");
    setAttempt(null);
    try {
      const found = await api.get(`/api/study-items/${itemId}/quiz`);
      setStatus(found);
      if (found.kind === "open" || found.kind === "ready") {
        setAttempt(await api.post(`/api/study-items/${itemId}/quiz`));
      }
    } catch (err) {
      setProblem(describe(err));
    }
  }

  useEffect(() => {
    open();
  }, [itemId]);

  async function send(path, body) {
    setBusy(true);
    try {
      setAttempt(await api.post(`/api/quiz-attempts/${attempt.attempt_id}/${path}`, body));
    } catch (err) {
      setProblem(describe(err));
    }
    setBusy(false);
  }

  if (problem) {
    return html`<div class="screen">
      <${Problem}>${problem}</${Problem}>
      <p><a class="button quiet-button" ...${linkProps("/quizzes")}>All quizzes</a></p>
    </div>`;
  }
  if (status && !attempt && !(status.kind === "open" || status.kind === "ready")) {
    return html`<div class="screen">
      <${ScreenHeader} title="Quiz" />
      <${Card}>
        <${QuizEntry} itemId=${itemId} />
      </${Card}>
    </div>`;
  }
  if (!attempt) return html`<div class="screen"><${Skeleton} hero=${true} rows=${0} /></div>`;

  if (attempt.finished) return html`<${Result} attempt=${attempt} onRetry=${open} />`;

  const question = attempt.questions.find((q) => q.chosen == null && !q.flagged);
  if (!question) return html`<div class="screen"><${Skeleton} hero=${true} rows=${0} /></div>`;

  return html`<div class="screen quiz">
    <p class="t-state quiz-head">
      <${SubjectName} name=${names[attempt.course_id] || attempt.course_name} />${` · ${attempt.label}`}
    </p>
    <${Question}
      attempt=${attempt}
      question=${question}
      busy=${busy}
      onAnswer=${(index, choice) => send("answers", { index, choice })}
      onFlag=${(index) => send("flags", { index })}
    />
  </div>`;
}

/** /quiz/attempt/:id -- a finished attempt, read back. */
export function PastAttempt({ attemptId }) {
  const [attempt, setAttempt] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get(`/api/quiz-attempts/${attemptId}`)
      .then((found) => live && setAttempt(found))
      .catch((err) => live && setProblem(describe(err)));
    return () => {
      live = false;
    };
  }, [attemptId]);

  if (problem) return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  if (!attempt) return html`<div class="screen"><${Skeleton} hero=${true} rows=${2} /></div>`;
  if (!attempt.finished) {
    navigate(`/quiz/${attempt.item_id}`, { replace: true });
    return null;
  }
  return html`<${Result} attempt=${attempt} onRetry=${() => navigate(`/quiz/${attempt.item_id}`)} />`;
}

// ---------------------------------------------------------------- the list

/** /quizzes -- what can be sat now, what is waiting, and what was sat. */
export function Quizzes() {
  const names = useSubjectNames();
  const subjectOf = (entry) => names[entry.course_id] || entry.course_name;
  const [body, setBody] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get("/api/quizzes")
      .then((found) => live && setBody(found))
      .catch((err) => live && setProblem(describe(err)));
    return () => {
      live = false;
    };
  }, []);

  if (problem) return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  if (!body) return html`<div class="screen"><${Skeleton} rows=${4} /></div>`;

  return html`<div class="screen">
    <${ScreenHeader} title="Quizzes">
      Lectures read and not yet verified, and every quiz already sat.
    </${ScreenHeader}>

    <section class="section">
      <h2 class="section-title">Ready to sit</h2>
      ${body.ready.length
        ? html`<ul class="grid">
            ${body.ready.map(
              (entry) => html`<li key=${entry.item_id}>
                <${Card} to=${`/quiz/${entry.item_id}`} subject=${subjectOf(entry)}>
                  <span class="t-meta"><${SubjectName} name=${subjectOf(entry)} /></span>
                  <span class="t-lead">${entry.label}</span>
                  <span class="t-meta">${entry.status.kind === "open" ? "Open — picks up where it stopped." : "Questions written."}</span>
                </${Card}>
              </li>`
            )}
          </ul>`
        : html`<p class="t-state">Nothing is ready to sit right now.</p>`}
    </section>

    ${body.waiting.length
      ? html`<section class="section">
          <h2 class="section-title">Not ready yet</h2>
          <ul class="grid">
            ${body.waiting.map(
              (entry) => html`<li key=${entry.item_id}>
                <${Card} subject=${subjectOf(entry)}>
                  <span class="t-meta"><${SubjectName} name=${subjectOf(entry)} /></span>
                  <span class="t-lead">${entry.label}</span>
                  <${QuizEntry} itemId=${entry.item_id} compact=${true} />
                </${Card}>
              </li>`
            )}
          </ul>
        </section>`
      : null}

    <section class="section">
      <h2 class="section-title">Sat</h2>
      ${body.attempts.length
        ? html`<ul class="list">
            ${body.attempts.map(
              (entry) => html`<li key=${entry.attempt_id}>
                <${Card} to=${`/quiz/attempt/${entry.attempt_id}`} subject=${subjectOf(entry)}>
                  <span class="t-lead">${entry.label}</span>
                  <span class="t-state num">
                    ${`${entry.correct} of ${entry.counted}${entry.passed ? " — verified" : ""}`}
                  </span>
                  <span class="t-meta">${`${subjectOf(entry)} · ${localTime(entry.finished_at)}`}</span>
                </${Card}>
              </li>`
            )}
          </ul>`
        : html`<p class="t-state">No quiz has been sat yet.</p>`}
    </section>
  </div>`;
}
