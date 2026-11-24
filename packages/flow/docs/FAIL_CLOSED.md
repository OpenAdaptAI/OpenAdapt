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
   count. One result can't settle two obligations: if two claim it, both
   are conflicting. The kernel checks the binding a result carries, not
   where its value came from: the adapter that reads the store must
   establish that the observation belongs to this run before it builds the
   result. An earlier run's read tagged with this run's subject looks like a
   fresh one.
6. **Say who signed only from a verified source.** `trust_label(source,
   verified=...)` names the signer only for a verified `TrustSource`; anything
   else is "not verified".
7. **Rates use the planned total.** `Settlement.rate(of)` divides by the
   number of obligations, never by the number that happened to be scored.
8. **Each site states its own failure order.** `Settlement.label(precedence=...)`
   returns `HELD` only when the settlement passes; otherwise it returns the
   first status in the site's order that applies. The kernel doesn't impose one
   total order, because that would silently relabel existing results. The
   harness ranks REFUTED < NOT_CHECKED < HELD unless the spec says, with a
   reason, that an unread result outranks a refuted one (P6).
9. **"Nothing else changed" holds only on a complete read.**
   `Result.nothing_else(subject, Observed(count, complete=...))` takes the
   number of changes a read found outside what was allowed: one or more
   refutes, even on a partial read; zero holds only when the read was
   complete. A frame, a "no undeclared delete" or a rollback check is one of
   these, never `not changes` over whatever the read returned.

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
| M3 | no results and no plan (a vacuous spec keeps the plan) | not pass (unless the spec says an empty set is vacuous, with the reason) |
| M4 | the probe raises `Exception`, `TimeoutError`, an HTTP 503 | not pass |
| M5 | a required field missing or null | not pass |
| M6 | reorder the results | change nothing |
| M7 | repeat a result as a separate equal copy; repeat it with a bad copy | not raise the level; not pass |
| M8 | a result bound to another run | not pass |
| M9 | sweep a knob; relabel a scored run as not scored | the default is the strictest value; not pass |
| M10 | perturb a value: prefix, superstring, `''`, null, absent, number for string | not pass |
| M11 | a corpus entry (`corpus/urls.json`, `corpus/values.json`) | not accepted |
| M12 | an outcome after delivery that may have changed something | not reported as a plain failure or as nothing changed |
| M14 | a code no list names | not pass |
| M15 | a "nothing else changed" read marked partial | not pass |
| M16 | on a pass seed, the first result turned bad | not pass, not raise the level |

On every seed the site must give the correct answer (P0), and a mutation that
only removes or corrupts evidence must never turn a not-pass into a pass (P1).
A crash counts as a not-pass.

