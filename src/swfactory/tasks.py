"""Task lifecycle.

Raises FactoryError with these codes:
- invalid_transition (EXIT_PRECONDITION): the action is not allowed from the task's state.
- task_not_found (EXIT_PRECONDITION): no approved change has a task with that id.
- task_not_ready (EXIT_PRECONDITION): claim of a task that is not `ready` (the reason is given).
- approval_not_on_base (EXIT_PRECONDITION): the approval commit is not on the base branch.
- task_claimed (EXIT_CONFLICT): the task is already claimed (from swfactory.state).
- already_launched (EXIT_CONFLICT): the current attempt already has a lead agent, or a launch
  whose result is unknown.
- launch_unconfirmed (EXIT_UNAVAILABLE): Orca did not answer a launch; an agent may have started.
- task_not_started (EXIT_PRECONDITION): the task has no state yet (it was never claimed).
- approval_not_valid (EXIT_PRECONDITION): unblock while the change's approval is not valid.
- evidence_not_found / ac_not_in_task / ac_not_manual / secret_file_refused (EXIT_PRECONDITION): bad
  `verify --manual`.
- review_invalid (EXIT_PRECONDITION): the reviewer JSON does not follow the reviewer JSON contract.
- agent_missing (EXIT_USAGE): `task launch` without `--agent` and without `agents.lead` in the config.
- review_agent_missing (EXIT_USAGE): a conformance review without `--agent` or a recorded reviewer.
- gates_not_met (EXIT_PRECONDITION): `finish` with closing criteria unmet; `details` lists them.
- workspace_not_clean (EXIT_PRECONDITION): `task cleanup` of a workspace with uncommitted files.
- checks_missing (EXIT_PRECONDITION): `swfactory check` without any check configured.
- not_merged (EXIT_PRECONDITION): `task merged` for a task that is not waiting to be merged.
- session_alive (EXIT_PRECONDITION): `recover --confirm` while Orca says the agent is alive.
- workspace_missing (EXIT_PRECONDITION): `recover --confirm` when the workspace no longer exists.
Plus the codes of factory.orca (Orca unavailable, not local, ...) and factory.models (model_unknown, ...).
"""

from __future__ import annotations

import copy
import datetime
import fnmatch
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn

import yaml

from swfactory import config, design, git, models, orca, sync, system
from swfactory.output import (
    EXIT_CONFLICT,
    EXIT_PRECONDITION,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    FactoryError,
    is_secret_file,
    mask_secrets,
)
from swfactory.state import State, TaskStore, now

# Reasons for `blocked` that the CLI sets itself; any other text comes from `task block --reason`.
PAUSED = "paused"
DESIGN_CHANGED = "design_changed"
REVIEW_NOT_CONVERGING = "review_not_converging"

# (state, action) -> next state. `block` records the previous state in
# `blocked_from`; `unblock` and `reapprove` return to it. The callers check the conditions
# that are not about states: a valid approval for `unblock`, liveness for `recover`.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("ready", "claim"): "in_progress",  # swfactory task claim
    ("interrupted", "recover"): "in_progress",  # swfactory task recover --confirm
    ("in_progress", "recover"): "in_progress",  # only when liveness is unknown
    ("in_review", "recover"): "in_progress",  # also only when unknown; the new lead submits again
    ("blocked", "recover"): "in_progress",  # a new agent takes over (not for design_changed)
    ("in_progress", "submit"): "in_review",  # swfactory task submit
    ("in_progress", "finish"): "to_merge",  # swfactory task finish (closing gates)
    ("in_review", "finish"): "to_merge",
    ("to_merge", "merged"): "done",  # sync saw it in the base branch, or the person said so
    ("done", "unmerged"): "to_merge",  # a task finished before `to_merge` existed, not merged yet
    ("in_progress", "block"): "blocked",  # task block, a read pause note, a design change
    ("in_review", "block"): "blocked",  # also a review that does not converge
    ("interrupted", "block"): "blocked",  # a design change
    ("in_progress", "interrupt"): "interrupted",  # status/recover: the lead agent finished
    ("in_review", "interrupt"): "interrupted",  # before finish, while the reviews run
}
RETURNS = {"unblock", "reapprove"}
# States that a design change blocks.
BLOCKED_BY_DESIGN_CHANGE = ("in_progress", "in_review", "interrupted")


def transition(task: State, action: str, current: str | None = None, reason: str | None = None) -> State:
    """Return a copy of `task` after `action`. `current` is the computed state (`pending` or
    `ready`) of a task that has no stored state yet; a stored state always wins."""
    state: str = task["state"] or current or ""
    result = copy.deepcopy(task)

    if action in RETURNS and state == "blocked":
        if action == "reapprove" and task["blocked_reason"] != DESIGN_CHANGED:
            _reject(task, action, f"it is blocked for '{task['blocked_reason']}', not by a design change")
        result.update(state=task["blocked_from"], blocked_reason=None, blocked_from=None)
        return result

    target = TRANSITIONS.get((state, action))
    if target is None:
        _reject(task, action, f"not allowed from '{state}'")
    if state == "blocked" and action == "recover":
        if task["blocked_reason"] == DESIGN_CHANGED:
            _reject(task, action, "it is blocked by a design change: approve the design again first")
        result.update(blocked_reason=None, blocked_from=None)
    if action == "block":
        if not reason:
            _reject(task, action, "a reason is required")
        result.update(blocked_reason=reason, blocked_from=state)
    result["state"] = target
    return result


def _reject(task: State, action: str, why: str) -> NoReturn:
    message = f"cannot {action} {task['task']}: {why}"
    if task.get("state") == "blocked" and action not in ("unblock", "reapprove", "recover", "block"):
        message += (f" (blocked: {task.get('blocked_reason')}). If the person resumed the work, they unblock it "
                    f"first: swfactory task unblock {task['task']}")
    raise FactoryError("invalid_transition", message, EXIT_PRECONDITION)


def sync_with_design(task_ids: list[str], approved: bool, store: TaskStore) -> None:
    """Keep the approved tasks of a change in step with its design: while the design does not match its
    approval, tasks in progress are blocked (`design_changed`); once it matches again (re-approval
    or a reverted edit), those tasks return to their previous state. `done` tasks are not touched."""
    for task_id in task_ids:
        stored = store.read_task(task_id)
        if stored is None:
            continue
        if not approved and stored["state"] in BLOCKED_BY_DESIGN_CHANGE:
            block = _if_state(BLOCKED_BY_DESIGN_CHANGE, "block", DESIGN_CHANGED)
            store.update_task(task_id, block, "design_changed")
        elif approved and stored["state"] == "blocked" and stored["blocked_reason"] == DESIGN_CHANGED:
            store.update_task(task_id, _if_state(("blocked",), "reapprove", None), "reapproved")


def _if_state(states: tuple[str, ...], action: str, reason: str | None) -> Callable[[State], State]:
    """A transition applied inside the task lock only if the state is still one of `states`."""

    def apply(task: State) -> State:
        if task["state"] not in states:
            return task
        if action == "reapprove" and task["blocked_reason"] != DESIGN_CHANGED:
            return task
        return transition(task, action, reason=reason)

    return apply


class Context:
    """What computing task states needs: the main checkout (owner of the approved changes and the
    shared state) and the base branch, read from Orca once and only when a dependency needs it."""

    def __init__(self, main_checkout: Path, store: TaskStore) -> None:
        self.main_checkout = main_checkout
        self.changes_dir = main_checkout / ".factory" / "changes"
        self.store = store
        self._base: str | None = None
        self._base_error: str | None = None

    def base(self) -> str | None:
        if self._base is None and self._base_error is None:
            try:
                self._base = orca.base_branch(self.main_checkout)
            except FactoryError as exc:
                self._base_error = exc.message
        return self._base

    def base_error(self) -> str:
        return self._base_error or ""


