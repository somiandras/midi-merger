# placement.yaml field reference

`placement.yaml` holds the placement intent for one board. The schema lives in
`scripts/constraints.py`; `templates/placement.yaml` is a starter file.

**Units and frame:** millimetres and degrees. Origin at the top-left corner of the Edge.Cuts
bounding box, x to the right, y downward (as in KiCad). Rotation follows KiCad: positive is
counter-clockwise on screen.

Every section is optional. Unknown keys are errors, so a typo never silently turns a
constraint off.

## Sections

### `board`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `outline` | `edge_cuts` | `edge_cuts` | Outline source; only Edge.Cuts is supported |
| `grid_mm` | number > 0 | `0.25` | Placer position grid |
| `edge_clearance_mm` | number >= 0 | `0.5` | Minimum courtyard-to-outline gap (hard) |
| `sides` | list of `top`/`bottom`, at least one | `[top]` | Sides the placer may use: it moves parts already on these sides and never flips a part; parts on other sides stay put |

### `fixed` (hard)

Map of ref to pose. The placer never moves these. Parts locked in KiCad are treated the
same even when not listed.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `x`, `y` | number | required | Footprint anchor point (KiCad position), board frame |
| `rot` | number | none | Rotation in degrees; omitted means any rotation is accepted |
| `note` | text | none | Why it is fixed |

Example: `J1: {x: 0.0, y: 15.0, rot: 90, note: "USB-C flush to left edge"}`

A fixed part may also be a group member. It keeps its pose, still counts for the group's
region (it must lie inside it at the fixed pose, or every run reports a region violation),
and can be the group's anchor.

### `regions`

Map of name to rectangle `[x0, y0, x1, y1]` with `x0 < x1` and `y0 < y1`, inside the board
outline. Regions do nothing alone; a group's `region` makes its members stay inside.

Example: `power: [30, 0, 50, 15]`

### `groups`

List of parts placed together. A part may be in at most one group.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | text | required | Unique group name (also usable in `separation`) |
| `members` | list of refs, at least one | required | Parts in the group |
| `region` | region name | none | Hard: every member's courtyard fully inside this region |
| `anchor` | ref | none | Member that recipe offsets are relative to |
| `recipe` | text | none | Block recipe `reference/block-recipes/<recipe>.md` (see Block recipes below); needs `anchor` |
| `roles` | map role -> ref | `{}` | Maps recipe role names to members |

Example: `{name: buck_3v3, region: power, anchor: U2, recipe: buck, roles: {CIN: C5, L: L1}, members: [U2, L1, C5]}`

### `decoupling` (soft unless `hard: true`)

Two kinds of entry:

| Kind | Fields | Meaning |
| --- | --- | --- |
| Explicit pair | `cap`, `ic`, `pin`, `max_mm` (> 0), `hard` (default false) | `cap` within `max_mm` of `ic`'s `pin` |
| Automatic | `auto: true`, `max_mm` (> 0), `hard` (default false) | Every cap not listed explicitly, to its nearest IC supply pin |

`pin` is matched as a pad number first, then as a symbol pin name. The cap must share a net
with that pin. Examples: `{cap: C12, ic: U1, pin: "1", max_mm: 2.0}`, `{auto: true, max_mm: 3.0}`.

### `proximity` (soft unless `hard: true`)

`{a: ref, b: ref, max_mm: number >= 0, hard: false}`: courtyard gap between `a` and `b` at
most `max_mm`. Example: `{a: Y1, b: U1, max_mm: 5.0}`

### `separation` (soft unless `hard: true`)

`{a: name, b: name, min_mm: number > 0, hard: false}`: gap between the courtyards of the
members of `a` and of `b` at least `min_mm`. A name is a group (its members) or a region
(the members of every group assigned to it). A name used for both a group and a region is
an error. Example: `{a: power, b: analog, min_mm: 5.0, hard: true}`

### `critical_nets` (soft)

