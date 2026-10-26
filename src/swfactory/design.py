"""Changes: digest, ids and the task block of `design.md`.

Raises FactoryError with these codes:
- design_invalid (EXIT_PRECONDITION): the task block or the acceptance criteria are not valid;
  `details` lists every problem found.
- change_not_found (EXIT_PRECONDITION): the change folder or its `design.md` does not exist.
- change_rejected (EXIT_PRECONDITION): the change was rejected; a new change must be created.
- already_approved (EXIT_PRECONDITION): the current design is already approved.
- approval_not_explicit (EXIT_PRECONDITION): the approval message does not name the change.
- approval_not_confirmed (EXIT_PRECONDITION): the id typed in the terminal is not the change id.
- not_on_base_branch (EXIT_PRECONDITION): approve and reject only run on the base branch.
- git_identity_missing (EXIT_PRECONDITION): `git config user.email` is not set.
Plus the codes of factory.git (commit_failed, ...) and factory.orca (base branch).
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

import yaml

from swfactory import git, orca, sync, system, tasks
from swfactory.output import EXIT_PRECONDITION, FactoryError
from swfactory.state import TaskStore, now

# A change is `CHG-12` (`CHG-0012` before 2.0, still accepted). Its folder is the id, or the id and a
# slug (`CHG-12-weekly-recurrence`). Its tasks are `CHG-12.1`, `CHG-12.2` (`CHG-0012-01` before).
CHANGE_ID = re.compile(r"^CHG-(\d+)$")
CHANGE_FOLDER = re.compile(r"^(CHG-(\d+))(?:-([a-z][a-z0-9-]*))?$")
SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,5}$")
TITLE = re.compile(r"^# (?:CHG-\d+\s*[—-]\s*)?(.+)$", re.MULTILINE)
AC_ID = re.compile(r"^AC-\d{2,}$")
AC_LINE = re.compile(r"^\s*-\s*(AC-\d{2,})\s*:")
FENCE = re.compile(r"^```yaml\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)

REQUIRED = ("id", "title", "depends_on", "complexity", "review", "acceptance")
OPTIONAL = ("scope", "integrate")
INTEGRATE_MODES = ("pr", "merge")
COMPLEXITIES = ("trivial", "normal", "complex")
REVIEW_TYPES = ("quality", "conformance")


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def digest(design_md: str) -> str:
    """SHA-256 of `design.md` with line breaks normalized to LF."""
    return "sha256:" + hashlib.sha256(normalize(design_md).encode("utf-8")).hexdigest()


def change_folders(changes_dir: Path) -> list[tuple[str, Path]]:
    """(change id, folder) of every change, in number order."""
    found = []
    if changes_dir.is_dir():
        for entry in changes_dir.iterdir():
            match = CHANGE_FOLDER.match(entry.name)
            if match and entry.is_dir():
                found.append((int(match.group(2)), match.group(1), entry))
    return [(change_id, folder) for _, change_id, folder in sorted(found, key=lambda f: (f[0], f[1]))]


def change_folder(changes_dir: Path, change_id: str) -> Path | None:
    """The folder of `change_id` (with or without its slug), or None."""
    return next((folder for found, folder in change_folders(changes_dir) if found == change_id), None)


def short_id(task_or_change_id: str) -> str:
    """`CHG-12.1` -> `12.1`: how ids are shown where space is short."""
    return task_or_change_id.removeprefix("CHG-")


def full_id(text: str) -> str:
    """What the person types (`12`, `12.1`, `chg-12.1`, `CHG-12.1`) as the canonical id."""
    text = text.strip()
    if re.fullmatch(r"\d+(?:\.\d+|-\d{2,})?", text):
        return f"CHG-{text}"
    if re.fullmatch(r"chg-\d+(?:\.\d+|-\d{2,})?", text, re.IGNORECASE):
        return "CHG-" + text[4:]
    return text


FILLER = {"a", "an", "the", "and", "or", "of", "to", "for", "with", "in", "on", "by"}


def slugify(text: str, words: int = 4, length: int = 32) -> str:
    """A short kebab-case slug of `text` for folder and branch names."""
    parts = [w for w in re.sub(r"[^a-z0-9]+", " ", text.lower()).split() if w not in FILLER][:words]
    while len(parts) > 1 and len("-".join(parts)) > length:  # whole words only
        parts.pop()
    slug = "-".join(parts)[:length]
    return slug if slug and slug[0].isalpha() else f"x-{slug}".strip("-")


def next_change_id(changes_dir: Path) -> str:
    """The next free `CHG-N` after the highest change number (also counting reserved numbers, the
    empty `.CHG-N.reserved` folders, which Git does not track)."""
    numbers = [int(change_id.removeprefix("CHG-")) for change_id, _ in change_folders(changes_dir)]
    if changes_dir.is_dir():
        numbers += [int(m.group(1)) for entry in changes_dir.iterdir()
                    if (m := re.fullmatch(r"\.CHG-(\d+)\.reserved", entry.name))]
    return f"CHG-{max(numbers, default=0) + 1}"


def section(text: str, title: str) -> str | None:
    """Body of the `## <title>` section, or None if there is none."""
    match = re.search(rf"^## {re.escape(title)}[ \t]*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else None


def parse_design(design_md: str, change_id: str, known_tasks: set[str]) -> dict[str, Any]:
    """Validate the acceptance criteria and the task block of a change and return both.

    `known_tasks` are task ids of other changes that `depends_on` may refer to.
    """
    text = normalize(design_md)
    errors: list[str] = []

    criteria_body = section(text, "Acceptance criteria")
    criteria: list[str] = []
    if criteria_body is None:
        errors.append("missing section '## Acceptance criteria'")
    else:
        criteria = [m.group(1) for line in criteria_body.splitlines() if (m := AC_LINE.match(line))]

    tasks = _task_block(text, errors)
    if tasks is not None:
        _validate_tasks(tasks, change_id, known_tasks, criteria, errors)

    if errors:
        message = f"the design of {change_id} has {len(errors)} problem(s)"
        raise FactoryError("design_invalid", message, EXIT_PRECONDITION, errors)
    assert tasks is not None
    return {"acceptance_criteria": criteria, "tasks": tasks}


def _task_block(text: str, errors: list[str]) -> list[dict[str, Any]] | None:
    body = section(text, "Tasks")
    if body is None:
        errors.append("missing section '## Tasks'")
        return None
    fence = FENCE.search(body)
    if fence is None:
        errors.append("the '## Tasks' section has no ```yaml block")
        return None
    try:
        tasks = yaml.safe_load(fence.group(1))
    except yaml.YAMLError as exc:
        errors.append(f"the task block is not valid YAML: {exc}")
        return None
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, dict) for t in tasks):
        errors.append("the task block must be a non-empty list of tasks")
        return None
    return tasks


def _validate_tasks(
    tasks: list[dict[str, Any]],
    change_id: str,
    known_tasks: set[str],
    criteria: list[str],
    errors: list[str],
) -> None:
    id_pattern = re.compile(rf"^{re.escape(change_id)}(?:\.[1-9]\d*|-\d{{2,}})$")
    ids = [str(t.get("id")) for t in tasks]
    own = set(ids)
    covered: set[str] = set()

    for position, task in enumerate(tasks, start=1):
        name = str(task.get("id", f"task #{position}"))
        for field in REQUIRED:
            if field not in task:
                errors.append(f"{name}: missing field '{field}'")
        for field in task:
            if field not in REQUIRED + OPTIONAL:
                errors.append(f"{name}: unknown field '{field}'")

        if "id" in task and not id_pattern.match(str(task["id"])):
            errors.append(f"{name}: id must be {change_id}.N (for example {change_id}.1)")
        if "id" in task and ids.count(str(task["id"])) > 1:
            errors.append(f"{name}: duplicated task id")
        if "title" in task and (not isinstance(task["title"], str) or not task["title"].strip()):
            errors.append(f"{name}: title must be a non-empty text")
        if "complexity" in task and task["complexity"] not in COMPLEXITIES:
            allowed = ", ".join(COMPLEXITIES)
            errors.append(f"{name}: complexity '{task['complexity']}' must be one of {allowed}")

        review = task.get("review")
        if "review" in task:
            if not isinstance(review, list) or any(r not in REVIEW_TYPES for r in review):
                errors.append(f"{name}: review {review!r} must be a list of {', '.join(REVIEW_TYPES)}")
            elif len(set(review)) != len(review):
                errors.append(f"{name}: review {review!r} has a duplicated type")

        if "scope" in task and (
            not isinstance(task["scope"], list) or not all(isinstance(g, str) and g for g in task["scope"])
        ):
            errors.append(f"{name}: scope must be a list of path globs")
        if "integrate" in task and task["integrate"] not in INTEGRATE_MODES:
            errors.append(f"{name}: integrate {task['integrate']!r} must be one of {', '.join(INTEGRATE_MODES)}")

        depends = task.get("depends_on")
        if "depends_on" in task:
            if not isinstance(depends, list):
                errors.append(f"{name}: depends_on must be a list of task ids")
            else:
                for dep in depends:
                    if str(dep) not in own and str(dep) not in known_tasks:
                        errors.append(f"{name}: depends on unknown task '{dep}'")

        acceptance = task.get("acceptance")
        if "acceptance" in task:
            if not isinstance(acceptance, dict) or not acceptance:
                errors.append(f"{name}: acceptance must map each AC to a command or 'manual'")
            else:
                for ac, how in acceptance.items():
                    if not AC_ID.match(str(ac)):
                        errors.append(f"{name}: '{ac}' is not an AC id (AC-NN)")
                    elif ac not in criteria:
                        errors.append(f"{name}: {ac} is not in '## Acceptance criteria'")
                    if not isinstance(how, str) or not how.strip():
                        errors.append(f"{name}: {ac} needs a command or 'manual'")
                    elif how != "manual" and (problem := command_problem(how)):
                        errors.append(f"{name}: {ac} {problem}")
                    covered.add(str(ac))

    for ac in criteria:
        if ac not in covered:
            errors.append(f"{ac} is covered by no task")

    cycle = _find_cycle(tasks, own)
    if cycle:
        errors.append(f"dependency cycle: {' -> '.join(cycle)}")


# Outside quotes a shell would read these; `task verify` runs a command without a shell, so they
# would reach the program as text. Inside quotes they are text anyway (`bash -c "a | b"` is fine).
SHELL_OPERATORS = {"|": "a pipe", "&": "'&' or '&&'", ";": "';'", "<": "a redirection", ">": "a redirection",
                   "*": "a glob", "`": "a command substitution"}
SHELL_EXPANSION = re.compile(r"\$[({\w]")
SHELL_HINT = "`task verify` runs it without a shell: wrap it as bash -c '...' or make the AC manual"


def command_problem(command: str) -> str | None:
    """Why an AC command would not run as written under `task verify`, or None."""
    quote, escaped = None, False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
        elif quote:
            quote = None if char == quote else quote
        elif char == "\\" and not system.WINDOWS:  # `\|` is a literal `|` (Windows paths use `\`)
            escaped = True
        elif char in "'\"":
            quote = char
        elif char in SHELL_OPERATORS:
            return f"uses {SHELL_OPERATORS[char]}; {SHELL_HINT}"
        elif SHELL_EXPANSION.match(command, index):
            return f"uses a shell expansion ($); {SHELL_HINT}"
    try:
        shlex.split(command)
    except ValueError as exc:
        return f"is not a valid command ({exc})"
    return None


def command_warnings(tasks: list[dict[str, Any]], root: Path) -> list[str]:
    """AC commands whose program is not found here: a sentence instead of a command, or a tool
    that `task verify` will not find either. A warning, since the workspace may differ."""
    warnings = []
    for task in tasks:
        for ac, how in (task.get("acceptance") or {}).items():
            if how == "manual":
                continue
            program = system.program(how)
            if not (shutil.which(program) or (root / program).is_file()):
                warnings.append(f"{task['id']}: {ac} runs '{program}', which is not found here: "
                                "check it is a command, or make the AC manual")
    return warnings


def _find_cycle(tasks: list[dict[str, Any]], own: set[str]) -> list[str] | None:
    graph = {
        str(t.get("id")): [str(d) for d in t.get("depends_on") or [] if str(d) in own]
        for t in tasks
        if isinstance(t.get("depends_on", []), list)
    }
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in visiting:
            return visiting[visiting.index(node) :] + [node]
        if node in done:
            return None
        visiting.append(node)
        for dep in graph.get(node, []):
            found = visit(dep)
            if found:
                return found
        visiting.pop()
        done.add(node)
        return None

    for node in graph:
        found = visit(node)
        if found:
            return found
    return None


def scope_overlaps(tasks: list[dict[str, Any]]) -> list[str]:
    """Pairs of tasks that may touch the same files (their `scope` globs overlap) although neither
    depends on the other: they could run in parallel and conflict, or one may really need the
    other. A warning for the designer, not an error."""
    own = {str(t.get("id")) for t in tasks}
    graph = {str(t.get("id")): {str(d) for d in t.get("depends_on") or [] if str(d) in own} for t in tasks}

    def reaches(start: str, goal: str) -> bool:
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(graph.get(node, ()))
        return False

    def prefix(glob: str) -> str:
        return re.split(r"[*?\[]", glob, maxsplit=1)[0]

    scoped = [(str(t.get("id")), [prefix(g) for g in t.get("scope") or [] if isinstance(g, str)]) for t in tasks]
    warnings = []
    for n, (first, first_scope) in enumerate(scoped):
        for second, second_scope in scoped[n + 1:]:
            if reaches(first, second) or reaches(second, first):
                continue
            shared = [a for a in first_scope for b in second_scope if a.startswith(b) or b.startswith(a)]
            if shared:
                warnings.append(f"{first} and {second} may touch the same files ({shared[0] or '**'}) but neither "
                                "depends on the other: add depends_on if one needs the other, or split the scope")
    return warnings


def reserve_change(changes_dir: Path, slug: str | None = None) -> dict[str, Any]:
    """Create the folder of the next free change id (`CHG-N-<slug>`); two sessions never get the
    same number: a number is taken when its plain `CHG-N` lock folder is created."""
    if slug is not None and not SLUG.match(slug):
        raise FactoryError("slug_invalid", f"'{slug}' is not a short kebab-case slug (e.g. weekly-recurrence)",
                           EXIT_PRECONDITION)
    changes_dir.mkdir(parents=True, exist_ok=True)
    while True:
        change_id = next_change_id(changes_dir)
        folder = changes_dir / (f"{change_id}-{slug}" if slug else change_id)
        try:
            (changes_dir / f".{change_id}.reserved").mkdir()
        except FileExistsError:
            continue
        folder.mkdir()
        return {"change": change_id, "folder": folder.name, "design": str(folder / "design.md")}


def register(change_id: str, changes_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Validate the `design.md` of a reserved change; warn about tasks that may collide."""
    folder = _folder(change_id, changes_dir)
    _refuse_rejected(change_id, folder)
    text = _design_text(folder)
    parsed = parse_design(text, change_id, known_tasks(changes_dir, exclude=change_id))
    warnings = scope_overlaps(parsed["tasks"]) + command_warnings(parsed["tasks"], changes_dir.parent.parent)
    return {"change": change_id, "digest": digest(text), **parsed}, warnings


