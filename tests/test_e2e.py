"""End-to-end flows through the CLI only, with the fake orca and gh."""

import json
from pathlib import Path

import pytest
from conftest import git, load_fixture, worktree_paths

from swfactory.cli import main

DESIGN = """# {change} — Recurring bookings

## Context
Bookings are single events (`src/bookings.py`).

## Goal and scope
Weekly recurrence and a calendar view.

## Acceptance criteria
- AC-01: Given a weekly rule, when saved, then it is stored — verified by: test
- AC-02: Given the calendar, when opened, then occurrences show — verified by: manual step
- AC-03: Given the export, when run, then it succeeds — verified by: test

## Approach
Store a rule.

## Implementation notes
`src/bookings.py`.

## Tasks
```yaml
- id: {change}-01
  title: Recurrence model
  depends_on: []
  complexity: normal
  review: [quality, conformance]
  scope: ["src/**"]
  acceptance:
    AC-01: 'python -c "import sys; sys.exit(0)"'
    AC-02: manual
- id: {change}-02
  title: Export
  depends_on: []
  complexity: trivial
  review: []
  acceptance:
    AC-03: 'python -c "import sys; sys.exit(0)"'
```

## Risks and open questions
None.
"""


def run(capsys, *argv):
    code = main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    return code, out


def ok(capsys, *argv):
    code, out = run(capsys, *argv)
    assert code == 0, (argv, out)
    return out["data"]


@pytest.fixture
def initialized(repo, fake_orca, fake_gh, monkeypatch, capsys):
    """A repository after /factory-init, with its result committed by the person."""
    monkeypatch.chdir(repo.root)
    plan = ok(capsys, "init", "--plan")
    ok(capsys, "init", "--apply", plan["plan_id"])
    config = repo.root / ".factory/config.toml"
    config.write_text(config.read_text().replace("[checks]\n", "[checks]\ntest = 'python -c \"print(1)\"'\n", 1))
    git("add", "-A", cwd=repo.root)
    git("commit", "-q", "-m", "Install the Software Factory", cwd=repo.root)
    return repo


def approved_change(repo, capsys):
    change = ok(capsys, "design", "register")["change"]
    (repo.root / f".factory/changes/{change}/design.md").write_text(DESIGN.format(change=change))
    ok(capsys, "design", "register", change)
    ok(capsys, "design", "approve", change, "--message", f"apruebo {change}")
    return change


def workspace_of(repo, task):
    record = json.loads((repo.root / f".factory/state/{task}.json").read_text())
    return Path(record["workspace"]["orca_id"].split("::", 1)[1])


def record_review(repo, capsys, task, kind, tmp_path, run_id=None):
    head = git("rev-parse", "HEAD", cwd=workspace_of(repo, task))
    report = tmp_path / f"{kind}.json"
    data = {"type": kind, "commit": head, "verdict": "pass", "findings": [
        {"severity": "minor", "file": "src/bookings.py", "line": 1, "problem": "terse name", "fix": "rename"}]}
    if kind == "conformance" and run_id:
        from review_support import coverage
        record = json.loads((repo.root / f".factory/state/{task}.json").read_text())
        run = next(r for r in record["review_runs"] if r["id"] == run_id)
        data.update(coverage=coverage(run), deviations=[])
    report.write_text(json.dumps(data))
    if run_id:
        return ok(capsys, "task", "review-complete", task, "--review", run_id, "--file", str(report))
    return ok(capsys, "task", "review", task, "--record", str(report))


def test_full_flow_from_init_to_published_pr(initialized, fake_gh, capsys, tmp_path):
    repo = initialized
    assert (repo.root / ".claude/skills/factory-build/SKILL.md").exists()
    change = approved_change(repo, capsys)
    task = f"{change}-01"

    states = {t["id"]: t["state"] for t in ok(capsys, "task", "list")["tasks"]}
    assert states == {task: "ready", f"{change}-02": "ready"}

    ok(capsys, "task", "claim", task)
    ok(capsys, "task", "launch", task, "--agent", "codex", "--reviewer", "claude")
    workspace = workspace_of(repo, task)
    repo.commit("src/bookings.py", "RULE = 'weekly'\n", "Add recurrence rule", cwd=workspace)
    ok(capsys, "task", "checkpoint", task, "--done", "rule", "--next", "verify")
    ok(capsys, "task", "verify", task)
    (tmp_path / "calendar.txt").write_text("4 occurrences shown")
    ok(capsys, "task", "verify", task, "--manual", "AC-02", "--evidence", str(tmp_path / "calendar.txt"))
    assert ok(capsys, "task", "submit", task)["reviews"] == ["quality", "conformance"]
    quality = ok(capsys, "task", "review", task, "--type", "quality")
    record_review(repo, capsys, task, "quality", tmp_path, quality["review_id"])
    from review_support import plan

    from swfactory import tasks
    from swfactory.state import TaskStore
    plan(tasks.Context(repo.root, TaskStore(repo.root / ".factory/state")), task, tmp_path / "proof.json")
    conformance = ok(capsys, "task", "review", task, "--type", "conformance")
    assert conformance["launched"] is True
    record_review(repo, capsys, task, "conformance", tmp_path, conformance["review_id"])
    ok(capsys, "task", "finish", task, "--memory", "none")
    published = ok(capsys, "task", "publish", task)
    assert published["pull_request"]["number"] == 1
    assert "terse name" in fake_gh.prs()[0]["body"]

    status = ok(capsys, "status")
    row = next(t for t in status["tasks"] if t["id"] == task)
    assert (row["state"], row["pull_request"]["number"], row["acceptance"]) == ("to_merge", 1,
                                                                                {"verified": 2, "total": 2})
    fake_gh.set_pr(1, state="MERGED", mergedAt="2026-10-20T10:00:00Z")  # squash-merged on GitHub
    row = next(t for t in ok(capsys, "sync") and ok(capsys, "status")["tasks"] if t["id"] == task)
    assert row["state"] == "done"
    record = json.loads((repo.root / f".factory/state/{task}.json").read_text())
    assert record["merged_at"] == "2026-10-20T10:00:00Z"


