# clowder

**One front door for a crew of coding agents.**

You talk to one agent. It dispatches to the others, keeps the state, and brings answers back
with their question attached.

---

## The problem

Running one coding agent is easy. Running four is where it goes wrong, and not for the reason
people expect.

The agents are fine. What breaks is *you*. You become the integration layer: you carry who is
on what, which question is still unanswered, which pane you spoke to last, and which repo each
task belongs to. The moment two tasks run in parallel you are tab-juggling, and an answer
arrives forty minutes later with nothing to say which question it belongs to.

That is not a model problem. It is a bookkeeping problem, and bookkeeping should not live in
your head or in a conversation.

## What this does

You talk to one agent - a *clowder* is a group of cats, so let us call the crew that. Your
front door takes the request, works out which agent and which repo it belongs to, sends it,
records it, and reports back:

```
Re: does the site need a refund policy before the review?
    verifier | <repo> | worktree feature/refund-policy | 14s | 8.2k | $0.001
    Yes - the terms page is a placeholder with no refund window.
    Open decision: accept a 14-day policy, or wait?
```

The question comes first, so a late answer still knows what it answers. Every task runs in its
own git worktree, so parallel changes to one repo cannot collide.

### Checkouts and branches

Each agent gets a space of its own, `<repo>/.worktrees/<agent>`, which keeps its install and its
caches. A space is free (no branch on it, sitting on the base) or in use (on one job's branch).
A job is one branch, so a dependency install is paid once per agent instead of once per job.

The tool refuses a job on a dirty space, refuses to switch branches in the main checkout, and
keeps the branch when a job closes. One job is a whole chain - research, spec, change, tests,
eval, docs - with one step open at a time, so two heavy runs cannot start together.

Every agent gets a space of its own that keeps its install, so it is reused instead of rebuilt.
`clowder agents` shows what each space holds, so you can tell them apart. When a writer has
saved, hand the save to a reviewer, which checks that exact commit in its own space. Nothing is
pushed between agents; pushing is publishing, and it happens at the end. The tool never commits,
pushes or merges for you.

Add `.worktrees/` to each repo's `.gitignore`.

### The board

`clowder board --open` writes one HTML file next to the state file and opens it. It leads with
what is waiting on you - a decision, a finished review, or work that is on no branch - then open
steps, agents and their spaces, answers, and jobs. It needs no server, and it still works when
the multiplexer cannot be read.

## What this is not

- **Not a new agent runtime.** The agents are [Pi](https://github.com/earendil-works/pi)
  sessions. You keep Pi's tools, skills, context files and model switching.
- **Not a new terminal.** It drives the terminal multiplexer you already use, addressing panes
  by name. [herdr](https://herdr.dev) is the current target.
- **Not an agent framework.** LangGraph, CrewAI and AutoGen orchestrate LLM calls inside one
  process, so an "agent" becomes an LLM loop you wrote. You lose your harness. Those tools
  replace the agent; this wraps it.
- **Not a reimplementation of [Firstmate](https://github.com/kunchenguid/firstmate).** That is
  an excellent project and the clearest statement of the one-liaison idea, and it is where the
  thinking here started. It supports eight harnesses on macOS and Linux. This is Pi, on
  Windows, and small.

## Status

**Steps 1 to 4 are built: the CLI, the front-door skill, spaces and jobs, and the board.** 269
tests, no dependencies. `/calm` is not built yet. The design, the decisions and the reasoning are
in [DESIGN.md](DESIGN.md).

### Use it

Needs Python 3.14. Nothing to install:

```
py -3.14 -m clowder --help
py -3.14 -m clowder config          # what the tool resolved
py -3.14 -m clowder agents          # the live roster, from the multiplexer
py -3.14 -m clowder ensure myrepo --role verifier   # or make one there
py -3.14 -m clowder job open myrepo --label refund --role maker
py -3.14 -m clowder job handover j-0001 --to verifier
py -3.14 -m clowder job list
py -3.14 -m clowder job close j-0001
py -3.14 -m clowder tasks           # what is queued, underway, unanswered
py -3.14 -m clowder board --open    # the page: what is waiting on you
py -3.14 -m clowder dispatch myrepo-maker myrepo "ship: add the refund page" --job j-0001
py -3.14 -m clowder report t-0001
```

Copy [clowder.example.toml](clowder.example.toml) to `~/.clowder/clowder.config.toml` first,
and set the workspace root. The tool ships no repo name, no path and no agent name.

Tests, also nothing to install:

```
py -3.14 -m unittest discover -s tests -t .
```

### Developing clowder

One command runs every gate, locally and in CI, so the list cannot drift between what you run
and what the robot runs:

```
py -3.14 scripts/check.py          # every gate
py -3.14 scripts/check.py --list   # what they are
```

The gates are **smoke** (the CLI runs with no multiplexer), **lint** (`ruff check`), **format**
(`ruff format --check`), **type** (`mypy clowder`) and **tests** (the whole suite). `ruff` and
`mypy` are fetched with `uvx`, so nothing needs installing first.

The flow for a change: run the gates, commit, push to `main`. CI runs the same five gates as
separate checks (`.github/workflows/ci.yml`), so a red one names itself. The tool never commits,
pushes or merges anything on your behalf - delivery is `local-only`, and the merge is yours.

### The front-door skill

[`skills/front-door/SKILL.md`](skills/front-door/SKILL.md) tells one agent how to act as the
front door: when to dispatch, how to write a brief, how to report an answer back, and the
rules that must hold every time. Install it as a Pi package:

```
pi install ./clowder
```

## Licence

MIT. See [LICENSE](LICENSE).