def show(change_id: str, changes_dir: Path, store: TaskStore, main_changes_dir: Path) -> dict[str, Any]:
    """Change state, digest, criteria, tasks with their stored state, approval or rejection.
    An invalid design is still shown, with its problems. Whether the design changed after its
    approval is decided on the main checkout's files, which own the shared task state."""
    folder = _folder(change_id, changes_dir)
    text = _design_text(folder)
    try:
        parsed = parse_design(text, change_id, known_tasks(changes_dir, exclude=change_id))
        problems: list[str] = []
    except FactoryError as exc:  # parse_design only raises design_invalid
        parsed, problems = {"acceptance_criteria": [], "tasks": []}, exc.details

    approval = _read_json(folder / "approval.json")
    rejection = _read_json(folder / "rejection.json")
    current = digest(text)
    sync_with_main(change_id, main_changes_dir, store)

    task_list = []
    for task in parsed["tasks"]:
        stored = store.read_task(task["id"])
        task_list.append({**task, "state": stored["state"] if stored else None})
    if rejection is not None:
        state = "rejected"
    elif approval is None or approval.get("design_digest") != current:
        state = "awaiting_approval"
    elif task_list and all(t["state"] == "done" for t in task_list):
        state = "done"
    else:
        state = "approved"

    title = TITLE.search(normalize(text))
    return {
        "change": change_id,
        "title": title.group(1).strip() if title else None,
        "state": state,
        "digest": current,
        "acceptance_criteria": parsed["acceptance_criteria"],
        "tasks": task_list,
        "approval": approval,
        "rejection": rejection,
        "problems": problems,
    }


