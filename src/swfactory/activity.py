"""What runs in each task workspace: Orca terminals, the agents Orca's hooks report, and the
subagents of Claude and Codex. Read-only and best effort: a source that cannot be read is
reported as unavailable, never raised. Orca's hook files and the agents' session folders are
internal formats of those tools, so every field is read defensively."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swfactory import orca, system
from swfactory.output import FactoryError, mask_secrets
from swfactory.state import State

ACTIVE_SECONDS = 30  # a terminal that wrote less than this ago is "active"
TEXT_LIMIT = 160
CODEX_DAYS = 3  # Codex sessions older than this are not scanned
TERMINAL_LIMIT = "1000"
INDEX_SECONDS = 2.0  # one scan of the Codex sessions serves every task of one board refresh
HOOK_DIRS = (".config/orca/agent-hooks", "Library/Application Support/orca/agent-hooks",
             "AppData/Roaming/orca/agent-hooks")
SUBAGENT_EVENTS = ("SubagentStart", "SubagentStop")
# The agent's state after each of its own hook events (Orca keeps no status for ended sessions).
TURN_STATES = {"SessionStart": "idle", "UserPromptSubmit": "working", "Stop": "done", "SessionEnd": "ended"}


def iso_ms(value: Any) -> str | None:
    """A millisecond epoch (Orca's timestamps) as the state's ISO format."""
    if not isinstance(value, int | float) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_s(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(mask_secrets(value).split())
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


# --- Orca: workspaces and terminals (documented CLI) ---------------------------------------------


def orca_view() -> dict[str, Any]:
    """Every Orca workspace and live terminal, in two calls. `ok` is False when Orca cannot answer."""
    view: dict[str, Any] = {"ok": True, "error": None, "worktrees": {}, "terminals": [], "complete": True}
    try:
        view["worktrees"] = {w["worktreeId"]: w for w in orca.run(["worktree", "ps"])["worktrees"]}
        listed = orca.run(["terminal", "list", "--limit", TERMINAL_LIMIT])
        view["terminals"] = list(listed["terminals"])
        view["complete"] = not listed.get("truncated")
    except (FactoryError, KeyError, TypeError) as exc:
        view.update(ok=False, error=getattr(exc, "message", str(exc)), worktrees={}, terminals=[])
    return view


def liveness(record: State, view: dict[str, Any]) -> str:
    """Like `orca.session_liveness`, from the terminal list instead of one call per task."""
    handle = record["attempts"][-1].get("terminal") if record["attempts"] else None
    if not view["ok"] or not handle:
        return "unknown"
    terminal = next((t for t in view["terminals"] if t.get("handle") == handle), None)
    if terminal is None and not view.get("complete", True):
        return "unknown"  # Orca cut the list short: absent is not proof of closed
    return "alive" if terminal and terminal.get("connected") else "finished"


def workspace_missing(record: State, view: dict[str, Any]) -> bool | None:
    if not view["ok"]:
        return None
    return record["workspace"]["orca_id"] not in view["worktrees"]


# --- Orca's agent hooks (internal files) ----------------------------------------------------------

_spool_cache: dict[Path, tuple[float, int, tuple[dict[str, Any] | None, list[dict[str, Any]]]]] = {}


def orca_hooks(home: Path) -> dict[str, Any]:
    """The agents Orca's hooks saw, by terminal pane (`leafId`), and every subagent start/stop."""
    candidates = [home / d for d in HOOK_DIRS]
    if os.environ.get("APPDATA"):  # Windows keeps app data here, not always under the home folder
        candidates.append(Path(os.environ["APPDATA"]) / "orca" / "agent-hooks")
    folder = next((path for path in candidates if path.is_dir()), None)
    result: dict[str, Any] = {"ok": folder is not None, "panes": {}, "subagents": []}
    if folder is None:
        return result
    try:
        entries = json.loads((folder / "last-status.json").read_text(encoding="utf-8")).get("entries", {})
    except (OSError, ValueError, AttributeError):
        entries = {}
    for key, entry in entries.items() if isinstance(entries, dict) else []:
        if not isinstance(entry, dict):
            continue
        payload: dict[str, Any] = entry["payload"] if isinstance(entry.get("payload"), dict) else {}
        result["panes"][str(key).rsplit(":", 1)[-1]] = {
            "provider": entry.get("source"),
            "state": payload.get("state"),
            "prompt": _text(payload.get("prompt")),
            "at": iso_ms(entry.get("receivedAt")),
        }
    spool = sorted((folder / "spool").glob("pane-*.jsonl"))
    for gone in set(_spool_cache) - set(spool):
        del _spool_cache[gone]
    for path in spool:
        pane, subagents = _spool(path)
        result["subagents"] += subagents
        leaf = path.stem.removeprefix("pane-")
        if pane and leaf not in result["panes"]:
            result["panes"][leaf] = pane
    return result


def _spool(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """One pane's spool file: its agent's last state and its subagent events. Cached while the
    file does not change."""
    try:
        stat = path.stat()
    except OSError:
        return None, []
    cached = _spool_cache.get(path)
    if cached and cached[:2] == (stat.st_mtime, stat.st_size):
        return cached[2]
    pane: dict[str, Any] | None = None
    subagents = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
            payload = event["payload"]
            name = payload["hook_event_name"]
        except (ValueError, KeyError, TypeError):
            continue
        if name in TURN_STATES:
            pane = {"provider": event.get("source"), "state": TURN_STATES[name], "at": iso_ms(event.get("receivedAt")),
                    "prompt": _text(payload.get("prompt")) or (pane or {}).get("prompt")}
        elif name in SUBAGENT_EVENTS and payload.get("agent_type"):  # untyped ones are the client's own
            subagents.append({"event": name, "provider": event.get("source"), "cwd": payload.get("cwd"),
                              "id": payload.get("agent_id"), "type": payload.get("agent_type"),
                              "at": iso_ms(event.get("receivedAt")),
                              "tasks": payload.get("background_tasks"),
                              "last": _text(payload.get("last_assistant_message"))})
    _spool_cache[path] = (stat.st_mtime, stat.st_size, (pane, subagents))
    return pane, subagents


# --- Agents' own session folders (internal formats) -----------------------------------------------


_claude_cache: dict[Path, tuple[float, float, dict[str, Any] | None]] = {}


def claude_project_dir(home: Path, workspace: Path) -> Path:
    """Where Claude Code keeps every session run in `workspace`."""
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))


