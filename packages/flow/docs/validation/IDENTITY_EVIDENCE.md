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
