"""Real proof-plan registration and conformance reports for integration fixtures."""

import json

from swfactory import proof, tasks, verification


def plan(ctx, task_id, path):
    task = tasks.catalog(ctx)[task_id]
    text = (ctx.main_checkout / task["design"]).read_text()
    entries = verification.plan(task, tasks._config(ctx)["checks"])
    bindings = {ac: entry["id"] for entry in entries for ac in entry["acceptance"]}
    data = {key: {"code": "src/bookings/model.py", "scenario": "exercise the specified behavior",
                  "assertion": "the observable result matches the requirement",
                  "verification": bindings.get(key, "manual"),
                  "discrimination": "the incorrect result fails this assertion"}
            for key in proof.requirements(text, task)}
    path.write_text(json.dumps(data))
    tasks.proof_plan(task_id, path, [], ctx)
    return data


def coverage(run):
    return {key: {"status": "satisfied", "implementation": "src/bookings/model.py:1",
                  "assertion": "observable output equals the required result", "reason": "inspected assertion",
                  "evidence": [run["acceptance_evidence"].get(key) or f"git:{run['commit']}"]}
            for key in run["requirements"]}
