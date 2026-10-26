"""`swfactory doctor` and `swfactory init`: analysis, plan, apply, backups and restore.

`doctor` only reads: it never writes to the repository or to Orca.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import tomllib
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from swfactory import config, git, orca
from swfactory.output import EXIT_PRECONDITION, FactoryError, is_secret_file

CHECK_KINDS = ("test", "lint", "typecheck", "build")
OTHER_INSTRUCTIONS = (".cursorrules", ".cursor/rules", ".windsurfrules", "GEMINI.md", ".github/copilot-instructions.md")


def doctor(root: Path, main_checkout: Path) -> dict[str, Any]:
    """Everything `/factory-init` needs to propose a plan , without changes."""
    problems: list[str] = []
    warnings: list[str] = []
    configured = config.load_config(root / ".factory" / "config.toml") if (root / ".factory").is_dir() else None

    report: dict[str, Any] = {
        "git": {
            "root": str(root),
            "branch": git.current_branch(root),
            "pending_changes": len(git.run(["status", "--porcelain"], root).splitlines()),
            "worktrees": [str(p) for p in git.worktree_paths(root)],
        },
        "orca": _orca(main_checkout, configured, problems),
        "agents": _agents(),
        "gh": _gh(root),
        "stack": _stack(root),
        "checks": _checks(root, configured),
        "ci": sorted(p.relative_to(root).as_posix() for p in (root / ".github" / "workflows").glob("*.y*ml")),
        "instructions": _instructions(root),
        "factory": {"installed": configured is not None},
    }
    if configured is None and any(v == "ask" for v in report["checks"].values()):
        warnings.append("some check commands were not found: ask the person for them")
    warnings += _permission_warnings(root)
    warnings += _stale_references(root)
    report["problems"] = problems
    report["warnings"] = warnings
    return report


def _orca(main_checkout: Path, configured: dict[str, Any] | None, problems: list[str]) -> dict[str, Any]:
    min_version = (configured or config.DEFAULTS)["orca"]["min_version"]
    try:
        info = orca.check(min_version)
    except FactoryError as exc:
        problems.append(f"Orca: {exc.message}")
        return {"ok": False, "problem": exc.message}
    result: dict[str, Any] = {"ok": True, "version": info["version"]}
    try:
        orca.run(["repo", "show", "--repo", f"path:{main_checkout}"])
        result["repo_registered"] = True
    except FactoryError:
        result["repo_registered"] = False
    try:
        result["base_branch"] = orca.base_branch(main_checkout)
    except FactoryError:
        result["base_branch"] = None
    try:
        skills = orca.run(["skills", "installed"])["skills"]
        result["orca_skills_installed"] = any(s.get("name") == "orca-cli" for s in skills)
    except (FactoryError, KeyError, TypeError):
        result["orca_skills_installed"] = None
    return result


def _agents() -> dict[str, bool | None]:
    try:
        statuses = {s["agent"]: s["state"] for s in orca.run(["agent", "hooks", "status"])["statuses"]}
    except (FactoryError, KeyError, TypeError):
        return {"claude": None, "codex": None}
    return {agent: statuses.get(agent) == "installed" for agent in ("claude", "codex")}


def _gh(root: Path) -> dict[str, bool]:
    try:
        git.gh_version(root)
    except FactoryError:
        return {"installed": False, "authenticated": False}
    return {"installed": True, "authenticated": git.gh_authenticated(root)}


def _stack(root: Path) -> list[str]:
    markers = {"python": ("pyproject.toml", "setup.py", "requirements.txt"), "node": ("package.json",),
               "go": ("go.mod",), "rust": ("Cargo.toml",)}
    return [name for name, files in markers.items() if any((root / f).exists() for f in files)]


def _checks(root: Path, configured: dict[str, Any] | None) -> dict[str, str]:
    """Check commands from the config, package.json, pyproject.toml or the Makefile; "ask" if
    there is no evidence."""
    sources = [dict((configured or {}).get("checks", {})), _node_checks(root), _python_checks(root),
               _make_checks(root)]
    return {kind: next((s[kind] for s in sources if kind in s), "ask") for kind in CHECK_KINDS}


def _node_checks(root: Path) -> dict[str, str]:
    path = root / "package.json"
    if not path.exists():
        return {}
    try:
        scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts", {})
    except (json.JSONDecodeError, AttributeError):
        return {}
    runner = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
    return {kind: f"{runner} run {kind}" for kind in CHECK_KINDS if kind in scripts}


def _python_checks(root: Path) -> dict[str, str]:
    path = root / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        tools = tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {})
    except tomllib.TOMLDecodeError:
        return {}
    found = {}
    if "pytest" in tools:
        found["test"] = "pytest"
    if "ruff" in tools:
        found["lint"] = "ruff check ."
    if "mypy" in tools:
        found["typecheck"] = "mypy src" if (root / "src").is_dir() else "mypy ."
    return found


def _make_checks(root: Path) -> dict[str, str]:
    path = root / "Makefile"
    if not path.exists():
        return {}
    targets = set(re.findall(r"^([A-Za-z][\w-]*):", path.read_text(encoding="utf-8"), re.MULTILINE))
    return {kind: f"make {kind}" for kind in CHECK_KINDS if kind in targets}


def _instructions(root: Path) -> dict[str, Any]:
    skills = []
    for base in (".claude/skills", ".agents/skills"):
        if (root / base).is_dir():
            skills += sorted(p.relative_to(root).as_posix() for p in (root / base).iterdir() if p.is_dir())
    return {
        "agents_md": (root / "AGENTS.md").exists(),
        "claude_md": (root / "CLAUDE.md").exists(),
        "readme": any((root / name).exists() for name in ("README.md", "README.rst", "README")),
        "docs": _doc_files(root),
        "skills": skills,
        "other": [name for name in OTHER_INSTRUCTIONS if (root / name).exists()],
        "migrate": _migration(root),
    }


def _migration(root: Path) -> list[dict[str, str]]:
    """What another workflow left in the repository: instruction files, skills, agents and commands
    of other setups, task or progress trackers, and a docs index under another name. The agent reads
    each one and proposes keeping it, moving what is worth keeping into AGENTS.md or docs/ and
    deleting it, or turning pending work into designs."""
    found: list[dict[str, str]] = []
    for path in OTHER_INSTRUCTIONS:
        if (root / path).is_file():
            found.append({"path": path, "kind": "instructions"})
    for base in SKILL_BASES:
        if (root / base).is_dir():
            found += [{"path": p.relative_to(root).as_posix(), "kind": "skill"} for p in sorted((root / base).iterdir())
                      if p.is_dir() and not p.name.startswith("factory-")]
    for base in TOOL_DIRS:
        folder = root / base
        if folder.is_dir():
            kind = "agent" if base.endswith("agents") else "command" if "rules" not in base else "instructions"
            found += [{"path": p.relative_to(root).as_posix(), "kind": kind} for p in sorted(folder.rglob("*"))
                      if p.is_file()]
    found += [{"path": name, "kind": "tracker"} for name in TRACKERS if (root / name).is_file()]
    for base in TRACKER_DIRS:
        if (root / base).is_dir():
            found += [{"path": p.relative_to(root).as_posix(), "kind": "tracker"}
                      for p in sorted((root / base).rglob("*.md"))]
    if (root / "docs/README.md").is_file():
        found.append({"path": "docs/README.md", "kind": "docs-index"})
    return found


def _permission_warnings(root: Path) -> list[str]:
    """Claude Code settings that skip permission prompts (then approval rests on the skills' rules)."""
    warnings = []
    for path in (root / ".claude/settings.json", root / ".claude/settings.local.json",
                 Path.home() / ".claude/settings.json"):
        try:
            mode = json.loads(path.read_text(encoding="utf-8")).get("permissions", {}).get("defaultMode")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if mode == "bypassPermissions":
            warnings.append(f"{path} skips permission prompts: approving then rests only on the skills' rules")
    return warnings


# Names that older versions wrote into a repository's instructions, and what replaced them.
STALE_NAMES = (
    (re.compile(r"/factory-implement\b"), "/factory-implement", "/factory-build"),
    (re.compile(r"`factory (?:task|design|board|doctor|init|status)\b"), "the `factory` command", "`swfactory`"),
)


def _stale_references(root: Path) -> list[str]:
    """Instructions and docs that still name a renamed skill or command. Decisions are history
    and are not checked."""
    files = [root / name for name in ("AGENTS.md", "CLAUDE.md", "README.md")]
    files += sorted(p for p in (root / "docs").rglob("*.md") if "decisions" not in p.relative_to(root).parts)
    warnings = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern, old, new in STALE_NAMES:
            if pattern.search(text):
                warnings.append(f"{file.relative_to(root).as_posix()} still mentions {old}: it is now {new}")
    return warnings


# --- init: plan, apply, restore -------------------------------------------------

SKILL_BASES = (".claude/skills", ".agents/skills")
# Roles each skill reads; the conformance reviewer's role goes in its launch prompt instead.
SKILL_ROLES = {"factory-design": ("designer", "design_critic"),
               "factory-build": ("lead", "planner", "quality_reviewer"),
               "factory-quick": ("quality_reviewer",)}
ASK_RULES = ["Bash(swfactory design approve:*)", "Bash(swfactory design reject:*)"]
CODEX_RULES = ".codex/rules/factory.rules"  # Codex's equivalent of ASK_RULES
GITIGNORE_LINE = ".factory/state/"
SMALL_REPO_FILES = 30
DOCS_SMALL = ("conventions.md",)
DOCS_NORMAL = ("index.md", "product.md", "architecture.md", "conventions.md", "working-agreements.md",
               "decisions/index.md")
# Renamed in 2.0: the index of decisions is docs/decisions/index.md (a README is for people).
OLD_DECISIONS_INDEX = "docs/decisions/README.md"
# Other workflows a repository may have, offered for migration: instruction files, skills, agents
# and commands of other setups, and task or progress trackers.
TRACKERS = ("TODO.md", "TODO", "ROADMAP.md", "PLAN.md", "PLANS.md", "STATUS.md", "PROGRESS.md", "BACKLOG.md",
            "TASKS.md", "NOTES.md")
TRACKER_DIRS = ("tasks", "plans", "todo", ".tasks", "docs/plans", "docs/tasks", "docs/roadmap")
TOOL_DIRS = (".claude/agents", ".claude/commands", ".codex/prompts", ".cursor/rules")
DELETABLE_SUFFIXES = (".md", ".mdc", ".rst")
# Folders of other workflows whose files (any kind) /factory-init may delete when the person agrees;
# never the Factory's own skills.
MIGRATION_ROOTS = tuple(f"{base}/" for base in (*TOOL_DIRS, *TRACKER_DIRS)) + tuple(
    f"{base}/" for base in SKILL_BASES)
# Never deleted or modified by init: approved designs, decisions, working agreements.
PROTECTED = (".factory/", "docs/decisions/", "docs/working-agreements.md")
# The [templates] header (maybe with a comment) and its `"path" = "hash"` lines.
TEMPLATES_SECTION = re.compile(r'^[ \t]*\[templates\][^\n]*\n(?:[ \t]*(?:"[^"\n]*"[ \t]*=[^\n]*)?\n?)*', re.MULTILINE)
MARK = re.compile(r"<!-- factory-template: ([\w./-]+)@(\d+) -->")


def _template(path: str) -> str:
    return resources.files("swfactory").joinpath(f"templates/{path}").read_text(encoding="utf-8")


def _skill_files() -> dict[str, str]:
    """Relative path inside a skills folder -> content, for the skills and what they reference."""
    files: dict[str, str] = {}
    base = resources.files("swfactory").joinpath("templates/skills")
    for skill in sorted(p.name for p in base.iterdir() if p.is_dir()):
        stack = [(base.joinpath(skill), skill)]
        while stack:
            folder, rel = stack.pop()
            for entry in folder.iterdir():
                if entry.is_dir():
                    stack.append((entry, f"{rel}/{entry.name}"))
                elif entry.name.endswith(".md"):
                    files[f"{rel}/{entry.name}"] = entry.read_text(encoding="utf-8")
    for skill, roles in SKILL_ROLES.items():
        for role in roles:
            files[f"{skill}/references/roles/{role}.md"] = _template(f"roles/{role}.md")
    files["factory-design/references/design-template.md"] = _template("design/design.md")
    files["factory-build/references/adr-template.md"] = _template("docs/decisions/adr.md")
    return dict(sorted(files.items()))


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _diff(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))


