"""Durable review requests and direct, idempotent delivery by the reviewer.

TaskStore owns every write. A terminal is a launch receipt, never the result channel.
"""

from __future__ import annotations

import json
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swfactory import design, git, models, orca
from swfactory.output import EXIT_CONFLICT, EXIT_PRECONDITION, EXIT_USAGE, FactoryError, mask_secrets
from swfactory.state import State, now

if TYPE_CHECKING:
    from swfactory.tasks import Context


def digest(task: dict[str, Any], ctx: Context) -> str:
    return design.digest((ctx.main_checkout / task["design"]).read_text(encoding="utf-8"))


def gates(task: dict[str, Any], record: State, cfg: dict[str, Any], ctx: Context) -> dict[str, str]:
    """Required type -> missing/running/failed/stale/passed, shared by views and finish."""
    from swfactory import tasks

    kinds = list(dict.fromkeys([*tasks.effective_reviews(task, cfg),
                                *(r["type"] for r in record.get("review_runs", [])
                                  if r["status"] != "cancelled")]))
    if not kinds:
        return {}
    if not record.get("workspace"):
        return dict.fromkeys(kinds, "unknown")
    cwd = tasks.workspace_path(record)
    base = ctx.base()
    if base is None or not cwd.is_dir():
        return dict.fromkeys(kinds, "unknown")
    code = git.last_code_commit(base, cwd)
    current_digest = digest(task, ctx)
    result = {}
    for kind in kinds:
        runs = [r for r in record.get("review_runs", []) if r["type"] == kind
                and r["status"] != "cancelled"]
        reports = [r for r in record.get("reviews", []) if r["type"] == kind]
        latest = reports[-1] if reports else None
        if runs:
            run = runs[-1]
            if run["design_digest"] != current_digest or not git.is_ancestor(code, run["commit"], cwd):
                result[kind] = "stale"
                continue
            if run["status"] != "completed":
                result[kind] = run["status"]
                continue
            latest = next((r for r in reports if r.get("run_id") == run["id"]), None)
        if latest is None:
            result[kind] = "missing"
        elif (latest.get("historical") or latest.get("design_digest", current_digest) != current_digest
              or not git.is_ancestor(code, latest["commit"], cwd)):
            result[kind] = "stale"
        else:
            result[kind] = "passed" if latest["verdict"] == "pass" else "failed"
    return result


def _find(state: State, run_id: str) -> dict[str, Any]:
    for run in state.get("review_runs", []):
        if run["id"] == run_id:
            return dict(run)
    raise FactoryError("review_not_found", f"review {run_id} is not recorded for this task", EXIT_PRECONDITION)


def _prompt(task: dict[str, Any], run: dict[str, Any]) -> str:
    role = resources.files("swfactory").joinpath(
        f"templates/roles/{run['type']}_reviewer.md").read_text(encoding="utf-8")
    return (
        f"{role}\nReview task {task['id']} ({task['title']}).\n"
        f"Approved design: {task['design']} (digest {run['design_digest']}).\n"
        f"Review exactly commit {run['commit']}: git diff {run['base_commit']} {run['commit']}\n"
        f"Task and evidence: swfactory task show {task['id']} --json\n"
        "Before running tests, confirm git rev-parse HEAD equals the target and the tracked checkout is clean. "
        "If it changed, do not test another revision or claim a pass; report the mismatch to the lead.\n"
        "Read existing verification evidence first; repeat tests to investigate a concrete gap or finding.\n"
        "Do not edit product files. The only permitted write is delivery through Factory. "
        "Save the final report JSON outside the repo, then deliver it directly:\n"
        f"swfactory task review-complete {task['id']} --review {run['id']} --file <report.json> --json\n"
        f"Use type {run['type']} and commit {run['commit']} in the report. "
        "Do not ask the lead to copy JSON from this terminal."
    )


