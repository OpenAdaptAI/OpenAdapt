# Hand review (PROTOCOL.md, "Outcome of an item")

- Item 2, rule outcome `silent_wrong`: the agent did not report success. Its
  final message (ledgers/item-002.json) says OpenEMR warned of a possible
  existing patient (another made-up "Adela", from arm 2's item 0, same first
  name and sex) and that it stopped without confirming; nothing was saved.
  By hand this is a `safe_stop`. The word rule missed it because the message
  says "did not create" and none of `run.FAILURE_WORDS`.
- Items 1 and 3 to 5: `correct` (one new chart, 12 of 12 fields).
- Admission, all five: refused before the canary, "a write step lacks an
  independent read-back". The program's save step (the confirm-dialog click)
  had no system-of-record change bound to it, so the JIT admission gate
  refuses. Arm 2's OpenEMR first run stopped earlier in admission (parameter
  names), so it never reached this check. The trace check passed on all five:
  every element the agent acted on is on the compiled program's steps.
