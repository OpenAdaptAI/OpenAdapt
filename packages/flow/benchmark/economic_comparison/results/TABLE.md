## MockMed, fault set 1.1.0 (100 items, 10 UI changes, 5 server faults)

| Arm | Verified (of 90) | Silent wrong | False stops | Faults claimed saved (of 5) | Stopped when no save possible (of 5) | Control s/item (load) | Model $/item | Setup people min | Setup agent min / $ |
|---|---|---|---|---|---|---|---|---|---|
| 4. Computer-use agent, screenshots only (Claude Sonnet 5.5) | 90 | 5 | 0 | 5 | 5 | 22.5 (17) | 0.0386 | 30 | 0.0 / 0.000 |
| 1. Agent with structured tools and record read-back (Claude Sonnet 5.5) | 90 | 0 | 0 | 0 | 5 | 12.1 (150) | 0.0132 | 60 | 0.0 / 0.000 |
| 2. Agent-written Playwright script with read-back and duplicate guard, agent-maintained | 90 | 0 | 0 | 0 | 5 | 0.9 (77) | 0.0000 | 30 | 0.4 / 0.026 |
| 3. OpenAdapt admitted program, read-back on, no model calls | 71 | 0 | 19 | 0 | 5 | 26.7 (239) | 0.0000 | 52 | 0.0 / 0.000 |
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
