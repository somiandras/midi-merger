"""Scoring on small synthetic boards (no KiCad needed)."""

import math
from pathlib import Path

import pytest

from board_model import BoardData, FootprintData, PadData, Pose, to_board, to_local
from constraints import (
    CriticalNet,
    DecouplingAuto,
    DecouplingPair,
    FixedPart,
    Group,
    Placement,
    PlacementFileError,
    Proximity,
    Separation,
    Weights,
    load_placement,
)
from metrics import exit_code, report
from scoring import HARD_PENALTY, ConstraintError, CostTerms, cost, score

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
    """A footprint with a w x h courtyard centred on its position."""
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


def test_rotation_follows_kicad_convention() -> None:
    # KiCad: positive angles turn counter-clockwise on screen, with y pointing down.
    pose = Pose(x=10, y=10, rot=90)
    assert to_board(pose, (1, 0)) == pytest.approx((10, 9))
    assert to_local(pose, to_board(pose, (0.3, -0.7))) == pytest.approx((0.3, -0.7))


def test_hpwl_sums_signal_nets_and_skips_power() -> None:
    b = board(
        part("R1", 10, 10, pads=[("1", "/SIG", 0, 0), ("2", "GND", 1, 0)]),
        part("R2", 20, 14, pads=[("1", "/SIG", 0, 0), ("2", "GND", 1, 0)]),
    )
    s = score(b, Placement())
    assert s.soft.hpwl_mm == pytest.approx(10 + 4)


def test_critical_net_adds_weight_and_includes_power_nets() -> None:
    b = board(
        part("R1", 10, 10, pads=[("1", "/SIG", 0, 0), ("2", "+3V3", 1, 0)]),
        part("R2", 20, 10, pads=[("1", "/SIG", 0, 0), ("2", "+3V3", 1, 0)]),
    )
    p = Placement(critical_nets=[CriticalNet(net="/SIG", weight=5), CriticalNet(net="+3V3", weight=2)])
    s = score(b, p)
    assert s.soft.hpwl_mm == pytest.approx(10 + 10)
    assert s.terms.weighted_hpwl == pytest.approx(10 * 5 + 10 * 2)
    assert {c.net: c.hpwl_mm for c in s.soft.critical_nets} == pytest.approx({"/SIG": 10, "+3V3": 10})


def test_power_net_patterns_can_be_overridden() -> None:
    b = board(
        part("R1", 10, 10, pads=[("1", "/SIG", 0, 0), ("2", "GND", 1, 0)]),
        part("R2", 20, 10, pads=[("1", "/SIG", 0, 0), ("2", "GND", 1, 0)]),
    )
    assert score(b, Placement(power_nets=[r"/?SIG"])).soft.hpwl_mm == pytest.approx(10)


def test_unknown_critical_net_is_an_input_error() -> None:
    with pytest.raises(ConstraintError):
        score(board(part("R1", 10, 10)), Placement(critical_nets=[CriticalNet(net="/NOPE", weight=2)]))


def test_crossings_count_intersecting_mst_segments_of_different_nets() -> None:
    crossing = board(
        part("A", 10, 10, pads=[("1", "/A", 0, 0)]),
        part("B", 20, 20, pads=[("1", "/A", 0, 0)]),
        part("C", 10, 20, pads=[("1", "/B", 0, 0)]),
        part("D", 20, 10, pads=[("1", "/B", 0, 0)]),
    )
    assert score(crossing, Placement()).soft.crossings == 1
    parallel = crossing.model_copy(
        update={
            "footprints": [
                part("A", 10, 10, pads=[("1", "/A", 0, 0)]),
                part("B", 20, 10, pads=[("1", "/A", 0, 0)]),
                part("C", 10, 20, pads=[("1", "/B", 0, 0)]),
                part("D", 20, 20, pads=[("1", "/B", 0, 0)]),
            ]
        }
    )
    assert score(parallel, Placement()).soft.crossings == 0


