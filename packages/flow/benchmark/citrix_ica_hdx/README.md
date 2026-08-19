# Deterministic ICA/HDX stand-in qualification (roadmap Section 10)

A **deterministic, license-free stand-in** for a real Citrix environment: a
synthetic in-process fixture that *reproduces* ICA/HDX conditions and qualifies
the pixel/no-DOM actuation contract of `CitrixWorkspaceBackend` against the full
Section 10 condition matrix.

> **HONEST LABEL (non-negotiable).** This is a **DETERMINISTIC STAND-IN** — a
> synthetic fixture that reproduces ICA/HDX *conditions*. It is **NOT real
> Citrix ICA/HDX.** It does **not** exercise HDX codecs, ICA compression, or the
> real Workspace-client transport. No result here is real-protocol acceptance.
> Real ICA/HDX evidence remains **pending the customer-environment (Accuro)
> lane** (see [`../citrix_workspace/README.md`](../citrix_workspace/README.md)
> "Real ICA/HDX release gate" and [`../../docs/desktop/CITRIX_PIXEL.md`](../../docs/desktop/CITRIX_PIXEL.md)).

## What it is

`run_ica_hdx_qualification.py` drives the **unmodified**
`openadapt_flow.backends.citrix_workspace.CitrixWorkspaceBackend` (a
`RemoteDisplayBackend` preset) through every Section 10 ICA/HDX condition, each
reproduced as a reproducible synthetic scenario in `fixture.py` with an explicit
**pass** (correct, out-of-band-verified actuation) or **halt** (safe refusal)
expectation. Only the backend's real `WindowClient` seam is synthetic
(`SyntheticIcaWindowClient`); the backend, its frame-freshness lease, DPI/scale
calibration, focus/occlusion binding, input-trust gate, one-shot actuation
lease, and readiness/identity gating are the shipping code.

It runs **fully in-process** — no Docker, no network, no Playwright — so the
entire matrix is qualified deterministically and stays green in CI
(`tests/test_ica_hdx_qualification.py`).

## Section 10 condition matrix covered

Each condition is a reproducible scenario with a pass/halt expectation:

| Condition | pass scenario(s) | halt scenario(s) |
|---|---|---|
| session launch + application readiness | `session_launch_ready` | `application_not_ready` |
| reconnect / roaming | | `reconnect_roaming_identity_change` |
| session lock / unlock | `session_unlock_recovery` | `session_lock` |
| window minimize / occlusion / move / resize | | `window_minimize`, `window_occlusion`, `window_move_after_acquire`, `window_resize_after_acquire` |
| DPI + scaling changes | `dpi_scale_consistent` | `dpi_anisotropic_uncalibrated` |
| single and multimonitor geometry | `single_monitor_geometry`, `multimonitor_secondary_offset` | `multimonitor_ambiguous_window` |
| display compression / codec artifacts | `codec_artifacts_mild_legible` | `codec_artifacts_severe_illegible` |
| latency / jitter / packet-loss / delayed-frames | `delayed_frame_settles` | `stale_frame_latency`, `frame_never_settles` |
| keyboard-layout / IME | `keyboard_named_key_ok` | `ime_unmapped_key` |
| clipboard restrictions | | `clipboard_restricted_paste` |
| focus theft | | `focus_theft_after_acquire` |
| unexpected dialogs / overlays | | `unexpected_dialog_overlay` |
| ambiguous visual identity | | `unverifiable_application_identity` |
| stale-frame detection | | `stale_frame_latency` |
| uncertain submission (no blind retry) | | `uncertain_submission_no_blind_retry` |
| duplicate prevention | | `duplicate_write_prevention_one_shot` |
| persisted-state readback (out-of-band effect verification) | `persisted_state_readback` | `optimistic_banner_effect_refused` |

## Enforcement verified on every scenario

- every actuation uses a **fresh frame** (frame-freshness lease);
- actuation stays **bound to the authorized window and session**;
- the target is **re-resolved immediately before acting**;
- a **one-shot actuation lease** is consumed exactly once (no double-fire);
- DPI/scale is **refused when anisotropic/uncalibrated**;
- **focus/occlusion binding** is enforced before every input edge;
- **readiness + identity** are gated on the fresh actuation frame;
- effect verification is **out of band** (an independent record, never the
  on-screen "Saved" banner);
- **zero model calls** on every path — healthy and refusal.

## Reviewable volatile-region masks

`fixture.py` declares a reviewable `VolatileMaskSpec`: which regions are volatile
(may be masked from continuity comparison — here only the remote clock chrome)
and which are **protected** and must never be masked (**target, actionability,
identity, workflow-state, effect-relevant** regions). `check_masks_reviewable()`
proves no volatile mask overlaps a protected region; the campaign fails if it
does, and `tests/test_ica_hdx_qualification.py` covers both the safe default and
a rejected bad spec. The backend's default remains conservative full-frame
decoded-RGB continuity; any real relaxation is a reviewed
application/environment artifact, never a permissive global default.

## Separate status dimensions (never one "Available")

`status_manifest.json` publishes each dimension separately:

- `backend_shipped` — **shipped** (qualified against this stand-in);
- `installed_driver_available` — **shipped host clients** (Mac/Win drivers;
  live capture/input needs per-host trust at deployment);
- `real_protocol_environment_evidence` — **pending** (no real ICA/HDX);
- `managed_execution_available` — **pending**;
- `customer_controlled_execution_available` — **pending**;
- `exact_application_qualification_available` — **pending**;
- `deterministic_standin_qualification` — **qualified** (this campaign, a
  stand-in, NOT real ICA/HDX).