def claude_subagents(home: Path, workspace: Path) -> list[dict[str, Any]]:
    """Subagents Claude Code recorded for sessions run in `workspace`."""
    project = claude_project_dir(home, workspace)
    found = []
    for meta_path in sorted(project.glob("*/subagents/*.meta.json")):
        sub = _claude_subagent(meta_path)
        if sub:
            found.append(sub)
    return found


def _claude_subagent(meta_path: Path) -> dict[str, Any] | None:
    """One subagent from its `.meta.json` and transcript, cached while neither changes."""
    transcript = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".jsonl")
    try:
        meta_time = meta_path.stat().st_mtime
        last = transcript.stat().st_mtime if transcript.exists() else meta_time
    except OSError:
        return None
    cached = _claude_cache.get(meta_path)
    if cached and cached[:2] == (meta_time, last):
        return cached[2]
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    sub = None
    if isinstance(meta, dict):
        sub = {"id": meta_path.name.removesuffix(".meta.json").removeprefix("agent-"),
               "provider": "claude", "type": meta.get("agentType"),
               "label": _text(meta.get("description")), "running": not meta.get("finished", False),
               "started": _first_timestamp(transcript), "last_activity": _iso_s(last)}
    _claude_cache[meta_path] = (meta_time, last, sub)
    return sub


