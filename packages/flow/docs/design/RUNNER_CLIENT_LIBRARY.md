# Runner client library (L1) — verification, lease logic, evidence, command mapping

**Status: Experimental. Library only — no daemon, no network loop, no CLI
verb.** The cloud half of this protocol (`/api/runners/*` in openadapt-cloud)
is merged but **mock-gated (410 in live mode)**, and its transport-facing
contract must change before any customer daemon ships (see “Required contract
revisions” below). This package (`openadapt_flow.runner`) is the
transport-agnostic half that survives that revision, fully unit-tested against
fixtures (`tests/test_runner_client_lib.py`). Un-gating the cloud routes and
choosing the revised transport belong to the cloud launch owner.

## The verified merged contract (openadapt-cloud, read 2026-07-19)

Sources: `src/lib/runners.ts`, `src/lib/runnerAuthorization.ts`,
`src/lib/runEvidence.ts`, `src/app/api/runners/{register,poll,dispatch}/
route.ts`, `src/app/api/runners/runs/[id]/callback/route.ts`.

* **Enrollment** — `POST /api/runners/register`, auth `Bearer <ingest token>`
  (the paired credential from `login`/`connect`). Returns a per-runner
  `oar_…` bearer token exactly once; server stores only its sha256.
* **Poll** — `POST /api/runners/poll`, auth `Bearer <runner token>`, body
  `{wait ≤ 25, lease_seconds ≤ 900}`. The poll IS the heartbeat
  (`last_seen_at`) and the CLAIM: 200 `{job}` returns a leased dispatch with
  a visibility timeout; 204 = no work. Liveness derives from `last_seen_at`
  (online ≤ 120 s, stale ≤ 600 s); the dispatch route 409s unless the runner
  is *online*.
* **Dispatch payload** (PHI-free by construction): `job_kind:
  'governed_run'`, `run_id`, `workflow_id`, `bundle {version_id,
  content_digest, url}`, `deployment_profile_id` (opaque name of a LOCAL
  deployment.yaml), `authorization` (a field-for-field
  `GovernedRunAuthorization`, flow PR #129, minted server-side and bound to
  the sealed bundle content digest + canonical runtime-inputs digest),
  `params` (`{values}` cloud lane or `{ref, expected_digest}` regulated
  lane), `expires_at` (agent refuses to START stale work).
* **Lease semantics** — TTL ≤ 900 s. **No renewal endpoint exists.**
  Reclaim/expiry runs only inside the same runner's next poll: expired
  before start → silently re-offered; expired after start → dispatch
  `failed` + run `dispatch_uncertain_at` (never silent re-dispatch). The
  cron tick never touches `runner_dispatches`.
* **Evidence callback** — `POST /api/runners/runs/{id}/callback`, runner
  token, `{events: [...]}` (≤ 50 per POST) in the fail-closed
  `openadapt.run-evidence/v1` schema: whitelisted fields, forbidden-key +
  PHI-shaped-text deep scan, 422 rejects the whole batch. Idempotent by
  `(run_id, seq)`; the terminal `run_summary` doubles as the lease ack
  (`confirmed → success`, `halted-needs-attention → halt`, `failed →
  failed`); `screenshots_may_leave_box` must be literal `false`. Late
  evidence after lease loss is accepted; duplicate terminals are no-op 202.

## What this library provides

| Module | Provides |
| --- | --- |
| `protocol` | Strict pydantic models of the dispatch wire shape; unknown fields are contract drift → refusal, never best-effort execution. |
| `config` | The operator-authored trust manifest `~/.openadapt/runner.toml`: profiles, trusted bundles by content digest, per-bundle policy pin, `params_ref_required`, per-param domain regexes. |
| `verify` | Independent local re-validation of a leased dispatch (the refusal matrix below). Local gates are final; a compromised control plane can at worst request what local policy already permits. |
| `lease` | Pure lease state machine with injectable clock: single-flight acquire, expired-before-start refusal, renewal (modeled for the coming contract), wall-clock sleep detection, honest late-completion disposition, per-workflow serialization, and an executable mirror of the server reclaim rule. |
| `evidence` | Schema-minimal `openadapt.run-evidence/v1` builders (state/step/halt/run_summary + refusal + engine-failure events). Free text from `HaltObservation` is never forwarded. |
| `outbox` | Durable on-disk evidence queue: ordered batches, atomic writes, idempotent flush semantics, permanent-rejection quarantine. |
| `commands` | Mapping of governed verbs onto the EXISTING CLI entry points: `run` → the fail-closed `openadapt-flow run` gate (with `--pin-digest`, local policy, mode-0600 params file), `resume` → governed durable resume (`--require-approval`). Everything else refuses. |

## Refusal matrix (what the client refuses, and why)

Every refusal has a stable code (`verify.RefusalCode`), is reportable as an
`authorization_refused` halt + terminal `failed` summary, and never executes.

