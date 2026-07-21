# EffectVerifier — independent effect verification against a system of record

**Status:** implemented (runtime + tests). Concrete runtime for the
`Effect` type proposed in
[`WORKFLOW_PROGRAM_IR.md`](./WORKFLOW_PROGRAM_IR.md) (PR #61).

**Operator guide:** the deployable kit built on this design — declarative
verifier configs (REST / FHIR / read-only SQL / file-SFTP / document-hash)
with secret-isolated auth, run-parameter binding, effect-coverage
lint/certify gates, and typed reconciliation tasks — is documented in
[`docs/EFFECT_KIT.md`](../EFFECT_KIT.md).

## The problem this closes

A vision/screen postcondition (`openadapt_flow.ir.Postcondition`) answers
*"do the pixels look like a save happened?"* The transactional fault-model
study (`benchmark/fault_model/`, `docs/LIMITS.md`) proved that insufficient:
**5 of 7 transactional fault classes are silently mishandled** by screen
verification. A partial save, a phantom optimistic-UI success, a duplicate
submission, a lost update, and a double-delivered click all leave the screen
showing "saved" while the **record** is wrong or missing.

An `EffectVerifier` answers the only question a record system may trust:
*"is the intended record actually in the system of record — exactly once,
with the right field values?"* — by reading the **system of record** (a
FHIR/REST API, a document store), never the screen.

## The Protocol

```python
class EffectVerifier(Protocol):
    substrate: str
    def capture_pre_state(self, context) -> EffectState: ...
    def verify(self, expected: Effect, before: EffectState, context) -> EffectVerdict: ...
```

- **`Effect`** is the RFC's typed effect. Kinds: `record_written` (a record
  matching a selector exists *exactly* `expected_count` times — at-most-once)
  and `field_equals` (a read-back of one field). Substrate-neutral: the SAME
  `Effect` is checked by every verifier.
- **`capture_pre_state`** snapshots the system of record *before* the action —
  a baseline for delta/at-most-once counting and for collateral-loss
  detection (a lost update deletes a record that was present before).
