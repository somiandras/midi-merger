"""The placement.yaml data model: the single owner of the constraint schema.

Units are millimetres and degrees. Coordinates use the board frame: origin at the top-left
corner of the Edge.Cuts bounding box, y growing downward as in KiCad.

Every section is optional; missing sections take the defaults below. Unknown keys are
rejected so that a typo never silently turns a constraint off.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Side = Literal["top", "bottom"]

# Net-name patterns are regular expressions matched against the whole net name with its sheet
# path stripped ("/power/+3V3" -> "+3V3"), ignoring case.

# Grounds. A decoupling cap pairs with an IC on its other (supply) net, never on ground.
GROUND_NET_PATTERNS: tuple[str, ...] = (r"[ADP]?GND[A-Z0-9_]*", r"VSS[A-Z0-9_]*")

# Power and ground nets. Pours connect these, so they are left out of wirelength and
# crossings unless listed under critical_nets. `power_nets:` in placement.yaml replaces it.
DEFAULT_POWER_NET_PATTERNS: tuple[str, ...] = GROUND_NET_PATTERNS + (
    r"VCC[A-Z0-9_]*",
    r"VDD[A-Z0-9_]*",
    r"VEE[A-Z0-9_]*",
    r"VBUS",
    r"VBAT",
    r"VSYS",
    r"VIN",
    r"[+-]?\d+(\.\d+)?V\d*",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BoardSettings(StrictModel):
    outline: Literal["edge_cuts"] = "edge_cuts"
    grid_mm: float = Field(default=0.25, gt=0)
    edge_clearance_mm: float = Field(default=0.5, ge=0)
    sides: list[Side] = Field(default=["top"], min_length=1)


class FixedPart(StrictModel):
    """Footprint position (KiCad anchor point) and optional rotation that must not change."""

    x: float
    y: float
    rot: float | None = None
    note: str | None = None


# Rectangle [x0, y0, x1, y1] in the board frame.
Region = tuple[float, float, float, float]


class Group(StrictModel):
    name: str
    members: list[str] = Field(min_length=1)
    region: str | None = None
    anchor: str | None = None
    recipe: str | None = None
    roles: dict[str, str] = {}


class DecouplingPair(StrictModel):
    """Cap `cap` must sit within `max_mm` of pin `pin` of `ic`.

    `pin` matches a pad number first, then a symbol pin name. Distance is from the centre of
    the cap pad on the pin's net to the centre of the pin.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, coerce_numbers_to_str=True)

    cap: str
    ic: str
    pin: str
    max_mm: float = Field(gt=0)
    hard: bool = False


class DecouplingAuto(StrictModel):
    """Pair every cap not listed explicitly with the nearest IC pin on a shared supply net."""

    auto: Literal[True]
    max_mm: float = Field(gt=0)
    hard: bool = False


class Proximity(StrictModel):
    """Courtyard-to-courtyard gap between parts `a` and `b` must be at most `max_mm`."""

    a: str
    b: str
    max_mm: float = Field(ge=0)
    hard: bool = False


class Separation(StrictModel):
    """Gap between the courtyards of two regions' or groups' members must be at least `min_mm`."""

    a: str
    b: str
    min_mm: float = Field(gt=0)
    hard: bool = False


class CriticalNet(StrictModel):
    net: str
    weight: float = Field(gt=0)


class Weights(StrictModel):
    hpwl: float = Field(default=1.0, ge=0)
    crossings: float = Field(default=2.0, ge=0)
    decoupling: float = Field(default=20.0, ge=0)
    proximity: float = Field(default=10.0, ge=0)
    separation: float = Field(default=10.0, ge=0)


class Placement(StrictModel):
    board: BoardSettings = BoardSettings()
    fixed: dict[str, FixedPart] = {}
    regions: dict[str, Region] = {}
    groups: list[Group] = []
    decoupling: list[DecouplingPair | DecouplingAuto] = []
    proximity: list[Proximity] = []
    separation: list[Separation] = []
    critical_nets: list[CriticalNet] = []
    weights: Weights = Weights()
    power_nets: list[str] = list(DEFAULT_POWER_NET_PATTERNS)

    @field_validator("regions")
    @classmethod
    def _regions_have_positive_size(cls, value: dict[str, Region]) -> dict[str, Region]:
        for name, (x0, y0, x1, y1) in value.items():
            if x1 <= x0 or y1 <= y0:
                raise ValueError(f"region {name}: need x0 < x1 and y0 < y1")
        return value


class PlacementFileError(Exception):
    """placement.yaml is missing, not YAML, or does not match the schema."""


def load_placement(path: Path) -> Placement:
    try:
        text = path.read_text()
    except OSError as exc:
        raise PlacementFileError(f"cannot read {path}: {exc}") from exc
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PlacementFileError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    try:
        return Placement.model_validate(data)
    except ValidationError as exc:
        raise PlacementFileError(f"{path} does not match the placement schema:\n{exc}") from exc
