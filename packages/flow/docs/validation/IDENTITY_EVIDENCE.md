# Identity verification evidence — bounded public summary

OpenAdapt's identity gate compares the recorded target context with fresh
runtime evidence before an armed action. The runtime mechanism, conservative
defaults, and boundary tests are public. The grown corpora, raw sweeps,
deployment-derived tuning, and reviewer probe recipes are private evaluation
artifacts and are not distributed with the engine.

## Aggregate evidence

The selected runtime operating point was measured on **6,900 synthetic string
pairs** plus **18 adversarial review probes**:

- measured false accepts: **0** in that bounded string-level set;
- false-abort rate: **48.31%** on same-entity pairs;
- justified abort rate: **100%** for deliberately indistinguishable pairs.

A separate rendered dense-surface exercise drove the repository's OCR path.
Before the pixel-identity hardening it measured **26 false accepts in 360
trials**; after the hardening it measured **0 in 360**. This is evidence for
that synthetic rendered task, not a guarantee for a real application.

## Known-ID list cross-check

A deployment can supply a complete list of the identifiers in the target
system (`runtime.known_ids`), taken from a confirmed export of the system of
record (see [LIMITS.md](../LIMITS.md)). An OCR abstain caused only by a
look-alike identifier is then lifted when exactly one listed identifier
falls in the look-alike class OCR read. The second read comes from a fresh
screenshot, not from an upscale of the first frame. Re-run on 2026-10-17
against the current matcher:

- rendered dense record list, real OCR: false stops on the correct row were
  **376 / 480** with no list, **204 / 480** with the list and two reads of
  the same frame (the earlier design), and **203 / 480** with the list and a
  fresh capture (the current design). False accepts on the sibling row were
  **0 / 480** in all three. In the 280 clicks whose sibling is not a
  look-alike twin, false stops went from 176 to 4 (same frame) and 3 (fresh
  capture). In the 200 clicks whose sibling's identifier differs only by a
  look-alike glyph, all 200 still stop, which is correct. The fixture renders
  deterministically, so its fresh captures are pixel-identical to the first;
  these numbers show the availability cost of the fresh capture, not the
  protection it adds on a live screen. See
  [`benchmark/known_ids/KNOWN_IDS.md`](../../benchmark/known_ids/KNOWN_IDS.md).
- private string corpora (one read per pair, so the two-read rule was
  relaxed to one; each pair's list holds the identifiers of the entities in
  that pair): same-entity false aborts went from 1,306 / 2,160 to
  755 / 2,160 (v1) and from 213 / 450 to 94 / 450 (v2). Verified
  different-entity pairs stayed 0 / 2,200 (v1), 0 / 1,590 (v2) and 0 / 300
  (v3), and indistinguishable pairs stayed 0 / 200 verified.

The current matcher's same-entity false-abort rates on those corpora (60.5%
for v1, 47.3% for v2) are higher than the published 43.6% to 49.3%, because
later fixes made the matcher more conservative.

## Interpretation

The high false-abort rate is intentional: when evidence cannot distinguish the
intended record from a plausible sibling, the runtime refuses rather than
guessing. Structured browser or accessibility evidence can avoid many OCR-only
ambiguities; pure-pixel environments pay the larger availability cost.

These measurements are not independent validation. The operating point was
selected using the same synthetic evidence on which it is reported, and the
history includes adversarial reviews that found new false-accept classes after
earlier zero measurements. Claims must therefore stay scoped to the named
corpora and rendered task. Deployment certification still requires evidence
for the actual workflow, application, identity surface, and effect oracle.

See [Source and evaluation boundary](../SOURCE_BOUNDARY.md) for the distinction
between public mechanisms and bounded summaries, already-public historical
reference material, and private successor corpora and tuning.
