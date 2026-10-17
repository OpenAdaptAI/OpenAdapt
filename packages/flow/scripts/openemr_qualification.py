"""Qualify, certify and run a compiled OpenEMR patient-registration bundle.

This is the OpenEMR counterpart of :mod:`scripts.export_public_demo_evidence`
(MockMed). It drives the local, made-up-patient OpenEMR fixture in
``benchmark/openemr_local`` and never calls a model:

1. copies a compiled patient-registration bundle (one irreversible
   ``#confirmCreate`` save carrying ``record_written``/``field_equals``
   effects) and opens a qualification project bound to the live OpenEMR
   browser boundary, with the independent system-of-record read-back
   (OpenEMR's REST ``patient`` API under a separate read-only OAuth client) as
   the required effect tier;
2. runs the six canonical qualification cases (representative plus the
   ambiguity, wrong-identity, stale-identity, weak-effect and missing-effect
   faults) through the real Standard-profile :class:`Replayer`, each trial
   checked by a second oracle (direct SQL on the fixture database);
3. records the signed case results and certifies the project with the
   ``clinical-write`` policy; nothing is written when certification fails;
4. optionally runs the certified bundle once in the Standard profile on a new
   made-up patient and writes the run report and timing.

Keys. The runner and fault-driver Ed25519 keys are generated for this one
campaign, held in memory, and trusted only by the qualification project they
are written into (exactly as the MockMed exporter does). They are local test
keys, never a production trust root: a bundle certified here proves the local
fixture campaign, nothing more.

Usage (fixture already up; see benchmark/openemr_local/README.md)::

    python -m scripts.openemr_qualification \
      --bundle-src path/to/compiled/bundle --out runs/openemr-qual-1 \
      --fixture-state path/to/state [--lock path/to/environment.lock.json] \
      [--ui-url http://127.0.0.1:9301 --api-url https://127.0.0.1:9300] \
      [--run-params patient.json]
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openadapt_flow import __version__ as FLOW_VERSION  # noqa: E402
from openadapt_flow.deployment import DeploymentConfig, PolicySection  # noqa: E402
from openadapt_flow.execution_profiles import (  # noqa: E402
    ExecutionProfile,
    execution_profile_contract,
)
from openadapt_flow.ir import RunReport, Workflow  # noqa: E402
from openadapt_flow.policy import load_policy  # noqa: E402
from openadapt_flow.qualification import (  # noqa: E402
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityPolicy,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    add_case,
    certify_project,
    init_project,
    qualification_campaign_id_sha256,
    qualification_run_id_sha256,
    record_case_results,
    save_qualified_workflow,
    set_action_classification,
    set_case_scope,
    set_effect_policy,
    set_identity_policy,
    set_trusted_fault_driver_key,
    set_trusted_runner_key,
    sign_case_result,
    workflow_contract_sha256,
)
from openadapt_flow.qualification_environment import (  # noqa: E402
    QualificationEnvironmentObservation,
)
from openadapt_flow.qualification_faults import (  # noqa: E402
    FaultMutationReceipt,
    QualificationFaultContext,
    QualificationFaultMutation,
    effect_verifier_input_sha256,
    sha256_bytes,
    sign_fault_mutation_receipt,
)
from openadapt_flow.report import render_run_report  # noqa: E402
from openadapt_flow.run_gate import (  # noqa: E402
    build_qualification_case_authorization,
    build_runtime_authorization,
    evaluate_run_gate,
)
from openadapt_flow.runtime import Replayer  # noqa: E402
from openadapt_flow.runtime.authorization import (  # noqa: E402
    runtime_inputs_bytes,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import RestRecordVerifier  # noqa: E402
from openadapt_flow.verification import VerificationTier  # noqa: E402

RUNNER_KEY_ID = "openemr-local-qualification-runner"
FAULT_DRIVER_KEY_ID = "openemr-local-qualification-fault-driver"
DEFAULT_CAMPAIGN_ID = "openemr-local-patient-registration-v1"
ENVIRONMENT_OBSERVER_ID = "openadapt.openemr.playwright-environment"
ENVIRONMENT_OBSERVER_CONTRACT_SHA256 = hashlib.sha256(
    b"openadapt.openemr.playwright-environment/v1"
).hexdigest()
SAVE_SELECTOR = "#confirmCreate"
PATIENT_API_PATH = "/apis/default/api/patient?_limit=1000"
SQL_FIELDS = (
    "id",
    "pid",
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)
CASES: tuple[dict[str, str], ...] = (
    {"case_id": "representative", "expected": "VERIFIED", "oracle": "exact_record"},
    {"case_id": "fault-ambiguity", "expected": "HALTED", "oracle": "no_mutation"},
    {"case_id": "fault-wrong-identity", "expected": "HALTED", "oracle": "no_mutation"},
    {"case_id": "fault-stale-identity", "expected": "HALTED", "oracle": "no_mutation"},
    {"case_id": "fault-weak-effect", "expected": "HALTED", "oracle": "no_mutation"},
    {"case_id": "fault-missing-effect", "expected": "HALTED", "oracle": "no_mutation"},
)


class QualificationError(RuntimeError):
    """The campaign refused; nothing it produced may be called certified."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def _ed25519_keypair() -> tuple[bytes, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_raw, base64.b64encode(public_raw).decode("ascii")


