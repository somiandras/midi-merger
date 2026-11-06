# /// script
# requires-python = ">=3.12"
# dependencies = ["kicad-python==0.8.0", "shapely>=2.0", "pydantic>=2.6", "pyyaml>=6"]
# ///
"""Read footprints from, and move footprints on, a board open in KiCad, over the IPC API.

The board is only ever reached through a running KiCad (kicad-python / kipy); the
.kicad_pcb file is never read or written. Before anything else, `open_board` checks that
the PCB open on the given socket is the file named on the command line, so a script can
never touch a different board that happens to be open on that socket.

Coordinates leaving this module are in the board frame of board_model (mm, degrees, origin
at the top-left of the bounding box of the chosen Edge.Cuts outline). KiCad's absolute
nanometres stay inside.

Edge.Cuts may hold several separate outlines (two boards side by side in one file). Then
`outline_at`, a point in KiCad's absolute mm inside the wanted outline, picks one; without
it read_board refuses and lists the outlines.

kicad-python is pinned to 0.8.0 in every script header because this module works around
kipy 0.8.0's KiCad 10 gaps (raw GetItems unpacking and a hand-built ReferencePoint message);
drop the workaround, and the pin, once kipy supports KiCad 10.

CLI (--outline-at X Y picks the outline when there are several):
    board_io.py BOARD [--socket URL] read          outlines, frame origin and footprint poses as JSON
    board_io.py BOARD [--socket URL] nets          every footprint's pads: number, pin name, net
    board_io.py BOARD [--socket URL] move FILE     apply [{ref, x, y, rot}, ...] in one undo step
"""

import argparse
import json
import math
import sys
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.any_pb2 import Any as AnyProto
from google.protobuf.message import Message
from kipy import KiCad
from kipy.board import Board
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.proto.board import board_types_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.commands.editor_commands_pb2 import (
    GetItems,
    GetItemsResponse,
    ItemStatusCode,
    UpdateItems,
    UpdateItemsResponse,
)
from kipy.proto.common.types import DocumentType, KiCadObjectType, base_types_pb2
from pydantic import TypeAdapter, ValidationError
from shapely import MultiPoint, MultiPolygon, Polygon, unary_union
from shapely.geometry import LineString
from shapely.geometry import Point as ShapelyPoint
from shapely.ops import polygonize

from board_model import BoardData, FootprintData, Move, PadData, Point, Pose, Ring, rotate, to_local

NM_PER_MM = 1_000_000
ARC_SEGMENTS_PER_TURN = 72
PAD_BBOX_MARGIN_MM = 0.25
COURTYARD_LAYERS = (BoardLayer.BL_F_CrtYd, BoardLayer.BL_B_CrtYd)

FootprintProto = board_types_pb2.FootprintInstance

_REFERENCE_POINT_URL = "type.googleapis.com/kiapi.board.types.ReferencePoint"


class BoardIOError(Exception):
    """The board cannot be reached, is the wrong board, or cannot be changed safely."""


@contextmanager
def _kicad_errors(action: str) -> Generator[None]:
    """Report kipy API and connection failures as BoardIOError."""
    try:
        yield
    except (ApiError, KiCadConnectionError) as exc:
        raise BoardIOError(f"{action}: {exc}") from exc