def test_design_change_after_approval(initialized, capsys):
    repo = initialized
    change = approved_change(repo, capsys)
    task = f"{change}-01"
    ok(capsys, "task", "claim", task)
    design = repo.root / f".factory/changes/{change}/design.md"
    design.write_text(design.read_text().replace("Store a rule.", "Store a rule (RFC 5545)."))
    status = ok(capsys, "status")
    assert status["summary"]["awaiting_approval"] == [change]
    assert next(t for t in status["tasks"] if t["id"] == task)["reason"] == "design_changed"
    code, out = run(capsys, "task", "claim", f"{change}-02")
    assert out["error"]["code"] == "task_not_ready"
    ok(capsys, "design", "approve", change, "--message", f"apruebo {change}")
    assert next(t for t in ok(capsys, "task", "list")["tasks"] if t["id"] == task)["state"] == "in_progress"


def test_two_tasks_in_parallel(initialized, capsys):
    change = approved_change(initialized, capsys)
    for task in (f"{change}-01", f"{change}-02"):
        ok(capsys, "task", "claim", task)
        ok(capsys, "task", "launch", task, "--agent", "claude")
    status = ok(capsys, "status")
    assert status["summary"]["tasks"] == {"in_progress": 2}
    workspaces = {t["workspace"] for t in status["tasks"]}
    assert len(workspaces) == 2
    listed = worktree_paths(initialized.root)
    assert all(Path(ws.split("::", 1)[1]).resolve() in listed for ws in workspaces)


def test_interruption_and_recovery(initialized, fake_orca, capsys):
    change = approved_change(initialized, capsys)
    task = f"{change}-01"
    ok(capsys, "task", "claim", task)
    ok(capsys, "task", "launch", task, "--agent", "claude")
    workspace = workspace_of(initialized, task)
    (workspace / "half.py").write_text("work in progress\n")
    terminal = load_fixture("agent-launch")["result"]["outcome"]["handle"]
    fake_orca.respond_fixture("terminal show", "terminal-show-closed", target=terminal)
    status = ok(capsys, "status")
    assert next(t for t in status["tasks"] if t["id"] == task)["state"] == "interrupted"
    report = ok(capsys, "task", "recover", task)
    assert "?? half.py" in report["git_status"]
    ok(capsys, "task", "recover", task, "--confirm")
    ok(capsys, "task", "launch", task, "--agent", "codex")
    record = json.loads((initialized.root / f".factory/state/{task}.json").read_text())
    assert [(a["id"], a["agent"], a["result"]) for a in record["attempts"]] == [(1, "claude", "interrupted"),
                                                                             (2, "codex", None)]
    assert (workspace / "half.py").exists()


def test_next_step_after_done(initialized, capsys):
    change = approved_change(initialized, capsys)
    task = f"{change}-02"
    ok(capsys, "task", "claim", task)
    ok(capsys, "task", "launch", task, "--agent", "claude")
    initialized.commit("src/export.py", "x = 1\n", "Export", cwd=workspace_of(initialized, task))
    ok(capsys, "task", "verify", task)
    ok(capsys, "task", "finish", task, "--memory", "none")

    def next_step():
        return next(t for t in ok(capsys, "status")["tasks"] if t["id"] == task)["next_step"]

    assert next_step() == f"swfactory task publish {task}"
    ok(capsys, "task", "publish", task)  # the fake gh opens PR #1
    assert next_step().startswith("merge PR #1")
    branch = json.loads((initialized.root / f".factory/state/{task}.json").read_text())["workspace"]["branch"]
    git("merge", "-q", "--no-ff", "-m", "merge", branch, cwd=initialized.root)
    assert next_step().startswith("merged: remove its workspace?")


def test_next_step_after_a_push_without_pr(initialized, fake_gh, capsys):
    change = approved_change(initialized, capsys)
    task = f"{change}-02"
    ok(capsys, "task", "claim", task)
    ok(capsys, "task", "launch", task, "--agent", "claude")
    initialized.commit("src/export.py", "x = 1\n", "Export", cwd=workspace_of(initialized, task))
    ok(capsys, "task", "verify", task)
    ok(capsys, "task", "finish", task, "--memory", "none")
    fake_gh.fail()
    ok(capsys, "task", "publish", task)  # pushed; the PR is left to the person
    step = next(t for t in ok(capsys, "status")["tasks"] if t["id"] == task)["next_step"]
    assert "is pushed: open its PR or merge it into main" in step
