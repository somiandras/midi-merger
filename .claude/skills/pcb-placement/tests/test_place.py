"""The placer on small synthetic boards (no KiCad needed)."""

import random
from pathlib import Path

import pytest

from board_model import BoardData, Pose
from constraints import FixedPart, Placement
from placer import Layout, place
from recipes import Recipe, RecipeError, load_recipe, part_pose
from scoring import score
from synthetic import board, part

RECIPES = Path(__file__).resolve().parent / "block-recipes"


def crowded_board() -> BoardData:
    """Ten resistors stacked on one spot over the edge, an IC, a fixed connector and a locked part."""
    resistors = [part(f"R{i}", 0.8, 0.8, pads=[("1", f"/S{i}", -0.5, 0), ("2", "/BUS", 0.5, 0)]) for i in range(1, 11)]
    return board(
        part("U1", 25, 20, w=6, h=6, pads=[("1", "+3V3", -2, -2), ("2", "GND", -2, 2), *[(str(i + 2), f"/S{i}", 2, -2 + 0.4 * i) for i in range(1, 11)]]),
        part("C1", 25, 20, pads=[("1", "+3V3", -0.5, 0), ("2", "GND", 0.5, 0)]),
        part("J1", 1.0, 20, w=4, h=6, pads=[("1", "/BUS", 0, 0)]),
        part("Q1", 40, 30, w=3, h=3, locked=True, pads=[("1", "/BUS", 0, 0)]),
        part("R99", 45, 5, side="bottom", pads=[("1", "/S1", 0, 0)]),
        *resistors,
    )


def crowded_placement() -> Placement:
    return Placement.model_validate(
        {
            "fixed": {"J1": {"x": 1.0, "y": 20}},
            "regions": {"left": [5, 5, 20, 35]},
            "groups": [{"name": "res", "region": "left", "members": [f"R{i}" for i in range(1, 6)]}],
            "decoupling": [{"auto": True, "max_mm": 2.0}],
        }
    )


def test_legaliser_removes_overlaps_and_respects_regions_and_edge() -> None:
    b, p = crowded_board(), crowded_placement()
    before = score(b, p)
    assert before.hard.overlaps and before.hard.edge and before.hard.region
    layout = Layout(b, p, {})
    layout.legalise_board()
    after = score(b, p, layout.poses)
    assert after.hard.overlaps == [] and after.hard.edge == [] and after.hard.region == []
    for ref in layout.movable:
        pose = layout.poses[ref]
        assert pose.rot in (0.0, 90.0, 180.0, 270.0)
        if pose != b.poses()[ref]:
            assert pose.x / p.board.grid_mm == pytest.approx(round(pose.x / p.board.grid_mm))
            assert pose.y / p.board.grid_mm == pytest.approx(round(pose.y / p.board.grid_mm))


def test_legal_parts_keep_their_exact_pose() -> None:
    b = board(part("R1", 10.03, 10.07, rot=45), part("R2", 20, 20))
    layout = Layout(b, Placement(), {})
    layout.legalise_board()
    assert layout.poses["R1"] == Pose(x=10.03, y=10.07, rot=45)


@pytest.mark.parametrize("from_scratch", [False, True])
def test_fixed_locked_and_other_side_parts_never_move(from_scratch: bool) -> None:
    b, p = crowded_board(), crowded_placement()
    out = place(b, p, {}, seed=3, iterations=400, budget_s=60, from_scratch=from_scratch)
    assert not {"J1", "Q1", "R99"} & set(out.poses)
    s = score(b, p, out.poses)
    assert s.hard.fixed_moved == []
    assert s.terms.hard_violations == 0
    assert out.unplaced == []


def test_fixed_part_off_its_entry_is_still_never_moved() -> None:
    b = board(part("J1", 10, 10), part("R1", 30, 30))
    p = Placement(fixed={"J1": FixedPart(x=12, y=10)})
    out = place(b, p, {}, seed=1, iterations=50, budget_s=60)
    assert set(out.poses) == {"R1"}


def test_same_seed_gives_identical_poses_and_other_seeds_differ() -> None:
    b, p = crowded_board(), crowded_placement()
    runs = [place(b, p, {}, seed=seed, iterations=300, budget_s=60, from_scratch=True) for seed in (7, 7, 8, 9)]
    assert runs[0].poses == runs[1].poses
    assert runs[0].cost == runs[1].cost
    assert not runs[0].budget_cap_hit
    assert any(r.poses != runs[0].poses for r in runs[2:])


