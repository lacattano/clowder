# `clowder/owner.py`

## Purpose

The one answer to "what waits on me". Seen 2026-10-26: `clowder start` printed
"decisions waiting on you: 0" while a change had waited for a walkthrough for a day, and
the board showed it, so the owner stopped trusting either view. The list is built once
here, from the state records, and `start`, the board's "For you" section and the generated
handover all render it: a count cannot say zero while a line is on the page.

Three kinds count as waiting on him:

- `decision` - a question recorded on a task (`owner --item`, or `--open-decision`);
- `merge` - a change that is passed, published, and waiting only for his merge word;
- `walkthrough` - a change whose review is finished and which waits for him to be walked
  through it.

A fourth kind is honest but not his, returned separately: a passed change that is not yet
published (the front door publishes it and CI runs there), and one whose word is recorded
and is waiting on CI before the front door merges it. Views show those as "not yours" and
never count them.

The module reads nothing and runs nothing: it takes the tasks and jobs it is given.

## Public API

```python
DECISION = "decision"
MERGE = "merge"
WALKTHROUGH = "walkthrough"
ACTIONS = {DECISION: "reply with your answer", MERGE: "reply merge",
           WALKTHROUGH: "reply pass, or what to change"}
HELD_QUIET_AFTER_SECONDS = 86400.0

@dataclass(frozen=True)
class Waiting:
    kind: str
    heading: str
    note: str
    action: str
    since: str
    id: str
    detail: str | None = None
    # waited (property) -> str

@dataclass(frozen=True)
class NotYours:
    heading: str
    note: str
    since: str
    id: str
    # waited (property) -> str

def names_the_merge(text: str) -> bool
def waiting_on_owner(tasks, jobs, *, rereview=None, review_unconfirmed=())
    -> tuple[list[Waiting], list[NotYours]]
```

- `Waiting` - one thing in his queue. `heading` is the thing in his words; `note` is
  where it stands; `action` is the word that answers it; `since` is the record's moment,
  and `waited` turns it into "3h". `id` is the task or job id for the record only - views
  never print it in his section.
- `NotYours` - work he may ask about and does not have to move.
- `names_the_merge(text)` - does an owner item name the merge, so it is the merge decision
  itself? Only the literal word counts: a miss shows a duplicate, a false hit would hide a
  decision.
- `waiting_on_owner(...)` - `(his, not_his)`, both oldest first, so the longest wait is
  answered first. `rereview` maps a job id to its moved tip and `review_unconfirmed` is a
  set of job ids, both from the board's git read; they change only a note.

## How It Works (internals)

- `_open_steps(tasks)` - how many steps are open on each job. A job with one is not his
  yet: the writer or reviewer is still working.
- A closed task waits for nobody.
- A job that is passed and unpublished is "not yours"; so is one whose merge word is
  recorded and which has not merged. A passed, published job with no open step is his
  merge line, unless an owner item on that job already names the merge - then the item is
  the one line.
- A held change with no pass and no open step is his walkthrough. Its note names the
  reviewer, the checked commit, and " - gone quiet" past `HELD_QUIET_AFTER_SECONDS`
  (a day), the second quiet bound in the crew's pages.