def sync_with_main(change_id: str, main_changes_dir: Path, store: TaskStore) -> None:
    """Block or restore the change's tasks from the main checkout's design and approval."""
    folder = change_folder(main_changes_dir, change_id)
    approval = _read_json(folder / "approval.json") if folder else None
    if folder is None or approval is None or not (folder / "design.md").exists():
        return
    approved = approval.get("design_digest") == digest((folder / "design.md").read_text(encoding="utf-8"))
    tasks.sync_with_design(approved_task_ids(folder), approved, store)


def approval_of(folder: Path) -> dict[str, Any] | None:
    return _read_json(folder / "approval.json")


def approved_task_ids(folder: Path) -> list[str]:
    """Task ids of the change's `tasks.yaml` (the last approved task list)."""
    path = folder / "tasks.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(t["id"]) for t in data.get("tasks", []) if isinstance(t, dict) and "id" in t]


def known_tasks(changes_dir: Path, exclude: str) -> set[str]:
    """Ids of approved tasks of other changes (their `tasks.yaml`)."""
    return {task_id for change_id, folder in change_folders(changes_dir) if change_id != exclude
            for task_id in approved_task_ids(folder)}


def _folder(change_id: str, changes_dir: Path) -> Path:
    folder = change_folder(changes_dir, change_id)
    if folder is None:
        message = f"{change_id} does not exist in {changes_dir}"
        raise FactoryError("change_not_found", message, EXIT_PRECONDITION)
    return folder