def _reference_point_class() -> type[Message]:
    """Message class for KiCad 10's footprint ReferencePoint item, which kipy 0.8.0 lacks.

    Field numbers follow api/proto/board/board_types.proto in KiCad 10.0.
    """
    pool = descriptor_pool.Default()
    name = "kiapi.board.types.ReferencePoint"
    try:
        return message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
    except KeyError:
        pass
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="pcb_placement/reference_point.proto",
        package="kiapi.board.types",
        syntax="proto3",
        dependency=["common/types/base_types.proto", "board/board_types.proto"],
    )
    msg = file_proto.message_type.add(name="ReferencePoint")
    fields = (
        ("id", 1, ".kiapi.common.types.KIID", descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE),
        ("layer", 2, ".kiapi.board.types.BoardLayer", descriptor_pb2.FieldDescriptorProto.TYPE_ENUM),
        ("position", 3, ".kiapi.common.types.Vector2", descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE),
        ("size", 4, ".kiapi.common.types.Distance", descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE),
        ("locked", 5, ".kiapi.common.types.LockedState", descriptor_pb2.FieldDescriptorProto.TYPE_ENUM),
        ("parent", 6, ".kiapi.common.types.KIID", descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE),
    )
    for field_name, number, type_name, field_type in fields:
        msg.field.add(
            name=field_name,
            number=number,
            type_name=type_name,
            type=field_type,
            label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        )
    pool.Add(file_proto)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(name))


def open_board(board_path: Path, socket: str | None) -> Board:
    """Connect to KiCad on `socket` (kipy's default when None) and return the board there.

    Raises BoardIOError unless the PCB open on that socket is `board_path`.
    """
    wanted = board_path.expanduser().resolve()
    where = socket or "the default socket"
    with _kicad_errors(f"cannot talk to KiCad on {where}"):
        kicad = KiCad(socket_path=socket)
        docs = kicad.get_open_documents(DocumentType.DOCTYPE_PCB)
        if len(docs) != 1:
            raise BoardIOError(f"expected one open PCB on {where}, found {len(docs)}")
        board = kicad.get_board()
    doc = board.document
    open_path = (Path(doc.project.path) / doc.board_filename).resolve()
    if open_path != wanted:
        raise BoardIOError(f"the PCB open on {where} is {open_path}, not {wanted}; refusing to continue")
    return board


def _footprint_protos(board: Board) -> list[FootprintProto]:
    """Raw footprint messages; kipy's own unpacking fails on KiCad 10 ReferencePoint items."""
    cmd = GetItems()
    cmd.header.document.CopyFrom(board.document)
    cmd.types.append(KiCadObjectType.KOT_PCB_FOOTPRINT)
    footprints: list[FootprintProto] = []
    for item in board.client.send(cmd, GetItemsResponse).items:
        fp = FootprintProto()
        item.Unpack(fp)
        footprints.append(fp)
    return footprints


def _ref(fp: FootprintProto) -> str:
    return fp.reference_field.text.text.text


def _mm(v: base_types_pb2.Vector2) -> Point:
    return (v.x_nm / NM_PER_MM, v.y_nm / NM_PER_MM)


