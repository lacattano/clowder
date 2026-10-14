# `extensions/diff.ts`

## Purpose

This is the clowder Pi extension that adds the `/diff` command: review one file at
a time in an interactive terminal Pi. It runs `git diff` read-only, splits the
output into per-file blocks, and shows them in a bounded, keyboard-driven viewer.
From the viewer the owner can finish or ask Pi to explain the current file.

The bare form reviews the working tree against the index, `--cached`/`--staged`
reviews the index against `HEAD`, and one revision or range reviews a committed
branch against its base. It is the tool the reviewer role uses to walk the owner
through a change, file by file.

`/diff` never edits, stages, commits or runs code. It reports a Git snapshot, not
proof of which agent made the changes.

## Public API

### Types

```ts
export interface DiffFile { header: string; title: string; lines: string[]; }
export interface DiffRequest { staged: boolean; stat: boolean; detail: boolean; rev: string; }
export interface ExecResult { stdout: string; stderr: string; code: number | null; killed: boolean; }
export type Exec = (command: string, args: string[],
                    options: { cwd: string; timeout: number }) => Promise<ExecResult>;
export interface PlaceInfo {
  folder: string; isRepo: boolean; branch: string | null; commit: string | null;
  baseName: string | null; baseCommit: string | null; shared: boolean;
  behind: boolean; line: string;
}
```

- `DiffFile` - one file block: the Git `diff --git` header, a display title, and
  the raw lines.
- `DiffRequest` - the parsed command flags.
- `Exec` / `ExecResult` - the shaped `pi.exec` call the code takes as a test seam.
- `PlaceInfo` - where the checkout is and what base the diff uses; `line` is the
  one-line human summary.

### Functions

```ts
export function parseRequest(args: string): DiffRequest | string
export function parseDiff(output: string): DiffFile[]
export function colourLine(theme: Theme, raw: string): string
export async function describePlace(exec: Exec, cwd: string, stateText?: string): Promise<PlaceInfo>
export function translateGitError(stderr: string, request: DiffRequest): string
export function emptyMessage(request: DiffRequest, place: PlaceInfo): string
export function explainPrompt(file: DiffFile, scope: string, place?: string): string
export default function registerDiffExtension(pi: ExtensionAPI): void
```

- `parseRequest(args)` - parses the command line. Returns a usage string instead of
  a request when the input is invalid (unknown flag, more than one revision, or a
  revision combined with `--cached`).
- `parseDiff(output)` - splits `git diff` output into `DiffFile` blocks. Only
  `diff --git` starts a file; patch content always has a leading marker.
- `colourLine(theme, raw)` - colours one raw line by its Git role (header, hunk,
  added, removed, context). Returns a string.
- `describePlace(exec, cwd, stateText?)` - says where this copy is and what base
  the diff uses. Every Git call is best effort: a missing answer is a fact to
  report, not a crash.
- `translateGitError(stderr, request)` - one plain line for a common Git failure
  (not a repo, no commits, unknown revision), with the raw error left behind
  `--detail`.
- `emptyMessage(request, place)` - why an empty diff is empty, in one line.
- `explainPrompt(file, scope, place?)` - the prompt sent to Pi when the owner
  presses `e`: review one file, do not edit, start with purpose, at most three
  change blocks, separate verified reasons from guesses, about 200 words.

### `DiffViewer` class

```ts
export class DiffViewer implements Component {
  constructor(files, theme, rows, done, requestRender, position?, place?)
  handleInput(data: string): void
  render(width: number): string[]
  invalidate(): void
}
```

- `handleInput(data)` - keyboard: `n`/right/enter next file, `b`/left back,
  up/down/page/home/end scroll, `e` close with `explain`, `q`/Esc/Ctrl-C close
  with `finish`.
- `render(width)` - draws the current file with color, wrapping, a row counter and
  a help block. It asks for a larger terminal when the viewport is too small.
- `invalidate()` - a no-op; colours and wrapping are recomputed on every render,
  including after a resize.

### `registerDiffExtension(pi)` (default export)

Registers the `/diff` command with Pi. Only runs in `tui` mode.

## How It Works (internals)

Private helpers are grouped under the public function that calls them.

### `describePlace(exec, cwd, stateText?)` - name this checkout and its base

- `heldBranch(stateText, commit) -> string | null` - reads the clowder state JSON
  and returns the writer's branch whose `review_commit` equals `commit`, or `null`.
  The point: a free space pinned at a commit can say whose save it holds. A state
  that cannot be read just means the branch is unnamed.
- `BASE_NAMES = ["main", "master", "trunk", "develop"]` - the base branch names
  `describePlace` probes in order.

`describePlace` runs a small set of best-effort `git` calls (inside-work-tree,
branch, short and full HEAD, git dir, common dir, base name, base commit, behind
count) and builds the one-line `line`.

### `colourLine(theme, raw)` - draw one line

- `displayText(text) -> string` - neutralizes source control characters (tabs to
  four spaces, other control bytes to `?`) before clowder adds its own ANSI colour,
  so a file cannot inject terminal control sequences.

### `registerDiffExtension(pi)` - the `/diff` command

- `readStateText() -> string | undefined` - reads the clowder state file
  (`$CLOWDER_STATE` or `~/.clowder/state.json`), best effort, for `heldBranch`.
- `EXPLAIN_CHAR_LIMIT = 24000` - the patch excerpt limit for the explain prompt.
- `USAGE` - the usage line shown for a bad command.

The handler parses the request, refuses non-TUI mode, builds a per-place position
key, reads `describePlace`, runs one read-only `git --no-pager diff` with
`--no-color --no-ext-diff --no-textconv`, then either shows the error, the stat, the
empty message, or opens `DiffViewer` as an overlay. On `explain` it sends
`explainPrompt` as a user message (as a follow-up when the agent is busy). It saves
the viewed file and scroll position per key so `/diff` returns to the same place.
