# Attest bridge — opt-in signed effect receipts

`openadapt-attest` is a **separate, privately distributed proof sidecar**:
after a run it independently verifies the run's claimed effect against the
system of record and writes a **signed receipt**. The public MIT engine never
depends on it. This bridge (`openadapt_flow/attest_bridge.py`) is the entire
coupling surface:

- **Lazy import.** The sidecar is imported only after you opt in with a
  contract. **Without `openadapt-attest` installed, the flags below print a
  one-line notice and do nothing** — the run is otherwise identical.
- **Wrap, never rewrite.** Both hooks are fully best-effort. Every exception
  is caught and printed as a warning, so attestation can never change the
  run's outcome, its report, or its exit code.

## Opting in

Available on `replay`, `run`, and `resume`. A CLI flag wins over its
environment fallback.

| Flag | Environment fallback | Meaning |
| --- | --- | --- |
| `--attest-contract PATH` | `OPENADAPT_FLOW_ATTEST_CONTRACT` | Effect-contract YAML; configuring it is the opt-in switch |
| `--attest-sign-key PATH` | `OPENADAPT_FLOW_ATTEST_SIGN_KEY` | Signing key for the receipt |
| `--attest-audit-log PATH` | `OPENADAPT_FLOW_ATTEST_AUDIT_LOG` | Append-only audit log the sidecar writes to |
| `--attest-pre-state PATH` | `OPENADAPT_FLOW_ATTEST_PRE_STATE` | Existing pre-actuation snapshot; suppresses the automatic capture |

Example:

```
openadapt-flow run BUNDLE --config deploy.yaml \
    --attest-contract contracts/eligibility.yaml \
    --attest-sign-key ~/.openadapt/attest-signing.key
```

## What happens during a run

1. **Before actuation** (replay/run only): when a contract is configured and
   no explicit `--attest-pre-state` was given, the bridge captures a
   system-of-record snapshot and writes it to `attest_pre_state.json` in the
   run directory. Delta-style effect checks (for example `count_new_only`,
   which needs a readable pre-state baseline to judge only newly created
   records) require this snapshot or an explicit `--attest-pre-state` file.
   A failed capture prints one warning line and the run proceeds.
2. **After the run**: the bridge hands the finished run directory to the
   sidecar, which reads `report.json`, auto-uses `attest_pre_state.json`
   when present, and writes the claim and the signed receipt. The CLI prints
   a compact summary: verdict (`confirmed` / `refuted` / `absent` /
   `unknown`), evidence tier, and the receipt path.

## Artifacts (all in the run directory)

| File | Written by | Contents |
| --- | --- | --- |
| `attest_pre_state.json` | the bridge (pre-actuation) | System-of-record snapshot baseline |
| `attest_claim.json` | the sidecar | The effect the run claims to have had |
| `attest_receipt.json` | the sidecar | The signed verification receipt |

The names are deliberately distinct from the engine's own unsigned
`receipt.json` (the local run receipt) — the two never collide. The operator
console's local JSON viewer lists `attest_claim.json` and
`attest_receipt.json` alongside the other run-root artifacts.
