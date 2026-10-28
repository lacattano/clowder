"""The command line. This is the spine the front door calls.

Three commands carry the state: dispatch, tasks, report. Two more explain the
setup: agents, config.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import __version__, audit, box, gitcmd, load
from . import handover as handover_doc
from . import owner as owner_mod
from .board import BoardAgent, BoardData, now_stamp, open_tasks, stamp_of, write_board
from .config import BUILT_DELIVERY_MODES, Config, load_config
from .defects import (
    ACCEPTED,
    HAS_JOB,
    DefectStore,
    matches_line,
    merge_warning,
    read_defects,
    recently_seen,
    short_date,
)
from .defects import STATES as DEFECT_STATES
from .errors import ClowderError, DispatchError, GitError, MuxError, StateError, UsageError
from .marker import DEFAULT_SENDER, apply_marker
from .mux import AgentInfo, Mux, MuxResult
from .report import INDENT, build_report, owner_line, usage_breakdown
from .sessions import Usage, read_answer, read_model, read_usage
from .state import (
    ABANDONED,
    CLOSED,
    DISPATCHED,
    FAILED,
    OWNER_GATE_WORDS,
    REPORTED,
    SHAPES,
    STATUSES,
    Job,
    Queued,
    StateStore,
    Task,
    step_in_flight,
)
from .timeutil import human_age, now_iso, to_epoch
from .topology import (
    DEFAULT_DIRECTION,
    DEFAULT_KIND,
    DEFAULT_ROLE,
    ROLES,
    ensure_agent,
    inside,
    match_agent,
    normalise,
    requested_name,
    sanitise,
)

PROGRAM = "clowder"

# Exit code for "a human has to decide": no agent serves that repo, and none was
# made. The front door branches on this instead of reading prose.
NEEDS_HUMAN = 3

# The multiplexer is the transport. Submission is async by design: the report
# arrives later, on the worker's own turn.
DEFAULT_TIMEOUT_S = 60.0

# A board refresh follows a command that already changed state, so it must not
# hold that command up. It no longer reads the multiplexer at all: the roster is
# built from the state file, and only `clowder board --live` (or `clowder agents`)
# asks the multiplexer. The old refresh timeout is gone with the call.
BOARD_LIVE_MUX_TIMEOUT_S = 15.0

# An owner item is the heading on the board, not the whole story. The detail field
# carries the body. A line longer than this is refused with the advice to use it,
# so the board cannot fill with walls of text one item at a time.
OWNER_ITEM_LIMIT = 120

# Exit code for "the page is drawn, but the defects record could not be read". The
# commands that draw - `start` and the board - fail open, because one malformed file
# must not take away the page that says what is happening. `defects list` is the
# command that refuses, with the error itself. This code says which happened.
RECORD_UNREADABLE = 4

# How far back `clowder start` shows defects a person has just added. A near-duplicate
# is caught by eye, not by matching: a reworded line is a new id by design.
RECENT_DEFECT_DAYS = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Dispatch and state for a crew of coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    parser.add_argument("--config", metavar="PATH", help="config file to use")
    parser.add_argument("--state", metavar="PATH", help="state file to use (beats the config)")

    # The same two options on every subcommand, so `clowder tasks --state x` works
    # as well as `clowder --state x tasks`. SUPPRESS matters: without it the
    # subparser's default would overwrite a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS)
    common.add_argument("--state", metavar="PATH", default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser(
        "dispatch", help="send a brief to an agent and record it", parents=[common]
    )
    send.add_argument("agent", help="agent name, as the multiplexer knows it")
    send.add_argument("repo", help="repo name under the workspace root, or a path")
    send.add_argument(
        "brief",
        nargs="*",
        help="the brief; every remaining word is joined into one message",
    )
    send.add_argument("--brief-file", metavar="PATH", help="read the brief from a file instead")
    send.add_argument(
        "--question",
        metavar="TEXT",
        help="the question this task answers, if it is not the brief's first line",
    )
    send.add_argument("--worktree", metavar="PATH", help="working directory for the task")
    send.add_argument(
        "--job",
        metavar="ID",
        help="the line of work this step belongs to; sets the directory and branch",
    )
    send.add_argument("--shape", choices=SHAPES, default="ship", help="default: ship")
    send.add_argument(
        "--cost",
        choices=box.COSTS,
        metavar="CLASS",
        help="what the step costs the box: "
        + ", ".join(box.COSTS)
        + " (default: none). The gate compares the class footprint with what the "
        "box has free",
    )
    send.add_argument(
        "--heavy",
        action="store_true",
        help="the old spelling of --cost suite: a step that installs, builds or runs a suite",
    )
    send.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, metavar="SECONDS")
    send.add_argument(
        "--wait", action="store_true", help="block until the agent is idle or blocked"
    )
    send.add_argument(
        "--until",
        action="append",
        default=[],
        choices=["idle", "working", "blocked", "done", "unknown"],
        metavar="STATE",
        help="with --wait, the state to wait for; repeatable",
    )
    send.add_argument(
        "--from",
        dest="sender",
        metavar="NAME",
        help="who is dispatching; goes into the marker (default: the front door)",
    )
    send.add_argument(
        "--no-marker",
        action="store_true",
        help="send the bare brief, with no job marker line",
    )
    send.add_argument(
        "--force",
        action="store_true",
        help="send even if the multiplexer does not list this agent",
    )
    send.add_argument("--dry-run", action="store_true", help="print the command, send nothing")
    send.add_argument("--json", action="store_true", help="machine-readable output")
    send.set_defaults(handler=cmd_dispatch, refreshes_board=True)

    list_tasks = sub.add_parser("tasks", help="list tasks and their state", parents=[common])
    list_tasks.add_argument("--status", choices=STATUSES)
    list_tasks.add_argument("--agent")
    list_tasks.add_argument("--repo")
    list_tasks.add_argument("--open", action="store_true", help="only tasks with no answer yet")
    list_tasks.add_argument("--json", action="store_true")
    list_tasks.add_argument(
        "--fields",
        metavar="NAMES",
        help="for --json: only these task fields, comma-separated (e.g. id,status,agent)",
    )
    list_tasks.set_defaults(handler=cmd_tasks)

    inbox = sub.add_parser(
        "inbox", help="what has reported since you last looked", parents=[common]
    )
    inbox.add_argument("--json", action="store_true")
    inbox.set_defaults(handler=cmd_inbox, refreshes_board=True)

    show = sub.add_parser("report", help="read one task's report", parents=[common])
    show.add_argument("id", help="task id, e.g. t-0001")
    show.add_argument(
        "--no-save", action="store_true", help="print only; do not update the record"
    )
    show.add_argument(
        "--open-decision",
        metavar="TEXT",
        help="record an open question for this task; a pass uses job pass",
    )
    show.add_argument(
        "--decide",
        metavar="TEXT",
        help="answer the task's open decision, so it leaves the board's owner section",
    )
    show.add_argument("--verbose", action="store_true", help="show the usage breakdown")
    show.add_argument(
        "--full",
        action="store_true",
        help="print the whole answer, not the first lines with a --full hint",
    )
    show.add_argument("--json", action="store_true")
    show.add_argument(
        "--fields",
        metavar="NAMES",
        help="for --json: only these task fields, comma-separated (e.g. id,status,agent)",
    )
    show.set_defaults(handler=cmd_report, refreshes_board=True)

    owner = sub.add_parser(
        "owner",
        help="record or clear one thing that waits on the owner",
        parents=[common],
    )
    owner.add_argument("id", help="task id, e.g. t-0001")
    owner.add_argument(
        "--item",
        metavar="TEXT",
        help="one line, in his words: what it is and where he does it",
    )
    owner.add_argument(
        "--detail",
        metavar="TEXT",
        help="the full body: the options, what each costs, and the recommendation",
    )
    owner.add_argument(
        "--detail-file",
        metavar="PATH",
        help="read the detail body from a file, for a long one",
    )
    owner.add_argument("--clear", action="store_true", help="he has answered it; remove it")
    owner.add_argument("--json", action="store_true")
    owner.set_defaults(handler=cmd_owner, refreshes_board=True)

    step = sub.add_parser("step", help="act on one dispatched step", parents=[common])
    step_sub = step.add_subparsers(dest="step_command", required=True)
    s_abandon = step_sub.add_parser(
        "abandon",
        help="record a dead step as abandoned, and free its job",
        parents=[common],
    )
    s_abandon.add_argument("id", help="task id, e.g. t-0001")
    s_abandon.add_argument("--why", metavar="TEXT", help="why it is abandoned (required)")
    s_abandon.add_argument(
        "--by", metavar="NAME", help="who is doing it (goes in the audit line)"
    )
    s_abandon.add_argument("--json", action="store_true")
    s_abandon.set_defaults(handler=cmd_step_abandon, refreshes_board=True)

    s_close = step_sub.add_parser(
        "close",
        help="close a dead step that can never report, keeping its answer",
        parents=[common],
    )
    s_close.add_argument("id", help="task id, e.g. t-0001")
    s_close.add_argument("--why", metavar="TEXT", help="why it is closed (required)")
    s_close.add_argument(
        "--by", metavar="NAME", help="who is doing it (goes in the audit line)"
    )
    s_close.add_argument("--json", action="store_true")
    s_close.set_defaults(handler=cmd_step_close, refreshes_board=True)

    s_report = step_sub.add_parser(
        "report",
        help="close a finished step at the place it reports, keeping the answer",
        parents=[common],
    )
    s_report.add_argument("id", help="task id, e.g. t-0001")
    s_report.add_argument(
        "--headline",
        required=True,
        metavar="TEXT",
        help="one line the reader sees first: what the step found (required)",
    )
    s_report.add_argument(
        "--answer-file",
        metavar="PATH",
        help="the whole answer, from a file the worker writes",
    )
    s_report.add_argument(
        "--by", metavar="NAME", help="who is reporting (default: the step's agent)"
    )
    s_report.add_argument("--json", action="store_true")
    s_report.set_defaults(handler=cmd_step_report, refreshes_board=True)

    make = sub.add_parser(
        "ensure",
        help="make sure an agent serves a repo, creating one when there is none",
        parents=[common],
    )
    make.add_argument("repo", help="repo name under the workspace root, or a path")
    make.add_argument(
        "--role",
        default=DEFAULT_ROLE,
        choices=ROLES,
        help=f"the part of the crew this agent plays (default: {DEFAULT_ROLE})",
    )
    make.add_argument("--kind", default=DEFAULT_KIND, help="agent kind (default: pi)")
    make.add_argument(
        "--name",
        help="ask for this agent name instead of deriving one from the repo and role",
    )
    make.add_argument("--direction", choices=("right", "down"), default=DEFAULT_DIRECTION)
    make.add_argument(
        "--split",
        action="store_true",
        help="leave the new pane beside this one instead of opening it as a new tab",
    )
    make.add_argument(
        "--no-create", action="store_true", help="report what is missing, make nothing"
    )
    make.add_argument("--json", action="store_true")
    make.set_defaults(handler=cmd_ensure, refreshes_board=True)

    roster = sub.add_parser(
        "agents", help="list live agents (the roster is the tool's)", parents=[common]
    )
    roster.add_argument("--json", action="store_true")
    roster.set_defaults(handler=cmd_agents)

    agent = sub.add_parser("agent", help="act on one live agent's pane", parents=[common])
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    a_reset = agent_sub.add_parser(
        "reset", help="give an agent a fresh context, and prove it happened", parents=[common]
    )
    a_reset.add_argument("name", help="the agent, as the multiplexer knows it")
    a_reset.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="how long to wait for the session to change (default: 30)",
    )
    a_reset.add_argument(
        "--force", action="store_true", help="reset even with an unreported step"
    )
    a_reset.add_argument("--json", action="store_true")
    a_reset.set_defaults(handler=cmd_agent_reset, refreshes_board=True)

    a_reseed = agent_sub.add_parser(
        "reseed",
        help="seed the crew git identity into a live pane and resume its Pi session",
        parents=[common],
    )
    a_reseed.add_argument("name", help="the agent, as the multiplexer knows it")
    a_reseed.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help=(
            "the budget for each wait, not for the whole command: quit, export, "
            "start and ready each get this many seconds (default: 60, because one "
            "slow start can take 45s; four waits can take about four minutes)"
        ),
    )
    a_reseed.add_argument(
        "--force", action="store_true", help="reseed even with an unreported step"
    )
    a_reseed.add_argument("--json", action="store_true")
    a_reseed.set_defaults(handler=cmd_agent_reseed, refreshes_board=True)

    a_rename = agent_sub.add_parser(
        "rename",
        help="rename a live agent in the multiplexer, its space, the records and the docs",
        parents=[common],
    )
    a_rename.add_argument("name", help="the agent's current name, as the multiplexer knows it")
    a_rename.add_argument("--to", required=True, metavar="NEW", help="the new name to give it")
    a_rename.add_argument("--json", action="store_true")
    a_rename.set_defaults(handler=cmd_agent_rename, refreshes_board=True)

    checkouts = sub.add_parser(
        "checkouts", help="how far behind its remote each checkout is", parents=[common]
    )
    checkouts.add_argument(
        "repo", nargs="*", help="repos to check (default: the repos of known jobs and steps)"
    )
    checkouts.add_argument(
        "--fetch", action="store_true", help="fetch before comparing, so the count is fresh"
    )
    checkouts.add_argument("--json", action="store_true")
    checkouts.set_defaults(handler=cmd_checkouts)

    board = sub.add_parser(
        "board",
        help="write the HTML page of what is queued, underway and waiting",
        parents=[common],
    )
    board.add_argument(
        "--out",
        metavar="PATH",
        help="where to write it (default: next to the state file)",
    )
    board.add_argument(
        "--live",
        action="store_true",
        help="read the live agent roster too (slower: it asks the multiplexer). The "
        "page is drawn from the state file by default",
    )
    board.add_argument("--open", action="store_true", help="open it in your browser")
    board.add_argument("--json", action="store_true")
    board.set_defaults(handler=cmd_board)

    job = sub.add_parser(
        "job", help="a line of work: one branch in an agent's worktree", parents=[common]
    )
    job_sub = job.add_subparsers(dest="job_command", required=True)

    def job_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        return job_sub.add_parser(name, help=help_text, parents=[common])

    j_open = job_parser("open", "start a branch for a line of work")
    j_open.add_argument("repo")
    j_open.add_argument("--label", required=True, help="what the work is, in a word or three")
    j_open.add_argument(
        "--title",
        metavar="TEXT",
        help="the change named for the owner, in his words (default: the label)",
    )
    j_open.add_argument(
        "--effect",
        metavar="TEXT",
        help="one line on what it changes for him or a user (default: the branch)",
    )
    # The job's own `--label` is the branch name, so the reserved tag families
    # (`sev:`, `phase:`) are set with a repeatable `--tag` here.
    j_open.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        metavar="LABEL",
        help="a label such as sev:high or phase:pre-launch; repeatable",
    )
    j_open.add_argument("--role", default=DEFAULT_ROLE, choices=ROLES)
    j_open.add_argument("--name", help="use or make an agent with this name")
    j_open.add_argument("--branch", help="branch name (default: job prefix plus label)")
    j_open.add_argument("--base", help="what to branch from (default: the base branch)")
    j_open.add_argument("--kind", default=DEFAULT_KIND)
    j_open.add_argument("--direction", choices=("right", "down"), default=DEFAULT_DIRECTION)
    j_open.add_argument(
        "--split",
        action="store_true",
        help="leave the new pane beside this one instead of opening it as a new tab",
    )
    j_open.add_argument(
        "--force",
        action="store_true",
        help="switch anyway, on a dirty tree or in the main checkout",
    )
    j_open.add_argument("--json", action="store_true")
    j_open.set_defaults(handler=cmd_job_open, refreshes_board=True)

    j_close = job_parser("close", "return the worktree to the agent's branch")
    j_close.add_argument("id")
    j_close.add_argument(
        "--delete-branch",
        action="store_true",
        help="also delete the job branch, if git agrees it is merged",
    )
    j_close.add_argument("--force", action="store_true")
    j_close.add_argument("--json", action="store_true")
    j_close.set_defaults(handler=cmd_job_close, refreshes_board=True)

    j_list = job_parser("list", "jobs, and where their branches live")
    j_list.add_argument("--all", action="store_true", help="closed jobs too")
    j_list.add_argument("--json", action="store_true")
    j_list.set_defaults(handler=cmd_job_list)

    j_hand = job_parser("handover", "give a reviewer the job's saved code to check")
    j_hand.add_argument("id")
    j_hand.add_argument(
        "--to",
        default="verifier",
        choices=ROLES,
        help="the role that checks it (default: verifier)",
    )
    j_hand.add_argument("--name", help="use or make a reviewer with this name")
    j_hand.add_argument("--kind", default=DEFAULT_KIND)
    j_hand.add_argument("--direction", choices=("right", "down"), default=DEFAULT_DIRECTION)
    j_hand.add_argument(
        "--split",
        action="store_true",
        help="leave the new pane beside this one instead of opening it as a new tab",
    )
    j_hand.add_argument(
        "--no-sync",
        action="store_true",
        help="do not merge origin/<base> into the branch before the freeze",
    )
    j_hand.add_argument("--force", action="store_true")
    j_hand.add_argument("--json", action="store_true")
    j_hand.set_defaults(handler=cmd_job_handover, refreshes_board=True)

    j_pin = job_parser("pin", "pin a job's saved commit into a reviewer's copy")
    j_pin.add_argument("id")
    j_pin.add_argument(
        "--to",
        default="verifier",
        choices=ROLES,
        help="the role that checks it (default: verifier)",
    )
    j_pin.add_argument("--name", help="use or make a reviewer with this name")
    j_pin.add_argument(
        "--commit", metavar="SHA", help="the save to pin (default: the job's reviewed commit)"
    )
    j_pin.add_argument("--kind", default=DEFAULT_KIND)
    j_pin.add_argument("--direction", choices=("right", "down"), default=DEFAULT_DIRECTION)
    j_pin.add_argument(
        "--split",
        action="store_true",
        help="leave the new pane beside this one instead of opening it as a new tab",
    )
    j_pin.add_argument("--force", action="store_true")
    j_pin.add_argument("--json", action="store_true")
    j_pin.set_defaults(handler=cmd_job_pin, refreshes_board=True)

    j_pass = job_parser("pass", "record the owner's pass for a job")
    j_pass.add_argument("id")
    j_pass.add_argument("--shown", required=True, help="what the owner was shown")
    j_pass.add_argument("--answer", required=True, help="the owner's answer, in his words")
    j_pass.add_argument("--by", required=True, metavar="NAME", help="who gave the pass")
    j_pass.add_argument("--json", action="store_true")
    j_pass.set_defaults(handler=cmd_job_pass, refreshes_board=True)

    j_word = job_parser("word", "record the owner's merge word for a job")
    j_word.add_argument("id")
    j_word.add_argument("--word", required=True, help="the owner's merge word, in his words")
    j_word.add_argument("--by", required=True, metavar="NAME", help="who gave the merge word")
    j_word.add_argument("--json", action="store_true")
    j_word.set_defaults(handler=cmd_job_word, refreshes_board=True)

    j_sync = job_parser("sync", "merge origin/<base> into the job branch, without rewriting it")
    j_sync.add_argument("id")
    j_sync.add_argument("--json", action="store_true")
    j_sync.set_defaults(handler=cmd_job_sync, refreshes_board=True)

    j_publish = job_parser("publish", "push the job's branch, once the owner has passed it")
    j_publish.add_argument("id")
    j_publish.add_argument("--json", action="store_true")
    j_publish.set_defaults(handler=cmd_job_publish, refreshes_board=True)

    j_park = job_parser(
        "park", "free the space of a published job that waits on the merge word"
    )
    j_park.add_argument("id")
    j_park.add_argument(
        "--by", metavar="NAME", help="who is parking it (goes in the audit line)"
    )
    j_park.add_argument("--json", action="store_true")
    j_park.set_defaults(handler=cmd_job_park, refreshes_board=True)

    j_merge = job_parser("merge", "merge the job's pull request, once the owner has said so")
    j_merge.add_argument("id")
    j_merge.add_argument("--pr", required=True, help="the pull request number or URL")
    j_merge.add_argument("--json", action="store_true")
    j_merge.set_defaults(handler=cmd_job_merge, refreshes_board=True)

    queue = sub.add_parser(
        "queue",
        help="decided-but-unsent work, kept where a restart cannot lose it",
        parents=[common],
    )
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)

    def queue_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        return queue_sub.add_parser(name, help=help_text, parents=[common])

    q_add = queue_parser("add", "record something decided but not sent yet")
    q_add.add_argument("repo")
    q_add.add_argument("brief", nargs="*", help="the brief; every remaining word is joined")
    q_add.add_argument("--brief-file", metavar="PATH")
    q_add.add_argument("--agent", help="the agent it is for")
    q_add.add_argument("--role", choices=ROLES, help="the role it is for")
    q_add.add_argument("--why", required=True, help="why it is not sent yet")
    q_add.add_argument("--question", metavar="TEXT")
    q_add.add_argument("--shape", choices=SHAPES, default="ship")
    q_add.add_argument(
        "--cost",
        choices=box.COSTS,
        metavar="CLASS",
        help="what the step costs the box: "
        + ", ".join(box.COSTS)
        + " (default: none); travels with the item to the dispatch",
    )
    q_add.add_argument(
        "--heavy",
        action="store_true",
        help="the old spelling of --cost suite: a step that installs, builds or runs a suite",
    )
    # `queue add` has no branch label, so both names work here; `--tag` matches
    # `job open`, where `--label` is already the branch name.
    q_add.add_argument(
        "--label",
        "--tag",
        action="append",
        default=[],
        dest="labels",
        metavar="LABEL",
        help="a label such as sev:high or phase:pre-launch; repeatable",
    )
    q_add.add_argument("--job", metavar="ID", help="the job it belongs to, if any")
    q_add.add_argument("--json", action="store_true")
    q_add.set_defaults(handler=cmd_queue_add, refreshes_board=True)

    q_list = queue_parser("list", "what is decided and still waiting to go")
    q_list.add_argument("--json", action="store_true")
    q_list.set_defaults(handler=cmd_queue_list)

    q_send = queue_parser("send", "send one queued item, once its block has cleared")
    q_send.add_argument("id")
    q_send.add_argument(
        "--job",
        metavar="ID",
        help="give or correct the job it belongs to, if any",
    )
    q_send.add_argument("--force", action="store_true")
    q_send.add_argument("--dry-run", action="store_true")
    q_send.add_argument("--json", action="store_true")
    q_send.set_defaults(handler=cmd_queue_send, refreshes_board=True)

    q_drop = queue_parser("drop", "drop an item that is already done or will never be sent")
    q_drop.add_argument("id")
    q_drop.add_argument("--why", metavar="TEXT", help="why it is dropped (required)")
    q_drop.add_argument(
        "--by", metavar="NAME", help="who is dropping it (required; goes in the audit line)"
    )
    q_drop.add_argument("--json", action="store_true")
    q_drop.set_defaults(handler=cmd_queue_drop, refreshes_board=True)

    paths = sub.add_parser("config", help="show resolved settings and paths", parents=[common])
    paths.add_argument("--json", action="store_true")
    paths.set_defaults(handler=cmd_config)

    box_cmd = sub.add_parser(
        "box",
        help="what this machine has, and what the steps in flight cost it",
        parents=[common],
        description=(
            "The three pools in plain words: the RAM fitted and the GPU's share of it, "
            "the GPU side, and the CPU side with the page file. Each line names the "
            "metric it came from. It ends with what the cost gate would decide now. "
            "`clowder capacity` would suggest a configured limit; this is a live "
            "reading of the box the crew runs on."
        ),
    )
    box_cmd.add_argument("--json", action="store_true")
    box_cmd.set_defaults(handler=cmd_box)

    start = sub.add_parser(
        "start",
        help="what a fresh front door reads first: the handover, decisions, "
        "work, queue, defects",
        parents=[common],
    )
    start.add_argument("--json", action="store_true")
    start.set_defaults(handler=cmd_start)

    defects = sub.add_parser(
        "defects",
        help="the one record of known defects, quirks and workarounds",
        parents=[common],
    )
    defects_sub = defects.add_subparsers(dest="defects_command", required=True)

    def defects_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        return defects_sub.add_parser(name, help=help_text, parents=[common])

    d_add = defects_parser("add", "record one sighting of a defect, or add it")
    d_add.add_argument(
        "--line",
        required=True,
        metavar="TEXT",
        help="the defect in one line, in the owner's words (required)",
    )
    d_add.add_argument("--job", metavar="ID", help="the job that fixes it, when one is open")
    # `--state` is the state file on every command, so a defect's own state is
    # `--status`. The record keeps the field name `state`.
    d_add.add_argument(
        "--status",
        choices=DEFECT_STATES,
        help="the defect's state: open (default), has-job, fixed, accepted-workaround",
    )
    d_add.add_argument(
        "--reason",
        metavar="TEXT",
        help="why it is accepted - required with --status accepted-workaround",
    )
    # Seeding: a defect known from an older record, not seen just now. The dates travel
    # with --status, so a seed always says where it stands today.
    d_add.add_argument(
        "--first-seen",
        metavar="DATE",
        help="when it was first recorded (a date or ISO timestamp); needs --status",
    )
    d_add.add_argument(
        "--last-seen",
        metavar="DATE",
        help="when it was last recorded; needs --first-seen and --status",
    )
    d_add.add_argument(
        "--count",
        type=int,
        metavar="N",
        help="the recorded count of sightings; needs --first-seen",
    )
    d_add.add_argument(
        "--detail",
        metavar="TEXT",
        help="the body behind the one line: workaround, citations, what was seen",
    )
    d_add.add_argument(
        "--detail-file",
        metavar="PATH",
        help="read the detail body from a file, for a long one",
    )
    d_add.add_argument("--json", action="store_true")
    d_add.set_defaults(handler=cmd_defects_add, refreshes_board=True)

    d_list = defects_parser("list", "every known defect, or only the ones needing action")
    d_list.add_argument(
        "--repeated",
        action="store_true",
        help="only defects seen twice or more with no open job",
    )
    d_list.add_argument(
        "--like",
        metavar="WORDS",
        help="only defects whose line holds every one of these words (exact words; "
        "a total rephrase is not found, so read the list too)",
    )
    d_list.add_argument("--json", action="store_true")
    d_list.set_defaults(handler=cmd_defects_list)

    d_show = defects_parser("show", "one defect, in full")
    d_show.add_argument("id", help="defect id, e.g. d-0001")
    d_show.add_argument("--json", action="store_true")
    d_show.set_defaults(handler=cmd_defects_show)

    handover = sub.add_parser(
        "handover", help="write the generated handover a fresh session reads", parents=[common]
    )
    handover_sub = handover.add_subparsers(dest="handover_command", required=True)
    h_write = handover_sub.add_parser(
        "write",
        help="compose the handover from the record and name the file it wrote",
        parents=[common],
    )
    h_write.add_argument("--json", action="store_true")
    h_write.set_defaults(handler=cmd_handover_write)

    state = sub.add_parser("state", help="inspect or repair the state file", parents=[common])
    state_sub = state.add_subparsers(dest="state_command", required=True)
    s_repair = state_sub.add_parser(
        "repair",
        help="drop a field no copy knows, with a backup and an audit line",
        parents=[common],
    )
    s_repair.add_argument("--drop-unknown", metavar="FIELD", help="the unknown field to remove")
    s_repair.add_argument(
        "--backup", metavar="PATH", help="write a copy of the state file here first"
    )
    s_repair.add_argument("--why", metavar="TEXT", help="why it is being dropped")
    s_repair.add_argument(
        "--by", metavar="NAME", help="who is doing it (goes in the audit line)"
    )
    s_repair.add_argument("--json", action="store_true")
    s_repair.set_defaults(handler=cmd_state_repair, refreshes_board=True)

    return parser


# -- helpers ---------------------------------------------------------------


def _context(args: argparse.Namespace) -> tuple[Config, StateStore]:
    config = load_config(args.config)
    state_path = args.state or config.state_path
    return config, StateStore(state_path)


def _read_brief(args: argparse.Namespace) -> str:
    if args.brief_file:
        if args.brief:
            raise UsageError("pass a brief or --brief-file, not both")
        path = Path(args.brief_file)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read {path}: {exc}") from exc
        if not text.strip():
            raise UsageError(f"{path} is empty")
        return text.strip()
    if not args.brief:
        raise UsageError("a brief is required. Pass words, or --brief-file PATH")
    return " ".join(args.brief).strip()


def _first_line(text: str, limit: int = 120) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= limit else stripped[: limit - 1] + "..."
    return text[:limit]


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "..."


def _column(rows: list[list[str]], gap: str = "  ") -> str:
    if not rows:
        return ""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = [row[i].ljust(widths[i]) for i in range(len(row))]
        lines.append(gap.join(cells).rstrip())
    return "\n".join(lines)


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))


def _select_fields(item: dict, fields: str | None) -> dict:
    """Keep only the named fields, for a smaller --json record.

    A task record is around thirty fields. A reader that wants three of them should
    not have to receive the rest. An unknown name is refused rather than ignored,
    so a typo cannot silently drop a field.
    """
    if not fields:
        return item
    wanted = [name.strip() for name in fields.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in item]
    if unknown:
        known = ", ".join(sorted(item))
        raise UsageError(f"unknown field(s): {', '.join(unknown)}. Known fields: {known}")
    return {name: item[name] for name in wanted}


# -- commands --------------------------------------------------------------


def cmd_dispatch(args: argparse.Namespace) -> int:
    config, store = _context(args)
    return _dispatch(args, config, store)


def _dispatch(args: argparse.Namespace, config: Config, store: StateStore) -> int:
    brief = _read_brief(args)
    question = (args.question or "").strip() or _first_line(brief)

    if args.shape.lower() == "ship" and not args.job and not args.worktree:
        # A ship's deliverable is a save on a job's branch, so it needs a place.
        # This sits ahead of every read and every delivery: nothing is sent before
        # it is refused. `--force` does not bypass it - a deliberate place is
        # `--worktree`, which is a name rather than an override. A scout changes
        # nothing, so it needs no place.
        raise UsageError(
            f"a ship step needs a place and this one has neither: {args.agent} would save "
            "a change that belongs on a job's branch or in a folder. Pass `--job <job-id>` "
            "to attach it to a job, or `--worktree <path>` when the folder is deliberate. "
            "A scout changes nothing, so it needs no place."
        )

    repo_path = config.resolve_repo(args.repo)
    worktree = str(Path(args.worktree).expanduser().absolute()) if args.worktree else None
    if worktree and not Path(worktree).is_dir():
        raise UsageError(f"worktree is not a directory: {worktree}")

    job = None
    if args.job:
        if args.worktree:
            raise UsageError("pass --job or --worktree, not both; a job knows its directory")
        job = store.get_job(args.job)
        if not job.is_open:
            raise DispatchError(
                f"{job.id} is closed. Open a job for this work, or dispatch without --job."
            )
        if job.is_parked:
            # Its checkout was released on purpose and may hold the next job now, so a
            # step here would either read the wrong code or take the space back.
            raise DispatchError(
                f"{job.id} is parked: {job.branch} waits on the owner's merge word and "
                "its space belongs to whatever came next. There is nothing to send to "
                "it. Once the word is recorded, merge it with "
                f"`{PROGRAM} job merge {job.id} --pr NUMBER`."
            )
        who = [name for name in (job.agent, job.reviewer) if name]
        if args.agent not in who:
            checked = f", checked by {job.reviewer}" if job.reviewer else ""
            raise DispatchError(
                f"{job.id} is {job.agent}'s job{checked}, and you named "
                f"{args.agent}. The writer and its reviewer take the steps."
            )
        worktree = job.worktree

    mux = Mux(
        config.mux_bin,
        config.mux_prompt_argv,
        bus_send_argv=config.bus_send_argv,
        bus_peers_argv=config.bus_peers_argv,
    )

    cost = _cost_of_args(args)
    agent_info = None
    if not args.force and not args.dry_run:
        agent_info = _lookup_agent(mux, args.agent)
        _refuse_cross_repo(args, agent_info, repo_path)
        _refuse_open_work(args, agent_info, store, job)
        if cost != box.COST_NONE:
            _refuse_high_load(config, args)
        if job is not None:
            _refuse_outside_job(args, agent_info, job, store)

    # The cost gate is measured even under --force, so a forced send can say in the
    # audit line what it overrode. A step with no stated cost is not measured at all:
    # an unmarked step must never be blocked by a machine check.
    overridden: str | None = None
    if not args.dry_run and cost != box.COST_NONE:
        refusal = _headroom_refusal(config, cost, store, args.agent)
        if refusal is not None:
            if not args.force:
                raise DispatchError(refusal)
            overridden = refusal

    # A fix step on a handed-over job must run on the branch, not the base the
    # writer's space was released to. This is a fix, not a guard, so --force does
    # not skip it; --dry-run must not move the space.
    if job is not None and not args.dry_run:
        _restore_writer_branch(job, agent_info, args)

    task = Task(
        id=store.next_id(),
        question=question,
        brief=brief,
        shape=args.shape,
        agent=args.agent,
        repo=args.repo,
        repo_path=str(repo_path),
        worktree=worktree,
        pane_id=agent_info.pane_id if agent_info else None,
        agent_session=agent_info.session_path if agent_info else None,
        cost=cost,
        heavy=box.heavy_flag(cost),
    )
    if job is not None:
        task.job = job.id
        task.branch = job.branch
        # The folder the step actually ran in. For the writer that is the job's own
        # space; for a reviewer it is the reviewer's space, holding the pinned save.
        # A report reads this folder, so recording the wrong one reads the wrong code.
        if agent_info is not None and agent_info.cwd:
            task.worktree = agent_info.cwd
        if job.reviewer == args.agent and job.review_commit:
            # A review is about one save, so it names that save, not wherever the
            # writer has moved on to since.
            where = agent_info.cwd if agent_info is not None else job.worktree
            task.commit = gitcmd.commit_of(where, job.review_commit, short=True)
        else:
            task.commit = gitcmd.head_commit(task.worktree or job.worktree)
    elif not args.worktree and agent_info is not None and agent_info.cwd:
        # With no job and no stated worktree the step still ran in the agent's own
        # directory. Leaving it empty recorded the step against the main checkout,
        # which is not where it ran - so a space holding an open job read as though
        # the step had landed on the user's own.
        task.worktree = agent_info.cwd

    # The marker is composed here, not by the front door, so it cannot be forgotten.
    sender = (args.sender or config.front_door_name or DEFAULT_SENDER).strip()
    use_marker = config.dispatch_marker and not args.no_marker
    task.sender = sender if use_marker else None
    message = (
        apply_marker(brief, task.id, args.shape, args.repo, sender) if use_marker else brief
    )

    if args.dry_run:
        argv = mux.build_prompt_argv(args.agent, message)
        task.mux_argv = argv
        if args.json:
            _emit_json({"dry_run": True, "argv": argv, "task": task.to_dict()})
        else:
            print(f"would run:\n{INDENT}{_quote(argv)}")
            print(f"repo: {repo_path}")
            print(f"worktree: {worktree or '(main checkout)'}")
        return 0

    if args.shape.lower() not in brief.lower():
        print(
            f"{PROGRAM}: note: the brief does not say {args.shape!r}. "
            "Every brief states ship or scout.",
            file=sys.stderr,
        )

    result = _deliver(mux, args, config, message, agent_info)
    task.mux_argv = list(result.argv)
    task.mux_returncode = result.returncode
    task.dispatched_at = now_iso()
    if result.ok:
        task.status = DISPATCHED
    else:
        task.status = FAILED
        task.mux_error = result.error_text()

    store.add(task)
    store.save()

    if overridden is not None:
        # A forced send is a person overruling a measurement, so the record says so.
        # Without this line the state would show a step that the box refused to take,
        # and no reason why it went anyway.
        audit_file = audit.append_audit(
            store.path,
            {
                "action": "dispatch-forced",
                "at": task.dispatched_at,
                "task": task.id,
                "agent": task.agent,
                "cost": cost,
                "forced": True,
                "refused": overridden,
            },
        )
        print(
            f"{PROGRAM}: warning: the box refused this step and --force sent it anyway "
            f"({audit_file.name})",
            file=sys.stderr,
        )
        print(overridden, file=sys.stderr)

    if args.json:
        _emit_json({"task": task.to_dict(), "ok": result.ok})
    elif result.ok:
        print(
            f"{task.id} sent to {task.agent} "
            f"({task.repo}, {_where(task)}), asked: {_clip(task.question, 70)}"
        )
    else:
        print(
            f"{PROGRAM}: dispatch failed: {result.error_text()}",
            file=sys.stderr,
        )
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        print(f"{PROGRAM}: recorded as {task.id} ({FAILED})", file=sys.stderr)

    return 0 if result.ok else 2


def _deliver(
    mux: Mux,
    args: argparse.Namespace,
    config: Config,
    message: str,
    agent_info: AgentInfo | None,
) -> MuxResult:
    """Send one brief: over the agent bus when it can, else typed under the guard.

    A bus send never touches the pane's input box, so it cannot collide with the
    owner. The typed fallback always reads the box first.
    """
    cwd = agent_info.cwd if agent_info is not None else None
    if config.bus_send_argv:
        joined = mux.is_bus_joined(args.agent, cwd)
        if joined is True:
            try:
                return mux.bus_send(args.agent, message, timeout_s=args.timeout)
            except MuxError as exc:
                # The configured bus command is missing or unrunnable. Refuse
                # rather than silently type: the config promised the bus.
                raise DispatchError(f"cannot send over the agent bus: {exc}") from exc
        if joined is None:
            print(
                f"{PROGRAM}: warning: cannot read the bus membership; typing the "
                "brief under the input-box guard",
                file=sys.stderr,
            )
    _refuse_if_input_dirty(mux, args.agent, config, args.force, "the brief")
    return mux.prompt(
        args.agent,
        message,
        timeout_s=args.timeout,
        wait=args.wait,
        until=args.until,
    )


def _refuse_if_input_dirty(
    mux: Mux,
    name: str,
    config: Config,
    force: bool,
    what: str,
) -> None:
    """Never type while text the owner left sits in the same input box.

    The multiplexer writes a sent message into the pane's own input box. The
    owner writes there too, so a non-empty box means the two texts would merge.
    Wait briefly for a mid-keystroke to clear, then refuse. A box that cannot be
    read is refused as well: typing blind is the collision this prevents.
    """
    if force:
        return
    deadline = time.monotonic() + max(0.0, config.dispatch_input_wait)
    while True:
        line = mux.read_input(name)
        if line is None:
            raise DispatchError(
                f"cannot read {name}'s input box, so {what} would be typed blind and "
                "could merge with text the owner left there. Check the pane, or pass "
                "--force to send anyway."
            )
        if not line.strip():
            return
        if time.monotonic() >= deadline:
            raise DispatchError(
                f"{name}'s input box is not empty, so {what} would merge with: "
                f"{_clip(line, 60)!r}. Send or clear it, then try again - or pass "
                "--force to type anyway."
            )
        time.sleep(min(0.25, max(0.05, deadline - time.monotonic())))


def _where(task: Task) -> str:
    return f"worktree {Path(task.worktree).name}" if task.worktree else "main checkout"


def _lookup_agent(mux: Mux, name: str):
    """Refuse to send into the void. A wrong name must fail loudly, not silently."""
    try:
        agents = mux.list_agents()
    except MuxError as exc:
        print(
            f"{PROGRAM}: warning: cannot read the live agent list ({exc}); sending anyway",
            file=sys.stderr,
        )
        return None
    for agent in agents:
        if agent.name == name:
            return agent
    live = ", ".join(sorted(a.name for a in agents)) or "none"
    raise DispatchError(
        f"no agent named {name!r}. Live agents: {live}. "
        f"Run `{PROGRAM} ensure <repo> --role <role>` to make one in a repo."
    )


def _refuse_cross_repo(args: argparse.Namespace, agent, repo_path) -> None:
    """An agent serves the repo its pane was opened in, and no other.

    A pane's directory is fixed when the pane is made, so its shell, its context
    files and its skills all belong to that repo. Sending a repo B job to a repo A
    pane can only produce a wrong answer, so it is refused before anything is sent.
    """
    if agent is None or not agent.cwd:
        return
    if inside(agent.cwd, repo_path):
        return
    raise DispatchError(
        f"{args.agent} is in {agent.cwd}, which is not {repo_path}. "
        f"A pane serves the repo it was opened in. Run "
        f"`{PROGRAM} ensure {args.repo} --role {DEFAULT_ROLE}` to make an agent "
        f"there, or pass --force if you know better."
    )


def _refuse_outside_job(args: argparse.Namespace, agent, job, store: StateStore) -> None:
    """A step runs on the job's code: the writer's branch, or a handed-over save.

    One branch lives in one folder, so a reviewer cannot hold the writer's branch.
    It holds the exact save instead, and this checks that it really does before a
    brief goes out. A step sent to a folder holding different code would produce a
    report about code the job never had.
    """
    if agent is not None and agent.cwd:
        on_the_branch = inside(agent.cwd, job.worktree)
        at_the_save = bool(
            job.review_commit and gitcmd.commit_of(agent.cwd, "HEAD") == job.review_commit
        )
        if not (on_the_branch or at_the_save):
            handed = f" ({job.review_commit[:7]})" if job.review_commit else ""
            raise DispatchError(
                f"{args.agent} is in {agent.cwd}, which holds neither {job.id}'s "
                f"branch ({job.worktree}) nor its handed-over save{handed}. Run "
                f"`{PROGRAM} job handover {job.id} --to {args.agent}` to give it the "
                "code to check."
            )
    open_steps = [task for task in store.all() if task.job == job.id and task.is_open]
    if open_steps:
        listed = ", ".join(f"{task.id} ({task.agent})" for task in open_steps)
        raise DispatchError(
            f"{job.id} already has an open step: {listed}. Wait for it to report "
            "before sending another; two steps at once in one checkout is how two "
            "costly runs start at once in one space."
        )


def _restore_writer_branch(job, agent, args: argparse.Namespace) -> None:
    """Put the writer's space back on the job's branch before a fix step.

    `job handover` releases the writer's space to the base so the reviewer can
    work, and the job stays open. A later fix step sent to the writer would then
    read the base commit and edit the wrong code. Switching the space back to the
    job's branch first is the fix; a space that cannot be switched is refused with
    the exact command. The reviewer path is untouched: it holds the pinned save.
    """
    if args.shape.lower() != "ship" or args.agent != job.agent:
        return
    if job.released_at is None:
        return  # the space was never handed back; it is still on the branch
    folder = agent.cwd if agent is not None and agent.cwd else job.worktree
    if not folder or not inside(folder, job.worktree) or not gitcmd.is_repo(folder):
        return  # not the job's own space; do not move someone else's checkout
    if gitcmd.current_branch(folder) == job.branch:
        return
    try:
        gitcmd.switch_branch(folder, job.branch)
    except GitError as exc:
        raise GitError(
            f"could not put {folder} back on {job.branch}: {exc}. Switch it by hand: "
            f'git -C "{folder}" switch {job.branch}'
        ) from exc


def _cost_of_args(args: argparse.Namespace) -> str:
    """The class this dispatch asks for, with `--heavy` read as `suite`.

    `--heavy` is kept as the alias it always meant, so a brief or a script that
    says `--heavy` costs exactly what a suite costs. Naming both is a mistake, not a
    tie to break: `--heavy --cost browser` says two different things.
    """
    stated = getattr(args, "cost", None)
    heavy = bool(getattr(args, "heavy", False))
    if stated and heavy and stated != box.COST_SUITE:
        raise UsageError(
            f"--heavy is the old name for --cost {box.COST_SUITE}, so it cannot also "
            f"be --cost {stated}. Pass one: `--cost {stated}`."
        )
    if stated:
        return str(stated)
    return box.COST_SUITE if heavy else box.COST_NONE


def _headroom_refusal(config: Config, cost: str, store: StateStore, agent: str) -> str | None:
    """What the box has to say about this step: the measured refusal, or None.

    Two checks, and both say the numbers they compared:

    - the class footprint against the machine's headroom, minus the steps already
      running. That is what lets two suites start together and stops a suite next to
      a model leg: the same comparison, different footprints.
    - the one class that takes the box alone. It is a state gate on purpose: the GPU
      counter is too slow for a gate, so two model legs cannot be measured against
      each other, and the record keeps one at a time.

    A machine that cannot be read refuses nothing. A broken probe must never block
    work, so the gate says it could not measure and lets the step go.
    """
    running = box.steps_in_flight(store.select(open_only=True))
    reading = box.read()
    decision = box.decide(cost, running, reading, config.floors)
    if decision.unmeasured:
        print(
            f"{PROGRAM}: warning: {decision.reason}; the {cost} step was not measured",
            file=sys.stderr,
        )
        return None

    klass = box.COST_CLASSES[cost]
    if klass.alone:
        others = [step for step in running if step.cost != box.COST_NONE]
        if others:
            listed = ", ".join(f"{step.id} ({step.cost})" for step in others)
            return (
                f"a {cost} step takes the box alone, and {listed} is running. The GPU "
                "counter is too slow to put in a gate, so the crew keeps one model leg "
                "at a time by the record. Wait for it to report, or pass --force.\n"
                f"  {box.summary(reading, running, config.floors)}"
            )
    elif any(box.COST_CLASSES[step.cost].alone for step in running):
        holder = next(step for step in running if box.COST_CLASSES[step.cost].alone)
        return (
            f"a {cost} step cannot start while {holder.id} ({holder.cost}) runs: that "
            "class takes the box alone. Wait for it to report, or pass --force.\n"
            f"  {box.summary(reading, running, config.floors)}"
        )

    if decision.allowed:
        return None
    lines = "\n".join(f"  {line}" for line in decision.lines)
    return (
        f"{agent}'s {cost} step did not send: {decision.reason}.\n"
        f"{lines}\n"
        f"  Let it finish, send it as a cheaper class, or pass --force to send it "
        f"anyway. `{PROGRAM} box` prints the whole readout."
    )


def _refuse_high_load(config: Config, args: argparse.Namespace) -> None:
    """Unix only: refuse when the 1-minute load per CPU is at or above the limit.

    Windows has no load average, and its memory is the GiB floors the class gate
    compares. This is kept for the other platform: the config key is still the way a
    Linux box says "not while it is this busy".
    """
    if config.heavy_max_load is None or load.metric_name() != load.METRIC_LOAD:
        return
    reading = load.current()
    if reading is None:
        print(
            f"{PROGRAM}: warning: cannot read the machine's load, so the load gate "
            "did not check it",
            file=sys.stderr,
        )
        return
    _, value = reading
    if value < config.heavy_max_load:
        return
    raise DispatchError(
        f"{args.agent}'s step costs the box and the machine is at load {value:.2f} "
        f"per CPU, at or above the {config.heavy_max_load:g} limit. Let it settle, or "
        "pass --force."
    )


def _refuse_open_work(args: argparse.Namespace, agent, store: StateStore, job) -> None:
    """A send lands in the pane, and the newest message wins.

    A dispatch preempts whatever turn is open in the agent's pane: the newer
    message is typed in and the older turn is dropped. `_refuse_outside_job`
    covers a second step on the same job; this covers the rest - an open step on
    a different job, a no-job step, and an open job whose worktree is the agent's
    own space when the dispatch does not name it. `--force` bypasses it, like the
    other guards.
    """
    name = args.agent
    dispatched = args.job
    open_tasks = [
        task
        for task in store.select(open_only=True)
        if task.agent == name and (dispatched is None or task.job != dispatched)
    ]
    if open_tasks:
        listed = ", ".join(f"{task.id} ({task.job or 'no job'})" for task in open_tasks)
        raise DispatchError(
            f"{name} already holds open work: {listed}. A send lands in the same pane "
            "and the newest message wins, so it would drop that turn. Read it first: "
            f"`{PROGRAM} report {open_tasks[0].id}`, or pass --force."
        )

    if agent is None or not agent.cwd:
        return
    holding = [
        open_job
        for open_job in store.open_jobs()
        if open_job.id != dispatched
        # A released job has handed its space back, so it does not hold a pane. A
        # parked job is released too: its branch waits, its checkout does not.
        and open_job.released_at is None
        and inside(agent.cwd, open_job.worktree)
    ]
    if holding:
        listed = ", ".join(f"{open_job.id} ({open_job.name_in_words})" for open_job in holding)
        raise DispatchError(
            f"{name} is in {agent.cwd}, inside open job {listed}. A send lands in the "
            "same pane and the newest message wins, so it would drop that turn. Close "
            f"it first: `{PROGRAM} job close {holding[0].id}`, or pass --force."
        )


def _quote(argv: list[str]) -> str:
    out = []
    for part in argv:
        if part == "" or " " in part or "\n" in part:
            out.append("'" + part.replace("'", "'\\''") + "'")
        else:
            out.append(part)
    return " ".join(out)


def cmd_tasks(args: argparse.Namespace) -> int:
    _, store = _context(args)
    tasks = store.select(
        status=args.status, agent=args.agent, repo=args.repo, open_only=args.open
    )

    if args.json:
        records = [_select_fields(t.to_dict(), args.fields) for t in tasks]
        _emit_json({"tasks": records, "count": len(tasks)})
        return 0

    if not tasks:
        print("no tasks")
        print(f"\nNext: check what has reported with: {PROGRAM} inbox")
        return 0

    rows = [["ID", "STATUS", "JOB", "AGENT", "REPO", "AGE", "QUESTION"]]
    for task in tasks:
        rows.append(
            [
                task.id,
                task.status,
                task.job or "-",
                task.agent,
                _clip(task.repo, 22),
                human_age(task.age_seconds),
                _clip(task.question, 40),
            ]
        )
    print(_column(rows))
    open_count = sum(1 for t in tasks if t.is_open)
    abandoned = sum(1 for t in tasks if t.is_abandoned)
    closed = sum(1 for t in tasks if t.is_closed)
    unanswered = sum(1 for t in tasks if not t.answer)
    print(
        f"\n{len(tasks)} task(s), {open_count} still open, {abandoned} abandoned, "
        f"{closed} closed, {unanswered} with no answer"
    )
    nxt = next((task for task in tasks if task.is_open and not task.answer), tasks[0])
    print(f"Next: read one with: {PROGRAM} report {nxt.id}")
    return 0


def _agents_by_name(mux: Mux) -> dict[str, AgentInfo] | None:
    """The live roster as a lookup, or None when it could not be read.

    None is not an empty list. A step whose agent is missing is closed on the
    strength of a roster that was read and did not contain it; a roster that could
    not be read at all proves nothing, so nothing is closed on it.
    """
    try:
        return {agent.name: agent for agent in mux.list_agents()}
    except MuxError:
        return None


def _session_and_answer(
    recorded: str | None,
    live: str | None,
    since: float | None,
    until: float | None = None,
) -> tuple[str | None, str | None]:
    """The session a step's answer should be read from, and the answer in it.

    The recorded session wins while it holds an answer for this step: that is the
    session the step was dispatched into, so the answer to it is there. A pane that
    restarted before the worker answered has a recorded session that says nothing
    about this step and the answer in the live one, so the live session is used
    then. When neither holds an answer the recorded pointer is kept, so reading a
    step never moves the pointer to a session that has nothing to say.

    The read is bounded to `[since, until)`, the step's own dispatch window, so a
    later task in the same pane is not read as this one's answer.

    The answer comes back with the path so no caller reads the same file twice;
    `inbox` runs this for every open step.
    """
    if recorded and Path(recorded).exists():
        answer = read_answer(recorded, since, until)
        if answer:
            return recorded, answer
    # The same file twice is the common case while a step has no answer yet, and
    # `inbox` walks every open step, so it is not read twice.
    if live and live != recorded:
        answer = read_answer(live, since, until)
        if answer:
            return live, answer
    return recorded or live, None


def _report_until(store: StateStore, task: Task, since: float | None) -> float | None:
    """Where this step's dispatch window ends: the next task to the same agent.

    A pane is one long-lived session shared by every task sent to it, so a read
    bounded only from below would pick up the next task's answer. The earliest
    task to the same agent whose window starts after this one's bounds the read.
    None when this is the latest task, or the start is unknown.
    """
    if since is None:
        return None
    starts: list[float] = []
    for other in store.all():
        if other.agent != task.agent or other.id == task.id:
            continue
        moment = to_epoch(other.dispatched_at) or to_epoch(other.created_at)
        if moment is not None and moment > since:
            starts.append(moment)
    return min(starts) if starts else None


def _reported_since_look(
    store: StateStore, agents: dict[str, AgentInfo]
) -> list[tuple[Task, str]]:
    """Open steps whose worker has settled and left an answer since dispatch.

    This is the cheap check the front door runs often: it reads the session the
    tool already recorded, so nobody reads each report by hand to find out.
    """
    found: list[tuple[Task, str]] = []
    for task in store.select(open_only=True):
        agent = agents.get(task.agent)
        status = agent.status if agent is not None else None
        if status not in (None, "idle", "done"):
            continue
        since = to_epoch(task.dispatched_at) or to_epoch(task.created_at)
        until = _report_until(store, task, since)
        _, answer = _session_and_answer(
            task.agent_session, agent.session_path if agent else None, since, until
        )
        if answer:
            found.append((task, answer))
    return found


def _close_reported_steps(store: StateStore, agents: dict[str, AgentInfo]) -> list[Task]:
    """Close every open step whose worker has finished and left its answer.

    A worker reports over the agent bus, and this tool cannot read the front
    door's inbox. It does hold the other half of the report: the answer stays in
    the worker's own session, which is what the marker already tells the worker.
    So the step closes on the evidence `inbox` already reads, and nobody has to
    read the whole answer first to clear the record.

    What is recorded is what `report` would have recorded - the answer, the usage
    window, the model, the commit - plus one audit line saying the step reported.
    A step that has already reported is no longer open, so a second run closes
    nothing twice, and a worker that reports twice is closed once.
    """
    closed: list[Task] = []
    for task, answer in _reported_since_look(store, agents):
        agent = agents.get(task.agent)
        session_path = task.agent_session or (agent.session_path if agent else None)
        since, until = _step_window(store, task)
        usage = read_usage(session_path, since, until) if session_path else None
        _record_step_facts(task, usage)
        task.answer = answer
        task.answer_source = "session"
        task.reported_at = now_iso()
        task.closed_at = task.reported_at
        task.close_reason = "the step reported"
        task.status = CLOSED
        closed.append(task)

    if closed:
        store.save()
        for task in closed:
            audit.append_audit(
                store.path,
                {
                    "at": task.closed_at,
                    "by": task.agent,
                    "action": "step-report",
                    "step": task.id,
                    "job": task.job,
                    "agent": task.agent,
                    "reason": task.close_reason,
                    "answer_kept": True,
                },
            )
    return closed


def cmd_inbox(args: argparse.Namespace) -> int:
    config, store = _context(args)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    agents = _agents_by_name(mux)
    roster = agents or {}
    # A step that reported closes itself here, so the next step is not blocked by a
    # record nobody has read yet. This is the same evidence the list below shows,
    # and it is only acted on when the live roster was actually read.
    just_closed = _close_reported_steps(store, roster) if agents is not None else []
    found = [(task, task.answer or "") for task in just_closed]
    found += _reported_since_look(store, roster)

    if args.json:
        _emit_json(
            {
                "reported": [
                    {
                        "id": task.id,
                        "agent": task.agent,
                        "repo": task.repo,
                        "job": task.job,
                        "dispatched_at": task.dispatched_at,
                        "answer": answer,
                    }
                    for task, answer in found
                ],
                "count": len(found),
            }
        )
        return 0

    if not found:
        print("nothing has reported since you last looked")
        return 0

    rows = [["ID", "AGENT", "REPO", "AGE", "ANSWER"]]
    for task, answer in found:
        first = next((line for line in answer.splitlines() if line.strip()), answer)
        rows.append(
            [
                task.id,
                task.agent,
                _clip(task.repo, 22),
                human_age(task.age_seconds),
                _clip(first.strip(), 50),
            ]
        )
    print(_column(rows))
    if just_closed:
        print(
            f"\n{len(just_closed)} step(s) closed on their report, so their jobs are "
            "free for the next step"
        )
    print(f"\n{len(found)} step(s) have reported. Read one with: {PROGRAM} report <id>")
    return 0


def _step_window(store: StateStore, task: Task) -> tuple[float | None, float | None]:
    """The step's own dispatch window `[since, until)`.

    One place, so a step that closes itself on its report reads the same window a
    read of its report would.
    """
    since = to_epoch(task.dispatched_at) or to_epoch(task.created_at)
    return since, _report_until(store, task, since)


def _record_step_facts(task: Task, usage: Usage | None) -> bool:
    """Record what a report is measured by: usage, model and commit.

    Shared by `report` (a read) and `step report` (the step closing itself), so the
    two cannot record different facts about the same step.
    """
    changed = False
    if usage is not None:
        task.usage = usage.to_dict()
        changed = True
        # The model that ran this step, read from its own session. Measurement,
        # not a guard: if the session names no model, nothing is recorded.
        if usage.model and task.model != usage.model:
            task.model = usage.model
            task.provider = usage.provider
            changed = True
    # The directory means different code at different times, so the commit is what
    # a report is really about.
    if task.worktree and gitcmd.is_repo(task.worktree):
        head = gitcmd.head_commit(task.worktree)
        if head and head != task.commit:
            task.commit = head
            changed = True
    return changed


def cmd_report(args: argparse.Namespace) -> int:
    config, store = _context(args)
    task = store.get(args.id)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)

    since, until = _step_window(store, task)
    session_path, session_answer, agent_status = _resolve_session(mux, task, since, until)

    usage = read_usage(session_path, since, until) if session_path else None
    answer = task.answer or session_answer
    # An abandoned step keeps no answer: it died before one came, and reading the
    # session now would capture a half sentence as its result.
    if task.is_abandoned:
        answer = None

    if task.answer:
        source = task.answer_source or "record"
    elif answer:
        source = "session"
    else:
        source = None

    if args.open_decision:
        task.open_decision = args.open_decision

    # An answer only counts as a report when the worker is no longer working.
    settled = agent_status in (None, "idle", "done")
    stranded = False
    if not args.no_save:
        changed = _record_step_facts(task, usage)
        # The reachability gate, where a worker makes its claim. A commit that is on
        # no branch exists only in that folder, and reusing the folder destroys it.
        # The board asks the same questions in the same order, so both answers
        # agree: is the work already merged, did the job move past the commit, and
        # only then is anything on no branch truly lost.
        if task.commit and task.worktree:
            job = store.get_job(task.job) if task.job else None
            stranded = False
            if not _work_has_landed(task, job):
                stranded = gitcmd.commit_risk(task.worktree, task.commit) == gitcmd.LOST
                if stranded:
                    stranded = not _job_moved_past(task, job)
        # A step that closed itself on its report keeps its status: a read is not a
        # second event, and it must not reopen a closed step.
        if (
            answer
            and (task.answer != answer or task.status != REPORTED)
            and settled
            and not task.is_closed
        ):
            task.answer = answer
            task.answer_source = source or "session"
            task.reported_at = now_iso()
            task.status = REPORTED
            changed = True
        if session_path and task.agent_session != session_path:
            task.agent_session = session_path
            changed = True
        if args.open_decision:
            changed = True
        if args.decide:
            task.decision_answer = args.decide
            task.decision_answered_at = now_iso()
            # The decision was on the board; recording his answer clears it, so the
            # list is never longer than the open decisions.
            if task.owner_item is not None:
                task.owner_item = None
                task.owner_item_at = None
                task.owner_detail = None
            changed = True
        if changed:
            store.save()

    if args.json:
        _emit_json(
            {
                "task": _select_fields(task.to_dict(), args.fields),
                "usage": usage.to_dict() if usage else None,
                "answer": answer,
                "answer_source": source,
                "agent_status": agent_status,
                "session_file": session_path,
                "commit_on_no_branch": stranded,
            }
        )
        return 0

    print(
        build_report(
            task,
            usage,
            answer,
            store.get_job(task.job) if task.job else None,
            full=args.full,
        )
    )
    if agent_status == "working":
        print(INDENT + "(the agent is working; this may not be its final word)")
    if stranded:
        print(
            INDENT
            + f"warning: commit {task.commit} is on no branch. It lives only in "
            + f"{task.worktree}, and reusing that space would lose it. Put it on a "
            + f'branch: git -C "{task.worktree}" branch <name> {task.commit}'
        )
    if args.verbose:
        print()
        print(INDENT + "usage:")
        print(usage_breakdown(usage) if usage else INDENT + "no session turns found")
        if session_path:
            print(INDENT + f"session: {session_path}")
    print(_report_next(task))
    return 0


def _report_next(task: Task) -> str:
    """One line naming what the reader most likely does after the report."""
    if task.open_decision:
        return (
            f"\nNext: answer the open decision with: {PROGRAM} report {task.id} --decide TEXT"
        )
    if task.job:
        return f"\nNext: see the job with: {PROGRAM} job list"
    return f"\nNext: see the other steps with: {PROGRAM} tasks"


def _owner_detail(args: argparse.Namespace) -> str | None:
    """The detail body from --detail or --detail-file, stripped, or None."""
    if args.detail_file:
        path = Path(args.detail_file).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --detail-file {path}: {exc}") from exc
    elif args.detail:
        text = args.detail
    else:
        return None
    return text.strip() or None


def cmd_owner(args: argparse.Namespace) -> int:
    """Record or clear one thing that waits on the owner, against a task."""
    _, store = _context(args)
    task = store.get(args.id)
    if args.detail and args.detail_file:
        raise UsageError("pass --detail or --detail-file, not both")
    if (args.detail or args.detail_file) and not args.item:
        raise UsageError("--detail needs --item: the one line the board shows first")
    if bool(args.item) == bool(args.clear):
        raise UsageError("pass exactly one of --item TEXT or --clear")
    cleared_item = False
    if args.item:
        text = args.item.strip()
        if not text:
            raise UsageError("--item needs a line of text")
        # The board's heading is one line; the body behind it is --detail. Enforcing
        # that here is what stops the wall of text coming back one item at a time.
        if "\n" in text:
            raise UsageError(
                "--item is one line. Put the body - the options, what each costs, the "
                "recommendation - in --detail or --detail-file."
            )
        if len(text) > OWNER_ITEM_LIMIT:
            raise UsageError(
                f"--item is {len(text)} characters, and the board shows one line of at "
                f"most {OWNER_ITEM_LIMIT}. Make it a heading and put the rest in "
                "--detail or --detail-file."
            )
        task.owner_item = text
        task.owner_item_at = now_iso()
        task.owner_detail = _owner_detail(args)
        note = f"{task.id}: recorded as waiting on the owner"
    else:
        # Clearing the note is not answering the question. A task can carry both, and
        # saying "cleared" while an open decision still shows is a false success - the
        # same class as a dispatch that says sent and did not. Report only what happened.
        cleared_item = task.owner_item is not None
        task.owner_item = None
        task.owner_item_at = None
        task.owner_detail = None
        note = (
            f"{task.id}: cleared from what waits on the owner"
            if cleared_item
            else f"{task.id}: nothing was recorded to clear"
        )
    store.save()

    still_open = bool(task.decision_is_open)
    if args.json:
        _emit_json(
            {
                "task": task.to_dict(),
                "cleared_item": cleared_item,
                "decision_still_open": still_open,
            }
        )
        return 0
    print(note)
    if still_open:
        print(
            f"{INDENT}the recorded decision is still open, and --clear does not answer "
            f"it. Answer it with: {PROGRAM} report {task.id} --decide TEXT"
        )
    return 0


def cmd_step_abandon(args: argparse.Namespace) -> int:
    """Record a dead step as abandoned, and free its job for the next step.

    When a pane dies or the machine restarts mid-step, the step can never report.
    Before this, the only way past was --force, which also skipped the branch and
    clean-tree checks. Abandoning keeps the reason, leaves the answer empty, and
    is not read as a report.
    """
    config, store = _context(args)
    why = (args.why or "").strip()
    if not why:
        raise UsageError("--why is required: say why the step is being abandoned")
    task = store.get(args.id)
    if task.status == ABANDONED:
        raise StateError(f"{task.id} is already abandoned.")
    if not task.is_open:
        raise StateError(
            f"{task.id} is {task.status}, not open. Only an open step can be abandoned; "
            "a step that reported is read with `clowder report`."
        )

    task.status = ABANDONED
    task.abandoned_at = now_iso()
    task.abandon_reason = why
    store.save()

    who = (args.by or config.front_door_name or "unknown").strip()
    audit_file = audit.append_audit(
        store.path,
        {
            "at": task.abandoned_at,
            "by": who,
            "action": "step-abandon",
            "step": task.id,
            "job": task.job,
            "agent": task.agent,
            "reason": why,
        },
    )

    if args.json:
        _emit_json({"task": task.to_dict(), "audit": str(audit_file)})
        return 0
    print(f"{task.id} abandoned: {why}")
    if task.job:
        print(f"{INDENT}{task.job} is free for the next step")
    print(f"{INDENT}audit: {audit_file}")
    return 0


def cmd_step_close(args: argparse.Namespace) -> int:
    """Close a step that can never report, and keep the answer it has.

    `step abandon` is for a step that never answered: its answer reads as empty.
    This is the other exit - a step whose job is long closed, or which never had
    one, and which will never report either way. Closing stops it counting as in
    flight, so the agent can be reset or renamed, and it discards nothing: the
    answer stays, the task stays, only the status changes.

    The one thing it will not do is close a step of a job that is still open.
    That would hide work still in flight, which is the rule abandon exists for.
    """
    config, store = _context(args)
    why = (args.why or "").strip()
    if not why:
        raise UsageError("--why is required: say why the step is being closed")
    task = store.get(args.id)
    if task.status == CLOSED:
        raise StateError(f"{task.id} is already closed.")
    if not task.is_open:
        raise StateError(
            f"{task.id} is {task.status}, not open. Only an open step can be closed; "
            "a step that reported is read with `clowder report`."
        )
    if task.job:
        job = next((job for job in store.all_jobs() if job.id == task.job), None)
        if job is not None and job.is_open:
            raise StateError(
                f"{task.id} is a step of {job.id}, which is still open. Let it report, "
                f"or close the job first. Closing a step of an open job would hide "
                "work that is still in flight."
            )

    task.status = CLOSED
    task.closed_at = now_iso()
    task.close_reason = why
    store.save()

    who = (args.by or config.front_door_name or "unknown").strip()
    audit_file = audit.append_audit(
        store.path,
        {
            "at": task.closed_at,
            "by": who,
            "action": "step-close",
            "step": task.id,
            "job": task.job,
            "agent": task.agent,
            "reason": why,
            "answer_kept": bool(task.answer),
        },
    )

    if args.json:
        _emit_json({"task": task.to_dict(), "audit": str(audit_file)})
        return 0
    print(f"{task.id} closed: {why}")
    if task.job:
        print(f"{INDENT}{task.job} is free for the next step")
    print(f"{INDENT}audit: {audit_file}")
    return 0


def cmd_step_report(args: argparse.Namespace) -> int:
    """Close a finished step where it reports, keeping the answer and the facts.

    A worker's report used to reach the record only when the front door read it,
    and the read was the side effect that freed the job. Here the step closes
    itself: the worker writes one line (and the whole answer, from a file, when it
    is long), the status leaves `dispatched`, and the one-open-step guard frees
    itself. Nothing is lost - a read still shows the answer, the usage, and the
    commit.

    A second report for the same step changes nothing and records nothing twice,
    so a retried report is safe.
    """
    config, store = _context(args)
    task = store.get(args.id)
    headline = (args.headline or "").strip()
    if not headline:
        raise UsageError("--headline is required: one line on what the step found")
    if "\n" in headline:
        raise UsageError(
            "--headline is one line. Put the rest in --answer-file, where the whole "
            "answer belongs."
        )
    if task.is_abandoned:
        raise StateError(
            f"{task.id} was abandoned: {task.abandon_reason or 'no reason recorded'}. "
            "An abandoned step keeps no answer; open a new step for this work."
        )
    if not task.is_open:
        if args.json:
            _emit_json({"task": task.to_dict(), "closed": False, "already": True})
        else:
            print(f"{task.id} is already {task.status}; nothing changed")
        return 0

    answer = headline
    source = "step"
    if args.answer_file:
        path = Path(args.answer_file).expanduser()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UsageError(f"cannot read --answer-file {path}: {exc}") from exc
        if text:
            answer = text
            source = "file"

    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    since, until = _step_window(store, task)
    session_path = task.agent_session
    if not (session_path and Path(session_path).exists()):
        agent = None
        try:
            agent = mux.find_agent(task.agent)
        except MuxError:
            agent = None
        session_path = agent.session_path if agent is not None else session_path
    usage = read_usage(session_path, since, until) if session_path else None
    _record_step_facts(task, usage)
    if session_path:
        task.agent_session = session_path

    task.answer = answer
    task.answer_source = source
    task.reported_at = now_iso()
    task.closed_at = task.reported_at
    task.close_reason = "the step reported"
    task.status = CLOSED
    store.save()

    who = (args.by or task.agent or config.front_door_name or "unknown").strip()
    audit_file = audit.append_audit(
        store.path,
        {
            "at": task.closed_at,
            "by": who,
            "action": "step-report",
            "step": task.id,
            "job": task.job,
            "agent": task.agent,
            "reason": task.close_reason,
            "answer_kept": True,
        },
    )

    if owner_line(answer) is None:
        # Not a refusal: the step is done and the record must say so. But a report
        # without the line cannot be relayed to the owner, so it is not silent.
        print(
            f"{PROGRAM}: note: this report has no 'For the owner:' line, so the front "
            "door cannot relay what it changes for him. Add one line to the answer.",
            file=sys.stderr,
        )

    if args.json:
        _emit_json(
            {
                "task": task.to_dict(),
                "closed": True,
                "audit": str(audit_file),
                "owner_line": owner_line(answer),
            }
        )
        return 0
    print(f"{task.id} closed on its report: {headline}")
    if task.job:
        print(f"{INDENT}{task.job} is free for the next step")
    print(f"{INDENT}audit: {audit_file}")
    return 0


def _resolve_session(
    mux: Mux, task: Task, since: float | None, until: float | None = None
) -> tuple[str | None, str | None, str | None]:
    """The session to read, the answer already in it, and the agent's status.

    A pane keeps one long-lived session, and a restart gives it a new one. The
    answer to a step is in the session the step was dispatched into while that
    session holds it; a pane that restarted before the worker answered has it in
    the live session instead. Reading the live one first is what overwrote the
    pointer to the session that held the answer. The read is bounded to the
    step's own window `[since, until)`.
    """
    agent = None
    try:
        agent = mux.find_agent(task.agent)
    except MuxError:
        agent = None
    live = agent.session_path if agent is not None else None
    session, answer = _session_and_answer(task.agent_session, live, since, until)
    return session, answer, agent.status if agent is not None else None


def cmd_ensure(args: argparse.Namespace) -> int:
    config, _ = _context(args)
    repo_path = config.resolve_repo(args.repo)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)

    result = _ensure(
        config,
        mux,
        repo_path,
        repo_name=args.repo,
        role=args.role,
        kind=args.kind,
        direction=args.direction,
        name=args.name,
        create=not args.no_create,
        split=args.split,
    )

    if args.json:
        _emit_json(
            {
                **result.as_dict(),
                "repo": args.repo,
                "repo_path": str(repo_path),
                "role": args.role,
            }
        )
        return 0 if result.ok else NEEDS_HUMAN

    if result.ok:
        assert result.agent is not None
        how = "made" if result.created else "already live"
        print(f"{result.agent.name} serves {args.repo} ({how}), pane {result.agent.pane_id}")
        print(f"{INDENT}working directory: {result.agent.cwd or '(unreported)'}")
        if result.note:
            print(f"{PROGRAM}: note: {result.note}", file=sys.stderr)
        return 0

    print(f"{PROGRAM}: {result.reason}", file=sys.stderr)
    if result.candidates:
        print(
            f"{PROGRAM}: agents in that repo: {', '.join(result.candidates)}. "
            "Dispatch to one of those by name.",
            file=sys.stderr,
        )
    elif args.no_create:
        print(f"{PROGRAM}: run without --no-create to start one there.", file=sys.stderr)
    return NEEDS_HUMAN


def _ensure(
    config: Config,
    mux: Mux,
    repo_path,
    *,
    repo_name: str,
    role: str,
    kind: str,
    direction: str,
    name: str | None = None,
    create: bool = True,
    split: bool = False,
):
    """One place where an agent for a repo is found or made."""
    return ensure_agent(
        mux,
        repo_name,
        repo_path,
        role=role,
        kind=kind,
        direction=direction,
        create=create,
        name=name,
        split=split,
        worktree_dir=config.worktree_dir,
        base=config.worktree_base,
        setup=config.worktree_setup,
        identity=_crew_identity(config),
    )


def cmd_job_open(args: argparse.Namespace) -> int:
    config, store = _context(args)
    repo_path = config.resolve_repo(args.repo)
    if not gitcmd.is_repo(repo_path):
        raise GitError(
            f"{repo_path} is not a git repository. A job is a branch, so it needs one."
        )

    mux = Mux(config.mux_bin, config.mux_prompt_argv)

    if config.delivery_mode not in BUILT_DELIVERY_MODES:
        raise UsageError(
            f"worktree.delivery is {config.delivery_mode!r}, and only "
            f"{', '.join(BUILT_DELIVERY_MODES)} is built. Nothing merges or pushes "
            "here yet, so a mode that promises more would be a lie."
        )

    ensured = _ensure(
        config,
        mux,
        repo_path,
        repo_name=args.repo,
        role=args.role,
        kind=args.kind,
        direction=args.direction,
        name=args.name,
        create=True,
        split=args.split,
    )
    if not ensured.ok:
        print(f"{PROGRAM}: {ensured.reason}", file=sys.stderr)
        if ensured.candidates:
            print(
                f"{PROGRAM}: agents in that repo: {', '.join(ensured.candidates)}",
                file=sys.stderr,
            )
        return NEEDS_HUMAN

    agent = ensured.agent
    assert agent is not None
    worktree = agent.cwd or str(repo_path)
    if ensured.note:
        print(f"{PROGRAM}: note: {ensured.note}", file=sys.stderr)

    top = gitcmd.toplevel(repo_path)
    if top and normalise(worktree) == normalise(top) and not args.force:
        raise GitError(
            f"{agent.name} works in the main checkout ({worktree}). Switching branches "
            "there would disturb your own work. Make an agent with its own worktree: "
            f"`{PROGRAM} ensure {args.repo} --role {args.role} --name <name>`, or pass "
            "--force."
        )

    fetched = False
    try:
        fetched = gitcmd.fetch(repo_path)
    except GitError as exc:
        print(f"{PROGRAM}: warning: could not fetch {repo_path}: {exc}", file=sys.stderr)
    base_ref = gitcmd.resolve_base(repo_path, args.base or config.worktree_base)
    # Fork from the published base, not the main checkout's local branch. The
    # local branch is often stale, and a job cut from it would carry old code - but
    # refusing every job until that one shared checkout is pulled blocks the whole
    # repo, even though each agent has its own worktree.
    #
    # Use the local remote-tracking ref whenever it exists, even after a failed
    # fetch: it is the last known published base, and the local branch can be older
    # still. A failed fetch means only that this ref may itself be stale, so it is a
    # warning. The base is refused only when the two histories disagree (a rewritten
    # base, or local commits that were never published), because forking from the
    # remote would then drop the local side.
    fork_from = base_ref
    remote_ref = f"origin/{base_ref}"
    if gitcmd.commit_of(repo_path, remote_ref) is not None:
        if not args.force and not gitcmd.is_ancestor(repo_path, base_ref, remote_ref):
            raise GitError(
                f"{remote_ref} does not contain {base_ref}: the base branch was "
                f"rewritten, or {base_ref} has local commits that were never "
                "published. A job forks from the published base, so the two must "
                f"agree first. Reconcile {base_ref} by hand, then open the job "
                "again. Or pass --force if you know better."
            )
        fork_from = remote_ref
        if not fetched:
            print(
                f"{PROGRAM}: warning: using {remote_ref} as the base, and it could "
                "not be refreshed, so it may be stale",
                file=sys.stderr,
            )
    label = args.label.strip()
    branch = args.branch or f"{config.job_branch_prefix}{sanitise(label)}"

    # One checked-out job per worktree. A released job keeps its record but has
    # handed its space back, so it does not block the next piece of work.
    here = next(
        (
            job
            for job in store.open_jobs()
            if job.released_at is None and normalise(job.worktree) == normalise(worktree)
        ),
        None,
    )
    if here is not None and here.branch == branch:
        if args.json:
            _emit_json({"job": here.to_dict(), "agent": agent.name, "already_open": True})
        else:
            print(f"{here.id} already open: {here.branch} in {here.worktree}")
        return 0
    if here is not None and not args.force:
        raise GitError(
            f"{here.id} is still open on this worktree: {here.branch} ({here.label}). "
            f"Close it before starting {branch} here, or pass --force to abandon it."
        )

    dirty = gitcmd.status_entries(worktree)
    if dirty and not args.force:
        shown = ", ".join(entry.split(maxsplit=1)[-1] for entry in dirty[:5])
        more = ", and more" if len(dirty) > 5 else ""
        raise GitError(
            f"{worktree} is not clean: {shown}{more}. A job starts from a clean tree, "
            "or the branch carries someone else's half-finished work. Commit, stash, "
            "or pass --force."
        )

    on = gitcmd.current_branch(worktree)
    if on == branch:
        pass
    elif gitcmd.branch_exists(worktree, branch):
        gitcmd.switch_branch(worktree, branch)
    else:
        gitcmd.switch_new_branch(worktree, branch, fork_from)

    job = Job(
        id=store.next_job_id(),
        label=label,
        repo=args.repo,
        repo_path=str(repo_path),
        worktree=worktree,
        branch=branch,
        base=base_ref,
        # The true fork point. For an existing branch that is not the base tip:
        # `merge-base` finds where the branch actually left the base. It is
        # measured against the ref the branch forked from, so a stale local base
        # does not make the record point at old code.
        base_commit=gitcmd.merge_base(worktree, fork_from, branch),
        agent=agent.name,
        commit=gitcmd.head_commit(worktree),
        # The change in the owner's words. Title falls back to the label, which is
        # already words rather than a handle.
        title=(args.title or "").strip() or label,
        effect=(args.effect or "").strip() or None,
        labels=list(args.tags),
    )
    store.add_job(job)
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict(), "agent": agent.name, "created": ensured.created})
        return 0

    print(f"{job.id} open: {job.branch} in {job.worktree}")
    print(f"{INDENT}{agent.name} | {job.repo} | from {job.base} | {job.commit or 'no commit'}")
    return 0


def cmd_job_park(args: argparse.Namespace) -> int:
    """Park a published job's branch, and free its space for the next job.

    A passed, published job is finished as far as the crew's work goes: its only
    open gate is the owner's merge word, and nothing is edited on the branch while
    it waits. It used to keep its checkout until that word came, so a second change
    to the same repo needed a second pane - 241 MB to 1.9 GB of worktree for a job
    nobody is working in.

    Parking is the release this tool already knows (`job handover` and `job close`
    do the same), applied to the last waiting state: the branch stays, the commit
    the owner's word is for is recorded, and the space goes back to the base, so the
    next job starts in the same space. No second branch enters the checkout, because
    the point is to release it, not to multiplex it. The merge still lands on the
    recorded commit: `job merge` refuses if the branch has moved since.
    """
    config, store = _context(args)
    job = store.get_job(args.id)
    if not job.is_open:
        raise StateError(f"{job.id} is {job.state_in_words}, so there is nothing to park.")
    if job.is_parked:
        if args.json:
            _emit_json({"job": job.to_dict(), "parked": False, "already": True})
        else:
            print(
                f"{job.id} is already parked: {job.branch} at "
                f"{(job.park_commit or job.commit or 'no commit')[:7]}"
            )
        return 0
    if not job.published_at:
        raise StateError(
            f"{job.id} is not published, so the owner's merge word is not the only "
            "gate left. Parking is for a published job that waits on that word: "
            f"publish it first with {PROGRAM} job publish {job.id}."
        )
    if job.has_merge_word:
        raise StateError(
            f"{job.id} already has the owner's merge word, so nothing is waiting. "
            f"Merge it: {PROGRAM} job merge {job.id} --pr NUMBER"
        )

    open_steps = [task for task in store.all() if task.job == job.id and task.is_open]
    if open_steps:
        listed = ", ".join(f"{task.id} ({task.agent})" for task in open_steps)
        raise StateError(
            f"{job.id} still has open steps: {listed}. Parking would free a space "
            "while work is in flight on the branch. Wait for it to report."
        )

    # The commit the owner's word will be for. Read from the branch, not the
    # folder, because a released space sits on the base.
    tip = gitcmd.commit_of(job.worktree, job.branch) or gitcmd.commit_of(
        job.repo_path, job.branch
    )
    if not tip:
        raise StateError(
            f"cannot read {job.branch}, so parking would forget which commit the "
            "owner's merge word is for. Nothing was released."
        )

    base_ref = _release_job_space(config, job)
    job.commit = tip
    job.park_commit = tip
    job.parked_at = now_iso()
    job.released_at = job.released_at or job.parked_at
    store.save()

    who = (args.by or config.front_door_name or "unknown").strip()
    audit_file = audit.append_audit(
        store.path,
        {
            "at": job.parked_at,
            "by": who,
            "action": "job-park",
            "job": job.id,
            "branch": job.branch,
            "commit": tip,
            "space": job.worktree,
        },
    )

    if args.json:
        _emit_json(
            {
                "job": job.to_dict(),
                "parked": True,
                "commit": tip,
                "returned_to": base_ref,
                "space": "free",
                "audit": str(audit_file),
            }
        )
        return 0
    print(f"{job.id} parked: {job.branch} kept at {tip[:7]}, space free on {base_ref}")
    print(f"{INDENT}{job.worktree}")
    print(f"{INDENT}the next job can start here; the owner's merge word is for {tip[:7]}")
    print(f"{INDENT}audit: {audit_file}")
    return 0


def _release_job_space(config: Config, job: Job, *, force: bool = False) -> str:
    """Return a job's space to the base, keeping its install. Returns the base ref.

    A space that handover already released is left alone. A space still on the
    job's branch is detached at the base, so it is free for the next piece of
    work. `force` skips the clean and on-branch checks; a merge uses it because
    the pull request has already merged and the record must not be held back by
    a folder that drifted.
    """
    if job.released_at is not None:
        return gitcmd.resolve_base(job.repo_path, config.worktree_base)
    worktree = job.worktree
    dirty = gitcmd.status_entries(worktree)
    if dirty and not force:
        shown = ", ".join(entry.split(maxsplit=1)[-1] for entry in dirty[:5])
        more = ", and more" if len(dirty) > 5 else ""
        raise GitError(
            f"{worktree} is not clean: {shown}{more}. A job that gives its space "
            "back leaves the tree ready for the next one, so this is refused. "
            "Commit, stash, or pass --force."
        )

    on = gitcmd.current_branch(worktree)
    if on != job.branch and not force:
        raise GitError(
            f"{worktree} is on {on or 'a detached HEAD'}, not {job.branch}. "
            "Nothing was switched. Close the job from its own branch."
        )
    if on == job.branch:
        job.commit = gitcmd.head_commit(worktree)

    # Release the space: no branch name on it, sitting on the current base,
    # clean. Its install and caches stay, the point of keeping the space.
    base_ref = gitcmd.resolve_base(job.repo_path, config.worktree_base)
    gitcmd.detach_at(worktree, base_ref)
    return base_ref


def cmd_job_close(args: argparse.Namespace) -> int:
    config, store = _context(args)
    job = store.get_job(args.id)
    worktree = job.worktree

    if args.delete_branch and not job.has_merge_word:
        raise StateError(
            f"{job.id} cannot delete {job.branch} without the owner's word. Record it "
            f"with: {PROGRAM} job word {job.id} --word TEXT --by NAME"
        )

    open_steps = [task for task in store.all() if task.job == job.id and task.is_open]
    if open_steps and not args.force:
        listed = ", ".join(f"{task.id} ({task.agent})" for task in open_steps)
        raise StateError(
            f"{job.id} still has open steps: {listed}. Closing now would leave work "
            "in flight on a branch nobody is holding. Wait for it to report, or pass "
            "--force."
        )

    base_ref = _release_job_space(config, job, force=args.force)

    deleted = False
    if args.delete_branch:
        gitcmd.delete_branch(worktree, job.branch)
        deleted = True

    job.status = CLOSED
    job.closed_at = now_iso()
    job.released_at = now_iso()
    store.save()

    if args.json:
        _emit_json(
            {
                "job": job.to_dict(),
                "returned_to": base_ref,
                "branch_deleted": deleted,
                "space": "free",
            }
        )
        return 0

    print(f"{job.id} closed: {job.branch} kept, space free again on {base_ref}")
    print(f"{INDENT}{worktree}")
    if deleted:
        print(f"{INDENT}the job branch was deleted")
    return 0


def _sync_job_branch(config: Config, store: StateStore, job: Job) -> bool:
    """Merge origin/<base> into the job branch, under the crew identity.

    A forward merge keeps every writer commit and the reviewed commit alive; a
    rebase would rewrite them and throw away the owner's pass. Returns True when
    it made a merge commit, False when the branch was already current. Raises
    GitError on conflict, after aborting the merge so the tree is left clean.
    """
    repo = job.repo_path
    if not gitcmd.has_remote(repo):
        return False
    gitcmd.fetch(repo)
    remote_ref = f"origin/{job.base}"
    if gitcmd.commit_of(repo, remote_ref) is None:
        return False

    worktree = job.worktree
    if gitcmd.status_entries(worktree):
        raise GitError(
            f"{worktree} is not clean, so {remote_ref} cannot be merged in. "
            f"{job.agent} must commit or stash first."
        )

    on_branch = gitcmd.current_branch(worktree) == job.branch
    if not on_branch:
        # A released space sits detached at the base. Put the branch back only for
        # this merge, then restore the resting state, unless another open job owns
        # the space now.
        if not job.released_at:
            raise GitError(
                f"{worktree} is not on {job.branch}, so it cannot be synced there. "
                f"Sync {job.id} in the writer's own space."
            )
        holder = next(
            (
                other
                for other in store.open_jobs()
                if other.id != job.id and normalise(other.worktree) == normalise(worktree)
            ),
            None,
        )
        if holder is not None:
            raise StateError(
                f"{holder.id} is open on {worktree}, so {job.id}'s branch cannot be "
                "checked out there to sync."
            )
        gitcmd.switch_branch(worktree, job.branch)

    before = gitcmd.commit_of(worktree, "HEAD")
    message = f"Merge {remote_ref} into {job.branch}"
    ok, conflicts = gitcmd.merge_branch(worktree, remote_ref, message, _crew_identity(config))
    if not ok:
        if not on_branch:
            base_ref = gitcmd.resolve_base(repo, config.worktree_base)
            gitcmd.detach_at(worktree, base_ref)
        raise GitError(
            f"merging {remote_ref} into {job.branch} conflicts in: "
            f"{', '.join(conflicts) or 'unknown paths'}. The merge was aborted and "
            f"the tree is clean. {job.agent} must resolve it by hand, then sync again."
        )
    changed = gitcmd.commit_of(worktree, "HEAD") != before
    if not on_branch and job.released_at:
        base_ref = gitcmd.resolve_base(repo, config.worktree_base)
        gitcmd.detach_at(worktree, base_ref)
    return changed


def cmd_job_sync(args: argparse.Namespace) -> int:
    """Merge origin/<base> into the job branch, without rewriting it."""
    config, store = _context(args)
    job = store.get_job(args.id)
    if not job.is_open:
        raise StateError(f"{job.id} is closed, so there is nothing to sync.")
    changed = _sync_job_branch(config, store, job)
    tip = gitcmd.commit_of(job.worktree, job.branch)
    if args.json:
        _emit_json({"job": job.to_dict(), "synced": changed, "commit": tip})
        return 0
    if changed:
        print(f"{job.id} synced: {job.branch} @ {(tip or '')[:7]}")
    else:
        print(f"{job.id} already current with origin/{job.base}")
    return 0


def cmd_job_handover(args: argparse.Namespace) -> int:
    """Give a reviewer the writer's saved code, pinned to one commit.

    The reviewer's folder is filled with the writer's files at that save, with no
    branch name on it. So the reviewer checks the real code, and the writer keeps
    the branch and can carry on.
    """
    config, store = _context(args)
    job = store.get_job(args.id)
    if not job.is_open:
        raise StateError(f"{job.id} is closed, so there is nothing to hand over.")

    writer = job.worktree
    pending = gitcmd.status_entries(writer)
    if pending and not args.force:
        shown = ", ".join(entry.split(maxsplit=1)[-1] for entry in pending[:5])
        more = ", and more" if len(pending) > 5 else ""
        raise GitError(
            f"{job.agent} has work that is not saved in {writer}: {shown}{more}. "
            "A reviewer checks a save, not a folder. Save it, or pass --force."
        )

    if gitcmd.count_commits(writer, f"{job.base}..HEAD") == 0:
        raise GitError(
            f"nothing has been saved on {job.branch} yet, so there is nothing to "
            "hand over. A reviewer needs a save to look at."
        )
    if not args.no_sync:
        if pending:
            print(
                f"{PROGRAM}: note: not syncing {job.branch}: {writer} is not clean",
                file=sys.stderr,
            )
        else:
            # Bring the base in before the freeze, so the reviewer checks the code
            # as it will be merged. A merge keeps every hash; a rebase would not.
            _sync_job_branch(config, store, job)
    commit = gitcmd.head_commit(writer, short=False)
    if commit is None:
        raise GitError(f"cannot read a commit in {writer}")

    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    ensured = _ensure(
        config,
        mux,
        job.repo_path,
        repo_name=job.repo,
        role=args.to,
        kind=args.kind,
        direction=args.direction,
        name=args.name,
        create=True,
        split=args.split,
    )
    if not ensured.ok:
        print(f"{PROGRAM}: {ensured.reason}", file=sys.stderr)
        if ensured.candidates:
            print(
                f"{PROGRAM}: agents in that repo: {', '.join(ensured.candidates)}",
                file=sys.stderr,
            )
        return NEEDS_HUMAN

    reviewer = ensured.agent
    assert reviewer is not None
    folder = reviewer.cwd or ""
    if ensured.note:
        print(f"{PROGRAM}: note: {ensured.note}", file=sys.stderr)

    top = gitcmd.toplevel(job.repo_path)
    if top and normalise(folder) == normalise(top) and not args.force:
        raise GitError(
            f"{reviewer.name} works in the main checkout ({folder}). Pinning that to a "
            "commit would move your own work. Make a reviewer with a copy of its own: "
            f"`{PROGRAM} ensure {job.repo} --role {args.to} --name <name>`."
        )
    if not folder or not gitcmd.is_repo(folder):
        raise GitError(
            f"{reviewer.name}'s folder is not a git repository "
            f"({folder or 'unknown'}), so it cannot hold the code to check."
        )

    busy = next(
        (
            other
            for other in store.open_jobs()
            if other.id != job.id and normalise(other.worktree) == normalise(folder)
        ),
        None,
    )
    if busy is not None and not args.force:
        raise StateError(
            f"{busy.id} is still open on {reviewer.name}'s folder. Close it first."
        )

    unsaved = gitcmd.status_entries(folder)
    if unsaved and not args.force:
        shown = ", ".join(entry.split(maxsplit=1)[-1] for entry in unsaved[:5])
        raise GitError(
            f"{reviewer.name} has work that is not saved in {folder}: {shown}. "
            "Pinning the folder would write over it. Save it, or pass --force."
        )

    gitcmd.switch_to_commit(folder, commit)

    # The space is a build cache, not the record. Hold the reviewed commit in a
    # local ref - so the branch may be kept or dropped - then hand the writer's
    # space back to the base. The job stays OPEN for the pass, publish and merge.
    held = gitcmd.hold_ref(job.repo_path, job.id, commit)
    base_ref = gitcmd.resolve_base(job.repo_path, config.worktree_base)
    gitcmd.detach_at(writer, base_ref)
    job.held_ref = held
    job.released_at = now_iso()
    job.reviewer = reviewer.name
    job.review_commit = commit
    job.handed_over_at = now_iso()
    job.commit = commit
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict(), "reviewer": reviewer.name, "commit": commit})
        return 0

    print(f"{job.id} handed over: {job.branch} @ {commit[:7]}")
    print(f"{INDENT}{reviewer.name} checks it in {folder}")
    print(
        f"{INDENT}send the review step with: {PROGRAM} dispatch {reviewer.name} "
        f"{job.repo} --job {job.id}"
    )
    return 0


def cmd_job_pin(args: argparse.Namespace) -> int:
    """Pin a job's saved commit into a reviewer's copy, and prove it landed.

    `job handover` needs the writer's folder to still hold the job branch. When
    the writer has moved on to a later job, that folder no longer does, and the
    front door used to detach the reviewer's copy by hand. This is that act, with
    the checks: the copy is a space of its own, clean, and holds no other open job.
    """
    config, store = _context(args)
    job = store.get_job(args.id)
    if not job.is_open:
        raise StateError(f"{job.id} is closed, so there is nothing to pin.")

    candidate = args.commit or job.review_commit or job.commit
    if not candidate and job.held_ref:
        candidate = gitcmd.commit_of(job.repo_path, job.held_ref)
    if not candidate:
        raise StateError(f"{job.id} names no commit to pin; pass --commit SHA")
    commit = gitcmd.commit_of(job.repo_path, candidate) or candidate

    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    ensured = _ensure(
        config,
        mux,
        job.repo_path,
        repo_name=job.repo,
        role=args.to,
        kind=args.kind,
        direction=args.direction,
        name=args.name,
        create=True,
        split=args.split,
    )
    if not ensured.ok:
        print(f"{PROGRAM}: {ensured.reason}", file=sys.stderr)
        if ensured.candidates:
            print(
                f"{PROGRAM}: agents in that repo: {', '.join(ensured.candidates)}",
                file=sys.stderr,
            )
        return NEEDS_HUMAN

    reviewer = ensured.agent
    assert reviewer is not None
    folder = reviewer.cwd or ""
    if ensured.note:
        print(f"{PROGRAM}: note: {ensured.note}", file=sys.stderr)

    top = gitcmd.toplevel(job.repo_path)
    if top and normalise(folder) == normalise(top) and not args.force:
        raise GitError(
            f"{reviewer.name} works in the main checkout ({folder}). Pinning that to a "
            "commit would move your own work. Make a reviewer with a copy of its own: "
            f"`{PROGRAM} ensure {job.repo} --role {args.to} --name <name>`."
        )
    if not folder or not gitcmd.is_repo(folder):
        raise GitError(
            f"{reviewer.name}'s folder is not a git repository "
            f"({folder or 'unknown'}), so it cannot hold the code to check."
        )

    busy = next(
        (
            other
            for other in store.open_jobs()
            if other.id != job.id and normalise(other.worktree) == normalise(folder)
        ),
        None,
    )
    if busy is not None and not args.force:
        raise StateError(
            f"{busy.id} is still open on {reviewer.name}'s folder. Close it first."
        )

    unsaved = gitcmd.status_entries(folder)
    if unsaved and not args.force:
        shown = ", ".join(entry.split(maxsplit=1)[-1] for entry in unsaved[:5])
        raise GitError(
            f"{reviewer.name} has work that is not saved in {folder}: {shown}. "
            "Pinning the folder would write over it. Save it, or pass --force."
        )

    gitcmd.switch_to_commit(folder, commit)
    held = gitcmd.commit_of(folder, "HEAD")
    if held != commit:
        raise GitError(
            f"pinned {folder} but it reports {held or 'no commit'}, not {commit}. "
            "Nothing was recorded."
        )

    job.reviewer = reviewer.name
    job.review_commit = commit
    job.commit = commit
    job.held_ref = gitcmd.hold_ref(job.repo_path, job.id, commit)
    job.handed_over_at = now_iso()
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict(), "reviewer": reviewer.name, "commit": commit})
        return 0
    print(f"{job.id} pinned: {job.branch} @ {commit[:7]}")
    print(f"{INDENT}{reviewer.name} holds it in {folder}")
    return 0


def _owner_words_by(by: str) -> str:
    """The name that gave the owner's words. A worker cannot give them."""
    name = by.strip()
    if not name:
        raise UsageError("--by is required: name who gave the words")
    if name in ROLES:
        raise UsageError(
            f"--by {name!r} is a worker role. Only the owner gives the pass or the merge "
            "word, and the record must name him. A worker cannot record one."
        )
    return name


