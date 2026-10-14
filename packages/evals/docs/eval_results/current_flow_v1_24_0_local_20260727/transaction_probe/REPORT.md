# Flow transaction outcome taxonomy probe

Does the terminal transaction outcome match what the system of record can actually prove? Judged only against MockMed's independent store, never against the runtime's report or the screen. **Not** a zero-shot computer-use comparison.

## Source and environment

- Flow commit: `4ca7566b73154769398d3135507060fa020aad0a` (version `1.24.0`; tracked-clean source)
- Release tag: `v1.24.0`
- Wheel SHA-256: `170fdac154794292c99dc6eea6486e7a2c3fdf321bcd87976d924bccd3db4aef`
- Evals base commit: `1fec68532e70dd05d6ad8668870c376c7fd8d996`
- Runner SHA-256: `097cc900b44872712ebd7a5bfbdb53f87c85bf5bd8dcbef88fa893743d240ce8`
- Platform: `macOS-15.7.3-arm64-arm-64bit`
- Python: `3.12.7`
- Playwright: `1.61.0`
- Chromium: `Playwright-managed headless Chromium`
- Network/provider use: loopback bundled MockMed fault server only; no cloud VM, hosted runner, or model API

## Counted result

3 trials per cell, no retries. The oracle is the system-of-record snapshot delta at `GET /api/db`: the intended effect is exactly one new `Triage` encounter for the intended patient carrying this run's note, with no pre-existing row destroyed.

| Fault mode | Verification | Runs | Ground-truth effect | Transaction outcome | Billable | Verification performed | Model calls |
|---|---|---:|---|---|---:|---:|---:|
| `ok` | `unverified` | 3 | intended_once 3 | COMPLETED_UNVERIFIED 3 | 0 | 0 | 0 |
| `ok` | `effect_verified` | 3 | intended_once 3 | COMPLETED_UNVERIFIED 2, HALTED_BEFORE_EFFECT 1 | 0 | 2 | 0 |
| `timeout` | `unverified` | 3 | intended_once 3 | HALTED_BEFORE_EFFECT 3 | 0 | 0 | 0 |
| `timeout` | `effect_verified` | 3 | intended_once 3 | HALTED_BEFORE_EFFECT 3 | 0 | 0 | 0 |
| `optimistic` | `unverified` | 3 | absent 3 | COMPLETED_UNVERIFIED 3 | 0 | 0 | 0 |
| `optimistic` | `effect_verified` | 3 | absent 3 | HALTED_BEFORE_EFFECT 3 | 0 | 2 | 0 |
| `session` | `unverified` | 3 | absent 3 | HALTED_BEFORE_EFFECT 3 | 0 | 0 | 0 |
| `session` | `effect_verified` | 3 | absent 3 | HALTED_BEFORE_EFFECT 3 | 0 | 0 | 0 |
| `duplicate` | `unverified` | 3 | duplicate 3 | COMPLETED_UNVERIFIED 3 | 0 | 0 | 0 |
| `duplicate` | `effect_verified` | 3 | duplicate 3 | RECONCILIATION_REQUIRED 3 | 0 | 3 | 0 |

`timeout` commits the row and then hangs past the client timeout, so the client sees an error though the write landed. `optimistic` paints a success banner the server then rejects. `session` returns 401 and persists nothing. `duplicate` accepts every arriving write.

## Invariants

An invariant no counted run could exercise is reported as `vacuous`; it proves nothing and is not a pass.

| Invariant | Holds | Applicable runs | Violations |
|---|---|---:|---:|
| COMPLETED_UNVERIFIED must never be reported as a production success and must never be billable. | yes | 11 | 0 |
| Only VERIFIED may be billable. | yes | 30 | 0 |
| An outcome that asserts no business effect occurred (HALTED_BEFORE_EFFECT / FAILED_PLATFORM / CANCELED / REJECTED_POLICY) must not be reported when the system of record shows the write landed. | **NO** | 16 | 7 |
| A consequential step that reached actuation but whose effect was never verified cannot be classified as a proven absence; it must be RECONCILIATION_REQUIRED. | **NO** | 23 | 14 |
| A single run must never write the intended record more than once. A duplicate row from a run whose backend is NOT the double-accepting 'duplicate' mode would be a blind retry. | yes | 24 | 0 |
| VERIFIED must never be reported when nothing landed. | vacuous | 0 | 0 |
| A healthy compiled run makes no model calls and costs $0. | yes | 30 | 0 |

## Findings

- **`no_false_absence_claim` FAILED (7 runs).** An outcome that asserts no business effect occurred (HALTED_BEFORE_EFFECT / FAILED_PLATFORM / CANCELED / REJECTED_POLICY) must not be reported when the system of record shows the write landed.
  - `timeout` / `unverified` trial 1: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `unverified` trial 2: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `unverified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `ok` / `effect_verified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `effect_verified` trial 1: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `effect_verified` trial 2: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `effect_verified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
- **`unverified_delivered_write_needs_reconciliation` FAILED (14 runs).** A consequential step that reached actuation but whose effect was never verified cannot be classified as a proven absence; it must be RECONCILIATION_REQUIRED.
  - `timeout` / `unverified` trial 1: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `unverified` trial 2: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `unverified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `session` / `unverified` trial 1: reported `HALTED_BEFORE_EFFECT` while the system of record holds `absent` (0 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `session` / `unverified` trial 2: reported `HALTED_BEFORE_EFFECT` while the system of record holds `absent` (0 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `session` / `unverified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `absent` (0 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `ok` / `effect_verified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `effect_verified` trial 1: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `effect_verified` trial 2: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `timeout` / `effect_verified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `intended_once` (1 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `optimistic` / `effect_verified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `absent` (0 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `session` / `effect_verified` trial 1: reported `HALTED_BEFORE_EFFECT` while the system of record holds `absent` (0 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `session` / `effect_verified` trial 2: reported `HALTED_BEFORE_EFFECT` while the system of record holds `absent` (0 targeted row(s)); attempt state `delivered`, verification performed: False.
  - `session` / `effect_verified` trial 3: reported `HALTED_BEFORE_EFFECT` while the system of record holds `absent` (0 targeted row(s)); attempt state `delivered`, verification performed: False.

## Caveats

- Synthetic MockMed fault server, one workflow, one macOS host, headless Chromium.
- The probe runs the exact published wheel named above, extracted locally, bound to the release-tagged tracked-clean source.
- The `effect_verified` configuration is authored by this script, not mined by the compiler; it shows what the taxonomy can prove when a verifier IS configured, and is not a claim that a compiled bundle ships one by default.
- No hosted lifecycle, Windows UIA, RDP, Citrix, or real customer application is represented.