`{net: name, weight: number > 0}`: that net's wirelength counts `weight` times in the cost.
A power net listed here is included in wirelength although power nets are normally left
out. Net names as in KiCad, with the sheet path (`/USB_D+`). Example: `{net: /SW, weight: 10}`

### `weights` (soft)

| Field | Default | Multiplies |
| --- | --- | --- |
| `hpwl` | 1.0 | Wirelength (critical-net weighted) |
| `crossings` | 2.0 | Crossing count |
| `decoupling` | 20.0 | Sum of squared decoupling overshoots |
| `proximity` | 10.0 | Sum of squared proximity overshoots |
| `separation` | 10.0 | Sum of squared separation shortfalls |

All >= 0.

### `power_nets`

List of regular expressions for power and ground nets, which are left out of wirelength
and crossings (pours connect them). Setting it replaces the default list. Each pattern must
match the whole net name with the sheet path stripped (`/power/+3V3` is tested as `+3V3`),
ignoring case. Default: `[ADP]?GND[A-Z0-9_]*`, `VSS[A-Z0-9_]*`, `VCC[A-Z0-9_]*`,
`VDD[A-Z0-9_]*`, `VEE[A-Z0-9_]*`, `VBUS`, `VBAT`, `VSYS`, `VIN`, `[+-]?\d+(\.\d+)?V\d*`
(so `+3V3`, `5V`, `-12V`).

Ground nets are always `[ADP]?GND[A-Z0-9_]*` and `VSS[A-Z0-9_]*`, whatever `power_nets` says.

## What the metrics mean

Reported by `scripts/metrics.py`; all distances in mm.

| Metric | Hard or soft | Definition |
| --- | --- | --- |
| Courtyard | - | Closed shapes on F.CrtYd/B.CrtYd; lines that do not close give their convex hull; a footprint with no courtyard uses its pad bounding box plus 0.25 mm |
| `overlaps` | Hard | Two footprints on the same side whose courtyards intersect with area > 0.0001 mm². `depth_mm` = smaller side of the intersection's bounding box |
| `edge` | Hard | Courtyard closer than `edge_clearance_mm` to the outline (board holes included). `clearance_mm` < 0 means it reaches outside. Fixed and KiCad-locked parts are exempt |
| `region` | Hard | Group member's courtyard not fully inside its region. `outside_mm` = farthest courtyard corner from the region |
| `fixed_moved` | Hard | Fixed part more than 0.01 mm from its `x, y`, or more than 0.1° from its `rot` (compared modulo 360) |
| `decoupling` | Soft | Distance from the centre of the cap pad on the shared net to the centre of the IC pin |
| Automatic decoupling | Soft | Caps are refs `C<n>`, ICs are `U<n>` or `IC<n>`. Each unlisted cap pairs with the nearest IC pad sharing one of its supply nets (a power net that is not ground). Caps with no such net are skipped |
| `proximity` | Soft | Gap between the two courtyards (0 when they touch or overlap) |
| `separation` | Soft | Gap between the union of `a`'s member courtyards and the union of `b`'s |
| `hpwl_mm` | Soft | Per net: half the perimeter of the bounding box of its pad centres, summed. Power nets left out unless in `critical_nets` |
| `crossings` | Soft | Minimum spanning tree over each signal net's pad centres; each intersecting pair of tree segments from different nets counts once |
| `hard_violations` | - | Count of all hard items plus soft items from rules with `hard: true` (those stay in their soft list, flagged `hard`) |

Cost: `hpwl·Σ(HPWL_n·k_n) + crossings·X + decoupling·Σ(d−d_max)² + proximity·Σ(p−p_max)² + separation·Σ(s_min−s)² + 10⁶·H`,
where `k_n` is the critical-net weight (1 otherwise), `X` the crossing count and `H` the hard
violation count. Only rules that are violated add a squared term.

## What validate.py rejects