def cmd_job_pass(args: argparse.Namespace) -> int:
    _, store = _context(args)
    job = store.get_job(args.id)
    by = _owner_words_by(args.by)
    shown = args.shown.strip()
    answer = args.answer.strip()
    if not shown or not answer:
        raise UsageError(
            "--shown and --answer are both required: what he saw, and what he said"
        )
    if not job.review_commit:
        raise StateError(
            f"{job.id} has no reviewed commit, so the owner's pass cannot be bound to "
            f"it. Hand the change over first: {PROGRAM} job handover {job.id}"
        )
    job.pass_shown = shown
    job.pass_answer = answer
    job.pass_at = now_iso()
    job.pass_by = by
    # Bind the pass to the commit the owner was shown. A forward base merge keeps
    # it an ancestor of the branch; a new writer commit does not.
    job.pass_commit = job.review_commit
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict()})
        return 0
    print(f"{job.id}: the owner's pass recorded, by {by} at {job.pass_at}")
    return 0


def cmd_job_word(args: argparse.Namespace) -> int:
    _, store = _context(args)
    job = store.get_job(args.id)
    by = _owner_words_by(args.by)
    word = args.word.strip()
    if not word:
        raise UsageError("--word is required: his merge word, in his words")
    job.merge_word = word
    job.merge_word_at = now_iso()
    job.merge_word_by = by
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict()})
        return 0
    print(f"{job.id}: the owner's merge word recorded, by {by} at {job.merge_word_at}")
    return 0


