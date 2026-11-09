# Capability claims

One file per claim: `claims/<id>.yaml`, where `<id>` is the claim's `id`.
Adding a claim adds a file, so two pull requests that each add a claim don't
edit the same lines. `scripts/validate_claims.py` loads every `*.yaml` here,
enforces the contract below, and regenerates `docs/VERIFICATION.md` and
`docs/verification.json`:

```bash
python scripts/validate_claims.py --check --structure-only
python scripts/validate_claims.py --report
```

The report is a deterministic function of these files and the evidence they
cite. It has no timestamp, claims are grouped by tier and sorted by id, and it
has no counts, so two pull requests that add different claims usually merge
cleanly. `tests/test_validate_claims.py` fails when the committed report is
stale.

## Contract

```text
Every public capability claim about openadapt-flow ("web is supported today",
"Windows has scoped acceptance") must be a FUNCTION of automated
evidence, not a hand-written adjective that quietly rots. This registry maps
each claim -> an evidence `tier` -> the specific backing test(s)/benchmark(s)
that prove it. `scripts/validate_claims.py` enforces the tier<->evidence
contract in CI and regenerates `docs/VERIFICATION.md` from it.

Evidence strength is derived from the repo, not asserted here:
  * a test file with NO module-level env skipif, that exists      -> "supported"
    candidate evidence; its required `test` or `e2e-browser` job must bind the
    file to at least one passing case in that job's real JUnit result
  * a test file gated by a module-level `pytestmark` env skipif    -> "validating"
    (opt-in / infra-gated: grounded, but never runs on default CI)
  * a doc / benchmark artifact (.md or a benchmark dir)            -> "roadmap"
    (design/field evidence; cannot by itself prove a running capability)
A claim FAILS the gate when its `tier` OUTRANKS its strongest evidence
(rank order research < roadmap < validating < supported). Additionally a
claim carrying `reproducibility: field` may NOT be tier `supported` (a
result that is not CI-reproducible is never presented as "supported").

TIERS
  supported  — bound to a real passing result in required default CI.
  validating — grounded in a REAL opt-in/infra-gated proof or a field test;
               being validated (incl. with design partners). NOT "supported".
  roadmap    — designed/specified, not yet proven by a running test.
  research   — open question / study.

EVIDENCE ENTRY FIELDS
  path   — repo-relative path to a test file, benchmark dir, or doc. MUST exist.
  proves — what THIS artifact actually demonstrates (honest scope).
  kind   — optional: test | benchmark | doc. Inferred when omitted
           (tests/**/*.py -> test, *.md -> doc, else -> benchmark).
  node   — optional: a `def <name>` that must exist in `path` (rot guard).

The gate detects opt-in gating from each test's source (a module-level
`pytestmark` skipif on an env var) — the registry cannot lie about it.
```
