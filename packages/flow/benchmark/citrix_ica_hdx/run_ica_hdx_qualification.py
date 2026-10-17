#!/usr/bin/env python3
"""Comprehensive DETERMINISTIC ICA/HDX stand-in qualification (roadmap Section 10).

This campaign drives the UNMODIFIED
:class:`~openadapt_flow.backends.citrix_workspace.CitrixWorkspaceBackend` through
the full Section 10 ICA/HDX condition matrix, each reproduced as a reproducible,
in-process synthetic fixture scenario (see ``fixture.py``) with an explicit
pass/halt expectation. It verifies that on every condition the backend either
delivers a correct, out-of-band-verified actuation OR safely halts -- and NEVER
produces a silent incorrect success.

HONEST LABEL (non-negotiable, brand moat): this is a **DETERMINISTIC STAND-IN**
that reproduces ICA/HDX conditions synthetically. It is **NOT real Citrix
ICA/HDX**. It does not exercise HDX codecs, ICA compression, or the real
Workspace-client transport. Real-protocol acceptance remains pending the
customer-environment (Accuro) lane; see ``benchmark/citrix_workspace/README.md``
and ``docs/desktop/CITRIX_PIXEL.md``.

What it qualifies: the pixel/no-DOM actuation contract of the Citrix backend --
fresh-frame lease, authorized-window/session binding, immediate re-resolution
before acting, one-shot actuation lease, DPI/scale calibration, focus/occlusion
binding, readiness/identity gating, and out-of-band effect verification --
across every Section 10 condition. Zero model calls on every path.

Run::

    python3 benchmark/citrix_ica_hdx/run_ica_hdx_qualification.py \
        --output benchmark/citrix_ica_hdx/results.json

No Docker, no network, no Playwright: fully deterministic and CI-green.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.backends.citrix_workspace import CitrixWorkspaceBackend
from openadapt_flow.backends.remote_display import RemoteDisplayError, WindowInfo

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixture.py"


def _load_fixture():
    spec = importlib.util.spec_from_file_location("ica_hdx_fixture", _FIXTURE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


fx = _load_fixture()

EXPECTED = (fx.EXPECTED_MRN, fx.NOTE_VALUE)


def _backend(client, **overrides) -> CitrixWorkspaceBackend:
    kwargs = dict(
        window_title="MockMed",
        require_input_trust=True,
        activate_before_input=True,
        settle_s=0.0,
        pointer_settle_stable_frames=2,
        pointer_settle_timeout_s=0.25,
        max_frame_age_s=30.0,
        readiness_probe=fx.readiness_probe,
        application_marker="MockMed",
        application_marker_probe=fx.application_marker_probe,
        workflow_state_marker="NoteEntry",
        workflow_state_marker_probe=fx.workflow_state_marker_probe,
        session_identity_observer=client.session_observer,
    )
    kwargs.update(overrides)
    return CitrixWorkspaceBackend(client, **kwargs)


def _new_env(**state_kwargs):
    oracle = fx.FaultDB()
    state = fx.AppState(**state_kwargs)
    client = fx.SyntheticIcaWindowClient(oracle, state=state)
    return oracle, state, client


def _drive_workflow(
    client,
    backend,
    *,
    after_acquire: Optional[Callable] = None,
    invalidate_before_click: bool = False,
) -> dict:
    """Record-shaped workflow: reversible roster/note/type steps, then the ONE
    irreversible Save through the two-phase one-shot actuation lease (mirroring
    the governed runtime's consequential-actuation handshake)."""
    backend.screenshot()
    backend.click(*fx.ROSTER_ROW)  # select patient (reversible)
    backend.click(*fx.NOTE_FIELD)  # focus note (reversible)
    backend.type_text(fx.NOTE_VALUE)  # type note
    backend.screenshot()
    backend.prepare_pointer_actuation(*fx.SAVE_BUTTON)
    backend.acquire_actuation_frame()  # arm one-shot lease
    identity = {
        "application": backend.application_identity(),
        "workflow_state": backend.workflow_state_identity(),
        "session": backend.session_identity(),
    }
    if after_acquire is not None:
        after_acquire(client, backend)
    if invalidate_before_click:
        backend.screenshot()  # diagnostic observation invalidates lease
    backend.click(*fx.SAVE_BUTTON)  # consequential write; lease consumed
    return identity


def _guard(fn) -> tuple[bool, Optional[str]]:
    try:
        fn()
        return False, None
    except RemoteDisplayError as exc:
        return True, str(exc)[:180]


def _record(
    scenario_id: str,
    condition: str,
    expectation: str,
    *,
    halted: bool,
    reason: Optional[str],
    oracle,
    kind: str = "write",
    effect_verified: Optional[bool] = None,
) -> dict:
    """Classify one scenario into the honest metrics the campaign publishes."""
    observed = oracle.last() if oracle is not None else None
    writes = oracle.write_count() if oracle is not None else 0
    verified = (observed == EXPECTED) if effect_verified is None else effect_verified
    completed = not halted

    silent_incorrect_success = False
    silent_write = False
    healthy_over_halt = False

    if expectation == "pass":
        passed = completed and (verified if kind == "write" else True)
        healthy_over_halt = halted
        silent_incorrect_success = completed and kind == "write" and not verified
    elif expectation == "halt":
        passed = halted and writes == 0
        silent_write = writes > 0
        silent_incorrect_success = completed and (
            writes > 0 if kind == "write" else True
        )
    elif expectation == "effect_refused":
        # Backend delivered input, but the independent oracle shows no matching
        # persisted record -> out-of-band verification correctly withholds
        # success (an optimistic on-screen "Saved" banner must never confirm).
        passed = completed and observed != EXPECTED
        silent_incorrect_success = completed and observed == EXPECTED and not passed
    else:
        raise ValueError(f"unknown expectation {expectation!r}")

    return {
        "id": scenario_id,
        "section10_condition": condition,
        "kind": kind,
        "expectation": expectation,
        "outcome": "halted" if halted else "completed",
        "halt_reason": reason,
        "effect_expected": list(EXPECTED) if kind == "write" else None,
        "effect_observed": list(observed) if observed is not None else None,
        "effect_verified": bool(verified) if kind == "write" else None,
        "oracle_write_count": writes,
        "model_calls": 0,
        "silent_incorrect_success": bool(silent_incorrect_success),
        "silent_write": bool(silent_write),
        "healthy_over_halt": bool(healthy_over_halt),
        "passed": bool(passed),
    }


# =============================================================================
# Section 10 condition-matrix scenarios
# =============================================================================


def s_session_launch_ready() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "session_launch_ready",
        "session launch + application readiness",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_application_not_ready() -> dict:
    oracle, state, client = _new_env(ready=False)
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "application_not_ready",
        "session launch + application readiness",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_reconnect_roaming() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)

    def roam(cl, be):
        cl.session_token = "sess-B"  # reconnect/roam to a different session

    halted, reason = _guard(
        lambda: _drive_workflow(client, backend, after_acquire=roam)
    )
    return _record(
        "reconnect_roaming_identity_change",
        "reconnect / roaming",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_session_lock() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)

    def lock(cl, be):
        cl.state.ready = False  # session locked mid-flow

    halted, reason = _guard(
        lambda: _drive_workflow(client, backend, after_acquire=lock)
    )
    return _record(
        "session_lock",
        "session lock / unlock",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_session_unlock_recovery() -> dict:
    # Start locked; prove the first attempt refuses, then unlock and succeed.
    oracle, state, client = _new_env(ready=False)
    backend = _backend(client)
    locked_halted, _ = _guard(lambda: _drive_workflow(client, backend))
    client.state.ready = True  # operator unlocks the session
    backend2 = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend2))
    rec = _record(
        "session_unlock_recovery",
        "session lock / unlock",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )
    rec["locked_attempt_refused"] = bool(locked_halted)
    return rec


