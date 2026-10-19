# Runs set aside

`superseded.jsonl` keeps runs from blocks that were stopped because the save
gate had a bug that refused the screenshot-loop agent. None of them is in
`runs.jsonl` or the summary.

1. r1, every_turn ($0.028): Enter with focus inside OpenEMR's form iframe was
   read in the top page, so the gate saw no form around it. Fixed in
   `observe.focused_element` (follows the frame that holds focus).
2. r1, every_turn ($0.105): Enter on the State drop-down was treated as a
   save. Enter on a native drop-down picks an option and does not submit the
   form; fixed in `Harness._commit_target`.
3. r1, code ($0.056, correct, 12/12 fields): valid, but from the block stopped
   at (2); the block was run again from a reset fixture.
