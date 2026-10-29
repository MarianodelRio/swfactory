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
import sqlite3
from collections import Counter
from importlib import resources
from pathlib import Path
from typing import Any

from swfactory import activity, system
from swfactory.output import mask_secrets
from swfactory.state import State, TaskStore
from swfactory.tasks import workspace_path

CODEX_TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
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
    tasks = [_extract_one(task_id, store, home) for task_id in task_ids]
    missing = [t["task"] for t in tasks if t["coverage"] == "missing"]
    summary = {"requested": len(task_ids), "resolvable": len(task_ids) - len(missing), "missing": missing}
    return {"tasks": tasks, "coverage_summary": summary}


def _extract_one(task_id: str, store: TaskStore, home: Path) -> dict[str, Any]:
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
        claude = _claude_evidence(home, workspace, gaps)
        codex = _codex_evidence(home, workspace, gaps)
        if claude["sessions"] == 0 and codex["sessions"] == 0:
            gaps.append("no resolvable Claude or Codex session for this task's workspace")

    if workspace is None or (claude["sessions"] == 0 and codex["sessions"] == 0):
        coverage = "missing"
    else:
        coverage = "partial" if gaps else "full"

    return {
        "task": task_id,
        "coverage": coverage,
        "coverage_gaps": gaps,
        "lifecycle": _lifecycle(record),
        "evidence": {"claude": claude, "codex": codex},
    }


def _workspace_path(record: State) -> Path | None:
    try:
        return workspace_path(record)
    except (KeyError, TypeError, IndexError):
        return None


def _lifecycle(record: State) -> dict[str, Any]:
    attempts = record.get("attempts") or []
    reviews = record.get("reviews") or []
    by_type: dict[str, dict[str, Any]] = {}
    for review in reviews:
        entry = by_type.setdefault(review.get("type", "unknown"), {"rounds": 0, "verdicts": []})
        entry["rounds"] += 1
        entry["verdicts"].append(review.get("verdict"))
    return {
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


def _claude_evidence(home: Path, workspace: Path, gaps: list[str]) -> dict[str, Any]:
    project_dir = activity.claude_project_dir(home, workspace)
    sessions = sorted(project_dir.glob("*.jsonl")) if project_dir.is_dir() else []
    subagent_files = sorted(project_dir.glob("*/subagents/*.jsonl")) if project_dir.is_dir() else []

    tool_calls: Counter[str] = Counter()
    command_counts: Counter[str] = Counter()
    command_failures: Counter[str] = Counter()
    cost: dict[str, Any] = {"cost_usd": 0.0, "models": {}}
    usage_only: dict[str, float] = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    files_with_cost_state = 0
    files_usage_only = 0
    for path in [*sessions, *subagent_files]:
        has_cost_state, file_usage = _parse_claude_transcript(path, tool_calls, command_counts, command_failures, cost)
        if has_cost_state:
            files_with_cost_state += 1
        else:
            files_usage_only += 1
            for key, value in file_usage.items():
                usage_only[key] += value

    if sessions and files_usage_only:
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

    if files_with_cost_state:
        tokens = {**cost, "files_with_cost_state": files_with_cost_state}
        if files_usage_only:
            tokens["files_usage_only"] = files_usage_only
            tokens["usage_only_tokens"] = usage_only
    else:
        tokens = {"estimated_from_usage": True, **usage_only}

    return {
        "sessions": len(sessions),
        "tool_calls": dict(tool_calls),
        "repeated_failing_commands": _repeated_failing(command_counts, command_failures),
        "tokens": tokens,
        "subagents": len(subagent_files),  # already parsed above; avoid re-reading each transcript
    }


def _parse_claude_transcript(
    path: Path, tool_calls: Counter[str], command_counts: Counter[str], command_failures: Counter[str],
    cost: dict[str, Any],
) -> tuple[bool, dict[str, float]]:
    """Parse one Claude Code transcript (a session or a subagent's). Returns whether a `cost-state`
    entry was found, and this file's own per-message usage (meaningful only when it wasn't — a file
    with a cost-state entry has its cost added to `cost` instead, never both, to avoid double-counting).
    Any unreadable or malformed line is skipped, never raised."""
    empty_usage = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_creation": 0.0}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, empty_usage
    pending: dict[str, str] = {}  # tool_use id -> masked command (Bash only)
    last_cost_state: dict[str, Any] | None = None
    file_usage = dict(empty_usage)
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "cost-state":
            last_cost_state = event  # each entry is a running total for the whole file, not a delta
        elif etype == "assistant":
            _accumulate_assistant(event, tool_calls, file_usage, pending)
        elif etype == "user":
            _accumulate_user_result(event, pending, command_counts, command_failures)
    if last_cost_state is not None:
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
) -> None:
    message = event.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if isinstance(usage, dict):
        fields = (("input_tokens", "input"), ("output_tokens", "output"),
                  ("cache_read_input_tokens", "cache_read"), ("cache_creation_input_tokens", "cache_creation"))
        for key, target in fields:
            value = usage.get(key)
            if isinstance(value, int | float):
                fallback[target] += value
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "unknown"
        tool_calls[name] += 1
        tool_id, block_input = block.get("id"), block.get("input")
        if name == "Bash" and tool_id and isinstance(block_input, dict) and block_input.get("command"):
            pending[tool_id] = mask_secrets(str(block_input["command"]))