def _read(root: Path, path: str) -> str | None:
    file = root / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


class Plan:
    """The four lists, the Orca actions and the exact content each write will have."""

    def __init__(self) -> None:
        self.create: list[dict[str, str]] = []
        self.modify: list[dict[str, str]] = []
        self.delete: list[dict[str, str]] = []
        self.respect: list[dict[str, str]] = []
        self.migrate: list[dict[str, str]] = []
        self.customized: list[dict[str, str]] = []
        self.orca: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.writes: dict[str, str] = {}
        self.tracked: dict[str, str] = {}  # skill files whose installed hash goes to [templates]
        self.recorded: dict[str, str] = {}  # hashes already in [templates]
        self.report: dict[str, Any] = {}
        self.deleted_content: dict[str, str] = {}

    def write(self, root: Path, path: str, new: str, reason: str) -> None:
        old = _read(root, path)
        if old == new:
            return
        self.writes[path] = new
        if old is None:
            self.create.append({"path": path})
        else:
            self.modify.append({"path": path, "reason": reason, "diff": _diff(path, old, new)})

    def data(self) -> dict[str, Any]:
        body = {"create": self.create, "modify": self.modify, "delete": self.delete, "respect": self.respect,
                "migrate": self.migrate, "customized": self.customized, "orca": self.orca, "warnings": self.warnings}
        fingerprint = json.dumps([body, self.writes, self.tracked, self.deleted_content], sort_keys=True)
        return {"plan_id": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12], **body}


