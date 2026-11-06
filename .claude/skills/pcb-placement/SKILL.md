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
error (bad file, KiCad unreachable, wrong board, outline not chosen).

## Hard rules

- The board must be open in KiCad's PCB editor; scripts talk to it over KiCad's IPC API
  only and never read or write the `.kicad_pcb` file. With the board closed, stop and ask
  the user to open it. Never read the board file yourself either: `board_io.py` gives you
  poses (`read`) and pads with their nets (`nets`).
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

Each running KiCad has its own socket in `/tmp/kicad/`: `api.sock` belongs to the first
KiCad instance started (its pid is the main `kicad` process, or the first `pcbnew` process
when the PCB editor runs on its own), `api-<pid>.sock` to each later one (`pgrep -fl
'kicad|pcbnew'` lists the pids). List the folder and run
`uv run $S/board_io.py BOARD --socket URL read` against each socket in turn (no
`placement.yaml` needed): the one that answers without "the PCB open on ... is <other
path>" is the right one. If none has the named board, ask the user to open it. Stale
sockets (from a closed KiCad) give "Connection refused".

## Workflow

1. **Gather.** Read the project spec (CLAUDE.md, spec files) and the schematic intent
   (Konnect `sch_analysis` tools or an exported netlist). Read the board:
   `board_io.py BOARD --socket URL read` (outlines, frame origin, poses, sides, locked
   flags) and `board_io.py BOARD --socket URL nets` (each part's pads, pin names, nets).
   If `read` lists more than one outline, the file holds several boards: ask which one to
   place and set `board.outline_at` (see `constraint-schema.md`).
2. **Floorplan.** Load `reference/placement-rules.md` and the matching
   `reference/block-recipes/*.md`. Write or update `placement.yaml` next to the board,
   starting from `templates/placement.yaml` (fields: `reference/constraint-schema.md`).
   Set `grid_mm` to the grid the board already uses: look at the existing poses (THT boards
   often sit on 1.27 or 0.635 mm). To place only some parts, list them under `movable:`.
   Check it: `uv run $S/validate.py BOARD placement.yaml --socket URL`; fix every error.
   Once it is valid for the first time, score the current placement and store it as the
   reference: `uv run $S/metrics.py BOARD placement.yaml --socket URL --baseline`.
   **Gate 1:** show the user a summary (fixed and movable parts, regions, groups and
   recipes, decoupling and proximity rules) and wait for approval. Commit `placement.yaml`.
3. **Place.** `uv run $S/place.py BOARD placement.yaml --socket URL --seed 1`
   (`--budget 60` seconds by default; `--iterations N` overrides the annealing length).
   It starts from the better of a freshly built placement and the current board. From the
   board it only refines: no part ends more than 2 mm from where it was, no swaps (turning
   a part in place is allowed). A part that must travel farther needs several applied runs,
   or `--from-scratch`, which rebuilds the whole placement. All moves land in one KiCad
   commit, so one Ctrl+Z in KiCad undoes the run. Add `--json` for the full metrics.
   **Gate 2 (first applied run):** ask the user to review the board in KiCad and lock the
   parts they like or place by hand. Locked parts never move in later runs.
4. **Score.** `place.py` prints the metrics of its result. Re-score any time with
   `uv run $S/metrics.py BOARD placement.yaml --socket URL` (`--json` for all fields,
   `--full` for every item instead of the 10 worst).
5. **Check with DRC.** After an applied run, run Konnect `run_drc` against the live board
   (sync the live board, not the saved file). Konnect only reaches the first KiCad instance
   (`api.sock`): if the board is open in a later one, say that DRC was not run and ask the
   user to run DRC in KiCad. Report DRC errors next to the metrics.
6. **Fix.** Hard violations: change constraints or region sizes, then go to 3. Soft cost:
   follow the iteration tactics below, then go to 3.
7. **Report.** A table of cost and each metric, baseline vs now; the remaining
   violations; and what the human should place or check (see "Rules the metrics do not
   check" in `placement-rules.md`). Remind the user to save the board in KiCad.
   **Gate 3:** once the metrics are clean, hand off to routing (the user routes critical
   nets, then the rest with Freerouting, Quilter or by hand).

## Stop when

- Hard violations are zero and the cost improved by less than 2% over the last two
  applied runs, or
- 5 applied placer runs this session (`--dry-run` runs do not count), or
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
| `history.jsonl` | One line per `metrics.py` call that scored the board and per applied `place.py` run (dry runs add none): time, board, cost, hard violations, wirelength, crossings, whether it set the baseline | Committed |
| `baseline.json` | Cost stored by `metrics.py --baseline` | Committed |

To revert an applied run after the board was saved, use the old poses in its
`moves.json` (`board_io.py BOARD --socket URL move FILE` takes `[{ref, x, y, rot}]`; add
`--outline-at X Y` before `move` when the file has several outlines).

## References (load on demand)

- `reference/placement-rules.md`: placement order, per-block defaults and how they map to
  `placement.yaml` fields (step 2).
- `reference/constraint-schema.md`: every `placement.yaml` field, what each metric
  means, what `validate.py` rejects, the block-recipe format (when writing the file).
- `reference/block-recipes/*.md`: `ldo`, `crystal`, `usb-c`, one per group with a
  `recipe:`. Check each recipe's pin assumptions against the real part first. The placer
  keeps a recipe block rigid: the anchor and its recipe parts move, turn and mirror only
  together, keeping their offsets (the recipe's on a fresh build, the board's when
  refining). Other members of the group move on their own.
