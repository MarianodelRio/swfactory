"""What differs between Linux, macOS and Windows: file locks, atomic replace, finding and running
external programs, comparing paths and starting a detached process. Everything else is
platform-neutral."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from swfactory.output import EXIT_USAGE, FactoryError

WINDOWS = sys.platform == "win32"
# Characters that cmd.exe interprets inside the arguments of a .cmd/.bat file.
BATCH_UNSAFE = set('\r\n"%^&|<>!')
RETRY_SECONDS = 0.02
RETRIES = 250  # about 5 s: Windows refuses to replace a file another process has open


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if needed), blocking until it is free. The file
    is only a lock: nothing reads it, so Windows' mandatory byte locks get in nobody's way."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(RETRY_SECONDS)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def replace(source: Path, target: Path) -> None:
    """`os.replace`, retried while Windows reports the target busy (a reader has it open)."""
    for attempt in range(RETRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not WINDOWS or attempt == RETRIES - 1:
                raise
            time.sleep(RETRY_SECONDS)


def tool_command(name: str, override_variable: str) -> list[str]:
    """How to run the CLI `name` (orca, gh): the environment variable `override_variable` (a path,
    or a JSON list of a program and its arguments), else `name` from PATH. On Windows a native
    `.exe` wins over a `.cmd` shim."""
    override = os.environ.get(override_variable, "").strip()
    if override:
        if override.startswith("["):
            try:
                command = json.loads(override)
            except ValueError as exc:
                raise FactoryError("invalid_override", f"{override_variable} is not a JSON list: {exc}",
                                   EXIT_USAGE) from exc
            if not (isinstance(command, list) and command and all(isinstance(p, str) for p in command)):
                raise FactoryError("invalid_override", f"{override_variable} must list a program and its "
                                   "arguments", EXIT_USAGE)
            return list(command)
        return [override]
    if WINDOWS:
        return [shutil.which(f"{name}.exe") or shutil.which(name) or name]
    return [name]


def check_batch_arguments(command: list[str], args: list[str]) -> None:
    """Refuse to pass text that cmd.exe would reinterpret to a `.cmd`/`.bat` program."""
    if not command[0].lower().endswith((".cmd", ".bat")):
        return
    if any(BATCH_UNSAFE & set(arg) for arg in args):
        message = (f"{command[0]} is a batch file, and cmd.exe would misread these arguments; "
                   "point the override variable at the program's .exe instead")
        raise FactoryError("unsafe_batch_arguments", message, EXIT_USAGE)


def command_line(command: str) -> list[str] | str:
    """A configured check or acceptance command, ready for `subprocess.run` without a shell. On
    POSIX it is split like a shell would; on Windows the program is resolved on PATH (so `.exe`
    and `.cmd` work) and the rest is left for the program to parse, as Windows does."""
    if not WINDOWS:
        return shlex.split(command)
    text = command.strip()
    tokens = shlex.split(text, posix=False)
    if not tokens:
        raise ValueError("empty command")
    program = tokens[0].strip('"')
    found = shutil.which(program) or program
    return subprocess.list2cmdline([found]) + text[len(tokens[0]):]


def program(command: str) -> str:
    """The program a check or acceptance command runs, split as `command_line` splits it."""
    if not WINDOWS:
        return shlex.split(command)[0]
    return shlex.split(command.strip(), posix=False)[0].strip('"')


def same_path(first: Any, second: Any) -> bool:
    """Whether two paths given as text name the same place (Windows: any case, either slash)."""
    if not first or not second:
        return False
    return path_key(first) == path_key(second)


def path_key(path: Any) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def detached() -> dict[str, Any]:
    """`subprocess.Popen` options for a background process that outlives the command starting it."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