def _require_pass(job: Job) -> None:
    if not job.has_pass:
        raise StateError(
            f"{job.id} has no owner's pass, so it cannot be published. Record it with: "
            f"{PROGRAM} job pass {job.id} --shown TEXT --answer TEXT --by NAME"
        )


def _require_merge_word(job: Job) -> None:
    if not job.has_merge_word:
        raise StateError(
            f"{job.id} has no owner's merge word, so it cannot be merged. Record it with: "
            f"{PROGRAM} job word {job.id} --word TEXT --by NAME"
        )


def _crew_identity(config: Config) -> gitcmd.Identity:
    """The commit identity every pane the crew makes is launched with."""
    return gitcmd.Identity(config.git_name, config.git_email)


def _require_pass_commit(job: Job) -> None:
    """The owner's pass is bound to a commit; the branch must not outrun it.

    A forward base merge is the only legitimate move after a pass: it adds a merge
    commit whose other parent is origin/<base>, and no writer content. A writer
    commit after the pass, appended or rewritten, is content the owner has not
    seen, and the pass is stale.
    """
    if not job.pass_commit:
        return
    tip = gitcmd.commit_of(job.worktree, job.branch)
    if not tip or not gitcmd.is_ancestor(job.worktree, job.pass_commit, tip):
        stale = True
    else:
        # Only the base coming in is allowed. When the remote-tracking base is
        # absent (pruned, renamed, never fetched) nothing is subtracted, so an
        # appended writer commit is still counted: over-refusing, never failing
        # open.
        remote_base = f"origin/{job.base}"
        exclude = remote_base if gitcmd.commit_of(job.worktree, remote_base) else None
        stray = gitcmd.writer_commits_since(job.worktree, job.pass_commit, tip, exclude)
        stale = stray != 0
    if not stale:
        return
    raise StateError(
        f"{job.id} has moved past the commit the owner passed "
        f"({job.pass_commit[:7]}). The new commits need a walkthrough and a fresh "
        "pass before publish."
    )