def test_reported_cost_is_the_full_score_of_the_returned_poses() -> None:
    b, p = crowded_board(), crowded_placement()
    out = place(b, p, {}, seed=5, iterations=300, budget_s=60, from_scratch=True)
    assert out.cost == pytest.approx(score(b, p, out.poses).cost)
    assert out.cost < score(b, p).cost


def test_annealing_lowers_the_cost_of_the_constructed_start() -> None:
    b, p = crowded_board(), crowded_placement()
    out = place(b, p, {}, seed=2, iterations=600, budget_s=60, from_scratch=True)
    assert out.start == "constructed"
    assert out.cost <= out.start_cost


def test_budget_cap_is_reported() -> None:
    b, p = crowded_board(), crowded_placement()
    out = place(b, p, {}, seed=1, iterations=10_000, budget_s=0.0)
    assert out.budget_cap_hit
    assert out.iterations_done < out.iterations_planned


def test_recipe_file_parses() -> None:
    recipe = load_recipe(RECIPES / "pair.md")
    assert recipe.anchor == "U"
    assert recipe.order() == ["C", "R"]
    assert recipe.bbox_mm == (10, 8)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no block here\n", "exactly one"),
        ("```yaml\nanchor: U\nparts: {C: {rel_to: X.1, dx: 0, dy: 0}}\n```\n", "role X is not defined"),
        ("```yaml\nanchor: U\nparts: {A: {rel_to: B, dx: 0, dy: 0}, B: {rel_to: A, dx: 0, dy: 0}}\n```\n", "cycle"),
        ("```yaml\nanchor: U\nparts: {C: {rel_to: U, dx: 0, dy: 0, rot: 45}}\n```\n", "multiple of 90"),
        ("```yaml\nanchor: U\nparts: {C: {rel_to: U, dx: 0}}\n```\n", "dy"),
    ],
)
def test_broken_recipes_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "bad.md"
    path.write_text(text)
    with pytest.raises(RecipeError, match=message):
        load_recipe(path)


def test_recipe_offsets_turn_with_the_anchor() -> None:
    recipe = load_recipe(RECIPES / "pair.md")
    c = recipe.parts["C"]
    assert part_pose(c, 0.0, (10.0, 10.0)) == Pose(x=10.0, y=8.0, rot=90.0)
    # KiCad rotation (y down): (0, -2) turned by 90 degrees is (-2, 0).
    turned = part_pose(c, 90.0, (10.0, 10.0))
    assert (turned.x, turned.y, turned.rot) == pytest.approx((8.0, 10.0, 180.0))


def recipe_board() -> tuple[BoardData, Placement, dict[str, Recipe]]:
    b = board(
        part("U1", 20, 20, w=6, h=6, rot=90, pads=[("1", "+3V3", -2, -2), ("2", "GND", -2, 2)]),
        part("C1", 40, 30, pads=[("1", "+3V3", -0.5, 0), ("2", "GND", 0.5, 0)]),
        part("R1", 40, 10, pads=[("1", "/A", -0.5, 0), ("2", "/B", 0.5, 0)]),
    )
    p = Placement.model_validate(
        {
            "fixed": {"U1": {"x": 20, "y": 20, "rot": 90}},
            "groups": [{"name": "blk", "anchor": "U1", "recipe": "pair", "roles": {"C": "C1", "R": "R1"}, "members": ["U1", "C1", "R1"]}],
        }
    )
    return b, p, {"blk": load_recipe(RECIPES / "pair.md")}


def test_construction_applies_recipe_offsets_from_a_rotated_anchor() -> None:
    b, p, recipes = recipe_board()
    layout = Layout(b, p, recipes)
    layout.construct(random.Random(1))
    # Pin 1 of U1 at rotation 90 sits at (18, 22). C: (0, -2) turned 90 -> (-2, 0), rot 90 + 90.
    # R: relative to C, (3, 0) turned 90 -> (0, -3), rot 90 + 0.
    assert layout.poses["C1"] == Pose(x=16.0, y=22.0, rot=180.0)
    assert layout.poses["R1"] == Pose(x=16.0, y=19.0, rot=90.0)
