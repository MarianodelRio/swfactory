import json
from pathlib import Path

import pytest
from test_approve import CHANGE, FOLDER
from test_claim_launch import TASK, orca_calls
from test_verify import DESIGN, configure

from swfactory.cli import main
from swfactory.state import TaskStore


def quality():
    from test_review import workspace_head
    return {"type": "quality", "commit": workspace_head(), "verdict": "pass", "findings": []}


@pytest.fixture
def ready_to_finish(repo, fake_orca, monkeypatch, capsys, tmp_path):
    """CHG-0012-01 implemented in its workspace, verified (AC-01 by command, AC-02 manual), submitted
    and with a passing quality review on its last commit."""
    monkeypatch.chdir(repo.root)
    folder = repo.root / FOLDER
    folder.mkdir(parents=True)
    (folder / "design.md").write_text(DESIGN)
    configure(repo, {"test": 'python -c "print(1)"'})
    assert main(["design", "approve", CHANGE, "--message", "apruebo CHG-0012"]) == 0
    assert main(["task", "claim", TASK]) == 0
    store = TaskStore(repo.root / ".factory" / "state")
    workspace = Path(store.read_task(TASK)["workspace"]["orca_id"].split("::", 1)[1])
    repo.commit("src/bookings/model.py", "RULE = 'weekly'\n", "Add recurrence model", cwd=workspace)
    assert main(["task", "verify", TASK]) == 0
    (tmp_path / "shot.txt").write_text("calendar shows 4 occurrences")
    assert main(["task", "verify", TASK, "--manual", "AC-02", "--evidence", str(tmp_path / "shot.txt")]) == 0
    assert main(["task", "submit", TASK]) == 0
    (tmp_path / "q.json").write_text(json.dumps(quality()))
    assert main(["task", "review", TASK, "--record", str(tmp_path / "q.json")]) == 0
    capsys.readouterr()
    return {"repo": repo, "store": store, "workspace": workspace}


def finish(capsys, memory="Added ADR 0003 on recurrence rules"):
    code = main(["task", "finish", TASK, "--memory", memory, "--json"])
    return code, json.loads(capsys.readouterr().out)


def failing_gates(capsys):
    code, out = finish(capsys)
    assert code == 3, out
    assert out["error"]["code"] == "gates_not_met"
    return out["error"]["details"]


def test_finish_happy_path(ready_to_finish, fake_orca, capsys):
    store = ready_to_finish["store"]
    code, out = finish(capsys)
    assert code == 0, out
    record = store.read_task(TASK)
    assert record["state"] == "to_merge"
    assert record["memory"] == "Added ADR 0003 on recurrence rules"
    assert record["scope_warnings"] == []
    assert store.read_claim(TASK) is None
    card = orca_calls(fake_orca, "worktree set")[-1]
    assert card[card.index("--workspace-status") + 1] == "in-review"
    assert card[card.index("--comment") + 1] == "2/2 AC verified · to merge"


def test_memory_none_is_accepted(ready_to_finish, capsys):
    code, _ = finish(capsys, memory="none")
    assert code == 0
    assert ready_to_finish["store"].read_task(TASK)["memory"] == "none"


def test_memory_is_mandatory(ready_to_finish, capsys):
    code = main(["task", "finish", TASK, "--json"])
    assert code == 2
    capsys.readouterr()
    code, out = finish(capsys, memory="  ")
    assert any("memory" in d for d in out["error"]["details"])


