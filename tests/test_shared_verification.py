import json
from unittest.mock import patch

import pytest
import yaml
from test_approve import CHANGE, FOLDER
from test_claim_launch import TASK
from test_design_parse import KNOWN, VALID
from test_finish import finish
from test_finish import ready_to_finish as ready_to_finish  # noqa: F401
from test_verify import DESIGN, cli, configure
from test_verify import claimed as claimed  # noqa: F401
from test_verify import store as store  # noqa: F401

from swfactory import design, tasks, verification
from swfactory.cli import main
from swfactory.output import FactoryError


def command(counter, exit_code=0):
    return ('python -c "from pathlib import Path; import sys; p=Path(sys.argv[1]); '
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
            f'raise SystemExit({exit_code})" "{counter.as_posix()}"')


def define(repo, acceptance, named=None):
    path = repo.root / FOLDER / "tasks.yaml"
    data = yaml.safe_load(path.read_text())
    data["tasks"][0]["acceptance"] = acceptance
    if named is not None:
        data["tasks"][0]["verification"] = named
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_one_check_serves_two_criteria_and_next_verify_runs_again(claimed, store, capsys, tmp_path):
    counter = tmp_path / "count"
    configure(claimed, {"test": command(counter)})
    define(claimed, {"AC-01": {"check": "test"}, "AC-02": {"check": "test"}})
    code, out = cli(capsys, "task", "verify", TASK)
    assert code == 0 and counter.read_text() == "1"
    record = store.read_task(TASK)
    shared = record["checks"]["evidence"]["test"]
    assert all(r["evidence"] == shared and r["execution"] == "check:test" for r in record["acceptance"].values())
    assert len(out["data"]["executions"]) == 1
    cli(capsys, "task", "verify", TASK)
    assert counter.read_text() == "2"
    assert store.read_task(TASK)["checks"]["evidence"]["test"] != shared


def test_named_verification_runs_once_and_unused_definition_is_not_executed(claimed, store, capsys, tmp_path):
    counter = tmp_path / "count"
    configure(claimed, {"test": 'python -c "print(1)"'})
    define(claimed, {"AC-01": {"verification": "suite"}, "AC-02": {"verification": "suite"}},
           {"suite": command(counter), "unused": "not-a-program"})
    code, out = cli(capsys, "task", "verify", TASK)
    assert code == 0 and counter.read_text() == "1"
    assert len(out["data"]["executions"]) == 2  # project test + suite
    assert store.read_task(TASK)["acceptance"]["AC-01"]["evidence"] == \
        store.read_task(TASK)["acceptance"]["AC-02"]["evidence"]


@pytest.mark.parametrize("cmd,expected", [('python -c "raise SystemExit(2)"', "failed"),
                                         ("missing-shared-verification-executable", "not_run")])
def test_shared_nonpass_propagates_to_every_obligation(claimed, store, capsys, cmd, expected):
    configure(claimed, {"test": cmd})
    define(claimed, {"AC-01": {"check": "test"}, "AC-02": {"check": "test"}})
    cli(capsys, "task", "verify", TASK)
    record = store.read_task(TASK)
    assert record["checks"]["status"] == expected
    assert {a["status"] for a in record["acceptance"].values()} == {expected}


def test_missing_reference_fails_before_any_execution(claimed, capsys):
    configure(claimed, {"test": 'python -c "print(1)"'})
    define(claimed, {"AC-01": {"check": "missing"}})
    with patch("swfactory.tasks._run_evidence") as execute:
        code, out = cli(capsys, "task", "verify", TASK)
    assert code == 3 and out["error"]["code"] == "verification_invalid"
    execute.assert_not_called()


def test_identical_legacy_commands_stay_independent(claimed, capsys, tmp_path):
    counter = tmp_path / "count"
    cmd = command(counter)
    configure(claimed, {"test": cmd})
    define(claimed, {"AC-01": cmd, "AC-02": cmd})
    cli(capsys, "task", "verify", TASK)
    assert counter.read_text() == "3"
    warnings = design.command_warnings([{"id": TASK, "acceptance": {"AC-01": cmd}}], claimed.root)
    assert any("duplicate command" in w and "explicit shared" in w for w in warnings)


def test_plan_is_inspectable_without_execution_or_state_changes(claimed, store, capsys, tmp_path):
    counter = tmp_path / "count"
    configure(claimed, {"test": command(counter)})
    define(claimed, {"AC-01": {"check": "test"}, "AC-02": "manual"})
    before = (store.root / f"{TASK}.events.jsonl").read_bytes()
    code, out = cli(capsys, "task", "verify", TASK, "--plan")
    assert code == 0 and out["data"]["plan"][0]["acceptance"] == ["AC-01"]
    assert not counter.exists()
    assert (store.root / f"{TASK}.events.jsonl").read_bytes() == before


