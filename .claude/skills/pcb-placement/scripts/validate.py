# /// script
# requires-python = ">=3.12"
# dependencies = ["kicad-python>=0.8.0", "shapely>=2.0", "pydantic>=2.6", "pyyaml>=6"]
# ///
"""Check placement.yaml against its schema and against the board open in KiCad.

    validate.py BOARD PLACEMENT_YAML [--socket URL] [--json]

Prints every problem, one line each, naming the field path (e.g. groups[0].members[2]).
Schema errors are reported first; the checks against the board run once the schema is valid.

Exit codes: 0 valid (warnings allowed), 1 invalid, 2 the file or the board cannot be read.
"""

import argparse
import json
import sys
from pathlib import Path

from shapely import box
from shapely.geometry import Point as ShapelyPoint
from shapely.geometry import Polygon

from board_io import BoardIOError, open_board, read_board
from board_model import BoardData, Pose
from constraints import DecouplingPair, Placement, PlacementFileError, Problem, load_placement
from scoring import courtyard_overlaps, find_pins

RECIPE_DIR = Path(__file__).resolve().parent.parent / "reference" / "block-recipes"
# Regions may touch the outline; this absorbs rounding in the outline coordinates.
OUTLINE_TOLERANCE_MM = 1e-6


def _fixed_overlaps(board: BoardData, placement: Placement) -> list[Problem]:
    """Overlaps the placer can never clear: a `fixed:` part against another fixed or locked part."""
    footprints = {fp.ref: fp for fp in board.footprints}
    poses = {
        ref: Pose(x=f.x, y=f.y, rot=footprints[ref].pose.rot if f.rot is None else f.rot)
        for ref, f in placement.fixed.items()
        if ref in footprints
    }
    immovable = set(poses) | {fp.ref for fp in board.footprints if fp.locked}
    problems: list[Problem] = []
    for o in courtyard_overlaps(board, poses):
        if o.a in immovable and o.b in immovable and (o.a in poses or o.b in poses):
            ref, other = (o.a, o.b) if o.a in poses else (o.b, o.a)
            how = "fixed" if other in poses else "locked in KiCad"
            problems.append(Problem(path=f"fixed.{ref}", message=f"courtyard overlaps {other} ({how}) by {o.depth_mm:.2f} mm"))
    return problems


