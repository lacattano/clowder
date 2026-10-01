"""The command line. This is the spine the front door calls.

Three commands carry the state: dispatch, tasks, report. Two more explain the
setup: agents, config.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__, audit, gitcmd
from .board import BoardAgent, BoardData, now_stamp, open_tasks, write_board
from .config import BUILT_DELIVERY_MODES, Config, load_config
from .errors import ClowderError, DispatchError, GitError, MuxError, StateError, UsageError
from .marker import DEFAULT_SENDER, apply_marker
from .mux import AgentInfo, Mux
from .report import INDENT, build_report, usage_breakdown
from .sessions import read_answer, read_usage
from .state import (
    CLOSED,
    DISPATCHED,
    FAILED,
    REPORTED,
    SHAPES,
    STATUSES,
    Job,
    Queued,
    StateStore,
    Task,
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
# hold that command up. The live list is read with this timeout; when it does not
# answer, the page is written from state alone.
BOARD_REFRESH_MUX_TIMEOUT_S = 3.0


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
    list_tasks.set_defaults(handler=cmd_tasks)

    inbox = sub.add_parser(
        "inbox", help="what has reported since you last looked", parents=[common]
    )
    inbox.add_argument("--json", action="store_true")
    inbox.set_defaults(handler=cmd_inbox)

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
    show.add_argument("--json", action="store_true")
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
    owner.add_argument("--clear", action="store_true", help="he has answered it; remove it")
    owner.add_argument("--json", action="store_true")
    owner.set_defaults(handler=cmd_owner, refreshes_board=True)

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
    j_open.add_argument("--role", default=DEFAULT_ROLE, choices=ROLES)
    j_open.add_argument("--name", help="use or make an agent with this name")
    j_open.add_argument("--branch", help="branch name (default: job prefix plus label)")
    j_open.add_argument("--base", help="what to branch from (default: the base branch)")
    j_open.add_argument("--kind", default=DEFAULT_KIND)
    j_open.add_argument("--direction", choices=("right", "down"), default=DEFAULT_DIRECTION)
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

    j_publish = job_parser("publish", "push the job's branch, once the owner has passed it")
    j_publish.add_argument("id")
    j_publish.add_argument("--json", action="store_true")
    j_publish.set_defaults(handler=cmd_job_publish, refreshes_board=True)

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
    q_add.add_argument("--job", metavar="ID", help="the job it belongs to, if any")
    q_add.add_argument("--json", action="store_true")
    q_add.set_defaults(handler=cmd_queue_add, refreshes_board=True)

    q_list = queue_parser("list", "what is decided and still waiting to go")
    q_list.add_argument("--json", action="store_true")
    q_list.set_defaults(handler=cmd_queue_list)

    q_send = queue_parser("send", "send one queued item, once its block has cleared")
    q_send.add_argument("id")
    q_send.add_argument("--force", action="store_true")
    q_send.add_argument("--dry-run", action="store_true")
    q_send.add_argument("--json", action="store_true")
    q_send.set_defaults(handler=cmd_queue_send, refreshes_board=True)

    paths = sub.add_parser("config", help="show resolved settings and paths", parents=[common])
    paths.add_argument("--json", action="store_true")
    paths.set_defaults(handler=cmd_config)

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


# -- commands --------------------------------------------------------------


def cmd_dispatch(args: argparse.Namespace) -> int:
    config, store = _context(args)
    return _dispatch(args, config, store)


def _dispatch(args: argparse.Namespace, config: Config, store: StateStore) -> int:
    brief = _read_brief(args)
    question = (args.question or "").strip() or _first_line(brief)

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
        who = [name for name in (job.agent, job.reviewer) if name]
        if args.agent not in who:
            checked = f", checked by {job.reviewer}" if job.reviewer else ""
            raise DispatchError(
                f"{job.id} is {job.agent}'s job{checked}, and you named "
                f"{args.agent}. The writer and its reviewer take the steps."
            )
        worktree = job.worktree

    mux = Mux(config.mux_bin, config.mux_prompt_argv)

    agent_info = None
    if not args.force and not args.dry_run:
        agent_info = _lookup_agent(mux, args.agent)
        _refuse_cross_repo(args, agent_info, repo_path)
        if job is not None:
            _refuse_outside_job(args, agent_info, job, store)

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

    result = mux.prompt(
        args.agent,
        message,
        timeout_s=args.timeout,
        wait=args.wait,
        until=args.until,
    )
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
            "heavy runs start at once."
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
        _emit_json({"tasks": [t.to_dict() for t in tasks], "count": len(tasks)})
        return 0

    if not tasks:
        print("no tasks")
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
    unanswered = sum(1 for t in tasks if not t.answer)
    print(f"\n{len(tasks)} task(s), {open_count} still open, {unanswered} with no answer")
    return 0


def _agents_by_name(mux: Mux) -> dict[str, AgentInfo]:
    """The live roster as a lookup. Best effort: an unreadable list is empty."""
    try:
        return {agent.name: agent for agent in mux.list_agents()}
    except MuxError:
        return {}


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
        session = (agent.session_path if agent else None) or task.agent_session
        since = to_epoch(task.dispatched_at) or to_epoch(task.created_at)
        answer = read_answer(session, since) if session else None
        if answer:
            found.append((task, answer))
    return found


def cmd_inbox(args: argparse.Namespace) -> int:
    config, store = _context(args)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)
    found = _reported_since_look(store, _agents_by_name(mux))

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
    print(f"\n{len(found)} step(s) have reported. Read one with: {PROGRAM} report <id>")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config, store = _context(args)
    task = store.get(args.id)
    mux = Mux(config.mux_bin, config.mux_prompt_argv)

    session_path, agent_status = _resolve_session(mux, task)
    since = to_epoch(task.dispatched_at) or to_epoch(task.created_at)

    usage = read_usage(session_path, since) if session_path else None
    answer = task.answer or (read_answer(session_path, since) if session_path else None)

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
        changed = False
        if usage is not None:
            task.usage = usage.to_dict()
            changed = True
        # The directory means different code at different times, so the commit is
        # what a report is really about.
        if task.worktree and gitcmd.is_repo(task.worktree):
            head = gitcmd.head_commit(task.worktree)
            if head and head != task.commit:
                task.commit = head
                changed = True
        # The reachability gate, where a worker makes its claim. A commit that is on
        # no branch exists only in that folder, and reusing the folder destroys it.
        if task.commit and task.worktree:
            stranded = not gitcmd.is_reachable(task.worktree, task.commit)
        if answer and (task.answer != answer or task.status != REPORTED) and settled:
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
            changed = True
        if changed:
            store.save()

    if args.json:
        _emit_json(
            {
                "task": task.to_dict(),
                "usage": usage.to_dict() if usage else None,
                "answer": answer,
                "answer_source": source,
                "agent_status": agent_status,
                "session_file": session_path,
                "commit_on_no_branch": stranded,
            }
        )
        return 0

    print(build_report(task, usage, answer))
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
    return 0


def cmd_owner(args: argparse.Namespace) -> int:
    """Record or clear one thing that waits on the owner, against a task."""
    _, store = _context(args)
    task = store.get(args.id)
    if bool(args.item) == bool(args.clear):
        raise UsageError("pass exactly one of --item TEXT or --clear")
    if args.item:
        text = args.item.strip()
        if not text:
            raise UsageError("--item needs a line of text")
        task.owner_item = text
        task.owner_item_at = now_iso()
        note = f"{task.id}: recorded as waiting on the owner"
    else:
        task.owner_item = None
        task.owner_item_at = None
        note = f"{task.id}: cleared from what waits on the owner"
    store.save()

    if args.json:
        _emit_json({"task": task.to_dict()})
        return 0
    print(note)
    return 0


def _resolve_session(mux: Mux, task: Task) -> tuple[str | None, str | None]:
    """Prefer the live answer, because a pane can be reset to a new session."""
    try:
        agent = mux.find_agent(task.agent)
    except MuxError:
        return task.agent_session, None
    if agent is None:
        return task.agent_session, None
    return agent.session_path or task.agent_session, agent.status


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
        worktree_dir=config.worktree_dir,
        base=config.worktree_base,
        setup=config.worktree_setup,
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

    try:
        gitcmd.fetch(repo_path)
    except GitError as exc:
        print(f"{PROGRAM}: warning: could not fetch {repo_path}: {exc}", file=sys.stderr)
    base_ref = gitcmd.resolve_base(repo_path, args.base or config.worktree_base)
    behind = gitcmd.branch_behind(repo_path, base_ref)
    if behind and not args.force:
        raise GitError(
            f"{base_ref} is {behind} commit(s) behind origin/{base_ref}. A job from a "
            "stale base carries old code, and its report would be about code that is "
            f'not current. Pull first: git -C "{repo_path}" pull --ff-only, then open '
            "the job again. Or pass --force if you know better."
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
        gitcmd.switch_new_branch(worktree, branch, base_ref)

    job = Job(
        id=store.next_job_id(),
        label=label,
        repo=args.repo,
        repo_path=str(repo_path),
        worktree=worktree,
        branch=branch,
        base=base_ref,
        agent=agent.name,
        commit=gitcmd.head_commit(worktree),
    )
    store.add_job(job)
    store.save()

    if args.json:
        _emit_json({"job": job.to_dict(), "agent": agent.name, "created": ensured.created})
        return 0

    print(f"{job.id} open: {job.branch} in {job.worktree}")
    print(f"{INDENT}{agent.name} | {job.repo} | from {job.base} | {job.commit or 'no commit'}")
    return 0


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

    if job.released_at is None:
        dirty = gitcmd.status_entries(worktree)
        if dirty and not args.force:
            shown = ", ".join(entry.split(maxsplit=1)[-1] for entry in dirty[:5])
            more = ", and more" if len(dirty) > 5 else ""
            raise GitError(
                f"{worktree} is not clean: {shown}{more}. Closing a job leaves the tree "
                "ready for the next one, so this is refused. Commit, stash, or pass --force."
            )

        on = gitcmd.current_branch(worktree)
        if on != job.branch and not args.force:
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
    else:
        # Handover already handed the space back; nothing to release.
        base_ref = gitcmd.resolve_base(job.repo_path, config.worktree_base)

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
    job.pass_shown = shown
    job.pass_answer = answer
    job.pass_at = now_iso()
    job.pass_by = by
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


def _publish_branch(job: Job) -> None:
    """Push the branch. Tests replace this; nothing else may skip the gate."""
    gitcmd.push_branch(job.worktree, job.branch)


def _merge_pull_request(job: Job, pr: str) -> None:
    """Merge the pull request through gh. Tests replace this."""
    gitcmd.run_command(f"gh pr merge {pr} --merge", job.worktree)


def cmd_job_publish(args: argparse.Namespace) -> int:
    _, store = _context(args)
    job = store.get_job(args.id)
    _require_pass(job)
    _publish_branch(job)
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
    _, store = _context(args)
    job = store.get_job(args.id)
    _require_merge_word(job)
    _merge_pull_request(job, args.pr)
    job.merged_at = now_iso()
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

    rows = [["ID", "STATUS", "REPO", "BRANCH", "AGENT", "AGE", "PASS", "WORD", "LABEL"]]
    for job in jobs:
        rows.append(
            [
                job.id,
                job.status,
                _clip(job.repo, 20),
                _clip(job.branch, 28),
                job.agent,
                human_age(job.age_seconds),
                "yes" if job.has_pass else "no",
                "yes" if job.has_merge_word else "no",
                _clip(job.label, 30),
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


def cmd_queue_send(args: argparse.Namespace) -> int:
    config, store = _context(args)
    item = store.get_queued(args.id)
    agent = _queue_target_agent(config, item)
    dispatch_args = _queued_dispatch_args(
        item,
        agent,
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
    force: bool,
    dry_run: bool,
    json: bool,
) -> argparse.Namespace:
    """The shape `dispatch` parses, so a queued send takes the same path."""
    return argparse.Namespace(
        config=None,
        state=None,
        repo=item.repo,
        worktree=None,
        job=item.job,
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

    before = agent.session_path
    result = mux.send_keys(name, ["/", "n", "e", "w", "enter"])
    if not result.ok:
        raise MuxError(f"could not type the reset into {name}: {result.error_text()}")
    after = _wait_for_session_change(mux, name, before, args.timeout)
    if after is None:
        raise StateError(
            f"{name}'s session did not change after the reset, so the context was not "
            "refreshed. The keys may not have arrived; check the pane and try again."
        )

    if args.json:
        _emit_json({"agent": name, "session_before": before, "session_after": after})
        return 0
    print(f"{name} reset: new session {after}")
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


def _board_data(config: Config, store: StateStore, mux_timeout_s: float = 15.0) -> BoardData:
    """Everything the page is drawn from."""
    agents, live_ok, note = _board_agents(config, timeout_s=mux_timeout_s)

    tasks = store.all()
    stranded = {
        task.id
        for task in tasks
        if task.commit and task.worktree and not gitcmd.is_reachable(task.worktree, task.commit)
    }

    return BoardData(
        tasks=tasks,
        jobs=store.all_jobs(),
        queued=store.all_queued(),
        agents=agents,
        state_path=str(store.path),
        generated_at=now_stamp(),
        live_ok=live_ok,
        note=note,
        front_door_name=config.front_door_name,
        stranded=stranded,
    )


def cmd_board(args: argparse.Namespace) -> int:
    config, store = _context(args)
    data = _board_data(config, store)

    target = Path(args.out).expanduser() if args.out else _board_path(store)
    written = write_board(data, target)

    if args.json:
        _emit_json(
            {
                "board": str(written),
                "tasks": len(data.tasks),
                "open": len(open_tasks(data.tasks)),
                "jobs": len(data.jobs),
                "agents": len(data.agents),
                "queued": len(data.queued),
                "live_ok": data.live_ok,
                "stranded": sorted(data.stranded),
            }
        )
        return 0

    print(written)
    if not data.live_ok and data.note:
        print(f"{PROGRAM}: note: {data.note}", file=sys.stderr)
    if args.open:
        webbrowser.open(written.resolve().as_uri())
    return 0


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
    the command still succeeds. The live list is read with a short timeout, so a
    slow multiplexer costs a state-only page rather than a slow dispatch.
    """
    try:
        config, store = _context(args)
        data = _board_data(config, store, mux_timeout_s=BOARD_REFRESH_MUX_TIMEOUT_S)
        write_board(data, _board_path(store))
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
