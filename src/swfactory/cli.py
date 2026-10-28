"""`swfactory` command line: argparse subcommands, output and exit codes. No logic lives here."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from swfactory import __version__, board, config, design, improve, init, memory, models, paths, tasks
from swfactory.output import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PRECONDITION,
    EXIT_USAGE,
    FactoryError,
    error_envelope,
    ok_envelope,
    render,
)
from swfactory.state import TaskStore

Result = tuple[dict[str, Any], list[str]]
Handler = Callable[[argparse.Namespace], Result]


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{self.prog}: {message}")


def change_id(value: str) -> str:
    value = design.full_id(value)
    if not design.CHANGE_ID.match(value):
        raise argparse.ArgumentTypeError(f"'{value}' is not a change id (CHG-N)")
    return value


def task_id(value: str) -> str:
    return design.full_id(value)


def task_ids(value: str) -> list[str]:
    return [task_id(part) for part in value.split(",") if part.strip()]


def design_register(args: argparse.Namespace) -> Result:
    if args.change is None:
        return design.reserve_change(paths.changes_dir(), args.slug), []
    return design.register(args.change, paths.changes_dir())


def design_show(args: argparse.Namespace) -> Result:
    store = TaskStore(paths.state_dir())
    return design.show(args.change, paths.changes_dir(), store, paths.main_checkout() / ".factory" / "changes"), []


def stdin_is_terminal() -> bool:
    return sys.stdin.isatty()


def design_approve(args: argparse.Namespace) -> Result:
    if args.message is None:
        if not stdin_is_terminal():
            message = "without --message, approve asks the person to type the id in a terminal"
            raise FactoryError("approval_not_confirmed", message, EXIT_PRECONDITION)
        print(f"Type {args.change} to approve its current design: ", end="", file=sys.stderr, flush=True)
        message, method = input(), "terminal"
    else:
        message, method = args.message, "chat"
    store = TaskStore(paths.state_dir())
    return design.approve(args.change, paths.changes_dir(), message, method, paths.main_checkout(), store)


def design_reject(args: argparse.Namespace) -> Result:
    return design.reject(args.change, paths.changes_dir(), args.reason, paths.main_checkout(), paths.state_dir())


def task_context() -> tasks.Context:
    return tasks.Context(paths.main_checkout(), TaskStore(paths.state_dir()))


def task_list(args: argparse.Namespace) -> Result:
    return tasks.list_tasks(task_context()), []


def task_show(args: argparse.Namespace) -> Result:
    return tasks.show_task(args.task, task_context()), []


def task_claim(args: argparse.Namespace) -> Result:
    return tasks.claim(args.task, task_context())


def task_launch(args: argparse.Namespace) -> Result:
    return tasks.launch(args.task, args.agent, args.reviewer, args.model, args.effort, task_context(),
                        args.reviewer_model, args.reviewer_effort)


def task_checkpoint(args: argparse.Namespace) -> Result:
    return tasks.checkpoint(args.task, args.done, args.next, task_context())


def task_note(args: argparse.Namespace) -> Result:
    return tasks.note(args.task, args.text, args.pause, task_context())


def task_block(args: argparse.Namespace) -> Result:
    return tasks.block(args.task, args.reason, task_context())


def task_unblock(args: argparse.Namespace) -> Result:
    return tasks.unblock(args.task, task_context())


def task_submit(args: argparse.Namespace) -> Result:
    return tasks.submit(args.task, task_context())


def task_review(args: argparse.Namespace) -> Result:
    if args.record:
        return tasks.record_review(args.task, Path(args.record).resolve(), args.agent, task_context())
    return tasks.review(args.task, args.type, args.agent, task_context(), args.model, args.effort)


def task_publish(args: argparse.Namespace) -> Result:
    return tasks.publish(args.task, task_context())


def task_cleanup(args: argparse.Namespace) -> Result:
    return tasks.cleanup(args.task, task_context(), args.keep)


def task_merged(args: argparse.Namespace) -> Result:
    return tasks.mark_merged(args.task, task_context())


def task_recover(args: argparse.Namespace) -> Result:
    return tasks.recover(args.task, args.confirm, task_context(), args.replace)


def init_command(args: argparse.Namespace) -> Result:
    if args.restore:
        return init.restore(paths.repo_root(), paths.state_dir(), args.restore), []
    deletes = list(zip(args.delete, args.reason, strict=True))
    if args.plan:
        return init.build_plan(paths.repo_root(), paths.main_checkout(), deletes).data(), []
    return init.apply_init(paths.repo_root(), paths.main_checkout(), paths.state_dir(), args.apply, deletes,
                           args.exclude)


def board_command(args: argparse.Namespace) -> Result:
    main_checkout, state_dir = paths.main_checkout(), paths.state_dir()
    action = args.command.removeprefix("board ")
    if action == "start":
        return board.start(main_checkout, state_dir, args.port), []
    if action == "open":
        return board.open_board(main_checkout, state_dir, args.port)
    if action == "status":
        return board.status(main_checkout, state_dir), []
    if action == "stop":
        return board.stop(main_checkout, state_dir), []
    if action == "snapshot":
        return board.snapshot(task_context(), Path.home(), Path(args.file).resolve()), []
    cfg = config.load_config(main_checkout / ".factory" / "config.toml")
    board.serve(main_checkout, state_dir, args.port or cfg["board"]["port"], cfg["board"]["idle_minutes"],
                Path.home())
    return {"stopped": True}, []


def install_skill(args: argparse.Namespace) -> Result:
    return init.install_skill(Path.home()), []


def models_command(args: argparse.Namespace) -> Result:
    return models.list_models(Path.home(), args.agent), []


def doctor(args: argparse.Namespace) -> Result:
    return init.doctor(paths.repo_root(), paths.main_checkout()), []


def memory_check(args: argparse.Namespace) -> Result:
    return memory.check(paths.repo_root()), []


def improve_extract(args: argparse.Namespace) -> Result:
    return improve.extract(args.tasks, TaskStore(paths.state_dir()), Path.home()), []


def improve_audit(args: argparse.Namespace) -> Result:
    return improve.audit(), []


def status(args: argparse.Namespace) -> Result:
    return tasks.status(task_context())


def check_command(args: argparse.Namespace) -> Result:
    return tasks.run_checks(paths.repo_root(), task_context())


def sync_command(args: argparse.Namespace) -> Result:
    return tasks.sync_command(task_context())


def task_finish(args: argparse.Namespace) -> Result:
    return tasks.finish(args.task, args.memory, task_context())


def task_verify(args: argparse.Namespace) -> Result:
    if args.manual:
        return tasks.verify_manual(args.task, args.manual, Path(args.evidence).resolve(), task_context())
    return tasks.verify(args.task, task_context())


# Command name ("task launch") -> handler.
HANDLERS: dict[str, Handler] = {
    "design register": design_register,
    "design show": design_show,
    "design approve": design_approve,
    "design reject": design_reject,
    "task list": task_list,
    "task show": task_show,
    "task claim": task_claim,
    "task launch": task_launch,
    "task checkpoint": task_checkpoint,
    "task note": task_note,
    "task block": task_block,
    "task unblock": task_unblock,
    "task verify": task_verify,
    "task submit": task_submit,
    "task review": task_review,
    "task finish": task_finish,
    "task publish": task_publish,
    "task merged": task_merged,
    "task cleanup": task_cleanup,
    "task recover": task_recover,
    "status": status,
    "sync": sync_command,
    "check": check_command,
    "memory check": memory_check,
    "improve extract": improve_extract,
    "improve audit": improve_audit,
    "doctor": doctor,
    "models": models_command,
    "init": init_command,
    "install-skill": install_skill,
    **{f"board {action}": board_command for action in ("start", "open", "status", "stop", "serve", "snapshot")},
}


def build_parser() -> Parser:
    json_flag = Parser(add_help=False)
    json_flag.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON object"
    )

    description = "swfactory: build software with coding agents on Orca."
    parser = Parser(prog="swfactory", description=description, parents=[json_flag])
    parser.add_argument("--version", action="version", version=f"swfactory {__version__}")
    groups = parser.add_subparsers(dest="group", required=True, metavar="<command>")

    def command(subparsers: Any, name: str, full_name: str, help_text: str) -> Parser:
        sub: Parser = subparsers.add_parser(name, help=help_text, parents=[json_flag])
        sub.set_defaults(command=full_name)
        return sub

    command(groups, "install-skill", "install-skill", "install /factory-init for this user (once per machine)")
    command(groups, "doctor", "doctor", "analyze the repository without changing it")
    command(groups, "models", "models", "models Orca knows for each agent").add_argument(
        "--agent", choices=["claude", "codex"])

    init_cmd = command(groups, "init", "init", "prepare the repository for the Factory")
    mode = init_cmd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="show the plan without applying it")
    mode.add_argument("--apply", metavar="PLAN_ID", help="apply exactly the confirmed plan")
    mode.add_argument("--restore", metavar="BACKUP", help="restore a backup")
    init_cmd.add_argument("--delete", action="append", default=[], metavar="PATH", help="also delete this file")
    init_cmd.add_argument("--reason", action="append", default=[], help="reason for each --delete")
    init_cmd.add_argument("--exclude", action="append", default=[], metavar="PATH", help="leave this item out")

    design = groups.add_parser("design", help="register, show, approve or reject a change")
    design_cmds = design.add_subparsers(dest="action", required=True, metavar="<action>")
    register = command(design_cmds, "register", "design register", "reserve an id, or validate a design")
    register.add_argument("change", nargs="?", type=change_id, help="omit to reserve the next id")
    register.add_argument("--slug", help="when reserving: 2-4 kebab-case words for the folder name")
    command(design_cmds, "show", "design show", "show a change").add_argument("change", type=change_id)
    approve = command(design_cmds, "approve", "design approve", "approve a change (base branch only)")
    approve.add_argument("change", type=change_id)
    approve.add_argument("--message", help="the person's literal approval message")
    reject = command(design_cmds, "reject", "design reject", "reject a change")
    reject.add_argument("change", type=change_id)
    reject.add_argument("--reason", required=True)

    task = groups.add_parser("task", help="task lifecycle")
    task_cmds = task.add_subparsers(dest="action", required=True, metavar="<action>")
    command(task_cmds, "list", "task list", "list tasks and their states")
    for name, help_text in (
        ("show", "show a task"),
        ("claim", "claim a task and create its Orca workspace"),
        ("unblock", "return a blocked task to its previous state"),
        ("submit", "move a task to in_review"),
        ("publish", "push the task branch and open a PR"),
        ("merged", "mark a finished task as merged (when the Factory cannot see it)"),
    ):
        command(task_cmds, name, f"task {name}", help_text).add_argument("task", type=task_id)

    launch = command(task_cmds, "launch", "task launch", "launch the lead agent in the task workspace")
    launch.add_argument("task", type=task_id)
    launch.add_argument("--agent", choices=["claude", "codex"], help="default: agents.lead in the config")
    launch.add_argument("--reviewer", choices=["claude", "codex"], help="agent for the conformance review")
    launch.add_argument("--model", help='model id or name as said ("opus 5.5")')
    launch.add_argument("--effort")
    launch.add_argument("--reviewer-model", help="model of the conformance reviewer")
    launch.add_argument("--reviewer-effort")

    checkpoint = command(task_cmds, "checkpoint", "task checkpoint", "record progress, read pending notes")
    checkpoint.add_argument("task", type=task_id)
    checkpoint.add_argument("--done", required=True)
    checkpoint.add_argument("--next", required=True)

    note = command(task_cmds, "note", "task note", "leave a note for the agent")
    note.add_argument("task", type=task_id)
    note.add_argument("--text", required=True)
    note.add_argument("--pause", action="store_true", help="ask the agent to pause")

    block = command(task_cmds, "block", "task block", "move a task to blocked")
    block.add_argument("task", type=task_id)
    block.add_argument("--reason", required=True)

    verify = command(task_cmds, "verify", "task verify", "run checks and acceptance criteria")
    verify.add_argument("task", type=task_id)
    verify.add_argument("--manual", metavar="AC", help="acceptance criterion verified by hand")
    verify.add_argument("--evidence", metavar="FILE", help="evidence file for --manual")

    review = command(task_cmds, "review", "task review", "launch or record a review")
    review.add_argument("task", type=task_id)
    review_mode = review.add_mutually_exclusive_group(required=True)
    review_mode.add_argument("--type", choices=["quality", "conformance"])
    review_mode.add_argument("--record", metavar="FILE", help="reviewer JSON to record")
    review.add_argument("--agent")
    review.add_argument("--model", help="default: the reviewer model chosen at launch")
    review.add_argument("--effort")

    finish = command(task_cmds, "finish", "task finish", "close a task when every gate passes")
    finish.add_argument("task", type=task_id)
    finish.add_argument("--memory", required=True, help='memory summary, or "none"')

    cleanup = command(task_cmds, "cleanup", "task cleanup", "remove a merged task's workspace and local branch")
    cleanup.add_argument("task", type=task_id)
    cleanup.add_argument("--keep", action="store_true", help="keep them, and stop offering it")

    recover = command(task_cmds, "recover", "task recover", "inspect an interrupted or blocked task")
    recover.add_argument("task", type=task_id)
    recover.add_argument("--confirm", action="store_true", help="start a new attempt")
    recover.add_argument("--replace", action="store_true",
                         help="the previous session is abandoned even if its terminal is open")

    memory = groups.add_parser("memory", help="project memory maintenance")
    memory_cmds = memory.add_subparsers(dest="action", required=True, metavar="<action>")
    command(memory_cmds, "check", "memory check", "check AGENTS.md, decisions and links")

    improve_group = groups.add_parser("improve", help="evidence for improving swfactory itself")
    improve_cmds = improve_group.add_subparsers(dest="action", required=True, metavar="<action>")
    extract_cmd = command(improve_cmds, "extract", "improve extract",
                          "deterministic facts from task state and agent sessions")
    extract_cmd.add_argument("--tasks", required=True, type=task_ids, metavar="CHG-N.M,...",
                             help="comma-separated task ids")
    command(improve_cmds, "audit", "improve audit", "check swfactory's own shipped skills")

    command(groups, "status", "status", "summary of changes and tasks")
    command(groups, "sync", "sync", "fetch origin and bring the base branch up to date")
    command(groups, "check", "check", "run the project checks here (for a quick change; nothing is recorded)")

    board_group = groups.add_parser("board", help="local kanban board of the tasks (read-only)")
    board_cmds = board_group.add_subparsers(dest="action", required=True, metavar="<action>")
    for name, help_text in (
        ("start", "start the board in the background"),
        ("open", "start it if needed and open it in an Orca tab (or the browser)"),
        ("serve", "serve the board in the foreground"),
    ):
        command(board_cmds, name, f"board {name}", help_text).add_argument(
            "--port", type=int, help="default: board.port in .factory/config.toml")
    command(board_cmds, "status", "board status", "is the board running, and where")
    command(board_cmds, "stop", "board stop", "stop the board")
    command(board_cmds, "snapshot", "board snapshot", "write the board with its data to one HTML file").add_argument(
        "file")
    return parser


def _validate(args: argparse.Namespace) -> None:
    if args.command == "init" and len(args.delete) != len(args.reason):
        raise UsageError("swfactory init: each --delete needs its --reason")
    if args.command == "task verify" and (args.manual is None) != (args.evidence is None):
        raise UsageError("swfactory task verify: --manual and --evidence go together")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        _validate(args)
    except UsageError as exc:
        return _emit(error_envelope(FactoryError("usage_error", str(exc), EXIT_USAGE)), as_json, EXIT_USAGE)

    handler = HANDLERS[args.command]
    try:
        data, warnings = handler(args)
    except FactoryError as exc:
        return _emit(error_envelope(exc), as_json, exc.exit_code)
    except Exception as exc:  # noqa: BLE001 - last resort: still answer with the contract
        error = FactoryError("unexpected_error", f"{type(exc).__name__}: {exc}", EXIT_ERROR)
        return _emit(error_envelope(error), as_json, EXIT_ERROR)
    return _emit(ok_envelope(data, warnings), as_json, EXIT_OK)


def _emit(envelope: dict[str, Any], as_json: bool, code: int) -> int:
    stream = sys.stdout if as_json or envelope["ok"] else sys.stderr
    print(render(envelope, as_json), file=stream)
    return code


def run() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to the ANSI code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