`scripts/validate.py BOARD placement.yaml [--socket URL] [--json]` lists every problem at
once, one line each: `error: <field path>: <what is wrong>`. Exit 0 valid, 1 invalid, 2 the
file or board cannot be read. Schema errors come first; the checks against the board run
only once the schema is valid. `metrics.py` refuses (exit 2) a file with any of these errors.

| Check | Field path example |
| --- | --- |
| Not YAML, unknown key, wrong type, value out of range, missing required field | `weights.typo`, `decoupling[0].max_mm` |
| Region with `x0 >= x1` or `y0 >= y1` | `regions.power` |
| `power_nets` entry that is not a valid regular expression | `power_nets[0]` |
| Ref not on the board (fixed, members, decoupling cap/ic, proximity) | `groups[1].members[2]` |
| IC has no pad number or pin name `pin`; cap shares no net with that pin | `decoupling[0].pin`, `decoupling[0].cap` |
| Part in two groups, or twice in one group | `groups[1].members[0]` |
| Group name used twice | `groups[2].name` |
| Group region not under `regions` | `groups[0].region` |
| Anchor or role ref not a member of its group | `groups[0].anchor`, `groups[0].roles.CIN` |
| Region reaching outside the board outline | `regions.power` |
| Fixed part whose courtyard, at its fixed pose, overlaps another fixed or KiCad-locked part | `fixed.J1` |
| Separation name that is neither a group nor a region, or both | `separation[0].b` |
| Critical net not on the board | `critical_nets[0].net` |
| Recipe with no file in `reference/block-recipes/` (a warning only while that folder does not exist), or a file that does not parse | `groups[0].recipe` |
| Group with a recipe but no `anchor`; role not in the recipe; recipe anchor role mapped to a ref other than `anchor` | `groups[0].anchor`, `groups[0].roles.X` |
| Recipe part relative to a pin its mapped part does not have | `groups[0].recipe` |
| Recipe role not mapped in `roles` (warning: the recipe does not place that part) | `groups[0].roles` |

`place.py` refuses (exit 2) a file with any of these errors, recipe errors included
(`metrics.py` does not read recipes).

## Block recipes

`reference/block-recipes/<name>.md` transcribes a datasheet's recommended layout as offsets
from the group's anchor. The file is free markdown with exactly one ```` ```yaml ```` fenced
block (model: `scripts/recipes.py`):

```yaml
anchor: U                                    # recipe role of the anchor part
parts:                                       # role -> where it goes
  CIN:  {rel_to: U.VIN, dx: 0.0, dy: 1.2, rot: 0}
  L:    {rel_to: U.SW,  dx: 2.5, dy: 0.0, rot: 90}
  COUT: {rel_to: L.2,   dx: 2.0, dy: 0.0, rot: 0}
bbox_mm: [8, 6]                              # optional block size, anchor at rotation 0
```

| Field | Type | Meaning |
| --- | --- | --- |
| `anchor` | text | Role name of the anchor; maps to the group's `anchor` ref |
| `parts.<ROLE>.rel_to` | `ROLE` or `ROLE.PIN` | Reference point: that role's footprint position, or its pin (pad number first, then symbol pin name; first matching pad) |
| `parts.<ROLE>.dx`, `dy` | number | Offset in mm from the reference point to the part's footprint position, written for the anchor at rotation 0 |
| `parts.<ROLE>.rot` | multiple of 90 | Part rotation relative to the anchor's rotation (default 0) |
| `bbox_mm` | `[w, h]`, both > 0 | Size of the block for macro placement; without it the group's size is its members' courtyard area x 1.6 |

The whole block turns with the anchor: a part goes to its reference point plus (dx, dy)
rotated by the anchor's rotation, at the anchor's rotation plus `rot`. `rel_to` may name the
anchor or another part, but not form a cycle. In `placement.yaml`, `groups[].roles` maps
every other role to a member (`roles: {CIN: C5, L: L1}`); the anchor role maps to
`groups[].anchor`. The placer puts each mapped part at its offset, then moves it to the
nearest legal spot if that one is taken or outside the region. Members without a role are
placed near the pins they connect to.
