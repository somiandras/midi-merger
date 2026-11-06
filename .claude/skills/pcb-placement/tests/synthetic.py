"""Small synthetic boards for tests (no KiCad needed)."""

from board_model import BoardData, FootprintData, PadData, Pose

PadSpec = tuple[str, str, float, float]


def part(
    ref: str,
    x: float,
    y: float,
    w: float = 2.0,
    h: float = 1.0,
    pads: list[PadSpec] | None = None,
    rot: float = 0.0,
    side: str = "top",
    locked: bool = False,
) -> FootprintData:
    """A footprint with a w x h courtyard centred on its position; pads are (number, net, x, y)."""
    ring = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2), (-w / 2, -h / 2)]
    return FootprintData(
        ref=ref,
        pose=Pose(x=x, y=y, rot=rot),
        side="bottom" if side == "bottom" else "top",
        locked=locked,
        pads=[PadData(number=n, pin_name="", net=net, x=px, y=py) for n, net, px, py in pads or []],
        courtyard=[ring],
        courtyard_from_pads=False,
    )


def board(*footprints: FootprintData, w: float = 50.0, h: float = 40.0, holes: list[list[tuple[float, float]]] | None = None) -> BoardData:
    return BoardData(
        path="test.kicad_pcb",
        origin_x_nm=0,
        origin_y_nm=0,
        width_mm=w,
        height_mm=h,
        outline=[(0, 0), (w, 0), (w, h), (0, h), (0, 0)],
        holes=holes or [],
        footprints=list(footprints),
    )
