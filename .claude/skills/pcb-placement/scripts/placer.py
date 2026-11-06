"""Constraint-driven placement: placement.yaml + board -> footprint poses. No KiCad access.

Four phases:
1. Macro placement: each group is one rectangle (its recipe's bbox_mm, or its members'
   courtyard area x 1.6), moved inside its region by force-directed placement that pulls
   groups together by the signal nets they share, and towards immovable parts they connect to.
2. Intra-group placement: the anchor goes to its rectangle's centre, recipe parts go to their
   offsets from the anchor, other members to a free spot near the pins they connect to.
   Ungrouped movable parts follow, each near the placed parts it shares nets with.
3. Legalisation: every part the placer moves is snapped to grid_mm, its rotation rounded to
   0/90/180/270, and pushed to the nearest free spot (spiral search, nearest first) where its
   courtyard bounding box lies inside its group's region, inside the outline with
   edge_clearance_mm, and clear of every other courtyard bounding box on its side.
4. Refinement: simulated annealing on exactly `scoring.score`'s cost. Moves: shift one part,
   rotate one part 90 degrees, swap two parts with the same footprint, mirror a group. The
   moved parts are legalised before the move is scored, so every scored state is legal.

Parts under `fixed:`, parts locked in KiCad, and parts on a side not in board.sides are
obstacles and never move. The placer never flips a part to the other side.

Start: the constructed placement (phases 1-3) or, unless `from_scratch`, the current board
poses with only the illegal parts legalised, whichever scores lower. A re-run on a placed
board therefore refines it instead of starting over. Parts the placer never moves keep their
exact pose, even off grid.

Determinism: one random.Random(seed) drives every choice, and the annealer runs a fixed
number of iterations. Wall-clock time only matters when the time budget runs out first;
the result then depends on machine speed and `budget_cap_hit` says so.
"""

import math
import random
import time
from collections.abc import Mapping, Sequence

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict
from shapely import Polygon, box, contains_xy

from board_model import BoardData, FootprintData, Point, Pose, to_board
from constraints import GROUND_NET_PATTERNS, Group, Placement
from recipes import Recipe, part_pose, reference_point
from scoring import HARD_PENALTY, IC_REF, compile_patterns, net_matches, score

Rect = tuple[float, float, float, float]
FloatArray = NDArray[np.float64]

# Boxes may touch; overlaps smaller than this do not count.
TOUCH_EPS_MM = 1e-6
# Extra inset of the outline, so that polygonised arcs never bring a courtyard under the clearance.
OUTLINE_MARGIN_MM = 0.01
# Most candidate spots one spiral search considers; the lattice gets coarser beyond this.
MAX_CANDIDATES = 200_000
CHUNK = 4096
GROUP_AREA_FACTOR = 1.6
MACRO_STEPS = 150
# Annealing: a moved part is legalised within this distance of where the move put it.
MOVE_RADIUS_MM = 2.0
T_END_RATIO = 1e-3
T0_SAMPLES = 40
MOVE_SHIFT, MOVE_ROTATE, MOVE_SWAP = 0.65, 0.80, 0.92
ITERATIONS_PER_PART = 250


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    poses: dict[str, Pose]
    start: str
    start_cost: float
    cost: float
    iterations_planned: int
    iterations_done: int
    accepted: int
    budget_cap_hit: bool
    # Parts with no legal spot; they keep their board pose and show up as hard violations.
    unplaced: list[str]


def movable_refs(board: BoardData, placement: Placement) -> list[str]:
    """Parts the placer may move: not fixed, not locked, on a side listed in board.sides."""
    return sorted(
        fp.ref for fp in board.footprints if fp.ref not in placement.fixed and not fp.locked and fp.side in placement.board.sides
    )


def default_iterations(board: BoardData, placement: Placement) -> int:
    return ITERATIONS_PER_PART * len(movable_refs(board, placement))


def quarter(rot: float) -> float:
    """Rotation rounded to the nearest of 0, 90, 180, 270."""
    return float(round(rot / 90.0) * 90 % 360)


def _area(r: Rect) -> float:
    return (r[2] - r[0]) * (r[3] - r[1])