def _arc_points(start: Point, mid: Point, end: Point) -> list[Point]:
    """Points along the circular arc through start, mid and end."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    radius = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    sweep = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    steps = max(2, math.ceil(abs(sweep) / (2 * math.pi) * ARC_SEGMENTS_PER_TURN))
    points = [
        (ux + radius * math.cos(a0 + sweep * i / steps), uy + radius * math.sin(a0 + sweep * i / steps))
        for i in range(1, steps)
    ]
    return [start, *points, end]


def _polyline_points(polyline: base_types_pb2.PolyLine) -> list[Point]:
    points: list[Point] = []
    for node in polyline.nodes:
        if node.HasField("arc"):
            points.extend(_arc_points(_mm(node.arc.start), _mm(node.arc.mid), _mm(node.arc.end)))
        else:
            points.append(_mm(node.point))
    if points:
        points.append(points[0])
    return points


def _shape_lines(shape: base_types_pb2.GraphicShape) -> list[list[Point]]:
    """Polylines (absolute mm) tracing a graphic shape's outline."""
    kind = shape.WhichOneof("geometry")
    if kind == "segment":
        return [[_mm(shape.segment.start), _mm(shape.segment.end)]]
    if kind == "arc":
        return [_arc_points(_mm(shape.arc.start), _mm(shape.arc.mid), _mm(shape.arc.end))]
    if kind == "circle":
        cx, cy = _mm(shape.circle.center)
        rx, ry = _mm(shape.circle.radius_point)
        r = math.hypot(rx - cx, ry - cy)
        ring = [
            (cx + r * math.cos(2 * math.pi * i / ARC_SEGMENTS_PER_TURN), cy + r * math.sin(2 * math.pi * i / ARC_SEGMENTS_PER_TURN))
            for i in range(ARC_SEGMENTS_PER_TURN)
        ]
        return [[*ring, ring[0]]]
    if kind == "rectangle":
        x0, y0 = _mm(shape.rectangle.top_left)
        x1, y1 = _mm(shape.rectangle.bottom_right)
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
    if kind == "polygon":
        lines: list[list[Point]] = []
        for poly in shape.polygon.polygons:
            lines.append(_polyline_points(poly.outline))
            lines.extend(_polyline_points(hole) for hole in poly.holes)
        return [line for line in lines if len(line) >= 2]
    if kind == "bezier":
        p0, p1, p2, p3 = (_mm(shape.bezier.start), _mm(shape.bezier.control1), _mm(shape.bezier.control2), _mm(shape.bezier.end))
        steps = 16
        return [
            [
                (
                    (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t * t * p2[0] + t**3 * p3[0],
                    (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t * t * p2[1] + t**3 * p3[1],
                )
                for t in (i / steps for i in range(steps + 1))
            ]
        ]
    return []


def _faces(lines: Iterable[list[Point]]) -> list[Polygon]:
    """Closed faces formed by the line work (nested rings come back as holes)."""
    return list(polygonize([LineString(line) for line in lines if len(line) >= 2]))


def _polygons(geom: Polygon | MultiPolygon) -> list[Polygon]:
    return list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]


def read_outlines(board: Board) -> list[Polygon]:
    """Separate board outlines in absolute mm (see outer_faces)."""
    with _kicad_errors("reading the board outline from KiCad"):
        shapes = board.get_shapes()
    return outer_faces([line for shape in shapes if shape.layer == BoardLayer.BL_Edge_Cuts for line in _shape_lines(shape.proto.shape)])


def outer_faces(lines: Iterable[list[Point]]) -> list[Polygon]:
    """Separate outlines formed by the line work, cutouts as holes, ordered left to right.

    The faces that fill a cutout are dropped: a face counts only when it lies inside no
    other face's outer ring.
    """
    faces = _faces(lines)
    if not faces:
        raise BoardIOError("Edge.Cuts does not form a closed outline")
    shells = [Polygon(face.exterior) for face in faces]
    outer = [
        face
        for i, face in enumerate(faces)
        if not any(j != i and shells[j].area > shells[i].area and shells[j].contains(face.representative_point()) for j in range(len(faces)))
    ]
    return sorted(outer, key=lambda face: (face.bounds[0], face.bounds[1]))


def _outline_line(face: Polygon) -> str:
    x0, y0, x1, y1 = face.bounds
    px, py = face.representative_point().coords[0]
    return f"x {x0:.2f} to {x1:.2f}, y {y0:.2f} to {y1:.2f} (KiCad mm); to pick it, add under board:  outline_at: [{px:.3f}, {py:.3f}]"


def choose_outline(faces: Sequence[Polygon], outline_at: Point | None) -> Polygon:
    """The outline containing `outline_at`; the only outline when there is one and no point."""
    listing = "".join(f"\n  outline {i + 1}: {_outline_line(face)}" for i, face in enumerate(faces))
    if outline_at is None:
        if len(faces) == 1:
            return faces[0]
        raise BoardIOError(
            f"Edge.Cuts holds {len(faces)} separate outlines; placement.yaml must pick one with board.outline_at, "
            f"a point inside it in KiCad's absolute coordinates (mm):{listing}"
        )
    point = ShapelyPoint(outline_at)
    for face in faces:
        if Polygon(face.exterior).covers(point):
            return face
    raise BoardIOError(f"board.outline_at {list(outline_at)} lies inside none of the Edge.Cuts outlines:{listing}")


def _origin_nm(outline: Polygon) -> tuple[int, int]:
    min_x, min_y, _, _ = outline.bounds
    return (round(min_x * NM_PER_MM), round(min_y * NM_PER_MM))


def _pads(fp: FootprintProto) -> list[board_types_pb2.Pad]:
    pads: list[board_types_pb2.Pad] = []
    for item in fp.definition.items:
        if item.Is(board_types_pb2.Pad.DESCRIPTOR):
            pad = board_types_pb2.Pad()
            item.Unpack(pad)
            pads.append(pad)
    return pads


def _courtyard_shapes(fp: FootprintProto) -> list[base_types_pb2.GraphicShape]:
    shapes: list[base_types_pb2.GraphicShape] = []
    for item in fp.definition.items:
        if item.Is(board_types_pb2.BoardGraphicShape.DESCRIPTOR):
            msg = board_types_pb2.BoardGraphicShape()
            item.Unpack(msg)
            if msg.layer in COURTYARD_LAYERS:
                shapes.append(msg.shape)
    return shapes


def _pad_rectangle(pad: board_types_pb2.Pad, with_drill: bool = False) -> list[Point]:
    """Corners (absolute mm) of the pad's largest copper shape, rotated with the pad;
    with `with_drill`, at least as large as the drilled hole."""
    width = max((layer.size.x_nm for layer in pad.pad_stack.copper_layers), default=0) / NM_PER_MM
    height = max((layer.size.y_nm for layer in pad.pad_stack.copper_layers), default=0) / NM_PER_MM
    if with_drill:
        width = max(width, pad.pad_stack.drill.diameter.x_nm / NM_PER_MM)
        height = max(height, pad.pad_stack.drill.diameter.y_nm / NM_PER_MM)
    cx, cy = _mm(pad.position)
    angle = math.radians(pad.pad_stack.angle.value_degrees)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = [(-width / 2, -height / 2), (width / 2, -height / 2), (width / 2, height / 2), (-width / 2, height / 2)]
    return [(cx + x * cos_a + y * sin_a, cy + y * cos_a - x * sin_a) for x, y in corners]


def _courtyard(fp: FootprintProto, pads: Sequence[board_types_pb2.Pad]) -> tuple[list[Polygon], bool]:
    """Courtyard polygons in absolute mm, and whether they fell back to the pad bounding box."""
    lines = [line for shape in _courtyard_shapes(fp) for line in _shape_lines(shape)]
    faces = _faces(lines)
    if faces:
        merged = unary_union(faces)
        if isinstance(merged, (Polygon, MultiPolygon)):
            return _polygons(merged), False
    # Courtyard lines that do not close: use their convex hull.
    hull = MultiPoint([p for line in lines for p in line]).convex_hull
    if isinstance(hull, Polygon):
        return [hull], False
    corners = [p for pad in pads for p in _pad_rectangle(pad)]
    if not corners:
        return [], True
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    m = PAD_BBOX_MARGIN_MM
    box = Polygon([(min(xs) - m, min(ys) - m), (max(xs) + m, min(ys) - m), (max(xs) + m, max(ys) + m), (min(xs) - m, max(ys) + m)])
    return [box], True


def _abs_pose(fp: FootprintProto) -> Pose:
    return Pose(x=fp.position.x_nm / NM_PER_MM, y=fp.position.y_nm / NM_PER_MM, rot=fp.orientation.value_degrees)


def _side(fp: FootprintProto) -> Literal["top", "bottom"]:
    return "bottom" if fp.layer == BoardLayer.BL_B_Cu else "top"


def footprint_data(fp: FootprintProto, origin: Point, shell: Polygon | None) -> FootprintData:
    """Footprint in the board frame at `origin`; off the board when it misses `shell` (None: on it)."""
    ox, oy = origin
    abs_pose = _abs_pose(fp)
    pads = _pads(fp)
    courtyard, from_pads = _courtyard(fp, pads)
    pad_data: list[PadData] = []
    for pad in pads:
        lx, ly = to_local(abs_pose, _mm(pad.position))
        pad_data.append(PadData(number=pad.number, pin_name=pad.symbol_pin.name, net=pad.net.name, x=lx, y=ly))
    rings: list[Ring] = [[to_local(abs_pose, (x, y)) for x, y in poly.exterior.coords] for poly in courtyard]
    holes: list[Ring] = [
        [to_local(abs_pose, p) for p in _pad_rectangle(pad, with_drill=True)]
        for pad in pads
        if pad.type in (board_types_pb2.PadType.PT_PTH, board_types_pb2.PadType.PT_NPTH)
    ]
    on_board = True
    if shell is not None:
        on_board = any(shell.intersects(poly) for poly in courtyard) if courtyard else shell.covers(ShapelyPoint(abs_pose.x, abs_pose.y))
    return FootprintData(
        ref=_ref(fp),
        pose=Pose(x=abs_pose.x - ox, y=abs_pose.y - oy, rot=abs_pose.rot),
        side=_side(fp),
        locked=fp.locked == base_types_pb2.LockedState.LS_LOCKED,
        pads=pad_data,
        courtyard=rings,
        courtyard_from_pads=from_pads,
        through_holes=holes,
        on_board=on_board,
    )


def read_board(board: Board, outline_at: Point | None = None) -> BoardData:
    """The board in the frame of its outline (see choose_outline for `outline_at`).

    With several outlines, footprints whose courtyard misses the chosen one are flagged
    `on_board=False`.
    """
    faces = read_outlines(board)
    outline = choose_outline(faces, outline_at)
    with _kicad_errors("reading the board from KiCad"):
        footprints = _footprint_protos(board)
    origin_nm = _origin_nm(outline)
    origin = (origin_nm[0] / NM_PER_MM, origin_nm[1] / NM_PER_MM)
    min_x, min_y, max_x, max_y = outline.bounds
    shell = Polygon(outline.exterior) if len(faces) > 1 else None

    def shift(coords: Iterable[tuple[float, ...]]) -> Ring:
        return [(c[0] - origin[0], c[1] - origin[1]) for c in coords]

    doc = board.document
    return BoardData(
        path=str(Path(doc.project.path) / doc.board_filename),
        origin_x_nm=origin_nm[0],
        origin_y_nm=origin_nm[1],
        width_mm=max_x - min_x,
        height_mm=max_y - min_y,
        outline=shift(outline.exterior.coords),
        holes=[shift(hole.coords) for hole in outline.interiors],
        footprints=[footprint_data(fp, origin, shell) for fp in footprints],
    )


def _normalize180(degrees: float) -> float:
    """Footprint orientation range used by KiCad: (-180, 180]."""
    value = math.fmod(degrees, 360.0)
    if value <= -180.0:
        value += 360.0
    elif value > 180.0:
        value -= 360.0
    return value


def _normalize360(degrees: float) -> float:
    """Pad and text angle range used by KiCad: [0, 360)."""
    value = math.fmod(degrees, 360.0)
    return value + 360.0 if value < 0.0 else value


def _rotate_nm(x: int, y: int, degrees: float) -> tuple[int, int]:
    """board_model.rotate on integer nanometres, exact for multiples of 90 degrees."""
    quarters = degrees / 90.0
    if quarters == round(quarters):
        match round(quarters) % 4:
            case 0:
                return (x, y)
            case 1:
                return (y, -x)
            case 2:
                return (-x, -y)
            case _:
                return (-y, x)
    rx, ry = rotate((x, y), degrees)
    return (round(rx), round(ry))


class _Transform:
    """Rigid motion of a footprint and its children: rotate about the old position by
    `turn` degrees, then translate the old position onto the new one."""

    def __init__(self, ref: str, old: tuple[int, int], new: tuple[int, int], turn: float) -> None:
        self.ref = ref
        self.old = old
        self.new = new
        self.turn = turn

    def point(self, v: base_types_pb2.Vector2) -> None:
        rx, ry = _rotate_nm(v.x_nm - self.old[0], v.y_nm - self.old[1], self.turn)
        v.x_nm = self.new[0] + rx
        v.y_nm = self.new[1] + ry

    def angle(self, a: base_types_pb2.Angle) -> None:
        a.value_degrees = _normalize360(a.value_degrees + self.turn)

    def text(self, t: base_types_pb2.Text) -> None:
        self.point(t.position)
        self.angle(t.attributes.angle)

    def polyset(self, polyset: base_types_pb2.PolySet) -> None:
        for polygon in polyset.polygons:
            for line in (polygon.outline, *polygon.holes):
                for node in line.nodes:
                    if node.HasField("arc"):
                        self.point(node.arc.start)
                        self.point(node.arc.mid)
                        self.point(node.arc.end)
                    else:
                        self.point(node.point)

    def shape(self, shape: base_types_pb2.GraphicShape) -> None:
        kind = shape.WhichOneof("geometry")
        if kind == "segment":
            self.point(shape.segment.start)
            self.point(shape.segment.end)
        elif kind == "arc":
            self.point(shape.arc.start)
            self.point(shape.arc.mid)
            self.point(shape.arc.end)
        elif kind == "circle":
            self.point(shape.circle.center)
            self.point(shape.circle.radius_point)
        elif kind == "rectangle":
            if self.turn % 90.0 != 0.0:
                raise BoardIOError(f"{self.ref}: rectangles in this footprint only rotate by multiples of 90 degrees")
            self.point(shape.rectangle.top_left)
            self.point(shape.rectangle.bottom_right)
        elif kind == "polygon":
            self.polyset(shape.polygon)
        elif kind == "bezier":
            for v in (shape.bezier.start, shape.bezier.control1, shape.bezier.control2, shape.bezier.end):
                self.point(v)
        else:
            raise BoardIOError(f"{self.ref}: cannot move a graphic shape of kind {kind}")


def _moved(fp: FootprintProto, x_nm: int, y_nm: int, rot: float) -> FootprintProto:
    """Copy of `fp` at a new absolute position and rotation, children moved along.

    KiCad stores footprint children at absolute coordinates and rebuilds them from the
    message, so every child must be transformed here or it is left behind.
    """
    new_rot = _normalize180(rot)
    t = _Transform(_ref(fp), (fp.position.x_nm, fp.position.y_nm), (x_nm, y_nm), new_rot - fp.orientation.value_degrees)
    moved = FootprintProto()
    moved.CopyFrom(fp)
    moved.position.x_nm, moved.position.y_nm = x_nm, y_nm
    moved.orientation.value_degrees = new_rot
    for field in (moved.reference_field, moved.value_field, moved.datasheet_field, moved.description_field):
        t.text(field.text.text)

    for item in moved.definition.items:
        if item.Is(board_types_pb2.Pad.DESCRIPTOR):
            pad = board_types_pb2.Pad()
            item.Unpack(pad)
            t.point(pad.position)
            t.angle(pad.pad_stack.angle)
            item.Pack(pad)
        elif item.Is(board_types_pb2.BoardGraphicShape.DESCRIPTOR):
            graphic = board_types_pb2.BoardGraphicShape()
            item.Unpack(graphic)
            t.shape(graphic.shape)
            item.Pack(graphic)
        elif item.Is(board_types_pb2.BoardText.DESCRIPTOR):
            text = board_types_pb2.BoardText()
            item.Unpack(text)
            t.text(text.text)
            item.Pack(text)
        elif item.Is(board_types_pb2.Field.DESCRIPTOR):
            field = board_types_pb2.Field()
            item.Unpack(field)
            t.text(field.text.text)
            item.Pack(field)
        elif item.Is(board_types_pb2.Zone.DESCRIPTOR):
            zone = board_types_pb2.Zone()
            item.Unpack(zone)
            t.polyset(zone.outline)
            for filled in zone.filled_polygons:
                t.polyset(filled.shapes)
            item.Pack(zone)
        elif item.type_url == _REFERENCE_POINT_URL:
            point = _reference_point_class()()
            item.Unpack(point)
            position = getattr(point, "position")
            if not isinstance(position, base_types_pb2.Vector2):
                raise BoardIOError(f"{t.ref}: unexpected ReferencePoint layout")
            t.point(position)
            item.Pack(point)
        elif not item.Is(board_types_pb2.Footprint3DModel.DESCRIPTOR):
            # 3D model offsets are relative to the footprint; anything else is unknown.
            raise BoardIOError(f"{t.ref}: cannot move a footprint containing {item.type_url}")
    return moved


def apply_moves(board: Board, moves: Sequence[Move], origin_nm: tuple[int, int], message: str = "pcb-placement") -> None:
    """Move footprints in one KiCad commit, so a single undo reverts all of them.

    Move coordinates are in the board frame whose origin is `origin_nm` (BoardData.origin_x_nm,
    origin_y_nm of the same board). Refuses, before changing anything, if a ref is unknown,
    repeated, or locked in KiCad.
    """
    refs = [move.ref for move in moves]
    repeated = sorted({ref for ref in refs if refs.count(ref) > 1})
    if repeated:
        raise BoardIOError(f"moved more than once: {', '.join(repeated)}")
    with _kicad_errors("reading the board from KiCad"):
        by_ref = {_ref(fp): fp for fp in _footprint_protos(board)}
    unknown = sorted(set(refs) - by_ref.keys())
    if unknown:
        raise BoardIOError(f"not on the board: {', '.join(unknown)}")
    locked = sorted(ref for ref in refs if by_ref[ref].locked == base_types_pb2.LockedState.LS_LOCKED)
    if locked:
        raise BoardIOError(f"locked in KiCad, refusing to move: {', '.join(locked)}")

    origin_x, origin_y = origin_nm
    cmd = UpdateItems()
    cmd.header.document.CopyFrom(board.document)
    for move in moves:
        x_nm = origin_x + round(move.x * NM_PER_MM)
        y_nm = origin_y + round(move.y * NM_PER_MM)
        packed = AnyProto()
        packed.Pack(_moved(by_ref[move.ref], x_nm, y_nm, move.rot))
        cmd.items.append(packed)
    if not cmd.items:
        return

    with _kicad_errors("moving footprints"):
        commit = board.begin_commit()
        try:
            response = board.client.send(cmd, UpdateItemsResponse)
        except (ApiError, KiCadConnectionError):
            board.drop_commit(commit)
            raise
    failed = [
        f"{move.ref}: {ItemStatusCode.Name(result.status.code)} {result.status.error_message}"
        for move, result in zip(moves, response.updated_items)
        if result.status.code != ItemStatusCode.ISC_OK
    ]
    with _kicad_errors("moving footprints"):
        if failed or len(response.updated_items) != len(moves):
            board.drop_commit(commit)
            raise BoardIOError("KiCad rejected the moves: " + "; ".join(failed or ["missing results"]))
        board.push_commit(commit, message)


def _read_json(board: Board, outline_at: Point | None) -> dict[str, object]:
    """Outlines, frame origin and footprint poses. With several outlines and no `outline_at`,
    poses are in KiCad's absolute coordinates ("frame": "kicad") instead of the board frame."""
    faces = read_outlines(board)
    chosen = None if outline_at is None and len(faces) > 1 else choose_outline(faces, outline_at)
    outlines = [
        {"kicad_mm": [round(v, 3) for v in face.bounds], "chosen": face is chosen, "outline_at": [round(v, 3) for v in face.representative_point().coords[0]]}
        for face in faces
    ]
    if chosen is None:
        with _kicad_errors("reading the board from KiCad"):
            protos = _footprint_protos(board)
        footprints: list[dict[str, object]] = []
        for fp in protos:
            pose = _abs_pose(fp)
            locked = fp.locked == base_types_pb2.LockedState.LS_LOCKED
            footprints.append({"ref": _ref(fp), "x": round(pose.x, 4), "y": round(pose.y, 4), "rot": pose.rot, "side": _side(fp), "locked": locked})
        return {
            "outlines": outlines,
            "frame": "kicad",
            "note": "several outlines: poses are in KiCad mm; pass --outline-at X Y (or set board.outline_at) for the board frame",
            "footprints": sorted(footprints, key=lambda f: str(f["ref"])),
        }
    data = read_board(board, outline_at)
    several = len(faces) > 1
    return {
        "outlines": outlines,
        "frame": "board",
        "origin_kicad_mm": [data.origin_x_nm / NM_PER_MM, data.origin_y_nm / NM_PER_MM],
        "board": {"w_mm": round(data.width_mm, 3), "h_mm": round(data.height_mm, 3)},
        "footprints": [
            {"ref": fp.ref, "x": round(fp.pose.x, 4), "y": round(fp.pose.y, 4), "rot": fp.pose.rot, "side": fp.side, "locked": fp.locked}
            | ({"on_board": fp.on_board} if several else {})
            for fp in sorted(data.footprints, key=lambda f: f.ref)
        ],
    }


def nets_line(fp: FootprintProto) -> str:
    """Ref, value, footprint name, side, then number=net for each pad ('-' for no net).
    A pad whose symbol pin name differs from its number shows as number(name)=net."""
    pads: list[str] = []
    for pad in _pads(fp):
        name = pad.symbol_pin.name
        label = pad.number if not name or name == pad.number else f"{pad.number}({name})"
        pads.append(f"{label}={pad.net.name or '-'}")
    lib = fp.definition.id
    return f"{_ref(fp)} {fp.value_field.text.text.text} [{lib.library_nickname}:{lib.entry_name}] {_side(fp)}: {' '.join(pads)}"


def _nets_text(board: Board) -> str:
    with _kicad_errors("reading the board from KiCad"):
        protos = sorted(_footprint_protos(board), key=_ref)
    return "\n".join(nets_line(fp) for fp in protos)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", type=Path, help="path of the .kicad_pcb open in KiCad")
    parser.add_argument("--socket", default=None, help="KiCad IPC socket, e.g. ipc:///tmp/kicad/api-1234.sock")
    parser.add_argument(
        "--outline-at", nargs=2, type=float, metavar=("X", "Y"), default=None, help="point inside the wanted Edge.Cuts outline, KiCad mm"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("read", help="print outlines, frame origin and footprint poses as JSON")
    sub.add_parser("nets", help="print each footprint's pads with pin name and net")
    move_parser = sub.add_parser("move", help="apply moves from a JSON file in one undo step")
    move_parser.add_argument("moves", type=Path, help="JSON list of {ref, x, y, rot}")
    args = parser.parse_args()
    outline_at: Point | None = None if args.outline_at is None else (args.outline_at[0], args.outline_at[1])

    try:
        board = open_board(args.board, args.socket)
        if args.command == "read":
            print(json.dumps(_read_json(board, outline_at)))
            return 0
        if args.command == "nets":
            print(_nets_text(board))
            return 0
        moves = TypeAdapter(list[Move]).validate_json(Path(args.moves).read_text())
        data = read_board(board, outline_at)
        apply_moves(board, moves, (data.origin_x_nm, data.origin_y_nm))
        print(f"moved {len(moves)} footprints")
        return 0
    except (BoardIOError, OSError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
