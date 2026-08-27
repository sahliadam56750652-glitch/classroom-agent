#!/usr/bin/env python3
# Dependencies are covered by pyproject.toml, installed via `pip install -e ".[dev]"`.
"""
probe.py -- throwaway feasibility spike against the Google Classroom API.

Single file, procedural, no config, no abstractions, no database. Run it once,
read the console output and dump.json, then delete it.

Writes into the folder it lives in:
    token.json   cached OAuth credentials
    dump.json    every raw API response this script received
    sample/      up to 5 downloaded PDF attachments

Every numbered step is wrapped in its own try/except: one permission failure
prints a traceback and the probe keeps going.
"""

from __future__ import annotations

import os

# Must be set before anything imports oauthlib, which arrives indirectly via
# google_auth_oauthlib -> requests_oauthlib -> oauthlib. When Google returns a
# narrower scope set than we asked for, oauthlib's default is to raise its
# "Scope has changed" Warning *as an exception* out of the token exchange --
# which aborts the OAuth step and cascades into every step needing a client.
# A scope mismatch is a finding, not a crash: relaxing this lets step 2 report
# it while the probe goes on to collect the data we came for.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

import io
import json
import random
import re
import sys
import time
import traceback
from collections import Counter

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# PyMuPDF ships as `fitz`, and as `pymupdf` from 1.24 on. Missing entirely is
# survivable -- only step 8 needs it.
try:
    import fitz
except ImportError:
    try:
        import pymupdf as fitz
    except ImportError:
        fitz = None

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, "credentials.json")
TOKEN_FILE = os.path.join(HERE, "token.json")
DUMP_FILE = os.path.join(HERE, "dump.json")
SAMPLE_DIR = os.path.join(HERE, "sample")

MAX_PDFS = 5
SCANNED_THRESHOLD = 100  # avg chars/page below this == probably a scan

# Two Google accounts are in play on this project. Authenticating as the wrong
# one does not error -- it returns a valid, EMPTY Classroom, which is
# indistinguishable from "you genuinely have no courses" until a whole run has
# been spent reading it. Step 1b checks this against the account Google says we
# actually are, and stops the run on a mismatch.
EXPECTED_ACCOUNT = "sahliadam56750652@gmail.com"


class WrongAccount(Exception):
    """Fatal. Deliberately not swallowed by the per-step handler in main()."""

# The six scopes asked for in the spec.
SPEC_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    # Deliberately NOT the .readonly variant. classroom.coursework.me.readonly
    # is real and documented, but it cannot be selected in the Cloud console's
    # scope picker, so it never lands on the OAuth client and Google strips it
    # from the grant -- you get 6 of 7 scopes back and the resulting 403s read
    # like missing data rather than missing permission. classroom.coursework.me
    # IS registrable and is a superset of it.
    #
    # The cost: this scope is READ-WRITE. It permits turning in work, modifying
    # submissions and reclaiming assignments. The probe must never call any
    # write method -- every Classroom call in this file is .list() or .get(),
    # and that is the only thing keeping invariant 6 (read-only against Google)
    # true now that the scope itself no longer enforces it.
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# courses.topics.list has its own scope and is NOT covered by any of the six
# above -- without this, step 4's topic listing is a guaranteed 403. Flip to
# False to probe the spec's exact scope set and watch topics fail.
INCLUDE_TOPICS_SCOPE = True
TOPICS_SCOPE = "https://www.googleapis.com/auth/classroom.topics.readonly"

SCOPES = SPEC_SCOPES + ([TOPICS_SCOPE] if INCLUDE_TOPICS_SCOPE else [])


# --------------------------------------------------------------------------
# global state (procedural spike; globals are fine)
# --------------------------------------------------------------------------

# Every raw API response lands here and gets written to dump.json in step 9.
RAW = {
    "tokeninfo": None,
    "about": None,
    "courses_pages": [],
    "courses_pages_studentid_me": [],
    "per_course": {},   # courseId -> {"course":..., "<resource>_pages":[...]}
    "drive_files": {},  # fileId -> raw files.get response (or error record)
}

STATE = {
    "creds": None,
    "classroom": None,
    "drive": None,
    "scopes_granted": [],
    "courses": [],
    "per_course": {},   # courseId -> parsed lists
    "attachments": [],
    "pdf_results": [],
    "failed_steps": [],
}


# --------------------------------------------------------------------------
# printing helpers
# --------------------------------------------------------------------------

def banner(num, title):
    print()
    print("=" * 78)
    print("STEP %s: %s" % (num, title))
    print("=" * 78)


def sub(title):
    print()
    print("-- " + title)


def trunc(value, width):
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "~"


