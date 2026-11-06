"""Block recipes: a datasheet's recommended layout as offsets from an anchor part.

A recipe is a markdown file `reference/block-recipes/<name>.md` whose single ```yaml fenced
block holds:

    anchor: U                  # role of the anchor part (maps to groups[].anchor)
    parts:
      CIN: {rel_to: U.VIN, dx: 0.0, dy: 1.2, rot: 0}
      L:   {rel_to: U.SW,  dx: 2.5, dy: 0.0, rot: 90}
      COUT: {rel_to: L.2,  dx: 2.0, dy: 0.0, rot: 0}
    bbox_mm: [8, 6]            # optional: block size with the anchor at rotation 0

`rel_to` is a role, or ROLE.PIN where PIN is a pad number or, failing that, a symbol pin name
(the first matching pad is used). A part's position is the reference point plus (dx, dy)
rotated by the anchor's rotation; its rotation is the anchor's rotation plus `rot`. So the
offsets are written for the anchor at rotation 0 and the whole block turns with the anchor.
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from board_model import FootprintData, Point, Pose, rotate, to_board
from scoring import find_pins

_YAML_BLOCK = re.compile(r"^```ya?ml[ \t]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


class RecipeError(Exception):
    """A recipe file is missing, has no single yaml block, or does not match the recipe schema."""


class RecipePart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rel_to: str
    dx: float
    dy: float
    rot: float = 0.0

    @property
    def rel_role(self) -> str:
        return self.rel_to.split(".", 1)[0]

    @property
    def rel_pin(self) -> str | None:
        parts = self.rel_to.split(".", 1)
        return parts[1] if len(parts) == 2 else None


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor: str
    parts: dict[str, RecipePart] = Field(min_length=1)
    bbox_mm: tuple[float, float] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Recipe":
        if self.anchor in self.parts:
            raise ValueError(f"anchor role {self.anchor} must not also be listed under parts")
        roles = {self.anchor, *self.parts}
        for role, part in self.parts.items():
            if part.rel_role not in roles:
                raise ValueError(f"parts.{role}.rel_to: role {part.rel_role} is not defined")
            if part.rel_role == role:
                raise ValueError(f"parts.{role}.rel_to: a part cannot be relative to itself")
            if part.rot % 90.0 != 0.0:
                raise ValueError(f"parts.{role}.rot: must be a multiple of 90 degrees")
        if self.bbox_mm is not None and min(self.bbox_mm) <= 0:
            raise ValueError("bbox_mm: both sizes must be > 0")
        self.order()
        return self

    def order(self) -> list[str]:
        """Part roles so that each comes after the role it is relative to (anchor excluded)."""
        placed = {self.anchor}
        ordered: list[str] = []
        pending = sorted(self.parts)
        while pending:
            ready = [role for role in pending if self.parts[role].rel_role in placed]
            if not ready:
                raise ValueError(f"rel_to forms a cycle among {', '.join(pending)}")
            ordered += ready
            placed.update(ready)
            pending = [role for role in pending if role not in placed]
        return ordered


def load_recipe(path: Path) -> Recipe:
    try:
        text = path.read_text()
    except OSError as exc:
        raise RecipeError(f"cannot read {path}: {exc}") from exc
    blocks = _YAML_BLOCK.findall(text)
    if len(blocks) != 1:
        raise RecipeError(f"{path.name} needs exactly one ```yaml block, found {len(blocks)}")
    try:
        data: object = yaml.safe_load(blocks[0])
        return Recipe.model_validate(data)
    except yaml.YAMLError as exc:
        raise RecipeError(f"{path.name}: not valid YAML: {' '.join(str(exc).split())}") from exc
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'recipe'}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors())
        raise RecipeError(f"{path.name}: {details}") from exc


def reference_point(fp: FootprintData, pose: Pose, pin: str | None) -> Point | None:
    """Board position of `fp`'s pin at `pose`, or its anchor point when `pin` is None.

    None when the footprint has no such pin.
    """
    if pin is None:
        return (pose.x, pose.y)
    pads = find_pins(fp, pin)
    return to_board(pose, (pads[0].x, pads[0].y)) if pads else None


def part_pose(part: RecipePart, anchor_rot: float, ref_point: Point) -> Pose:
    """Pose of a recipe part, given the anchor's rotation and its `rel_to` point on the board."""
    dx, dy = rotate((part.dx, part.dy), anchor_rot)
    return Pose(x=ref_point[0] + dx, y=ref_point[1] + dy, rot=(anchor_rot + part.rot) % 360.0)
