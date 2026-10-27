# `clowder/cli.py`

## Purpose

This module is the command line: the spine the front door calls. It parses
arguments with `argparse`, builds a `Config` and a `StateStore`, runs one command
handler, and turns the result into text or JSON.

The module docstring names the core commands: "Three commands carry the state:
dispatch, tasks, report. Two more explain the setup: agents, config." Around those
satellite commands exist for jobs, the queue, steps, agents, checkouts, the board
and the state file.

Every command handler has the same shape: `cmd_<name>(args: argparse.Namespace) ->
int`, returning the process exit code. `main` wires them up. On a
`ClowderError` it prints a clean message and returns the error's `exit_code`; on
`KeyboardInterrupt` it returns `130`. A command flagged `refreshes_board=True`
rewrites the HTML board on the way out, whichever way it ended.

## Public API

### Entry points

```python
def build_parser() -> argparse.ArgumentParser
def main(argv: list[str] | None = None) -> int
```

- `build_parser()` - builds the full argument parser: global options plus every
  subcommand and its `handler`.
- `main(argv=None)` - relaxes stdout/stderr error handling so a non-UTF-8 console
  cannot crash a report, parses the arguments, runs the chosen handler, and returns
  its exit code.

### Command tree and handlers

All handlers take `args: argparse.Namespace` and return `int`.

