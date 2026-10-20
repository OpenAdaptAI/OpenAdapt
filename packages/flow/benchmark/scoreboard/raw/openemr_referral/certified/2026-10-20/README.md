# OpenEMR referrals r3–r6: certified runs, each under an offline Seal

The landing demo's four referrals, run by the compiled OpenEMR referral program on a local OpenEMR 8.0.0.3 test fixture with synthetic patients. No model was called.

| Run directory | Referral | Execution | Transaction | New charts (SQL) | Changed charts (SQL) |
|---|---|---|---|---|---|
| `1-r3-ines-carvalho` | r3, Ines Carvalho | VERIFIED | VERIFIED | 1 | 0 |
| `2-r4-theo-lindqvist` | r4, Theo Lindqvist | VERIFIED | VERIFIED | 1 | 0 |
| `3-r5-grace-adeyemi` | r5, Grace Adeyemi | VERIFIED | VERIFIED | 1 | 0 |
| `4-r6-rosa-delgado` | r6, Rosa Delgado 1958-03-22 | HALTED | HALTED_BEFORE_EFFECT | 0 | 0 |

Before each run the fixture was reset and the existing chart Rosa Delgado (born 1958-03-27) seeded. For r6, OpenEMR's duplicate dialog listed that chart, and the program stopped at step_039 because "No matches were found." was not on screen. Nothing was saved.

## How the runs were authorized

- **Profile:** `standard`.
- **Authorization:** path A. The Standard run gate was evaluated on the certified bundle under `clinical-write` before each run, and the governed authorization was built from that gate (`run-gate.json` in each run directory).
- **Authorizing person:** the founder, recorded in `authorization.json` with their words.

## The bundle

- `bundle/`, content digest `ec096afa2b614326b00ec3155f256041c19d86f3998761ce9d772cfd911460b6`, certified under `clinical-write` (`qualification/`):
  - 6 cases × 3 trials passed, with `silent_incorrect_success=0` and `over_halt=0`.
  - The certified run (r3) was VERIFIED.
  - The real-duplicate check passed 3 of 3 (`qualification/duplicate-check/`): each trial had transaction outcome HALTED_BEFORE_EFFECT, halted at step_039, and left 0 new charts.
- **Engine:** OpenAdapt commit `7a7a3c548`, plus the script changes in PR #1402, which does not change `openadapt_flow`. The certification is tied to that engine. On a later engine whose contracts changed, `Workflow.load` refuses the bundle as "not a current certification", and the bundle must be re-qualified before it runs again.
- **Superseded:** bundles dated 2026-10-20 before this one had no duplicate guard (#1399, #1400). Their runs are internal evidence only.

## Checking the Seals

Each `engine/report.json` is signed with an offline Seal (`openadapt.local-seal/v1`), key `seal-ed25519-ca08dd7f42a4be01`. The public key is in `seal-key.json`; the private key was never in this repository.

```bash
openadapt-flow local-seal verify 1-r3-ines-carvalho/engine --keys seal-key.json
```

These Seals are not anchored in a transparency log.

OpenEMR puts its per-session CSRF token in the entry URL (`main.php?token_main=...`). Each report here was run through `openadapt-flow redact-report` before it was sealed, so `execution_entry_url` reads `token_main=<redacted>`, and the report's `redactions` field names that one change. The duplicate-check reports in `qualification/duplicate-check/` were redacted the same way. The unredacted originals stay on the machine that ran the work. See `packages/flow/docs/REPORT_REDACTION.md`.

## Not committed

- Step screenshots (`engine/steps/*.png`) and identity crops (`engine/private/`) stay out of the commit. `screenshots.sha256` lists the SHA-256 of every screenshot.
- `sql-readback.json` is a direct SQL read of `openemr.patient_data`, made beside the engine's REST read-back.