def build_plan(root: Path, main_checkout: Path, deletes: list[tuple[str, str]]) -> Plan:
    """What `/factory-init` would do in this repo, without changing anything."""
    report = doctor(root, main_checkout)
    configured = config.load_config(root / ".factory" / "config.toml") if (root / ".factory").is_dir() else None
    recorded: dict[str, str] = dict((configured or {}).get("templates", {}))
    plan = Plan()
    plan.report, plan.recorded = report, recorded
    plan.migrate = report["instructions"]["migrate"]
    plan.warnings = report["warnings"] + [
        f"Codex asks before approve/reject through {CODEX_RULES} (a prompt rule; not verified on a real Codex): "
        "it loads only if this project is trusted in Codex"
    ]

    installed = {f"{base}/{rel}": content for rel, content in _skill_files().items() for base in SKILL_BASES}
    installed[CODEX_RULES] = _template("codex/factory.rules")
    for path, content in installed.items():
        current = _read(root, path)
        if current is None or current == content or recorded.get(path) == _sha(current):
            plan.write(root, path, content, "update to the current Factory template")
            plan.tracked[path] = _sha(content)
        else:
            plan.customized.append({"path": path, "diff": _diff(path, current, content)})

    _docs(root, plan)
    _deletions(root, plan, deletes)
    _settings(root, plan)
    gitignore = _read(root, ".gitignore")
    if gitignore is None or GITIGNORE_LINE not in gitignore.splitlines():
        text = (gitignore or "") + ("" if not gitignore or gitignore.endswith("\n") else "\n") + GITIGNORE_LINE + "\n"
        plan.write(root, ".gitignore", text, "ignore the Factory's local state")
    plan.write(root, ".factory/config.toml", _config_text(root, report, plan.tracked), "record template hashes")
    plan.orca = _orca_actions(root, main_checkout, report, plan)
    return plan


