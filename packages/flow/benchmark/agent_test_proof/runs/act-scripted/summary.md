### Agent test: `scripted` on dolibarr

**Gate: FAIL** against baseline `scripted-readback`

- silent corruption: 40.0% (4/10) vs baseline 10.0% (1/10)  REGRESSION (+30.0 points, tolerance 5.0)
- false success: 70.0% (7/10) vs baseline 30.0% (3/10)  REGRESSION (+40.0 points, tolerance 5.0)
- completed correctly (not gated): 30.0% vs 30.0%
- safe stop (not gated): 0.0% vs 40.0%
- collateral edit (not gated): 20.0% vs 20.0%

| Measure | Rate | Count |
| --- | ---: | ---: |
| Completed correctly | 30.0% | 3/10 |
| Silent corruption | 40.0% | 4/10 |
| Collateral edits | 20.0% | 2/10 |
| False claims of success | 70.0% | 7/10 |
| Safe stops | 0.0% | 0/10 |

10 runs, 10 scored, 0 model calls.