def patient_record_verifier(api_base_url: str, session: Any) -> RestRecordVerifier:
    """The independent read-back: OpenEMR's REST patient list, read-only client.

    The query is input-independent so the verifier is the same object shape
    for every patient; the typed effects select the record by ``lname`` and
    ``email`` and compare every declared field.
    """

    return RestRecordVerifier(
        api_base_url.rstrip("/"),
        records_path=PATIENT_API_PATH,
        records_key="data",
        session=session,
        timeout_s=10.0,
        poll_interval_s=0.2,
    )


def _is_ctrl_a(step: Any) -> bool:
    from openadapt_flow.ir import ActionKind

    if step.action is ActionKind.HOTKEY:
        return (step.key or "").lower() == "a" and [
            m.lower() for m in step.modifiers
        ] in (["control"], ["ctrl"])
    return step.action is ActionKind.KEY and (step.key or "").lower() in {
        "control+a",
        "ctrl+a",
    }


def prepare_registration_bundle(
    src: Path, dst: Path, recording_dir: Path
) -> tuple[Workflow, list[dict[str, Any]]]:
    """Author the compiled registration program up to ``clinical-write``.

    The compiler keeps the demonstration's literal gestures. Four of them leave
    steps that assert nothing, which ``clinical-write`` refuses. Each change
    below is listed in the returned notes and replaces a gesture with an
    equivalent one that carries its own check, using only the recording's own
    frames:

    - the Enter after typing an option label into a browser ``<select>``
      (role ``combobox``) is dropped: the typed label selects the option, and
      the TYPE step's read-back verifies the focused select's selected option
      label equals the typed value;
    - ``Control+a`` before typing into a field of the freshly opened form is
      dropped (the field is empty; the TYPE step's own read-back still
      verifies the field holds exactly the typed value);
    - a field click with no postcondition gets a ``REGION_STABLE`` check of the
      clicked field box from the recorded after-frame (the focused field before
      anything is typed into it; no parameter value is in that box);
    - an identity-applicable click on a static form control with no usable
      identity band (the Contact section header, the Create New Patient
      button) gets a pixel identifier crop of the control itself from the
      recorded before-frame, verified by the pixel tier of the identity ladder.

    The result is resealed; qualification then exercises it end to end.
    """

    from openadapt_flow.compiler.compile import _crop_png
    from openadapt_flow.ir import ActionKind, Postcondition, PostconditionKind
    from openadapt_flow.qualification import save_qualified_workflow
    from openadapt_flow.vision.hashing import phash_png

    if dst.exists():
        raise QualificationError(f"bundle path exists: {dst}")
    shutil.copytree(src, dst)
    workflow = Workflow.load(dst)
    frames = recording_dir / "frames"

    def frame(step: Any, phase: str) -> bytes:
        index = int(step.id.rsplit("_", 1)[1])
        return (frames / f"{index:04d}_{phase}.png").read_bytes()

    steps = list(workflow.steps)
    out: list[Any] = []
    notes: list[dict[str, Any]] = []
    dropped_enter: set[str] = set()
    i = 0
    while i < len(steps):
        step = steps[i]
        structural = step.anchor.structural if step.anchor is not None else None
        if (
            step.action is ActionKind.CLICK
            and structural is not None
            and structural.role == "combobox"
            and i + 2 < len(steps)
            and steps[i + 1].action is ActionKind.TYPE
            and steps[i + 2].action is ActionKind.KEY
            and (steps[i + 2].key or "").lower() in {"enter", "return"}
            and steps[i + 1].text
        ):
            # A browser <select> takes the typed option label directly; the
            # TYPE step's read-back then verifies the focused select's
            # selected option label equals the typed value. The Enter that
            # followed in the demonstration asserts nothing and is dropped.
            key = steps[i + 2]
            dropped_enter.add(key.id)
            notes.append(
                {
                    "change": "drop_select_commit_enter",
                    "step": key.id,
                    "selector": structural.selector,
                }
            )
        if step.id in dropped_enter:
            i += 1
            continue
        if _is_ctrl_a(step) and i > 0 and steps[i - 1].action is ActionKind.CLICK and (
            i + 1 < len(steps) and steps[i + 1].action is ActionKind.TYPE
        ):
            notes.append({"change": "drop_select_all_on_empty_field", "step": step.id})
            i += 1
            continue
        step = step.model_copy(deep=True)
        if (
            step.action is ActionKind.CLICK
            and not step.expect
            and structural is not None
            and structural.role in {"textbox", "combobox"}
            and step.anchor is not None
        ):
            after = frame(step, "after")
            region = tuple(step.anchor.region)
            rel = f"templates/{step.id}_expect.png"
            (dst / rel).write_bytes(_crop_png(after, region))
            step.expect = [
                Postcondition(
                    kind=PostconditionKind.REGION_STABLE,
                    region=region,
                    phash=phash_png(after, region=region),
                    phash_tolerance=8,
                    template=rel,
                )
            ]
            notes.append({"change": "focused_field_region_stable", "step": step.id})
        if (
            step.action is ActionKind.CLICK
            and step.anchor is not None
            and not (
                step.anchor.context_text
                or step.anchor.structured_identity
                or step.anchor.identity_template
                or step.anchor.identifier_crop
            )
            and structural is not None
            and structural.role == "button"
        ):
            before = frame(step, "before")
            region = tuple(step.anchor.region)
            rel = f"templates/identifiers/{step.id}.png"
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            (dst / rel).write_bytes(_crop_png(before, region))
            step.anchor.identifier_crop = rel
            step.anchor.identifier_region = region
            step.identity_armed = True
            step.identity_unarmed_reason = None
            step.identifier_crop_missing_reason = None
            notes.append(
                {"change": "static_control_identifier_crop", "step": step.id}
            )
        out.append(step)
        i += 1
    # OpenEMR has no idempotency column. The record_written read-back counts
    # charts bearing this run's email (a made-up, per-patient address) and
    # requires exactly one, so a second chart for the same person is refuted
    # by the read-back. This detects a duplicate; OpenEMR itself does not
    # collapse one.
    from openadapt_flow.runtime.effects import ValueExpr

    for step in out:
        for effect in step.effects:
            if effect.kind.value == "record_written" and effect.idempotency_key is None:
                effect.idempotency_key = ValueExpr(param="email")
                effect.key_field = "email"
                notes.append({"change": "email_as_duplicate_key", "step": step.id})
    workflow.steps = out
    save_qualified_workflow(workflow, dst)
    return Workflow.load(dst), notes


