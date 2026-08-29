# DESIGN.md — the brief the Phase 5 client is built against

`CLAUDE.md` says how to build. `PLAN.md` says what and in what order. This says
how it should feel, and what it must never do to me to get there.

This is a brief, not a spec. No components, no framework, no layout grid. Every
rule here is meant to be testable by looking at a screen and asking whether it
holds.

The web client inherits the Telegram layer's stance rather than replacing it.
Four decisions are already made there and are not reopened here: **silence when
there is nothing to report** (`composer.compose` returns `None` and nothing is
sent); **Skip always available and always logged** — the button says
`⏭ Skip — log it`, because hiding the logging would make the button a trap;
**a flagged question leaves the denominator** rather than counting as wrong, so
honesty never costs me the pass; and **the gate never manufactures urgency** —
one prompt a day, and a day with nothing on it sends nothing.

---

## 1. Emotional stance

**The principle.** This app is the only thing that knows exactly how far behind
I am, and it does not care. It is a colleague who has read the material and will
tell me what is in it — not an invigilator keeping a record against me. Its job
is to make the next twenty minutes of reading obvious. Its job is not to have an
opinion about the last three weeks.

Three rules that follow, in the order they get broken:

1. **No number without an action next to it.** Any count of what I have not done
   must appear on the same screen as the one thing that would reduce it. A
   deficit with no affordance attached is not information, it is an accusation.
   If a figure has no action — a subject with no readable material, an archived
   term — it does not go on the default screen at all.

2. **The material is the subject of the sentence, never me.** "Chapter 3 is
   unreviewed", not "you haven't reviewed Chapter 3". The Telegram layer already
   writes this way — *"The item stays read, not verified"* — and it is not
   politeness. The app knows the state of a file; it does not know what my week
   was, so it should not write sentences that imply it does.

3. **The honest button is always the cheapest one.** Skip is one tap, on the
   same screen, at the same visual weight as Read, and it says what it records.
   The flag is on every question. Read-not-verified is never buried behind a
   confirmation. The moment the honest path costs more taps than the flattering
   one, I will stop taking it, and the coverage figure becomes fiction — which
   is the only failure this project cannot recover from.

---

## 2. The default screen

**The next single action. Decided.**

Opening the app with unreviewed material lands on one item: subject, title, the
window I am being asked for (`pages 21–42 of 92`), how much of it is readable,
and one primary action. The deficit appears as **one subordinate line above it**
— `Database · 6 unreviewed` — and nowhere else.

Why not the deficit. I already know I am behind; that is why I opened the app.
Re-stating it costs the whole first screen and buys nothing I did not walk in
with. What I do not know at 23:00, and what the app is uniquely able to answer,
is *which twenty pages*. A dashboard makes me do the triage the scheduler
already did.

There is a second reason, and it is structural. The Telegram gate is already
this shape: it names tomorrow, lists the subjects, and every subject is a
button. If the web client opens on a wall of figures, it is a regression to a
worse interface for the same data.

The whole picture stays **one tap away and never the front door**. Per-subject
standing, the backlog, coverage — all reachable, none of it the thing I meet
first.

---

## 3. How progress is shown

The line: **absolute counts of finished work, yes. Percentages of a deficit,
never. Streaks, never.**

- **Counts, with an honest denominator.** `4 verified · 2 read · 6 unreviewed`.
  A count is something I can move — twelve becoming eleven is visibly a thing I
  did tonight. `34%` becoming `36%` is the same feeling twice, and a percentage
  is precisely the transformation from a number I can act on into a number I can
  only feel.
- **A bar is allowed for one item and nothing larger.** `window 3 of 6` inside a
  92-page chapter is a finite thing that finishes tonight, so a bar is honest
  there. A bar for a subject, a term, or "overall coverage" is a progress
  indicator for something that never completes, which is a mood ring.
