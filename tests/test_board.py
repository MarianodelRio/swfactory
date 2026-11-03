import json
import os
import socket
import threading
import time
import urllib.request

import pytest
from conftest import set_home
from test_claim_launch import TASK, cli, orca_calls
from test_claim_launch import project as project  # noqa: F401

from swfactory import board, tasks
from swfactory.state import TaskStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    set_home(monkeypatch, path)  # the background server reads the agents' folders from here
    return path


@pytest.fixture
def working(project, capsys):  # noqa: F811
    config = project.root / ".factory" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[board]\nidle_minutes = 1\n")  # a board a failing test leaves behind goes away soon
    cli(capsys, "task", "claim", TASK)
    cli(capsys, "task", "launch", TASK, "--agent", "claude", "--reviewer", "claude")
    cli(capsys, "task", "checkpoint", TASK, "--done", "tests for AC-01", "--next", "implement")
    return project


def collect(repo, home):
    ctx = tasks.Context(repo.root, TaskStore(repo.root / ".factory" / "state"))
    return board.collect(ctx, home)


def task_in(data, task=TASK):
    return next(t for t in data["tasks"] if t["id"] == task)


def state_files(repo):
    folder = repo.root / ".factory" / "state"
    return {p.name: p.read_bytes() for p in folder.glob("*") if p.is_file()}


def test_board_shows_columns_and_agents(working, home):
    data = collect(working, home)
    task = task_in(data)
    assert (task["state"], task["column"], task["liveness"]) == ("in_progress", "in_progress", "alive")
    assert task_in(data, "CHG-0012-02")["column"] == "backlog"
    assert task["last_checkpoint"]["next"] == "implement"
    assert [c["id"] for c in task["criteria"]] == ["AC-01", "AC-02"]
    [lead] = task["runs"]
    assert (lead["kind"], lead["role"], lead["state"]) == ("terminal", "lead", "active")
    assert data["counts"]["in_progress"] == 1 and data["counts"]["backlog"] == 1
    assert data["changes"][0]["state"] == "approved"
    assert "activity" not in data
    assert data["sources"]["orca"] == "ok"


def test_board_only_reads(working, home, fake_orca):
    fake_orca.respond("terminal list", {"id": "x", "ok": True, "result": {"terminals": []}})
    before, cards = state_files(working), len(orca_calls(fake_orca, "worktree set"))
    task = task_in(collect(working, home))
    assert (task["state"], task["liveness"], task["column"]) == ("in_progress", "finished", "needs_you")
    assert state_files(working) == before  # not marked interrupted: /factory-status does that
    assert len(orca_calls(fake_orca, "worktree set")) == cards


def test_orca_cutting_the_terminal_list_short_is_not_proof_of_closed(working, home, fake_orca):
    fake_orca.respond("terminal list", {"id": "x", "ok": True, "result": {"terminals": [], "truncated": True}})
    task = task_in(collect(working, home))
    assert (task["liveness"], task["column"]) == ("unknown", "in_progress")


def test_a_task_whose_design_changed_needs_you(working, home):
    design = working.root / ".factory/changes/CHG-0012/design.md"
    design.write_text(design.read_text() + "\nOne more line.\n")
    task = task_in(collect(working, home))
    assert (task["state"], task["design_changed"], task["column"]) == ("in_progress", True, "needs_you")


def test_cards_are_ordered(working, home):
    store = TaskStore(working.root / ".factory" / "state")
    data = collect(working, home)
    assert [t["id"] for t in data["tasks"] if t["column"] == "backlog"] == ["CHG-0012-02"]
    # two merged tasks: the one merged last comes first
    for task, when in (("CHG-0012-01", "2026-10-01T10:00:00Z"), ("CHG-0012-02", "2026-10-02T10:00:00Z")):
        store.update_task(task, lambda s, when=when: {**s, "state": "done", "merged_at": when}, "x")
    data = collect(working, home)
    assert [t["id"] for t in data["tasks"] if t["column"] == "done"] == ["CHG-0012-02", "CHG-0012-01"]


def test_backlog_shows_what_can_start_first():
    rows = [{"id": i, "column": "backlog", "state": s} for i, s in
            (("CHG-12.10", "ready"), ("CHG-12.2", "pending"), ("CHG-12.9", "ready"))]
    assert [r["id"] for r in sorted(rows, key=board._order)] == ["CHG-12.9", "CHG-12.10", "CHG-12.2"]