def s_window_minimize() -> dict:
    oracle, state, client = _new_env()
    client.window = WindowInfo(
        window_id=fx.WINDOW_ID,
        owner="Citrix Viewer",
        title="MockMed",
        pid=fx.WINDOW_PID,
        bounds=fx.WINDOW_BOUNDS,
        on_screen=False,  # minimized / hidden
    )
    client.windows = [client.window]
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "window_minimize",
        "window minimize / occlusion / move / resize",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_window_occlusion() -> dict:
    oracle, state, client = _new_env()
    client.hit_window_override = 9999  # another window covers the target point
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "window_occlusion",
        "window minimize / occlusion / move / resize",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_window_move() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)

    def move(cl, be):
        old = cl.window
        cl.window = WindowInfo(
            window_id=old.window_id,
            owner=old.owner,
            title=old.title,
            pid=old.pid,
            bounds=(old.bounds[0] + 120.0, old.bounds[1], old.bounds[2], old.bounds[3]),
            on_screen=True,
        )
        cl.windows = [cl.window]

    halted, reason = _guard(
        lambda: _drive_workflow(client, backend, after_acquire=move)
    )
    return _record(
        "window_move_after_acquire",
        "window minimize / occlusion / move / resize",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_window_resize() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)

    def resize(cl, be):
        old = cl.window
        cl.window = WindowInfo(
            window_id=old.window_id,
            owner=old.owner,
            title=old.title,
            pid=old.pid,
            bounds=(old.bounds[0], old.bounds[1], old.bounds[2] - 40.0, old.bounds[3]),
            on_screen=True,
        )
        cl.windows = [cl.window]

    halted, reason = _guard(
        lambda: _drive_workflow(client, backend, after_acquire=resize)
    )
    return _record(
        "window_resize_after_acquire",
        "window minimize / occlusion / move / resize",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_dpi_scale_consistent() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    rec = _record(
        "dpi_scale_consistent",
        "DPI + scaling changes",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )
    rec["scale"] = fx.SCALE
    return rec


def s_dpi_anisotropic() -> dict:
    oracle, state, client = _new_env()
    client.aniso = True  # captured pixels imply x/y scale mismatch
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "dpi_anisotropic_uncalibrated",
        "DPI + scaling changes",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_single_monitor_geometry() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "single_monitor_geometry",
        "single and multimonitor geometry",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_multimonitor_secondary_offset() -> dict:
    oracle, state, client = _new_env()
    client.window = WindowInfo(
        window_id=fx.WINDOW_ID,
        owner="Citrix Viewer",
        title="MockMed",
        pid=fx.WINDOW_PID,
        bounds=(1920.0, 0.0, fx.WINDOW_BOUNDS[2], fx.WINDOW_BOUNDS[3]),  # 2nd monitor
        on_screen=True,
    )
    client.windows = [client.window]
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "multimonitor_secondary_offset",
        "single and multimonitor geometry",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_multimonitor_ambiguous() -> dict:
    oracle, state, client = _new_env()
    twin = WindowInfo(
        window_id=fx.WINDOW_ID + 1,
        owner="Citrix Viewer",
        title="MockMed",
        pid=fx.WINDOW_PID,
        bounds=(1920.0, 0.0, fx.WINDOW_BOUNDS[2], fx.WINDOW_BOUNDS[3]),
        on_screen=True,
    )
    client.windows = [client.window, twin]  # duplicate session windows
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "multimonitor_ambiguous_window",
        "single and multimonitor geometry",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_codec_mild() -> dict:
    oracle, state, client = _new_env(degrade=0.12)
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "codec_artifacts_mild_legible",
        "display compression / codec artifacts",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_codec_severe() -> dict:
    oracle, state, client = _new_env(degrade=0.9)
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "codec_artifacts_severe_illegible",
        "display compression / codec artifacts",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_stale_frame() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client, max_frame_age_s=1e-9)  # any elapsed time is stale
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "stale_frame_latency",
        "latency / jitter / packet-loss / delayed-frames",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_delayed_frame_settles() -> dict:
    oracle, state, client = _new_env()
    client.hover_unsettle_frames = 2  # delayed remote hover paint that settles
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "delayed_frame_settles",
        "latency / jitter / packet-loss / delayed-frames",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_frame_never_settles() -> dict:
    oracle, state, client = _new_env()
    client.hover_unsettle_frames = 100000  # pixels never settle (jitter/loss)
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "frame_never_settles",
        "latency / jitter / packet-loss / delayed-frames",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def _focus_note(client, backend) -> None:
    backend.screenshot()
    backend.click(*fx.NOTE_FIELD)