def _design_text(folder: Path) -> str:
    path = folder / "design.md"
    if not path.exists():
        raise FactoryError("change_not_found", f"{path} does not exist yet", EXIT_PRECONDITION)
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def approve(
    change_id: str, changes_dir: Path, message: str, method: str, main_checkout: Path, store: TaskStore
) -> tuple[dict[str, Any], list[str]]:
    """Approve the current design: on the base branch, with an explicit message
    (`chat`) or the typed id (`terminal`); commit only the change folder, on top of the latest
    remote base, and push it. On re-approval the task ids are kept and tasks blocked by the design
    change return to their state."""
    folder = _folder(change_id, changes_dir)
    _refuse_rejected(change_id, folder)
    text = _design_text(folder)
    removed = _removed_tasks(text, folder)
    try:
        parsed = parse_design(text, change_id, known_tasks(changes_dir, exclude=change_id))
    except FactoryError as exc:
        exc.details += removed
        raise
    if removed:
        message = f"the design of {change_id} removes approved tasks"
        raise FactoryError("design_invalid", message, EXIT_PRECONDITION, removed)
    current = digest(text)
    previous = _read_json(folder / "approval.json")
    if previous and previous.get("design_digest") == current:
        raise FactoryError("already_approved", f"{change_id} is already approved", EXIT_PRECONDITION)
    if method == "terminal" and message.strip() != change_id:
        message = f"typed '{message.strip()}', expected {change_id}: not approved"
        raise FactoryError("approval_not_confirmed", message, EXIT_PRECONDITION)
    if method == "chat" and not re.search(rf"\b{re.escape(change_id)}\b", message, re.IGNORECASE):
        message = f"the approval message must name {change_id} explicitly (e.g. 'approve {change_id}')"
        raise FactoryError("approval_not_explicit", message, EXIT_PRECONDITION)
    email, base = _ready_to_commit(folder, main_checkout)
    warnings = sync.refresh(main_checkout, store.root, base)

    approval = {
        "change": change_id,
        "design_digest": current,
        "approved_by": email,
        "approved_at": now(),
        "method": method,
        "message": message,
    }
    tasks_yaml = {"change": change_id, "design_digest": current, "tasks": parsed["tasks"]}
    files = {
        "approval.json": json.dumps(approval, indent=2, ensure_ascii=False) + "\n",
        "tasks.yaml": yaml.safe_dump(tasks_yaml, sort_keys=False, allow_unicode=True),
    }
    commit = _write_and_commit(folder, files, f"Approve {change_id}")
    tasks.sync_with_design(approved_task_ids(folder), True, store)
    warnings += sync.push_approvals(main_checkout, base)
    warnings += command_warnings(parsed["tasks"], changes_dir.parent.parent)
    return {"change": change_id, "digest": current, "commit": commit, "tasks": parsed["tasks"]}, warnings


