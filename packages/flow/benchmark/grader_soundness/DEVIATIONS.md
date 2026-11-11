# Deviations from PROTOCOL.md

Dated entries, in the order they happened.

1. **2026-11-11, shakedown before the first scored cell.** To check the
   harness, the runner was run on AgentDojo banking `user_task_3` and
   `user_task_11` and on AppWorld `22cc237_1`, into a scratch directory
   outside this repository. Those cells are not used as data. The protocol
   did not mention a shakedown. Nothing in the protocol, the family
   definitions or the task sample changed after it. One runner change
   followed it: `run_meta.json` now keeps the union of tasks across
   invocations on one ledger, so AppWorld can run one task per invocation
   and delete that task's seed dump afterwards to stay above the 30 GiB disk
   floor.
2. **2026-11-11, tau-bench not added.** The protocol named tau-bench (MIT)
   as an optional third benchmark. Its source was cloned to a scratch
   directory and its license checked (MIT), but installing and running code
   from that fresh clone was blocked by this session's permission policy.
   It was dropped before any cell ran. No tau-bench cell exists.
3. **2026-11-11, session interruption during the AppWorld run.** The
   agent session running the study was cut off by a transient API network
   error while the AppWorld run was about halfway through. The two
   background AppWorld streams were not affected: on resumption one had
   finished all 24 of its tasks and the other was at 22 of 24, and it then
   finished. Every one of the 48 per-task invocations exited 0, and no cell
   in any ledger ended in `HARNESS_ERROR`, `SCORER_ERROR` or
   `EFFECT_NOT_REPRODUCIBLE`, so nothing was restarted or rerun.
4. **2026-11-11, AppWorld kit reports removed.** AppWorld ran one task per
   invocation (entry 1), so the kit's `report.json` and `REPORT.md` in each
   shard described only the last task. They were deleted. The ledgers
   (`cells.jsonl`, `alternatives.jsonl`) are complete and `results.json` is
   built from them.
5. **2026-11-11, analyses added after seeing results (post hoc, labeled as
   such in RESULTS.md).** (a) A pooled false-accept rate without
   `late_write`, because on AppWorld that family is 48 of the 58 false
   accepts and it concerns when the claim was made, not the end state.
   (b) A manual reading of each `wrong_value` false accept that the
   pre-registered whole-word heuristic marked "named in the instruction",
   because some matches are a common word, not a specified value. The
   pre-registered numbers are reported first and unchanged.
6. **2026-11-11, review changes (after the first PR revision).**
   (a) **`late_write` taken out of the false-accept rate.** The protocol
   counts a wrong cell only when the end state differs from the reference.
   `late_write` is judged on the claim-time effect; its final end state is
   identical to the reference. The first revision pooled it anyway, which
   did not match the protocol's own rule and inflated AppWorld's pooled
   rate from 10 / 148 to 58 / 196. It is now reported as a separate,
   post hoc claim-timing row, with the note that a standard AppWorld
   episode ends at the claim.
   (b) **Strict AgentDojo figure added** (53 / 199), from the manual
   reading in entry 5, recorded in `results/wrong_value_review.json`.
   (c) **AppWorld aggregate only.** The redacted per-task AppWorld ledgers
   were removed from this repository: unsalted digests can be reversed by
   guessing and call sequences are partial solution derivatives. They are
   kept in a private repository. `results/appworld/aggregate.json` holds
   the counts and is what `summarize` reads when no ledger is present.
   (d) `results.json` moved to `results/results.json`, and
   `benchmark/grader_soundness/results` is excluded from the Flow wheel and
   sdist.
   (e) The draft post was moved out of this repository.
