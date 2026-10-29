## OpenEMR referral intake (seed 7, 55 items, one app change: the save button relabeled at item 26)

| Arm | Items | Verified in the end | Automated (no hand entry) | Silent wrong | False stops | Median s/item | Model $/item | Operator min/item | Maintenance for the change (people min / model $) | Setup people min | Setup agent min / $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 3. OpenAdapt, Regulated + problems_only, durable, pipelined read-back, relabel repair (engine of measure/production-lifecycle; the API step is refused on this form, so the GUI save runs) | 55 | 53 | 41 | 0 | 9 of 35 | 118.5 | 0.000 | 2.15 | 7.5 / 0.00 | 52 | 13.3 / 0.00 |
| 3 on main. Same configuration, current main's engine [short run: 27 of 55 planned items; the ledger ends before the planned items (the run exited or was killed)] | 27 | 27 | 4 | 0 | 19 of 20 | 226.9 | 0.000 | 4.56 | 0.0 / 0.00 | 52 | 13.4 / 0.00 |
| 1. Agent with structured tools and REST read-back (Claude Sonnet 5.5) | 55 | 55 | 49 | 0 | 5 of 35 | 24.3 | 0.050 | 0.91 | 0.0 / 0.00 | 60 | 0.0 / 0.00 |
| 2. Agent-written Playwright + REST script with read-back and duplicate check, agent-maintained (Claude Sonnet 5.5) [short run: 16 of 55 planned items; hung: no progress for about 2 h after item 16 (process at 0% CPU, one Playwright driver idle); stopped by hand to release the shared fixture lease] | 16 | 16 | 7 | 0 | 7 of 12 | 5.3 | 0.000 | 2.44 | not reached | 70 | 2.6 / 0.86 |
| Reference: a person enters every item (cost model V1) | None | None | 0 | 0 | None of None | 210.0 | 0.000 | 3.50 | 0.0 / 0.00 | 0 | 0.0 / 0.00 |

### Totals (people time priced at the assumed rates; x0.5 and x2 in brackets)

| Arm | 10 items: total $ | people min | 100 items: total $ | people min | 1000 items: total $ | people min |
|---|---|---|---|---|---|---|
| openadapt-production-relabel | 72 [36-144] | 74 | 171 [86-340] | 270 | 1,158 [584-2,307] | 2,228 |
| openadapt-production | 84 [42-168] | 98 | 290 [146-579] | 508 | 2,351 [1,181-4,689] | 4,608 |
| agent_tools | 105 [53-210] | 69 | 151 [78-296] | 151 | 606 [329-1,161] | 969 |
| agent_script | 130 [65-259] | 94 | 239 [120-478] | 314 | 1,337 [669-2,672] | 2,508 |
| all_manual | 18 [9-35] | 35 | 175 [88-350] | 350 | 1,750 [875-3,500] | 3,500 |

## MockMed, fault set 1.1.0 (100 items, 10 UI changes, 5 server faults)

| Arm | Verified (of 90) | Silent wrong | False stops | Faults claimed saved (of 5) | Stopped when no save possible (of 5) | Control s/item (load) | Model $/item | Setup people min | Setup agent min / $ |
|---|---|---|---|---|---|---|---|---|---|
| 4. Computer-use agent, screenshots only (Claude Sonnet 5.5) | 90 | 5 | 0 | 5 | 5 | 22.5 (17) | 0.0386 | 30 | 0.0 / 0.000 |
| 1. Agent with structured tools and record read-back (Claude Sonnet 5.5) | 90 | 0 | 0 | 0 | 5 | 13.7 (15) | 0.0132 | 60 | 0.0 / 0.000 |
| 2. Agent-written Playwright script with read-back and duplicate guard, agent-maintained | 90 | 0 | 0 | 0 | 5 | 0.8 (6) | 0.0000 | 30 | 0.4 / 0.026 |
| 3. OpenAdapt admitted program, read-back on, no model calls | 71 | 0 | 19 | 0 | 5 | 7.3 (8) | 0.0000 | 52 | 0.0 / 0.000 |
| 3r. OpenAdapt program with self-repair (engine of 2026-10-19) | 62 | 0 | 28 | 0 | 5 | 7.4 (10) | 0.0000 | 52 | 0.0 / 0.000 |
| 4 + OpenAdapt read-back: the same agent runs, each claim gated on verify-run | 90 | 0 | 0 | 0 | 5 | 22.5 (17) | 0.0386 | 30 | 0.0 / 0.000 |

### Each UI change: excess people minutes (silent wrongs in the block)

| Change | cua_alone | agent_tools | agent_script | openadapt_program | openadapt_self_repair | cua_plus_openadapt_readback |
|---|---|---|---|---|---|---|
| renamed controls | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| display scaling | 0.0 | 0.0 | 0.0 | 0.0 | 49.0 +fix | 0.0 |
| moved controls | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| new interstitial | 0.0 | 0.0 | 10.0 | 52.0 +fix | 52.0 +fix | 0.0 |
| latency | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| reordered queue | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| renamed and moved New Encounter | 0.0 | 0.0 | 10.0 | 49.0 +fix | 49.0 +fix | 0.0 |
| look-alike referral | 0.0 | 0.0 | 10.0 | 0.0 | 0.0 | 0.0 |
| relabeled and reordered type | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| target referral gone | 10.0 | 10.0 | 40.0 | 10.0 | 10.0 | 10.0 |

### Totals (people time priced at the assumed rates; x0.5 and x2 in brackets)

| Arm | 10 items: total $ | people min | silent wrongs | 100 items: total $ | people min | silent wrongs | 1000 items: total $ | people min | silent wrongs |
|---|---|---|---|---|---|---|---|---|---|
| cua_alone | 51 [26-101] | 30 | 0.1 | 56 [30-108] | 34 | 1.0 | 110 [75-179] | 69 | 10.0 |
| agent_tools | 100 [50-201] | 60 | 0.0 | 104 [52-206] | 64 | 0.0 | 135 [74-256] | 102 | 0.0 |
| agent_script | 51 [25-101] | 31 | 0.0 | 56 [28-112] | 37 | 0.0 | 111 [56-222] | 96 | 0.0 |
| openadapt_program | 61 [31-123] | 53 | 0.0 | 65 [33-130] | 60 | 0.0 | 104 [53-206] | 134 | 0.0 |
| openadapt_self_repair | 61 [31-123] | 53 | 0.0 | 66 [33-132] | 62 | 0.0 | 110 [55-219] | 148 | 0.0 |
| cua_plus_openadapt_readback | 51 [26-101] | 30 | 0.0 | 56 [30-108] | 34 | 0.0 | 111 [76-182] | 72 | 0.0 |