The verdict has three levels, not one bit (P6). A spec gives `level`, and the
harness ranks REFUTED < NOT_CHECKED < HELD. A mutation that keeps every
result and adds a copy or turns one bad (M7, M16) must not raise the level
at all. A reordering (M6) leaves the level
exactly where it was. A mutation that removes evidence (M1, M3, M4, M5, M8,
M10) may move REFUTED to NOT_CHECKED, because the refutation it removed is no
longer known, but never to HELD. A site whose order puts an unread result
below a refuted one says so in `precedence` with the reason in
`notes["precedence"]` (a test enforces the note). `verify_run`'s folds and
`receipt_record._line` do, and keep doing so by decision: when an effect may
have landed unseen, reconciliation beats an assumed refutation. A run with
one refuted effect and one unread effect is INDETERMINATE ("check the
record"), because the unread write may have happened; reporting it as
REFUTED would invite a retry or a plain failure over a change that may be in
the store (C7).

M13, a trust-source swap, waits for the signer vectors (follow-up 5).

## The self-test

`tests/failopen/selftest/` holds the pre-fix code of historical bugs, copied
from this repository's history. The first line of each file names the commit
and the lines; the copied lines sit between `# fmt: off` and `# fmt: on`.
`test_selftest.py` requires the harness to flag every pre-fix copy and to pass
every post-fix copy. A spec with no pass seed, or one that no mutation
applies to, fails too:

| Bug | Pre-fix | Post-fix | Caught by |
|---|---|---|---|
| S1 receipt tally after the 256-line slice (#2394) | `adef5a431^` | `adef5a431` | M2 |
| S2 audit read ends at an old-stamped event before the cursor (#2437) | `de5ee3db2^` | `de5ee3db2` | M2, M7 |
| S3 a write with no undo counted as rolled back (#2428) | `fda539c78^` | `fda539c78` | M14 |
| S4 a written field with no expected value gets no verdict (#2429) | `fa26f69fc^` | `fa26f69fc` | M1 |
| S5 `min_scored=1` passes 1 of 20 planned runs; an undelivered fault drops a run from the rates (#2419) | `275199914^` and `275199914` | open | P0, M9 (relabel, `^` only) |
| S6 a type-ahead prefix matches an assumed typed baseline (#2448) | `c329c57dc^` | `c329c57dc` | P0, M11 |
| S7 a cancel after the child started is reported as failed (#2300) | `fd7133866` | #2300 head | M12 |
| S3b the S3 fix still keeps a deny-list: a per-write code nobody listed is `ROLLED_BACK` | `fda539c78` | open | M14 |
| S8 `''` and NULL in the governed check (#2258) | main at `576f87b41` | `00a55cc16` | P0, M10 |
| Wire tap relays `///host` raw (#2470, merged; the live site is now registered) | main at `576f87b41` | #2470 head | M11 |
| S9 `verify_run` folds pass an empty effect list | main before this change | main | M1, M3 |

The pre-fix controls are pinned copies, never live code, so landing a fix on
main can't turn a control green.

Two sites on main are still fail-open and are owned by open pull requests.
Their specs are `owned`: they name the owner and list exactly the failures the
site has today. This is a strict xfail per case. A new failure fails the test,
so a known bug can't hide another one. A known failure that goes away also
fails it, so the list, and then the `owned` marker, comes out in the change
that fixes the site.

- `governed_safety.check`: a held target note changed between `''` and NULL
  reads as no write (#2258).
- `connector.status_from_report`: a report whose `transaction_outcome` is
  `RECONCILIATION_REQUIRED` and whose `execution_outcome` is `FAILED` maps to
  `failed`; the cases come from `outcomes.json` (follow-up 3).

## The ratchet

`scripts/check_verdict_folds.py` runs in the required `ci` check, in the smoke
job that runs on every change, next to the file-size ratchet. It counts, in
`openadapt_flow/`, the launcher and `scripts/ci/`, these kinds of hand-written
fold:

| Kind | What it counts |
|---|---|
| `says_success` | a success literal produced in code (`VERIFIED`, `CONFIRMED`, `held`, ...) |
| `cmp_literal` | a compare against an outcome literal |
| `bool_fold` | `all()`/`any()` over verdicts, results, checks, effects, lines or tiers |
| `swallow` | `except Exception` (or bare) with a `return`, `continue` or `pass` that hands back no not-checked value |
| `swallow_wide` | what `swallow` misses: a broad handler that falls through (`except Exception: logger.error(...)` and carry on), leaves by `break`, or names `Exception` in a tuple or by attribute |
| `slice_count` | a slice in a statement that counts |
| `else_success` | a final `else` that assigns success |
| `says_unchanged` | a "nothing changed" literal produced in code (`unchanged`, `no_change`, `ROLLED_BACK`, ...) |
| `empty_change_set` | `not changes`, `len(collateral) == 0`, `deleted == []`, `not any(diffs)`: a "nothing else changed" verdict computed from a set that may be a partial read |

A handler that re-raises, returns a not-checked value, or records one
(`status = "not_checked"`, `out.append(Errored(...))`) before falling through
is not a swallow. A file the check can't parse fails it, rather than counting
as a file with no folds.

The check fails only when the repository-wide total for a kind rises above
`verdict_folds_baseline.json`, and then lists the files whose count rose.
Totals decide, so moving code into a new module (as the decomposition plan
asks) is not a new fold. It never fails on a drop; run it with
`--update-baseline` to lower the baseline. The static count can't tell a
complete read from a partial one, so a site that says nothing else changed
also gets M15 in the harness.

It also fails when a `@verdict_site` has no spec. A spec's id is an `id=`
argument, or a string passed to the spec factory given to `register(...)`;
any other quoted word in a spec file doesn't count.

The baseline was measured again on this branch after merging main at
`9b6956be3`, because main had gained folds after the first measurement. The
method: `git archive origin/main` of `packages/flow/openadapt_flow`,
`openadapt`, `scripts/ci` and the outcome vocabulary into an empty folder;
`scan(load_config(), root=<that folder>)` there and on this branch with the
same detector and config; a per-kind comparison of the two totals; then the
branch's per-file counts written as the baseline. For every kind the branch's
count is at or below clean main's, so no kind's allowance rose above main's:

| Kind | Clean main | This branch |
|---|---|---|
| says_success | 92 | 89 |
| cmp_literal | 732 | 724 |
| bool_fold | 209 | 201 |
| swallow | 805 | 805 |
| swallow_wide | 377 | 377 |
| slice_count | 40 | 40 |
| else_success | 3 | 3 |
| says_unchanged | 32 | 30 |
| empty_change_set | 40 | 40 |

To add verdict code, put it in a `@verdict_site` with a spec; the ratchet
doesn't count inside one.

## Review rule

A C1 to C7 blocker is fixed by moving the fold onto `settle` and registering a
spec, not by a local patch. A reviewer who finds one asks for the spec.

## Follow-ups

1. Owners adopt: #2429, #2258 and #2300 replace their local patch with
   `settle` plus a spec and remove the strict xfail (#2470 has merged and its
   site is registered). The stacked pull requests (#2437, #2428, #2457,
   #2413, #2419, #2414, #2448) register their sites when they rebase.
2. `receipt_record`'s line judge and tally are on the kernel now (#2394 has
   merged); the #2437 stack rebases onto them.
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

## Migration checklist

The sites below fold evidence by hand and belong to one of the classes
above. Each moves onto `settle` (or `Result.nothing_else` for a "nothing else
changed" claim), gets `@verdict_site` and a spec, and keeps its behaviour
except where a fail-open is fixed, with a differential test against the old
copy. Tick a row in the pull request that moves it.

Done:

- [x] `hard_invariants.judge_hard_invariants` (C2 C5): three obligations;
  "no outside-scope change" and "no undeclared delete" on
  `Result.nothing_else`; a hand-written "held" no longer holds. Unchanged on
  every small input.
- [x] `runtime/effects/versions.judge_versions` (C1 C5): one obligation for
  reads succeeding and one per target. Fix: a duplicate, missing-history or
  no-new-version row no longer hides a version by someone else (refuted, not
  "not checked").
- [x] `verify_run._aggregate` and `_observed` (C1 C2): settled against the
  planned effects (`cfg.effects`), so a missing or unplanned verdict never
  passes. Fix: an observed code nobody listed is `unknown`, not `present`.
- [x] `receipt_record._line` (C1 C3): declared contract hashes are the
  obligations. Unchanged on every generated input.
- [x] `receipt_record._tally_lines` (C2 C3): fix: a line check nobody listed
  keeps the run from VERIFIED and counts as not checked.
- [x] `tx/wire_tap.relay_redirect_location` (C6): registered after #2470.
- [x] `verify_draft.judge_case` (C3 C5): admission cases settle on the verdict
  kernel via allow-listed outcome maps; untestable, error, or unknown codes
  never hold.

Next, most bug-prone first:

- [ ] `verify_run.judge_run_frame` and the frame merge in `verify_run` (C1 C2):
  the main "nothing else changed" verdict; status literals merged by hand.
- [ ] `runtime/effects/history.judge_initial_history` (C1 C2): a history
  frame over a read window.
- [ ] `verify_run.readback_class` (C1 C2): registered, still `all()`/`any()`.
- [ ] `scripts/ci/check_windows_governed_safety.check` (C5, owned by #2258).
- [ ] `connector.executor.status_from_report` (C7, owned; follow-up 3).
- [ ] `txplan.engine.undo_plan` (C3): the per-write deny-list (S3b).
- [ ] `tx/lens.py` final `else: outcome = "CONFIRMED"` (C3, follow-up 8).
- [ ] `txplan.audit.AuditReader.after` (C1, S2; #2437 stack).
- [ ] `form_tx.judge.SummaryJudge.evaluate` and
  `form_tx.reconciler._control_matches_fact` (C1 C5; #2429, #2448).
- [ ] `agent_test.gate.compare` (C4: `min_scored`; S5, follow-up 6).
- [ ] `receipt_record._approval` (C4): the weakest approver by `all()`.
- [ ] `public_seal._checks` (C1): `min(confirmed, required)` clamps instead of
  settling.
- [ ] `readback_values.compare_value` users `confirms()`/`changed()` (C5,
  follow-up 4).
- [ ] provenance and signer labels (C4, follow-up 5, then M13).
- [ ] `safe_url` in Python and JS (C6, follow-up 7).
