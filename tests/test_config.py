import pytest

from swfactory.config import load_config
from swfactory.output import EXIT_PRECONDITION, FactoryError

DEFAULTS = {
    "schema_version": 1,
    "orca": {"min_version": "1.4.229"},
    "checks": {},
    "review": {"quality": "auto", "conformance": "auto", "max_rounds": 2},
    "git": {"pr_on_done": "offer", "integrate": "pr", "cleanup": "ask"},
    "language": {"artifacts": "en"},
    "board": {"port": 8765, "idle_minutes": 30},
    "agents": {"lead": "", "lead_model": "", "lead_effort": "", "reviewer": "", "reviewer_model": "",
               "reviewer_effort": ""},
}


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def invalid(path):
    with pytest.raises(FactoryError) as info:
        load_config(path)
    assert info.value.code == "config_invalid"
    assert info.value.exit_code == EXIT_PRECONDITION
    return info.value.message


def test_missing_file_returns_defaults(tmp_path):
    assert load_config(tmp_path / "config.toml") == DEFAULTS


def test_empty_file_returns_defaults(tmp_path):
    assert load_config(write(tmp_path, "")) == DEFAULTS


def test_partial_section_is_merged_with_defaults(tmp_path):
    cfg = load_config(write(tmp_path, '[review]\nquality = "always"\n'))
    assert cfg["review"] == {"quality": "always", "conformance": "auto", "max_rounds": 2}
    assert cfg["git"] == {"pr_on_done": "offer", "integrate": "pr", "cleanup": "ask"}


def test_full_valid_config(tmp_path):
    text = """
schema_version = 1

[orca]
min_version = "1.5.0"

[checks]
test = "pytest"
lint = "ruff check ."
typecheck = "mypy src"

[review]
quality = "never"
conformance = "always"
max_rounds = 3

[git]
pr_on_done = "never"

[language]
artifacts = "es"
"""
    cfg = load_config(write(tmp_path, text))
    assert cfg == {
        "schema_version": 1,
        "orca": {"min_version": "1.5.0"},
        "checks": {"test": "pytest", "lint": "ruff check .", "typecheck": "mypy src"},
        "review": {"quality": "never", "conformance": "always", "max_rounds": 3},
        "git": {"pr_on_done": "never", "integrate": "pr", "cleanup": "ask"},
        "language": {"artifacts": "es"},
        "board": {"port": 8765, "idle_minutes": 30},
        "agents": {"lead": "", "lead_model": "", "lead_effort": "", "reviewer": "", "reviewer_model": "",
                   "reviewer_effort": ""},
    }


@pytest.mark.parametrize("value", ["auto", "always", "never"])
def test_review_values_accepted(tmp_path, value):
    cfg = load_config(write(tmp_path, f'[review]\nquality = "{value}"\nconformance = "{value}"\n'))
    assert cfg["review"]["quality"] == value
    assert cfg["review"]["conformance"] == value


@pytest.mark.parametrize("value", ["offer", "always", "never"])
def test_pr_on_done_values_accepted(tmp_path, value):
    assert load_config(write(tmp_path, f'[git]\npr_on_done = "{value}"\n'))["git"]["pr_on_done"] == value


def test_unknown_sections_and_keys_are_kept(tmp_path):
    text = '[templates]\n"a.md" = "abc123"\n\n[review]\nextra = 5\n'
    cfg = load_config(write(tmp_path, text))
    assert cfg["templates"] == {"a.md": "abc123"}
    assert cfg["review"]["extra"] == 5


@pytest.mark.parametrize("key", ["quality", "conformance"])
def test_invalid_review_value(tmp_path, key):
    message = invalid(write(tmp_path, f'[review]\n{key} = "sometimes"\n'))
    assert f"review.{key}" in message
    assert "sometimes" in message
    assert "auto, always, never" in message


def test_invalid_pr_on_done(tmp_path):
    message = invalid(write(tmp_path, '[git]\npr_on_done = "maybe"\n'))
    assert "git.pr_on_done" in message
    assert "maybe" in message
    assert "offer, always, never" in message


@pytest.mark.parametrize("value", ["0", "-1", '"2"', "1.5", "true"])
@pytest.mark.parametrize("key", ["max_rounds", "max_failed_rounds"])
def test_invalid_review_round_limit(tmp_path, value, key):
    message = invalid(write(tmp_path, f"[review]\n{key} = {value}\n"))
    assert f"review.{key}" in message


def test_invalid_check_command(tmp_path):
    message = invalid(write(tmp_path, "[checks]\ntest = 1\n"))
    assert "checks.test" in message


def test_invalid_min_version(tmp_path):
    message = invalid(write(tmp_path, "[orca]\nmin_version = 1.4\n"))
    assert "orca.min_version" in message


@pytest.mark.parametrize("value", ["2", "0", '"1"'])
def test_unknown_schema_version(tmp_path, value):
    message = invalid(write(tmp_path, f"schema_version = {value}\n"))
    assert "schema_version" in message


def test_known_section_not_a_table(tmp_path):
    message = invalid(write(tmp_path, 'review = "always"\n'))
    assert "review" in message


def test_invalid_toml(tmp_path):
    path = write(tmp_path, "[review\n")
    message = invalid(path)
    assert str(path) in message


@pytest.mark.parametrize("text", ["[board]\nport = 0\n", "[board]\nport = \"8765\"\n", "[board]\nidle_minutes = 0\n"])
def test_board_values_are_validated(tmp_path, text):
    assert "board." in invalid(write(tmp_path, text))


@pytest.mark.parametrize(("key", "value"), [("integrate", "squash"), ("cleanup", "sometimes")])
def test_invalid_integration_and_cleanup(tmp_path, key, value):
    assert f"git.{key}" in invalid(write(tmp_path, f'[git]\n{key} = "{value}"\n'))


def test_default_agents(tmp_path):
    cfg = load_config(write(tmp_path, '[agents]\nlead = "codex"\nlead_model = "sol 6.1"\n'))
    assert (cfg["agents"]["lead"], cfg["agents"]["lead_model"], cfg["agents"]["reviewer"]) == ("codex", "sol 6.1", "")
    assert "agents.reviewer" in invalid(write(tmp_path, '[agents]\nreviewer = "gemini"\n'))
    assert "agents.lead_model" in invalid(write(tmp_path, '[agents]\nlead_model = 5\n'))
