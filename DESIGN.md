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

> Nothing to review.
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
  *(Amended 2026-10-02.)* Today opens with the date and a greeting -- "Good
  evening" -- and that greeting is the only thing in the app that reads the hour.
  It names the time of day and nothing else: no "it's late", no "still up", no
  change of colour, layout or wording anywhere else. At 23:00 it says "Good
  evening", exactly as it did at 19:00 -- and at 01:30 too: midnight to 05:00 is
  still the evening to someone still studying.
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
> Nothing to review.
> Next session: OS lab, tomorrow 08:30.

*(Was "Nothing waiting" until 2026-10-02. Today also lists what is due, so
"nothing waiting" above a homework due on Thursday said two contradictory
things. What is empty is the reading queue, so that is what it says.)*

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

- **The chrome follows the theme; neither theme is pure.** *(Amended
  2026-10-02: was "dark is the default, not a toggle".)* Light, dark or the
  system's choice, per device. Neither is pure white on pure black: the dark
  theme is off-white on warm charcoal, the light one ink on paper. Maximum
  contrast haloes text at 23:00 and makes a 40-page sitting physically tiring,
  in either.
- **The page is never inverted.** In either theme the chrome around it follows
  the theme and the PDF renders exactly as
  the professor made it. Inverting a slide deck destroys diagrams, code
  screenshots and photographed boards — which is precisely the content the vision
  OCR exists to preserve, and losing it visually after paying to read it would be
  absurd.
- **The app's own type is a legibility face at a real reading size** — 17px
  body floor, generous line height, no thin weights. This text is read tired.
  *(Amended 2026-10-02: was "a system stack". Which face is a question of how the
  app looks, which section 8 owns; what this line protects -- size, line height,
  weight, and text that paints before the font arrives -- is unchanged.)*
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
- **What has been delivered is readable offline**, and "delivered" is doing real
  work in that sentence. The reader must not be the one thing that needs a
  round-trip at 23:00.

  **The original justification was "the library is already local by invariant 5",
  and Phase 5a retired it.** That was true while the backend was this laptop. On
  the Oracle box the library is local to the *server*; the phone is a client over
  mobile data, and 116 MB is not going to live on it. The rule stands, so it needs
  an honest mechanism instead of a premise that no longer holds:

  - **Tonight's gate item, automatically.** When online, the app fetches the
    document `/` is asking for and keeps it. It is one file, a few MB, and it is
    precisely the one I will want with the radio off.
  - **Anything I explicitly keep.** A per-document action, because a 40 MB deck
    must not be cached just because I scrolled past it.
  - **Nothing else.** Everything outside those two needs a connection, and says so
    rather than failing blankly.

  Two consequences worth stating, because both would otherwise look like bugs. A
  document kept offline is fetched **whole** — the range requests that make a
  92-page deck open instantly return `206`, and a `206` cannot be stored in a
  browser cache, so reading online and keeping offline are deliberately two
  different requests. And an API screen opened offline shows the last thing it
  knew **with the time it knew it visible**; stale data presented as current would
  be the app inventing a state, which §7 forbids.

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

## 8. Visual system

Everything above says what the app may say. This says how it looks, and every
rule above wins where the two meet. Measured rather than chosen by eye: the
contrast figures are computed, and `tests/test_web_assets.py` recomputes them
from `app.css` -- in both themes -- so a token cannot drift below AA unnoticed.

*Rewritten 2026-10-02 after the first checkpoint: the structure was right and
the look was not. Dark-only, colour confined to a 6px dot, a terminal's slashed
zeros on every time, a narrow column in a wide window, and no name.*

### The idea

**A desk with a timetable pinned above it.** By day it is paper: warm, matte,
printed, the subject colours of a highlighter run over a timetable. By night it
is the same desk under a lamp: warm charcoal, never blue-black, the same colours
dimmed rather than inverted. The one memorable thing is the subject colour,
used the way a student actually uses it -- to find Database on a crowded week at
a glance -- and everything around it stays quiet.

### Name and mark

Placeholder name **Margin** -- where the notes go, and the space this app keeps
for the reading. The other two proposals: **Lectern** (the thing you read
standing at), **Lamplight** (when it is used). The mark is a page with its
margin rule: a rounded sheet, one vertical line near its left edge, and a short
line of accent beside it. It is the favicon, the PWA icon and the sidebar's mark.
`web/icon.svg` is the source; the PNGs are drawn from the same geometry.

### Themes

Light, dark and System, chosen in More and remembered per device
(`localStorage`, never synced -- it is a fact about a screen, not about me).
System is the default and follows the OS. Each theme is designed, not derived:
every colour token is one `light-dark()` pair in `app.css`, so a token that has
a light value has a dark one chosen beside it.

The reader's chrome follows the theme. **The PDF page itself is never inverted,
in either theme** -- section 6.

### Type

Two families, both self-hosted in `web/fonts/` (OFL, about 110 KB together),
precached, and swapped in over the system stack so text never waits.

- **Bricolage Grotesque** for headings, the hero, and every time and count that
  is read as a figure. A grotesque with a hand in it -- ink-trapped joins, a warm
  `a` -- and tabular figures with a plain zero, so `08:30` reads as a time and
  not as terminal output.
- **Atkinson Hyperlegible Next** for everything read as prose: designed by the
  Braille Institute for legibility, with letterforms built to be told apart.

