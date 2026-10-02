// Subjects and coverage. DESIGN.md section 3.
//
// "Absolute counts of finished work, yes. Percentages of a deficit, never.
// Streaks, never."
//
// The server sends no percentage, and this screen does not compute one. That is
// not belt-and-braces: the enforcement only works if BOTH halves hold, and
// `unreviewed / (unreviewed + verified)` is one line away at any time. There is
// no such line here and there must not be.
//
// Four states are what section 3 names; six is what the data actually
// distinguishes, and the two extra exist because collapsing them would say
// something false. A subject awaiting a Classroom id is not one whose course
// this install holds nothing for. A manual subject with nothing entered is not
// "no readable material" -- nothing is missing and nothing is broken.
//
// The list is in the order of each subject's next session, because the one I
// have a lecture in tomorrow is the one I came to look at. Subjects with nothing
// in them yet are gathered at the bottom, each still saying exactly what it is.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";
import { age, plural, relativeDay } from "/format.js";
import { subjectStyle as subjectStyleOf } from "/subject-color.js";
import { linkProps } from "/router.js";
import { Card, Chip, Empty, Problem, ScreenHeader, SectionLinks, Skeleton, STUDY_PARTS, SubjectName } from "/ui.js";
import { QuizEntry } from "/screens/quiz.js";
import { Icon } from "/icons.js";
import { navigate } from "/router.js";

// What each state says, and whether there is anything to do about it. The
// wording is the client's half of `messages._subject_line`; the classification
// is the server's, from `Subject.state`, so the two cannot drift.
const SAYS = {
  awaiting_course: "no Classroom course yet, never gated",
  no_material_here: "mapped to a course with no material here",
  nothing_entered: "nothing entered yet",
  no_readable_material: "no readable material",
  up_to_date: "up to date",
  behind: null, // counted, not described
};

// Subjects with nothing in them to act on, gathered into one group at the
// bottom. Not hidden -- every one keeps its exact state line inside the group --
// but a subject whose course does not exist yet should not stand between me and
// one I have a lecture in tomorrow. `no_readable_material` stays out of this
// group on purpose: dead attachments are a fact worth seeing at full size.
const INACTIVE = new Set(["nothing_entered", "awaiting_course"]);

/** The state line for a subject, the same words on the list and its page. */
function standing(subject) {
  const said = SAYS[subject.state];
  if (said) {
    return (
      said +
      (subject.dead_files
        ? ` — ${plural(subject.dead_files, "attachment")} gone from Drive`
        : "")
    );
  }
  // "6 unreviewed, 2 ready" is section 3's own example. With none ready the
  // zero is not said: "none ready yet" is the same fact in words.
  if (!subject.blocked) return plural(subject.unreviewed, "unreviewed", "unreviewed");
  return subject.ready
    ? `${subject.unreviewed} unreviewed, ${subject.ready} ready`
    : `${subject.unreviewed} unreviewed, none ready yet`;
}

/** "Mon 5 Oct 08:30" -- when a session is, as the instant it is. */
function when(found) {
  if (!found) return "";
  return `${relativeDay(found.date)} ${found.start}`;
}

/**
 * Each subject's next session in the timetable's window, by name.
 *
 * The timetable names subjects the same way the subject list does, because both
 * come from timetable.yaml. A subject with no session in the window has no
 * entry and sorts after every one that does.
 */
function nextSessions(timetable) {
  const next = {};
  for (const day of (timetable && timetable.days) || []) {
    for (const session of day.sessions || []) {
      for (const part of session.parts || []) {
        if (!next[part.subject]) {
          next[part.subject] = { date: day.date, start: session.start, session };
        }
      }
    }
  }
  return next;
}

/** Rows grouped by the date of their next session, in order; "~" is no session. */
function byDay(rows, next) {
  const groups = [];
  for (const row of rows) {
    const day = next[row.name] ? next[row.name].date : "~";
    const last = groups[groups.length - 1];
    if (last && last[0] === day) last[1].push(row);
    else groups.push([day, [row]]);
  }
  return groups;
}

function dayHeading(day) {
  if (day === "~") return "No session in the next two weeks";
  const said = relativeDay(day);
  return said.charAt(0).toUpperCase() + said.slice(1);
}

function order(rows, next) {
  const key = (row) => {
    const found = next[row.name];
    return found ? `${found.date} ${found.start}` : "~";
  };
  // Stable: subjects with no session keep the server's order among themselves.
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const ka = key(a.row);
      const kb = key(b.row);
      return ka < kb ? -1 : ka > kb ? 1 : a.index - b.index;
    })
    .map(({ row }) => row);
}

