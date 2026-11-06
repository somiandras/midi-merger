# /// script
# requires-python = ">=3.12"
# dependencies = ["kicad-python>=0.8.0", "shapely>=2.0", "pydantic>=2.6", "pyyaml>=6"]
# ///
"""Score the placement of a board open in KiCad against placement.yaml.

    metrics.py BOARD PLACEMENT_YAML [--socket URL] [--baseline] [--full] [--json]

Prints a short text summary, or with --json one compact JSON object. Lists hold the 10 worst
items per category unless --full; `counts` gives every category's full size. --baseline
stores this run's cost as the reference that later runs report as baseline_cost.

Every run appends one line to .placement/history.jsonl next to PLACEMENT_YAML.

Exit codes: 0 no hard violations, 1 hard violations, 2 input error.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from board_io import BoardIOError, open_board, read_board
from board_model import BoardData
from constraints import Placement, PlacementFileError, load_placement
from scoring import ConstraintError, Score, score

WORST_ITEMS = 10
STATE_DIR = ".placement"
BASELINE_FILE = "baseline.json"
HISTORY_FILE = "history.jsonl"

Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None


class Baseline(BaseModel):
    ts: str
    cost: float
    hard_violations: int


def _rounded(value: Json) -> Json:
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


def _dump(model: BaseModel) -> Json:
    data: Json = json.loads(model.model_dump_json())
    return _rounded(data)


def _worst_first(s: Score) -> Score:
    """Same score with every list sorted worst first."""
    hard = s.hard.model_copy(
        update={
            "overlaps": sorted(s.hard.overlaps, key=lambda v: -v.depth_mm),
            "edge": sorted(s.hard.edge, key=lambda v: v.clearance_mm - v.min_mm),
            "region": sorted(s.hard.region, key=lambda v: -v.outside_mm),
            "fixed_moved": sorted(s.hard.fixed_moved, key=lambda v: -(v.moved_mm + abs(v.rot_delta))),
        }
    )
    soft = s.soft.model_copy(
        update={
            "decoupling": sorted(s.soft.decoupling, key=lambda v: v.max_mm - v.dist_mm),
            "proximity": sorted(s.soft.proximity, key=lambda v: v.max_mm - v.dist_mm),
            "separation": sorted(s.soft.separation, key=lambda v: v.dist_mm - v.min_mm),
            "critical_nets": sorted(s.soft.critical_nets, key=lambda v: -v.hpwl_mm * v.weight),
        }
    )
    return s.model_copy(update={"hard": hard, "soft": soft})


def _messages(s: Score, placement: Placement) -> list[str]:
    """One line per violation, hard ones first, soft ones by their cost contribution."""
    hard: list[str] = [f"{v.a} overlaps {v.b} by {v.depth_mm:.2f} mm" for v in s.hard.overlaps]
    hard += [
        f"{v.ref} courtyard reaches {-v.clearance_mm:.2f} mm outside the board outline"
        if v.clearance_mm < 0
        else f"{v.ref} courtyard is {v.clearance_mm:.2f} mm from the board edge (min {v.min_mm:.2f})"
        for v in s.hard.edge
    ]
    hard += [f"{v.ref} is {v.outside_mm:.2f} mm outside region {v.region}" for v in s.hard.region]
    hard += [f"fixed part {v.ref} moved {v.moved_mm:.2f} mm, rotated {v.rot_delta:.1f} deg" for v in s.hard.fixed_moved]

    w = placement.weights
    soft: list[tuple[bool, float, str]] = [
        (v.hard, w.decoupling * (v.dist_mm - v.max_mm) ** 2, f"{v.cap} is {v.dist_mm:.2f} mm from {v.ic} pin {v.pin} (max {v.max_mm:.2f})")
        for v in s.soft.decoupling
    ]
    soft += [
        (v.hard, w.proximity * (v.dist_mm - v.max_mm) ** 2, f"{v.a} is {v.dist_mm:.2f} mm from {v.b} (max {v.max_mm:.2f})")
        for v in s.soft.proximity
    ]
    soft += [
        (v.hard, w.separation * (v.min_mm - v.dist_mm) ** 2, f"{v.a} and {v.b} are {v.dist_mm:.2f} mm apart (min {v.min_mm:.2f})")
        for v in s.soft.separation
    ]
    soft.sort(key=lambda item: (not item[0], -item[1]))
    return (hard + [("hard: " if is_hard else "") + text for is_hard, _, text in soft])[:WORST_ITEMS]


def _truncated(items: Json, full: bool) -> Json:
    if full or not isinstance(items, list):
        return items
    return items[:WORST_ITEMS]


def report(board: BoardData, placement: Placement, s: Score, baseline_cost: float | None, full: bool) -> dict[str, Json]:
    s = _worst_first(s)
    worst: list[Json] = list(_messages(s, placement))
    movable = sum(1 for fp in board.footprints if not fp.locked and fp.ref not in placement.fixed)
    hard: dict[str, Json] = {
        "overlaps": _truncated(_dump_list(s.hard.overlaps), full),
        "edge": _truncated(_dump_list(s.hard.edge), full),
        "region": _truncated(_dump_list(s.hard.region), full),
        "fixed_moved": _truncated(_dump_list(s.hard.fixed_moved), full),
    }
    soft: dict[str, Json] = {
        "hpwl_mm": round(s.soft.hpwl_mm, 3),
        "crossings": s.soft.crossings,
        "decoupling": _truncated(_dump_list(s.soft.decoupling), full),
        "proximity": _truncated(_dump_list(s.soft.proximity), full),
        "separation": _truncated(_dump_list(s.soft.separation), full),
        "critical_nets": _truncated(_dump_list(s.soft.critical_nets), full),
    }
    counts: dict[str, Json] = {
        "overlaps": len(s.hard.overlaps),
        "edge": len(s.hard.edge),
        "region": len(s.hard.region),
        "fixed_moved": len(s.hard.fixed_moved),
        "decoupling": len(s.soft.decoupling),
        "proximity": len(s.soft.proximity),
        "separation": len(s.soft.separation),
    }
    return {
        "board": {
            "w_mm": round(board.width_mm, 3),
            "h_mm": round(board.height_mm, 3),
            "footprints": len(board.footprints),
            "movable": movable,
        },
        "hard": hard,
        "soft": soft,
        "counts": counts,
        "hard_violations": s.terms.hard_violations,
        "cost": round(s.cost, 3),
        "baseline_cost": None if baseline_cost is None else round(baseline_cost, 3),
        "worst": worst,
    }


def exit_code(s: Score) -> int:
    """0 without hard violations, else 1. Input errors (2) are decided before scoring."""
    return 1 if s.terms.hard_violations else 0


def _dump_list(items: Sequence[BaseModel]) -> Json:
    return [_dump(item) for item in items]


def _text(data: dict[str, Json]) -> str:
    soft = data["soft"]
    assert isinstance(soft, dict)
    lines = [
        f"cost {data['cost']} (baseline {data['baseline_cost']}), hard violations {data['hard_violations']}",
        f"hpwl {soft['hpwl_mm']} mm, crossings {soft['crossings']}",
    ]
    worst = data["worst"]
    if isinstance(worst, list) and worst:
        lines.append("worst:")
        lines += [f"  - {line}" for line in worst]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", type=Path, help="path of the .kicad_pcb open in KiCad")
    parser.add_argument("placement", type=Path, help="placement.yaml")
    parser.add_argument("--socket", default=None, help="KiCad IPC socket, e.g. ipc:///tmp/kicad/api-1234.sock")
    parser.add_argument("--baseline", action="store_true", help="store this run's cost as the reference")
    parser.add_argument("--full", action="store_true", help="list every item, not only the 10 worst")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a text summary")
    args = parser.parse_args()

    try:
        placement = load_placement(args.placement)
        board = read_board(open_board(args.board, args.socket))
        result = score(board, placement)
    except (PlacementFileError, BoardIOError, ConstraintError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    state = args.placement.resolve().parent / STATE_DIR
    baseline_path = state / BASELINE_FILE
    now = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        state.mkdir(exist_ok=True)
        if args.baseline:
            baseline_path.write_text(Baseline(ts=now, cost=result.cost, hard_violations=result.terms.hard_violations).model_dump_json() + "\n")
        baseline_cost = Baseline.model_validate_json(baseline_path.read_text()).cost if baseline_path.exists() else None
        history = {
            "ts": now,
            "board": board.path,
            "cost": round(result.cost, 3),
            "hard_violations": result.terms.hard_violations,
            "hpwl_mm": round(result.soft.hpwl_mm, 3),
            "crossings": result.soft.crossings,
            "baseline": args.baseline,
        }
        with (state / HISTORY_FILE).open("a") as fh:
            fh.write(json.dumps(history) + "\n")
    except (OSError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    data = report(board, placement, result, baseline_cost, args.full)
    print(json.dumps(data, separators=(",", ":")) if args.json else _text(data))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
