# Phase 5a — moving to the Oracle Always Free box

The runbook. `PLAN.md` says why; this says what to type, in order.

Everything here rests on invariant 5: the database, the token, the library and
the logs all live under `DATA_DIR`, and no absolute path appears in the source.
The move is therefore a directory copy. **The one thing that is not under
`DATA_DIR` is the configuration** — `config.yaml`, `.env` and `timetable.yaml`
sit at the repo root and are all gitignored, so they arrive via neither
`git clone` nor a `data/` copy. Copying `data/` alone produces a box that
cannot start.

Placeholders: `BOX` is the instance's IP, `AGENT_HOME` is
`/home/ubuntu/classroom-agent`. The units in `systemd/` hard-code that path;
`sed -i` them if it differs.

---

## 0. Pre-flight, on the laptop — do this first

**Check the OAuth consent screen's publishing status.** Google Cloud console →
APIs & Services → OAuth consent screen.

If it says **Testing**, refresh tokens expire after 7 days. On the laptop that
is invisible — `auth.py` deletes the dead token and opens a browser, you click,
it works. On a headless box it is a hard stop that arrives a week after cutover
and presents as "the sync stopped working". **Move it to In production before
copying the token.** Unverified production is fine for one user: you see the
unverified-app interstitial once at consent time and proceed.

This is the single item on this page that fails late, silently, and for a reason
that does not look like its cause.

## 1. Provision

`VM.Standard.A1.Flex`, 2 OCPU / 12 GB, Ubuntu LTS, **50 GB boot volume**, in the
tenancy's home region (Always Free cannot be created elsewhere).

Not 200 GB: the Always Free block allowance is 200 GB *total* across at most two
volumes and the boot volume counts. The library is 116 MB.

Expect `Out of host capacity` on A1, possibly for days. Script the retry rather
than clicking, and try every availability domain. If it never lands,
`VM.Standard.E2.1.Micro` (x86, also Always Free) will run the sync, gate and
bot — but set `ocr.run_limit: 0` on it, because 1 GB of RAM is thin for PyMuPDF
rendering a page to an image, and leave OCR on the laptop until the ARM box
exists.

Then:

    sudo timedatectl set-timezone Africa/Tunis

Stored timestamps do not care (everything goes through
`datetime.now(timezone.utc)`) and neither does the meaning of "tomorrow"
(`cli._tomorrow` reads `config.timezone`, not the OS). This is so `OnCalendar`
and journald agree with the config. A box left on UTC just shifts all three
units an hour later in local terms and still works — the mistake is visible
rather than silent, but fix it anyway.

## 2. Snapshot the laptop

Stop every writer first. Not "wait until they are idle" — **disable** the two
Task Scheduler entries and kill the `pythonw.exe` running `agent bot`, so the
19:30 run cannot fire mid-transfer.

    python -c "import sqlite3; c=sqlite3.connect('data/academic.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
    ls data/academic.db-wal data/academic.db-shm   # must be gone

    python deploy/fingerprint.py ./data --manifest laptop-manifest.txt > laptop.txt
    cat laptop.txt

The checkpoint is the same reasoning `db/store.py` already writes down for the
v2 to v3 backup: WAL keeps recent writes outside the `.db` file, so copying an
un-checkpointed database produces a copy missing the newest rows — precisely the
copy that looks fine until it is needed.

Then transfer three sets:

    git clone <repo>                                    # code, on the box
    tar czf data.tgz data/ && scp data.tgz BOX:         # data
    scp config.yaml .env timetable.yaml BOX:AGENT_HOME/ # config

Leave behind: `.venv/`, `__pycache__/`, `.pytest_cache/`, and
`data/academic.db.bak-v2` (the v2 to v3 migration backup — laptop history, no
reason for it on the server).

## 3. Install

    curl -LsSf https://astral.sh/uv/install.sh | sh
    cd AGENT_HOME
    uv venv --python 3.13 .venv       # pyproject pins >=3.13; Ubuntu LTS ships 3.12
    source .venv/bin/activate

    pip install --only-binary=:all: pymupdf   # force the wheel, so a fallback to
                                              # a source build fails loudly rather
                                              # than hanging on a missing toolchain
    pip install -e .                          # EDITABLE -- see below
    chmod 600 data/token.json data/credentials.json .env

**`pip install -e .` is not a preference.** `config.REPO_ROOT` is
`Path(__file__).resolve().parents[2]`. From
`.venv/lib/python3.13/site-packages/agent/config.py` that resolves to
`.venv/lib/python3.13/`, so `DEFAULT_CONFIG_PATH` becomes
`.venv/lib/python3.13/config.yaml` and every command fails with "No config file
at …". Editable keeps `REPO_ROOT` pointing at the checkout.