class Layout:
    """Working placement: poses plus the courtyard bounding box of every part on the board."""

    def __init__(self, board: BoardData, placement: Placement, recipes: Mapping[str, Recipe]) -> None:
        self.board = board
        self.placement = placement
        self.recipes = recipes
        self.grid = placement.board.grid_mm
        self.footprints: dict[str, FootprintData] = {fp.ref: fp for fp in board.footprints}
        self.refs: list[str] = sorted(self.footprints)
        self.index: dict[str, int] = {ref: i for i, ref in enumerate(self.refs)}
        self.movable: list[str] = movable_refs(board, placement)
        self.movable_set = frozenset(self.movable)
        self.group_of: dict[str, Group] = {ref: g for g in placement.groups for ref in g.members}
        self.bounds: Rect = (0.0, 0.0, board.width_mm, board.height_mm)
        self.allowed = Polygon(board.outline, board.holes).buffer(-(placement.board.edge_clearance_mm + OUTLINE_MARGIN_MM))
        self.rect_outline = self.allowed.equals(box(*self.allowed.bounds))
        self.ground = compile_patterns(GROUND_NET_PATTERNS)
        self.power = compile_patterns(placement.power_nets)

        self._points: dict[str, FloatArray] = {
            ref: np.array([p for ring in fp.courtyard for p in ring], dtype=np.float64).reshape(-1, 2) for ref, fp in self.footprints.items()
        }
        self._local_boxes: dict[tuple[str, float], Rect | None] = {}
        self.poses: dict[str, Pose] = board.poses()
        self.boxes: FloatArray = np.zeros((len(self.refs), 4), dtype=np.float64)
        self.has_box = np.zeros(len(self.refs), dtype=np.bool_)
        self.side = np.array([self.footprints[r].side == "bottom" for r in self.refs], dtype=np.bool_)
        self.active = np.ones(len(self.refs), dtype=np.bool_)
        for ref in self.refs:
            self._store(ref, self.poses[ref])
        self.claimed: set[tuple[str, str]] = set()

    # --- geometry -------------------------------------------------------------------

    def local_box(self, ref: str, rot: float) -> Rect | None:
        """Courtyard bounding box relative to the footprint position, at rotation `rot`."""
        key = (ref, round(rot % 360.0, 6))
        if key not in self._local_boxes:
            pts = self._points[ref]
            if len(pts) == 0:
                self._local_boxes[key] = None
            else:
                rad = math.radians(rot)
                c, s = math.cos(rad), math.sin(rad)
                xs = pts[:, 0] * c + pts[:, 1] * s
                ys = pts[:, 1] * c - pts[:, 0] * s
                self._local_boxes[key] = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
        return self._local_boxes[key]

    def box_at(self, ref: str, pose: Pose) -> Rect | None:
        lb = self.local_box(ref, pose.rot)
        return None if lb is None else (pose.x + lb[0], pose.y + lb[1], pose.x + lb[2], pose.y + lb[3])

    def _store(self, ref: str, pose: Pose) -> None:
        i = self.index[ref]
        self.poses[ref] = pose
        b = self.box_at(ref, pose)
        self.has_box[i] = b is not None
        if b is not None:
            self.boxes[i] = b

    def set_pose(self, ref: str, pose: Pose) -> None:
        self._store(ref, pose)
        self.active[self.index[ref]] = True

    def area(self, ref: str) -> float:
        lb = self.local_box(ref, 0.0)
        return 0.0 if lb is None else _area(lb)

    def limits(self, ref: str) -> Rect:
        """Rectangle the part's courtyard box must stay in: its group's region, else the board."""
        group = self.group_of.get(ref)
        if group is not None and group.region is not None:
            return self.placement.regions[group.region]
        return self.bounds

    def _obstacles(self, ref: str) -> FloatArray:
        i = self.index[ref]
        mask = self.active & self.has_box & (self.side == self.side[i])
        mask[i] = False
        return self.boxes[mask]

    def _inside_outline(self, x0: FloatArray, y0: FloatArray, x1: FloatArray, y1: FloatArray) -> NDArray[np.bool_]:
        if self.rect_outline:
            ax0, ay0, ax1, ay1 = self.allowed.bounds
            return (x0 >= ax0) & (y0 >= ay0) & (x1 <= ax1) & (y1 <= ay1)
        ok = np.ones(len(x0), dtype=np.bool_)
        for xs, ys in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            ok &= np.asarray(contains_xy(self.allowed, xs, ys), dtype=np.bool_)
        return ok

    def is_legal(self, ref: str, pose: Pose) -> bool:
        """Courtyard box inside the part's limits and the outline, clear of active same-side boxes."""
        b = self.box_at(ref, pose)
        if b is None:
            return True
        lx0, ly0, lx1, ly1 = self.limits(ref)
        if b[0] < lx0 - TOUCH_EPS_MM or b[1] < ly0 - TOUCH_EPS_MM or b[2] > lx1 + TOUCH_EPS_MM or b[3] > ly1 + TOUCH_EPS_MM:
            return False
        if not self.allowed.covers(box(*b)):
            return False
        ob = self._obstacles(ref)
        hits = (b[0] < ob[:, 2] - TOUCH_EPS_MM) & (ob[:, 0] < b[2] - TOUCH_EPS_MM) & (b[1] < ob[:, 3] - TOUCH_EPS_MM) & (ob[:, 1] < b[3] - TOUCH_EPS_MM)
        return not bool(hits.any())

    def find_spot(self, ref: str, x: float, y: float, rot: float, radius_mm: float = math.inf) -> Pose | None:
        """Nearest legal grid position to (x, y) within `radius_mm`, rotation rounded to 90 degrees."""
        rot = quarter(rot)
        g = self.grid
        lb = self.local_box(ref, rot)
        if lb is None:
            return Pose(x=round(x / g) * g, y=round(y / g) * g, rot=rot)
        lx0, ly0, lx1, ly1 = self.limits(ref)
        ax0, ay0, ax1, ay1 = self.allowed.bounds
        # Positions where the box fits the limits, the outline's bounds and the search radius.
        px0 = max(lx0, ax0) - lb[0]
        px1 = min(lx1, ax1) - lb[2]
        py0 = max(ly0, ay0) - lb[1]
        py1 = min(ly1, ay1) - lb[3]
        if math.isfinite(radius_mm):
            px0, px1 = max(px0, x - radius_mm), min(px1, x + radius_mm)
            py0, py1 = max(py0, y - radius_mm), min(py1, y + radius_mm)
        i0, i1 = math.ceil(px0 / g - 1e-9), math.floor(px1 / g + 1e-9)
        j0, j1 = math.ceil(py0 / g - 1e-9), math.floor(py1 / g + 1e-9)
        if i1 < i0 or j1 < j0:
            return None
        step = max(1, math.ceil(math.sqrt((i1 - i0 + 1) * (j1 - j0 + 1) / MAX_CANDIDATES)))
        gx = np.arange(i0, i1 + 1, step, dtype=np.float64) * g
        gy = np.arange(j0, j1 + 1, step, dtype=np.float64) * g
        cx, cy = (a.ravel() for a in np.meshgrid(gx, gy))
        dist = np.hypot(cx - x, cy - y)
        keep = dist <= radius_mm + 1e-9
        cx, cy, dist = cx[keep], cy[keep], dist[keep]
        order = np.lexsort((cx, cy, dist))
        cx, cy = cx[order], cy[order]
        ob = self._obstacles(ref)
        for start in range(0, len(cx), CHUNK):
            sx, sy = cx[start : start + CHUNK], cy[start : start + CHUNK]
            x0, y0, x1, y1 = sx + lb[0], sy + lb[1], sx + lb[2], sy + lb[3]
            ok = self._inside_outline(x0, y0, x1, y1)
            if len(ob):
                hits = (
                    (x0[:, None] < ob[None, :, 2] - TOUCH_EPS_MM)
                    & (ob[None, :, 0] < x1[:, None] - TOUCH_EPS_MM)
                    & (y0[:, None] < ob[None, :, 3] - TOUCH_EPS_MM)
                    & (ob[None, :, 1] < y1[:, None] - TOUCH_EPS_MM)
                )
                ok &= ~hits.any(axis=1)
            for k in np.flatnonzero(ok):
                if self.rect_outline or self.allowed.covers(box(float(x0[k]), float(y0[k]), float(x1[k]), float(y1[k]))):
                    return Pose(x=float(sx[k]), y=float(sy[k]), rot=rot)
        return None

    def move_all(self, targets: Sequence[tuple[str, Pose]], radius_mm: float) -> bool:
        """Legalise each part near its target, in order; on failure restore every part and return False."""
        old = [(ref, self.poses[ref]) for ref, _ in targets]
        for ref, _ in targets:
            self.active[self.index[ref]] = False
        for ref, target in targets:
            spot = self.find_spot(ref, target.x, target.y, target.rot, radius_mm)
            if spot is None:
                for r, pose in old:
                    self.set_pose(r, pose)
                return False
            self.set_pose(ref, spot)
        return True

    # --- connectivity -----------------------------------------------------------------

    def _is_signal(self, net: str) -> bool:
        return bool(net) and not net_matches(net, self.power)

    def _is_supply(self, net: str) -> bool:
        return bool(net) and net_matches(net, self.power) and not net_matches(net, self.ground)

    def _placed_pins(self, net: str, exclude: str) -> list[tuple[str, str, Point]]:
        """(ref, pad number, board point) of active parts' pads on `net`."""
        found: list[tuple[str, str, Point]] = []
        for ref in self.refs:
            if ref == exclude or not self.active[self.index[ref]]:
                continue
            fp = self.footprints[ref]
            for pad in fp.pads:
                if pad.net == net:
                    found.append((ref, pad.number, to_board(self.poses[ref], (pad.x, pad.y))))
        return found

    def target(self, ref: str, near: Point) -> Point:
        """Where `ref` wants to be: by the placed pins of its signal nets, else by a free IC
        supply pin (nearest `near`, claimed so the next cap picks another), else `near`."""
        fp = self.footprints[ref]
        nets = sorted({p.net for p in fp.pads if p.net})
        centroids: list[Point] = []
        for net in (n for n in nets if self._is_signal(n)):
            pins = self._placed_pins(net, ref)
            if pins:
                centroids.append((sum(p[2][0] for p in pins) / len(pins), sum(p[2][1] for p in pins) / len(pins)))
        if centroids:
            return (sum(c[0] for c in centroids) / len(centroids), sum(c[1] for c in centroids) / len(centroids))
        supply = [pin for net in nets if self._is_supply(net) for pin in self._placed_pins(net, ref) if IC_REF.fullmatch(pin[0])]
        free = [pin for pin in supply if (pin[0], pin[1]) not in self.claimed]
        if free:
            best = min(free, key=lambda pin: (math.dist(pin[2], near), pin[0], pin[1]))
            self.claimed.add((best[0], best[1]))
            return best[2]
        if supply:
            return min(supply, key=lambda pin: (math.dist(pin[2], near), pin[0], pin[1]))[2]
        return near

    # --- phases 1-3 ---------------------------------------------------------------------

    def _group_size(self, group: Group, movers: Sequence[str]) -> tuple[float, float]:
        recipe = self.recipes.get(group.name)
        anchor_rot = quarter(self.poses[group.anchor].rot) if group.anchor else 0.0
        if recipe is not None and recipe.bbox_mm is not None:
            w, h = recipe.bbox_mm
            return (h, w) if anchor_rot in (90.0, 270.0) else (w, h)
        boxes = [lb for ref in movers if (lb := self.local_box(ref, quarter(self.poses[ref].rot))) is not None]
        if not boxes:
            return (self.grid, self.grid)
        area = sum(_area(b) for b in boxes) * GROUP_AREA_FACTOR
        w = max(math.sqrt(area), max(b[2] - b[0] for b in boxes))
        h = max(area / w, max(b[3] - b[1] for b in boxes))
        return (w, h)

    def macro(self, rng: random.Random) -> dict[str, Point]:
        """Phase 1: centre of each group with movable members."""
        groups = [g for g in self.placement.groups if any(r in self.movable_set for r in g.members)]
        names = [g.name for g in groups]
        size: dict[str, tuple[float, float]] = {}
        area_of: dict[str, Rect] = {}
        pos: dict[str, Point] = {}
        pinned: set[str] = set()
        for g in groups:
            movers = [r for r in g.members if r in self.movable_set]
            lim = self.placement.regions[g.region] if g.region else self.bounds
            w, h = self._group_size(g, movers)
            w, h = min(w, lim[2] - lim[0]), min(h, lim[3] - lim[1])
            size[g.name], area_of[g.name] = (w, h), lim
            if g.anchor is not None and g.anchor not in self.movable_set:
                pos[g.name] = (self.poses[g.anchor].x, self.poses[g.anchor].y)
                pinned.add(g.name)
            else:
                cx, cy = (lim[0] + lim[2]) / 2, (lim[1] + lim[3]) / 2
                pos[g.name] = (cx + rng.uniform(-0.25, 0.25) * (lim[2] - lim[0] - w), cy + rng.uniform(-0.25, 0.25) * (lim[3] - lim[1] - h))

        nets_of = {g.name: {p.net for r in g.members for p in self.footprints[r].pads if self._is_signal(p.net)} for g in groups}
        links: list[tuple[str, str, float]] = [
            (a, b, float(len(nets_of[a] & nets_of[b]))) for i, a in enumerate(names) for b in names[i + 1 :] if nets_of[a] & nets_of[b]
        ]
        fixed_pull: dict[str, list[Point]] = {}
        immovable = [r for r in self.refs if r not in self.movable_set]
        for g in groups:
            pts: list[Point] = []
            for net in sorted(nets_of[g.name]):
                pins = [to_board(self.poses[r], (p.x, p.y)) for r in immovable for p in self.footprints[r].pads if p.net == net]
                if pins:
                    pts.append((sum(p[0] for p in pins) / len(pins), sum(p[1] for p in pins) / len(pins)))
            fixed_pull[g.name] = pts

        for step in range(MACRO_STEPS):
            rate = 0.5 * (1.0 - step / MACRO_STEPS)
            force: dict[str, tuple[float, float, float]] = {n: (0.0, 0.0, 0.0) for n in names}
            for a, b, w in links:
                dx, dy = pos[b][0] - pos[a][0], pos[b][1] - pos[a][1]
                fa, fb = force[a], force[b]
                force[a] = (fa[0] + w * dx, fa[1] + w * dy, fa[2] + w)
                force[b] = (fb[0] - w * dx, fb[1] - w * dy, fb[2] + w)
            for n in names:
                fx, fy, wsum = force[n]
                for px, py in fixed_pull[n]:
                    fx, fy, wsum = fx + px - pos[n][0], fy + py - pos[n][1], wsum + 1.0
                if n not in pinned and wsum > 0:
                    pos[n] = (pos[n][0] + rate * fx / wsum, pos[n][1] + rate * fy / wsum)
            for i, a in enumerate(names):
                for b in names[i + 1 :]:
                    ox = (size[a][0] + size[b][0]) / 2 - abs(pos[a][0] - pos[b][0])
                    oy = (size[a][1] + size[b][1]) / 2 - abs(pos[a][1] - pos[b][1])
                    if ox <= 0 or oy <= 0 or (a in pinned and b in pinned):
                        continue
                    share_a = 0.0 if a in pinned else (1.0 if b in pinned else 0.5)
                    share_b = 1.0 - share_a
                    if ox < oy:
                        sign = 1.0 if pos[a][0] >= pos[b][0] else -1.0
                        pos[a] = (pos[a][0] + sign * ox * share_a, pos[a][1])
                        pos[b] = (pos[b][0] - sign * ox * share_b, pos[b][1])
                    else:
                        sign = 1.0 if pos[a][1] >= pos[b][1] else -1.0
                        pos[a] = (pos[a][0], pos[a][1] + sign * oy * share_a)
                        pos[b] = (pos[b][0], pos[b][1] - sign * oy * share_b)
            for n in names:
                if n in pinned:
                    continue
                lim, (w, h) = area_of[n], size[n]
                pos[n] = (min(max(pos[n][0], lim[0] + w / 2), lim[2] - w / 2), min(max(pos[n][1], lim[1] + h / 2), lim[3] - h / 2))
        return pos

    def _place_near(self, ref: str, x: float, y: float, rot: float) -> None:
        """Nearest legal spot to (x, y); with none, the part stays at its board pose (illegal)."""
        spot = self.find_spot(ref, x, y, rot)
        self.set_pose(ref, self.board_pose(ref) if spot is None else spot)

    def board_pose(self, ref: str) -> Pose:
        return self.footprints[ref].pose

    def _recipe_parts(self, group: Group) -> set[str]:
        """Place the group's recipe parts at their offsets; return the refs placed."""
        recipe = self.recipes.get(group.name)
        if recipe is None or group.anchor is None:
            return set()
        refs = {**group.roles, recipe.anchor: group.anchor}
        anchor_rot = self.poses[group.anchor].rot
        done: set[str] = set()
        for role in recipe.order():
            ref = refs.get(role)
            part = recipe.parts[role]
            rel_ref = refs.get(part.rel_role)
            if ref is None or ref not in self.movable_set or rel_ref is None or not self.active[self.index[rel_ref]]:
                continue
            point = reference_point(self.footprints[rel_ref], self.poses[rel_ref], part.rel_pin)
            if point is None:
                continue
            pose = part_pose(part, anchor_rot, point)
            self._place_near(ref, pose.x, pose.y, pose.rot)
            done.add(ref)
        return done

    def construct(self, rng: random.Random) -> None:
        """Phases 1-3 from scratch."""
        for ref in self.movable:
            self.active[self.index[ref]] = False
        self.claimed = set()
        centres = self.macro(rng)
        groups = sorted(
            (g for g in self.placement.groups if g.name in centres),
            key=lambda g: (-sum(self.area(r) for r in g.members), g.name),
        )
        for g in groups:
            movers = [r for r in g.members if r in self.movable_set]
            anchor = g.anchor if g.anchor is not None else max(movers, key=lambda r: (self.area(r), r))
            centre = centres[g.name]
            if anchor in self.movable_set:
                self._place_near(anchor, centre[0], centre[1], quarter(self.board_pose(anchor).rot))
            done = {anchor} | self._recipe_parts(g)
            rest = sorted((r for r in movers if r not in done), key=lambda r: (-self.area(r), r))
            near = (self.poses[anchor].x, self.poses[anchor].y)
            for ref in rest:
                tx, ty = self.target(ref, near)
                self._place_near(ref, tx, ty, quarter(self.board_pose(ref).rot))
        centre = (self.board.width_mm / 2, self.board.height_mm / 2)
        for ref in sorted((r for r in self.movable if r not in self.group_of), key=lambda r: (-self.area(r), r)):
            tx, ty = self.target(ref, centre)
            self._place_near(ref, tx, ty, quarter(self.board_pose(ref).rot))

    def legalise_board(self) -> None:
        """The current board poses with only the illegal parts moved."""
        for ref in self.movable:
            self.active[self.index[ref]] = False
            self._store(ref, self.board_pose(ref))
        for ref in sorted(self.movable, key=lambda r: (-self.area(r), r)):
            pose = self.board_pose(ref)
            if self.is_legal(ref, pose):
                self.set_pose(ref, pose)
            else:
                self._place_near(ref, pose.x, pose.y, pose.rot)

    def load(self, poses: Mapping[str, Pose]) -> None:
        for ref in self.movable:
            self.set_pose(ref, poses[ref])

    def cost(self) -> float:
        return score(self.board, self.placement, self.poses).cost

    # --- phase 4 ------------------------------------------------------------------------

    def _swap_classes(self) -> list[list[str]]:
        """Movable parts with identical pads and courtyard on the same side, two or more per class."""
        classes: dict[str, list[str]] = {}
        for ref in self.movable:
            fp = self.footprints[ref]
            pads = sorted((p.number, round(p.x, 3), round(p.y, 3)) for p in fp.pads)
            ring = [(round(x, 3), round(y, 3)) for r in fp.courtyard for x, y in r]
            classes.setdefault(repr((fp.side, pads, ring)), []).append(ref)
        return [refs for _, refs in sorted(classes.items()) if len(refs) >= 2]

    def propose(self, rng: random.Random, progress: float, swaps: Sequence[Sequence[str]], mirrors: Sequence[Group]) -> list[tuple[str, Pose]]:
        kind = rng.random()
        if kind >= MOVE_SWAP and mirrors:
            group = mirrors[rng.randrange(len(mirrors))]
            movers = [r for r in group.members if r in self.movable_set]
            boxes = [b for r in movers if (b := self.box_at(r, self.poses[r])) is not None]
            if not boxes:
                return []
            cx = (min(b[0] for b in boxes) + max(b[2] for b in boxes)) / 2
            cy = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2
            if rng.random() < 0.5:
                return [(r, Pose(x=2 * cx - self.poses[r].x, y=self.poses[r].y, rot=quarter(180.0 - self.poses[r].rot))) for r in movers]
            return [(r, Pose(x=self.poses[r].x, y=2 * cy - self.poses[r].y, rot=quarter(-self.poses[r].rot))) for r in movers]
        if kind >= MOVE_ROTATE and swaps:
            refs = swaps[rng.randrange(len(swaps))]
            a, b = rng.sample(list(refs), 2)
            return [(a, self.poses[b]), (b, self.poses[a])]
        ref = self.movable[rng.randrange(len(self.movable))]
        pose = self.poses[ref]
        if kind >= MOVE_SHIFT:
            return [(ref, Pose(x=pose.x, y=pose.y, rot=quarter(pose.rot) + rng.choice((90.0, 270.0))))]
        lim = self.limits(ref)
        span = max(lim[2] - lim[0], lim[3] - lim[1])
        reach = max(4 * self.grid, 0.5 * span * (1.0 - progress))
        return [(ref, Pose(x=pose.x + rng.uniform(-reach, reach), y=pose.y + rng.uniform(-reach, reach), rot=pose.rot))]

    def anneal(self, rng: random.Random, iterations: int, deadline: float) -> tuple[int, int, bool]:
        """Phase 4. Leaves the best state found loaded. Returns (iterations run, accepted, deadline hit)."""
        if not self.movable or iterations <= 0:
            return (0, 0, False)
        swaps = self._swap_classes()
        mirrors = [g for g in self.placement.groups if sum(1 for r in g.members if r in self.movable_set) >= 2]
        current = self.cost()
        best, best_poses = current, dict(self.poses)

        rises: list[float] = []
        for _ in range(T0_SAMPLES):
            move = self.propose(rng, 0.0, swaps, mirrors)
            old = [(ref, self.poses[ref]) for ref, _ in move]
            if move and self.move_all(move, MOVE_RADIUS_MM):
                delta = self.cost() - current
                if 0 < delta < HARD_PENALTY / 10:
                    rises.append(delta)
                for ref, pose in old:
                    self.set_pose(ref, pose)
        t0 = sum(rises) / len(rises) if rises else 1.0

        done = accepted = 0
        hit = False
        for it in range(iterations):
            if time.perf_counter() > deadline:
                hit = True
                break
            done = it + 1
            progress = it / iterations
            temp = t0 * T_END_RATIO**progress
            move = self.propose(rng, progress, swaps, mirrors)
            old = [(ref, self.poses[ref]) for ref, _ in move]
            if not move or not self.move_all(move, MOVE_RADIUS_MM):
                continue
            new = self.cost()
            delta = new - current
            if delta <= 0 or rng.random() < math.exp(-delta / temp):
                current = new
                accepted += 1
                if new < best:
                    best, best_poses = new, dict(self.poses)
            else:
                for ref, pose in old:
                    self.set_pose(ref, pose)
        self.load(best_poses)
        return (done, accepted, hit)


def place(
    board: BoardData,
    placement: Placement,
    recipes: Mapping[str, Recipe],
    *,
    seed: int,
    iterations: int,
    budget_s: float,
    from_scratch: bool = False,
) -> Outcome:
    """Run the four phases. `recipes` maps group name to its recipe.

    The budget covers every phase; only the annealer stops early when it runs out.
    """
    deadline = time.perf_counter() + budget_s
    rng = random.Random(seed)
    layout = Layout(board, placement, recipes)
    layout.construct(rng)
    start, start_cost = "constructed", layout.cost()
    if not from_scratch:
        built = dict(layout.poses)
        layout.legalise_board()
        board_cost = layout.cost()
        if board_cost <= start_cost:
            start, start_cost = "board", board_cost
        else:
            layout.load(built)
    done, accepted, hit = layout.anneal(rng, iterations, deadline)
    return Outcome(
        poses={ref: layout.poses[ref] for ref in layout.movable},
        start=start,
        start_cost=start_cost,
        cost=layout.cost(),
        iterations_planned=iterations,
        iterations_done=done,
        accepted=accepted,
        budget_cap_hit=hit,
        unplaced=[ref for ref in layout.movable if not layout.is_legal(ref, layout.poses[ref])],
    )
