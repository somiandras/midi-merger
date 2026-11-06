"""Score a placement: board data + footprint poses + placement.yaml -> metrics and cost.

Pure geometry with no KiCad access, so the placer can score candidate poses in memory.
All lengths are millimetres in the board frame (see board_model).

`score` expects a placement that `validate.check` accepts for the same board: every part,
pin, net, region and group it names exists. It does not re-check this on every call.

Definitions:
- HPWL: per net, half the perimeter of the bounding box of its pad centres. Power and
  ground nets (placement.power_nets) are left out unless listed under critical_nets.
- Crossings: a minimum spanning tree over each signal net's pad centres; every pair of
  tree segments from different nets that intersect counts once.
- Overlaps: courtyards of footprints on the same side whose intersection has positive
  area. Depth is the smaller side of the intersection's bounding box.
- Edge: a courtyard must lie inside the Edge.Cuts outline (holes included) at least
  edge_clearance_mm from it. Parts under `fixed:` or locked in KiCad are exempt, because a
  human put them there (connectors often overhang the edge on purpose).
- Region: every member of a group with a region has its courtyard inside that rectangle.
- Fixed moved: a part under `fixed:` is more than 0.01 mm or 0.1 degrees off its entry.
- Decoupling: centre of the cap pad on the shared net to the centre of the IC pin.
- Proximity: gap between the two parts' courtyards.
- Separation: gap between the courtyards of the members of two regions or groups.
- Cost: see `cost`.
"""

import math
import re
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict
from shapely import STRtree, affinity, box
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry import Point as ShapelyPoint
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from board_model import BoardData, FootprintData, PadData, Point, Pose, to_board
from constraints import GROUND_NET_PATTERNS, DecouplingAuto, DecouplingPair, Placement, Weights

HARD_PENALTY = 1e6
OVERLAP_MIN_AREA_MM2 = 1e-4
FIXED_POSITION_TOLERANCE_MM = 0.01
FIXED_ROTATION_TOLERANCE_DEG = 0.1
CAPACITOR_REF = re.compile(r"C\d+")
IC_REF = re.compile(r"(U|IC)\d+")


class ConstraintError(Exception):
    """Poses were given for parts the board does not have."""


class Result(BaseModel):
    model_config = ConfigDict(frozen=True)


class Overlap(Result):
    a: str
    b: str
    depth_mm: float


class EdgeViolation(Result):
    ref: str
    # Negative when the courtyard reaches outside the outline.
    clearance_mm: float
    min_mm: float


class RegionViolation(Result):
    ref: str
    region: str
    outside_mm: float


class FixedMoved(Result):
    ref: str
    moved_mm: float
    rot_delta: float


class DecouplingViolation(Result):
    cap: str
    ic: str
    pin: str
    dist_mm: float
    max_mm: float
    hard: bool


class ProximityViolation(Result):
    a: str
    b: str
    dist_mm: float
    max_mm: float
    hard: bool


class SeparationViolation(Result):
    a: str
    b: str
    dist_mm: float
    min_mm: float
    hard: bool


class CriticalNetLength(Result):
    net: str
    hpwl_mm: float
    weight: float


class Hard(Result):
    overlaps: list[Overlap]
    edge: list[EdgeViolation]
    region: list[RegionViolation]
    fixed_moved: list[FixedMoved]


class Soft(Result):
    hpwl_mm: float
    crossings: int
    decoupling: list[DecouplingViolation]
    proximity: list[ProximityViolation]
    separation: list[SeparationViolation]
    critical_nets: list[CriticalNetLength]


class CostTerms(Result):
    """The quantities the cost function weighs (see `cost`)."""

    weighted_hpwl: float
    crossings: int
    decoupling_sq: float
    proximity_sq: float
    separation_sq: float
    hard_violations: int


class Score(Result):
    hard: Hard
    soft: Soft
    terms: CostTerms
    cost: float


