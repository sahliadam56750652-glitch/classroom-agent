"""OAuth for the single account this tool belongs to.

Read-only in practice, not by scope. The grant includes
`classroom.coursework.me`, which is read-write, because Google will not let the
`.readonly` variant be registered in the Cloud console -- it gets stripped from
the grant with no error. Invariant 6 is therefore enforced here in code: nothing
in this codebase may call a Classroom write method.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    # Deliberately not the .readonly variant: it cannot be selected in the Cloud
    # console scope picker and the "Pasted Scopes" box silently ignores it, so
    # Google strips it from the grant and every courses.courseWork.list call
    # 403s in a way that looks like missing data. This one is read-write.
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class AuthError(Exception):
    """Anything that stops us obtaining usable credentials."""


class ScopeMismatch(AuthError):
    """Google granted fewer scopes than we asked for."""


class WrongAccount(AuthError):
    """The credentials belong to somebody other than config.account."""


# --------------------------------------------------------------------------
# checks (kept small and argument-driven so they are testable offline)
# --------------------------------------------------------------------------

def check_scopes(granted: Sequence[str] | None) -> list[str]:
    """Raise ScopeMismatch unless every scope in SCOPES was actually granted.

    `granted` must come from the grant itself -- Credentials.granted_scopes on a
    fresh flow, or the copy we persisted from it. Never Credentials.scopes: that
    only echoes back what we requested, so "requested minus granted" computed
    from it is always empty, reporting a clean bill of health in exactly the
    situation this check exists to catch.
    """
    granted_list = list(granted or [])
    granted_set = set(granted_list)
    missing = [scope for scope in SCOPES if scope not in granted_set]
    if not missing:
        return granted_list

    raise ScopeMismatch(
        "Google did not grant every scope this tool needs.\n"
        "  missing:\n"
        + "".join(f"    {scope}\n" for scope in missing)
        + "  granted:\n"
        + ("".join(f"    {scope}\n" for scope in sorted(granted_list)) or "    (none)\n")
        + "Calls needing a missing scope return 403, which reads like missing "
        "data rather than missing permission.\n"
        "Register the scope on the OAuth client, delete the cached token, then "
        "run `agent auth` again and tick every box.\n"
        "Note that some .readonly scopes cannot be registered in the Cloud "
        "console at all and are dropped from the grant without an error."
    )


def check_account(drive, expected: str) -> str:
    """Raise WrongAccount unless Drive reports `expected` as the signed-in user.

    Two Google accounts are in play on this project. The wrong one authenticates
    perfectly and returns an empty Classroom, which is indistinguishable from
    "you have no courses" until a whole sync has been spent on it. A missing
    emailAddress counts as a mismatch: an unconfirmed identity is exactly what
    this refuses to proceed on.
    """
    about = drive.about().get(fields="user").execute()
    actual = (about.get("user") or {}).get("emailAddress")

    if actual and actual.strip().lower() == expected.strip().lower():
        return actual

    raise WrongAccount(
        "Authenticated as the wrong Google account.\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual or '(Drive reported no emailAddress -- unverifiable)'}\n"
        "Nothing downstream would have raised: the other account answers every "
        "call and returns an empty Classroom.\n"
        "Delete the cached token, run `agent auth` again, and pick the right "
        "account on the consent screen. A browser already signed in as the "
        "other account gets chosen silently."
    )


# --------------------------------------------------------------------------
# token storage
# --------------------------------------------------------------------------

def _save_token(path: Path, creds: Credentials, granted: Sequence[str]) -> None:
    """Write the token plus the granted scope list, which google-auth drops.

    Measured on google-auth 2.57: Credentials.to_json() omits granted_scopes and
    from_authorized_user_info() ignores the key even when it is present, so
    granted_scopes is populated only on a fresh flow. Persisting it ourselves is
    what lets check_scopes() run on every invocation instead of just the first.
    Do not "simplify" this back to creds.to_json().
    """
    data = json.loads(creds.to_json())
    data["granted_scopes"] = list(granted)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_token(path: Path) -> tuple[Credentials | None, list[str]]:
    """Return (credentials, granted_scopes) from the token file, or (None, [])."""
    if not path.is_file():
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(data, scopes=SCOPES)
    except (OSError, ValueError, KeyError, TypeError):
        # A corrupt or hand-edited token is not worth diagnosing -- re-consent.
        return None, []
    return creds, list(data.get("granted_scopes") or [])


def read_granted_scopes(config: Config) -> list[str]:
    """The scopes recorded at the last successful grant. Empty if unknown."""
    return _load_token(config.token_path)[1]


def account_email(creds: Credentials) -> str | None:
    """The address Drive reports for these credentials."""
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    about = drive.about().get(fields="user").execute()
    return (about.get("user") or {}).get("emailAddress")


# --------------------------------------------------------------------------
# the flow
# --------------------------------------------------------------------------

def _run_flow(config: Config) -> Credentials:
    if not config.credentials_path.is_file():
        raise AuthError(
            f"No OAuth client file at {config.credentials_path}.\n"
            "Download the Desktop OAuth client JSON from the Google Cloud "
            "console and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(config.credentials_path), SCOPES)
    # select_account so a browser already signed in elsewhere cannot silently
    # pick the other account; consent so a refresh token always comes back.
    return flow.run_local_server(port=0, prompt="consent select_account")


def get_credentials(config: Config) -> Credentials:
    """Return verified credentials for config.account, running OAuth if needed.

    Verifies both the granted scopes and the authenticated account before
    handing anything back, so no caller has to remember to.
    """
    creds, granted = _load_token(config.token_path)
    refresh_failure: RefreshError | None = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(config.token_path, creds, granted)
        except RefreshError as err:
            # Revoked, expired beyond refresh, or the grant changed underneath
            # us. Drop the token and consent once more.
            refresh_failure = err
            config.token_path.unlink(missing_ok=True)
            creds, granted = None, []

    if creds and not creds.valid:
        creds, granted = None, []

    if creds and not granted:
        # We cannot verify a grant we never recorded -- an older build or a
        # hand-made token. Re-consent rather than skip the scope check.
        creds, granted = None, []

    if creds is None:
        try:
            creds = _run_flow(config)
        except AuthError:
            raise
        except Exception as err:
            if refresh_failure is not None:
                raise AuthError(
                    f"The cached token could not be refreshed ({refresh_failure}), "
                    f"so it was deleted and a fresh consent flow was attempted -- "
                    f"and that failed too: {err}\n"
                    f"Check {config.credentials_path} and network access, then "
                    f"run `agent auth` again."
                ) from err
            raise AuthError(f"OAuth flow failed: {err}") from err
        granted = list(creds.granted_scopes or [])
        _save_token(config.token_path, creds, granted)

    check_scopes(granted)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    check_account(drive, config.account)
    return creds
