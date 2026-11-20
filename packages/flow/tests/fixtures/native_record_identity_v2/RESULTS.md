# Offline results

2026-11-20, source-only candidate:

- 21 focused pytest cases passed with no skips, using Python 3.12.13 and
  PowerShell 7.5.0 on macOS. Flow and Types imports were bound to this checkout.
- One test executes 26 lifecycle cases from the generated PowerShell functions,
  against stand-in controls and the real SQLite CLI. These are included in the
  pytest count, not additional native trials.
- A separate planted omission of note clearing was rejected at selection reset.
- CLI IDs matched independent SQLite reads, including zero and signed int64
  boundaries. Wrong schema/mapping controls refused. All lifecycle DB rows stayed
  unchanged.
- Ruff 0.17.0 checks and formatting passed. Generated output matches its pinned
  base/override/manifest; the original save-handler bytes match.

No WinForms UI, desktop input, lab, VM, cloud resource or native CI campaign was
used. Windows PowerShell 5.1 and native WinForms event/provider behavior remain
unmeasured. The shared consumer protocol, exact canonical-ID matching, live
DB-row-to-control binding, combined campaign and held-out qualification remain
held. No prior result or denominator changed.
