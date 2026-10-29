# swfactory — user guide

This guide explains what the Factory does and how to use it, from the point of view of the person
working with it. For installation, see the [README](../README.md).

## 1. What it is

swfactory is a method for building software with coding agents (Claude Code, Codex), plus
the tools that enforce it. Every change goes through the same steps:

**design → approve → implement in an isolated workspace → verify with evidence → review →
publish → remember what was learned**

It is made of:

- **Eight skills** you call in plain language from an agent terminal: `/factory-init`,
  `/factory-design`, `/factory-build`, `/factory-quick`, `/factory-status`, `/factory-board`,
  `/factory-help` and `/factory-improve`.
- **A small CLI**, `swfactory`. The skills use it for everything that must be exact: approvals, task
  claims, state and evidence. You don't need to use it, but you can.
- **Readable files** in your repository (`.factory/`, `AGENTS.md`, `docs/`) that hold the designs,
  the task state and the project's memory.

The Factory does not run agents or manage worktrees itself. **Orca** does: it creates a workspace
(a Git worktree with its own branch) for each task, launches the agents in it and shows each task
as a card in its sidebar. The Factory brings the method and keeps the record.

### What it is good for

- Changes big enough to deserve a design: a feature, a refactor, an integration. Small edits don't
  need a design: use `/factory-quick`.
- Working on several tasks at once without them stepping on each other: each task has its own
  workspace and branch, and your main session stays free.
- Knowing that "done" means proven: each acceptance criterion has recorded evidence, the checks
  pass and the reviews passed on the final code.
- Continuing work with another agent or provider: everything needed is in files, not in a chat.

### What it never does

It never deploys or force-pushes. It merges only tasks you designed with `integrate: merge`, and
it only deletes merged branches and, if you agree, merged workspaces. It never approves a
design without your explicit message. It never deletes or overwrites your files without showing
you first and keeping a backup.

## 2. Key ideas

| Term | Meaning |
|---|---|
| **Change** (`CHG-1`) | One piece of work you want: a feature, a fix, a refactor. It has one design. Its folder carries a short slug: `.factory/changes/CHG-1-weekly-recurrence/`. |
| **Design** | `.factory/changes/CHG-1-…/design.md`: context, goal and scope, acceptance criteria, approach, tasks, risks. Written by the agent, approved by you. |
| **Task** (`CHG-1.1`) | A slice of a change that one agent implements in one workspace. A change has one or more tasks; a task can depend on others. You can type it short: `1.1`. Its Orca workspace and branch are named after it and its title (`chg-1.1-recurrence-model`). |
| **Acceptance criterion** (`AC-01`) | A verifiable statement ("Given …, when …, then …") and how it is checked: a command, or `manual` with an evidence file. |
| **Evidence** | The recorded output of each check and criterion, saved under `.factory/state/evidence/`. |
| **Checks** | The project's own test, lint, typecheck and build commands, configured once in `.factory/config.toml`. |
| **Reviews** | Independent reviews of the finished code. *Quality* covers maintainability; *conformance* checks that it does what the approved design says. |
| **Lead agent** | The agent that implements a task, inside the task's workspace. |
| **Memory** | What the project learns, kept in `AGENTS.md` and `docs/`: decisions (ADRs), working agreements, known pitfalls, architecture and product notes. |

### Who does what

| You | The agents | Orca |
|---|---|---|
| Ask for changes, say OK to outlines, **approve or reject** designs, answer questions, accept or decline working agreements, review and **merge** PRs | Research, design, implement, test, verify, review, propose memory updates, push the task branch | Workspaces and branches, launching agents, the sidebar cards, knowing whether a terminal is still open |

## 3. The life of a change

```
 you: /factory-design <what you want>
      │  agent researches the code, shows a short outline, waits for your OK
      │  writes design.md, self-reviews it, validates it
      ▼
 change CHG-1  ── awaiting approval
      │  you: "apruebo CHG-1" / "approve CHG-1"  (+ the permission prompt)
      ▼
 approved: tasks CHG-1.1, CHG-1.2 …  ── ready (or pending, waiting for a dependency)
      │  you: /factory-build CHG-1.1 con claude, revisa claude
      ▼
 Orca workspace + branch, lead agent launched ── in_progress
      │  tests first, code, commits, checkpoints, verify (checks + every AC)
      ▼
 submit ── in_review
      │  quality and/or conformance reviews; fix and repeat if needed
      ▼
 memory updated on the task branch, finish ── to_merge
      │  publish: push the branch and open the PR, or merge it right away (the task's integrate)
      ▼
 merged ── done → tasks that depended on it become ready; the workspace can be removed
```