function Standing({ subject, next }) {
  const item = subject.next_item;
  const meta = [
    subject.state === "behind" && subject.oldest_posted_at
      ? `oldest from ${age(subject.oldest_posted_at)}`
      : "",
  ].filter(Boolean);

  return html`<li class="subject">
    <${Card}
      to=${`/study/${encodeURIComponent(subject.name)}`}
      subject=${subject.name}
    >
      <span class="subject-top">
        <${Chip} name=${subject.name} />
        ${next
          ? html`<span class="subject-when"><span class="time">${next.start}</span> ${kindOf(next.session)}</span>`
          : null}
      </span>
      <span class="t-state subject-said">${standing(subject)}</span>
      ${subject.state === "behind" && item
        ? html`<span class="t-lead subject-next">${item.label}</span>`
        : null}
      ${meta.length ? html`<span class="t-meta">${meta.join(", ")}</span>` : null}
    </${Card}>
  </li>`;
}

function Inactive({ rows }) {
  if (!rows.length) return null;
  return html`<details class="section inactive">
    <summary class="inactive-summary">
      <span class="t-body">Nothing here yet</span>
      <span class="t-meta">${plural(rows.length, "subject")}</span>
    </summary>
    <ul class="inactive-list">
      ${rows.map(
        (row) => html`<li key=${row.name}>
          <a class="inactive-row" ...${linkProps(`/study/${encodeURIComponent(row.name)}`)}>
            <${SubjectName} name=${row.name} />
            <span class="t-meta">${standing(row)}</span>
          </a>
        </li>`
      )}
    </ul>
  </details>`;
}

export function Subjects() {
  const [rows, setRows] = useState(null);
  const [next, setNext] = useState({});
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get("/api/subjects")
      .then((found) => live && setRows(found))
      .catch((err) => {
        if (!live) return;
        setProblem(
          err instanceof Offline
            ? "No connection, and nothing has been kept for this screen."
            : describe(err)
        );
      });
    // The order, not the content: without the timetable the list still renders,
    // in the server's order.
    api
      .get("/api/timetable")
      .then((found) => live && setNext(nextSessions(found)))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  if (problem) {
    return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  }
  if (!rows) return html`<div class="screen"><${Skeleton} rows=${5} /></div>`;

  const behind = rows.filter((row) => row.state === "behind");
  const active = order(rows.filter((row) => !INACTIVE.has(row.state)), next);
  const inactive = rows.filter((row) => INACTIVE.has(row.state));

  return html`
    <div class="screen">
      <${ScreenHeader} title="Study">
        ${behind.length
          ? `${plural(behind.length, "subject")} with something unreviewed, ` +
            `${plural(
              behind.reduce((sum, row) => sum + row.unreviewed, 0),
              "item"
            )} in total.`
          : "Nothing unreviewed in any gated subject."}
      </${ScreenHeader}>
      <${SectionLinks} links=${STUDY_PARTS} here="/study" />
      <div class="study-days">
        ${byDay(active, next).map(
          ([day, rows]) => html`<section class="day-group" key=${day} aria-label=${dayHeading(day)}>
            <h2 class="day-heading">${dayHeading(day)}</h2>
            <ul class="grid subjects">
              ${rows.map(
                (row) => html`<${Standing} key=${row.name} subject=${row} next=${next[row.name]} />`
              )}
            </ul>
          </section>`
        )}
      </div>
      <${Inactive} rows=${inactive} />
    </div>
  `;
}

/** This subject's sessions in the timetable's window. */
function sessionsOf(name, timetable) {
  const found = [];
  for (const day of (timetable && timetable.days) || []) {
    for (const session of day.sessions || []) {
      if ((session.parts || []).some((part) => part.subject === name)) {
        found.push({ date: day.date, session });
      }
    }
  }
  return found;
}

function kindOf(session) {
  const kind = (session.kind || "").toLowerCase();
  return { lec: "lecture", tut: "tutorial", lab: "lab" }[kind] || kind;
}

/** What a study item's state means, in words. */
const ITEM_STATE = {
  pending: "Not opened yet",
  delivered: "Opened, not marked read",
  reviewed: "Read, not verified",
  verified: "Verified",
  skipped: "Skipped",
};