- **No red on unreviewed.** The only red in this app is a deadline that has
  already passed. Unreviewed is the resting state of most material most of the
  time, and colouring the normal case as an alarm teaches me to ignore the
  colour.
- **Four states, never collapsed into a number.** `messages._subject_line`
  already distinguishes them and the client must too: *no Classroom course*, *no
  readable material*, *up to date*, and *N unreviewed*. Probability & Statistics
  has 20 dead attachments and no study items at all — it renders as "no readable
  material" and must never render as 0%, as 100%, or as "up to date". Same for a
  subject whose pages are not yet transcribed: `6 unreviewed, 2 ready` says the
  true thing that one number cannot.
- **No comparison to last week.** "5 verified this week" is a fact. "Down from
  9" invents a target I never set and makes an ordinary week a decline.

---

## 4. The empty state and the worst state

### Nothing due, everything done

One line, then get out of the way.

> Nothing waiting.
> Next session: OS lab, tomorrow 08:30.

No charts backfilled to occupy the space, no "great work", no suggestion of
something else to do. This is the same decision as `compose()` returning `None`:
an interface that performs busyness when there is nothing to say trains me to
stop reading it, and then the screen that mattered goes with it.

### 23:00, nothing done, a lecture at 08:30

**This is the state the app will be in most often, so it is the state to design
first.** Everything else is the exception.

- **It looks the same as any other state.** No alarm colour, no countdown, no
  change in tone. The app has no information at 23:00 that it lacked at 19:00;
  behaving differently is theatre.
- **It asks for the smallest true thing.** One window — roughly twenty pages,
  the ones tomorrow's session actually needs — never the 92-page chapter. This
  is what Phase 3d exists for, and until it ships the client shows the window
  boundary from `agent sections` as read-only context rather than pretending the
  whole post is the ask.
- **If the material is not readable, it says so instead of offering a quiz.**
  `I have not read all of this lecture yet — 26 pages are not transcribed. This
  counts as read, not verified.` A button that will apologise is worse than no
  button; `item_keyboard` already omits the quiz on an unready item.
- **Skip is right there, same weight, and states the record.**
  `⏭ Skip — logged`. No confirmation dialog. A second tap to be honest is a tax
  on honesty.
- **Closing the app having done nothing costs me nothing.** No penalty accrues
  overnight, no streak breaks, and opening it at 07:00 shows the same screen with
  the same item. The backlog is a fact about the material — `study_items` never
  expire — not a debt that compounds while I sleep.

---

## 5. Voice — six real moments

Short. Specific. The material is the subject. No exclamation marks anywhere in
this app.

**A clear day**
> Nothing waiting.
> Next session: OS lab, tomorrow 08:30.

*No praise.* An empty queue is a fact about what the professors posted, not
something I achieved — and praising it makes every other day a reproach.

**New material arriving**
> 🆕 new material — Database · Chapter 4, SQL joins
> 38 pages · 12 not yet readable

*States the size, because the size is the decision.* "3 files" says nothing
about whether that is a page of admin or forty pages of lecture notes.
`_material_facts` already does this; the client keeps it.

**A deadline in 24 hours**
> ⏰ due in under 24 h — TP3 Graphes · Tue 15 Sep 08:30

*The time is the fact.* No countdown, no colour escalation as it nears, no
second reminder invented by the client. Urgency is mine to feel; the app's job
is to be accurate about when.

**A failed quiz**
> Not passed — 3 of 6 · pass mark 75%
> The item stays read, not verified.
> **What was missed** …

And on the third failure of the same lecture, verbatim from `result_message`:

> It is worth considering that the questions are wrong rather than that you are
> — flag the set and it will be regenerated from scratch.

*The failure spends its length on what was missed, with the page named.* The
point of failing is knowing what to reread.

**A skipped gate**
> Skipped — Chapter 1, logged 23:41.

*One line, past tense, and then nothing.* No "are you sure", no re-offer, no
softer nag tomorrow morning. Skip being fully honoured is what keeps `skipped`
meaning "I chose to duck this" rather than "the app wore me down".

