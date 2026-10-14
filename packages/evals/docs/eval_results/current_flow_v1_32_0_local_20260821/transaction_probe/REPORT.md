# Flow transaction outcome taxonomy probe

Does the terminal transaction outcome match what the system of record can actually prove? Judged only against MockMed's independent store, never against the runtime's report or the screen. **Not** a zero-shot computer-use comparison.

## Source and environment

- Flow commit: `d1b054c718d60465fb8a2e1853e6a84c7f26dec2` (version `1.32.0`; tracked-clean source)
- Release tag: `v1.32.0`
- Wheel SHA-256: `d58dcf8b7e54c8a45199db4390a588dc261bb754c5ab2af0fdb540e44cb6bb8a`
- Evals base commit: `b7d4fe842f5a9ae9e4188df6087911b24a1706f2`
- Runner SHA-256: `e27ecc7fcda287c9f946bf002f02443c8628c3a2cbc50f88ed03b1ad3abf6a42`
- Platform: `macOS-15.7.3-arm64-arm-64bit`
- Python: `3.12.7`
- Playwright: `1.62.0`
- Chromium: `Playwright-managed headless Chromium`
- Network/provider use: loopback bundled MockMed fault server only; no cloud VM, hosted runner, or model API

## Counted result

3 trials per cell, no retries. The oracle is the system-of-record snapshot delta at `GET /api/db`: the intended effect is exactly one new `Triage` encounter for the intended patient carrying this run's note, with no pre-existing row destroyed.

| Fault mode | Verification | Runs | Ground-truth effect | Transaction outcome | Billable | Verification performed | Model calls |
|---|---|---:|---|---|---:|---:|---:|
| `ok` | `unverified` | 3 | intended_once 3 | COMPLETED_UNVERIFIED 3 | 0 | 0 | 0 |
| `ok` | `effect_verified` | 3 | intended_once 3 | COMPLETED_UNVERIFIED 3 | 0 | 3 | 0 |
| `timeout` | `unverified` | 3 | intended_once 3 | RECONCILIATION_REQUIRED 3 | 0 | 0 | 0 |
| `timeout` | `effect_verified` | 3 | intended_once 3 | RECONCILIATION_REQUIRED 3 | 0 | 0 | 0 |
| `optimistic` | `unverified` | 3 | absent 3 | COMPLETED_UNVERIFIED 3 | 0 | 0 | 0 |
| `optimistic` | `effect_verified` | 3 | absent 3 | RECONCILIATION_REQUIRED 3 | 0 | 3 | 0 |
| `session` | `unverified` | 3 | absent 3 | RECONCILIATION_REQUIRED 3 | 0 | 0 | 0 |
| `session` | `effect_verified` | 3 | absent 3 | RECONCILIATION_REQUIRED 3 | 0 | 0 | 0 |
| `duplicate` | `unverified` | 3 | duplicate 3 | COMPLETED_UNVERIFIED 3 | 0 | 0 | 0 |
| `duplicate` | `effect_verified` | 3 | duplicate 3 | RECONCILIATION_REQUIRED 3 | 0 | 3 | 0 |

`timeout` commits the row and then hangs past the client timeout, so the client sees an error though the write landed. `optimistic` paints a success banner the server then rejects. `session` returns 401 and persists nothing. `duplicate` accepts every arriving write.

## Invariants

An invariant no counted run could exercise is reported as `vacuous`; it proves nothing and is not a pass.

| Invariant | Holds | Applicable runs | Violations |
|---|---|---:|---:|
| COMPLETED_UNVERIFIED must never be reported as a production success and must never be billable. | yes | 12 | 0 |
| Only VERIFIED may be billable. | yes | 30 | 0 |
| An outcome that asserts no business effect occurred (HALTED_BEFORE_EFFECT / FAILED_PLATFORM / CANCELED / REJECTED_POLICY) must not be reported when the system of record shows the write landed. | vacuous | 0 | 0 |
| A consequential step that reached actuation but whose effect was never verified cannot be classified as a proven absence; it must be RECONCILIATION_REQUIRED. | yes | 21 | 0 |
| A single run must never write the intended record more than once. A duplicate row from a run whose backend is NOT the double-accepting 'duplicate' mode would be a blind retry. | yes | 24 | 0 |
| VERIFIED must never be reported when nothing landed. | vacuous | 0 | 0 |
| A healthy compiled run makes no model calls and costs $0. | yes | 30 | 0 |

## Findings

- Every invariant held across every counted trial.

## Caveats

- Synthetic MockMed fault server, one workflow, one macOS host, headless Chromium.
- The probe runs the exact published wheel named above, extracted locally, bound to the release-tagged tracked-clean source.
- The `effect_verified` configuration is authored by this script, not mined by the compiler; it shows what the taxonomy can prove when a verifier IS configured, and is not a claim that a compiled bundle ships one by default.
- No hosted lifecycle, Windows UIA, RDP, Citrix, or real customer application is represented.
