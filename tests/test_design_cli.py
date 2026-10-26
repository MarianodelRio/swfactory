import json
from pathlib import Path

import pytest
import yaml
from test_design_parse import VALID

from swfactory.cli import main
from swfactory.state import TaskStore

CHANGE = "CHG-0012"


@pytest.fixture
def project(repo, monkeypatch):
    monkeypatch.chdir(repo.root)
    return repo


def cli(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def write_design(root, change=CHANGE, text=VALID):
    folder = root / ".factory" / "changes" / change
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "design.md").write_text(text.replace("CHG-0012", change))
    return folder


def known_dependency(root):
    # CHG-0012-02 depends on CHG-0007-03, an approved task of another change.
    folder = root / ".factory" / "changes" / "CHG-0007"
    folder.mkdir(parents=True)
    tasks = {"change": "CHG-0007", "tasks": [{"id": "CHG-0007-03"}]}
    (folder / "tasks.yaml").write_text(yaml.safe_dump(tasks))


def test_register_without_id_reserves_the_next_one(project, capsys):
    code, out = cli(capsys, "design", "register")
    assert code == 0
    assert out["data"]["change"] == "CHG-1"
    assert (project.root / ".factory/changes/CHG-1").is_dir()
    _, out = cli(capsys, "design", "register", "--slug", "weekly-recurrence")
    assert (out["data"]["change"], out["data"]["folder"]) == ("CHG-2", "CHG-2-weekly-recurrence")
    assert Path(out["data"]["design"]).as_posix().endswith(".factory/changes/CHG-2-weekly-recurrence/design.md")
    code, out = cli(capsys, "design", "register", "--slug", "Not A Slug")
    assert (code, out["error"]["code"]) == (3, "slug_invalid")


def test_register_validates_a_design(project, capsys):
    known_dependency(project.root)
    write_design(project.root)
    code, out = cli(capsys, "design", "register", CHANGE)
    assert code == 0
    data = out["data"]
    assert data["change"] == CHANGE
    assert data["digest"].startswith("sha256:")
    assert data["acceptance_criteria"] == ["AC-01", "AC-02", "AC-03"]
    assert [t["id"] for t in data["tasks"]] == ["CHG-0012-01", "CHG-0012-02"]


def test_register_warns_about_a_criterion_that_is_not_a_command(project, capsys):
    known_dependency(project.root)
    write_design(project.root, text=VALID.replace(
        '"pytest tests/integration/test_calendar.py"', '"Retries show in the sync log"'))
    code, out = cli(capsys, "design", "register", CHANGE)
    assert code == 0, out
    assert any("AC-03 runs 'Retries', which is not found here" in w for w in out["warnings"])


def test_register_invalid_design_lists_problems(project, capsys):
    write_design(project.root)  # CHG-0007-03 is unknown here
    code, out = cli(capsys, "design", "register", CHANGE)
    assert code == 3
    assert out["error"]["code"] == "design_invalid"
    assert any("CHG-0007-03" in d for d in out["error"]["details"])


def test_register_unknown_change(project, capsys):
    code, out = cli(capsys, "design", "register", "CHG-0099")
    assert code == 3
    assert out["error"]["code"] == "change_not_found"


def test_register_bad_id_is_a_usage_error(project, capsys):
    code, out = cli(capsys, "design", "register", "change-1")
    assert code == 2
    assert out["error"]["code"] == "usage_error"


def test_register_rejected_change(project, capsys):
    folder = write_design(project.root)
    (folder / "rejection.json").write_text("{}")
    code, out = cli(capsys, "design", "register", CHANGE)
    assert code == 3
    assert out["error"]["code"] == "change_rejected"


def approve(folder, digest):
    (folder / "approval.json").write_text(json.dumps({"change": folder.name, "design_digest": digest}))


def test_show_awaiting_approval(project, capsys):
    known_dependency(project.root)
    write_design(project.root)
    code, out = cli(capsys, "design", "show", CHANGE)
    assert code == 0
    data = out["data"]
    assert data["state"] == "awaiting_approval"
    assert data["title"] == "Recurring bookings"
    assert data["approval"] is None
    assert [t["id"] for t in data["tasks"]] == ["CHG-0012-01", "CHG-0012-02"]
    assert data["problems"] == []


def test_show_approved_then_edited(project, capsys):
    known_dependency(project.root)
    folder = write_design(project.root)
    _, out = cli(capsys, "design", "show", CHANGE)
    approve(folder, out["data"]["digest"])
    _, out = cli(capsys, "design", "show", CHANGE)
    assert out["data"]["state"] == "approved"
    assert out["data"]["approval"]["design_digest"] == out["data"]["digest"]
    (folder / "design.md").write_text((folder / "design.md").read_text() + "\nA typo fix.\n")
    _, out = cli(capsys, "design", "show", CHANGE)
    assert out["data"]["state"] == "awaiting_approval"


def test_show_rejected(project, capsys):
    folder = write_design(project.root)
    (folder / "rejection.json").write_text(json.dumps({"change": CHANGE, "reason": "no"}))
    _, out = cli(capsys, "design", "show", CHANGE)
    assert out["data"]["state"] == "rejected"
    assert out["data"]["rejection"]["reason"] == "no"


def test_show_done_when_every_task_is_done(project, capsys):
    known_dependency(project.root)
    folder = write_design(project.root)
    _, out = cli(capsys, "design", "show", CHANGE)
    approve(folder, out["data"]["digest"])
    store = TaskStore(project.root / ".factory" / "state")
    store.update_task("CHG-0012-01", lambda s: {**s, "state": "done"}, "finish")
    _, out = cli(capsys, "design", "show", CHANGE)
    assert out["data"]["state"] == "approved"
    store.update_task("CHG-0012-02", lambda s: {**s, "state": "done"}, "finish")
    _, out = cli(capsys, "design", "show", CHANGE)
    assert out["data"]["state"] == "done"
    assert [t["state"] for t in out["data"]["tasks"]] == ["done", "done"]


def test_show_invalid_design_still_shows(project, capsys):
    write_design(project.root, text=VALID.replace("complexity: normal", "complexity: huge"))
    code, out = cli(capsys, "design", "show", CHANGE)
    assert code == 0
    assert out["data"]["tasks"] == []
    assert any("huge" in p for p in out["data"]["problems"])


def test_show_from_a_worktree_reads_shared_state(project, capsys, monkeypatch):
    known_dependency(project.root)
    write_design(project.root)
    project.commit(".factory/changes/CHG-0012/design.md",
                   (project.root / ".factory/changes/CHG-0012/design.md").read_text(), "design")
    project.commit(".factory/changes/CHG-0007/tasks.yaml",
                   (project.root / ".factory/changes/CHG-0007/tasks.yaml").read_text(), "dep")
    worktree = project.add_worktree("w1")
    TaskStore(project.root / ".factory" / "state").update_task(
        "CHG-0012-01", lambda s: {**s, "state": "in_progress"}, "claim")
    monkeypatch.chdir(worktree)
    _, out = cli(capsys, "design", "show", CHANGE)
    assert out["data"]["tasks"][0]["state"] == "in_progress"
