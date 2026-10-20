# Referral demo runs, with offline Seals

The compiled OpenEMR referral program on the landing page's four referrals,
run on 19 October 2026 on a private copy of the pinned OpenEMR fixture. All
patients are synthetic. No model was called.

| Referral | Patient | Run | Outcome in the report | Read-back |
|---|---|---|---|---|
| 1 | Nadia Brennan | `referral-1-r1-nadia-brennan/engine` | `COMPLETED_UNVERIFIED`, every record check confirmed | 1 new chart, 12 of 12 fields |
| 2 | Marcus Feld | `referral-2-r2-marcus-feld/engine` | `COMPLETED_UNVERIFIED`, every record check confirmed | 1 new chart, 12 of 12 fields |
| 3 | Ines Carvalho | `referral-3-r3-ines-carvalho/engine` | `COMPLETED_UNVERIFIED`, every record check confirmed | 1 new chart, 12 of 12 fields |
| 4 | Rosa Delgado, 1958-03-22 (possible duplicate of the seeded chart born 1958-03-27) | `referral-4-r6-rosa-delgado/engine` | `HALTED` at the duplicate check (`step_039`: "No matches were found." not shown) before saving; transaction outcome `RECONCILIATION_REQUIRED` | no new chart |

- **What ran.** The #1269 harness's own run (`benchmark/openemr_api_tier/feature_e2e.run_one`, engine 93c32e9f), with API steps off (the screen program). The engine's record check reads OpenEMR's REST API through the fixture's read-only oracle client. The runs use no named execution profile, so a confirmed run is recorded as `COMPLETED_UNVERIFIED`, not `VERIFIED`. `VERIFIED` needs the standard profile, whose gate refuses this bundle today.
- **Referral 4 is not `HALTED_BEFORE_EFFECT`.** Nothing was saved, but steps whose risk was never reviewed keep the engine from proving that, so it records `RECONCILIATION_REQUIRED` (see #1331).
- **Seals.** Each `seal.json` is an **offline Seal, signed by this machine's key** (`openadapt.local-seal/v1`, key `seal-ed25519-accd03a9246d29bb`). It covers the exact bytes of the `report.json` beside it and copies that report's outcome; it never upgrades it. It is not an OpenAdapt Cloud Seal. A statement signed by the machine that ran the work is weaker evidence than an independent party's.
- **Referral 4 has a report and its offline Seal, and no receipt.** The local receipt is written only for a `VERIFIED` run.

Verify any run against the pinned public key in `seal-key.json`:

```bash
openadapt-flow local-seal verify referral-1-r1-nadia-brennan/engine --keys seal-key.json
```

It prints `valid offline Seal over report.json, key seal-ed25519-accd03a9246d29bb`. A changed report prints `not valid: subject_mismatch`.

`summary.json` lists each run's outcome, read-back and Seal digest. `result.json` beside each run is the harness's own SQL read-back. Screenshots are not kept. The script is `scoreboard/seals_referral_demo.py`.
