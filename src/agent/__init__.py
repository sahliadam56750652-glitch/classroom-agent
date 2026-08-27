"""classroom-agent: personal Google Classroom sync, catch-up tracker and gate."""

from __future__ import annotations

import os

# This must be set before anything in this package imports oauthlib, which
# arrives indirectly via google_auth_oauthlib -> requests_oauthlib -> oauthlib.
# Python runs this module before any agent.* submodule, so it is the one place
# guaranteed to be early enough.
#
# Google's grant can legitimately come back narrower than requested. oauthlib's
# default is to raise its "Scope has changed" warning *as an exception* out of
# the token exchange, which turns a mismatch we could report and act on into an
# opaque crash during auth. auth.check_scopes() reports it properly instead.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

__version__ = "0.1.0"
