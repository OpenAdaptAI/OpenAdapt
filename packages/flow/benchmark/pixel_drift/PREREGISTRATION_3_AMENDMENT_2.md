# Amendment 2 to pre-registration 3: exact draw rules for the private fixture

Written on 2026-11-05, before any private case exists. It replaces the
"free parameters" sentence of amendment 1, which named draws the fixture does
not declare. Seed, arms, isolation, assertions and bars are unchanged.

## What varies and what does not

- **Fixed per cell (unchanged):** kind, group, pitch, move, retained,
  rows_down and mismatch, exactly as `cases()` in `test_registered_controls.py`
  defines the 44 cells. So every cell's criterion is the one registered.
- **Drawn per case:** the list origin and the label texts. Nothing else.

## Origin

`x0` is drawn from `range(60, 301, 10)` and `y0` from `range(80, 261, 10)`,
uniformly, with `rng.randrange`. Every literal position in `html()` moves by
`(x0 - 120, y0 - 120)`:

- control and label `left` (120 becomes `x0`, 220 becomes `x0 + 100`);
- `top` base (120 becomes `y0`);
- the identity table's margin: `margin:{y0-20}px 0 0 {x0-20}px` in place of
  `margin:100px`.

The deepest element sits at `y0 + 2*40 + 80 + 24 <= 444` and the widest
label ends before `x0 + 100 + 250 <= 650`, so everything stays inside the
800x600 viewport. Relative geometry (pitch, move, the 100 px label offset) is
unchanged, so each cell's criterion and the oracle mean the same thing.

## Labels

From two fixed pools:

```
FIRST  = ("Amber", "Birch", "Cedar", "Delta", "Ember", "Fable", "Garnet", "Harbor",
          "Indigo", "Juniper", "Kestrel", "Lagoon", "Meadow", "Nectar", "Orchard",
          "Pebble", "Quartz", "Raven", "Saffron", "Timber", "Velvet", "Willow",
          "Yarrow", "Zephyr")
SECOND = ("anchor", "beacon", "canyon", "dune", "estuary", "fjord", "glacier",
          "heron", "island", "jasper", "kelp", "lantern", "marble", "nimbus",
          "opal", "prairie", "quill", "ridge", "sierra", "thistle", "tundra",
          "valley", "walnut", "garden")
```

Per case, draw `firsts = rng.sample(FIRST, 4)` and then
`seconds = rng.sample(SECOND, 3)`:

- `LABELS[i]` becomes `f"{firsts[i]} {seconds[i]}"` for i = 0, 1, 2;
- the OCR button text `"Orchid action"` becomes `f"{firsts[0]} action"`;
- the identity names `"Record Orchid"`, `"Record Lantern"` and
  `"Record Cobalt"` become `f"Record {firsts[i]}"`;
- the mismatch name `"Record Meadow"` becomes `f"Record {firsts[3]}"`.

First words are distinct within a case, and so are second words, so no two
labels on one page read alike at `OCR_MIN_RATIO`.

## The oracle

The oracle is the document `pointerdown` readback of the button id
(`control-0` is correct). It does not depend on positions or label text, so
it stays valid.

## Reference: seed to case list

Run exactly this. Draws happen once, in `CASES` order (`case-00` to
`case-43`), and the same 44 drawn cases go to all four arms.

```python
import hashlib, json, random

P3 = hashlib.sha256(open("PREREGISTRATION_3.md", "rb").read()).hexdigest()
SEED = int(hashlib.sha256((P3 + ":private-full-replay").encode()).hexdigest()[:16], 16)

def drawn_cases(cases):            # cases = test_registered_controls.CASES
    rng = random.Random(SEED)
    out = []
    for case in cases:
        x0 = rng.randrange(60, 301, 10)
        y0 = rng.randrange(80, 261, 10)
        firsts = rng.sample(FIRST, 4)
        seconds = rng.sample(SECOND, 3)
        out.append(dict(case, x0=x0, y0=y0,
                        labels=[f"{f} {s}" for f, s in zip(firsts[:3], seconds)],
                        ocr_text=f"{firsts[0]} action",
                        records=[f"Record {f}" for f in firsts[:3]],
                        mismatch_record=f"Record {firsts[3]}"))
    return out

listing = json.dumps(drawn_cases(CASES), sort_keys=True).encode()
print(hashlib.sha256(listing).hexdigest())   # write this hash before any case runs
```

The printed hash of the case list is recorded before any case runs. The list
is then frozen: no case is dropped, added or rerun after its outcome is seen.
