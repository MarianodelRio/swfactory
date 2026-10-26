# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The public interface covered by semantic versioning is: the `swfactory` commands and their `--json`
output, the files under `.factory/` (`schema_version`), and the skills' names and triggers.

## [Unreleased]

## [2.1.0] - 2026-10-26

Run `/factory-init` in each repository after upgrading: it updates the skills and the
`## Software Factory` section of `AGENTS.md`.

### Fixed
- A reviewer's `verdict` is honored: a `fail` is recorded even without blocker or major findings
  (with a warning to ask the reviewer what must change), and a `pass` with `blocker` or `major`
  findings is recorded as `fail`, with a warning. `verdict` (`pass` or `fail`)
  is now required in the reviewer JSON.
- `task finish` refuses a workspace with uncommitted changes to tracked files, checks that ran
  while there were any, and a criterion or check whose evidence file is missing. Untracked files
  (not ignored; `.factory/` and `.claude/settings.local.json` aside) are a warning at `verify` and
  `finish`: they are not part of the branch.
- Evidence is no longer overwritten: each `task verify` (and each manual evidence) is saved in its
  own folder, `evidence/<task>/<UTC time>/`, so earlier events keep pointing to their own output.
- Without Orca's model catalog, a Claude model said as a family and a version is completed to
  Claude's id scheme ("opus 5.5", "opus-5.5" and "claude-opus-5.5" are `claude-opus-5-5`) instead
  of being passed as written, which the agent refused; Claude Code's aliases are lowercased
  (`Opus` is `opus`, `Sonnet [1m]` is `sonnet[1m]`). For Codex, a GPT model is completed to
  OpenAI's lowercase ids ("GPT 6.1 Sol", "gpt6.1-sol" are `gpt-6.1-sol`). Other names are still
  passed as written.
- `/factory-init` updates the `## Software Factory` section of `AGENTS.md` when the person has not
  edited it (its hash is recorded in `[templates]`, and the sections earlier versions installed are
  recognized); an edited section is shown as a diff, never overwritten.

### Added
- `swfactory design register` refuses AC commands that need a shell (pipes, `&&`, `;`,
  redirections, globs, `$VAR` outside quotes) and, like `design approve`, warns when an AC
  command's program is not found (a sentence written where a command goes). The design critic
  checks every AC command as the verifier will run it, or asks for `manual`.
- `swfactory doctor` (and the `init` plan) warn about instructions and docs that still name an old
  skill or command (`/factory-implement`, the `factory` command); `/factory-init` fixes them.
- `task verify` records `dirty` in the checks and warns about uncommitted changes and untracked
  files.

## [2.0.0] - 2026-10-20

A major version: `/factory-implement` is renamed `/factory-build` and `done` now means merged.
Upgrade notes are in the user guide (section 14, *Upgrading*).

### Added
- `swfactory sync`: fetches `origin`, fast-forwards the base branch when the main checkout is on
  it, rebases and pushes local approval commits. It never merges, forces or touches uncommitted
  work; what it cannot do is a warning.

- A new task state, `to_merge`: `task finish` moves a task there, and it becomes `done` once its
  work is merged into the base branch. A pull request merged on GitHub counts, also squashed or
  rebased (`gh pr view`); without one, the branch's commits must be in the base. A pull request
  closed without merging needs the person. `swfactory task merged <task>` records a merge the
  Factory cannot see. The board has a *To merge* column and records merges once a minute.
- Each task chooses how it integrates: `integrate: pr` (the person merges the PR) or
  `integrate: merge` (`task publish` opens the PR and merges it right away with a merge commit).
  The default is `git.integrate` (`pr`); the designer asks in the outline.
- The task branch is pushed when the task is claimed (visible on GitHub from the start) and again
  when it is finished. The PR is linked to the Orca card.
- After a merge the remote branch is deleted, and `git.cleanup` (`ask`, `always`, `never`) decides
  whether the workspace and the local branch are removed. `swfactory task cleanup <task>` removes
  them (never with uncommitted files), `--keep` keeps them.

- `swfactory task recover` also hands a `blocked` task (not `design_changed`) to another agent;
  `--replace` confirms that a session whose terminal is still open is abandoned.
- `swfactory design register` warns about independent tasks whose `scope` overlaps.

- Models for the lead and the conformance reviewer, named as the person says them ("opus 5.5"):
  `task launch --model/--effort/--reviewer-model/--reviewer-effort`, resolved with Orca's model
  catalog (`swfactory models`), recorded in the attempt and used for the conformance review.
  `[agents]` in the config sets the default agents, models and efforts.

- `/factory-init` proposes migrating what another workflow left: instruction files, skills,
  agents and commands of other setups, and task or progress trackers (`migrate` in the plan).
  A folder of another workflow can be deleted whole, with backup.
- `/factory-init` creates a root `README.md` when there is none.

- `/factory-quick`: a small change without design or workspace, with the project checks
  (`swfactory check`, which records nothing), a commit and a push with a PR or a merge.