def _docs(root: Path, plan: Plan) -> None:
    _agents_section(root, plan)
    claude = _read(root, "CLAUDE.md")
    if claude is None:
        plan.write(root, "CLAUDE.md", _template("docs/CLAUDE.md"), "")
    elif "@AGENTS.md" not in claude.splitlines():
        plan.write(root, "CLAUDE.md", claude.rstrip("\n") + "\n\n@AGENTS.md\n", "import AGENTS.md")
    # The repo's own size: files the Factory installs (skills, config, docs) do not count.
    installed = {"AGENTS.md", "CLAUDE.md", "README.md", CODEX_RULES, *(f"docs/{doc}" for doc in DOCS_NORMAL)}
    factory_dirs = (".factory/", "docs/decisions/", *(f"{base}/factory-" for base in SKILL_BASES))
    own = [f for f in git.run(["ls-files"], root).splitlines()
           if f not in installed and not f.startswith(factory_dirs)]
    for doc in DOCS_SMALL if len(own) < SMALL_REPO_FILES else DOCS_NORMAL:
        path = f"docs/{doc}"
        if _read(root, path) is None:
            plan.write(root, path, _template(path), "")
    old_index = _read(root, OLD_DECISIONS_INDEX)
    renaming = old_index is not None and _read(root, "docs/decisions/index.md") is None
    for path in _existing_docs(root):
        if path not in plan.writes and not (renaming and path == OLD_DECISIONS_INDEX):
            plan.respect.append({"path": path, "reason": "existing documentation is kept and linked"})
    if _read(root, "README.md") is None and not (root / "README.rst").exists() and not (root / "README").exists():
        plan.write(root, "README.md", _template("docs/README.md"), "")
    if renaming and old_index is not None:
        plan.write(root, "docs/decisions/index.md", old_index.replace("docs/decisions/README.md@1",
                                                                     "docs/decisions/index.md@1"), "")
        for path in ["AGENTS.md", *(p for p in _doc_files(root) if "/" not in p.removeprefix("docs/"))]:
            text = plan.writes.get(path, _read(root, path))
            if text and "decisions/README.md" in text:
                plan.write(root, path, text.replace("decisions/README.md", "decisions/index.md"),
                           "the decisions index is now docs/decisions/index.md")


