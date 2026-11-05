# Amendment 3 to pre-registration 3: one pair shares its draws

Written on 2026-11-05, before any private case exists. Everything in
amendments 1 and 2 stands except the reference snippet, which this replaces.

## The pair

The fixture's final assertions require two entries to show the same image.
The `structural` entry (move 80) and the `former_unique` entry with move 80
must have equal recorded and live screenshot hashes. That is how the fixture
shows that only the structural locator differs between them. They must
therefore render from the same drawn parameters.

No other pair of entries has an equal-hash or same-image assertion. The only
hash comparisons in the fixture are this pair and the per-dispatch
`frame_sha256 == expected_frame_sha256` check inside one entry, which does not
link two entries.

## Sharing rule

The `structural` entry reuses the `former_unique` entry of the same `move`:
the same `x0`, `y0`, `labels`, `ocr_text`, `records` and `mismatch_record`.
It consumes **no** draws from the generator. Every other entry draws as in
amendment 2, in `CASES` order. `former_unique` (case-34) comes before
`structural` (case-36) in that order, so the reuse needs nothing drawn ahead.

## Reference snippet (replaces amendment 2's)

`FIRST` and `SECOND` are the pools in amendment 2.

```python
import hashlib, json, random

P3 = hashlib.sha256(open("PREREGISTRATION_3.md", "rb").read()).hexdigest()
SEED = int(hashlib.sha256((P3 + ":private-full-replay").encode()).hexdigest()[:16], 16)
SHARED = ("x0", "y0", "labels", "ocr_text", "records", "mismatch_record")

def drawn_cases(cases):            # cases = test_registered_controls.CASES
    rng = random.Random(SEED)
    out = []
    for case in cases:
        if case["group"] == "structural":
            twin = next(c for c in out
                        if c["group"] == "former_unique" and c["move"] == case["move"])
            out.append(dict(case, **{k: twin[k] for k in SHARED}))
            continue
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
print(hashlib.sha256(listing).hexdigest())
```

Run against the round 1 fixture's `cases()` (its file's sha256 begins
`27ec1f18e069d9d8`), this prints:

```
987ec9917e822f8a3686930579024a520cb9067e03ee68957f51712e9d844c9a
```

Producing that hash generates only the parameter list; no case has been
rendered or run. The hash is to be recorded with the run, and the list is
frozen from now on.
