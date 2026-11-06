# pair (test fixture)

An IC with a cap beside pin 1 and a resistor right of the cap.

```yaml
anchor: U
parts:
  C: {rel_to: U.1, dx: 0.0, dy: -2.0, rot: 90}
  R: {rel_to: C, dx: 3.0, dy: 0.0, rot: 0}
bbox_mm: [10, 8]
```
