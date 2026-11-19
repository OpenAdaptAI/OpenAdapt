# Governed Form & Wizard Transaction Layer (`openadapt_flow.form_tx`)

`openadapt_flow.form_tx` executes structured dossiers (`InputDossier`) across stateful multi-screen web and desktop forms that have **no usable entity REST API and no test sandbox**, where the UI wizard itself is the read-modify-write transaction.

Healthy runs make **zero model calls**, send nothing outside the operator's machine, and fail closed whenever a required field lacks grounded provenance or when the target portal pre-populates conflicting data.

## Core Components

| Module | Responsibility |
| :--- | :--- |
| `form_tx/types.py` & `codecs.py` | Strict Pydantic contracts (`InputDossier`, `UIProfileSpec`, `FormPack`, `FormHaltItem`, `SignedFormTxReceipt`) and deterministic normalization codecs (`ISO_DATE`, `CURRENCY_CENTS`, `PHONE_DIGITS`, `BOOLEAN_YES_NO`, `ENUM_MAP`). |
| `form_tx/playwright_surface.py` & `profiles/` | Quiescent structural form observer and actuator pairing `PageQuiescence` with declarative `busy_selectors`, detached body-portaled comboboxes (`role="listbox"`), child popup subroutines (`window.open`), and zero-mutation keepalive checks. |
| `form_tx/reconciler.py` | Screen-by-screen fixed-point dossier reconciler. Re-snapshots after server-driven Partial Page Renders (PPR) so conditional follow-up fields are caught immediately, and halts on `UNGROUNDED_REQUIRED_FIELD`, `PREFILL_VALUE_CONFLICT`, `PREFILL_ENTITY_CONFLICT`, `PORTAL_VALIDATION_BLOCKED`, or `MODAL_CONFIRMATION_REQUIRED`. |
| `form_tx/judge.py` & `receipt.py` | Independent post-commit Summary / Lookup Read-Back verifier (`SummaryJudge`), typed output extraction, and value-scrubbed Ed25519 `SignedFormTxReceipt` issuance. |
| `form_tx/learning.py` | `RuleAccumulator`: proposes an unadmitted `PackPatchCandidate` only when two or more runs resolve the same `field_signature` identically with zero contradictions. |
| `form_tx/session_lease.py` | `WarmSessionLease` (auth-wall preflight, secret-suppressed `BrowserAuthHandoff`, zero-mutation `heartbeat`) and idempotent `FormTxQueueRunner` (`request_id` deduplication). |
| `form_tx/pack_compiler.py` | Deterministic `induce_form_pack` mapping canonical domain schemas onto captured `FormSnapshot`s with volatile ID stripping and PII exclusion. |
| `copilot_bridge.py` & `chrome_extension/` | Local Chrome Side-Panel Co-Pilot (`sidepanel.html`) communicating over `chrome.runtime.connectNative('com.openadapt.extension')` to highlight halted/conflicted fields in-place and submit governed operator resolutions. |

## Honesty & Safety Guarantees

1. **No guessing on unknown fields:** Any visible `required` control without a grounded fact (`confidence >= min_confidence`) in `InputDossier` halts `UNGROUNDED_REQUIRED_FIELD` before clicking *Next* or *Submit*.
2. **Server-prefill conflict detection:** Non-empty pre-populated fields or entity table rows (such as server-injected household members or matched records) that differ from or are absent in `InputDossier` halt `PREFILL_VALUE_CONFLICT` or `PREFILL_ENTITY_CONFLICT`.
3. **Zero-mutation session keepalive:** `WarmSessionLease.heartbeat()` faults immediately (`HeartbeatMutationError`) if a keepalive probe emits any `POST`, `PUT`, `PATCH`, or `DELETE` request.
4. **Independent summary read-back:** `VERIFIED_DONE` is returned only after `SummaryJudge` confirms every bound read-back field matches `(InputDossier ∪ ApprovedHumanResolutions)` under its declared codec and all required outputs are extracted and signed.