| Code | Trigger | Why |
| --- | --- | --- |
| `unsupported_job_kind` | `job_kind != governed_run` | Only governed runs are implemented; unknown verbs are never guessed. |
| `malformed_dispatch` | strict parse failure / bad `expires_at` | Contract drift: a partially understood dispatch cannot be safely judged. |
| `concurrent_run` | same workflow already in flight locally | Dispatch enqueue has no idempotency key (review S3); double-click Run must not write twice. |
| `dispatch_expired` | `expires_at` passed | Stale attended work is never started; the queue mirrors the refusal. |
| `bundle_not_held` | digest not in `runner.toml` | **No remote code delivery**: only operator-installed bundles run; `bundle.url` is never fetched. |
| `digest_mismatch` | authorization digest ≠ dispatch bundle digest | The payload disagrees with itself. |
| `params_ref_unsupported` | `{ref, expected_digest}` params | No local reference resolver exists yet; guessing values would break the digest binding. |
| `params_values_refused` | inline values for a `params_ref_required` bundle | Regulated posture: runtime params ARE the PHI (review PHI-3); they must not ride the dispatch wire. |
| `param_domain_refused` | param fails/lacks its pinned regex | Local policy must distinguish good params from bad ones (review S2); an approved bundle with attacker-chosen params is the silent-wrong-action class. |
| `unknown_profile` | `deployment_profile_id` not configured / unloadable | Profile contents never leave the box; only named local profiles are honored. |
| `egress_profile_refused` | profile enables model grounding | The evidence stream asserts `screenshots_may_leave_box: false`; a profile that could falsify that is refused up front. |
| `bundle_load_failed` | trusted bundle fails to load/decrypt | Fail closed on integrity/crypto errors. |
| `authorization_mismatch` | `validate_workflow` refusal | Digest, semantics, identity steps, or exact write-approval contract hashes do not fit the sealed bundle. |
| `runtime_inputs_mismatch` | recomputed runtime-inputs digest ≠ binding | Params drift between mint and execution; fail closed, never fail open. |
| `policy_mismatch` | admitted policy ≠ operator pin | The machine's policy pin is final, not the cloud's label. |

Additionally, `commands.map_control_verb` refuses `pause` / `approve` /
`rollback-to-version` (`UnmappedVerbError`): `pause` has no governed CLI verb,
`approve` must come from a locally authenticated human (never a cloud POST
body — review S2), and the merged FIFO lease queue cannot deliver mid-run
control at all (review E3).

## Required contract revisions before a daemon ships

Lifted from the 2026-07-19 end-to-end design review (findings E1, E2, E3, S2,
S3, PHI-3). **Hardening a client transport against the merged contract before
these land is wasted work — that is why this PR contains no poll loop.**

1. **Transport & economics (E1, P0).** The merged 25 s long-poll costs ≈ 104k
   Netlify invocations + ≈ 720 function-hours per always-on machine per month
   against ≈ 49k/mo invocation headroom and a 100-hour allowance, and exceeds
   Netlify's 10 s sync function timeout. Required: fast-return polls with
   server-set adaptive cadence, dispatch-to-stale queueing instead of the
   online-only 409, and either a Supabase Edge/RPC transport or an explicit
   paid-tier COGS decision.
2. **Dispatch idempotency (S3, P1).** `enqueueRunnerDispatch` has no
   idempotency key; double-click Run executes twice on the clinic machine.
   Required: an idempotency key on the dispatch route (+ UI single-flight).
   This library's client-side mitigation (`concurrent_run` refusal +
   `WorkflowSerialization`) stays regardless, as defense in depth.
3. **Lease renewal + reaper (E2, P1).** No renewal exists and reclaim only
   runs on the dead machine's own next poll, so a sleeping laptop leaves a
   run *running* forever and any > 15-min run goes uncertain-while-
   progressing. Required: evidence callbacks extend the active lease; a
   cron-tick reaper marks runs uncertain after `lease_expires_at + grace`;
   late terminal callbacks reconcile the dispatch row. `LeaseTracker.renew`
   already models the client half.
4. **Control-verb channel (E3, P2).** `pause`/`resume`/`approve` must ride
   the poll *response* as a separate non-leased idempotent `control[]`
   channel keyed to the active run — not FIFO queue jobs. This library
   deliberately refuses verb-in-queue.
5. **Params by reference mandatory for regulated orgs (PHI-3/S2, P0 when
   built).** Cloud-lane `params.values` are patient identifiers for the
   wedge ICP and persist in `runner_dispatches` rows. Required: the
   `{ref, expected_digest}` lane becomes mandatory server-side for regulated
   orgs, plus a local reference resolver in flow. Until then this library
   refuses ref-lane dispatches (no resolver) and lets operators refuse the
   values lane per bundle (`params_ref_required`).
6. **Server-side approval provenance (S2, P1).** At live promotion,
   `admitted_policy_name` / `unverified_write_approvals` must resolve from
   stored human approval records, not request-body fields; `approval_source`
   must reference a real approval event row.

## What the launch owner must do to go live (after the revisions)

1. Land the contract revisions above in openadapt-cloud (transport, lease,
   idempotency, params-ref, approval provenance) and re-run the
   regulated-boundary review.
2. Un-gate `/api/runners/*` (remove the mock-mode 410), routing dispatch
   through the same billing/access gates as `POST /api/runs`.
3. Only then build the daemon (natural host per the multi-substrate design:
   the desktop/tray agent, which owns the pairing credential and the
   attended UX) on top of this library, with the operator model: explicit
   `--allow-run` opt-in, never root/admin, visible status, local kill
   switch, wake-hook poll+flush.

## Operator model (library posture, enforced today)

* The trust manifest is operator-authored; nothing writes it, and nothing
  executes outside it.
* All verification happens against material already on the machine; the
  child process re-runs the entire fail-closed `run` admission gate.
* Evidence is digests/counts/step-ids only; the full-fidelity record stays in
  the local run directory.
