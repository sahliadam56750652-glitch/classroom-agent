"""Config loading: path resolution, directory creation, and missing-key errors."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent.config import Config, ConfigError, load_config

COMPLETE = """\
account: someone@example.com
timezone: Africa/Tunis
courses:
  tracked: []
  ignored: []
"""


def write_config(tmp_path: Path, body: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_ambient_data_dir(monkeypatch):
    """DATA_DIR in the developer's own environment must not leak into tests."""
    monkeypatch.delenv("DATA_DIR", raising=False)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

def test_missing_file_names_the_path(tmp_path):
    missing = tmp_path / "config.yaml"
    with pytest.raises(ConfigError) as err:
        load_config(missing)
    assert str(missing) in str(err.value)
    assert "config.example.yaml" in str(err.value)


def test_empty_file_is_an_error(tmp_path):
    path = write_config(tmp_path, "")
    with pytest.raises(ConfigError, match="empty"):
        load_config(path)


def test_non_mapping_is_an_error(tmp_path):
    path = write_config(tmp_path, "- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


def test_invalid_yaml_is_an_error(tmp_path):
    path = write_config(tmp_path, "account: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


@pytest.mark.parametrize("key", ["account", "timezone", "courses"])
def test_missing_key_names_that_key(tmp_path, key):
    lines = [line for line in COMPLETE.splitlines() if not line.startswith(key)]
    if key == "courses":
        lines = [line for line in lines if not line.startswith("  ")]
    path = write_config(tmp_path, "\n".join(lines) + "\n")

    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert f"'{key}'" in str(err.value)


def test_null_key_counts_as_missing(tmp_path):
    path = write_config(tmp_path, "account:\ntimezone: Africa/Tunis\ncourses:\n  tracked: []\n")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert "'account'" in str(err.value)


def test_courses_must_be_a_mapping(tmp_path):
    path = write_config(tmp_path, "account: a@b.c\ntimezone: UTC\ncourses: []\n")
    with pytest.raises(ConfigError, match="'courses' must be a mapping"):
        load_config(path)


def test_tracked_must_be_a_list(tmp_path):
    path = write_config(
        tmp_path, "account: a@b.c\ntimezone: UTC\ncourses:\n  tracked: 1234\n"
    )
    with pytest.raises(ConfigError, match="courses.tracked"):
        load_config(path)


# --------------------------------------------------------------------------
# data_dir resolution
# --------------------------------------------------------------------------

def test_data_dir_defaults_to_data_beside_the_config(tmp_path):
    path = write_config(tmp_path, COMPLETE)
    config = load_config(path)
    assert config.data_dir == (tmp_path / "data").resolve()


def test_data_dir_from_config_is_used(tmp_path):
    path = write_config(tmp_path, COMPLETE + "data_dir: ./elsewhere\n")
    config = load_config(path)
    assert config.data_dir == (tmp_path / "elsewhere").resolve()


def test_env_data_dir_wins_over_config(tmp_path, monkeypatch):
    override = tmp_path / "from-env"
    monkeypatch.setenv("DATA_DIR", str(override))
    path = write_config(tmp_path, COMPLETE + "data_dir: ./from-config\n")

    config = load_config(path)

    assert config.data_dir == override.resolve()
    assert not (tmp_path / "from-config").exists()


def test_relative_paths_resolve_against_the_config_directory(tmp_path, monkeypatch):
    """Running from a different cwd must not move the data directory."""
    nested = tmp_path / "project"
    nested.mkdir()
    path = write_config(nested, COMPLETE + "data_dir: ./data\n")

    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(path)

    assert config.data_dir == (nested / "data").resolve()


def test_absolute_data_dir_is_left_alone(tmp_path):
    absolute = tmp_path / "somewhere" / "deep"
    path = write_config(tmp_path, COMPLETE + f"data_dir: {absolute.as_posix()}\n")
    config = load_config(path)
    assert config.data_dir == absolute.resolve()


def test_directories_are_created(tmp_path):
    path = write_config(tmp_path, COMPLETE)
    config = load_config(path)

    assert config.data_dir.is_dir()
    assert config.library_dir.is_dir()
    assert config.log_dir.is_dir()


def test_loading_twice_is_fine(tmp_path):
    """mkdir must not fail on an existing tree."""
    path = write_config(tmp_path, COMPLETE)
    load_config(path)
    load_config(path)


# --------------------------------------------------------------------------
# derived paths and course lists
# --------------------------------------------------------------------------

def test_derived_paths_all_sit_under_data_dir(tmp_path):
    path = write_config(tmp_path, COMPLETE)
    config = load_config(path)

    derived = [
        config.db_path,
        config.token_path,
        config.credentials_path,
        config.library_dir,
        config.log_dir,
    ]
    for candidate in derived:
        assert candidate.is_relative_to(config.data_dir), candidate

    assert config.db_path.name == "academic.db"
    assert config.token_path.name == "token.json"
    assert config.credentials_path.name == "credentials.json"


def test_course_ids_are_coerced_to_strings(tmp_path):
    """YAML reads a bare 771234 as an int; Classroom IDs are strings."""
    path = write_config(
        tmp_path,
        """\
        account: a@b.c
        timezone: UTC
        courses:
          tracked: [771234, "889900"]
          ignored: [111]
        """,
    )
    config = load_config(path)

    assert config.tracked_courses == ["771234", "889900"]
    assert config.ignored_courses == ["111"]


def test_absent_course_lists_default_to_empty(tmp_path):
    path = write_config(tmp_path, "account: a@b.c\ntimezone: UTC\ncourses:\n  tracked: []\n")
    config = load_config(path)
    assert config.ignored_courses == []


def test_config_is_frozen(tmp_path):
    path = write_config(tmp_path, COMPLETE)
    config = load_config(path)
    with pytest.raises(Exception):
        config.account = "someone-else@example.com"  # type: ignore[misc]


def test_dotenv_beside_the_config_sets_data_dir(tmp_path, monkeypatch):
    """A DATA_DIR in .env behaves like one exported in the shell."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    target = tmp_path / "from-dotenv"
    (tmp_path / ".env").write_text(f"DATA_DIR={target.as_posix()}\n", encoding="utf-8")
    path = write_config(tmp_path, COMPLETE)

    config = load_config(path)

    assert config.data_dir == target.resolve()


def test_config_type_is_exported():
    assert Config.__dataclass_fields__.keys() == {
        "account",
        "timezone",
        "data_dir",
        "tracked_courses",
        "ignored_courses",
        "telegram_chat_id",
        "packs_dir_override",
        "timetable_path_override",
        "ocr_run_limit",
        "quiz_pass_threshold",
        "quiz_question_count",
    }


# --------------------------------------------------------------------------
# telegram
# --------------------------------------------------------------------------

WITH_TELEGRAM = COMPLETE + """telegram:
  chat_id: 123456789
"""


def test_chat_id_is_read_from_config(tmp_path):
    config = load_config(write_config(tmp_path, WITH_TELEGRAM))
    assert config.telegram_chat_id == 123456789


def test_a_quoted_chat_id_is_coerced_to_an_int(tmp_path):
    """YAML reads a quoted id as a string; the Bot API needs a number."""
    body = COMPLETE + 'telegram:\n  chat_id: "123456789"\n'
    assert load_config(write_config(tmp_path, body)).telegram_chat_id == 123456789


def test_a_negative_group_chat_id_is_accepted(tmp_path):
    body = COMPLETE + "telegram:\n  chat_id: -1001234567890\n"
    assert load_config(write_config(tmp_path, body)).telegram_chat_id == -1001234567890


def test_no_telegram_section_is_not_an_error(tmp_path):
    """`agent sync` must work on a machine with no bot configured."""
    assert load_config(write_config(tmp_path, COMPLETE)).telegram_chat_id is None


def test_a_non_numeric_chat_id_is_rejected(tmp_path):
    body = COMPLETE + "telegram:\n  chat_id: not-a-number\n"
    with pytest.raises(ConfigError) as err:
        load_config(write_config(tmp_path, body))
    assert "telegram.chat_id" in str(err.value)


def test_a_telegram_section_that_is_not_a_mapping_is_rejected(tmp_path):
    body = COMPLETE + "telegram: 12345\n"
    with pytest.raises(ConfigError) as err:
        load_config(write_config(tmp_path, body))
    assert "'telegram'" in str(err.value)


def test_missing_token_names_the_env_var(tmp_path, monkeypatch):
    from agent.config import telegram_settings

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    config = load_config(write_config(tmp_path, WITH_TELEGRAM))

    with pytest.raises(ConfigError) as err:
        telegram_settings(config)

    assert "TELEGRAM_BOT_TOKEN" in str(err.value)
    assert "telegram.chat_id" not in str(err.value)


def test_missing_chat_id_names_the_config_key(tmp_path, monkeypatch):
    from agent.config import telegram_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    config = load_config(write_config(tmp_path, COMPLETE))

    with pytest.raises(ConfigError) as err:
        telegram_settings(config)

    assert "telegram.chat_id" in str(err.value)
    assert "TELEGRAM_BOT_TOKEN" not in str(err.value)


def test_both_missing_names_both(tmp_path, monkeypatch):
    from agent.config import telegram_settings

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    config = load_config(write_config(tmp_path, COMPLETE))

    with pytest.raises(ConfigError) as err:
        telegram_settings(config)

    assert "TELEGRAM_BOT_TOKEN" in str(err.value)
    assert "telegram.chat_id" in str(err.value)


def test_both_present_returns_the_pair(tmp_path, monkeypatch):
    from agent.config import telegram_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  123:ABC  ")
    config = load_config(write_config(tmp_path, WITH_TELEGRAM))

    assert telegram_settings(config) == ("123:ABC", 123456789)


def test_a_blank_token_counts_as_missing(tmp_path, monkeypatch):
    from agent.config import telegram_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    config = load_config(write_config(tmp_path, WITH_TELEGRAM))

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        telegram_settings(config)


def test_the_token_never_comes_from_config_yaml(tmp_path, monkeypatch):
    """A token in config.yaml is ignored: secrets come from .env only."""
    from agent.config import telegram_settings

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    body = COMPLETE + 'telegram:\n  chat_id: 1\n  bot_token: "leaked:token"\n'
    config = load_config(write_config(tmp_path, body))

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        telegram_settings(config)


# --------------------------------------------------------------------------
# the quiz section
# --------------------------------------------------------------------------

def test_the_quiz_defaults_need_no_config(tmp_path):
    config = load_config(write_config(tmp_path, COMPLETE))
    assert config.quiz_pass_threshold == 0.75
    assert config.quiz_question_count == 6


def test_the_quiz_section_is_read(tmp_path):
    path = write_config(tmp_path, COMPLETE + """
quiz:
  pass_threshold: 0.5
  question_count: 3
""")
    config = load_config(path)
    assert config.quiz_pass_threshold == 0.5
    assert config.quiz_question_count == 3


def test_either_key_can_be_set_on_its_own(tmp_path):
    path = write_config(tmp_path, COMPLETE + "\nquiz:\n  question_count: 8\n")
    config = load_config(path)
    assert config.quiz_question_count == 8
    assert config.quiz_pass_threshold == 0.75


def test_a_threshold_written_as_a_percentage_is_refused(tmp_path):
    """75 instead of 0.75 makes every quiz unpassable and looks like bad luck."""
    path = write_config(tmp_path, COMPLETE + "\nquiz:\n  pass_threshold: 75\n")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert "fraction, not a percentage" in str(err.value)


def test_a_threshold_of_zero_is_refused(tmp_path):
    """It would pass a quiz with every answer wrong."""
    path = write_config(tmp_path, COMPLETE + "\nquiz:\n  pass_threshold: 0\n")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("count", [0, 1, 2, 11, 40])
def test_an_out_of_range_question_count_is_refused(tmp_path, count):
    path = write_config(tmp_path, COMPLETE + f"\nquiz:\n  question_count: {count}\n")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert "quiz.question_count" in str(err.value)


@pytest.mark.parametrize("count", [3, 6, 10])
def test_the_ends_of_the_range_are_accepted(tmp_path, count):
    path = write_config(tmp_path, COMPLETE + f"\nquiz:\n  question_count: {count}\n")
    assert load_config(path).quiz_question_count == count


def test_a_non_mapping_quiz_section_is_refused(tmp_path):
    path = write_config(tmp_path, COMPLETE + "\nquiz: 0.75\n")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert "must be a mapping" in str(err.value)


def test_the_question_bounds_match_the_quiz_module():
    """config.py repeats them to avoid an import cycle; they must not drift."""
    from agent.config import MAX_QUESTIONS, MIN_QUESTIONS
    from agent.gate import quiz

    assert (MIN_QUESTIONS, MAX_QUESTIONS) == (quiz.MIN_QUESTIONS, quiz.MAX_QUESTIONS)
    assert Config.quiz_question_count == quiz.DEFAULT_QUESTIONS
    assert Config.quiz_pass_threshold == quiz.DEFAULT_PASS_RATIO


def test_the_example_config_is_what_the_defaults_say(tmp_path):
    """config.example.yaml documents six and 0.75; a drift between the file and
    the code would be a comment that lies."""
    import yaml

    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    assert raw["quiz"]["question_count"] == Config.quiz_question_count
    assert raw["quiz"]["pass_threshold"] == Config.quiz_pass_threshold
    assert raw["ocr"]["run_limit"] == Config.ocr_run_limit


def test_the_ocr_run_limit_leaves_room_for_the_quiz():
    """12 pages a day of a ~20 request allowance, so the gate can still ask."""
    assert Config.ocr_run_limit * 2 <= 12
