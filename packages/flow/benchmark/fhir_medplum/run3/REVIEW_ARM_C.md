# Bias review of arm C, before the freeze

Arm C, asynchronous verification, was added at the founder's request on
2026-10-30, before any run-3 item ran. On the same day an independent agent
reviewed its design for bias, for or against A, B or C. It reviewed the
draft at `8fa4efb66`:

- `PREREGISTRATION_3.md`;
- `run3/score.py`, `run3/run.py` and `costs_3.json`;
- the tests;
- the arm-A review.

It read no fault set, and it wasn't given the author's reasoning.

Its verdict: C was **not** fair to freeze until findings 1 to 3 were fixed.
All of the findings below are fixed in the commit that adds this file.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | Bias (blocker) | The secondary "C no worse than B" reading could never hold. C's people time is B's plus non-negative surcharges, and waiting, the thing C saves, had no price | Reframed as a trade fixed now: C's extra people seconds per item must be no more than B's median check time per item, which is the waiting C removes, priced as if a person attends the queue. Both sizes are published, and the text says that C vs B is settled on its three criteria before the run |
| 2 | Bias (blocker) | The text said every claim goes through at once and that a `not_done` means "a person is already involved". The code took only the verifier's word | One rule, stated and coded: a `done` claim goes through at once, and a `not_done` claim is held for the verdict. C's wall time includes the check on held items |
| 3 | Bias (blocker) | The dependence sensitivity used write-to-verdict time, which B shares and which is mostly the agent's own read-back | The sensitivity uses claim-to-verdict time, C's own delay. Write-to-verdict time is still reported, as exposure common to B and C |
| 4 | Should-fix | "Upper bound" was the wrong framing for detection time | Both clocks are reported. Claim-to-verdict is described as a lower bound, because this harness has no queue of checks. The text says why a background check can't settle before the claim |
| 5 | Should-fix | A missing clock was read as 0, and late stops without one dropped out of the count | Late stops are counted on their own. In the sensitivity, a late catch with no clock counts as over the threshold |
| 6 | Should-fix | A late false stop was charged V1's reconcile operator, which `costs_2.json` charges only when the state is wrong | Late stops on a right state add V1's interrupt (30 s), and on a wrong state its reconcile (120 s). The verdicts are also reported with the surcharge at 0 s and at 120 s |
| 7 | Note | The 330 s cap removes the late penalty at V1, and binds for cards above 210 s | Stated in the text |
| 8 | Note | A stop the verifier decided before the agent ran was charged as late | Not late: it is raised at once |
| 9 | Note | The wall-time saving is set by construction | Published as "C saves B's check time", not as measured throughput |
| 10 | Note | Reading "used first" as missed is the worst case | Labelled worst case. A middle case is added: caught, charged the card plus the 330 s cleanup |
| 11 | Note | The three comparisons are correlated | Stated in the text: one piece of evidence read three ways |
| 12 | Note | Stale wording | Fixed: the scorer's docstring, "three places", the `costs_3.json` note, "all three arms" |
| 13 | Note | The C paths had no tests | Tests added: the not-late intake stop, a missing clock, C's wall time, the late-stop count, the secondary reading holding, and C vs B not winning |
