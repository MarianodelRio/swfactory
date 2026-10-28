"""The models an agent can run, from Orca's model catalog, and turning what the person says
("opus 5.5", "GPT 6.1 Sol") into the provider's model id.

The catalog is an internal file of Orca (`agent-model-catalog.json` in its settings folder). If it
cannot be read, or does not list the agent, what the person said is completed to the provider's id
scheme when it follows it: Claude's `claude-<family>-<major>-<minor>` ("opus 5.5" is
`claude-opus-5-5`) and its aliases in lowercase (`Opus` is `opus`), and OpenAI's lowercase
`gpt-<version>-<codename>` ("GPT 6.1 Sol" and "Sol 6.1" are both `gpt-6.1-sol`). Any other name is
passed as the person wrote it (with spaces turned into dashes). A warning says so either way.

Raises FactoryError with these codes (EXIT_USAGE):
- model_unknown: the catalog lists the agent but no model matches; `details` lists them.
- model_ambiguous: several models match; `details` lists them.
- effort_unknown: the model does not take that reasoning effort; `details` lists the ones it does.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from swfactory.output import EXIT_USAGE, FactoryError

CATALOG_DIRS = (".config/orca", "Library/Application Support/orca", "AppData/Roaming/orca")
CATALOG_FILE = "agent-model-catalog.json"
# Claude's ids: claude-<family>-<major>[-<minor>][-<date>][1m]. Without the `claude-` prefix, only
# these families are completed.
CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")
CLAUDE_ID = re.compile(r"(?:claude-)?([a-z]+)-(\d+(?:-\d{1,2})*)(-\d{8})?(\[1m\])?")
# Claude Code's aliases (`default` resets to the account's model); each takes a `[1m]` suffix.
CLAUDE_ALIAS = re.compile(r"(?:default|best|fable|opus|sonnet|haiku|opusplan)(?:\[1m\])?")
# OpenAI's ids for Codex: gpt-<major>[.<minor>][-<tier or variant>...], always lowercase.
GPT_ID = re.compile(r"gpt-\d+(?:\.\d+)?(?:-[a-z0-9]+)*")
# OpenAI's codenames for Codex's current models (it renames this line often; update this list when
# it does). Without the `gpt-` prefix, only these are completed, in either order with the version.
GPT_CODENAMES = ("astra", "sol", "terra", "luna")
GPT_BARE = re.compile(
    r"(?:(?P<name1>{names})-(?P<ver1>\d+(?:\.\d+)?)|(?P<ver2>\d+(?:\.\d+)?)-(?P<name2>{names}))".format(
        names="|".join(GPT_CODENAMES)))


def catalog(home: Path) -> dict[str, list[dict[str, Any]]] | None:
    """Agent -> its models ({id, label, default, efforts}), or None when the catalog is unreadable."""
    candidates = [home / d / CATALOG_FILE for d in CATALOG_DIRS]
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "orca" / CATALOG_FILE)
    for path in candidates:
        try:
            entries = json.loads(path.read_text(encoding="utf-8")).get("entries", [])
        except (OSError, ValueError, AttributeError):
            continue
        found: dict[str, list[dict[str, Any]]] = {}
        for entry in entries if isinstance(entries, list) else []:
            models = ((entry or {}).get("discovered") or {}).get("models") if isinstance(entry, dict) else None
            if not isinstance(models, list):
                continue
            found[str(entry.get("agent"))] = [
                {"id": str(m["id"]), "label": str(m.get("label") or m["id"]), "default": bool(m.get("isDefault")),
                 "efforts": [str(e["value"]) for e in m.get("efforts") or [] if isinstance(e, dict) and "value" in e]}
                for m in models if isinstance(m, dict) and m.get("id")
            ]
        return found
    return None


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def resolve(agent: str, wanted: str | None, effort: str | None, home: Path) -> tuple[str | None, list[str]]:
    """The model id for what the person asked (`wanted`), checking `effort` when the catalog knows
    the model's efforts. Returns (id, warnings)."""
    if not wanted:
        return None, []
    known = catalog(home)
    models = (known or {}).get(agent)
    if not models:
        where = ("Orca's model catalog cannot be read" if known is None
                 else f"Orca's model catalog does not list {agent}")
        given = re.sub(r"\s+", "-", wanted.strip())
        completed = {"claude": _claude_id, "codex": _gpt_id}.get(agent, lambda _: None)(wanted)
        if completed and completed != given:
            return completed, [f"{where}: passing '{wanted}' as the {agent} model '{completed}' (not checked)"]
        return given, [f"{where}: passing the model '{given}' as given"]
    exact = [m for m in models if m["id"] == wanted.strip()]
    matches = exact or [m for m in models if _key(m["label"]) == _key(wanted)] or \
        [m for m in models if _key(m["id"]) == _key(wanted)]
    listing = [f"{m['id']} ({m['label']})" for m in models]
    if not matches:
        raise FactoryError("model_unknown", f"{agent} has no model '{wanted}'", EXIT_USAGE, listing)
    if len(matches) > 1:
        raise FactoryError("model_ambiguous", f"'{wanted}' matches several {agent} models", EXIT_USAGE,
                           [f"{m['id']} ({m['label']})" for m in matches])
    model = matches[0]
    if effort and model["efforts"] and effort not in model["efforts"]:
        raise FactoryError("effort_unknown", f"{model['label']} does not take the effort '{effort}'", EXIT_USAGE,
                           model["efforts"])
    return model["id"], []


def _claude_id(wanted: str) -> str | None:
    """What the person said as a full Claude id ("Opus 5.5", "opus-5.5" and "claude-opus-5.5" are
    all `claude-opus-5-5`), or None when it is not a family and a version (an alias such as `opus`,
    an older or a cloud provider's id)."""
    said = re.sub(r"[\s._]+", "-", re.sub(r"\s*\[", "[", wanted.strip().lower()))
    if CLAUDE_ALIAS.fullmatch(said):
        return said
    match = CLAUDE_ID.fullmatch(said)
    if match is None or not (said.startswith("claude-") or match[1] in CLAUDE_FAMILIES):
        return None
    return f"claude-{match[1]}-{match[2]}{match[3] or ''}{match[4] or ''}"


def _gpt_id(wanted: str) -> str | None:
    """What the person said as an OpenAI id ("GPT 6.1 Sol", "Sol 6.1", "gpt6.1-sol" and
    "gpt-6-1-sol" are all `gpt-6.1-sol`), or None when it is not a GPT model (an `o3`, a local
    model, or a codename this version doesn't know)."""
    said = re.sub(r"[\s_]+", "-", wanted.strip().lower())
    said = re.sub(r"^gpt-?(\d+)(?:[-.](\d)(?=-|$))?", lambda m: f"gpt-{m[1]}" + (f".{m[2]}" if m[2] else ""), said)
    if GPT_ID.fullmatch(said):
        return said
    bare = GPT_BARE.fullmatch(said)
    if bare is None:
        return None
    return f"gpt-{bare['ver1'] or bare['ver2']}-{bare['name1'] or bare['name2']}"


def list_models(home: Path, agent: str | None) -> dict[str, Any]:
    """`swfactory models`: what Orca's catalog lists, per agent."""
    known = catalog(home)
    if known is None:
        return {"available": False, "agents": {}}
    return {"available": True, "agents": {a: m for a, m in known.items() if agent is None or a == agent}}