def test_gate_ac_not_verified(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    store.update_task(TASK, lambda s: {**s, "acceptance": {**s["acceptance"],
                                                          "AC-02": {"status": "pending", "evidence": None}}}, "x")
    assert any("AC-02" in d for d in failing_gates(capsys))


def test_gate_checks_failed(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    store.update_task(TASK, lambda s: {**s, "checks": {**s["checks"], "status": "failed"}}, "x")
    assert any("checks" in d for d in failing_gates(capsys))


def test_gate_checks_not_run(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    store.update_task(TASK, lambda s: {**s, "checks": None}, "x")
    assert any("checks" in d for d in failing_gates(capsys))


def test_gate_review_missing(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    store.update_task(TASK, lambda s: {**s, "reviews": []}, "x")
    assert any("quality" in d for d in failing_gates(capsys))


def test_gate_review_on_an_older_code_commit(ready_to_finish, capsys):
    repo, workspace = ready_to_finish["repo"], ready_to_finish["workspace"]
    repo.commit("src/bookings/model.py", "RULE = 'monthly'\n", "Change after review", cwd=workspace)
    assert any("quality" in d and "last commit" in d for d in failing_gates(capsys))


def test_docs_only_changes_after_review_still_hold(ready_to_finish, capsys):
    repo, workspace = ready_to_finish["repo"], ready_to_finish["workspace"]
    repo.commit("docs/decisions/0003-recurrence.md", "# 0003\n", "ADR", cwd=workspace)
    repo.commit(".factory/notes.md", "x\n", "factory file", cwd=workspace)
    code, out = finish(capsys)
    assert code == 0, out


def test_gate_review_not_passing(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    store.update_task(TASK, lambda s: {**s, "reviews": [{**s["reviews"][0], "verdict": "fail"}]}, "x")
    assert any("quality" in d for d in failing_gates(capsys))


def test_gate_approval_not_valid(ready_to_finish, capsys):
    path = ready_to_finish["repo"].root / FOLDER / "design.md"
    path.write_text(path.read_text() + "\nEdited.\n")
    code, out = finish(capsys)
    assert code == 3
    # the design change blocks the task first; either way it is not done
    assert ready_to_finish["store"].read_task(TASK)["state"] != "done"


def test_scope_warning_is_stored_but_does_not_block(ready_to_finish, capsys):
    repo, workspace = ready_to_finish["repo"], ready_to_finish["workspace"]
    repo.commit("src/payments/hack.py", "x = 1\n", "Out of scope", cwd=workspace)
    store = ready_to_finish["store"]
    assert main(["task", "verify", TASK]) == 0
    capsys.readouterr()
    (workspace.parent / "q2.json").write_text(json.dumps(quality()))
    assert main(["task", "review", TASK, "--record", str(workspace.parent / "q2.json")]) == 0
    capsys.readouterr()
    code, out = finish(capsys)
    assert code == 0, out
    assert store.read_task(TASK)["scope_warnings"] == ["src/payments/hack.py"]
    assert any("src/payments/hack.py" in w for w in out["warnings"])


def test_all_failing_gates_are_reported_together(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    store.update_task(TASK, lambda s: {**s, "reviews": [], "checks": None}, "x")
    assert len(failing_gates(capsys)) >= 2


def test_finish_records_the_branch_commits(ready_to_finish, capsys):
    from conftest import git
    finish(capsys)
    commits = ready_to_finish["store"].read_task(TASK)["commits"]
    assert commits[-1] == git("rev-parse", "HEAD", cwd=ready_to_finish["workspace"])


def test_gate_checks_on_an_older_code_commit(ready_to_finish, capsys, tmp_path):
    repo, workspace = ready_to_finish["repo"], ready_to_finish["workspace"]
    repo.commit("src/bookings/model.py", "RULE = 'daily'\n", "Change after verify", cwd=workspace)
    (tmp_path / "q3.json").write_text(json.dumps(quality()))
    assert main(["task", "review", TASK, "--record", str(tmp_path / "q3.json")]) == 0
    capsys.readouterr()
    assert any("checks passed on an older commit" in d for d in failing_gates(capsys))


def test_unread_pause_note_stops_finish(ready_to_finish, capsys):
    assert main(["task", "note", TASK, "--text", "wait", "--pause"]) == 0
    capsys.readouterr()
    code, out = finish(capsys)
    assert code == 0
    assert out["data"]["paused"] is True
    assert ready_to_finish["store"].read_task(TASK)["state"] == "blocked"


def test_gate_uncommitted_changes(ready_to_finish, capsys):
    workspace = ready_to_finish["workspace"]
    (workspace / "src/bookings/model.py").write_text("RULE = 'yearly'\n")
    details = failing_gates(capsys)
    assert any("uncommitted changes (src/bookings/model.py)" in d for d in details)


def test_untracked_files_are_a_warning(ready_to_finish, capsys):
    workspace = ready_to_finish["workspace"]
    (workspace / "shot one.png").write_text("evidence\n")
    (workspace / ".factory").mkdir(exist_ok=True)
    (workspace / ".factory/local note.md").write_text("x\n")
    code, out = finish(capsys)
    assert code == 0, out
    untracked = [w for w in out["warnings"] if w.startswith("untracked files")]
    assert len(untracked) == 1 and "shot one.png" in untracked[0] and ".factory" not in untracked[0]


def test_a_staged_rename_is_an_uncommitted_change(ready_to_finish, capsys):
    from conftest import git
    workspace = ready_to_finish["workspace"]
    git("mv", "src/bookings/model.py", "src/bookings/rules.py", cwd=workspace)
    assert any("src/bookings/rules.py" in d for d in failing_gates(capsys))


def test_agent_local_files_do_not_count_as_uncommitted(ready_to_finish, capsys):
    workspace = ready_to_finish["workspace"]
    (workspace / ".claude").mkdir(exist_ok=True)
    (workspace / ".claude/settings.local.json").write_text("{}\n")
    code, out = finish(capsys)
    assert code == 0, out


def test_gate_checks_run_on_a_dirty_workspace(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    store.update_task(TASK, lambda s: {**s, "checks": {**s["checks"], "dirty": True}}, "x")
    assert any("checks ran with uncommitted changes" in d for d in failing_gates(capsys))


def test_gate_missing_evidence(ready_to_finish, capsys):
    store = ready_to_finish["store"]
    record = store.read_task(TASK)
    (store.root / record["acceptance"]["AC-02"]["evidence"]).unlink()
    (store.root / record["checks"]["evidence"]["test"]).unlink()
    details = failing_gates(capsys)
    assert any("evidence of AC-02 is missing" in d for d in details)
    assert any("evidence of the test check is missing" in d for d in details)