def _publish_branch(job: Job, identity: gitcmd.Identity) -> None:
    """Push the branch. Tests replace this; nothing else may skip the gate.

    Two checks run first. The recorded base must still be in `origin/<base>`: a
    history rewrite replaces it, and the branch can show as up to date while still
    carrying the replaced history. Then every commit must be the crew's identity: a
    commit made in a pane that predates the identity, or by hand, is refused before
    it reaches origin.
    """
    gitcmd.refuse_rewritten_base(job.worktree, job.base_commit, job.base)
    gitcmd.refuse_foreign_authors(job.worktree, f"{job.base}..{job.branch}", identity)
    gitcmd.push_branch(job.worktree, job.branch)


def _merge_pull_request(job: Job, pr: str) -> None:
    """Merge the pull request through gh. Tests replace this."""
    gitcmd.run_command(f"gh pr merge {pr} --merge", job.worktree)


def cmd_job_publish(args: argparse.Namespace) -> int:
    config, store = _context(args)
    job = store.get_job(args.id)
    _require_pass(job)
    _require_pass_commit(job)
    _publish_branch(job, _crew_identity(config))
    job.published_at = now_iso()
    # The space may be released, so read the commit from the branch or the held
    # ref, never from the worktree HEAD (which sits on the base once released).
    job.commit = gitcmd.commit_of(job.worktree, job.branch) or job.commit
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict(), "published": True})
        return 0
    print(f"{job.id} published: {job.branch} pushed to origin")
    return 0