def save_step(workflow: Workflow) -> Any:
    """The single irreversible, effect-bound, identity-armed save step."""

    candidates = [s for s in workflow.steps if s.risk == "irreversible" and s.effects]
    if len(candidates) != 1:
        raise QualificationError(
            "expected exactly one irreversible effect-bound save, found "
            f"{[s.id for s in candidates]}"
        )
    step = candidates[0]
    # Identity may rest on structured (DOM) text or on the retained identity
    # template; the run gate and certification decide whether it is enough.
    if step.anchor is None or not step.identity_armed:
        raise QualificationError("the save step has no armed identity")
    if any(effect.needs_operator_confirmation for effect in step.effects):
        raise QualificationError("the save step has an unbound placeholder effect")
    kinds = {effect.kind.value for effect in step.effects}
    if not {"record_written", "field_equals"} <= kinds:
        raise QualificationError("the save step lacks record_written/field_equals")
    return step


def _modal_frame(page: Any) -> Any:
    """The frame that holds the duplicate-check dialog's confirm button."""

    for frame in page.frames:
        try:
            if frame.locator(SAVE_SELECTOR).count() == 1:
                return frame
        except Exception:  # noqa: BLE001 - detached frames are skipped
            continue
    raise QualificationError("OpenEMR confirm control not found for the fault")


def _environment_payload(backend: Any, application_identity: str) -> dict[str, Any]:
    page = backend.page
    browser = page.context.browser
    if browser is None:
        raise QualificationError("qualification page has no owning browser")
    return {
        "browser": "chromium",
        "browser_version": browser.version,
        "user_agent": page.evaluate("navigator.userAgent"),
        "viewport": list(backend.viewport),
        "device_scale_factor": page.evaluate("window.devicePixelRatio"),
        "headless": True,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "application": application_identity,
        "runtime_version": FLOW_VERSION,
        "target_kind": "web",
    }


