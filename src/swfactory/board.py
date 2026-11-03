"""`swfactory board`: a local kanban of the repository's changes and tasks, with what runs in each
task. It only looks, except that it records tasks whose work was merged (a fact, never a decision).
A small HTTP server (standard library only, 127.0.0.1) serves one page that polls `/api/board`; it
runs in the background and stops by itself when nobody looks at it."""

from __future__ import annotations

import collections
import datetime
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from swfactory import activity, config, design, orca, sync, system, tasks, verification
from swfactory.output import EXIT_CONFLICT, EXIT_ERROR, EXIT_UNAVAILABLE, FactoryError, mask_secrets
from swfactory.state import TaskStore, now

CACHE_SECONDS = 2.0
START_TIMEOUT = 10.0
IDLE_CHECK_SECONDS = 15.0
DONE_STEP_SECONDS = 60.0  # a finished task's next step (pushed? PR?) costs git calls: reuse it this long
MERGES_SECONDS = 60.0  # how often the server fetches origin and records merged tasks
INFO_FILE = "board.json"
LOCK_FILE = "board.lock"
TOKEN_HEADER = "X-Swfactory-Token"
# Set by `start` so it recognizes the server it launched (on Windows a venv's python.exe is a
# launcher, so the server's pid is not the pid `start` sees).
LAUNCH_VARIABLE = "SWFACTORY_BOARD_LAUNCH"
LOG_FILE = "board.log"
COLUMNS = ("needs_you", "backlog", "in_progress", "in_review", "to_merge", "done")


# --- What the page shows --------------------------------------------------------------------------


def collect(ctx: tasks.Context, home: Path) -> dict[str, Any]:
    """The whole board as JSON. Reads only: it never changes a task's state, not even to mark it
    interrupted (`/factory-status` does that). The server records merges separately."""
    cfg = config.load_config(ctx.main_checkout / ".factory" / "config.toml")
    view = activity.orca_view()
    hooks = activity.orca_hooks(home)
    rows = []
    for task in tasks.catalog(ctx, sync=False).values():
        record = ctx.store.read_task(task["id"])
        working = record is not None and record["state"] in tasks.WORKING
        active = record is not None and record["state"] in tasks.ACTIVE
        live = activity.liveness(record, view) if working and record else None
        missing = activity.workspace_missing(record, view) if active and record else False
        finished = record is not None and record["state"] == "to_merge"
        done_step = _cached_done_step(ctx, task["id"]) if finished else None
        row = tasks.task_row(task, record, live, missing, cfg, ctx, next_step=done_step)
        if finished and done_step is None:
            _remember_done_step(ctx, task["id"], row["next_step"])
        row.update(_details(task, record))
        row["design_changed"] = active and not task["approved"]
        row["runs"] = (activity.runs(tasks.workspace_path(record), record, view, hooks, home)
                       if record and record.get("workspace") else [])
        row["column"] = _column(row)
        row["updated_at"] = _iso(_state_mtime(ctx, task["id"])) if record else None
        row["merged_at"] = record.get("merged_at") if record else None
        rows.append(row)
    rows.sort(key=_order)

    changes = _changes(ctx, rows)
    counts = collections.Counter(row["column"] for row in rows)
    counts["needs_you"] += sum(1 for c in changes if c["state"] == "awaiting_approval")
    return {
        "repo": ctx.main_checkout.name,
        "path": str(ctx.main_checkout),
        "base": ctx.base(),
        "generated_at": now(),
        "sources": {"orca": "ok" if view["ok"] else "unavailable",
                    "hooks": "ok" if hooks["ok"] else "unavailable",
                    "claude": "ok" if (home / ".claude" / "projects").is_dir() else "unavailable",
                    "codex": "unverified" if (home / ".codex" / "sessions").is_dir() else "unavailable"},
        "orca_error": view["error"],
        "counts": {column: counts.get(column, 0) for column in COLUMNS},
        "changes": changes,
        "tasks": rows,
    }


_done_steps: dict[str, tuple[float, float, str]] = {}


def _state_mtime(ctx: tasks.Context, task_id: str) -> float:
    try:
        return (ctx.store.root / f"{task_id}.json").stat().st_mtime
    except OSError:
        return 0.0


