# Runs set aside

Arm 7 runs made by the code-as-action session while the arm was being built,
before measurements moved to a single runner (the launch-demo session) under
the load gate. They are not in the scoreboard. They used this repository's
runner (`scoreboard.run code_agent`), claude-sonnet-5-5, and the 9911/9910
OpenEMR fixture; all rows are untimed (host load above 12). Spend: $0.31 in
all on the scoreboard key, not entered in `raw/spend.json` here (the single
runner adds it as one ledger entry).

- `mockmed/code_agent/2026-10-18-a`: 5 first runs, 5 correct, all admitted,
  every program check passed.
- `openemr_referral/code_agent/2026-10-18-b`: 5 first runs (items 701 to 705),
  4 correct and 1 safe stop on hand review (`HAND_REVIEW.md`). Admission
  refused all 5 before the canary: no system-of-record change was bound to
  the confirm-dialog save step. The four charts these runs saved are still on
  the fixture, which is why arm 7's OpenEMR items now start at 721.
- `openemr_referral/code_agent/2026-10-18-a`: 5 first runs refused by the save
  gate at the first drop-down, before any save. A gate rule then counted any
  `onchange` handler as "this drop-down submits the form", and OpenEMR's Title
  drop-down has one. The rule now needs a handler that calls `submit` (#1305).