## 4. Verify before cutting over

    python deploy/fingerprint.py ./data > server.txt
    diff laptop.txt server.txt          # must be empty
    cd data/library && sha256sum -c ../../laptop-manifest.txt && cd -

    python -m pytest -q                 # 979 passed; all offline
    agent whoami                        # right account, all 7 scopes
    agent timetable --check
    agent ocr --status                  # queue head must match the laptop exactly
    agent sync --dry-run                # ZERO events
    agent notify --dry-run              # deadlines in Africa/Tunis, as on the laptop

Two of those are load-bearing:

- **`agent sync --dry-run` reporting zero events** is the best single proof
  nothing was lost. An incomplete database reports a wall of "new" rather than
  silence, because the differ compares stored state against live state.
- **`agent ocr --status` matching** is a strong check precisely because the
  queue order is a function of the material alone — not the clock, not OCR
  progress, pinned by tests. Any difference means the data moved wrong rather
  than that the box is different.

## 5. Cut over

    sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now classroom-agent-run.timer \
                                classroom-agent-gate.timer \
                                classroom-agent-keepalive.timer \
                                classroom-agent-bot.service
    systemctl list-timers 'classroom-agent-*'

**The bot cutover is a hard switch with no overlap.** Telegram hands each update
to exactly one `getUpdates` caller, so two `agent bot` processes on one token
split the button presses between them at random and the loser silently does
nothing. Disable the Windows Startup `.vbs` *before* starting the service here,
not after. Five minutes of overlap is enough to lose taps.

Then, in order: press a button on the phone and watch
`journalctl -u classroom-agent-bot -f`; run `agent ocr --limit 1` and confirm
`ocr_pages ok` goes to 45; let one real 19:30 + 20:00 cycle happen.

**After seven days**, confirm the token still refreshes with no intervention.
That is step 0 paying off, and it is the last thing that can still go wrong.

## 6. Afterwards

**The laptop becomes the dev machine, the OAuth browser, and the cold standby.
Nothing scheduled runs on it.**

Develop against a snapshot, never the live database:

    DATA_DIR=./data-dev agent sections --item 8

`_resolve_data_dir` already lets the environment variable win over
`config.yaml`, so this needs no code and no second config file. Seed `data-dev/`
by pulling from the server, **one way, always**. And leave `TELEGRAM_BOT_TOKEN`
out of the dev environment: `telegram_settings` then raises a `ConfigError`
naming exactly what is missing, `sync`, `extract`, `ocr`, `sections` and
`quiz --dry-run` all still work, and `notify`, `gate` and `bot` refuse. Dev is
then *structurally* unable to message the real chat rather than relying on care.

Weekly, from the laptop — a **pull**, so a confused server cannot overwrite the
standby:

    rsync -av BOX:AGENT_HOME/data/ ./data-standby/

This doubles as the liveness check. A reclaimed instance fails by going quiet,
which is the failure mode this project ranks worst, and a weekly pull that
errors is the signal. `sync_runs` should also gain two rows a day.

## 7. Rollback

The recovery artifact is `data/` plus the three root config files — **not the
VM**. Nothing in the systemd units is Oracle-specific.

If the instance is reclaimed, or Oracle changes its terms: re-enable the two
Task Scheduler entries and the Startup `.vbs`, restore the last pull, run
`agent sync`. Catch-up safety is what makes this cheap — one run reports
everything that changed while the box was gone, no gaps and no duplicates.
Between reclamation and restore the gate and bot are down; the sync loses
nothing.

**Do the restore drill once, before trusting it.** Point `DATA_DIR` at a pulled
snapshot and run `agent sync --dry-run`; expect zero events. A backup that has
never been restored is a belief, not a backup.

Keep the laptop's virtualenv working. Do not tidy it up after cutover.

## Re-consenting on a box with no browser

When the token is eventually revoked, `agent auth` needs a browser to complete
the loopback flow. Tunnel one:

    ssh -L 8765:localhost:8765 ubuntu@BOX     # from the laptop
    OAUTH_PORT=8765 agent auth                # on the box
    # paste the printed URL into the laptop's browser

`OAUTH_PORT` exists for exactly this: `run_local_server(port=0)` picks a random
port, and a tunnel has to be opened before the port is known. Unset, it stays 0
and the laptop behaves as it always has.

The always-available fallback is to run `agent auth` on the laptop and re-copy
`data/token.json` — nothing in that file is bound to a machine.
