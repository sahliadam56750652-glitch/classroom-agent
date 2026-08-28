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

# The bot token is a credential and comes from .env only. A chat id is not a
# secret -- it identifies a conversation, not an account -- so it lives in
# config.yaml beside everything else that describes this installation.
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

# Bounds on gate.questions. Repeated here rather than imported from gate/quiz.py
# because that module imports this one, and a personal tool does not need an
# import cycle to keep two integers in step. The test below them asserts they
# match.
MIN_QUESTIONS = 3
MAX_QUESTIONS = 5


class ConfigError(Exception):
    """config.yaml is absent, unreadable, or missing a required key."""


@dataclass(frozen=True)
class Config:
    account: str
    timezone: str
    data_dir: Path
    tracked_courses: list[str]
    ignored_courses: list[str]
    # Optional at load time and checked only when something actually wants to
    # send: `agent sync` must keep working on a machine with no bot configured.
    # telegram_settings() below is the single gate that demands both halves.
    telegram_chat_id: int | None = None

    # Where study packs are written. The default stays under data_dir like
    # everything else (invariant 5), but this is the one path deliberately
    # allowed to point outside it: packs reach NotebookLM by being written into
    # a Drive-synced folder, which is how they get there without this project
    # ever uploading anything. Already resolved to an absolute path at load.
    packs_dir_override: Path | None = None

    # The timetable file. Hand-edited configuration like config.yaml itself
    # rather than data, which is why it defaults to sitting beside it instead
    # of under data_dir. load_config() always fills this in; the fallback on
    # the property below only applies to a Config built by hand in a test, and
    # points inside data_dir so that no test can reach the real one.
    # See gate/timetable.py for why the timetable is a file and not a table.
    timetable_path_override: Path | None = None

    # How many pages one scheduled `agent run` may send for transcription.
    # The free tier allows about 20 a day and the scheduler fires twice, so the
    # default deliberately leaves room: a routine run must never swallow the
    # whole allowance and leave a manual `agent ocr` with nothing to spend.
    #
    # Lowered from 8 when the quiz arrived. The arithmetic, so that the next
    # person to raise it can see what they are spending: 6 pages x 2 runs = 12
    # of ~20 requests a day, leaving ~8 for quiz generation. A quiz costs one
    # request per lecture and then never again (the set is cached against the
    # text it came from), so ~8 covers an evening comfortably.
    ocr_run_limit: int = 6

    # The share of a quiz's countable questions that has to be right. 3 of 4 at
    # the default. A ratio rather than a count, so a lecture too thin for four
    # questions still has a meaningful pass mark, and so does one where I
    # flagged a bad question out of the denominator.
    gate_pass_ratio: float = 0.75

    # How many questions to ask for. The model may return fewer and say why --
    # fewer honest questions is a better answer than four invented ones.
    gate_questions: int = 4

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

    @property
    def packs_dir(self) -> Path:
        return self.packs_dir_override or self.library_dir / "packs"

    @property
    def timetable_path(self) -> Path:
        return self.timetable_path_override or self.data_dir / "timetable.yaml"


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


def _telegram_chat_id(raw: dict[str, Any], config_path: Path) -> int | None:
    """The chat to send to, or None when no telegram section is configured."""
    section = raw.get("telegram")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(
            f"{config_path}: 'telegram' must be a mapping with a 'chat_id', "
            f"got {type(section).__name__}."
        )

    value = section.get("chat_id")
    if value is None:
        return None

    # Group chat ids are negative and long, so this is parsed rather than
    # trusted: a quoted id in YAML reads as a string and would otherwise reach
    # the Bot API as the wrong type and come back as an opaque 400.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(
            f"{config_path}: 'telegram.chat_id' must be an integer, "
            f"got {type(value).__name__}."
        )
    try:
        return int(str(value).strip())
    except ValueError:
        raise ConfigError(
            f"{config_path}: 'telegram.chat_id' must be an integer, got {value!r}."
        ) from None


def telegram_settings(config: Config) -> tuple[str, int]:
    """(bot token, chat id), or a ConfigError naming exactly what is missing.

    The two halves come from deliberately different places -- the token from
    .env because it is a credential, the chat id from config.yaml because it is
    not -- so "notifications are not set up" has two separate causes and the
    error has to say which one applies.

    Reads the environment directly. load_config() has already pulled .env into
    it; a Config built by hand in a test has not.
    """
    token = (os.environ.get(BOT_TOKEN_ENV) or "").strip()

    missing = []
    if not token:
        missing.append(
            f"  {BOT_TOKEN_ENV} is not set. Put it in the .env file beside "
            f"config.yaml:\n"
            f"      {BOT_TOKEN_ENV}=123456:ABC...\n"
            f"    Get one from @BotFather. It is a secret and never goes in "
            f"config.yaml."
        )
    if config.telegram_chat_id is None:
        missing.append(
            "  telegram.chat_id is not set in config.yaml:\n"
            "      telegram:\n"
            "        chat_id: 123456789\n"
            "    Message the bot once, then read the id from\n"
            "    https://api.telegram.org/bot<token>/getUpdates"
        )

    if missing:
        raise ConfigError(
            "Telegram is not configured, so there is nowhere to send.\n"
            + "\n".join(missing)
        )

    assert config.telegram_chat_id is not None  # narrowed by the check above
    return token, config.telegram_chat_id