| Command | Handler | What it does |
| --- | --- | --- |
| `dispatch` | `cmd_dispatch` | Send a brief to an agent and record it. |
| `tasks` | `cmd_tasks` | List tasks and their state. |
| `inbox` | `cmd_inbox` | Show what has reported since the last look. |
| `report` | `cmd_report` | Read one task's report (`report <id>`). |
| `owner` | `cmd_owner` | Record or clear one thing that waits on the owner, against a task. |
| `step abandon` | `cmd_step_abandon` | Record a dead step as abandoned and free its job for the next step. |
| `step close` | `cmd_step_close` | Close a step that can never report, and keep the answer it has. |
| `step report` | `cmd_step_report` | Close a finished step where it reports: one line, the answer from a file, one audit line. |
| `ensure` | `cmd_ensure` | Make sure an agent serves a repo, creating one when there is none. |
| `agents` | `cmd_agents` | List live agents (the roster is the tool's). |
| `agent reset` | `cmd_agent_reset` | Give an agent a fresh context, and prove the session changed. |
| `agent reseed` | `cmd_agent_reseed` | Seed the crew git identity into a live pane, keeping its conversation. |
| `agent rename` | `cmd_agent_rename` | Rename one live agent everywhere its name lives, or change nothing. |
| `job open` | `cmd_job_open` | Start a branch for a line of work. |
| `job close` | `cmd_job_close` | Close a job. |
| `job list` | `cmd_job_list` | List jobs. |
| `job handover` | `cmd_job_handover` | Give a reviewer the writer's saved code, pinned to one commit. |
| `job pin` | `cmd_job_pin` | Pin a job's saved commit into a reviewer's copy, and prove it landed. |
| `job park` | `cmd_job_park` | Free the space of a published job that waits only on the merge word. |
| `job pass` | `cmd_job_pass` | Record the owner's pass. |
| `job word` | `cmd_job_word` | Record the owner's merge word. |
| `job sync` | `cmd_job_sync` | Merge `origin/<base>` into the job branch, without rewriting it. |
| `job publish` | `cmd_job_publish` | Publish the passed branch. |
| `job merge` | `cmd_job_merge` | Merge the pull request, once the merge word is given. |
| `queue add` | `cmd_queue_add` | Record something decided but not sent yet. |
| `queue list` | `cmd_queue_list` | Show what is decided and still waiting to go. |
| `queue send` | `cmd_queue_send` | Send one queued item, once its block has cleared. |
| `queue drop` | `cmd_queue_drop` | Drop a queued item, with a reason and a name that stay on the record. |
| `start` | `cmd_start` | What a fresh front door reads first: the handover and its age (stale said plainly), the owner's decisions, steps in flight, the queue, repeated defects, and the defects seen in the last seven days. Fails open on an unreadable record, with exit code 4. |
| `defects add` | `cmd_defects_add` | Record one sighting of a defect, or the same one again. |
| `defects list` | `cmd_defects_list` | Every known defect, `--repeated` (seen twice with no open job), or `--like WORDS` (an exact-word search). Refuses on an unreadable record. |
| `defects show` | `cmd_defects_show` | One defect in full; the only place its detail lives. |
| `handover write` | `cmd_handover_write` | Compose today's handover from the record and name the file it wrote. |
| `checkouts` | `cmd_checkouts` | How far behind its remote each checkout is, in one line each. |
| `board` | `cmd_board` | Write the HTML page: drawn from the state file, with `--live` to add the multiplexer roster. |
| `config` | `cmd_config` | Show resolved settings and paths. |
| `state repair` | `cmd_state_repair` | Drop a field no copy knows, with a backup and an audit line. |

`main` reads `args.handler` and `args.refreshes_board`, both set by `build_parser`
with `set_defaults`.

Module constants: `PROGRAM = "clowder"`, `NEEDS_HUMAN = 3`, `DEFAULT_TIMEOUT_S =
60.0`, `BOARD_LIVE_MUX_TIMEOUT_S = 15.0`, `OWNER_ITEM_LIMIT = 120`,
`RECORD_UNREADABLE = 4`, `RECENT_DEFECT_DAYS = 7`, `_QUIT_KEYS`, `_RESEED_MARKER`.

## How It Works (internals)

The helpers are grouped by the public command that calls them. Many are shared
plumbing reached by almost every handler.

### Shared plumbing - reached by most commands

- `_context(args) -> tuple[Config, StateStore]` - loads the config and builds a
  `StateStore` from `--state` or the configured state path. Nearly every command
  starts here.
- `_emit_json(payload) -> None` - prints a payload as sorted, indented JSON. Used
  by nearly every command that supports `--json`.
- `_clip(text, width) -> str` - one-line truncation with `...`; used by most
  table-printing commands.
- `_column(rows, gap="  ") -> str` - aligns a list of rows into fixed-width
  columns; used by `tasks`, `agents`, `inbox`, `checkouts`, `config`, `job list`
  and `queue list`.
- `_select_fields(item, fields) -> dict` - keeps only the named fields for a
  smaller `--json` record. An unknown name is refused, not ignored, so a typo
  cannot silently drop a field. Used by `report` and `tasks`.
- `_refresh_board(args) -> None` - rewrites the board after a state-changing
  command. Called from `main`; never fails the command, and never asks the
  multiplexer.
- `_page_stamp(path) -> str | None` - when the page being replaced was drawn, read
  from its own `<!-- drawn: ... -->` marker; None for a first draw.
- `_board_agents_from_state(store) -> list[BoardAgent]` - the roster the state file
  knows: names, spaces and the open job on each. Every live-only field is left empty.
- `_defect_store(config) -> DefectStore` - the one defects record for this config,
  beside the state file.
- `_warn_unreadable_record(error) -> None` - the loud stderr warning both drawing
  commands print: the file, the parse error, and the command that shows it.
- `_open_job_ids(store) -> list[str]` - the ids a repetition check needs, so a job
  that is open counts and a closed one does not.
- `_read_brief(args) -> str` - returns the brief from words or `--brief-file`
  (refusing both, requiring one). Used by `queue add`.
- `_first_line(text, limit=120) -> str` - the first non-blank line, clipped. Used
  by `queue add` and `queue drop`.

### Dispatch path - `cmd_dispatch` and `cmd_queue_send`

- `_dispatch(args, config, store) -> int` - the shared body of a dispatch: resolve
  the repo and place, find or check the agent, apply the guards, apply the marker,
  send, and record the task. Used by `cmd_dispatch` and `cmd_queue_send`.
- `_deliver(mux, args, config, message, agent_info) -> MuxResult` - sends one brief:
  over the agent bus when a command is configured, else typed under the input-box
  guard.
- `_refuse_if_input_dirty(mux, name, config, force, what) -> None` - never types
  while text the owner left sits in the same input box. Used by `agent reseed` and
  `agent reset`.
- `_lookup_agent(mux, name)` - looks the agent up in the live roster; a wrong name
  fails loudly instead of sending into the void.
- `_refuse_cross_repo(args, agent, repo_path) -> None` - an agent serves the repo
  its pane was opened in, and no other.
- `_refuse_outside_job(args, agent, job, store) -> None` - a step runs on the job's
  code: the writer's branch, or a handed-over save.
- `_restore_writer_branch(job, agent, args) -> None` - puts the writer's space back
  on the job's branch before a fix step.
- `_refuse_heavy_overlap(store) -> None` - only one heavy step is open at a time,
  across the whole crew.
- `_refuse_high_load(config, args) -> None` - refuses a heavy step when the machine
  is already loaded (the optional `[gates]` check).
- `_refuse_open_work(args, agent, store, job) -> None` - a send lands in the pane,
  and the newest message wins; this refuses when older work is still open.
- `_where(task) -> str` - `worktree <name>` or `main checkout`, for table output.
- `_quote(argv) -> str` - shell-quotes an argv list for display or a command string.

### Report and inbox - `cmd_report`, `cmd_inbox`

- `_agents_by_name(mux) -> dict[str, AgentInfo] | None` - the live roster as a
  lookup, or `None` when the list could not be read. `None` is not an empty list:
  nothing is closed on a roster that proved nothing.
- `_session_and_answer(recorded, live, since, until=None) -> tuple[str | None, str | None]`
  - the session a step's answer should be read from, and the answer in it.
- `_report_until(store, task, since) -> float | None` - where this step's dispatch
  window ends: the next task to the same agent.
- `_reported_since_look(store, agents) -> list[tuple[Task, str]]` - open steps whose
  worker has settled and left an answer since dispatch.
- `_close_reported_steps(store, agents) -> list[Task]` - closes every open step that
  reported, recording what `report` would, so the one-open-step guard frees itself
  without a read. Called by `inbox`; idempotent, so a second report closes nothing.
- `_step_window(store, task) -> tuple[float | None, float | None]` - the step's own
  dispatch window, shared by `report` and `step report`.
- `_record_step_facts(task, usage) -> bool` - records the usage, the model and the
  commit a report is measured by; shared by `report` and `step report`.
- `_report_next(task) -> str` - one line naming what the reader most likely does
  after the report.
- `_resolve_session(mux, task, since, until=None) -> tuple[str | None, str | None, str | None]`
  - the session to read, the answer already in it, and the agent's status.
- `_job_moved_past(task, job) -> bool` - has the task's job branch or held ref moved
  past the step's commit?
- `_work_has_landed(task, job) -> bool` - is this step's work already in the job's
  history, so its commit is moot?
- `_job_review_state(job) -> tuple[str, str | None]` - where a held candidate's
  work is: held, moved, or unknown.
- `_job_state_word(job, tasks) -> str` - the job's state word with the re-review
  states from the tip check.

### Owner words - `cmd_owner`, `cmd_job_pass`, `cmd_job_word`

- `_owner_detail(args) -> str | None` - the detail body from `--detail` or
  `--detail-file`, stripped, or `None`.
- `_owner_words_by(by) -> str` - the name that gave the owner's words. A worker
  cannot give them, so this refuses a worker's name.

### Agents and jobs - `cmd_ensure`, `cmd_job_*`

- `_ensure(config, mux, repo_path, *, repo_name, role, kind, direction, name=None,
  create=True, split=False)` - the one place where an agent for a repo is found or
  made. Used by `ensure`, `job open`, `job handover` and `job pin`.
- `_release_job_space(config, job, *, force=False) -> str` - returns a job's space
  to the base while keeping its install; returns the base ref. Used by `job close`,
  `job merge` and `job park`.
- `_sync_job_branch(config, store, job) -> bool` - merges `origin/<base>` into the
  job branch under the crew identity. Used by `job handover` and `job sync`.
- `_require_pass(job) -> None` - refuses unless the owner's pass is recorded. Used
  by `job publish`.
- `_require_merge_word(job) -> None` - refuses unless the owner's merge word is
  recorded. Used by `job merge`.
- `_require_pass_commit(job) -> None` - the owner's pass is bound to a commit; the
  branch must not outrun it. Used by `job publish`.
- `_crew_identity(config) -> gitcmd.Identity` - the commit identity every pane the
  crew makes is launched with. Used by `agent reseed` and `job publish`.
- `_publish_branch(job, identity) -> None` - pushes the branch (a seam tests
  replace; nothing else may skip the gate).
- `_merge_pull_request(job, pr) -> None` - merges the pull request through `gh` (a
  seam tests replace).
- `_queue_target_agent(config, item) -> str` - resolves a queued item to an agent
  name. It never makes an agent.
- `_queued_dispatch_args(item, agent, *, job, force, dry_run, json) ->
  argparse.Namespace` - builds the argument shape `dispatch` parses, so a queued
  send takes the same path as a direct dispatch.

### Agent maintenance - `cmd_agent_reseed`, `cmd_agent_reset`, `cmd_agent_rename`

- `_wait_for_agent_gone(mux, name, timeout_s) -> bool` - waits until the agent
  leaves the roster, or the wait runs out.
- `_reseed_export(identity, shell) -> str` - the shell command that pins the crew
  identity and proves it ran.
- `_pane_not_a_shell(result) -> bool` - whether a start failed only because the
  multiplexer does not see the pane as a shell yet. That is the one refusal the
  restart waits out; every other failure is reported at once.
- `_start_agent_when_ready(mux, name, pane_id, timeout_s) -> RestartResult` - starts
  Pi again after a reseed, retrying while that refusal comes back. `timeout_s` is
  the budget for the start step, not one attempt: the first attempt may spend all
  of it, so a slow but working start (22 to 45s on this box) is not cut short by a
  cap of the helper's own. A retry spends only the backoff between attempts, and
  each later attempt is clipped to what is left of the budget. It returns the last
  attempt, how many were made, and the budget.
- `RestartResult(result, attempts, budget_s)` - the last start attempt, plus the
  count and the budget. `clipped` is true when more than one attempt was made, so
  the last one did not get the whole budget; `ok` mirrors the attempt's result.
- `_restart_reason(restart) -> str` - the reason shown to the owner. The budget
  is named only when the budget is what stopped the start: a clipped final attempt
  that was killed by it had no time to answer, so its own duration is a sliver and
  says nothing about the pane. Every attempt that answered is quoted as it
  answered - a refusal, a named failure such as `agent_not_ready`, or a timeout
  given its whole allowance - whatever the attempt number.
- `_seconds(value) -> str` - a number of seconds as the owner would say it (60s,
  2.5s), used by the failure message and by nothing else.
- `_start_agent_by_hand(mux, name, pane_id) -> str` - the exact `agent start`
  command the owner can run when the restart timed out. Its argv comes from
  `Mux.build_start_agent_argv`, so it cannot drift from the command the tool runs.
- `_wait_for_session_change(mux, name, before, timeout_s) -> str | None` - polls the
  live session until it differs from `before`, or the wait runs out.
- `_space_rename(cwd, old, new, worktree_dir) -> tuple[Path, Path] | None` - the
  folder move for a rename, or `None` when the agent has no linked space.
- `_rename_remote_pi(cwd, old, new) -> tuple[Path, str] | None` - points a pane's
  remote-pi config at the new name, when it names the old one.
- `_roster_paths(config) -> list[Path]` - the shared documents that list agents,
  when a workspace root is set.
- `_replace_name(text, old, new) -> str` - replaces one agent name and never a
  longer name that contains it.

### Checkouts and board - `cmd_checkouts`, `cmd_board`

- `_checkout_repos(config, store, names) -> list[Path]` - the repos to inspect: the
  ones named, or the ones known jobs and steps use.
- `_board_path(store) -> Path` - where the page lives when nobody asked for a
  different place.
- `_board_data(config, store, mux_timeout_s=15.0) -> BoardData` - everything the
  page is drawn from.
- `_board_agents(config, timeout_s=15.0)` - the live agents and their spaces, for
  `board --live` and `clowder agents`. Best
  effort: the page works with none.

### Internal utilities

- `_risk_memo_path(store) -> Path` - the memo of at-risk answers, beside the state
  it describes. Reached through the board path, not a command by name.
