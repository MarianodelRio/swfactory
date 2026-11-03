import json
import sqlite3
from pathlib import Path

import pytest
from test_improve_extract import TASK, claude_project_dir, cost_state, seed, tool_use_assistant, write

from swfactory import improve, sessions, telemetry
from swfactory.output import FactoryError
from swfactory.state import TaskStore

FIXTURES = Path(__file__).parent / "fixtures/telemetry"


def setup(tmp_path):
    home = tmp_path / "home"
    store = TaskStore(tmp_path / "state")
    workspace = Path("/work/task")
    seed(store, TASK, workspace, agent="codex")
    for name in ("parent", "child"):
        write(home / f".codex/sessions/2026/10/28/rollout-{name}.jsonl",
              (FIXTURES / f"codex-{name}.jsonl").read_text())
    return home, store, workspace


def extract(home, store):
    return improve.extract([TASK], store, home)["tasks"][0]


def test_inherited_calls_deduplicate_and_external_child_is_included(tmp_path):
    home, store, _ = setup(tmp_path)
    task = extract(home, store)
    c = task["evidence"]["codex"]
    assert c["sessions"] == 2 and c["subagents"] == 1
    assert sum(c["raw_tool_calls"].values()) == 3
    assert sum(c["tool_calls"].values()) == 2 and c["duplicate_tool_records"] == 1
    assert c["command_outcomes"] == {"success": 0, "failure": 1, "unknown": 1, "pending": 0}
    assert next(s for s in c["sources"] if s["id"] == "child")["attribution"] == "descendant"


def test_cumulative_usage_is_not_summed_and_inherited_child_usage_is_separate(tmp_path):
    home, store, _ = setup(tmp_path)
    c = extract(home, store)["evidence"]["codex"]
    assert c["tokens"]["total_tokens"] == 210
    assert {s["session"]: s["tokens"]["total_tokens"] for s in c["usage_by_session"]} == {
        "parent": 210, "child": 42}


def test_partial_sqlite_falls_back_per_session_in_read_only_mode(tmp_path):
    home, store, _ = setup(tmp_path)
    path = home / ".codex/state_5.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE threads (id TEXT, tokens_used INTEGER)")
        db.execute("INSERT INTO threads VALUES ('parent', 300)")
    c = extract(home, store)["evidence"]["codex"]
    assert c["tokens"]["total_tokens"] == 300
    assert {s["session"]: s["source"] for s in c["usage_by_session"]} == {"parent": "sqlite", "child": "rollout"}
    assert not list(path.parent.glob("*-journal"))


def test_missing_sqlite_row_and_usage_are_visible(tmp_path):
    home, store, _ = setup(tmp_path)
    child = home / ".codex/sessions/2026/10/28/rollout-child.jsonl"
    lines = child.read_text().splitlines()
    child.write_text("\n".join(line for line in lines if '"token_count"' not in line))
    task = extract(home, store)
    assert any("no token fields found for Codex session child" in gap for gap in task["coverage_gaps"])
    assert task["evidence"]["codex"]["coverage"]["usage"]["known"] == 1


def test_unrelated_roots_with_same_call_id_are_distinct(tmp_path):
    home, store, _ = setup(tmp_path)
    parent = (FIXTURES / "codex-parent.jsonl").read_text().replace('"id":"parent"', '"id":"unrelated"')
    write(home / ".codex/sessions/2026/10/28/rollout-other.jsonl", parent)
    c = extract(home, store)["evidence"]["codex"]
    assert sum(c["tool_calls"].values()) == 3


def test_external_root_requires_explicit_registration_and_missing_registration_is_reported(tmp_path):
    home, store, _ = setup(tmp_path)
    external = (FIXTURES / "codex-parent.jsonl").read_text().replace('"id":"parent"', '"id":"validation"')
    external = external.replace('/work/task', '/tmp/validation')
    write(home / ".codex/sessions/2026/10/28/rollout-validation.jsonl", external)
    assert extract(home, store)["evidence"]["codex"]["sessions"] == 2
    sessions.attach(TASK, "codex", "validation", "validation", None, store)
    sessions.attach(TASK, "codex", "missing", "review", None, store)
    task = extract(home, store)
    assert task["evidence"]["codex"]["sessions"] == 3
    assert any("registered Codex session missing is missing" in gap for gap in task["coverage_gaps"])


