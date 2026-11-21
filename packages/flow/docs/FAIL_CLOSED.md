# Fail closed: the verdict kernel, the harness and the ratchet

A check fails open when it reports a pass that its evidence doesn't support.
Reviews of the November 2026 pull requests found about 20 blocking bugs of
this kind, and most were one of seven shapes. Fixing each one where it was
found didn't stop the next one, so the shapes are now fixed in one place:

- `openadapt_flow/verdict.py`, the kernel, folds evidence into a verdict once.
- `tests/failopen`, the harness, runs the same mutations against every
  function that makes a verdict.
- `scripts/check_verdict_folds.py`, the ratchet, fails CI when a file gains a
  verdict fold written by hand.

## The classes

| Class | Shape | Example (pull request) |
|---|---|---|
| C1 | The obligations come from what was checked, not from what was written: a written field with no expected value gets no verdict; a tally runs over the first 256 writes; a read stops before the cursor. | #2429, #2394, #2437 |
| C2 | An empty set passes: `all([])` is true, so no effects reads as all confirmed. | `verify_run._aggregate([])` before this change |
| C3 | A code nobody listed passes: a deny-list, a final `else: "CONFIRMED"`, a write with no undo counted as rolled back. | #2428 |
| C4 | A weak default or a dropped denominator: `min_scored=1`, runs left out of a rate, a signature labelled "this computer" whatever verified it. | #2419, #2394 |
| C5 | Close values treated as equal: `''` and NULL, a type-ahead prefix, `"250"` and `"$1,250.00"`, `1` and `"1"`. | #2258, #2448 |
| C6 | A URL a parser reads one way and a browser another: `///host`, `/\host`, userinfo, a redirect chain. | #2470, #2414 |
| C7 | An outcome that may have changed something is reported as a plain failure or as "nothing changed": a cancel after the child started, `RECONCILIATION_REQUIRED` mapped to `failed`. | #2300 |

## The rules

1. **Fix the obligations first.** `Obligations.fix(items, source=...)` takes
   the plan: what the run wrote, what the plan staged, the runs that were
   planned. It never takes what a check happened to return. It may be empty.
2. **Settle, don't fold.** `settle(obligations, results, limit=...)` puts each
   obligation in exactly one of held, refuted, not checked, missing, mismatched
   or conflicting, and lists results nobody asked for as unexpected. The
   settlement passes only when every obligation held and nothing else is
   present. An empty set never passes, and nothing is cut at the limit: more
   obligations than the limit sets `over_limit`.
3. **Only a complete observation holds.** `Result.held` needs an `Observed`
   value with `complete=True`. A read that found nothing is `Missing`, one that
   raised is `Errored`, and both give `NOT_CHECKED`. `observe(fn)` turns a probe
   into one of these and never returns a default. Writing
   `Result(subject, Status.HELD, ...)` directly raises `TypeError`.
4. **Codes are allow-listed.** `from_code(code, held=..., refuted=...)` maps any
   code not listed to `NOT_CHECKED`.
5. **Bind the result to the run.** A `Subject` names the thing checked
   (system, surface, table, key, field) and the run it belongs to (run id,
   nonce, digest). A result whose binding disagrees with the obligation is
   mismatched, so a result from another run or from a decoy table doesn't
   count.
6. **Say who signed only from a verified source.** `trust_label(source,
   verified=...)` names the signer only for a verified `TrustSource`; anything
   else is "not verified".
7. **Rates use the planned total.** `Settlement.rate(of)` divides by the
   number of obligations, never by the number that happened to be scored.
8. **Each site states its own failure order.** `Settlement.label(precedence=...)`
   returns `HELD` only when the settlement passes; otherwise it returns the
   first status in the site's order that applies. The kernel doesn't impose one
   total order, because that would silently relabel existing results.

## Register a site

Any function that turns evidence into a verdict is a site.

1. Build the verdict on `settle` (or `Result.from_code` for an upstream code).
2. Mark the function:

   ```python
   from openadapt_flow.verdict_sites import verdict_site

   @verdict_site("my_area.judge", classes="C1 C2")
   def judge(...): ...
   ```

   The decorator does nothing at run time beyond recording the site.
3. Add a spec under `tests/failopen/sites/` and `register(...)` it. A spec says
   how to call the site, what a pass is, and gives seeds (evidence with the
   correct answer). It also gives lenses that point at the parts of the
   evidence the mutations change: the per-obligation results, the plan, the
   required fields, the observed values, the run binding, a corpus and the
   outcome codes. Fill in the lenses that apply; each one switches on its
   mutations.

`test_registry_complete.py` fails when a decorated site has no spec, or a spec
names a function that isn't decorated. The ratchet doesn't count folds inside
a decorated function.

## The mutations and properties