def _packs_dir(raw: dict[str, Any], config_path: Path) -> Path | None:
    """An explicit packs directory, resolved like every other configured path.

    Relative paths resolve against the directory holding config.yaml, so
    `packs_dir: ../OneDrive/Classroom` behaves the same wherever `agent` is run
    from. Returning None means "use the default under data_dir".
    """
    value = raw.get("packs_dir")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(
            f"{config_path}: 'packs_dir' must be a path, got {type(value).__name__}."
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


TIMETABLE_FILENAME = "timetable.yaml"


def _timetable_path(raw: dict[str, Any], config_path: Path) -> Path:
    """The timetable file, resolved like every other configured path.

    Relative paths resolve against the directory holding config.yaml -- the
    same rule as data_dir and packs_dir -- so the same config behaves
    identically whichever directory `agent` runs from. Absent means the
    default name beside config.yaml, resolved here rather than left None, so
    that the path a Config carries is always the one that will be opened.
    """
    value = raw.get("timetable_path")
    if value is None:
        return (config_path.parent / TIMETABLE_FILENAME).resolve()
    if not isinstance(value, str):
        raise ConfigError(
            f"{config_path}: 'timetable_path' must be a path, "
            f"got {type(value).__name__}."
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _ocr_run_limit(raw: dict[str, Any], config_path: Path) -> int:
    """Pages `agent run` may transcribe per run. 0 disables OCR in the pipeline."""
    section = raw.get("ocr")
    if section is None:
        return Config.ocr_run_limit
    if not isinstance(section, dict):
        raise ConfigError(
            f"{config_path}: 'ocr' must be a mapping with a 'run_limit', "
            f"got {type(section).__name__}."
        )
    value = section.get("run_limit")
    if value is None:
        return Config.ocr_run_limit
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"{config_path}: 'ocr.run_limit' must be a non-negative integer, "
            f"got {value!r}."
        )
    return value


def _gate_settings(raw: dict[str, Any], config_path: Path) -> tuple[float, int]:
    """(pass ratio, questions per quiz), validated rather than trusted.

    Both are checked here because both fail quietly if they are wrong. A
    pass_ratio of 75 instead of 0.75 makes every quiz unpassable and looks
    exactly like a run of bad luck; a questions count of 0 produces a quiz that
    passes with nothing answered, which is the worst possible failure in this
    particular file.
    """
    section = raw.get("gate")
    if section is None:
        return Config.gate_pass_ratio, Config.gate_questions
    if not isinstance(section, dict):
        raise ConfigError(
            f"{config_path}: 'gate' must be a mapping, got {type(section).__name__}."
        )

    ratio = section.get("pass_ratio")
    if ratio is None:
        ratio = Config.gate_pass_ratio
    elif isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise ConfigError(
            f"{config_path}: 'gate.pass_ratio' must be a number between 0 and 1, "
            f"got {ratio!r}."
        )
    elif not 0 < float(ratio) <= 1:
        raise ConfigError(
            f"{config_path}: 'gate.pass_ratio' must be between 0 and 1 -- "
            f"0.75 means three of four. Got {ratio!r}."
        )

    count = section.get("questions")
    if count is None:
        count = Config.gate_questions
    elif isinstance(count, bool) or not isinstance(count, int):
        raise ConfigError(
            f"{config_path}: 'gate.questions' must be a whole number, got {count!r}."
        )
    elif not MIN_QUESTIONS <= count <= MAX_QUESTIONS:
        raise ConfigError(
            f"{config_path}: 'gate.questions' must be between {MIN_QUESTIONS} and "
            f"{MAX_QUESTIONS}, got {count}. Fewer is not worth the request; more "
            f"does not fit a phone screen one question at a time."
        )

    return float(ratio), int(count)


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

    pass_ratio, questions = _gate_settings(raw, config_path)

    data_dir = _resolve_data_dir(raw.get("data_dir"), config_path)
    for directory in (data_dir, data_dir / "library", data_dir / "logs"):
        directory.mkdir(parents=True, exist_ok=True)

    return Config(
        account=account,
        timezone=timezone,
        data_dir=data_dir,
        tracked_courses=_course_ids(courses.get("tracked"), "courses.tracked", config_path),
        ignored_courses=_course_ids(courses.get("ignored"), "courses.ignored", config_path),
        telegram_chat_id=_telegram_chat_id(raw, config_path),
        packs_dir_override=_packs_dir(raw, config_path),
        timetable_path_override=_timetable_path(raw, config_path),
        ocr_run_limit=_ocr_run_limit(raw, config_path),
        gate_pass_ratio=pass_ratio,
        gate_questions=questions,
    )
