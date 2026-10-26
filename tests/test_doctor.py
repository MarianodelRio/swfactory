import hashlib
import json
from pathlib import Path

import pytest

from swfactory.cli import main


def snapshot(root: Path) -> dict[str, str]:
    """Every file of the repository (including .git/index) with its hash."""
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and (".git" not in path.relative_to(root).parts or path.name == "index"):
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def doctor(capsys):
    code = main(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    return out["data"]


@pytest.fixture
def project(repo, fake_orca, fake_gh, monkeypatch):
    monkeypatch.chdir(repo.root)
    return repo


def test_python_repo(project, capsys):
    (project.root / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[tool.pytest.ini_options]\n[tool.ruff]\n[tool.mypy]\n')
    (project.root / "src/x").mkdir(parents=True)
    report = doctor(capsys)
    assert report["stack"] == ["python"]
    assert report["checks"] == {"test": "pytest", "lint": "ruff check .", "typecheck": "mypy src",
                                "build": "ask"}


def test_node_repo(project, capsys):
    (project.root / "package.json").write_text(json.dumps(
        {"scripts": {"test": "vitest", "lint": "eslint .", "build": "vite build"}}))
    (project.root / "pnpm-lock.yaml").write_text("")
    report = doctor(capsys)
    assert report["stack"] == ["node"]
    assert report["checks"] == {"test": "pnpm run test", "lint": "pnpm run lint", "typecheck": "ask",
                                "build": "pnpm run build"}


def test_makefile_targets(project, capsys):
    (project.root / "Makefile").write_text("test:\n\tgo test ./...\nlint:\n\tgolangci-lint run\n")
    assert doctor(capsys)["checks"] == {"test": "make test", "lint": "make lint", "typecheck": "ask",
                                        "build": "ask"}


def test_empty_repo(project, capsys):
    report = doctor(capsys)
    assert report["stack"] == []
    assert set(report["checks"].values()) == {"ask"}
    assert report["git"]["branch"] == "main"
    assert report["git"]["pending_changes"] == 0
    assert report["factory"]["installed"] is False
    assert report["instructions"] == {"agents_md": False, "claude_md": False, "readme": True, "docs": [],
                                      "skills": [], "other": [], "migrate": []}


def test_orca_gh_and_agents(project, capsys):
    report = doctor(capsys)
    assert report["orca"]["ok"] is True
    assert report["orca"]["version"] == "1.4.229"
    assert report["orca"]["repo_registered"] is True
    assert report["orca"]["base_branch"] == "main"
    assert report["orca"]["orca_skills_installed"] is True
    assert report["agents"] == {"claude": True, "codex": False}
    assert report["gh"] == {"installed": True, "authenticated": True}


def test_orca_problems_are_reported_not_raised(project, fake_orca, capsys):
    fake_orca.fail("status", code="runtime_unavailable", message="Orca is not running")
    report = doctor(capsys)
    assert report["orca"]["ok"] is False
    assert "Orca is not running" in report["orca"]["problem"]
    assert any("Orca" in p for p in report["problems"])


def test_already_initialized(project, capsys):
    (project.root / ".factory").mkdir()
    (project.root / ".factory/config.toml").write_text('schema_version = 1\n[checks]\ntest = "pytest"\n')
    (project.root / "AGENTS.md").write_text("<!-- factory-template: docs/AGENTS.md@1 -->\n# AGENTS\n")
    (project.root / "CLAUDE.md").write_text("@AGENTS.md\n")
    (project.root / ".claude/skills/factory-design").mkdir(parents=True)
    (project.root / ".claude/skills/factory-design/SKILL.md").write_text("x")
    (project.root / ".cursorrules").write_text("old rules")
    (project.root / "docs").mkdir()
    (project.root / "docs/architecture.md").write_text("# Architecture\n")
    report = doctor(capsys)
    assert report["factory"]["installed"] is True
    assert report["checks"]["test"] == "pytest"  # the configured value wins
    assert report["instructions"]["agents_md"] is True
    assert report["instructions"]["skills"] == [".claude/skills/factory-design"]
    assert report["instructions"]["other"] == [".cursorrules"]
    assert report["instructions"]["docs"] == ["docs/architecture.md"]


def test_permission_bypass_warning(project, capsys):
    (project.root / ".claude").mkdir()
    settings = {"permissions": {"defaultMode": "bypassPermissions"}}
    (project.root / ".claude/settings.json").write_text(json.dumps(settings))
    assert any("permission" in w for w in doctor(capsys)["warnings"])


def test_doctor_changes_nothing(project, capsys):
    (project.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (project.root / "untracked.txt").write_text("x")
    before = snapshot(project.root)
    doctor(capsys)
    assert snapshot(project.root) == before


def test_no_ask_warning_once_configured(project, capsys):
    (project.root / ".factory").mkdir()
    (project.root / ".factory/config.toml").write_text('schema_version = 1\n[checks]\ntest = "pytest"\n')
    assert not any("check commands" in w for w in doctor(capsys)["warnings"])


def test_stale_skill_and_command_names_are_warned(project, capsys):
    (project.root / "AGENTS.md").write_text("Use /factory-implement CHG-3.1.\n")
    (project.root / "docs/how").mkdir(parents=True)
    (project.root / "docs/how/flow.md").write_text("Run `factory task verify`.\n")
    (project.root / "docs/decisions").mkdir()
    (project.root / "docs/decisions/0001-x.md").write_text("We chose /factory-implement.\n")
    (project.root / "docs/index.md").write_text("Use /factory-build and `swfactory task verify`.\n")
    warnings = doctor(capsys)["warnings"]
    assert "AGENTS.md still mentions /factory-implement: it is now /factory-build" in warnings
    assert any(w.startswith("docs/how/flow.md still mentions the `factory` command") for w in warnings)
    assert not any("decisions" in w or "index.md" in w for w in warnings)
