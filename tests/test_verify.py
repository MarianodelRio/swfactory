import json
from pathlib import Path

import pytest
import yaml
from test_approve import CHANGE, FOLDER
from test_claim_launch import TASK
from test_design_parse import VALID

from swfactory.cli import main
from swfactory.state import TaskStore

# CHG-0012-01 has AC-01 (a command) and AC-02 (manual).
DESIGN = VALID.replace("depends_on: [CHG-0012-01, CHG-0007-03]", "depends_on: [CHG-0012-01]").replace(
    '"pytest tests/bookings/test_recurrence.py::test_weekly"', '\'python -c "import sys; sys.exit(0)"\'')


@pytest.fixture
def claimed(repo, fake_orca, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    folder = repo.root / FOLDER
    folder.mkdir(parents=True)
    (folder / "design.md").write_text(DESIGN)
    assert main(["design", "approve", CHANGE, "--message", "apruebo CHG-0012"]) == 0
    assert main(["task", "claim", TASK]) == 0
    capsys.readouterr()
    return repo


@pytest.fixture
def store(claimed):
    return TaskStore(claimed.root / ".factory" / "state")


def configure(repo, checks):
    lines = ["schema_version = 1", "[checks]"] + [f"{k} = {json.dumps(v)}" for k, v in checks.items()]
    (repo.root / ".factory").mkdir(exist_ok=True)
    (repo.root / ".factory/config.toml").write_text("\n".join(lines) + "\n")


def set_ac_command(store, claimed, command):
    path = claimed.root / FOLDER / "tasks.yaml"
    data = yaml.safe_load(path.read_text())
    data["tasks"][0]["acceptance"]["AC-01"] = command
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def cli(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_verify_passes(claimed, store, capsys):
    configure(claimed, {"test": 'python -c "print(42)"'})
    code, out = cli(capsys, "task", "verify", TASK)
    assert code == 0, out
    record = store.read_task(TASK)
    assert record["checks"]["status"] == "passed"
    assert record["checks"]["results"] == {"test": "passed"}
    assert record["acceptance"]["AC-01"]["status"] == "verified"
    assert record["acceptance"]["AC-02"]["status"] == "pending"  # manual, untouched
    evidence = claimed.root / ".factory/state" / record["acceptance"]["AC-01"]["evidence"]
    assert "exit code: 0" in evidence.read_text()
    check_evidence = (claimed.root / ".factory/state" / record["checks"]["evidence"]["test"]).read_text()
    assert "42" in check_evidence


def test_commands_run_in_the_task_workspace(claimed, store, capsys):
    configure(claimed, {"where": 'python -c "import os; print(os.getcwd())"'})
    cli(capsys, "task", "verify", TASK)
    record = store.read_task(TASK)
    workspace = record["workspace"]["orca_id"].split("::", 1)[1]
    text = (claimed.root / ".factory/state" / record["checks"]["evidence"]["where"]).read_text()
    assert workspace in text


def test_verify_fails(claimed, store, capsys):
    configure(claimed, {"lint": 'python -c "import sys; sys.exit(3)"'})
    set_ac_command(store, claimed, 'python -c "import sys; sys.exit(1)"')
    code, out = cli(capsys, "task", "verify", TASK)
    assert code == 0
    record = store.read_task(TASK)
    assert record["checks"]["status"] == "failed"
    assert record["acceptance"]["AC-01"]["status"] == "failed"
    assert "exit code: 3" in (claimed.root / ".factory/state" / record["checks"]["evidence"]["lint"]).read_text()


def test_command_that_cannot_run_is_not_run(claimed, store, capsys):
    configure(claimed, {"typecheck": "no-such-tool-xyz src"})
    set_ac_command(store, claimed, "'unterminated")
    code, out = cli(capsys, "task", "verify", TASK)
    assert code == 0, out
    record = store.read_task(TASK)
    assert record["checks"]["status"] == "not_run"
    assert record["checks"]["results"]["typecheck"] == "not_run"
    assert record["acceptance"]["AC-01"]["status"] == "not_run"


def test_no_checks_configured(claimed, store, capsys):
    cli(capsys, "task", "verify", TASK)
    assert store.read_task(TASK)["checks"]["status"] == "not_run"


def test_manual_evidence(claimed, store, capsys, tmp_path):
    shot = tmp_path / "calendar.png"
    shot.write_bytes(b"\x89PNG fake")
    code, out = cli(capsys, "task", "verify", TASK, "--manual", "AC-02", "--evidence", str(shot))
    assert code == 0, out
    ac = store.read_task(TASK)["acceptance"]["AC-02"]
    assert ac["status"] == "verified"
    assert ac["evidence"].endswith(".png")
    assert (claimed.root / ".factory/state" / ac["evidence"]).read_bytes() == b"\x89PNG fake"


def test_manual_evidence_errors(claimed, capsys, tmp_path):
    code, out = cli(capsys, "task", "verify", TASK, "--manual", "AC-02", "--evidence", str(tmp_path / "x"))
    assert (code, out["error"]["code"]) == (3, "evidence_not_found")
    (tmp_path / "note.txt").write_text("ok")
    code, out = cli(capsys, "task", "verify", TASK, "--manual", "AC-01", "--evidence", str(tmp_path / "note.txt"))
    assert (code, out["error"]["code"]) == (3, "ac_not_manual")
    code, out = cli(capsys, "task", "verify", TASK, "--manual", "AC-09", "--evidence", str(tmp_path / "note.txt"))
    assert (code, out["error"]["code"]) == (3, "ac_not_in_task")


def test_verify_needs_a_started_task(claimed, capsys):
    code, out = cli(capsys, "task", "verify", "CHG-0012-02")
    assert (code, out["error"]["code"]) == (3, "task_not_started")


def test_evidence_lives_in_the_main_checkout(claimed, store, capsys, monkeypatch):
    configure(claimed, {"test": 'python -c "print(1)"'})
    workspace = Path(store.read_task(TASK)["workspace"]["orca_id"].split("::", 1)[1])
    monkeypatch.chdir(workspace)
    cli(capsys, "task", "verify", TASK)
    assert (claimed.root / ".factory/state/evidence" / TASK).is_dir()
    assert not (workspace / ".factory/state").exists()


def test_each_run_keeps_its_own_evidence(claimed, store, capsys):
    configure(claimed, {"test": 'python -c "print(1)"'})
    cli(capsys, "task", "verify", TASK)
    first = store.read_task(TASK)["checks"]["evidence"]["test"]
    configure(claimed, {"test": 'python -c "import sys; sys.exit(3)"'})
    cli(capsys, "task", "verify", TASK)
    second = store.read_task(TASK)["checks"]["evidence"]["test"]
    assert first != second
    assert "exit code: 0" in (claimed.root / ".factory/state" / first).read_text()
    assert "exit code: 3" in (claimed.root / ".factory/state" / second).read_text()


def test_verify_records_uncommitted_files(claimed, store, capsys):
    configure(claimed, {"test": 'python -c "print(1)"'})
    workspace = Path(store.read_task(TASK)["workspace"]["orca_id"].split("::", 1)[1])
    code, out = cli(capsys, "task", "verify", TASK)
    assert store.read_task(TASK)["checks"]["dirty"] is False
    assert out["warnings"] == []
    (workspace / "draft.py").write_text("x = 1\n")
    code, out = cli(capsys, "task", "verify", TASK)
    assert code == 0, out
    assert store.read_task(TASK)["checks"]["dirty"] is False  # untracked: not part of the branch, a warning
    assert any(w.startswith("untracked files") and "draft.py" in w for w in out["warnings"])
    (workspace / "README.md").write_text("changed\n")
    code, out = cli(capsys, "task", "verify", TASK)
    assert store.read_task(TASK)["checks"]["dirty"] is True
    assert any("uncommitted changes" in w for w in out["warnings"])


def test_check_runs_the_project_checks_here(repo, fake_orca, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    configure(repo, {"test": 'python -c "print(1)"', "lint": 'python -c "import sys; sys.exit(\'bad style\')"'})
    assert main(["check", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["passed"] is False
    assert (data["checks"]["test"]["status"], data["checks"]["lint"]["status"]) == ("passed", "failed")
    assert "bad style" in data["checks"]["lint"]["output"]
    assert not (repo.root / ".factory/state").exists()  # nothing recorded


def test_check_without_checks(repo, fake_orca, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    assert main(["check", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "checks_missing"