class OpenEMREnvironmentObserver:
    """Observe the exact live OpenEMR browser boundary for qualification."""

    observer_id = ENVIRONMENT_OBSERVER_ID
    contract_sha256 = ENVIRONMENT_OBSERVER_CONTRACT_SHA256

    def __init__(self, *, application_version: str) -> None:
        self._application_version = application_version

    def observe(
        self,
        backend: Any,
        target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"],
    ) -> QualificationEnvironmentObservation:
        if target_kind != "web":
            raise ValueError("OpenEMR qualification requires its web boundary")
        page = backend.page
        origin = page.evaluate("location.origin")
        session_material = page.evaluate(
            """() => JSON.stringify({origin: location.origin,
                timeOrigin: performance.timeOrigin,
                userAgent: navigator.userAgent})"""
        )
        payload = _environment_payload(backend, self._application_version)
        return QualificationEnvironmentObservation(
            target_kind="web",
            application_identity=origin,
            application_version=self._application_version,
            session_identity_sha256=sha256_bytes(session_material.encode("utf-8")),
            environment_digest=sha256_bytes(_canonical_json(payload)),
        )


class _WeakEffectVerifier:
    """A configured verifier whose evidence is too weak for Standard."""

    verification_tier = VerificationTier.IMMEDIATE_SCREEN


class OpenEMRFaultDriver:
    """Fixture-owned mutations of the live OpenEMR duplicate-check dialog.

    Each mutation changes the real detector input at the gate the runtime
    selects (the ``#confirmCreate`` save); the ordinary resolver, identity
    check or effect gate must then refuse. It never returns a verdict.
    """

    driver_id = "openadapt.openemr.local-qualification-faults"
    attestation_key_id = FAULT_DRIVER_KEY_ID
    contract_sha256 = hashlib.sha256(
        b"openadapt.openemr.local-qualification-faults/v1"
    ).hexdigest()

    def __init__(self) -> None:
        self.private_key, self.public_key_base64 = _ed25519_keypair()

    @staticmethod
    def _settled_frame_sha256(context: QualificationFaultContext) -> str:
        """Hash the frame the detector reads, once the mutation has rendered.

        Uses the detector's own capture path; a frame taken mid-repaint would
        name bytes the detector never sees and the receipt would not bind.
        """

        from openadapt_flow.qualification_drills import _settled_screenshot

        capture = context.capture_frame or context.backend.screenshot
        frame = _settled_screenshot(capture)
        if frame is None:
            raise QualificationError("fault mutation never settled on screen")
        return sha256_bytes(frame)

    @staticmethod
    def mutation_bytes(kind: str) -> bytes:
        return f"openadapt.openemr.synthetic-fault/{kind}/v1\n".encode("ascii")

    def mutate(
        self, context: QualificationFaultContext
    ) -> QualificationFaultMutation | None:
        replacement = context.effect_verifier
        replace_effect_verifier = False
        page = context.backend.page
        if context.fault_kind == "ambiguity":
            _modal_frame(page).evaluate(
                """(sel) => { const b = document.querySelector(sel);
                    b.parentNode.insertBefore(b.cloneNode(true), b); }""",
                SAVE_SELECTOR,
            )
            after_sha256 = self._settled_frame_sha256(context)
        elif context.fault_kind == "wrong_identity":
            # Relabel the confirm control itself to a different action. The
            # structural selector still resolves the same unique element, so
            # only the identity check (which binds the control's own label)
            # can refuse it.
            _modal_frame(page).evaluate(
                """(sel) => { const b = document.querySelector(sel);
                    if (b.tagName === 'INPUT') b.value = 'Merge Into Existing Chart';
                    else b.textContent = 'Merge Into Existing Chart'; }""",
                SAVE_SELECTOR,
            )
            after_sha256 = self._settled_frame_sha256(context)
        elif context.fault_kind == "stale_identity":
            frame = _modal_frame(page)
            frame.evaluate(
                """() => { document.documentElement.style.background = '#ffffff';
                    document.body.replaceChildren();
                    document.body.style.background = '#ffffff'; }"""
            )
            after_sha256 = self._settled_frame_sha256(context)
        elif context.fault_kind in {"weak_effect", "missing_effect"}:
            replace_effect_verifier = True
            replacement = (
                _WeakEffectVerifier() if context.fault_kind == "weak_effect" else None
            )
            after_sha256 = effect_verifier_input_sha256(replacement, context.effects)
        else:
            return None
        mutation = self.mutation_bytes(context.fault_kind)
        receipt = FaultMutationReceipt(
            project_id=context.project_id,
            project_revision=context.project_revision,
            project_contract_sha256=context.project_contract_sha256,
            campaign_id_sha256=context.campaign_id_sha256,
            case_id_sha256=context.case_id_sha256,
            case_input_sha256=context.case_input_sha256,
            run_id_sha256=context.run_id_sha256,
            step_id_sha256=sha256_bytes(context.step_id.encode("utf-8")),
            actuation_path=context.actuation_path,
            fault_kind=context.fault_kind,
            gate=context.gate,
            driver_id=self.driver_id,
            driver_contract_sha256=self.contract_sha256,
            before_input_sha256=context.before_input_sha256,
            after_input_sha256=after_sha256,
            mutation_artifact_sha256=sha256_bytes(mutation),
            attestation_key_id=self.attestation_key_id,
        )
        return QualificationFaultMutation(
            receipt=sign_fault_mutation_receipt(receipt, private_key=self.private_key),
            replace_effect_verifier=replace_effect_verifier,
            effect_verifier=replacement,
        )