def _doc_files(root: Path) -> list[str]:
    folder = root / "docs"
    return sorted(p.relative_to(root).as_posix() for p in folder.rglob("*.md")) if folder.is_dir() else []


def _existing_docs(root: Path) -> list[str]:
    return [p for p in _doc_files(root) if (root / p).read_text(encoding="utf-8").strip()]


AGENTS_SECTION = "## Software Factory"
AGENTS_KEY = "AGENTS.md#software-factory"  # its hash in [templates]
# The Factory section of AGENTS.md as older versions installed it: replaced like an untouched file.
PAST_AGENTS_SECTIONS = frozenset({
    "sha256:94cb0a75b1a2bae65ff385032f3819a94fc9779970d1f5a057e4a6d4a3fde8a8",  # 2.0
    "sha256:7dad3f117ff19a39e4517f4e6ccf2874614baeb5fc402a76dc38c47d31665d55",  # during 2.0 development
    "sha256:f5805cf88ee68ff2dd23f7def59327853c635052999f6a6c90af641de31616e1",  # 1.0 and 1.1
    "sha256:100f93f3f4e3c5c124477223bd74991fe849eaba1f73918caef866c3c0c6c4d1",  # before 1.0
    "sha256:7e78452d455d473ec953fed3801fc7ef71f2f1eb0fac64a291c8ed3d3a468ca6",  # before 1.0
})


def _section_span(text: str) -> tuple[int, int] | None:
    """Where the Factory section of AGENTS.md is: from its heading (maybe with more words) to the
    next `## ` heading."""
    match = re.search(rf"^{AGENTS_SECTION}\b[^\n]*$", text, re.MULTILINE)
    if match is None:
        return None
    following = re.search(r"^## ", text[match.end():], re.MULTILINE)
    return match.start(), match.end() + following.start() if following else len(text)


def _section(text: str) -> str | None:
    span = _section_span(text)
    return text[span[0]:span[1]].rstrip() if span else None


