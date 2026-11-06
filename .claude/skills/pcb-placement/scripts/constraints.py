"""The placement.yaml data model: the single owner of the constraint schema.

Units are millimetres and degrees. Coordinates use the board frame: origin at the top-left
corner of the Edge.Cuts bounding box, y growing downward as in KiCad.

Every section is optional; missing sections take the defaults below. Unknown keys are
rejected so that a typo never silently turns a constraint off.
"""

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Discriminator, Field, Tag, ValidationError

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


def _positive_size(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        raise ValueError("need x0 < x1 and y0 < y1")
    return rect


def _regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from exc
    return pattern


# Rectangle [x0, y0, x1, y1] in the board frame.
Region = Annotated[tuple[float, float, float, float], AfterValidator(_positive_size)]


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


_PAIR_TAG = "pair_rule"
_AUTO_TAG = "auto_rule"


def _decoupling_kind(value: object) -> str:
    """Pick the rule model up front, so a broken rule reports only its own errors."""
    if isinstance(value, DecouplingAuto) or (isinstance(value, dict) and "auto" in value):
        return _AUTO_TAG
    return _PAIR_TAG


DecouplingRule = Annotated[
    Annotated[DecouplingPair, Tag(_PAIR_TAG)] | Annotated[DecouplingAuto, Tag(_AUTO_TAG)],
    Discriminator(_decoupling_kind),
]


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
    decoupling: list[DecouplingRule] = []
    proximity: list[Proximity] = []
    separation: list[Separation] = []
    critical_nets: list[CriticalNet] = []
    weights: Weights = Weights()
    power_nets: list[Annotated[str, AfterValidator(_regex)]] = list(DEFAULT_POWER_NET_PATTERNS)


class Problem(StrictModel):
    """One thing wrong with placement.yaml, located by its field path (e.g. groups[0].members[2])."""

    path: str
    message: str
    level: Literal["error", "warning"] = "error"

    def line(self) -> str:
        return f"{self.level}: {self.path}: {self.message}"


class PlacementFileError(Exception):
    """placement.yaml is missing, not YAML, or does not match the schema.

    `problems` lists every schema error; it is empty when the file could not be read at all.
    """

    def __init__(self, message: str, problems: list[Problem] | None = None) -> None:
        self.problems: list[Problem] = problems or []
        details = "".join(f"\n  {p.line()}" for p in self.problems)
        super().__init__(message + details)


def _field_path(loc: tuple[int | str, ...]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part not in (_PAIR_TAG, _AUTO_TAG):
            path += f".{part}" if path else part
    return path or "(top level)"


def _schema_message(error_type: str, msg: str) -> str:
    if error_type == "extra_forbidden":
        return "unknown key"
    return msg.removeprefix("Value error, ")


def schema_problems(exc: ValidationError) -> list[Problem]:
    return [Problem(path=_field_path(err["loc"]), message=_schema_message(err["type"], err["msg"])) for err in exc.errors()]


def load_placement(path: Path) -> Placement:
    try:
        text = path.read_text()
    except OSError as exc:
        raise PlacementFileError(f"cannot read {path}: {exc}") from exc
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PlacementFileError(f"{path} is not valid YAML", [Problem(path="(yaml)", message=" ".join(str(exc).split()))]) from exc
    if data is None:
        data = {}
    try:
        return Placement.model_validate(data)
    except ValidationError as exc:
        raise PlacementFileError(f"{path} does not match the placement schema", schema_problems(exc)) from exc
