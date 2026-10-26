"""The only module that runs git (and gh): argument lists, never a shell.

Raises FactoryError with these codes:
- git_unavailable (EXIT_UNAVAILABLE): the git executable is not installed or not on PATH.
- not_a_git_repo (EXIT_PRECONDITION): the working directory is not inside a Git repository.
- git_failed (EXIT_ERROR): any other git failure; the message carries the command and stderr.
- commit_failed (EXIT_PRECONDITION): `git commit` was refused (e.g. by a hook); nothing is committed.
- push_rejected (EXIT_UNAVAILABLE): the remote refused the push; the local branch is intact.
- gh_unavailable / gh_failed (EXIT_UNAVAILABLE): `gh` is not installed, or it failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from swfactory import system
from swfactory.output import EXIT_ERROR, EXIT_PRECONDITION, EXIT_UNAVAILABLE, FactoryError


def run(args: list[str], cwd: Path | None = None) -> str:
    """Run `git <args>` in `cwd` and return its stripped stdout."""
    command = ["git", *args]
    # Untranslated messages so failures can be classified by their text; no optional locks, so read
    # commands such as `status` never rewrite the index.
    env = {**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
    except FileNotFoundError as error:
        if cwd is not None and not cwd.is_dir():
            raise FactoryError("git_failed", f"directory does not exist: {cwd}", EXIT_ERROR) from error
        message = "git is not installed or not on PATH"
        raise FactoryError("git_unavailable", message, EXIT_UNAVAILABLE) from error
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "not a git repository" in stderr.lower():
            where = cwd or Path.cwd()
            raise FactoryError("not_a_git_repo", f"not inside a Git repository: {where}", EXIT_PRECONDITION)
        raise FactoryError("git_failed", f"{' '.join(command)} failed: {stderr}", EXIT_ERROR)
    return result.stdout.strip()


def toplevel(cwd: Path | None = None) -> Path:
    """Absolute root of the checkout (main or worktree) that contains `cwd`."""
    return Path(run(["rev-parse", "--show-toplevel"], cwd)).resolve()


def worktree_paths(cwd: Path | None = None) -> list[Path]:
    """Absolute paths of every worktree of the clone, main checkout first."""
    output = run(["worktree", "list", "--porcelain"], cwd)
    prefix = "worktree "
    return [Path(line[len(prefix) :]).resolve() for line in output.splitlines() if line.startswith(prefix)]


def _try(args: list[str], cwd: Path | None) -> str | None:
    """`run`, or None when git answers with a failure (unknown ref, unset key, ...)."""
    try:
        return run(args, cwd)
    except FactoryError as exc:
        if exc.code == "git_failed":
            return None
        raise


def current_branch(cwd: Path | None = None) -> str:
    """The checked-out branch, or "" when HEAD is detached."""
    return _try(["symbolic-ref", "--short", "-q", "HEAD"], cwd) or ""


def config_value(key: str, cwd: Path | None = None) -> str:
    """`git config --get <key>`, or "" when it is not set."""
    return _try(["config", "--get", key], cwd) or ""


def commit_paths(paths: list[str], message: str, cwd: Path) -> str:
    """Commit the working-tree content of `paths` only (`git commit --only`); other staged or
    unstaged changes stay as they are. New files are made known with `git add -N`, which is
    undone if the commit is refused, so the index ends as it was (commit_failed is raised)."""
    untracked = run(["ls-files", "--others", "--exclude-standard", "--", *paths], cwd).splitlines()
    if untracked:
        run(["add", "-N", "--", *untracked], cwd)
    try:
        run(["commit", "-q", "--only", "-m", message, "--", *paths], cwd)
    except FactoryError as exc:
        if untracked:
            try:
                run(["rm", "-q", "--cached", "--", *untracked], cwd)
            except FactoryError:
                pass  # the commit failure below is what the person needs to see
        message = f"the commit was refused: {exc.message}"
        raise FactoryError("commit_failed", message, EXIT_PRECONDITION) from exc
    return run(["rev-parse", "HEAD"], cwd)


def is_ancestor(commit: str, ref: str, cwd: Path | None = None) -> bool:
    """True if `commit` is already contained in `ref`; False if not, or if either is unknown."""
    return _try(["merge-base", "--is-ancestor", commit, ref], cwd) is not None


def ref_exists(ref: str, cwd: Path | None = None) -> bool:
    return _try(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd) is not None


def has_remote(name: str, cwd: Path) -> bool:
    return _try(["remote", "get-url", name], cwd) is not None


def fetch(cwd: Path) -> None:
    """Update the `origin/*` refs (and drop those deleted on the remote); the branches are not touched."""
    run(["fetch", "-q", "--prune", "origin"], cwd)


def commits_in(revision_range: str, cwd: Path) -> list[str]:
    """Commits of `old..new`, newest first."""
    return run(["rev-list", revision_range], cwd).splitlines()


def paths_in(revision_range: str, cwd: Path) -> set[str]:
    """Every file changed by the commits of `old..new`."""
    return {line for line in run(["log", "--format=", "--name-only", revision_range], cwd).splitlines() if line}


def is_clean(cwd: Path) -> bool:
    """No changes to tracked files (untracked files do not count)."""
    return not run(["status", "--porcelain", "--untracked-files=no"], cwd)


# Files an agent's client writes in a workspace that are not part of the work.
LOCAL_FILES = (".claude/settings.local.json",)


def uncommitted(cwd: Path) -> tuple[list[str], list[str]]:
    """(changed tracked files, untracked files that are not ignored), leaving out the Factory's own
    (`.factory/`) and the agent clients' local settings."""
    entries = iter(run(["status", "--porcelain=v2", "-z", "--untracked-files=all"], cwd).split("\0"))
    changed: list[str] = []
    untracked: list[str] = []
    for entry in entries:
        kind = entry[:2]
        if kind == "? ":
            untracked.append(entry[2:])
        elif kind in ("1 ", "2 ", "u "):
            fields = {"1 ": 8, "2 ": 9, "u ": 10}[kind]
            changed.append(entry.split(" ", fields)[fields])
            if kind == "2 ":
                next(entries, None)  # a rename's original path

    def work(path: str) -> bool:
        return not path.startswith(".factory/") and path not in LOCAL_FILES

    return [p for p in changed if work(p)], [p for p in untracked if work(p)]