def _agents_section(root: Path, plan: Plan) -> None:
    """Create AGENTS.md, add the Factory section to it, or bring that section up to date when the
    person has not edited it (its recorded hash, or a version an older Factory installed)."""
    template = _template("docs/AGENTS.md")
    wanted = _section(template)
    assert wanted is not None
    agents = _read(root, "AGENTS.md")
    if agents is None:
        plan.write(root, "AGENTS.md", template, "")
        plan.tracked[AGENTS_KEY] = _sha(wanted)
        return
    span = _section_span(agents)
    if span is None:
        plan.write(root, "AGENTS.md", agents.rstrip("\n") + "\n\n" + wanted + "\n", "add the Factory section")
        plan.tracked[AGENTS_KEY] = _sha(wanted)
        return
    current = agents[span[0]:span[1]].rstrip()
    rest = agents[span[1]:]
    updated = agents[:span[0]] + wanted + ("\n\n" + rest if rest.strip() else "\n")
    if current == wanted or _sha(current) in (plan.recorded.get(AGENTS_KEY), *PAST_AGENTS_SECTIONS):
        plan.write(root, "AGENTS.md", updated, "update the Factory section to the current template")
        plan.tracked[AGENTS_KEY] = _sha(wanted)
    else:
        plan.customized.append({"path": "AGENTS.md", "diff": _diff("AGENTS.md", agents, updated)})


def _deletions(root: Path, plan: Plan, deletes: list[tuple[str, str]]) -> None:
    proposed: dict[str, str] = {}
    current = set(_skill_files())
    for base in SKILL_BASES:
        folder = root / base
        if not folder.is_dir():
            continue
        for skill in folder.iterdir():
            if not skill.name.startswith("factory-"):
                continue
            for file in sorted(skill.rglob("*")):
                if (file.is_file() and file.relative_to(folder).as_posix() not in current
                        and MARK.search(file.read_text(encoding="utf-8", errors="ignore"))):
                    proposed[file.relative_to(root).as_posix()] = "leftover from an earlier Factory version"
    if (root / "docs").is_dir():
        for file in sorted((root / "docs").rglob("*.md")):
            if not file.read_text(encoding="utf-8").strip():
                proposed[file.relative_to(root).as_posix()] = "empty document"
    if (root / OLD_DECISIONS_INDEX).is_file() and (plan.writes.get("docs/decisions/index.md")
                                                   or (root / "docs/decisions/index.md").is_file()):
        proposed[OLD_DECISIONS_INDEX] = "renamed to docs/decisions/index.md"
    for path, reason in deletes:
        for name in _deletable_files(root, path):
            proposed[name] = reason
    plan.delete = [{"path": p, "reason": r} for p, r in sorted(proposed.items())]
    plan.deleted_content = {p: _sha((root / p).read_text(encoding="utf-8", errors="replace")) for p in proposed}


