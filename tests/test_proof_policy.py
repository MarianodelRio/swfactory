"""Proof plans and documentation policy distinguish prose, instructions and executable docs."""


import pytest
from review_support import plan
from test_claim_launch import TASK
from test_review_runs import ctx as ctx  # noqa: F401
from test_review_runs import project as project  # noqa: F401
from test_review_runs import working as working  # noqa: F401

from swfactory import config, file_policy, git, proof, tasks
from swfactory.cli import main
from swfactory.output import FactoryError


def test_requirements_include_assigned_acs_and_global_constraints():
    text = "## Acceptance criteria\n- AC-01: first\n- AC-02: other task\n## Constraints\n- C-01: six actions\n"
    assert proof.requirements(text, {"acceptance": {"AC-01": "manual"}}) == {
        "AC-01": "first", "C-01": "six actions"}


def test_plan_needs_all_obligations_and_a_real_execution():
    required = {"AC-01": "before execution"}
    with pytest.raises(FactoryError):
        proof.validate_plan({}, required, {"check:test"})
    row = {"code": "execute.py", "scenario": "manifest changes after preview", "assertion": "Core not called",
           "verification": "check:missing", "discrimination": "preview-only guard fails this test"}
    with pytest.raises(FactoryError, match="execution"):
        proof.validate_plan({"AC-01": row}, required, {"check:test"})


def test_proof_cli_persists_and_masks_focused_evidence(ctx, tmp_path, capsys):
    path = tmp_path / "plan.json"
    plan(ctx, TASK, path)
    evidence = tmp_path / "focused.txt"
    evidence.write_text("expected assertion failed before fix; passed after fix\npassword=private-value")
    assert main(["task", "proof-plan", TASK, "--file", str(path), "--evidence", str(evidence), "--json"]) == 0
    capsys.readouterr()
    saved = ctx.store.read_task(TASK)["proof_plan"]
    output = (ctx.store.root / saved["evidence"][0]).read_text()
    assert "private-value" not in output and "expected assertion" in output
    assert saved["requirements"]["AC-01"]["assertion"]


def test_proof_refuses_secret_evidence(ctx, tmp_path):
    path = tmp_path / "plan.json"
    plan(ctx, TASK, path)
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret")
    with pytest.raises(FactoryError) as error:
        tasks.proof_plan(TASK, path, [secret], ctx)
    assert error.value.code == "secret_file_refused"


@pytest.mark.parametrize("path,allowed,behavior", [
    ("docs/conventions.md", True, False), ("harness/docs/conventions.md", True, False),
    ("harness/docs/example.py", False, True), ("harness/docs/contract.md", True, True),
    ("AGENTS.md", False, True), (".factory/config.toml", False, True),
])
def test_prose_permission_and_check_freshness_are_separate(path, allowed, behavior):
    cfg = {"memory": {"paths": ["docs/*.md", "harness/docs/*.md"],
                      "behavior_paths": ["harness/docs/contract.md"]}}
    assert file_policy.memory(path, cfg) == allowed
    assert file_policy.behavioral(path, cfg) == behavior
    outside = tasks._outside_scope({"scope": ["src/**"]}, [path], cfg)
    if not path.startswith(".factory/"):
        assert bool(outside) != allowed


def test_nested_prose_keeps_checks_but_invalidates_conformance(ctx, tmp_path, working):
    from test_review_contract import conform, deliver, status

    cfg = ctx.main_checkout / ".factory/config.toml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text('[memory]\npaths = ["harness/docs/*.md"]\n')
    run = conform(ctx, tmp_path)
    from review_support import coverage
    deliver(ctx, tmp_path, run, coverage=coverage(run["run"]), deviations=[])
    cwd = tasks.workspace_path(ctx.store.read_task(TASK))
    before = git.last_code_commit("main", cwd, tasks._config(ctx))
    working.commit("harness/docs/conventions.md", "lesson\n", "record lesson", cwd=cwd)
    assert git.last_code_commit("main", cwd, tasks._config(ctx)) == before
    assert status(ctx) == {"quality": "passed", "conformance": "stale"}
    working.commit("harness/docs/example.py", "print('changed')\n", "executable documentation", cwd=cwd)
    assert git.last_code_commit("main", cwd, tasks._config(ctx)) != before
    assert status(ctx)["quality"] == "stale"


def test_renaming_code_into_memory_still_invalidates_checks(repo):
    repo.commit("src/runtime.py", "print('behavior')", "runtime")
    base = git.run(["rev-parse", "HEAD"], repo.root)
    (repo.root / "docs").mkdir()
    git.run(["mv", "src/runtime.py", "docs/lesson.md"], repo.root)
    git.run(["commit", "-m", "move runtime into prose"], repo.root)
    assert git.last_code_commit(base, repo.root) != base


@pytest.mark.parametrize("text", ["[memory]\npaths = 'docs/**'", "[memory]\npaths = ['../outside']",
                                  "[review]\nmax_failed_rounds = 0", "memory = 7"])
def test_policy_and_budget_config_are_validated(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(FactoryError):
        config.load_config(path)


def test_new_failure_limit_and_legacy_alias(tmp_path):
    from swfactory.review_policy import limit

    path = tmp_path / "config.toml"
    path.write_text("[review]\nmax_rounds = 3\nmax_failed_rounds = 4\n")
    assert limit(config.load_config(path)) == 4
    path.write_text("[review]\nmax_rounds = 3\n")
    assert limit(config.load_config(path)) == 3


def test_constraints_cannot_disappear_from_a_conformance_pass():
    run = {"requirements": {"C-01": "exactly six operations"}, "evidence": ["git:target"],
           "acceptance_evidence": {}}
    with pytest.raises(FactoryError, match="every reserved requirement"):
        proof.validate_coverage({"verdict": "pass", "coverage": {}, "deviations": []}, run)
    row = {"status": "satisfied", "implementation": "api.py:OPERATIONS", "assertion": "exact set of six",
           "reason": "inspected the public registry and schema", "evidence": ["git:target"]}
    proof.validate_coverage({"verdict": "pass", "coverage": {"C-01": row}, "deviations": []}, run)


def test_duplicate_ac_ids_are_rejected_before_approval():
    from test_design_parse import KNOWN, VALID

    from swfactory.design import parse_design

    text = VALID.replace("## Approach", "- AC-01: a second conflicting definition\n\n## Approach")
    with pytest.raises(FactoryError, match="problem"):
        parse_design(text, "CHG-0012", KNOWN)