function capital(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

function dateOf(day) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function weekdayOf(day) {
  return dateOf(day).toLocaleDateString(undefined, { weekday: "short" });
}

function monthDayOf(day) {
  return dateOf(day).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/**
 * A subject's figures as a few chips, and never a zero.
 *
 * "0 pages not transcribed" is not a fact anyone needs: a figure is shown only
 * when it is something, and in words a person would use. Rule 1 of section 1
 * still holds -- the material, with its doors, is right beneath.
 */
function Figures({ subject }) {
  const found = [
    [subject.unreviewed, "to review"],
    [subject.ready, "ready to quiz"],
    [subject.unread_pages, subject.unread_pages === 1 ? "page still to transcribe" : "pages still to transcribe"],
  ].filter(([count]) => count);
  if (!found.length && !subject.oldest_posted_at) return null;
  return html`<ul class="figures">
    ${found.map(
      ([count, words]) => html`<li key=${words} class="figure"><span class="num">${count}</span>${words}</li>`
    )}
    ${subject.state === "behind" && subject.oldest_posted_at
      ? html`<li class="figure">oldest from ${age(subject.oldest_posted_at)}</li>`
      : null}
  </ul>`;
}

/**
 * One post of this subject's: what it is, where I am with it, and the doors.
 *
 * Opening a post fetches its files on the tap rather than up front: a subject
 * can hold forty posts, and asking for forty file lists to draw forty buttons
 * would be forty requests for a screen I mostly scroll past.
 */
function Post({ post, subject }) {
  const [problem, setProblem] = useState("");
  const state = post.study_item_state;

  async function open() {
    setProblem("");
    try {
      const found = await api.get(`/api/study-items/${post.study_item_id}`);
      const file = (found.files || []).find((f) => f.readable);
      if (file) navigate(`/read/${encodeURIComponent(file.drive_id)}`);
      else setProblem("Nothing readable is held for this post yet.");
    } catch (err) {
      setProblem(describe(err));
    }
  }

  const meta = [
    plural(post.pages, "page"),
    post.unread ? `${post.unread} not transcribed` : "",
    post.creation_time ? `posted ${age(post.creation_time)}` : "",
  ].filter(Boolean);

  return html`<li>
    <div class="card has-subject post-card" style=${subjectStyleOf(subject)}>
      <div class="card-body">
        <span class="t-lead">${post.title}</span>
        ${state ? html`<span class="t-state">${ITEM_STATE[state] || state}</span>` : null}
        <span class="t-meta">${meta.join(" · ")}</span>
        <div class="post-doors">
          ${post.study_item_id
            ? html`<button class="button quiet-button" type="button" onClick=${open}>
                <${Icon} name="document" /> Open
              </button>`
            : null}
          ${state === "delivered" || state === "reviewed"
            ? html`<${QuizEntry} itemId=${post.study_item_id} compact=${true} />`
            : null}
        </div>
        ${problem ? html`<p class="t-meta">${problem}</p>` : null}
      </div>
    </div>
  </li>`;
}

export function Subject({ name }) {
  const [subject, setSubject] = useState(null);
  const [sessions, setSessions] = useState(null);
  const [posts, setPosts] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get(`/api/subjects/${encodeURIComponent(name)}`)
      .then((found) => {
        if (!live) return;
        setSubject(found);
        if (!found.course_id) return setPosts([]);
        api
          .get(`/api/library?course=${encodeURIComponent(found.course_id)}`)
          .then((rows) => live && setPosts(rows))
          .catch(() => live && setPosts([]));
      })
      .catch((err) => live && setProblem(describe(err)));
    api
      .get("/api/timetable")
      .then((found) => live && setSessions(sessionsOf(name, found)))
      .catch(() => live && setSessions([]));
    return () => {
      live = false;
    };
  }, [name]);

  if (problem) {
    return html`<div class="screen">
      <${Problem}>${problem}</${Problem}>
      <p><a class="button quiet-button" ...${linkProps("/study")}>All subjects</a></p>
    </div>`;
  }
  if (!subject) return html`<div class="screen"><${Skeleton} rows=${3} /></div>`;


  return html`
    <div class="screen subject-page" style=${subjectStyleOf(subject.name)}>
      <header class="subject-band">
        <a class="back-link t-meta" ...${linkProps("/study")}>Study</a>
        <h1 class="t-title subject-title">${subject.name}</h1>
        ${subject.state === "behind"
          ? null
          : html`<p class="t-state">${capital(standing(subject))}</p>`}
        <${Figures} subject=${subject} />
      </header>

      <div class="split">
        <section class="section">
          <h2 class="section-title">Material</h2>
          ${posts === null
            ? html`<${Skeleton} rows=${2} />`
            : posts.length
            ? html`<ul class="list">
                ${posts.map((post) => html`<${Post} key=${post.entity_id} post=${post} subject=${subject.name} />`)}
              </ul>`
            : html`<${Empty} title="Nothing here yet.">Material appears once it is posted or uploaded.</${Empty}>`}
        </section>

        ${sessions && sessions.length
          ? html`<section class="section">
              <h2 class="section-title">Coming up</h2>
              <ol class="timeline">
                ${sessions.map(
                  ({ date, session }) => html`<li
                    key=${`${date}-${session.start}`}
                    class="slot slot-dated"
                  >
                    <span class="slot-time">
                      <span class="slot-day">${weekdayOf(date)}</span>
                      <span class="time slot-start">${session.start}</span>
                    </span>
                    <span class="slot-block">
                      <span class="slot-name">${capital(kindOf(session))}</span>
                      <span class="slot-where">${[monthDayOf(date), session.parts[0] && session.parts[0].room]
                        .filter(Boolean)
                        .join(" · ")}</span>
                      ${session.note ? html`<span class="slot-where">${session.note}</span>` : null}
                    </span>
                  </li>`
                )}
              </ol>
            </section>`
          : null}
      </div>
    </div>
  `;
}