def check(board: BoardData, placement: Placement) -> list[Problem]:
    """Every reference and geometry problem in `placement` for `board`. Empty means valid."""
    footprints = {fp.ref: fp for fp in board.footprints}
    nets = {pad.net for fp in board.footprints for pad in fp.pads if pad.net}
    group_names = {g.name for g in placement.groups}
    problems: list[Problem] = []

    def require(path: str, ref: str) -> bool:
        if ref in footprints:
            return True
        problems.append(Problem(path=path, message=f"{ref} is not on the board"))
        return False

    for ref in placement.fixed:
        require(f"fixed.{ref}", ref)
    problems += _fixed_overlaps(board, placement)

    outline = Polygon(board.outline)
    for name, rect in placement.regions.items():
        if not outline.buffer(OUTLINE_TOLERANCE_MM).covers(box(*rect)):
            x0, y0, x1, y1 = rect
            out = max(outline.distance(ShapelyPoint(x, y)) for x in (x0, x1) for y in (y0, y1))
            problems.append(
                Problem(
                    path=f"regions.{name}",
                    message=f"reaches up to {out:.2f} mm outside the board outline (board is {board.width_mm:.2f} x {board.height_mm:.2f} mm)",
                )
            )

    owner: dict[str, str] = {}
    seen_names: set[str] = set()
    for i, group in enumerate(placement.groups):
        at = f"groups[{i}]"
        if group.name in seen_names:
            problems.append(Problem(path=f"{at}.name", message=f"group name {group.name} is used more than once"))
        seen_names.add(group.name)
        for j, ref in enumerate(group.members):
            if not require(f"{at}.members[{j}]", ref):
                continue
            if ref in owner:
                where = "this group" if owner[ref] == group.name else f"group {owner[ref]}"
                problems.append(Problem(path=f"{at}.members[{j}]", message=f"{ref} is already in {where}"))
            else:
                owner[ref] = group.name
        if group.region is not None and group.region not in placement.regions:
            problems.append(Problem(path=f"{at}.region", message=f"region {group.region} is not defined under regions"))
        if group.anchor is not None and group.anchor not in group.members:
            problems.append(Problem(path=f"{at}.anchor", message=f"{group.anchor} is not a member of group {group.name}"))
        for role, ref in group.roles.items():
            if ref not in group.members:
                problems.append(Problem(path=f"{at}.roles.{role}", message=f"{ref} is not a member of group {group.name}"))

    for i, rule in enumerate(placement.decoupling):
        if not isinstance(rule, DecouplingPair):
            continue
        at = f"decoupling[{i}]"
        cap_found = require(f"{at}.cap", rule.cap)
        ic_found = require(f"{at}.ic", rule.ic)
        if not (cap_found and ic_found):
            continue
        pins = find_pins(footprints[rule.ic], rule.pin)
        if not pins:
            problems.append(Problem(path=f"{at}.pin", message=f"{rule.ic} has no pad number or pin name {rule.pin}"))
        elif not {p.net for p in pins if p.net} & {p.net for p in footprints[rule.cap].pads}:
            problems.append(Problem(path=f"{at}.cap", message=f"{rule.cap} shares no net with {rule.ic} pin {rule.pin}"))

    for i, rule in enumerate(placement.proximity):
        require(f"proximity[{i}].a", rule.a)
        require(f"proximity[{i}].b", rule.b)

    for i, rule in enumerate(placement.separation):
        for side, name in (("a", rule.a), ("b", rule.b)):
            is_group, is_region = name in group_names, name in placement.regions
            if is_group and is_region:
                problems.append(Problem(path=f"separation[{i}].{side}", message=f"{name} is both a group and a region; rename one"))
            elif not (is_group or is_region):
                problems.append(Problem(path=f"separation[{i}].{side}", message=f"{name} is neither a group nor a region"))

    for i, critical in enumerate(placement.critical_nets):
        if critical.net not in nets:
            problems.append(Problem(path=f"critical_nets[{i}].net", message=f"net {critical.net} is not on the board"))
    return problems


def check_recipes(placement: Placement, recipe_dir: Path) -> list[Problem]:
    """Every group `recipe:` needs a file <recipe>.md in `recipe_dir` (only warned if the dir is missing)."""
    problems: list[Problem] = []
    for i, group in enumerate(placement.groups):
        if group.recipe is None:
            continue
        path = f"groups[{i}].recipe"
        if not recipe_dir.is_dir():
            problems.append(Problem(path=path, message=f"recipe {group.recipe} not checked: {recipe_dir} does not exist", level="warning"))
        elif not (recipe_dir / f"{group.recipe}.md").is_file():
            problems.append(Problem(path=path, message=f"no recipe file {group.recipe}.md in {recipe_dir}"))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", type=Path, help="path of the .kicad_pcb open in KiCad")
    parser.add_argument("placement", type=Path, help="placement.yaml")
    parser.add_argument("--socket", default=None, help="KiCad IPC socket, e.g. ipc:///tmp/kicad/api-1234.sock")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args()

    try:
        placement = load_placement(args.placement)
        problems = check(read_board(open_board(args.board, args.socket)), placement) + check_recipes(placement, RECIPE_DIR)
    except PlacementFileError as exc:
        if not exc.problems:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        problems = exc.problems
    except BoardIOError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    errors = sum(1 for p in problems if p.level == "error")
    warnings = len(problems) - errors
    if args.json:
        print(json.dumps({"valid": errors == 0, "errors": errors, "warnings": warnings, "problems": [p.model_dump() for p in problems]}))
    else:
        for p in problems:
            print(p.line())
        print(f"{args.placement}: {'valid' if errors == 0 else 'invalid'}, {errors} errors, {warnings} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
