# /// script
# requires-python = ">=3.12"
# dependencies = ["kicad-python==0.8.0", "shapely>=2.0", "pydantic>=2.6", "pyyaml>=6", "numpy>=1.26"]
# ///
"""Place the movable footprints of a board open in KiCad from placement.yaml.

    place.py BOARD PLACEMENT_YAML --socket URL [--seed N] [--budget S] [--iterations N]
             [--from-scratch] [--dry-run] [--json]

Runs the placer (see placer.py), writes .placement/runs/<timestamp>/moves.json (only parts
whose pose changed) and metrics.json next to PLACEMENT_YAML, applies the moves to the board
in one KiCad commit (one undo reverts the run; skipped with --dry-run), then prints the
metrics summary of the result.

The same seed, input and --iterations give identical moves, unless the --budget runs out
first: the output then says "budget cap hit" and the result depends on machine speed.
By default the annealer starts from the better of the constructed placement and the current
board; starting from the board it only refines (small moves near each part's current spot).
--from-scratch always starts from the constructed placement and may move anything.

Exit codes: 0 no hard violations, 1 hard violations, 2 input error.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from board_io import BoardIOError, apply_moves, open_board, read_board
from board_model import BoardData, Move, Pose
from constraints import PlacementFileError, load_placement
from metrics import exit_code, fit_problems, measure, render, state_dir
from placer import Outcome, default_iterations, place
from recipes import Recipe, RecipeError, load_recipe
from validate import RECIPE_DIR, check_recipes

RUNS_DIR = "runs"
# Rounding to KiCad's nanometres on write-back changes the cost by far less than this.
READBACK_TOLERANCE = 1e-3


class MoveRecord(BaseModel):
    ref: str
    old: Pose
    new: Pose


class RunRecord(BaseModel):
    seed: int
    start: str
    start_cost: float
    cost: float
    iterations_planned: int
    iterations_done: int
    accepted: int
    budget_cap_hit: bool
    unplaced: list[str]
    moves: list[MoveRecord]


def run_record(board: BoardData, outcome: Outcome, seed: int) -> RunRecord:
    old = board.poses()
    moves = [MoveRecord(ref=ref, old=old[ref], new=pose) for ref, pose in sorted(outcome.poses.items()) if pose != old[ref]]
    return RunRecord(
        seed=seed,
        start=outcome.start,
        start_cost=outcome.start_cost,
        cost=outcome.cost,
        iterations_planned=outcome.iterations_planned,
        iterations_done=outcome.iterations_done,
        accepted=outcome.accepted,
        budget_cap_hit=outcome.budget_cap_hit,
        unplaced=outcome.unplaced,
        moves=moves,
    )


def new_run_dir(placement_path: Path) -> Path:
    runs = state_dir(placement_path) / RUNS_DIR
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path, n = runs / stamp, 1
    while path.exists():
        n += 1
        path = runs / f"{stamp}-{n}"
    path.mkdir(parents=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", type=Path, help="path of the .kicad_pcb open in KiCad")
    parser.add_argument("placement", type=Path, help="placement.yaml")
    parser.add_argument("--socket", default=None, help="KiCad IPC socket, e.g. ipc:///tmp/kicad/api-1234.sock")
    parser.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    parser.add_argument("--budget", type=float, default=60.0, help="time limit in seconds (default 60)")
    parser.add_argument("--iterations", type=int, default=None, help="annealing iterations (default: a fixed number per movable part)")
    parser.add_argument("--from-scratch", action="store_true", help="ignore the current poses of movable parts")
    parser.add_argument("--dry-run", action="store_true", help="write the run files but do not move anything on the board")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a text summary")
    args = parser.parse_args()
    started = time.perf_counter()

    try:
        placement = load_placement(args.placement)
        handle = open_board(args.board, args.socket)
        board = read_board(handle)
    except (PlacementFileError, BoardIOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if fit_problems(board, placement, args.placement):
        return 2
    recipe_errors = [p for p in check_recipes(board, placement, RECIPE_DIR) if p.level == "error"]
    if recipe_errors:
        print(f"error: {args.placement} has recipe problems (run validate.py):", file=sys.stderr)
        for p in recipe_errors:
            print(f"  {p.line()}", file=sys.stderr)
        return 2
    try:
        recipes: dict[str, Recipe] = {
            g.name: load_recipe(RECIPE_DIR / f"{g.recipe}.md") for g in placement.groups if g.recipe is not None and RECIPE_DIR.is_dir()
        }
    except RecipeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    outcome = place(
        board,
        placement,
        recipes,
        seed=args.seed,
        iterations=args.iterations if args.iterations is not None else default_iterations(board, placement),
        budget_s=args.budget - (time.perf_counter() - started),
        from_scratch=args.from_scratch,
    )
    record = run_record(board, outcome, args.seed)
    try:
        run_dir = new_run_dir(args.placement)
        (run_dir / "moves.json").write_text(record.model_dump_json(indent=1) + "\n")
        if args.dry_run:
            result, data = measure(board, placement, args.placement, history=False, poses=outcome.poses)
        else:
            apply_moves(handle, [Move(ref=m.ref, x=m.new.x, y=m.new.y, rot=m.new.rot) for m in record.moves], message=f"pcb-placement seed {args.seed}")
            result, data = measure(read_board(handle), placement, args.placement)
        (run_dir / "metrics.json").write_text(json.dumps(data, indent=1) + "\n")
    except (OSError, ValidationError, BoardIOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    wall = time.perf_counter() - started
    placer = {
        "run_dir": str(run_dir),
        "seed": args.seed,
        "start": outcome.start,
        "start_cost": round(outcome.start_cost, 3),
        "cost": round(outcome.cost, 3),
        "iterations": f"{outcome.iterations_done}/{outcome.iterations_planned}",
        "accepted": outcome.accepted,
        "budget_cap_hit": outcome.budget_cap_hit,
        "moved": len(record.moves),
        "unplaced": outcome.unplaced,
        "applied": not args.dry_run,
        "wall_s": round(wall, 1),
    }
    if abs(result.cost - outcome.cost) > READBACK_TOLERANCE:
        print(f"warning: the board scores {result.cost:.3f}, the placer computed {outcome.cost:.3f}", file=sys.stderr)
    if args.json:
        print(json.dumps({"placer": placer, "metrics": data}, separators=(",", ":")))
    else:
        cap = "budget cap HIT, result depends on machine speed" if outcome.budget_cap_hit else "budget cap not hit"
        print(
            f"placer: seed {args.seed}, start {outcome.start} (cost {outcome.start_cost:.1f}), "
            f"{outcome.iterations_done}/{outcome.iterations_planned} iterations ({cap}), "
            f"moved {len(record.moves)} parts{'' if not args.dry_run else ' (dry run, board unchanged)'}, {wall:.1f} s"
        )
        if outcome.unplaced:
            print(f"placer: no legal spot for {', '.join(outcome.unplaced)}")
        print(f"run files: {run_dir}")
        print(render(data, False))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
