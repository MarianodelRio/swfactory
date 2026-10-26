# Lead agent flow

Your role: `roles/lead.md`. You own the task end to end, inside its workspace.

## Before writing code

- `swfactory task show <task> --json`: acceptance criteria, `complexity`, `review`, `scope`, state,
  last checkpoint and unread notes. If there is a previous checkpoint, continue from it.
- If the task is `blocked`, tell the person why and ask before going on; with their OK,
  `swfactory task unblock <task> --json` (a `design_changed` block waits for the design's approval).
- If this is a recovered attempt and the task already has reviews, verify and submit again, then run
  only the reviews whose latest verdict is not `pass` on the current commit.
- Read the approved design (`design` in `swfactory task show`), `docs/conventions.md` and
  `docs/working-agreements.md`, and the decisions it cites.

## Work by complexity

| Complexity | Flow |
|---|---|
| `trivial` | Implement → checks |
| `normal` | Short plan → tests first → implement → checks → reviews |
| `complex` | Plan with a Planner subagent (`roles/planner.md`) → small steps with checkpoints → checks → reviews |

- **Checkpoint** after each significant step: `swfactory task checkpoint <task> --done "..." --next "..." --json`.
  Read the `notes` it returns. If `paused` is true, stop immediately and say so.
- **Commit** each coherent step on the task branch with a clear message.
- If something forces a design change (new requirement, architecture), stop:
  `swfactory task block <task> --reason "..." --json`. Do not improvise architecture.
- If the task needs another task's work that is not in the base branch yet (a dependency the design
  missed), block it with `--reason "missing dependency: <task>"` and tell the person: the design
  is adjusted (`depends_on`) and approved again.

## Verify and submit

1. Commit first: `finish` refuses uncommitted changes and checks run while there were any. Keep
   evidence files outside the repo (or ignored), so they do not end up in the branch.
   `swfactory task verify <task> --json` runs the project checks and each AC command in this
   workspace. For a `manual` AC, produce real evidence (a note, `orca screenshot`, `orca snapshot`)
   and record it: `swfactory task verify <task> --manual AC-NN --evidence <file> --json`. Use
   computer use only when there is no programmatic way. Never record a secret file as evidence.
2. Re-read your whole diff (`git diff <base>...HEAD`).
3. If the task needs no reviews (`review: []` and none forced by the config or asked by the person),
   skip `submit` and go to "Memory, finish and publish". Otherwise run
   `swfactory task submit <task> --json`: it hands over pending notes first (stop if `paused`) and
   lists the reviews the task needs.

## Reviews

- **Quality** (if required): start a read-only subagent with `roles/quality_reviewer.md` on the
  diff. If your client has no subagents, run `swfactory task review <task> --type quality --agent <agent> --json`
  with the conformance reviewer's provider (or your own if there is none).
- **Conformance** (if required): `swfactory task review <task> --type conformance --json` (add
  `--agent <claude|codex>` if no reviewer was chosen at launch). Orca opens the reviewer in this
  workspace, with the model chosen for it at launch.
- For a reviewer launched in Orca, read its terminal (the `terminal` in the answer) with
  `orca terminal read --terminal <terminal>` until its final JSON appears; `orca terminal wait`
  can return before the agent has finished, so never take its output as the answer by itself.
- Each reviewer ends with JSON (`type`, `commit`, `verdict`, `findings`). Save it to a file outside
  the repo (so it is not committed) and record it: `swfactory task review <task> --record <file> --json`.
- Fix `blocker` and `major` findings (or, for a `fail` without them, what the reviewer says must
  change), verify again and repeat that review. After two rounds
  without `pass` the CLI blocks the task with the findings saved: stop and tell the person.
  `minor` findings do not block; they go to the PR.
- Reviewers never edit files or approve anything.

## Memory, finish and publish

1. Ask the questions of `memory.md`, write what applies on this branch and commit it (it goes in
   the same PR).
2. `swfactory task finish <task> --memory "<summary of what you proposed>" --json` (or
   `--memory none`): the task becomes `to_merge`, and `done` once its work is merged. It hands over pending notes first: stop if `paused`. If it answers
   `gates_not_met`, fix each item in `error.details`. Mention any "changed outside the task scope"
   warning to the person; the conformance review decides whether it is a deviation.
3. Publishing follows `git.pr_on_done` in `.factory/config.toml` and the task's integration
   (`integrate` in `swfactory task show`, else `git.integrate`):
   - `pr`: `offer` (default) → offer "push the branch and open the PR?"; `always` → do it;
     `never` → do not. The person merges the PR.
   - `merge`: `offer` → offer "push and merge into <base>?"; `always` → do it; `never` → do not.
     The publish opens the PR and merges it right away (a merge commit); if GitHub refuses (checks,
     conflicts), the PR stays open for the person.
   Run `swfactory task publish <task> --json` (also directly if the person asks). Running it again
   pushes later commits to the same PR. If `gh` is missing or the push is rejected, report it: the
   branch stays intact and the PR (or merge) is left to the person. Never merge any other way,
   never force push, never delete branches or workspaces yourself.
