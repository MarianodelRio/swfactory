import pytest

from swfactory.design import command_problem, command_warnings, digest, next_change_id, parse_design, scope_overlaps
from swfactory.output import EXIT_PRECONDITION, FactoryError

VALID = """# CHG-0012 — Recurring bookings

## Context
Bookings are single events today (`src/bookings/model.py`).

## Goal and scope
Weekly recurrence.

## Acceptance criteria
- AC-01: Given a weekly booking, when it is saved, then 4 occurrences exist — verified by: test
- AC-02: Given the calendar, when synced, then occurrences appear — verified by: manual step
- AC-03: Given a sync, when it fails, then it retries — verified by: test

## Approach
Store a rule.

## Implementation notes
`src/bookings/`.

## Tasks
```yaml
- id: CHG-0012-01
  title: Add recurrence model and validation
  depends_on: []
  complexity: normal
  review: [quality]
  scope: ["src/bookings/**", "tests/bookings/**"]
  acceptance:
    AC-01: "pytest tests/bookings/test_recurrence.py::test_weekly"
    AC-02: manual
- id: CHG-0012-02
  title: Sync recurring bookings with calendar
  depends_on: [CHG-0012-01, CHG-0007-03]
  complexity: complex
  review: [quality, conformance]
  acceptance:
    AC-03: "pytest tests/integration/test_calendar.py"
```

## Risks and open questions
None.
"""

KNOWN = {"CHG-0007-03"}


def errors_of(text, change="CHG-0012", known=KNOWN):
    with pytest.raises(FactoryError) as exc:
        parse_design(text, change, known)
    assert exc.value.code == "design_invalid"
    assert exc.value.exit_code == EXIT_PRECONDITION
    return exc.value.details


def test_valid_design_gives_its_tasks_and_criteria():
    parsed = parse_design(VALID, "CHG-0012", KNOWN)
    assert parsed["acceptance_criteria"] == ["AC-01", "AC-02", "AC-03"]
    assert [t["id"] for t in parsed["tasks"]] == ["CHG-0012-01", "CHG-0012-02"]
    assert parsed["tasks"][0]["acceptance"]["AC-02"] == "manual"
    assert "scope" not in parsed["tasks"][1]


def test_digest_ignores_line_endings():
    assert digest(VALID) == digest(VALID.replace("\n", "\r\n"))
    assert digest(VALID).startswith("sha256:")
    assert digest(VALID) != digest(VALID + "x")


def test_next_change_id(tmp_path):
    assert next_change_id(tmp_path / "missing") == "CHG-1"
    for name in ("CHG-0003", "CHG-0012", "notes", "CHG-12x"):
        (tmp_path / name).mkdir()
    (tmp_path / "CHG-0020").write_text("a file, not a change")
    assert next_change_id(tmp_path) == "CHG-13"  # numbering goes on after the old four-digit ids
    (tmp_path / "CHG-13-weekly-recurrence").mkdir()
    assert next_change_id(tmp_path) == "CHG-14"
    (tmp_path / ".CHG-14.reserved").mkdir()  # reserved by another session, folder not created yet
    assert next_change_id(tmp_path) == "CHG-15"


def test_new_task_ids_and_what_the_person_types():
    from swfactory.design import full_id, slugify
    design = VALID.replace("CHG-0012-01", "CHG-12.1").replace("CHG-0012-02", "CHG-12.2").replace("CHG-0012", "CHG-12")
    assert [t["id"] for t in parse_design(design, "CHG-12", KNOWN)["tasks"]] == ["CHG-12.1", "CHG-12.2"]
    assert any("id must be CHG-12.N" in e for e in errors_of(design.replace("CHG-12.1", "CHG-12.01"), "CHG-12"))
    assert [full_id(t) for t in ("12", "12.1", "chg-12.1", "CHG-0012-01", "x")] == \
        ["CHG-12", "CHG-12.1", "CHG-12.1", "CHG-0012-01", "x"]
    assert slugify("Add the recurrence model and validation") == "add-recurrence-model-validation"


def replace(old, new, text=VALID):
    assert old in text
    return text.replace(old, new)


def test_missing_tasks_section():
    assert any("## Tasks" in e for e in errors_of(VALID.split("## Tasks")[0]))


def test_tasks_section_without_yaml_block():
    text = VALID.split("```yaml")[0] + "\n## Risks and open questions\n"
    assert any("yaml" in e for e in errors_of(text))


def test_malformed_yaml():
    assert any("YAML" in e for e in errors_of(replace("  title: Add recurrence", "  title: [unclosed")))


def test_block_is_not_a_list():
    text = VALID.split("```yaml")[0] + "```yaml\nid: CHG-0012-01\n```\n"
    assert any("list" in e for e in errors_of(text))


@pytest.mark.parametrize("field", ["title", "depends_on", "complexity", "review", "acceptance"])
def test_missing_required_field(field):
    lines = [line for line in VALID.splitlines(keepends=True)]
    start = next(i for i, line in enumerate(lines) if line.startswith("- id: CHG-0012-02"))
    for i in range(start, len(lines)):
        if lines[i].strip().startswith(f"{field}:"):
            del lines[i]
            if field == "acceptance":
                del lines[i]
            break
    details = errors_of("".join(lines))
    assert any(field in e and "CHG-0012-02" in e for e in details)


def test_unknown_field():
    text = replace("  complexity: complex", "  complexity: complex\n  owner: x")
    assert any("owner" in e for e in errors_of(text))


def test_invalid_values():
    text = replace("complexity: normal", "complexity: huge")
    text = replace("review: [quality, conformance]", "review: [quality, style]", text)
    text = replace('scope: ["src/bookings/**", "tests/bookings/**"]', "scope: src/bookings", text)
    details = errors_of(text)
    assert any("complexity" in e and "huge" in e for e in details)
    assert any("review" in e and "style" in e for e in details)
    assert any("scope" in e for e in details)