- **`verify`** returns a three-valued **`Verdict`**, fail-safe to HALT
  (mirrors the identity gate's refuse-rather-than-guess posture):
  - `CONFIRMED` — effect present and correct → proceed;
  - `REFUTED` — the system of record **affirmatively contradicts** the effect
    (missing / duplicated / wrong value / collateral loss) → HALT;
  - `INDETERMINATE` — the system of record is **unreachable or unreadable**
    (transport error, non-2xx, expired OAuth token, unparseable body) → HALT.
    An expired token is *never* mistaken for "record absent"; it halts.

`EffectVerdict.should_halt` is true for both non-confirmed verdicts. There is
no "probably fine."

## Substrates verified against (different verifier *types*, anti-overfit)

| Substrate | Verifier | System of record | Status |
|---|---|---|---|
| **OpenEMR FHIR R4** (primary; the healthcare wedge) | `FhirEffectVerifier` | `GET {base}/Observation?patient=…` → FHIR `Bundle` search-set of nested resources | **Contract-gated in CI + verified LIVE.** CI runs against a byte-faithful fake FHIR R4 server (`tests/_fhir_fake.py`); a live end-to-end test (`tests/test_effect_fhir_live_openemr.py`) runs against a **real local OpenEMR** stood up by `benchmark/openemr_live/` when `OPENEMR_FHIR_BASE_URL` (+ token) is set. |
| **REST/JSON** (MockMed transactional back end) | `RestRecordVerifier` | `GET /api/db` on `mockmed.fault_server` — the same in-process HTTP system of record the fault-model study judges by | **Live in CI** (localhost). Drives the proof matrix below. |
| **Filesystem document store** | `DocumentHashVerifier` | a directory of written documents, verified by SHA-256 content hash | **Live in CI.** A non-HTTP verifier type — proves the protocol is substrate-agnostic, not OpenEMR-shaped. |

**Did OpenEMR run live?** **Yes.** `benchmark/openemr_live/` stands up a real
local OpenEMR (OpenEMR + MariaDB via `docker-compose`) with the REST + FHIR R4
APIs and OAuth2 enabled, and `tests/test_effect_fhir_live_openemr.py` writes a
real Patient through OpenEMR's FHIR API and has this same verifier
independently read it back — asserting CONFIRMED (write landed), REFUTED
(wrong field value; absent record), and INDETERMINATE→HALT (a `401` from a bad
token is never mistaken for "record absent"). Verified end-to-end against
`openemr/openemr:7.0.3` (6/6 live tests). The live write is a **FHIR Patient
POST** — an API write, not a GUI-driven one — because OpenEMR's FHIR API
exposes Observation as **read-only** (no `user/Observation.write` scope), so
the note-as-Observation write the fake models cannot be created over FHIR on a
stock OpenEMR; the property the live test establishes is the one the fake
could not — the verdicts are correct against a **real FHIR server**, not a
fake. CI still runs vision-only against the **public demo**
(`openadapt_flow/benchmark/openemr_benchmark.py`, `scripts/openemr_demo.py`);
the live FHIR test is gated behind `OPENEMR_FHIR_BASE_URL` and skipped
otherwise. See `benchmark/openemr_live/README.md` for the one-command
bring-up.

## THE PROOF — fault-class matrix (screen-verify vs effect-verify)

Driven at the **real persistence boundary** (`mockmed.fault_server`) by
`tests/test_effect_fault_matrix.py`. "screen-verify" is the documented weak
oracle (does the app paint the saved banner? — read from
`mockmed/static/app.js`; the end-to-end version driving the real replayer + OCR
is `benchmark/fault_model/run.py`). "effect-verify" is `RestRecordVerifier`
reading `GET /api/db`.

| Fault class | screen-verify | effect-verify | how effect-verify catches it |
|---|---|---|---|
| **(a) duplicate submission** | ✅ PASS (banner) | 🛑 **REFUTED** | `record_written`: 2 records match, expected 1 |
| **(b) optimistic-UI then backend reject** | ✅ PASS (painted early) | 🛑 **REFUTED** | `record_written`: 0 records (phantom) |
| **(c) partial save** | ✅ PASS (banner) | 🛑 **REFUTED** | `field_equals`: `note` field dropped |
| **(d) stale / concurrent overwrite** | ✅ PASS (banner) | 🛑 **REFUTED** | collateral loss: a pre-state record vanished |
| **(e) double-click extra record** | ✅ PASS (banner) | 🛑 **REFUTED** | `record_written`: 2 records, expected 1 |
| timeout after write | 🛑 FAIL (false-abort) | ✅ CONFIRMED | row landed — effect-verify is *more* correct, prevents a double-write retry |
| session expiry | 🛑 FAIL (safe-halt) | 🛑 REFUTED (absent) | both refuse to claim success (agree) |
| clean write (control) | ✅ PASS | ✅ CONFIRMED | one correct record |
| idempotent key (fix) | ✅ PASS | ✅ CONFIRMED | double-submit collapses to one row |

For the **5 classes the screen silently mishandles, effect-verify REFUTES and
halts.** That is the thesis.

## Idempotency / at-most-once

`Effect.idempotency_key` plumbs an at-most-once key through a consequential
write; `record_written` then counts records **bearing that key** and requires
exactly `expected_count`. A non-idempotent double-submit lands two rows
(REFUTED); the same double-submit carrying an idempotency key the server
de-duplicates on collapses to one (CONFIRMED). Proven in
`test_idempotency_key_neutralizes_duplicate` and
`test_rest_idempotent_write_is_at_most_once`.

## Compensation — reconcile or escalate

`reconcile_or_escalate` (`runtime.effects.compensation`) never silently
proceeds on a REFUTED consequential effect:

- **Duplicate** on an irreversible effect → a `Compensator` (`RestCompensator`)
  deletes the extra records (keeping the earliest), then the effect is
  **re-verified**; only a CONFIRMED re-verification lets the run proceed
  (`RECONCILED`). Reconciles against the **same real system of record** via an
  additive `DELETE /api/encounter/<id>` route (the fault-model study never
  issues DELETE, so its behavior is unchanged).
- **Missing / partial / collateral loss** (no safe automatic undo),
  **reversible** effects, and **INDETERMINATE** (unreadable SoR) always
  **ESCALATE** — a durable halt for a human. Reconciliation must never invent
  or overwrite state.

Proven in `test_compensation_reconciles_detected_duplicate` /
`test_compensation_escalates_partial_save` /
`test_compensation_escalates_when_indeterminate`.

## Explicit fallback when no verifier exists

The permissive `replay` path still halts before a step that declares effects
without an `EffectVerifier`. A certified `run` invocation may instead supply
`--approve-unverified-writes`. The gate issues a run-bound authorization tied
to the sealed bundle, exact step, and effect-contract hashes; the GUI action
then retains its screen postconditions and is reported as
`effect_approved_unverified`, never as independently confirmed. Direct API
writes cannot use this fallback because they have no independent outcome check
or GUI-postcondition floor. See
[`GOVERNED_RUN_AUTHORIZATION.md`](GOVERNED_RUN_AUTHORIZATION.md).

## How this binds to PR #61's `Effect` type

The RFC (`WORKFLOW_PROGRAM_IR.md` §2.2) promotes `Postcondition` → a typed
`Effect` with system-of-record kinds `record_written` / `field_equals` whose
probe is backend-specific (§4: the `api` implementation tier — "call the app's
API / DB write; effect probed against the system of record"). This subsystem
is the concrete runtime for exactly that:

- RFC `Effect(kind="record_written", probe="encounter exists for patient")`
  → `Effect(kind=RECORD_WRITTEN, match={…}, expected_count=1, probe=…)`.
- RFC `Effect(kind="field_equals", field="note", value=params.note)`
  → `Effect(kind=FIELD_EQUALS, match={…}, field="note", value=…)`.
- RFC §4 fidelity ladder (`api` → `dom_uia` → `vision_rdp`, first viable wins):
  the `EffectVerifier` implementations ARE the `api` tier; the existing vision
  postcondition remains the `vision_rdp` tier for pixel-only substrates.
- RFC §2.4 `compensation` + durable escalation → `reconcile_or_escalate`.

When the program IR lands, a `State.effects` list of RFC `Effect`s maps
directly onto these verifiers; nothing here diverges from the RFC schema.

## Running

```bash
# proof matrix + unit + FHIR-fake + filesystem + compensation (all in CI):
pytest tests/test_effect_verifier.py tests/test_effect_fault_matrix.py tests/test_effect_fhir.py

# live OpenEMR FHIR (skipped unless set):
OPENEMR_FHIR_BASE_URL=https://your-openemr/apis/default/fhir/R4 \
OPENEMR_FHIR_TOKEN=<bearer> pytest tests/test_effect_fhir.py -k live
```

No model / Anthropic calls on any path here (the runtime hot path stays $0).
