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
    }