@pytest.mark.parametrize("value", [{"check": "missing"}, {"verification": "missing"},
                                   {"check": "test", "extra": "bad"}, {"verification": "../path"}])
def test_design_rejects_bad_references(value):
    text = VALID.replace('AC-01: "pytest tests/bookings/test_recurrence.py::test_weekly"',
                         'AC-01: ' + json.dumps(value))
    with pytest.raises(FactoryError) as exc:
        design.parse_design(text, "CHG-0012", KNOWN, {"test": "pytest"})
    assert exc.value.code == "design_invalid"


@pytest.mark.parametrize("named", [{"suite": 42}, {"../bad": "pytest"}, {"suite": "pytest | cat"}, []])
def test_design_rejects_invalid_named_commands(named):
    text = VALID.replace("  scope:", "  verification: " + json.dumps(named) + "\n  scope:", 1)
    with pytest.raises(FactoryError):
        design.parse_design(text, "CHG-0012", KNOWN)


def test_config_change_invalidates_previously_passed_evidence(ready_to_finish, capsys):
    configure(ready_to_finish["repo"], {"test": 'python -c "print(2)"'})
    code, out = finish(capsys)
    assert code == 3 and any("bindings changed" in p for p in out["error"]["details"])


def test_missing_shared_evidence_prevents_finish(ready_to_finish, capsys):
    repo, store = ready_to_finish["repo"], ready_to_finish["store"]
    define(repo, {"AC-01": {"check": "test"}, "AC-02": {"check": "test"}})
    assert cli(capsys, "task", "verify", TASK)[0] == 0
    evidence = store.read_task(TASK)["checks"]["evidence"]["test"]
    (store.root / evidence).unlink()
    code, out = finish(capsys)
    assert code == 3 and any("evidence of AC-01 is missing" in p for p in out["error"]["details"])


def test_design_accepts_named_verification_and_check_references():
    text = VALID.replace("  scope:", '  verification: {suite: "pytest tests/test_suite.py"}\n  scope:', 1)
    text = text.replace('AC-01: "pytest tests/bookings/test_recurrence.py::test_weekly"',
                        'AC-01: {verification: suite}').replace("AC-02: manual", "AC-02: {check: test}")
    task = design.parse_design(text, "CHG-0012", KNOWN, {"test": "pytest"})["tasks"][0]
    plan = verification.plan(task, {"test": "pytest"})
    assert {p["id"] for p in plan} == {"check:test", "verification:suite"}


def test_commit_changed_during_run_cannot_be_recorded_as_fresh(claimed, store, capsys, monkeypatch):
    configure(claimed, {"test": 'python -c "print(1)"'})
    original = tasks._run_evidence
    changed = False

    def execute(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            changed = True
            claimed.commit("src/concurrent.py", "x = 1\n", "concurrent change", cwd=args[1])
        return result

    monkeypatch.setattr(tasks, "_run_evidence", execute)
    cli(capsys, "task", "verify", TASK)
    checks = store.read_task(TASK)["checks"]
    assert checks["dirty"] and checks["changed_during_run"]


def test_shared_flow_register_approve_verify_and_finish(repo, fake_orca, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(repo.root)
    counter = tmp_path / "count"
    configure(repo, {"test": command(counter)})
    folder = repo.root / FOLDER
    folder.mkdir(parents=True)
    text = DESIGN.replace("review: [quality]", "review: []", 1)
    text = text.replace('AC-01: \'python -c "import sys; sys.exit(0)"\'', 'AC-01: {check: test}')
    text = text.replace("AC-02: manual", "AC-02: {check: test}")
    (folder / "design.md").write_text(text)
    assert main(["design", "register", CHANGE]) == 0
    assert main(["design", "approve", CHANGE, "--message", f"approve {CHANGE}"]) == 0
    assert main(["task", "claim", TASK]) == 0
    capsys.readouterr()
    assert cli(capsys, "task", "verify", TASK)[0] == 0
    assert counter.read_text() == "1"
    code, out = cli(capsys, "task", "finish", TASK, "--memory", "none")
    assert code == 0 and out["data"]["state"] == "to_merge"


def test_reference_description_for_board_and_pr():
    assert verification.describe({"check": "test"}) == "check:test"
