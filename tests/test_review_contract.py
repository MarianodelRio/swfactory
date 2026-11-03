"""Coverage, finding continuity and budgets operate on the durable review lifecycle."""

import json

import pytest
from review_support import coverage, plan
from test_claim_launch import TASK
from test_review import MAJOR
from test_review_runs import ctx as ctx  # noqa: F401
from test_review_runs import project as project  # noqa: F401
from test_review_runs import report, reserve
from test_review_runs import working as working  # noqa: F401

from swfactory import reviews, tasks
from swfactory.output import FactoryError


def deliver(ctx, tmp_path, run, verdict="pass", findings=(), **fields):
    path = report(tmp_path, run, verdict, findings)
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))
    return reviews.complete(TASK, run["review_id"], path, ctx)[0]


def conform(ctx, tmp_path):
    cwd = tasks.workspace_path(ctx.store.read_task(TASK))
    test = cwd / "tests/bookings/test_recurrence.py"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text("def test_weekly():\n    assert len(range(4)) == 4\n")
    tasks.git.run(["add", str(test)], cwd)
    tasks.git.run(["commit", "-m", "add acceptance fixture"], cwd)
    quality = reserve(ctx)
    deliver(ctx, tmp_path, quality)
    tasks.verify(TASK, ctx)
    # Manual acceptance evidence is still required, even with a shared green suite.
    manual = tmp_path / "manual.txt"
    manual.write_text("observed the expected calendar")
    tasks.verify_manual(TASK, "AC-02", manual, ctx)
    plan(ctx, TASK, tmp_path / "plan.json")
    return reserve(ctx, "conformance")


def status(ctx):
    return reviews.gates(tasks.catalog(ctx)[TASK], ctx.store.read_task(TASK), tasks._config(ctx), ctx)


def test_empty_conformance_pass_cannot_complete(ctx, tmp_path):
    run = conform(ctx, tmp_path)
    with pytest.raises(FactoryError, match="coverage"):
        deliver(ctx, tmp_path, run)
    assert status(ctx)["conformance"] == "running"


@pytest.mark.parametrize("change", ["missing", "unknown", "unverified", "deviation", "wrong_evidence", "no_bound"])
def test_incomplete_or_misbound_conformance_is_rejected(ctx, tmp_path, change):
    run = conform(ctx, tmp_path)
    rows = coverage(run["run"])
    deviations = []
    if change == "missing":
        rows.pop("AC-01")
    elif change == "unknown":
        rows["AC-99"] = rows["AC-01"]
    elif change == "unverified":
        rows["AC-01"]["status"] = "unverified"
    elif change == "deviation":
        deviations = ["a seventh public operation was added"]
    elif change == "wrong_evidence":
        rows["AC-01"]["evidence"] = ["evidence/not-reserved.txt"]
    else:
        rows["AC-01"]["evidence"] = [f"git:{run['run']['commit']}"]
    with pytest.raises(FactoryError):
        deliver(ctx, tmp_path, run, coverage=rows, deviations=deviations)
    assert len(ctx.store.read_task(TASK)["reviews"]) == 1  # only quality


def test_complete_conformance_uses_reserved_sources_and_is_idempotent(ctx, tmp_path):
    run = conform(ctx, tmp_path)
    snapshot = run["run"]
    assert "Acceptance criteria" in snapshot["design_text"]
    assert snapshot["verification_digest"] and snapshot["proof_plan"]
    fields = {"coverage": coverage(snapshot), "deviations": []}
    first = deliver(ctx, tmp_path, run, **fields)
    assert deliver(ctx, tmp_path, run, **fields)["review"] == first["review"]
    assert status(ctx)["conformance"] == "passed"
    evidence = snapshot["acceptance_evidence"]["AC-02"]
    (ctx.store.root / evidence).unlink()
    assert status(ctx)["conformance"] == "evidence_missing"


def test_new_verification_definition_invalidates_review_context(ctx, tmp_path):
    run = reserve(ctx)
    deliver(ctx, tmp_path, run)
    cfg = ctx.main_checkout / ".factory/config.toml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text('[checks]\ntest = "python --version"\n')
    assert status(ctx)["quality"] == "stale"


def test_compiled_task_cannot_silently_drop_an_approved_obligation(ctx):
    import yaml

    task = tasks.catalog(ctx)[TASK]
    path = ctx.main_checkout / task["design"]
    compiled = path.parent / "tasks.yaml"
    data = yaml.safe_load(compiled.read_text())
    data["tasks"][0]["acceptance"].pop("AC-02")
    compiled.write_text(yaml.safe_dump(data))
    with pytest.raises(FactoryError, match="approved design"):
        reserve(ctx)


def test_passes_do_not_spend_failure_allowance(ctx, tmp_path):
    for _ in range(2):
        deliver(ctx, tmp_path, reserve(ctx))
    failed = deliver(ctx, tmp_path, reserve(ctx), "fail", [MAJOR])
    assert failed["state"] == "in_review"
    saved = ctx.store.read_task(TASK)
    assert saved["review_diagnosis"]["failed_rounds"] == 1
    assert saved["review_diagnosis"]["cause"] == "undetermined"