def catalog(ctx: Context, sync: bool = True) -> dict[str, dict[str, Any]]:
    """Every task of every approved, not rejected change: id -> task definition plus `change`
    and whether the change's approval is still valid. Syncs each change with its design first,
    unless `sync` is False (read-only views)."""
    tasks: dict[str, dict[str, Any]] = {}
    for change_id, folder in design.change_folders(ctx.changes_dir):
        tasks_file = folder / "tasks.yaml"
        if not tasks_file.exists() or (folder / "rejection.json").exists():
            continue
        if sync:
            design.sync_with_main(change_id, ctx.changes_dir, ctx.store)
        approval = design.approval_of(folder)
        current = design.digest((folder / "design.md").read_text(encoding="utf-8"))
        approved = approval is not None and approval.get("design_digest") == current
        data = yaml.safe_load(tasks_file.read_text(encoding="utf-8")) or {}
        for task in data.get("tasks", []):
            tasks[str(task["id"])] = {**task, "change": change_id, "approved": approved,
                                      "design": f".factory/changes/{folder.name}/design.md"}
    return tasks


def view(task: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """The task with its state and the reason it is not ready."""
    record = ctx.store.read_task(task["id"])
    state, reason = (record["state"], record.get("blocked_reason")) if record else _computed(task, ctx)
    workspace = (record or {}).get("workspace") or {}
    return {
        "id": task["id"],
        "title": task["title"],
        "change": task["change"],
        "state": state,
        "reason": reason,
        "depends_on": task.get("depends_on", []),
        "workspace": workspace.get("orca_id"),
        "branch": workspace.get("branch"),
    }


def _computed(task: dict[str, Any], ctx: Context) -> tuple[str, str | None]:
    """`pending` or `ready` for a task without a state file: approval valid and every dependency
    `done` (merged into the base branch)."""
    if not task["approved"]:
        return "pending", f"{task['change']} is awaiting approval"
    for dep in task.get("depends_on", []):
        record = ctx.store.read_task(dep)
        if record is not None and record["state"] == "to_merge":
            return "pending", f"waiting for {dep} to be merged into {ctx.base() or 'the base branch'}"
        if record is None or record["state"] != "done":
            return "pending", f"waiting for {dep} to be done"
    return "ready", None


def _merged(record: State, base: str, cwd: Path) -> bool:
    """The task's last commit is in the base branch (local or remote). A squashed or rebased merge
    is not: the pull request's state tells those."""
    branch = (record.get("workspace") or {}).get("branch")
    tip = branch if branch and git.ref_exists(branch, cwd) else (record.get("commits") or [None])[-1]
    if not tip:
        return False
    targets = [ref for ref in (f"origin/{base}", base) if git.ref_exists(ref, cwd)]
    return any(git.is_ancestor(tip, ref, cwd) for ref in targets)


def _merge_status(record: State, base: str | None, ctx: Context) -> tuple[bool, str | None, bool]:
    """(merged, when, pull request closed without merging) for a finished task: merged if its pull
    request is (GitHub knows about squash and rebase merges) or if its commits are in the base."""
    pull_request = record.get("pull_request")
    state = None
    if pull_request:
        try:
            state = git.pr_state(int(pull_request["number"]), ctx.main_checkout)
        except FactoryError:
            state = None  # no usable gh: the commits tell
    if state and state.get("state") == "MERGED":
        return True, state.get("mergedAt") or now(), False
    if base and _merged(record, base, ctx.main_checkout):
        return True, now(), False
    return False, None, bool(state and state.get("state") == "CLOSED")


def record_merges(ctx: Context) -> list[str]:
    """Move each `to_merge` task whose work reached the base branch to `done`. A task finished
    before `to_merge` existed (`done` without `merged_at`) gets its merge date, or goes back to
    `to_merge` if it is not merged yet. Returns warnings (never raises for one task)."""
    warnings: list[str] = []
    if ctx.base() is None:
        return [f"cannot check which tasks are merged: {ctx.base_error()}"]
    for path in sorted(ctx.store.root.glob("*.events.jsonl")) if ctx.store.root.is_dir() else []:
        task_id = path.name.removesuffix(".events.jsonl")
        record = ctx.store.read_task(task_id)
        if record is None or not (record["state"] == "to_merge"
                                  or (record["state"] == "done" and not record.get("merged_at"))):
            continue
        try:
            merged, when, closed = _merge_status(record, ctx.base(), ctx)
        except FactoryError as exc:
            warnings.append(f"cannot tell whether {task_id} is merged: {exc.message}")
            continue
        warnings += _apply_merge(task_id, record["state"], merged, when, closed, ctx)
    return warnings


def _apply_merge(task_id: str, state: str, merged: bool, when: str | None, closed: bool,
                 ctx: Context) -> list[str]:
    def apply(current: State) -> State:
        if current["state"] != state:
            return current  # something else changed it meanwhile
        if merged:
            if current["state"] == "to_merge":
                current = transition(current, "merged")
            current["merged_at"] = when
        elif current["state"] == "done":
            current = transition(current, "unmerged")
        if current.get("pull_request"):
            current["pull_request"]["closed"] = closed
        return current

    previous = ctx.store.read_task(task_id) or {}
    if merged:
        record = ctx.store.update_task(task_id, apply, "merged")
    elif state == "done" or (previous.get("pull_request") or {}).get("closed", False) != closed:
        record = ctx.store.update_task(task_id, apply, "awaiting_merge" if state == "done" else "pr_closed")
    else:
        return []
    workspace = (record.get("workspace") or {}).get("orca_id")
    task = catalog(ctx, sync=False).get(task_id)
    warnings = []
    if workspace and task:
        warnings += orca.update_card(workspace, task_id, task["title"], record["state"], card_detail(record))
    if merged and state == "to_merge":
        warnings += _after_merge(task_id, record, ctx)
    return warnings


def _after_merge(task_id: str, record: State, ctx: Context) -> list[str]:
    """Tidy up after a merge: delete the remote branch, and remove the workspace and the local
    branch when `git.cleanup` is `always` (with `ask`, the person is asked)."""
    warnings: list[str] = []
    branch = (record.get("workspace") or {}).get("branch")
    if branch and git.ref_exists(f"origin/{branch}", ctx.main_checkout):
        try:
            git.delete_remote_branch(branch, ctx.main_checkout)
        except FactoryError as exc:
            warnings.append(f"the remote branch {branch} was not deleted: {exc.message}")
    if _config(ctx)["git"]["cleanup"] == "always":
        try:
            warnings += cleanup(task_id, ctx)[1]
        except FactoryError as exc:
            warnings.append(f"the workspace of {task_id} was kept: {exc.message}")
    return warnings


def cleanup(task_id: str, ctx: Context, keep: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Remove a merged task's Orca workspace and its local branch (or, with `keep`, record that the
    person keeps them). Refused while the workspace has uncommitted or untracked files."""
    _, record = _started(task_id, ctx)
    if record["state"] != "done":
        _reject(record, "clean up", f"it is {record['state']}; only a merged (done) task is cleaned up")
    if keep:
        ctx.store.update_task(task_id, lambda s: {**s, "cleanup": "kept"}, "cleanup_kept")
        return {"task": task_id, "kept": True}, []
    workspace = record.get("workspace") or {}
    path = workspace_path(record)
    if path.is_dir():
        leftovers = git.run(["status", "--porcelain"], path).splitlines()
        if leftovers:
            raise FactoryError("workspace_not_clean", f"the workspace of {task_id} has uncommitted files",
                               EXIT_PRECONDITION, leftovers[:20])
        orca.remove_workspace(workspace["orca_id"])
    warnings: list[str] = []
    branch = workspace.get("branch")
    if branch and git.ref_exists(f"refs/heads/{branch}", ctx.main_checkout):
        try:
            git.run(["branch", "-q", "-D", branch], ctx.main_checkout)
        except FactoryError as exc:
            warnings.append(f"the local branch {branch} was kept: {exc.message}")

    ctx.store.update_task(task_id, lambda s: {**s, "cleanup": "done"}, "cleanup")
    return {"task": task_id, "workspace_removed": not path.is_dir(), "branch": branch}, warnings


def cleanup_pending(record: State | None, cfg: dict[str, Any]) -> bool:
    """A merged task whose workspace is still there and the person has not been asked about."""
    return (record is not None and record["state"] == "done" and cfg["git"]["cleanup"] == "ask"
            and not record.get("cleanup") and bool(record.get("workspace")) and workspace_path(record).is_dir())


def mark_merged(task_id: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """The person says a `to_merge` task is merged (for example squashed without `gh`)."""
    task, record = _started(task_id, ctx)
    if record["state"] != "to_merge":
        raise FactoryError("not_merged", f"{task_id} is {record['state']}, not waiting to be merged",
                           EXIT_PRECONDITION)
    return {"task": task_id, "state": "done"}, _apply_merge(task_id, "to_merge", True, now(), False, ctx)


def list_tasks(ctx: Context) -> dict[str, Any]:
    record_merges(ctx)
    all_tasks = catalog(ctx)
    return {"tasks": [view(task, ctx) for task in all_tasks.values()]}


def _task(task_id: str, ctx: Context) -> dict[str, Any]:
    all_tasks = catalog(ctx)
    if task_id not in all_tasks:
        raise FactoryError("task_not_found", f"no approved change has a task {task_id}", EXIT_PRECONDITION)
    return all_tasks[task_id]


def show_task(task_id: str, ctx: Context) -> dict[str, Any]:
    record_merges(ctx)
    task = _task(task_id, ctx)
    shown = view(task, ctx)
    definition = {k: v for k, v in task.items() if k not in ("change", "approved")}
    return {**definition, **shown, "record": ctx.store.read_task(task_id)}


def new_attempt(number: int) -> dict[str, Any]:
    return {
        "id": number,
        "agent": None,
        "reviewer": None,
        "model": None,
        "effort": None,
        "reviewer_model": None,
        "reviewer_effort": None,
        "operation_id": None,
        "terminal": None,
        "started_at": now(),
        "ended_at": None,
        "result": None,
    }


def claim(task_id: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Claim a ready task and create its Orca workspace from the latest base branch (fetched first,
    so the work starts from what is on the remote)."""
    task = _task(task_id, ctx)
    if ctx.store.read_claim(task_id) is not None:
        raise FactoryError("task_claimed", f"{task_id} is already claimed", EXIT_CONFLICT)
    orca.check(_min_version(ctx))
    base = orca.base_branch(ctx.main_checkout)
    synced = sync_repo(ctx)
    shown = view(task, ctx)
    if shown["state"] != "ready":
        raise FactoryError("task_not_ready", f"{task_id} is {shown['state']}: {shown['reason']}", EXIT_PRECONDITION)
    start_ref = _approved_start(task["change"], base, ctx)

    ctx.store.create_claim(task_id, 1)
    try:
        name = f"{task_id.lower()}-{design.slugify(task['title'])}"  # Orca's workspace and branch name
        workspace = orca.create_workspace(name, start_ref, ctx.main_checkout)

        def start(state: State) -> State:
            state = transition(state, "claim", current="ready")
            state["workspace"] = {"orca_id": workspace["id"], "branch": workspace["branch"]}
            state["attempts"] = [new_attempt(1)]
            return state

        ctx.store.update_task(task_id, start, "claim")
    except Exception:
        ctx.store.release_claim(task_id)  # the task goes back to ready
        raise
    synced += _push_quietly(workspace["branch"], ctx)
    next_action = f"launch the lead agent (swfactory task launch {task_id})"
    warnings = synced + orca.update_card(workspace["id"], task_id, task["title"], "in_progress", next_action)
    data = {"task": task_id, "state": "in_progress", "workspace": workspace["id"], "path": workspace["path"],
            "branch": workspace["branch"], "base": start_ref, "attempt": 1}
    return data, warnings


def agent_choice(role: str, agent: str | None, model: str | None, effort: str | None,
                 cfg: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """The agent, model and effort of the lead or the reviewer: what the person asked, else
    `[agents]` in the config. A configured model only applies to its configured agent."""
    defaults = cfg["agents"]
    chosen = agent or defaults[role] or None
    if chosen and chosen == defaults[role]:
        model = model or defaults[f"{role}_model"] or None
        effort = effort or defaults[f"{role}_effort"] or None
    return chosen, model, effort


def launch(
    task_id: str, agent: str | None, reviewer: str | None, model: str | None, effort: str | None, ctx: Context,
    reviewer_model: str | None = None, reviewer_effort: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Open the lead agent in the task's workspace with the brief `/factory-build <id>`. Agents,
    models and efforts not given come from `[agents]` in the config; a model may be named as the
    person says it ("opus 5.5"): Orca's model catalog gives its id."""
    task = _task(task_id, ctx)
    if ctx.store.read_task(task_id) is None:
        _reject({"task": task_id}, "launch", f"it is {view(task, ctx)['state']}; claim it first")
    cfg = _config(ctx)
    agent, model, effort = agent_choice("lead", agent, model, effort, cfg)
    if not agent:
        raise FactoryError("agent_missing", "name the lead agent (--agent claude|codex) or set agents.lead",
                           EXIT_USAGE)
    reviewer, reviewer_model, reviewer_effort = agent_choice("reviewer", reviewer, reviewer_model, reviewer_effort, cfg)
    model, warnings = models.resolve(agent, model, effort, Path.home())
    if reviewer:
        reviewer_model, more = models.resolve(reviewer, reviewer_model, reviewer_effort, Path.home())
        warnings += more
    orca.check(_min_version(ctx))

    def mark(state: State) -> State:
        """Take the attempt for this launch inside the task lock, so two launches never both start."""
        if state["state"] != "in_progress":
            _reject(state, "launch", f"it is {state['state']}; claim it first")
        attempt = state["attempts"][-1]
        if attempt["agent"] is not None:
            message = f"attempt {attempt['id']} of {task_id} already has a lead agent"
            raise FactoryError("already_launched", message, EXIT_CONFLICT)
        attempt.update(agent=agent, reviewer=reviewer, model=model, effort=effort,
                       reviewer_model=reviewer_model if reviewer else None,
                       reviewer_effort=reviewer_effort if reviewer else None)
        return state

    record = ctx.store.update_task(task_id, mark, "launch")
    prompt = f"/{_build_skill(ctx)} {task_id}" + (f" --reviewer {reviewer}" if reviewer else "")
    workspace_id = record["workspace"]["orca_id"]
    try:
        launched = orca.launch_agent(workspace_id, agent, prompt, model, effort)
    except orca.OrcaError as exc:
        if exc.orca_code is None:
            message = (f"Orca did not confirm the launch ({exc.message}); an agent may have started. "
                       f"Check the workspace in Orca; if no agent is there, use swfactory task recover {task_id}")
            raise FactoryError("launch_unconfirmed", message, EXIT_UNAVAILABLE) from exc
        _set_attempt(ctx, task_id, {"agent": None, "reviewer": None, "model": None}, "launch_failed")
        raise
    except FactoryError:
        _set_attempt(ctx, task_id, {"agent": None, "reviewer": None, "model": None}, "launch_failed")
        raise
    _set_attempt(ctx, task_id, {"operation_id": launched["operation_id"], "terminal": launched["terminal"]},
                 "launched")
    warnings += orca.update_card(workspace_id, task_id, task["title"], "in_progress", f"lead agent {agent} working")
    data = {"task": task_id, "agent": agent, "reviewer": reviewer, "model": model, "effort": effort,
            "reviewer_model": reviewer_model if reviewer else None,
            "operation_id": launched["operation_id"], "terminal": launched["terminal"],
            "message": f"{task_id} launched; open it from the Orca sidebar"}
    return data, warnings


def _build_skill(ctx: Context) -> str:
    """The skill the lead is launched with: `factory-build`, or `factory-implement` in a repository
    whose skills were installed before the rename (until `/factory-init` updates them)."""
    installed = [ctx.main_checkout / base for base in (".claude/skills", ".agents/skills")]
    if not any((base / "factory-build").is_dir() for base in installed) and \
            any((base / "factory-implement").is_dir() for base in installed):
        return "factory-implement"
    return "factory-build"


def _push_quietly(branch: str, ctx: Context) -> list[str]:
    """Push the task branch so it is visible on the remote (at claim) and backed up (at finish);
    a failure is only a warning."""
    if not git.has_remote("origin", ctx.main_checkout):
        return []
    try:
        git.push_branch(branch, ctx.main_checkout)
    except FactoryError as exc:
        return [f"the branch {branch} was not pushed: {exc.message}"]
    return []


def _set_attempt(ctx: Context, task_id: str, fields: dict[str, Any], event: str) -> None:
    def apply(state: State) -> State:
        state["attempts"][-1].update(fields)
        return state

    ctx.store.update_task(task_id, apply, event)


def _min_version(ctx: Context) -> str:
    return str(_config(ctx)["orca"]["min_version"])


def _approved_start(change_id: str, base: str, ctx: Context) -> str:
    """The ref a task workspace starts from: `origin/<base>` when the approval is already there,
    else the local base branch (an approval that could not be pushed yet)."""
    folder = design.change_folder(ctx.changes_dir, change_id)
    approval = (design.approval_of(folder) if folder else None) or {}
    for ref in (f"origin/{base}", base):
        on_ref = git.show_file(ref, f".factory/changes/{folder.name if folder else change_id}/approval.json",
                               ctx.main_checkout)
        if on_ref is not None and json.loads(on_ref).get("design_digest") == approval.get("design_digest"):
            return ref
    message = f"the approval of {change_id} is not committed on {base}"
    raise FactoryError("approval_not_on_base", message, EXIT_PRECONDITION)


def _started(task_id: str, ctx: Context) -> tuple[dict[str, Any], State]:
    task = _task(task_id, ctx)
    record = ctx.store.read_task(task_id)
    if record is None:
        raise FactoryError("task_not_started", f"{task_id} has not been claimed yet", EXIT_PRECONDITION)
    return task, record


def pending_reviews(task: dict[str, Any], record: State, cfg: dict[str, Any]) -> list[str]:
    """Required review types whose latest verdict is not `pass`."""
    pending = []
    for kind in effective_reviews(task, cfg):
        latest = [r for r in record.get("reviews", []) if r["type"] == kind]
        if not latest or latest[-1]["verdict"] != "pass":
            pending.append(kind)
    return pending


def card_detail(record: State, pending: list[str] | None = None) -> str | None:
    """The card comment for the task's state."""
    state = record["state"]
    if state == "in_progress":
        checkpoint = record.get("last_checkpoint") or {}
        return checkpoint.get("next") or "lead agent working"
    if state == "in_review":
        return ("pending reviews: " + ", ".join(pending)) if pending else "reviews passed: finish"
    if state == "blocked":
        return str(record["blocked_reason"])
    if state in ("to_merge", "done"):
        acceptance = record.get("acceptance") or {}
        verified = sum(1 for ac in acceptance.values() if ac.get("status") == "verified")
        pr = (record.get("pull_request") or {}).get("number")
        return orca.done_comment(verified, len(acceptance), pr, merged=state == "done")
    return None


def _card(task: dict[str, Any], record: State, ctx: Context) -> list[str]:
    workspace = record["workspace"]["orca_id"]
    pending = pending_reviews(task, record, _config(ctx)) if record["state"] == "in_review" else None
    return orca.update_card(workspace, task["id"], task["title"], record["state"], card_detail(record, pending))


def checkpoint(task_id: str, done: str, next_step: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Record progress and hand over the person's unread notes, marking them read. A pause note
    moves the task to `blocked` (`paused`)."""
    task, _ = _started(task_id, ctx)
    delivered: list[dict[str, Any]] = []

    def apply(state: State) -> State:
        if state["state"] not in ("in_progress", "in_review"):
            _reject(state, "checkpoint", f"it is {state['state']}")
        state["last_checkpoint"] = {"at": now(), "done": [done], "next": next_step}
        return _deliver_notes(state, delivered)

    record = ctx.store.update_task(task_id, apply, "checkpoint")
    paused = record["state"] == "blocked"
    data = {"task": task_id, "state": record["state"], "notes": delivered, "paused": paused}
    if paused:
        data["message"] = "paused by the person: stop here; they continue with swfactory task unblock"
    return data, _card(task, record, ctx)


def _deliver_notes(state: State, delivered: list[dict[str, Any]]) -> State:
    """Mark the unread notes as read, adding them to `delivered`; a pause note blocks the task."""
    unread = [n for n in state["notes"] if not n["read"]]
    for note in unread:
        note["read"] = True
    delivered.extend(dict(n) for n in unread)
    if any(n.get("pause") for n in unread):
        return transition(state, "block", reason=PAUSED)
    return state


def note(task_id: str, text: str, pause: bool, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Leave a note (or a pause) for the lead agent; it reads it at its next checkpoint or before
    `submit`."""
    _started(task_id, ctx)

    def apply(state: State) -> State:
        state["notes"].append({"at": now(), "text": text, "pause": pause, "read": False})
        return state

    ctx.store.update_task(task_id, apply, "pause_note" if pause else "note")
    return {"task": task_id, "note": text, "pause": pause}, []


def block(task_id: str, reason: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    task, _ = _started(task_id, ctx)
    record = ctx.store.update_task(task_id, lambda s: transition(s, "block", reason=reason), "block")
    return {"task": task_id, "state": "blocked", "reason": reason}, _card(task, record, ctx)


def unblock(task_id: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Return a blocked task to its previous state, only while the change's approval is valid."""
    task, _ = _started(task_id, ctx)
    if not task["approved"]:
        message = f"{task['change']} is awaiting approval: approve the design again before unblocking"
        raise FactoryError("approval_not_valid", message, EXIT_PRECONDITION)
    record = ctx.store.update_task(task_id, lambda s: transition(s, "unblock"), "unblock")
    return {"task": task_id, "state": record["state"]}, _card(task, record, ctx)


def workspace_path(record: State) -> Path:
    return Path(str(record["workspace"]["orca_id"]).split("::", 1)[1])


def verify(task_id: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Run the project checks and each AC command in the task's workspace. An AC is
    `verified` only on exit code 0; a command that cannot run is `not_run`, never passed."""
    task, record = _started(task_id, ctx)
    if record["state"] not in ("in_progress", "in_review"):
        _reject(record, "verify", f"it is {record['state']}")
    cfg = _config(ctx)
    cwd = workspace_path(record)
    evidence_dir = _evidence_dir(task_id, ctx)
    changed, untracked = git.uncommitted(cwd)
    dirty = bool(changed)

    results: dict[str, str] = {}
    check_evidence: dict[str, str] = {}
    for name, command in cfg["checks"].items():
        results[name], check_evidence[name] = _run_evidence(command, cwd, evidence_dir / f"check-{name}.txt", ctx)
    if not results or "not_run" in results.values():
        checks_status = "not_run"
    else:
        checks_status = "failed" if "failed" in results.values() else "passed"

    acceptance: dict[str, dict[str, Any]] = {}
    for ac, how in task["acceptance"].items():
        if how == "manual":
            continue
        outcome, evidence = _run_evidence(how, cwd, evidence_dir / f"{ac}.txt", ctx)
        acceptance[ac] = {"status": "verified" if outcome == "passed" else outcome, "evidence": evidence}
    commit = git.run(["rev-parse", "HEAD"], cwd)

    def apply(state: State) -> State:
        if state["state"] not in ("in_progress", "in_review"):
            _reject(state, "verify", f"it became {state['state']} while verifying")
        state["checks"] = {"status": checks_status, "at": now(), "commit": commit, "dirty": dirty,
                           "results": results, "evidence": check_evidence}
        for ac in task["acceptance"]:
            state["acceptance"].setdefault(ac, {"status": "pending", "evidence": None})
        state["acceptance"].update(acceptance)
        return state

    updated = ctx.store.update_task(task_id, apply, "verify")
    warnings = [UNCOMMITTED_VERIFY] if dirty else []
    warnings += _untracked_warning(untracked)
    return {"task": task_id, "checks": updated["checks"], "acceptance": updated["acceptance"]}, warnings


UNCOMMITTED_VERIFY = "the workspace has uncommitted changes: commit them and verify again before finishing"


def _untracked_warning(untracked: list[str]) -> list[str]:
    """Untracked files are not part of the branch: a new module left out would break it."""
    if not untracked:
        return []
    listed = ", ".join(untracked[:5]) + (", ..." if len(untracked) > 5 else "")
    return [f"untracked files are not part of the branch ({listed}): commit them if they belong to the work"]


def _evidence_dir(task_id: str, ctx: Context) -> Path:
    """A new folder for one run's evidence, `evidence/<task>/<UTC time>`, so a later run never
    overwrites the output an earlier event points to."""
    base = ctx.store.root / "evidence" / task_id
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    folder, number = base / stamp, 1
    while True:
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            number += 1
            folder = base / f"{stamp}-{number}"


def _run_evidence(command: str, cwd: Path, output: Path, ctx: Context) -> tuple[str, str]:
    """Run one check or AC command (an argument list, never a shell) and save its evidence.
    Returns (`passed` | `failed` | `not_run`, evidence path relative to the state dir)."""
    try:
        done = subprocess.run(system.command_line(command), cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (ValueError, OSError) as exc:
        output.write_text(f"command: {command}\nnot run: {exc}\n", encoding="utf-8")
        return "not_run", output.relative_to(ctx.store.root).as_posix()
    output.write_text(
        mask_secrets(
            f"command: {command}\nexit code: {done.returncode}\n\n"
            f"--- stdout ---\n{done.stdout}\n--- stderr ---\n{done.stderr}"
        ),
        encoding="utf-8",
    )
    return ("passed" if done.returncode == 0 else "failed"), output.relative_to(ctx.store.root).as_posix()


def run_checks(cwd: Path, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """`swfactory check`: run the project checks in `cwd`, for a quick change outside any task.
    Nothing is recorded; a failed check's output (its end) is returned."""
    checks = _config(ctx)["checks"]
    if not checks:
        raise FactoryError("checks_missing", "no checks are configured in .factory/config.toml", EXIT_PRECONDITION)
    results: dict[str, dict[str, str]] = {}
    for name, command in checks.items():
        try:
            done = subprocess.run(system.command_line(command), cwd=cwd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except (ValueError, OSError) as exc:
            results[name] = {"command": command, "status": "not_run", "output": str(exc)}
            continue
        failed = done.returncode != 0
        results[name] = {"command": command, "status": "failed" if failed else "passed",
                         "output": mask_secrets((done.stdout + done.stderr)[-3000:]) if failed else ""}
    return {"passed": all(r["status"] == "passed" for r in results.values()), "checks": results}, []


def verify_manual(task_id: str, ac: str, evidence: Path, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Record a `manual` AC with the person's or agent's evidence file."""
    task, record = _started(task_id, ctx)
    if record["state"] not in ("in_progress", "in_review"):
        _reject(record, "verify", f"it is {record['state']}")
    if ac not in task["acceptance"]:
        raise FactoryError("ac_not_in_task", f"{task_id} has no {ac}", EXIT_PRECONDITION)
    if task["acceptance"][ac] != "manual":
        message = f"{ac} is verified by its command; run swfactory task verify"
        raise FactoryError("ac_not_manual", message, EXIT_PRECONDITION)
    if not evidence.is_file():
        raise FactoryError("evidence_not_found", f"{evidence} does not exist", EXIT_PRECONDITION)
    if is_secret_file(str(evidence)):
        message = f"{evidence.name} looks like a secret (key or .env file) and cannot be evidence"
        raise FactoryError("secret_file_refused", message, EXIT_PRECONDITION)
    target = _evidence_dir(task_id, ctx) / f"{ac}{evidence.suffix}"
    shutil.copyfile(evidence, target)
    relative = target.relative_to(ctx.store.root).as_posix()

    def apply(state: State) -> State:
        state["acceptance"][ac] = {"status": "verified", "evidence": relative}
        return state

    ctx.store.update_task(task_id, apply, "verify_manual")
    return {"task": task_id, "acceptance": {ac: {"status": "verified", "evidence": relative}}}, []


REVIEW_TYPES = ("quality", "conformance")
SEVERITIES = ("blocker", "major", "minor")
VERDICTS = ("pass", "fail")


def effective_reviews(task: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """The task's `review` field, with the config's `always` / `never` applied."""
    chosen = set(task.get("review") or [])
    for kind in REVIEW_TYPES:
        if cfg["review"][kind] == "always":
            chosen.add(kind)
        elif cfg["review"][kind] == "never":
            chosen.discard(kind)
    return [kind for kind in REVIEW_TYPES if kind in chosen]


def _config(ctx: Context) -> dict[str, Any]:
    return config.load_config(ctx.main_checkout / ".factory" / "config.toml")


def submit(task_id: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Read the person's pending notes and move the task to `in_review`; a pause note blocks it
    instead."""
    task, _ = _started(task_id, ctx)
    delivered: list[dict[str, Any]] = []

    def apply(state: State) -> State:
        if state["state"] != "in_progress":
            _reject(state, "submit", f"it is {state['state']}")
        state = _deliver_notes(state, delivered)
        return state if state["state"] == "blocked" else transition(state, "submit")

    record = ctx.store.update_task(task_id, apply, "submit")
    reviews = effective_reviews(task, _config(ctx))
    paused = record["state"] == "blocked"
    data = {"task": task_id, "state": record["state"], "notes": delivered, "paused": paused, "reviews": reviews}
    return data, _card(task, record, ctx)


def review(task_id: str, kind: str, agent: str | None, ctx: Context, model: str | None = None,
           effort: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Launch a reviewer: conformance is an Orca agent in the same workspace, with the model chosen
    at launch for the reviewer; quality is a read-only subagent of the lead unless an agent is named
    (client without subagents)."""
    task, record = _started(task_id, ctx)
    if record["state"] != "in_review":
        _reject(record, "review", f"it is {record['state']}; submit it first")
    attempt = record["attempts"][-1]
    if kind == "quality" and agent is None:
        message = (f"the quality review runs as a read-only subagent of the lead; record its JSON with "
                   f"swfactory task review {task_id} --record <file>")
        return {"task": task_id, "type": kind, "launched": False, "message": message}, []
    if agent is None or agent == attempt.get("reviewer"):
        model = model or attempt.get("reviewer_model")
        effort = effort or attempt.get("reviewer_effort")
    agent = agent or attempt.get("reviewer")
    if not agent:
        message = "name the reviewer with --agent (it was not given at launch with --reviewer)"
        raise FactoryError("review_agent_missing", message, EXIT_USAGE)
    model, warnings = models.resolve(agent, model, effort, Path.home())
    orca.check(_min_version(ctx))
    base = orca.base_branch(ctx.main_checkout)
    role = resources.files("swfactory").joinpath(f"templates/roles/{kind}_reviewer.md").read_text(encoding="utf-8")
    prompt = (
        f"{role}\n"
        f"Review task {task_id} ({task['title']}) of change {task['change']} in this workspace.\n"
        f"- Approved design and acceptance criteria: {task['design']}\n"
        f"- Task, evidence and checks: swfactory task show {task_id} --json\n"
        f"- The change to review: git diff {base}...HEAD\n"
        "Do not edit files. End with the JSON; the lead agent records it."
    )
    launched = orca.launch_agent(record["workspace"]["orca_id"], agent, prompt, model, effort)
    data = {"task": task_id, "type": kind, "agent": agent, "model": model, "launched": True, **launched}
    return data, warnings


def record_review(task_id: str, path: Path, agent: str | None, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Store a reviewer's JSON with its round and the reviewed commit. `pass` only when the
    reviewer says `pass` and there is no `blocker` or `major` finding; a type that is not `pass`
    after `max_rounds` blocks the task with the findings kept."""
    task, record = _started(task_id, ctx)
    report = _review_report(path)
    kind = report["type"]
    reviewer = agent or (record["attempts"][-1].get("reviewer") if kind == "conformance" else None)
    reviewer = reviewer or f"{record['attempts'][-1].get('agent') or 'lead'}-subagent"
    serious = any(f["severity"] in ("blocker", "major") for f in report["findings"])
    verdict = "fail" if serious or report["verdict"] == "fail" else "pass"
    warnings = []
    if serious and report["verdict"] == "pass":
        warnings.append("the reviewer said pass but reported blocker or major findings: recorded as fail")
    elif not serious and report["verdict"] == "fail":
        warnings.append("the reviewer said fail without blocker or major findings: ask it what must change, "
                        "or fix its minor findings, before repeating the review")
    cwd = workspace_path(record)
    if not git.ref_exists(report["commit"], cwd) or not git.is_ancestor(report["commit"], "HEAD", cwd):
        message = f"the reviewed commit {report['commit']} is not on the branch of {task_id}"
        raise FactoryError("review_invalid", message, EXIT_PRECONDITION)
    commit = git.run(["rev-parse", report["commit"]], cwd)
    max_rounds = int(_config(ctx)["review"]["max_rounds"])

    def apply(state: State) -> State:
        if state["state"] != "in_review":
            _reject(state, "record a review", f"it is {state['state']}")
        round_number = 1 + sum(1 for r in state["reviews"] if r["type"] == kind)
        state["reviews"].append({"type": kind, "reviewer": reviewer, "round": round_number, "commit": commit,
                                 "verdict": verdict, "findings": report["findings"], "at": now()})
        if verdict != "pass" and round_number >= max_rounds:
            state = transition(state, "block", reason=REVIEW_NOT_CONVERGING)
        return state

    updated = ctx.store.update_task(task_id, apply, "review")
    saved = updated["reviews"][-1]
    data = {"task": task_id, "state": updated["state"], "review": saved}
    return data, warnings + _card(task, updated, ctx)


def _review_report(path: Path) -> dict[str, Any]:
    problems: list[str] = []
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("review_invalid", f"{path} is not a readable JSON file: {exc}", EXIT_PRECONDITION) from exc
    if not isinstance(report, dict):
        problems.append("the review must be a JSON object")
        report = {}
    if report.get("type") not in REVIEW_TYPES:
        problems.append(f"type must be one of {', '.join(REVIEW_TYPES)}")
    if not isinstance(report.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{7,40}", report["commit"]):
        problems.append("commit must be the reviewed commit (git rev-parse HEAD)")
    if report.get("verdict") not in VERDICTS:
        problems.append(f"verdict must be one of {', '.join(VERDICTS)}")
    findings = report.get("findings")
    if not isinstance(findings, list):
        problems.append("findings must be a list")
        findings = []
    for number, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            problems.append(f"finding {number} must be an object")
            continue
        if finding.get("severity") not in SEVERITIES:
            problems.append(f"finding {number}: severity must be one of {', '.join(SEVERITIES)}")
        if not isinstance(finding.get("problem"), str) or not finding["problem"].strip():
            problems.append(f"finding {number}: problem is required")
    if problems:
        raise FactoryError("review_invalid", f"{path} is not a valid review", EXIT_PRECONDITION, problems)
    return {"type": report["type"], "commit": report["commit"], "verdict": report["verdict"], "findings": findings}


def finish(task_id: str, memory: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Move the task to `to_merge` only if every closing criterion holds; it becomes `done` once
    its work is merged into the base branch."""
    task, _ = _started(task_id, ctx)
    delivered: list[dict[str, Any]] = []

    def read_notes(state: State) -> State:
        """Like `submit`, `finish` hands over unread notes first; a pause blocks the task."""
        if state["state"] not in ("in_progress", "in_review"):
            _reject(state, "finish", f"it is {state['state']}")
        return _deliver_notes(state, delivered)

    record = ctx.store.update_task(task_id, read_notes, "finish_notes")
    if record["state"] == "blocked":
        data = {"task": task_id, "state": "blocked", "notes": delivered, "paused": True,
                "message": "paused by the person before finishing: stop here"}
        return data, _card(task, record, ctx)
    base = orca.base_branch(ctx.main_checkout)
    cwd = workspace_path(record)
    problems: list[str] = []

    for ac in task["acceptance"]:
        entry = record["acceptance"].get(ac) or {}
        status = entry.get("status", "pending")
        if status != "verified":
            problems.append(f"{ac} is {status}, not verified")
        elif not _evidence_exists(entry.get("evidence"), ctx):
            problems.append(f"the evidence of {ac} is missing: verify it again")
    changed, untracked = git.uncommitted(cwd)
    if changed:
        problems.append(f"the workspace has uncommitted changes ({', '.join(changed[:5])}"
                        f"{', ...' if len(changed) > 5 else ''}): commit or discard them")
    code_commit = git.last_code_commit(base, cwd)
    checks = record.get("checks") or {}
    if checks.get("status", "not_run") != "passed":
        problems.append(f"the project checks are {checks.get('status', 'not_run')}, not passed")
    elif checks.get("dirty"):
        problems.append("the project checks ran with uncommitted changes: commit them and verify again")
    elif not git.is_ancestor(code_commit, checks["commit"], cwd):
        problems.append("the project checks passed on an older commit: run swfactory task verify again")
    else:
        problems += [f"the evidence of the {name} check is missing: verify again"
                     for name, path in (checks.get("evidence") or {}).items() if not _evidence_exists(path, ctx)]
    for kind in effective_reviews(task, _config(ctx)):
        latest = [r for r in record["reviews"] if r["type"] == kind]
        if not latest or latest[-1]["verdict"] != "pass":
            problems.append(f"the {kind} review has no pass verdict")
        elif not git.is_ancestor(code_commit, latest[-1]["commit"], cwd):
            problems.append(f"the {kind} review passed on {latest[-1]['commit'][:8]}, not on the last commit with code")
    if not memory.strip():
        problems.append("memory was not evaluated: pass --memory '<summary>' or --memory none")
    if not task["approved"]:
        problems.append(f"the approval of {task['change']} is no longer valid")
    if problems:
        raise FactoryError("gates_not_met", f"{task_id} cannot be done yet", EXIT_PRECONDITION, problems)

    out_of_scope = _outside_scope(task, git.changed_files(base, cwd))
    commits = git.branch_commits(base, cwd)

    def apply(state: State) -> State:
        state = transition(state, "finish")
        state["memory"] = memory
        state["scope_warnings"] = out_of_scope
        state["commits"] = commits
        return state

    updated = ctx.store.update_task(task_id, apply, "finish")
    ctx.store.release_claim(task_id)
    warnings = [f"changed outside the task scope: {path}" for path in out_of_scope]
    warnings += _untracked_warning(untracked)
    warnings += _card(task, updated, ctx)
    warnings += _push_quietly(updated["workspace"]["branch"], ctx)
    data = {"task": task_id, "state": "to_merge", "memory": memory, "scope_warnings": out_of_scope,
            "notes": delivered}
    return data, warnings


def _evidence_exists(relative: str | None, ctx: Context) -> bool:
    return bool(relative) and (ctx.store.root / str(relative)).is_file()


def _outside_scope(task: dict[str, Any], files: list[str]) -> list[str]:
    """Files outside the task's `scope` globs, ignoring `docs/` and `.factory/`."""
    scope = task.get("scope")
    if not scope:
        return []
    return [
        path for path in files
        if not path.startswith(("docs/", ".factory/")) and not any(fnmatch.fnmatch(path, g) for g in scope)
    ]


def publish(task_id: str, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Push the task branch (only it, never forced) and open its PR against the base branch, or
    reuse the open one; a task that integrates by `merge` has its PR merged right away. Without a
    usable `gh` the PR (and the merge) is left to the person."""
    task, record = _started(task_id, ctx)
    if record["state"] != "to_merge":
        _reject(record, "publish", f"it is {record['state']}; only a finished task waiting to be merged is published")
    base = orca.base_branch(ctx.main_checkout)
    branch = record["workspace"]["branch"]
    git.push_branch(branch, ctx.main_checkout)

    warnings: list[str] = []
    pull_request = None
    try:
        git.gh_version(ctx.main_checkout)
        pull_request = git.pr_for_branch(branch, ctx.main_checkout) or git.pr_create(
            base, branch, f"{task_id}: {task['title']}", mask_secrets(pr_body(task, record)), ctx.main_checkout
        )
    except FactoryError as exc:
        if exc.code not in ("gh_unavailable", "gh_failed"):
            raise
        warnings.append(f"{exc.message}: the branch {branch} is pushed; open the PR against {base} yourself")

    mode = integration(task, _config(ctx))
    merged, merged_at = False, None

    def apply(state: State) -> State:
        state["pull_request"] = pull_request or state.get("pull_request")
        state["published_at"] = now()
        return state

    record = ctx.store.update_task(task_id, apply, "publish")
    warnings += _card(task, record, ctx)
    if pull_request:
        warnings += orca.link_pull_request(record["workspace"]["orca_id"], pull_request["number"])
        if mode == "merge":
            try:
                git.pr_merge(pull_request["number"], ctx.main_checkout)
                merged = True
            except FactoryError as exc:
                warnings.append(f"PR #{pull_request['number']} could not be merged ({exc.message}); "
                                "it stays open for the person")
        if merged:
            try:
                merged_at = git.pr_state(pull_request["number"], ctx.main_checkout).get("mergedAt")
            except FactoryError:
                merged_at = None
    elif mode == "merge":
        warnings.append(f"the task integrates by merge, but without gh it cannot be merged: merge {branch} "
                        f"into {base} yourself")
    if merged:
        warnings += sync.refresh(ctx.main_checkout, ctx.store.root, base)
        warnings += _apply_merge(task_id, "to_merge", True, merged_at or now(), False, ctx)
    data = {"task": task_id, "branch": branch, "integrate": mode, "pull_request": pull_request,
            "merged": merged, "state": "done" if merged else "to_merge"}
    return data, warnings


def integration(task: dict[str, Any], cfg: dict[str, Any]) -> str:
    """How the task's work reaches the base branch: `pr` (the person merges) or `merge`."""
    return str(task.get("integrate") or cfg["git"]["integrate"])


def pr_body(task: dict[str, Any], record: State) -> str:
    """PR description from the task: change, AC with evidence, reviews with every finding, memory
    and what remains pending."""
    lines = [f"Task {task['id']} of change {task['change']} "
             f"(design: `{task['design']}`).", "", "## Acceptance criteria", ""]
    for ac, how in task["acceptance"].items():
        entry = record["acceptance"].get(ac) or {}
        evidence = Path(entry["evidence"]).name if entry.get("evidence") else "no evidence"
        lines.append(f"- {ac} ({how}): {entry.get('status', 'pending')} — evidence `{evidence}`")
    checks = record.get("checks") or {}
    lines += ["", f"Project checks: {checks.get('status', 'not run')}.", "", "## Reviews", ""]
    if not record["reviews"]:
        lines.append("- none required")
    for review in record["reviews"]:
        lines.append(f"- {review['type']} by {review['reviewer']}, round {review['round']}: {review['verdict']}")
        for finding in review["findings"]:
            where = f"{finding.get('file', '')}:{finding.get('line', '')}".strip(":")
            lines.append(f"  - {finding['severity']} {where}: {finding['problem']}")
    lines += ["", "## Memory", "", str(record.get("memory") or "none"), "", "## Pending", ""]
    pending = [f"changed outside the task scope: `{p}`" for p in record.get("scope_warnings") or []]
    lines += [f"- {p}" for p in pending] or ["- nothing"]
    return "\n".join(lines) + "\n"


STALE_MINUTES = 10
ACTIVE = ("in_progress", "in_review", "blocked", "interrupted")
# States whose lead agent must still be running: liveness is checked and recover is allowed.
WORKING = ("in_progress", "in_review")


def _liveness_and_interrupt(task: dict[str, Any], ctx: Context) -> tuple[str | None, State | None]:
    """Ask Orca about the lead agent of an `in_progress` or `in_review` task; a session that
    finished before `finish` moves the task to `interrupted` now."""
    record = ctx.store.read_task(task["id"])
    if record is None or record["state"] not in WORKING:
        return None, record
    attempt = record["attempts"][-1]["id"]
    liveness = orca.session_liveness(record["attempts"][-1].get("terminal"))
    if liveness == "finished":
        def interrupt(state: State) -> State:
            if state["state"] not in WORKING or state["attempts"][-1]["id"] != attempt:
                return state  # something else changed the task meanwhile
            return transition(state, "interrupt")

        record = ctx.store.update_task(task["id"], interrupt, "interrupted")
        _card(task, record, ctx)
    return liveness, record


def _broken_link(record: State | None) -> bool | None:
    """True if the task's workspace no longer exists in Orca; None when Orca cannot say."""
    if record is None or record["state"] not in ACTIVE or not record.get("workspace"):
        return False
    try:
        return not orca.workspace_exists(record["workspace"]["orca_id"])
    except FactoryError:
        return None


def _minutes_since(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    then = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    return int((datetime.datetime.now(datetime.UTC) - then).total_seconds() // 60)


def sync_repo(ctx: Context, force: bool = True) -> list[str]:
    """Fetch origin, bring the local base branch up to date (see swfactory.sync) and record the
    tasks whose work is now merged."""
    warnings = sync.refresh(ctx.main_checkout, ctx.store.root, ctx.base(), force)
    return warnings + record_merges(ctx)


def sync_command(ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """`swfactory sync`: fetch, update the base branch and report where it stands."""
    warnings = sync_repo(ctx)
    base = ctx.base()
    data: dict[str, Any] = {"base": base, "head": None}
    if base and git.ref_exists(base, ctx.main_checkout):
        data["head"] = git.run(["rev-parse", base], ctx.main_checkout)
    return data, warnings


def status(ctx: Context) -> tuple[dict[str, Any], list[str]]:
    """Summary, every task and what needs attention, with liveness from Orca. Fetches first (at
    most once a minute) so merges done on the remote are seen."""
    synced = sync_repo(ctx, force=False)
    all_tasks = catalog(ctx)
    cfg = _config(ctx)
    rows: list[dict[str, Any]] = []
    attention: list[str] = []
    awaiting = awaiting_approval(ctx)
    attention += [f"{change} is awaiting approval" for change in awaiting]

    for task in all_tasks.values():
        liveness, record = _liveness_and_interrupt(task, ctx)
        row = task_row(task, record, liveness, _broken_link(record), cfg, ctx)
        rows.append(row)
        attention += _attention(row)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    summary = {"awaiting_approval": awaiting, "tasks": counts}
    return {"summary": summary, "tasks": rows, "attention": attention}, synced


def task_row(task: dict[str, Any], record: State | None, liveness: str | None, broken: bool | None,
             cfg: dict[str, Any], ctx: Context, next_step: str | None = None) -> dict[str, Any]:
    """One task as `status` shows it, from its record and what Orca said about it. `next_step`
    skips computing it (the board reuses a done task's, which costs git calls)."""
    row = view(task, ctx)
    attempt = record["attempts"][-1] if record and record["attempts"] else {}
    checkpoint = (record or {}).get("last_checkpoint")
    acceptance = (record or {}).get("acceptance") or {}
    row.update(
        agent=attempt.get("agent"),
        model=attempt.get("model"),
        reviewer=attempt.get("reviewer"),
        reviewer_model=attempt.get("reviewer_model"),
        liveness=liveness,
        last_checkpoint=checkpoint,
        minutes_since_checkpoint=_minutes_since((checkpoint or {}).get("at") or attempt.get("started_at")),
        acceptance={"verified": sum(1 for a in acceptance.values() if a.get("status") == "verified"),
                    "total": len(task["acceptance"])},
        reviews=[{k: r[k] for k in ("type", "round", "verdict", "commit")} for r in (record or {}).get("reviews", [])],
        pull_request=(record or {}).get("pull_request"),
        broken_link=broken,
        next_step=next_step or _next_step(row, record, task, cfg, ctx),
    )
    return row


def awaiting_approval(ctx: Context) -> list[str]:
    waiting = []
    for change_id, folder in design.change_folders(ctx.changes_dir):
        if not (folder / "design.md").exists() or (folder / "rejection.json").exists():
            continue
        approval = design.approval_of(folder)
        current = design.digest((folder / "design.md").read_text(encoding="utf-8"))
        if approval is None or approval.get("design_digest") != current:
            waiting.append(change_id)
    return waiting


def _attention(row: dict[str, Any]) -> list[str]:
    task, state, reason = row["id"], row["state"], row["reason"]
    notes = []
    if state == "done" and row["next_step"].startswith("merged: remove its workspace"):
        notes.append(f"{task} is merged: remove its workspace and branch? (swfactory task cleanup {task})")
    if state == "blocked":
        notes.append(f"{task} is blocked: {reason}")
    if state == "interrupted":
        notes.append(f"{task} is interrupted: recover it with /factory-status")
    if state == "pending" and reason and "merged into" in reason:
        notes.append(f"{task} is pending: {reason}")
    if state == "to_merge" and (row.get("pull_request") or {}).get("closed"):
        notes.append(f"{task}: its PR was closed without merging")
    if state == "in_progress" and (row["minutes_since_checkpoint"] or 0) > STALE_MINUTES:
        notes.append(f"{task}: no checkpoint for {row['minutes_since_checkpoint']} minutes (only a warning)")
    if row["broken_link"]:
        notes.append(f"{task} has a broken link: its Orca workspace no longer exists")
    return notes


def _next_step(
    row: dict[str, Any], record: State | None, task: dict[str, Any], cfg: dict[str, Any], ctx: Context
) -> str:
    state = row["state"]
    if state == "ready":
        return f"/factory-build {task['id']}"
    if state == "pending":
        return str(row["reason"])
    if state == "in_progress":
        return (card_detail(record) or "") if record else ""
    if state == "in_review":
        return "reviews: " + (", ".join(effective_reviews(task, cfg)) or "none required") + ", then finish"
    if state == "blocked":
        return f"resolve '{row['reason']}', then swfactory task unblock {task['id']}"
    if state == "interrupted":
        return f"swfactory task recover {task['id']}"
    if state == "to_merge" and record:
        return _after_finish(record, task, ctx)
    if state == "done":
        if cleanup_pending(record, cfg):
            return f"merged: remove its workspace? (swfactory task cleanup {task['id']}, or --keep)"
        return f"nothing: merged into {ctx.base() or 'the base branch'}"
    return "nothing"


def _after_finish(record: State, task: dict[str, Any], ctx: Context) -> str:
    """Next step of a finished task: publish it, then the person merges it (or its PR)."""
    base = ctx.base()
    branch = record["workspace"]["branch"]
    pull_request = record.get("pull_request")
    if pull_request and pull_request.get("closed"):
        return (f"PR #{pull_request['number']} was closed without merging: reopen it, or tell the Factory "
                f"it is merged (swfactory task merged {task['id']})")
    if pull_request:
        return f"merge PR #{pull_request['number']} when ready (the person decides)"
    if record.get("published_at"):
        return f"{branch} is pushed: open its PR or merge it into {base or 'the base branch'} (the person decides)"
    return f"swfactory task publish {task['id']}"


RECOVERABLE = (*WORKING, "interrupted", "blocked")


def recover(task_id: str, confirm: bool, ctx: Context, replace: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Gather what an interrupted or blocked task left. With `confirm` (after the person agrees),
    close the previous attempt, open a new one and reassign the claim, so another agent (or
    provider) takes over. `replace` is the person saying the previous session is abandoned even if
    its terminal is still open (an agent out of quota, for example). Files are never touched and
    nothing is launched."""
    task, _ = _started(task_id, ctx)
    liveness, current = _liveness_and_interrupt(task, ctx)
    assert current is not None
    record = current
    if record["state"] not in RECOVERABLE:
        _reject(record, "recover", f"it is {record['state']}")
    if record["state"] == "blocked" and record["blocked_reason"] == DESIGN_CHANGED:
        _reject(record, "recover", "it is blocked by a design change: approve the design again first")
    if record["state"] in ("interrupted", "blocked"):
        liveness = orca.session_liveness(record["attempts"][-1].get("terminal"))
    broken = _broken_link(record)

    if not confirm:
        return _recovery_report(task, record, liveness, broken, ctx), []
    if liveness == "alive" and not replace:
        message = (f"the lead agent of {task_id} is still alive: continue in it, or, if the person says it is "
                   f"abandoned (out of quota, stuck), confirm with --replace and close its terminal in Orca")
        raise FactoryError("session_alive", message, EXIT_PRECONDITION)
    if broken:
        raise FactoryError("workspace_missing", f"the workspace of {task_id} no longer exists in Orca",
                           EXIT_PRECONDITION)

    if ctx.store.read_claim(task_id) is None:
        raise FactoryError("not_claimed", f"{task_id} has no claim to reassign", EXIT_PRECONDITION)

    def apply(state: State) -> State:
        if state["state"] not in RECOVERABLE:
            _reject(state, "recover", f"it is {state['state']}")
        state["attempts"][-1].update(ended_at=now(), result="replaced" if state["state"] == "blocked" or replace
                                     else "interrupted")
        state["attempts"].append(new_attempt(len(state["attempts"]) + 1))
        return transition(state, "recover")

    updated = ctx.store.update_task(task_id, apply, "recover")
    attempt = updated["attempts"][-1]["id"]
    ctx.store.reassign_claim(task_id, attempt)
    warnings = _card(task, updated, ctx)
    data = {"task": task_id, "state": updated["state"], "attempt": attempt,
            "next_step": f"swfactory task launch {task_id} --agent <claude|codex>: "
                         "it continues from the last checkpoint"}
    return data, warnings


def _recovery_report(
    task: dict[str, Any], record: State, liveness: str | None, broken: bool | None, ctx: Context
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "task": task["id"],
        "state": record["state"],
        "blocked_reason": record.get("blocked_reason"),
        "previous_agent": {k: record["attempts"][-1].get(k) for k in ("agent", "model", "reviewer")},
        "liveness": liveness,
        "broken_link": broken,
        "workspace": record["workspace"]["orca_id"],
        "branch": record["workspace"]["branch"],
        "last_checkpoint": record.get("last_checkpoint"),
        "acceptance": record.get("acceptance"),
        "missing_acceptance": [ac for ac in task["acceptance"]
                               if (record["acceptance"].get(ac) or {}).get("status") != "verified"],
        "reviews": [{k: r[k] for k in ("type", "round", "verdict", "commit")} for r in record.get("reviews", [])],
        "git_status": None,
        "commits": None,
    }
    cwd = workspace_path(record)
    if not broken and cwd.is_dir():
        report["git_status"] = git.run(["status", "--porcelain"], cwd).splitlines()
        base = ctx.base()
        if base:
            report["commits"] = git.run(["log", "--oneline", f"{base}..HEAD"], cwd).splitlines()
    if liveness == "alive":
        report["proposal"] = ("the previous session is alive: continue in it from the Orca sidebar, or, if the "
                              "person says it is abandoned, confirm with --replace to launch another agent")
    elif liveness == "unknown":
        report["proposal"] = ("Orca cannot say whether the previous session is alive; if it is gone, confirm to "
                              "launch a new agent in the same workspace")
    else:
        report["proposal"] = "launch a new agent in the same workspace (confirm first)"
    return report
