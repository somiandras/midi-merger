# crystal: crystal with two load caps

**Follows:** the usual crystal layout rule from MCU hardware guides (for example the
Raspberry Pi RP2040 hardware design guide): crystal and load caps close together and close
to the oscillator pins, short traces, each load cap straight to GND. Same as the crystal
row of `placement-rules.md` (at most 5 mm). The MCU's own guide wins where it differs.

**Works for** a 2-pad crystal and a 4-pad SMD crystal (3.2 x 2.5 mm and smaller) whose
two crystal pads sit at the left and right ends of the body at rotation 0. Offsets are from
the crystal's position, not from a pin, so no pin numbers are assumed. Check in the
footprint that the crystal pads really are at the two ends (on a 3225 4-pad part they are
pads 1 and 3, on opposite corners). A longer crystal (HC-49) needs larger `dx` values, or
the placer pushes the caps outward to the nearest free spot.

**Layout, crystal at rotation 0:** one cap stands upright (rotated 90) at each end of the
crystal, centred on the body's height, clear of a 3.2 x 2.5 mm crystal's courtyard (2.1 x
1.75 mm half sizes) for an 0402 or 0603 cap.

**Using it:** make the crystal its own group, anchored on the crystal, and keep it next to
the MCU with a `proximity` rule; a group has one recipe, and the MCU's group usually has
none. A series resistor on the crystal's output, if any, can be an unmapped member: the
placer puts it near the pins it connects to.

**Roles to map in `placement.yaml`:** anchor = the crystal, `C1` = load cap at the left
end, `C2` = load cap at the right end.
Example: `{name: xtal, anchor: Y1, recipe: crystal, roles: {C1: C19, C2: C20}, members: [Y1, C19, C20, R9]}`
plus `proximity: [{a: Y1, b: U5, max_mm: 3.0}]`.

```yaml
anchor: Y
parts:
  C1: {rel_to: Y, dx: -2.85, dy: 0.0, rot: 90}
  C2: {rel_to: Y, dx: 2.85, dy: 0.0, rot: 90}
bbox_mm: [7, 4]
```