def test_awaiting_approval_needs_you(working, home):
    folder = working.root / ".factory/changes/CHG-0013"
    folder.mkdir()
    (folder / "design.md").write_text("# CHG-0013 — Room capacity\n")
    data = collect(working, home)
    assert {"id": "CHG-0013", "title": "Room capacity", "state": "awaiting_approval", "tasks": 0} in data["changes"]
    assert data["counts"]["needs_you"] == 1


def test_board_without_orca_still_shows_the_tasks(working, home, fake_orca):
    fake_orca.fail("worktree ps", code="runtime_unavailable")
    data = collect(working, home)
    assert data["sources"]["orca"] == "unavailable" and data["orca_error"]
    assert (task_in(data)["liveness"], task_in(data)["broken_link"]) == ("unknown", None)


def test_page_is_self_contained_and_snapshot_embeds_the_data(working, home, tmp_path, capsys):
    page = board.page()
    assert "const EMBEDDED = null;" in page
    assert "http://" not in page and "https://" not in page
    assert all(link.split('href="')[1].startswith("data:") for link in page.split("<link")[1:])
    assert "Activity" not in page
    out = tmp_path / "board.html"
    code, result = cli(capsys, "board", "snapshot", str(out))
    assert code == 0, result
    html = out.read_text()
    assert TASK in html and "const EMBEDDED = null;" not in html
    embedded = board.page({"x": "</script><!--<script>&"}).split("const EMBEDDED = ")[1].split("\n")[0]
    assert not any(c in embedded for c in "<>&")
    assert json.loads(embedded.removesuffix(";")) == {"x": "</script><!--<script>&"}


def board_log(repo):
    log = repo.root / ".factory" / "state" / "board.log"
    return log.read_text() if log.exists() else "(no board.log)"


def get(url):
    with urllib.request.urlopen(url, timeout=5) as reply:
        return reply.status, reply.read()


