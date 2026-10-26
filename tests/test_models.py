"""Choosing models: names as the person says them, Orca's catalog, and the config's defaults."""

import json

import pytest
from conftest import set_home
from test_claim_launch import TASK, orca_calls
from test_claim_launch import project as project  # noqa: F401

from swfactory import models
from swfactory.cli import main
from swfactory.state import TaskStore

CATALOG = {"version": 3, "entries": [
    {"agent": "claude", "discovered": {"models": [
        {"id": "opus", "label": "Opus 5.5", "isDefault": True, "efforts": [{"value": "low"}, {"value": "high"}]},
        {"id": "claude-opus-5", "label": "Opus 5", "efforts": [{"value": "high"}]},
        {"id": "sonnet", "label": "Sonnet 5.5", "efforts": []},
    ]}},
    {"agent": "codex", "discovered": {"models": [{"id": "sol-6.1", "label": "Sol 6.1", "efforts": []}]}},
]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    (path / ".config/orca").mkdir(parents=True)
    (path / ".config/orca/agent-model-catalog.json").write_text(json.dumps(CATALOG))
    set_home(monkeypatch, path)
    return path


def cli(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(("agent", "said", "model_id"), [
    ("claude", "opus 5.5", "opus"), ("claude", "Opus 5", "claude-opus-5"), ("claude", "opus", "opus"),
    ("claude", "sonnet-5.5", "sonnet"), ("codex", "sol 6.1", "sol-6.1"),
])
def test_names_as_said_become_ids(home, agent, said, model_id):
    assert models.resolve(agent, said, None, home) == (model_id, [])


def test_unknown_model_and_effort_are_refused_with_the_list(home):
    with pytest.raises(models.FactoryError) as exc:
        models.resolve("claude", "opus 9", None, home)
    assert exc.value.code == "model_unknown"
    assert "opus (Opus 5.5)" in exc.value.details
    with pytest.raises(models.FactoryError) as exc:
        models.resolve("claude", "opus 5.5", "max", home)
    assert (exc.value.code, exc.value.details) == ("effort_unknown", ["low", "high"])


def test_without_a_catalog_the_model_is_passed_as_given(tmp_path):
    model, warnings = models.resolve("codex", "sol 6.1", None, tmp_path)
    assert model == "sol-6.1"
    assert "cannot be read" in warnings[0]


@pytest.mark.parametrize(("said", "model_id"), [
    ("opus 5.5", "claude-opus-5-5"), ("opus-5.5", "claude-opus-5-5"), ("claude-opus-5.5", "claude-opus-5-5"),
    ("Claude Opus 5.5", "claude-opus-5-5"), ("claude-opus-5-5", "claude-opus-5-5"), ("Sonnet 5.5", "claude-sonnet-5-5"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5-20250929"), ("opus-4-6[1m]", "claude-opus-4-6[1m]"),
    ("claude-nova-7", "claude-nova-7"), ("Opus 4.6 [1m]", "claude-opus-4-6[1m]"),
])
def test_without_a_catalog_a_claude_model_follows_claudes_ids(tmp_path, said, model_id):
    model, warnings = models.resolve("claude", said, None, tmp_path)
    assert model == model_id
    assert "cannot be read" in warnings[0]


@pytest.mark.parametrize(("said", "model_id"), [
    ("Opus", "opus"), ("Sonnet [1m]", "sonnet[1m]"), ("OpusPlan", "opusplan"), ("Best", "best"),
])
def test_without_a_catalog_claude_aliases_are_lowercased(tmp_path, said, model_id):
    assert models.resolve("claude", said, None, tmp_path)[0] == model_id


@pytest.mark.parametrize(("said", "model_id"), [
    ("GPT 6.1 Sol", "gpt-6.1-sol"), ("gpt6.1-sol", "gpt-6.1-sol"), ("gpt-6-1-sol", "gpt-6.1-sol"),
    ("GPT-5.3-Codex-Spark", "gpt-5.3-codex-spark"), ("gpt 6 luna", "gpt-6-luna"), ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gpt-5-5", "gpt-5.5"), ("GPT 5", "gpt-5"),
])
def test_without_a_catalog_a_codex_gpt_model_follows_openais_ids(tmp_path, said, model_id):
    model, warnings = models.resolve("codex", said, None, tmp_path)
    assert model == model_id
    assert "cannot be read" in warnings[0]


@pytest.mark.parametrize(("said", "given"), [("o3", "o3"), ("gpt-oss:20b", "gpt-oss:20b"), ("My Model", "My-Model")])
def test_without_a_catalog_other_codex_names_are_passed_as_given(tmp_path, said, given):
    model, warnings = models.resolve("codex", said, None, tmp_path)
    assert model == given
    assert "as given" in warnings[0]


@pytest.mark.parametrize(("said", "given"), [
    ("opus", "opus"), ("sonnet[1m]", "sonnet[1m]"), ("opusplan", "opusplan"),
    ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20241022"),
    ("us.anthropic.claude-opus-4-1-20250805-v1:0", "us.anthropic.claude-opus-4-1-20250805-v1:0"),
])
def test_without_a_catalog_other_claude_names_are_passed_as_given(tmp_path, said, given):
    model, warnings = models.resolve("claude", said, None, tmp_path)
    assert model == given
    assert "as given" in warnings[0]


def test_a_catalog_without_the_agent_also_completes_claude_ids(tmp_path):
    path = tmp_path / ".config/orca"
    path.mkdir(parents=True)
    (path / "agent-model-catalog.json").write_text(json.dumps({"entries": [CATALOG["entries"][1]]}))
    model, warnings = models.resolve("claude", "opus 5.5", None, tmp_path)
    assert model == "claude-opus-5-5"
    assert "does not list claude" in warnings[0]


def test_models_command(home, capsys):
    code, out = cli(capsys, "models", "--agent", "codex")
    assert code == 0
    assert out["data"]["agents"] == {"codex": [{"id": "sol-6.1", "label": "Sol 6.1", "default": False,
                                                "efforts": []}]}


@pytest.fixture
def claimed(project, home, capsys):  # noqa: F811
    assert main(["task", "claim", TASK]) == 0
    capsys.readouterr()
    return project


def test_lead_and_reviewer_models_are_recorded_and_used(claimed, fake_orca, capsys):
    code, out = cli(capsys, "task", "launch", TASK, "--agent", "claude", "--model", "opus 5.5", "--effort", "high",
                    "--reviewer", "codex", "--reviewer-model", "sol 6.1")
    assert code == 0, out
    launch = orca_calls(fake_orca, "agent launch")[-1]
    assert launch[launch.index("--model") + 1] == "opus"
    assert launch[launch.index("--effort") + 1] == "high"
    attempt = TaskStore(claimed.root / ".factory/state").read_task(TASK)["attempts"][-1]
    assert (attempt["model"], attempt["reviewer"], attempt["reviewer_model"]) == ("opus", "codex", "sol-6.1")
    TaskStore(claimed.root / ".factory/state").update_task(TASK, lambda s: {**s, "state": "in_review"}, "x")
    code, out = cli(capsys, "task", "review", TASK, "--type", "conformance")
    assert code == 0, out
    review = orca_calls(fake_orca, "agent launch")[-1]
    assert (review[review.index("--agent") + 1], review[review.index("--model") + 1]) == ("codex", "sol-6.1")


def test_config_defaults_apply_only_to_their_agent(claimed, fake_orca, capsys):
    config = claimed.root / ".factory/config.toml"
    config.write_text('[agents]\nlead = "claude"\nlead_model = "sonnet 5.5"\nreviewer = "claude"\n')
    code, out = cli(capsys, "task", "launch", TASK)
    assert code == 0, out
    assert (out["data"]["agent"], out["data"]["model"], out["data"]["reviewer"]) == ("claude", "sonnet", "claude")


def test_another_agent_does_not_take_the_configured_model(claimed, fake_orca, capsys):
    (claimed.root / ".factory/config.toml").write_text('[agents]\nlead = "claude"\nlead_model = "sonnet 5.5"\n')
    code, out = cli(capsys, "task", "launch", TASK, "--agent", "codex")
    assert code == 0, out
    assert out["data"]["model"] is None


def test_no_agent_anywhere(claimed, capsys):
    code, out = cli(capsys, "task", "launch", TASK)
    assert (code, out["error"]["code"]) == (2, "agent_missing")
