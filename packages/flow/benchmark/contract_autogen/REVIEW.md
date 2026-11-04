# Adversarial review, 2026-10-29

A separate agent reviewed `openadapt_flow/generate/` and
`benchmark/stop_card/card.py` at 1de3c1021. It was given the code and the
safety claim, not the author's conclusions, and reproduced each confirmed
finding by running code. Every confirmed finding is fixed, with a regression
test, before the first blind demonstration was recorded.

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | High | A list told apart by `use` while `system` differs: a home phone stored as `system: email` read as right, because the allowance for `telecom.system` was unfiltered | refuse `use` when `system` still differs; refuse, never allow, an unexplained element of a told-apart list |
| 2 | High | Server facts weren't type-checked: `"false"` was truthy | require JSON booleans and a finite lag of zero or more |
| 3 | High | `teach.json` could carry a person's confirmation; a proposal of `[sex]` was admitted | confirmations come only from the command; a proposal needs a subject reference and a coded value |
| 4 | Medium | `compare: date` was chosen from how the values looked, so an extension string `2026-03-20 REVOKED…` read as right | date only for FHIR date elements (`date`, `*Date`, `*DateTime`) |
| 5 | High | Card: Undo and Fix ran with no concurrency check, overwriting a write made after the card was built | the action refuses if a record's `versionId` moved or anyone else wrote since the card was built; Undo and Fix are not offered when anyone else wrote in the window |
| 6 | High | Card: Undo dropped `meta.security`, and the read-back ignored it | keep security, tag, account, compartment and profile; compare them on read-back |
| 7 | Medium | Card: a failed read counted as a successful delete | only HTTP 404 or 410 confirms a delete |
| 8 | Medium | Card: Fix dropped the right element when two shared a path | Fix only when every extra element is in one place; otherwise Open |
| 9 | Low | The verifier's `casefold` makes `ſ` (long s) equal `s` in an email | not fixed here: it is in `ensure_record.py` on main, and it is a follow-up |

The remaining exposure is a read-then-write race in the card's version check.
A conditional write (`If-Match`) would close it. The card is a measurement
prototype, not shipped.