def test_crossings_use_a_spanning_tree_not_all_pairs() -> None:
    # Net /A has three collinear pads: the tree is two short segments, which /B crosses once.
    b = board(
        part("A1", 10, 10, pads=[("1", "/A", 0, 0)]),
        part("A2", 20, 10, pads=[("1", "/A", 0, 0)]),
        part("A3", 30, 10, pads=[("1", "/A", 0, 0)]),
        part("B1", 15, 5, pads=[("1", "/B", 0, 0)]),
        part("B2", 15, 15, pads=[("1", "/B", 0, 0)]),
    )
    assert score(b, Placement()).soft.crossings == 1


def test_overlap_depth_and_sides() -> None:
    b = board(part("C5", 10, 10, w=2, h=2), part("L1", 11.7, 10, w=2, h=2))
    s = score(b, Placement())
    assert [(o.a, o.b) for o in s.hard.overlaps] == [("C5", "L1")]
    assert s.hard.overlaps[0].depth_mm == pytest.approx(0.3)

    touching = board(part("C5", 10, 10, w=2, h=2), part("L1", 12, 10, w=2, h=2))
    assert score(touching, Placement()).hard.overlaps == []

    other_side = board(part("C5", 10, 10, w=2, h=2), part("L1", 11, 10, w=2, h=2, side="bottom"))
    assert score(other_side, Placement()).hard.overlaps == []


def test_edge_clearance() -> None:
    b = board(
        part("R1", 1.2, 10, w=2, h=1),  # left courtyard edge at 0.2 mm
        part("R2", 25, -0.5, w=2, h=2),  # reaches 1.5 mm past the top edge
        part("R3", 25, 20),  # well inside
    )
    s = score(b, Placement())
    edge = {v.ref: v for v in s.hard.edge}
    assert set(edge) == {"R1", "R2"}
    assert edge["R1"].clearance_mm == pytest.approx(0.2)
    assert edge["R2"].clearance_mm == pytest.approx(-1.5)


def test_edge_exempts_fixed_and_locked_parts() -> None:
    b = board(part("J1", 0.5, 10, w=3, h=2), part("J2", 0.5, 20, w=3, h=2, locked=True))
    p = Placement(fixed={"J1": FixedPart(x=0.5, y=10)})
    assert score(b, p).hard.edge == []


def test_edge_counts_board_holes() -> None:
    hole = [(24.0, 19.0), (26.0, 19.0), (26.0, 21.0), (24.0, 21.0), (24.0, 19.0)]
    b = board(part("R1", 25, 20, w=6, h=4), holes=[hole])
    assert [v.ref for v in score(b, Placement()).hard.edge] == ["R1"]


def test_region_containment() -> None:
    b = board(part("R4", 10, 10, w=2, h=2), part("R5", 3, 3, w=2, h=2))
    p = Placement(
        regions={"power": (0, 0, 10.2, 20)},
        groups=[Group(name="buck", region="power", members=["R4", "R5"])],
    )
    s = score(b, p)
    assert [(v.ref, v.region) for v in s.hard.region] == [("R4", "power")]
    assert s.hard.region[0].outside_mm == pytest.approx(0.8)


def test_region_must_be_defined() -> None:
    p = Placement(groups=[Group(name="g", region="nowhere", members=["R1"])])
    with pytest.raises(ConstraintError):
        score(board(part("R1", 10, 10)), p)


def test_fixed_moved_compares_position_and_rotation_modulo_360() -> None:
    b = board(part("J1", 5, 5, rot=-90), part("H1", 3, 3))
    p = Placement(fixed={"J1": FixedPart(x=5, y=5, rot=270), "H1": FixedPart(x=3, y=4)})
    s = score(b, p)
    assert [v.ref for v in s.hard.fixed_moved] == ["H1"]
    assert s.hard.fixed_moved[0].moved_mm == pytest.approx(1.0)


def test_fixed_part_must_exist() -> None:
    with pytest.raises(ConstraintError):
        score(board(part("R1", 10, 10)), Placement(fixed={"J9": FixedPart(x=0, y=0)}))


