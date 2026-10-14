# Remote frame-lease delivery: the two safety properties, measured

openadapt-flow `c9618cc` (1.28.0) lets a consequential remote click be
delivered through the backend's one-shot actuation lease when no typed
delivery receipt exists. That relaxes a refusal, so its two stated
safety properties are measured here rather than trusted.

## Source and environment

- Flow commit: `2d225dea9a0ad29ca84ce1b037cc0ac671367e28` (version `1.31.0`; tracked-clean source)
- Release tag: `v1.31.0`
- Wheel SHA-256: `81133db1528ad1bb1f26e3fcb6aea61b0651db6d905cf2e4943e8383c1f3d29c`
- Evals base commit: `1082192e9c2ec299d31330608c1016a939d3b88d`
- Runner SHA-256: `d75c1c62248818d937705e3a7b3170b09ea211b54591f37a57520018b25a7388`
- Platform: `macOS-15.7.3-arm64-arm-64bit`
- Python: `3.12.13`
- Network/provider use: none. No server, no browser, no model API.

## Counted result

3 trials per cell. `input edges` counts what the backend was actually asked to deliver.

| Cell | Runs | Delivered | Refused | Receipts | Actuation tier | `transaction_outcome` (demo / standard / regulated) |
|---|---:|---:|---:|---:|---|---|
| `ungoverned_lease` | 3 | 3 | 0 | 0 | None | COMPLETED_UNVERIFIED / COMPLETED_UNVERIFIED / COMPLETED_UNVERIFIED |
| `governed_lease` | 3 | 0 | 3 | 0 | None | HALTED_BEFORE_EFFECT / HALTED_BEFORE_EFFECT / HALTED_BEFORE_EFFECT |
| `lease_frame_changed` | 3 | 0 | 3 | 0 | None | RECONCILIATION_REQUIRED / RECONCILIATION_REQUIRED / RECONCILIATION_REQUIRED |

## Invariants

Every invariant states the denominator it was evaluated over. A `vacuous` invariant had no applicable run and proves nothing.

| Invariant | Applicable runs | Violations | Holds |
|---|---:|---:|---|
| A governed run must refuse a consequential remote click on a backend that cannot bind its exact fresh frame and target to delivery, before the first input edge. | 3 | 0 | yes |
| A remote write delivered through the frame lease alone carries no typed receipt, so no execution profile may classify it VERIFIED or production-eligible. | 9 | 0 | yes |
| A lease-only delivery must leave the result unlabeled: no typed delivery receipt and no recorded actuation tier. | 3 | 0 | yes |
| The lease is the safety property: a remote frame that changed between the lease and the input edge must stop delivery. | 3 | 0 | yes |
| A compiled remote replay makes no model calls and costs $0. | 9 | 0 | yes |

## Scope

- The backend is a fake implementing ONLY the two-phase remote
  actuation lease, which is the exact protocol surface a pixel-only
  no-DOM canvas backend exposes. This measures a runtime contract, not
  a real Citrix or RDP session.
- One synthetic single-step workflow whose only step is the
  irreversible write. Resolution is scripted to one fixed point so the
  delivery decision is the only thing that varies between cells.
- No claim is made here about wrong-target immunity on a real remote
  surface, about identity coverage, or about any hosted lifecycle.

Reproduce: `python scripts/probe_remote_lease_safety.py --flow-source <v1.31.0-checkout> --flow-wheel <openadapt_flow-1.31.0-py3-none-any.whl> --out <new-output-directory>`
