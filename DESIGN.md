# Design

**Status:** draft, nothing built. This is the design of the *tool*. Anything specific to one
person's setup - which repos, which models, which agents, the dispatch rules - lives in that
person's workspace, never here.

## The layers

Three layers, kept separate. Firstmate's docs taught us this and it is the main structural
idea here:

| Layer | Owns | Provided by |
|---|---|---|
| Panes | terminals, tabs, layout | the multiplexer you already run |
| Isolation | one git worktree per task | this tool |
| Dispatch and state | who is doing what, what came back, what is unanswered | **this tool - the point of it** |

## The problem it solves

Not the agents. You. With several agents running, you become the integration layer: you carry
who is on what, which question is unanswered, which pane you last spoke to. Answers arrive with
nothing to say which question they answer.

That is bookkeeping, and bookkeeping should not live in a person's head or in a conversation.

## What it is not

- **Not an agent runtime.** The agents are Pi sessions. You keep Pi's tools, skills, context
  files and model switching.
- **Not a terminal.** It drives the multiplexer you already use, addressing panes by name.
- **Not an agent framework.** LangGraph, CrewAI and AutoGen orchestrate LLM calls inside one
  process, so an "agent" becomes an LLM loop you wrote. Those replace the agent; this wraps it.

## Decisions

### 1. The front door is an agent, not a program

A Pi agent in its own pane, because that is what you already talk to, what the multiplexer
already displays, and what you can click into to change its model.

### 2. The front door is long-lived and is never reset by hand

Firstmate's author: *"Do not manage context manually... Don't waste mental bandwidth
constantly compacting or resetting sessions manually."* He runs one front-door session across
36 projects and lets auto-compaction handle it.

So the front door is the memory, deliberately. **Workers** are the ones started fresh per
task, each in its own pane.

### 3. Workers are persistent panes, not spawned processes

You keep the ability to click into any worker and change its model or reset its context by
hand. Consequence: the dispatcher does **not** manage models or context.

### 4. One git worktree per task, passed as the working directory

Parallel changes in one repo must not collide.

**The trap:** the tool under test must run the *worktree's* code, not the main checkout. A
worktree run that silently imports the main checkout's source is the failure mode to design
against, and it has bitten a real project.

### 5. Transport is the multiplexer's own names

`<mux> agent prompt <name> "<brief>"`. Names are stable across reloads; internal session
addresses are not. The live agent list gives name, pane and status, so **there is no roster to
maintain**. It also hands back each agent's Pi session file, so a report reads the right
session without guessing.

### 6. Agent and model choice is a natural-language rules file

Not a lookup table, and not a small classifier. Firstmate's `crew-dispatch.json` is prose the
front door reads at dispatch time: *"for simple bug fixes where the root cause is defined, I
use ..."*. You state preferences; the front door writes the file.

### 7. When several models match, pick by remaining quota

So nothing goes to waste, and so the mapping can be measured rather than guessed.

### 8. Pending decisions are a generated HTML board

Not a sidebar panel. Built in step 4: `clowder board` writes one file, so it survives a fresh
context and needs no server. It opens with what is waiting on the human - a decision, a finished
review, or work on no branch - then open steps, agents and their spaces, answers, and jobs. It
works with the multiplexer unreadable, because the state file is enough to be useful, and every
word that came from a worker is escaped.

### 9. Hide tool-call noise in the front-door pane

Firstmate's `/calm` does this, toggled by a slash command. Most of the time tool calls are
noise. Small, and it is a harness extension, which is a supported surface.

### 10. The spine is code; the agent is only the front

The front door reads the request. Everything else - task ids, state, dispatch, collecting
reports - is a small CLI it calls through its shell, plus a skill telling it when to call it.

Rules in prose get forgotten. Rules in a tool cannot be.

### 11. The repo is the tool; the workspace is the config

No absolute paths, no repo names, no keys, no personal agent names. Without this the tool is
neither shareable nor safe to publish.

### 12. The front door's name is user config

The tool ships a front door; what you call it is yours. (Naming it after someone else's
product is worth avoiding for the same reason the multiplexer's name is kept out of the
project name.)

### 13. The CLI is Python, pinned to 3.14