def _accumulate_user_result(
    event: dict[str, Any], pending: dict[str, str], command_counts: Counter[str], command_failures: Counter[str],
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
        command = pending.pop(tool_use_id, None)
        if not command:
            continue
        command_counts[command] += 1
        if block.get("is_error"):
            command_failures[command] += 1


# --- Codex sessions ---------------------------------------------------------------------------------


def _codex_evidence(home: Path, workspace: Path, gaps: list[str]) -> dict[str, Any]:
    root = home / ".codex" / "sessions"
    matches: list[tuple[Path, dict[str, Any]]] = []
    if root.is_dir():
        for path in sorted(root.glob("*/*/*/rollout-*.jsonl")):
            meta = _codex_meta(path)
            if meta and system.same_path(meta.get("cwd"), workspace):
                matches.append((path, meta))

    tool_calls: Counter[str] = Counter()
    command_counts: Counter[str] = Counter()
    command_failures: Counter[str] = Counter()
    unsupported_calls: Counter[str] = Counter()
    tokens: dict[str, float] = {}
    found_tokens = False
    for path, _meta in matches:
        found_tokens |= _parse_codex_rollout(
            path, tool_calls, command_counts, command_failures, unsupported_calls, tokens,
        )

    sqlite_tokens = _codex_sqlite_tokens(home, [meta for _, meta in matches])
    if sqlite_tokens is not None:
        tokens = sqlite_tokens
    elif matches and not found_tokens:
        gaps.append("no token fields found in Codex rollout files for this task (codex-cli schema drift?)")

    total_unsupported = sum(unsupported_calls.values())
    if total_unsupported:
        gaps.append(
            f"{total_unsupported} Codex tool call(s) use an argument format this extractor doesn't parse (e.g. "
            "execution code embedded as a script call rather than a `command` argument); their command text and "
            "success/failure are unknown, not absent from repeated_failing_commands — see unsupported_calls"
        )

    return {
        "sessions": len(matches),
        "tool_calls": dict(tool_calls),
        "repeated_failing_commands": _repeated_failing(command_counts, command_failures),
        "unsupported_calls": dict(unsupported_calls),
        "tokens": tokens,
        "subagents": sum(1 for _, meta in matches if _codex_is_subagent(meta)),
    }


def _codex_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            meta = json.loads(handle.readline()).get("payload", {})
    except (OSError, ValueError, AttributeError):
        return None
    return meta if isinstance(meta, dict) and isinstance(meta.get("cwd"), str) else None


def _codex_is_subagent(meta: dict[str, Any]) -> bool:
    return "subagent" in json.dumps(meta.get("source", "")).lower()


def _parse_codex_rollout(
    path: Path, tool_calls: Counter[str], command_counts: Counter[str], command_failures: Counter[str],
    unsupported_calls: Counter[str], tokens: dict[str, float],
) -> bool:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    pending: dict[str, str] = {}
    found_tokens = False
    for line in lines[1:]:  # the first line is the session meta, already read
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type") or (event.get("type") if isinstance(event, dict) else None)
        if kind in ("function_call", "custom_tool_call"):
            _codex_record_call(payload, tool_calls, pending, unsupported_calls)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            _codex_record_result(payload, pending, command_counts, command_failures)
        found_tokens |= _codex_accumulate_tokens(payload, tokens)
    return found_tokens


_JS_EXEC_COMMAND_RE = re.compile(
    r"tools\.exec_command\(\s*\{\s*cmd\s*:\s*(['\"])(?P<cmd>(?:\\.|(?!\1).)*)\1", re.DOTALL,
)


def _codex_extract_command(payload: dict[str, Any]) -> str | None:
    """The reliably parseable subset of Codex call-argument shapes: a `command` field (string or
    JSON-encoded string), or the observed `tools.exec_command({cmd: "..."})` script-call shape.
    Anything else is left unrecognized rather than guessed at or evaluated as code."""
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("command"):
            return str(parsed["command"])
    elif isinstance(arguments, dict) and arguments.get("command"):
        return str(arguments["command"])

    for field in ("arguments", "input"):
        value = payload.get(field)
        if isinstance(value, str):
            match = _JS_EXEC_COMMAND_RE.search(value)
            if match:
                return match.group("cmd").replace("\\\"", "\"").replace("\\'", "'")
    return None


def _codex_record_call(
    payload: dict[str, Any], tool_calls: Counter[str], pending: dict[str, str], unsupported_calls: Counter[str],
) -> None:
    name = payload.get("name") or "unknown"
    tool_calls[name] += 1
    call_id = payload.get("call_id") or payload.get("id")
    if not call_id:
        return
    command = _codex_extract_command(payload)
    if command:
        pending[call_id] = mask_secrets(command)
    else:
        unsupported_calls[name] += 1


def _codex_record_result(
    payload: dict[str, Any], pending: dict[str, str], command_counts: Counter[str], command_failures: Counter[str],
) -> None:
    call_id = payload.get("call_id") or payload.get("id")
    command = pending.pop(call_id, None) if call_id else None
    if not command:
        return
    command_counts[command] += 1
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        command_failures[command] += 1


def _codex_accumulate_tokens(payload: dict[str, Any], tokens: dict[str, float]) -> bool:
    usage = payload.get("usage")
    source = usage if isinstance(usage, dict) else payload
    found = False
    for field in CODEX_TOKEN_FIELDS:
        value = source.get(field)
        if isinstance(value, int | float):
            tokens[field] = tokens.get(field, 0) + value
            found = True
    return found


def _codex_sqlite_tokens(home: Path, metas: list[dict[str, Any]]) -> dict[str, float] | None:
    db_path = home / ".codex" / "state_5.sqlite"
    ids = {meta.get("id") for meta in metas if meta.get("id")}
    if not db_path.is_file() or not ids:
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT tokens_used FROM threads WHERE id IN ({placeholders})", list(ids)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    totals = [row[0] for row in rows if isinstance(row[0], int | float)]
    return {"total_tokens": sum(totals)} if totals else None


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
