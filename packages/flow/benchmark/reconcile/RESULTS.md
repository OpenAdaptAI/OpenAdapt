# Reconciler fault run, 2026-11-05

One run of `run.py run` on the pre-registered fixture set
(`fixtures.json`, sha256 `f75c31b6…3d933278d`, committed before any run), with
the specs in this folder and the reconciler at `5e3242460`. The design is in
[`docs/RECONCILE.md`](../../docs/RECONCILE.md).

## Systems

| Leg | System |
|---|---|
| EHR and write-back | Medplum 5.2.8 (local, synthetic patients) |
| Payer | HL7 Da Vinci PAS reference implementation, `HL7-DaVinci/prior-auth` at `848f28c1`, built from source and run locally |
| Clearinghouse | Folder of X12 005010 278 and 277 files written by `x12_fixtures.py` |

All data is synthetic. Separately, `epic_check.py` read one of Epic's
published sandbox test patients (four read-only calls) as the EHR leg of two
claim units: the matching 277 reconciled, and a 277 with a wrong birth date
was `REFUTED` as a `mismatch` on the clearinghouse leg.

## Results

47 units: 35 prior authorizations and 12 claims. The run evaluates them 36
hours after seeding.

| Measure | Result |
|---|---|
| Planted breaks found with the right kind, leg and outcome | 36 of 36 |
| Planted `REFUTED` breaks found | 30 of 30 |
| Units with a planted fault reported `CONFIRMED` | 0 of 27 |
| Clean and look-alike units `CONFIRMED` | 17 of 17 |
| Units still inside their deadline reported `UNCERTAIN` | 3 of 3 |
| `REFUTED` breaks on a clean or in-flight unit | 0 |
| Median time to reconcile one unit | 0.026 s |

The run also reported breaks that the fixture set doesn't list:

- **3 `REFUTED` `not_received` breaks on the `payer_decision` leg** of the 3
  units whose submission was dropped before the payer. A payer that never got
  the request has no decision either. The fixture set predates the separate
  decision leg, so it lists only the payer and clearinghouse breaks for these
  units. The run's `false_breaks` field counts these 3 breaks because they
  aren't listed.
- **9 `UNCERTAIN` breaks**, on the decision and write-back legs of in-flight
  units and on the write-back leg of the dropped units. These are legs whose
  record depends on one that isn't there yet.

## What the reference implementation needed

These are found in its source and in the live run:

- It writes no review-action code for an approval, a denial or a pend. The
  decision is in `ClaimResponse.disposition`. The spec maps it to X12 HCR01
  with the server's own table (`FhirUtils.dispositionToReviewAction`). A word
  the map doesn't know leaves the decision unchecked (`UNCERTAIN`).
- Its rules come from an external CDS library, which isn't loaded. So every
  request pends, and the server grants it on a timer as a second
  ClaimResponse with a new id and a new `preAuthRef`. The decision leg judges
  the latest ClaimResponse per submission.
- A request whose code asks for documentation (73722 here) stays pended. It
  is never granted.
- A ClaimResponse search returns response Bundles, with the ClaimResponse
  inside each one.
- A ClaimResponse id is its auth number, so the result refers to that record
  as `ClaimResponse/…`.
- `ProcessClaimItemTask.start()` starts the item's thread before it marks the
  item running. An item that finishes first is then reported as failed, and
  the submit returns HTTP 400 after the Claim is stored. `run.py build-pas`
  swaps those two lines before the build (`PAS_PATCH`) and fails if the patch
  no longer applies. Nothing else in the server is changed.

## Not shown by this run

- Denials. The server's rules engine isn't loaded, so it grants or pends
  every request. The unit tests cover denial and pend against SQLite stores.
- Claims at a payer. Claim units reconcile the EHR against the 277 only.
- Any production payer, clearinghouse or EHR.
