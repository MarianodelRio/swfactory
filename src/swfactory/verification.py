"""Explicit execution identities and their acceptance/check obligations."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from swfactory.output import EXIT_PRECONDITION, FactoryError

NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


def reference(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    kind, name = next(iter(value.items()))
    if kind not in ("check", "verification") or not isinstance(name, str) or not NAME.fullmatch(name):
        return None
    return kind, name


def describe(value: Any) -> str:
    ref = reference(value)
    return f"{ref[0]}:{ref[1]}" if ref else str(value)


def plan(task: dict[str, Any], checks: dict[str, str]) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {
        f"check:{name}": {"id": f"check:{name}", "command": cmd, "checks": [name], "acceptance": []}
        for name, cmd in checks.items()}
    named = task.get("verification", {})
    if not isinstance(named, dict):
        raise FactoryError("verification_invalid", "verification must map names to commands", EXIT_PRECONDITION)
    for ac, how in task["acceptance"].items():
        if how == "manual":
            continue
        if isinstance(how, str) and how.strip():
            key, cmd = f"ac:{ac}", how
        elif (ref := reference(how)) is not None:
            kind, name = ref
            key = f"{kind}:{name}"
            source = checks if kind == "check" else named
            if name not in source:
                raise FactoryError("verification_invalid", f"{ac} references missing {kind} '{name}'",
                                   EXIT_PRECONDITION)
            cmd = source[name]
        else:
            raise FactoryError("verification_invalid", f"{ac} needs a command, manual or a named reference",
                               EXIT_PRECONDITION)
        if not isinstance(cmd, str) or not cmd.strip():
            raise FactoryError("verification_invalid", f"{ac} references an empty/non-command verification",
                               EXIT_PRECONDITION)
        entry = entries.setdefault(key, {"id": key, "command": cmd, "checks": [], "acceptance": []})
        entry["acceptance"].append(ac)
    return list(entries.values())


def digest(entries: list[dict[str, Any]]) -> str:
    text = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
