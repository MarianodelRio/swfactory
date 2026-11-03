"""Codex rollout adapter. Parse known envelopes; never execute transcript code."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from swfactory.output import mask_secrets

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")
EXEC_NAMES = {"exec", "shell", "exec_command", "functions.exec", "functions.exec_command"}
JS_COMMAND = re.compile(
    r"tools\.exec_command\(\s*\{\s*cmd\s*:\s*(['\"])(?P<cmd>(?:\\.|(?!\1).)*)\1(?=\s*[,}])", re.DOTALL)


def command(payload: dict[str, Any]) -> str | None:
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if isinstance(args, dict):
        value = args.get("command", args.get("cmd"))
        if isinstance(value, str) and value:
            return mask_secrets(value)
        if isinstance(value, list) and value and all(isinstance(s, str) for s in value):
            return mask_secrets(shlex.join(value))
    for field in ("arguments", "input"):
        script = payload.get(field)
        if isinstance(script, str):
            matches = list(JS_COMMAND.finditer(script))
            if len(matches) == 1 and script.count("tools.exec_command(") == 1:
                return mask_secrets(matches[0].group("cmd").replace('\\"', '"').replace("\\'", "'"))
    return None


def outcome(value: Any) -> str:
    """Exit status from a result envelope, never from arbitrary command stdout."""
    if isinstance(value, str):
        try:
            return outcome(json.loads(value))
        except ValueError:
            # A native execution summary precedes Output. Do not search the output itself.
            header = value.split("Output:", 1)[0]
            match = re.search(r"(?m)^(?:Process exited with code|Exit code:)\s*(-?\d+)\s*$", header)
            if match:
                return "success" if int(match[1]) == 0 else "failure"
            if re.match(r"^(?:Script|Process) running\b", header):
                return "pending"
            return "unknown"
    if isinstance(value, list):
        # Script runners place their summary first, followed by arbitrary printed output.
        # Only recognize a structured execution envelope in subsequent text blocks.
        parts = []
        for index, block in enumerate(value):
            if index and isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                try:
                    data = json.loads(block.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if (not isinstance(data, dict) or "exit_code" not in data
                        or not {"chunk_id", "wall_time_seconds", "session_id"}.intersection(data)):
                    continue
                parts.append(outcome(data))
            else:
                parts.append(outcome(block))
        known = [p for p in parts if p != "unknown"]
        return known[0] if len(known) == 1 else "unknown"
    if isinstance(value, dict):
        code = value.get("exit_code")
        if type(code) is int:
            return "success" if code == 0 else "failure"
        if value.get("session_id") is not None and code is None:
            return "pending"
        if value.get("type") in ("input_text", "text"):
            return outcome(value.get("text"))
        if "output" in value:
            return outcome(value["output"])
    return "unknown"


def _sqlite_usage(home: Path, ids: set[str]) -> dict[str, dict[str, float]]:
    path = home / ".codex/state_5.sqlite"
    if not path.is_file() or not ids:
        return {}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(f"SELECT id, tokens_used FROM threads WHERE id IN ({placeholders})", list(ids)).fetchall()
    except sqlite3.Error:
        return {}
    return {str(sid): {"total_tokens": float(value)} for sid, value in rows
            if type(value) in (int, float) and value >= 0}


def codex(rows: list[dict[str, Any]], home: Path, gaps: list[str]) -> dict[str, Any]:
    parents = {r["id"]: r["parent"] for r in rows}

    def root(sid: str) -> str:
        visited: set[str] = set()
        while parents.get(sid):
            if sid in visited:
                gaps.append("Codex session ancestry contains a cycle")
                break
            visited.add(sid)
            sid = parents[sid]
        return sid

    raw: Counter[str] = Counter()
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    results: dict[tuple[str, str], set[str]] = {}
    usage: dict[str, dict[str, float]] = {}
    broken = 0
    missing_ids = 0
    for row in rows:
        family = root(row["id"])
        try:
            lines = row["path"].read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            gaps.append(f"cannot read Codex session {row['id']}")
            continue
        latest_usage: dict[str, float] = {}
        for number, line in enumerate(lines[1:], start=2):
            try:
                event = json.loads(line)
            except ValueError:
                broken += 1
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") or event.get("type")
            call_id = payload.get("call_id") or payload.get("id")
            key = (family, str(call_id)) if call_id else (str(row["path"]), f"line-{number}")
            if kind in ("function_call", "custom_tool_call"):
                name = str(payload.get("name") or "unknown")
                raw[name] += 1
                if not call_id:
                    missing_ids += 1
                signature = json.dumps([name, payload.get("arguments"), payload.get("input")], sort_keys=True)
                if key in calls and calls[key]["signature"] != signature:
                    gaps.append(f"conflicting Codex call id {call_id}; kept separate")
                    key = (row["id"], str(call_id))
                calls.setdefault(key, {"name": name, "command": command(payload), "signature": signature,
                                       "session": row["id"]})
            elif kind in ("function_call_output", "custom_tool_call_output") and call_id:
                results.setdefault(key, set()).add(outcome(payload))
            elif kind == "token_count":
                info = payload.get("info")
                total = info.get("total_token_usage") if isinstance(info, dict) else None
                # Copied parent usage events precede the child's session creation.
                own = not row["parent"] or (row.get("timestamp") and event.get("timestamp")
                                             and event["timestamp"] >= row["timestamp"])
                if isinstance(total, dict) and own:
                    latest_usage = {k: float(total[k]) for k in TOKEN_FIELDS
                                    if type(total.get(k)) in (int, float) and total[k] >= 0}
            elif kind == "turn.completed" and isinstance(payload.get("usage"), dict):
                # A per-turn usage event is additive; cumulative snapshots above replace it.
                if row["parent"] and (not row.get("timestamp") or not event.get("timestamp")
                                      or event["timestamp"] < row["timestamp"]):
                    continue
                for key_name in TOKEN_FIELDS:
                    value = payload["usage"].get(key_name)
                    if type(value) in (int, float) and value >= 0:
                        latest_usage[key_name] = latest_usage.get(key_name, 0) + value
        if latest_usage:
            usage[row["id"]] = latest_usage

    sqlite_usage = _sqlite_usage(home, {r["id"] for r in rows})
    session_usage = []
    for row in {r["id"]: r for r in rows}.values():
        sid = row["id"]
        totals = sqlite_usage.get(sid) or usage.get(sid)
        source = "sqlite" if sid in sqlite_usage else "rollout"
        session_usage.append({"session": sid, "source": source if totals else None,
                              "tokens": totals, "parent": row["parent"]})
        if not totals:
            gaps.append(f"no token fields found for Codex session {sid} (codex-cli schema drift?)")
    tokens: dict[str, float] = {}
    for item in session_usage:
        if not item["parent"]:
            for field, value in (item["tokens"] or {}).items():
                tokens[field] = tokens.get(field, 0) + value
    if any(item["parent"] and item["tokens"] for item in session_usage):
        gaps.append("Codex child usage is reported per session, excluded from aggregate tokens "
                    "to avoid inherited totals")

    unique: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    for key, call in calls.items():
        unique[call["name"]] += 1
        cmd = call["command"]
        if cmd is None:
            if call["name"] in EXEC_NAMES:
                unsupported[call["name"]] += 1
            continue
        counts[cmd] += 1
        observed = results.get(key, set()) - {"unknown"}
        final = observed & {"success", "failure"}
        if len(final) == 1:
            status = final.pop()
        elif len(final) > 1:
            status = "unknown"
        else:
            status = "pending" if "pending" in observed or key not in results else "unknown"
        outcomes[status] += 1
        if status == "failure":
            failures[cmd] += 1
        elif status in ("unknown", "pending"):
            unknown[cmd] += 1
    if unsupported:
        gaps.append(f"{sum(unsupported.values())} Codex execution calls have unsupported arguments; "
                    "see unsupported_calls")
    if outcomes["unknown"] or outcomes["pending"]:
        gaps.append(f"Codex command outcomes: {outcomes['unknown']} unknown, {outcomes['pending']} pending")
    if broken or missing_ids:
        gaps.append(f"Codex evidence has {broken} malformed lines and {missing_ids} calls without ids")
    repeated = []
    for cmd in sorted(counts, key=lambda c: (-failures[c], -counts[c], c)):
        if counts[cmd] >= 2 or failures[cmd] or unknown[cmd]:
            item = {"command": cmd, "count": counts[cmd], "failures": failures[cmd]}
            if unknown[cmd]:
                item["unknown"] = unknown[cmd]
            repeated.append(item)
    known = outcomes["success"] + outcomes["failure"]
    return {"sessions": len({r["id"] for r in rows}), "tool_calls": dict(unique),
            "raw_tool_calls": dict(raw), "duplicate_tool_records": sum(raw.values()) - sum(unique.values()),
            "unsupported_calls": dict(unsupported), "repeated_failing_commands": repeated[:20],
            "command_outcomes": {s: outcomes[s] for s in ("success", "failure", "unknown", "pending")},
            "tokens": tokens, "usage_by_session": session_usage,
            "token_scope": "reported root-session totals; child totals listed separately; not billed cost",
            "subagents": sum(bool(r["parent"]) for r in {r["id"]: r for r in rows}.values()),
            "sources": [{**r, "path": str(r["path"])} for r in rows],
            "coverage": {"calls": {"status": "partial" if broken or missing_ids else "observed",
                                    "raw": sum(raw.values()), "unique": sum(unique.values())},
                         "command_results": {"known": known, "total": sum(outcomes.values()),
                                             "unknown": outcomes["unknown"], "pending": outcomes["pending"]},
                         "usage": {"known": sum(bool(s["tokens"]) for s in session_usage),
                                   "total": len(session_usage), "aggregation": "root_sessions_only"}}}
