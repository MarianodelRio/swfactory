"""Requirement sources, a small proof plan, and structured conformance coverage."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from swfactory import verification
from swfactory.output import EXIT_PRECONDITION, FactoryError


def invalid(message: str) -> FactoryError:
    return FactoryError("review_invalid", message, EXIT_PRECONDITION)


def requirements(text: str, task: dict[str, Any]) -> dict[str, str]:
    """ACs assigned to this task plus explicit change-wide constraints C-NN."""
    from swfactory import design

    result: dict[str, str] = {}
    for section, prefix in (("Acceptance criteria", "AC"), ("Constraints", "C")):
        body = design.section(design.normalize(text), section) or ""
        for match in re.finditer(rf"^\s*-\s*({prefix}-\d{{2,}}):\s*(.+)$", body, re.MULTILINE):
            key, value = match.groups()
            if key in result:
                raise invalid(f"duplicate requirement {key}")
            if prefix == "C" or key in task["acceptance"]:
                result[key] = value.strip()
    missing = set(task["acceptance"]) - result.keys()
    if missing:
        raise invalid(f"requirements missing from the design: {', '.join(sorted(missing))}")
    return result


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise invalid(f"cannot read JSON from {path}: {exc}") from exc


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_plan(data: Any, required: dict[str, str], executions: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != set(required):
        raise invalid("proof plan must cover exactly the task ACs and change-wide constraints")
    for key, row in data.items():
        if not isinstance(row, dict) or any(not nonempty(row.get(k)) for k in
                                            ("code", "scenario", "assertion", "verification", "discrimination")):
            raise invalid(f"{key}: proof needs code, scenario, assertion, verification and discrimination")
        if row["verification"] not in executions | {"manual", "inspection"}:
            raise invalid(f"{key}: unknown verification execution")
    return data


def execution_ids(task: dict[str, Any], checks: dict[str, str]) -> set[str]:
    return {entry["id"] for entry in verification.plan(task, checks)}


def validate_coverage(report: dict[str, Any], run: dict[str, Any]) -> None:
    coverage = report.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != set(run["requirements"]):
        raise invalid("conformance coverage must include exactly every reserved requirement")
    if not isinstance(report.get("deviations"), list) or not all(
            nonempty(d) for d in report["deviations"]):
        raise invalid("conformance needs deviations (an empty list when none)")
    for key, row in coverage.items():
        if not isinstance(row, dict) or row.get("status") not in ("satisfied", "violated", "unverified"):
            raise invalid(f"{key}: coverage status must be satisfied, violated or unverified")
        if any(not nonempty(row.get(k)) for k in ("implementation", "assertion", "reason")):
            raise invalid(f"{key}: coverage needs implementation, assertion and reason")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not all(nonempty(p) for p in evidence):
            raise invalid(f"{key}: evidence must be a list of reserved evidence references")
        if any(p not in run["evidence"] for p in evidence):
            raise invalid(f"{key}: evidence was not reserved for this review")
        if row["status"] == "satisfied" and not evidence:
            raise invalid(f"{key}: satisfied coverage requires evidence")
        bound = run.get("acceptance_evidence", {}).get(key)
        if row["status"] == "satisfied" and key.startswith("AC-") and bound not in evidence:
            raise invalid(f"{key}: include the acceptance evidence bound to this criterion")
        if (row["status"] == "satisfied" and key.startswith("AC-")
                and run.get("acceptance_status", {}).get(key) != "verified"):
            raise invalid(f"{key}: acceptance verification did not pass")
    if report["verdict"] == "pass" and (report["deviations"] or any(
            row["status"] != "satisfied" for row in coverage.values())):
        raise invalid("a conformance pass cannot contain deviations or unverified/violated requirements")
