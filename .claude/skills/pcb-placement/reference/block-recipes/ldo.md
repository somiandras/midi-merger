# ldo: 3-pin LDO in SOT-23 with input and output caps

**Follows:** the usual LDO datasheet rule (for example Torex XC6206, Microchip MCP1700):
input and output caps as close to their pins as possible, each with a short path to GND.
Same as the LDO row of `placement-rules.md` (caps at their pins, at most 3 mm). Your
part's datasheet layout wins where it differs.

**Pinout this recipe assumes** (SOT-23-3, as XC6206, MCP1700): pad 1 = GND/VSS,
pad 2 = VOUT, pad 3 = VIN; pads 1 and 2 on one side of the body, pad 3 alone on the
other. Check the pad numbers against your part's datasheet and footprint before using it.
A different pinout or package (SOT-223, SOT-23-5, SOT-89) needs its own recipe file:
offsets relative to the wrong pad put the cap on the wrong pin.

**Layout, anchor at rotation 0:**

- `CIN` stands upright (rotated 90) just past pad 3 (VIN), on the side away from the body.
- `COUT` stands upright left of pads 1 and 2, centred between them, so one of its pads
  faces VOUT and the other faces GND.
- Bulk caps and anything else in the group are not part of the recipe; leave them as
  unmapped members and the placer puts them near the pins they connect to.

Offsets clear the SOT-23 courtyard (2.05 x 1.7 mm half sizes) for an 0402 or 0603 cap.

**Roles to map in `placement.yaml`:** anchor = the regulator, `CIN` = the input cap
(the smallest one if there are several), `COUT` = the output cap.
Example: `{name: ldo_3v3, anchor: U4, recipe: ldo, roles: {CIN: C3, COUT: C5}, members: [U4, C3, C4, C5, C6]}`

```yaml
anchor: U
parts:
  CIN:  {rel_to: U.3, dx: 1.9, dy: 0.0, rot: 90}
  COUT: {rel_to: U.2, dx: -1.9, dy: -0.95, rot: 90}
bbox_mm: [8, 4.5]
```
