# usb-c: USB-C receptacle with its CC resistors

**Follows:** the USB Type-C rule for a sink (device): one 5.1 kOhm pull-down on each CC
pin, placed at the connector. Same as the USB-C rows of `placement-rules.md` (CC resistors
at most 5 mm from the connector; nothing but ESD protection between the connector and the
MCU on D+/D-).

**Pins this recipe assumes:** pad `A5` = CC1 and pad `B5` = CC2, the pad numbers the USB
Type-C specification gives and KiCad's USB-C receptacle footprints use (16-pin and 24-pin).
Check them against your footprint. It also assumes the footprint's pad row is on the -y
side of the connector at rotation 0 and the plug opening on the +y side, as in KiCad's
library footprints; the resistors go on the pad-row side, away from the opening.

**Layout, connector at rotation 0:** each resistor stands upright (rotated 90) beyond the
connector's courtyard, in line with its CC pad but shifted 1 mm outward, so the channel in
front of the D+/D- pads stays free for the data lines to run straight to the MCU.

**Not in this recipe:**

- D+/D- series resistors (for example the 27 Ohm pair on an RP2040). Datasheets usually
  want them at the MCU end (the RP2040 hardware design guide does); put them in the MCU's
  group or their own group near the MCU, not here.
- ESD protection: packages and pinouts vary too much. Add the ESD part as an unmapped
  member, with a `proximity` rule to the connector (at most 5 mm).

**Roles to map in `placement.yaml`:** anchor = the receptacle (usually also under
`fixed:`, which is allowed), `RCC1` = resistor on CC1, `RCC2` = resistor on CC2. With the
connector fixed or locked, the resistors go to their recipe spots and the annealer leaves
them there. Leave `region` off the group unless the region also holds the whole connector
courtyard.
Example: `{name: usb_cc, anchor: J3, recipe: usb-c, roles: {RCC1: R5, RCC2: R6}, members: [J3, R5, R6]}`

```yaml
anchor: J
parts:
  RCC1: {rel_to: J.A5, dx: -1.0, dy: -2.45, rot: 90}
  RCC2: {rel_to: J.B5, dx: 1.0, dy: -2.45, rot: 90}
bbox_mm: [11, 12]
```