def configure_qualification(
    workflow: Workflow,
    *,
    ui_base_url: str,
    application_version: str,
    environment: dict[str, Any],
    case_params: dict[str, Any],
) -> tuple[bytes, OpenEMRFaultDriver, OpenEMREnvironmentObserver]:
    """Open the project, classify every step and bind the six cases."""

    environment_digest = sha256_bytes(_canonical_json(environment))
    boundary = EnvironmentBoundary(
        target_kind="web",
        application="OpenEMR (local fixture, made-up patients)",
        application_identity=ui_base_url.rstrip("/"),
        application_version=application_version,
        environment_observer_id=ENVIRONMENT_OBSERVER_ID,
        environment_observer_contract_sha256=ENVIRONMENT_OBSERVER_CONTRACT_SHA256,
        environment_digest=environment_digest,
        runtime_version=FLOW_VERSION,
        required_capabilities=[
            "headless_chromium",
            "independent_system_of_record",
            "playwright_dom",
        ],
    )
    project = init_project(
        workflow,
        environment=boundary,
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    for step in workflow.steps:
        irreversible = step.risk == "irreversible"
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification=(
                    ActionRiskClass.IRREVERSIBLE
                    if irreversible
                    else ActionRiskClass.READ_ONLY
                ),
                explanation=(
                    "confirms the new patient chart: a system-of-record write"
                    if irreversible
                    else "fills or navigates the new-patient form; no record effect"
                ),
                operator_confirmed=True,
            ),
        )
    save = save_step(workflow)
    set_identity_policy(
        workflow,
        IdentityPolicy(step_id=save.id, enforcement=IdentityEnforcement.CANONICAL_LADDER),
    )
    for index, _effect in enumerate(save.effects):
        set_effect_policy(
            workflow,
            step_id=save.id,
            effect_index=index,
            tier=VerificationTier.INDEPENDENT_SYSTEM,
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
            description="Recorded registration under its qualified OpenEMR boundary",
        ),
    )
    private_raw, public_b64 = _ed25519_keypair()
    set_trusted_runner_key(workflow, key_id=RUNNER_KEY_ID, public_key_base64=public_b64)
    driver = OpenEMRFaultDriver()
    set_trusted_fault_driver_key(
        workflow,
        key_id=driver.attestation_key_id,
        public_key_base64=driver.public_key_base64,
    )
    input_sha256 = runtime_inputs_digest(workflow, case_params, None)
    target = QualificationActionTarget(step_id=save.id, actuation_path="gui")
    for case in list(project.cases):
        set_case_scope(
            workflow,
            case_id=case.id,
            runtime_input_sha256=input_sha256,
            action_targets=[target],
            fault_target=(
                None if case.kind is QualificationCaseKind.REPRESENTATIVE else target
            ),
        )
    return (
        private_raw,
        driver,
        OpenEMREnvironmentObserver(application_version=application_version),
    )


def sql_patients(fixture: Any) -> list[dict[str, str]]:
    """Every patient row, read directly from the fixture database."""

    out = fixture._db(
        "SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data ORDER BY id"
    ).decode()
    return [
        {k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))}
        for line in out.splitlines()
        if line.strip()
    ]


def sql_oracle(
    fixture: Any,
    *,
    before: list[dict[str, str]],
    params: dict[str, Any],
    expected_fields: dict[str, str],
    oracle_kind: str,
    report: RunReport,
) -> dict[str, Any]:
    """Second, direct-SQL oracle; independent of the run report and the REST read."""

    after = sql_patients(fixture)
    before_ids = {row["id"] for row in before}
    new_rows = [row for row in after if row["id"] not in before_ids]
    changed_existing = [
        row for row in after if row["id"] in before_ids and row not in before
    ]
    fields = []
    row = new_rows[0] if len(new_rows) == 1 else None
    for name, want in expected_fields.items():
        got = None if row is None else row.get(name)
        fields.append({"field": name, "match": row is not None and got == want})
    if oracle_kind == "exact_record":
        passed = (
            len(new_rows) == 1
            and not changed_existing
            and all(item["match"] for item in fields)
        )
        observed = "exact_record" if passed else "record_mismatch"
    elif oracle_kind == "no_mutation":
        passed = not new_rows and not changed_existing
        observed = "no_mutation" if passed else "unexpected_mutation"
    else:
        raise QualificationError(f"unknown oracle kind {oracle_kind!r}")
    return {
        "schema_version": "openadapt.openemr-qualification-oracle/v1",
        "oracle_kind": oracle_kind,
        "read_path": "direct SQL on openemr.patient_data",
        "read_boundary": "independent from browser pixels, the REST read-back and RunReport",
        "passed": passed,
        "observed": observed,
        "patients_before": len(before),
        "patients_after": len(after),
        "new_charts": len(new_rows),
        "existing_charts_changed": len(changed_existing),
        "fields_matched": sum(item["match"] for item in fields),
        "fields_expected": len(fields),
        "field_check": fields,
        "silent_incorrect_success": bool(not passed and report.success),
    }


