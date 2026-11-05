# Pre-registration 3, public held-out suite: result

Product code: P `89f51575`, N `9809ecab2`. The N arms' `commit` field reads
`c9616912c`, which has the same product code; only documents changed after
`9809ecab2`. The seed is drawn from `PREREGISTRATION_3.md` (sha256
`af9b9c91…c776`). 39 controls in each of 5 conditions (195 targets).

| Condition (39 each) | P-off | P-on | N-off | N-on |
|---|---|---|---|---|
| unchanged | 39 right | 39 right | 39 right | 39 right |
| moved | 38 right, **1 wrong** | 35 right, 4 refused | 39 right | 35 right, 4 refused |
| dark | 29 right, **10 wrong** | 25 right, **1 wrong**, 13 refused | 39 right | 26 right, 13 refused |
| transfer_missing | 28 right, 5 refused, 6 missed | 28 right, 9 refused, 2 missed | 28 right, 5 refused, 6 missed | 28 right, 9 refused, 2 missed |
| transfer_none | same as above | same as above | same as above | same as above |

The regression suites on N-off and N-on had 0 wrong: drift-21, controls,
81 AVC444 frames and 1,428 live RDP targets.

## Against the bars

| Bar | Result |
|---|---|
| 0 wrong in N-off and N-on (held-out and regression) | Met: 0 |
| unchanged, N correct at least 98% | Met: 39/39 in both |
| moved, N-off at least 90%, N-on at least 85% | Met: 39/39 (100%), 35/39 (89.7%) |
| dark, N-off correct at least 80% | Met: 39/39 |
| transfer, N correct at least P's; N refused + missed at most P's | Met: equal (28 right; 11 refused or missed per condition) |
| Private full replay (run by its owners) | Pending |

The public suite meets every bar. A GO also needs the private full replay
under amendment 1.