## 4. Preparing a repository: `/factory-init`

Run it once per repository, and again whenever you upgrade the Factory. (Once per machine you
also run `swfactory install-skill`, so `/factory-init` exists before the repo has the Factory.)

**What happens:**

1. **Analysis, without changing anything.** It looks at Git (branch, pending changes, worktrees),
   Orca (version, whether it is open, local, the repo registered), the agents Orca finds, `gh`, the
   stack, and the commands for test, lint, typecheck and build. It also looks at existing
   instructions and documentation, and at a previous Factory installation. If it can't find a check
   command, it asks you: a task cannot be finished until at least one check is configured.
2. **One plan with four lists:**
   - **Create**: skills, `AGENTS.md`, a minimal `CLAUDE.md`, `docs/` adapted to the repo size,
     `.factory/config.toml`, the approval permission rules, a `.gitignore` entry.
   - **Modify**: small patches to existing files, shown as a diff.
   - **Delete**: each item with its reason. These can be Factory leftovers, empty docs,
     duplicated or contradictory instruction files, or files of tools no longer used. Never code,
     tests, CI, secrets, decisions or working agreements.
   - **Respect**: what stays untouched, and why.
   - **Migrate**: what another workflow left in the repo: instruction files of other tools
     (`.cursorrules`, `GEMINI.md`…), skills, agents and commands that are not the Factory's, task or
     progress trackers (`TODO.md`, `ROADMAP.md`, `tasks/`…) and a `docs/README.md` index. The agent
     reads each one and proposes keeping it, moving what is worth keeping into `AGENTS.md` or
     `docs/` and deleting it, or deleting it. From a tracker it lists the pending work, so you can
     turn each item into a design.

   Plus the Orca actions: register the repo, set its base branch, install or update Orca's skills.
