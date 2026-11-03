"""Explicit task/session membership and read-only provider metadata discovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swfactory import activity, system
from swfactory.output import EXIT_CONFLICT, EXIT_PRECONDITION, FactoryError
from swfactory.state import TaskStore, now

PROVIDERS = ("claude", "codex")
ROLES = ("lead", "review", "validation", "subagent")


def attach(task_id: str, provider: str, session_id: str, role: str, parent: str | None,
           store: TaskStore) -> dict[str, Any]:
    if provider not in PROVIDERS or role not in ROLES:
        raise FactoryError("session_invalid", "unsupported provider or session role", EXIT_PRECONDITION)
    if store.read_task(task_id) is None:
        raise FactoryError("task_not_started", f"{task_id} has no recorded state", EXIT_PRECONDITION)
    if any(not sid.strip() or any(c in sid for c in ("/", "\\", "\n", "\r"))
           for sid in (session_id, parent) if sid is not None):
        raise FactoryError("session_invalid", "use the provider session id, not a file path", EXIT_PRECONDITION)
    if parent == session_id:
        raise FactoryError("session_invalid", "a session cannot be its own parent", EXIT_PRECONDITION)
    item = {"provider": provider, "id": session_id, "role": role, "parent": parent}

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        registered = state.setdefault("sessions", [])
        previous = next((s for s in registered if s["provider"] == provider and s["id"] == session_id), None)
        if previous and any(previous[k] != item[k] for k in item):
            raise FactoryError("session_conflict", "this session is already attached with different metadata",
                               EXIT_CONFLICT)
        if not previous:
            ancestry = {s["id"]: s.get("parent") for s in registered if s["provider"] == provider}
            ancestor = parent
            visited = {session_id}
            while ancestor:
                if ancestor in visited:
                    raise FactoryError("session_invalid", "session ancestry would contain a cycle", EXIT_PRECONDITION)
                visited.add(ancestor)
                ancestor = ancestry.get(ancestor)
            registered.append({**item, "attached_at": now()})
        return state

    state = store.update_task(task_id, apply, "session_attached")
    return {"task": task_id, "sessions": state["sessions"]}


def _parent(source: Any) -> str | None:
    if not isinstance(source, dict):
        return None
    sub = source.get("subagent")
    if not isinstance(sub, dict):
        return None
    spawn = sub.get("thread_spawn", sub)
    return str(spawn["parent_thread_id"]) if isinstance(spawn, dict) and spawn.get("parent_thread_id") else None


def codex_index(home: Path) -> list[dict[str, Any]]:
    indexed = []
    for path in sorted((home / ".codex/sessions").glob("*/*/*/rollout-*.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                meta = json.loads(handle.readline()).get("payload", {})
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(meta, dict) or not isinstance(meta.get("cwd"), str):
            continue
        indexed.append({"id": str(meta.get("id") or path.stem), "path": path, "cwd": meta["cwd"],
                        "parent": _parent(meta.get("source")), "source": meta.get("source"),
                        "timestamp": meta.get("timestamp")})
    return indexed


def select_codex(index: list[dict[str, Any]], workspace: Path, registered: list[dict[str, Any]],
                 gaps: list[str]) -> list[dict[str, Any]]:
    explicit = {s["id"]: s for s in registered if s["provider"] == "codex"}
    selected: dict[str, dict[str, Any]] = {}
    for row in index:
        if row["id"] in explicit or system.same_path(row["cwd"], workspace):
            declared_parent = explicit.get(row["id"], {}).get("parent")
            if row["parent"] and declared_parent and row["parent"] != declared_parent:
                gaps.append(f"Codex session {row['id']} has conflicting registered and provider parent ids")
            selected[str(row["path"])] = {**row, "parent": row["parent"] or declared_parent,
                                           "attribution": "registered" if row["id"] in explicit else "workspace",
                                           "role": explicit.get(row["id"], {}).get("role", "unknown")}
    while True:
        ids = {r["id"] for r in selected.values()}
        added = [{**r, "attribution": "descendant", "role": "subagent"} for r in index
                 if r["parent"] in ids and str(r["path"]) not in selected]
        if not added:
            break
        selected.update({str(r["path"]): r for r in added})
    found = {r["id"] for r in selected.values()}
    for missing in sorted(explicit.keys() - found):
        gaps.append(f"registered Codex session {missing} is missing")
    return list(selected.values())


def claude_files(home: Path, workspace: Path, registered: list[dict[str, Any]],
                 gaps: list[str]) -> tuple[list[Path], list[Path]]:
    project = activity.claude_project_dir(home, workspace)
    roots = set(project.glob("*.jsonl"))
    children = set(project.glob("*/subagents/*.jsonl"))
    explicit = {s["id"] for s in registered if s["provider"] == "claude"}
    if explicit:
        for path in (home / ".claude/projects").glob("*/*.jsonl"):
            if path.stem in explicit:
                roots.add(path)
        for path in (home / ".claude/projects").glob("*/*/subagents/*.jsonl"):
            if {path.stem, path.stem.removeprefix("agent-")} & explicit:
                children.add(path)
        found = {p.stem for p in roots | children} | {p.stem.removeprefix("agent-") for p in children}
        for missing in sorted(explicit - found):
            gaps.append(f"registered Claude session {missing} is missing")
    for path in roots:
        children.update(path.parent.glob(f"{path.stem}/subagents/*.jsonl"))
    return sorted(roots), sorted(children)