def safe_path(root: Path, path: str) -> str:
    """`path` as a clean repo-relative path; refused if it leaves the repository or goes through a
    symbolic link."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise FactoryError("unsafe_path", f"{path} is not a path inside the repository", EXIT_PRECONDITION)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise FactoryError("unsafe_path", f"{path} goes through a symbolic link", EXIT_PRECONDITION)
    return relative.as_posix()


def _deletable_files(root: Path, path: str) -> list[str]:
    """`path`, or every file of a folder of another workflow (a skill, agents, commands, trackers)."""
    try:
        clean = safe_path(root, path)
    except FactoryError as exc:
        raise FactoryError("delete_not_allowed", exc.message, EXIT_PRECONDITION) from exc
    if (root / clean).is_dir() and clean.startswith(MIGRATION_ROOTS):
        return [_check_deletable(root, p.relative_to(root).as_posix())
                for p in sorted((root / clean).rglob("*")) if p.is_file()]
    return [_check_deletable(root, clean)]


def _check_deletable(root: Path, path: str) -> str:
    """Only documentation and instruction files may be deleted; never code, tests, CI, secrets,
    approved designs, decisions or working agreements."""
    try:
        path = safe_path(root, path)
    except FactoryError as exc:
        raise FactoryError("delete_not_allowed", exc.message, EXIT_PRECONDITION) from exc
    file = root / path
    parts = Path(path).parts
    allowed = (
        file.is_file()
        and not is_secret_file(path)
        and not ({"tests", "test"} & set(parts))
        and not path.startswith((".github/workflows/", ".gitlab-ci", ".circleci/", *PROTECTED,
                                 *(f"{base}/factory-" for base in SKILL_BASES)))
        and (file.suffix in DELETABLE_SUFFIXES or path in OTHER_INSTRUCTIONS or path in TRACKERS
             or path.startswith(MIGRATION_ROOTS))
    )
    if not allowed:
        message = f"{path} cannot be deleted by /factory-init (missing, code, tests, CI, a secret or protected)"
        raise FactoryError("delete_not_allowed", message, EXIT_PRECONDITION)
    return path


def _settings(root: Path, plan: Plan) -> None:
    """`permissions.ask` for approve and reject in Claude Code, merged into existing settings."""
    current = _read(root, ".claude/settings.json")
    try:
        settings = json.loads(current) if current else {}
        ask = settings.setdefault("permissions", {}).setdefault("ask", [])
        if not isinstance(ask, list):
            raise TypeError("permissions.ask is not a list")
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        plan.warnings.append(f".claude/settings.json could not be merged ({exc}): add the 'ask' rules by hand")
        plan.respect.append({"path": ".claude/settings.json", "reason": "not valid settings JSON"})
        return
    ask.extend(rule for rule in ASK_RULES if rule not in ask)
    plan.write(root, ".claude/settings.json", json.dumps(settings, indent=2) + "\n", "always ask before approve/reject")


def _config_text(root: Path, report: dict[str, Any], tracked: dict[str, str]) -> str:
    """`.factory/config.toml`: created from the defaults and detected checks; an existing one keeps
    everything and only its `[templates]` section is replaced."""
    current = _read(root, ".factory/config.toml")
    if current is None:
        checks = {k: v for k, v in report["checks"].items() if v != "ask"}
        d = config.DEFAULTS
        lines = [
            f"schema_version = {d['schema_version']}", "",
            "[orca]", f"min_version = {json.dumps(d['orca']['min_version'])}", "",
            "[checks]", *[f"{k} = {json.dumps(v)}" for k, v in checks.items()], "",
            "[review]", *[f"{k} = {json.dumps(v)}" for k, v in d["review"].items()], "",
            "[git]", *[f"{k} = {json.dumps(v)}" for k, v in d["git"].items()], "",
            "[language]", f"artifacts = {json.dumps(d['language']['artifacts'])}", "",
            "[board]", *[f"{k} = {json.dumps(v)}" for k, v in d["board"].items()], "",
            "[agents]", *[f"{k} = {json.dumps(v)}" for k, v in d["agents"].items()], "",
        ]
        current = "\n".join(lines)
    body = TEMPLATES_SECTION.sub("", current.replace("\r\n", "\n")).rstrip("\n")
    table = "\n".join(f"{json.dumps(path)} = {json.dumps(sha)}" for path, sha in sorted(tracked.items()))
    return f"{body}\n\n[templates]\n{table}\n"


def _orca_actions(root: Path, main_checkout: Path, report: dict[str, Any], plan: Plan) -> list[dict[str, Any]]:
    info = report["orca"]
    if not info.get("ok"):
        plan.warnings.append(f"Orca actions skipped: {info.get('problem')}")
        return []
    actions: list[dict[str, Any]] = []
    if not info.get("repo_registered"):
        actions.append({"id": "repo-add", "command": ["repo", "add", "--path", str(main_checkout)]})
    if not info.get("base_branch"):
        ref = f"origin/{_default_branch(root)}"
        actions.append({"id": "set-base-ref",
                        "command": ["repo", "set-base-ref", "--repo", f"path:{main_checkout}", "--ref", ref]})
    verb = "update" if info.get("orca_skills_installed") else "install"
    actions.append({"id": f"skills-{verb}", "command": ["skills", verb, "--skill", "orca-cli"], "global": True})
    return actions


def _default_branch(root: Path) -> str:
    try:
        return git.run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root).removeprefix("origin/")
    except FactoryError:
        return git.current_branch(root) or "main"


def apply_init(
    root: Path, main_checkout: Path, state_dir: Path, plan_id: str, deletes: list[tuple[str, str]], excludes: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Apply exactly the confirmed plan (same id), minus the excluded items, with backups. Nothing
    is committed."""
    plan = build_plan(root, main_checkout, deletes)
    data = plan.data()
    if data["plan_id"] != plan_id:
        message = "the repository changed since that plan was shown (or the --delete options differ): plan again"
        raise FactoryError("plan_changed", message, EXIT_PRECONDITION)

    def excluded(path: str) -> bool:
        return any(path == ex.rstrip("/") or path.startswith(ex.rstrip("/") + "/") for ex in excludes)

    # The [templates] table keeps the old hash of an excluded skill file, so it is not seen as customized later.
    def file_of(key: str) -> str:
        return key.split("#", 1)[0]  # AGENTS_KEY tracks a section of AGENTS.md

    tracked = {p: h for p, h in plan.tracked.items() if not excluded(file_of(p))}
    tracked |= {p: plan.recorded[p] for p in plan.tracked if excluded(file_of(p)) and p in plan.recorded}
    config_path = ".factory/config.toml"
    config_text = _config_text(root, plan.report, tracked)
    if not excluded(config_path):
        plan.create = [i for i in plan.create if i["path"] != config_path]
        plan.modify = [i for i in plan.modify if i["path"] != config_path]
        plan.write(root, config_path, config_text, "record template hashes")

    created = [safe_path(root, i["path"]) for i in plan.create if not excluded(i["path"])]
    modified = [safe_path(root, i["path"]) for i in plan.modify if not excluded(i["path"])]
    deleted = [safe_path(root, i["path"]) for i in plan.delete if not excluded(i["path"])]
    backup = _backup_dir(state_dir)
    for path in modified + deleted:
        target = backup / "files" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, target)
    manifest = {"created": created, "modified": modified, "deleted": deleted}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    for path in created + modified:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(plan.writes[path], encoding="utf-8", newline="\n")  # LF on every OS
    for path in deleted:
        (root / path).unlink()
        _remove_empty_parents(root, Path(path).parent)

    results = []
    warnings = list(plan.warnings)
    for action in plan.orca:
        if excluded(action["id"]):
            continue
        try:
            command = action["command"]
            orca.run_plain(command) if command[0] == "skills" else orca.run(command)
            results.append({"id": action["id"], "ok": True})
        except FactoryError as exc:
            results.append({"id": action["id"], "ok": False, "error": exc.message})
            warnings.append(f"Orca action {action['id']} failed: {exc.message}")
    data = {"backup": backup.name, "created": created, "modified": modified, "deleted": deleted,
            "customized": [c["path"] for c in plan.customized], "orca": results}
    return data, warnings