Decided before step 1, as required. Stdlib only - `tomllib`, `subprocess`, `json`, `argparse` -
so there is no build step, no lockfile and no dependency to audit. Python 3.13 is what bare
`python` resolves to on this machine, so the tool names its interpreter: `py -3.14`, or the
`3.14` in `.python-version`.

The alternatives were weighed, not skipped. Rust, Go and Zig each buy a single binary and
speed, and this work is glue: spawn one process, read JSONL, write one file. None is
CPU-bound, and each costs a new toolchain. Zig is pre-1.0 with a thin JSON story. Bun was the
only credible rival - TypeScript, near-instant startup, `bun build --compile` - and it stays
available: if `/calm` ever wants code shared with a Pi extension, the CLI is small enough to
move.

### 14. A brief carries a marker

Decided. A plain prompt is not enough. A worker's pane receives text, and without a marker it
cannot tell a peer job from its owner typing - and those two need different answers. The tool
adds the line at dispatch time, so the front door cannot forget it:

```
[clowder job t-0004 | ship | myrepo | from topcat]
```

Four things in one line: that this is a job, which job, which repo, and who sent it. The repo
is there on purpose - it is what a worker checks against `git rev-parse --show-toplevel`
before it edits, when its pane happens to be sitting in a different repo. `--from` names the
sender; `dispatch.marker = false` turns the line off.

### 15. The front door creates the agent, and never closes a pane

Decided. When a job arrives for a repo with no agent, the front door makes one:
`pane split --cwd <repo>`, then `agent start <name> --kind pi`. A pane is given its directory
when it is made and cannot be moved later, so making the pane is the only way to get a correct
working directory. It is also why the worktree and the pane are made together.

This reverses the note that agents only talk to panes the human started. That note described
what was impossible before `agent start` existed, not what was undesirable. The fear behind it -
a roster living in someone's head, a brief sent to a pane that is not there - is answered by
reading the live list every time, and by confirming the new name before the brief goes out.

Panes are never closed, by the front door or by anyone: a worker that has reported can still
answer a follow-up, and its context can be reset if a clean one is wanted.

Four rules keep it bounded:

- Create only when no live agent's directory is inside that repo. Reuse that one.
- One name per repo and role, `<repo>-<role>`, sanitised to the multiplexer's charset.
- Never make a second agent under a name that is already live.
- Never steal focus, and confirm the name in the live list before dispatching.

### 16. A space per agent, a branch per job

First written as one worktree per task, then per agent on a branch of its own. Both were wrong.
Firstmate, after years of this, keeps a pool of reusable worktrees with the install and the build
cache kept, lends one to a task, and takes it back. Their worktree tool exists for the exact
reason we hit: agents "losing all your installed dependencies and build cache each time".

So: one space per agent, at `<repo>/.worktrees/<agent>`. A space is either

- **free** - clean, with no branch name on it, sitting on the current base, or
- **in use** - on one job's branch.

Finishing a job takes the branch off and leaves the space free. The install and the caches stay
in the folder, so they are paid for once instead of once per job.

Two things fall out of "free means detached":

- Nothing has to be handed back, and no branch is held hostage. Two spaces can sit on the base at
  once, which the per-agent-branch model could not do.
- Taking a space gives a fresh base, because a free space is re-pointed at the base when work
  starts. A job cannot accidentally begin from a stale checkout.

