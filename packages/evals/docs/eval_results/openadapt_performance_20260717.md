# OpenAdapt performance evidence — 2026-07-17

This report separates retained model-free compiler/runtime evidence from historical demo-conditioned agent evidence.

## Retained model-free compiler/runtime evidence: compiled versus API

| Application | Arm | Condition | Runs | Success | Silent incorrect | Over-halt | Mean | p50 | p95 | Model calls | Cost |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| frappe_lending | compiled | baseline | 3 | 3/3 | 0 | 0 | 20.82s | 20.81s | 21.76s | 0 | $0.00 |
| frappe_lending | compiled | ui_cosmetic_v1 | 3 | 3/3 | 0 | 0 | 34.71s | 39.10s | 46.72s | 0 | $0.00 |
| frappe_lending | api | baseline | 3 | 3/3 | 0 | 0 | 0.98s | 1.00s | 1.03s | 0 | $0.00 |
| frappe_lending | api | ui_cosmetic_v1 | 3 | 3/3 | 0 | 0 | 0.98s | 0.95s | 1.05s | 0 | $0.00 |
| openemr_local | compiled | baseline | 3 | 3/3 | 0 | 0 | 36.32s | 35.97s | 37.26s | 0 | $0.00 |
| openemr_local | compiled | ui_cosmetic_v1 | 3 | 3/3 | 0 | 0 | 37.26s | 37.17s | 37.48s | 0 | $0.00 |
| openemr_local | api | baseline | 3 | 3/3 | 0 | 0 | 2.27s | 2.26s | 2.34s | 0 | $0.00 |
| openemr_local | api | ui_cosmetic_v1 | 3 | 3/3 | 0 | 0 | 2.30s | 2.30s | 2.35s | 0 | $0.00 |

Compiled execution passed 12/12 and averaged 32.28s; API control passed 12/12 and averaged 1.63s. The compiled GUI path was 19.8× slower. Both had zero measured silent incorrect success, over-halt, model calls, and model cost.

This is a complete model-free subset, not a complete comparative matrix: the paid/zero-shot agent arm was intentionally omitted, and both source artifacts mark `publication_ready=false`.
The summaries came from a worktree later committed as `84c7a94`, but they do not embed a runtime revision. This is a post-hoc association, not exact source binding; exact current Flow 1.12.1 requalification remains pending.

The current benchmark's silent-incorrect and over-halt counters are retained from its source rows. Historical counters are derived from the agent's `done` claim and the WAA oracle. p95 uses nearest rank.

## Historical legacy agent: DC versus zero-shot

| Task ID | Retained instruction |
|---|---|
| `04d9aeaf` | In a new sheet with 4 headers "Year", "CA changes", "FA changes", and "OA changes", calculate the annual changes for the Current Assets, Fixed Assets, and Other Assets columns. Set the results as percentage type. |
| `0bf05a7d` | I would like to pad all the numbers in the "Old ID" column with zeros in front, to fill them up to seven digits in the "New 7 Digit ID" column. |
| `0e763496` | Change the font to "Times New Roman" throughout the text. |
| `70745df8` | Can you delay VS Code autoSave for 1000 milliseconds? |

| Condition | Runs | Success | Silent incorrect | Over-halt | Mean | p50 | p95 | Mean steps |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ZS | 12 | 3/12 | 5 | 0 | 84.37s | 78.47s | 132.04s | 9.75 |
| DC | 12 | 3/12 | 6 | 0 | 71.37s | 64.68s | 116.80s | 9.25 |

Both historical arms passed 3/12 (25%), so DC − ZS was 0 percentage points. DC reduced mean wall time from 84.37s to 71.37s, but silent incorrect successes rose from 5/12 to 6/12. This is legacy computer use with an unknown retained model ID, not current compiled Flow.

## What remains unmeasured

- Current compiled Flow versus zero-shot on WAA: no valid result.
- Exact current Flow 1.12.1 on the matched Frappe/OpenEMR matrix: requalification pending.
- Current paid agent arm on the matched Frappe/OpenEMR matrix: intentionally omitted.
- A real design-partner Windows/Citrix workflow: not represented here.

## Caveats

- Frappe and OpenEMR tasks use synthetic records on one macOS host.
- The current study measures one workflow per application and cosmetic drift only.
- Direct API controls are expected to be faster and are the preferred actuation tier when available.
- The historical WAA study used a legacy computer-use architecture, not the current compiler/runtime.
- The retained model-free summaries came from a worktree later committed as 84c7a94, but do not embed a runtime revision; that revision is a post-hoc association, not an exact binding.
- Primary screenshots and oracle-evidence files remain outside Git; the committed summaries retain per-run evidence and artifact hashes, not the raw files.
- The committed legacy input is a compact non-sensitive derivative; it retains normalized rows, task instructions, and hashes for 48 raw summary/execution inputs, not the raw inputs themselves.
- Legacy model ID, token usage, and dollar cost are unavailable in the retained artifacts.
- No new GUI, cloud, or model runs were performed for this report.

The JSON companion contains every normalized row, hashes of the three committed compact inputs, per-run current evidence hashes retained by those inputs, and hashes for the 48 uncommitted historical summary/execution inputs.