def reject(change_id: str, changes_dir: Path, reason: str, main_checkout: Path,
           state_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Reject a change (final): write `rejection.json` on the base branch, commit and push it."""
    folder = _folder(change_id, changes_dir)
    _refuse_rejected(change_id, folder)
    text = _design_text(folder)
    email, base = _ready_to_commit(folder, main_checkout)
    warnings = sync.refresh(main_checkout, state_dir, base)
    rejection = {
        "change": change_id,
        "design_digest": digest(text),
        "rejected_by": email,
        "rejected_at": now(),
        "method": "chat",
        "reason": reason,
    }
    files = {"rejection.json": json.dumps(rejection, indent=2, ensure_ascii=False) + "\n"}
    commit = _write_and_commit(folder, files, f"Reject {change_id}")
    warnings += sync.push_approvals(main_checkout, base)
    return {"change": change_id, "commit": commit}, warnings


def _removed_tasks(text: str, folder: Path) -> list[str]:
    """Problems for approved task ids missing from the design: nothing is deleted."""
    block = _task_block(normalize(text), []) or []
    ids = {str(t.get("id")) for t in block}
    missing = [i for i in approved_task_ids(folder) if i not in ids]
    return [f"{i} was approved before and cannot be removed" for i in missing]


def _refuse_rejected(change_id: str, folder: Path) -> None:
    if (folder / "rejection.json").exists():
        message = f"{change_id} was rejected; create a new change to take it up again"
        raise FactoryError("change_rejected", message, EXIT_PRECONDITION)


def _ready_to_commit(folder: Path, main_checkout: Path) -> tuple[str, str]:
    """The session is on the base branch and has a Git identity; return its email and the base."""
    base = orca.base_branch(main_checkout)
    branch = git.current_branch(folder)
    if branch != base:
        where = branch or "a detached HEAD"
        message = f"this session is on '{where}', not on the base branch '{base}'; run `git switch {base}`"
        raise FactoryError("not_on_base_branch", message, EXIT_PRECONDITION)
    email = git.config_value("user.email", folder)
    if not email:
        raise FactoryError("git_identity_missing", "set `git config user.email` first", EXIT_PRECONDITION)
    return email, base


def _write_and_commit(folder: Path, files: dict[str, str], message: str) -> str:
    """Write `files` in the change folder and commit only that folder; undo the writes if the
    commit fails."""
    previous = {name: (folder / name).read_text(encoding="utf-8") if (folder / name).exists() else None
                for name in files}
    for name, content in files.items():
        (folder / name).write_text(content, encoding="utf-8", newline="\n")
    root = git.toplevel(folder)
    try:
        return git.commit_paths([folder.relative_to(root).as_posix()], message, root)
    except FactoryError:
        for name, before in previous.items():
            if before is None:
                (folder / name).unlink(missing_ok=True)
            else:
                (folder / name).write_text(before, encoding="utf-8", newline="\n")
        raise
