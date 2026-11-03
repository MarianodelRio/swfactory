"""Load `.factory/config.toml`, fill in the defaults and validate known values.

Raises FactoryError "config_invalid" (EXIT_PRECONDITION) for invalid TOML, an unknown
schema_version or a known key with a bad value. Unknown sections and keys are kept unvalidated.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

from swfactory.output import EXIT_PRECONDITION, FactoryError

SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "orca": {"min_version": "1.4.229"},
    "checks": {},
    "review": {"quality": "auto", "conformance": "auto", "max_rounds": 2},
    "git": {"pr_on_done": "offer", "integrate": "pr", "cleanup": "ask"},
    "language": {"artifacts": "en"},
    "board": {"port": 8765, "idle_minutes": 30},
    # Default agents for /factory-build; empty means "ask". Models are named as the person
    # says them ("opus 5.5") or by id, and only apply to the agent next to them.
    "agents": {"lead": "", "lead_model": "", "lead_effort": "",
               "reviewer": "", "reviewer_model": "", "reviewer_effort": ""},
}

AGENTS = ("", "claude", "codex")

REVIEW_MODES = ("auto", "always", "never")
PR_MODES = ("offer", "always", "never")
INTEGRATE_MODES = ("pr", "merge")
CLEANUP_MODES = ("ask", "always", "never")


def load_config(path: Path) -> dict[str, Any]:
    """Return the config at `path` merged over the defaults; a missing file gives the defaults."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(f"{path} is not valid TOML: {exc}") from exc

    config = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if isinstance(DEFAULTS.get(key), dict):
            if not isinstance(value, dict):
                raise _invalid(f"{key} must be a table, got {value!r}")
            config[key].update(value)
        else:
            config[key] = value

    memory = config.get("memory", {})
    if not isinstance(memory, dict):
        raise _invalid("memory must be a table")
    for name in ("paths", "behavior_paths"):
        value = memory.get(name, [])
        if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() and not p.startswith("/")
                                                and ".." not in p.split("/") for p in value):
            raise _invalid(f"memory.{name} must contain relative repository path globs")

    _validate(config)
    return config


def _validate(config: dict[str, Any]) -> None:
    version = config["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise _invalid(f"schema_version {version!r} is not supported (expected {SCHEMA_VERSION})")
    if not isinstance(config["orca"]["min_version"], str):
        raise _invalid(f"orca.min_version must be a string, got {config['orca']['min_version']!r}")
    for name, command in config["checks"].items():
        if not isinstance(command, str):
            raise _invalid(f"checks.{name} must be a string command, got {command!r}")
    for key in ("quality", "conformance"):
        _choice(f"review.{key}", config["review"][key], REVIEW_MODES)
    # Validate the explicit limit and its legacy alias through the same rule.
    for key in ("max_rounds", "max_failed_rounds"):
        if key in config["review"]:
            rounds = config["review"][key]
            if type(rounds) is not int or rounds < 1:
                raise _invalid(f"review.{key} must be a positive integer, got {rounds!r}")
    _choice("git.pr_on_done", config["git"]["pr_on_done"], PR_MODES)
    _choice("git.integrate", config["git"]["integrate"], INTEGRATE_MODES)
    _choice("git.cleanup", config["git"]["cleanup"], CLEANUP_MODES)
    for role in ("lead", "reviewer"):
        _choice(f"agents.{role}", config["agents"][role], AGENTS)
        for key in (f"{role}_model", f"{role}_effort"):
            if not isinstance(config["agents"][key], str):
                raise _invalid(f"agents.{key} must be a string, got {config['agents'][key]!r}")
    port, idle = config["board"]["port"], config["board"]["idle_minutes"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise _invalid(f"board.port must be a TCP port number, got {port!r}")
    if type(idle) is not int or idle < 1:
        raise _invalid(f"board.idle_minutes must be a positive integer, got {idle!r}")


def _choice(key: str, value: Any, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise _invalid(f"{key} is {value!r}; allowed values: {', '.join(allowed)}")


def _invalid(message: str) -> FactoryError:
    return FactoryError("config_invalid", message, EXIT_PRECONDITION)