- `/factory-help`: how the Factory works and the next step in this repository.
- `/factory-build` launches several tasks at once: a list of ids, a change (its ready tasks) or
  every ready task, after one confirmation.
- The user guide has an *Upgrading* section, with the notes for moving from 1.x.

### Changed
- `/factory-implement` is now `/factory-build`. `/factory-init` installs it and removes the old
  skill's files; until then, leads are launched with the old skill's name.
- The index of `docs/` is `docs/index.md`, and the decisions index `docs/decisions/index.md` (a
  README is for people). `/factory-init` renames an existing `docs/decisions/README.md` and fixes
  the links to it in `AGENTS.md` and `docs/`.
- The board is simpler: a logo, no *Activity* feed, and shorter cards (the next step moved to the
  card's details; checks show only when they fail; one line for the running subagents). The backlog
  shows ready tasks first, *Done* the tasks merged most recently first, and the other columns the
  most recently updated first.
- Shorter, more descriptive ids: changes are `CHG-12` (in a `CHG-12-<slug>` folder, from
  `design register --slug`), tasks `CHG-12.1`, and every command takes them typed short (`12`,
  `12.1`). Orca workspaces and branches are named after the task and its title
  (`chg-12.1-recurrence-model`) and the card shows `12.1 · <title>`. Ids of earlier versions
  (`CHG-0012`, `CHG-0012-01`) keep working, and numbering goes on after them.
- The designer and the design critic make every real dependency explicit in `depends_on`, and the
  lead blocks a task with `missing dependency: …` when the design missed one.
- A command refused because the task is blocked says how to unblock it.
- `done` now means merged: dependencies only wait for their tasks to be `done`, without git checks.
  Tasks finished with an earlier version are checked once and go back to `to_merge` if they are
  not merged.
- Claiming a task and approving or rejecting a design fetch `origin` first; `swfactory status`
  fetches at most once a minute. Merges done on GitHub are seen without pulling by hand.
- Task workspaces start from `origin/<base>` when the approval is there (the local base otherwise).
- Approving or rejecting pushes the base branch when the approval is all it would publish; the
  person's own unpushed commits are never pushed (a warning says so).

## [1.1.0] - 2026-10-18

### Added
- Windows support. The CI runs every test on Linux, macOS and Windows with Python 3.11-3.13.
- `SWFACTORY_ORCA` and `SWFACTORY_GH`: how to run Orca's or GitHub's CLI when it is not on `PATH`
  (a path, or a JSON list of a program and its arguments).

### Changed
- Task state locks work on every OS: writers take turns on a `.<task>.mutex` file next to the
  state, with `fcntl` on Linux and macOS and `msvcrt` on Windows.
- The board no longer inspects process ids: `swfactory board stop` asks the board to stop over
  HTTP with a token only its owner can read, and the board recognizes the server it launched.
- Repository files written by `/factory-init` and approvals always use LF line endings; paths
  recorded in state and plans always use `/`.
- Output of `git`, `orca`, `gh` and the check commands is read as UTF-8, and the CLI writes UTF-8.

### Fixed
- The board's `board.json` could be read half-written while the board started (an intermittent
  test failure); it is now written atomically.
- On Windows, a check command's backslashes are no longer eaten, and `orca.cmd` shims never get
  text that `cmd.exe` would reinterpret.

## [1.0.0] - 2026-10-18

First stable release, published as `swfactory` on PyPI.

### Added
- Five skills for Claude Code and Codex: `/factory-init`, `/factory-design`, `/factory-implement`,
  `/factory-status` and `/factory-board`.
- The `swfactory` CLI: designs with explicit approval, tasks in
  their own Orca workspace, verification with evidence, quality and conformance reviews, closing
  gates, publishing the task branch and PR, notes, pauses, interruption and recovery, project
  memory checks.
- `swfactory board`: a live, read-only kanban of the tasks in an Orca tab or any browser, with the
  agents, terminals and subagents running in each task.
- `/factory-init` installs a prompt rule for Codex (`.codex/rules/factory.rules`) so approving or
  rejecting a design asks the person, as Claude Code's `permissions.ask` does.
- A user guide (`docs/guide.md`).

### Changed
- The package, the module and the command are now `swfactory` (formerly `factory`).
- A lead agent that dies while its task is in review is now detected as interrupted, and recovery
  keeps the reviews already recorded.

### Verified
- End to end on Linux with Claude Code and Orca 1.4.229. The test suite passes on Linux and macOS
  with Python 3.11-3.13. Not yet verified on a real machine: Codex, and macOS with Orca. Windows
  is not supported.

## [0.1.0] - 2026-10-18

Initial development version (not published to a package registry).

[Unreleased]: https://github.com/MarianodelRio/swfactory/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/MarianodelRio/swfactory/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/MarianodelRio/swfactory/compare/v1.1.0...v2.0.0
[1.1.0]: https://github.com/MarianodelRio/swfactory/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/MarianodelRio/swfactory/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/MarianodelRio/swfactory/releases/tag/v0.1.0
