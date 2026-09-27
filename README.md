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

**Draft. Nothing is built yet.** The design, the decisions and the reasoning are in
[DESIGN.md](DESIGN.md).

## Licence

MIT. See [LICENSE](LICENSE).