## What this proves — and does NOT prove

**Proves:** the Citrix backend's pixel/no-DOM actuation contract holds across the
whole Section 10 ICA/HDX condition matrix — on every condition it either
delivers a correct, independently verified actuation or safely halts, with zero
silent incorrect successes, zero healthy over-halts, and zero model calls.

**Does NOT prove:** anything about real Citrix ICA/HDX. There are no HDX codecs,
no ICA compression, no real Workspace-client transport, and no exact
published-application (e.g. Accuro) qualification here. The synthetic
"compression/codec artifacts" are a PIL-generated degradation, not an HDX/
Thinwire bitstream. Real-protocol acceptance is the separate customer-environment
release gate.

## Run

```bash
python3 benchmark/citrix_ica_hdx/run_ica_hdx_qualification.py \
    --output benchmark/citrix_ica_hdx/results.json \
    --status-output benchmark/citrix_ica_hdx/status_manifest.json
# deterministic, ~2s; exits non-zero unless all scenarios pass with
# 0 silent-incorrect-successes, 0 over-halts, 0 model calls.

python3 -m pytest tests/test_ica_hdx_qualification.py -q
```

## Real ICA/HDX acceptance preflight

`run_real_acceptance.py` is the public, cost-safe campaign mechanism. It has no
infrastructure lifecycle operation. It invokes one digest-pinned executable
only after a retained customer approval binds that executable and principal to
a pre-existing Citrix session. That executable can actuate the bounded workflow
inside its approved authority. A private configuration supplies the exact
fingerprints and independent system-of-record oracle. Do not place an Accuro
recipe, credentials, identifiers, screenshots, or customer data in this repo.

The single deliberate command is:

```bash
python3 benchmark/citrix_ica_hdx/run_real_acceptance.py \
  --config /secure/customer-boundary/citrix-acceptance.json \
  --trust-roots /customer-managed/trust/citrix-acceptance-roots.json \
  --nonce-registry /customer-managed/state/citrix-acceptance-nonces \
  --output /secure/customer-boundary/citrix-acceptance-report.json \
  --execute
```

Without `--execute`, the command only validates the campaign contract and
writes a preflight report. The configuration must give complete structured
fingerprints for Workspace, ICA/HDX, the application, session, display, runner,
bundle, verifier, collector, and environment. Every one of the eight conditions
requires at least three trials and a fixed expected outcome.

The trust-root file is separate from the campaign configuration. Its customer,
upgrade, collector, and oracle Ed25519 public keys must all be distinct.
Signatures under the customer and upgrade keys validate each approval and
executable attestation. The harness hashes the executable files itself. It
rejects a symbolic link or world-writable trust-root or executable file. It
also rechecks each executable and its mode immediately before use.

The customer-signed runner approval binds the campaign nonce. The nonce
registry is a durable customer-controlled directory independent of the report
path. On first use, the harness binds one nonce to one configuration, trust-root
file, absolute report path, hash-chained journal, fallback report, and generated
execution challenge. A second use with another report path or configuration is
rejected. A second use of the same binding recovers or returns its terminal
state and never dispatches the trial again.

The harness generates an unpredictable execution challenge for the campaign
and a new observation challenge for each oracle and collector call. Each signed
observation must return both challenges and a signed `observed_at` timestamp.
The harness accepts the observation challenge once and rejects a stale, future,
or replayed observation. A signed independent collector also binds its current
OS observations of the runner, oracle, and collector principals and executable
digests to the configuration, trial, session, transport, and exact display.
The display observation includes the viewport, DPI, scale, window mode, and a
monitor-topology digest. A resize or monitor change outside the configured
trial condition refuses before dispatch.

Before each dispatch, a separately authenticated read-only oracle must report a
signed `REFUTED` baseline for the exact trial, entity, and effect. The collector
must then return fresh signed native ICA/HDX diagnostic evidence. The harness
writes these bindings to an exclusive hash-chained journal and calls `fsync`
before it invokes the runner.

Before any dispatch, the harness creates and synchronizes the nonce binding,
journal, primary report, and registry fallback report. It closes each journal
write before it continues. If a process stops after a durable
`DISPATCH_ATTEMPT`, the next use verifies the complete hash chain and records
`HALTED_UNCERTAIN`, zero retries, and required reconciliation. It does not
dispatch again. A stop before dispatch recovers as `HALTED` and
`not_dispatched`.

After dispatch, every runner, receipt, or oracle error becomes
`HALTED_UNCERTAIN`. The harness records zero retries, requires reconciliation,
retains the available evidence, writes a terminal report, and stops the
campaign. It also stops at the first failed safety, identity, or effect trial.

The runner receipt supplies delivery metadata. It cannot supply the trial
outcome. The harness derives the outcome only from validated oracle evidence
and the fixed condition contract. It emits `VERIFIED` only for a healthy trial
whose complete oracle checks pass. A trial with `passed: false` is always
`HALTED` or `HALTED_UNCERTAIN`; it is never `VERIFIED`.

The runner receipt must also report zero retries and zero model calls. The
terminal report includes explicit counts for every condition, verified and halt
outcomes, silent incorrect success, over-halt, retries, and model calls.

A commit timeout has the fixed result `HALTED_UNCERTAIN`. The runner reports
uncertain delivery, zero retries, and required independent reconciliation. The
oracle must independently confirm or refute the effect before the counted trial
can pass. An indeterminate result stops the campaign. The campaign never changes
the runtime result to `VERIFIED` and never retries the possibly delivered
operation.