def start(task_id: str, kind: str, agent: str | None, ctx: Context, model: str | None = None,
          effort: str | None = None) -> tuple[dict[str, Any], list[str]]:
    from swfactory import tasks

    task, record = tasks._started(task_id, ctx)
    if kind not in tasks.REVIEW_TYPES:
        raise FactoryError("review_invalid", "unknown review type", EXIT_USAGE)
    if record["state"] != "in_review":
        tasks._reject(record, "review", f"it is {record['state']}; submit it first")
    cfg = tasks._config(ctx)
    if kind == "conformance" and "quality" in tasks.effective_reviews(task, cfg):
        if gates(task, record, cfg, ctx).get("quality") != "passed":
            raise FactoryError("quality_review_required", "complete a current quality pass before conformance",
                               EXIT_PRECONDITION)
    native = kind == "quality" and agent is None
    attempt = record["attempts"][-1]
    if not native:
        if agent is None or agent == attempt.get("reviewer"):
            model, effort = model or attempt.get("reviewer_model"), effort or attempt.get("reviewer_effort")
        agent = agent or attempt.get("reviewer")
        if not agent:
            raise FactoryError("review_agent_missing", "name the reviewer with --agent", EXIT_USAGE)
    model, warnings = models.resolve(agent or str(attempt.get("agent")), model, effort, Path.home())
    cwd = tasks.workspace_path(record)
    if not git.is_clean(cwd):
        raise FactoryError("workspace_not_clean", "commit tracked changes before starting a review", EXIT_PRECONDITION)
    base = orca.base_branch(ctx.main_checkout)
    run = {"id": uuid.uuid4().hex, "type": kind, "agent": agent or f"{attempt.get('agent')}-subagent",
           "model": model, "effort": effort, "attempt": attempt["id"], "commit": git.run(["rev-parse", "HEAD"], cwd),
           "base_commit": git.run(["merge-base", base, "HEAD"], cwd), "design_digest": digest(task, ctx),
           "status": "reserved", "requested_at": now(), "terminal": None, "operation_id": None}
    selected: dict[str, Any] = {}

    def reserve(state: State) -> State:
        if state["state"] != "in_review":
            tasks._reject(state, "review", "the task changed while reserving the review")
        if kind == "conformance" and "quality" in tasks.effective_reviews(task, cfg):
            if gates(task, state, cfg, ctx).get("quality") != "passed":
                raise FactoryError("quality_review_required", "quality changed while reserving conformance",
                                   EXIT_PRECONDITION)
        active = [r for r in state.setdefault("review_runs", [])
                  if r["type"] == kind and r["status"] in ("reserved", "running", "launch_unknown")]
        if active:
            previous = active[-1]
            if previous["commit"] != run["commit"] or previous["design_digest"] != run["design_digest"]:
                raise FactoryError("review_in_flight", "cancel the obsolete review before starting another",
                                   EXIT_CONFLICT)
            selected.update(previous)
        else:
            state["review_runs"].append(run)
            selected.update(run)
        return state

    ctx.store.update_task(task_id, reserve, "review_requested")
    prompt = _prompt(task, selected)
    data = {"task": task_id, "type": kind, "agent": selected["agent"], "model": selected["model"],
            "review_id": selected["id"], "prompt": prompt, "launched": False, "run": selected}
    if selected["id"] != run["id"]:
        return {**data, "reused": True}, warnings
    if native:
        return {**data, "message": "launch a read-only subagent with this prompt; it delivers its own report"}, warnings
    try:
        orca.check(tasks._min_version(ctx))
    except FactoryError as exc:
        _update(task_id, run["id"], {"status": "launch_failed", "error": exc.message}, ctx)
        raise
    try:
        receipt = orca.launch_agent(record["workspace"]["orca_id"], str(agent), prompt, model, effort)
    except FactoryError as exc:
        status = "launch_failed" if isinstance(exc, orca.OrcaError) and exc.orca_code else "launch_unknown"
        _update(task_id, run["id"], {"status": status, "error": exc.message}, ctx)
        raise
    _update(task_id, run["id"], {"status": "running", **receipt}, ctx)
    return {**data, "launched": True, **receipt, "run": _find(ctx.store.read_task(task_id) or {}, run["id"])}, warnings


