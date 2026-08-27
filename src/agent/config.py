"""Configuration loading and path resolution.

Every path the rest of the codebase touches comes from here. Nothing else builds
a path by hand -- that is what turns invariant 5 (everything under DATA_DIR) into
something checkable rather than aspirational, and what makes the Phase 3 move to
a cloud VM a copy instead of a rewrite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# src/agent/config.py -> src/agent -> src -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_DATA_DIR = "./data"


class ConfigError(Exception):
    """config.yaml is absent, unreadable, or missing a required key."""


@dataclass(frozen=True)
class Config:
    account: str
    timezone: str
    data_dir: Path
    tracked_courses: list[str]
    ignored_courses: list[str]

    # Derived paths. Everything lives under data_dir so that relocating the
    # project is a directory copy.

    @property
    def db_path(self) -> Path:
        return self.data_dir / "academic.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def _require(data: dict[str, Any], key: str, config_path: Path) -> Any:
    """Fetch a required key, or raise an error that names it."""
    if data.get(key) is None:
        raise ConfigError(
            f"{config_path} is missing the required key '{key}'.\n"
            f"Copy config.example.yaml to config.yaml and fill it in."
        )
    return data[key]


def _course_ids(value: Any, key: str, config_path: Path) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"{config_path}: '{key}' must be a list of course IDs, "
            f"got {type(value).__name__}."
        )
    # Classroom course IDs are strings, but YAML reads a bare 7712... as an int.
    # Coerce so comparisons against API responses never silently fail.
    return [str(item) for item in value]


def _resolve_data_dir(configured: Any, config_path: Path) -> Path:
    """DATA_DIR from the environment wins, then config.yaml, then ./data.

    Relative paths resolve against the directory holding config.yaml, so the
    same config behaves identically no matter which directory `agent` runs from.
    """
    raw = os.environ.get("DATA_DIR") or configured or DEFAULT_DATA_DIR
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def load_config(config_path: Path | None = None) -> Config:
    """Read config.yaml and return a Config, creating the data directories."""
    config_path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH

    if not config_path.is_file():
        raise ConfigError(
            f"No config file at {config_path}.\n"
            f"Copy config.example.yaml to config.yaml and fill it in."
        )

    # .env sits beside the config file. Load it before reading DATA_DIR so that
    # setting it there behaves exactly like exporting it in the shell.
    load_dotenv(config_path.parent / ".env")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigError(f"{config_path} is not valid YAML: {err}") from err
    except OSError as err:
        raise ConfigError(f"{config_path} could not be read: {err}") from err

    if raw is None:
        raise ConfigError(
            f"{config_path} is empty.\n"
            f"Copy config.example.yaml to config.yaml and fill it in."
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level.")

    account = str(_require(raw, "account", config_path))
    timezone = str(_require(raw, "timezone", config_path))

    courses = _require(raw, "courses", config_path)
    if not isinstance(courses, dict):
        raise ConfigError(
            f"{config_path}: 'courses' must be a mapping with 'tracked' and "
            f"'ignored' lists, got {type(courses).__name__}."
        )

    data_dir = _resolve_data_dir(raw.get("data_dir"), config_path)
    for directory in (data_dir, data_dir / "library", data_dir / "logs"):
        directory.mkdir(parents=True, exist_ok=True)

    return Config(
        account=account,
        timezone=timezone,
        data_dir=data_dir,
        tracked_courses=_course_ids(courses.get("tracked"), "courses.tracked", config_path),
        ignored_courses=_course_ids(courses.get("ignored"), "courses.ignored", config_path),
    )
