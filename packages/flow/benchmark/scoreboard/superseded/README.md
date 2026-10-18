# Runs set aside

`openemr_referral/code_agent/2026-10-18-a`: five arm-7 first runs, all refused
by the save gate at the first drop-down, before any save. A gate rule then
counted any `onchange` handler as "this drop-down submits the form", and
OpenEMR's Title drop-down has one. The rule now needs a handler that calls
`submit` (#1305). Nothing was written. The runs cost $0.04 (in
`raw/spend.json`) and are not in the scoreboard; arm 7's OpenEMR rows are run
`2026-10-18-b`.