def _remove_empty_parents(root: Path, folder: Path) -> None:
    """Remove the folders a deletion left empty (a deleted skill's folder), up to the repository."""
    while folder.parts and (root / folder).is_dir() and not any((root / folder).iterdir()):
        (root / folder).rmdir()
        folder = folder.parent


def _backup_dir(state_dir: Path) -> Path:
    base = state_dir / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate, n = base, 1
    while candidate.exists():
        candidate, n = base.with_name(f"{base.name}-{n}"), n + 1
    candidate.mkdir(parents=True)
    return candidate


def restore(root: Path, state_dir: Path, name: str) -> dict[str, Any]:
    """Put back every file a run of `init --apply` modified or deleted."""
    backup = state_dir / "backups" / name
    manifest_file = backup / "manifest.json"
    if not manifest_file.is_file():
        raise FactoryError("backup_not_found", f"no backup named {name} in {state_dir / 'backups'}", EXIT_PRECONDITION)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    restored = []
    for path in manifest["modified"] + manifest["deleted"]:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / "files" / path, root / path)
        restored.append(path)
    return {"backup": name, "restored": restored, "created_left": manifest["created"]}


def install_skill(home: Path) -> dict[str, Any]:
    """Install the `factory-init` skill for this user (Claude and Codex), so `/factory-init` works in
    a repository that has no Factory yet."""
    files = {rel: content for rel, content in _skill_files().items() if rel.startswith("factory-init/")}
    written = []
    for base in (".claude/skills", ".agents/skills"):
        for rel, content in files.items():
            target = home / base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8", newline="\n")
                written.append(str(target))
    return {"installed": [str(home / base / "factory-init") for base in (".claude/skills", ".agents/skills")],
            "written": written}
