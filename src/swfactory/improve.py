"""`swfactory improve extract` / `swfactory improve audit`: deterministic evidence for improving
swfactory itself.

Read-only, no LLM calls. `extract` reads `.factory/state/` (reused via `TaskStore`, already
authoritative for attempts and reviews) and, for each task's workspace, the matching Claude Code /
Codex session files on disk — the same matching primitives `activity.py` already uses
(`activity.claude_project_dir`, `system.path_key`), read at full depth instead of the live board's
thin slice. `audit` extends `memory.py`'s pattern of plain structural checks to swfactory's own
shipped `templates/skills/*`.

Every field from an agent's session file is read defensively: a key that is missing or
differently shaped is a coverage gap, never assumed to be zero (Codex's schema drifts across
`codex-cli` versions; its reasoning content is usually encrypted).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from importlib import resources
from pathlib import Path
from typing import Any

from swfactory import sessions, telemetry
from swfactory.output import mask_secrets
from swfactory.state import State, TaskStore
from swfactory.tasks import workspace_path

REPEATED_LIMIT = 20

SKILL_MD_MAX_LINES = 120
REFERENCES_MAX_LINES = 300
DUPLICATE_MIN_WORDS = 8

EmptyEvidence: dict[str, Any] = {
    "sessions": 0, "tool_calls": {}, "repeated_failing_commands": [], "tokens": {}, "subagents": 0,
}


# --- extract --------------------------------------------------------------------------------------


def extract(task_ids: list[str], store: TaskStore, home: Path) -> dict[str, Any]:
    """One evidence record per task id, plus a coverage summary. A task id is never dropped, even
    with no resolvable state or session."""
    index = sessions.codex_index(home)
    tasks = [_extract_one(task_id, store, home, index) for task_id in task_ids]
    missing = [t["task"] for t in tasks if t["coverage"] == "missing"]
    summary = {"requested": len(task_ids), "resolvable": len(task_ids) - len(missing), "missing": missing}
    safe: dict[str, Any] = json.loads(mask_secrets(json.dumps({"tasks": tasks, "coverage_summary": summary})))
    return safe


def _extract_one(task_id: str, store: TaskStore, home: Path,
                 index: list[dict[str, Any]]) -> dict[str, Any]:
    record = store.read_task(task_id)
    if record is None:
        return {"task": task_id, "coverage": "missing", "coverage_gaps": ["no task state recorded for this task"]}

    gaps: list[str] = []
    workspace = _workspace_path(record)
    if workspace is None:
        gaps.append("no workspace recorded for this task")
        claude: dict[str, Any] = dict(EmptyEvidence)
        codex: dict[str, Any] = dict(EmptyEvidence)
    else:
        registered = record.get("sessions", [])
        claude = _claude_evidence(home, workspace, gaps, registered)
        selected = sessions.select_codex(index, workspace, registered, gaps)
        codex = telemetry.codex(selected, home, gaps)
        if not (claude["sessions"] or claude["subagents"] or codex["sessions"]):
            gaps.append("no resolvable Claude or Codex session for this task's workspace")
        registered_ids = {(s["provider"], s["id"]) for s in registered}
        inferred_roots = any((provider, source["id"]) not in registered_ids and not source.get("parent")
                             for provider, data in (("claude", claude), ("codex", codex))
                             for source in data.get("sources", []))
        if not registered or inferred_roots:
            gaps.append("session membership is inferred from workspace paths; external validation may be missing")

    lifecycle = _lifecycle(record, store)
    if lifecycle["event_coverage"]["malformed_lines"]:
        gaps.append("task event history contains malformed lines; lifecycle counts are partial")
    if not lifecycle["event_coverage"]["available"]:
        gaps.append("task event history is unavailable; event-derived lifecycle counts are unknown")
    gaps = list(dict.fromkeys(gaps))
    if workspace is None or not (claude["sessions"] or claude["subagents"] or codex["sessions"]):
        coverage = "missing"
    else:
        coverage = "partial" if gaps else "full"

    return {
        "task": task_id,
        "coverage": coverage,
        "coverage_gaps": gaps,
        "lifecycle": lifecycle,
        "session_membership": "registered_and_discovered" if record.get("sessions") else "inferred",
        "evidence": {"claude": claude, "codex": codex},
    }


def _workspace_path(record: State) -> Path | None:
    try:
        return workspace_path(record)
    except (KeyError, TypeError, IndexError):
        return None


def _lifecycle(record: State, store: TaskStore) -> dict[str, Any]:
    attempts = record.get("attempts") or []
    reviews = record.get("reviews") or []
    by_type: dict[str, dict[str, Any]] = {}
    for review in reviews:
        entry = by_type.setdefault(review.get("type", "unknown"), {"rounds": 0, "verdicts": []})
        entry["rounds"] += 1
        entry["verdicts"].append(review.get("verdict"))
    events: Counter[str] = Counter()
    malformed = 0
    try:
        lines = (store.root / f"{record['task']}.events.jsonl").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(event, dict) and isinstance(event.get("event"), str):
            events[event["event"]] += 1
    return {
        "unblocks": events["unblock"] if lines else None,
        "recovery_events": events["recover"] if lines else None,
        "event_coverage": {"available": bool(lines), "malformed_lines": malformed},
        "review_requests": len(record.get("review_runs", [])),
        "review_results_recorded": len(reviews),
        "review_failures_applicable": sum(r.get("verdict") == "fail" and not r.get("historical") for r in reviews),
        "review_results_historical": sum(bool(r.get("historical")) for r in reviews),
        "review_period": record.get("review_period", 0),
        "review_diagnosis": record.get("review_diagnosis"),
        "review_history_scope": "recorded results only; unrecorded historical reviews are unknown",
        "state": record.get("state"),
        "blocked_reason": record.get("blocked_reason"),
        "attempts": len(attempts),
        "recoveries": max(0, len(attempts) - 1),
        "agents": [a.get("agent") for a in attempts],
        "models": [a.get("model") for a in attempts],
        "reviews": by_type,
        "review_rounds_total": len(reviews),
    }


def _repeated_failing(counts: Counter[str], failures: Counter[str]) -> list[dict[str, Any]]:
    keys = {c for c, n in counts.items() if n >= 2} | set(failures)
    triples = sorted(((c, counts.get(c, 0), failures.get(c, 0)) for c in keys), key=lambda t: (-t[2], -t[1]))
    return [{"command": c, "count": count, "failures": fails} for c, count, fails in triples[:REPEATED_LIMIT]]


# --- Claude Code sessions --------------------------------------------------------------------------


def _claude_evidence(home: Path, workspace: Path, gaps: list[str],
                     registered: list[dict[str, Any]]) -> dict[str, Any]:
    roots, subagent_files = sessions.claude_files(home, workspace, registered, gaps)

    tool_calls: Counter[str] = Counter()
    command_counts: Counter[str] = Counter()
    command_failures: Counter[str] = Counter()
    cost: dict[str, Any] = {"cost_usd": 0.0, "models": {}}
    usage_only: dict[str, float] = {}
    files_with_cost_state = 0
    files_usage_only = 0
    root_cost_files = 0
    child_costs: list[dict[str, Any]] = []
    raw_calls: Counter[str] = Counter()
    seen_tools: set[tuple[str, str]] = set()
    seen_usage: dict[tuple[str, str], dict[str, float]] = {}
    pending: dict[str, str] = {}
    damaged: list[str] = []
    for path in [*roots, *subagent_files]:
        family = path.parent.parent.name if path in subagent_files else path.stem
        file_cost: dict[str, Any] = {"cost_usd": 0.0, "models": {}}
        has_cost_state, file_usage = _parse_claude_transcript(
            path, tool_calls, command_counts, command_failures, file_cost,
            family, raw_calls, seen_tools, seen_usage, pending, damaged,
        )
        if has_cost_state:
            if path in subagent_files:
                child_costs.append({"file": str(path), **file_cost})
            else:
                root_cost_files += 1
                cost["cost_usd"] += file_cost["cost_usd"]
                for model, values in file_cost["models"].items():
                    target = cost["models"].setdefault(model, {})
                    for key, value in values.items():
                        target[key] = target.get(key, 0) + value
        if has_cost_state:
            files_with_cost_state += 1
        else:
            files_usage_only += 1
            for key, value in file_usage.items():
                usage_only[key] = usage_only.get(key, 0) + value

    if (roots or subagent_files) and files_usage_only:
        if files_with_cost_state:
            gaps.append(
                f"{files_usage_only} of {files_with_cost_state + files_usage_only} Claude session/subagent files "
                "have no cost-state entry; their usage is reported separately under tokens.usage_only_tokens and "
                "is not included in tokens.cost_usd"
            )
        else:
            gaps.append(
                "no cost-state entry in Claude sessions for this task; tokens are summed from per-message usage"
            )

    if root_cost_files:
        tokens = {**cost, "files_with_cost_state": files_with_cost_state}
        if files_usage_only:
            tokens["files_usage_only"] = files_usage_only
            tokens["usage_only_tokens"] = usage_only
    else:
        tokens = {"estimated_from_usage": True, **usage_only}

    for cmd in pending.values():
        command_counts[cmd] += 1
    if pending:
        gaps.append(f"{len(pending)} Claude commands have no recorded result")
    if child_costs:
        gaps.append("Claude child cost states are listed separately; their overlap with root costs is unknown")
    gaps.extend(damaged)
    tokens["scope"] = "reported root-session totals; not a complete provider bill"
    if child_costs:
        tokens["subagent_cost_states"] = child_costs
    return {
        "raw_tool_calls": dict(raw_calls),
        "duplicate_tool_records": sum(raw_calls.values()) - sum(tool_calls.values()),
        "command_outcomes": {"success": sum(command_counts.values()) - len(pending) - sum(command_failures.values()),
                             "failure": sum(command_failures.values()), "unknown": 0, "pending": len(pending)},
        "sources": [{"path": str(p), "id": p.stem, "parent": p.parent.parent.name if p in subagent_files else None}
                    for p in [*roots, *subagent_files]],
        "coverage": {"calls": {"raw": sum(raw_calls.values()), "unique": sum(tool_calls.values()),
                               "status": "partial" if damaged else "observed"},
                     "command_results": {"known": sum(command_counts.values()) - len(pending),
                                         "pending": len(pending), "total": sum(command_counts.values())},
                     "usage": {"root_cost_files": root_cost_files, "root_files": len(roots),
                               "usage_only_files": files_usage_only, "aggregation": "root_sessions_only"}},
        "sessions": len(roots),
        "tool_calls": dict(tool_calls),
        "repeated_failing_commands": _repeated_failing(command_counts, command_failures),
        "tokens": tokens,
        "subagents": len(subagent_files),  # already parsed above; avoid re-reading each transcript
    }


def _parse_claude_transcript(
    path: Path, tool_calls: Counter[str], command_counts: Counter[str], command_failures: Counter[str],
    cost: dict[str, Any], family: str, raw_calls: Counter[str], seen_tools: set[tuple[str, str]],
    seen_usage: dict[tuple[str, str], dict[str, float]], pending: dict[str, str], damaged: list[str],
) -> tuple[bool, dict[str, float]]:
    """Parse one Claude Code transcript (a session or a subagent's). Returns whether a `cost-state`
    entry was found, and this file's own per-message usage (meaningful only when it wasn't — a file
    with a cost-state entry has its cost added to `cost` instead, never both, to avoid double-counting).
    Any unreadable or malformed line is skipped, never raised."""
    empty_usage: dict[str, float] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        damaged.append(f"cannot read Claude file {path.name}")
        return False, empty_usage
    last_cost_state: dict[str, Any] | None = None
    file_usage = dict(empty_usage)
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            damaged.append(f"malformed line in Claude file {path.name}")
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "cost-state":
            last_cost_state = event  # each entry is a running total for the whole file, not a delta
        elif etype == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use"
                                                 and not b.get("id") for b in content):
                damaged.append(f"Claude file {path.name} has calls without ids; deduplication is partial")
            _accumulate_assistant(event, tool_calls, file_usage, pending, family, raw_calls, seen_tools, seen_usage)
        elif etype == "user":
            _accumulate_user_result(event, pending, command_counts, command_failures, family)
    if last_cost_state is not None:
        if (type(last_cost_state.get("totalCostUSD")) not in (int, float)
                or last_cost_state.get("hasUnknownModelCost")):
            damaged.append(f"Claude file {path.name} has incomplete cost-state data")
            return False, file_usage
        _accumulate_cost_state(last_cost_state, cost)
        return True, empty_usage
    return False, file_usage


def _accumulate_cost_state(event: dict[str, Any], cost: dict[str, Any]) -> None:
    total = event.get("totalCostUSD")
    if isinstance(total, int | float):
        cost["cost_usd"] += total
    usage_by_model = event.get("modelUsage")
    if not isinstance(usage_by_model, dict):
        return
    for model, usage in usage_by_model.items():
        if not isinstance(usage, dict):
            continue
        entry = cost["models"].setdefault(model, {})
        for key in ("inputTokens", "outputTokens", "thinkingTokens", "cacheReadInputTokens",
                    "cacheCreationInputTokens", "costUSD"):
            value = usage.get(key)
            if isinstance(value, int | float):
                entry[key] = entry.get(key, 0) + value


def _accumulate_assistant(
    event: dict[str, Any], tool_calls: Counter[str], fallback: dict[str, float], pending: dict[str, str],
    family: str, raw_calls: Counter[str], seen_tools: set[tuple[str, str]],
    seen_usage: dict[tuple[str, str], dict[str, float]],
) -> None:
    message = event.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if isinstance(usage, dict):
        message_id = message.get("id") or event.get("uuid")
        usage_key = (family, str(message_id)) if message_id else None
        previous = seen_usage.setdefault(usage_key, {}) if usage_key else {}
        fields = (("input_tokens", "input"), ("output_tokens", "output"),
                  ("cache_read_input_tokens", "cache_read"), ("cache_creation_input_tokens", "cache_creation"))
        for key, target in fields:
            value = usage.get(key)
            if isinstance(value, int | float):
                delta = max(0, value - previous.get(target, 0))
                fallback[target] = fallback.get(target, 0) + delta
                previous[target] = max(value, previous.get(target, 0))
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "unknown"
        raw_calls[name] += 1
        tool_id, block_input = block.get("id"), block.get("input")
        if tool_id:
            tool_key = (family, str(tool_id))
            if tool_key in seen_tools:
                continue
            seen_tools.add(tool_key)
        tool_calls[name] += 1
        if name == "Bash" and tool_id and isinstance(block_input, dict) and block_input.get("command"):
            pending[f"{family}:{tool_id}"] = mask_secrets(str(block_input["command"]))


def _accumulate_user_result(
    event: dict[str, Any], pending: dict[str, str], command_counts: Counter[str], command_failures: Counter[str],
    family: str,
) -> None:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            continue
        command = pending.pop(f"{family}:{tool_use_id}", None)
        if not command:
            continue
        command_counts[command] += 1
        if block.get("is_error"):
            command_failures[command] += 1


# --- audit ------------------------------------------------------------------------------------------


def audit(base: Any = None) -> dict[str, Any]:
    """Structural checks over swfactory's own shipped `templates/skills/*` (the same source
    `init.py`'s `_skill_files()` walks), extending `memory.py`'s plain, no-LLM pattern. `base`
    overrides the skills folder (for tests); the real command always reads the shipped package."""
    base = base if base is not None else resources.files("swfactory").joinpath("templates/skills")
    findings: list[dict[str, str]] = []
    seen_lines: dict[str, tuple[str, str]] = {}
    for skill in sorted(p.name for p in base.iterdir() if p.is_dir()):
        skill_dir = base.joinpath(skill)
        text = skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) > SKILL_MD_MAX_LINES:
            findings.append(_finding("skill_too_large", f"{skill}/SKILL.md",
                                     f"{len(lines)} lines (keep it within {SKILL_MD_MAX_LINES})"))
        description = _frontmatter_description(text)
        if description and "references/" in description:
            findings.append(_finding("unconditional_reference", f"{skill}/SKILL.md",
                                     "the frontmatter description names a references/ path, which loads for "
                                     "every skill-matching decision, not only when the skill actually runs"))
        findings += _check_duplicates(skill, "SKILL.md", lines, seen_lines)

        refs = skill_dir.joinpath("references")
        if refs.is_dir():
            for ref in sorted(p for p in refs.iterdir() if p.name.endswith(".md")):
                ref_lines = ref.read_text(encoding="utf-8").splitlines()
                rel = f"{skill}/references/{ref.name}"
                if len(ref_lines) > REFERENCES_MAX_LINES:
                    findings.append(_finding("skill_too_large", rel,
                                             f"{len(ref_lines)} lines (keep it within {REFERENCES_MAX_LINES})"))
                findings += _check_duplicates(skill, f"references/{ref.name}", ref_lines, seen_lines)
    return {"findings": findings}


def _frontmatter_description(text: str) -> str | None:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return None


def _check_duplicates(
    skill: str, filename: str, lines: list[str], seen: dict[str, tuple[str, str]],
) -> list[dict[str, str]]:
    found = []
    for line in lines:
        normalized = re.sub(r"\s+", " ", line.strip().lower())
        if len(normalized.split()) < DUPLICATE_MIN_WORDS:
            continue
        previous = seen.get(normalized)
        if previous and previous[0] != skill:
            found.append(_finding("duplicated_instruction", f"{skill}/{filename}",
                                  f"shares a line with {previous[0]}/{previous[1]}: \"{normalized[:80]}\""))
        else:
            seen[normalized] = (skill, filename)
    return found


def _finding(check: str, path: str, detail: str) -> dict[str, str]:
    return {"check": check, "file": path, "detail": detail}
