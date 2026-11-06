"""validate.check and the schema errors on deliberately broken placement files (no KiCad needed)."""

from pathlib import Path

import pytest

from board_model import BoardData
from constraints import FixedPart, Placement, PlacementFileError, Problem, load_placement
from synthetic import board, part
from validate import check, check_recipes

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "placement.yaml"

VALID = """\
fixed:
  J1: {x: 1.5, y: 20, rot: 0}
  H1: {x: 45, y: 5}
regions:
  core: [10, 10, 35, 30]
  io: [25, 5, 40, 20]
groups:
  - {name: mcu, region: core, anchor: U1, roles: {C: C1}, members: [U1, C1]}
  - {name: pull, region: io, members: [R1, R2]}
decoupling:
  - {cap: C1, ic: U1, pin: 1, max_mm: 2}
  - {auto: true, max_mm: 3}
proximity:
  - {a: R1, b: U1, max_mm: 5}
separation:
  - {a: mcu, b: io, min_mm: 1}
critical_nets:
  - {net: /SIG, weight: 2}
"""


def sample_board() -> BoardData:
    return board(
        part("U1", 20, 20, w=6, h=6, pads=[("1", "+3V3", -2, -2), ("2", "GND", -2, 2), ("3", "/IO", 2, 0)]),
        part("C1", 15, 18, pads=[("1", "+3V3", 0, 0), ("2", "GND", 1, 0)]),
        part("R1", 30, 10, pads=[("1", "/IO", 0, 0), ("2", "/SIG", 1, 0)]),
        part("R2", 30, 14, pads=[("1", "/SIG", 0, 0)]),
        part("J1", 1.5, 20, w=3, h=4),
        part("H1", 45, 5),
    )


def problems_for(text: str, tmp_path: Path, b: BoardData | None = None) -> list[Problem]:
    path = tmp_path / "placement.yaml"
    path.write_text(text)
    try:
        placement = load_placement(path)
    except PlacementFileError as exc:
        return exc.problems
    return check(b or sample_board(), placement)


def test_valid_file_has_no_problems(tmp_path: Path) -> None:
    assert problems_for(VALID, tmp_path) == []


@pytest.mark.parametrize(
    ("old", "new", "path"),
    [
        # Schema errors
        ("weight: 2}", "weight: 2, colour: red}", "critical_nets[0].colour"),
        ("pin: 1, max_mm: 2}", "pin: 1, max_mm: -1}", "decoupling[0].max_mm"),
        ("core: [10, 10, 35, 30]", "core: [35, 10, 10, 30]", "regions.core"),
        ("core: [10, 10, 35, 30]", "core: [10, 30, 35, 30]", "regions.core"),
        ("critical_nets:", "power_nets: ['[']\ncritical_nets:", "power_nets[0]"),
        ("b: io, min_mm: 1}", "b: io}", "separation[0].min_mm"),
        # Unknown references
        ("H1: {x: 45", "H9: {x: 45", "fixed.H9"),
        ("members: [R1, R2]", "members: [R1, R9]", "groups[1].members[1]"),
        ("cap: C1", "cap: C9", "decoupling[0].cap"),
        ("ic: U1", "ic: U9", "decoupling[0].ic"),
        ("pin: 1,", "pin: 7,", "decoupling[0].pin"),
        ("pin: 1,", "pin: 3,", "decoupling[0].cap"),
        ("a: R1, b: U1", "a: R1, b: U7", "proximity[0].b"),
        ("/SIG, weight", "/NOPE, weight", "critical_nets[0].net"),
        # Groups
        ("members: [R1, R2]", "members: [R1, R2, C1]", "groups[1].members[2]"),
        ("members: [R1, R2]", "members: [R1, R2, R1]", "groups[1].members[2]"),
        ("anchor: U1", "anchor: R1", "groups[0].anchor"),
        ("roles: {C: C1}", "roles: {C: R2}", "groups[0].roles.C"),
        ("region: io", "region: nowhere", "groups[1].region"),
        ("name: pull", "name: mcu", "groups[1].name"),
        # Separation names
        ("a: mcu, b: io", "a: mcu, b: nope", "separation[0].b"),
        ("name: pull", "name: io", "separation[0].b"),
        # Geometry
        ("io: [25, 5, 40, 20]", "io: [25, 5, 60, 20]", "regions.io"),
        ("H1: {x: 45, y: 5}", "H1: {x: 2, y: 20}", "fixed.H1"),
    ],
)
def test_each_broken_file_reports_its_field(tmp_path: Path, old: str, new: str, path: str) -> None:
    assert old in VALID
    problems = problems_for(VALID.replace(old, new, 1), tmp_path)
    assert [p.path for p in problems] == [path]
    assert all(p.level == "error" and p.message for p in problems)


