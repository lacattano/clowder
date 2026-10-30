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
hand. The front door can reset a worker's context from its side too, by typing the command into
the pane as keys. Consequence: the dispatcher does **not** manage models, and it resets context
only at a clean boundary - never while a job or a step is open.

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

### 13. The CLI is Python, pinned to 3.15

Decided before step 1, as required. Stdlib only - `tomllib`, `subprocess`, `json`, `argparse` -
so there is no build step, no lockfile and no dependency to audit. Python 3.13 is what bare
`python` resolves to on this machine, so the tool names its interpreter: `py -3.15`, or the
`3.15` in `.python-version`.

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

The new pane opens as a **new tab**, not beside the caller. A repo's agents are tabs in one
workspace, so the pane joins the workspace a peer reports; with no peer it opens a new tab in
the caller's own workspace. Either way the caller's tab is left whole - a split halves it every
time an agent is made, which is the owner's "split window thing". A caller that wants the old
placement on purpose passes `--split`, and `--direction` still chooses the side.

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
  starts, and the job forks from `origin/<base>`. A stale local branch is never carried in.

What a switch does **not** do is clean up. Untracked files survive it, which is the point (they
are the install) and also the risk (they can be yesterday's artifacts). Four rules keep it shut:

- A job starts from a clean space. Dirt is refused, with the file names.
- One open job per space.
- One open step per job, so two runs cannot start at once in one space. Across spaces the cost
  class and the box's headroom decide, and a model leg takes the box alone (section 39).
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
gate chain, kept in the shipped `crew` skill.

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
  chain, in the shipped `crew` skill.

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
around them. Part of it is checkable now: the skill states one message - one decision or report,
three lines of context at most, and the word that answers it - and it carries his own words of
2026-10-25, with the message he was sent, as the worked bad case. The same section names the
record behind the messages, and only the files that exist: `state.json` under `.clowder/` is the
cumulative one, rewritten on every change and never appended, with `state.json.audit` beside it;
`HANDOVER-<date>.md` is composed from it by `clowder handover write`, so it holds no second copy
of a fact; `handover-notes.md` is the only hand-written part, capped at ten lines; and
`defects.json` is the one home for what cost time, written by `clowder defects add`. The chat and
the board are not the record. `tests/test_skill.py` holds the shape, his words, the files -
`state.json` with `state.json.audit`, `HANDOVER-<date>.md`, `handover-notes.md` and
`defects.json` - and a 587-line cap on the whole skill, not on the section alone, so the
discipline cannot become an essay. The first wording of this rule named a per-day state file that
no command wrote (defect d-0040); the test now refuses that claim by name.

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
session. The instruction is short: the bus mechanics live in the shipped crew skill, which the
worker has in its system prompt, and the marker keeps only the instruction to send, so shortening
it cannot lose the behavior that decision was about.

A pane can send only once the remote-pi Docker service is running and the pane is joined with
`/remote-pi join`; a pane that has not joined answers "Not in a session", which looks the same as
a failed send, so the front door says which it is rather than let it fail quietly. The front door
can join an unjoined pane from its side by typing the command into the pane as keys
(`herdr agent send-keys`); a slash command sent through the agent prompt is a message, not a
command. These mechanics are in the crew skill's Reporting section, not in the marker.

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

### 28. A hand-action the front door repeats is a command, not a rule

The owner, 2026-09-30: "im asking if the action you tried to take could instead be written in python
as a process". He was right. Four things were done by hand on this machine - with shell commands and
keystrokes - and each now does its own work and checks it:

- `clowder agent reset NAME` types `/new` into the pane as keys, then verifies the session file
  changed and fails if it did not. It refuses while a step on that agent is unreported, because
  the reset wipes the session copy the answer is read from. A slash command sent through the agent
  prompt is a message, not a command, so the keys path is the only one that works.
- `clowder job pin <job> --to <reviewer>` pins the job's recorded save into the reviewer's copy
  and proves the copy holds that commit. It exists for when the writer's space has moved on to a
  later job, so `job handover` can no longer read the commit from the branch.
- `clowder job open` fetches, then forks the new branch from `origin/<base>`, so a stale local
  branch neither blocks nor contaminates a job. It uses that ref whenever it exists, even after a
  failed fetch, and warns that it may be stale. It refuses only a base whose own history was
  rewritten, because then the local branch and the remote disagree about what the base is.
  `clowder checkouts` prints one line per checkout so a stale one is visible without anyone
  running git by hand.
- `clowder state repair --drop-unknown FIELD --backup PATH` removes a field no copy knows from
  every record that carries it. It refuses without a free backup path, writes the backup first,
  names what it removed, and appends one line to `state.json.audit`. The audit format is shared
  with the queue drop (q-0036), so both write lines a reader can scan the same way.

The rule behind the rule: if the front door has to do something more than once, and it can be
checked, it belongs in the tool. `--force` stays, as a human's deliberate override, and the pass
and merge gates are untouched.

### 29. A dead step is abandoned, not forced

When a pane dies or the machine restarts mid-step, the step can never report. `dispatch` refuses
the next step while it is open, and the only way past was `--force`, which skips the branch and
clean-tree checks with it. That is how a half-captured answer once got recorded as a step's result.

- `clowder step abandon <id> --why TEXT` marks an open step abandoned, keeps the reason and the
time, leaves the answer empty, and frees its job for the next step. A missing `--why` is refused.
- `report` never captures an answer for an abandoned step, and says it was abandoned instead of
  "no answer yet". `tasks` shows `abandoned` in the status column, apart from `reported`.
- It appends an audit line, the same shape the state repair writes: time, who, action, step, job,
  agent, reason.
- `--force` is kept. This replaces its routine use, not its existence.

### 30. The board is rewritten by every state change, and the owner's decisions are numbered

The owner reads the board, not a fast-moving chat, so the board has to be current without anyone
remembering to refresh it, and it has to carry every decision put to him.

- Every command that changes state rewrites the board file beside the state: dispatch, report,
  ensure, owner `--item`/`--clear`, step abandon, state repair, and job open/close/handover/pin/
  pass/word/publish/merge and queue add/send/drop. The write is best effort: a failure warns on stderr
  and never fails the command that triggered it. `clowder board` still writes and prints the path.
- A decision is recorded with `clowder owner <task> --item TEXT` at the moment it is put, or as an
  open decision on a report. The board numbers every question the owner must answer, both kinds in
  one sequence, so a number names one question on the page. It clears the item in the same command
  path that records his answer (`report --decide`, or `owner --clear`), so the list is never longer
  than the open decisions.

The at-risk section is content-based, not hash-based, and both the board and `report` ask the same
question, so the answer does not change with where it is asked. A commit on no branch is flagged only
when its change is on no branch either: two commits with the same patch-id carry the same change, so
a rebased-away commit is superseded, not lost. A merge carries its parents' content and an empty
change carries nothing, so each is named as such; `could not check` is kept for a real read failure.
A short hash must be resolved to its full form before comparing, because `git log --all` includes the
worktree's own HEAD and a short hash would match itself. Three false alarms in one evening had taught
the owner to ignore the section.

### 31. The crew sets its own commit identity, and refuses a foreign one

Seen 2026-10-01: the product checkout's `.git/config` named
`openhands <openhands@all-hands.dev>` for 77 commits. Worktrees share the main checkout's config,
so one `git config user.email <anything>` covered every worktree the crew would make there. The
crew tool set no identity of its own, so a commit inherited whatever the file said.

The identity is the crew's, not the owner's, so his own global git config stays his:

- `[git] name` and `[git] email` in the clowder config, defaulting to
  `clowder-bot <94532220+lacattano@users.noreply.github.com>`. One place to change, every repo the
  crew touches.
- Every pane the tool makes is launched with `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
  `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` (`herdr pane split --env`). The process
  environment beats the checkout's config, and a `git config` write cannot reach it, so the fix
  does not depend on the file being right - which is what failed.

The backstop runs in the ship path, not as a global hook: `job publish` checks the commits in
`base..branch` with `refuse_foreign_authors` before `git push`, and refuses any whose author or
committer is not the crew's identity. A global `core.hooksPath` hook would also work; this was
chosen because it lives in the tool's own path, needs no global state on the owner's machine, and
cannot be skipped. Its costs: it checks only at publish time, and only the job's own new commits -
a commit already on origin is the separate history rewrite, not this.

The hole it cannot close: a pane made before the change, or by hand, still inherits the checkout's
config. The backstop catches those at publish. `user.useConfigOnly` is complementary only: it
catches git guessing an identity, not a wrong explicit value. It is set on this machine
(`git config --global user.useConfigOnly true`) and recommended in `clowder.example.toml`; it
never touches the owner's own identity, which is configured explicitly.

### 32. A dispatch never types over text the owner left in the box

Seen 2026-09: dispatch submits a brief with `<mux> agent prompt`, which writes the text into the
worker's own input box and presses enter. That is the same box the owner types in, so when he
had a half-written sentence there, the two writers merged and he lost both. The bus does not
touch the box, but the tool had no bus-send path for a brief.

Two paths, in order:

- **The bus, when one is configured and the target is joined.** `[bus] send_argv` is a complete
  command with `{agent}` and `{brief}`; `[bus] peers_argv` lists the joined peers, matched to the
  target's folder and name so a repeated name in another repo cannot pass. A bus send never opens
  the input box, so it cannot collide with the owner. If no peers command is configured but a
  send command is, dispatch attempts the send. A send that fails is recorded as a failed step;
  it is not retyped, because a timeout does not prove non-delivery.
- **Typing, only with a clear box.** The fallback reads the pane's input box from a screen
  snapshot (`<mux> agent read --source visible`). The editor is drawn between two border lines,
  so the box is the region between the last two border lines: a bare rule, or the "Working"
  header rule the spinner writes. The borders, not the usage arrows, are the anchor, so a fresh
  pane with no usage line is still readable. If the box holds text, dispatch waits
  `[dispatch] input_wait` seconds for a mid-keystroke to clear, then refuses with the draft in
  the message and records nothing. A snapshot with fewer than two borders is refused too:
  typing blind is the collision. `--force` is the explicit way past it.

  The region was fixed on 2026-10-11 after the reviewer caught the first version reading below
  the *bottom* border and returning "" for every box, empty or not - so the guard did nothing.
  The parser is now pinned against raw snapshots from a real pane (`tests/fixtures`), empty and
  holding a draft.

The same guard covers `agent reset`, which types `/new` into the same box.

remote-pi 0.7.0 has no shell `send`; until it does, a machine that wants the bus sets
`[bus] send_argv` to a bridge it trusts. With no bus configured the collision is still stopped,
by the guard.

### 33. A step that reports closes itself

Seen 2026-10-24: a worker's report reached the record only when the front door read it. Until
then the step still said `dispatched`, and the one-open-step guard refused the next step on the
job. The way past was to read a finished report for its side effect. It happened nine times in
one day, and each time the workaround was to read the report for a side effect.

The tool cannot see the front door's bus inbox, but it holds the other half of every report: the
answer stays in the worker's own session, which is what the marker already tells the worker. So
there are two intakes, and either closes the step:

- `clowder step report <id> --headline TEXT [--answer-file PATH]` is the worker closing its own
  record. One line, plus the file when the answer is long. It is the exception to "the tool never
  records an answer for the worker": the worker wrote it, and it carries `answer_source: step`
  or `file`.
- `clowder inbox` closes every open step whose worker has settled and left an answer, on the
  evidence it already read. `answer_source: session`.

Either way the record is what `report` would have written - the answer, the usage window, the
model, the commit - and one audit line, action `step-report`, with the same fields as a
`step-close` line. The status becomes `closed` with `reported_at` set, so `is_open` is false, the
one-open-step, class-slot and reset guards free themselves, and the board's Done column stops
calling finished work "in progress". A closed step that has an answer reads `answered`, not
`closed, no answer came`.

Reporting twice closes once. A step that is not open is not swept, and `step report` on a closed
step prints "already closed" and changes nothing, so a retried report cannot write a second
close, a second timestamp or a second audit line. A read of a closed step does not reopen it.

What it does not do: it does not decide the report was good. A worker that answers in one line
and stops is closed as reported. The read still shows the whole answer, and the front door still
reads the report it must act on - it just no longer reads it to clear a record.

### 34. The owner item is a heading, and the detail is the body

Seen 2026-10-24, in his words: the board "shows a massive wall of text". An owner item was one
field, so everything went into it: the question, the options, the costs and the recommendation.

- `owner --item` is one line, at most 120 characters. A line break or a longer line is refused
  with the advice to use `--detail`, so the enforcement sits where the text is recorded and the
  board cannot refill one item at a time. A record written by an older copy is still rendered in
  full; the rule binds new writes, it does not rewrite the past.
- The board renders the item as a heading (`.owner-title`, bold, wrapping) and the detail as a
  body under it (`.owner-detail`, `pre-wrap`, wrapping, never clipped). The detail keeps its own
  line breaks; the item is flattened, because it is one line.
- One decision is shown once. If an owner item on a job names the merge, the derived "your
  word" line for that job is skipped: the item is the line he reads and answers, so the two
  would be the same decision twice. The check is the literal word, because a miss shows a
  duplicate and a false hit would hide a decision - and hiding is the worse failure. An item
  about something else on the same job leaves the merge line in place.

### 35. A published job parks its branch and frees its space

Seen 2026-10-24: a passed, published job waiting on the owner's merge word held its checkout. A
new job in the same repo was refused, and the only way forward was a second pane - 241 MB to
1.9 GB of worktree for a job nobody is editing.

The mechanism is the release this tool already has, applied to the last waiting state. `job
handover` and `job close` both detach a space at the base and keep the branch; `job park` does
the same for a job whose only remaining gate is the owner's word:

- It refuses unless the job is open, published, has no merge word yet, and has no open step. An
  unpublished or unsigned job is not waiting on him yet, and a job with the word is merged, not
  parked.
- It records `parked_at` and `park_commit` (the branch tip), sets `released_at`, and detaches the
  space at the base. The branch is kept, so the commit is reachable and the pull request is
  untouched. `job open` already gates on a job whose space is checked out, so the next job starts
  there. One branch per checkout still holds: parking releases the checkout, it does not
  multiplex it.
- A step cannot be sent to a parked job: its checkout is released, so a step would read
  whatever branch came next. The refusal names the merge command instead.
- One audit line, action `job-park`, with the branch and the commit.
- `job merge` on a parked job refuses if the branch no longer points at `park_commit`. The
  owner's word was for the commit he passed, so a moved branch is code he has not seen: the fix
  is a fresh walkthrough and pass, or moving the branch back - never a merge of the new tip. A
  job that was never parked is checked exactly as before, because only a recorded `park_commit`
  turns the check on.

What happens if the parked branch moves: nothing moves it from inside the crew. The space is
released, so no pane holds it, and the branch is not checked out anywhere. Only a hand-made
checkout can move it, and the merge check above catches that.

### 36. The learning loop: one record, one generated view, one command

Seen 2026-10-25, measured from the files rather than felt: 28 days of carried work had five
handover documents and 13 working days with none, including days of 54, 50, 58, 66, 28 and 44
steps. One known defect was written in three handovers in a row and still bit; another was in one
handover, absent from the next two, and back in the last. Both are in the defects record now, as
d-0001 and d-0002. They are not quoted here, because a defect has one home and this is not it.
Five items in the latest handover
appear in no earlier one, and whether they were older and never written down cannot be known. The
front door did not read the latest handover at session start; its own start order named a list of
files, so reading it depended on memory.

Writing a thing down is not the mechanism that stops it. So the fact moves into the tool, and the
document becomes a view of the tool:

- **One record.** `.clowder/defects.json`, beside the state file, is the only home for a known
  defect, quirk or workaround. `clowder defects add|list|show` writes and reads it. Fields: id,
  one line in the owner's words, first and last seen (a moment; the list shows the date), count,
  job or none, state (open, has-job, fixed, accepted-workaround), the reason on an accepted
  workaround, and a detail body for the workaround and the citations. Seeding an entry from an
  older record carries its dates and its count, and is refused without a state: a seed must say
  where the defect stands today, not inherit a default. The same normalised line is the same defect, so a second sighting raises the count
  and moves `last_seen` instead of adding a record. If it is not in this file it is not known.
- **One generated view.** `clowder handover write` composes `HANDOVER-<date>.md` from the record:
  what waits on the owner, the steps in flight, the open changes, what merged since the last
  handover, the queue count, and the defects whose `last_seen` is inside the window, each as one
  line and an id. Only the last section is hand-written, from `handover-notes.md`, capped at ten
  lines - the cap is what stops the old document growing back. A defect entry with no id stops the
  write, so a hand-edited file cannot lose one quietly. A second write in one day replaces the
  file; the window still starts at the newest earlier handover, so a rewrite cannot drop what the
  first write recorded. The old `HANDOVER-*.md` files are history and stay read-only: a
  file with no `generated:` mark is never written to, so a day whose date one already
  carries gets `HANDOVER-<date>.generated.md` beside it, and the old file is left byte
  for byte as it was. `clowder start` reads the newest document, and for a day that has
  both kinds, the generated one.
- **One command that reads it.** `clowder start` prints, in order, the handover file and its age
  (and says plainly when it is STALE, with the reason), the decisions that wait on the owner, the
  steps in flight, the queue count, and the defects seen twice with no open job. Stale is a fact
  about the record, not a memory: any recorded activity - a dispatch, a report, a job opened or
  merged, a queue item, a defect sighting - newer than the handover's own `generated:` line means
  a session ran and nobody wrote a new handover. The front-door skill's start order is this one
  command, not a list of files.
- **The repetition rule, with teeth.** A second sighting of the same defect must open a job or
  record an accepted workaround with the reason; bumping the count is not enough. The tool says so
  when the count moves, and keeps saying it where it is read: `clowder defects list --repeated`,
  `clowder start`, and the board's section that waits for the front door - not the section that
  waits for the owner, because opening the job is the front door's act.
- **A reworded defect is a new one, by design.** Matching is on the exact words, so the same
  defect written differently gets a new id and the count never rises. Do not fix that with fuzzy
  matching: it would merge two real defects, which is worse than a miss. Fix it with what a person
  can act on: `clowder defects list --like "<words>"` finds a line holding every one of those
  words in any order, and `clowder start` prints the defects seen in the last seven days so a
  near-duplicate is visible before a new one is added. A total rephrase is still missed, and a
  test pins that miss so the limit cannot be quietly forgotten.
- **A broken record fails open.** `clowder start` and the board are drawn from the state, so a
  malformed `defects.json` prints everything else, warns with the file and the parse error, and
  exits 4 (`RECORD_UNREADABLE`); the board shows the warning in the front door's section. One
  malformed file must not take away the page that says what is happening. `clowder defects list`
  is the command that refuses, because reading the record is its job.
- **No second copy.** The handover and DESIGN's Open section name a defect by its one line and its
  id; neither restates it. The test of the rule is mechanical: if a defect's text is found in two
  of the three places, one of them is wrong by construction. So DESIGN's Open section stopped
  carrying nine defects in prose on 2026-10-25 (decision 36's note in that section says where they
  went); the detail lives in `clowder defects show <id>`, and nowhere else.

What would have to be true to need retrieval: the record would have to outgrow reading it. The
threshold to watch is `clowder defects list` growing past a screen or two - a few hundred entries,
or a session that has to search rather than scan. Even then the first answer is a filter on the
record (by repo, by job, by state), not an embedding: a defect is one line, and a one-line index
is a `grep`. Only a record whose lines are no longer enough to choose from would need semantic
search, and that day is not today. No index is built.

### 37. The page he reads: cheap to draw, in his order, honest about its age

Seen 2026-10-26, in his words: "when you come back to me, you often think I know everything and
I know exactly what you're talking about with no context... The board was meant to be the way
that I could pick up information that I'd missed in the chat. But the board is just always stale,
and I have to ask you to refresh the board. And even then, it doesn't particularly work that
well". Measured on this box: a plain command took 0.9s, `clowder agents` 3.3s, `clowder board`
5.5s, and twenty-six commands rewrite the page on every state change, so each paid that.

- **The draw is from the record.** The roster used to be read from the multiplexer on every
  render, with a git look per agent. It is built from the state file now, and every field the
  state cannot know is left empty ("-") with one line saying the roster was not read live.
  `board --live` asks the multiplexer once for the live list; `clowder agents` still reads it,
  because that command is about agents. Measured on this box, min of five, memory at 61%: `board`
  3.15s, `board --live` 4.90s, `agents` 3.56s, `tasks` 0.63s, and a state-changing command on a
  small state 0.23s. The roster read is worth the `board --live` minus `board` delta, 1.4-1.8s.
  The rest of `board` is **not** the roster: it is 182 `commit_risk` git looks for the stranded-
  commit check on this record, about 1.4s in-process and the larger part of the 3.15s wall time.
  A 0.62s figure quoted earlier was a run where every memo answer still held - a best case, not the
  number. That check is the next target; it is untouched here.
- **The order is his.** "For you" (one line per question, the number he replies with, and how
  long it has waited), "Running now" (each worker, what it is on, how long, what it costs the
  box), plus one line of what the box has left and what the gate would decide now,
  "Waiting for a worker" (the front door's queue, oldest first, with its own warnings), then
  "What landed since you last looked". The records follow, unchanged: the filterable cards,
  agents and spaces, answers, dropped items, the model rollup.
- **"What landed" is scoped to what it counts.** The section reads the owner's last look, not the
  page's last draw. A draw is a moment the tool chose; a look is his, recorded on `board --open`
  and on `board --looked` when the front door relays the page. Every state-changing command
  redraws the page, so measuring from a draw would always answer "nothing landed", which is the
  defect seen on 2026-10-30. The section counts landed work only - a step that started is under
  Running now, not here - and both the heading and the empty line say so ("Nothing has landed
  since you last looked at 12:40"), because "nothing changed" was false while a step ran. One
  sentence each, in his words, no id and no state word - the newest twelve, with "showing the
  newest 12 of N" when the record is older, because a first look at an old record held months of
  merges. The footer keeps the page's own age: generated at, and the record's last change.
- **It reloads only while work runs.** The page always carried a 30s meta refresh. It now carries
  one only while a step is in flight: the browser re-reads the file, and the box draws nothing
  between commands, so an idle reload is flicker. The alternative - a timer that redraws the page
  while work runs - would cost a render per tick; at the measured 0.23s for a state change on a
  small state, and 3.15s on this record, that is 27s to 6 minutes of CPU per hour per open page,
  plus a process to start and stop. The meta refresh costs a file read and no daemon, so it is the
  cheaper one and there is still no server or port to manage.
- **Not live means "not checked", not "busy" or "idle".** The roster is not read on a normal draw,
  so the page says what it can: a step whose record has not changed for the quiet threshold is
  marked "nothing recorded for 6h" under Running now, and the worker section repeats it as "not
  checked live - shipped nothing to the record for 6h". A dead worker therefore reads as doubtful
  instead of running, and nothing is called idle without a live read. The threshold is 60 minutes:
  this crew's steps run 10 to 40 minutes routinely (2026-10-26: 5, 7, 20, 38, 57 and 73; the
  review step took 35), so a two-minute flag fired on every healthy step. It sits above any
  healthy step and below the 24-hour held-change figure, which is the file's other quiet bound.
- **Every report carries one line for him.** `For the owner: <two sentences, his words, what this
  changes for him>`. The dispatch marker requires it, `clowder report` shows it first and marks a
  report without it "(incomplete: ...)", and `step report` says so on stderr. The front door
  relays it verbatim. It is part of the answer, so it lives in the record with the answer - no new
  field, no second store.

No read tracking: the page records when the tool drew it, never when he read it. Nothing was
deleted: the old first section keeps its content under "For you", and every record section is
where it was.

### 38. One answer to "what waits on me"

Seen 2026-10-26: `clowder start` printed "decisions waiting on you: 0" while a change had waited
for a walkthrough for a day. The board showed it under "For you", so the two views disagreed, and
the owner stopped trusting either - "anything waiting on me as i can't trust the board at the
moment". That question is the defect.

The list is now built once, in `owner.waiting_on_owner(tasks, jobs)`, and every view renders it:

- **His list, three kinds.** A decision recorded on a task (`owner --item` or `--open-decision`);
  a change that is passed, published and waiting only for his merge word; a change whose review
  is finished and which waits for him to be walked through it. Each line carries the thing in his
  words, the word that answers it ("reply with your answer", "reply merge", "reply pass, or what
  to change"), and how long it has waited. No task or job id: the number is the view's own.
- **Not his, listed apart and never counted.** A passed change that is not yet published (the
  front door publishes it and CI runs), and one whose word is recorded and is waiting on CI before
  the front door merges it.
- **The board's "work at risk" line moved to the front-door section.** Keeping a stranded commit
  is the front door's act, and it was the only line in "For you" that `start` could not know; with
  it here, the section's count is exactly his list. Nothing is deleted: the line and its command
  are unchanged, in the section that waits for the front door.
- **One count, two views.** `start`, the board's "For you", and the generated handover all call
  that function, so a count cannot say zero while a line is on the page. A test patches the one
  function and asserts all three views render its output, which is the shared path rather than
  equal output on one fixture.
- **A recorded question waits whatever the step did.** Seen 2026-10-27, minutes after this
  landed: three owner items sat on closed steps and had vanished from both views the moment their
  steps closed. An owner item is cleared with `owner --clear`; an open decision is answered with
  `report --decide`. `--clear` clears the note only: when a decision on the same task is still
  open it says so, and names `--decide`, instead of reporting a clear that left the line standing
  (seen 2026-10-27, the same day: a clear printed "cleared" while the line stayed, and a command
  that says it did something and did not is a false success). The step's own status is
  irrelevant, so the list no longer checks it, and the wait is the item's own moment.
- **One record, one list, one count.** Seen 2026-10-27 on the live page: "38 jobs in the todo
  queue but 40 on the list of waiting for an agent". The section counted the queue plus its
  warnings. It now counts the queued records only - the same number the To Do column shows,
  because both are the same records - and the warnings sit below under "Also for <front door>:
  N warning(s). These are not queued items, so none of them is in the queue count above."

The re-review and unconfirmed notes are passed in by the board, which reads the refs; `start`
passes none, because it does not. Only a note differs, never the count.

### 39. A step states its cost, and the gate is a headroom measurement

Seen 2026-10-27, in his words: "it seems random what you consider heavy". It was. `--heavy` was
one boolean the front door set by feel, held from dispatch to report. On 2026-10-26 and again on
2026-10-27 a worker that was only editing files held the crew's only heavy slot for twenty
minutes and blocked a genuine one-hour measurement, while a step that reads a large repository
cost nothing and was never marked. The two machine gates existed (`heavy_max_memory`,
`heavy_max_load`) and were unset, so nothing measured the box at all.

The numbers this rests on were measured on this box on 2026-10-27 (t-0986): 64.00 GiB fitted,
31.78 GiB visible because 32.00 GiB is carved out for the GPU; `dwMemoryLoad` at 72-79 percent
of the visible half, moved not at all by a resident 25.35 GiB model. The honest figures are
`\Memory\Available MBytes` (free plus standby), commit limit minus committed bytes, GPU adapter
dedicated usage, and the page file's own use.

- **A class, not a flag.** `--cost none|read|suite|install|browser|model`. `--heavy` is kept as
  the old name for `suite`, so every brief, script and state file written before this reads the
  same way. A task and a queue item carry the class; a record that carries only `heavy` is read
  as a suite, and the `heavy` field is still written true for a suite so an older copy reads it.
- **A stated footprint per class, in one place** (`clowder/box.py`, with the comment that holds
  this table): `none` 0 GiB; `read` 1 GiB; `suite` 3 GiB, 7 minutes (the clowder suite); `install`
  2 GiB, minutes and disk; `browser` 6 GiB and 3 GiB of the GPU, about an hour (a 113-story leg);
  `model` 4 GiB host side and 26 GiB of the GPU (the 27B resident is 25.35 GiB), about an hour.
  The model's host share is small because its weights sit on the card: charging 25 GiB to the CPU
  pool as well would refuse every model leg on a box that runs one perfectly well.
- **The gate is a subtraction.** Free memory (the metric `load` already reads) minus what the
  running steps hold, compared with the new step's class; the commit headroom and the GPU's
  carved-out memory are compared the same way. Floors keep the box usable after the step starts:
  4 GiB free, 6 GiB of commit headroom, 2 GiB of the GPU. The floors are the defaults and are
  set in `[gates]` in GiB. `heavy_max_memory` is retired and refused with its replacement named,
  because a config holding it would otherwise read as "no gate at all".
- **What that allows.** Two suites start together when the room covers both. A suite and a model
  leg are refused with the free figure, the held figure and the floor named. A step with no
  stated cost is never refused, and a box that cannot be read fails open with a warning - a
  broken probe must not block work.
- **The state gate that survives is the model's.** The GPU counter costs seconds, so it cannot go
  in a gate; the crew cannot measure two model legs against each other, and `model` is the class
  that takes the box alone. This is the old one-heavy-step gate, kept where it is still true.
- **The numbers are shown where the decision is made.** `start` and the board print each
  in-flight step's class and one line of what the box has left; a refusal prints the checks it
  compared instead of a percentage; `clowder box` prints the whole readout - the RAM fitted and
  the GPU's share, the GPU side with the process holding it, the CPU side with the page file,
  and what the gate would decide now. Each line names the metric it came from. It is `box`, not
  `capacity`: the crew already says "the box", and "capacity" suggests a configured limit where
  this is a live reading.
- **The readout may shell out; the gate may not.** GPU use and page-file use come from
  `typeperf` and take seconds, so `read(with_gpu=True)` is only for `clowder box`.
  `GlobalMemoryStatusEx` already fills `ullAvailPhys`, so free memory needs no second call, and
  the GPU's carved-out size is read once from the registry and kept.
- **`--force` still sends, and says so.** The measurement is taken even under `--force`; when it
  refused, the send overrides it and appends one `dispatch-forced` audit line carrying the class
  and the refusal, so the record never shows a step the box refused for no reason.
- **`load.py`'s `except OSError, AttributeError, ValueError:`** is PEP 758 syntax: valid on 3.14
  and above, and required by nothing. `pyproject.toml` requires 3.15, so it is legal here, and it
  is not portable to 3.13 and below. It is not normalised, because it cannot be: `ruff format`
  under `target-version = "py315"` rewrites the bracketed form back, so the portable spelling
  fails the format gate. The choice was made on 2026-10-05 (see the comment in `pyproject.toml`)
  and this change keeps it: the tool targets 3.15 and the formatter owns the spelling.

### 40. A defect names the jobs that fix it, and the merge closes it

Seen on the day the defects record was built: d-0007 (a committed evidence bundle carried a
machine name and absolute paths) is fixed by j-0206, which was in review. When it merged, the
record would still have said open, because nothing joined a defect to the work that fixed it. A
person had to notice, and after seven days the defect dropped out of `clowder start`'s recent
list, so the chance to notice went with it.

- **A link, made where the job is opened.** `clowder job open --defect <id>` (repeatable), or
  `clowder defects link <id> --job <job>` later. A defect may name more than one job; the front
  door links it when the job is opened, so the link is not a thing to remember.
- **The merge closes it.** `clowder job merge` marks every defect naming that job fixed, with the
  job, the pull request and the date, and writes one `defects-fixed` audit line. A defect that
  still names another open job stays `has-job`: the work is not finished while another branch
  says it is coming, and the merge that lands the last link is the one that fixes it.
- **The list tells the truth.** `defects list` shows a linked defect whose job is open as
  `has-job`, and a link whose job closed without merging as `open` again, so the repeat alarm
  returns. That is a read-time answer, not a write: reading the record never edits it.
- **A defect that comes back starts clean.** Seen again after a fix, the settlement fields and
  the old job link are cleared, so the defect is open with no job; the next job's merge closes it
  again. Left in place, the closed fixer read as a second link and the defect could never close.
- **A merge never rewrites a decision.** An accepted workaround keeps its state and its reason,
  and a link to a closed job is dropped rather than turned into a fix.
- **Every change leaves a trail.** A merge that only drops a link, a job closed without merging,
  and `defects unlink` each write one audit line. `defects unlink <id> --job <job>` drops a wrong
  link without closing the job, and `describe` names every linked job.
- **Not fuzzy matching.** The link is named by a command, never guessed from a defect's words.
  The word search and the seed are unchanged.

### 41. He reads one place: what he missed, built from the record every time

His words, 2026-10-26: "a lot of our turns are just me asking you to repeat yourself with context
... The board was meant to be the way that I could pick up information that I'd missed in the chat.
But the board is just always stale, and I have to ask you to refresh the board." Three mechanical
faults sat behind it. The board is a file a command writes, so between commands it is a snapshot
of a record that moved on. Its first section lists records (job ids, states, worker questions),
not what happened and what it means for him. And nothing recorded what he had already read, so
"what did I miss" could not be computed: every view answered "what is true now".

What already covered part of it: the board's "What landed" section counted by the page's own draw
moment, not his reading, so it could never change while every command redrew the page; "For you"
already carries the word that answers each line; the dispatch marker already requires a
`For the owner:` line, and `report` already shows it first and marks a report without it
incomplete. Those stayed.

- **`clowder catchup`** builds the answer from the record on every run and caches nothing: what
  landed and what it changes, what broke or is at risk, what waits on him with the answering
  word, what runs now, what is queued. One line each, names before handles, no worker narration.
- **Read tracking in the one state file.** Each line has a stable key and the moment it became
  true; `seen` maps key to the moment shown, and `catchup_at` is his last read. A line is new when
  its key carries a moment he has not been shown, so a line that moves (a job that lands later)
  is new again. `--all` is the standing picture. No second store.
- **The board opens with that text**, built by the same function, so the page and the command
  cannot disagree. Drawing never marks a line read: opening the page (`board --open`), or the
  front door recording that it relayed it (`board --looked`), does. The record sections follow,
  unchanged and in their order.
- **The owner line is enforced where it is recorded.** `step report` refuses an answer with no
  usable `For the owner:` line, with `--force` to close anyway and the report then reading
  incomplete. That is the step where the meaning used to leak: the worker wrote it, the front door
  paraphrased it. The front door now relays it verbatim (section 20).
- **A first read is bounded, and says so.** With no mark to read from, every past landing
  counts as new: the owner's own state listed 244 lines on 2026-10-29. A first read lists the
  last seven days of landings, says the window applies to landings only and that the rest stands
  today (or, with `--all`, that this is everything on record), and marks the whole picture read
  so the old history cannot flood the next read. Nothing else is aged out: waiting, running,
  queued and a still-biting defect are never hidden, because each is work he must act on now.
- **A landed line uses the title he would recognise.** `job open --title` already recorded one;
  `catchup` now prefers the effect line, then the title, and falls back to the label only when
  neither was recorded - the old behaviour, not a new guess.
- **A save carries what it does not know.** The read marks live in the state file, so a copy that
  rebuilt the payload from the keys it knew would delete them. Unknown top-level keys are kept and
  written back, exactly as an unknown field on a task is.
- **Only his read is a read.** `catchup --json` reports and marks nothing: a machine read is not
  the owner reading, and a relay must not eat the line before he sees it.
- **A returning line is news, and the line is in his nouns.** A read replaces the picture rather
  than growing it, so an item that left and came back is not "already read"; a defect put back to
  open by a job that closed without merging carries the moment it became actionable again, and a
  count that moves with it, because a timestamp with second resolution cannot tell two reopens in
  one second apart; and a running line drops the shape label and any job or task handle the
  brief's first line carried, while an English sentence such as "scout the area" is left alone.

## What step 1 built

`src`-less, flat `clowder/` package. No dependencies, so `py -3.15 -m clowder ...` works from
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
shape, a pointer to the reviewer's walkthrough, and the rules the front door runs by - say when
you dispatch, ask for exactly one decision. The owner gate and the always-rules are not repeated
here; the skill carries one pointer at the shipped `crew` skill. It opens with one command for a
fresh context - `clowder start`, which prints the handover and its age (stale said plainly), the
owner's decisions, the steps in flight, the queue and the repeated defects - so a refresh is cheap
and the front door holds no state in its head. (Decision 36 replaced an earlier list of files
with that one command.)

`skills/crew/SKILL.md` is the other half: the generic rules that hold for any crew, shipped so a
fresh install has them. `code/AGENTS.md` is now the instance overlay beside the workspace - the
scope, the multiplexer check, the focus repo, the roster pointer and the dated stories - and it
restates no rule.

`skills/reviewer/SKILL.md` holds the walkthrough steps, the reviewer's thirteen-row senior check,
and the four questions the walkthrough asks the owner. The reviewer reads it; the front door keeps
only the pointer at it.

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

The page opens with **what waits on you** (now titled "For you"), because that is the only part
with a deadline:

- a decision a worker left open,
- a review that is finished and ready to merge,
- work that is on no branch, so it would be lost when the space is reused.

A second section, **waiting for a worker**, holds the front door's own list: queued items, and
steps that have gone quiet while their agent is idle, or whose agent is not in the live list at
all. Then open steps, agents and their spaces, recent answers with usage, dropped queue items, and
jobs. An item the front door drops with `clowder queue drop <id> --why TEXT --by NAME` leaves the
waiting list and moves to the **dropped from the queue** history, which reads the queue-drop lines
of the audit file: what left, why, when, and who decided it. A drop with no reason is refused, and
the audit line is written before the item leaves, so the record cannot be skipped.

The page is a pure function of the state, the live agent list, the audit file's queue-drop
lines, and a few git reads, so it is tested without a browser. The agent list is best effort:
when the multiplexer cannot be read the page says
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

A defect is not restated here any more. This section used to carry nine of them, in prose,
between 2026-09-25 and 2026-09-29; keeping them here as well as in the record is the second copy
that let one be dropped and come back. From 2026-10-25 a defect has one home, `clowder defects
list`, and this section names one only by its one line and its id. The nine older ones live in this
file's git history; whether the front door seeds them into the record is the one open decision in
the t-0939 report. A second copy is what this rule exists to end.

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

The code-reading method in the reviewer skill is adapted from Matt Pocock's skills
([mattpocock/skills](https://github.com/mattpocock/skills), installed here with
`setup-matt-pocock-skills`): follow one concrete thing through the code, one step at a time,
pacing to the reader, and teach the reason rather than the location. The wording is ours; only
the idea is borrowed.