def test_findings_require_explicit_resolution_and_survive_continuation(ctx, tmp_path):
    first = deliver(ctx, tmp_path, reserve(ctx), "fail", [MAJOR])
    finding_id = first["review"]["findings"][0]["id"]
    second = reserve(ctx)
    with pytest.raises(FactoryError, match="assess"):
        deliver(ctx, tmp_path, second)
    partial = {finding_id: {"status": "partial", "reason": "move is fixed but copy retry still duplicates"}}
    failed = deliver(ctx, tmp_path, second, "fail", finding_status=partial,
                     diagnosis={"cause": "incomplete_fix", "explanation": "copy retry is still uncovered",
                                "evidence": ["tests/test_retry.py::test_copy_retry"]})
    assert failed["state"] == "blocked"
    assert ctx.store.read_task(TASK)["review_diagnosis"]["failed_rounds"] == 2
    tasks.unblock(TASK, ctx)
    assert ctx.store.read_task(TASK)["review_period"] == 1
    third = reserve(ctx)
    assert third["run"]["previous_findings"][finding_id]["status"] == "partial"
    resolution = {finding_id: {"status": "resolved", "reason": "retry test now checks no duplicate copy"}}
    deliver(ctx, tmp_path, third, finding_status=resolution)
    assert status(ctx)["quality"] == "passed"
    assert deliver(ctx, tmp_path, reserve(ctx), "fail", [MAJOR])["state"] == "in_review"


def test_disputed_finding_cannot_pass(ctx, tmp_path):
    first = deliver(ctx, tmp_path, reserve(ctx), "fail", [MAJOR])
    finding_id = first["review"]["findings"][0]["id"]
    with pytest.raises(FactoryError, match="unresolved"):
        deliver(ctx, tmp_path, reserve(ctx), finding_status={
            finding_id: {"status": "disputed", "reason": "the lead disagrees"}})


def test_assessment_cannot_lower_the_severity_of_an_unresolved_finding(ctx, tmp_path):
    first = deliver(ctx, tmp_path, reserve(ctx), "fail", [MAJOR])
    finding_id = first["review"]["findings"][0]["id"]
    with pytest.raises(FactoryError, match="assessment"):
        deliver(ctx, tmp_path, reserve(ctx), "fail", finding_status={
            finding_id: {"status": "partial", "reason": "not fixed", "severity": "minor"}})


def test_late_prior_period_result_cannot_consume_new_allowance(ctx, tmp_path):
    run = reserve(ctx)
    ctx.store.update_task(TASK, lambda s: tasks.transition(s, "block", reason=tasks.REVIEW_NOT_CONVERGING), "test")
    tasks.unblock(TASK, ctx)
    result = deliver(ctx, tmp_path, run, "fail", [MAJOR])
    assert result["review"]["historical"] and result["state"] == "in_review"
    assert ctx.store.read_task(TASK).get("review_diagnosis") is None


def test_legacy_import_cannot_override_managed_result(ctx, tmp_path):
    run = reserve(ctx)
    deliver(ctx, tmp_path, run)
    with pytest.raises(FactoryError, match="managed"):
        tasks.record_review(TASK, report(tmp_path, run, "fail", [MAJOR]), None, ctx)


def test_legacy_delivery_retry_is_idempotent(ctx, tmp_path):
    from test_review import review_file

    path = tmp_path / "legacy.json"
    review_file(tmp_path, verdict="fail", findings=[MAJOR], name=path.name)
    first = tasks.record_review(TASK, path, None, ctx)[0]
    second = tasks.record_review(TASK, path, None, ctx)[0]
    assert first["review"] == second["review"]
    assert second["state"] == "in_review"
    assert len(ctx.store.read_task(TASK)["reviews"]) == 1


def test_failed_conformance_can_record_unverified_coverage(ctx, tmp_path):
    run = conform(ctx, tmp_path)
    rows = coverage(run["run"])
    rows["AC-01"]["status"] = "unverified"
    result = deliver(ctx, tmp_path, run, "fail", [MAJOR], coverage=rows, deviations=["contract differs"])
    assert result["review"]["coverage"]["AC-01"]["status"] == "unverified"
    assert result["state"] == "in_review"


def test_unresolved_old_findings_get_stable_readable_ids(ctx):
    ctx.store.update_task(TASK, lambda s: {**s, "reviews": [
        {"type": "quality", "verdict": "fail", "findings": [MAJOR]}]}, "old-state")
    run = reserve(ctx)
    old = run["run"]["previous_findings"]
    assert list(old) == ["history-quality-1.F1"]


def test_manual_evidence_must_cover_current_behavior(ctx, tmp_path, working):
    old = conform(ctx, tmp_path)
    reviews.cancel(TASK, old["review_id"], "correct behavior before review", ctx)
    cwd = tasks.workspace_path(ctx.store.read_task(TASK))
    working.commit("src/change.py", "print('changed')", "change after manual observation", cwd=cwd)
    tasks.verify(TASK, ctx)  # refreshes automatic evidence, not the manual observation
    deliver(ctx, tmp_path, reserve(ctx))
    run = reserve(ctx, "conformance")
    assert run["run"]["acceptance_status"]["AC-02"] == "stale"
    with pytest.raises(FactoryError, match="verification did not pass"):
        deliver(ctx, tmp_path, run, coverage=coverage(run["run"]), deviations=[])


def test_failure_without_a_diagnosis_is_not_silently_accepted(ctx, tmp_path):
    run = reserve(ctx)
    path = report(tmp_path, run, "fail", [MAJOR])
    data = json.loads(path.read_text())
    data.pop("diagnosis")
    path.write_text(json.dumps(data))
    with pytest.raises(FactoryError, match="diagnosis"):
        reviews.complete(TASK, run["review_id"], path, ctx)


def test_old_conformance_pass_is_readable_but_needs_coverage(ctx):
    config = ctx.main_checkout / ".factory/config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[review]\nconformance = "always"\n')
    record = ctx.store.read_task(TASK)
    commit = tasks.git.run(["rev-parse", "HEAD"], tasks.workspace_path(record))
    ctx.store.update_task(TASK, lambda s: {**s, "reviews": [{"type": "conformance", "commit": commit,
                          "verdict": "pass", "findings": []}]}, "legacy")
    assert status(ctx)["conformance"] == "coverage_missing"