def expected_sql_fields(params: dict[str, Any], state_code: Optional[str]) -> dict[str, str]:
    """The patient_data columns the registration must write, from the inputs."""

    fields = {
        "title": params["title"],
        "fname": params["fname"],
        "lname": params["lname"],
        "DOB": params["DOB"],
        "sex": params["sex"],
        "street": params["street"],
        "city": params["city"],
        "postal_code": params["postal_code"],
        "phone_home": params["phone_home"],
        "email": params["email"],
        "country_code": params["country_code"],
    }
    if state_code:
        fields["state"] = state_code
    return {k: str(v) for k, v in fields.items()}


BrowserFactory = Callable[[], tuple[Any, Callable[[], None]]]


def _token_session_with_retry(fixture: Any, attempts: int = 5) -> Any:
    """The read-only OAuth session; retries only transport errors.

    Right after a fixture reset the API container can drop the first
    connection. A refused credential is never retried.
    """

    import httpx

    for attempt in range(1, attempts + 1):
        try:
            return fixture.token_session("oracle")
        except httpx.TransportError:
            if attempt == attempts:
                raise
            time.sleep(2.0 * attempt)
    raise AssertionError("unreachable")


def run_once(
    *,
    fixture: Any,
    workflow: Workflow,
    bundle_dir: Path,
    run_dir: Path,
    params: dict[str, Any],
    open_browser: BrowserFactory,
    authorization_for: Callable[[Any], Any],
    fault_driver: Optional[OpenEMRFaultDriver],
    environment_observer: OpenEMREnvironmentObserver,
    run_id: str,
    on_ready: Optional[Callable[[Any], None]] = None,
    marks: Optional[dict[str, float]] = None,
) -> RunReport:
    """One Standard-profile replay with the REST read-back as the verifier."""

    marks = marks if marks is not None else {}
    backend, close = open_browser()
    marks["browser_ready"] = time.time()
    try:
        verifier = patient_record_verifier(
            fixture.api_base_url, _token_session_with_retry(fixture)
        )
        gate_kwargs = dict(
            bundle_dir=bundle_dir,
            deployment=DeploymentConfig(policy=PolicySection(policy="clinical-write")),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
        )
        authorization = authorization_for((verifier, gate_kwargs))
        marks["admitted"] = time.time()
        if on_ready is not None:
            on_ready(backend)
        marks["task_start"] = time.time()
        report = Replayer(
            backend,
            effect_verifier=verifier,
            governed_authorization=authorization,
            qualification_fault_driver=fault_driver,
            qualification_environment_observer=environment_observer,
            durable=True,
            require_settled=True,
            # Two static form controls (the Contact section header and the
            # Create New Patient button) carry a pixel identifier crop as
            # their identity evidence; see prepare_registration_bundle.
            pixel_verify_enabled=True,
        ).run(
            workflow.model_copy(deep=True),
            params=params,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            execution_target_kind="web",
            execution_origin=fixture.ui_base_url.rstrip("/"),
            execution_entry_url=backend.page.url,
            run_id=run_id,
        )
        marks["task_end"] = time.time()
        return report
    finally:
        close()
        marks["browser_closed"] = time.time()


def _evidence_ref(root: Path, path: Path, kind: Any) -> EvidenceRef:
    return EvidenceRef(
        kind=kind, sha256=_sha256(path), relative_path=path.relative_to(root).as_posix()
    )