| Mutation | What it does | Must |
|---|---|---|
| M1 | drop a result, keep the plan | not pass |
| M2 | grow to the limit plus one, the last result bad | not pass |
| M3 | no results, no plan | not pass (unless the spec says an empty set is vacuous, with the reason) |
| M4 | the probe raises `Exception`, `TimeoutError`, an HTTP 503 | not pass |
| M5 | a required field missing or null | not pass |
| M6 | reorder the results | change nothing |
| M7 | repeat a result; repeat it with a bad copy | not improve; not pass |
| M8 | a result bound to another run | not pass |
| M9 | sweep a knob; relabel a scored run as not scored | the default is the strictest value; not pass |
| M10 | perturb a value: prefix, superstring, `''`, null, absent, number for string | not pass |
| M11 | a corpus entry (`corpus/urls.json`, `corpus/values.json`) | not accepted |
| M12 | an outcome after delivery that may have changed something | not reported as a plain failure or as nothing changed |
| M14 | a code no list names | not pass |

On every seed the site must give the correct answer (P0), and a mutation that
only removes or corrupts evidence must never turn a not-pass into a pass (P1).
A crash counts as a not-pass. M13, a trust-source swap, waits for the signer
vectors (follow-up 5).

## The self-test

`tests/failopen/selftest/` holds the pre-fix code of historical bugs, copied
from this repository's history. The first line of each file names the commit
and the lines; the copied lines sit between `# fmt: off` and `# fmt: on`.
`test_selftest.py` requires the harness to flag every pre-fix copy and to pass
every post-fix copy:

| Bug | Pre-fix | Post-fix | Caught by |
|---|---|---|---|
| S1 receipt tally after the 256-line slice (#2394) | `adef5a431^` | `adef5a431` | M2 |
| S2 audit read ends at an old-stamped event before the cursor (#2437) | `de5ee3db2^` | `de5ee3db2` | M2, M7 |
| S3 a write with no undo counted as rolled back (#2428) | `fda539c78^` | `fda539c78` | M14 |
| S4 a written field with no expected value gets no verdict (#2429) | `fa26f69fc^` | `fa26f69fc` | M1 |
| S5 `min_scored=1`; an undelivered fault drops a run from the rates (#2419) | `275199914^` and `275199914` | open | M9 |
| S6 a type-ahead prefix matches an assumed typed baseline (#2448) | `c329c57dc^` | `c329c57dc` | P0, M11 |
| S7 a cancel after the child started is reported as failed (#2300) | `fd7133866` | #2300 head | M12 |
| S8 `''` and NULL in the governed check (#2258) | main | `00a55cc16` | P0, M10 |
| Wire tap relays `///host` raw (#2470) | main | #2470 head | M11 |
| S9 `verify_run` folds pass an empty effect list | main before this change | main | M1, M3 |

Three sites on main are still fail-open and are owned by open pull requests.
Their specs are `xfail(strict=True)` with the owner in the reason: when the fix
lands, the test passes unexpectedly and fails, so the marker has to come out
in the same change.

- `wire_tap.relay_redirect_location`: `///evil.example/x` is relayed raw (#2470).
- `governed_safety.check`: a held target note changed between `''` and NULL
  reads as no write (#2258).
- `connector.status_from_report`: a report whose `transaction_outcome` is
  `RECONCILIATION_REQUIRED` and whose `execution_outcome` is `FAILED` maps to
  `failed`; the cases come from `outcomes.json` (follow-up 3).

## The ratchet

`scripts/check_verdict_folds.py` runs in the required `ci` check, next to the
engine-generality check. It counts, per file in `openadapt_flow/`, the
launcher and `scripts/ci/`, six kinds of hand-written fold: a success literal
produced in code (`VERIFIED`, `CONFIRMED`, `held`, ...), a compare against an
outcome literal, `all()`/`any()` over verdicts or results, a broad `except`
that returns without a raise or a not-checked value, a slice in a statement
that counts, and a final `else` that assigns success. It fails only when a
file's count rises above `verdict_folds_baseline.json`. It never fails on a
drop, so open pull requests that don't add folds are unaffected. To add
verdict code, put it in a `@verdict_site` with a spec; the ratchet doesn't
count inside one. When a file is renamed, move its baseline entry in the same
change.

## Review rule

A C1 to C7 blocker is fixed by moving the fold onto `settle` and registering a
spec, not by a local patch. A reviewer who finds one asks for the spec.

## Follow-ups

1. Owners adopt: #2429, #2258, #2470 and #2300 replace their local patch with
   `settle` plus a spec and remove the strict xfail. The stacked pull requests
   (#2437, #2394, #2428, #2457, #2413, #2419, #2414, #2448) register their
   sites when they rebase.
2. One owner for `receipt_record`; the #2394 and #2437 stacks carry copies
   that have diverged.
3. A connector `unknown` status derived from `outcomes.json`
   `may_have_changed`, with a control-plane contract bump.
4. `confirms()` and `changed()` over `readback_values.compare_value`, keeping
   `''`, NULL and absent distinct.
5. Provenance and `signers.json` vectors shared by the Python and JS label
   renderers, then M13.
6. Strict defaults: `min_scored` is the planned total.
7. `safe_url` in Python and JS from the shared URL corpus.
8. `tx/lens.py`'s final `else: outcome = "CONFIRMED"` becomes an allow-list.
9. Move `verdict.py` to `openadapt_types` at the next types release.
10. A capability token for `Judgement` and receipt v2, verified by recompute.
11. A nightly real-app lane for the negative controls: a database killed
    mid-read, a short audit page, a decoy table, a cancel after delivery, a
    cross-origin redirect.
