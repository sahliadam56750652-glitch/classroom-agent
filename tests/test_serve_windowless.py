"""The long-running commands run under pythonw.exe, where there is no console.

At logon `agent serve` and `agent bot` are started windowless. There
sys.stdout and sys.stderr are None: an unconditional `sys.stdout.flush()` killed
the server before it bound the port, and a bot that died left its traceback
nowhere (both found 2026-10-17). main() now points both streams at
DATA_DIR/logs/<command>.log.
"""

from __future__ import annotations

import sys

from api_fixtures import TOKEN, make_config, seed

from agent import cli


def _no_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)


def test_serve_starts_with_no_console_and_logs_to_a_file(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    seed(config)
    monkeypatch.setenv("WEB_API_TOKEN", TOKEN)
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)
    started = {}

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(kwargs))
    _no_console(monkeypatch)

    assert cli.main(["serve"]) == 0
    assert started["port"] == 8000
    sys.stdout.flush()
    log = (config.data_dir / "logs" / "serve.log").read_text(encoding="utf-8")
    assert "serving on" in log


def test_a_crash_with_no_console_leaves_its_traceback_in_the_log(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    seed(config)
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)

    def explode(config, args):
        print("about to fail", file=sys.stderr)
        raise RuntimeError("the network went away")

    monkeypatch.setitem(cli.COMMANDS, "bot", explode)
    _no_console(monkeypatch)
    try:
        cli.main(["bot"])
    except RuntimeError:
        pass
    sys.stderr.flush()
    log = (config.data_dir / "logs" / "bot.log").read_text(encoding="utf-8")
    assert "about to fail" in log


def test_a_console_run_is_unchanged(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    seed(config)
    monkeypatch.setenv("WEB_API_TOKEN", TOKEN)
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    assert cli.main(["serve"]) == 0
    assert "serving on" in capsys.readouterr().out
    assert not (config.data_dir / "logs" / "serve.log").exists()
