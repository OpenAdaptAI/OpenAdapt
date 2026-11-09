#!/usr/bin/env python3
"""Run the first real-RDP multi-window vision qualification campaign.

The harness uses the production Recorder, compiler, FreeRDPBackend, resolver,
run gate, authorization, Replayer, and effect-verifier adapters.  The fixture
is synthetic.  Pixels and input still cross a real FreeRDP round trip.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import secrets
import shutil
import signal
import sqlite3
import statistics
import tempfile
import time
import traceback
from contextlib import contextmanager
from difflib import SequenceMatcher
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageEnhance, ImageOps

from benchmark.multiapp_common import CsvRecordVerifier, SurfaceRoutedVerifier
from benchmark.rdp_ladder.run_rdp_ladder_qualification import (
    DockerX11RdpTransport,
)

VIEWPORT = (1280, 800)
TARGET_REQUEST = "REQ-LIVE-2048"
TARGET_RECORD = "REC-2048"
TARGET_NAME = "Jordan Lee"
APPLICATION_IDENTITY = "oa-rdp-fixture"
APPLICATION_VERSION = "oa-fixture-v1"
ENVIRONMENT_MARKER = "oa-rdp-env"
SLOT_PARAM = "appointment_slot"
TYPE_PARAM = "appointment_type"
REQUEST_PARAM = "request_id"
DEMO_PARAMS = {
    SLOT_PARAM: "2026-08-12 09:30",
    TYPE_PARAM: "Cardiology follow-up",
    REQUEST_PARAM: TARGET_REQUEST,
}
REPLAY_PARAMS = {
    SLOT_PARAM: "2026-08-14 10:45",
    TYPE_PARAM: "Intake consultation",
    REQUEST_PARAM: TARGET_REQUEST,
}
TRIALS = 3
TRIAL_DEADLINE_S = 10 * 60

# Stable synthetic-fixture geometry. Runtime replay resolves visual anchors
# from the recording; these points are used only to make the demonstration.
INBOX_REQUEST = (380, 170)
WORKLIST_TAB = (555, 770)
SCHEDULER_TAB = (745, 770)
INBOX_TAB = (365, 770)
WORKLIST_VIEW = (1110, 147)
WORKLIST_TARGET_AFTER_SCROLL = (430, 490)
MARK_SCHEDULED = (1110, 217)
TARGET_RECORD_ROW = (250, 155)
WRONG_RECORD_ROW = (250, 223)
SLOT_FIELD = (720, 222)
TYPE_FIELD = (720, 342)
REQUEST_FIELD = (720, 462)
SAVE_APPOINTMENT = (650, 568)
SAVE_APPOINTMENT_REGION = (520, 540, 260, 56)
SEND_CONFIRMATION = (165, 447)

# Identity bands are recorded pixels, not live values sent out of the runner.
ACTIVE_RECORD_REGION = (510, 86, 740, 60)
WORKLIST_SELECTION_REGION = (35, 596, 920, 58)
INBOX_DETAIL_REGION = (35, 238, 915, 150)
INBOX_HEADER_REGION = (35, 20, 930, 82)
INBOX_REQUEST_REGION = (35, 123, 890, 100)
WORKLIST_HEADER_REGION = (35, 20, 930, 82)
WORKLIST_TARGET_REGION = (35, 455, 920, 70)
TARGET_RECORD_REGION = (35, 123, 430, 68)

POLICY_PATH = Path(__file__).with_name("policy.yaml")


class CampaignTrialDeadlineExceeded(BaseException):
    """Stop one non-terminating trial without accepting its outcome."""


@contextmanager
def _trial_watchdog(timeout_s: float):
    """Bound one campaign trial on the Linux hosted acceptance runner."""

    if timeout_s <= 0:
        raise ValueError("campaign trial deadline must be positive")
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        raise RuntimeError("the RDP campaign requires a POSIX trial watchdog")

    def raise_deadline(_signum, _frame) -> None:
        raise CampaignTrialDeadlineExceeded(
            f"RDP campaign trial exceeded {timeout_s:g} seconds"
        )

    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    if 0 < previous_timer[0] <= timeout_s:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        raise RuntimeError("an earlier process deadline is already active")
    signal.signal(signal.SIGALRM, raise_deadline)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            remaining = max(0.001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


class MultiappRdpTransport(DockerX11RdpTransport):
    """Expose the live isolated FreeRDP client as a session-bound transport.

    The fixture still verifies application, version, and environment from
    rendered pixels. The client-window identifier is a stronger session signal
    than OCR of a changing fixture label: it changes when the local RDP client
    is replaced and it never contains application data.
    """

    def session_identity(self) -> str:
        window_id = self._client_window_id()
        payload = (
            f"openadapt.rdp-multiapp-session.v1\\0{self._c}\\0"
            f"{self._display}\\0{window_id}"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class DisplayDriftTransport(MultiappRdpTransport):
    """Inject rendered drift after a real RDP decode, before resolution.

    The fixture and the input channel remain unchanged. This models the
    customer-controlled viewer receiving a changed theme, scale, or lossy
    remote frame. It is not a claim about a specific Citrix codec.
    """

    def __init__(self, container: str) -> None:
        super().__init__(container)
        self.display_drift: Optional[str] = None
        self._drift_frames = 0

    def framebuffer(self):
        image, width, height = super().framebuffer()
        mode = self.display_drift
        if mode is None:
            return image, width, height
        if mode == "moderate_display_drift":
            # A bounded scale/compression/theme change that keeps text legible.
            image = image.resize(
                (max(1, int(width * 0.92)), max(1, int(height * 0.92))),
                Image.Resampling.BILINEAR,
            ).resize((width, height), Image.Resampling.BILINEAR)
            image = ImageEnhance.Contrast(image).enhance(0.88)
            encoded = io.BytesIO()
            image.save(encoded, format="JPEG", quality=72)
            image = Image.open(io.BytesIO(encoded.getvalue())).convert("RGB")
        elif mode == "severe_display_drift":
            # A heavy scale/theme/compression fault removes reliable target
            # detail. The visual ladder must halt before any effect.
            image = image.resize(
                (max(1, int(width * 0.35)), max(1, int(height * 0.35))),
                Image.Resampling.BILINEAR,
            ).resize((width, height), Image.Resampling.BILINEAR)
            image = ImageOps.invert(image)
            encoded = io.BytesIO()
            image.save(encoded, format="JPEG", quality=8)
            image = Image.open(io.BytesIO(encoded.getvalue())).convert("RGB")
        else:
            raise ValueError(f"unknown display drift mode: {mode}")
        self._drift_frames += 1
        return image, width, height

    def display_drift_diagnostic(self) -> dict[str, Any]:
        return {
            "mode": self.display_drift,
            "transformed_frames": self._drift_frames,
            "source": "simulated-rendered-drift-on-real-rdp-session",
        }


def _export_failed_step_frames(
    *,
    artifact_root: Path,
    run_dir: Path,
    failed_step_ids: list[str],
) -> list[dict[str, str]]:
    """Export only the first failed step's exact before and after frames."""

    if not failed_step_ids:
        return []
    step_id = failed_step_ids[0]
    # Runtime pseudo-steps (for example ``<authorization>``) carry typed
    # refusal evidence but never have a captured action frame. Preserve the
    # typed result and record that no media exists. Do not manufacture a frame
    # or let export hide the original qualification result.
    if step_id.startswith("<") and step_id.endswith(">"):
        return [
            {
                "kind": "failed_step_frame_unavailable",
                "step_id": step_id,
                "reason": "runtime_pseudo_step_has_no_retained_frame",
            }
        ]
    if Path(step_id).name != step_id or step_id in {"", ".", ".."}:
        raise ValueError(f"unsafe failed step id: {step_id!r}")

    artifact_root = artifact_root.resolve()
    run_root = run_dir.resolve()
    destination = artifact_root / "failure" / run_dir.name
    destination.mkdir(parents=True, exist_ok=True)
    destination.resolve().relative_to(artifact_root)

    exported: list[dict[str, str]] = []
    for phase in ("before", "after"):
        source = run_dir / "steps" / f"{step_id}_{phase}.png"
        if not source.exists():
            exported.append(
                {
                    "kind": "failed_step_frame_unavailable",
                    "step_id": step_id,
                    "phase": phase,
                    "reason": "retained_frame_not_found",
                }
            )
            continue
        if source.is_symlink():
            raise ValueError(f"refusing linked failure frame: {source}")
        resolved_source = source.resolve(strict=True)
        resolved_source.relative_to(run_root)
        if not resolved_source.is_file():
            raise ValueError(f"failure frame is not a file: {source}")

        target = destination / source.name
        shutil.copyfile(resolved_source, target)
        exported.append(
            {
                "kind": f"failed_step_{phase}_frame",
                "path": target.relative_to(artifact_root).as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    return exported


def _install_commit_then_timeout_fault(
    backend: Any,
    *,
    condition: str,
    save_region: tuple[int, int, int, int],
) -> dict[str, Any]:
    """Lose the receipt only after the real qualified Save click returns.

    The wrapper does not simulate the write. It lets the RDP backend send the
    guarded pointer action to the fixture first. It then reports typed delivery
    uncertainty to the runtime. The runtime must not send the click again.
    """

    state: dict[str, Any] = {"injected": False, "save_delivery_calls": 0}
    original_click_guarded = backend.click_guarded

    def click_guarded(*args: Any, **kwargs: Any):
        from openadapt_flow.backend import ActionDeliveryUncertain

        receipt = original_click_guarded(*args, **kwargs)
        x = int(args[0] if args else kwargs["x"])
        y = int(args[1] if len(args) > 1 else kwargs["y"])
        left, top, width, height = save_region
        is_save_attempt = left <= x < left + width and top <= y < top + height
        if condition == "commit_then_timeout" and is_save_attempt:
            state["save_delivery_calls"] += 1
            if not state["injected"]:
                state["injected"] = True
                raise ActionDeliveryUncertain(
                    operation="rdp_click",
                    native=False,
                    target_fingerprint=receipt.target_fingerprint,
                    cause_type="TimeoutError",
                )
        return receipt

    backend.click_guarded = click_guarded
    return state


def _read_ack(root: Path) -> Optional[str]:
    try:
        value = (root / "reset_ack.txt").read_text().strip()
        return value or None
    except OSError:
        return None


def _read_fault_ack(root: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads((root / "fault_ack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _read_database(root: Path) -> Optional[list[dict[str, str]]]:
    path = root / "appointments.sqlite3"
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT appointment_id, request_id, record_id, entity_name, "
            "appointment_slot, appointment_type, status FROM appointments"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()


def _read_worklist(root: Path) -> Optional[list[dict[str, str]]]:
    try:
        with (root / "worklist.csv").open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def _read_mail(root: Path) -> Optional[list[dict[str, str]]]:
    mail_root = root / "outbox"
    if not mail_root.is_dir():
        return None
    records: list[dict[str, str]] = []
    try:
        for folder in (mail_root / "new", mail_root / "cur"):
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if not path.is_file():
                    continue
                message = BytesParser(policy=email_policy.default).parsebytes(
                    path.read_bytes()
                )
                records.append(
                    {
                        "file": path.name,
                        "to": str(message.get("To", "")),
                        "subject": str(message.get("Subject", "")),
                        "request_id": str(message.get("X-Request-ID", "")),
                    }
                )
    except (OSError, ValueError):
        return None
    return records


def _read_input_ledger(root: Path) -> Optional[list[dict[str, Any]]]:
    try:
        return [
            json.loads(line)
            for line in (root / "input-ledger.jsonl").read_text().splitlines()
            if line
        ]
    except (OSError, ValueError, TypeError):
        return None


def _reset(_container: str, root: Path, scenario: str = "healthy") -> dict[str, Any]:
    token = secrets.token_hex(16)
    root.mkdir(parents=True, exist_ok=True)
    (root / "control.json").write_text(
        json.dumps({"reset_token": token, "scenario": scenario}) + "\n",
        encoding="utf-8",
    )
    deadline = time.monotonic() + 10
    diagnostics: dict[str, Any] = {}
    while time.monotonic() < deadline:
        after = _read_ack(root)
        acknowledged = after == token
        rows = _read_database(root)
        worklist = _read_worklist(root)
        mail = _read_mail(root)
        input_ledger = _read_input_ledger(root)
        fault_ack = _read_fault_ack(root)
        diagnostics = {
            "ack_expected": token,
            "ack_after": after,
            "acknowledged": acknowledged,
            "database": rows,
            "worklist_rows": None if worklist is None else len(worklist),
            "mail": mail,
            "input_ledger": input_ledger,
            "fault_ack": fault_ack,
            "root_entries": (
                sorted(path.name for path in root.iterdir()) if root.is_dir() else None
            ),
        }
        if (
            acknowledged
            and rows == []
            and worklist
            and mail == []
            and input_ledger == []
            and fault_ack is not None
            and fault_ack.get("reset_token") == token
            and fault_ack.get("scenario") == scenario
        ):
            return fault_ack
        time.sleep(0.1)
    raise RuntimeError(
        "fixture reset did not produce clean persisted state: "
        + json.dumps(diagnostics, sort_keys=True, default=str)
    )


def _arm_partial_render(root: Path) -> dict[str, Any]:
    """Arm the latched incomplete frame without changing the reset token."""

    token = secrets.token_hex(16)
    try:
        control = json.loads((root / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "fixture control is unavailable before partial render"
        ) from exc
    if not isinstance(control, dict) or not isinstance(control.get("reset_token"), str):
        raise RuntimeError("fixture control has no reset token before partial render")
    control.update({"fault": "partial_render", "fault_token": token})
    (root / "control.json").write_text(
        json.dumps(control, sort_keys=True) + "\n", encoding="utf-8"
    )
    deadline = time.monotonic() + 10
    latest: Optional[dict[str, Any]] = None
    while time.monotonic() < deadline:
        latest = _read_fault_ack(root)
        if (
            latest is not None
            and latest.get("reset_token") == control["reset_token"]
            and latest.get("scenario") == "healthy"
            and latest.get("fault") == "partial_render"
            and latest.get("fault_token") == token
            and latest.get("scheduler_ready_visible") is False
            and latest.get("active_identity_visible") is False
            and latest.get("save_control_count") == 1
            and latest.get("identity_surface") == "loading_skeleton"
        ):
            return latest
        time.sleep(0.05)
    raise RuntimeError(
        "fixture did not acknowledge latched partial render: "
        + json.dumps(latest, sort_keys=True, default=str)
    )


def _record(backend: Any, recording_dir: Path) -> None:
    from openadapt_flow.recorder import Recorder

    recorder = Recorder(
        backend,
        recording_dir,
        settle_interval_s=0.3,
        settle_stable_frames=2,
        settle_timeout_s=6.0,
    )
    recorder.click(*INBOX_REQUEST)
    recorder.click(*WORKLIST_TAB)
    recorder.click(*WORKLIST_VIEW)
    recorder.scroll(0, 800)
    recorder.click(*WORKLIST_TARGET_AFTER_SCROLL)
    recorder.click(*SCHEDULER_TAB)
    recorder.click(*TARGET_RECORD_ROW)
    recorder.click(*SLOT_FIELD)
    recorder.type_text(DEMO_PARAMS[SLOT_PARAM], param=SLOT_PARAM)
    recorder.click(*TYPE_FIELD)
    recorder.type_text(DEMO_PARAMS[TYPE_PARAM], param=TYPE_PARAM)
    recorder.click(*REQUEST_FIELD)
    recorder.type_text(DEMO_PARAMS[REQUEST_PARAM], param=REQUEST_PARAM)
    recorder.click(*SAVE_APPOINTMENT)
    recorder.click(*WORKLIST_TAB)
    recorder.click(*WORKLIST_VIEW)
    recorder.scroll(0, 800)
    recorder.click(*WORKLIST_TARGET_AFTER_SCROLL)
    recorder.click(*MARK_SCHEDULED)
    recorder.click(*INBOX_TAB)
    recorder.click(*INBOX_REQUEST)
    recorder.click(*SEND_CONFIRMATION)
    recorder.finish()


def _arm_recording(recording_dir: Path) -> None:
    path = recording_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    expected = {
        0: INBOX_REQUEST_REGION,
        1: INBOX_HEADER_REGION,
        2: WORKLIST_HEADER_REGION,
        4: WORKLIST_TARGET_REGION,
        5: WORKLIST_SELECTION_REGION,
        6: TARGET_RECORD_REGION,
        7: ACTIVE_RECORD_REGION,
        9: ACTIVE_RECORD_REGION,
        11: ACTIVE_RECORD_REGION,
        13: ACTIVE_RECORD_REGION,
        14: ACTIVE_RECORD_REGION,
        15: WORKLIST_HEADER_REGION,
        17: WORKLIST_TARGET_REGION,
        18: WORKLIST_SELECTION_REGION,
        19: WORKLIST_SELECTION_REGION,
        20: INBOX_REQUEST_REGION,
        21: INBOX_DETAIL_REGION,
    }
    for index, region in expected.items():
        if index >= len(events) or events[index].get("kind") != "click":
            raise RuntimeError(f"recorded event {index} is not the expected click")
        events[index]["identifier_region"] = list(region)
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _build_verifier(root: Path) -> SurfaceRoutedVerifier:
    from openadapt_flow.runtime.effects import (
        MaildirDeliveryVerifier,
        SqlRecordVerifier,
    )

    database = root / "appointments.sqlite3"
    return SurfaceRoutedVerifier(
        {
            "appointments": SqlRecordVerifier(
                lambda: sqlite3.connect(f"file:{database}?mode=ro", uri=True),
                "SELECT appointment_id, request_id, record_id, entity_name, "
                "appointment_slot, appointment_type, status FROM appointments",
            ),
            "worklist": CsvRecordVerifier(root / "worklist.csv"),
            "outbox": MaildirDeliveryVerifier(
                str(root / "outbox"),
                content_probe=r"Jordan Lee is scheduled",
                poll_interval_s=0.05,
            ),
        },
        default_surface="appointments",
    )


def _qualify(workflow: Any, bundle_dir: Path, root: Path):
    from openadapt_flow import __version__
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import execution_profile_contract
    from openadapt_flow.ir import Postcondition, PostconditionKind, Workflow
    from openadapt_flow.qualification import (
        ActionRiskClass,
        ActionRiskClassification,
        EnvironmentBoundary,
        QualificationActionTarget,
        QualificationCase,
        QualificationCaseKind,
        QualificationOutcome,
        add_case,
        init_project,
        set_action_classification,
        set_case_scope,
        set_effect_policy,
    )
    from openadapt_flow.qualification_environment import (
        BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256,
    )
    from openadapt_flow.run_gate import evaluate_run_gate, runtime_inputs_digest
    from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
    from openadapt_flow.verification import VerificationTier

    if len(workflow.steps) != 22:
        raise RuntimeError(
            f"expected 22 compiled actions from the demonstration, got {len(workflow.steps)}"
        )
    save, reconcile, send = workflow.steps[13], workflow.steps[18], workflow.steps[21]
    for step, label in ((save, "save"), (reconcile, "reconcile"), (send, "send")):
        step.risk = "irreversible"
        step.risk_explanation = f"qualified {label} write"
        step.risk_review_required = False
    save.expect = [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Appointment saved")
    ]
    save.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "request_id": ValueExpr(param=REQUEST_PARAM),
                "record_id": ValueExpr(literal=TARGET_RECORD),
                "appointment_slot": ValueExpr(param=SLOT_PARAM),
                "appointment_type": ValueExpr(param=TYPE_PARAM),
                "status": ValueExpr(literal="scheduled"),
            },
            expected_count=1,
            count_new_only=True,
            key_field="request_id",
            idempotency_key=ValueExpr(param=REQUEST_PARAM),
            risk="irreversible",
            probe="surface=appointments|read-only exact appointment lookup",
        )
    ]
    reconcile.expect = [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT, text=f"Reconciled {TARGET_REQUEST}"
        )
    ]
    reconcile.effects = [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"request_id": ValueExpr(param=REQUEST_PARAM)},
            field="status",
            value=ValueExpr(literal="Scheduled"),
            idempotency_key=ValueExpr(param=REQUEST_PARAM),
            key_field="request_id",
            risk="irreversible",
            probe="surface=worklist|CSV row re-read",
        )
    ]
    send.expect = [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Confirmation queued")
    ]
    send.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "to": ValueExpr(literal="referrals@example.test"),
                "subject": ValueExpr(literal=f"Scheduled {TARGET_REQUEST}"),
                "content_match": ValueExpr(literal="True"),
            },
            expected_count=1,
            count_new_only=True,
            idempotency_key=ValueExpr(literal=f"Scheduled {TARGET_REQUEST}"),
            key_field="subject",
            risk="irreversible",
            probe="surface=outbox|Maildir delivery capture",
        )
    ]

    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="rdp",
            application="RDP multi-window synthetic suite",
            application_identity=APPLICATION_IDENTITY,
            application_version=APPLICATION_VERSION,
            environment_observer_id=(
                "backend:openadapt_flow.backends.rdp_backend.FreeRDPBackend"
            ),
            environment_observer_contract_sha256=(
                BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256
            ),
            environment_digest=hashlib.sha256(
                f"rdp-environment-v1\0{ENVIRONMENT_MARKER}".encode("utf-8")
            ).hexdigest(),
            runtime_version=__version__,
            required_capabilities=[
                "vision-only-resolution",
                "window-switching",
                "read-only-sql-effect",
                "csv-effect",
                "maildir-effect",
            ],
        ),
    )
    for step in workflow.steps:
        classification = (
            ActionRiskClass.IRREVERSIBLE
            if step.id in {save.id, reconcile.id, send.id}
            else ActionRiskClass.READ_ONLY
        )
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification=classification,
                explanation=(
                    "Qualified persisted business write"
                    if classification is ActionRiskClass.IRREVERSIBLE
                    else "Navigation, focus, data entry, or scrolling before a qualified write"
                ),
                operator_confirmed=True,
            ),
        )

    # Standard qualification binds the exact required verification tier for
    # every consequential effect. Each reference verifier reads an independent
    # persisted system surface (SQLite, CSV, or Maildir), so Tier 1 is correct.
    for step in (save, reconcile, send):
        set_effect_policy(
            workflow,
            step_id=step.id,
            effect_index=0,
            tier=VerificationTier.INDEPENDENT_SYSTEM,
        )

    # The campaign executes from an exact qualification case authority, not a
    # generic production authorization. Its scope binds the representative
    # input digest and all three consequential GUI writes; each individual
    # trial receives its own immutable run identifier below.
    case_id = "rdp-multiapp-representative"
    case_targets = [
        QualificationActionTarget(step_id=step.id, actuation_path="gui")
        for step in (save, reconcile, send)
    ]
    add_case(
        workflow,
        QualificationCase(
            id=case_id,
            kind=QualificationCaseKind.REPRESENTATIVE,
            description="Real-RDP multi-window representative execution",
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    set_case_scope(
        workflow,
        case_id=case_id,
        runtime_input_sha256=runtime_inputs_digest(workflow, REPLAY_PARAMS, None),
        action_targets=case_targets,
    )

    bundle_key = secrets.token_urlsafe(32)
    checkpoint_key = secrets.token_urlsafe(32)
    workflow.save(bundle_dir, encrypt=True, key=bundle_key)
    workflow = Workflow.load(bundle_dir, key=bundle_key)
    verifier = _build_verifier(root)
    gate = evaluate_run_gate(
        workflow,
        bundle_dir=bundle_dir,
        deployment=DeploymentConfig(policy=PolicySection(policy=str(POLICY_PATH))),
        effect_verifier=verifier,
        policy_source=str(POLICY_PATH),
        strict_templates=True,
        require_encryption=True,
        # This is a qualification campaign, not a production deployment.  It
        # nevertheless exercises the Standard runtime contract so every trial
        # receives the runtime's exact outcome and transaction classification.
        profile_contract=execution_profile_contract("standard"),
        effective_durable=True,
        effective_require_settled=True,
        qualification_evidence_only=True,
    )
    if not gate.passed:
        raise RuntimeError(gate.render())
    return (
        workflow,
        verifier,
        gate,
        checkpoint_key,
        case_id,
        {"save": save.id, "reconcile": reconcile.id, "send": send.id},
    )


def _oracle_result(root: Path, params: dict[str, str]) -> dict[str, Any]:
    database = _read_database(root)
    worklist = _read_worklist(root)
    mail = _read_mail(root)
    input_ledger = _read_input_ledger(root)
    expected_db = {
        "request_id": TARGET_REQUEST,
        "record_id": TARGET_RECORD,
        "entity_name": TARGET_NAME,
        "appointment_slot": params[SLOT_PARAM],
        "appointment_type": params[TYPE_PARAM],
        "status": "scheduled",
    }
    db_ok = bool(
        database is not None
        and len(database) == 1
        and all(database[0].get(key) == value for key, value in expected_db.items())
    )
    target_rows = [
        row for row in worklist or [] if row.get("request_id") == TARGET_REQUEST
    ]
    adjacent_unchanged = bool(
        worklist is not None
        and all(
            row.get("status") == "New"
            for row in worklist
            if row.get("request_id") != TARGET_REQUEST
        )
    )
    csv_ok = (
        len(target_rows) == 1
        and target_rows[0].get("status") == "Scheduled"
        and adjacent_unchanged
    )
    worklist_unchanged = bool(
        worklist is not None
        and len(worklist) == 19
        and len(target_rows) == 1
        and all(row.get("status") == "New" for row in worklist)
    )
    mail_ok = bool(
        mail is not None
        and len(mail) == 1
        and mail[0].get("to") == "referrals@example.test"
        and mail[0].get("subject") == f"Scheduled {TARGET_REQUEST}"
        and mail[0].get("request_id") == TARGET_REQUEST
    )
    expected_input_counts = {
        "save_appointment": 1,
        "mark_scheduled": 1,
        "send_confirmation": 1,
    }
    input_counts = {
        action: sum(1 for entry in input_ledger or [] if entry.get("action") == action)
        for action in expected_input_counts
    }
    input_cardinality_ok = bool(
        input_ledger is not None and input_counts == expected_input_counts
    )
    wrong_record_input = bool(
        input_ledger is not None
        and any(
            entry.get("action") == "save_appointment"
            and entry.get("active_record_id") != TARGET_RECORD
            for entry in input_ledger
        )
    )
    return {
        "database_ok": db_ok,
        "worklist_ok": csv_ok,
        "mail_ok": mail_ok,
        "all_effects_ok": db_ok and csv_ok and mail_ok and input_cardinality_ok,
        "database": database,
        "worklist_target": target_rows,
        "mail": mail,
        "adjacent_rows_unchanged": adjacent_unchanged,
        "worklist_unchanged": worklist_unchanged,
        "input_ledger": input_ledger,
        "input_counts": input_counts,
        "input_cardinality_ok": input_cardinality_ok,
        "no_consequential_input": not any(input_counts.values()),
        "wrong_record_write": bool(
            wrong_record_input
            or (
                database is not None
                and any(
                    row.get("request_id") != TARGET_REQUEST
                    or row.get("record_id") != TARGET_RECORD
                    for row in database
                )
            )
        ),
        "duplicate_effect": bool(
            (database is not None and len(database) > 1)
            or len(target_rows) > 1
            or (mail is not None and len(mail) > 1)
            or any(count > 1 for count in input_counts.values())
        ),
    }


def _campaign_coverage(
    conditions: tuple[str, ...],
    trials: list[dict[str, Any]],
    *,
    required_trials: int,
) -> dict[str, Any]:
    """Derive campaign completion from retained per-condition results."""

    counts = {
        condition: sum(trial.get("condition") == condition for trial in trials)
        for condition in conditions
    }
    executed = [condition for condition in conditions if counts[condition] > 0]
    pending = [
        condition for condition in conditions if counts[condition] < required_trials
    ]
    return {
        "configured_conditions": list(conditions),
        "implemented_conditions": executed,
        "condition_trial_counts": counts,
        "full_campaign_complete": not pending,
        "full_campaign_pending_conditions": pending,
    }


def _compile_campaign_recording(
    compile_recording: Any,
    recording_dir: Path,
    bundle_dir: Path,
) -> Any:
    """Compile the fixture into its exact external-RDP execution boundary."""

    workflow = compile_recording(
        recording_dir,
        bundle_dir,
        name="rdp-multiapp-vision",
        target_surface="rdp",
    )
    if workflow.surface != "rdp" or workflow.execution_mode != "external":
        raise RuntimeError(
            "RDP campaign compilation did not retain its external surface binding"
        )
    return workflow


def _deadline_trial(
    *,
    root: Path,
    condition: str,
    runtime_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    """Retain a fail-closed result when one trial exceeds its deadline."""

    oracle = _oracle_result(root, dict(REPLAY_PARAMS))
    return {
        "condition": condition,
        "passed": False,
        "runtime_s": round(runtime_s, 3),
        "runtime_success": False,
        "model_calls": 0,
        "fault_injected": False,
        "fault_ack": {},
        "reset_ack": {},
        "pointer_delivery": {},
        "display_drift": {},
        "environment_preflight": {},
        "exact_fault_evidence": False,
        "typed_target_refusal": False,
        "relevant_partial_refusal": False,
        "safety_refusal_evidence": [],
        "commit_timeout_injected": False,
        "save_delivery_calls": int(oracle["input_counts"].get("save_appointment", 0)),
        # The watchdog can fire after an RDP input edge crossed the transport
        # but before the fixture processed or logged it. Ledger absence cannot
        # prove non-delivery, so every deadline requires reconciliation.
        "transaction_outcome": "RECONCILIATION_REQUIRED",
        "safe_halt": False,
        "silent_incorrect_success": False,
        "over_halt": condition in {"healthy", "row_reordered"},
        "oracle": oracle,
        "rung_counts": {},
        "errors": [f"campaign trial exceeded {timeout_s:g} seconds"],
        "failed_step_ids": [],
        "step_diagnostics": [],
        "trial_deadline_exceeded": True,
    }


def _campaign_result(
    conditions: tuple[str, ...],
    trials: list[dict[str, Any]],
    *,
    stopped_early: bool,
) -> dict[str, Any]:
    """Build one complete or resumable campaign result from retained trials."""

    coverage = _campaign_coverage(conditions, trials, required_trials=TRIALS)
    runtimes = sorted(float(trial["runtime_s"]) for trial in trials)

    def nearest_rank(percentile: float) -> Optional[float]:
        if not runtimes:
            return None
        index = max(
            0,
            min(len(runtimes) - 1, int(len(runtimes) * percentile + 0.999999) - 1),
        )
        return runtimes[index]

    p95 = nearest_rank(0.95)
    return {
        "schema_version": "openadapt.rdp-multiapp-results.v1",
        "campaign_contract": "benchmark/rdp_multiapp/campaign.json",
        **coverage,
        "run_count": len(trials),
        "stopped_early": stopped_early,
        # A checkpoint with passing work so far is not acceptance evidence.
        "accepted_subset": bool(
            coverage["full_campaign_complete"]
            and not stopped_early
            and all(trial["passed"] for trial in trials)
        ),
        "verified_outcomes": sum(
            bool(trial["runtime_success"] and trial["oracle"]["all_effects_ok"])
            for trial in trials
        ),
        "safe_halts": sum(bool(trial["safe_halt"]) for trial in trials),
        "silent_incorrect_successes": sum(
            bool(trial["silent_incorrect_success"]) for trial in trials
        ),
        "over_halts": sum(bool(trial["over_halt"]) for trial in trials),
        "wrong_record_writes": sum(
            bool(trial["oracle"]["wrong_record_write"]) for trial in trials
        ),
        "duplicate_effects": sum(
            bool(trial["oracle"]["duplicate_effect"]) for trial in trials
        ),
        "model_calls": sum(int(trial["model_calls"]) for trial in trials),
        "p50_runtime_s": (round(statistics.median(runtimes), 3) if runtimes else None),
        "p95_runtime_s": round(p95, 3) if p95 is not None else None,
        "trials": trials,
    }


def _write_campaign_result(out: Path, result: dict[str, Any]) -> None:
    """Atomically retain the latest completed campaign trial."""

    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, out)


def _retain_harness_failure(out: Path, exc: Exception) -> dict[str, Any]:
    """Add a bounded harness failure without discarding prior trial evidence."""

    extracted = traceback.extract_tb(exc.__traceback__)
    failure_frame = extracted[-1] if extracted else None
    try:
        result = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        conditions = tuple(
            condition["id"]
            for condition in json.loads(
                Path(__file__).with_name("campaign.json").read_text(encoding="utf-8")
            )["conditions"]
        )
        result = _campaign_result(conditions, [], stopped_early=True)
    result["accepted_subset"] = False
    result["stopped_early"] = True
    result["harness_failure"] = {
        "exception_type": type(exc).__name__,
        "stage": "campaign_execution",
        "source": (
            f"{Path(failure_frame.filename).name}:{failure_frame.lineno}"
            if failure_frame is not None
            else None
        ),
        "function": failure_frame.name if failure_frame is not None else None,
    }
    _write_campaign_result(out, result)
    return result


def _run_once(
    *,
    container: str,
    root: Path,
    workflow: Any,
    verifier: Any,
    gate: Any,
    bundle_dir: Path,
    checkpoint_key: str,
    qualification_case_id: str,
    run_dir: Path,
    condition: str,
    save_pointer_acquisition: int,
    save_step_id: str,
) -> dict[str, Any]:
    from openadapt_flow.backends.rdp_backend import FreeRDPBackend
    from openadapt_flow.run_gate import build_qualification_case_authorization
    from openadapt_flow.runtime.replayer import Replayer

    fixture_scenarios = {"row_reordered", "duplicate_save_control"}
    reset_ack = _reset(
        container,
        root,
        condition if condition in fixture_scenarios else "healthy",
    )
    fault_ack = reset_ack
    params = dict(REPLAY_PARAMS)
    authorization = build_qualification_case_authorization(
        workflow,
        gate,
        case_id=qualification_case_id,
        params=params,
        worklists=None,
        campaign_id="rdp-multiapp-vision-v1",
        run_id=run_dir.name,
    )
    transport = DisplayDriftTransport(container)
    environment_probe_text: dict[str, list[str]] = {}
    environment_ocr_cache: dict[str, list[str]] = {}

    def environment_marker_visible(marker: str, png: bytes) -> bool:
        """Read a fixture's PHI-free marker from its stable top-right band."""

        from openadapt_flow import vision

        # This dedicated fixture region contains only static, PHI-free
        # environment labels. One exact frame supplies all three probes. Do
        # not run OCR again for each marker: repeated OCR can consume the
        # bounded actuation-frame lease before any input edge.
        digest = hashlib.sha256(png).hexdigest()
        texts = environment_ocr_cache.get(digest)
        if texts is None:
            texts = [
                line.text.strip().casefold()
                for line in vision.ocr(png, region=(960, 0, 320, 72))
                if line.text.strip()
            ]
            environment_ocr_cache.clear()
            environment_ocr_cache[digest] = texts
        environment_probe_text[marker] = list(texts)
        candidate = marker.strip().casefold()
        return any(
            SequenceMatcher(None, text, candidate).ratio() >= 0.9 for text in texts
        )

    backend = FreeRDPBackend(
        transport,
        connect=True,
        # OCR identity checks can take longer on a shared CI runner. The
        # backend still captures and compares exact canonical pixels again at
        # the last common point before input, so this only bounds how long the
        # resolver can work; it does not permit input on changed content.
        max_frame_age_s=30.0,
        application_marker=APPLICATION_IDENTITY,
        application_marker_probe=lambda png: environment_marker_visible(
            APPLICATION_IDENTITY, png
        ),
        application_version_marker=APPLICATION_VERSION,
        application_version_marker_probe=lambda png: environment_marker_visible(
            APPLICATION_VERSION, png
        ),
        environment_marker=ENVIRONMENT_MARKER,
        environment_marker_probe=lambda png: environment_marker_visible(
            ENVIRONMENT_MARKER, png
        ),
    )
    # FreeRDP can expose its client window before the first complete remote
    # paint arrives. Wait for one complete, atomic environment observation
    # before Replayer starts. This loop only captures frames; it sends no
    # keyboard or pointer input. The run still refuses if the environment does
    # not become complete within the bounded startup window.
    environment_identity = None
    environment_deadline = time.monotonic() + 5.0
    while environment_identity is None:
        environment_identity = backend.qualification_environment_identity()
        if environment_identity is not None or time.monotonic() >= environment_deadline:
            break
        time.sleep(0.25)

    # These are only PHI-free qualification boundary signals. They make an
    # environment refusal diagnosable without exporting a screen image or an
    # application record.
    environment_preflight = {
        "application_identity": (
            environment_identity[0] if environment_identity is not None else None
        ),
        "application_version": (
            environment_identity[1] if environment_identity is not None else None
        ),
        "session_identity_present": environment_identity is not None,
        "qualification_environment_present": environment_identity is not None,
        "marker_probe_text": environment_probe_text,
    }
    if condition in {"moderate_display_drift", "severe_display_drift"}:
        # Qualify the known environment first, then alter only the decoded
        # observation stream used by the real RDP runner. This proves that a
        # display change during execution cannot silently reuse old geometry.
        transport.display_drift = condition
    original_acquire = backend.acquire_actuation_frame
    acquisitions = 0
    injected = False

    def acquire_with_fault() -> bytes:
        nonlocal acquisitions, fault_ack, injected
        acquisitions += 1
        if acquisitions == save_pointer_acquisition:
            if condition == "wrong_record_before_write":
                transport.pointer(*WRONG_RECORD_ROW, "left", True)
                transport.pointer(*WRONG_RECORD_ROW, "left", False)
                time.sleep(0.45)
                injected = True
            elif condition == "focus_theft_before_write":
                transport.pointer(*INBOX_TAB, "left", True)
                transport.pointer(*INBOX_TAB, "left", False)
                time.sleep(0.45)
                injected = True
            elif condition == "partial_render":
                # This runs after the form fields are populated and immediately
                # before the qualified Save fresh-frame acquisition. The fixture
                # polling loop only removes visual regions; it never resets data.
                fault_ack = _arm_partial_render(root)
                injected = True
        return original_acquire()

    backend.acquire_actuation_frame = acquire_with_fault  # type: ignore[method-assign]
    commit_timeout = _install_commit_then_timeout_fault(
        backend,
        condition=condition,
        save_region=SAVE_APPOINTMENT_REGION,
    )
    started = time.monotonic()
    try:
        report = Replayer(
            backend,
            poll_interval_s=0.3,
            effect_verifier=verifier,
            governed_authorization=authorization,
            pixel_verify_enabled=True,
            durable=True,
            require_settled=True,
            checkpoint_key=checkpoint_key,
        ).run(
            workflow,
            params=params,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            idempotency_key=f"{condition}-{run_dir.name}",
            execution_target_kind="rdp",
        )
    finally:
        # A trial creates a fresh backend binding. Always release it before the
        # next trial, including when the watchdog interrupts a blocked run.
        backend.close()
    oracle = _oracle_result(root, params)
    expected_halt = condition in {
        "wrong_record_before_write",
        "focus_theft_before_write",
        "duplicate_save_control",
        "partial_render",
        "severe_display_drift",
    }
    errors = [result.error for result in report.results if result.error]
    refusal_evidence = [
        result.safety_refusal_evidence
        for result in report.results
        if result.safety_refusal_evidence is not None
    ]
    typed_target_refusal = any(
        evidence.code == "target_ambiguous"
        and evidence.stage in {"target_resolution", "actuation_revalidation"}
        for evidence in refusal_evidence
    )
    relevant_partial_refusal = any(
        result.safety_halt
        and result.failure_category in {"governed_refusal", "safety_halt"}
        and result.safety_refusal_evidence is not None
        and result.safety_refusal_evidence.stage == "identity_verification"
        and result.safety_refusal_evidence.code
        in {"identity_conflict", "identity_unverifiable"}
        and result.delivery_attempted is False
        for result in report.results
    )
    exact_fault_evidence = {
        "duplicate_save_control": bool(
            fault_ack.get("scenario") == condition
            and fault_ack.get("save_control_count") == 2
            and typed_target_refusal
        ),
        "partial_render": bool(
            fault_ack.get("scenario") == "healthy"
            and fault_ack.get("fault") == "partial_render"
            and isinstance(fault_ack.get("fault_token"), str)
            and fault_ack.get("scheduler_ready_visible") is False
            and fault_ack.get("active_identity_visible") is False
            and fault_ack.get("save_control_count") == 1
            and fault_ack.get("identity_surface") == "loading_skeleton"
            and relevant_partial_refusal
        ),
    }.get(condition, True)
    safe_halt = bool(
        expected_halt
        and not report.success
        and not oracle["database"]
        and oracle["worklist_unchanged"]
        and oracle["mail"] == []
        and oracle["no_consequential_input"]
    )
    drift_safe_halt = bool(
        condition in {"moderate_display_drift", "severe_display_drift"}
        and not report.success
        and not oracle["database"]
        and oracle["worklist_unchanged"]
        and oracle["mail"] == []
        and oracle["no_consequential_input"]
        and transport.display_drift_diagnostic()["transformed_frames"] > 0
    )
    if condition == "moderate_display_drift":
        passed = bool(drift_safe_halt or (report.success and oracle["all_effects_ok"]))
    elif condition == "severe_display_drift":
        passed = drift_safe_halt
    elif expected_halt:
        injected_fault = (
            injected
            if condition in {"wrong_record_before_write", "focus_theft_before_write"}
            else exact_fault_evidence
        )
        passed = bool(injected_fault and safe_halt)
    elif condition == "commit_then_timeout":
        save_result = next(
            (result for result in report.results if result.step_id == save_step_id),
            None,
        )
        uncertainty = (
            save_result.delivery_uncertainty if save_result is not None else None
        )
        sqlite_proved_one_write = bool(
            oracle["database_ok"]
            and oracle["input_counts"].get("save_appointment") == 1
            and not oracle["duplicate_effect"]
        )
        verified_after_uncertainty = bool(
            report.success
            and report.transaction_outcome == "VERIFIED"
            and sqlite_proved_one_write
            and uncertainty is not None
            and uncertainty.retried is False
            and uncertainty.effects_confirmed is True
            and uncertainty.resolved_by_contract is True
        )
        reconciliation_required = bool(
            not report.success
            and not sqlite_proved_one_write
            and report.transaction_outcome == "RECONCILIATION_REQUIRED"
        )
        passed = bool(
            commit_timeout["injected"]
            and commit_timeout["save_delivery_calls"] == 1
            and oracle["input_counts"].get("save_appointment") == 1
            and (verified_after_uncertainty or reconciliation_required)
        )
    else:
        passed = bool(report.success and oracle["all_effects_ok"])
    step_diagnostics = [
        {
            "step_id": result.step_id,
            "ok": result.ok,
            "failure_category": result.failure_category,
            "safety_halt": result.safety_halt,
            "delivery_attempted": result.delivery_attempted,
            "actuation": result.actuation,
            "resolution_rung": (
                result.resolution.rung if result.resolution is not None else None
            ),
            "resolution_point": (
                list(result.resolution.point) if result.resolution is not None else None
            ),
            "error": result.error,
            "delivery_uncertainty": (
                result.delivery_uncertainty.model_dump(
                    mode="json",
                    exclude={"observed_at"},
                )
                if result.delivery_uncertainty is not None
                else None
            ),
            "safety_refusal_evidence": (
                result.safety_refusal_evidence.model_dump(mode="json")
                if result.safety_refusal_evidence is not None
                else None
            ),
        }
        for result in report.results
        if (
            result.error is not None
            or result.delivery_uncertainty is not None
            or result.safety_refusal_evidence is not None
        )
    ]
    return {
        "condition": condition,
        "passed": passed,
        "runtime_s": round(time.monotonic() - started, 3),
        "runtime_success": bool(report.success),
        "model_calls": int(report.model_calls),
        "fault_injected": bool(
            injected
            or commit_timeout["injected"]
            or (
                condition in {"duplicate_save_control", "partial_render"}
                and exact_fault_evidence
            )
            or transport.display_drift_diagnostic()["transformed_frames"] > 0
        ),
        "fault_ack": fault_ack,
        "reset_ack": reset_ack,
        "pointer_delivery": transport.pointer_delivery_diagnostic(),
        "display_drift": transport.display_drift_diagnostic(),
        "environment_preflight": environment_preflight,
        "exact_fault_evidence": exact_fault_evidence,
        "typed_target_refusal": typed_target_refusal,
        "relevant_partial_refusal": relevant_partial_refusal,
        "safety_refusal_evidence": [
            evidence.model_dump(mode="json") for evidence in refusal_evidence
        ],
        "commit_timeout_injected": bool(commit_timeout["injected"]),
        "save_delivery_calls": int(commit_timeout["save_delivery_calls"]),
        "transaction_outcome": (
            report.transaction_outcome if condition == "commit_then_timeout" else None
        ),
        "safe_halt": safe_halt or drift_safe_halt,
        "silent_incorrect_success": bool(
            report.success and not oracle["all_effects_ok"]
        ),
        "over_halt": bool(
            condition in {"healthy", "row_reordered"} and not report.success
        ),
        "oracle": oracle,
        "rung_counts": dict(report.rung_counts),
        "errors": errors,
        "failed_step_ids": [
            result.step_id for result in report.results if not result.ok
        ],
        "step_diagnostics": step_diagnostics,
    }


def run(container: str, root: Path, out: Path, work: Path) -> dict[str, Any]:
    from openadapt_flow.backends.rdp_backend import FreeRDPBackend
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind

    root.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    _reset(container, root)
    record_backend = FreeRDPBackend(DockerX11RdpTransport(container), connect=True)
    recording_dir = work / "recording"
    bundle_dir = work / "bundle"
    _record(record_backend, recording_dir)
    _arm_recording(recording_dir)
    compiled = _compile_campaign_recording(
        compile_recording,
        recording_dir,
        bundle_dir,
    )
    workflow, verifier, gate, checkpoint_key, qualification_case_id, step_ids = (
        _qualify(compiled, bundle_dir, root)
    )
    pointer_actions = {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
        ActionKind.DRAG,
    }
    save_index = next(
        i for i, step in enumerate(workflow.steps) if step.id == step_ids["save"]
    )
    save_pointer_acquisition = sum(
        step.action in pointer_actions for step in workflow.steps[: save_index + 1]
    )
    conditions = (
        "healthy",
        "row_reordered",
        "wrong_record_before_write",
        "focus_theft_before_write",
        "duplicate_save_control",
        "partial_render",
        "commit_then_timeout",
        "moderate_display_drift",
        "severe_display_drift",
    )
    trials: list[dict[str, Any]] = []
    stopped_early = False
    _write_campaign_result(
        out,
        _campaign_result(conditions, trials, stopped_early=False),
    )
    for condition in conditions:
        for index in range(1, TRIALS + 1):
            trial_run_dir = work / f"run-{condition}-{index}"
            trial_started = time.monotonic()
            try:
                with _trial_watchdog(TRIAL_DEADLINE_S):
                    trial = _run_once(
                        container=container,
                        root=root,
                        workflow=workflow,
                        verifier=verifier,
                        gate=gate,
                        bundle_dir=bundle_dir,
                        checkpoint_key=checkpoint_key,
                        qualification_case_id=qualification_case_id,
                        run_dir=trial_run_dir,
                        condition=condition,
                        save_pointer_acquisition=save_pointer_acquisition,
                        save_step_id=step_ids["save"],
                    )
            except CampaignTrialDeadlineExceeded:
                trial = _deadline_trial(
                    root=root,
                    condition=condition,
                    runtime_s=time.monotonic() - trial_started,
                    timeout_s=TRIAL_DEADLINE_S,
                )
            if not trial["passed"]:
                trial["failure_artifacts"] = _export_failed_step_frames(
                    artifact_root=out.parent,
                    run_dir=trial_run_dir,
                    failed_step_ids=trial["failed_step_ids"],
                )
            trials.append(trial)
            if not trial["passed"]:
                stopped_early = True
            checkpoint = _campaign_result(
                conditions,
                trials,
                stopped_early=stopped_early,
            )
            _write_campaign_result(out, checkpoint)
            print(
                json.dumps(
                    {
                        "campaign_progress": {
                            "condition": condition,
                            "trial": index,
                            "passed": bool(trial["passed"]),
                            "runtime_s": trial["runtime_s"],
                            "run_count": len(trials),
                        }
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if stopped_early:
                break
        if stopped_early:
            break
    result = _campaign_result(
        conditions,
        trials,
        stopped_early=stopped_early,
    )
    _write_campaign_result(out, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="oaflow-rdp-multiapp")
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark/rdp_multiapp/results.json")
    )
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="oaflow-rdp-multiapp-"))
    try:
        result = run(args.container, args.oracle_root.resolve(), args.output, work)
    except Exception as exc:  # noqa: BLE001 - retain bounded harness failure
        traceback.print_exc()
        result = _retain_harness_failure(args.output, exc)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["accepted_subset"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