def ic_and_caps() -> BoardData:
    return board(
        part("U1", 20, 20, w=6, h=6, pads=[("1", "+3V3", -2, -2), ("2", "GND", -2, 2), ("3", "/IO", 2, 0)]),
        part("C12", 15, 18, pads=[("1", "+3V3", 0, 0), ("2", "GND", 1, 0)]),
        part("C13", 30, 30, pads=[("1", "+3V3", 0, 0), ("2", "GND", 1, 0)]),
    )


def test_decoupling_explicit_pair() -> None:
    p = Placement(decoupling=[DecouplingPair(cap="C12", ic="U1", pin="1", max_mm=2.0)])
    s = score(ic_and_caps(), p)
    assert len(s.soft.decoupling) == 1
    v = s.soft.decoupling[0]
    assert (v.cap, v.ic, v.pin) == ("C12", "U1", "1")
    assert v.dist_mm == pytest.approx(3.0)  # cap pad (15, 18) to pin (18, 18)


def test_decoupling_pin_matches_symbol_pin_name() -> None:
    b = ic_and_caps()
    u1 = b.footprints[0]
    named = u1.model_copy(update={"pads": [pad.model_copy(update={"pin_name": "VDD"}) if pad.number == "1" else pad for pad in u1.pads]})
    b = b.model_copy(update={"footprints": [named, *b.footprints[1:]]})
    p = Placement(decoupling=[DecouplingPair(cap="C12", ic="U1", pin="VDD", max_mm=5.0)])
    assert score(b, p).soft.decoupling == []


def test_decoupling_without_shared_net_is_an_input_error() -> None:
    p = Placement(decoupling=[DecouplingPair(cap="C12", ic="U1", pin="3", max_mm=2.0)])
    with pytest.raises(ConstraintError):
        score(ic_and_caps(), p)


def test_decoupling_auto_pairs_remaining_caps_on_supply_net() -> None:
    p = Placement(
        decoupling=[
            DecouplingPair(cap="C12", ic="U1", pin="1", max_mm=5.0),
            DecouplingAuto(auto=True, max_mm=3.0),
        ]
    )
    s = score(ic_and_caps(), p)
    assert [(v.cap, v.ic, v.pin) for v in s.soft.decoupling] == [("C13", "U1", "1")]
    assert s.soft.decoupling[0].dist_mm == pytest.approx(math.dist((30, 30), (18, 18)))


def test_proximity_uses_courtyard_gap() -> None:
    b = board(part("Y1", 10, 10, w=2, h=2), part("U1", 20, 10, w=2, h=2))
    p = Placement(proximity=[Proximity(a="Y1", b="U1", max_mm=5.0)])
    s = score(b, p)
    assert s.soft.proximity[0].dist_mm == pytest.approx(8.0)


def test_separation_between_groups_and_regions() -> None:
    b = board(part("L1", 10, 10, w=2, h=2), part("U5", 14, 10, w=2, h=2))
    p = Placement(
        regions={"power": (0, 0, 12, 40), "analog": (12, 0, 50, 40)},
        groups=[
            Group(name="buck", region="power", members=["L1"]),
            Group(name="afe", region="analog", members=["U5"]),
        ],
        separation=[Separation(a="power", b="analog", min_mm=5.0), Separation(a="buck", b="afe", min_mm=1.0)],
    )
    s = score(b, p)
    assert [(v.a, v.b) for v in s.soft.separation] == [("power", "analog")]
    assert s.soft.separation[0].dist_mm == pytest.approx(2.0)


def test_cost_formula() -> None:
    terms = CostTerms(weighted_hpwl=100, crossings=3, decoupling_sq=4, proximity_sq=2, separation_sq=1, hard_violations=2)
    w = Weights(hpwl=1, crossings=2, decoupling=20, proximity=10, separation=10)
    assert cost(terms, w) == pytest.approx(100 + 6 + 80 + 20 + 10 + 2 * HARD_PENALTY)