def cmd_job_merge(args: argparse.Namespace) -> int:
    config, store = _context(args)
    job = store.get_job(args.id)
    _require_merge_word(job)
    # A parked job's space is detached, so the recorded commit is the only thing
    # that says what the owner's word is for. If the branch moved by hand since it
    # was parked, a merge would land code he never passed.
    if job.park_commit:
        tip = gitcmd.commit_of(job.worktree, job.branch) or gitcmd.commit_of(
            job.repo_path, job.branch
        )
        if tip is None:
            raise StateError(
                f"{job.id} was parked at {job.park_commit[:7]}, and {job.branch} cannot be "
                "read now, so the commit the owner's word is for cannot be confirmed. "
                "Nothing was merged."
            )
        if tip != job.park_commit:
            raise StateError(
                f"{job.id} was parked at {job.park_commit[:7]}, and {job.branch} has "
                f"moved to {tip[:7]}. The owner's word was for the commit he passed, so "
                "merging now would land code he has not seen. Walk the new commit and "
                "take a fresh pass, or move the branch back."
            )
    _merge_pull_request(job, args.pr)
    # The pull request merged, so the job is finished. Record that first, then
    # close the record the way `job close` does: the branch is kept and the space
    # goes back to the base. Left open, a merged job kept reading as in progress
    # on the board and in `open_jobs()` until someone closed it by hand.
    job.merged_at = now_iso()
    job.status = CLOSED
    job.closed_at = now_iso()
    store.save()
    try:
        _release_job_space(config, job, force=True)
        if job.released_at is None:
            job.released_at = now_iso()
    except GitError as exc:
        # The merge already happened. A folder that drifted must not lose the
        # record; `job close` can still free the space later.
        print(f"{PROGRAM}: warning: {exc}", file=sys.stderr)
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict(), "merged": True, "pr": args.pr})
        return 0
    print(f"{job.id} merged: pull request {args.pr}")
    return 0


