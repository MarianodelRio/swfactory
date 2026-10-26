import json
from pathlib import Path

import pytest
from test_claim_launch import TASK, orca_calls
from test_claim_launch import project as project  # noqa: F401
from test_verify import configure

from swfactory import tasks
from swfactory.cli import main
from swfactory.state import TaskStore


@pytest.fixture
def working(project, capsys):  # noqa: F811
    assert main(["task", "claim", TASK]) == 0
    assert main(["task", "launch", TASK, "--agent", "codex", "--reviewer", "claude"]) == 0
    capsys.readouterr()
    return project


@pytest.fixture
def store(working):
    return TaskStore(working.root / ".factory" / "state")


def cli(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def review_file(tmp_path, kind="quality", findings=(), name="r.json", commit=None, verdict="pass"):
    path = tmp_path / name
    report = {"type": kind, "commit": commit or workspace_head(), "verdict": verdict, "findings": list(findings)}
    path.write_text(json.dumps(report))
    return str(path)


def workspace_head():
    """HEAD of the task's workspace, as a reviewer reports it (`git rev-parse HEAD`)."""
    from pathlib import Path

    from conftest import git

    from swfactory.state import TaskStore
    record = TaskStore(Path.cwd() / ".factory" / "state").read_task("CHG-0012-01")
    return git("rev-parse", "HEAD", cwd=record["workspace"]["orca_id"].split("::", 1)[1])


MAJOR = {"severity": "major", "file": "src/x.py", "line": 4, "problem": "no error handling", "fix": "raise"}
MINOR = {"severity": "minor", "file": "src/x.py", "line": 9, "problem": "long name", "fix": "rename"}


@pytest.mark.parametrize(("field", "quality", "conformance", "expected"), [
    (["quality"], "auto", "auto", ["quality"]),
    ([], "auto", "auto", []),
    (["quality", "conformance"], "auto", "auto", ["quality", "conformance"]),
    ([], "always", "auto", ["quality"]),
    (["quality"], "never", "always", ["conformance"]),
    (["quality", "conformance"], "never", "never", []),
])
def test_effective_review_types(field, quality, conformance, expected):
    cfg = {"review": {"quality": quality, "conformance": conformance, "max_rounds": 2}}
    assert tasks.effective_reviews({"review": field}, cfg) == expected


def test_submit_moves_to_in_review_and_reads_notes(working, store, fake_orca, capsys):
    cli(capsys, "task", "note", TASK, "--text", "remember the changelog")
    code, out = cli(capsys, "task", "submit", TASK)
    assert code == 0, out
    assert store.read_task(TASK)["state"] == "in_review"
    assert [n["text"] for n in out["data"]["notes"]] == ["remember the changelog"]
    assert out["data"]["reviews"] == ["quality"]
    card = orca_calls(fake_orca, "worktree set")[-1]
    assert card[card.index("--workspace-status") + 1] == "in-review"


def test_pause_before_submit_blocks_instead(working, store, capsys):
    cli(capsys, "task", "note", TASK, "--text", "hold on", "--pause")
    _, out = cli(capsys, "task", "submit", TASK)
    assert out["data"]["paused"] is True
    assert store.read_task(TASK)["state"] == "blocked"


def test_conformance_review_launches_an_orca_agent(working, store, fake_orca, capsys):
    cli(capsys, "task", "submit", TASK)
    code, out = cli(capsys, "task", "review", TASK, "--type", "conformance")
    assert code == 0, out
    launch = orca_calls(fake_orca, "agent launch")[-1]
    assert launch[launch.index("--agent") + 1] == "claude"  # the attempt's --reviewer
    assert launch[launch.index("--worktree") + 1] == f"id:{store.read_task(TASK)['workspace']['orca_id']}"
    prompt = launch[launch.index("--prompt") + 1]
    assert "You verify that the implementation matches the approved design" in prompt
    assert TASK in prompt
    assert "git diff main...HEAD" in prompt
    assert out["data"]["launched"] is True


def test_quality_review_is_a_subagent_unless_an_agent_is_given(working, fake_orca, capsys):
    cli(capsys, "task", "submit", TASK)
    _, out = cli(capsys, "task", "review", TASK, "--type", "quality")
    assert out["data"]["launched"] is False
    assert "subagent" in out["data"]["message"]
    assert len(orca_calls(fake_orca, "agent launch")) == 1  # only the lead
    _, out = cli(capsys, "task", "review", TASK, "--type", "quality", "--agent", "codex")
    assert out["data"]["launched"] is True


def test_review_needs_in_review(working, capsys):
    code, out = cli(capsys, "task", "review", TASK, "--type", "conformance")
    assert (code, out["error"]["code"]) == (3, "invalid_transition")


def test_record_pass_with_minor_findings(working, store, tmp_path, capsys):
    cli(capsys, "task", "submit", TASK)
    code, out = cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path, findings=[MINOR]))
    assert code == 0, out
    review = store.read_task(TASK)["reviews"][0]
    assert review["type"] == "quality"
    assert review["verdict"] == "pass"
    assert review["round"] == 1
    assert review["findings"] == [MINOR]
    assert review["reviewer"] == "codex-subagent"
    workspace = store.read_task(TASK)["workspace"]["orca_id"].split("::", 1)[1]
    from conftest import git
    assert review["commit"] == git("rev-parse", "HEAD", cwd=workspace)