def test_score_cost_combines_terms_and_counts_hard_flagged_soft_rules() -> None:
    b = board(
        part("Y1", 10, 10, w=2, h=2, pads=[("1", "/X", 0, 0)]),
        part("U1", 20, 10, w=2, h=2, pads=[("1", "/X", 0, 0)]),
    )
    p = Placement(proximity=[Proximity(a="Y1", b="U1", max_mm=5.0, hard=True)])
    s = score(b, p)
    assert s.terms.hard_violations == 1
    assert s.cost == pytest.approx(10 * 1.0 + 10.0 * (8 - 5) ** 2 + HARD_PENALTY)


def test_poses_override_board_positions() -> None:
    b = board(
        part("R1", 10, 10, pads=[("1", "/SIG", 0, 0)]),
        part("R2", 20, 10, pads=[("1", "/SIG", 0, 0)]),
    )
    s = score(b, Placement(), {"R2": Pose(x=12, y=13, rot=90)})
    assert s.soft.hpwl_mm == pytest.approx(2 + 3)
    with pytest.raises(ConstraintError):
        score(b, Placement(), {"R9": Pose(x=0, y=0, rot=0)})


def test_exit_code() -> None:
    clean = board(part("R1", 10, 10), part("R2", 20, 10))
    assert exit_code(score(clean, Placement())) == 0
    overlapping = board(part("R1", 10, 10), part("R2", 10.5, 10))
    assert exit_code(score(overlapping, Placement())) == 1


def test_report_lists_ten_worst_unless_full() -> None:
    parts = [part(f"R{i}", 10 + i * 0.1, 10, w=2, h=2) for i in range(6)]
    s = score(board(*parts), Placement())
    assert len(s.hard.overlaps) > 10
    short = report(board(*parts), Placement(), s, None, full=False)
    full = report(board(*parts), Placement(), s, None, full=True)
    short_hard, full_hard, counts = short["hard"], full["hard"], short["counts"]
    assert isinstance(short_hard, dict) and isinstance(full_hard, dict) and isinstance(counts, dict)
    short_overlaps, full_overlaps = short_hard["overlaps"], full_hard["overlaps"]
    assert isinstance(short_overlaps, list) and isinstance(full_overlaps, list)
    assert len(short_overlaps) == 10
    assert len(full_overlaps) == counts["overlaps"] == len(s.hard.overlaps)
    first = short_overlaps[0]
    assert isinstance(first, dict)
    assert first["depth_mm"] == pytest.approx(max(o.depth_mm for o in s.hard.overlaps))


def test_load_placement_defaults_and_list_regions(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    p = load_placement(empty)
    assert p.board.edge_clearance_mm == 0.5
    assert p.weights.decoupling == 20.0

    minimal = tmp_path / "placement.yaml"
    minimal.write_text(
        "regions:\n  power: [30, 0, 50, 15]\n"
        "decoupling:\n  - {cap: C12, ic: U1, pin: 1, max_mm: 2.0}\n  - {auto: true, max_mm: 3.0}\n"
    )
    p = load_placement(minimal)
    assert p.regions["power"] == (30, 0, 50, 15)
    first = p.decoupling[0]
    assert isinstance(first, DecouplingPair) and first.pin == "1"
    assert isinstance(p.decoupling[1], DecouplingAuto)


@pytest.mark.parametrize(
    "text",
    [
        "weights: {hpwl: 1, typo: 2}\n",
        "regions:\n  bad: [10, 0, 5, 15]\n",
        "board: [not, a, mapping]\n",
        "fixed: {J1: {x: 0}}\n",
        ": : :\n",
    ],
)
def test_load_placement_rejects_bad_files(tmp_path: Path, text: str) -> None:
    path = tmp_path / "placement.yaml"
    path.write_text(text)
    with pytest.raises(PlacementFileError):
        load_placement(path)


def test_load_placement_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PlacementFileError):
        load_placement(tmp_path / "nope.yaml")