**A subject slipping**
> Database — 6 unreviewed · oldest posted 3 weeks ago
> Next: SQL joins, pages 1–20

*Names the age, which is the real signal, and ends on the action per rule 1.*
"Slipping" is my word for this, never the app's. No "falling behind", no "needs
attention", no icon that means worry.

---

## 6. Reading

Most of my time here is dense PDFs, on a phone, at night. The reader is the
product; everything else is navigation.

- **Dark is the default, not a toggle.** Not pure white on pure black — an
  off-white on a near-black ground. Maximum contrast haloes text at 23:00 and
  makes a 40-page sitting physically tiring.
- **The page is never inverted.** App chrome is dark; the PDF renders exactly as
  the professor made it. Inverting a slide deck destroys diagrams, code
  screenshots and photographed boards — which is precisely the content the vision
  OCR exists to preserve, and losing it visually after paying to read it would be
  absurd.
- **The app's own type is a system stack at a real reading size** — 17px body
  floor, generous line height, no thin weights. This text is read tired.
- **On scroll, the chrome goes.** One element persists: position within the
  window (`34 / 42`). No floating action button, no toolbar, no toast, no
  animation over 150ms, no page-turn effect.
- **Pinch-zoom is never disabled.** A 92-page deck has diagrams that only work
  zoomed, and disabling zoom is the single most common way a reader becomes
  unusable on a phone.
- **Position is restored, and a lost position is said out loud.** Reopening a
  document returns to where I stopped and says so once. When the anchor is gone
  because the document was re-uploaded, the app says the document changed rather
  than silently starting from page one — the same rule as the 3d cursor, and the
  same instinct as the recurring lesson: a silent fallback is a misreport.
- **What has been delivered is readable offline.** The library is already local
  by invariant 5. The reader must not be the one thing that needs a round-trip at
  23:00.

---

## 7. What the app must NEVER do

- **No streaks.** A streak attaches a cost to a missed day that has nothing to do
  with the material, and once broken it is an argument for giving up entirely.
  There is no version of this that survives one bad week of a real degree.
- **No guilt copy.** No "you haven't", no "still", no "again", no exclamation on
  a deficit, no red on unreviewed, no emoji that means disappointment.
- **No notification the app invented.** Every push corresponds to an external
  fact: a deadline, new material, tomorrow's session. Never "you have not opened
  this in three days". Never a re-nag of a prompt I already answered.
- **No badge that only counts up.**
- **Nothing that makes honesty cost me something.** Skip never asks twice. The
  flag never counts as wrong. Read-not-verified is never one screen deeper than
  verified. If the honest tap is ever slower than the flattering one, that is a
  bug of the same severity as a wrong answer.
- **No percentage of a deficit, and no 0% or 100% for a subject with no readable
  material.** Untracked must read as untracked.
- **No celebration.** Confetti on a verified item makes an unverified one a
  failure, and most items are unverified most of the time.
- **No manufactured urgency.** No countdown timers, no colour that escalates as a
  deadline nears, no "only 9 hours left". The deadline scanner fires at 72 h,
  24 h and 3 h and that is the whole of it.
- **Never the whole 92-page chapter as tonight's ask.** That is the failure Phase
  3d exists to fix, and reproducing it in a nicer typeface fixes nothing.
- **Never a state the app counts but will not show me.** Anything feeding the
  Phase 4 coverage figure must be reachable in the UI. A number I cannot audit is
  a number I will not trust, and an untrusted coverage figure is a project that
  has failed.

---

## Whether this is working

Four questions, answerable by looking:

1. Opening it at 23:00 having done nothing — does it feel like somewhere I can
   spend twenty minutes, or somewhere I am about to be told off?
2. Is the honest button still the fastest one on every screen?
3. Does an empty day still say almost nothing?
4. Can I find every number the coverage figure is built from?