What a switch does **not** do is clean up. Untracked files survive it, which is the point (they
are the install) and also the risk (they can be yesterday's artifacts). Four rules keep it shut:

- A job starts from a clean space. Dirt is refused, with the file names.
- One open job per space.
- One open step per job, so two heavy runs cannot start at once.
- A branch is kept when a job closes, and deleted only when git agrees it is merged. The branch
  is the record of the work; the folder is not.

The gate: a commit that is on no branch exists only in one folder's HEAD, and reusing that folder
destroys it silently. `report` names that case for what it is.

### 17. A job is a chain of ordered steps, and only one step is open at a time

Decided by the shape of the work. The real request is rarely one change. It is: research the
update, work out how to implement it, write a spec, implement, run the unit tests, run the eval
for proof, commit, rerun graphify, update the docs. Every one of those speaks about the same
code, so they are one job: one repo, one branch, one checkout, many steps.

The steps are sequential and dependent, so the tool holds the order. A step cannot be dispatched
while another step in the same job has no answer. That rule is not tidiness: it is what stops a
unit test run and an eval starting at the same time in one checkout, which is how this box
crashed on 2026-09-25.

`dispatch --job <id>` records the job, the branch and the commit on the step, so a report can
name the code it is about. The commit is read when the report is read, not remembered from
dispatch, so a commit made during the step is the one reported.

What happens after the commit - the walkthrough and everything that follows it - is the owner's
gate chain, kept in the workspace rules file at `code/AGENTS.md`.

### 18. The handoff is a save, and a reviewer gets a pinned copy

Decided. Nothing is pushed between agents. A hand over is a **commit**, on the writer's branch,
on the same disk. The verifier's own copy is then filled with the writer's files at that one
save, with no branch name on it. So the writer keeps its branch, the reviewer holds the exact
code it checked, and one branch never sits in two folders.

Why the reviewer gets the commit and not the branch: a test result only means something if it
names the exact code it ran on. If the reviewer followed a branch the writer was still editing,
the tests could pass on code that no longer exists, and nothing would say so. Pinning the copy
is what makes the proof reproducible, and naming the commit in the report is what makes it
checkable later.

Two consequences worth keeping:

- A handover needs a save. Unsaved work is refused, by name, because a reviewer checks a save
  and not a folder.
- A review needs a save to exist at all, and a save is a commit. The gate itself is the owner's
  chain, in the workspace rules file.

Pushing is publishing, not handing over.

### 19. What Firstmate settled, and what we left

Read from their architecture doc, not guessed:

- **A pooled, reusable space rather than a fresh checkout per task.** Decision 16.
- **Worker spaces sit at detached HEAD, not on a branch.** Their rule: the operating checkout is
  healthy on its default branch, and worker checkouts are healthy detached. That one idea
  removes the branch-can-only-be-held-once problem this design spent three turns working around.
- **A delivery mode per task.** They name three - `no-mistakes`, `direct-PR`, `local-only`. We
  take `local-only` and refuse the other two by name until they exist, so a config cannot promise
  what the code does not do.
- **The reachability gate**, at the point where a worker claims it is finished.
- **A slow check on a button.** Their eval harness runs only when asked, which is what a
  verifier's step should press.

Left on purpose:

- **Their merge automation.** Their doc carries locks, ownership proofs, teardown proofs and
  TOCTOU reasoning, because their tool merges to main. That is the price of the tool merging.
  Ours stops at "the branch is ready"; what follows is the owner's gate chain, in the workspace
  rules file.
- **Prose and shell scripts as the spine.** They are an agent distro: instructions, skills and
  helper scripts. We chose a tested CLI plus a skill, so the rules that can be checked in code
  are checked in code.

### 20. The front door writes for the owner, not the crew

Decided after the owner asked three times what one report meant. The tool needs handles: job,
task and queue ids name records, and a dispatch carries them on the command line. The owner
does not need them. So the front door translates, and three rules keep the translation honest:

1. **Names, not handles.** Name the thing in his words first ("the B-097 branch", "the case
   study page", "the waiting list"), and put a handle in brackets after it, only where he has
   to type it. Never open a sentence with a handle, and never ask him to choose between two.
   This is a language rule, not a removal: the ids stay in the tool and in every brief.
2. **A question must stand on its own.** A decision put to him says what it is, what changes
   for him, and what it costs, without a lookup.
3. **Say where it happens.** Name the place when the action is his - this chat, a pull-request
   page, a pane, a file. Do not put a crew-only step in front of him, and do not count one
   under "waiting on you".

The board and the report shape already separate what waits on him. This rule covers the prose
around them, which no test can reach, so it lives in the skill and is guarded there only by the
examples it must carry.

### 21. The owner's pass and merge word are recorded, and the gates read them

Written rules did not hold: on 2026-09-28 the front door took the owner out of the loop within
an hour of being told he could not read code. So the gate is in the tool.

A job carries two recorded words from the owner, separate from each other:

- A **pass**: what he was shown, what he answered, who gave it, and when. It lets the branch be
  published.
- A **merge word**: his words, who gave it, and when. It lets the pull request be merged.

`clowder job publish` refuses without a pass. `clowder job merge` refuses without a merge word.
`clowder job close --delete-branch` refuses without a merge word, on top of git's own merged
check. Each refusal names the record command. No flag skips a gate in silence.

Who gives the words is the owner, and the record names him. A worker role cannot record one: the
record commands refuse a `--by` that names a worker. The tool cannot prove who typed a command,
so the record is the proof, and it carries his words and the time.

### 22. A finished step reports itself, and the inbox is the fallback

Seen on 2026-09-29: three steps had finished and their agents were idle, and the front door did
not know. The first fix was `clowder inbox`, a command the front door ran. That is cheaper
polling, not notification, and the owner said so: polling should not be the front door's job.

The bus does work. The same day, a worker's report arrived over the agent bus with an id, a
headline and a report path, and the front door had not asked for it. The first attempt concluded
the opposite, from reports that opened "no bus session is active". That was a briefs problem, not
a channel problem: no brief told the worker to send.

So the tool tells it. Every dispatched brief carries the report instruction, next to the marker:
send the report to the sender named in the marker, resolved by name in the worker's own peer
list, with the job id, a headline, and where the full answer is. No address is baked in -
addresses are per-observer and move when panes reload. The answer still stays in the worker's
session.

`clowder inbox` is the fallback, not the first check. When a send fails, the step stays in the
inbox until `report` reads it, so the bus message is never the only record. The board is not a
channel: a page somebody has to reload is how this was missed.

### 23. The board says only what is true, and says how old it is

Seen on 2026-09-29: the owner's section showed two decisions that had already been answered and a
job that was already merged, and he asked "is this right?". Three fixes:

- A decision can be answered. `report <id> --decide TEXT` records his answer and a time; an
  answered decision leaves his section, and the record keeps what he said. A pass is not a
  decision: a pass has its own field (`job pass`), and `--open-decision` is only an open question.
- The merge line matches the tool: the front door merges on his recorded word once the checks are
  green. It is his word that is missing, so the item says so, and it disappears once he gives it.
- The page prints when it was generated, in the body, and says that a tab keeps what it loaded.
  It reloads itself, but only a command rewrites the file, so an old time is the staleness.

### 23. State saves serialize, and a lost update is refused

Seen on 2026-09-28: two `clowder report` runs at once left the state file as valid JSON followed
by a fragment, and every command failed until it was repaired by hand. `StateStore.save` wrote a
fixed temp path `<state>.tmp`, so two writers shared one temp file, and either could replace the
state with the other's half-written file.

Now:

- The temp file carries the pid, the thread and a random token, so no two writers share a path.
- A lock file beside the state serializes a load-modify-save across processes; the OS releases
  it on exit.
- The payload carries a `rev`. A save that finds a newer `rev` folds the other writer's records
  in. A record changed by both since load is refused loudly, never overwritten. A lost update is
  never silent.

### 24. A released space, and the change held in a ref

Seen 2026-09-29: a finished change held its worker's space until the owner walked it, and a space
takes one open job at a time. The key change held the tancat-maker space for hours. The owner
feels this as work stopping while it waits for him.

The space is a build cache, not the record. The record is the branch, the held ref and the job.

- One **checked-out** job per space, not one open job. `released_at` marks a job that has handed
  its space back.
- `job handover` detaches the writer's space to the base, writes the reviewed commit to
  `refs/clowder/held/<job>`, and keeps the job OPEN. The branch may be kept or deleted; the ref
  keeps the commit reachable, and `is_reachable` counts that ref so the board does not call held
  work stranded.
- `job open` gates on a job whose space is still checked out. `job close` still releases the
  space, and skips the release when handover already did it.
- `job publish` and `job merge` never tested `is_open`, so they still work from a released job.
  Publish reads the commit from the branch or the held ref, not the worktree HEAD, which now sits
  on the base.

### 25. One reviewer space per repo, and the held queue on the board

The second half of the review-flow design. A finished change should not sit in the writer's space
while the owner gets to it, and the owner should have one review place per repo.

- One reviewer per repo with active work. It holds that repo's held changes, one at a time, and
  does both the check and the walkthrough - one role, not two. `job handover` makes the reviewer
  on first use, so a repo with none is never stuck.
- The board's owner section lists the held changes waiting for him, oldest first, with each one's
  age, and a "gone quiet" flag past a day. A queue with an age, not a silent pile.
- The reviewer discusses only the change in front of the owner and what he asks about it. It does
  not take new work, dispatch, route, or answer research, and it never records the pass or the
  merge word - the record commands refuse a worker's name. That keeps the one front door.

This depends on the first half, which releases the writer's space and holds the change in a ref;
until that lands, the commit stays reachable through the writer's branch.

### 26. The owner's section shows what a command recorded, and says so

Seen 2026-09-29: the owner opened the board, read "Nothing is waiting on you.", and was owed two
things. Neither lived in a job record: a report the front door was holding, and a question it had
asked in chat. The section could not see them, so it lied.

- A task carries one recorded owner item: `clowder owner <id> --item TEXT` writes one line in his
  words (names first, and where he does it); `--clear` removes it when he answers. It is the
  catch-all for anything that waits on him and no other field carries.
- The merge-word line keys off the recorded pass (`pass_at`), not the reviewer field.
- The empty line is honest: "Nothing is recorded as waiting on you." It says the tool shows only
  what a command recorded, so a report held in the front door's words will not appear.

### 27. An unknown field is skipped and kept, never fatal

Seen 2026-09-29: a working copy wrote two fields it had invented (`owner_item`,
`owner_item_at`) into the shared state, then that work was amended away. No surviving copy knew the
fields, and `Task.from_dict` refused a record with an unknown field, so every clowder command on
the machine stopped until the file was repaired by hand.

The tool ships its own state path, so one stale checkout must not freeze the crew.

- A reader skips a field it does not know, says so in one line naming the field, and keeps the
  value. It never drops it on save: a newer copy may still need it.
- A record keeps the extras in an `extra` map, and `to_dict` writes them back beside the known
  fields. This applies to tasks, jobs and queued items.
- The top-level schema check stays: a file whose whole shape is unknown is a different case and is
  still refused.

### 27. The viewer loads in every pane, and the front-door skill does not

Seen 2026-09-29: the owner was told to run `/diff main...HEAD` in a product-repo pane and the
command was absent. The viewer ships in the clowder package, so it loads wherever that package
loads - but loading the whole package everywhere would also put the `front-door` skill in panes
that must not act as a front door.

The install is a filtered package entry, not a copy of the file:

- Personal (`~/.pi/agent/settings.json`): the clowder package with
  `extensions: ["extensions/*.ts"]` and `skills: ["!skills/front-door"]`. Every pane gets `/diff`
  and the `diff-review` skill.
- Project (`clowder/.pi/settings.json`): the same package with `autoload: false` and
  `skills: ["+skills/front-door"]`. The front-door skill returns in clowder panes only.

A pane that still lacks `/diff` runs `/reload`, or `pi install ./clowder` from the checkout. The
fallback is unchanged and must be offered: walk the change as before-and-after text, file by file.

## What step 1 built

`src`-less, flat `clowder/` package. No dependencies, so `py -3.14 -m clowder ...` works from
a checkout with nothing installed.

| File | Holds |
|---|---|
| `cli.py` | the arg surface: `dispatch`, `tasks`, `report`, `agents`, `config` |
| `state.py` | the one state file, written atomically |
| `mux.py` | the multiplexer adapter, by agent name |
| `sessions.py` | usage, cost and the answer, read from Pi session files |
| `report.py` | the report block |
| `config.py` | workspace config; no personal path ships |

Two deviations from the sketch in Build order, both deliberate:

- `dispatch <agent> <repo> <brief>` with `--worktree PATH`, not `dispatch <agent> <repo>
  [worktree] <brief>`. A third positional cannot be told from the first word of a brief.
- `--shape ship|scout` is a flag. The tool warns when the brief does not say the shape, and
  does not fail: the brief is the front door's job, the flag is the record's.

Two facts found while building, which change what was assumed:

- `herdr agent list` returns each agent's Pi session file path. So decision 5 is stronger than
  written: the live list is the roster *and* the pointer to the right session. No guessing by
  mtime, no slug arithmetic.
- `herdr` already has `worktree create|open|remove`. Whatever decision 2 settles, the front
  door will not be writing git plumbing.

Not built yet at this point: the front door skill (step 2), the worktree helper (step 3), the
board (step 4), `/calm` (step 5).

## What step 2 built

`skills/front-door/SKILL.md`, shipped as a Pi package by `package.json`. The skill holds the
part of this design that code cannot: when to dispatch, the five parts of a brief, the report
shape, the walkthrough procedure, and the rules that must hold every time - say when you
dispatch, ask for exactly one decision, never overlap heavy runs. The gate chain is not repeated
here; the skill carries one pointer at the workspace rules file. It opens with a reading order for
a fresh context - inbox, queue, open steps, jobs, agents, the Open section - so a refresh is cheap
and the front door holds no state in its head.

Two tests keep the prose honest. One fails when a CLI command is added and the skill is not
updated. Another fails when the marker in the skill stops matching the marker in the code.
Prose that drifts is worse than no prose.

Install it with `pi install ./clowder` from this repo, or copy `skills/front-door/` into
`~/.pi/agent/skills/`.

## What step 3 built

The first half of step 3: `ensure`, and the guard that refuses a cross-repo dispatch.

`clowder ensure <repo> --role <role>` finds the agent that serves a repo, or makes one there.
The matching is deliberately conservative: the name this tool would have made, then a name that
is the role or ends in it (a hand-made crew is usually named `maker`, not `tancat-maker`), then
the only agent in the repo. Anything less certain stops and asks. Guessing which of four agents
should get a job is the bookkeeping this tool exists to remove, not something it should do
quietly.

The refusal is the important part. `dispatch` now refuses a brief for a pane whose directory is
not inside the target repo. It costs nothing, and it turns a silently wrong answer into a message
that names the fix:

```
clowder: maker is in C:\Users\me\code\repo-a, which is not C:\Users\me\code\repo-b.
A pane serves the repo it was opened in. Run `clowder ensure repo-b --role maker` to make an
agent there, or pass --force if you know better.
```

Exit code 3 means a human has to decide, so the front door branches on a number instead of
reading prose.

The second half of step 3 is the worktree and branch machinery from decision 16: `ensure` now
makes a new agent's checkout, and `job open` / `job close` / `job list` switch branches inside
it. Git is called directly (`gitcmd.py`) and never commits, pushes or merges.

Step 3 is finished: a step records its job, its branch and its commit; only one step is open per
job; a reviewer can be given the writer's save; a space goes free again when a job closes; and a
report says when a commit is on no branch at all.

## What step 4 built

`clowder board` writes one HTML file next to the state file, and prints the path. `--open` opens
it. It is a file, not a panel: it survives a fresh context, it holds far more than a terminal,
and it needs no server and no network.

The page opens with **waiting on you**, because that is the only part with a deadline:

- a decision a worker left open,
- a review that is finished and ready to merge,
- work that is on no branch, so it would be lost when the space is reused.

A second section, **waiting for a worker**, holds the front door's own list: queued items, and
steps that have gone quiet while their agent is idle, or whose agent is not in the live list at
all. Then open steps, agents and their spaces, recent answers with usage, and jobs.

The page is a pure function of the state, the live agent list and a few git reads, so it is tested
without a browser. The agent list is best effort: when the multiplexer cannot be read the page says
so and shows the state anyway, which is what makes it useful during a restart. Text that came from
a worker is escaped, because a report is data and not markup.

Step 5 remains: `/calm`, if the front-door pane turns out to be noisy.

## Layout

```
spaces                 agents
  <your repo>            maker        <pane>
    tabs per agent       verifier     <pane>
  <your repo>            teacher      <pane>
  ...                    researcher   <pane>
  <this tool>            <front door> its own pane
```

Unchanged except one more tab. The front door appears under *agents* because it is an agent.

## A report

```
Re: <the question it answers>
    verifier | <repo> | worktree <branch> | 14s | 8.2k | $0.001
    <the answer>
    Open decision: <exactly one>
```

The question comes first, so a late answer still knows what it answers. Usage, cost, provider
and model come from the agent's own session file, which records them per turn - read, not
scraped.

## Build order

1. The CLI: `dispatch <agent> <repo> [worktree] <brief>`, `tasks`, `report <id>`. State in one
   file. Drives the multiplexer and reads session files.
2. A skill so the front door knows when to call it, plus the report format above.
3. A worktree helper: create one per task, hand its path to the CLI.
4. The HTML board for pending decisions.
5. `/calm`, if the front-door pane turns out to be noisy.

## Open

- Whether reports come from a file the worker writes, or from its final message read out of
  its session file. The file is more robust; the session file needs no discipline.
- What happens when a job's branch and the main checkout have both moved, and the merge at the
  end does not apply cleanly. Nothing merges yet, so nothing decides this yet.
- Whether the tool should read CI status into a report, and fire the manual eval workflow. Your
  repos have `gh`, a `ci.yml` that runs on pull requests into main, and an `eval-harness.yml`
  that only runs when a button is pressed. Reading CI would put a robot's verdict next to the
  verifier's, and firing the eval would make the verifier's slow check one command.
- How a repo whose development needs accumulated local state works with a space per agent.
  `AI-Playwright-Test-Generator` keeps 7.8 GB of test-run output and a 1.8 GB install that git
  ignores; four spaces keep four of those. The baseline a run is compared against is committed
  (`fixtures/golden_package/evidence/`), which is what makes a new space usable at all. Untested:
  whether re-running is cheap enough there, or whether that repo wants fewer spaces, more
  sharing, or a cleanup step between jobs.

Eight defects found while using the tool on 2026-09-28. Recorded here so they outlive the
conversation; none is fixed by the queue work.

- **A task with no place.** A task sent without `--job` or `--worktree` records no working
  directory, so a report names the wrong place and skips the "commit on no branch" check.
  Seen: t-0007 says "main checkout" while its worker was in `.worktrees/clowder-maker`. Fix:
  record the agent's live directory at dispatch, or require a place.
- **One answer per agent, not per task.** `report` reads the last thing an agent said, so a
  later task by the same agent overwrites an earlier task's answer. Seen: t-0006 showed a
  publish task's text as its own. Fix: scope the answer to the task's dispatch window.
- **A send preempts an open job.** A task sent to an agent that already has an open job takes
  the newest message and stops the older work. Seen: it happened today, and j-0004 had to be
  re-sent. Fix: refuse or queue a dispatch to an agent whose space holds an open job, unless
  it is that job.
- **Last-write-wins state.** (Fixed - decision 23.) The state file had no lock, and concurrent
  commands are normal. Seen: t-0005 was recorded reported, then read back dispatched; a second
  read fixed it. Later two `report` runs at once left the file as JSON plus a fragment. Now a
  lock serializes saves, a unique temp per writer stops the torn file, and a stale save is
  refused rather than written.
- **A stale base.** A job's base comes from the local branch, which is behind after a merge
  elsewhere. Seen: both merges today left local main behind until the front door pulled. Fix:
  fetch before resolving a base, and refuse a base that is behind its remote.
- **No landed state.** A merged branch still reads as merely closed, so the board cannot show
  what has actually landed. Seen: after today's two merges. Fix: a landed state set when the
  branch's commit is reachable from the base, and shown on the board.
- **A scout gets a full space.** Reading does not need isolation, yet a read-only scout still
  gets a checkout of its own. Seen: spaces made for investigation. Fix: give a scout the main
  checkout or a shared read-only copy, and make a space only for work that changes code.
- **No load check.** Nothing checks machine load before a heavy step. Seen: two agents
  installing or running suites at once is the pattern that crashed this box on 2026-09-25.
  Fix: a load check before a heavy step, and one heavy step open at a time across the crew.

- AI-Playwright still holds a local copy of the `/diff` viewer and its skill under its
  gitignored `.pi/` (`.pi/extensions/diff.ts`, `.pi/skills/diff-review/SKILL.md`). Clowder's Pi
  package is now the source of truth for both, so those two local files should be deleted once
  the package is installed, or `/diff` registers twice in one session. Nothing is deleted -
  a file, a branch, a folder, a record - without the owner's word first.

## Prior art

[Firstmate](https://github.com/kunchenguid/firstmate) is the clearest statement of the
one-liaison idea and this design started from it. It supports eight harnesses on macOS and
Linux; this is one harness, on Windows, and small. Its licence is MIT.

Ideas borrowed, no code: the one-liaison model, ship versus scout task shapes, a plain-language
dispatch rules file, quota-aware model choice, hiding tool noise, and a generated board for
pending decisions.
