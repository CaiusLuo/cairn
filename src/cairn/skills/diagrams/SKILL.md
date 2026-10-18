---
name: diagrams
description: Draw a boxed ASCII flow or state diagram when control flow or state transitions genuinely resist prose. Use before reviewing a change to a state machine, when taking over unfamiliar code whose flow spans several modules, when debugging logic you keep failing to correct, or when a diagram is going into documentation. Skip single functions and small linear changes.
---

# Drawing a diagram

A diagram is worth drawing when the reader cannot hold the behaviour in their
head from one function. It is waste when the answer is three lines of code.

## Decide first: is this the moment?

Draw when **at least one** is true:

- The flow spans modules: who owns state, and what survives a failure, is spread
  across files the reader would have to assemble themselves.
- It is a state machine, or an admission/fallback ladder where each step has a
  different failure consequence.
- The reader is about to approve or review a change and needs to see that the
  author's mental model matches the code.
- The logic has already defeated you once: you changed it, the change was wrong,
  and you still cannot say why. Lay the transitions out before changing them
  again.
- The diagram is a deliverable: documentation, a PR description, a design note.

Do **not** draw when **any** is true:

- It is one function with linear control flow. Quote the code instead.
- The change is small and local — one file, a few dozen lines, no new state.
- Existing tests already pin the behaviour and the diagram would only restate
  them.
- The flow is already documented where the reader will look.

If none of the draw conditions clearly holds, say so and ask rather than
producing a diagram nobody requested. An unrequested diagram costs tokens and
adds a comment someone must maintain.

## Read the transition sites, not the names

Every arrow must come from a line you have actually read: an assignment, a
compare-and-swap, a `raise` and the `except` that owns it, a state constant, or a
guard clause. If you cannot point at the line, do not draw the arrow.

When two modules both advance the same lifecycle, stop. That is a defect to
report, not a diagram to draw — a picture of a contested lifecycle hides it.

## Build it with a throwaway script

Layout arithmetic does not generalise: a fan-out, a linear chain and an annotated
ladder each need different numbers. Write one small script per diagram, run it,
copy the output into the file, delete the script. Never add the generator, a sync
test, or a "generated, do not edit" banner to the repository — that is
infrastructure with no caller, and it ties the picture to layout code instead of
to meaning.

```python
W, H = 60, 12
cv = [[" "] * W for _ in range(H)]
IW = 21  # inner width
BW = IW + 2  # box width including corners


def box(x, y, label):
    for i, r in enumerate(
        ("┌" + "─" * IW + "┐", "│" + label.center(IW) + "│", "└" + "─" * IW + "┘")
    ):
        for j, c in enumerate(r):
            cv[y + i][x + j] = c


def v(x, y0, y1):
    for y in range(y0, y1 + 1):
        cv[y][x] = "│"


# edges first, boxes last
v(12, 3, 4)
cv[4][12] = "v"
box(1, 0, "ready")
box(1, 5, "executing")

for row in cv:
    print("".join(row).rstrip())
```

Rules that prevent the usual breakage:

- **Edges first, boxes last.** A horizontal run drawn over a corner produces
  `───>│ label │` with a torn border.
- **Count characters, not bytes.** `┌` is one cell and three bytes, so `awk
  length`, `wc -c` and `cat -A` all misjudge box width. Use Python `len` on the
  decoded line.
- **Arrowheads touch the border.** A `v` one row short of the box reads as a
  dangling edge.
- **A label needs a wide enough gap.** If it does not fit between two boxes,
  widen the gap or put the label on its own row; never let it overlay `│`.

## Shapes that recur

| Shape | Layout |
| --- | --- |
| State machine with a return edge | two columns; drop down the right column, run a lane back, arrow up into the target |
| One entry, several terminal states | source on the left, terminals side by side; fan out on a shared lane row |
| Linear chain, one trigger per step | one box per row, arrow and label between them |
| Admission ladder with per-step failure | vertical lane; annotate each failure to the right of its box |
| Single path with branch notes | vertical lane; `#` notes to the right, clear of the arrow column |

## Place and verify

Put the art directly above the definition it describes, indented to match it:
class-level diagrams at column 0, method-level diagrams at the method's
indentation. Follow the art with the rules a picture cannot show — invariants,
ownership, and what is deliberately *not* an arrow.

Then re-read the rendered block in the file. The comment prefix shifts every
column, so a box aligned in the script can read as ragged once indented. Run the
project's format and type checks, and confirm the diff contains only comments:

```
git diff -U0 | grep -E '^[+-]' | grep -vE '^(\+\+\+|---)' | grep -vE '^[+-]\s*#'
```

An empty result means you changed nothing executable. Delete the scripts and
leave no residue.