def test_a_pass_with_major_findings_is_recorded_as_fail(working, store, tmp_path, capsys):
    cli(capsys, "task", "submit", TASK)
    _, out = cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path, "conformance", [MAJOR]),
                 "--agent", "claude")
    review = store.read_task(TASK)["reviews"][0]
    assert review["verdict"] == "fail"
    assert review["reviewer"] == "claude"
    assert any("said pass" in w for w in out["warnings"])
    assert store.read_task(TASK)["state"] == "in_review"  # first round: the lead fixes it


def test_a_fail_verdict_without_findings_is_kept(working, store, tmp_path, capsys):
    cli(capsys, "task", "submit", TASK)
    code, out = cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path, verdict="fail"))
    assert code == 0, out
    assert store.read_task(TASK)["reviews"][0]["verdict"] == "fail"
    assert any("said fail without blocker or major findings" in w for w in out["warnings"])


def test_second_round_without_pass_blocks_and_keeps_findings(working, store, tmp_path, fake_orca, capsys):
    cli(capsys, "task", "submit", TASK)
    cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path, findings=[MAJOR], name="1.json"))
    code, out = cli(capsys, "task", "review", TASK, "--record",
                    review_file(tmp_path, findings=[MAJOR], name="2.json"))
    assert code == 0
    record = store.read_task(TASK)
    assert (record["state"], record["blocked_reason"]) == ("blocked", "review_not_converging")
    assert [r["round"] for r in record["reviews"]] == [1, 2]
    assert record["reviews"][1]["findings"] == [MAJOR]
    assert "--unread" in orca_calls(fake_orca, "worktree set")[-1]


def test_max_rounds_from_config(working, store, tmp_path, capsys):
    configure(working, {})
    path = working.root / ".factory/config.toml"
    path.write_text(path.read_text() + "[review]\nmax_rounds = 1\n")
    cli(capsys, "task", "submit", TASK)
    cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path, findings=[MAJOR]))
    assert store.read_task(TASK)["state"] == "blocked"


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps({"verdict": "pass", "findings": []}),
    json.dumps({"type": "quality", "verdict": "pass", "findings": []}),
    json.dumps({"type": "quality", "commit": "not-a-sha", "verdict": "pass", "findings": []}),
    json.dumps({"type": "style", "verdict": "pass", "findings": []}),
    json.dumps({"type": "quality", "verdict": "pass", "findings": [{"severity": "huge", "problem": "x"}]}),
    json.dumps({"type": "quality", "verdict": "pass", "findings": [{"severity": "minor"}]}),
    json.dumps({"type": "quality", "verdict": "pass", "findings": "none"}),
    "MISSING_VERDICT",
    "BAD_VERDICT",
])
def test_invalid_review_json(working, tmp_path, capsys, content):
    cli(capsys, "task", "submit", TASK)
    if content in ("MISSING_VERDICT", "BAD_VERDICT"):
        report = {"type": "quality", "commit": workspace_head(), "findings": []}
        if content == "BAD_VERDICT":
            report["verdict"] = "ok"
        content = json.dumps(report)
    (tmp_path / "bad.json").write_text(content)
    code, out = cli(capsys, "task", "review", TASK, "--record", str(tmp_path / "bad.json"))
    assert (code, out["error"]["code"]) == (3, "review_invalid")


def test_card_in_review_lists_pending_reviews(working, fake_orca, tmp_path, capsys):
    cli(capsys, "task", "submit", TASK)
    card = orca_calls(fake_orca, "worktree set")[-1]
    assert card[card.index("--comment") + 1] == "pending reviews: quality"
    cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path))
    card = orca_calls(fake_orca, "worktree set")[-1]
    assert card[card.index("--comment") + 1] == "reviews passed: finish"


def test_review_commit_must_be_on_the_branch(working, tmp_path, capsys):
    cli(capsys, "task", "submit", TASK)
    code, out = cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path, commit="0" * 40))
    assert (code, out["error"]["code"]) == (3, "review_invalid")


def test_review_stores_the_reviewed_commit_not_head(working, store, tmp_path, capsys):
    cli(capsys, "task", "submit", TASK)
    reviewed = workspace_head()
    workspace = Path(store.read_task(TASK)["workspace"]["orca_id"].split("::", 1)[1])
    working.commit("src/later.py", "x = 1\n", "after the review", cwd=workspace)
    cli(capsys, "task", "review", TASK, "--record", review_file(tmp_path, commit=reviewed))
    assert store.read_task(TASK)["reviews"][0]["commit"] == reviewed