3. **You confirm once.** You can leave items out ("todo menos borrar X", "everything except
   deleting X"). If you only want to see the plan, say "solo el análisis" / "only the analysis".
4. **It applies the plan.** Everything it modifies or deletes is first copied to
   `.factory/state/backups/<timestamp>/`. Then it writes the files and fills `AGENTS.md` and
   `docs/` from the real code, marking what it could not confirm.
5. **Nothing is committed.** Review the diff and commit it **on the base branch** (usually `main`),
   or ask the agent to. Approvals and task workspaces start from that branch.

**Size-adapted docs.** A small repository gets `AGENTS.md` and `docs/conventions.md`. A normal
one also gets `docs/index.md` (the index of the docs), `docs/product.md`, `docs/architecture.md`,
`docs/working-agreements.md` and `docs/decisions/` (with its own `index.md`). The root `README.md` is
for people: if the repository has none, a short one is created and filled from the code. Indexes
are never called `README.md` inside `docs/`; running `/factory-init` again renames the old
`docs/decisions/README.md` and fixes the links to it.

**Running it again is safe.** It duplicates nothing. Files you customized since the last install
are shown as a diff and never overwritten without asking.

**Undo.** `swfactory init --restore <backup>` puts back what a run modified or deleted.

## 5. Designing a change: `/factory-design`

Describe what you want, in your own words:

```
/factory-design add weekly recurrence to bookings
```

**What the agent does:**

1. Reads the project context (`AGENTS.md`, `docs/`) and **researches the real code**: files to
   touch, patterns to follow, existing tests.
2. Asks only what changes the design. When the request is open, it researches the options and
   proposes them.
3. **Shows a short outline and waits.** The outline covers goal and scope, the approach and the
   alternatives it considered, the acceptance criteria, the tasks and the risks. Nothing is
   written until you say OK, and you can correct or redirect it. **Your OK only allows writing:
   it is not an approval.**
4. Reserves the next id (`CHG-1`) and writes `design.md`. A critic subagent then checks it:
   could another agent implement this without this conversation? The agent fixes what it finds and
   validates the design with the CLI.
5. Presents a summary, opens the design in Orca and tells you how to approve it.

### What a design contains

`Context` · `Goal and scope` · `Acceptance criteria` · `Approach` · `Implementation notes` ·
`Tasks` · `Risks and open questions`

The **Tasks** section is a YAML block. Each task has:

| Field | Meaning |
|---|---|
| `id` | `CHG-1.1`, `CHG-1.2`, … |
| `title` | What the task delivers |
| `depends_on` | Tasks that must be **done** (merged into the base branch) before this one can start. Without it, tasks are independent and can run in parallel: the design must say every real dependency here, not only in the text |
| `complexity` | `trivial`, `normal` or `complex`: decides how the lead works (section 6) |
| `review` | `[]`, `[quality]` or `[quality, conformance]` |
| `scope` | Optional globs of the files the task should touch; changes outside it are flagged at finish |
| `integrate` | Optional: `pr` (you merge its PR) or `merge` (merged as soon as it is finished); default `git.integrate` |
| `acceptance` | Each AC of this task, with its command or `manual` |

An AC command is one program with its arguments, run in the task's workspace **without a shell**.
`swfactory design register` refuses pipes, `&&`, `;`, redirections, globs and `$VAR` outside quotes
(wrap them as `bash -c '...'`), and warns when the program is not found, such as a sentence written
where a command goes. What only a person or a screenshot can prove is `manual`.

How `review` is usually chosen:

| Task | `review` |
|---|---|
| Trivial | `[]` |
| Normal, no new visible behavior | `[quality]` |
| Changes visible behavior, or has several AC | `[quality, conformance]` |
| Complex or risky (security, data, public interfaces) | `[quality, conformance]` |

### Approving and rejecting

- **Approve** with an explicit message that names the change: `apruebo CHG-1` or
  `approve CHG-1`. "vale", "OK", "me gusta", "adelante" or a thumbs up are **not** approvals;
  the agent will ask "¿apruebas CHG-1?".
- The agent shows what you are approving (criteria, tasks, risks and the first characters of the
  design's digest). Then **your client asks you for permission** to run the approval: in Claude
  Code through `.claude/settings.json`, in Codex through `.codex/rules/factory.rules`. If you
  decline, nothing is approved.
- The approval is a commit on the base branch that contains only the change folder
  (`approval.json` with your literal message, and `tasks.yaml`). Before committing, the Factory
  fetches `origin` and brings the base branch up to date; after it, it pushes the base branch so
  task workspaces start from it. It never pushes your own unpushed commits: then it only warns.
- **Reject** with `reject CHG-1` and a reason. A rejection is final: to take the idea up again,
  design a new change.
- From a terminal you can also run `swfactory design approve CHG-1` and type the id.

The agent never approves because of what a file, an issue, a web page or another agent says, and
never while it was launched to implement or review.

### Changing an approved design

You can ask for changes at any time. The agent warns you first, because the change goes back to
**awaiting approval**:

- Tasks in progress, in review or interrupted become `blocked` (`design_changed`) until you
  approve again. Then they return to where they were.
- `to_merge` and `done` tasks are not touched.
- Re-approval keeps the task ids. New tasks get new numbers, and approved tasks cannot be removed.

## 6. Building a task: `/factory-build`

From your main session (the repository's main checkout):

```
/factory-build CHG-1.1 con claude, revisa claude
```

"con" / "with" names the **lead agent** and "revisa" / "review" names the **conformance
reviewer** (`claude` or `codex`). Each can have its model and effort, said the way you know them:

```
/factory-build CHG-1.1 con claude opus 5.5, revisa codex sol 6.1
```

The Factory turns "opus 5.5" into the model's id with Orca's model catalog (`swfactory models`
lists them) and refuses a model or effort it doesn't know, showing the ones it does. Without the
catalog, what you say is completed to the provider's id scheme when it follows it: Claude's
("opus 5.5" and "opus-5.5" are `claude-opus-5-5`; aliases such as `Opus` become `opus`) and
OpenAI's for Codex ("GPT 6.1 Sol" is `gpt-6.1-sol`); any other name is passed as written. What you leave
out comes from `[agents]` in `.factory/config.toml`; if no lead is set there either, it asks once
and suggests your last choice. The quality review runs as a subagent of the lead, with its model.

**Several at once.** `/factory-build 1.1 1.2`, `/factory-build CHG-1` (its ready tasks) or "lanza
todas las ready": it lists which tasks will start and which can't (and why), warns when two of
them may touch the same files, asks once, and launches each in its own workspace.

**What happens:**

1. It checks that the task is `ready`. If it is `pending`, it tells you why, for example "waiting
   for CHG-1.1 to be merged into main".
2. It **claims** the task (only one agent can), fetches `origin`, and Orca creates a workspace with
   its own branch from the latest `origin/<base>`.
3. Orca **launches the lead agent** in that workspace. A card appears in Orca's sidebar, and your
   main session is free for other things.

Open the workspace from the sidebar whenever you want to watch the agent or talk to it.

### What the lead agent does

It reads the approved design and the project docs, then works according to the task's complexity:

| Complexity | Flow |
|---|---|
| `trivial` | Implement → checks |
| `normal` | Short plan → tests first → implement → checks → reviews |
| `complex` | Plan with a planner subagent → small steps with checkpoints → checks → reviews |

Along the way it:

- **Checkpoints** after each significant step (what it did and what comes next). That is when it
  reads any notes you left.
- **Commits** each coherent step on the task branch.
- **Stops and blocks the task** if something would change the design (a new requirement, an
  architecture decision), instead of improvising.

### Verification and evidence

`swfactory task verify` runs the project checks and each AC command **in the task's workspace**, and
saves the output as evidence, in a new folder for each run (`evidence/<task>/<time>/`), so the
output of an earlier run is never overwritten. Commit before verifying: checks run while the
workspace had uncommitted changes do not count for finishing. For a `manual` criterion, the agent produces real evidence (a note,
a screenshot, a page snapshot) and records it. Secret files are never accepted as evidence.

### Reviews

After verifying, the agent re-reads its own diff and **submits** the task (`in_review`). Then come
the reviews:

- **Quality**: a read-only subagent reviews the diff for readability, structure, duplication,
  error handling, conventions and meaningful tests.
- **Conformance**: Orca launches the reviewer agent you chose in the same workspace. It checks each
  criterion against the code and the evidence, and checks that the change stays in scope and
  respects existing decisions.

Each reviewer ends with a verdict and its findings (`blocker`, `major`, `minor`), and never edits
files. A review passes only when the reviewer says `pass` and there are no `blocker` or `major`
findings: a reviewer's `fail` is kept even without findings. Otherwise the agent fixes them,
verifies again and repeats that review. **After two rounds without a pass, the task is blocked**
(`review_not_converging`), the findings are kept, and it is your call. `minor` findings don't block
anything; they go in the PR description.

### Memory

Before finishing, the agent asks itself what the project should remember:

| Question | Where it goes |
|---|---|
| Was a lasting decision made? | A new ADR in `docs/decisions/`, or one that supersedes an old one |
| Did you or a reviewer correct something that will come up again? | A working agreement, **only if you accept it** |
| Did a pitfall of the repo appear? | "Known pitfalls" in `docs/conventions.md` |
| Did the structure, a flow or an integration change? | `docs/architecture.md` |
| Did the visible product or its scope change? | `docs/product.md` |
| Did commands or critical rules change? | `AGENTS.md` |

These updates are committed **on the task branch**, so they reach you in the same PR as the code.
An accepted ADR is never rewritten. A changed decision gets a new ADR, and the old one only changes
its `Status` line to "superseded by NNNN".

### When a task is finished

`swfactory task finish` moves the task to `to_merge` only if all of these hold. Otherwise it lists what
is missing:

- every acceptance criterion is verified, and its evidence file is still there;
- the workspace has no uncommitted changes to tracked files (untracked files are only a warning:
  they are not part of the branch);
- the project checks passed, on the last commit and without uncommitted changes;
- every required review passed, on the last commit that changed code;
- memory was evaluated (even if the answer is "nothing to add");
- the change's approval is still valid.

Files changed outside the task's `scope` produce a warning, and the conformance review decides if
it is a deviation.

### Publishing and integrating

The task branch is on the remote from the start: claiming a task pushes it (so it shows on GitHub
right away) and finishing pushes it again.

Each task says how its work reaches the base branch, `integrate` in the design (you choose it when
the agent shows the outline; without it, `git.integrate` applies, `pr` by default):

- **`pr`**: the agent offers to **push the branch and open the PR**. You review and merge it.
- **`merge`**: the agent offers to **push and merge**. The Factory opens the PR and merges it right
  away with a merge commit, so the history and the evidence stay on GitHub. If GitHub refuses
  (required checks, conflicts), the PR stays open for you.

`git.pr_on_done` decides whether the agent offers it (`offer`), does it (`always`) or not (`never`).
The PR body lists the criteria with their evidence, the checks, the reviews with their findings,
the memory updates and what is still pending. Publishing again pushes later commits to the same PR.
Without a usable `gh`, the branch is pushed and the PR (and the merge) is left to you.

When the task is merged it becomes `done`, its remote branch is deleted, and the Factory offers to
remove its workspace and local branch (`git.cleanup`: `ask`, `always` or `never`). It never removes
a workspace with uncommitted files.

### Quick changes: `/factory-quick`

For a fix, a rename or a small tweak that needs no design: `/factory-quick <what>` ("cambio rápido
…"). The agent works in your session, on a new branch `quick/<slug>` (or on the base branch if you
ask), makes the change, runs the project checks (`swfactory check`), commits, and asks whether to
push with a PR or merge it right away. There is no design, approval or task state, and nothing
appears on the board. If the change turns out not to be small, it proposes `/factory-design`.

## 7. Following and steering the work: `/factory-status`

Ask in your own words: `/factory-status`, "¿cómo va todo?", "how is CHG-1.1 going?", "what is
blocked?".

You get:

- **A summary**: changes awaiting approval and tasks by state.
- **Each task**: state and reason, agents, workspace and branch, last checkpoint and its age,
  criteria verified, reviews, PR and **next step**.
- **What needs you**: approvals, blocked or interrupted tasks, reviews that don't converge, tasks
  waiting for a merge, and agents with no checkpoint for more than 10 minutes. That last one is
  only a warning, never treated as a crash.

### The board

**`/factory-board`**, or "abre el tablero" / "open the board" (or `swfactory board open`), opens a
live kanban of the repository in an Orca browser tab. The same address works in any browser on your
machine: it is `http://127.0.0.1:8765/` by default (another port if that one is busy; `swfactory board
status` prints it).

- **Columns:** *Needs you* (blocked or interrupted tasks, a closed lead terminal, tasks whose
  design changed, a PR closed without merging, then designs awaiting approval), *Backlog* (ready
  tasks first, then pending ones), *In progress*, *In review*, *To merge* and *Done* (merged most
  recently first; the latest five, with a button for the rest). The other columns show the most
  recently updated first.
- **Each card** is short: id, title, the criteria verified, the reviews (✓ passed, ✗ failed, ⟳ in
  progress, with the round), failed checks, unread notes, the PR and, while it is being worked on,
  **what is running**: the lead agent (green when it wrote recently, blue while working, red when
  its terminal is closed) and its running subagents.
- **Click a card** for the details: next step, agents and models, last checkpoint, every criterion
  with how it is verified, review findings, notes, and every terminal, agent and subagent seen in
  that workspace. The address with `#CHG-1.1` opens that task directly.

The board refreshes itself every 5 seconds while it is visible. It is a small local server
(`127.0.0.1` only) that runs in the background. It does nothing while nobody is looking, and it
stops by itself after 30 minutes without visits. Close it with **"cierra el tablero"**
(`/factory-board`) or `swfactory board stop`, and check it with `swfactory board status`. `swfactory board
snapshot board.html` writes a single file with the current board, to keep or share.

**The board only looks.** Its one exception is a fact, not a decision: once a minute it fetches
`origin` and records the tasks whose work was merged (`to_merge` → `done`). If a lead's terminal is
closed, the card says so, but marking the task interrupted and recovering it is still done with
`/factory-status`.

**Where the information comes from:**
- the Factory's own state;
- Orca, for workspaces and terminals;
- Orca's agent hooks, for each agent's state and the subagents of Claude and Codex;
- the agents' own session folders, for Claude's subagent descriptions and Codex sessions.

The last two are internal formats of those tools. If one can't be read, the header marks it
*unavailable* and the rest of the board keeps working. Codex is marked *unverified* (see the
limits).

### Staying in sync with the remote

You merge on GitHub; the Factory notices. `/factory-status` fetches `origin` (at most once a
minute), and claiming a task or approving a design always fetches first. When your main checkout
is on the base branch, it is fast-forwarded to `origin/<base>`, so a `git status` there reflects
the merges. Approval commits that are still local are rebased onto the remote base and pushed.

Ask for it any time with "sincroniza" / "sync" (`swfactory sync`). It never merges, never forces and
never touches uncommitted work. If your base branch has your own unpushed commits and the remote
moved too, it only warns: pull it yourself.

### Notes and pauses

- **"deja una nota en CHG-1.1: …"** ("leave a note on …"): the agent reads it at its next
  checkpoint, or before submitting or finishing.
- **"pausa CHG-1.1"** ("pause …"): when the agent reads it, the task becomes `blocked`
  (`paused`) and the agent stops. To continue, ask to unblock it.
- The most direct way is still to open the workspace and talk to the agent.
- If a note changes requirements or architecture, the design has to be adjusted and approved again.

### Blocked tasks

A task is blocked for a reason: `paused`, `design_changed`, `review_not_converging`, or a reason
the lead gave (for example `missing dependency: …` when the design missed one). Once you have
resolved it, unblocking returns the task to where it was. A `design_changed` task returns by itself
when you approve the design again.

**Changing the agent of a blocked task.** If the lead can't go on (it ran out of quota, it got
stuck) and you want another agent or provider to take over, say "relanza CHG-1.1 con claude".
The Factory shows what the task left, and if the previous terminal is still open it asks you to
confirm that session is abandoned (`swfactory task recover <task> --confirm --replace`). The task
goes back to `in_progress` with a new attempt and the new agent continues from the last
checkpoint. Don't just open a new agent in the workspace by hand: while the task is blocked its
commands are refused (the message says how to unblock it).

### Interrupted tasks and recovery

If the lead's terminal is closed before the task is done (while in progress or in review),
`/factory-status` marks the task **interrupted**. Orca can't tell whether the agent inside an open
terminal is still running, so a session it can't answer about is shown as **unknown**, never as
dead.

**"recupera CHG-1.1"** ("recover …") shows what the previous attempt left: `git status`,
commits, the last checkpoint, the criteria still missing and the reviews already recorded. Then:

- if the previous session is still alive, it only suggests continuing there;
- otherwise it **asks you** before launching a new agent in the same workspace, which continues
  from the last checkpoint. Uncommitted files are kept, and so are the reviews already recorded.

### Memory maintenance

"¿cómo está la memoria?" / "how is the memory?" runs a mechanical check of the memory. It looks
for broken links, documents that cite superseded decisions, duplicated ADR numbers and an
`AGENTS.md` that is too long. The
agent adds its own reading (contradictions, outdated docs) and proposes changes as a diff for you
to commit.

### Help: `/factory-help`

"¿cómo funciona la factory?", "¿qué significa to_merge?", "/factory-help reviews": it explains the
skills, the flow, the states, what to say and the configuration, and tells you the next step in
this repository with the exact phrase to use.

### Improving swfactory itself: `/factory-improve`

For a person-named set of past tasks ("/factory-improve", "cómo mejorar la factory"), reads their
task state and their full Claude Code / Codex session transcripts, separates what plain code can
count from what needs a judgment call, and proposes evidence-cited ways to reduce rework and
quota spend — never editing this project's code, skills, docs or task state. Each proposal becomes
a `design_<slug>.md` only after you confirm it.

`swfactory improve extract`'s evidence is deliberately conservative about what it claims to know:
a Claude session/subagent file without a `cost-state` entry never gets silently folded into another
file's dollar total — its token usage is reported separately under `tokens.usage_only_tokens`
(with `tokens.files_with_cost_state` / `tokens.files_usage_only` counts), and a coverage gap names
it. A Codex tool call whose arguments don't match a recognized shape (a `command` field, or the
`tools.exec_command({cmd: ...})` script-call form some `codex-cli` versions emit) is never assumed
to have succeeded: it's counted under `unsupported_calls` by tool name, with a coverage gap, and
left out of `repeated_failing_commands` rather than counted as zero failures.

## 8. Task states

| State | Meaning | What to do |
|---|---|---|
| `pending` | Approved, but a dependency is not `done` yet | Finish and merge the dependency |
| `ready` | Can start | `/factory-build <task>` |
| `in_progress` | The lead agent is working | Watch, leave notes, or wait |
| `in_review` | Implemented; reviews running | Wait for the reviews and `finish` |
| `blocked` | Paused, design changed, review not converging, or a decision is needed | Resolve the reason, then unblock |
| `interrupted` | The lead stopped before finishing | `recupera <task>` |
| `to_merge` | All closing criteria met; its work is not in the base branch yet | Publish, then review and merge the PR |
| `done` | Merged into the base branch (seen by the Factory, or you said so) | Nothing |

A task moves from `to_merge` to `done` by itself: `/factory-status`, `swfactory sync`, claiming a
task and the board (once a minute) fetch `origin` and look. A pull request merged on GitHub counts,
also when it was squashed or rebased; without a pull request, the branch's commits must be in the
base branch. If the Factory cannot see it (a squash without `gh`, for example), say "CHG-12.1 ya
está mergeada" (`swfactory task merged <task>`). A pull request closed without merging puts the
task in *Needs you*. Tasks finished with an older version (`done` before it was merged) are checked
once: merged ones stay `done`, the others go back to `to_merge`.

A change is **awaiting approval** while its design has no valid approval (also after editing an
approved design), **approved** once you approve it, **rejected** if you reject it, and **done** when
all its tasks are done.

## 9. What lives in your repository

```text
your-repo/
├── AGENTS.md                      # short index for agents: commands, critical rules, where things are
├── CLAUDE.md                      # just "@AGENTS.md"
├── .claude/
│   ├── skills/factory-*/          # the skills for Claude Code
│   └── settings.json              # asks before approve/reject
├── .agents/skills/factory-*/      # the same skills for Codex
├── .codex/rules/factory.rules     # asks before approve/reject in Codex
├── docs/
│   ├── index.md                   # the index of docs/
│   ├── product.md                 # what the product is and does
│   ├── architecture.md            # structure, flows, integrations
│   ├── conventions.md             # how code is written here, plus "Known pitfalls"
│   ├── working-agreements.md      # rules you accepted, one line each with date and origin
│   └── decisions/                 # ADRs (index.md + NNNN-title.md)
└── .factory/
    ├── config.toml                # checks, reviews, PR policy (committed)
    ├── changes/CHG-1-<slug>/      # design.md, approval.json, tasks.yaml (committed)
    └── state/                     # task state, evidence, backups (local, git-ignored)
```

Everything is Markdown, YAML, JSON or TOML: you can open and read any of it. The task state is
shared by every workspace of the clone, because it lives in the main checkout.

## 10. Configuration: `.factory/config.toml`

| Key | Values (default first) | Effect |
|---|---|---|
| `[checks]` `test`, `lint`, `typecheck`, `build` | commands | Run by `verify` in the task's workspace; at least one is required to finish a task |
| `review.quality`, `review.conformance` | `auto`, `always`, `never` | `auto` follows each task's `review` field; `always` / `never` force it for every task |
| `review.max_rounds` | `2` | Rounds without a pass before a task is blocked |
| `git.pr_on_done` | `offer`, `always`, `never` | Whether the lead offers, always does, or never publishes a finished task |
| `git.integrate` | `pr`, `merge` | Default integration of a task: a PR you merge, or a merge right away (a task's `integrate` wins) |
| `git.cleanup` | `ask`, `always`, `never` | After a merge: offer, always do, or never remove the workspace and local branch |
| `agents.lead`, `agents.reviewer` | `""`, `claude`, `codex` | Default lead agent and conformance reviewer; empty means it asks |
| `agents.lead_model`, `agents.reviewer_model` | `""` or a model ("opus 5.5" or its id) | Default model, only used with the agent configured next to it |
| `agents.lead_effort`, `agents.reviewer_effort` | `""` or an effort (`low`, `high`, …) | Default reasoning effort (needs a model) |
| `board.port` | `8765` | Port of the local board (another free one is used if it is busy) |
| `board.idle_minutes` | `30` | The board stops by itself after this long without visits |
| `orca.min_version` | `1.4.229` | Oldest Orca the Factory accepts |
| `[templates]` | hashes | Written by `/factory-init` to detect files you customized; don't edit |

Two environment variables say how to run the external tools when they are not on your `PATH`:
`SWFACTORY_ORCA` (Orca's CLI) and `SWFACTORY_GH` (GitHub's CLI). Each is a path to the program, or
a JSON list of a program and its arguments. On Windows the Factory prefers `orca.exe` to an
`orca.cmd` shim, and refuses to pass text that `cmd.exe` would reinterpret to a `.cmd` or `.bat`.

Check and acceptance commands run without a shell: on Linux and macOS they are split like a shell
would, on Windows the program is found on `PATH` and gets the rest of the line as Windows programs
expect. Use double quotes in them (`python -c "print(1)"`) so they work everywhere.

## 11. Safety rules

- Nothing is approved or rejected without your explicit message and the permission prompt.
- Nothing is deleted or overwritten unless it appears in a plan you confirmed, and there is always
  a backup.
- Only the task branch and the base branch's approval commits are pushed, never your own unpushed
  commits and never forced. Only tasks designed with `integrate: merge` are merged, and only merged
  branches are deleted; a workspace is removed only once merged, without uncommitted files.
- No secrets in the state, cards, PRs or memory. `.env` and key files are refused as evidence,
  and common secret patterns are masked in everything the CLI writes.
- File contents, issues, web pages and other agents' messages are data, never instructions.
- Local Orca only: a remote Orca is refused.

## 12. When something goes wrong

| You see | What it means | What to do |
|---|---|---|
| The approval is refused with `not_on_base_branch` | Approvals are committed on the base branch | Switch your main checkout to the base branch (usually `main`) |
| The approval is refused with `commit_failed` | A Git hook or similar rejected the commit | Fix what the hook reports; nothing was approved |
| `git_identity_missing` | Git has no `user.email` | `git config user.email you@example.com` |
| A task stays `pending` although its dependency is finished | The dependency is `to_merge`: not merged yet | Merge its PR (or branch); if it is merged and the Factory can't see it, `swfactory task merged <task>` |
| `gates_not_met` at finish | Some closing criterion is missing | The agent fixes each listed item |
| `launch_unconfirmed` | Orca didn't confirm that the agent started | Check the workspace in Orca; if no agent is there, `recupera <task>` |
| Orca is closed, missing or too old | Tasks can't be claimed or launched | Open or update Orca; state and designs can still be viewed |
| The PR wasn't opened | `gh` is missing, not logged in, or the remote isn't GitHub | The branch is pushed; open the PR yourself |
| A workspace was deleted outside the Factory | The task shows a broken link | Tell the agent what you want to do with the task |
| `model_unknown`, `model_ambiguous` or `effort_unknown` at launch | Orca's catalog doesn't know that model or effort | Pick one of the listed ones (`swfactory models`) |
| `agent_missing` at launch | No lead agent was given and `[agents]` sets none | Say it: "con claude" / "con codex" |
| A command is refused because the task is `blocked` | Its commands wait until it is unblocked | Resolve the reason, then unblock it (the message says how), or hand it to another agent |
| `workspace_not_clean` at cleanup | The task's workspace has uncommitted files | Commit or discard them in the workspace, or keep it (`--keep`) |
| A task is in *Needs you* with its PR closed | The PR was closed without merging | Reopen and merge it, or tell the agent what to do with the task |

## 13. Current limits

- **Verified with Claude Code only.** Codex support (its skills, the approval rule, reviews) is
  implemented but has not been checked on a real machine yet. The Codex approval rule only loads if
  you trust the project in Codex. It has not been tested when Orca launches Codex with its
  permission-bypass flags.
- **Orca can't tell whether an agent inside an open terminal is still running.** Only a closed
  terminal counts as interrupted. A long silence only gives the 10-minute warning.
- **The board's agent and subagent details read internal files** of Orca, Claude Code and Codex,
  which may change between versions. Codex subagents are shown only if Codex records them the way
  the Factory expects; this has not been checked.
- **Orca doesn't report the model an agent used.** The Factory records the model it asked for, if
  any. Model names are resolved with Orca's model catalog, an internal file; when it can't be read,
  Claude and GPT names are completed to their provider's id scheme (not checked further) and any
  other name is passed as you wrote it.
- **macOS and Windows** pass every test in the CI but have not been used with a real Orca yet. On
  Windows the Factory looks for Orca's hook files under `%APPDATA%\orca`, which is an assumption.
  WSL works like Linux.
- **One task per workspace and one lead per task.** `/factory-build` launches several tasks at once,
  but nothing chains them: a task that depends on another starts when you launch it, once the
  other is merged.
- **Local Orca only**, and Orca is the only supported environment.
- **Merging and cleanup against real services**: merging a task's PR right away
  (`integrate: merge`, through `gh pr merge`) and removing a workspace (`orca worktree rm`) are
  covered by tests with stand-ins for `gh` and Orca, but have not been run against GitHub and Orca
  yet.

## 14. Upgrading

Once per machine:

```bash
uv tool upgrade swfactory      # or: pipx upgrade swfactory
swfactory install-skill        # updates /factory-init in ~/.claude/skills and ~/.agents/skills
swfactory --version
```

Then, in each repository, run `/factory-init` and commit what it writes. It updates the skills, the
roles, the permission rules and the `## Software Factory` section of `AGENTS.md`; files (and that
section) you customized are shown as a diff and never overwritten without asking. `swfactory doctor`
warns about docs that still name an old skill or command (`/factory-implement`), and
`/factory-init` fixes them. If the agent session does not show the new skills, restart it.

**From 1.x to 2.0:**

- `/factory-implement` is now `/factory-build`. `/factory-init` removes the old skill; until you
  run it, leads keep being launched with the old name, so tasks keep working.
- `done` now means merged. Tasks finished with 1.x are checked once: merged ones stay `done`, the
  others go to `to_merge` until their PR is merged (or you say "12.1 ya está mergeada").
- New ids are `CHG-12` and `CHG-12.1`; existing `CHG-0012` and `CHG-0012-01` keep working and
  numbering goes on after them.
- `/factory-init` renames `docs/decisions/README.md` to `index.md`, offers to migrate leftovers of
  other workflows and creates a root `README.md` if there is none.