def test_duplicated_review_type():
    assert any("review" in e for e in errors_of(replace("review: [quality]", "review: [quality, quality]")))


def test_bad_task_ids():
    text = replace("- id: CHG-0012-02", "- id: CHG-0099-02")
    assert any("CHG-0099-02" in e for e in errors_of(text))
    text = replace("- id: CHG-0012-02", "- id: CHG-0012-01")
    assert any("duplicated" in e for e in errors_of(text))
    text = replace("- id: CHG-0012-02", "- id: CHG-0012-2")
    assert any("CHG-0012-2" in e for e in errors_of(text))


def test_unknown_dependency():
    details = errors_of(VALID, known=set())
    assert any("CHG-0007-03" in e for e in details)


def test_dependency_cycle():
    text = replace("depends_on: []", "depends_on: [CHG-0012-02]")
    assert any("cycle" in e for e in errors_of(text))


def test_self_dependency_is_a_cycle():
    text = replace("depends_on: []", "depends_on: [CHG-0012-01]")
    assert any("cycle" in e for e in errors_of(text))


def test_criterion_without_task():
    text = replace("    AC-03: \"pytest tests/integration/test_calendar.py\"\n", "    AC-02: manual\n")
    assert any("AC-03" in e and "no task" in e for e in errors_of(text))


def test_task_criterion_not_in_the_list():
    text = replace("    AC-02: manual\n", "    AC-02: manual\n    AC-09: manual\n")
    assert any("AC-09" in e for e in errors_of(text))


def test_bad_acceptance_values():
    text = replace("    AC-02: manual\n", "    AC-02: \"\"\n")
    text = replace("    AC-01: \"pytest", "    AC1: \"pytest", text)
    details = errors_of(text)
    assert any("AC-02" in e for e in details)
    assert any("AC1" in e for e in details)


@pytest.mark.parametrize("command", [
    "pytest tests/x.py | tee out.txt", "make build && make test", "test -f a; test -f b", "pytest > out.txt",
    "ls tests/*.py", "echo $HOME", "test -n `date`", "grep 'unclosed",
])
def test_acceptance_commands_that_need_a_shell_are_refused(command):
    text = replace('"pytest tests/bookings/test_recurrence.py::test_weekly"', repr(command))
    details = errors_of(text)
    assert any(e.startswith("CHG-0012-01: AC-01 ") for e in details), details


@pytest.mark.parametrize("command", [
    "pytest tests/x.py::test_a -k 'a or b'", 'python -c "import sys; sys.exit(0 if 2 > 1 else 1)"',
    "bash -c 'pytest -q | grep passed && test -f out.txt'", "grep -q 'end$' notes.txt", 'npm test -- --grep "a b"',
    "grep -E foo\\|bar notes.txt",
])
def test_acceptance_commands_without_shell_syntax_are_accepted(command):
    assert command_problem(command) is None


def test_acceptance_programs_not_found_are_warned(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check.sh").write_text("#!/bin/sh\n")
    tasks = [{"id": "CHG-1.1", "acceptance": {
        "AC-01": "python -c 'print(1)'", "AC-02": "manual", "AC-03": "scripts/check.sh --fast",
        "AC-04": "The calendar shows four occurrences", "AC-05": "no-such-tool-xyz run"}}]
    warnings = command_warnings(tasks, tmp_path)
    assert len(warnings) == 2
    assert warnings[0].startswith("CHG-1.1: AC-04 runs 'The', which is not found here")
    assert "AC-05 runs 'no-such-tool-xyz'" in warnings[1]


def test_no_acceptance_criteria_section():
    text = VALID.replace("## Acceptance criteria", "## Criteria")
    assert any("Acceptance criteria" in e for e in errors_of(text))


def test_crlf_design_parses():
    assert len(parse_design(VALID.replace("\n", "\r\n"), "CHG-0012", KNOWN)["tasks"]) == 2


def test_a_task_may_choose_how_it_integrates():
    merge = VALID.replace("  complexity: normal\n", "  complexity: normal\n  integrate: merge\n", 1)
    assert parse_design(merge, "CHG-0012", KNOWN)["tasks"][0]["integrate"] == "merge"
    errors = errors_of(merge.replace("integrate: merge", "integrate: squash"))
    assert any("integrate 'squash' must be one of pr, merge" in e for e in errors)


def tasks_with(scopes, depends=None):
    depends = depends or {}
    return [{"id": f"CHG-0012-0{n}", "scope": scope, "depends_on": depends.get(n, [])}
            for n, scope in enumerate(scopes, start=1)]


def test_overlapping_scopes_without_a_dependency_are_flagged():
    warnings = scope_overlaps(tasks_with([["src/bookings/**"], ["src/bookings/sync.py", "docs/**"]]))
    assert len(warnings) == 1
    assert "CHG-0012-01 and CHG-0012-02 may touch the same files" in warnings[0]


@pytest.mark.parametrize(("scopes", "depends"), [
    ([["src/bookings/**"], ["src/bookings/**"]], {2: ["CHG-0012-01"]}),  # ordered: fine
    ([["src/a/**"], ["src/b/**"]], {}),  # disjoint
    ([["src/**"], ["src/x.py"], ["src/y.py"]], {2: ["CHG-0012-01"], 3: ["CHG-0012-02"]}),  # transitive
])
def test_no_warning_when_ordered_or_disjoint(scopes, depends):
    assert scope_overlaps(tasks_with(scopes, {k: [f"CHG-0012-0{d[-1]}" for d in v] for k, v in depends.items()})) \
        == []
