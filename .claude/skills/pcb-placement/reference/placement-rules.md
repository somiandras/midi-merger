# Placement rules

Load this when writing or reworking `placement.yaml`. Field syntax is in
`constraint-schema.md`. Most bad layouts come from placing in the wrong order, so follow the
order first and the per-block numbers second.

## Placement order

1. Mechanical and fixed parts: connectors, mounting holes, buttons, LEDs and displays,
   positioned from the enclosure.
2. Main ICs, rotated so their pin groups face the parts they connect to.
3. The power path in signal-flow order: input, protection, regulators, loads.
4. Critical loops: switching-regulator hot loop, crystal, USB and other differential pairs.
5. Decoupling: smallest value closest to the pin, on the same side as the IC.
6. Remaining passives, next to the pin they serve.
7. Test points and silkscreen last.

The placer does not follow this order by itself. The order decides what you write down:
steps 1 and 2 are what the human positions (`fixed:` or locked in KiCad), steps 3 and 4
become regions, groups and recipes, steps 5 and 6 become decoupling and proximity rules.

## Per-block defaults

Starting values. The part's datasheet layout always wins; say so when you use a default
without checking the datasheet.

| Block | Rule | Default |
| --- | --- | --- |
| Buck converter | Input cap across VIN and GND pins; smallest possible hot-loop area | ≤ 2 mm |
| Buck converter | Inductor next to SW pin; SW copper as small as possible | Adjacent |
| Buck converter | Feedback divider at FB pin, away from inductor and SW | ≥ 3 mm from SW |
| LDO | Input and output caps at their pins | ≤ 3 mm |
| MCU | One 100 nF per VDD pin; bulk cap per rail | ≤ 2 mm / ≤ 10 mm |
| Crystal | Crystal and load caps beside the oscillator pins | ≤ 5 mm |
| USB-C | ESD protection at the connector; CC resistors near the connector | ≤ 5 mm |
| USB-C | D+/D- path straight to the MCU, no part in between except ESD | Direct line |
| Analog front end | Away from switching nodes and inductors | ≥ 5 mm |
| Pull-ups, series resistors | At the driving pin | ≤ 5 mm |

## Turning the rules into placement.yaml

| Rule | Field | How |
| --- | --- | --- |
| Part placed by the enclosure (connector, hole, button, display) | `fixed:` | Its pose from the enclosure drawing or the user; or ask the user to lock it in KiCad |
| Blocks kept apart or in a known area (power, analog, MCU core) | `regions:` + `groups[].region` | One rectangle per area; every member's courtyard must fit inside (hard) |
| Parts of one functional block | `groups:` | One group per block; a part in at most one group |
| Datasheet layout of a block | `groups[].recipe` + `anchor` + `roles` | When a file in `block-recipes/` matches the part; check its pin assumptions first |
| Cap at an IC pin (decoupling, LDO and buck caps) | `decoupling:` | Explicit `{cap, ic, pin, max_mm}` for caps that matter; `{auto: true, max_mm: 3.0}` for the rest |
| Part next to another part (crystal to MCU, ESD to connector, pull-up to its pin's IC) | `proximity:` | `{a, b, max_mm}`; add `hard: true` only when the distance is a must |
| Keep-away (analog from switching, ≥ 3 mm FB divider from SW) | `separation:` | `{a, b, min_mm}` between two groups or regions; split the parts into their own group if needed |
| Short path matters (SW node, USB D+/D-, crystal lines) | `critical_nets:` | `{net, weight}`; weight 3 to 10 |
| "Direct line", "hot-loop area" | none | No field measures these; use a recipe, `critical_nets` and tight `proximity`, and check by eye at the human review |

Defaults to start from: `decoupling` max 2 mm for MCU 100 nF caps, 3 mm for LDO caps;
`proximity` 5 mm for crystal-to-MCU (3 mm if the board is dense), ESD-to-connector and
CC-resistor-to-connector; `separation` 5 mm analog-to-switching.

## Rules the metrics do not check

Same-side placement of decoupling caps, cap value order (smallest closest), ground return
paths and silkscreen are not measured. Mention them in the report for the human to check
in KiCad.
