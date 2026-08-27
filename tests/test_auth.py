"""Auth checks, exercised offline with stubs. Nothing here touches the network."""

from __future__ import annotations

import json

import pytest
from google.oauth2.credentials import Credentials

from agent.auth import (
    SCOPES,
    ScopeMismatch,
    WrongAccount,
    _load_token,
    _save_token,
    check_account,
    check_scopes,
)

ACCOUNT = "sahliadam56750652@gmail.com"


class FakeDrive:
    """Stands in for the Drive service: drive.about().get(...).execute()."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def about(self):
        return self

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def execute(self):
        return self.payload


def make_credentials(granted=None) -> Credentials:
    return Credentials(
        token="access-token",
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=list(SCOPES),
        granted_scopes=list(granted) if granted is not None else None,
    )


# --------------------------------------------------------------------------
# scopes
# --------------------------------------------------------------------------

def test_all_scopes_granted_passes():
    assert check_scopes(SCOPES) == list(SCOPES)


def test_extra_scopes_are_fine():
    """Google always adds openid / userinfo.*; that is not a mismatch."""
    granted = list(SCOPES) + ["openid", "https://www.googleapis.com/auth/userinfo.email"]
    assert check_scopes(granted) == granted


def test_scopes_granted_out_of_order_pass():
    assert check_scopes(list(reversed(SCOPES)))


def test_missing_scope_raises_and_names_it():
    dropped = "https://www.googleapis.com/auth/classroom.coursework.me"
    granted = [scope for scope in SCOPES if scope != dropped]

    with pytest.raises(ScopeMismatch) as err:
        check_scopes(granted)

    message = str(err.value)
    assert dropped in message
    assert "missing" in message
    # The surviving scopes are listed too, so the report is self-contained.
    assert "https://www.googleapis.com/auth/drive.readonly" in message


def test_several_missing_scopes_are_all_named():
    granted = SCOPES[:2]
    with pytest.raises(ScopeMismatch) as err:
        check_scopes(granted)
    for scope in SCOPES[2:]:
        assert scope in str(err.value)


@pytest.mark.parametrize("granted", [None, []])
def test_unknown_or_empty_grant_is_a_mismatch(granted):
    """Never pass silently when the granted set could not be determined."""
    with pytest.raises(ScopeMismatch):
        check_scopes(granted)


def test_check_scopes_cannot_be_fooled_by_the_requested_list():
    """The guard against reading credentials.scopes instead of granted_scopes.

    A Credentials object always reports the full requested list in .scopes, even
    when Google granted less. Feeding the granted list through is the only way
    the mismatch is visible.
    """
    creds = make_credentials(granted=SCOPES[:3])

    assert creds.scopes == list(SCOPES)          # echoes the request
    check_scopes(creds.scopes)                   # ...so this wrongly passes
    with pytest.raises(ScopeMismatch):
        check_scopes(creds.granted_scopes)       # the real answer


# --------------------------------------------------------------------------
# account
# --------------------------------------------------------------------------

def test_matching_account_returns_the_address():
    drive = FakeDrive({"user": {"emailAddress": ACCOUNT, "displayName": "Adam"}})
    assert check_account(drive, ACCOUNT) == ACCOUNT
    assert drive.calls == [{"fields": "user"}]


@pytest.mark.parametrize(
    "reported",
    [
        ACCOUNT.upper(),
        f"  {ACCOUNT}  ",
        "SahliAdam56750652@Gmail.com",
    ],
)
def test_account_comparison_is_case_insensitive_and_stripped(reported):
    drive = FakeDrive({"user": {"emailAddress": reported}})
    assert check_account(drive, ACCOUNT) == reported


def test_expected_account_is_also_stripped_and_lowered():
    drive = FakeDrive({"user": {"emailAddress": ACCOUNT}})
    assert check_account(drive, f"  {ACCOUNT.upper()} ") == ACCOUNT


def test_wrong_account_raises_naming_both_addresses():
    other = "other.account@gmail.com"
    drive = FakeDrive({"user": {"emailAddress": other}})

    with pytest.raises(WrongAccount) as err:
        check_account(drive, ACCOUNT)

    message = str(err.value)
    assert ACCOUNT in message
    assert other in message


@pytest.mark.parametrize("payload", [{}, {"user": {}}, {"user": {"displayName": "X"}}, {"user": None}])
def test_missing_email_address_is_a_mismatch(payload):
    """An identity we cannot confirm is not an identity we accept."""
    drive = FakeDrive(payload)
    with pytest.raises(WrongAccount) as err:
        check_account(drive, ACCOUNT)
    assert "unverifiable" in str(err.value)


# --------------------------------------------------------------------------
# token round-trip
# --------------------------------------------------------------------------

def test_granted_scopes_survive_a_save_load_round_trip(tmp_path):
    """google-auth drops granted_scopes on to_json(); we persist it ourselves.

    Without this the scope check would work on the run that consented and then
    silently have nothing to check on every run afterwards.
    """
    path = tmp_path / "token.json"
    granted = SCOPES[:4]

    _save_token(path, make_credentials(granted=granted), granted)
    loaded, loaded_granted = _load_token(path)

    assert loaded is not None
    assert loaded_granted == list(granted)
    assert json.loads(path.read_text(encoding="utf-8"))["granted_scopes"] == list(granted)


def test_save_token_creates_the_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deeper" / "token.json"
    _save_token(path, make_credentials(granted=SCOPES), SCOPES)
    assert path.is_file()


def test_absent_token_loads_as_nothing(tmp_path):
    assert _load_token(tmp_path / "token.json") == (None, [])


def test_corrupt_token_loads_as_nothing_rather_than_raising(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{ not json", encoding="utf-8")
    assert _load_token(path) == (None, [])


def test_token_without_granted_scopes_reports_none(tmp_path):
    """A token from an older build or from probe.py cannot be verified."""
    path = tmp_path / "token.json"
    data = json.loads(make_credentials().to_json())
    path.write_text(json.dumps(data), encoding="utf-8")

    creds, granted = _load_token(path)

    assert creds is not None
    assert granted == []
    with pytest.raises(ScopeMismatch):
        check_scopes(granted)