def case_result(
    *,
    root: Path,
    workflow: Workflow,
    case_id: str,
    observed_outcome: str,
    run_id: str,
    trial_dir: Path,
    private_key: bytes,
    case_params: dict[str, Any],
    campaign_id: str,
) -> QualificationCaseResult:
    project = workflow.qualification
    if project is None:
        raise QualificationError("workflow qualification project disappeared")
    run_dir = trial_dir / "run"
    run_artifacts = [
        _evidence_ref(root, path, "other")
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != "report.json"
    ]
    faults = (
        [
            _evidence_ref(root, trial_dir / "fault-receipt.json", "fault_receipt"),
            _evidence_ref(root, trial_dir / "fault-mutation.bin", "fault_mutation"),
        ]
        if (trial_dir / "fault-receipt.json").is_file()
        else []
    )
    result = QualificationCaseResult(
        case_id=case_id,
        project_id=project.project_id,
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        environment_digest=project.environment.environment_digest,
        runtime_version=project.environment.runtime_version,
        runner_id="openadapt-flow/openemr-local-headless",
        runner_capabilities=list(project.environment.required_capabilities),
        status="passed",
        observed_outcome=QualificationOutcome(observed_outcome.lower()),
        campaign_id_sha256=qualification_campaign_id_sha256(campaign_id),
        case_input_sha256=runtime_inputs_digest(workflow, case_params, None),
        run_id_sha256=qualification_run_id_sha256(run_id),
        evidence=[
            _evidence_ref(root, run_dir / "report.json", "run_report"),
            _evidence_ref(root, trial_dir / "case-input.json", "case_input"),
            _evidence_ref(root, trial_dir / "oracle.json", "effect"),
            *run_artifacts,
            *faults,
        ],
        detail_code=f"{case_id}.{run_id.rsplit('-', 1)[-1]}",
        attestation_key_id=RUNNER_KEY_ID,
    )
    return sign_case_result(result, private_key=private_key)


def _summary(report: RunReport) -> dict[str, Any]:
    failed = next((item for item in report.results if not item.ok), None)
    return {
        "execution_outcome": report.execution_outcome,
        "transaction_outcome": report.transaction_outcome,
        "execution_profile": report.execution_profile,
        "production_eligible": report.production_eligible,
        "success": report.success,
        "model_calls": report.model_calls,
        "total_ms": report.total_ms,
        "failed_step_id": failed.step_id if failed is not None else None,
        "failed_error": failed.error if failed is not None else None,
        "halt": report.halt.model_dump(mode="json") if report.halt else None,
    }


