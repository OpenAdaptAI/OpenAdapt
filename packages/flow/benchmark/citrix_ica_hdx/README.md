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