def cmd_job_list(args: argparse.Namespace) -> int:
    _, store = _context(args)
    jobs = store.all_jobs() if args.all else store.open_jobs()

    if args.json:
        _emit_json({"jobs": [job.to_dict() for job in jobs], "count": len(jobs)})
        return 0

    if not jobs:
        print("no jobs" if args.all else "no open jobs")
        return 0

    tasks = store.all()
    rows = [["CHANGE", "STATE", "REPO", "BRANCH", "AGENT", "AGE", "PASS", "WORD", "ID"]]
    for job in jobs:
        rows.append(
            [
                _clip(job.name_in_words, 30),
                _job_state_word(job, tasks),
                _clip(job.repo, 20),
                _clip(job.branch, 28),
                job.agent,
                human_age(job.age_seconds),
                "yes" if job.has_pass else "no",
                "yes" if job.has_merge_word else "no",
                job.id,
            ]
        )
    print(_column(rows))
    return 0


def cmd_queue_add(args: argparse.Namespace) -> int:
    config, store = _context(args)
    if bool(args.agent) == bool(args.role):
        raise UsageError(
            "a queued item is for exactly one agent or role: pass --agent or --role"
        )
    brief = _read_brief(args)
    question = (args.question or "").strip() or _first_line(brief)
    why = args.why.strip()
    if not why:
        raise UsageError("--why must say why the item is not sent yet")
    config.resolve_repo(args.repo)  # refuse a bad repo now, not at send time
    cost = _cost_of_args(args)

    item = Queued(
        id=store.next_queue_id(),
        brief=brief,
        question=question,
        repo=args.repo,
        agent=args.agent,
        role=args.role,
        why=why,
        shape=args.shape,
        job=args.job,
        cost=cost,
        heavy=box.heavy_flag(cost),
        labels=list(args.labels),
    )
    store.add_queued(item)
    store.save()

    if args.json:
        _emit_json({"queued": item.to_dict()})
        return 0
    print(f"{item.id} queued for {item.target} in {item.repo}: {_clip(item.why, 70)}")
    return 0


def cmd_queue_list(args: argparse.Namespace) -> int:
    _, store = _context(args)
    items = store.all_queued()

    if args.json:
        _emit_json({"queued": [item.to_dict() for item in items], "count": len(items)})
        return 0

    if not items:
        print("the queue is empty")
        return 0

    rows = [["ID", "FOR", "REPO", "AGE", "WHY", "BRIEF"]]
    for item in items:
        rows.append(
            [
                item.id,
                item.target,
                _clip(item.repo, 20),
                human_age(item.age_seconds),
                _clip(item.why, 40),
                _clip(item.question or item.brief, 60),
            ]
        )
    print(_column(rows))
    return 0


def cmd_queue_drop(args: argparse.Namespace) -> int:
    """Drop a queued item, with a reason and a name that stay on the record.

    The queue had add, list and send, so an item found already done could not
    leave it - the only way out was editing the state file by hand, which left no
    trace. This removes the item and appends one audit line: what was dropped, why,
    when, and who. The line is written before the item leaves, so a drop that
    cannot be recorded does not happen.

    The reason and the name are both required. There is no guest, so there is
    always a person or an agent to attribute the drop to; "unknown" is not an
    answer. The owner's decision at t-0525.
    """
    _, store = _context(args)
    why = (args.why or "").strip()
    if not why:
        raise UsageError("--why is required: say why the item is being dropped")
    who = (args.by or "").strip()
    if not who:
        raise UsageError("--by is required: name who is dropping the item")
    item = store.get_queued(args.id)
    at = now_iso()
    audit_file = audit.append_audit(
        store.path,
        {
            "at": at,
            "by": who,
            "action": "queue-drop",
            "item": item.id,
            "question": item.question or _first_line(item.brief),
            "repo": item.repo,
            "target": item.target,
            "agent": item.agent,
            "role": item.role,
            "shape": item.shape,
            "job": item.job,
            "reason": why,
        },
    )
    store.remove_queued(item.id)
    store.save()

    if args.json:
        _emit_json(
            {
                "dropped": item.to_dict(),
                "by": who,
                "at": at,
                "reason": why,
                "audit": str(audit_file),
            }
        )
        return 0
    print(f"{item.id} dropped: {why}")
    print(f"{INDENT}queued for {item.target} in {item.repo}")
    print(f"{INDENT}audit: {audit_file}")
    return 0


def cmd_queue_send(args: argparse.Namespace) -> int:
    config, store = _context(args)
    item = store.get_queued(args.id)
    agent = _queue_target_agent(config, item)
    job = (args.job or item.job or "").strip() or None
    if item.shape.lower() == "ship" and not job:
        # A queued item carries no worktree, so its job is the only place its save
        # can go. Sending a write item with neither would put it on no branch, and
        # nothing would be there to read.
        raise UsageError(
            f"{item.id} is a ship item with no job, and a queued item has no worktree "
            "of its own, so its save would land on no branch. Send it with the job it "
            f"belongs to - `{PROGRAM} queue send {item.id} --job <job-id>`. A ship needs "
            "a place either way: a job's branch, or a deliberate folder."
        )
    dispatch_args = _queued_dispatch_args(
        item,
        agent,
        job=job,
        force=args.force,
        dry_run=args.dry_run,
        json=args.json,
    )
    code = _dispatch(dispatch_args, config, store)
    if code == 0 and not args.dry_run:
        # One store, so one save writes both the task and the removal. A second
        # store would be a stale copy and would drop the task.
        store.remove_queued(item.id)
        store.save()
        if not args.json:
            print(f"{item.id} sent and removed from the queue")
    return code


def _queue_target_agent(config: Config, item: Queued) -> str:
    """Resolve a queued item to an agent name. It never makes an agent."""
    if item.agent:
        return item.agent
    if not item.role:
        raise StateError(f"queued item {item.id} names neither an agent nor a role")
    role = item.role
    repo_path = config.resolve_repo(item.repo)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    try:
        agents = mux.list_agents()
    except MuxError as exc:
        raise DispatchError(
            f"cannot read the live agent list to resolve role {role!r}: {exc}"
        ) from exc
    matched = match_agent(agents, repo_path, item.repo, role)
    if matched is None:
        raise DispatchError(
            f"no agent in {item.repo} plays {role!r}. Run "
            f"`{PROGRAM} ensure {item.repo} --role {role}` first, or queue it "
            "with --agent."
        )
    return matched.name


def _queued_dispatch_args(
    item: Queued,
    agent: str,
    *,
    job: str | None,
    force: bool,
    dry_run: bool,
    json: bool,
) -> argparse.Namespace:
    """The shape `dispatch` parses, so a queued send takes the same path.

    The job is whatever the send says, falling back to the one the item was
    recorded with. That is the whole point of the option: an item recorded while a
    block was in place can still be given its job once the block clears.
    """
    return argparse.Namespace(
        config=None,
        state=None,
        repo=item.repo,
        worktree=None,
        job=job,
        agent=agent,
        brief=[item.brief],
        brief_file=None,
        question=item.question,
        shape=item.shape,
        timeout=DEFAULT_TIMEOUT_S,
        wait=False,
        until=[],
        sender=None,
        no_marker=False,
        force=force,
        dry_run=dry_run,
        json=json,
        cost=box.cost_of(item),
        heavy=box.heavy_flag(box.cost_of(item)),
    )


def cmd_agents(args: argparse.Namespace) -> int:
    config, _ = _context(args)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    agents = mux.list_agents()

    if args.json:
        described: list[dict[str, object]] = []
        for agent in agents:
            copy = gitcmd.describe_copy(agent.cwd) if agent.cwd else None
            described.append(
                {
                    "name": agent.name,
                    "pane_id": agent.pane_id,
                    "cwd": agent.cwd,
                    "status": agent.status,
                    "session_file": agent.session_path,
                    "focused": agent.focused,
                    "copy": (
                        {
                            "branch": copy.branch,
                            "commit": copy.commit,
                            "saved": not copy.dirty,
                            "detached": copy.detached,
                            "main_checkout": copy.main_checkout,
                            "path": copy.path,
                        }
                        if copy and copy.is_repo
                        else None
                    ),
                }
            )
        _emit_json({"agents": described})
        return 0

    if not agents:
        print("no agents")
        return 0
    rows = [["NAME", "PANE", "STATUS", "COPY", "FOLDER"]]
    for agent in agents:
        copy = gitcmd.describe_copy(agent.cwd) if agent.cwd else None
        rows.append(
            [
                agent.name,
                agent.pane_id or "-",
                agent.status or "-",
                copy.label() if copy else "-",
                _clip(agent.cwd or "-", 44),
            ]
        )
    print(_column(rows))
    print(
        "\nA space is a folder on disk that keeps its install. Each agent needs one "
        "of its own; `clowder ensure` makes one."
    )
    return 0


# The keys that quit Pi, sent one at a time the way `agent reset` sends `/new`.
# A slash command sent through the agent prompt is a message, not a command.
_QUIT_KEYS = ("/", "q", "u", "i", "t", "enter")

# The line the export prints, so the wait proves the command ran in the shell.
_RESEED_MARKER = "clowder-identity-seeded"

# What the multiplexer says while it still believes a pane holds the agent that
# just quit. Pi has gone and the pane is at its shell, but the multiplexer has not
# noticed yet, so a start sent now is refused until it does. Matched on the words
# rather than the exit code, because the same refusal is what a start reports.
_PANE_NOT_A_SHELL = ("not an available shell", "not a shell")


def _wait_for_agent_gone(mux: Mux, name: str, timeout_s: float) -> bool:
    """Wait until the agent leaves the roster, or the wait runs out.

    After Pi quits, the multiplexer clears the name and the pane is back at its
    shell. The shell prompt text is theme-dependent, so the roster is the stable
    signal that the shell is ready for the export.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if mux.find_agent(name) is None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))


def _reseed_export(identity: gitcmd.Identity, shell: str | None) -> str:
    """The shell command that pins the crew identity and proves it ran.

    PowerShell and POSIX shells set the environment differently, so the pane's
    own shell decides the syntax. The marker is printed last so `pane wait-output`
    can wait for it instead of racing.
    """
    env = identity.env()
    if shell and ("pwsh" in shell.lower() or "powershell" in shell.lower()):
        pwsh_pairs = []
        for key, value in env.items():
            quoted = value.replace("'", "''")
            pwsh_pairs.append(f"$env:{key}='{quoted}'")
        return "; ".join(pwsh_pairs) + f"; Write-Output '{_RESEED_MARKER}'"
    posix_pairs = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return f"export {posix_pairs}; echo {_RESEED_MARKER}"


def _pane_not_a_shell(result: MuxResult) -> bool:
    """True when a start failed only because the pane is not seen as a shell yet."""
    text = result.error_text().lower()
    return any(marker in text for marker in _PANE_NOT_A_SHELL)


@dataclass(frozen=True)
class RestartResult:
    """The last start attempt, and how much of the budget it was allowed.

    `clipped` means the attempt did not get the whole budget, because earlier
    attempts and their backoff spent part of it. Its own duration is then the
    last sliver of the budget, not the time a start really had, so it says
    nothing about the pane and must not be quoted to the owner as the reason.
    """

    result: MuxResult
    attempts: int
    budget_s: float

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def clipped(self) -> bool:
        return self.attempts > 1


def _seconds(value: float) -> str:
    """A number of seconds as the owner would say it: 60s, 2.5s, 0.4s."""
    return f"{value:g}s"


def _restart_reason(restart: RestartResult) -> str:
    """Why the start did not land, in words that describe the real wait.

    The budget is named only when the budget is what stopped the start: a clipped
    final attempt that was killed by it had no time to answer, so its own duration
    (often a few milliseconds) is noise. Every attempt that answered is quoted as
    it answered - a refusal, a named failure such as agent_not_ready, or a timeout
    that was given its whole allowance. A different failure on a retry is still
    that failure's own reason: the budget did not stop it.
    """
    if restart.clipped and restart.result.timed_out:
        return f"the restart ran out of its {_seconds(restart.budget_s)} budget"
    return restart.result.error_text()


def _start_agent_when_ready(
    mux: Mux, name: str, pane_id: str, timeout_s: float
) -> RestartResult:
    """Start Pi again, retrying while the multiplexer still calls the pane busy.

    The quit and the identity export both finish before this runs, but the
    multiplexer can take a moment to see that the pane is a shell again. A start
    sent in that gap is refused with "not an available shell", and one attempt
    would leave the pane down. Only that refusal is retried: any other failure is
    reported at once.

    `timeout_s` is the whole restart budget, not one attempt. The first attempt
    may spend all of it, because a slow pane that is still starting must be
    allowed to land; a fixed per-attempt cap would cut exactly that start short.
    A retry spends only the backoff between attempts, and every attempt after
    the first gets what is left, so the last one is clipped to the budget.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    delay = 0.25
    attempts = 0
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        attempts += 1
        started = mux.start_agent(
            name,
            pane_id,
            kind=DEFAULT_KIND,
            timeout_s=remaining,
            agent_args=("--continue",),
        )
        if started.ok or not _pane_not_a_shell(started):
            return RestartResult(started, attempts, timeout_s)
        if time.monotonic() >= deadline:
            return RestartResult(started, attempts, timeout_s)
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 2, 2.0)


def _start_agent_by_hand(mux: Mux, name: str, pane_id: str) -> str:
    """The exact command the owner can run to start the agent in its pane."""
    argv = mux.build_start_agent_argv(
        name, pane_id, kind=DEFAULT_KIND, agent_args=("--continue",)
    )
    return shlex.join(argv)


def cmd_agent_reseed(args: argparse.Namespace) -> int:
    """Seed the crew git identity into a live pane, keeping its conversation.

    herdr sets a pane's environment only when the pane is made, so a pane made
    before the identity guard carries no GIT_AUTHOR_* or GIT_COMMITTER_*. Every
    commit it makes is authored as the owner, and the publish backstop refuses
    the job. This quits Pi, exports the identity into the pane's shell, and
    starts Pi again with the session resumed. No history is rewritten: the
    owner's pass stays on the exact commit that is pushed.

    Option (b), a re-author at handover, is a manual recovery only. It rewrites
    the commits the owner passed, so `review_commit`, `commit` and `held_ref`
    must all be re-pointed to the new hashes or the custody goes stale. It is
    never the normal path; reseed the pane instead.
    """
    config, store = _context(args)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    name = args.name.strip()
    agent = mux.find_agent(name)
    if agent is None:
        live = ", ".join(sorted(a.name for a in mux.list_agents())) or "none"
        raise DispatchError(f"no live agent named {name!r}. Live agents: {live}.")

    # The same guard `agent reset` uses: the session holds the answer to an open
    # step, and quitting Pi would wipe the copy that answer is read from.
    pending = [task for task in store.all() if task.agent == name and task.is_open]
    if pending and not args.force:
        listed = ", ".join(f"{task.id} ({_clip(task.question, 40)})" for task in pending)
        raise StateError(
            f"{name} has an unreported step: {listed}. A reseed would wipe the session "
            f"its answer is read from. Read it first: {PROGRAM} report {pending[0].id}, "
            "or pass --force."
        )

    # The quit is typed into the same box the owner uses, so it takes the same
    # guard a dispatch does.
    _refuse_if_input_dirty(mux, name, config, args.force, "the reseed")

    identity = _crew_identity(config)
    pane_id = agent.pane_id
    if not pane_id:
        raise StateError(
            f"{name} has no pane id, so its pane cannot be addressed for a reseed."
        )
    result = mux.send_keys(name, _QUIT_KEYS)
    if not result.ok:
        raise MuxError(f"could not quit Pi in {name}: {result.error_text()}")
    if not _wait_for_agent_gone(mux, name, args.timeout):
        raise StateError(
            f"{name} did not leave the roster after quitting, so its shell was not "
            "ready and the identity was not seeded. The pane needs a hand."
        )

    shell = mux.pane_foreground(pane_id)
    seeded = mux.run_in_pane(pane_id, _reseed_export(identity, shell))
    if not seeded.ok:
        raise MuxError(f"could not export the identity in {name}: {seeded.error_text()}")
    landed = mux.wait_for_output(pane_id, _RESEED_MARKER, args.timeout)
    if not landed.ok:
        raise StateError(
            f"the identity export did not reach {name}'s shell, so nothing was seeded."
        )

    restart = _start_agent_when_ready(mux, name, pane_id, args.timeout)
    if not restart.ok:
        # The quit and the export already ran, so the pane is at its shell with
        # the identity seeded. It is left that way: nothing here closes it or
        # quits again. The owner gets the exact command to bring Pi back.
        manual = _start_agent_by_hand(mux, name, pane_id)
        raise MuxError(
            f"could not restart Pi in {name} after the identity export: "
            f"{_restart_reason(restart)}. The pane is left at its shell with the "
            f"identity seeded, so it is usable. Start it by hand:\n{INDENT}{manual}"
        )
    ready = mux.wait_for_agent(name, ("idle", "done"), args.timeout)
    if not ready.ok:
        raise StateError(
            f"Pi restarted in {name} but did not become ready, so the pane needs a look."
        )

    audit.append_audit(
        store.path,
        {
            "at": now_iso(),
            "by": (config.front_door_name or "unknown").strip(),
            "action": "agent-reseed",
            "agent": name,
            "pane": pane_id,
            "identity": f"{identity.name} <{identity.email}>",
        },
    )

    if args.json:
        _emit_json(
            {
                "agent": name,
                "pane": pane_id,
                "identity": f"{identity.name} <{identity.email}>",
                "resumed": True,
            }
        )
        return 0
    print(f"{name} reseeded: the crew identity is in its shell, and Pi resumed")
    print(f"{INDENT}{identity.name} <{identity.email}>")
    return 0


