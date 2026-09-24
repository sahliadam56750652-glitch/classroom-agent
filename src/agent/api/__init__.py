"""The HTTP API behind the web client. Phase 5b.

Three rules hold across every module in this package, and each is pinned by a
test in `tests/test_api_guards.py` rather than left to discipline:

**No business logic.** Every figure returned here is computed by a function the
CLI already calls. Where that computation used to live in `cli.py` it moved down
into `entries.py` or `gate/adjustments.py`; it was not written a second time. Two
implementations of one rule is how the CLI and the API come to disagree, and a
disagreement between them looks exactly like either one working.

**Read-only over `study_items`, and the gate is never fired.** `verified` has
exactly one writer and this is not it. Nothing here imports
`store.verify_study_item`, `store.advance_study_item` or `gate/quiz.py`, and
nothing writes a `gate_runs` row -- computing a plan and recording that a prompt
was sent are different acts, and conflating them would let opening the dashboard
in the morning silently swallow that evening's prompt.

**Every route is `def`, never `async def`.** A sync route runs in Starlette's
threadpool, where the per-request `sqlite3` connection this package opens is
correct and safe. One `async def` route touching the store would block the event
loop and put an async framework around a sync store, which is the stated reason
`python-telegram-bot` was rejected twice.
"""
