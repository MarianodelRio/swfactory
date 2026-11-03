"""Managed reviews retain results independently of terminal and task liveness."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_claim_launch import TASK
from test_review import MAJOR, cli
from test_review import project as project  # noqa: F401
from test_review import working as working  # noqa: F401

from swfactory import reviews, tasks
from swfactory.cli import main
from swfactory.output import FactoryError
from swfactory.state import TaskStore


@pytest.fixture
def ctx(working, capsys):
    assert main(["task", "submit", TASK]) == 0
    capsys.readouterr()
    return tasks.Context(working.root, TaskStore(working.root / ".factory/state"))


def reserve(ctx, kind="quality", agent=None):
    return reviews.start(TASK, kind, agent, ctx)[0]


def report(tmp_path, run, verdict="pass", findings=()):
    path = tmp_path / f"{run['review_id']}.json"
    path.write_text(json.dumps({"type": run["type"], "commit": run["run"]["commit"],
                                "verdict": verdict, "findings": list(findings)}))
    return path


def test_native_reservation_and_direct_delivery(ctx, tmp_path):
    run = reserve(ctx)
    assert not run["launched"]
    assert run["review_id"] in run["prompt"] and "review-complete" in run["prompt"]
    assert "git diff " + run["run"]["base_commit"] in run["prompt"]
    assert reviews.gates(tasks.catalog(ctx)[TASK], ctx.store.read_task(TASK), tasks._config(ctx), ctx) == {
        "quality": "reserved"}
    saved = reviews.complete(TASK, run["review_id"], report(tmp_path, run), ctx)[0]
    assert saved["review"]["verdict"] == "pass" and not saved["review"]["historical"]
    assert reviews.wait(TASK, run["review_id"], 0, ctx)["pending"] is False


def test_same_request_never_launches_another_agent(ctx, fake_orca):
    first = reserve(ctx, agent="codex")
    second = reserve(ctx, agent="codex")
    assert second["reused"] and first["review_id"] == second["review_id"]
    calls = [c for c in fake_orca.calls() if c[:2] == ["agent", "launch"]]
    assert len(calls) == 2  # lead, reviewer
    assert first["run"]["terminal"] and first["run"]["operation_id"]


def test_ambiguous_launch_is_retained_and_not_repeated(ctx, monkeypatch):
    def lost(*args, **kwargs):
        raise FactoryError("orca_unavailable", "lost reply")

    monkeypatch.setattr("swfactory.orca.launch_agent", lost)
    with pytest.raises(FactoryError):
        reserve(ctx, agent="codex")
    run = ctx.store.read_task(TASK)["review_runs"][-1]
    assert run["status"] == "launch_unknown"
    assert reserve(ctx, agent="codex")["reused"]


def test_conformance_waits_for_current_quality_pass(ctx, tmp_path):
    with pytest.raises(FactoryError, match="quality"):
        reserve(ctx, "conformance")
    quality = reserve(ctx)
    reviews.complete(TASK, quality["review_id"], report(tmp_path, quality), ctx)
    assert reserve(ctx, "conformance")["launched"]


def test_delivery_retries_are_idempotent_under_concurrency(ctx, tmp_path):
    run = reserve(ctx)
    path = report(tmp_path, run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reviews.complete(TASK, run["review_id"], path, ctx)[0], range(2)))
    assert results[0]["review"] == results[1]["review"]
    assert len(ctx.store.read_task(TASK)["reviews"]) == 1
    report(tmp_path, run, "fail")
    with pytest.raises(FactoryError) as exc:
        reviews.complete(TASK, run["review_id"], path, ctx)
    assert exc.value.code == "review_conflict"


@pytest.mark.parametrize("field,value", [("commit", "0" * 40), ("type", "conformance")])
def test_delivery_must_match_reserved_target(ctx, tmp_path, field, value):
    run = reserve(ctx)
    path = report(tmp_path, run)
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(FactoryError) as exc:
        reviews.complete(TASK, run["review_id"], path, ctx)
    assert exc.value.code == "review_invalid"


@pytest.mark.parametrize("state", ["blocked", "done", "to_merge"])
def test_late_failure_is_retained_without_changing_state(ctx, tmp_path, state):
    run = reserve(ctx)
    ctx.store.update_task(TASK, lambda s: {**s, "state": state}, "test")
    result = reviews.complete(TASK, run["review_id"], report(tmp_path, run, "fail", [MAJOR]), ctx)[0]
    assert result["state"] == state and result["review"]["historical"]


def test_old_cancelled_delivery_cannot_replace_new_pass(ctx, tmp_path):
    old = reserve(ctx)
    reviews.cancel(TASK, old["review_id"], "abandoned", ctx)
    new = reserve(ctx)
    reviews.complete(TASK, new["review_id"], report(tmp_path, new), ctx)
    reviews.complete(TASK, old["review_id"], report(tmp_path, old, "fail"), ctx)
    assert reviews.gates(tasks.catalog(ctx)[TASK], ctx.store.read_task(TASK), tasks._config(ctx), ctx) == {
        "quality": "passed"}
    assert ctx.store.read_task(TASK)["review_runs"][0]["status"] == "cancelled"


def test_changed_code_is_visible_before_finish(ctx, tmp_path, working, capsys):
    run = reserve(ctx)
    reviews.complete(TASK, run["review_id"], report(tmp_path, run), ctx)
    cwd = tasks.workspace_path(ctx.store.read_task(TASK))
    working.commit("src/new.py", "x = 1\n", "change after review", cwd=cwd)
    code, shown = cli(capsys, "task", "show", TASK)
    assert code == 0 and shown["data"]["review_status"] == {"quality": "stale"}
    task = tasks.catalog(ctx)[TASK]
    assert tasks.pending_reviews(task, ctx.store.read_task(TASK), tasks._config(ctx), ctx) == ["quality"]


def test_wait_timeout_and_cancel(ctx):
    run = reserve(ctx)
    assert reviews.wait(TASK, run["review_id"], 0, ctx)["pending"]
    reviews.cancel(TASK, run["review_id"], "agent exited", ctx)
    assert not reviews.wait(TASK, run["review_id"], 0, ctx)["pending"]


def test_complete_cli_stdin_masks_secrets(ctx, tmp_path, monkeypatch, capsys):
    import io

    run = reserve(ctx)
    path = report(tmp_path, run, findings=[{**MAJOR, "problem": "password=do-not-store"}])
    monkeypatch.setattr("sys.stdin", io.StringIO(path.read_text()))
    assert main(["task", "review-complete", TASK, "--review", run["review_id"], "--file", "-", "--json"]) == 0
    assert "do-not-store" not in capsys.readouterr().out
    assert "do-not-store" not in (ctx.store.root / f"{TASK}.json").read_text()


def test_no_agent_edit_during_review_can_claim_a_current_pass(ctx, tmp_path, working):
    run = reserve(ctx)
    cwd = tasks.workspace_path(ctx.store.read_task(TASK))
    working.commit("src/new.py", "x = 1\n", "concurrent correction", cwd=cwd)
    saved = reviews.complete(TASK, run["review_id"], report(tmp_path, run), ctx)[0]
    assert saved["review"]["historical"]
    assert reviews.gates(tasks.catalog(ctx)[TASK], ctx.store.read_task(TASK), tasks._config(ctx), ctx) == {
        "quality": "stale"}


def test_launch_prompt_replaces_terminal_scraping(ctx, fake_orca, tmp_path):
    run = reserve(ctx, agent="codex")
    calls = [c for c in fake_orca.calls() if c[:2] == ["agent", "launch"]]
    assert "review-complete" in calls[-1][calls[-1].index("--prompt") + 1]
    reviews.complete(TASK, run["review_id"], report(tmp_path, run), ctx)
    assert not [c for c in fake_orca.calls() if c[:2] == ["terminal", "read"]]


def test_result_delivered_before_launch_reply_keeps_receipt(ctx, tmp_path, monkeypatch):
    def launch(*args, **kwargs):
        run = ctx.store.read_task(TASK)["review_runs"][-1]
        data = {"review_id": run["id"], "type": run["type"], "run": run}
        reviews.complete(TASK, run["id"], report(tmp_path, data), ctx)
        return {"terminal": "term-fast", "operation_id": "op-fast", "kind": "terminal"}

    monkeypatch.setattr("swfactory.orca.launch_agent", launch)
    run = reserve(ctx, agent="codex")["run"]
    assert run["status"] == "completed" and run["terminal"] == "term-fast" and run["operation_id"] == "op-fast"
