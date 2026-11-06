"""Plain board data passed from board_io (KiCad IPC) to scoring (pure geometry).

Units are millimetres and degrees. The board frame has its origin at the top-left corner of
the Edge.Cuts bounding box and y grows downward, as in KiCad.

Pad and courtyard coordinates are local to their footprint: the offset from the footprint
position with the footprint's rotation undone. A footprint at pose (x, y, rot) has a local
point p at `to_board(pose, p)`, so a placer can try new poses without touching KiCad.
"""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict

Point = tuple[float, float]
Ring = list[Point]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Pose(Frozen):
    x: float
    y: float
    rot: float


class Move(Frozen):
    """A new pose for one footprint, in the board frame."""

    ref: str
    x: float
    y: float
    rot: float


class PadData(Frozen):
    number: str
    pin_name: str
    net: str
    x: float
    y: float


class FootprintData(Frozen):
    ref: str
    pose: Pose
    side: Literal["top", "bottom"]
    locked: bool
    pads: list[PadData]
    # Exterior rings of the courtyard polygons, local coordinates.
    courtyard: list[Ring]
    # True when the footprint has no courtyard and its pad bounding box plus a margin is used.
    courtyard_from_pads: bool
    # Rectangles (local coordinates) of the through-hole pads and holes (plated or not): the
    # larger of the copper and the drill. They pierce the board, so they block both sides.
    through_holes: list[Ring] = []
    # False when the board file has several outlines and this footprint's courtyard lies
    # entirely outside the chosen one (see board_io.read_board).
    on_board: bool = True


class BoardData(Frozen):
    path: str
    origin_x_nm: int
    origin_y_nm: int
    width_mm: float
    height_mm: float
    outline: Ring
    holes: list[Ring]
    footprints: list[FootprintData]

    def poses(self) -> dict[str, Pose]:
        return {fp.ref: fp.pose for fp in self.footprints}


def rotate(point: Point, degrees: float) -> Point:
    """Rotate about the origin the way KiCad rotates footprint children (y axis down)."""
    rad = math.radians(degrees)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    x, y = point
    return (x * cos_a + y * sin_a, y * cos_a - x * sin_a)


def to_board(pose: Pose, local: Point) -> Point:
    rx, ry = rotate(local, pose.rot)
    return (pose.x + rx, pose.y + ry)


def to_local(pose: Pose, board_point: Point) -> Point:
    return rotate((board_point[0] - pose.x, board_point[1] - pose.y), -pose.rot)