def test_session_attachment_is_idempotent_and_rejects_conflicts_and_cycles(tmp_path):
    _, store, _ = setup(tmp_path)
    for _ in range(2):
        sessions.attach(TASK, "codex", "a", "lead", "b", store)
    assert len(store.read_task(TASK)["sessions"]) == 1
    with pytest.raises(FactoryError):
        sessions.attach(TASK, "codex", "a", "validation", None, store)
    with pytest.raises(FactoryError):
        sessions.attach(TASK, "codex", "b", "subagent", "a", store)


def test_lifecycle_counts_unblocks_separately_from_attempts(tmp_path):
    home, store, _ = setup(tmp_path)
    for _ in range(3):
        store.update_task(TASK, lambda s: s, "unblock")
    lifecycle = extract(home, store)["lifecycle"]
    assert lifecycle["recoveries"] == 0 and lifecycle["unblocks"] == 3
    assert lifecycle["review_requests"] == 0


@pytest.mark.parametrize("value,expected", [
    ({"exit_code": 0}, "success"),
    ({"output": json.dumps({"exit_code": 2, "output": "failure"})}, "failure"),
    ({"output": [{"type": "input_text", "text": "Process exited with code 1\nOutput:\n..."}]}, "failure"),
    ({"output": "Script completed\nOutput:\nExit code: 1"}, "unknown"),
    ({"output": [{"type": "input_text", "text": "Script completed\nOutput:\n"},
                 {"type": "input_text", "text": "Process exited with code 0"}]}, "unknown"),
    ({"session_id": 123, "exit_code": None}, "pending"),
    ({"output": "ordinary stdout"}, "unknown"),
])
def test_result_envelopes_do_not_infer_success_from_missing_codes(value, expected):
    assert telemetry.outcome(value) == expected


def test_multi_command_script_is_not_reported_as_one_complete_execution():
    assert telemetry.command({"input": 'await tools.exec_command({cmd: "a"}); '
                                       'await tools.exec_command({cmd: "b"});'}) is None
    assert telemetry.command({"input": 'await tools.exec_command({cmd: "pytest " + selected});'}) is None


def test_claude_streamed_message_usage_and_tools_are_deduplicated(tmp_path):
    home = tmp_path / "home"
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "workspace"
    seed(store, TASK, workspace)
    event = json.loads(tool_use_assistant("t1", "Read", usage={"input_tokens": 10, "output_tokens": 2}))
    event["message"]["id"] = "message-1"
    second = json.loads(json.dumps(event))
    second["message"]["usage"]["output_tokens"] = 4
    write(claude_project_dir(home, workspace) / "session.jsonl", "\n".join(map(json.dumps, [event, second])))
    c = extract(home, store)["evidence"]["claude"]
    assert c["raw_tool_calls"] == {"Read": 2} and c["tool_calls"] == {"Read": 1}
    assert c["tokens"]["input"] == 10 and c["tokens"]["output"] == 4


def test_claude_subagent_cost_is_not_added_to_parent_cost(tmp_path):
    home = tmp_path / "home"
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "workspace"
    seed(store, TASK, workspace)
    project = claude_project_dir(home, workspace)
    write(project / "session.jsonl", cost_state(1.0))
    write(project / "session/subagents/agent.jsonl", cost_state(0.2))
    c = extract(home, store)["evidence"]["claude"]
    assert c["tokens"]["cost_usd"] == 1.0
    assert c["tokens"]["subagent_cost_states"][0]["cost_usd"] == 0.2


def test_registered_claude_external_subagent_can_be_resolved(tmp_path):
    home = tmp_path / "home"
    store = TaskStore(tmp_path / "state")
    seed(store, TASK, tmp_path / "workspace")
    write(home / ".claude/projects/external/parent/subagents/agent-child.jsonl",
          tool_use_assistant("tool-1", "Read"))
    sessions.attach(TASK, "claude", "child", "subagent", "parent", store)
    result = extract(home, store)
    assert result["evidence"]["claude"]["subagents"] == 1
    assert result["coverage"] == "partial"  # a resolved child is evidence even without its parent file
    assert not any("registered Claude session child is missing" in gap for gap in result["coverage_gaps"])


def test_missing_event_history_is_unknown_instead_of_zero(tmp_path):
    home, store, _ = setup(tmp_path)
    (store.root / f"{TASK}.events.jsonl").unlink()
    result = extract(home, store)
    assert result["lifecycle"]["unblocks"] is None
    assert result["lifecycle"]["recovery_events"] is None
    assert any("event history is unavailable" in gap for gap in result["coverage_gaps"])