def test_server_serves_the_page_and_the_data_then_stops_when_idle(working, home, monkeypatch):
    monkeypatch.setattr(board, "IDLE_CHECK_SECONDS", 0.05)
    state = working.root / ".factory" / "state"
    errors = []

    def serve():
        try:
            board.serve(working.root, state, 0, 0.1, home)
        except Exception as exc:  # shown in the assertion below
            errors.append(repr(exc))

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    deadline = time.monotonic() + 10
    while not (state / "board.json").exists() and time.monotonic() < deadline and not errors:
        time.sleep(0.02)
    assert (state / "board.json").exists(), errors
    url = json.loads((state / "board.json").read_text())["url"]
    assert get(url)[0] == 200 and b"<title>Factory board</title>" in get(url)[1]
    status, body = get(url + "api/board")
    assert status == 200 and task_in(json.loads(body))["id"] == TASK
    health = json.loads(get(url + "health")[1])
    assert health == {"factory_board": True, "pid": os.getpid(), "repo": str(working.root)}
    with pytest.raises(urllib.error.HTTPError):
        get(url + "nope")
    wrong_token = urllib.request.Request(url + "stop", data=b"", method="POST", headers={"X-Swfactory-Token": "x"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(wrong_token, timeout=5)
    assert refused.value.code == 403
    assert get(url + "health")[0] == 200  # still serving
    other_site = urllib.request.Request(url + "api/board", headers={"Host": "evil.example:80"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(other_site, timeout=5)
    assert refused.value.code == 403
    server.join(timeout=20)  # about 6 s without requests
    assert not server.is_alive()
    assert not (state / "board.json").exists()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_start_status_open_and_stop_a_background_board(working, home, fake_orca, capsys):
    port = free_port()
    code, out = cli(capsys, "board", "start", "--port", str(port))
    assert code == 0, (out, board_log(working))
    started = out["data"]
    try:
        assert (started["started"], started["port"], started["url"]) == (True, port, f"http://127.0.0.1:{port}/")
        assert get(started["url"] + "api/board")[0] == 200
        _, again = cli(capsys, "board", "start")
        assert (again["data"]["started"], again["data"]["pid"]) == (False, started["pid"])
        _, opened = cli(capsys, "board", "open")
        assert opened["data"]["opened_in"] == "orca"
        tab = orca_calls(fake_orca, "tab create")[-1]
        assert tab[tab.index("--url") + 1] == started["url"]
        assert tab[tab.index("--worktree") + 1] == f"path:{working.root}"
        assert cli(capsys, "board", "status")[1]["data"]["running"] is True
    finally:
        code, stopped = cli(capsys, "board", "stop")
    assert code == 0, (stopped, board_log(working))
    assert stopped["data"]["stopped"] is True
    assert cli(capsys, "board", "status")[1]["data"] == {"running": False}
    with pytest.raises(OSError):
        get(started["url"] + "health")


def test_authenticated_stop_survives_client_disconnect(working, home, monkeypatch):
    original_server = board._Server
    disconnect = [True]

    def server_with_disconnecting_client(address, handler):
        send = handler._send

        def interrupted_send(self, status, kind, body):
            if self.path == "/stop" and disconnect[0]:
                disconnect[0] = False
                raise BrokenPipeError("client disconnected before receiving the response")
            return send(self, status, kind, body)

        handler._send = interrupted_send
        server = original_server(address, handler)
        server.handle_error = lambda *_: None  # the simulated disconnect is intentional
        return server

    monkeypatch.setattr(board, "_Server", server_with_disconnecting_client)
    state = working.root / ".factory/state"
    thread = threading.Thread(target=board.serve, args=(working.root, state, 0, 60, home), daemon=True)
    thread.start()
    info = {}
    try:
        deadline = time.monotonic() + 10
        while not info and time.monotonic() < deadline:
            info = board._read_info(state)
            time.sleep(0.02)
        assert info, "board did not start"
        request = urllib.request.Request(info["url"] + "stop", data=b"", method="POST",
                                         headers={board.TOKEN_HEADER: info["token"]})
        with pytest.raises(OSError):
            urllib.request.urlopen(request, timeout=5)
        thread.join(timeout=5)
        assert not thread.is_alive(), "an authenticated request must stop even if sending its reply fails"
    finally:
        if thread.is_alive() and info:
            disconnect[0] = False
            with urllib.request.urlopen(request, timeout=5) as reply:
                reply.read()
            thread.join(timeout=5)


def test_two_starts_at_once_give_one_board(working, home, capsys):
    port, results = free_port(), []

    def start():
        results.append(board.start(working.root, working.root / ".factory" / "state", port))

    threads = [threading.Thread(target=start) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len({r["pid"] for r in results}) == 1
        assert sorted(r["started"] for r in results) == [False, True]
    finally:
        cli(capsys, "board", "stop")


def test_open_falls_back_to_the_browser(working, home, fake_orca, monkeypatch, capsys):
    fake_orca.fail("tab create")
    opened_urls = []
    monkeypatch.setattr(board.webbrowser, "open", lambda url: opened_urls.append(url) or True)
    _, out = cli(capsys, "board", "open", "--port", str(free_port()))
    try:
        assert out["data"]["opened_in"] == "browser" and opened_urls == [out["data"]["url"]]
        assert any("Orca tab" in w for w in out["warnings"])
    finally:
        cli(capsys, "board", "stop")


def test_a_stale_info_file_is_not_a_running_board(working, home, capsys):
    state = working.root / ".factory" / "state"
    (state / "board.json").write_text(json.dumps({"pid": 999999, "port": free_port(), "url": "x"}))
    assert cli(capsys, "board", "status")[1]["data"] == {"running": False}
    assert not (state / "board.json").exists()
    assert cli(capsys, "board", "stop")[1]["data"]["stopped"] is False


def test_stop_never_signals_a_process_that_is_not_this_board(working, home, monkeypatch, capsys):
    state = working.root / ".factory" / "state"
    (state / "board.json").write_text(json.dumps({"pid": os.getpid(), "port": free_port(), "url": "x"}))
    real_kill = os.kill

    def kill(pid, sig):
        if sig != 0:  # 0 only asks whether the process exists
            pytest.fail("signalled a process")
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", kill)
    assert cli(capsys, "board", "stop")[1]["data"]["stopped"] is False


def test_finished_tasks_wait_in_to_merge_and_a_closed_pr_needs_you(working, home):
    store = TaskStore(working.root / ".factory" / "state")
    store.update_task(TASK, lambda s: {**s, "state": "to_merge", "pull_request": {"number": 7, "url": "u"}}, "x")
    assert task_in(collect(working, home))["column"] == "to_merge"
    store.update_task(TASK, lambda s: {**s, "pull_request": {**s["pull_request"], "closed": True}}, "x")
    assert task_in(collect(working, home))["column"] == "needs_you"
