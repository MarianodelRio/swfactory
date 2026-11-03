"""Task state files in `.factory/state/`: the only code that writes them.

Everything written goes through `mask_secrets`.
Per task: `<task>.json` (current state, replaced atomically), `<task>.events.jsonl` (append-only
history; each event carries the resulting state, so a corrupted JSON is rebuilt from the last
readable event), `<task>.lock` (the claim) and `.<task>.mutex`, the file writers lock to take
turns (an exclusive lock that works on Linux, macOS and Windows).

Raises FactoryError with these codes:
- task_claimed (EXIT_CONFLICT): the claim file already exists.
- not_claimed (EXIT_PRECONDITION): reassigning a claim that does not exist.
- state_corrupted (EXIT_ERROR): the JSON is unreadable and no event can rebuild it.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swfactory import system
from swfactory.output import EXIT_CONFLICT, EXIT_ERROR, EXIT_PRECONDITION, FactoryError, mask_secrets

State = dict[str, Any]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_state(task_id: str) -> State:
    """A task state with every field and nothing recorded yet."""
    return {
        "task": task_id,
        "state": None,
        "blocked_reason": None,
        "blocked_from": None,
        "workspace": None,
        "attempts": [],
        "last_checkpoint": None,
        "acceptance": {},
        "checks": None,
        "reviews": [],
        "review_runs": [],
        "sessions": [],
        "notes": [],
        "memory": None,
        "commits": [],
        "pull_request": None,
        "merged_at": None,
    }


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _json(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"

    def _events(self, task_id: str) -> Path:
        return self.root / f"{task_id}.events.jsonl"

    def _claim(self, task_id: str) -> Path:
        return self.root / f"{task_id}.lock"

    def read_task(self, task_id: str) -> State | None:
        """The current state, rebuilt from the events if the JSON is missing or corrupted; None if
        the task has no state at all."""
        events = self._events(task_id)
        if not self._json(task_id).exists() and (not events.exists() or events.stat().st_size == 0):
            return None
        try:
            state: State = json.loads(self._json(task_id).read_text(encoding="utf-8"))
            return state
        except (FileNotFoundError, json.JSONDecodeError):
            with self._locked(task_id):
                state = self.rebuild_from_events(task_id)
                self._write_json(task_id, state)
            return state

    def update_task(self, task_id: str, fn: Callable[[State], State], event: str) -> State:
        """Lock, read, apply `fn`, log `event` and write atomically. If `fn` raises, nothing changes.
        The event goes first, so the events are never behind the JSON."""
        with self._locked(task_id) as log:
            current = self._load(task_id)
            updated = fn(copy.deepcopy(current))
            log.write(mask_secrets(json.dumps({"at": now(), "event": event, "state": updated})) + "\n")
            log.flush()
            os.fsync(log.fileno())
            self._write_json(task_id, updated)
        return updated

    def rebuild_from_events(self, task_id: str) -> State:
        path = self._events(task_id)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for line in reversed(lines):
            try:
                state: State = json.loads(line)["state"]
                return state
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        message = f"{self._json(task_id)} is unreadable and has no events to rebuild it"
        raise FactoryError("state_corrupted", message, EXIT_ERROR)

    def create_claim(self, task_id: str, attempt: int) -> None:
        """Create the claim with its content in one step: a hard link fails if the claim exists."""
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._temp_file(json.dumps({"attempt": attempt, "at": now()}))
        try:
            os.link(tmp, self._claim(task_id))
        except FileExistsError as exc:
            raise FactoryError("task_claimed", f"{task_id} is already claimed", EXIT_CONFLICT) from exc
        finally:
            tmp.unlink()

    def read_claim(self, task_id: str) -> dict[str, Any] | None:
        try:
            claim: dict[str, Any] = json.loads(self._claim(task_id).read_text(encoding="utf-8"))
            return claim
        except FileNotFoundError:
            return None

    def release_claim(self, task_id: str) -> None:
        with self._locked(task_id):
            self._claim(task_id).unlink(missing_ok=True)

    def reassign_claim(self, task_id: str, attempt: int) -> None:
        """Give an existing claim to a new attempt (only `recover` does this)."""
        with self._locked(task_id):
            if not self._claim(task_id).exists():
                raise FactoryError("not_claimed", f"{task_id} has no claim to reassign", EXIT_PRECONDITION)
            self._atomic_write(self._claim(task_id), json.dumps({"attempt": attempt, "at": now()}))

    def _load(self, task_id: str) -> State:
        try:
            state: State = json.loads(self._json(task_id).read_text(encoding="utf-8"))
            return state
        except FileNotFoundError:
            if self._events(task_id).stat().st_size == 0:
                return empty_state(task_id)
            return self.rebuild_from_events(task_id)
        except json.JSONDecodeError:
            return self.rebuild_from_events(task_id)

    def _write_json(self, task_id: str, state: State) -> None:
        self._atomic_write(self._json(task_id), mask_secrets(json.dumps(state, indent=2, ensure_ascii=False)) + "\n")

    def _atomic_write(self, path: Path, text: str) -> None:
        tmp = self._temp_file(text)
        try:
            system.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _temp_file(self, text: str) -> Path:
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name)

    @contextlib.contextmanager
    def _locked(self, task_id: str) -> Iterator[Any]:
        """The task's lock; yields its events file open for appending."""
        self.root.mkdir(parents=True, exist_ok=True)
        with system.exclusive_lock(self.root / f".{task_id}.mutex"):
            with self._events(task_id).open("a", encoding="utf-8", newline="\n") as log:
                yield log