def test_all_problems_are_reported_at_once(tmp_path: Path) -> None:
    text = VALID.replace("H1: {x: 45", "H9: {x: 45").replace("cap: C1", "cap: C9").replace("region: io", "region: nowhere")
    assert {p.path for p in problems_for(text, tmp_path)} == {"fixed.H9", "decoupling[0].cap", "groups[1].region"}

    schema = VALID.replace("weight: 2}", "weight: 0}").replace("pin: 1, max_mm: 2}", "pin: 1}")
    assert {p.path for p in problems_for("board_typo: 1\n" + schema, tmp_path)} == {
        "board_typo",
        "critical_nets[0].weight",
        "decoupling[0].max_mm",
    }


def test_yaml_syntax_error_is_one_problem(tmp_path: Path) -> None:
    problems = problems_for("fixed: [unclosed\n", tmp_path)
    assert [p.path for p in problems] == ["(yaml)"]
    assert "\n" not in problems[0].message


def test_fixed_part_overlapping_a_locked_part(tmp_path: Path) -> None:
    b = board(part("J1", 10, 10, w=3, h=3), part("J2", 12, 10, w=3, h=3, locked=True), part("R1", 10.5, 10))
    problems = check(b, Placement(fixed={"J1": FixedPart(x=10, y=10)}))
    assert [(p.path, "J2" in p.message) for p in problems] == [("fixed.J1", True)]


def test_fixed_part_checked_at_its_fixed_pose_not_its_board_pose() -> None:
    b = board(part("J1", 10, 10, w=3, h=3), part("J2", 30, 10, w=3, h=3))
    apart = Placement(fixed={"J1": FixedPart(x=10, y=10), "J2": FixedPart(x=30, y=10)})
    assert check(b, apart) == []
    together = Placement(fixed={"J1": FixedPart(x=10, y=10), "J2": FixedPart(x=11, y=10, rot=90)})
    assert [p.path for p in check(b, together)] == ["fixed.J1"]


def test_movable_list_refs_must_exist_and_not_be_fixed(tmp_path: Path) -> None:
    assert problems_for("movable: [R1, R2]\n" + VALID, tmp_path) == []
    problems = problems_for("movable: [R1, R9, J1]\n" + VALID, tmp_path)
    assert [(p.path, p.level) for p in problems] == [("movable[1]", "error"), ("movable[2]", "error")]
    assert "fixed" in problems[1].message


def test_movable_list_warns_about_parts_that_cannot_move() -> None:
    b = board(part("R1", 10, 10, locked=True), part("R2", 20, 10, side="bottom"), part("R3", 30, 10))
    problems = check(b, Placement(movable=["R1", "R2", "R3"]))
    assert [(p.path, p.level) for p in problems] == [("movable[0]", "warning"), ("movable[1]", "warning")]


def test_fixed_part_may_also_be_a_group_member(tmp_path: Path) -> None:
    text = VALID.replace("members: [U1, C1]", "members: [U1, C1, J1]")
    assert problems_for(text, tmp_path) == []


PAIR_RECIPE = Path(__file__).resolve().parent / "block-recipes" / "pair.md"


def recipe_problems(group: dict[str, object], recipe_dir: Path) -> list[tuple[str, str]]:
    p = Placement.model_validate({"groups": [group, {"name": "x", "members": ["R2"]}]})
    return [(q.path, q.level) for q in check_recipes(sample_board(), p, recipe_dir)]


def test_recipes(tmp_path: Path) -> None:
    group: dict[str, object] = {"name": "blk", "recipe": "pair", "anchor": "U1", "roles": {"C": "C1", "R": "R1"}, "members": ["U1", "C1", "R1"]}
    recipes = tmp_path / "block-recipes"
    assert recipe_problems(group, recipes) == [("groups[0].recipe", "warning")]
    recipes.mkdir()
    assert recipe_problems(group, recipes) == [("groups[0].recipe", "error")]
    (recipes / "pair.md").write_text("anchor: U\n")
    assert recipe_problems(group, recipes) == [("groups[0].recipe", "error")]
    (recipes / "pair.md").write_text(PAIR_RECIPE.read_text())
    assert recipe_problems(group, recipes) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"anchor": None}, [("groups[0].anchor", "error")]),
        ({"roles": {"C": "C1", "R": "R1", "X": "R1"}}, [("groups[0].roles.X", "error")]),
        ({"roles": {"C": "C1", "R": "R1", "U": "C1"}}, [("groups[0].roles.U", "error")]),
        ({"roles": {"C": "C1"}}, [("groups[0].roles", "warning")]),
        ({"anchor": "H1", "members": ["H1", "C1", "R1"]}, [("groups[0].recipe", "error")]),
    ],
)
def test_recipe_roles_must_fit_the_group(change: dict[str, object], expected: list[tuple[str, str]]) -> None:
    group: dict[str, object] = {"name": "blk", "recipe": "pair", "anchor": "U1", "roles": {"C": "C1", "R": "R1"}, "members": ["U1", "C1", "R1"]}
    assert recipe_problems({**group, **change}, PAIR_RECIPE.parent) == expected


def test_template_fits_the_schema_and_fails_only_on_example_names() -> None:
    placement = load_placement(TEMPLATE)
    problems = check(sample_board(), placement)
    assert problems
    assert all("EX_" in p.message for p in problems), [p.line() for p in problems]