def show_file(ref: str, path: str, cwd: Path | None = None) -> str | None:
    """Content of `path` in `ref`, or None if it is not there."""
    return _try(["show", f"{ref}:{path}"], cwd)


NOT_CODE = [":(exclude)docs", ":(exclude).factory"]


def last_code_commit(base: str, cwd: Path) -> str:
    """The last commit since `base` that changed files outside `docs/` and `.factory/`; the
    merge base if there is none."""
    merge_base = run(["merge-base", base, "HEAD"], cwd)
    last = run(["log", "-1", "--format=%H", f"{merge_base}..HEAD", "--", ".", *NOT_CODE], cwd)
    return last or merge_base


def changed_files(base: str, cwd: Path) -> list[str]:
    """Files changed on the branch since it left `base`."""
    merge_base = run(["merge-base", base, "HEAD"], cwd)
    return run(["diff", "--name-only", merge_base, "HEAD"], cwd).splitlines()


def push_branch(branch: str, cwd: Path) -> None:
    """Push only `branch` to origin, never forced."""
    try:
        run(["push", "-q", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], cwd)
    except FactoryError as exc:
        if exc.code != "git_failed":
            raise
        message = f"the push of {branch} was rejected; the branch is intact. {exc.message}"
        raise FactoryError("push_rejected", message, EXIT_UNAVAILABLE) from exc


def delete_remote_branch(branch: str, cwd: Path) -> None:
    """Delete `branch` on origin (after its work was merged)."""
    run(["push", "-q", "origin", "--delete", f"refs/heads/{branch}"], cwd)


def _gh(args: list[str], cwd: Path) -> str:
    command = system.tool_command("gh", "SWFACTORY_GH")
    system.check_batch_arguments(command, args)
    try:
        done = subprocess.run([*command, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    except FileNotFoundError as exc:
        raise FactoryError("gh_unavailable", "gh is not installed", EXIT_UNAVAILABLE) from exc
    if done.returncode != 0:
        raise FactoryError("gh_failed", f"gh {' '.join(args[:2])} failed: {done.stderr.strip()}", EXIT_UNAVAILABLE)
    return done.stdout.strip()


def gh_version(cwd: Path) -> str:
    return _gh(["--version"], cwd)


def pr_for_branch(branch: str, cwd: Path) -> dict[str, Any] | None:
    """The open pull request whose head is `branch`, if any."""
    found = json.loads(_gh(["pr", "list", "--head", branch, "--json", "number,url"], cwd) or "[]")
    return {"number": int(found[0]["number"]), "url": found[0]["url"]} if found else None


def pr_state(number: int, cwd: Path) -> dict[str, Any]:
    """`state` (OPEN, CLOSED or MERGED) and `mergedAt` of a pull request."""
    found: dict[str, Any] = json.loads(_gh(["pr", "view", str(number), "--json", "state,mergedAt"], cwd))
    return found


def pr_merge(number: int, cwd: Path) -> None:
    """Merge a pull request with a merge commit (its commits stay in the base branch's history)."""
    _gh(["pr", "merge", str(number), "--merge"], cwd)


def pr_create(base: str, head: str, title: str, body: str, cwd: Path) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as file:
        file.write(body)
    try:
        url = _gh(["pr", "create", "--base", base, "--head", head, "--title", title, "--body-file", file.name],
                  cwd).splitlines()[-1]
    finally:
        os.unlink(file.name)
    return {"number": int(url.rstrip("/").rsplit("/", 1)[1]), "url": url}


def branch_commits(base: str, cwd: Path) -> list[str]:
    """Commits of the branch since it left `base`, oldest first."""
    merge_base = run(["merge-base", base, "HEAD"], cwd)
    return run(["log", "--reverse", "--format=%H", f"{merge_base}..HEAD"], cwd).splitlines()


def gh_authenticated(cwd: Path) -> bool:
    try:
        _gh(["auth", "status"], cwd)
    except FactoryError:
        return False
    return True