def _first_timestamp(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stamp = json.loads(line).get("timestamp")
                if isinstance(stamp, str):
                    return stamp[:19] + "Z"
    except (OSError, ValueError, AttributeError):
        pass
    return None


_codex_index: dict[Path, tuple[float, dict[str, list[dict[str, Any]]]]] = {}


def codex_sessions(home: Path, workspace: Path) -> list[dict[str, Any]]:
    """Recent Codex sessions run in `workspace`; those whose `source` names a subagent count as
    subagents. Not verified against a real Codex: unreadable or unknown files are skipped."""
    root = home / ".codex" / "sessions"
    cached = _codex_index.get(root)
    if cached is None or time.monotonic() - cached[0] > INDEX_SECONDS:
        cached = (time.monotonic(), _scan_codex(root))
        _codex_index[root] = cached
    return cached[1].get(system.path_key(workspace), [])


def _scan_codex(root: Path) -> dict[str, list[dict[str, Any]]]:
    if not root.is_dir():
        return {}
    since = time.time() - CODEX_DAYS * 86400
    by_cwd: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(root.glob("*/*/*/rollout-*.jsonl")):
        try:
            modified = path.stat().st_mtime
            if modified < since:
                continue
            with path.open(encoding="utf-8") as handle:
                meta = json.loads(handle.readline()).get("payload", {})
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(meta, dict) or not isinstance(meta.get("cwd"), str):
            continue
        by_cwd.setdefault(system.path_key(meta["cwd"]), []).append({
            "id": meta.get("id") or path.stem, "provider": "codex",
            "subagent": "subagent" in json.dumps(meta.get("source", "")).lower(),
            "label": _text(meta.get("agent_nickname") or meta.get("agent_role")),
            "started": meta.get("timestamp"), "last_activity": _iso_s(modified),
            "running": time.time() - modified < 2 * ACTIVE_SECONDS})
    return by_cwd


# --- Everything that runs in one workspace --------------------------------------------------------


def runs(workspace: Path, record: State, view: dict[str, Any], hooks: dict[str, Any], home: Path,
         now: float | None = None) -> list[dict[str, Any]]:
    """Terminals (with the agent in each), then subagents, for one task workspace."""
    now = time.time() if now is None else now
    lead = record["attempts"][-1].get("terminal") if record["attempts"] else None
    found: list[dict[str, Any]] = []
    for terminal in view["terminals"]:
        if not system.same_path(terminal.get("worktreePath"), workspace):
            continue
        pane = hooks["panes"].get(str(terminal.get("leafId")))
        last = terminal.get("lastOutputAt")
        recent = isinstance(last, int | float) and now - last / 1000 < ACTIVE_SECONDS
        if not terminal.get("connected"):
            state = "closed"
        elif pane and pane["state"]:
            state = str(pane["state"])
        else:
            state = "active" if recent else "idle"
        found.append({"kind": "agent" if pane else "terminal", "provider": pane["provider"] if pane else None,
                      "role": "lead" if terminal.get("handle") == lead else None,
                      "label": _text(terminal.get("title")), "state": state, "active": bool(recent),
                      "last_activity": iso_ms(last), "detail": (pane or {}).get("prompt")})
    found += _subagents(workspace, hooks, home)
    return found


def _subagents(workspace: Path, hooks: dict[str, Any], home: Path) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for event in hooks["subagents"]:
        if not system.same_path(event["cwd"], workspace) or not event["id"]:
            continue
        item = merged.setdefault(event["id"], {"kind": "subagent", "provider": event["provider"],
                                               "type": event["type"], "label": None, "state": "running",
                                               "started": None, "last_activity": None, "detail": None})
        if event["event"] == "SubagentStart":
            item["started"] = event["at"]
        else:
            item.update(state="done", last_activity=event["at"], detail=event["last"])
        for task in event["tasks"] if isinstance(event["tasks"], list) else []:
            if isinstance(task, dict) and task.get("id") == event["id"]:
                item["label"] = item["label"] or _text(task.get("description"))
    for sub in claude_subagents(home, workspace):
        item = merged.setdefault(sub["id"], {"kind": "subagent", "provider": "claude", "type": sub["type"],
                                             "label": None, "state": "running", "started": None,
                                             "last_activity": None, "detail": None})
        item["label"] = sub["label"] or item["label"]
        item["started"] = item["started"] or sub["started"]
        item["last_activity"] = item["last_activity"] or sub["last_activity"]
        if not sub["running"]:
            item["state"] = "done"
    for session in codex_sessions(home, workspace):
        if session["subagent"]:
            merged.setdefault(session["id"], {"kind": "subagent", "provider": "codex", "type": None,
                                              "label": session["label"],
                                              "state": "running" if session["running"] else "done",
                                              "started": session["started"],
                                              "last_activity": session["last_activity"], "detail": None})
    return sorted(merged.values(), key=lambda s: s["started"] or "")
