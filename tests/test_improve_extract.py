import json

import pytest
from conftest import set_home

from swfactory import improve
from swfactory.cli import main
from swfactory.state import TaskStore, empty_state

TASK = "CHG-0020-01"


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def seed(store, task_id, workspace, *, agent="claude", attempts=1, reviews=()):
    def build(state):
        state = {**empty_state(task_id), "state": "to_merge",
                 "workspace": {"orca_id": f"r::{workspace}", "branch": f"{task_id.lower()}"}}
        state["attempts"] = [
            {"id": n, "agent": agent, "model": "m", "effort": None, "reviewer": None, "terminal": None,
             "reviewer_model": None, "reviewer_effort": None, "operation_id": None, "started_at": None,
             "ended_at": None, "result": None}
            for n in range(1, attempts + 1)
        ]
        state["reviews"] = list(reviews)
        return state

    store.update_task(task_id, build, "seed")


def claude_message(event_type, **extra):
    return json.dumps({"type": event_type, **extra})


def tool_use_assistant(tool_id, name, command=None, usage=None):
    block = {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": command} if command else {}}
    message = {"content": [block], "usage": usage or {}}
    return claude_message("assistant", message=message)


def tool_result_user(tool_id, is_error=False):
    message = {"content": [{"type": "tool_result", "tool_use_id": tool_id, "is_error": is_error}]}
    return claude_message("user", message=message)


def cost_state(cost_usd=0.01, model="claude-x"):
    return claude_message("cost-state", totalCostUSD=cost_usd,
                          modelUsage={model: {"inputTokens": 10, "outputTokens": 20, "costUSD": cost_usd}})


def claude_project_dir(home, workspace):
    from swfactory import activity

    return activity.claude_project_dir(home, workspace)


def test_missing_state_is_reported_not_dropped(tmp_path):
    store = TaskStore(tmp_path / "state")
    result = improve.extract(["CHG-9999-01"], store, tmp_path / "home")
    [task] = result["tasks"]
    assert task == {"task": "CHG-9999-01", "coverage": "missing",
                    "coverage_gaps": ["no task state recorded for this task"]}
    assert result["coverage_summary"] == {"requested": 1, "resolvable": 0, "missing": ["CHG-9999-01"]}


def test_state_without_any_session_is_missing_but_keeps_lifecycle(tmp_path):
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    seed(store, TASK, workspace)
    [task] = improve.extract([TASK], store, tmp_path / "home")["tasks"]
    assert task["coverage"] == "missing"
    assert "no resolvable Claude or Codex session" in task["coverage_gaps"][0]
    assert task["lifecycle"]["attempts"] == 1


def test_full_coverage_counts_tool_calls_tokens_and_failing_commands(tmp_path):
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    reviews = [{"type": "quality", "round": 1, "verdict": "fail", "commit": "a", "findings": []},
               {"type": "quality", "round": 2, "verdict": "pass", "commit": "b", "findings": []}]
    seed(store, TASK, workspace, reviews=reviews)
    project = claude_project_dir(tmp_path / "home", workspace)
    write(project / "session-1.jsonl", "\n".join([
        tool_use_assistant("t1", "Bash", command="pytest -k broken", usage={"input_tokens": 5, "output_tokens": 7}),
        tool_result_user("t1", is_error=True),
        tool_use_assistant("t2", "Bash", command="pytest -k broken"),
        tool_result_user("t2", is_error=True),
        tool_use_assistant("t3", "Read"),
        cost_state(),
    ]))

    [task] = improve.extract([TASK], store, tmp_path / "home")["tasks"]
    assert task["coverage"] == "full"
    assert task["coverage_gaps"] == []
    assert task["lifecycle"]["review_rounds_total"] == 2
    assert task["lifecycle"]["reviews"]["quality"]["verdicts"] == ["fail", "pass"]
    claude = task["evidence"]["claude"]
    assert claude["sessions"] == 1
    assert claude["tool_calls"] == {"Bash": 2, "Read": 1}
    [failing] = claude["repeated_failing_commands"]
    assert failing == {"command": "pytest -k broken", "count": 2, "failures": 2}
    assert claude["tokens"]["cost_usd"] == pytest.approx(0.01)


def test_missing_cost_state_falls_back_to_message_usage_and_reports_gap(tmp_path):
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    seed(store, TASK, workspace)
    project = claude_project_dir(tmp_path / "home", workspace)
    write(project / "session-1.jsonl", "\n".join([
        tool_use_assistant("t1", "Read", usage={"input_tokens": 3, "output_tokens": 4}),
        tool_result_user("t1"),
    ]))

    [task] = improve.extract([TASK], store, tmp_path / "home")["tasks"]
    assert task["coverage"] == "partial"
    assert "cost-state" in task["coverage_gaps"][0]
    tokens = task["evidence"]["claude"]["tokens"]
    assert tokens["estimated_from_usage"] is True
    assert tokens["input"] == 3
    assert tokens["output"] == 4


def test_cost_state_takes_the_last_running_total_not_a_sum(tmp_path):
    """Claude Code's cost-state entries are each a running total for the whole session so far,
    not a per-turn delta — summing every line would inflate cost/tokens by ~the number of
    cost-state events in the file."""
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    seed(store, TASK, workspace)
    project = claude_project_dir(tmp_path / "home", workspace)
    write(project / "session-1.jsonl", "\n".join([
        tool_use_assistant("t1", "Read"),
        cost_state(cost_usd=10.0),
        tool_use_assistant("t2", "Read"),
        cost_state(cost_usd=25.0),
    ]))

    [task] = improve.extract([TASK], store, tmp_path / "home")["tasks"]
    tokens = task["evidence"]["claude"]["tokens"]
    assert tokens["cost_usd"] == pytest.approx(25.0)
    assert tokens["models"]["claude-x"]["costUSD"] == pytest.approx(25.0)


def test_malformed_orca_id_is_a_coverage_gap_not_a_crash(tmp_path):
    store = TaskStore(tmp_path / "state")

    def build(state):
        state = {**empty_state(TASK), "state": "to_merge", "workspace": {"orca_id": "no-separator-here"}}
        return state

    store.update_task(TASK, build, "seed")

    result = improve.extract([TASK, "CHG-9999-01"], store, tmp_path / "home")
    [task, other] = result["tasks"]
    assert task["coverage"] == "missing"
    assert "no workspace recorded for this task" in task["coverage_gaps"]
    assert other["task"] == "CHG-9999-01"  # the malformed task never takes down the whole batch


def test_subagent_count_matches_parsed_transcripts_without_a_second_read(tmp_path):
    """The subagent count comes from the same `subagent_files` glob already parsed for tool
    calls/cost above, not a second pass over `activity.claude_subagents` (which re-opens every
    transcript just to read its first timestamp and additionally requires a readable
    `.meta.json`, which this fixture does not provide)."""
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    seed(store, TASK, workspace)
    project = claude_project_dir(tmp_path / "home", workspace)
    write(project / "session-1.jsonl", tool_use_assistant("t1", "Read"))
    write(project / "sess-1" / "subagents" / "agent-a.jsonl", tool_use_assistant("s1", "Bash", command="ls"))
    write(project / "sess-1" / "subagents" / "agent-b.jsonl", tool_use_assistant("s2", "Bash", command="ls"))

    [task] = improve.extract([TASK], store, tmp_path / "home")["tasks"]
    assert task["evidence"]["claude"]["subagents"] == 2


def test_malformed_transcript_lines_are_skipped_not_raised(tmp_path):
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    seed(store, TASK, workspace)
    project = claude_project_dir(tmp_path / "home", workspace)
    write(project / "session-1.jsonl", "\n".join([
        "{not json",
        tool_use_assistant("t1", "Read"),
        tool_result_user("t1"),
    ]))

    [task] = improve.extract([TASK], store, tmp_path / "home")["tasks"]
    assert task["evidence"]["claude"]["tool_calls"] == {"Read": 1}


def codex_rollout(cwd, source="cli"):
    meta = json.dumps({"payload": {"id": "sess-1", "cwd": str(cwd), "source": source}})
    call = json.dumps({"payload": {"type": "function_call", "name": "shell", "call_id": "c1",
                                   "arguments": json.dumps({"command": "pytest"})}})
    output = json.dumps({"payload": {"type": "function_call_output", "call_id": "c1", "exit_code": 1}})
    return "\n".join([meta, call, output])


def test_codex_sessions_matched_by_cwd_count_tool_calls_and_failures(tmp_path):
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    seed(store, TASK, workspace, agent="codex")
    home = tmp_path / "home"
    write(home / ".codex/sessions/2026/10/18/rollout-sess-1.jsonl", codex_rollout(workspace))
    write(home / ".codex/sessions/2026/10/18/rollout-other.jsonl", codex_rollout("/elsewhere"))

    [task] = improve.extract([TASK], store, home)["tasks"]
    codex = task["evidence"]["codex"]
    assert codex["sessions"] == 1
    assert codex["tool_calls"] == {"shell": 1}
    [failing] = codex["repeated_failing_commands"]
    assert failing == {"command": "pytest", "count": 1, "failures": 1}


def test_codex_missing_token_fields_is_reported_as_a_gap(tmp_path):
    store = TaskStore(tmp_path / "state")
    workspace = tmp_path / "work" / "repo"
    seed(store, TASK, workspace, agent="codex")
    home = tmp_path / "home"
    write(home / ".codex/sessions/2026/10/18/rollout-sess-1.jsonl", codex_rollout(workspace))

    [task] = improve.extract([TASK], store, home)["tasks"]
    assert any("schema drift" in gap for gap in task["coverage_gaps"])


def test_cli_wiring(tmp_path, monkeypatch, repo, capsys):
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.chdir(repo.root)
    store = TaskStore(repo.root / ".factory" / "state")
    seed(store, TASK, tmp_path / "work" / "repo")
    code = main(["improve", "extract", "--tasks", TASK, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["data"]["tasks"][0]["task"] == TASK


def test_audit_cli_wiring(capsys):
    code = main(["improve", "audit", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert "findings" in out["data"]
