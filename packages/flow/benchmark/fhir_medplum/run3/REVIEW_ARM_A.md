# Fairness review of arm A, before the freeze

Run 3's arm A has to be as strong on Medplum as run 2's was on OpenEMR:

- the same agent, with the same model, effort, turns and budget;
- its own FHIR read-back and duplicate check;
- the same tools and prompt budget.

On 2026-10-30, an independent agent reviewed the draft harness at
`2b3324d02` against `PREREGISTRATION_2.md`, `incremental_value/arms.py` and
#1528's `agent_tools`. It was given the code only, not the author's
reasoning, and it didn't read the fault set.

Its verdict: A was **not** fair to freeze until finding 1 and findings 2 to 6
were fixed. All of them are fixed in `c402ddb47`, except finding 6, which the
pre-registration handles.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | Unfair (blocker) | B's patient and practitioner references came from the harness's ground truth. A gets only names and dates of birth, so on a look-alike or wrong-patient case B was told the answer | B's references come from a search by the same name and date of birth A gets, made before the agent starts, as an intake mapping would make them. If 0 or 2+ records match, that is B's stop. Run 2's rule ("from the referral, no truth from the harness") holds again |
| 2 | Should-fix | An agent that finished its work but never called `finish` counted as A's stop, with no nudge, while run 2's loop asked once | The agent is resumed once with run 2's words, "Call the finish tool to report the outcome." Whether it was nudged is logged per item |
| 3 | Should-fix | An API or SDK failure counted as A's stop | An SDK infrastructure error makes the item an error, which leaves both arms (run 2's rule). The agent running out of turns or budget is still its own outcome. The SDK result subtype is logged per run |
| 4 | Should-fix | The effort and budget options had never run on this stack | Checked on 3 items before the freeze. The smoke run is recorded in the pre-registration |
| 5 | Should-fix | The prompt dropped run 2's retry rule | Added: "If a write fails or times out, search before you try again, and never leave two records" |
| 6 | Should-fix | B's caught harms are charged from the stop card's modelled seconds (about 42 s), and A's at 420 s | The pre-registration says "modelled" and reports the verdict at B = 420 s alongside it, as the coordinator set |
| 7 | Note (helped A) | A restart gave A two budgets | The restart now shares the item's one $0.50, as in run 2 |
| 8 | Note | Truncated tool output had no marker | It now says how many characters were cut |
| 9 | Note | The tool sends no headers, so the agent has no conditional create | Kept at parity with run 2's agent, which had no equivalent. The pre-registration says so |
| 10, 11 | Note | Exclusions and the rest of the scoring are fair or lean against B | None needed |
| 12 | Note (B-side defect) | B's read token used the wrong variable name, and it was never refreshed | The driver sets the generated contracts' variables and refreshes the token before every read step |