def _cached_done_step(ctx: tasks.Context, task_id: str) -> str | None:
    cached = _done_steps.get(task_id)
    fresh = cached and cached[0] == _state_mtime(ctx, task_id) and time.monotonic() - cached[1] < DONE_STEP_SECONDS
    return cached[2] if cached and fresh else None


def _remember_done_step(ctx: tasks.Context, task_id: str, step: str) -> None:
    _done_steps[task_id] = (_state_mtime(ctx, task_id), time.monotonic(), step)


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _number(task_id: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", task_id))


def _order(row: dict[str, Any]) -> tuple[Any, ...]:
    """Cards in each column: what can start first in the backlog (ready, then pending, by id), the
    most recent first everywhere else (merged most recently, for done)."""
    if row["column"] == "backlog":
        return (row["column"], row["state"] != "ready", 0.0, _number(row["id"]))
    recent = (row.get("merged_at") if row["column"] == "done" else None) or row.get("updated_at")
    return (row["column"], False, -_epoch(recent), _number(row["id"]))


def _epoch(timestamp: str | None) -> float:
    """Seconds of an ISO timestamp; 0 (oldest) when there is none."""
    try:
        return datetime.datetime.strptime(str(timestamp), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.UTC).timestamp()
    except ValueError:
        return 0.0


def _column(row: dict[str, Any]) -> str:
    state = row["state"]
    if (state in ("blocked", "interrupted") or row["broken_link"] or row["liveness"] == "finished"
            or row["design_changed"] or (row.get("pull_request") or {}).get("closed")):
        return "needs_you"
    if state in ("pending", "ready"):
        return "backlog"
    return state if state in COLUMNS else "backlog"


def _details(task: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any]:
    record = record or {}
    stored = record.get("acceptance") or {}
    attempt = record["attempts"][-1] if record.get("attempts") else {}
    notes = record.get("notes") or []
    return {
        "criteria": [{"id": ac, "how": verification.describe(how),
                      "status": (stored.get(ac) or {}).get("status", "pending"),
                      "evidence": (stored.get(ac) or {}).get("evidence")}
                     for ac, how in task["acceptance"].items()],
        "checks": (record.get("checks") or {}).get("status"),
        "review_details": [{"type": r["type"], "round": r["round"], "verdict": r["verdict"],
                            "reviewer": r.get("reviewer"), "commit": r["commit"][:8],
                            "findings": [{"severity": f.get("severity"), "file": f.get("file"),
                                          "problem": mask_secrets(str(f.get("problem", "")))}
                                         for f in r.get("findings", [])]}
                           for r in record.get("reviews", [])],
        "required_reviews": task.get("review", []),
        "complexity": task.get("complexity"),
        "model": attempt.get("model"),
        "reviewer_model": attempt.get("reviewer_model"),
        "attempts": len(record.get("attempts") or []),
        "started_at": attempt.get("started_at"),
        "notes": [{"at": n["at"], "text": n["text"], "pause": n.get("pause", False), "read": n["read"]}
                  for n in notes[-5:]],
        "unread_notes": sum(1 for n in notes if not n["read"]),
    }


def _changes(ctx: tasks.Context, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    awaiting = set(tasks.awaiting_approval(ctx))
    found = []
    for change_id, folder in design.change_folders(ctx.changes_dir):
        path = folder / "design.md"
        if not path.exists():
            continue
        match = design.TITLE.search(design.normalize(path.read_text(encoding="utf-8")))
        own = [r for r in rows if r["change"] == change_id]
        if (folder / "rejection.json").exists():
            state = "rejected"
        elif change_id in awaiting:
            state = "awaiting_approval"
        elif own and all(r["state"] == "done" for r in own):
            state = "done"
        else:
            state = "approved"
        found.append({"id": change_id, "title": match.group(1).strip() if match else change_id,
                      "state": state, "tasks": len(own)})
    return found


def page(data: dict[str, Any] | None = None) -> str:
    """The board page; with `data` the board is embedded (a snapshot) and does not poll."""
    html = resources.files("swfactory").joinpath("templates/board/board.html").read_text(encoding="utf-8")
    embedded = "null"
    if data is not None:  # safe inside <script>: no "<", ">" or "&" can end it or start a comment
        embedded = json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return html.replace("__BOARD_DATA__", embedded)


def snapshot(ctx: tasks.Context, home: Path, out: Path) -> dict[str, Any]:
    out.write_text(page(collect(ctx, home)), encoding="utf-8")
    return {"snapshot": str(out)}


# --- The server -----------------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer looks up the host's name (socket.getfqdn), which can hang for a long time on
        # macOS; the board only listens on 127.0.0.1 and never needs that name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def serve(main_checkout: Path, state_dir: Path, port: int, idle_minutes: float, home: Path) -> None:
    """Serve the board in the foreground until stopped (`POST /stop` with the token in
    `board.json`, Ctrl-C or SIGTERM) or idle for `idle_minutes`."""
    other = running(main_checkout, state_dir)
    if other:
        raise FactoryError("board_running", f"a board is already running at {other['url']}", EXIT_CONFLICT)
    lock = threading.Lock()
    cache: dict[str, Any] = {"at": 0.0, "body": b"", "merges_at": 0.0}
    last_seen = [time.monotonic()]
    token = secrets.token_urlsafe(24)
    stopping = threading.Event()

    def board_body() -> bytes:
        with lock:
            if time.monotonic() - cache["at"] > CACHE_SECONDS:
                ctx = tasks.Context(main_checkout, TaskStore(state_dir))  # fresh: Orca may have recovered
                if time.monotonic() - cache["merges_at"] > MERGES_SECONDS:
                    # Recording a merge is a fact, not a decision: the one change the board makes.
                    sync.fetch(main_checkout, state_dir, force=False)
                    tasks.record_merges(ctx)
                    cache["merges_at"] = time.monotonic()
                cache["body"] = json.dumps(collect(ctx, home)).encode("utf-8")
                cache["at"] = time.monotonic()
            return bytes(cache["body"])

    class Handler(BaseHTTPRequestHandler):
        def _allowed(self) -> bool:
            bound = self.server.server_port  # type: ignore[attr-defined]
            if self.headers.get("Host") in (f"127.0.0.1:{bound}", f"localhost:{bound}"):
                return True
            self._send(403, "text/plain", b"forbidden")  # another site through DNS rebinding
            return False

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0]
            if path == "/health":
                health = {"factory_board": True, "pid": os.getpid(), "repo": str(main_checkout)}
                return self._send(200, "application/json", json.dumps(health).encode("utf-8"))
            last_seen[0] = time.monotonic()
            if path == "/":
                return self._send(200, "text/html; charset=utf-8", page().encode("utf-8"))
            if path == "/api/board":
                try:
                    return self._send(200, "application/json", board_body())
                except Exception as exc:  # the page shows it and keeps polling
                    error = {"error": f"{type(exc).__name__}: {exc}"}
                    return self._send(500, "application/json", json.dumps(error).encode("utf-8"))
            self._send(404, "text/plain", b"not found")

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if not self._allowed():
                return
            if self.path != "/stop" or not secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), token):
                return self._send(403, "text/plain", b"forbidden")
            self._send(200, "application/json", b'{"stopping": true}')
            stopping.set()

        def _send(self, status: int, kind: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    try:
        server = _Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise FactoryError("board_port_busy", f"cannot listen on 127.0.0.1:{port}: {exc}", EXIT_UNAVAILABLE) from exc
    bound = server.server_address[1]
    info_path = state_dir / INFO_FILE
    info = {"pid": os.getpid(), "port": bound, "url": f"http://127.0.0.1:{bound}/", "started_at": now(),
            "repo": str(main_checkout), "token": token, "launch": os.environ.get(LAUNCH_VARIABLE)}
    pending = state_dir / f".{INFO_FILE}.{os.getpid()}.tmp"  # readers never see a half-written file
    pending.write_text(json.dumps(info) + "\n", encoding="utf-8")
    system.replace(pending, info_path)

    def watch() -> None:
        while not stopping.wait(IDLE_CHECK_SECONDS):
            if time.monotonic() - last_seen[0] > idle_minutes * 60:
                stopping.set()
        server.shutdown()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        server.server_close()
        if _read_info(state_dir).get("token") == token:
            info_path.unlink(missing_ok=True)


# --- Background process: start, status, stop, open ------------------------------------------------
# The board is told apart from any other process by HTTP (its /health names this repository and
# its pid), never by inspecting process ids: on Windows a "probe" signal would kill the process.


def _read_info(state_dir: Path) -> dict[str, Any]:
    try:
        info = json.loads((state_dir / INFO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _health(info: dict[str, Any]) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/health", timeout=2) as reply:
            health = json.loads(reply.read())
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return health if isinstance(health, dict) and health.get("factory_board") else None


def running(main_checkout: Path, state_dir: Path) -> dict[str, Any] | None:
    """The board serving this repository and answering, or None. An info file nobody answers for
    (checked twice, in case the board is just busy) is removed."""
    info = _read_info(state_dir)
    if not info:
        return None
    for _ in range(2):
        health = _health(info)
        if health and health.get("pid") == info.get("pid") and health.get("repo") == str(main_checkout):
            return info
    (state_dir / INFO_FILE).unlink(missing_ok=True)
    return None


def _free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            return 0  # the server picks a free one


def start(main_checkout: Path, state_dir: Path, port: int | None) -> dict[str, Any]:
    """Start the board in the background, or return the one already running."""
    with system.exclusive_lock(state_dir / LOCK_FILE):  # two quick `board open` never launch two
        current = running(main_checkout, state_dir)
        if current:
            return {**_public(current), "running": True, "started": False}
        cfg = config.load_config(main_checkout / ".factory" / "config.toml")
        chosen = _free_port(port or cfg["board"]["port"])
        launch = secrets.token_hex(8)
        with (state_dir / LOG_FILE).open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "swfactory", "board", "serve", "--port", str(chosen)],
                cwd=main_checkout, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, LAUNCH_VARIABLE: launch}, **system.detached(),
            )
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline and process.poll() is None:
            info = _read_info(state_dir)
            if info.get("launch") == launch and _health(info):
                return {**_public(info), "running": True, "started": True}
            time.sleep(0.1)
        if process.poll() is None:
            process.terminate()
    raise FactoryError("board_not_started", f"the board did not start; see {state_dir / LOG_FILE}", EXIT_ERROR)