def qualify(
    *,
    fixture: Any,
    bundle_src: Path,
    out: Path,
    open_browser: BrowserFactory,
    trials: int,
    state_code: Optional[str],
    campaign_id: str = DEFAULT_CAMPAIGN_ID,
    log: Callable[[str], None] = print,
) -> tuple[Workflow, Path, OpenEMREnvironmentObserver]:
    """Run the six-case campaign and certify. Raises on any refusal."""

    if trials < 1:
        raise QualificationError("at least one trial per case")
    if out.exists():
        raise QualificationError(f"output already exists: {out}")
    bundle_dir = out / "bundle"
    shutil.copytree(bundle_src, bundle_dir)
    workflow = Workflow.load(bundle_dir)
    save_step(workflow)
    case_params = {k: str(v) for k, v in (workflow.params or {}).items()}
    if not case_params:
        raise QualificationError("bundle has no recorded inputs for the cases")
    application_version = "openemr:" + str(
        getattr(fixture, "source_identity", lambda: {})().get("openemr", "unknown")
    )
    backend, close = open_browser()
    try:
        environment = _environment_payload(backend, application_version)
    finally:
        close()
    private_key, driver, observer = configure_qualification(
        workflow,
        ui_base_url=fixture.ui_base_url,
        application_version=application_version,
        environment=environment,
        case_params=case_params,
    )
    save_qualified_workflow(workflow, bundle_dir)
    workflow = Workflow.load(bundle_dir)
    results: list[QualificationCaseResult] = []
    summary: list[dict[str, Any]] = []
    for case in CASES:
        qcase = next(
            item
            for item in (workflow.qualification.cases if workflow.qualification else [])
            if item.id == case["case_id"]
        )
        for trial in range(1, trials + 1):
            # Every trial starts from the verified baseline, so no trial can
            # see another trial's chart (the EMR's own duplicate check).
            fixture.reset()
            trial_dir = out / "cases" / case["case_id"] / f"trial-{trial:02d}"
            run_dir = trial_dir / "run"
            run_id = f"{campaign_id}-{case['case_id']}-trial-{trial:02d}"
            before = sql_patients(fixture)

            def authorize(arg: Any, _qcase: Any = qcase, _run_id: str = run_id) -> Any:
                _verifier, gate_kwargs = arg
                gate = evaluate_run_gate(
                    workflow, qualification_evidence_only=True, **gate_kwargs
                )
                if not gate.passed:
                    raise QualificationError(gate.render())
                return build_qualification_case_authorization(
                    workflow,
                    gate,
                    case_id=_qcase.id,
                    params=case_params,
                    worklists=None,
                    campaign_id=campaign_id,
                    run_id=_run_id,
                    fault_driver=(
                        None
                        if _qcase.kind is QualificationCaseKind.REPRESENTATIVE
                        else driver
                    ),
                )

            report = run_once(
                fixture=fixture,
                workflow=workflow,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                params=case_params,
                open_browser=open_browser,
                authorization_for=authorize,
                fault_driver=driver,
                environment_observer=observer,
                run_id=run_id,
            )
            time.sleep(1.0)
            render_run_report(run_dir)
            oracle = sql_oracle(
                fixture,
                before=before,
                params=case_params,
                expected_fields=expected_sql_fields(case_params, state_code),
                oracle_kind=case["oracle"],
                report=report,
            )
            _write_json(trial_dir / "oracle.json", oracle)
            (trial_dir / "case-input.json").write_bytes(
                runtime_inputs_bytes(workflow, case_params, None)
            )
            if report.qualification_fault_mutations:
                if len(report.qualification_fault_mutations) != 1:
                    raise QualificationError("fault case retained several receipts")
                receipt = report.qualification_fault_mutations[0]
                (trial_dir / "fault-receipt.json").write_bytes(receipt.artifact_bytes())
                (trial_dir / "fault-mutation.bin").write_bytes(
                    driver.mutation_bytes(receipt.fault_kind)
                )
            row = {
                "case_id": case["case_id"],
                "trial": trial,
                "expected": case["expected"],
                **_summary(report),
                "oracle": oracle["observed"],
            }
            summary.append(row)
            _write_json(out / "campaign-summary.json", summary)
            log(json.dumps(row, default=str))
            if report.execution_outcome != case["expected"]:
                raise QualificationError(
                    f"{case['case_id']} trial {trial}: observed "
                    f"{report.execution_outcome}, expected {case['expected']}"
                )
            if report.model_calls != 0:
                raise QualificationError("a qualification run called a model")
            if not oracle["passed"] or oracle["silent_incorrect_success"]:
                raise QualificationError(
                    f"{case['case_id']} trial {trial}: SQL oracle {oracle['observed']}"
                )
            results.append(
                case_result(
                    root=out,
                    workflow=workflow,
                    case_id=case["case_id"],
                    observed_outcome=case["expected"],
                    run_id=run_id,
                    trial_dir=trial_dir,
                    private_key=private_key,
                    case_params=case_params,
                    campaign_id=campaign_id,
                )
            )
    for result in results:
        record_case_results(workflow, [result], evidence_root=out)
    qualification = certify_project(
        workflow, policy=load_policy("clinical-write"), evidence_root=out
    )
    _write_json(out / "qualification" / "report.json", qualification.model_dump(mode="json"))
    (out / "qualification" / "verdict.txt").write_text(qualification.render() + "\n")
    log(qualification.render())
    if not qualification.passed:
        raise QualificationError("certification refused; see qualification/verdict.txt")
    save_qualified_workflow(workflow, bundle_dir)
    _write_json(
        out / "qualification" / "project.json",
        workflow.qualification.model_dump(mode="json") if workflow.qualification else {},
    )
    return Workflow.load(bundle_dir), bundle_dir, observer


def certified_run(
    *,
    fixture: Any,
    workflow: Workflow,
    bundle_dir: Path,
    out: Path,
    params: dict[str, Any],
    open_browser: BrowserFactory,
    observer: OpenEMREnvironmentObserver,
    state_code: Optional[str],
    run_id: str,
    on_ready: Optional[Callable[[Any], None]] = None,
) -> tuple[RunReport, dict[str, Any], dict[str, float]]:
    """One ordinary Standard-profile run of the certified bundle."""

    before = sql_patients(fixture)
    marks: dict[str, float] = {"start": time.time()}

    def authorize(arg: Any) -> Any:
        _verifier, gate_kwargs = arg
        gate = evaluate_run_gate(workflow, **gate_kwargs)
        _write_json(out / "run-gate.json", gate.model_dump(mode="json"))
        if not gate.passed:
            raise QualificationError(gate.render())
        return build_runtime_authorization(workflow, gate, params=params)

    report = run_once(
        fixture=fixture,
        workflow=workflow,
        bundle_dir=bundle_dir,
        run_dir=out / "run",
        params=params,
        open_browser=open_browser,
        authorization_for=authorize,
        fault_driver=None,
        environment_observer=observer,
        run_id=run_id,
        on_ready=on_ready,
        marks=marks,
    )
    time.sleep(1.0)
    oracle = sql_oracle(
        fixture,
        before=before,
        params=params,
        expected_fields=expected_sql_fields(params, state_code),
        oracle_kind="exact_record",
        report=report,
    )
    marks["oracle_done"] = time.time()
    render_run_report(out / "run")
    return report, oracle, marks