def cmd_agent_reset(args: argparse.Namespace) -> int:
    """Give an agent a fresh context, and prove the session changed.

    The reset is typed into the pane as individual keys, because a slash command
    sent through the agent prompt is a message, not a command. It is refused while
    a step on the agent is unreported: that would wipe the session copy the answer
    is read from, and the record would lose its answer.
    """
    config, store = _context(args)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    name = args.name.strip()
    agent = mux.find_agent(name)
    if agent is None:
        live = ", ".join(sorted(a.name for a in mux.list_agents())) or "none"
        raise DispatchError(f"no live agent named {name!r}. Live agents: {live}.")

    pending = [task for task in store.all() if task.agent == name and task.is_open]
    if pending and not args.force:
        listed = ", ".join(f"{task.id} ({_clip(task.question, 40)})" for task in pending)
        raise StateError(
            f"{name} has an unreported step: {listed}. A reset would wipe the session "
            f"its answer is read from. Read it first: {PROGRAM} report {pending[0].id}, "
            "or pass --force."
        )

    # The reset is typed into the same box the owner uses, so it takes the same
    # guard a dispatch does: never type over text that is already there.
    _refuse_if_input_dirty(mux, name, config, args.force, "the reset")

    before = agent.session_path
    model_before = read_model(before) if before else None
    result = mux.send_keys(name, ["/", "n", "e", "w", "enter"])
    if not result.ok:
        raise MuxError(f"could not type the reset into {name}: {result.error_text()}")
    after = _wait_for_session_change(mux, name, before, args.timeout)
    if after is None:
        raise StateError(
            f"{name}'s session did not change after the reset, so the context was not "
            "refreshed. The keys may not have arrived; check the pane and try again."
        )

    # A reset starts a fresh session that takes the startup default, so the pane's
    # model and thinking level can silently change. Say so, and record it. The
    # reset still goes through: it is a recovery action, and the change is not
    # destructive. Nothing is restored unless the owner asks for it.
    model_after = read_model(after)
    before_label = model_before.label if model_before else "unknown"
    after_label = model_after.label if model_after else "unknown"
    who = (config.front_door_name or "unknown").strip()
    audit_file = audit.append_audit(
        store.path,
        {
            "at": now_iso(),
            "by": who,
            "action": "agent-reset",
            "agent": name,
            "session_before": before,
            "session_after": after,
            "model_before": before_label,
            "model_after": after_label,
        },
    )

    if args.json:
        _emit_json(
            {
                "agent": name,
                "session_before": before,
                "session_after": after,
                "model_before": before_label,
                "model_after": after_label,
                "audit": str(audit_file),
            }
        )
        return 0
    print(f"{name} reset: new session {after}")
    print(f"{INDENT}model: {before_label} -> {after_label}")
    return 0


def _wait_for_session_change(
    mux: Mux, name: str, before: str | None, timeout_s: float
) -> str | None:
    """Poll the live session until it differs from `before`, or the wait runs out."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        agent = mux.find_agent(name)
        after = agent.session_path if agent is not None else None
        if after and after != before:
            return after
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


def _space_rename(
    cwd: Path | None, old: str, new: str, worktree_dir: str
) -> tuple[Path, Path] | None:
    """The folder move for a rename, or None when the agent has no linked space.

    Only a linked worktree named after the agent, under the configured worktree
    directory, is moved. An agent sitting in the main checkout has no folder of
    its own to move, and moving the checkout is not this command's business.
    """
    if cwd is None:
        return None
    if cwd.name != old or cwd.parent.name != worktree_dir:
        return None
    if not gitcmd.is_linked_worktree(cwd):
        return None
    return cwd, cwd.parent / new


def _rename_remote_pi(cwd: Path | None, old: str, new: str) -> tuple[Path, str] | None:
    """Point a pane's remote-pi config at the new name, when it names the old one.

    The bus name is the config's `agent_name` with any `parent/` prefix and `#N`
    suffix stripped, so a config naming `tancat-ai/tancat` is this agent when the
    old name is `tancat`. Returns the path and its original text, so the caller
    can put it back.
    """
    if cwd is None:
        return None
    path = cwd / ".pi" / "remote-pi" / "config.json"
    if not path.is_file():
        return None
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(original)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("agent_name")
    if not isinstance(name, str):
        return None
    leaf = re.split(r"[/\\]", name)[-1].split("#", 1)[0].strip()
    if leaf != old:
        return None
    data["agent_name"] = new
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path, original


def _roster_paths(config: Config) -> list[Path]:
    """The shared documents that list agents, when a workspace root is set."""
    if config.workspace_root is None:
        return []
    return [config.workspace_root / "roster.md", config.workspace_root / "AGENTS.md"]


def _replace_name(text: str, old: str, new: str) -> str:
    """Replace one agent name and never a longer name that contains it.

    `maker` must not touch `tancat-maker`, so the name must stand alone: the
    characters around it are not part of a name. Name characters are letters,
    digits, `_` and `-`.
    """
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(old)}(?![A-Za-z0-9_-])"
    return re.sub(pattern, new, text)


def cmd_agent_rename(args: argparse.Namespace) -> int:
    """Rename one live agent everywhere its name lives, or change nothing.

    A name lives in six places that do not check each other: the multiplexer's
    agent list, the space folder, the state records for tasks and jobs, the
    pane's remote-pi config, and the two shared documents `roster.md` and
    `AGENTS.md` under the workspace root. They change together, and every change
    already made is undone if a later one fails.
    """
    config, store = _context(args)
    old = args.name.strip()
    new = requested_name(args.to)
    if old == new:
        raise UsageError(f"{old!r} is already its own name; nothing to rename")

    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    agents = mux.list_agents()
    agent = next((item for item in agents if item.name == old), None)
    if agent is None:
        live = ", ".join(sorted(item.name for item in agents)) or "none"
        raise DispatchError(f"no live agent named {old!r}. Live agents: {live}.")
    if any(item.name == new for item in agents):
        raise DispatchError(f"{new!r} is already a live agent; a name names one pane.")

    # The space is a build cache. Moving it under an open job or an open step,
    # or over unsaved work, would lose the job's code, so all three refuse
    # before anything changes.
    blockers = [
        job.id for job in store.all_jobs() if job.is_open and old in (job.agent, job.reviewer)
    ]
    if blockers:
        raise StateError(
            f"{old} has an open job ({', '.join(sorted(blockers))}). Rename only when "
            "the space holds no open job: hand the job over or close it first."
        )
    open_steps = [task.id for task in store.all() if task.agent == old and task.is_open]
    if open_steps:
        raise StateError(
            f"{old} has an open step ({', '.join(sorted(open_steps))}). Read it with "
            f"`{PROGRAM} report {open_steps[0]}`, or abandon it, before a rename."
        )

    cwd = Path(agent.cwd) if agent.cwd else None
    if cwd is not None and gitcmd.is_repo(cwd) and not gitcmd.is_clean(cwd):
        raise StateError(
            f"{old}'s space at {cwd} has unsaved work. Commit or stash it before a rename."
        )

    move = _space_rename(cwd, old, new, config.worktree_dir)
    if move is not None and move[1].exists():
        raise StateError(
            f"a folder already exists at {move[1]}, so the space cannot move there. "
            "Choose another name."
        )
    if cwd is not None and gitcmd.is_repo(cwd) and gitcmd.branch_exists(cwd, new):
        raise StateError(
            f"a branch named {new!r} already exists. Choose another name, so a name "
            "still points at one thing."
        )
    new_cwd = move[1] if move is not None else cwd

    undo: list[Callable[[], object]] = []
    counts = {"tasks": 0, "jobs": 0, "queued": 0, "worktrees": 0}
    touched_config: Path | None = None
    touched_docs: list[Path] = []
    try:
        renamed = mux.rename_agent(old, new)
        if not renamed.ok:
            raise MuxError(f"could not rename {old} to {new}: {renamed.error_text()}")
        undo.append(lambda: mux.rename_agent(new, old))

        if move is not None:
            gitcmd.move_worktree(move[0], move[1])
            undo.append(lambda: gitcmd.move_worktree(move[1], move[0]))

        config_change = _rename_remote_pi(new_cwd, old, new)
        if config_change is not None:
            config_path, original = config_change
            touched_config = config_path
            undo.append(partial(config_path.write_text, original, encoding="utf-8"))

        for path in _roster_paths(config):
            if not path.is_file():
                continue
            original = path.read_text(encoding="utf-8")
            updated = _replace_name(original, old, new)
            if updated == original:
                continue
            path.write_text(updated, encoding="utf-8")
            touched_docs.append(path)
            undo.append(partial(path.write_text, original, encoding="utf-8"))

        # Prove the multiplexer really carries the new name before the state is
        # written. A rename that did not take must leave nothing behind.
        after = mux.list_agents()
        if not any(item.name == new for item in after):
            raise StateError(f"{new} is not in the live agent list after the rename")

        counts = store.rename_agent(
            old,
            new,
            old_path=move[0] if move is not None else None,
            new_path=move[1] if move is not None else None,
        )
        store.save()
    except Exception as exc:
        failures: list[str] = []
        for step in reversed(undo):
            try:
                step()
            except Exception as undo_exc:
                failures.append(str(undo_exc))
        if failures:
            raise StateError(
                f"the rename failed ({exc}) and could not be fully undone: "
                f"{'; '.join(failures)}. Check the space and the records before "
                "trying again."
            ) from exc
        raise

    if args.json:
        _emit_json(
            {
                "agent": old,
                "new_name": new,
                "space_from": str(move[0]) if move is not None else None,
                "space_to": str(move[1]) if move is not None else None,
                "remote_pi_config": str(touched_config) if touched_config else None,
                "documents": [str(path) for path in touched_docs],
                **counts,
            }
        )
        return 0

    where = f"; space {move[0].name} -> {move[1].name}" if move is not None else ""
    print(f"{old} renamed to {new}{where}")
    print(
        f"{INDENT}tasks {counts['tasks']}, jobs {counts['jobs']}, "
        f"queued {counts['queued']}, worktree paths {counts['worktrees']}"
    )
    if touched_config is not None:
        print(f"{INDENT}remote-pi name: {touched_config}")
    else:
        print(f"{INDENT}no remote-pi config named {old} was found")
    if touched_docs:
        print(f"{INDENT}documents: {', '.join(str(path) for path in touched_docs)}")
    else:
        print(f"{INDENT}no roster.md or AGENTS.md under the workspace root")
    if move is None:
        print(f"{INDENT}{old} is not in a linked space, so no folder moved")
    return 0


def _checkout_repos(config: Config, store: StateStore, names: list[str]) -> list[Path]:
    """The repos to inspect: the ones named, or the ones known jobs and steps use."""
    paths: list[Path] = [config.resolve_repo(name) for name in names]
    if not paths:
        paths = [Path(job.repo_path) for job in store.all_jobs()]
        paths.extend(Path(task.repo_path) for task in store.all() if task.repo_path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = normalise(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def cmd_checkouts(args: argparse.Namespace) -> int:
    """How far behind its remote each checkout is, in one line each."""
    config, store = _context(args)
    repos = _checkout_repos(config, store, args.repo)
    if not repos:
        print("no checkouts known; name a repo, or open a job first")
        return 0

    found: list[dict[str, object]] = []
    for repo in repos:
        if not gitcmd.is_repo(repo):
            continue
        if args.fetch:
            try:
                gitcmd.fetch(repo)
            except GitError as exc:
                print(f"{PROGRAM}: warning: could not fetch {repo}: {exc}", file=sys.stderr)
        for worktree in gitcmd.list_worktrees(repo):
            branch = worktree.branch
            if worktree.detached or not branch:
                behind = None
                where = f"detached @ {(worktree.commit or '?')[:7]}"
            else:
                behind = gitcmd.branch_behind(repo, branch)
                if behind is None:
                    where = "no remote"
                elif behind == 0:
                    where = "current"
                else:
                    where = f"behind {behind}"
            found.append(
                {
                    "repo": repo.name,
                    "path": str(repo),
                    "folder": worktree.path,
                    "branch": branch,
                    "state": where,
                    "behind": behind,
                }
            )

    if args.json:
        _emit_json({"checkouts": found, "count": len(found)})
        return 0
    if not found:
        print("no checkouts found")
        return 0
    rows = [["REPO", "STATE", "BRANCH", "FOLDER"]]
    for item in found:
        rows.append(
            [
                str(item["repo"]),
                str(item["state"]),
                str(item["branch"] or "-"),
                _clip(str(item["folder"]), 52),
            ]
        )
    print(_column(rows))
    return 0


def _board_path(store: StateStore) -> Path:
    """Where the page lives when nobody asked for a different place."""
    return store.path.parent / "board.html"


def _risk_memo_path(store: StateStore) -> Path:
    """The memo of at-risk answers, beside the state it describes."""
    return store.path.parent / gitcmd.RISK_MEMO_FILENAME


def _job_review_state(job: Job) -> tuple[str, str | None]:
    """Where a held candidate's work is: held, moved, or unknown.

    The branch tip is the job's current work; the held ref is what remains when the
    branch is gone. "held" means a readable ref still points at the reviewed
    commit, "moved" means a readable ref points elsewhere, and "unknown" means no
    ref could be read - which must never be reported as held.
    """
    resolved = False
    for ref in (job.branch, job.held_ref):
        if not ref:
            continue
        tip = gitcmd.commit_of(job.repo_path, ref)
        if tip is None:
            continue
        resolved = True
        if tip != job.review_commit:
            return "moved", tip
    if resolved:
        return "held", job.review_commit
    return "unknown", None


def _job_state_word(job: Job, tasks: list[Task]) -> str:
    """The job's state word, with the re-review states from the tip check.

    While a step on the job is still running, an owner-facing gate word is
    premature: the reviewer or writer has not finished, so the row says "under
    review" instead. Only a held candidate is looked up in git: every other job
    keeps its derived state with no process spent on a question that cannot change
    it.
    """
    if job.is_open and job.reviewer and job.review_commit and not job.has_pass:
        state, tip = _job_review_state(job)
        state = job.state_in_words_at(tip, unreadable=(state == "unknown"))
    else:
        state = job.state_in_words
    if state in OWNER_GATE_WORDS and step_in_flight(job, tasks):
        return "under review"
    return state


def _job_moved_past(task: Task, job: Job | None) -> bool:
    """Has the task's job branch or held ref moved past the step's commit?

    A job's branch or held ref holds the job's current work. A step commit that is
    no longer reachable from it is an earlier, rewritten version: the job moved
    past it, so it is superseded and not work at risk. A ref that is behind the
    commit (a reset) has not moved past it, so that stays at risk.
    """
    if job is None or not task.commit:
        return False
    for ref in (job.branch, job.held_ref):
        if not ref:
            continue
        tip = gitcmd.commit_of(job.repo_path, ref)
        if not tip:
            continue
        if gitcmd.is_ancestor(job.repo_path, tip, task.commit):
            continue  # the ref is behind the commit, so it has not moved past it
        return True
    return False


def _work_has_landed(task: Task, job: Job | None) -> bool:
    """Is this step's work already in the job's history, so its commit is moot?

    A note about a commit on no branch exists to catch work that could be lost
    before it lands. Once the step has reported and its job is merged, the change
    is in the history the owner merged. A later rebase can leave the recorded
    commit on no branch, but that is not a reason to keep chasing it. The claim
    keys off the step's status and the merge, not the commit's reachability alone.

    A closed job is not enough. Closing can release a space whose commit never
    merged, so a closed-but-unmerged commit stays at risk.
    """
    if task.is_open or job is None:
        return False
    return bool(job.merged_at)


def _page_stamp(path: Path) -> str | None:
    """When the page this write replaces was drawn, or None if there is none.

    Read from the page itself (`<!-- drawn: ... -->`), so the tool needs no second
    record to answer "what changed since you last drew it". An older page without the
    marker reads as None: a first draw, which is honest about knowing nothing.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"<!-- drawn: (.*?) -->", text)
    return match.group(1).strip() if match else None


def _board_data(
    config: Config,
    store: StateStore,
    *,
    live: bool = False,
    previous_draw: str | None = None,
    mux_timeout_s: float = BOARD_LIVE_MUX_TIMEOUT_S,
) -> BoardData:
    """Everything the page is drawn from.

    The roster is built from the state file unless `live` is set. That is what makes a
    board render cheap: the multiplexer read (and a git look per agent) cost seconds,
    and twenty-six commands rewrite the page on every state change. A state-only
    roster says so, in `live_ok` and in one line on the page.
    """
    if live:
        agents, live_ok, note = _board_agents(config, timeout_s=mux_timeout_s)
    else:
        agents = _board_agents_from_state(store)
        live_ok = False
        note = (
            "The agent roster was not read live; this is what the state file records. "
            f"Run `{PROGRAM} board --live` for the live list."
        )

    tasks = store.all()
    jobs = store.all_jobs()
    jobs_by_id = {job.id: job for job in jobs}
    # The defects record fails open here: the page is drawn from state, and a note in
    # the worker section names the file and the fault. `defects list` is the refusing
    # command, so a broken record never hides the page that says what is happening.
    defect_entries, defects_note = read_defects(_defect_store(config))
    open_job_ids = [job.id for job in jobs if job.is_open]
    # A held change whose branch or held ref has moved past the reviewed commit is
    # not ready for the owner's walk: the reviewer checked an older commit. Record
    # the current tip so the board asks for a re-review, with both commits named.
    # A ref that cannot be read is not called held; it is called unconfirmed.
    rereview: dict[str, str] = {}
    review_unconfirmed: set[str] = set()
    for job in jobs:
        if not (job.is_open and job.reviewer and job.review_commit and not job.has_pass):
            continue
        state, tip = _job_review_state(job)
        if state == "moved" and tip:
            rereview[job.id] = tip
        elif state == "unknown":
            review_unconfirmed.add(job.id)
    # A commit on no branch is only at risk when its change is on no branch either.
    # A rebase rewrites the hash but keeps the patch-id, so the old commit is
    # superseded, not lost. When the change cannot be compared, say so instead of
    # raising a false alarm. So does a job that moved past the commit: its branch
    # or held ref holds the later work, and the old hash is a superseded version.
    # So does a step that has reported on a job that is merged or closed: the
    # change has landed, and the step no longer waits for a worker.
    #
    # The answer is a pure function of the commit and the refs that hold it, and
    # asking git costs a process each time, so it is memoised: one look per commit
    # inside a write, and across writes while the refs have not moved.
    stranded: set[str] = set()
    risk_notes: dict[str, str] = {}
    memo = gitcmd.RiskMemo(_risk_memo_path(store))
    memo.load()
    for task in tasks:
        if not (task.commit and task.worktree):
            continue
        task_job = jobs_by_id.get(task.job or "")
        if _work_has_landed(task, task_job):
            continue  # the change is merged or closed; the commit is moot
        risk = memo.risk(task.worktree, task.commit)
        if risk == gitcmd.LOST:
            if _job_moved_past(task, task_job):
                continue
            stranded.add(task.id)
        elif risk in (gitcmd.EMPTY, gitcmd.MERGE, gitcmd.UNKNOWN):
            risk_notes[task.id] = risk
    memo.save()

    activity = handover_doc.last_activity(store)
    return BoardData(
        capacity=box.summary(box.read(), box.steps_in_flight(open_tasks(tasks)), config.floors),
        tasks=tasks,
        jobs=jobs,
        queued=store.all_queued(),
        agents=agents,
        state_path=str(store.path),
        generated_at=now_stamp(),
        drawn_at=now_iso(),
        previous_draw=previous_draw,
        last_change=stamp_of(activity) if activity is not None else None,
        live_ok=live_ok,
        note=note,
        front_door_name=config.front_door_name,
        stranded=stranded,
        risk_notes=risk_notes,
        rereview=rereview,
        review_unconfirmed=review_unconfirmed,
        dropped=audit.read_actions(store.path, "queue-drop"),
        repeated_defects=[
            defect for defect in defect_entries if defect.needs_action(open_job_ids)
        ],
        defects_note=defects_note,
    )


def cmd_board(args: argparse.Namespace) -> int:
    config, store = _context(args)
    target = Path(args.out).expanduser() if args.out else _board_path(store)
    data = _board_data(config, store, live=args.live, previous_draw=_page_stamp(target))
    written = write_board(data, target)

    if data.defects_note:
        _warn_unreadable_record(data.defects_note)

    if args.json:
        _emit_json(
            {
                "board": str(written),
                "tasks": len(data.tasks),
                "open": len(open_tasks(data.tasks)),
                "agents": len(data.agents),
                "jobs": len(data.jobs),
                "queued": len(data.queued),
                "live_ok": data.live_ok,
                "stranded": sorted(data.stranded),
                "risk_notes": data.risk_notes,
                "defects_error": data.defects_note,
            }
        )
        return RECORD_UNREADABLE if data.defects_note else 0

    print(written)
    # The state-only roster is the normal, cheap page and says so on itself. The note is
    # worth printing only when a live read was asked for and failed.
    if args.live and not data.live_ok and data.note:
        print(f"{PROGRAM}: note: {data.note}", file=sys.stderr)
    if args.open:
        webbrowser.open(written.resolve().as_uri())
    return RECORD_UNREADABLE if data.defects_note else 0


