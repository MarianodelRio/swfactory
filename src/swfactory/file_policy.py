"""Shared scope and freshness policy for project memory; executable files stay behavioral."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

DEFAULT_PATHS = ["docs/*.md", "docs/**/*.md", "docs/*.rst", "docs/**/*.rst"]


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"paths": cfg.get("memory", {}).get("paths", DEFAULT_PATHS),
            "behavior_paths": cfg.get("memory", {}).get("behavior_paths", [])}


def memory(path: str, cfg: dict[str, Any]) -> bool:
    """Permission to document lessons is separate from permission to change behavior."""
    return (PurePosixPath(path).suffix.lower() in (".md", ".rst", ".txt")
            and any(fnmatch.fnmatchcase(path, p) for p in settings(cfg)["paths"]))


def behavioral(path: str, cfg: dict[str, Any]) -> bool:
    if any(fnmatch.fnmatchcase(path, p) for p in settings(cfg)["behavior_paths"]):
        return True
    if path.startswith(".factory/state/") or path.startswith(".factory/changes/"):
        return False  # live state is ignored; approved designs have their own digest
    if path.startswith(".factory/") and PurePosixPath(path).suffix == ".md":
        return False
    return not memory(path, cfg)


def digest(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(settings(cfg), sort_keys=True).encode()).hexdigest()