def _public(info: dict[str, Any]) -> dict[str, Any]:
    """The info file without its token (the token only stops the board)."""
    return {k: v for k, v in info.items() if k not in ("token", "launch")}


def status(main_checkout: Path, state_dir: Path) -> dict[str, Any]:
    current = running(main_checkout, state_dir)
    return {**_public(current), "running": True} if current else {"running": False}


def stop(main_checkout: Path, state_dir: Path) -> dict[str, Any]:
    """Ask this repository's board to stop, with the token only it and this user can read."""
    current = running(main_checkout, state_dir)
    if not current:
        return {"running": False, "stopped": False}
    request = urllib.request.Request(f"http://127.0.0.1:{int(current['port'])}/stop", data=b"", method="POST",
                                     headers={TOKEN_HEADER: str(current.get("token", ""))})
    try:
        with urllib.request.urlopen(request, timeout=5):
            pass
    except OSError as exc:
        raise FactoryError("board_not_stopped", f"the board did not accept the stop: {exc}", EXIT_ERROR) from exc
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _health(current):
        time.sleep(0.1)
    if _health(current):
        raise FactoryError("board_not_stopped", "the board is still answering; try again", EXIT_ERROR)
    (state_dir / INFO_FILE).unlink(missing_ok=True)
    return {"running": False, "stopped": True, "pid": current["pid"]}


def open_board(main_checkout: Path, state_dir: Path, port: int | None) -> tuple[dict[str, Any], list[str]]:
    """Start the board if needed and open it in an Orca browser tab, or else in the system browser."""
    info = start(main_checkout, state_dir, port)
    warnings: list[str] = []
    opened: str | None
    try:
        orca.run(["tab", "create", "--url", info["url"], "--worktree", f"path:{main_checkout}"])
        opened = "orca"
    except FactoryError as exc:
        warnings.append(f"could not open an Orca tab ({exc.message}); opening the system browser")
        opened = "browser" if webbrowser.open(info["url"]) else None
    return {**info, "opened_in": opened}, warnings