def _board_agents_from_state(store: StateStore) -> list[BoardAgent]:
    """The roster as the state file knows it, with no multiplexer call.

    The record says who work was dispatched to, and which space a job lives in; it does
    not say whether a pane is running or idle now. Every field the state cannot know is
    left empty, so the page can show "-" and say the roster was not read live. The
    newest step to an agent wins its space and pane; an open job fills a gap.
    """
    spaces: dict[str, str] = {}
    panes: dict[str, str] = {}
    own_checkout: dict[str, bool] = {}
    for task in store.all():  # oldest first, so the newest step wins
        if not task.agent:
            continue
        if task.worktree:
            spaces[task.agent] = task.worktree
            # The state records both folders, so "this is the main checkout" can be
            # answered without the live list, which is the one thing about the space
            # that costs nothing to know.
            own_checkout[task.agent] = bool(task.repo_path) and normalise(
                task.worktree
            ) == normalise(task.repo_path)
        if task.pane_id:
            panes[task.agent] = task.pane_id
    for job in store.all_jobs():
        for name in (job.agent, job.reviewer):
            if name and job.worktree and name not in spaces:
                spaces[name] = job.worktree

    agents: list[BoardAgent] = []
    for name in sorted(spaces):
        space = spaces[name]
        holding = next(
            (
                job
                for job in store.open_jobs()
                if normalise(job.worktree) == normalise(space) and job.agent == name
            ),
            None,
        )
        agents.append(
            BoardAgent(
                name=name,
                pane_id=panes.get(name),
                status=None,
                space=space,
                branch=holding.branch if holding else None,
                commit=holding.commit if holding else None,
                saved=None,
                main_checkout=own_checkout.get(name, False),
                detached=False,
            )
        )
    return agents


def _board_agents(config: Config, timeout_s: float = 15.0):
    """The live agents and their spaces. Best effort: the page works with none."""
    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    try:
        live = mux.list_agents(timeout_s)
    except MuxError as exc:
        return (
            [],
            False,
            f"The agent list could not be read ({exc}), so this page shows state only.",
        )

    agents: list[BoardAgent] = []
    for agent in live:
        copy = gitcmd.describe_copy(agent.cwd) if agent.cwd else None
        agents.append(
            BoardAgent(
                name=agent.name,
                pane_id=agent.pane_id,
                status=agent.status,
                space=agent.cwd,
                branch=copy.branch if copy else None,
                commit=copy.commit if copy else None,
                saved=(not copy.dirty) if copy else True,
                main_checkout=copy.main_checkout if copy else False,
                detached=copy.detached if copy else False,
            )
        )
    return agents, True, None


def _refresh_board(args: argparse.Namespace) -> None:
    """Rewrite the page after a command that changed state.

    Best effort by design: when the page cannot be written it warns on stderr and
    the command still succeeds. It draws from the state file and never asks the
    multiplexer, so a state change pays for a page, not for a live roster.
    """
    try:
        config, store = _context(args)
        target = _board_path(store)
        data = _board_data(config, store, previous_draw=_page_stamp(target))
        write_board(data, target)
    except Exception as exc:  # the board must never fail the command it follows
        print(f"{PROGRAM}: could not refresh the board: {exc}", file=sys.stderr)


def cmd_state_repair(args: argparse.Namespace) -> int:
    """Drop a field no copy knows, with a backup and an audit line.

    The reader half skips an unknown field and keeps it. When a stale copy keeps
    writing it back, the field has to be removed from the record. This refuses
    without a backup, writes it first, names what it removed, and audits the act.
    """
    config, store = _context(args)
    field = (args.drop_unknown or "").strip()
    if not field:
        raise UsageError("--drop-unknown FIELD is required")
    if not args.backup:
        raise UsageError(
            "--backup PATH is required: a repair writes a copy of the state file first"
        )
    backup = Path(args.backup).expanduser().absolute()
    if backup.exists():
        raise StateError(f"backup already exists: {backup}. Choose a path that is free.")

    store.load()
    if not store.path.exists():
        raise StateError(f"no state file at {store.path}; nothing to repair")
    original = store.path.read_bytes()
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        backup.write_bytes(original)
    except OSError as exc:
        raise StateError(f"cannot write the backup {backup}: {exc}") from exc
    if backup.read_bytes() != original:
        raise StateError(f"backup {backup} does not match {store.path}; refusing to repair")

    removed = store.drop_unknown_fields(field)
    total = sum(removed.values())
    if total:
        store.save()

    who = (args.by or config.front_door_name or "unknown").strip()
    why = (args.why or f"removed unknown field {field!r} so the record can be read").strip()
    audit_file = audit.append_audit(
        store.path,
        {
            "at": now_iso(),
            "by": who,
            "action": "drop-unknown",
            "field": field,
            "why": why,
            "removed": removed,
            "backup": str(backup),
        },
    )

    if args.json:
        _emit_json(
            {
                "removed": removed,
                "backup": str(backup),
                "audit": str(audit_file),
                "state": str(store.path),
            }
        )
        return 0
    if total:
        shown = ", ".join(f"{count} {label}(s)" for label, count in removed.items() if count)
        print(f"dropped {field!r} from {shown}")
    else:
        print(f"nothing carried {field!r}")
    print(f"{INDENT}backup: {backup}")
    print(f"{INDENT}audit: {audit_file}")
    return 0


def _defect_store(config: Config) -> DefectStore:
    """The one defects record for this config."""
    return DefectStore(config.defects_path)


def _open_job_ids(store: StateStore) -> list[str]:
    return [job.id for job in store.open_jobs()]


def cmd_defects_add(args: argparse.Namespace) -> int:
    """Record one sighting: a new defect, or the same one seen again.

    The same normalised line is the same defect. A second sighting raises the count
    and moves `last_seen`; it is not cleared by the count going up, so the tool says
    here, and on the board, that it now needs a job or an accepted workaround.
    """
    config, store = _context(args)
    line = " ".join((args.line or "").split())
    if not line:
        raise UsageError("--line is required: the defect in one line")
    if "\n" in (args.line or ""):
        raise UsageError(
            "--line is one line. Split it into separate defects, or keep the detail out "
            "of the record: a defect is a line, not a report."
        )
    state = args.status
    reason = (args.reason or "").strip() or None
    if state == ACCEPTED and not reason:
        raise UsageError(
            "--state accepted-workaround needs --reason: what the workaround is and what "
            "it costs, so a later session can judge it."
        )
    if state == HAS_JOB and not args.job:
        raise UsageError("--state has-job needs --job ID")
    if args.job:
        store.get_job(args.job)  # refuse an id that names nothing
    if args.detail and args.detail_file:
        raise UsageError("pass --detail or --detail-file, not both")
    # A seed carries a date and a state, and either alone is refused: a seeded entry
    # must say both when it was recorded and where it stands today, or the record
    # would quietly claim the defect was seen now, or leave its state to a default.
    if (args.first_seen or args.last_seen or args.count is not None) and not state:
        raise UsageError(
            "a seeded entry needs --status: say where the defect stands today "
            f"({', '.join(DEFECT_STATES)})."
        )
    if args.count is not None and not args.first_seen:
        raise UsageError("--count needs --first-seen: a recorded count needs its start")
    if args.last_seen and not args.first_seen:
        raise UsageError("--last-seen needs --first-seen")

    detail = _owner_detail(args)
    defects = _defect_store(config)
    try:
        defect, created = defects.sighting(
            line,
            job=args.job,
            state=state,
            reason=reason,
            detail=detail,
            first_seen=args.first_seen,
            last_seen=args.last_seen,
            count=args.count if args.count is not None else 1,
        )
    except StateError as exc:
        raise UsageError(str(exc)) from exc

    if args.json:
        _emit_json(
            {
                "defect": defect.to_dict(),
                "defect_id": defect.id,
                "created": created,
                "repeated": defect.needs_action(_open_job_ids(store)),
            }
        )
        return 0
    if created:
        print(f"{defect.id} recorded: {defect.line}")
    else:
        print(
            f"{defect.id} seen again: {defect.count} times, last {short_date(defect.last_seen)}"
        )
    warning = merge_warning(defect)
    if warning:
        print(warning)
    return 0


def cmd_defects_list(args: argparse.Namespace) -> int:
    """Every known defect, or the ones a repetition obliges someone to act on."""
    config, store = _context(args)
    defects = _defect_store(config)
    open_jobs = _open_job_ids(store)
    entries = defects.repeated(open_jobs) if args.repeated else defects.all()
    if args.like:
        entries = [defect for defect in entries if matches_line(defect.line, args.like)]

    if args.json:
        _emit_json(
            {
                "defects": [defect.to_dict() for defect in entries],
                "count": len(entries),
                "path": str(defects.path),
                "open_jobs": open_jobs,
                "like": args.like,
            }
        )
        return 0
    if not entries:
        if args.like:
            print(
                f"no defect holds every word of {args.like!r}. This is an exact-word "
                "search: a total rephrase is not found, so read the list before adding a "
                "new one."
            )
        elif args.repeated:
            print(
                "no defect has been seen twice with no open job. A second sighting opens "
                "a job or records an accepted workaround with the reason."
            )
        else:
            print(
                f"no defect is recorded in {defects.path}. If it is not here, it is not "
                "known: record one with `clowder defects add --line TEXT`."
            )
        return 0

    rows = [["ID", "SEEN", "LAST", "STATE", "JOB", "DEFECT"]]
    for defect in entries:
        rows.append(
            [
                defect.id,
                str(defect.count),
                short_date(defect.last_seen),
                defect.state,
                defect.job or "-",
                _clip(defect.line, 90),
            ]
        )
    print(_column(rows))
    print(
        f"\n{len(entries)} defect(s) in {defects.path}. "
        f"Read one in full with: {PROGRAM} defects show <id>"
    )
    if args.like:
        print(
            f"Matched on words {args.like!r} only. A reworded defect shares no word and "
            "will not be found; read the list before adding a new one."
        )
    return 0


def cmd_defects_show(args: argparse.Namespace) -> int:
    """One defect in full. This is the only place its detail lives."""
    config, _ = _context(args)
    defects = _defect_store(config)
    defect = defects.get(args.id)

    if args.json:
        _emit_json({"defect": defect.to_dict(), "path": str(defects.path)})
        return 0
    rows = [
        ["id", defect.id],
        ["line", defect.line],
        ["seen", str(defect.count)],
        ["first seen", short_date(defect.first_seen)],
        ["last seen", short_date(defect.last_seen)],
        ["state", defect.state],
        ["job", defect.job or "(none)"],
    ]
    if defect.reason:
        rows.append(["reason", defect.reason])
    print(_column(rows))
    if defect.detail:
        print()
        for line in defect.detail.splitlines():
            print(f"{INDENT}{line}")
    print(f"\nrecord: {defects.path}")
    return 0


def cmd_handover_write(args: argparse.Namespace) -> int:
    """Write today's handover from the record, and name the file it wrote."""
    config, store = _context(args)
    defects = _defect_store(config)
    target = handover_doc.target_path(handover_doc.handover_dir(store.path), handover_doc.now())
    replaced = target.exists() and handover_doc.is_generated(target)
    path, dropped = handover_doc.write(store, defects)

    if dropped:
        print(
            f"{PROGRAM}: warning: {handover_doc.NOTES_NAME} is longer than "
            f"{handover_doc.NOTES_MAX_LINES} lines; {dropped} line(s) were not written",
            file=sys.stderr,
        )
    if args.json:
        _emit_json(
            {
                "handover": str(path),
                "replaced": replaced,
                "notes_lines_dropped": dropped,
                "defects": len(defects.all()),
            }
        )
        return 0
    print(f"wrote {path}")
    if replaced:
        print(f"{INDENT}replaced the handover already written today")
    print(f"{INDENT}generated from the record; hand-written section: {handover_doc.NOTES_NAME}")
    return 0


def cmd_box(args: argparse.Namespace) -> int:
    """Print the machine's three pools, and what the gate would decide right now.

    The reading is taken once, so every figure on the page is from the same moment,
    and the slow counters (GPU use, page file use) are read here and nowhere else:
    `clowder box` may take a few seconds, a gate may not.
    """
    config, store = _context(args)
    running = box.steps_in_flight(store.select(open_only=True))
    reading = box.read(with_gpu=True)
    decision_suite = box.decide(box.COST_SUITE, running, reading, config.floors)
    decision_model = box.decide(box.COST_MODEL, running, reading, config.floors)

    if args.json:
        _emit_json(
            {
                "read_at": reading.read_at,
                "installed_gib": _gib_value(reading.installed_bytes),
                "visible_gib": _gib_value(reading.total_bytes),
                "available_gib": _gib_value(reading.available_bytes),
                "commit_limit_gib": _gib_value(reading.commit_limit_bytes),
                "commit_headroom_gib": _gib_value(reading.commit_headroom_bytes),
                "pagefile_gib": _gib_value(reading.pagefile_bytes),
                "pagefile_percent": reading.pagefile_percent,
                "gpu_carveout_gib": _gib_value(reading.carveout_bytes),
                "gpu_used_gib": _gib_value(reading.gpu_used_bytes),
                "gpu_free_gib": _gib_value(reading.gpu_free_bytes),
                "gpu_adapter": reading.gpu_adapter,
                "gpu_holder": reading.gpu_holder,
                "unread": list(reading.unread),
                "running": [{"id": step.id, "cost": step.cost} for step in running],
                "floors": {
                    "min_free_gib": config.floors.min_free_gib,
                    "min_commit_gib": config.floors.min_commit_gib,
                    "min_gpu_gib": config.floors.min_gpu_gib,
                },
                "suite": {
                    "allowed": decision_suite.allowed,
                    "reason": decision_suite.reason,
                    "lines": decision_suite.lines,
                },
                "model": {
                    "allowed": decision_model.allowed,
                    "reason": decision_model.reason,
                    "lines": decision_model.lines,
                },
            }
        )
        return 0

    print(f"{PROGRAM} box")
    for line in box.readout(reading, running, config.floors):
        print(f"{INDENT}{line}")
    return 0


def _gib_value(value: int | None) -> float | None:
    """Bytes as GiB, one decimal, or None. The one conversion a reader can check."""
    return None if value is None else round(value / box.GIB, 1)


def cmd_start(args: argparse.Namespace) -> int:
    """What a fresh front door reads first, in one command.

    The handover and its age, with stale said plainly; the decisions that wait on
    the owner; the steps in flight; the queue; the defects seen twice with no job;
    and the defects added in the last week, so a reworded duplicate is visible to the
    person who is about to add one. The order is the order to read them in, so a start
    order is one command instead of a list of files a new session has to remember.

    A malformed defects record fails open: everything else still prints, a loud
    warning names the file and the parse error, and the exit code says the page was
    drawn with the record unreadable. `clowder defects list` is the command that
    refuses, because reading the record is its job.
    """
    config, store = _context(args)
    defects = _defect_store(config)
    entries, defect_error = read_defects(defects)
    # Staleness counts a defect sighting as work, but only if the record can be read.
    latest = handover_doc.latest(store, defects if defect_error is None else None)
    # One function decides what waits on him, and the board renders the same list.
    his, not_his = owner_mod.waiting_on_owner(store.all(), store.all_jobs())
    in_flight = store.select(open_only=True)
    queued = len(store.all_queued())
    repeated = [defect for defect in entries if defect.needs_action(_open_job_ids(store))]
    recent = recently_seen(entries, RECENT_DEFECT_DAYS)

    if args.json:
        _emit_json(
            {
                "handover": str(latest.path) if latest.path else None,
                "handover_age_days": latest.age_days,
                "handover_stale": latest.stale,
                "handover_stale_why": latest.why,
                "waiting": [
                    {
                        "kind": item.kind,
                        "heading": item.heading,
                        "action": item.action,
                        "waited": item.waited,
                        "id": item.id,
                    }
                    for item in his
                ],
                "not_yours": [
                    {"heading": item.heading, "note": item.note, "id": item.id}
                    for item in not_his
                ],
                "in_flight": [task.id for task in in_flight],
                "in_flight_cost": [
                    {"id": task.id, "cost": box.cost_of(task)} for task in in_flight
                ],
                "box": box.summary(box.read(), box.steps_in_flight(in_flight), config.floors),
                "queue": queued,
                "repeated_defects": [defect.id for defect in repeated],
                "recent_defects": [defect.id for defect in recent],
                "defects_error": defect_error,
            }
        )
        return RECORD_UNREADABLE if defect_error is not None else 0

    if latest.path is None:
        print(f"handover: none - {latest.why}")
    else:
        age = f"{latest.age_days} day(s) old" if latest.age_days is not None else "age unknown"
        state = "STALE" if latest.stale else "current"
        print(f"handover: {latest.path} | {age} | {state}")
        if latest.stale:
            print(f"{INDENT}{latest.why}")
            print(f"{INDENT}write the next one with: {PROGRAM} handover write")

    print(f"\nwaiting on you: {len(his)}")
    for number, item in enumerate(his, start=1):
        print(
            f"{INDENT}{number}. {_clip(item.heading, 80)} - "
            f"{owner_mod.answered_by(item, number)}; waited {item.waited}"
        )
    if not his:
        print(f"{INDENT}(nothing recorded)")
    if not_his:
        print(f"\nnot yours: {len(not_his)}")
        for other in not_his:
            print(f"{INDENT}- {_clip(other.heading, 80)} - {other.note}; waited {other.waited}")

    print(f"\nsteps in flight: {len(in_flight)}")
    steps = box.steps_in_flight(in_flight)
    for task in in_flight:
        cost = box.cost_of(task)
        klass = "" if cost == box.COST_NONE else f" [{cost}]"
        print(f"{INDENT}- {task.id} {task.agent}{klass}: {_clip(task.question, 70)}")
    if not in_flight:
        print(f"{INDENT}(none)")
    # What the box has left, so a step that waits says why. This is the same line the
    # board carries; `clowder box` prints the numbers behind it.
    print(f"{INDENT}{box.summary(box.read(), steps, config.floors)}")

    print(f"\nqueue: {queued} item(s) waiting to be sent")

    if defect_error is not None:
        _warn_unreadable_record(defect_error)
        print("\ndefects: unreadable, so neither list is shown")
        print(f"{INDENT}read the error with: {PROGRAM} defects list")
        return RECORD_UNREADABLE

    print(f"\ndefects seen twice with no open job: {len(repeated)}")
    for defect in repeated:
        print(f"{INDENT}- {defect.id} {_clip(defect.line, 70)} ({defect.count} times)")
    if not repeated:
        print(f"{INDENT}(none)")

    print(f"\ndefects seen in the last {RECENT_DEFECT_DAYS} days: {len(recent)}")
    for defect in recent:
        print(f"{INDENT}- {defect.id} {_clip(defect.line, 70)}")
    if not recent:
        print(f"{INDENT}(none)")
    return 0


def _warn_unreadable_record(error: str) -> None:
    """The loud warning both drawing commands print, naming the file and the fault."""
    print(
        f"{PROGRAM}: warning: the defects record could not be read: {error}. "
        "The page is drawn from everything else; "
        f"`{PROGRAM} defects list` shows the error until it is fixed.",
        file=sys.stderr,
    )


def cmd_config(args: argparse.Namespace) -> int:
    config, store = _context(args)
    payload = config.as_dict()
    payload["state_path"] = str(store.path)

    if args.json:
        _emit_json(payload)
        return 0

    rows = [["SETTING", "VALUE"]]
    for key, value in payload.items():
        if isinstance(value, dict):
            if not value:
                rows.append([key, "(none)"])
            for name, path in value.items():
                rows.append([f"{key}.{name}", str(path)])
        else:
            rows.append([key, str(value) if value is not None else "(unset)"])
    print(_column(rows))
    if not store.path.exists():
        print(f"\nstate file does not exist yet: {store.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The console may not encode what an answer holds (cp1252 on this box). Relax
    # only the error mode, once, so a report prints with a replacement character
    # instead of raising UnicodeEncodeError out of the command. The getattr guard
    # is needed: io.StringIO (tests, embedders) has no reconfigure, and the
    # streams can be None.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    code = 1
    try:
        try:
            code = args.handler(args)
        except ClowderError as exc:
            print(f"{PROGRAM}: {exc}", file=sys.stderr)
            code = exc.exit_code
        except KeyboardInterrupt:
            print(f"{PROGRAM}: interrupted", file=sys.stderr)
            code = 130
    finally:
        # A command that changes state rewrites the page, whichever way it ended,
        # so an open tab never shows a stale board. It never fails the command.
        if getattr(args, "refreshes_board", False):
            _refresh_board(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
