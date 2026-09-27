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
maintain**.

### 6. Agent and model choice is a natural-language rules file

Not a lookup table, and not a small classifier. Firstmate's `crew-dispatch.json` is prose the
front door reads at dispatch time: *"for simple bug fixes where the root cause is defined, I
use ..."*. You state preferences; the front door writes the file.

### 7. When several models match, pick by remaining quota

So nothing goes to waste, and so the mapping can be measured rather than guessed.

### 8. Pending decisions are a generated HTML board

Not a sidebar panel. It shows what is queued, what is underway, and the decisions waiting on
the human. A browser page shows far more than a terminal, and it is a file, so it survives a
fresh context.

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

- The implementation language for the CLI. Python was the original ask (Windows, no bash);
  TypeScript would match a harness extension if one is ever needed. Decide before step 1.
- Whether the front door creates worktrees or only uses ones you made. Leaning: it creates
  them, because that is mechanical.
- Whether reports come from a file the worker writes, or from its final message read out of
  its session file. The file is more robust; the session file needs no discipline.
- What happens when a task's worktree conflicts with the main checkout after the fact.
- Whether a plain prompt is enough for dispatch, or whether a job needs a marker so a worker
  can tell a peer job from the human typing.

## Prior art

[Firstmate](https://github.com/kunchenguid/firstmate) is the clearest statement of the
one-liaison idea and this design started from it. It supports eight harnesses on macOS and
Linux; this is one harness, on Windows, and small. Its licence is MIT.

Ideas borrowed, no code: the one-liaison model, ship versus scout task shapes, a plain-language
dispatch rules file, quota-aware model choice, hiding tool noise, and a generated board for
pending decisions.