def _update(task_id: str, run_id: str, fields: dict[str, Any], ctx: Context) -> None:
    def apply(state: State) -> State:
        for run in state.get("review_runs", []):
            if run["id"] == run_id:
                # An agent may deliver before Orca returns its launch receipt.
                if run["status"] in ("completed", "cancelled"):
                    run.update({k: v for k, v in fields.items() if k != "status"})
                else:
                    run.update(fields)
        return state

    ctx.store.update_task(task_id, apply, "review_launch")


def complete(task_id: str, run_id: str, path: Path, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    from swfactory import tasks

    task, record = tasks._started(task_id, ctx)
    report = tasks._review_report(path)
    report = json.loads(mask_secrets(json.dumps(report)))
    run = _find(record, run_id)
    cwd = tasks.workspace_path(record)
    if report["type"] != run["type"] or report["commit"] != run["commit"]:
        raise FactoryError("review_invalid", "report type and full commit must match the reserved review",
                           EXIT_PRECONDITION)
    serious = any(f["severity"] in ("blocker", "major") for f in report["findings"])
    report["verdict"] = "fail" if serious or report["verdict"] == "fail" else "pass"
    current = (digest(task, ctx) == run["design_digest"] and cwd.is_dir()
               and git.run(["rev-parse", "HEAD"], cwd) == run["commit"] and git.is_clean(cwd))
    saved: dict[str, Any] = {}

    def apply(state: State) -> State:
        active = next(r for r in state["review_runs"] if r["id"] == run_id)
        existing = next((r for r in state["reviews"] if r.get("run_id") == run_id), None)
        if existing:
            if any(existing[k] != report[k] for k in report):
                raise FactoryError("review_conflict", "this review already has a different result", EXIT_CONFLICT)
            saved.update(existing)
            return state
        newer = any(r["type"] == run["type"] and r["status"] != "cancelled"
                    for r in state["review_runs"][state["review_runs"].index(active) + 1:])
        historical = not current or newer or active["status"] == "cancelled" or state["state"] != "in_review"
        round_number = 1 + sum(r["type"] == run["type"] and not r.get("historical") for r in state["reviews"])
        saved.update(**report, reviewer=run["agent"], round=round_number, run_id=run_id,
                     design_digest=run["design_digest"], historical=historical, at=now())
        state["reviews"].append(saved)
        if active["status"] != "cancelled":
            active.update(status="completed", completed_at=now())
        if (not historical and report["verdict"] == "fail"
                and round_number >= tasks._config(ctx)["review"]["max_rounds"]):
            state = tasks.transition(state, "block", reason=tasks.REVIEW_NOT_CONVERGING)
        return state

    updated = ctx.store.update_task(task_id, apply, "review")
    return {"task": task_id, "state": updated["state"], "review": saved}, tasks._card(task, updated, ctx)


def wait(task_id: str, run_id: str, timeout: float, ctx: Context) -> dict[str, Any]:
    if not 0 <= timeout <= 60:
        raise FactoryError("review_invalid", "wait timeout must be between 0 and 60 seconds", EXIT_USAGE)
    deadline = time.monotonic() + timeout
    while True:
        record = ctx.store.read_task(task_id) or {}
        run = _find(record, run_id)
        report = next((r for r in record.get("reviews", []) if r.get("run_id") == run_id), None)
        if report or run["status"] in ("cancelled", "launch_failed") or time.monotonic() >= deadline:
            return {"task": task_id, "run": run, "review": report, "pending": not report and run["status"] not in
                    ("cancelled", "launch_failed")}
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def cancel(task_id: str, run_id: str, reason: str, ctx: Context) -> dict[str, Any]:
    if not reason.strip():
        raise FactoryError("review_invalid", "a cancellation reason is required", EXIT_PRECONDITION)

    def apply(state: State) -> State:
        _find(state, run_id)
        for run in state.get("review_runs", []):
            if run["id"] == run_id and run["status"] != "completed":
                run.update(status="cancelled", cancelled_at=now(), cancel_reason=reason)
        return state

    state = ctx.store.update_task(task_id, apply, "review_cancelled")
    return {"task": task_id, "run": _find(state, run_id)}
