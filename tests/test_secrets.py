"""No secret appears in the state, the cards, the PRs or the evidence."""

import json

import pytest
from test_claim_launch import TASK, orca_calls
from test_verify import claimed as claimed  # noqa: F401
from test_verify import cli, configure

from swfactory.output import is_secret_file, mask_secrets
from swfactory.state import TaskStore

TOKENS = [
    "ghp_" + "a" * 36,
    "github_pat_" + "B" * 30,
    "sk-ant-" + "c" * 30,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "xoxb-" + "1234567890-abc",
]
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"


@pytest.mark.parametrize("secret", TOKENS)
def test_tokens_are_masked(secret):
    assert secret not in mask_secrets(f"value {secret} here")


def test_private_key_and_assignments_are_masked():
    masked = mask_secrets(f"{KEY}\npassword=hunter2 api_key: abc123 token = t0k")
    assert "b3BlbnNzaC1rZXk" not in masked
    assert "hunter2" not in masked and "abc123" not in masked and "t0k" not in masked
    assert "password=[redacted]" in masked


def test_masking_keeps_json_valid():
    text = json.dumps({"a": "password=hunter2", "b": ["token: x", "ghp_" + "z" * 36]})
    assert json.loads(mask_secrets(text))["a"] == "password=[redacted]"


def test_normal_text_is_untouched():
    text = "Add recurrence model; tests in tests/test_recurrence.py pass (sk-learn is fine)"
    assert mask_secrets(text) == text


@pytest.mark.parametrize(("name", "secret"), [(".env", True), (".env.local", True), ("server.pem", True),
                                              ("id_rsa", True), ("deploy.key", True), ("shot.png", False),
                                              ("notes.txt", False), ("environment.md", False)])
def test_secret_files(name, secret):
    assert is_secret_file(f"/tmp/{name}") is secret


def test_state_never_stores_secrets(claimed, capsys):  # noqa: F811
    cli(capsys, "task", "note", TASK, "--text", f"use token {TOKENS[0]}")
    store_dir = claimed.root / ".factory" / "state"
    for path in store_dir.glob(f"{TASK}*"):
        assert TOKENS[0] not in path.read_text()
    assert "[redacted]" in TaskStore(store_dir).read_task(TASK)["notes"][0]["text"]


def test_evidence_is_masked(claimed, capsys):  # noqa: F811
    configure(claimed, {"leak": f"python -c \"print('{TOKENS[3]}')\""})
    _, out = cli(capsys, "task", "verify", TASK)
    evidence = (claimed.root / ".factory/state" / out["data"]["checks"]["evidence"]["leak"]).read_text()
    assert TOKENS[3] not in evidence


def test_secret_files_are_refused_as_evidence(claimed, capsys, tmp_path):  # noqa: F811
    (tmp_path / ".env").write_text("DB_PASSWORD=x\n")
    code, out = cli(capsys, "task", "verify", TASK, "--manual", "AC-02", "--evidence", str(tmp_path / ".env"))
    assert (code, out["error"]["code"]) == (3, "secret_file_refused")


def test_cards_are_masked(claimed, fake_orca, capsys):  # noqa: F811
    cli(capsys, "task", "block", TASK, "--reason", f"need the key {TOKENS[1]}")
    card = orca_calls(fake_orca, "worktree set")[-1]
    assert TOKENS[1] not in " ".join(card)