def print_table(headers, rows, caps=None, indent="   "):
    if not rows:
        print(indent + "(none)")
        return
    caps = caps or [40] * len(headers)
    cells = [[trunc(r[i], caps[i]) for i in range(len(headers))] for r in rows]
    widths = []
    for i, head in enumerate(headers):
        widths.append(max([len(head)] + [len(row[i]) for row in cells]))
    print(indent + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print(indent + "-+-".join("-" * w for w in widths))
    for row in cells:
        print(indent + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def print_counter(counter, indent="   ", empty="(none)"):
    if not counter:
        print(indent + empty)
        return
    width = max(len(str(k)) for k in counter)
    for key, count in counter.most_common():
        print("%s%s  %d" % (indent, str(key).ljust(width), count))


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------

RETRY_STATUSES = {429, 500, 502, 503, 504}


def execute(request, what="", tries=5):
    """Execute one googleapiclient request with backoff on 429/5xx."""
    for attempt in range(1, tries + 1):
        try:
            return request.execute()
        except HttpError as err:
            status = getattr(err.resp, "status", None)
            if status in RETRY_STATUSES and attempt < tries:
                delay = min(30.0, 2.0 ** attempt) + random.random()
                print("   [retry %d/%d] %s -> HTTP %s, sleeping %.1fs"
                      % (attempt, tries - 1, what, status, delay))
                time.sleep(delay)
                continue
            raise


def paginate(list_method, params, item_key, sink, what=""):
    """Follow nextPageToken to exhaustion. Appends every raw page to `sink`."""
    items = []
    page_token = None
    page_num = 0
    while True:
        page_num += 1
        call = dict(params)
        if page_token:
            call["pageToken"] = page_token
        response = execute(list_method(**call), what="%s page %d" % (what, page_num))
        sink.append(response)
        items.extend(response.get(item_key) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def require_clients():
    if STATE["classroom"] is None:
        raise RuntimeError("no Classroom client -- step 1 (auth) did not complete")


def fmt_due_date(due_date):
    if not due_date:
        return "-"
    return "%04d-%02d-%02d" % (due_date.get("year", 0),
                               due_date.get("month", 0),
                               due_date.get("day", 0))


def fmt_due_time(due_time):
    # dueTime omits zero-valued fields entirely, so 00:00 arrives as {}.
    if due_time is None:
        return "-"
    return "%02d:%02d" % (due_time.get("hours", 0), due_time.get("minutes", 0))


def safe_filename(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file").strip("._")
    return (cleaned or "file")[:80]


# --------------------------------------------------------------------------
# STEP 1 -- OAuth
# --------------------------------------------------------------------------

def step_1_auth():
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            "credentials.json not found at %s -- download the Desktop OAuth "
            "client JSON from Google Cloud Console and put it there."
            % CREDENTIALS_FILE)

    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            print("   loaded cached token from token.json")
        except Exception:
            print("   cached token.json unreadable, ignoring it:")
            traceback.print_exc()
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            print("   refreshed expired access token")
        except Exception:
            print("   refresh failed, falling back to a fresh consent flow:")
            traceback.print_exc()
            creds = None

    if not creds or not creds.valid:
        print("   starting installed-app OAuth flow (a browser window opens)")
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        # prompt=consent so the consent screen always shows every checkbox and
        # we reliably get a refresh_token back.
        creds = flow.run_local_server(port=0, prompt="consent")

    with open(TOKEN_FILE, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
    print("   token cached to token.json")

    STATE["creds"] = creds
    STATE["classroom"] = build("classroom", "v1", credentials=creds, cache_discovery=False)
    STATE["drive"] = build("drive", "v3", credentials=creds, cache_discovery=False)
    print("   built classroom v1 + drive v3 clients")


# --------------------------------------------------------------------------
# STEP 1b -- which account did we actually authenticate as?
# --------------------------------------------------------------------------

def step_1b_account_guard():
    if STATE["drive"] is None:
        raise RuntimeError("no Drive client -- step 1 did not complete")

    about = execute(STATE["drive"].about().get(fields="user"), what="drive.about.get")
    RAW["about"] = about
    user = about.get("user") or {}
    actual = user.get("emailAddress")

    print("   authenticated as: %s" % (actual or "(Drive returned no emailAddress)"))
    print("   display name:     %s" % (user.get("displayName") or "(none)"))
    print("   expected account: %s" % EXPECTED_ACCOUNT)

    if actual and actual.strip().lower() == EXPECTED_ACCOUNT.strip().lower():
        print()
        print("   OK -- account matches.")
        return

    print()
    print("!" * 78)
    print("!!  WRONG GOOGLE ACCOUNT -- STOPPING THE RUN")
    print("!!")
    print("!!    expected:  %s" % EXPECTED_ACCOUNT)
    if actual:
        print("!!    actual:    %s" % actual)
    else:
        print("!!    actual:    UNVERIFIABLE -- Drive reported no emailAddress.")
        print("!!               Treated as a mismatch: an unconfirmed identity is")
        print("!!               exactly what this guard exists to refuse.")
    print("!!")
    print("!!  Nothing downstream would have raised. The other account answers")
    print("!!  every call successfully and returns an EMPTY Classroom -- zero")
    print("!!  courses, zero coursework -- which reads as real data right up")
    print("!!  until you act on it.")
    print("!!")
    print("!!  Fix: delete token.json, re-run, and choose the right account on")
    print("!!  the consent screen. If a browser is already signed in as the other")
    print("!!  account it will be picked silently -- sign out first, or run the")
    print("!!  flow in a fresh browser profile.")
    print("!!")
    print("!!  Stopping here. No dump.json will be written from this account.")
    print("!" * 78)

    raise WrongAccount("expected %s, authenticated as %s"
                       % (EXPECTED_ACCOUNT, actual or "an unidentified account"))


# --------------------------------------------------------------------------
# STEP 2 -- granted vs requested scopes
# --------------------------------------------------------------------------

def step_2_scopes():
    creds = STATE["creds"]
    if creds is None:
        raise RuntimeError("no credentials -- step 1 did not complete")

    # Captured for dump.json, and used as a cross-check plus a fallback source.
    tokeninfo_scopes = None
    try:
        resp = requests.get("https://oauth2.googleapis.com/tokeninfo",
                            params={"access_token": creds.token}, timeout=20)
        try:
            body = resp.json()
        except ValueError:
            body = {"_raw_text": resp.text}
        RAW["tokeninfo"] = {"http_status": resp.status_code, "body": body}
        if resp.ok:
            tokeninfo_scopes = (body.get("scope") or "").split()
        else:
            print("   tokeninfo cross-check returned HTTP %s" % resp.status_code)
    except Exception:
        print("   tokeninfo cross-check failed (not fatal):")
        traceback.print_exc()

    # The credentials object is the authoritative source. granted_scopes holds
    # the `scope` value Google actually returned alongside the token, which is
    # the one place a silently-dropped scope becomes visible.
    granted = list(getattr(creds, "granted_scopes", None) or [])
    source = "credentials.granted_scopes"

    if not granted and tokeninfo_scopes is not None:
        # Older google-auth doesn't round-trip granted_scopes through token.json.
        granted = list(tokeninfo_scopes)
        source = "oauth2 tokeninfo endpoint (granted_scopes unavailable)"

    if not granted:
        # Last resort, and worth naming: creds.scopes is the list we ASKED for,
        # so MISSING computed from it is always empty. That would report a clean
        # bill of health in exactly the situation this step exists to catch.
        granted = list(creds.scopes or [])
        source = "credentials.scopes -- THIS IS THE REQUESTED LIST, A DROP CANNOT BE SEEN"

    STATE["scopes_granted"] = granted
    granted_set = set(granted)
    missing = [s for s in SCOPES if s not in granted_set]
    extra = sorted(granted_set - set(SCOPES))

    print("   scope source: %s" % source)

    sub("REQUESTED (%d)" % len(SCOPES))
    for scope in SCOPES:
        print("   %s   [%s]"
              % (scope, "spec" if scope in SPEC_SCOPES else "added by probe"))

    sub("GRANTED (%d)" % len(granted))
    if granted:
        for scope in sorted(granted):
            print("   %s" % scope)
    else:
        print("   (none reported)")

    sub("MISSING -- requested but not granted (%d)" % len(missing))
    if missing:
        for scope in missing:
            print("   %s" % scope)
    else:
        print("   (none)")

    sub("GRANTED BUT NOT REQUESTED (%d)" % len(extra))
    if extra:
        for scope in extra:
            print("   %s" % scope)
        print("   (openid / userinfo.* are normal -- Google adds them)")
    else:
        print("   (none)")

    if tokeninfo_scopes is not None and set(tokeninfo_scopes) != granted_set:
        sub("NOTE: tokeninfo disagrees with the credentials object")
        print("   only in tokeninfo: %s" % (sorted(set(tokeninfo_scopes) - granted_set) or "none"))
        print("   only in creds:     %s" % (sorted(granted_set - set(tokeninfo_scopes)) or "none"))
        print("   Trust tokeninfo -- it reflects the live token server-side.")

    print()
    if missing:
        print("!" * 78)
        print("!!  SCOPE MISMATCH -- %d REQUESTED SCOPE(S) WERE NOT GRANTED" % len(missing))
        print("!!")
        for scope in missing:
            print("!!    %s" % scope)
        print("!!")
        print("!!  Every call needing one of these will 403, and a 403 here looks")
        print("!!  exactly like missing data. Read the later steps with that in mind.")
        print("!!")
        print("!!  Two causes, in order of likelihood:")
        print("!!   1. The scope is not registered on the OAuth client. Some scopes")
        print("!!      -- notably certain .readonly variants -- cannot be selected")
        print("!!      in the Cloud console picker at all, and Google strips them")
        print("!!      from the grant without erroring.")
        print("!!   2. Granular consent: a checkbox was left unticked on the screen.")
        print("!!")
        print("!!  Fix: register the scope, delete token.json, re-run, tick every box.")
        print("!!")
        print("!!  NOT FATAL. The probe continues so you still get the rest of the data.")
        print("!" * 78)
    else:
        print("   OK -- every requested scope was granted.")

    if INCLUDE_TOPICS_SCOPE:
        print()
        print("   NOTE: classroom.topics.readonly is not in the spec's six scopes.")
        print("         courses.topics.list requires it, so the probe adds it.")
        print("         Set INCLUDE_TOPICS_SCOPE = False to test the exact spec set.")


# --------------------------------------------------------------------------
# STEP 3 -- list all courses, ACTIVE + ARCHIVED
# --------------------------------------------------------------------------

def step_3_courses():
    require_clients()
    svc = STATE["classroom"]

    courses = paginate(
        svc.courses().list,
        {"courseStates": ["ACTIVE", "ARCHIVED"], "pageSize": 100},
        "courses",
        RAW["courses_pages"],
        what="courses.list",
    )
    STATE["courses"] = courses

    sub("courses.list(courseStates=[ACTIVE, ARCHIVED])  -- %d course(s), %d page(s)"
        % (len(courses), len(RAW["courses_pages"])))
    print_table(
        ["id", "name", "section", "courseState", "creationTime"],
        [[c.get("id"), c.get("name"), c.get("section"),
          c.get("courseState"), c.get("creationTime")] for c in courses],
        caps=[16, 40, 24, 12, 26],
    )

    by_state = Counter(c.get("courseState") for c in courses)
    sub("courses by state")
    print_counter(by_state)

    if not by_state.get("ARCHIVED"):
        print()
        print("   *** NO ARCHIVED COURSES RETURNED ***")
        print("   Either the account genuinely has none, or courseStates was ignored.")
        print("   Cross-check against classroom.google.com/h -> Archived classes.")

    # Cross-check: does restricting to studentId='me' change the picture? The
    # next phase needs to know whether to filter by role.
    try:
        mine = paginate(
            svc.courses().list,
            {"studentId": "me", "courseStates": ["ACTIVE", "ARCHIVED"], "pageSize": 100},
            "courses",
            RAW["courses_pages_studentid_me"],
            what="courses.list(studentId=me)",
        )
        mine_ids = {c.get("id") for c in mine}
        all_ids = {c.get("id") for c in courses}
        sub("cross-check: courses.list(studentId='me') -- %d course(s)" % len(mine))
        if mine_ids == all_ids:
            print("   Identical set. You are a STUDENT in every visible course.")
        else:
            only_all = sorted(all_ids - mine_ids)
            only_mine = sorted(mine_ids - all_ids)
            print("   Sets differ.")
            print("   Visible but not enrolled as student (teacher/owner?): %s"
                  % (only_all or "none"))
            print("   Student-only, missing from the unfiltered call: %s"
                  % (only_mine or "none"))
    except Exception:
        print("   studentId='me' cross-check failed:")
        traceback.print_exc()


# --------------------------------------------------------------------------
# STEP 4 -- coursework / materials / announcements / topics per course
# --------------------------------------------------------------------------

def step_4_course_contents():
    require_clients()
    svc = STATE["classroom"]
    courses = STATE["courses"]
    if not courses:
        raise RuntimeError("no courses from step 3 -- nothing to enumerate")

    resources = [
        ("courseWork", lambda cid: svc.courses().courseWork().list, "courseWork"),
        ("courseWorkMaterials", lambda cid: svc.courses().courseWorkMaterials().list, "courseWorkMaterial"),
        ("announcements", lambda cid: svc.courses().announcements().list, "announcements"),
        ("topics", lambda cid: svc.courses().topics().list, "topic"),
    ]

    count_rows = []

    for course in courses:
        cid = course.get("id")
        cname = course.get("name")
        cstate = course.get("courseState")

        raw_bucket = RAW["per_course"].setdefault(cid, {"course": course})
        parsed = STATE["per_course"].setdefault(cid, {"course": course, "errors": {}})

        sub("COURSE %s | %s | %s" % (cid, cname, cstate))

        counts = {}
        for label, method_getter, item_key in resources:
            sink = raw_bucket.setdefault(label + "_pages", [])
            try:
                items = paginate(
                    method_getter(cid),
                    {"courseId": cid, "pageSize": 100},
                    item_key,
                    sink,
                    what="%s (%s)" % (label, cid),
                )
                parsed[label] = items
                counts[label] = len(items)
            except Exception as err:
                parsed[label] = []
                parsed["errors"][label] = repr(err)
                counts[label] = "ERROR"
                print("   %s FAILED for course %s:" % (label, cid))
                traceback.print_exc()

        print("   counts: courseWork=%s  courseWorkMaterials=%s  announcements=%s  topics=%s"
              % (counts.get("courseWork"), counts.get("courseWorkMaterials"),
                 counts.get("announcements"), counts.get("topics")))

        count_rows.append([cid, cname, cstate,
                           counts.get("courseWork"), counts.get("courseWorkMaterials"),
                           counts.get("announcements"), counts.get("topics")])

        # --- coursework detail ------------------------------------------
        work = parsed.get("courseWork") or []
        if work:
            print()
            print("   courseWork detail:")
            print_table(
                ["id", "title", "workType", "dueDate", "dueTime", "maxPoints", "topicId", "state"],
                [[w.get("id"), w.get("title"), w.get("workType"),
                  fmt_due_date(w.get("dueDate")), fmt_due_time(w.get("dueTime")),
                  w.get("maxPoints"), w.get("topicId"), w.get("state")] for w in work],
                caps=[14, 34, 18, 10, 8, 9, 14, 12],
                indent="     ",
            )

        # --- materials preview ------------------------------------------
        materials = parsed.get("courseWorkMaterials") or []
        if materials:
            print()
            print("   courseWorkMaterials detail:")
            print_table(
                ["id", "title", "#materials", "topicId", "state", "updateTime"],
                [[m.get("id"), m.get("title"), len(m.get("materials") or []),
                  m.get("topicId"), m.get("state"), m.get("updateTime")] for m in materials],
                caps=[14, 40, 10, 14, 12, 26],
                indent="     ",
            )

        # --- announcements preview --------------------------------------
        announcements = parsed.get("announcements") or []
        if announcements:
            print()
            print("   announcements (first 10 of %d):" % len(announcements))
            print_table(
                ["id", "updateTime", "#materials", "text"],
                [[a.get("id"), a.get("updateTime"), len(a.get("materials") or []),
                  a.get("text")] for a in announcements[:10]],
                caps=[14, 26, 10, 60],
                indent="     ",
            )

        # --- topics ------------------------------------------------------
        topics = parsed.get("topics") or []
        if topics:
            print()
            print("   topics:")
            print_table(
                ["topicId", "name", "updateTime"],
                [[t.get("topicId"), t.get("name"), t.get("updateTime")] for t in topics],
                caps=[14, 44, 26],
                indent="     ",
            )

    sub("COUNTS PER COURSE")
    print_table(
        ["id", "name", "state", "work", "materials", "announce", "topics"],
        count_rows,
        caps=[16, 34, 10, 8, 10, 9, 8],
    )


# --------------------------------------------------------------------------
# STEP 5 -- all my submissions per course in one call (courseWorkId='-')
# --------------------------------------------------------------------------

def step_5_submissions():
    require_clients()
    svc = STATE["classroom"]
    courses = STATE["courses"]
    if not courses:
        raise RuntimeError("no courses from step 3 -- nothing to fetch")

    overall_states = Counter()
    history_populated = 0
    total_subs = 0

    for course in courses:
        cid = course.get("id")
        cname = course.get("name")
        raw_bucket = RAW["per_course"].setdefault(cid, {"course": course})
        parsed = STATE["per_course"].setdefault(cid, {"course": course, "errors": {}})
        sink = raw_bucket.setdefault("studentSubmissions_pages", [])

        sub("SUBMISSIONS %s | %s" % (cid, cname))
        try:
            submissions = paginate(
                svc.courses().courseWork().studentSubmissions().list,
                {"courseId": cid, "courseWorkId": "-", "userId": "me", "pageSize": 100},
                "studentSubmissions",
                sink,
                what="studentSubmissions (%s)" % cid,
            )
        except Exception as err:
            parsed["submissions"] = []
            parsed["errors"]["studentSubmissions"] = repr(err)
            print("   FAILED:")
            traceback.print_exc()
            continue

        parsed["submissions"] = submissions
        total_subs += len(submissions)
        print("   %d submission(s) in %d page(s) via courseWorkId='-'"
              % (len(submissions), len(sink)))

        rows = []
        for s in submissions:
            history = s.get("submissionHistory") or []
            if history:
                history_populated += 1
            overall_states[s.get("state")] += 1
            rows.append([
                s.get("courseWorkId"), s.get("id"), s.get("state"),
                s.get("late", False), s.get("assignedGrade"), s.get("draftGrade"),
                "yes (%d)" % len(history) if history else "no",
            ])

        shown = rows[:15]
        print_table(
            ["courseWorkId", "submissionId", "state", "late", "assignedGrade", "draftGrade", "history"],
            shown,
            caps=[14, 14, 22, 6, 13, 11, 10],
            indent="     ",
        )
        if len(rows) > len(shown):
            print("     ... %d more (full data in dump.json)" % (len(rows) - len(shown)))

    sub("SUBMISSION TOTALS")
    print("   total submissions: %d" % total_subs)
    print("   with a populated submissionHistory: %d / %d" % (history_populated, total_subs))
    print()
    print("   by state:")
    print_counter(overall_states, indent="     ")


# --------------------------------------------------------------------------
# STEP 6 -- is there ANY comment field in the payloads?
# --------------------------------------------------------------------------

def walk_key_paths(obj, prefix="", out=None):
    """Collect every dotted key path present anywhere in a JSON structure."""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = "%s.%s" % (prefix, key) if prefix else key
            out.add(path)
            walk_key_paths(value, path, out)
    elif isinstance(obj, list):
        for item in obj:
            walk_key_paths(item, prefix + "[]", out)
    return out


def step_6_comment_fields():
    all_paths = walk_key_paths(RAW)
    if not all_paths:
        raise RuntimeError("no captured responses to scan -- earlier steps produced nothing")

    pattern = re.compile(r"comment|feedback|remark|annotation", re.IGNORECASE)
    hits = sorted(p for p in all_paths if pattern.search(p.rsplit(".", 1)[-1]))

    sub("scanned %d distinct key paths across every captured response" % len(all_paths))

    print()
    print("   keys matching /comment|feedback|remark|annotation/:")
    if hits:
        for path in hits:
            print("     %s" % path)
    else:
        print("     (none)")

    # Show the complete key surface of a StudentSubmission so the verdict is
    # checkable rather than asserted.
    sub_paths = set()
    for bucket in RAW["per_course"].values():
        for page in bucket.get("studentSubmissions_pages", []):
            for submission in page.get("studentSubmissions") or []:
                sub_paths |= walk_key_paths(submission)

    print()
    print("   every field the API actually returned on a StudentSubmission:")
    if sub_paths:
        for path in sorted(sub_paths):
            print("     %s" % path)
    else:
        print("     (no submissions were fetched -- step 5 returned nothing)")

    sub("DEFINITIVE ANSWER: teacher / private comments on submissions")
    if hits:
        print("   YES -- comment-like field(s) found. Listed above. Inspect dump.json.")
    else:
        print("   NO. Not one comment-bearing field appears anywhere in the payloads.")
    print()
    print("   Context for the verdict: Classroom REST v1 has no representation for")
    print("   private comments or class comments. StudentSubmission carries only")
    print("   submissionHistory, which is stateHistory + gradeHistory entries --")
    print("   who changed state or grade and when, never any text a teacher wrote.")
    print("   Comments live in the Classroom UI only. Plan around that.")


# --------------------------------------------------------------------------
# STEP 7 -- every attachment, everywhere
# --------------------------------------------------------------------------

KNOWN_MATERIAL_KEYS = ("driveFile", "youtubeVideo", "link", "form")


def describe_material(material):
    """Return (att_type, drive_id, title, url) for one Classroom material."""
    if "driveFile" in material:
        drive_file = (material.get("driveFile") or {}).get("driveFile") or {}
        return ("driveFile", drive_file.get("id"), drive_file.get("title"),
                drive_file.get("alternateLink"))
    if "youtubeVideo" in material:
        video = material.get("youtubeVideo") or {}
        return ("youTube", video.get("id"), video.get("title"), video.get("alternateLink"))
    if "link" in material:
        link = material.get("link") or {}
        return ("link", None, link.get("title"), link.get("url"))
    if "form" in material:
        form = material.get("form") or {}
        return ("form", None, form.get("title"), form.get("formUrl"))
    # Anything Google adds later shows up under its own key.
    unknown = [k for k in material.keys() if k not in KNOWN_MATERIAL_KEYS]
    return ("UNKNOWN:" + (",".join(unknown) or "?"), None, None, None)


def lookup_drive_file(file_id):
    """files.get with caching; records errors instead of raising."""
    if file_id in RAW["drive_files"]:
        return RAW["drive_files"][file_id]
    drive = STATE["drive"]
    if drive is None:
        record = {"_error": "no drive client"}
        RAW["drive_files"][file_id] = record
        return record
    try:
        record = execute(
            drive.files().get(
                fileId=file_id,
                fields="id,name,mimeType,size,trashed,shortcutDetails,owners(emailAddress)",
                supportsAllDrives=True,
            ),
            what="drive.files.get(%s)" % file_id,
        )
    except Exception as err:
        record = {"_error": repr(err)}
    RAW["drive_files"][file_id] = record
    return record


def step_7_attachments():
    if not STATE["per_course"]:
        raise RuntimeError("no per-course data from step 4 -- nothing to scan")

    attachments = []

    for cid, parsed in STATE["per_course"].items():
        cname = (parsed.get("course") or {}).get("name")

        sources = [
            ("courseWork", parsed.get("courseWork") or [], "title"),
            ("courseWorkMaterial", parsed.get("courseWorkMaterials") or [], "title"),
            ("announcement", parsed.get("announcements") or [], "text"),
        ]
        for parent_type, parents, title_field in sources:
            for parent in parents:
                parent_title = parent.get(title_field)
                for material in parent.get("materials") or []:
                    att_type, drive_id, title, url = describe_material(material)
                    attachments.append({
                        "course_id": cid,
                        "course_name": cname,
                        "parent_type": parent_type,
                        "parent_id": parent.get("id"),
                        "parent_title": parent_title,
                        "att_type": att_type,
                        "drive_id": drive_id,
                        "title": title,
                        "url": url,
                        "mime_type": None,
                        "download_id": drive_id,
                    })

    # Classroom does not return mimeType -- Drive has to be asked.
    drive_ids = sorted({a["drive_id"] for a in attachments
                        if a["att_type"] == "driveFile" and a["drive_id"]})
    if drive_ids:
        print("   resolving mimeType for %d Drive file(s) via drive.files.get..." % len(drive_ids))
    for file_id in drive_ids:
        lookup_drive_file(file_id)

    for att in attachments:
        if att["att_type"] != "driveFile" or not att["drive_id"]:
            att["mime_type"] = "n/a"
            continue
        record = RAW["drive_files"].get(att["drive_id"]) or {}
        if "_error" in record:
            att["mime_type"] = "ERR: " + trunc(record["_error"], 60)
            continue
        mime = record.get("mimeType")
        # A shortcut points at the real file; follow it for typing/downloading.
        if mime == "application/vnd.google-apps.shortcut":
            details = record.get("shortcutDetails") or {}
            att["mime_type"] = "shortcut -> " + str(details.get("targetMimeType"))
            att["download_id"] = details.get("targetId") or att["drive_id"]
        else:
            att["mime_type"] = mime
        if record.get("trashed"):
            att["mime_type"] = (att["mime_type"] or "") + " [TRASHED]"

    STATE["attachments"] = attachments

    sub("ATTACHMENT TABLE (%d total)" % len(attachments))
    print_table(
        ["course", "parent type", "parent title", "att type", "drive id", "title", "mimeType"],
        [[a["course_name"], a["parent_type"], a["parent_title"], a["att_type"],
          a["drive_id"], a["title"], a["mime_type"]] for a in attachments],
        caps=[22, 18, 30, 12, 34, 34, 34],
    )

    sub("attachments by parent type")
    by_parent = Counter(a["parent_type"] for a in attachments)
    print_counter(by_parent)

    if by_parent:
        top_type, top_count = by_parent.most_common(1)[0]
        total = sum(by_parent.values())
        print()
        print("   MOST ATTACHMENTS: %s -- %d of %d (%.0f%%)"
              % (top_type, top_count, total, 100.0 * top_count / total))

    sub("attachments by attachment type")
    print_counter(Counter(a["att_type"] for a in attachments))

    sub("Drive attachments by mimeType")
    print_counter(Counter(a["mime_type"] for a in attachments
                          if a["att_type"] == "driveFile"))


# --------------------------------------------------------------------------
# STEP 8 -- download PDFs and check whether they hold real text
# --------------------------------------------------------------------------

def step_8_pdfs():
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed -- pip install PyMuPDF")
    if STATE["drive"] is None:
        raise RuntimeError("no Drive client -- step 1 did not complete")

    candidates = [a for a in STATE["attachments"]
                  if a["att_type"] == "driveFile"
                  and a["mime_type"]
                  and "application/pdf" in a["mime_type"]
                  and "TRASHED" not in a["mime_type"]]

    print("   %d PDF attachment(s) found; downloading up to %d"
          % (len(candidates), MAX_PDFS))
    if not candidates:
        print("   nothing to download")
        return

    os.makedirs(SAMPLE_DIR, exist_ok=True)
    drive = STATE["drive"]
    results = []
    seen_ids = set()

    for att in candidates:
        if len(results) >= MAX_PDFS:
            break
        file_id = att["download_id"] or att["drive_id"]
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)

        name = safe_filename(att["title"] or file_id)
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        path = os.path.join(SAMPLE_DIR, name)

        record = {"drive_id": file_id, "title": att["title"], "path": path,
                  "course": att["course_name"], "error": None,
                  "pages": None, "chars": None, "avg": None, "verdict": None}

        sub("PDF: %s" % trunc(att["title"], 60))
        try:
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(
                buffer,
                drive.files().get_media(fileId=file_id, supportsAllDrives=True),
            )
            done = False
            while not done:
                _status, done = downloader.next_chunk()
            data = buffer.getvalue()
            with open(path, "wb") as handle:
                handle.write(data)
            print("   downloaded %d bytes -> %s" % (len(data), os.path.relpath(path, HERE)))
        except Exception as err:
            record["error"] = "download: %r" % (err,)
            print("   DOWNLOAD FAILED -- exact error:")
            print("   %r" % (err,))
            traceback.print_exc()
            results.append(record)
            continue

        try:
            doc = fitz.open(path)
            if getattr(doc, "needs_pass", False):
                record["error"] = "extract: PDF is password protected"
                print("   ENCRYPTED -- cannot extract text")
                doc.close()
                results.append(record)
                continue
            pages = doc.page_count
            chars = sum(len(page.get_text() or "") for page in doc)
            doc.close()
            avg = (chars / pages) if pages else 0.0
            record.update(pages=pages, chars=chars, avg=avg,
                          verdict="SCANNED?" if avg < SCANNED_THRESHOLD else "native")
            print("   pages=%d  chars=%d  avg=%.1f chars/page  -> %s"
                  % (pages, chars, avg, record["verdict"]))
            if avg < SCANNED_THRESHOLD:
                print("   *** under %d chars/page -- likely a scan, needs OCR ***"
                      % SCANNED_THRESHOLD)
        except Exception as err:
            record["error"] = "extract: %r" % (err,)
            print("   TEXT EXTRACTION FAILED -- exact error:")
            print("   %r" % (err,))
            traceback.print_exc()

        results.append(record)

    STATE["pdf_results"] = results

    sub("PDF SUMMARY")
    print_table(
        ["title", "pages", "chars", "avg/page", "verdict", "error"],
        [[r["title"], r["pages"], r["chars"],
          "%.1f" % r["avg"] if r["avg"] is not None else "-",
          r["verdict"] or "-", r["error"] or ""] for r in results],
        caps=[34, 6, 9, 9, 10, 46],
    )


# --------------------------------------------------------------------------
# STEP 9 -- dump every raw response
# --------------------------------------------------------------------------

def step_9_dump():
    payload = dict(RAW)
    payload["_probe"] = {
        "scopes_requested": SCOPES,
        "scopes_granted": STATE["scopes_granted"],
        "attachments_flattened": STATE["attachments"],
        "pdf_results": STATE["pdf_results"],
        "failed_steps": STATE["failed_steps"],
    }
    with open(DUMP_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
    print("   wrote %s (%.1f KB)" % (DUMP_FILE, os.path.getsize(DUMP_FILE) / 1024.0))


# --------------------------------------------------------------------------
# STEP 10 -- final summary
# --------------------------------------------------------------------------

def step_10_summary():
    courses = STATE["courses"]
    per_course = STATE["per_course"]
    attachments = STATE["attachments"]

    total_work = sum(len(p.get("courseWork") or []) for p in per_course.values())
    total_materials = sum(len(p.get("courseWorkMaterials") or []) for p in per_course.values())
    total_announcements = sum(len(p.get("announcements") or []) for p in per_course.values())
    total_topics = sum(len(p.get("topics") or []) for p in per_course.values())
    total_submissions = sum(len(p.get("submissions") or []) for p in per_course.values())

    work_with_due = 0
    courses_with_due = 0
    for parsed in per_course.values():
        hits = [w for w in (parsed.get("courseWork") or []) if w.get("dueDate")]
        work_with_due += len(hits)
        if hits:
            courses_with_due += 1

    sub("TOTALS")
    by_state = Counter(c.get("courseState") for c in courses)
    print("   courses ............. %d  (%s)"
          % (len(courses),
             ", ".join("%s=%d" % (k, v) for k, v in by_state.most_common()) or "none"))
    print("   courseWork .......... %d" % total_work)
    print("   courseWorkMaterials . %d" % total_materials)
    print("   announcements ....... %d" % total_announcements)
    print("   topics .............. %d" % total_topics)
    print("   studentSubmissions .. %d" % total_submissions)
    print("   attachments ......... %d" % len(attachments))

    sub("ATTACHMENTS BY TYPE")
    print_counter(Counter(a["att_type"] for a in attachments))

    sub("ATTACHMENTS BY MIMETYPE (Drive files only)")
    print_counter(Counter(a["mime_type"] for a in attachments if a["att_type"] == "driveFile"))

    sub("ATTACHMENTS BY PARENT TYPE")
    print_counter(Counter(a["parent_type"] for a in attachments))

    sub("DUE DATES")
    print("   courses with at least one usable dueDate: %d / %d"
          % (courses_with_due, len(courses)))
    print("   courseWork items with a dueDate: %d / %d" % (work_with_due, total_work))
    if total_work:
        print("   coverage: %.0f%%" % (100.0 * work_with_due / total_work))
    print("   NOTE: dueDate/dueTime are UTC and dueTime omits zero fields,")
    print("         so a midnight deadline arrives as dueTime={}.")

    sub("PDF VERDICT")
    results = STATE["pdf_results"]
    if not results:
        print("   no PDFs were tested")
    else:
        native = sum(1 for r in results if r["verdict"] == "native")
        scanned = sum(1 for r in results if r["verdict"] == "SCANNED?")
        failed = sum(1 for r in results if r["error"])
        print("   tested %d | native text %d | likely scanned %d | failed %d"
              % (len(results), native, scanned, failed))
        if scanned and not native:
            print("   => Course PDFs look SCANNED. Text extraction alone will not work; OCR needed.")
        elif native and not scanned:
            print("   => Course PDFs carry NATIVE text. PyMuPDF extraction is viable.")
        elif native and scanned:
            print("   => MIXED. Detect per-file with the chars/page check and OCR the rest.")
        else:
            print("   => Inconclusive; every candidate failed. See the errors above.")

    sub("STEPS THAT FAILED")
    if STATE["failed_steps"]:
        for num, title in STATE["failed_steps"]:
            print("   step %s (%s)" % (num, title))
    else:
        print("   none -- every step completed")

    sub("ARTEFACTS")
    print("   dump.json  every raw API response")
    print("   sample/    downloaded PDFs")
    print("   token.json cached OAuth credentials (do NOT commit)")


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

STEPS = [
    (1, "OAuth installed-app flow", step_1_auth),
    ("1b", "Account guard", step_1b_account_guard),
    (2, "Granted vs requested scopes", step_2_scopes),
    (3, "All courses (ACTIVE + ARCHIVED)", step_3_courses),
    (4, "courseWork / materials / announcements / topics", step_4_course_contents),
    (5, "My submissions (courseWorkId='-')", step_5_submissions),
    (6, "Teacher / private comment fields?", step_6_comment_fields),
    (7, "Every attachment", step_7_attachments),
    (8, "Download PDFs + PyMuPDF text check", step_8_pdfs),
    (9, "Write dump.json", step_9_dump),
    (10, "Final summary", step_10_summary),
]


def main():
    print("classroom-agent feasibility probe")
    print("working folder: %s" % HERE)
    print("python: %s" % sys.version.split()[0])
    print("PyMuPDF: %s" % (getattr(fitz, "__doc__", "installed") if fitz else "NOT INSTALLED"))

    for num, title, func in STEPS:
        banner(num, title)
        try:
            func()
        except WrongAccount:
            # The one failure worth aborting for. Every later step would succeed
            # against the wrong account and write a plausible-looking empty
            # dump.json, so "continue and report" is the dangerous option here.
            raise
        except Exception:
            STATE["failed_steps"].append((num, title))
            print()
            print("*** STEP %s FAILED -- continuing ***" % num)
            traceback.print_exc()

    print()
    print("=" * 78)
    print("PROBE COMPLETE -- %d step(s) failed" % len(STATE["failed_steps"]))
    print("=" * 78)


if __name__ == "__main__":
    main()
