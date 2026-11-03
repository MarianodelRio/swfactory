"""One accounting and diagnosis policy for managed and imported review results."""

from __future__ import annotations

from typing import Any

from swfactory.proof import invalid, nonempty
from swfactory.state import State

CAUSES = ("omitted_requirement", "incomplete_fix", "regression", "insufficient_evidence",
          "unclear_contract", "review_workflow", "undetermined")


def limit(cfg: dict[str, Any]) -> int:
    return int(cfg["review"].get("max_failed_rounds", cfg["review"].get("max_rounds", 2)))


def findings(record: State, kind: str | None = None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for report_index, report in enumerate(record.get("reviews", []), start=1):
        if report.get("historical") or (kind and report["type"] != kind):
            continue
        for key, assessment in report.get("finding_status", {}).items():
            if key in result:
                result[key] = {**result[key], "status": assessment["status"], "reason": assessment["reason"]}
        for number, finding in enumerate(report["findings"], start=1):
            key = finding.get("id", f"history-{report['type']}-{report_index}.F{number}")
            result[key] = {**finding, "id": key, "type": report["type"], "status": "unresolved"}
    return result


def prepare(report: dict[str, Any], run_id: str, state: State, strict: bool,
            known_findings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Assign finding ids without guessing that similar prose describes the same defect."""
    known = findings(state, report["type"]) if known_findings is None else known_findings
    assessments = report.get("finding_status", {})
    if not isinstance(assessments, dict):
        raise invalid("finding_status must map existing finding ids to assessments")
    for key, row in assessments.items():
        if key not in known or not isinstance(row, dict) or row.get("status") not in (
                "resolved", "unresolved", "partial", "disputed") or not nonempty(row.get("reason")) \
                or set(row) != {"status", "reason"}:
            raise invalid(f"{key}: invalid finding assessment; include status and reason")
    outstanding = {key for key, f in known.items() if f["severity"] in ("blocker", "major")
                   and f["status"] != "resolved"}
    if strict and not outstanding <= assessments.keys():
        raise invalid("assess every previous unresolved major/blocker finding")
    if report["verdict"] == "pass" and any(
            assessments.get(key, {}).get("status") != "resolved" for key in outstanding):
        raise invalid("a pass cannot leave previous major/blocker findings unresolved")
    ids: set[str] = set()
    for number, finding in enumerate(report["findings"], start=1):
        key = finding.get("id", f"{run_id}.F{number}")
        if not nonempty(key) or key in ids or key in known or key in findings(state):
            raise invalid("new findings need unique ids; assess existing findings through finding_status")
        finding["id"] = key
        ids.add(key)
    report["finding_status"] = assessments
    diagnosis = report.get("diagnosis", {"cause": "undetermined", "explanation": "No diagnosis supplied",
                                         "evidence": []})
    if (not isinstance(diagnosis, dict) or diagnosis.get("cause") not in CAUSES
            or not nonempty(diagnosis.get("explanation")) or not isinstance(diagnosis.get("evidence"), list)
            or not all(nonempty(p) for p in diagnosis["evidence"])):
        raise invalid("diagnosis needs a recognized cause, explanation and evidence references")
    if diagnosis["cause"] != "undetermined" and not diagnosis["evidence"]:
        raise invalid("a causal diagnosis requires evidence; use undetermined when unknown")
    report["diagnosis"] = diagnosis
    return report


def failure_count(state: State, kind: str) -> int:
    period = state.get("review_period", 0)
    return sum(r["type"] == kind and r["verdict"] == "fail" and not r.get("historical")
               and r.get("period", 0) == period for r in state.get("reviews", []))


def append(state: State, saved: dict[str, Any], cfg: dict[str, Any]) -> State:
    from swfactory import tasks

    state["reviews"].append(saved)
    if (not saved.get("historical") and saved["verdict"] == "pass"
            and (state.get("review_diagnosis") or {}).get("type") == saved["type"]):
        state["review_diagnosis"] = None
    if not saved.get("historical") and saved["verdict"] == "fail":
        count = failure_count(state, saved["type"])
        state["review_diagnosis"] = {"type": saved["type"], "failed_rounds": count,
                                     "limit": limit(cfg), "period": state.get("review_period", 0),
                                     **saved["diagnosis"]}
        if count >= limit(cfg):
            state = tasks.transition(state, "block", reason=tasks.REVIEW_NOT_CONVERGING)
    return state
