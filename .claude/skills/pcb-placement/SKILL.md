---
name: pcb-placement
description: Place and improve footprint placement on a KiCad PCB (.kicad_pcb) open in KiCad, from a constraint file (placement.yaml), an algorithmic placer and numeric metrics. Use when the user asks to place, arrange, floorplan, pack, or improve the placement of many components. Do not use for routing, schematic edits, footprint or symbol creation, Gerber or fabrication export, or moving one or two parts by hand (use Konnect's move tools for that).
---

# PCB placement

You set intent; scripts do the geometry and the measuring. Judge placement from the
metrics only, never from an image. Change intent by editing `placement.yaml`, never by
typing coordinates: coordinates come only from `place.py`.

`S=.claude/skills/pcb-placement/scripts`. Run every script as `uv run $S/<name>.py`.
Exit codes: 0 = no hard violations (or valid), 1 = hard violations (or invalid), 2 = input
error (bad file, KiCad unreachable, wrong board).

## Hard rules

- The board must be open in KiCad's PCB editor; scripts talk to it over KiCad's IPC API
  only and never read or write the `.kicad_pcb` file. With the board closed, stop and ask
  the user to open it.
- Only touch the board the user named. Every script takes the board path and
  `--socket ipc:///tmp/kicad/<file>.sock` and refuses (exit 2) when the board open on that
  socket is a different file.
- Never move a part locked in KiCad or listed under `fixed:` (the scripts refuse too).
- Commit `placement.yaml` to git before every placer run that applies moves.
- When unsure what a run will do, use `--dry-run` first: it writes the run files and
  prints the metrics without moving anything.
- KiCad does not save after a placer run. Never save on your own: the user saves after
  review, or you save with Konnect `save_project` when they ask.
- Images only when the user asks, through Konnect's render/export tools, and always shown
  next to the metrics for the same area.
- Konnect's no-direct-edit rule applies; its one exception is these scripts moving and
  rotating footprints through KiCad's IPC API.

## Finding the socket

Each running PCB editor has its own socket in `/tmp/kicad/`: `api.sock` for the first,
`api-<pid>.sock` for later ones (`pgrep -fl pcbnew` lists the pids). List the folder and
run `validate.py` against each socket in turn: the one that answers without
"the PCB open on ... is <other path>" is the right one. If none has the named board, ask
the user to open it. Stale sockets (from a closed editor) give "Connection refused".

## Workflow

1. **Gather.** Read the project spec (CLAUDE.md, spec files) and the schematic intent
   (Konnect `sch_analysis` tools or an exported netlist). Read the board:
   `uv run $S/board_io.py BOARD --socket URL read` (poses, sides, locked flags).
   Score the current placement and store it as the reference:
   `uv run $S/metrics.py BOARD placement.yaml --socket URL --baseline`.
2. **Floorplan.** Load `reference/placement-rules.md` and the matching
   `reference/block-recipes/*.md`. Write or update `placement.yaml` next to the board,
   starting from `templates/placement.yaml` (fields: `reference/constraint-schema.md`).
   Check it: `uv run $S/validate.py BOARD placement.yaml --socket URL`; fix every error.
   **Gate 1:** show the user a summary (fixed parts, regions, groups and recipes,
   decoupling and proximity rules) and wait for approval. Commit `placement.yaml`.
3. **Place.** `uv run $S/place.py BOARD placement.yaml --socket URL --seed 1`
   (`--budget 60` seconds by default; `--iterations N` overrides the annealing length).
   It starts from the better of a freshly built placement and the current board. From the
   board it only refines: no part moves more than 2 mm, no swaps. `--from-scratch`
   rebuilds the whole placement. All moves land in one KiCad commit, so one Ctrl+Z in
   KiCad undoes the run. Add `--json` for the full metrics in one line.
   **Gate 2 (first applied run):** ask the user to review the board in KiCad and lock the
   parts they like or place by hand. Locked parts never move in later runs.
4. **Score.** `place.py` prints the metrics of its result. Re-score any time with
   `uv run $S/metrics.py BOARD placement.yaml --socket URL` (`--json` for all fields,
   `--full` for every item instead of the 10 worst).
5. **Check with DRC.** After an applied run, run Konnect `run_drc` against the live board
   (sync the live board, not the saved file). Report DRC errors next to the metrics.
6. **Fix.** Hard violations: change constraints or region sizes, then go to 3. Soft cost:
   follow the iteration tactics below, then go to 3.
7. **Report.** A table of cost and each metric, baseline vs now; the remaining
   violations; and what the human should place or check (see "Rules the metrics do not
   check" in `placement-rules.md`). Remind the user to save the board in KiCad.
   **Gate 3:** once the metrics are clean, hand off to routing (the user routes critical
   nets, then the rest with Freerouting, Quilter or by hand).

## Stop when

- Hard violations are zero and the cost improved by less than 2% over the last two runs, or
- 5 placer runs this session, or
- the same hard violation survives 2 fixes: stop and ask, naming the likely cause (board
  too small, region too tight, constraints that contradict each other).

`place.py` stops at `--budget` seconds; when it says "budget cap hit", the result depends
on machine speed and is not reproducible by seed.

## Iteration tactics, cheapest first

1. Try 2-3 more seeds (`--seed 2`, `--seed 3`), with `--dry-run` to compare costs before
   applying the best one.
2. Raise the weight of the worst soft metric under `weights:`, or add a `critical_nets` entry.
3. Resize or move a region.
4. Split or merge a group.
5. Ask the user to hand-place and lock the part causing trouble.

## Run files

`.placement/` sits next to `placement.yaml`:

| Path | Content | Git |
| --- | --- | --- |
| `runs/<timestamp>/moves.json` | Every part the run moved: old and new pose; seed, start, costs | Ignored |
| `runs/<timestamp>/metrics.json` | Full metrics of the run's result | Ignored |
| `history.jsonl` | One line per scored run (time, cost, hard violations, wirelength, crossings) | Committed |
| `baseline.json` | Cost stored by `metrics.py --baseline` | Committed |

To revert an applied run after the board was saved, use the old poses in its
`moves.json` (`board_io.py BOARD --socket URL move FILE` takes `[{ref, x, y, rot}]`).

## References (load on demand)

- `reference/placement-rules.md`: placement order, per-block defaults and how they map to
  `placement.yaml` fields (step 2).
- `reference/constraint-schema.md`: every `placement.yaml` field, what each metric
  means, what `validate.py` rejects, the block-recipe format (when writing the file).
- `reference/block-recipes/*.md`: `ldo`, `crystal`, `usb-c`, one per group with a
  `recipe:`. Check each recipe's pin assumptions against the real part first. A recipe
  sets where its parts start; the annealer may still shift or turn them to lower the cost,
  so back the distances that matter with `decoupling` or `proximity` rules.
