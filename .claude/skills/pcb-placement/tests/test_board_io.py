"""board_io pieces that need no running KiCad: outlines and footprint messages."""

import pytest
from kipy.proto.board import board_types_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer
from shapely import Polygon

from board_io import BoardIOError, FootprintProto, choose_outline, footprint_data, nets_line, outer_faces
from board_model import Point

NM = 1_000_000


def rect(x0: float, y0: float, x1: float, y1: float) -> list[Point]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def two_boards() -> list[Polygon]:
    """Board A (10 x 10, one cutout) left of board B (10 x 10)."""
    return outer_faces([rect(0, 0, 10, 10), rect(4, 4, 6, 6), rect(20, 0, 30, 10)])


def test_outer_faces_drop_cutouts_and_sort_left_to_right() -> None:
    faces = two_boards()
    assert [f.bounds for f in faces] == [(0, 0, 10, 10), (20, 0, 30, 10)]
    assert len(faces[0].interiors) == 1
    with pytest.raises(BoardIOError, match="closed outline"):
        outer_faces([[(0.0, 0.0), (5.0, 0.0)]])


def test_choose_outline() -> None:
    faces = two_boards()
    assert choose_outline(faces[:1], None) is faces[0]
    assert choose_outline(faces, (25.0, 5.0)) is faces[1]
    # A point in board A's cutout still picks board A.
    assert choose_outline(faces, (5.0, 5.0)) is faces[0]
    with pytest.raises(BoardIOError) as missing:
        choose_outline(faces, None)
    message = str(missing.value)
    assert "2 separate outlines" in message and message.count("outline_at: [") == 2
    assert "x 20.00 to 30.00" in message
    with pytest.raises(BoardIOError, match="inside none"):
        choose_outline(faces, (15.0, 5.0))


def add_pad(fp: FootprintProto, number: str, kind: board_types_pb2.PadType.ValueType, x: float, size: float, drill: float, net: str, pin: str = "") -> None:
    pad = board_types_pb2.Pad(number=number, type=kind)
    pad.position.x_nm, pad.position.y_nm = round(x * NM), 20 * NM
    layer = pad.pad_stack.copper_layers.add()
    layer.size.x_nm = layer.size.y_nm = round(size * NM)
    pad.pad_stack.drill.diameter.x_nm = pad.pad_stack.drill.diameter.y_nm = round(drill * NM)
    pad.net.name = net
    pad.symbol_pin.name = pin
    fp.definition.items.add().Pack(pad)


def jack() -> FootprintProto:
    """J1 at (10, 20) on the bottom: a plated pad, an SMD pad and an unplated hole."""
    fp = FootprintProto()
    fp.position.x_nm, fp.position.y_nm = 10 * NM, 20 * NM
    fp.layer = BoardLayer.BL_B_Cu
    fp.reference_field.text.text.text = "J1"
    fp.value_field.text.text.text = "Jack"
    fp.definition.id.library_nickname = "Conn"
    fp.definition.id.entry_name = "Jack_3.5mm"
    add_pad(fp, "1", board_types_pb2.PadType.PT_PTH, 8.0, 1.6, 1.0, "/IN", pin="T")
    add_pad(fp, "2", board_types_pb2.PadType.PT_SMD, 10.0, 1.0, 0.0, "GND", pin="2")
    add_pad(fp, "", board_types_pb2.PadType.PT_NPTH, 12.0, 0.0, 1.2, "")
    return fp


def test_through_hole_pads_are_read_with_the_larger_of_copper_and_drill() -> None:
    data = footprint_data(jack(), (0.0, 0.0), None)
    assert data.side == "bottom" and data.on_board
    sizes = [(max(x for x, _ in ring) - min(x for x, _ in ring), max(y for _, y in ring) - min(y for _, y in ring)) for ring in data.through_holes]
    assert sizes == [pytest.approx((1.6, 1.6)), pytest.approx((1.2, 1.2))]
    centres = [(sum(x for x, _ in ring) / 4, sum(y for _, y in ring) / 4) for ring in data.through_holes]
    assert centres == [pytest.approx((-2.0, 0.0)), pytest.approx((2.0, 0.0))]


def test_footprint_outside_the_chosen_outline_is_off_board() -> None:
    assert footprint_data(jack(), (0.0, 0.0), Polygon(rect(0, 0, 30, 30))).on_board
    assert not footprint_data(jack(), (0.0, 0.0), Polygon(rect(40, 0, 60, 30))).on_board


def test_nets_line() -> None:
    assert nets_line(jack()) == "J1 Jack [Conn:Jack_3.5mm] bottom: 1(T)=/IN 2=GND =-"