def cost(terms: CostTerms, weights: Weights) -> float:
    """C = w_h * sum_n HPWL_n * k_n + w_x * X + w_d * sum max(0, d - d_max)^2
    + w_p * sum max(0, p - p_max)^2 + w_s * sum max(0, s_min - s)^2 + P * H."""
    return (
        weights.hpwl * terms.weighted_hpwl
        + weights.crossings * terms.crossings
        + weights.decoupling * terms.decoupling_sq
        + weights.proximity * terms.proximity_sq
        + weights.separation * terms.separation_sq
        + HARD_PENALTY * terms.hard_violations
    )


def _matches(name: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    base = name.rsplit("/", 1)[-1]
    return any(p.fullmatch(base) for p in patterns)


def _compile(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


class _Placed:
    """Footprints at the scored poses, with board-frame geometry."""

    def __init__(self, board: BoardData, poses: Mapping[str, Pose]) -> None:
        self.footprints: dict[str, FootprintData] = {fp.ref: fp for fp in board.footprints}
        unknown = sorted(set(poses) - self.footprints.keys())
        if unknown:
            raise ConstraintError(f"poses for parts not on the board: {', '.join(unknown)}")
        self.poses: dict[str, Pose] = {ref: poses.get(ref, fp.pose) for ref, fp in self.footprints.items()}
        self.courtyards: dict[str, BaseGeometry] = {
            ref: self._courtyard(fp, self.poses[ref]) for ref, fp in self.footprints.items()
        }

    @staticmethod
    def _courtyard(fp: FootprintData, pose: Pose) -> BaseGeometry:
        rad = math.radians(pose.rot)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        polygons = [Polygon(ring) for ring in fp.courtyard if len(ring) >= 3]
        local = unary_union(polygons) if polygons else Polygon()
        return affinity.affine_transform(local, [cos_a, sin_a, -sin_a, cos_a, pose.x, pose.y])

    def pad_point(self, ref: str, pad: PadData) -> Point:
        return to_board(self.poses[ref], (pad.x, pad.y))


def find_pins(ic: FootprintData, pin: str) -> list[PadData]:
    """Pads of `ic` matching `pin`: by pad number first, then by symbol pin name."""
    return [p for p in ic.pads if p.number == pin] or [p for p in ic.pads if p.pin_name == pin]


def named_members(name: str, placement: Placement) -> list[str]:
    """Members of the group called `name`, else of every group in the region called `name`."""
    for group in placement.groups:
        if group.name == name:
            return group.members
    return [ref for g in placement.groups if g.region == name for ref in g.members]


def _mst_segments(pts: Sequence[Point]) -> list[tuple[Point, Point]]:
    """Prim's minimum spanning tree over the points (Euclidean)."""
    if len(pts) < 2:
        return []
    in_tree = [False] * len(pts)
    best = [math.inf] * len(pts)
    parent = [0] * len(pts)
    best[0] = 0.0
    segments: list[tuple[Point, Point]] = []
    for _ in range(len(pts)):
        u = min((i for i in range(len(pts)) if not in_tree[i]), key=lambda i: best[i])
        in_tree[u] = True
        if u != 0:
            segments.append((pts[parent[u]], pts[u]))
        for v in range(len(pts)):
            if not in_tree[v]:
                d = math.dist(pts[u], pts[v])
                if d < best[v]:
                    best[v] = d
                    parent[v] = u
    return segments


def _crossings(net_points: Mapping[str, Sequence[Point]]) -> int:
    lines: list[LineString] = []
    owner: list[str] = []
    for net, pts in net_points.items():
        for a, b in _mst_segments(pts):
            if a != b:
                lines.append(LineString([a, b]))
                owner.append(net)
    if not lines:
        return 0
    return sum(1 for i, j in _intersecting_pairs(lines) if owner[i] != owner[j])


def _intersecting_pairs(geoms: Sequence[BaseGeometry]) -> list[tuple[int, int]]:
    """Index pairs (i < j) of geometries that intersect."""
    left, right = STRtree(geoms).query(geoms, predicate="intersects")
    return [(i, j) for i, j in zip(map(int, left), map(int, right)) if i < j]


def _overlaps(placed: _Placed) -> list[Overlap]:
    refs = sorted(placed.courtyards)
    geoms = [placed.courtyards[ref] for ref in refs]
    found: list[Overlap] = []
    for i, j in _intersecting_pairs(geoms):
        a, b = refs[i], refs[j]
        if placed.footprints[a].side != placed.footprints[b].side:
            continue
        inter = geoms[i].intersection(geoms[j])
        if inter.area <= OVERLAP_MIN_AREA_MM2:
            continue
        min_x, min_y, max_x, max_y = inter.bounds
        found.append(Overlap(a=a, b=b, depth_mm=min(max_x - min_x, max_y - min_y)))
    return found


def courtyard_overlaps(board: BoardData, poses: Mapping[str, Pose]) -> list[Overlap]:
    """Courtyard overlaps with footprints at `poses` (missing refs keep their board pose)."""
    return _overlaps(_Placed(board, poses))


def _vertices(geom: BaseGeometry) -> list[Point]:
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    return [(x, y) for poly in polys if isinstance(poly, Polygon) for x, y in poly.exterior.coords]


def _max_vertex_distance(geom: BaseGeometry, target: BaseGeometry) -> float:
    """How far the farthest courtyard corner lies outside `target` (0 if none does)."""
    return max((target.distance(ShapelyPoint(v)) for v in _vertices(geom)), default=0.0)


def _edge(board: BoardData, placed: _Placed, placement: Placement) -> list[EdgeViolation]:
    outline = Polygon(board.outline, board.holes)
    min_mm = placement.board.edge_clearance_mm
    found: list[EdgeViolation] = []
    for ref, geom in sorted(placed.courtyards.items()):
        if geom.is_empty or ref in placement.fixed or placed.footprints[ref].locked:
            continue
        if outline.contains(geom):
            clearance = geom.distance(outline.boundary)
            if clearance >= min_mm:
                continue
        else:
            clearance = -_max_vertex_distance(geom, outline)
        found.append(EdgeViolation(ref=ref, clearance_mm=clearance, min_mm=min_mm))
    return found


def _regions(placed: _Placed, placement: Placement) -> list[RegionViolation]:
    found: list[RegionViolation] = []
    for group in placement.groups:
        if group.region is None:
            continue
        area = box(*placement.regions[group.region])
        for ref in group.members:
            geom = placed.courtyards[ref]
            if geom.is_empty or area.covers(geom):
                continue
            found.append(RegionViolation(ref=ref, region=group.region, outside_mm=_max_vertex_distance(geom, area)))
    return found


def _fixed_moved(placed: _Placed, placement: Placement) -> list[FixedMoved]:
    found: list[FixedMoved] = []
    for ref, fixed in sorted(placement.fixed.items()):
        pose = placed.poses[ref]
        moved = math.hypot(pose.x - fixed.x, pose.y - fixed.y)
        rot_delta = 0.0 if fixed.rot is None else (pose.rot - fixed.rot + 180.0) % 360.0 - 180.0
        if moved > FIXED_POSITION_TOLERANCE_MM or abs(rot_delta) > FIXED_ROTATION_TOLERANCE_DEG:
            found.append(FixedMoved(ref=ref, moved_mm=moved, rot_delta=rot_delta))
    return found


def _decoupling(placed: _Placed, placement: Placement, ground: Sequence[re.Pattern[str]], power: Sequence[re.Pattern[str]]) -> list[DecouplingViolation]:
    found: list[DecouplingViolation] = []
    explicit = [rule for rule in placement.decoupling if isinstance(rule, DecouplingPair)]
    for rule in explicit:
        cap = placed.footprints[rule.cap]
        ic = placed.footprints[rule.ic]
        dist = min(
            math.dist(placed.pad_point(cap.ref, cp), placed.pad_point(ic.ref, ip))
            for ip in find_pins(ic, rule.pin)
            for cp in cap.pads
            if cp.net and cp.net == ip.net
        )
        if dist > rule.max_mm:
            found.append(DecouplingViolation(cap=rule.cap, ic=rule.ic, pin=rule.pin, dist_mm=dist, max_mm=rule.max_mm, hard=rule.hard))

    paired = {rule.cap for rule in explicit}
    ics = [fp for ref, fp in sorted(placed.footprints.items()) if IC_REF.fullmatch(ref)]
    for rule in (r for r in placement.decoupling if isinstance(r, DecouplingAuto)):
        for ref, cap in sorted(placed.footprints.items()):
            if ref in paired or not CAPACITOR_REF.fullmatch(ref):
                continue
            best: tuple[float, str, str] | None = None
            for cp in cap.pads:
                if not cp.net or not _matches(cp.net, power) or _matches(cp.net, ground):
                    continue
                for ic in ics:
                    for ip in ic.pads:
                        if ip.net == cp.net:
                            d = math.dist(placed.pad_point(ref, cp), placed.pad_point(ic.ref, ip))
                            if best is None or d < best[0]:
                                best = (d, ic.ref, ip.number)
            if best is not None and best[0] > rule.max_mm:
                found.append(DecouplingViolation(cap=ref, ic=best[1], pin=best[2], dist_mm=best[0], max_mm=rule.max_mm, hard=rule.hard))
    return found


def _proximity(placed: _Placed, placement: Placement) -> list[ProximityViolation]:
    found: list[ProximityViolation] = []
    for rule in placement.proximity:
        dist = placed.courtyards[rule.a].distance(placed.courtyards[rule.b])
        if dist > rule.max_mm:
            found.append(ProximityViolation(a=rule.a, b=rule.b, dist_mm=dist, max_mm=rule.max_mm, hard=rule.hard))
    return found


def _separation(placed: _Placed, placement: Placement) -> list[SeparationViolation]:
    found: list[SeparationViolation] = []
    for rule in placement.separation:
        sides: list[BaseGeometry] = [
            unary_union([placed.courtyards[ref] for ref in named_members(name, placement)]) for name in (rule.a, rule.b)
        ]
        if sides[0].is_empty or sides[1].is_empty:
            continue
        dist = sides[0].distance(sides[1])
        if dist < rule.min_mm:
            found.append(SeparationViolation(a=rule.a, b=rule.b, dist_mm=dist, min_mm=rule.min_mm, hard=rule.hard))
    return found


def score(board: BoardData, placement: Placement, poses: Mapping[str, Pose] | None = None) -> Score:
    """Score `board` with footprints at `poses` (missing refs keep their board pose)."""
    placed = _Placed(board, poses or {})
    power = _compile(placement.power_nets)
    ground = _compile(GROUND_NET_PATTERNS)
    critical = {c.net: c.weight for c in placement.critical_nets}

    net_points: dict[str, list[Point]] = {}
    for ref, fp in placed.footprints.items():
        for pad in fp.pads:
            if pad.net:
                net_points.setdefault(pad.net, []).append(placed.pad_point(ref, pad))

    signal = {net: pts for net, pts in net_points.items() if not _matches(net, power)}
    hpwl: dict[str, float] = {}
    for net, pts in net_points.items():
        if net in signal or net in critical:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            hpwl[net] = (max(xs) - min(xs)) + (max(ys) - min(ys))

    hard = Hard(
        overlaps=_overlaps(placed),
        edge=_edge(board, placed, placement),
        region=_regions(placed, placement),
        fixed_moved=_fixed_moved(placed, placement),
    )
    soft = Soft(
        hpwl_mm=sum(hpwl.values()),
        crossings=_crossings(signal),
        decoupling=_decoupling(placed, placement, ground, power),
        proximity=_proximity(placed, placement),
        separation=_separation(placed, placement),
        critical_nets=[CriticalNetLength(net=net, hpwl_mm=hpwl[net], weight=w) for net, w in critical.items()],
    )
    hard_soft = sum(1 for v in [*soft.decoupling, *soft.proximity, *soft.separation] if v.hard)
    terms = CostTerms(
        weighted_hpwl=sum(length * critical.get(net, 1.0) for net, length in hpwl.items()),
        crossings=soft.crossings,
        decoupling_sq=sum((v.dist_mm - v.max_mm) ** 2 for v in soft.decoupling),
        proximity_sq=sum((v.dist_mm - v.max_mm) ** 2 for v in soft.proximity),
        separation_sq=sum((v.min_mm - v.dist_mm) ** 2 for v in soft.separation),
        hard_violations=len(hard.overlaps) + len(hard.edge) + len(hard.region) + len(hard.fixed_moved) + hard_soft,
    )
    return Score(hard=hard, soft=soft, terms=terms, cost=cost(terms, placement.weights))
