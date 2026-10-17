"""`agent serve` starts under pythonw.exe, where there is no stdout or stderr.

The Startup .vbs runs it with pythonw so no console window opens at logon. There
sys.stdout and sys.stderr are None, and an unconditional `sys.stdout.flush()`
killed the server before it bound the port (found 2026-10-17: it had never
started that way). With no streams, uvicorn logs to DATA_DIR/logs/serve.log.
"""

from __future__ import annotations

import argparse
import sys

from api_fixtures import TOKEN, make_config, seed

from agent import cli


def test_serve_starts_with_no_console_streams(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    seed(config)
    monkeypatch.setenv("WEB_API_TOKEN", TOKEN)
    started = {}

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(kwargs))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    assert cli.cmd_serve(config, argparse.Namespace(host="127.0.0.1", port=8000, no_client=False, check=False)) == 0

    handler = started["log_config"]["handlers"]["file"]
    assert handler["filename"].endswith("serve.log")
    assert str(config.data_dir) in handler["filename"]


def test_serve_logs_to_the_console_when_there_is_one(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    seed(config)
    monkeypatch.setenv("WEB_API_TOKEN", TOKEN)
    started = {}

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(kwargs))
    assert cli.cmd_serve(config, argparse.Namespace(host="127.0.0.1", port=8000, no_client=False, check=False)) == 0
    assert "log_config" not in started
