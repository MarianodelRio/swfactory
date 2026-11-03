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
- Before implementing, save a short proof plan outside the repo and record it with
  `swfactory task proof-plan <task> --file <plan.json> --json`. Key it by every assigned AC-NN
  and every change-wide C-NN. Each entry has `code`, `scenario`, `assertion`, `verification`
  (an execution id from `task verify --plan`, `manual` or `inspection`) and `discrimination`
  (which old/incorrect behavior the test detects, or why inspection/manual proof applies).
  Update it when the approach changes. For important changed behavior, observe the expected
  assertion fail on old behavior or a targeted fault, then pass after the fix; import/fixture
  failures are not proof. Attach focused output with `task proof-plan ... --evidence <text-file>`.
  Keep one plan and reuse it in reviews; shared execution does not imply shared semantic coverage.

When the provider session id is available, register it with
`swfactory task session-attach <task> --provider <claude|codex> --session <id> --role lead --json`.
Never invent an id. When launching separate validation sessions (especially from temporary
folders), capture their provider ids and attach each with `--role validation`, plus `--parent`
when known. Proven descendants are discovered automatically. Unregistered history remains
explicitly partial in `/factory-improve`.

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

1. Evaluate and write project memory using `memory.md` before final verification and reviews.
   `task show` exposes `memory_policy`; configured package docs are valid destinations too.
   Commit the memory with this task. Do not revert a lesson just because it is outside product
   scope; inspect the policy. Instructions, ADRs and executable docs may require fresh review.
2. Commit first: `finish` refuses uncommitted changes and checks run while there were any. Keep
   evidence files outside the repo (or ignored), so they do not end up in the branch.
   Use focused tests while developing. For final acceptance run
   `swfactory task verify <task> --json` directly, without launching the same full suite first.
   It runs project checks and AC commands; explicit shared references reuse one execution in
   that invocation. `swfactory task verify <task> --plan --json` shows the execution plan.
   A new verify always runs again; do not edit the approved design to remove checks. For a
   `manual` AC, produce real evidence (a note, `orca screenshot`, `orca snapshot`)
   and record it: `swfactory task verify <task> --manual AC-NN --evidence <file> --json`. Use
   computer use only when there is no programmatic way. Never record a secret file as evidence.
3. Re-read your whole diff (`git diff <base>...HEAD`).
4. If the task needs no reviews (`review: []` and none forced by the config or asked by the person),
   skip `submit` and go to "Memory, finish and publish". Otherwise run
   `swfactory task submit <task> --json`: it hands over pending notes first (stop if `paused`) and
   lists the reviews the task needs.

## Reviews

Run **quality first, then conformance** when both are required. Do not change code while a
review is outstanding. `task show` reports `review_status`: only `passed` covers the current
code and design; `stale` means repeat it after corrections.

1. `swfactory task review <task> --type quality --json` reserves a review id and returns its
   `prompt`. Launch a read-only native subagent with exactly that prompt. If your client has no
   subagents, add `--agent <agent>` and Orca launches it. The reviewer delivers its report
   directly with `task review-complete`; never copy its JSON from a terminal.
2. `swfactory task review-wait <task> --review <id> --timeout 60 --json` observes the persisted
   result. If `pending` is true, the review is still unresolved: wait again on the same id.
   Terminal silence, a timeout or an idle TUI is never a verdict. Do not schedule continuation
   wakeups or relaunch on silence. A repeated launch request reuses the reserved review.
3. Read the recorded findings. Fix blocker/major findings or the reason for a `fail`, commit,
   verify and repeat quality as needed. Assess prior findings by id; do not silently omit them.
   The configured limit counts applicable failures per type since the last authorized continuation,
   not passes or delivery retries. If blocked, explain `review_diagnosis` and outstanding findings;
   stop and tell the person. Minor findings go to the PR.
4. Once quality is a current pass, `swfactory task review <task> --type conformance --json`
   launches the chosen conformance reviewer. Wait on its returned review id the same way.
   If its corrections change code, verify and obtain a current quality pass before repeating
   conformance. Use `task show` to identify obsolete passes before attempting finish.
5. A failed launch or abandoned reviewer remains visible. Inspect it; only when it is known to
   be abandoned use `swfactory task review-cancel <task> --review <id> --reason "..." --json`
   before starting a replacement. Cancellation does not stop its agent. Late reports remain
   in history, without replacing a newer result or reopening a closed task.

Reviewers do not edit product files or approve designs. Their permitted write is their own
report via `swfactory task review-complete <task> --review <id> --file <report.json> --json`
(or `--file -` for stdin). The file lives outside the repo. Legacy `task review --record`
is for historical/manual reports, not the managed review flow.

## Memory, finish and publish

1. Memory was prepared before reviews. If a new lesson appears now, commit it and refresh the
   reviews indicated by `task show`; behavioral checks only need repeating when their inputs changed.
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
4. If a PR was opened, check it can still be merged: `gh pr view <number> --json mergeable,mergeStateStatus`
   (the number is `pull_request.number` from the publish answer). If `mergeable` is `UNKNOWN`,
   GitHub is still computing it: wait a few seconds and check again. If it is `CONFLICTING`,
   `git fetch origin <base>` and `git merge origin/<base>` in the workspace (never rebase: it would
   need a force push), resolve the conflicts yourself, commit, re-run
   `swfactory task verify <task> --json` (merging the base can change behavior), then
   `swfactory task publish <task> --json` again to push the resolved branch. If the merge itself
   has conflicts you cannot resolve with what the task knows, stop and tell the person instead of
   guessing at the base's intent.