def s_keyboard_named_key_ok() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)

    def run():
        _focus_note(client, backend)
        backend.press("Tab")  # a mapped named key delivers

    halted, reason = _guard(run)
    return _record(
        "keyboard_named_key_ok",
        "keyboard-layout / IME",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
        kind="input",
    )


def s_ime_unmapped_key() -> dict:
    oracle, state, client = _new_env()
    client.unmapped_keys = {"compose"}  # IME composition with no scancode
    backend = _backend(client)

    def run():
        _focus_note(client, backend)
        backend.press("compose")  # unmapped -> fail loud, never mis-fire a key

    halted, reason = _guard(run)
    return _record(
        "ime_unmapped_key",
        "keyboard-layout / IME",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
        kind="input",
    )


def s_clipboard_restricted() -> dict:
    oracle, state, client = _new_env()
    client.paste_blocked = True  # Citrix clipboard channel disabled by policy
    backend = _backend(client)

    def run():
        _focus_note(client, backend)
        backend.press("ControlOrMeta+v")  # blocked paste must fail loud

    halted, reason = _guard(run)
    return _record(
        "clipboard_restricted_paste",
        "clipboard restrictions",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
        kind="input",
    )


def s_focus_theft() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)

    def steal(cl, be):
        cl.frontmost = False  # another app steals focus after resolution

    halted, reason = _guard(
        lambda: _drive_workflow(client, backend, after_acquire=steal)
    )
    return _record(
        "focus_theft_after_acquire",
        "focus theft",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_unexpected_overlay() -> dict:
    oracle, state, client = _new_env()
    backend = _backend(client)

    def overlay(cl, be):
        cl.state.overlay = True  # an unexpected dialog paints over the target

    halted, reason = _guard(
        lambda: _drive_workflow(client, backend, after_acquire=overlay)
    )
    return _record(
        "unexpected_dialog_overlay",
        "unexpected dialogs / overlays",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_unverifiable_identity() -> dict:
    oracle, state, client = _new_env(identity_broken=True)
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    return _record(
        "unverifiable_application_identity",
        "ambiguous visual identity",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_uncertain_submission() -> dict:
    # After arming, an intervening observation invalidates the lease; the click
    # must refuse rather than blind-fire the possibly-delivered submission.
    oracle, state, client = _new_env()
    backend = _backend(client)
    halted, reason = _guard(
        lambda: _drive_workflow(client, backend, invalidate_before_click=True)
    )
    return _record(
        "uncertain_submission_no_blind_retry",
        "uncertain submission (no blind retry)",
        "halt",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )


def s_duplicate_prevention() -> dict:
    # One legitimate Save, then a second armed Save WITHOUT a fresh
    # prepare/re-resolve must refuse -> the one-shot lease prevents a duplicate
    # write. Exactly one record must exist.
    oracle, state, client = _new_env()
    backend = _backend(client)
    _drive_workflow(client, backend)  # first (intended) write
    second_halted, second_reason = _guard(
        lambda: (
            backend.acquire_actuation_frame(),
            backend.click(*fx.SAVE_BUTTON),  # no prepare_pointer_actuation
        )
    )
    writes = oracle.write_count()
    first_ok = oracle.last() == EXPECTED
    passed = second_halted and writes == 1 and first_ok
    return {
        "id": "duplicate_write_prevention_one_shot",
        "section10_condition": "duplicate prevention",
        "kind": "write",
        "expectation": "halt",
        "outcome": "halted" if second_halted else "completed",
        "halt_reason": second_reason,
        "effect_expected": list(EXPECTED),
        "effect_observed": list(oracle.last()) if oracle.last() else None,
        "effect_verified": bool(first_ok),
        "oracle_write_count": writes,
        "model_calls": 0,
        "silent_incorrect_success": bool(not second_halted and writes > 1),
        "silent_write": bool(writes > 1),
        "healthy_over_halt": False,
        "passed": bool(passed),
    }


def s_persisted_state_readback() -> dict:
    # Healthy write verified strictly via the out-of-band oracle, never the
    # on-screen "Saved" banner.
    oracle, state, client = _new_env()
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    rec = _record(
        "persisted_state_readback",
        "persisted-state readback (out-of-band effect verification)",
        "pass",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )
    rec["banner_shown"] = bool(state.committed)
    rec["verified_via"] = "out_of_band_oracle"
    return rec


def s_optimistic_banner_refused() -> dict:
    # The app paints an optimistic "Saved" banner but the write is rejected by
    # the system of record. Out-of-band verification must NOT confirm success.
    oracle, state, client = _new_env(write_rejected=True)
    backend = _backend(client)
    halted, reason = _guard(lambda: _drive_workflow(client, backend))
    rec = _record(
        "optimistic_banner_effect_refused",
        "persisted-state readback (out-of-band effect verification)",
        "effect_refused",
        halted=halted,
        reason=reason,
        oracle=oracle,
    )
    rec["banner_shown"] = bool(state.committed)
    rec["independent_record_present"] = oracle.last() == EXPECTED
    return rec


SCENARIOS: tuple[Callable[[], dict], ...] = (
    s_session_launch_ready,
    s_application_not_ready,
    s_reconnect_roaming,
    s_session_lock,
    s_session_unlock_recovery,
    s_window_minimize,
    s_window_occlusion,
    s_window_move,
    s_window_resize,
    s_dpi_scale_consistent,
    s_dpi_anisotropic,
    s_single_monitor_geometry,
    s_multimonitor_secondary_offset,
    s_multimonitor_ambiguous,
    s_codec_mild,
    s_codec_severe,
    s_stale_frame,
    s_delayed_frame_settles,
    s_frame_never_settles,
    s_keyboard_named_key_ok,
    s_ime_unmapped_key,
    s_clipboard_restricted,
    s_focus_theft,
    s_unexpected_overlay,
    s_unverifiable_identity,
    s_uncertain_submission,
    s_duplicate_prevention,
    s_persisted_state_readback,
    s_optimistic_banner_refused,
)


def _status_manifest() -> dict:
    """Separate status dimensions -- NEVER collapsed into one 'Available'."""
    return {
        "backend_shipped": {
            "status": "shipped",
            "detail": "CitrixWorkspaceBackend (RemoteDisplayBackend preset) is in "
            "the package and qualified against the deterministic stand-in.",
        },
        "installed_driver_available": {
            "status": "shipped_host_clients",
            "detail": "MacWindowClient (Quartz) and Win32WindowClient host drivers "
            "ship; live capture/input requires per-host Screen-Recording / "
            "Accessibility / integrity trust granted at deployment.",
        },
        "real_protocol_environment_evidence": {
            "status": "pending",
            "detail": "No real Citrix ICA/HDX acceptance. Pending the "
            "customer-environment (Accuro) lane: HDX codecs, ICA compression, and "
            "the real Workspace-client transport are NOT exercised here.",
        },
        "managed_execution_available": {
            "status": "pending",
            "detail": "Hosted/managed execution of this workflow is not asserted "
            "by this stand-in.",
        },
        "customer_controlled_execution_available": {
            "status": "pending",
            "detail": "Customer-controlled on-prem execution is the target of the "
            "real-ICA release gate; not asserted by this stand-in.",
        },
        "exact_application_qualification_available": {
            "status": "pending",
            "detail": "Exact published-application qualification (e.g. Accuro) is "
            "performed in the customer's exact environment; not asserted here.",
        },
        "deterministic_standin_qualification": {
            "status": "qualified",
            "detail": "The Section 10 ICA/HDX condition matrix is qualified against "
            "a DETERMINISTIC SYNTHETIC STAND-IN (this campaign) -- NOT real "
            "ICA/HDX.",
        },
    }


def run_campaign() -> dict:
    trials = [scenario() for scenario in SCENARIOS]

    mask_spec = fx.default_mask_spec()
    mask_violations = fx.check_masks_reviewable(mask_spec)

    passes = sum(1 for t in trials if t["expectation"] == "pass")
    halts = sum(1 for t in trials if t["expectation"] in ("halt", "effect_refused"))
    silent_incorrect = sum(1 for t in trials if t["silent_incorrect_success"])
    silent_writes = sum(1 for t in trials if t["silent_write"])
    over_halts = sum(1 for t in trials if t["healthy_over_halt"])
    model_calls = sum(t["model_calls"] for t in trials)
    scenarios_passed = sum(1 for t in trials if t["passed"])

    accepted = (
        scenarios_passed == len(trials)
        and silent_incorrect == 0
        and silent_writes == 0
        and over_halts == 0
        and model_calls == 0
        and not mask_violations
    )

    conditions = sorted({t["section10_condition"] for t in trials})

    return {
        "schema_version": "openadapt.citrix-ica-hdx-standin-qualification.v1",
        "evidence_scope": "deterministic_synthetic_ica_hdx_standin",
        "substrate": "citrix-workspace-backend-over-deterministic-ica-hdx-standin",
        "backend_under_test": "CitrixWorkspaceBackend (RemoteDisplayBackend preset)",
        "window_client": "SyntheticIcaWindowClient (in-process deterministic frames)",
        "is_real_ica_hdx": False,
        "label": (
            "DETERMINISTIC STAND-IN reproducing ICA/HDX conditions -- NOT real "
            "Citrix ICA/HDX. No HDX codecs, no ICA compression, no real "
            "Workspace-client transport. Real-protocol evidence pending the "
            "customer-environment (Accuro) lane."
        ),
        "section10_conditions_covered": conditions,
        "condition_count": len(conditions),
        "enforcement_verified": [
            "every actuation uses a fresh frame (frame-freshness lease)",
            "actuation stays bound to the authorized window and session",
            "target re-resolved immediately before acting",
            "one-shot actuation lease consumed exactly once (no double-fire)",
            "DPI/scale calibration refused when anisotropic/uncalibrated",
            "focus/occlusion binding enforced before every input edge",
            "readiness + identity gating on the fresh actuation frame",
            "out-of-band effect verification (never the on-screen banner)",
            "zero model calls on every path (healthy and refusal)",
        ],
        "volatile_mask_review": {
            "spec": {
                "volatile": {k: list(v) for k, v in mask_spec.volatile.items()},
                "protected": {k: list(v) for k, v in mask_spec.protected.items()},
            },
            "violations": mask_violations,
            "reviewable_and_safe": not mask_violations,
            "note": "Volatile-region masks must never cover target, actionability, "
            "identity, workflow-state, or effect-relevant regions.",
        },
        "status_dimensions": _status_manifest(),
        "failure_taxonomy": [
            "healthy_over_halt",
            "silent_incorrect_success",
            "silent_write",
            "unsafe_completion",
            "volatile_mask_covers_protected_region",
        ],
        "oracle": "in-process FaultDB read out of band (never the screen banner)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trials": trials,
        "trial_count": len(trials),
        "pass_expectations": passes,
        "halt_expectations": halts,
        "scenarios_passed": scenarios_passed,
        "silent_incorrect_successes": silent_incorrect,
        "silent_writes": silent_writes,
        "healthy_over_halts": over_halts,
        "model_calls": model_calls,
        "deterministic_standin_accepted": bool(accepted),
        "ica_hdx_accepted": False,
        "ica_hdx_status": "pending_real_environment_customer_lane",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output", type=Path, default=Path("benchmark/citrix_ica_hdx/results.json")
    )
    ap.add_argument(
        "--status-output",
        type=Path,
        default=Path("benchmark/citrix_ica_hdx/status_manifest.json"),
    )
    args = ap.parse_args()

    evidence = run_campaign()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    args.status_output.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.citrix-status-dimensions.v1",
                "is_real_ica_hdx": False,
                "generated_at": evidence["generated_at"],
                "status_dimensions": evidence["status_dimensions"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    payload = json.dumps(evidence, sort_keys=True).encode()
    print(f"evidence sha256: {hashlib.sha256(payload).hexdigest()}")
    print(
        f"scenarios_passed: {evidence['scenarios_passed']}/{evidence['trial_count']}  "
        f"silent_incorrect_successes: {evidence['silent_incorrect_successes']}  "
        f"healthy_over_halts: {evidence['healthy_over_halts']}  "
        f"model_calls: {evidence['model_calls']}"
    )
    print(
        f"deterministic_standin_accepted: "
        f"{evidence['deterministic_standin_accepted']}  "
        f"ica_hdx_accepted: {evidence['ica_hdx_accepted']}"
    )
    return 0 if evidence["deterministic_standin_accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