| role | face | size / line | weight | used for |
|---|---|---|---|---|
| greeting | Bricolage | 15 / 1.4 | 500 | the date and greeting above Today |
| hero | Bricolage | 32 / 1.12 (42 at ≥1024) | 650 | the one item on Today |
| title | Bricolage | 28 / 1.15 | 650 | a screen's name |
| lead | Bricolage | 19 / 1.3 | 600 | a card's name: subject, post, session |
| body | Atkinson | 17 / 1.55 | 400 | the floor for anything read as prose |
| state | Atkinson | 17 / 1.45 | 400 | what a card's thing is doing |
| meta | Bricolage | 15 / 1.45 | 400 | which room, how many pages -- short and full of figures, so the plain zero |
| time | Bricolage | as context | 500 | every clock time, tabular |

Headings track tight (-0.02em). No weight below 400 for text, no all-caps
labels, no tracked-out eyebrows.

### Colour

| token | light | dark | role |
|---|---|---|---|
| ground | `#f3efe7` paper | `#171512` charcoal | the page |
| card | `#fbf9f5` | `#211e1a` | a sheet on it |
| raised | `#ffffff` | `#2a2621` | the hero, a hovered card |
| pressed | `#ebe5d9` | `#34302a` | pressed, selected |
| ink | `#211d18` (16:1 on card) | `#eee7db` (13.5:1) | names, titles, body |
| ink-2 | `#524b42` (8.2:1) | `#c3baab` (8.6:1) | state |
| ink-3 | `#6b6358` (5.6:1) | `#a0978a` (5.8:1) | metadata |
| accent | `#2a4f93` ink blue (7.6:1) | `#a9c1f2` (9.2:1) | focus, the primary action, "here" |
| passed | `#a8271d` (6.7:1) | `#f0958b` (7.4:1) | a deadline already gone -- nothing else |
| edge | `#8f8676` (3.4:1) | `#766d60` (3.3:1) | a control's border |
| line | `#e3ddd1` | `#35302a` | dividers |

The accent is fountain-pen blue on paper and the same ink lit on the desk at
night -- deliberately not the terracotta or the acid green every generated page
reaches for. **There is still exactly one alarm and it is `passed`.**

### Subject colour, with intent

Each subject has one hue, a pure function of its name (FNV-1a over UTF-8, mod
twelve, hues 88°-330° in 22° steps -- no red, no orange, so no subject can read
as an alarm). Each hue has three values per theme:

| value | light | dark | used for |
|---|---|---|---|
| `fg` | OKLCH L .47 | L .82 | the subject's name, its chip text, an edge |
| `tint` | L .955 | L .265 | the surface of anything that belongs to it -- a card, a page header |
| `fill` | L .865 / .82 alternating | L .40 / .34 alternating | a timetable block, a chip, the Today timeline |

Every pairing is AA in both themes (`fg` on `tint` at least 5.5:1, `ink` on
`fill` at least 7:1), and the test checks them all. The colour is never alone:
twelve hues for any number of subjects means two can share one, so the name is
always beside it.

Where it shows: a subject's card is tinted its colour; its name is a filled
chip; on the timetable each session is a filled block; on Today the day's
schedule is a column of filled blocks. A subject should be findable on a crowded
week by colour before it is read.

### Space, radius, surface

Spacing 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64. Radius says what a thing is: 10 a
control, 16 a card, 24 the hero, round for a chip. Depth is surface and a 1px
line; no drop shadows, in either theme.

### Motion

Only in answer to something I did, and every bit of it off under
`prefers-reduced-motion`:

- a card press: 120ms scale to .985;
- a page change: 180ms cross-fade, through the View Transitions API where the
  browser has it, and nothing where it does not;
- choosing a quiz answer: the option fills with the accent for 220ms before
  the next question arrives, so the choice is seen to land;
- the quiz result: the count rises into place over 260ms, once.

Nothing loops, nothing moves on its own, no skeleton shimmers. The reader keeps
section 6's ceiling of 150ms.

### Layout

| place | holds |
|---|---|
| Today | date and greeting; the one next action (sections 2 and 4) beside today's schedule as a timeline; then what is due in the next few days |
| Study | subjects grouped by the day of their next session; a subject's material, items and quizzes; Quizzes; Library |
| Work | homework (Classroom and hand-entered) and projects |
| Timetable | the week as a grid on a wide screen, a day at a time on a phone; one menu per session to record a move or a cancellation |
| More | the theme, add, status, sign out |

Below 1024px: one column and a tab bar with those five, no counts. At 1024px and
above: a sidebar, and the width used -- Today in two columns, the timetable as a
full week grid, the quiz beside its own context. Prose never exceeds 72
characters a line.

### Components

| component | what it is |
|---|---|
| shell | sidebar (mark, name, places, their parts) or tab bar |
| screen header | Bricolage title, and at most one line beneath |
| card | a whole tappable sheet; tinted when it belongs to a subject; presses in |
| chip | a subject's name on its fill, round |
| hero | the one next action: raised sheet, subject chip, Bricolage title |
| timeline | a day's sessions as filled blocks against their times |
| week grid | days across, hours down, sessions as filled blocks placed by time |
| session menu | one button per session opening Cancel and Move; never a row of buttons per line |
| quiz option | a whole row per option, its letter in a square; fills with the accent when chosen |
| empty state | the mark, small, above one line that says what is true |
| notice | a neutral block stating a fact; never alarm-coloured |
| theme choice | three options, the current one marked |

### Words

Section 5's voice, applied to the interface's own sentences: short, the
material as the subject, never the system describing its plumbing. Zero values
are not shown -- "0 pages not transcribed" is not a fact anyone needs. Figures
are one readable line or a few chips, never a key-value table.

---

## Whether this is working

Four questions, answerable by looking:

1. Opening it at 23:00 having done nothing — does it feel like somewhere I can
   spend twenty minutes, or somewhere I am about to be told off?
2. Is the honest button still the fastest one on every screen?
3. Does an empty day still say almost nothing?
4. Can I find every number the coverage figure is built from?
