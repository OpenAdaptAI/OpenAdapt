"""Replayer: execute a compiled Workflow against a Backend.

Per step: settle, screenshot, resolve the anchor via the resolution ladder,
enforce the irreversible-step risk gate, verify the resolved target's
IDENTITY against the anchor's recorded context band (never click a
positional look-alike — see ``runtime.identity``), act through the Backend
(TYPE actions additionally verify the input visibly landed, with one
refocus-and-retype retry), settle again, and poll postconditions until they
pass or time out (with one re-settle retry). Postcondition failure is
semantic drift: the run halts, naming the step and embedding its
before/after screenshots in the report.

Steps that declare system-of-record ``effects`` (``ir.Step.effects``) get a
SECOND, independent verification the screen oracle cannot provide: an
``EffectVerifier`` (``runtime.effects``) snapshots the real system of record
BEFORE the action and, after the action's screen postconditions pass, rules
each declared ``Effect`` CONFIRMED / REFUTED / INDETERMINATE against that
record (an API/DB read, never the pixels). A non-CONFIRMED verdict HALTS the
run — mirroring the identity gate's refuse-rather-than-guess posture — and for
an irreversible effect a configured compensator may ``reconcile_or_escalate``
(RECONCILED continues, ESCALATE halts). A step that declares effects with NO
verifier configured is a deployment error and HALTS unless the fail-closed
``run`` gate supplies an exact, bundle-bound approval for that GUI step. An
approved write remains visibly unverified and still must pass its screen
postconditions; direct API writes always require an independent verifier. This
path makes ZERO model calls — effect verification reads the system of record.

Steps that succeed via any rung other than ``template`` produce a governed
repair. An ungoverned local run can apply the refreshed anchor in memory. An
exactly authorized run records a promotable candidate without changing its
admitted program. Repair evidence is written under
``run_dir/heals/<step_id>/``; an explicit later lifecycle can qualify and
promote a new bundle.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import struct
import time
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    Optional,
    TypeVar,
    cast,
)
from urllib.parse import urlsplit

from openadapt_flow.backend import (
    ActionDeliveryUncertain,
    Backend,
    BrowserPresentationGeometryBackend,
    FocusedElementActuationLeaseBackend,
    FreshActuationReacquisitionBackend,
    FreshActuationRequired,
    GuardedCoordinateActionBackend,
    GuardedDragActionBackend,
    GuardedKeyboardActionBackend,
    GuardedRemotePointerActionBackend,
    GuardedSelectOptionBackend,
    PreparedPointerActuationBackend,
    RemoteActuationBackend,
    RemoteFrameContractBackend,
    RichPointerActionBackend,
    SelectOptionBackend,
    StructuralResolutionRefused,
)
from openadapt_flow.bundle_validation import compute_parameter_schema_digest
from openadapt_flow.identity_signals import (
    normalize_signal_text,
    parameterize_identity_text,
)
from openadapt_flow.ir import (
    ActionDeliveryUncertainty,
    ActionKind,
    Anchor,
    ApiBinding,
    BusinessDecisionEvidence,
    EffectVerificationEvidence,
    ExecutionTargetKind,
    FreshActuationEvent,
    HaltObservation,
    IdentityCheck,
    IdentitySignalEvidence,
    Interstitial,
    InterstitialActionResult,
    LoopSpec,
    PixelIdentityEvidence,
    Point,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramExceptionEvidence,
    ProgramExecutionScopeFrame,
    ProgramGraph,
    ProgramGuardAssetEvidence,
    ProgramTransitionEvidence,
    Region,
    Resolution,
    RunReport,
    SafetyRefusalEvidence,
    State,
    StateKind,
    Step,
    StepResult,
    UnarmedStep,
    VisualResolutionEvidence,
    Workflow,
    predicate_contract_sha256,
)
from openadapt_flow.privacy import scrub_image_bytes as _scrub_png
from openadapt_flow.privacy import scrub_text as _scrub_phi
from openadapt_flow.qualification_environment import (
    BackendQualificationEnvironmentObserver,
    QualificationEnvironmentObservation,
    QualificationEnvironmentObserver,
)
from openadapt_flow.qualification_faults import (
    FaultMutationReceipt,
    QualificationFaultContext,
    QualificationFaultDriver,
    QualificationFaultGate,
    QualificationFaultKind,
    effect_verifier_input_sha256,
    sha256_bytes,
    verify_fault_mutation_receipt,
)
from openadapt_flow.run_gate import is_consequential
from openadapt_flow.runtime import heal as heal_mod
from openadapt_flow.runtime import healing as healing_mod
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    RuntimeParamScalar,
    runtime_inputs_digest,
    runtime_param_text,
    runtime_params_for_gui,
)
from openadapt_flow.runtime.durable.approval import StateDiverged
from openadapt_flow.runtime.durable.program_checkpoint import (
    TOP_GRAPH_ID,
    GraphFrame,
    LoopCursor,
    ProgramCheckpoint,
    ProgramTransitionReceipt,
    control_frames_hash,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    bundle_version as _bundle_version,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    history_hash as _history_hash,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectState,
    EffectVerifier,
    reconcile_or_escalate,
)
from openadapt_flow.runtime.effects.adapter import (
    _probe_adapter_connection,
)
from openadapt_flow.runtime.effects.adapter import (
    verifier_identity as effect_verifier_identity,
)
from openadapt_flow.runtime.program_predicates import (
    evaluate_program_predicate,
    exact_png_size,
    predicate_template_refs,
    predicate_uses_frame,
    program_predicate_evaluator_contract_sha256,
)
from openadapt_flow.runtime.resolver import (
    is_below_ocr,
    pad_region,
    resolve,
    visual_resolution_anchor_contract_sha256,
    visual_resolution_evaluator_contract_sha256,
)
from openadapt_flow.verification import (
    EffectContractMutationError,
    verifier_effect_tier,
    verify_effect_without_mutation,
)
from openadapt_flow.vision.ocr import OcrResolutionRefused

_DeliveryResultT = TypeVar("_DeliveryResultT")
_MAX_FRESH_ACTUATION_REACQUISITIONS = 2
# Private module capability.  The public ``run`` API cannot mint a durable
# continuation.  ``runtime.durable.resume`` imports this object only after it
# has validated the pause, approval, bundle, checkpoint, and live state.
_DURABLE_RESUME_AUTHORITY = object()

if TYPE_CHECKING:
    from openadapt_flow.production_qualification import ProductionQualificationGuard
    from openadapt_flow.qualification_campaign_authority import (
        QualificationCampaignGuard,
    )
    from openadapt_flow.runtime.control_overlay import RuntimeControlOverlayEmitter
    from openadapt_flow.transaction import IdempotencyLedger

# REGION_STABLE template check: how far the expected content may shift from
# the recorded region (real apps re-layout by a few pixels between runs),
# and the minimum template-match score to accept it.
PC_TEMPLATE_SEARCH_PAD = 80


def _frame_viewport(frame_png: bytes) -> tuple[int, int]:
    """Return the viewport of THIS frame, read from the frame itself.

    ``self.backend.viewport`` is a live read. Pairing it with an
    already-captured frame lets a resize or move between capture and use
    reinterpret that frame under geometry it never had, which can place a
    coordinate where the operator never demonstrated one. A PNG carries its own
    dimensions in the IHDR chunk, so the frame can always answer for itself.
    """

    if len(frame_png) < 24 or not frame_png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("frame viewport requires valid PNG bytes")
    width, height = struct.unpack(">II", frame_png[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("frame viewport must be positive")
    return int(width), int(height)


PC_TEMPLATE_THRESHOLD = 0.9
# REGION_STABLE asserts recorded structure, not palette.  The ordinary
# grayscale template matcher remains the first (stricter) check; this edge-map
# threshold is a color-invariant fallback.  Exact v1.16.1 MockMed evidence
# measured correct theme+parameter substitution at 0.86 and a true blocking
# modal at 0.24, leaving a deliberate margin on both sides.
PC_STRUCTURAL_TEMPLATE_THRESHOLD = 0.8

# Typed-input verification: size of the "field region" diffed/OCRed around
# the focusing click point after a TYPE action. Generous on purpose — the
# typed text renders at the field's own left edge / first line, not at the
# click point — and clamped to the viewport. When no focusing click is
# known (keyboard-only focus moves, e.g. Tab between fields), the whole
# frame is used instead.
FIELD_REGION_SIZE = (640, 240)
FIELD_REGION_PAD = 16

# Typed-input verification, secret-only diff acceptance: when a SECRET typed
# value is OCR-able but OCR cannot find it, a pixel change alone is accepted
# only if the field region gained no other READABLE text — masked fields render
# dots, which OCR reads as nothing, low-confidence noise, or punctuation
# runs, while a dialog rendering over the region adds confident words.
# "Readable" therefore counts alphanumeric characters of lines OCR is
# confident about (>= MASKED_MIN_CONFIDENCE), EXCLUDING homogeneous glyph
# runs: a dot row can misread as a confident digit run (measured on the
# Linux renderer: 17 bullets -> '0000000000006' at 0.81 confidence), and
# such runs are >= MASKED_REPEAT_FRACTION one repeated character —
# no real dialog sentence is. Alnum counts are also invariant to
# segmentation differences between frames. The gain may not exceed
# MASKED_NEW_TEXT_SLACK.
MASKED_NEW_TEXT_SLACK = 3
MASKED_MIN_CONFIDENCE = 0.6
MASKED_REPEAT_FRACTION = 0.66

# Closed-loop scroll: a SCROLL step keeps scrolling by its recorded delta
# until the NEXT anchored step's anchor resolves on a settled frame, bounded
# by this multiple of the step's own recorded scroll distance. Consecutive
# SCROLL steps hand the loop to each other (each probes first and no-ops
# once the anchor is in view), so a run of N recorded scrolls has a combined
# budget of ~2.5x the total recorded distance.
SCROLL_BUDGET_FACTOR = 2.5

# A secret TYPE step's value is never stored in the bundle; it is read at
# replay from this environment variable (the param name upper-cased, with
# non-alphanumeric characters mapped to '_'). See ir.Step.secret.
SECRET_ENV_PREFIX = "OPENADAPT_FLOW_SECRET_"


def secret_env_var(param: str) -> str:
    """Environment variable a secret parameter's value is read from."""
    key = "".join(ch if ch.isalnum() else "_" for ch in param).upper()
    return f"{SECRET_ENV_PREFIX}{key}"


# Bounds for the Phase-2 program interpreter -- deterministic termination
# guarantees. A worklist loop is bounded per-loop (LoopSpec.max_iterations); the
# graph walk itself is bounded by a total step budget (so an authored cycle of
# always-true transitions can never spin forever) and subflow/loop nesting is
# depth-bounded.
PROGRAM_MAX_STEPS = 100_000
PROGRAM_MAX_DEPTH = 64


class _GraphStepContext:
    """Per-action-state context the program interpreter feeds ``_run_step``.

    Supplies the two pieces the linear loop derives from list position but a
    graph lacks: the previously EXECUTED action (for the click-to-focus TYPE
    heuristic) and the SCROLL closed-loop successors (the next anchored action
    state and the immediately following state). All optional; a None field means
    "no such successor" (the scroll loop then falls back to its open-loop
    gesture, exactly as at the end of a linear workflow).
    """

    __slots__ = (
        "prev_action",
        "next_anchored",
        "following",
        "has_exception_handler",
    )

    def __init__(
        self,
        prev_action: Optional[ActionKind] = None,
        next_anchored: Optional[Step] = None,
        following: Optional[Step] = None,
        has_exception_handler: bool = False,
    ) -> None:
        self.prev_action = prev_action
        self.next_anchored = next_anchored
        self.following = following
        self.has_exception_handler = has_exception_handler


class _ProgramHalt(Exception):
    """Internal signal: an unrecoverable state was reached (an unhandled action
    failure, a dead-end with no matching transition, a bound exceeded, or a
    ``halt`` / ``escalate`` terminal). Carries the outcome + reason the run
    report records. Caught only by ``_interpret_program`` -- never escapes the
    Replayer."""

    def __init__(self, outcome: str, reason: str, *, safety: bool = False) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.safety = safety
        # Populated at an ACTION-state failure before graph frames unwind.
        # Empty means the halt is not eligible for a generic attended
        # Continue/Skip transition.
        self.program_frames: list[GraphFrame] = []
        self.program_params: dict[str, RuntimeParamScalar] = {}
        self.program_history_hash: str = ""


def _all_workflow_steps(workflow: Workflow):
    """Yield every ``Step`` in a workflow for whole-bundle audits.

    Linear bundle: the ``steps`` list. Program bundle: every ``action`` state's
    step across the top-level program graph AND all subflows (a loop body's
    steps included). Order is stable but not execution order -- used only for
    static coverage counting, not for running.
    """
    if workflow.program is None:
        yield from workflow.steps
        return
    graphs = [workflow.program, *workflow.subflows.values()]
    for graph in graphs:
        for state in graph.states.values():
            if state.kind is StateKind.ACTION and state.step is not None:
                yield state.step


class EgressNotPermitted(RuntimeError):
    """Raised when an egress-capable model component (a grounder / identity-VLM
    / state-verifier that could send a screenshot off the box) is wired without
    the operator's explicit ``allow_model_grounding`` opt-in (PHI audit REM-3).
    """


class Replayer:
    """Replays a Workflow against a Backend using injected vision.

    Args:
        backend: The Backend to act through (screenshot/click/type/press).
        vision: Namespace-like object exposing ``find_template``,
            ``find_text``, ``text_present``, ``ocr``, ``phash_png``,
            ``phash_distance``, and ``wait_settled``. Defaults to the
            real ``openadapt_flow.vision``
            module, imported lazily so unit tests can inject a fake without
            the OCR stack ever loading. Target-resolution providers implement
            ``find_text(..., raise_on_ambiguity=True)`` so repeated candidates
            produce a typed refusal instead of a first-match action.
        grounder: Optional Grounder used as the last resolution rung.
        identity_vlm: Optional IdentityVLM (see runtime.identity) used as the
            optional local-VLM veto tier of the pre-click identity ladder;
            None (default) disables that tier -- the ladder still runs
            structured-text, pixel-compare, OCR, and halt with no model.
        effect_verifier: Optional system-of-record ``EffectVerifier``
            (``runtime.effects``) bound to the deployment's system of record
            (a REST/FHIR API, a document store). When set, any step that
            declares ``ir.Step.effects`` is verified against the REAL record
            after its screen postconditions pass; a non-CONFIRMED verdict
            HALTS. When None (default) a bundle with NO effects replays
            exactly as before, but a step that DOES declare effects is a
            deployment/config error and HALTS (fail-safe: an unverifiable
            consequential write is never silently accepted). Reads only —
            ZERO model calls.
        effect_compensator: Optional ``Compensator`` (e.g. ``RestCompensator``)
            invoked via ``reconcile_or_escalate`` when an IRREVERSIBLE effect
            is REFUTED as a duplicate — deletes the extras and re-verifies
            (RECONCILED continues, else ESCALATE halts). None (default) means
            every refuted/indeterminate effect simply halts.
        api_actuator: Optional ``ApiActuator`` (``runtime.actuators``) bound to
            the deployment's API. It is the TOP of the capability ladder: when a
            step carries an ``ir.Step.api_binding`` and this actuator is set,
            the step's write is PERFORMED via the API (deterministic, $0, no
            model), CONFIRMED by the ``effect_verifier``, and the GUI
            resolve/act is SKIPPED. None (default) disables the API tier -- a
            step with a binding then actuates through the GUI ladder exactly as
            today (the API tier is an optimization whose safe fallback IS the
            GUI). Never makes a model call.
        durable: When True, enable the Tier-3 durable runtime (RFC §5): write a
            checkpoint after each verified step and, on a halt, a durable
            pending escalation, so the run can be RESUMED from the last verified
            checkpoint (``openadapt_flow.runtime.durable.resume``) rather than
            re-run from step 0. None of this touches the backend, vision, or a
            model — it is bookkeeping over the ``StepResult``. Off by default;
            a non-durable run behaves exactly as before.
        governed_authorization: Optional, bundle-bound capability produced by
            the fail-closed ``run`` gate. It can require affirmative identity
            on selected steps and approve exact GUI effect contracts without a
            verifier while keeping them visibly unverified. Ordinary ``replay``
            leaves it unset.
        poll_interval_s: Postcondition polling interval in seconds.
        require_settled: When True, the step-entry readiness gate HALTs
            gracefully if the screen never settles (a still-loading /
            mid-transition frame) instead of acting on a stale frame. OFF by
            default (behavior unchanged; animated UIs are not over-halted).
            Recommended ON for slow-loading / state-sensitive targets. Fails
            closed when the injected vision cannot report settledness.
        settle_readiness_timeout_s: Single bounded settle window for the
            readiness gate (seconds) before it HALTs.
        interstitials: Operator-supplied KNOWN reversible, non-consequential
            interstitials detected model-free at each step's entry. Each
            dismissal is audited and must pass its declared visual clearance;
            otherwise replay halts. Merged with the bundle's own
            ``Workflow.interstitials``. None (default) => no handling.
        control_overlay: Optional presentation-only canonical event producer.
            It has no execution authority. Sink/schema errors are retained on
            ``control_overlay_error`` and disable later presentation events;
            they never change actuation or the evidence-qualified run outcome.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        vision: Optional[Any] = None,
        grounder: Optional[Any] = None,
        identity_vlm: Optional[Any] = None,
        state_verifier: Optional[Any] = None,
        effect_verifier: Optional[EffectVerifier] = None,
        effect_compensator: Optional[Any] = None,
        api_actuator: Optional[Any] = None,
        durable: bool = False,
        checkpoint_key: Optional[str] = None,
        poll_interval_s: float = 0.05,
        use_structural: bool = True,
        pixel_verify_enabled: bool = False,
        allow_model_grounding: bool = False,
        governed_authorization: Optional[GovernedRunAuthorization] = None,
        delivery_authority_kind: Literal[
            "customer_local", "cloud_runner"
        ] = "customer_local",
        remote_delivery_run_id: Optional[str] = None,
        managed_dispatch_binding: Optional[Any] = None,
        production_qualification_guard: Optional["ProductionQualificationGuard"] = None,
        qualification_campaign_guard: Optional["QualificationCampaignGuard"] = None,
        governed_continuation: bool = False,
        require_settled: bool = False,
        settle_readiness_timeout_s: float = 10.0,
        interstitials: Optional[list[Interstitial]] = None,
        control_overlay: Optional["RuntimeControlOverlayEmitter"] = None,
        idempotency_ledger: Optional["IdempotencyLedger"] = None,
        qualification_fault_driver: Optional[QualificationFaultDriver] = None,
        qualification_environment_observer: Optional[
            QualificationEnvironmentObserver
        ] = None,
    ) -> None:
        if vision is None:
            import openadapt_flow.vision as vision  # lazy: heavy OCR deps

        # Egress guard (PHI audit REM-3): a grounder / identity-VLM /
        # state-verifier that can send a screenshot OFF the box (a paid API or an
        # on-prem appliance) may only be wired when the operator EXPLICITLY opts
        # in. Fail closed otherwise, so no caller can silently route a live
        # patient screen off the machine. The default local replay wires no such
        # component and makes zero outbound calls (the load-bearing "stays local"
        # claim, guarded by test_egress_guard).
        from openadapt_flow.runtime.grounder import component_may_egress

        self.allow_model_grounding = allow_model_grounding
        egress = [
            name
            for name, comp in (
                ("grounder", grounder),
                ("identity_vlm", identity_vlm),
                ("state_verifier", state_verifier),
            )
            if component_may_egress(comp)
        ]
        self._screenshots_may_leave_box = bool(egress)
        if egress and not allow_model_grounding:
            raise EgressNotPermitted(
                "refusing to wire an egress-capable model component "
                f"({', '.join(egress)}) that could send a screenshot off the "
                "box: pass allow_model_grounding=True (CLI: "
                "--allow-model-grounding) to opt in explicitly. The default "
                "local replay makes zero outbound calls."
            )

        self.backend = backend
        # Defaulted to the ``openadapt_flow.vision`` module above when None, so it
        # is always populated by the time replay runs; typed Any (a namespace-like
        # facade exposing find_template/ocr/wait_settled/...).
        self.vision: Any = vision
        self.grounder = grounder
        # Whether the deterministic structural ACTION rung (top of the ladder)
        # may run. True (default) = product behavior: on a structure-bearing
        # backend, resolve the recorded target as a DOM/UIA element. Set False
        # to force the VISUAL fallback floor even on a structure-bearing
        # backend -- used to characterize the pixel-only substrate path
        # (RDP/Citrix/VDI) on a Playwright surface (see the e2e visual-floor
        # drift/heal suites). Never disables the visual ladder itself.
        self.use_structural = use_structural
        # Arm the pixel-compare identity tier's VERIFY branch (experimental,
        # evidence-gated; OFF by default = today's behavior). When False the
        # pixel tier only HALTs on a localized glyph change or ABSTAINs; it
        # never pixel-VERIFIES. Resolved from deployment.yaml
        # ``runtime.pixel_verify_enabled`` via build_replayer; read only by the
        # identity ladder's pixel_tier (see identity.verify_pixel_identity and
        # docs/LIMITS.md).
        self.pixel_verify_enabled = pixel_verify_enabled
        # System-of-record effect verification (OFF by default). The verifier
        # is bound to the deployment's system of record; the optional
        # compensator undoes a detected duplicate irreversible write. Neither
        # makes a model call — the $0 runtime guarantee is preserved.
        self.effect_verifier = effect_verifier
        self.effect_compensator = effect_compensator
        # ``run``-only, bundle-bound admission capability.  The ordinary
        # ``replay`` path leaves this unset and preserves its permissive
        # behavior.  When present it carries exact identity/effect decisions
        # from the run gate into the shared executor instead of reducing them
        # to a boolean that could authorize a different workflow.
        if (
            delivery_authority_kind == "cloud_runner"
            and managed_dispatch_binding is None
        ):
            raise ValueError(
                "cloud_runner requires a verified internal managed dispatch binding"
            )
        if managed_dispatch_binding is not None:
            from openadapt_flow.runner.dispatch_envelope import ManagedDispatchBinding
            from openadapt_flow.runner.protocol import dispatch_binding_sha256

            if not isinstance(managed_dispatch_binding, ManagedDispatchBinding):
                raise ValueError("managed dispatch binding is invalid")
            if not durable:
                raise ValueError("managed dispatch binding requires durable=True")
            if managed_dispatch_binding.binding_sha256 != dispatch_binding_sha256(
                managed_dispatch_binding.run_id,
                managed_dispatch_binding.authorization,
            ):
                raise ValueError(
                    "managed dispatch binding does not match its authorization"
                )
            if governed_authorization not in (
                None,
                managed_dispatch_binding.authorization,
            ) or remote_delivery_run_id not in (None, managed_dispatch_binding.run_id):
                raise ValueError(
                    "managed dispatch binding does not match public inputs"
                )
            governed_authorization = managed_dispatch_binding.authorization
            delivery_authority_kind = "cloud_runner"
            remote_delivery_run_id = managed_dispatch_binding.run_id
        if production_qualification_guard is not None:
            from openadapt_flow.production_qualification import (
                ProductionQualificationGuard,
            )

            if not isinstance(
                production_qualification_guard, ProductionQualificationGuard
            ):
                raise ValueError("Production qualification authority is invalid")
        if qualification_campaign_guard is not None:
            from openadapt_flow.qualification_campaign_authority import (
                QualificationCampaignGuard,
            )

            if not isinstance(qualification_campaign_guard, QualificationCampaignGuard):
                raise ValueError("qualification campaign authority is invalid")
        carries_campaign_permit = (
            getattr(governed_authorization, "qualification_campaign_permit_id", None)
            is not None
        )
        if carries_campaign_permit and qualification_campaign_guard is None:
            raise ValueError(
                "qualification actuation bound to a campaign permit requires the "
                "signed non-production campaign authority"
            )
        self.managed_dispatch_binding = managed_dispatch_binding
        self.governed_authorization = governed_authorization
        self.production_qualification_guard = production_qualification_guard
        self.qualification_campaign_guard = qualification_campaign_guard
        self.delivery_authority_kind = delivery_authority_kind
        self.remote_delivery_run_id = remote_delivery_run_id
        self._managed_dispatch_snapshot: tuple[str, str, str] | None = None
        if managed_dispatch_binding is not None:
            self._managed_dispatch_snapshot = (
                managed_dispatch_binding.run_id,
                managed_dispatch_binding.binding_sha256,
                json.dumps(
                    managed_dispatch_binding.authorization.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )
        self.governed_continuation = governed_continuation
        self._governed_asset_snapshot: dict[str, bytes] = {}
        self._governed_asset_hashes: dict[str, str] = {}
        self._governed_plaintext_assets = False
        self._governed_asset_mutation: Optional[str] = None
        self._governed_base_params: Optional[dict[str, RuntimeParamScalar]] = None
        self._active_runtime_worklists: Optional[dict[str, list[dict[str, str]]]] = None
        self._active_delivery_resolution: Optional[Resolution] = None
        self._active_delivery_region: Optional[Region] = None
        self._current_graph_id: Optional[str] = None
        self._execution_workflow_source: Optional[Workflow] = None
        self._execution_workflow_snapshot: Optional[Workflow] = None
        self._execution_snapshot_required = False
        self._governed_authorization_source: Optional[GovernedRunAuthorization] = None
        self._governed_authorization_snapshot_error: Optional[str] = None
        self._durable_resume_secret = os.urandom(32)
        self._durable_resume_admission: Optional[tuple[dict[str, Any], str]] = None
        self._durable_resume_mode: Optional[Literal["linear", "program"]] = None
        self._durable_linear_snapshot: tuple[Any, ...] = ()
        self._durable_program_snapshot: tuple[ProgramCheckpoint, ...] = ()
        self._durable_pending_snapshot: Optional[Any] = None
        self._active_delivery_acknowledgers: tuple[Any, ...] = ()
        self._durable_continuation_guard: Optional[Any] = None
        # API/tool actuator -- the TOP of the capability ladder (RFC section 4
        # `api` tier). When set, a step carrying an `api_binding` has its write
        # performed via the API and confirmed by the effect_verifier, SKIPPING
        # the GUI resolve/act. None (default) = the API tier is off and such a
        # step falls through to the GUI ladder unchanged. Deterministic; makes
        # no model call (the $0 runtime guarantee holds on this path too).
        self.api_actuator = api_actuator
        # Durable tiered runtime (RFC §5, Tier 3), OFF by default. When True,
        # run() writes a RunCheckpoint after each VERIFIED step and, on a halt,
        # a PendingEscalation instead of just dying -- so the run can be
        # RESUMED from the last verified checkpoint (see
        # openadapt_flow.runtime.durable and run()'s ``resume_from``). Pure
        # bookkeeping over the StepResult; no backend/vision/model involvement.
        self.durable = durable
        # Encryption-at-rest for the durable checkpoints (opt-in, OFF by
        # default). A passphrase (explicit, or from OPENADAPT_BUNDLE_KEY) seals
        # every checkpoint / pending-escalation / manifest with AES-256-GCM
        # (openadapt_flow.crypto). None => plaintext checkpoints, unchanged.
        self.checkpoint_key = checkpoint_key
        # Optional local-VLM identity veto (tier 3 of the identity ladder),
        # OFF by default like the grounder: an IdentityVLM verifier or None.
        # When None the ladder runs structured-text + pixel-compare + OCR +
        # halt with no model dependency.
        self.identity_vlm = identity_vlm
        # Optional VLM drift-oracle (state-verifier), OFF by default. When set,
        # it may CONFIRM a render-drift-sensitive postcondition that
        # deterministically false-failed (the same heal-under-drift the
        # resolution ladder does for click targets). VETO-SAFE: it only ever
        # rescues on a confident "yes"; "no"/"uncertain"/outage keep the halt,
        # and it never touches structural or text_absent postconditions.
        self.state_verifier = state_verifier
        self.poll_interval_s = poll_interval_s
        if not math.isfinite(settle_readiness_timeout_s) or (
            settle_readiness_timeout_s <= 0
        ):
            raise ValueError("settle_readiness_timeout_s must be finite and > 0")
        # Point of the most recent successful click (the focusing click for
        # a following TYPE step); reset per run.
        self._last_click_point: Optional[Point] = None
        self._last_click_region: Optional[Region] = None
        # Stable-per-run identity; (re)assigned at the top of run(). Present as
        # an empty default so a direct _execute_step call (tests) still resolves
        # effect contracts (a literal effect ignores it).
        self._run_id: str = ""
        # ``True`` only after this runtime instance either creates an exact
        # idempotency reservation or validates ownership of the reservation it
        # resumes.  A refusal before reservation must never write a terminal
        # outcome into an unrelated ledger record.
        self._idempotency_reservation_owned = False
        # -- state-dependency robustness (docs/LIMITS.md "state dependency") ----
        # ``require_settled``: when True the step-entry readiness gate REFUSES to
        # act on a frame that never settled -- a still-loading / mid-transition
        # screen HALTs gracefully instead of proceeding-anyway on a stale frame
        # (the wait_settled proceed-anyway path). OFF by default so behavior is
        # byte-for-byte unchanged (and inherently-animated UIs are not over-
        # halted); recommended ON for slow-loading / state-sensitive targets.
        # The real vision module exposes ``wait_settled_result``. An opt-in
        # caller that injects a facade without that readiness signal HALTs
        # before action rather than silently degrading to proceed-anyway.
        self.require_settled = require_settled
        # One settle poll owns this complete bound. The replayer does not wrap
        # it in an outer retry loop, avoiding unaccounted blind retries.
        self.settle_readiness_timeout_s = settle_readiness_timeout_s
        # Operator-supplied KNOWN interstitials, merged with the bundle's own at
        # each step's entry.  Keep both the caller-owned source and a deep,
        # canonical private snapshot: governed authorization validates and
        # execution consumes the snapshot, while the source is re-fingerprinted
        # immediately before any dismissal so a callback/concurrent mutation is
        # detected rather than changing the admitted action surface.
        self._runtime_interstitial_source = (
            interstitials if interstitials is not None else []
        )
        self._runtime_interstitial_snapshot_error: Optional[str] = None
        try:
            (
                self._interstitials,
                self._runtime_interstitial_snapshot,
            ) = self._canonical_interstitial_snapshot(self._runtime_interstitial_source)
        except Exception as exc:
            self._interstitials = ()
            self._runtime_interstitial_snapshot = ()
            self._runtime_interstitial_snapshot_error = (
                "runtime interstitial declarations could not be snapshotted "
                f"({type(exc).__name__})"
            )
        self._workflow_interstitials: tuple[Interstitial, ...] = ()
        self._workflow_interstitial_snapshot: tuple[str, ...] = ()
        self._workflow_interstitial_snapshot_error: Optional[str] = None
        self.control_overlay = control_overlay
        self.control_overlay_error: Optional[str] = None
        self._control_overlay_disabled = False
        self._control_overlay_last_observation_png: Optional[bytes] = None
        # At-most-once ledger (Section 3). When set together with a
        # caller-supplied ``idempotency_key`` on run(), a repeat under the same
        # key is SUPPRESSED before any actuation (never blind-retried). None
        # (default) leaves the run path byte-for-byte unchanged.
        self.idempotency_ledger = idempotency_ledger
        self.qualification_fault_driver = qualification_fault_driver
        self.qualification_environment_observer = qualification_environment_observer
        self._qualification_fault_mutations: list[FaultMutationReceipt] = []
        self._qualification_fault_public_key: Optional[str] = None
        self._qualification_case_action_paths: dict[str, Literal["gui", "api"]] = {}
        self._qualification_environment_call: Optional[
            Callable[[], QualificationEnvironmentObservation]
        ] = None
        self._qualification_environment_bound: Optional[
            QualificationEnvironmentObservation
        ] = None

    @staticmethod
    def _canonical_resume_value(value: Any) -> Any:
        """Return one deterministic JSON value for a resume admission field."""

        if value is None:
            return None
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        if isinstance(value, list):
            return [Replayer._canonical_resume_value(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): Replayer._canonical_resume_value(item)
                for key, item in value.items()
            }
        return value

    def _durable_resume_payload(
        self,
        *,
        mode: Literal["linear", "program"],
        workflow: Workflow,
        run_dir: Path,
        bundle_dir: Path,
        run_id: str,
        params: Mapping[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        resume_from: Optional[int],
        resume_program: Optional[ProgramCheckpoint],
        durable_context: Optional[dict[str, Any]] = None,
        authorizing_approval: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Bind one continuation to the exact retained durable context."""

        from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

        store = CheckpointStore(run_dir, key=self.checkpoint_key)
        retained = durable_context or {
            "manifest": store.read_manifest(),
            "pending": store.read_pending(),
            "approval": store.read_approval(),
            "linear_checkpoints": store.checkpoints(),
            "program_checkpoints": store.program_checkpoints(),
            "auxiliary_digest": self._durable_auxiliary_digest(
                run_dir, store.program_checkpoints()
            ),
        }
        return {
            "mode": mode,
            "run_dir": str(Path(run_dir).resolve()),
            "bundle_dir": str(Path(bundle_dir).resolve()),
            "bundle_version": _bundle_version(Path(bundle_dir)),
            "run_id": run_id,
            "workflow_name": workflow.name,
            "workflow": workflow.model_dump(mode="json"),
            "params": dict(params),
            "worklists": self._canonical_resume_value(worklists),
            "resume_from": resume_from,
            "resume_program": self._canonical_resume_value(resume_program),
            "manifest": self._canonical_resume_value(retained["manifest"]),
            "pending": self._canonical_resume_value(retained["pending"]),
            "approval": self._canonical_resume_value(retained["approval"]),
            "authorizing_approval": self._canonical_resume_value(authorizing_approval),
            "linear_checkpoints": self._canonical_resume_value(
                retained["linear_checkpoints"]
            ),
            "program_checkpoints": self._canonical_resume_value(
                retained["program_checkpoints"]
            ),
            "auxiliary_digest": retained["auxiliary_digest"],
        }

    @staticmethod
    def _durable_auxiliary_digest(
        run_dir: Path,
        program_checkpoints: list[ProgramCheckpoint],
    ) -> str:
        """Bind namespace, active capability, and every attended receipt."""

        receipts_dir = Path(run_dir) / ".attended_program_receipts"
        expected = {
            f"{checkpoint.attended_transition.pause_id}.json"
            for checkpoint in program_checkpoints
            if checkpoint.attended_transition is not None
        }
        actual = (
            {path.name for path in receipts_dir.glob("*.json")}
            if receipts_dir.is_dir()
            else set()
        )
        if actual != expected:
            from openadapt_flow.runtime.durable.approval import StateDiverged

            raise StateDiverged(
                "attended program receipt inventory does not match checkpoints"
            )
        digest = hashlib.sha256()
        paths = [
            Path(run_dir) / ".durable_run.claim",
            Path(run_dir) / "attended_capability.json",
            Path(run_dir) / "attended_capability_history.json",
            *[receipts_dir / name for name in sorted(actual)],
        ]
        for path in paths:
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            if path.is_file():
                digest.update(path.read_bytes())
            else:
                digest.update(b"<missing>")
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def _admit_durable_resume(
        self,
        authority: object,
        *,
        mode: Literal["linear", "program"],
        workflow: Workflow,
        run_dir: Path,
        bundle_dir: Path,
        run_id: Optional[str],
        params: Mapping[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        resume_from: Optional[int],
        resume_program: Optional[ProgramCheckpoint],
        durable_context: dict[str, Any],
        authorizing_approval: Any,
    ) -> str:
        """Mint a single-use continuation after ``durable.resume`` admits it."""

        if authority is not _DURABLE_RESUME_AUTHORITY:
            raise PermissionError("durable resume authority is private")
        if set(durable_context) != {
            "manifest",
            "pending",
            "approval",
            "linear_checkpoints",
            "program_checkpoints",
            "auxiliary_digest",
        }:
            raise PermissionError("durable resume context is incomplete")
        effective_run_id = run_id or uuid.uuid4().hex
        payload = self._durable_resume_payload(
            mode=mode,
            workflow=workflow,
            run_dir=run_dir,
            bundle_dir=bundle_dir,
            run_id=effective_run_id,
            params=params,
            worklists=worklists,
            resume_from=resume_from,
            resume_program=resume_program,
            durable_context=durable_context,
            authorizing_approval=authorizing_approval,
        )
        approval_bundle_version = getattr(
            authorizing_approval,
            "bundle_version",
            None,
        )
        if not isinstance(approval_bundle_version, str) or not hmac.compare_digest(
            payload["bundle_version"],
            approval_bundle_version,
        ):
            from openadapt_flow.runtime.durable.approval import ApprovalRequired

            raise ApprovalRequired(
                "the workflow bundle changed after resume approval; refusing "
                "to mint continuation authority"
            )
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        signature = hmac.new(
            self._durable_resume_secret, canonical, hashlib.sha256
        ).hexdigest()
        self._durable_resume_admission = (payload, signature)
        return effective_run_id

    def _consume_durable_resume_admission(
        self,
        *,
        workflow: Workflow,
        run_dir: Path,
        bundle_dir: Path,
        run_id: str,
        params: Mapping[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        resume_from: Optional[int],
        resume_program: Optional[ProgramCheckpoint],
    ) -> None:
        """Consume and verify the exact resume context before any callback."""

        from openadapt_flow.runtime.durable.approval import ApprovalRequired

        admission = self._durable_resume_admission
        self._durable_resume_admission = None
        if admission is None:
            raise ApprovalRequired(
                "durable continuation must enter through the authenticated resume API"
            )
        admitted, signature = admission
        canonical = json.dumps(
            admitted, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        expected_signature = hmac.new(
            self._durable_resume_secret, canonical, hashlib.sha256
        ).hexdigest()
        mode = admitted.get("mode")
        if mode not in {"linear", "program"} or not hmac.compare_digest(
            signature, expected_signature
        ):
            raise ApprovalRequired("durable continuation admission is invalid")
        current = self._durable_resume_payload(
            mode=mode,
            workflow=workflow,
            run_dir=run_dir,
            bundle_dir=bundle_dir,
            run_id=run_id,
            params=params,
            worklists=worklists,
            resume_from=resume_from,
            resume_program=resume_program,
            authorizing_approval=admitted.get("authorizing_approval"),
        )
        if current != admitted:
            raise ApprovalRequired(
                "durable continuation context changed after approval"
            )
        from openadapt_flow.runtime.durable.checkpoint import RunCheckpoint

        self._durable_resume_mode = mode
        self._durable_linear_snapshot = tuple(
            RunCheckpoint.model_validate(value).model_copy(deep=True)
            for value in admitted["linear_checkpoints"]
        )
        self._durable_program_snapshot = tuple(
            ProgramCheckpoint.model_validate(value).model_copy(deep=True)
            for value in admitted["program_checkpoints"]
        )
        pending_value = admitted.get("pending")
        if pending_value is None:
            self._durable_pending_snapshot = None
        else:
            from openadapt_flow.runtime.durable.checkpoint import PendingEscalation

            self._durable_pending_snapshot = PendingEscalation.model_validate(
                pending_value
            ).model_copy(deep=True)

    # -- public API ----------------------------------------------------------

    def run(
        self,
        workflow: Workflow,
        *,
        params: Optional[dict[str, RuntimeParamScalar]] = None,
        worklists: Optional[dict[str, list[dict[str, str]]]] = None,
        bundle_dir: Path,
        run_dir: Path,
        save_healed_to: Optional[Path] = None,
        resume_from: Optional[int] = None,
        resume_program: Optional[ProgramCheckpoint] = None,
        run_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        execution_target_kind: Optional[ExecutionTargetKind] = None,
        surface_override: bool = False,
        execution_origin: Optional[str] = None,
        execution_entry_url: Optional[str] = None,
        prior_screenshots_may_leave_box: bool = False,
        prior_model_calls: int = 0,
        prior_external_network_calls: Literal[
            "none", "observed", "unknown"
        ] = "unknown",
    ) -> RunReport:
        """Execute the workflow and write a run directory.

        A workflow with no ``program`` graph runs the LINEAR ``steps`` loop
        exactly as before (byte-for-byte back-compatible). A workflow carrying a
        ``program`` (Workflow-program IR Phase 2) is interpreted as a STATE
        MACHINE -- loops, branches, subflows, exception paths -- reusing the
        IDENTICAL per-action machinery (resolve / identity gate / effect verify /
        risk gate / heal) for every ``action`` state. Both paths make ZERO model
        calls on the deterministic path (the $0 runtime guarantee).

        Args:
            workflow: The compiled workflow. Heals are applied to this
                in-memory object as the run progresses.
            params: Values for parameterized TYPE steps (``step.param``).
                Parameters not supplied here fall back to the recorded
                example/default values in ``workflow.params``.
            worklists: Program mode only -- run-time worklist rows for ``loop``
                states, keyed by relation name (each a list of param-dict rows).
                Overrides any inline ``Workflow.data_sources`` rows of the same
                name; the mechanism that makes a loop VARIABLE-LENGTH at run
                time. Ignored for a linear (no-program) workflow.
            bundle_dir: The workflow bundle directory (source of template
                crops).
            run_dir: Output directory for report.json, per-step screenshots
                (``steps/``), and heal artifacts (``heals/``).
            save_healed_to: When set, write a full healed bundle (updated
                workflow.json + new and unchanged template crops) here.
            resume_from: Tier-3 durable resume (RFC §5), LINEAR mode. When set,
                steps with index ``< resume_from`` are treated as already
                verified: they are NOT re-executed (so an already-confirmed
                consequential write is never re-performed) and their results are
                reconstructed from the persisted checkpoints. Execution begins at
                ``resume_from`` (the previously-paused step). Normally supplied by
                ``openadapt_flow.runtime.durable.resume``, not by hand.
            resume_program: Tier-3 durable resume (RFC §5), PROGRAM mode. The last
                verified :class:`ProgramCheckpoint`; when set, the interpreter's
                state is RESTORED from it (frame stack, loop cursors, bound
                params, completed effect keys) and the run continues from the
                paused state -- never from the graph entry, never re-performing an
                already-confirmed write. Supplied by
                ``openadapt_flow.runtime.durable.resume``, not by hand.
            run_id: Stable run-instance identity supplied only by durable resume.
                New runs generate one; resumed legs preserve the manifest value
                so attended capabilities and effect idempotency remain bound to
                the same logical run.
            idempotency_key: Caller-supplied at-most-once key for this run
                (Section 3). When the Replayer was built with an
                ``idempotency_ledger``, the key is RESERVED before any
                actuation; a later run that supplies the same key is SUPPRESSED
                (``idempotent_replay=True``, no consequential action
                re-performed) rather than blind-retried. None (default) leaves
                behavior unchanged.
            execution_target_kind: Resolved backend token (web, windows, macos,
                linux, rdp, or citrix). Runtime validation binds this report
                field into the signed substrate-aware attestation.
            surface_override: True when the caller explicitly overrode the
                workflow's bound surface (``Workflow.surface``) and this run
                executes on a different surface. Recorded in the report as
                compatibility evidence; the CLI refuses a cross-surface run
                without it.
            execution_origin: Actual browser origin loaded before replay. It is
                evidence metadata only; hosted validation requires an exact
                match to its signed target origin.
            execution_entry_url: Browser URL requested before replay. Hosted
                validation binds this to the runner's entry URL so workflows
                that start below `/` retain their required path.
            prior_screenshots_may_leave_box: Durable-resume audit carry. True
                when an earlier leg of the same logical run was configured with
                an egress-capable screenshot consumer.
            prior_model_calls: Cumulative model calls made by earlier legs of
                the same logical run.
            prior_external_network_calls: Sticky network observation from
                earlier legs of the same logical run.

        Returns:
            The RunReport (also saved as ``run_dir/report.json``). A linear run
            aborts at the first failed step; a program run ends at a terminal
            state (or an unhandled failure). ``success`` reflects that outcome.
        """
        managed_refusal = self._managed_dispatch_refusal()
        if managed_refusal is not None:
            raise ValueError(managed_refusal)
        production_refusal = self._production_qualification_refusal(workflow)
        if production_refusal is not None:
            raise ValueError(production_refusal)
        campaign_refusal = self._qualification_campaign_refusal(workflow)
        if campaign_refusal is not None:
            raise ValueError(campaign_refusal)
        bundle_dir = Path(bundle_dir)
        run_dir = Path(run_dir)
        self._install_execution_snapshots(workflow)
        # Snapshot bundle declarations before authorization validation and
        # before the first backend/vision callback.  Execution uses only this
        # deep copy; the caller-owned workflow remains available solely for a
        # mutation comparison immediately before an interstitial action.
        self._workflow_interstitial_snapshot_error = None
        try:
            (
                self._workflow_interstitials,
                self._workflow_interstitial_snapshot,
            ) = self._canonical_interstitial_snapshot(workflow.interstitials)
        except Exception as exc:
            self._workflow_interstitials = ()
            self._workflow_interstitial_snapshot = ()
            self._workflow_interstitial_snapshot_error = (
                "workflow interstitial declarations could not be snapshotted "
                f"({type(exc).__name__})"
            )
        # Stable-per-run identity (P0-3): distinct across runs, constant within
        # one. Exposed to effect-contract resolution under the reserved
        # ``__run_id__`` param so an idempotency key can be bound PER-RUN (via
        # ``ValueExpr(param="__run_id__")``) instead of reusing a frozen demo
        # literal across unrelated runs.
        self._run_id = run_id or (
            self.governed_authorization.authorization_id
            if self.governed_authorization is not None
            else uuid.uuid4().hex
        )
        self._idempotency_reservation_owned = False
        self._qualification_fault_mutations = []
        self._qualification_fault_public_key = None
        self._qualification_case_action_paths = {}
        self._qualification_environment_call = None
        self._qualification_environment_bound = None
        clear_guard = getattr(self.backend, "set_qualification_input_guard", None)
        if callable(clear_guard):
            clear_guard(None)
        # Parameter resolution (Workflow-program IR, Phase 1): recorded defaults
        # (``params`` dict) plus each TYPED ``param_specs`` example as a default,
        # with caller-supplied values overriding both. A v0 bundle (empty
        # ``param_specs``) collapses to exactly the old ``{**workflow.params,
        # **caller}`` merge.
        merged: dict[str, RuntimeParamScalar] = {**workflow.params}
        for pname, spec in workflow.param_specs.items():
            if spec.example is not None:
                merged.setdefault(pname, spec.example)
        merged.update(params or {})
        params = merged
        self._outcome_worklists = deepcopy(worklists or {})

        from openadapt_flow.qualification import workflow_contract_sha256

        # Keep the admitted base parameter scope separate from row-derived
        # program-loop bindings. Last-edge authorization hashes this base scope
        # together with the caller-owned worklist, then proves the active
        # iteration scope from the exact loop cursor below. Hashing ``params``
        # inside a loop would count each row twice and reject valid runs.
        self._governed_base_params = dict(params)
        # Preserve the exact caller-owned worklist object for last-common-point
        # authorization checks.  A callback that mutates it after admission
        # must invalidate the authorization before any reacquired input edge.
        self._active_runtime_worklists = worklists
        durable_resume = (
            resume_from is not None
            or resume_program is not None
            or self._durable_resume_admission is not None
        )
        if not durable_resume:
            self._durable_resume_mode = None
            self._durable_linear_snapshot = ()
            self._durable_program_snapshot = ()
            self._durable_pending_snapshot = None
        if durable_resume:
            self._consume_durable_resume_admission(
                workflow=workflow,
                run_dir=run_dir,
                bundle_dir=bundle_dir,
                run_id=self._run_id,
                params=params,
                worklists=worklists or {},
                resume_from=resume_from,
                resume_program=resume_program,
            )

        # All execution decisions consume the deep semantic snapshot. The
        # caller-owned source remains available only for drift comparison.
        if self._execution_workflow_snapshot is not None:
            workflow = self._execution_workflow_snapshot

        # Claim or validate the durable namespace before report output,
        # screenshots, overlay callbacks, idempotency state, or any other
        # execution side effect can touch the run directory. A fresh invocation
        # must never overwrite an existing run's report before discovering that
        # the directory is already owned.
        durable_run = None
        if self.durable:
            from openadapt_flow.runtime.durable import DurableRun

            durable_run = DurableRun(
                run_dir,
                run_id=self._run_id,
                workflow_name=workflow.name,
                bundle_dir=bundle_dir,
                params=params,
                worklists=worklists or {},
                idempotency_key=idempotency_key,
                save_healed_to=save_healed_to,
                key=self.checkpoint_key,
                governed_authorization=self.governed_authorization,
                delivery_authority_kind=self.delivery_authority_kind,
                remote_delivery_run_id=self.remote_delivery_run_id,
                managed_dispatch_binding_sha256=(
                    self.managed_dispatch_binding.binding_sha256
                    if self.managed_dispatch_binding is not None
                    else None
                ),
                screenshots_may_leave_box=(
                    self._screenshots_may_leave_box or prior_screenshots_may_leave_box
                ),
                model_calls=prior_model_calls,
                external_network_calls=prior_external_network_calls,
                resume_existing=durable_resume,
            )
        self._durable_initial_delivery_guard = None
        if (
            durable_run is not None
            and not durable_resume
            and self.managed_dispatch_binding is not None
        ):
            from openadapt_flow.runtime.durable.authority import DurableAuthority

            managed_durable_run = durable_run

            class _ManagedInitialDeliveryGuard:
                def __init__(self_nonlocal) -> None:
                    self_nonlocal.authority = DurableAuthority(
                        run_dir, managed_durable_run.store
                    )
                    self_nonlocal.pending_remote_permit: Any = None

                def before_delivery(self_nonlocal) -> None:
                    if self_nonlocal.pending_remote_permit is not None:
                        raise RuntimeError(
                            "a prior production delivery lacks an acknowledgment receipt"
                        )
                    self_nonlocal.pending_remote_permit = (
                        self_nonlocal.authority.before_initial_delivery(
                            managed_durable_run._manifest  # noqa: SLF001 - exact retained manifest
                        )
                    )

                def acknowledge_delivery(self_nonlocal) -> None:
                    pending = self_nonlocal.pending_remote_permit
                    if pending is None:
                        return
                    self_nonlocal.authority.acknowledge_remote_delivery(
                        managed_durable_run._manifest,  # noqa: SLF001
                        pending,
                    )
                    self_nonlocal.pending_remote_permit = None

            self._durable_initial_delivery_guard = _ManagedInitialDeliveryGuard()
        (run_dir / "steps").mkdir(parents=True, exist_ok=True)

        report = RunReport(
            workflow_name=workflow.name,
            started_at=(
                durable_run.started_at
                if durable_run is not None
                else datetime.now(timezone.utc).isoformat()
            ),
            execution_profile=(
                self.governed_authorization.execution_profile
                if self.governed_authorization is not None
                else "demo"
            ),
            execution_target_kind=execution_target_kind,
            recorded_surface=workflow.surface,
            surface_override=surface_override,
            execution_origin=execution_origin,
            execution_entry_url=execution_entry_url,
            bundle_content_digest=(
                workflow.manifest.content_digest if workflow.manifest else None
            ),
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            source_recording_sha256=(
                workflow.manifest.provenance.source_recording_sha256
                if workflow.manifest
                else None
            ),
            parameter_schema_sha256=(compute_parameter_schema_digest(workflow)),
            run_id_sha256=hashlib.sha256(self._run_id.encode("utf-8")).hexdigest(),
            params=params,
            model_calls=prior_model_calls,
            external_network_calls=prior_external_network_calls,
            screenshots_may_leave_box=(
                self._screenshots_may_leave_box or prior_screenshots_may_leave_box
            ),
            idempotency_key=idempotency_key,
        )
        snapshot_refusal = self._workflow_snapshot_refusal(workflow)
        if snapshot_refusal is None:
            snapshot_refusal = self._governed_authorization_snapshot_refusal()
        if snapshot_refusal is not None:
            report.results.append(
                StepResult(
                    step_id="<authorization>",
                    intent="snapshot governed execution admission",
                    ok=False,
                    safety_halt=True,
                    failure_category="governed_refusal",
                    error=snapshot_refusal,
                )
            )
            report.success = False
            return self._finalize_report(report, workflow, run_dir)
        self._begin_control_overlay(report.execution_profile)
        # At-most-once idempotency (Section 3): a repeat under an already-seen
        # key is SUPPRESSED before any actuation. A durable RESUME carries the
        # same logical run_id and legitimately re-enters with the same key, so it
        # is exempt (its own completed-effect ledger already prevents re-writes).
        if (
            idempotency_key is not None
            and self.idempotency_ledger is not None
            and not durable_resume
        ):
            from openadapt_flow.transaction import DuplicateActuation

            try:
                # One atomic reservation is the check. A separate ``seen``
                # call would let two runtime processes both pass the check
                # before either persisted ownership.
                self.idempotency_ledger.reserve(
                    idempotency_key,
                    run_id=self._run_id,
                )
                self._idempotency_reservation_owned = True
            except DuplicateActuation:
                report.results.append(
                    StepResult(
                        step_id="<idempotency>",
                        intent="suppress duplicate actuation for idempotency key",
                        ok=False,
                        failure_category="governed_refusal",
                        error=(
                            "a run with this idempotency key already actuated; "
                            "refusing to re-actuate the consequential write "
                            "-- reconcile current state before resuming"
                        ),
                    )
                )
                report.idempotent_replay = True
                report.success = False
                return self._finalize_report(report, workflow, run_dir)
        elif (
            idempotency_key is not None
            and self.idempotency_ledger is not None
            and durable_resume
        ):
            # A resumed durable leg did not create a new reservation.  It may
            # project the final outcome only when the existing reservation is
            # bound to this exact stable run identity.
            reservation_error: Optional[str]
            try:
                existing = self.idempotency_ledger.lookup(idempotency_key)
            except Exception as exc:  # noqa: BLE001 - durable ledger boundary
                existing = None
                reservation_error = type(exc).__name__
            else:
                reservation_error = None
            if existing is None or existing.get("run_id") != self._run_id:
                detail = (
                    f" ({reservation_error})" if reservation_error is not None else ""
                )
                report.results.append(
                    StepResult(
                        step_id="<idempotency>",
                        intent="validate durable idempotency reservation",
                        ok=False,
                        safety_halt=True,
                        failure_category="governed_refusal",
                        error=(
                            "durable continuation has no exact owned idempotency "
                            "reservation; refusing to resume before actuation" + detail
                        ),
                    )
                )
                report.success = False
                return self._finalize_report(report, workflow, run_dir)
            self._idempotency_reservation_owned = True
        if self.governed_authorization is not None:
            profile_refusal = self._profile_runtime_refusal(workflow)
            if profile_refusal is not None:
                report.results.append(
                    StepResult(
                        step_id="<profile>",
                        intent="validate execution profile runtime",
                        ok=False,
                        failure_category="governed_refusal",
                        error=f"{profile_refusal} — refusing to run; run aborted",
                    )
                )
                report.success = False
                return self._finalize_report(report, workflow, run_dir)
            refusal, assets = self.governed_authorization.validate_execution_snapshot(
                workflow,
                bundle_dir=bundle_dir,
                params=params,
                worklists=worklists,
                interstitials=list(self._interstitials),
                continuation=self.governed_continuation,
            )
            self._governed_asset_snapshot = assets
            assert workflow.manifest is not None
            self._governed_asset_hashes = dict(workflow.manifest.file_hashes)
            self._governed_plaintext_assets = not workflow.encrypted
            self._governed_asset_mutation = None
            report.governed_authorization_id = (
                self.governed_authorization.authorization_id
            )
            report.governed_approval_source = (
                self.governed_authorization.approval_source
            )
            report.governed_authorization_created_at = (
                self.governed_authorization.created_at
            )
            report.governed_policy_name = (
                self.governed_authorization.admitted_policy_name
            )
            report.governed_policy_contract_sha256 = (
                self.governed_authorization.admitted_policy_contract_sha256
            )
            report.governed_minimum_effect_tier = (
                self.governed_authorization.minimum_effect_tier
            )
            report.governed_qualified_effect_requirements = list(
                self.governed_authorization.qualified_effect_requirements
            )
            report.governed_runtime_inputs_digest = (
                self.governed_authorization.runtime_inputs_digest
            )
            report.governed_qualification_project_id = (
                self.governed_authorization.qualification_project_id
            )
            report.governed_qualification_project_revision = (
                self.governed_authorization.qualification_project_revision
            )
            report.governed_qualification_project_contract_sha256 = (
                self.governed_authorization.qualification_project_contract_sha256
            )
            report.governed_qualification_campaign_id_sha256 = (
                self.governed_authorization.qualification_campaign_id_sha256
            )
            report.governed_qualification_case_id_sha256 = (
                self._qualification_id_sha256(
                    self.governed_authorization.qualification_case_id
                )
            )
            report.governed_qualification_case_input_sha256 = (
                self.governed_authorization.qualification_case_input_sha256
            )
            report.governed_qualification_run_id_sha256 = (
                self.governed_authorization.qualification_run_id_sha256
            )
            report.governed_qualification_case_kind = (
                self.governed_authorization.qualification_case_kind
            )
            self._qualification_case_action_paths = dict(
                self.governed_authorization.qualification_case_action_paths
            )
            report.governed_qualification_case_action_paths = dict(
                self._qualification_case_action_paths
            )
            report.governed_qualification_fault_driver_id = (
                self.governed_authorization.qualification_fault_driver_id
            )
            report.governed_qualification_fault_driver_contract_sha256 = (
                self.governed_authorization.qualification_fault_driver_contract_sha256
            )
            report.governed_qualification_fault_driver_key_id = (
                self.governed_authorization.qualification_fault_driver_key_id
            )
            report.governed_qualification_fault_step_id_sha256 = (
                self.governed_authorization.qualification_fault_step_id_sha256
            )
            report.required_identity_step_ids = list(
                self.governed_authorization.required_identity_step_ids
            )
            report.approved_unverified_effect_step_ids = [
                approval.step_id
                for approval in self.governed_authorization.unverified_write_approvals
            ]
            report.governed_authorized_effect_contracts = {
                approval.step_id: list(approval.effect_contract_hashes)
                for approval in self.governed_authorization.unverified_write_approvals
            }
            if refusal is not None:
                report.results.append(
                    StepResult(
                        step_id="<authorization>",
                        intent="validate governed run authorization",
                        ok=False,
                        failure_category="governed_refusal",
                        error=f"{refusal} — refusing to run; run aborted",
                    )
                )
                report.success = False
                return self._finalize_report(report, workflow, run_dir)
            environment_refusal = self._observe_qualification_environment(
                workflow,
                report,
                execution_target_kind=execution_target_kind,
            )
            if environment_refusal is not None:
                report.results.append(
                    StepResult(
                        step_id="<qualification-environment>",
                        intent="observe exact qualification environment",
                        ok=False,
                        failure_category="governed_refusal",
                        error=environment_refusal,
                    )
                )
                report.success = False
                return self._finalize_report(report, workflow, run_dir)
            driver_refusal = self._qualification_fault_driver_refusal(workflow)
            if driver_refusal is not None:
                report.results.append(
                    StepResult(
                        step_id="<qualification-fault-driver>",
                        intent="bind the environment-owned fault driver",
                        ok=False,
                        failure_category="runtime_failure",
                        error=driver_refusal,
                    )
                )
                report.success = False
                return self._finalize_report(report, workflow, run_dir)
        self._record_identity_coverage(workflow, report)
        new_crops: dict[str, bytes] = {}
        # Per-run reset; the attribute is declared in __init__.
        self._last_click_point = None
        self._last_click_region = None
        t_run = time.monotonic()

        # Fail fast, naming them, when a REQUIRED typed parameter has no value
        # (neither a caller override nor a recorded example) -- never start a
        # run that will substitute a hole into a step.
        missing = sorted(
            pname
            for pname, spec in workflow.param_specs.items()
            if spec.required
            and (
                pname not in params
                or (isinstance(params[pname], str) and params[pname] == "")
            )
        )
        if missing:
            report.results.append(
                StepResult(
                    step_id="<params>",
                    intent="validate required workflow parameters",
                    ok=False,
                    failure_category="governed_refusal",
                    error=(
                        "Required workflow parameter(s) not supplied and no "
                        "recorded example is available: "
                        + ", ".join(missing)
                        + " — refusing to run; run aborted"
                    ),
                )
            )
            report.success = False
            report.total_ms = (time.monotonic() - t_run) * 1000.0
            return self._finalize_report(report, workflow, run_dir)

        if resume_from is not None:
            from openadapt_flow.runtime.durable import resumed_step_results

            resumed_results = resumed_step_results(
                run_dir,
                workflow,
                resume_from,
                key=self.checkpoint_key,
                checkpoints=list(self._durable_linear_snapshot),
                run_id=self._run_id,
            )
            report.results.extend(resumed_results)
            for result in resumed_results:
                self._account_result(report, result, account_model_calls=False)
            report.model_calls = max(
                report.model_calls,
                sum(self._result_model_calls(result) for result in resumed_results),
            )

        if workflow.program is not None:
            # Workflow-program IR, Phase 2: interpret the STATE MACHINE (loops /
            # branches / subflows / exception paths). When durability is enabled
            # the interpreter checkpoints its whole state after each verified
            # state and, on a halt, durably PAUSES; ``resume_program`` RESTORES
            # that interpreter state instead of restarting from the graph entry.
            self._interpret_program(
                workflow,
                params=params,
                worklists=worklists or {},
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
                durable_run=durable_run,
                resume_checkpoint=resume_program,
            )
        else:
            for step_index, step in enumerate(workflow.steps):
                # Resume: never re-execute an already-verified step (no
                # re-performed confirmed writes); its result was pre-loaded.
                if resume_from is not None and step_index < resume_from:
                    continue
                result = self._run_step(
                    step,
                    workflow=workflow,
                    step_index=step_index,
                    params=params,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                    new_crops=new_crops,
                )
                post_action_refusal = self._workflow_snapshot_refusal(workflow)
                if post_action_refusal is None:
                    post_action_refusal = (
                        self._governed_authorization_snapshot_refusal()
                    )
                if post_action_refusal is not None:
                    prior_error = (
                        f"; earlier result: {result.error}" if result.error else ""
                    )
                    result.ok = False
                    result.safety_halt = True
                    result.failure_category = (
                        "governed_refusal"
                        if self.governed_authorization is not None
                        else "safety_halt"
                    )
                    result.error = (
                        f"{post_action_refusal}; the action result was checked, "
                        f"but the workflow changed during delivery{prior_error}"
                    )
                report.results.append(result)
                self._account_result(report, result)
                if durable_run is not None:
                    self._sync_durable_audit(durable_run, report)
                    # Tier-3: verified step -> checkpoint; halt -> pending
                    # escalation (resumable from the last checkpoint).
                    if post_action_refusal is None:
                        durable_run.record(
                            step_index,
                            step,
                            result,
                            params,
                            workflow=workflow,
                            transition_observation=(
                                self._attended_transition_observation()
                                if not result.ok
                                else None
                            ),
                        )
                        if (
                            self._durable_continuation_guard is not None
                            and result.failure_category != "continuation_preempted"
                        ):
                            self._durable_continuation_guard.acknowledge_progress(
                                terminal=not result.ok
                            )
                if not result.ok:
                    break

            report.success = len(report.results) == len(workflow.steps) and all(
                result.ok for result in report.results
            )
        report.total_ms = (time.monotonic() - t_run) * 1000.0

        # Emit the structured HALT record on any unsuccessful run, so the
        # halt->learn loop (openadapt_flow.learning.halt_loop) can lift it into
        # the trace corpus. No-op on a successful run.
        if not report.success:
            self._emit_halt(report)

        if save_healed_to is not None:
            heal_mod.write_healed_bundle(
                workflow, bundle_dir, Path(save_healed_to), new_crops
            )
            if new_crops:
                # Governed repair promotion (Section 9): the healed bundle is
                # a lifecycle CANDIDATE, never an implicitly active bundle. A
                # detached repair/candidate.json (binding diff by digest,
                # privacy-safe failure fingerprints, contract-invariant
                # verdict) is written into it; promotion to active requires
                # the explicit lifecycle (campaigns, human approval, staged
                # canary) driven by `openadapt-flow repair`. Registration
                # failure fails CLOSED by absence: without the record the
                # healed bundle cannot enter the promotion lifecycle at all.
                from openadapt_flow.repair.registration import (
                    register_bundle_candidate,
                )

                try:
                    register_bundle_candidate(
                        Path(bundle_dir),
                        Path(save_healed_to),
                        source="heal",
                        evidence_run_dir=Path(run_dir),
                    )
                except Exception as exc:
                    print(
                        "repair candidate registration failed (the healed "
                        f"bundle stays ungoverned and non-promotable): {exc}"
                    )

        staged_durable_terminal = (
            durable_resume and durable_run is not None and report.success
        )
        finalized = self._finalize_report(
            report,
            workflow,
            run_dir,
            persist=not staged_durable_terminal,
            project_terminal=not staged_durable_terminal,
        )
        if staged_durable_terminal:
            # Persist the terminal evidence before consuming the continuation
            # pause. A crash can then leave either a restartable checkpoint or
            # an exact successful report, never a cleared pause with only the
            # stale pre-resume report.
            if self._durable_continuation_guard is not None:
                assert durable_run is not None
                candidate = finalized.save(
                    run_dir, filename=".report.terminal-candidate.json"
                )
                report_sha256 = self._durable_continuation_guard.prepare_terminal(
                    candidate
                )
                durable_run.complete()
                os.replace(candidate, run_dir / "report.json")
                from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

                CheckpointStore._fsync_directory(run_dir)
                self._durable_continuation_guard.completed(report_sha256=report_sha256)
                # Do not project before ``completed``. A crash or authority
                # failure between projection and durable completion would make
                # the ledger claim a terminal outcome while the pause remains
                # restartable. ``pending`` keeps this completion
                # non-attestable until the projection settles or one typed
                # fail-closed report correction is bound.
                try:
                    self._record_idempotency_outcome(finalized)
                except Exception as exc:  # noqa: BLE001 - durable ledger boundary
                    self._mark_idempotency_projection_failure(
                        finalized,
                        workflow,
                        run_dir,
                        exc,
                    )
                    corrected = finalized.save(run_dir)
                    self._durable_continuation_guard.correct_terminal_projection_failure(
                        original_report_sha256=report_sha256,
                        corrected_report_path=corrected,
                    )
                else:
                    self._durable_continuation_guard.settle_terminal_projection(
                        report_sha256=report_sha256
                    )
                self._emit_control_overlay_terminal(finalized.execution_outcome)
            else:
                # Authenticated durable continuation always installs the
                # shared guard. Refuse to consume its pause if that admission
                # invariant is ever lost.
                raise RuntimeError(
                    "durable continuation lost its terminal authority guard"
                )
        return finalized

    def _stamp_execution_outcome(
        self, report: RunReport, workflow: Workflow, run_dir: Path
    ) -> None:
        """Apply the named profile's evidence contract before persistence."""

        if report.execution_profile is None:
            return
        from openadapt_flow.execution_profiles import stamp_execution_outcome

        stamp_execution_outcome(
            report,
            workflow,
            report.execution_profile,
            runtime_worklists=self._outcome_worklists,
            transition_evidence_root=run_dir,
            transition_predicate_vision=self.vision,
        )

    def _begin_control_overlay(self, profile: Optional[str]) -> None:
        """Start an optional presentation rail without affecting execution."""

        self.control_overlay_error = None
        self._control_overlay_disabled = False
        self._control_overlay_last_observation_png = None
        if self.control_overlay is None:
            return
        try:
            if profile is None:
                raise ValueError("run report has no execution profile")
            self.control_overlay.begin(profile=profile)
            # Prime browser-only presentation metadata at the run boundary,
            # never between final target revalidation and one-shot delivery.
            # Subsequent reads are cache-only and fail closed if viewport
            # dimensions changed.
            if isinstance(self.backend, BrowserPresentationGeometryBackend):
                self.backend.browser_presentation_viewport()
        except Exception as exc:
            self.control_overlay_error = type(exc).__name__
            self._control_overlay_disabled = True

    def _emit_control_overlay_phase(
        self,
        phase: str,
        *,
        current_step: Optional[int] = None,
        total_steps: Optional[int] = None,
        target_tracking: Optional[Any] = None,
        observation_png: Optional[bytes] = None,
    ) -> None:
        if self.control_overlay is None or self._control_overlay_disabled:
            return
        try:
            self.control_overlay.emit_phase(
                phase,
                current_step=current_step,
                total_steps=total_steps,
                target_tracking=target_tracking,
                observation_png=observation_png,
            )
            if observation_png is not None:
                self._control_overlay_last_observation_png = observation_png
        except Exception as exc:
            self.control_overlay_error = type(exc).__name__
            self._control_overlay_disabled = True

    def _control_overlay_browser_target(
        self,
        step: Step,
        resolution: Optional[Resolution],
        matched_region: Optional[Region],
        observation_png: bytes,
    ) -> Optional[Any]:
        """Build presentation-only geometry from the final resolved target.

        Only a backend that explicitly advertises top-level browser CSS
        geometry can produce this V2 target.  Missing/invalid presentation
        metadata omits the rectangle without disabling the status rail or
        influencing execution.
        """

        if (
            self.control_overlay is None
            or self._control_overlay_disabled
            or resolution is None
            or not isinstance(self.backend, BrowserPresentationGeometryBackend)
        ):
            return None
        viewport = self.backend.browser_presentation_viewport()
        if viewport is None:
            return None
        # Visual ladder regions are screenshot-pixel coordinates.  The
        # reference browser runner records CSS-scale screenshots (DPR 1); for
        # any other DPR, omit visual tracking until an exact pixel-to-CSS
        # projection is retained.  Structural handle regions are already CSS
        # viewport coordinates and remain exact at any DPR.
        if resolution.structural_handle is None and viewport[2] != 1.0:
            return None
        region = (
            resolution.structural_handle.region
            if resolution.structural_handle is not None
            and resolution.structural_handle.region is not None
            else matched_region
        )
        if region is None:
            return None
        action_kind = {
            ActionKind.CLICK: "click",
            ActionKind.DOUBLE_CLICK: "double_click",
            ActionKind.RIGHT_CLICK: "right_click",
            ActionKind.DRAG: "drag",
            ActionKind.TYPE: "type",
            ActionKind.SELECT_OPTION: "type",
            ActionKind.SCROLL: "scroll",
        }.get(step.action)
        if action_kind is None:
            return None
        try:
            return self.control_overlay.browser_target_tracking(
                observation_png=observation_png,
                region_css_px=region,
                viewport=viewport,
                action_kind=action_kind,
            )
        except Exception:
            return None

    def _emit_control_overlay_terminal(self, outcome: Optional[str]) -> None:
        if self.control_overlay is None or self._control_overlay_disabled:
            return
        try:
            if outcome is None:
                raise ValueError("run report has no exact execution outcome")
            self.control_overlay.emit_terminal(
                outcome,
                observation_png=self._control_overlay_last_observation_png,
            )
        except Exception as exc:
            self.control_overlay_error = type(exc).__name__
            self._control_overlay_disabled = True

    def _finalize_report(
        self,
        report: RunReport,
        workflow: Workflow,
        run_dir: Path,
        *,
        persist: bool = True,
        project_terminal: bool = True,
    ) -> RunReport:
        """Persist exact evidence first, then project its terminal outcome."""

        report.qualification_fault_mutations = list(self._qualification_fault_mutations)
        self._stamp_execution_outcome(report, workflow, run_dir)
        if persist:
            persisted_path = report.save(run_dir)
            # The evidence file must exist before the terminal projection.  A
            # projection error is a runtime failure, but it cannot erase the
            # completed action evidence or make the same key available again.
            durable_state = self._durable_terminal_state(run_dir)
            if durable_state == "indeterminate":
                self._mark_idempotency_projection_failure(
                    report,
                    workflow,
                    run_dir,
                    RuntimeError("durable terminal state is unavailable"),
                    stage="durable terminal state persistence",
                )
                report.save(run_dir, filename=persisted_path.name)
            elif durable_state != "restartable":
                self._project_idempotency_outcome_after_persist(
                    report,
                    workflow,
                    run_dir,
                    persisted_path=persisted_path,
                )
        clear_guard = getattr(self.backend, "set_qualification_input_guard", None)
        if callable(clear_guard):
            clear_guard(None)
        if project_terminal:
            self._emit_control_overlay_terminal(report.execution_outcome)
        return report

    def _project_idempotency_outcome_after_persist(
        self,
        report: RunReport,
        workflow: Workflow,
        run_dir: Path,
        *,
        persisted_path: Path,
    ) -> None:
        """Project a durable report into the owned idempotency reservation.

        The first save is the evidence boundary.  If the ledger cannot accept
        the projection, retain that evidence and replace it with a typed,
        fail-closed report.  The immutable reservation remains in force, so
        the runtime never re-dispatches the action to recover from this error.
        """

        if not self._idempotency_reservation_owned:
            return
        try:
            self._record_idempotency_outcome(report)
        except Exception as exc:  # noqa: BLE001 - durable ledger boundary
            self._mark_idempotency_projection_failure(report, workflow, run_dir, exc)
            report.save(run_dir, filename=persisted_path.name)

    def _durable_terminal_state(
        self, run_dir: Path
    ) -> Literal["not_durable", "restartable", "terminal", "indeterminate"]:
        """Classify the durable state that controls terminal projection.

        The idempotency reservation stays outcome-free across a restartable
        durable halt. Only the logical terminal continuation writes its final
        outcome. This prevents a write-once ledger from recording ``HALTED``
        before a later approved resume can record ``VERIFIED``.
        """

        if not self.durable:
            return "not_durable"
        try:
            from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

            pending = CheckpointStore(run_dir, key=self.checkpoint_key).read_pending()
        except Exception:  # noqa: BLE001 - durable evidence boundary
            return "indeterminate"
        if pending is not None and pending.status in {
            "pending",
            "approved",
            "continuing",
        }:
            return "restartable"
        return "terminal"

    def _mark_idempotency_projection_failure(
        self,
        report: RunReport,
        workflow: Workflow,
        run_dir: Path,
        exc: Exception,
        *,
        stage: str = "terminal idempotency outcome persistence",
    ) -> None:
        """Turn a post-save ledger failure into one typed terminal failure."""

        report.results.append(
            StepResult(
                step_id="<idempotency>",
                intent="persist terminal idempotency outcome",
                ok=False,
                failure_category="runtime_failure",
                error=(
                    f"{stage} failed "
                    f"({type(exc).__name__}); retained action evidence "
                    "requires reconciliation and the action was not retried"
                ),
            )
        )
        # Do not preserve a successful or completed status when the exact
        # terminal projection is unavailable. The transaction classifier will
        # conservatively produce RECONCILIATION_REQUIRED if an action may have
        # changed the business system.
        report.success = False
        report.execution_completed = False
        self._stamp_execution_outcome(report, workflow, run_dir)

    def _record_idempotency_outcome(self, report: RunReport) -> None:
        """Commit a terminal outcome only into this run's owned reservation."""

        if (
            self._idempotency_reservation_owned
            and self.idempotency_ledger is not None
            and report.idempotency_key is not None
            and not report.idempotent_replay
        ):
            if report.transaction_outcome is None:
                raise RuntimeError("terminal transaction outcome is missing")
            self.idempotency_ledger.record_outcome(
                report.idempotency_key,
                report.transaction_outcome,
                run_id=self._run_id,
            )

    @staticmethod
    def _control_overlay_progress(
        workflow: Workflow,
        step_index: int,
        graph_ctx: Optional["_GraphStepContext"],
    ) -> tuple[Optional[int], Optional[int]]:
        if workflow.program is not None or graph_ctx is not None:
            return None, None
        return step_index + 1, len(workflow.steps)

    def _profile_runtime_refusal(self, workflow: Workflow) -> Optional[str]:
        """Recheck load-bearing profile invariants at the actuation boundary."""

        authorization = self.governed_authorization
        if authorization is None or authorization.execution_profile is None:
            return None
        profile = authorization.execution_profile
        from openadapt_flow.execution_profiles import execution_profile_contract

        contract = execution_profile_contract(profile)
        if contract.require_durable and not self.durable:
            return f"{profile} execution requires the durable runtime"
        if contract.require_settled and not self.require_settled:
            return f"{profile} execution requires settled-state detection"
        if contract.require_encryption and not workflow.encrypted:
            return f"{profile} execution requires an encrypted bundle"
        if (
            contract.production and (contract.require_encryption or workflow.encrypted)
        ) and not self.checkpoint_key:
            return f"{profile} execution requires encrypted durable checkpoints"
        return None

    @staticmethod
    def _record_identity_coverage(workflow: Workflow, report: RunReport) -> None:
        """Record the bundle's identity-protection coverage on the report.

        Computed over the WHOLE workflow at run start (not just executed
        steps): every anchored click / double-click / TYPE / KEY step is
        identity-applicable; it is ARMED when the pre-actuation identity gate
        will actually run (``anchor.context_text`` present — the ground
        truth the gate itself keys on). Unarmed steps proceed with NO
        identity verification (docs/LIMITS.md), so each one is listed by
        id with the compile-time reason for the operator.
        """
        # Cover the whole bundle: the linear ``steps`` list, OR (program mode)
        # every ``action`` state's step across the program graph and all
        # subflows -- so a hand-authored/compiled PROGRAM's identity coverage is
        # audited exactly as a linear bundle's is.
        for step in _all_workflow_steps(workflow):
            if step.action not in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
                ActionKind.TYPE,
                ActionKind.SELECT_OPTION,
                ActionKind.KEY,
                ActionKind.HOTKEY,
            ):
                continue
            anchorless_submission = step.action in (
                ActionKind.KEY,
                ActionKind.HOTKEY,
            ) and is_consequential(step, workflow)
            if step.anchor is None and not anchorless_submission:
                continue
            report.identity_applicable_steps += 1
            if step.anchor is not None and (
                step.anchor.context_text
                or step.anchor.structured_identity
                or step.anchor.identity_template
                or step.anchor.identifier_crop
            ):
                report.identity_armed_steps += 1
            else:
                report.identity_unarmed.append(
                    UnarmedStep(
                        step_id=step.id,
                        intent=step.intent,
                        reason=step.identity_unarmed_reason
                        or (
                            "consequential keyboard action has no declared "
                            "record identity evidence"
                            if anchorless_submission
                            else None
                        )
                        or (
                            "no identity context recorded at compile time"
                            " (bundle predates the armed-coverage audit"
                            " field)"
                        ),
                    )
                )

    def _emit_halt(self, report: RunReport) -> None:
        """Populate ``report.halt`` with the structured HALT record.

        Captures WHERE the run stopped (the last failed step / the program
        terminal), WHAT unexpected on-screen text was observed there (probed from
        the current frame, PHI-scrubbed), and the PRE-context (the intents that
        completed before the halt). This is the ONLY thing the runtime does with
        the learning loop: it emits an audit record shaped exactly like an
        ExecutionTrace's (intents + observed facts); the learning bridge decides
        what to do with it (openadapt_flow.learning.halt_loop). Never raises —
        an emission failure must not turn a halt into a crash.
        """
        try:
            results = report.results
            # The halted step: the last executed step that FAILED (skip the
            # synthetic <terminal>/<params> markers the interpreter appends).
            failed = [
                r
                for r in results
                if not r.ok
                and not r.skipped
                and r.step_id not in ("<terminal>", "<params>")
            ]
            halted = failed[-1] if failed else (results[-1] if results else None)
            completed = [r.intent for r in results if r.ok and not r.skipped]
            reason = ""
            state_id = ""
            intent = ""
            if halted is not None:
                reason = halted.error or ""
                state_id = halted.step_id
                intent = halted.intent
            # If a program terminal recorded the reason, prefer its text.
            for r in results:
                if r.step_id == "<terminal>" and r.error:
                    reason = reason or r.error
            observed = self._observe_screen_texts()
            report.halt = HaltObservation(
                state_id=state_id,
                intent=intent,
                reason=reason,
                outcome=report.terminal_outcome or "halt",
                observed_texts=observed,
                completed_intents=completed,
            )
        except Exception:  # pragma: no cover - emission must never crash a run
            pass

    def _observe_screen_texts(self) -> list[str]:
        """Read the on-screen text at the halt point (PHI-scrubbed).

        The unexpected UI state the compiled program had no branch for — the
        text a learned branch's ``TEXT_PRESENT`` guard will key on. Uses the
        backend's current frame through the SAME OCR the runtime already uses
        for text presence; returns [] when OCR is unavailable (pixel-only
        substrate / a vision stub without ocr)."""
        try:
            frame = self.backend.screenshot()
        except Exception:
            return []
        ocr = getattr(self.vision, "ocr", None)
        if ocr is None:
            return []
        try:
            lines = ocr(frame)
        except Exception:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for line in lines or []:
            text = getattr(line, "text", None)
            if not text:
                continue
            # ``text`` is truthy here, so scrub_text returns a str; ``or ""``
            # only satisfies its Optional[str] return signature.
            scrubbed = (_scrub_phi(text) or "").strip()
            if scrubbed and scrubbed not in seen:
                seen.add(scrubbed)
                out.append(scrubbed)
        return out

    @staticmethod
    def _account_result(
        report: RunReport,
        result: StepResult,
        *,
        account_model_calls: bool = True,
    ) -> None:
        """Fold one StepResult's rung / model-call / heal counts into the
        report. Shared by the linear loop and the program interpreter so both
        account a step identically (a rescued or grounded run is never counted
        as a zero-model run)."""
        if result.ok and result.resolution is not None:
            rung = result.resolution.rung
            report.rung_counts[rung] = report.rung_counts.get(rung, 0) + 1
        elif result.ok and result.actuation == "api":
            # API-tier actuation has no visual resolution rung; count it under
            # "api" so the report shows the deterministic top of the ladder in
            # the same place as the visual rungs. Zero model calls.
            report.rung_counts["api"] = report.rung_counts.get("api", 0) + 1
        # Drift-oracle state-verifier calls are model calls too (honest
        # accounting).
        if account_model_calls:
            report.model_calls += Replayer._result_model_calls(result)
        if result.heal is not None:
            report.heal_count += 1

    @staticmethod
    def _result_model_calls(result: StepResult) -> int:
        return int(
            result.drift_oracle_calls
            + (
                1
                if (
                    result.ok
                    and result.resolution is not None
                    and result.resolution.rung == "grounder"
                )
                else 0
            )
        )

    @staticmethod
    def _sync_durable_audit(durable_run: Any, report: RunReport) -> None:
        """Persist whole-run counters before a leg can durably pause."""

        from openadapt_flow.execution_profiles import _external_network_call_state

        report.external_network_calls = _external_network_call_state(report)
        durable_run.update_audit_evidence(
            screenshots_may_leave_box=report.screenshots_may_leave_box,
            model_calls=report.model_calls,
            external_network_calls=report.external_network_calls,
        )

    # -- Workflow-program IR, Phase 2: the state-machine interpreter -----------
    #
    # Deterministic graph interpreter ($0, ZERO model calls on the happy path).
    # It REUSES the linear per-action machinery unchanged: every ``action``
    # state is executed by ``_run_step`` -- the SAME settle / resolve / identity
    # gate / effect verify / risk gate / heal pipeline the linear replayer runs
    # -- so no safety property is weakened by adding control flow AROUND the
    # hardened leaf. The interpreter only adds: which state runs next (guarded
    # transitions), loops over a worklist, subflow dispatch, and routing a
    # failed action to a local exception handler instead of aborting.

    def _interpret_program(
        self,
        workflow: Workflow,
        *,
        params: dict[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
        durable_run: Optional[Any] = None,
        resume_checkpoint: Optional[ProgramCheckpoint] = None,
    ) -> None:
        """Interpret ``workflow.program`` as a state machine, writing results
        and the terminal outcome onto ``report``.

        Normal completion (a ``success`` terminal, or falling off a graph with
        no further transition) => ``report.success = True``. Any ``_ProgramHalt``
        -- an unhandled action failure, a dead-end branch, a ``halt`` /
        ``escalate`` terminal, or a safety bound exceeded -- stops the whole run
        with ``success = False`` and records the reason.

        Durability (RFC §5, Tier 3): when ``durable_run`` is supplied, the
        interpreter checkpoints its whole state (frame stack, loop cursors, bound
        params, completed effect keys) after each verified ``action`` state and,
        on a halt, writes a durable PAUSE. When ``resume_checkpoint`` is supplied
        the interpreter state is RESTORED from it and execution continues from the
        paused state -- never from the graph entry.
        """
        assert workflow.program is not None
        self._prev_action: Optional[ActionKind] = None
        self._program_step_budget = PROGRAM_MAX_STEPS
        # -- durable interpreter state (Phase-2, RFC §5) ---------------------
        self._program_durable = durable_run
        self._program_run_dir = run_dir
        self._program_governed_runtime_inputs_digest = (
            report.governed_runtime_inputs_digest
        )
        self._active_business_decision_evidence = report.business_decision_evidence
        self._frame_stack: list[dict] = []
        # Where the interpreter currently is (for a durable pause record).
        self._current_state_id: str = ""
        self._current_intent: str = ""
        self._current_params: dict[str, RuntimeParamScalar] = dict(params)
        if durable_run is not None:
            self._bundle_version = _bundle_version(bundle_dir)
            if self._durable_resume_mode == "program":
                checkpoints = list(self._durable_program_snapshot)
                pending_snapshot = self._durable_pending_snapshot
                if (
                    checkpoints
                    and pending_snapshot is not None
                    and pending_snapshot.program
                    and len(checkpoints) > pending_snapshot.program_checkpoint_seq
                ):
                    durable_base_history = list(checkpoints[-1].transition_history)
                elif pending_snapshot is not None and pending_snapshot.program:
                    pending_history = list(pending_snapshot.program_history)
                    pending_delta = list(pending_snapshot.program_history_delta)
                    pause_frame = (
                        pending_snapshot.program_frames[-1]
                        if pending_snapshot.program_frames
                        else None
                    )
                    pause_graph = (
                        workflow.program
                        if pause_frame is not None
                        and pause_frame.graph_id == TOP_GRAPH_ID
                        else (
                            workflow.subflows.get(pause_frame.graph_id)
                            if pause_frame is not None
                            else None
                        )
                    )
                    pause_state = (
                        pause_graph.states.get(pause_frame.state_id)
                        if pause_graph is not None and pause_frame is not None
                        else None
                    )
                    if (
                        pause_state is not None
                        and pause_state.kind is StateKind.BUSINESS_DECISION
                    ):
                        # The decision state has not executed until its signed
                        # answer is consumed. Remove that paused visit before
                        # replaying it, or a later pause counts it twice.
                        durable_base_history = (
                            pending_history[: -len(pending_delta)]
                            if pending_delta
                            else pending_history
                        )
                    else:
                        durable_base_history = pending_history
                elif checkpoints:
                    durable_base_history = list(checkpoints[-1].transition_history)
                else:
                    durable_base_history = []
                successful_history = self._program_checkpoint_history(checkpoints)
                if successful_history is None:
                    raise _ProgramHalt(
                        "halt",
                        "durable program checkpoints have no exact successful "
                        "execution trace",
                        safety=True,
                    )
                report.visited_states.extend(successful_history)
                for checkpoint in checkpoints:
                    report.program_transition_evidence.extend(
                        checkpoint.program_transition_evidence_delta
                    )
                    report.business_decision_evidence.extend(
                        checkpoint.business_decision_evidence_delta
                    )
                    report.program_exception_evidence.extend(
                        checkpoint.program_exception_evidence_delta
                    )
                    if checkpoint.attended_transition_evidence is not None:
                        report.attended_program_transition_evidence.append(
                            checkpoint.attended_transition_evidence
                        )
                self._program_history_boundary_index = len(successful_history)
                self._program_durable_history = durable_base_history
                self._program_history_parent_hash = _history_hash(durable_base_history)
                self._program_seq = len(checkpoints)
                self._completed_effect_keys = [
                    key
                    for checkpoint in checkpoints
                    for key in checkpoint.new_effect_keys
                ]
                self._completed_effect_evidence = [
                    evidence
                    for checkpoint in checkpoints
                    for evidence in checkpoint.new_effect_evidence
                ]
                self._completed_unverified_effect_keys = [
                    key
                    for checkpoint in checkpoints
                    for key in checkpoint.new_unverified_effect_keys
                ]
                resumed_results = self._resumed_program_results(checkpoints, workflow)
                report.results.extend(resumed_results)
                for result in resumed_results:
                    self._account_result(report, result, account_model_calls=False)
                report.model_calls = max(
                    report.model_calls,
                    sum(self._result_model_calls(result) for result in resumed_results),
                )
            else:
                # A fresh program run must not import state from its output
                # directory. DurableRun owns a newly claimed empty namespace.
                self._program_seq = 0
                self._program_history_boundary_index = 0
                self._program_durable_history = []
                self._program_history_parent_hash = _history_hash([])
                self._completed_effect_keys = []
                self._completed_effect_evidence = []
                self._completed_unverified_effect_keys = []
            self._program_checkpoint_history_len = len(report.visited_states)
            self._program_transition_evidence_checkpoint_len = len(
                report.program_transition_evidence
            )
            self._business_decision_evidence_checkpoint_len = len(
                report.business_decision_evidence
            )
            self._program_exception_evidence_checkpoint_len = len(
                report.program_exception_evidence
            )
        else:
            self._program_seq = 0
            self._program_history_boundary_index = 0
            self._program_durable_history = []
            self._program_history_parent_hash = _history_hash([])
            self._completed_effect_keys = []
            self._completed_effect_evidence = []
            self._completed_unverified_effect_keys = []
            self._bundle_version = ""
            self._program_checkpoint_history_len = 0
            self._program_transition_evidence_checkpoint_len = 0
            self._business_decision_evidence_checkpoint_len = 0
            self._program_exception_evidence_checkpoint_len = 0
        try:
            if resume_checkpoint is not None:
                # RESTORE the interpreter state and continue from the pause.
                self._resume_program_state(
                    resume_checkpoint,
                    workflow=workflow,
                    worklists=worklists,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                    report=report,
                    new_crops=new_crops,
                )
            else:
                self._walk_graph(
                    workflow.program,
                    graph_id=TOP_GRAPH_ID,
                    workflow=workflow,
                    params=params,
                    worklists=worklists,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                    report=report,
                    new_crops=new_crops,
                    depth=0,
                )
            self._raise_on_governed_asset_mutation()
            self._raise_on_workflow_snapshot_mutation(workflow)
            report.success = True
            if report.terminal_outcome is None:
                # Fell off the top graph with no explicit terminal: a clean,
                # complete run (every executed state succeeded or was handled).
                report.terminal_outcome = "success"
        except _ProgramHalt as halt:
            report.success = False
            report.terminal_outcome = halt.outcome
            # Durably PAUSE (never just die): capture WHERE we stopped so an
            # approved resume can RESTORE the interpreter from here.
            # A caller callback can mutate the live workflow. Never persist a
            # new resumable cursor under semantics that differ from the exact
            # run-entry snapshot. The report still records the refusal.
            if (
                self._program_durable is not None
                and self._workflow_snapshot_refusal(workflow) is None
            ):
                self._record_program_pause(halt, report, workflow=workflow)
            report.results.append(
                StepResult(
                    step_id="<terminal>",
                    intent=f"program {halt.outcome}",
                    ok=False,
                    error=halt.reason,
                    safety_halt=halt.safety,
                )
            )

    def _walk_graph(
        self,
        graph: ProgramGraph,
        *,
        graph_id: str,
        workflow: Workflow,
        params: dict[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
        depth: int,
        start: Optional[str] = None,
        loop_cursor: Optional[LoopCursor] = None,
    ) -> None:
        """Walk one graph from ``start`` (default ``entry``) to a terminal /
        fall-off.

        Pushes a durable :class:`GraphFrame`-shaped entry onto ``_frame_stack``
        for the whole walk (so a checkpoint written inside captures this level of
        the subflow/loop nesting) and pops it on the way out. ``graph_id`` is the
        durable id of this graph (``TOP_GRAPH_ID`` or a subflow name);
        ``loop_cursor`` is set for a loop-body iteration.

        Returns normally on completion (a ``success`` terminal RETURNS to the
        caller -- for a subflow that means "continue after the call"; for the
        top program it means the run succeeded). Raises ``_ProgramHalt`` to stop
        the ENTIRE run.
        """
        if depth > PROGRAM_MAX_DEPTH:
            raise _ProgramHalt(
                "halt",
                f"program nesting exceeded {PROGRAM_MAX_DEPTH} levels "
                "(possible unbounded recursion) — run aborted",
            )
        frame = {
            "graph_id": graph_id,
            "state_id": start if start is not None else graph.entry,
            "params": params,
            "loop": loop_cursor,
        }
        self._frame_stack.append(frame)
        try:
            self._run_states_from(
                graph,
                frame,
                workflow=workflow,
                params=params,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
                depth=depth,
            )
        finally:
            self._frame_stack.pop()

    def _run_states_from(
        self,
        graph: ProgramGraph,
        frame: dict,
        *,
        workflow: Workflow,
        params: dict[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
        depth: int,
    ) -> None:
        """Run ``frame``'s graph from ``frame['state_id']`` to a terminal /
        fall-off, keeping ``frame['state_id']`` pointed at the current state.

        Split out of :meth:`_walk_graph` so a RESUME can drive an
        already-pushed frame from a restored state (rather than always from
        ``entry``). Raises ``_ProgramHalt`` to stop the entire run.
        """
        state_id: Optional[str] = frame["state_id"]
        while state_id is not None:
            self._program_step_budget -= 1
            if self._program_step_budget <= 0:
                raise _ProgramHalt(
                    "halt",
                    f"program exceeded {PROGRAM_MAX_STEPS} state transitions "
                    "(possible non-terminating graph) — run aborted",
                )
            frame["state_id"] = state_id
            state = graph.states.get(state_id)
            if state is None:
                raise _ProgramHalt(
                    "halt",
                    f"program references undefined state '{state_id}' — run aborted",
                )
            report.visited_states.append(state.id)
            self._current_graph_id = str(frame["graph_id"])
            self._current_state_id = state.id
            self._current_intent = (
                state.step.intent
                if state.step is not None
                else (
                    state.decision.question if state.decision is not None else state.id
                )
            )
            self._current_params = dict(params)

            if state.kind is StateKind.TERMINAL:
                self._raise_on_governed_asset_mutation()
                self._raise_on_workflow_snapshot_mutation(workflow)
                outcome = state.outcome or "success"
                report.terminal_outcome = outcome
                if outcome == "success":
                    return  # RETURN to caller / top-program success
                raise _ProgramHalt(
                    outcome,
                    state.reason
                    or f"reached '{outcome}' terminal state '{state.id}' — run aborted",
                )

            state_id = self._exec_state(
                state,
                graph,
                workflow=workflow,
                params=params,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
                depth=depth,
            )
        # Fell off the graph (a state with no outgoing transition): normal
        # completion / subflow return.

    def _exec_state(
        self,
        state: State,
        graph: ProgramGraph,
        *,
        workflow: Workflow,
        params: dict[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
        depth: int,
    ) -> Optional[str]:
        """Execute one non-terminal state; return the next state id (or None to
        fall off the current graph). Raises ``_ProgramHalt`` on an unrecoverable
        state."""
        if state.kind is StateKind.ACTION:
            return self._exec_action_state(
                state,
                graph,
                workflow=workflow,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
            )

        if state.kind is StateKind.BRANCH:
            # A branch performs no action: it picks an outgoing edge purely by
            # guard (evaluated on the current frame). No matching edge is a
            # fail-safe HALT unless the state routes to an exception handler.
            nxt = self._select_transition(
                state,
                workflow=workflow,
                params=params,
                bundle_dir=bundle_dir,
                report=report,
                run_dir=run_dir,
                allow_unmatched=True,
            )
            if nxt is None:
                return self._on_state_failure(
                    state,
                    f"branch state '{state.id}' has no transitions to follow "
                    "— run aborted",
                    failure_kind="branch_without_transition",
                    report=report,
                    workflow=workflow,
                )
            return nxt

        if state.kind is StateKind.BUSINESS_DECISION:
            return self._exec_business_decision_state(
                state,
                workflow=workflow,
                params=params,
                bundle_dir=bundle_dir,
                report=report,
                run_dir=run_dir,
            )

        if state.kind is StateKind.LOOP:
            return self._exec_loop_state(
                state,
                workflow=workflow,
                params=params,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
                depth=depth,
            )

        if state.kind is StateKind.SUBFLOW_CALL:
            sub = workflow.subflows.get(state.subflow or "")
            if sub is None:
                return self._on_state_failure(
                    state,
                    f"subflow_call state '{state.id}' names undefined subflow "
                    f"'{state.subflow}' — run aborted",
                    failure_kind="missing_subflow",
                    report=report,
                    workflow=workflow,
                )
            self._walk_graph(
                sub,
                graph_id=state.subflow or "",
                workflow=workflow,
                params=params,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
                depth=depth + 1,
            )
            return self._select_transition(
                state,
                workflow=workflow,
                params=params,
                bundle_dir=bundle_dir,
                report=report,
                run_dir=run_dir,
            )

        raise _ProgramHalt(
            "halt", f"state '{state.id}' has unsupported kind {state.kind!r}"
        )

    def _exec_action_state(
        self,
        state: State,
        graph: ProgramGraph,
        *,
        workflow: Workflow,
        params: dict[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
    ) -> Optional[str]:
        """Run an ``action`` state's Step through the SHARED per-step pipeline,
        then pick the next transition. A genuine failure routes to
        ``on_exception`` (recorded as handled) or HALTs the run."""
        if state.step is None:
            raise _ProgramHalt("halt", f"action state '{state.id}' carries no step")
        action_step = state.step
        # Idempotency (RFC §5): on a RESUME, an action whose declared effects were
        # ALL already CONFIRMED in the pre-pause leg is NOT re-executed -- a
        # confirmed consequential write is never re-performed. Keyed on the
        # resolved effect contract hashes recorded in the completed-effect ledger.
        if self._skip_completed_effect_state(state, params, report):
            return self._select_transition(
                state,
                workflow=workflow,
                params=params,
                bundle_dir=bundle_dir,
                report=report,
                run_dir=run_dir,
            )
        ctx = self._build_graph_ctx(state, graph)
        # Keep the exact validated interpreter cursor from before any backend,
        # vision, overlay, or verifier callback. A callback can corrupt the
        # mutable live frame stack. A refusal must still persist a safe resume
        # cursor instead of serializing that corrupted state.
        pre_action_frames = [self._frame_to_model(frame) for frame in self._frame_stack]
        result = self._run_step(
            action_step,
            workflow=workflow,
            step_index=0,
            params=params,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            new_crops=new_crops,
            graph_ctx=ctx,
        )
        # Report the exact cursor that was validated before callbacks. The live
        # frame stack is mutable backend-facing state and can no longer be
        # trusted after an action callback starts.
        result.program_scope = self._program_scope_from_frames(pre_action_frames)
        try:
            post_action_refusal = self._active_program_frame_refusal(
                workflow,
                params,
                action_step,
                self._governed_base_params or {},
            )
        except Exception as exc:  # noqa: BLE001 - mutated interpreter boundary
            post_action_refusal = (
                "program frame validation failed closed after delivery "
                f"({type(exc).__name__})"
            )
        if post_action_refusal is None:
            post_action_refusal = self._workflow_snapshot_refusal(workflow)
        if post_action_refusal is None:
            post_action_refusal = self._governed_authorization_snapshot_refusal()
        if post_action_refusal is not None:
            prior_error = f"; earlier result: {result.error}" if result.error else ""
            result.ok = False
            result.safety_halt = True
            result.failure_category = (
                "governed_refusal"
                if self.governed_authorization is not None
                else "safety_halt"
            )
            result.error = (
                f"{post_action_refusal}; the action result was checked, "
                "but the program changed during delivery; no later state "
                f"was executed{prior_error}"
            )
        report.results.append(result)
        self._account_result(report, result)
        if self._program_durable is not None:
            self._sync_durable_audit(self._program_durable, report)
        # Track the previously EXECUTED action for the next TYPE step's
        # click-to-focus heuristic. A SKIPPED step (guard on_unmet="skip") did
        # not act, so it leaves the previous action / click point untouched.
        if not result.skipped:
            self._prev_action = action_step.action

        if not result.ok:
            # A skipped guard is ok=True; only a genuine failure lands here.
            if state.on_exception is not None and not result.safety_halt:
                result.exception_handled = True
                if (
                    result.failure_category != "runtime_failure"
                    or result.error is None
                    or not result.program_scope
                ):
                    raise _ProgramHalt(
                        "halt",
                        "handled action failure lacks an exact typed cause",
                        safety=True,
                    )
                report.program_exception_evidence.append(
                    ProgramExceptionEvidence(
                        decision_index=self._next_program_decision_index(report),
                        graph_id=result.program_scope[-1].graph_id,
                        state_id=state.id,
                        program_scope=list(result.program_scope),
                        target_state_id=state.on_exception,
                        failure_kind="action_failure",
                        error_sha256=hashlib.sha256(
                            result.error.encode("utf-8")
                        ).hexdigest(),
                        action_failure_category="runtime_failure",
                        governed_runtime_inputs_digest=(
                            report.governed_runtime_inputs_digest
                        ),
                    )
                )
                return state.on_exception  # graph try/except: route + continue
            halt = _ProgramHalt(
                "halt",
                result.error or f"action state '{state.id}' failed — run aborted",
                safety=result.safety_halt,
            )
            halt.program_frames = pre_action_frames
            halt.program_params = dict(params)
            halt.program_history_hash = _history_hash(report.visited_states)
            raise halt
        # Tier-3 durable checkpoint: this action state VERIFIED (identity +
        # effects + postconditions); capture the whole interpreter state so an
        # approved resume can RESTORE it from exactly here. Persist BEFORE
        # selecting an outgoing guarded edge: predicate evaluation can halt,
        # but the already-performed write must still enter the effect ledger.
        self._record_program_checkpoint(state, result, params, report)
        nxt = self._select_transition(
            state,
            workflow=workflow,
            params=params,
            bundle_dir=bundle_dir,
            report=report,
            run_dir=run_dir,
        )
        return nxt

    def _exec_loop_state(
        self,
        state: State,
        *,
        workflow: Workflow,
        params: dict[str, RuntimeParamScalar],
        worklists: dict[str, list[dict[str, str]]],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
        depth: int,
    ) -> Optional[str]:
        """Iterate a worklist, running the body subflow once per row (RFC §2.3).

        Zero rows => the body runs zero times. The row's fields merge into the
        params in scope for that iteration (an ``entity_ref`` param then
        re-resolves by the identity ladder each pass). Iteration is BOUNDED:
        more rows than ``max_iterations`` HALTs (fail-safe)."""
        loop = state.loop
        if loop is None:
            raise _ProgramHalt("halt", f"loop state '{state.id}' carries no LoopSpec")
        body = workflow.subflows.get(loop.body)
        if body is None:
            return self._on_state_failure(
                state,
                f"loop state '{state.id}' body subflow '{loop.body}' is not "
                "defined — run aborted",
                failure_kind="missing_loop_body",
                report=report,
                workflow=workflow,
            )
        rows = self._resolve_worklist(state, loop, workflow, worklists)
        if len(rows) > loop.max_iterations:
            return self._on_state_failure(
                state,
                f"loop state '{state.id}' worklist has {len(rows)} rows, "
                f"exceeding max_iterations={loop.max_iterations} — run aborted",
                failure_kind="loop_bound_exceeded",
                report=report,
                workflow=workflow,
            )
        for i, row in enumerate(rows):
            iter_params = {**params, **row}
            self._walk_graph(
                body,
                graph_id=loop.body,
                workflow=workflow,
                params=iter_params,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
                depth=depth + 1,
                loop_cursor=LoopCursor(
                    loop_state_id=state.id,
                    relation=loop.relation,
                    row_index=i,
                    rows=rows,
                ),
            )
        return self._select_transition(
            state,
            workflow=workflow,
            params=params,
            bundle_dir=bundle_dir,
            report=report,
            run_dir=run_dir,
        )

    def _on_state_failure(
        self,
        state: State,
        reason: str,
        *,
        failure_kind: Literal[
            "branch_without_transition",
            "missing_subflow",
            "missing_loop_body",
            "loop_bound_exceeded",
        ],
        report: RunReport,
        workflow: Workflow,
    ) -> str:
        """A non-action state hit an unrecoverable condition: route to its
        ``on_exception`` handler if it has one, else HALT the run."""
        self._raise_on_workflow_snapshot_mutation(workflow)
        if state.on_exception is not None:
            scope = self._program_execution_scope()
            if not scope:
                raise _ProgramHalt(
                    "halt",
                    "program exception evidence has no execution scope — run aborted",
                    safety=True,
                )
            report.program_exception_evidence.append(
                ProgramExceptionEvidence(
                    decision_index=self._next_program_decision_index(report),
                    graph_id=scope[-1].graph_id,
                    state_id=state.id,
                    program_scope=scope,
                    target_state_id=state.on_exception,
                    failure_kind=failure_kind,
                    governed_runtime_inputs_digest=(
                        report.governed_runtime_inputs_digest
                    ),
                )
            )
            return state.on_exception
        raise _ProgramHalt("halt", reason)

    @staticmethod
    def _next_program_decision_index(report: RunReport) -> int:
        """Return the next index shared by all typed program decisions."""

        decision_indexes = [
            evidence.decision_index for evidence in report.program_transition_evidence
        ]
        decision_indexes.extend(
            evidence.decision_index for evidence in report.program_exception_evidence
        )
        decision_indexes.extend(
            evidence.decision_index
            for evidence in report.attended_program_transition_evidence
        )
        decision_indexes.extend(
            evidence.decision_index for evidence in report.business_decision_evidence
        )
        return max(decision_indexes, default=-1) + 1

    def _exec_business_decision_state(
        self,
        state: State,
        *,
        workflow: Workflow,
        params: dict[str, RuntimeParamScalar],
        bundle_dir: Path,
        report: RunReport,
        run_dir: Path,
    ) -> str:
        """Consume one signed finite answer, then revalidate its live branch."""

        spec = state.decision
        durable = self._program_durable
        frames = [self._frame_to_model(frame) for frame in self._frame_stack]
        graph_id = self._current_graph_id or ""

        def required(reason: str) -> _ProgramHalt:
            halt = _ProgramHalt("halt", reason, safety=True)
            halt.program_frames = frames
            halt.program_params = dict(params)
            return halt

        if spec is None:
            raise required("business decision state has no qualified answer contract")
        if not graph_id:
            raise required("business decision lost its exact program graph")
        if durable is None:
            raise required(
                "business decision requires the durable runtime; refusing an "
                "answer that cannot be paused, attributed, and resumed"
            )
        from openadapt_flow.runtime.durable.business_decision import (
            BusinessDecisionRefused,
            BusinessDecisionStore,
        )

        pending = durable.store.read_pending()
        manifest = durable.store.read_manifest()
        if pending is None or manifest is None:
            raise required(
                "business decision required; the run paused before any "
                "workflow actuation can continue"
            )
        try:
            retained = BusinessDecisionStore(
                run_dir, checkpoint_key=self.checkpoint_key
            ).consume(
                pending=pending,
                manifest=manifest,
                workflow=workflow,
                graph_id=graph_id,
                state_id=state.id,
                frames=frames,
                params=dict(params),
                spec=spec,
                governed_runtime_inputs_digest=(report.governed_runtime_inputs_digest),
            )
        except BusinessDecisionRefused as exc:
            raise required(f"business decision receipt was refused: {exc}") from exc
        if retained is None:
            raise required("business decision required; no signed answer is available")
        request, request_sha256, receipt, receipt_sha256 = retained
        scope = self._program_execution_scope()
        if not scope:
            raise required("business decision has no exact program scope")
        decision_evidence = BusinessDecisionEvidence(
            decision_index=self._next_program_decision_index(report),
            graph_id=graph_id,
            state_id=state.id,
            program_scope=scope,
            decision_contract_sha256=spec.contract_sha256(),
            request_digest=request.digest,
            request_inventory_ref=(
                f".business_decisions/requests/{request_sha256}.json"
            ),
            request_sha256=request_sha256,
            receipt_digest=receipt.digest,
            receipt_inventory_ref=(
                f".business_decisions/receipts/{receipt_sha256}.json"
            ),
            receipt_sha256=receipt_sha256,
            option_id=receipt.option_id,
            output_param=receipt.output_param,
            output_value=receipt.output_value,
            target_state_id=receipt.target_state_id,
            operator_ref=receipt.operator_ref,
            authorized_role=receipt.authorized_role,
            authentication_context_sha256=receipt.authentication_context_sha256,
            evidence_artifact_sha256s=receipt.evidence_artifact_sha256s,
            idempotency_key_sha256=receipt.idempotency_key_sha256,
            decided_at=receipt.decided_at,
            governed_runtime_inputs_digest=report.governed_runtime_inputs_digest,
        )
        # Reserve the shared program-decision index before guard evaluation.
        # If the fresh frame rejects the branch, remove both this control item
        # and its transition observations so no unapplied answer enters the
        # durable program trace.
        transition_evidence_start = len(report.program_transition_evidence)
        report.business_decision_evidence.append(decision_evidence)
        prior_output = params.get(spec.output_param)
        had_prior_output = spec.output_param in params
        params[spec.output_param] = receipt.output_value
        target = self._select_transition(
            state,
            workflow=workflow,
            params=params,
            bundle_dir=bundle_dir,
            report=report,
            run_dir=run_dir,
            allow_unmatched=True,
        )
        if target != receipt.target_state_id:
            report.business_decision_evidence.pop()
            del report.program_transition_evidence[transition_evidence_start:]
            if had_prior_output:
                assert prior_output is not None
                params[spec.output_param] = prior_output
            else:
                params.pop(spec.output_param, None)
            raise required(
                "the live application no longer satisfies the selected business "
                "decision branch; refusing resumed actuation"
            )
        # The provisional item now represents applied branch control. A refused
        # answer stays only in the local signed request/receipt audit and never
        # changes restored parameters or the applied program-control trace.
        if len(scope) == 1:
            report.params[spec.output_param] = receipt.output_value
        return target

    def _resolve_worklist(
        self,
        state: State,
        loop: LoopSpec,
        workflow: Workflow,
        worklists: dict[str, list[dict[str, str]]],
    ) -> list[dict[str, str]]:
        """The rows a loop iterates: a run-time ``worklists`` entry (variable
        length, highest priority) else the inline ``data_sources`` relation. An
        undefined relation is a config error (HALT) -- distinct from a defined
        but EMPTY relation, which legitimately runs the body zero times."""
        if loop.relation in worklists:
            return list(worklists[loop.relation])
        relation = workflow.data_sources.get(loop.relation)
        if relation is not None:
            return list(relation.rows)
        raise _ProgramHalt(
            "halt",
            f"loop state '{state.id}' relation '{loop.relation}' is not "
            "defined in data_sources and no run-time worklist was supplied — "
            "run aborted",
        )

    def _select_transition(
        self,
        state: State,
        *,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        report: Optional[RunReport] = None,
        run_dir: Optional[Path] = None,
        allow_unmatched: bool = False,
    ) -> Optional[str]:
        """Pick this state's next state id (RFC §2.2): evaluate ``transitions``
        IN ORDER, first whose guard holds wins; ``None`` guard is unconditional.

        Returns the target id, or None when the state has NO transitions (fall
        off / subflow return). When transitions exist but NONE match on the
        current screen it is a dead end -- a fail-safe HALT (never guess an
        edge). Screenshots only when a guard actually needs a frame, so a
        degenerate all-unconditional chain replays with no extra settles (the
        byte-identical linear lift)."""
        self._raise_on_workflow_snapshot_mutation(workflow)
        transitions = state.transitions
        if not transitions:
            return None
        if all(t.guard is None for t in transitions):
            target = transitions[0].target
            self._retain_program_transition_evidence(
                state=state,
                evaluations=[(0, True)],
                selected_target=target,
                frame=None,
                bundle_dir=bundle_dir,
                report=report,
                run_dir=run_dir,
            )
            return target
        evaluator_contract_sha256: Optional[str] = None
        if any(self._program_guard_uses_frame(t.guard) for t in transitions):
            try:
                evaluator_contract_sha256 = program_predicate_evaluator_contract_sha256(
                    self.vision
                )
            except ValueError as exc:
                raise _ProgramHalt(
                    "halt",
                    "visual program predicate evaluator has no exact semantic "
                    f"contract ({exc}) — run aborted",
                    safety=True,
                ) from exc
        frame = self.vision.wait_settled(self.backend)
        self._raise_on_workflow_snapshot_mutation(workflow)
        evaluations: list[tuple[int, bool]] = []
        for transition_index, t in enumerate(transitions):
            matched = t.guard is None or self._predicate_holds(
                t.guard, frame, bundle_dir, params
            )
            evaluations.append((transition_index, matched))
            self._raise_on_governed_asset_mutation()
            self._raise_on_workflow_snapshot_mutation(workflow)
            if matched:
                self._retain_program_transition_evidence(
                    state=state,
                    evaluations=evaluations,
                    selected_target=t.target,
                    frame=frame,
                    bundle_dir=bundle_dir,
                    report=report,
                    run_dir=run_dir,
                    evaluator_contract_sha256=evaluator_contract_sha256,
                )
                return t.target
        self._retain_program_transition_evidence(
            state=state,
            evaluations=evaluations,
            selected_target=None,
            frame=frame,
            bundle_dir=bundle_dir,
            report=report,
            run_dir=run_dir,
            evaluator_contract_sha256=evaluator_contract_sha256,
        )
        if allow_unmatched:
            return None
        raise _ProgramHalt(
            "halt",
            f"no outgoing transition matched at state '{state.id}' on the "
            "current screen (guards: "
            + ", ".join(
                self._describe_predicate(t.guard)
                for t in transitions
                if t.guard is not None
            )
            + ") — run aborted",
        )

    @staticmethod
    def _program_guard_uses_frame(predicate: Optional[Predicate]) -> bool:
        """Return whether a guard contract reads the observed application frame."""

        return predicate_uses_frame(predicate)

    def _retain_program_transition_evidence(
        self,
        *,
        state: State,
        evaluations: list[tuple[int, bool]],
        selected_target: Optional[str],
        frame: Optional[bytes],
        bundle_dir: Path,
        report: Optional[RunReport],
        run_dir: Optional[Path],
        evaluator_contract_sha256: Optional[str] = None,
    ) -> None:
        """Retain the exact ordered evidence used to select one transition."""

        if report is None or run_dir is None:
            return
        scope = self._program_execution_scope()
        if not scope:
            raise _ProgramHalt(
                "halt",
                "program transition evidence has no execution scope — run aborted",
                safety=True,
            )
        decision_index = self._next_program_decision_index(report)
        frame_sha256: Optional[str] = None
        frame_ref: Optional[str] = None
        viewport: Optional[tuple[int, int]] = None
        context_sha256: Optional[str] = None
        context_ref: Optional[str] = None
        if any(
            self._program_guard_uses_frame(state.transitions[index].guard)
            for index, _verdict in evaluations
        ):
            if frame is None:
                raise _ProgramHalt(
                    "halt",
                    "visual program transition has no retained frame — run aborted",
                    safety=True,
                )
            frame_sha256 = hashlib.sha256(frame).hexdigest()
            frame_ref = f"private/program-transitions/{frame_sha256}.png"
            viewport = (
                int(self.backend.viewport[0]),
                int(self.backend.viewport[1]),
            )
            try:
                retained_frame_size = exact_png_size(frame)
            except Exception as exc:
                raise _ProgramHalt(
                    "halt",
                    "visual program transition frame is not a valid PNG "
                    f"({type(exc).__name__}) — run aborted",
                    safety=True,
                ) from exc
            if evaluator_contract_sha256 is None:
                raise _ProgramHalt(
                    "halt",
                    "visual program transition has no exact evaluator contract — "
                    "run aborted",
                    safety=True,
                )
            context_payload = json.dumps(
                {
                    "schema_version": 1,
                    "frame_sha256": frame_sha256,
                    "frame_size": list(retained_frame_size),
                    "viewport": list(viewport),
                    "evaluator_contract_sha256": evaluator_contract_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            context_sha256 = hashlib.sha256(context_payload).hexdigest()
            context_ref = (
                f"private/program-transition-observations/{context_sha256}.json"
            )
            root = Path(run_dir).resolve()
            private_dir = root / "private"
            inventory_dir = private_dir / "program-transitions"
            context_dir = private_dir / "program-transition-observations"
            try:
                if (
                    private_dir.is_symlink()
                    or inventory_dir.is_symlink()
                    or context_dir.is_symlink()
                ):
                    raise OSError("transition inventory path is a symlink")
                inventory_dir.mkdir(parents=True, exist_ok=True)
                context_dir.mkdir(parents=True, exist_ok=True)
                if (
                    private_dir.is_symlink()
                    or inventory_dir.is_symlink()
                    or context_dir.is_symlink()
                    or not inventory_dir.resolve().is_relative_to(root)
                    or not context_dir.resolve().is_relative_to(root)
                ):
                    raise OSError("transition inventory leaves the run root")
                frame_path = root / frame_ref
                if frame_path.is_symlink():
                    raise OSError("transition frame path is a symlink")
                existing = frame_path.read_bytes() if frame_path.exists() else None
                if existing is not None and existing != frame:
                    raise OSError("transition frame digest path has other bytes")
                frame_path.write_bytes(frame)
                context_path = root / context_ref
                if context_path.is_symlink():
                    raise OSError("transition context path is a symlink")
                existing_context = (
                    context_path.read_bytes() if context_path.exists() else None
                )
                if existing_context is not None and existing_context != context_payload:
                    raise OSError("transition context digest path has other bytes")
                context_path.write_bytes(context_payload)
            except OSError as exc:
                raise _ProgramHalt(
                    "halt",
                    "program transition frame could not be retained exactly "
                    f"({type(exc).__name__}) — run aborted",
                    safety=True,
                ) from exc

        selected_index = evaluations[-1][0] if selected_target is not None else None
        for transition_index, verdict in evaluations:
            transition = state.transitions[transition_index]
            uses_frame = self._program_guard_uses_frame(transition.guard)
            evidence_kind: Literal["unconditional", "parameters", "frame"]
            if transition.guard is None:
                evidence_kind = "unconditional"
            elif uses_frame:
                evidence_kind = "frame"
            else:
                evidence_kind = "parameters"
            assets: list[ProgramGuardAssetEvidence] = []
            if uses_frame and transition.guard is not None:
                root = Path(run_dir).resolve()
                private_dir = root / "private"
                asset_dir = private_dir / "program-transition-assets"
                for source_ref in predicate_template_refs(transition.guard):
                    payload = self._asset_bytes(bundle_dir, source_ref)
                    if payload is None:
                        raise _ProgramHalt(
                            "halt",
                            "program transition guard asset is unavailable — "
                            "run aborted",
                            safety=True,
                        )
                    digest = hashlib.sha256(payload).hexdigest()
                    inventory_ref = f"private/program-transition-assets/{digest}.bin"
                    try:
                        if private_dir.is_symlink() or asset_dir.is_symlink():
                            raise OSError(
                                "transition asset inventory path is a symlink"
                            )
                        asset_dir.mkdir(parents=True, exist_ok=True)
                        if (
                            private_dir.is_symlink()
                            or asset_dir.is_symlink()
                            or not asset_dir.resolve().is_relative_to(root)
                        ):
                            raise OSError("transition asset inventory leaves run root")
                        path = root / inventory_ref
                        if path.is_symlink():
                            raise OSError("transition asset path is a symlink")
                        existing = path.read_bytes() if path.exists() else None
                        if existing is not None and existing != payload:
                            raise OSError(
                                "transition asset digest path has other bytes"
                            )
                        path.write_bytes(payload)
                    except OSError as exc:
                        raise _ProgramHalt(
                            "halt",
                            "program transition guard asset could not be retained "
                            f"exactly ({type(exc).__name__}) — run aborted",
                            safety=True,
                        ) from exc
                    assets.append(
                        ProgramGuardAssetEvidence(
                            source_ref=source_ref,
                            sha256=digest,
                            inventory_ref=inventory_ref,
                        )
                    )
            report.program_transition_evidence.append(
                ProgramTransitionEvidence(
                    decision_index=decision_index,
                    graph_id=scope[-1].graph_id,
                    state_id=state.id,
                    program_scope=scope,
                    transition_index=transition_index,
                    guard_contract_sha256=predicate_contract_sha256(transition.guard),
                    guard_verdict=verdict,
                    selected=(
                        selected_index is not None
                        and transition_index == selected_index
                    ),
                    selected_target=selected_target,
                    guard_evidence_kind=evidence_kind,
                    observed_frame_sha256=frame_sha256 if uses_frame else None,
                    observed_frame_inventory_ref=frame_ref if uses_frame else None,
                    observed_viewport=viewport if uses_frame else None,
                    observed_context_sha256=(context_sha256 if uses_frame else None),
                    observed_context_inventory_ref=(
                        context_ref if uses_frame else None
                    ),
                    guard_evaluator_contract_sha256=(
                        evaluator_contract_sha256 if uses_frame else None
                    ),
                    guard_assets=assets,
                    governed_runtime_inputs_digest=(
                        report.governed_runtime_inputs_digest
                    ),
                )
            )

    # -- Tier-3 durable checkpoint / pause / resume (program mode, RFC §5) ----

    def _frame_to_model(self, frame: dict) -> GraphFrame:
        """Snapshot one live interpreter frame as a durable :class:`GraphFrame`."""
        loop = frame["loop"]
        if loop is not None:
            loop = LoopCursor.model_validate(loop).model_copy(deep=True)
        return GraphFrame(
            graph_id=frame["graph_id"],
            state_id=frame["state_id"],
            params=dict(frame["params"]),
            loop=loop,
        )

    def _program_execution_scope(self) -> list[ProgramExecutionScopeFrame]:
        """Return graph and loop cursors without retaining worklist values."""

        frames = [
            self._frame_to_model(frame) for frame in getattr(self, "_frame_stack", [])
        ]
        return self._program_scope_from_frames(frames)

    @staticmethod
    def _program_scope_from_frames(
        frames: list[GraphFrame],
    ) -> list[ProgramExecutionScopeFrame]:
        """Project a previously validated cursor into privacy-safe evidence."""

        return [
            ProgramExecutionScopeFrame(
                graph_id=frame.graph_id,
                loop_state_id=(
                    frame.loop.loop_state_id if frame.loop is not None else None
                ),
                relation=(frame.loop.relation if frame.loop is not None else None),
                row_index=(frame.loop.row_index if frame.loop is not None else None),
            )
            for frame in frames
        ]

    def _params_with_business_decisions(
        self,
        params: Mapping[str, RuntimeParamScalar],
        scope: list[ProgramExecutionScopeFrame],
        evidence: list[BusinessDecisionEvidence],
        *,
        run_dir: Path,
        workflow: Workflow,
        governed_runtime_inputs_digest: str | None,
    ) -> dict[str, RuntimeParamScalar]:
        """Reapply authenticated decisions made in this exact frame scope."""

        if not evidence:
            return dict(params)

        from openadapt_flow.runtime.durable.business_decision import (
            BusinessDecisionStore,
        )

        resolved = dict(params)
        store = BusinessDecisionStore(run_dir, checkpoint_key=self.checkpoint_key)
        for item in sorted(evidence, key=lambda retained: retained.decision_index):
            item_scope = list(item.program_scope)
            if item_scope != scope:
                continue
            store.authenticate_evidence(
                item,
                workflow=workflow,
                run_id=self._run_id,
                expected_bundle_version=self._bundle_version,
                governed_runtime_inputs_digest=governed_runtime_inputs_digest,
            )
            resolved[item.output_param] = item.output_value
        return resolved

    def _skip_completed_effect_state(
        self,
        state: State,
        params: Mapping[str, RuntimeParamScalar],
        report: RunReport,
    ) -> bool:
        """Idempotency guard: skip an action state whose declared effects were
        ALL already CONFIRMED (in the completed-effect ledger).

        Belt-and-suspenders for a resume that re-reaches an already-verified
        consequential write: the write is NOT re-performed (no backend action, no
        effect re-verification). A verified result is synthesized so the report
        still accounts for the state. Returns True when it skipped."""
        if (
            not self._completed_effect_keys
            and not self._completed_unverified_effect_keys
        ):
            return False
        step = state.step
        if step is None:
            return False
        from openadapt_flow.policy import iter_effect_paths

        path_keys: list[list[str]] = []
        for _path, effects in iter_effect_paths(step):
            if not effects:
                continue
            try:
                resolved = self._resolve_effects(effects, params)
            except Exception:
                return False
            path_keys.append([effect.contract_hash() for effect in resolved])
        if not path_keys:
            return False
        confirmed_keys = next(
            (
                keys
                for keys in path_keys
                if all(key in self._completed_effect_keys for key in keys)
            ),
            None,
        )
        performed_keys = next(
            (
                keys
                for keys in path_keys
                if all(
                    key in self._completed_effect_keys
                    or key in self._completed_unverified_effect_keys
                    for key in keys
                )
            ),
            None,
        )
        if confirmed_keys is None and performed_keys is None:
            return False
        confirmed = confirmed_keys is not None
        keys = confirmed_keys or performed_keys or []
        result = StepResult(
            step_id=step.id,
            intent=step.intent,
            ok=True,
            risk=step.risk,
            risk_explanation=step.risk_explanation,
            risk_review_required=step.risk_review_required,
            effect_verified=True if confirmed else None,
            effect_approved_unverified=not confirmed,
            effect_contract_hashes=keys,
            effect_evidence=(
                self._evidence_for_completed_effects(keys) if confirmed else []
            ),
            effect_results=(
                ["[resume] previously CONFIRMED effect was not re-executed"]
                if confirmed
                else [
                    "[resume] previously approved-unverified effect was not "
                    "re-executed; it remains unverified"
                ]
            ),
            program_scope=self._program_execution_scope(),
        )
        report.results.append(result)
        self._account_result(report, result)
        return True

    @staticmethod
    def _program_checkpoint_history(
        checkpoints: list[ProgramCheckpoint],
    ) -> Optional[list[str]]:
        """Reconstruct one contiguous, hash-bound pre-resume state trace."""

        history: list[str] = []
        for expected_seq, checkpoint in enumerate(checkpoints, start=1):
            delta = checkpoint.visited_states_delta
            attended = checkpoint.attended_transition is not None
            if (
                checkpoint.seq != expected_seq
                or (not attended and delta != checkpoint.transition_delta)
                or (attended and checkpoint.transition_delta)
                or not delta
                or (delta and delta[-1] != checkpoint.verified_state_id)
            ):
                return None
            history.extend(delta)
        return history

    @staticmethod
    def _resumed_program_results(
        checkpoints: list[ProgramCheckpoint],
        workflow: Workflow,
    ) -> list[StepResult]:
        """Reconstruct the verified pre-resume leg without re-actuating it."""

        steps = {
            step.id: step
            for graph in [
                workflow.program,
                *workflow.subflows.values(),
            ]
            if graph is not None
            for state in graph.states.values()
            if state.step is not None
            for step in [state.step]
        }
        results: list[StepResult] = []
        for checkpoint in checkpoints:
            step_id = checkpoint.step_id or checkpoint.verified_state_id
            step = steps.get(step_id)
            verified_keys = list(checkpoint.new_effect_keys)
            unverified_keys = list(checkpoint.new_unverified_effect_keys)
            results.append(
                StepResult(
                    step_id=step_id,
                    intent=checkpoint.intent
                    or (step.intent if step is not None else ""),
                    ok=True,
                    skipped=checkpoint.skipped,
                    risk=step.risk if step is not None else "reversible",
                    risk_explanation=(
                        step.risk_explanation if step is not None else None
                    ),
                    risk_review_required=(
                        step.risk_review_required if step is not None else False
                    ),
                    effect_verified=True if verified_keys else None,
                    effect_approved_unverified=bool(unverified_keys),
                    effect_contract_hashes=verified_keys or unverified_keys,
                    effect_evidence=list(checkpoint.new_effect_evidence),
                    identity=checkpoint.identity,
                    input_verified=checkpoint.input_verified,
                    starting_state_settled=checkpoint.starting_state_settled,
                    delivery_attempted=checkpoint.delivery_attempted,
                    delivery_receipt=checkpoint.delivery_receipt,
                    drag_end_resolution=checkpoint.drag_end_resolution,
                    fresh_actuation_events=list(checkpoint.fresh_actuation_events),
                    before_png=checkpoint.before_png,
                    postconditions_ok=checkpoint.postconditions_ok,
                    actuation=checkpoint.actuation,
                    delivery_uncertainty=checkpoint.delivery_uncertainty,
                    resolution=checkpoint.resolution,
                    drift_oracle_calls=checkpoint.drift_oracle_calls,
                    heal=checkpoint.heal,
                    program_scope=[
                        ProgramExecutionScopeFrame(
                            graph_id=frame.graph_id,
                            loop_state_id=(
                                frame.loop.loop_state_id
                                if frame.loop is not None
                                else None
                            ),
                            relation=(
                                frame.loop.relation if frame.loop is not None else None
                            ),
                            row_index=(
                                frame.loop.row_index if frame.loop is not None else None
                            ),
                        )
                        for frame in checkpoint.frames
                    ],
                )
            )
        return results

    def _evidence_for_completed_effects(
        self, effect_keys: list[str]
    ) -> list[EffectVerificationEvidence]:
        """Return one retained proof per idempotently skipped effect."""

        remaining = list(self._completed_effect_evidence)
        selected: list[EffectVerificationEvidence] = []
        for effect_key in effect_keys:
            for index, evidence in enumerate(remaining):
                if evidence.effect_contract_hash == effect_key:
                    selected.append(remaining.pop(index))
                    break
        return selected

    def _record_program_checkpoint(
        self,
        state: State,
        result: StepResult,
        params: Mapping[str, RuntimeParamScalar],
        report: RunReport,
    ) -> None:
        """Persist a verified-state interpreter checkpoint (Tier-3, program mode).

        Captures the whole interpreter state -- the frame stack (subflow / loop
        nesting), each loop's cursor, the bound params, the effects CONFIRMED at
        this state (both their hashes, for the idempotency ledger, and their
        resolved contracts, for the resume-time re-verification), the expected
        on-screen text (for the resume-time state revalidation), and the bundle
        version -- so an approved resume RESTORES the interpreter from here."""
        durable = self._program_durable
        if durable is None:
            return
        step = state.step
        if step is None:
            raise _ProgramHalt(
                "halt",
                "verified action checkpoint has no compiled step",
                safety=True,
            )
        from openadapt_flow.action_evidence import action_evidence_error

        profile = (
            self.governed_authorization.execution_profile
            if self.governed_authorization is not None
            else None
        )
        evidence_error = action_evidence_error(
            step,
            result,
            params=params,
            identity_required=(
                step.identity_armed
                or (
                    self.governed_authorization is not None
                    and self.governed_authorization.requires_verified_identity(step.id)
                )
            ),
            strict_production=(
                profile in {"standard", "regulated"}
                and (
                    step.identity_armed
                    or (
                        self.governed_authorization is not None
                        and self.governed_authorization.requires_verified_identity(
                            step.id
                        )
                    )
                )
            ),
        )
        if evidence_error is not None:
            raise _ProgramHalt(
                "halt",
                "refusing a durable checkpoint with invalid action evidence: "
                f"{evidence_error}",
                safety=True,
            )
        new_keys = (
            list(result.effect_contract_hashes)
            if result.effect_verified is True
            else []
        )
        new_unverified_keys = (
            list(result.effect_contract_hashes)
            if result.effect_approved_unverified
            else []
        )
        from openadapt_flow.policy import effects_for_actuation

        declared_effects = effects_for_actuation(step, result.actuation)
        resolved_effects = (
            [
                e.model_dump(mode="json")
                for e in self._resolve_effects(declared_effects, params)
            ]
            if declared_effects
            else []
        )
        # Extend the live ledger so a later state in the SAME leg (and the next
        # checkpoint's union) sees these as already-confirmed.
        self._completed_effect_keys.extend(new_keys)
        if new_keys:
            self._completed_effect_evidence.extend(result.effect_evidence)
        self._completed_unverified_effect_keys.extend(new_unverified_keys)
        expected = [
            pc.text
            for pc in step.expect
            if pc.kind is PostconditionKind.TEXT_PRESENT and pc.text
        ]
        self._program_seq += 1
        report_history = list(report.visited_states)
        transition_delta = report_history[self._program_history_boundary_index :]
        transition_history = [*self._program_durable_history, *transition_delta]
        checkpoint = ProgramCheckpoint(
            run_id=self._run_id,
            workflow_name=durable.workflow_name,
            seq=self._program_seq,
            verified_state_id=state.id,
            intent=step.intent if step is not None else "",
            frames=[self._frame_to_model(f) for f in self._frame_stack],
            bound_params=dict(params),
            new_effect_keys=new_keys,
            new_effects=resolved_effects if new_keys else [],
            new_effect_evidence=(list(result.effect_evidence) if new_keys else []),
            new_unverified_effect_keys=new_unverified_keys,
            new_unverified_effects=(resolved_effects if new_unverified_keys else []),
            step_id=step.id,
            identity=result.identity,
            input_verified=result.input_verified,
            starting_state_settled=result.starting_state_settled,
            delivery_attempted=result.delivery_attempted,
            delivery_receipt=result.delivery_receipt,
            drag_end_resolution=result.drag_end_resolution,
            fresh_actuation_events=list(result.fresh_actuation_events),
            before_png=result.before_png,
            postconditions_ok=result.postconditions_ok,
            skipped=result.skipped,
            actuation=result.actuation,
            delivery_uncertainty=result.delivery_uncertainty,
            resolution=result.resolution,
            drift_oracle_calls=result.drift_oracle_calls,
            heal=result.heal,
            governed_authorization_id=(
                self.governed_authorization.authorization_id
                if self.governed_authorization is not None
                else None
            ),
            governed_approval_source=(
                self.governed_authorization.approval_source
                if self.governed_authorization is not None
                else None
            ),
            expected_texts=expected,
            expected_postconditions=(
                [condition.model_copy(deep=True) for condition in step.expect]
                if not result.skipped
                else []
            ),
            transition_history_hash=_history_hash(transition_history),
            visited_states_delta=transition_delta,
            program_transition_evidence_delta=report.program_transition_evidence[
                self._program_transition_evidence_checkpoint_len :
            ],
            business_decision_evidence_delta=report.business_decision_evidence[
                self._business_decision_evidence_checkpoint_len :
            ],
            program_exception_evidence_delta=report.program_exception_evidence[
                self._program_exception_evidence_checkpoint_len :
            ],
            transition_parent_hash=self._program_history_parent_hash,
            transition_delta=transition_delta,
            transition_history=transition_history,
            bundle_version=self._bundle_version,
        )
        durable.record_program_checkpoint(checkpoint)
        self._program_checkpoint_history_len = len(report_history)
        self._program_transition_evidence_checkpoint_len = len(
            report.program_transition_evidence
        )
        self._business_decision_evidence_checkpoint_len = len(
            report.business_decision_evidence
        )
        self._program_exception_evidence_checkpoint_len = len(
            report.program_exception_evidence
        )
        self._program_history_boundary_index = len(report_history)
        self._program_durable_history = transition_history
        self._program_history_parent_hash = checkpoint.transition_history_hash
        if self._durable_continuation_guard is not None:
            self._durable_continuation_guard.acknowledge_progress()

    def _record_program_pause(
        self, halt: "_ProgramHalt", report: RunReport, *, workflow: Workflow
    ) -> None:
        """Persist a durable PROGRAM pause (the interpreter HALTED for a human).

        Uses the last executed state's failing result (an action failure) or, for
        a non-action halt (dead-end branch, unmet guard, terminal), a synthesized
        result carrying the halt reason -- so :func:`classify_halt` can categorize
        the pause and propose operator options."""
        durable = self._program_durable
        if durable is None:
            return
        failing = next((r for r in reversed(report.results) if not r.ok), None)
        if failing is None:
            failing = StepResult(
                step_id=self._current_state_id or "<program>",
                intent=self._current_intent,
                ok=False,
                error=halt.reason,
            )
        elif failing.error is None:
            failing = failing.model_copy(update={"error": halt.reason})
        program_history_delta = list(
            report.visited_states[self._program_history_boundary_index :]
        )
        durable_program_history = [
            *self._program_durable_history,
            *program_history_delta,
        ]
        durable.record_program_halt(
            state_id=self._current_state_id or failing.step_id,
            intent=self._current_intent or failing.intent,
            result=failing,
            params=halt.program_params or self._current_params,
            workflow=workflow,
            transition_observation=self._attended_transition_observation(),
            program_frames=halt.program_frames,
            program_checkpoint_seq=self._program_seq,
            program_history_hash=_history_hash(durable_program_history),
            program_parent_history_hash=self._program_history_parent_hash,
            program_history_delta=program_history_delta,
            program_history=durable_program_history,
            program_transition_evidence_delta=report.program_transition_evidence[
                self._program_transition_evidence_checkpoint_len :
            ],
            business_decision_evidence_delta=report.business_decision_evidence[
                self._business_decision_evidence_checkpoint_len :
            ],
            program_exception_evidence_delta=report.program_exception_evidence[
                self._program_exception_evidence_checkpoint_len :
            ],
        )
        graph_id = self._current_graph_id or ""
        if not graph_id:
            return
        graph = self._resolve_graph(workflow, graph_id)
        decision_state = graph.states.get(self._current_state_id)
        if (
            decision_state is not None
            and decision_state.kind is StateKind.BUSINESS_DECISION
            and decision_state.decision is not None
        ):
            from openadapt_flow.runtime.durable.business_decision import (
                BusinessDecisionRefused,
                BusinessDecisionStore,
            )

            pending = durable.store.read_pending()
            manifest = durable.store.read_manifest()
            if pending is not None and manifest is not None:
                try:
                    BusinessDecisionStore(
                        durable.store.run_dir,
                        checkpoint_key=self.checkpoint_key,
                    ).issue(
                        pending=pending,
                        manifest=manifest,
                        workflow=workflow,
                        graph_id=graph_id,
                        state_id=decision_state.id,
                        frames=list(halt.program_frames),
                        params=dict(halt.program_params or self._current_params),
                        spec=decision_state.decision,
                        governed_runtime_inputs_digest=(
                            report.governed_runtime_inputs_digest
                        ),
                    )
                except BusinessDecisionRefused as exc:
                    halt.reason = (
                        f"{halt.reason}; the signed decision request could not "
                        f"be issued: {exc}"
                    )
        if (
            self._durable_continuation_guard is not None
            and failing.failure_category != "continuation_preempted"
        ):
            self._durable_continuation_guard.acknowledge_progress(terminal=True)

    def revalidate_program_checkpoint(
        self,
        checkpoint: ProgramCheckpoint,
        completed_effects: list[dict],
        *,
        workflow: Workflow,
        bundle_dir: Path,
    ) -> None:
        """Revalidate the live app before RESTORING a program checkpoint (RFC §5).

        Two checks, both raising :class:`StateDiverged` (refuse -- never re-drive
        from a state the checkpoint was not captured against):

        1. the live app must still satisfy every replayable retained
           postcondition for the state where the interpreter paused;
        2. every already-confirmed effect must STILL hold (a read-only re-verify
           against the system of record -- an already-landed write that has since
           been reverted / deleted means the world moved under the checkpoint).
        """
        self.revalidate_program_checkpoint_state(
            checkpoint,
            bundle_dir=bundle_dir,
        )
        self.revalidate_program_checkpoint_effects(
            checkpoint,
            completed_effects,
            workflow=workflow,
        )

    def revalidate_program_checkpoint_state(
        self,
        checkpoint: ProgramCheckpoint,
        *,
        bundle_dir: Path,
    ) -> None:
        """Prove that the live application still matches the resume cursor."""

        conditions = checkpoint.expected_postconditions
        if not conditions and not checkpoint.expected_texts:
            return
        if not conditions:
            raise StateDiverged(
                "the durable checkpoint predates exact postcondition retention; "
                "refusing to resume"
            )
        self._revalidate_retained_postconditions(
            conditions,
            bundle_dir=bundle_dir,
        )

    def revalidate_linear_checkpoint_state(
        self,
        checkpoint: Any,
        *,
        bundle_dir: Path,
    ) -> None:
        """Prove that the live app still satisfies a linear resume cursor."""

        self._revalidate_retained_postconditions(
            checkpoint.expected_postconditions,
            bundle_dir=bundle_dir,
        )

    def _revalidate_retained_postconditions(
        self,
        conditions: list[Postcondition],
        *,
        bundle_dir: Path,
    ) -> None:
        """Re-evaluate an exact retained postcondition contract or refuse."""

        if not conditions:
            return
        non_replayable = [
            condition
            for condition in conditions
            if condition.kind
            in {
                PostconditionKind.URL_CHANGED,
                PostconditionKind.TITLE_CHANGED,
                PostconditionKind.NEW_TAB_OPENED,
            }
        ]
        if non_replayable:
            raise StateDiverged(
                "the durable checkpoint contains a transition-relative "
                "postcondition that cannot be revalidated after restart"
            )
        frame = self.vision.wait_settled(self.backend)
        failed = [
            condition
            for condition in conditions
            if not self._postcondition_passes(
                condition,
                frame,
                bundle_dir,
                {},
            )
        ]
        if failed:
            raise StateDiverged(
                "the live app no longer satisfies the retained postcondition "
                "contract ("
                + ", ".join(self._describe_postcondition(item) for item in failed)
                + "); refusing to resume"
            )

    def revalidate_program_checkpoint_effects(
        self,
        checkpoint: ProgramCheckpoint,
        completed_effects: list[dict],
        *,
        workflow: Workflow,
    ) -> None:
        """Re-read effects owned by one exact retained program action."""

        parsed: list[Effect] = []
        for dump in completed_effects:
            try:
                parsed.append(Effect.model_validate(dump))
            except Exception as exc:
                raise StateDiverged(
                    "a retained system-of-record effect contract is invalid"
                ) from exc
        source_step = next(
            (
                step
                for step in _all_workflow_steps(workflow)
                if step.id == checkpoint.step_id
            ),
            None,
        )
        if source_step is None and workflow.program is not None:
            source_step = next(
                (
                    state.step
                    for graph in [workflow.program, *workflow.subflows.values()]
                    for state in graph.states.values()
                    if state.id == checkpoint.verified_state_id
                    and state.step is not None
                ),
                None,
            )
        if parsed and source_step is None:
            raise StateDiverged(
                "retained program effects have no exact source action contract"
            )
        if source_step is not None:
            self.revalidate_retained_effects(
                parsed,
                workflow=workflow,
                step=source_step,
                actuation_path="api" if checkpoint.actuation == "api" else "gui",
                retained_evidence=list(checkpoint.new_effect_evidence),
            )

    def revalidate_retained_effects(
        self,
        effects: list[Effect],
        *,
        workflow: Workflow,
        step: Step,
        actuation_path: Literal["gui", "api"],
        retained_evidence: Optional[list[EffectVerificationEvidence]] = None,
    ) -> None:
        """Prove that previously confirmed effects still hold before resume."""

        if not effects:
            return
        if self.effect_verifier is None:
            raise StateDiverged(
                "resume requires the qualified effect verifier for retained "
                "confirmed writes"
            )
        refusal = self._profile_effect_tier_refusal(
            workflow,
            step,
            actuation_path,
            effects,
            self.effect_verifier,
        )
        if refusal is not None:
            raise StateDiverged(refusal)
        try:
            current = self._capture_effect_pre_state(self.effect_verifier, effects)
        except Exception as exc:
            raise StateDiverged(
                "the retained effects could not be read before resume"
            ) from exc
        binding_refusal = self._candidate_binding_refusal(current, effects)
        if binding_refusal is not None:
            raise StateDiverged(binding_refusal)
        remaining_evidence = list(retained_evidence or [])
        for effect in effects:
            if effect.needs_operator_confirmation:
                raise StateDiverged(
                    "a retained effect still requires an operator-authored binding"
                )
            retained = next(
                (
                    item
                    for item in remaining_evidence
                    if item.effect_contract_hash == effect.contract_hash()
                ),
                None,
            )
            if retained is not None:
                remaining_evidence.remove(retained)
            (
                current_verifier,
                effect_current,
                current_tier,
                current_identity,
            ) = self._effect_binding_for(self.effect_verifier, current, effect)
            selected = getattr(current, "for_effect", None)
            if callable(selected) and (
                retained is None or retained.verifier_identity is None
            ):
                raise StateDiverged(
                    "the retained candidate verifier identity is missing; refusing "
                    "to select a different verifier after restart"
                )
            if retained is not None and retained.verifier_identity is not None:
                if (
                    retained.verifier_identity != current_identity
                    or retained.verification_tier
                    != (int(current_tier) if current_tier is not None else None)
                ):
                    raise StateDiverged(
                        "the retained verifier identity or evidence tier changed "
                        "after restart; refusing to resume"
                    )

            # Historical evidence already proves the original delta and
            # collateral-loss contract. Resume now asks the exact retained
            # adapter to prove that the intended effect still persists. The
            # adapter owns its correct current-state read path, including
            # different-path on-screen re-navigation.
            persistence_effect = effect.model_copy(
                update={
                    "count_new_only": False,
                    "forbid_collateral_loss": False,
                }
            )
            try:
                verdict = self._verify_current_effect(
                    current_verifier, persistence_effect, effect_current
                )
            except Exception as exc:  # noqa: BLE001 - resume verifier boundary
                raise StateDiverged(
                    "the retained effect current-state readback failed"
                ) from exc
            if callable(selected) and (
                verifier_effect_tier(current_verifier, effect) != current_tier
                or effect_verifier_identity(current_verifier) != current_identity
            ):
                raise StateDiverged(
                    "the retained verifier identity or evidence tier changed "
                    "during current-state readback"
                )
            if not verdict.confirmed:
                raise StateDiverged(
                    "an already-confirmed effect no longer holds "
                    f"({effect.kind.value}: {verdict.verdict.value}) — "
                    "refusing to resume; the system of record diverged from "
                    "the checkpoint"
                )

    def revalidate_attended_program_completion(
        self,
        workflow: Workflow,
        *,
        graph_id: str,
        state_id: str,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        run_id: str,
        transition_baseline: Any,
        transition_digest: Callable[[str, str], str],
    ) -> tuple[StepResult, Optional[str]]:
        """Verify one human-completed ACTION state and select one exact edge.

        The source action is observed through the same postcondition/effect
        contract as a linear attended completion. Only after it verifies do we
        evaluate its guarded transitions once against the live session. The
        caller persists that selected target in a transition receipt before
        resume, so neither the source action nor its edge selection is repeated.
        """
        # This observation-only continuation runs outside ``run()``. Install
        # the same exact semantic snapshot before its first vision/effect
        # callback so a callback cannot rewrite an outgoing edge and then have
        # that edge committed into the attended transition receipt.
        self._install_execution_snapshots(workflow)
        execution_workflow = self._execution_workflow_snapshot or workflow
        snapshot_refusal = self._workflow_snapshot_refusal(execution_workflow)
        if snapshot_refusal is None:
            snapshot_refusal = self._governed_authorization_snapshot_refusal()
        if snapshot_refusal is not None:
            raise StateDiverged(snapshot_refusal)
        graph = self._resolve_graph(execution_workflow, graph_id)
        state = graph.states.get(state_id)
        if state is None or state.kind is not StateKind.ACTION or state.step is None:
            raise StateDiverged(
                "the attended program cursor does not name an action state"
            )
        verification_workflow = Workflow(
            name=execution_workflow.name,
            steps=[state.step.model_copy(deep=True)],
            params=dict(execution_workflow.params),
            param_specs=dict(execution_workflow.param_specs),
        )
        result = self.revalidate_attended_completion(
            verification_workflow,
            step_index=0,
            params=params,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            run_id=run_id,
            transition_baseline=transition_baseline,
            transition_digest=transition_digest,
            _preserve_execution_snapshot=True,
        )
        if not result.ok:
            return result, None
        snapshot_refusal = self._workflow_snapshot_refusal(execution_workflow)
        if snapshot_refusal is None:
            snapshot_refusal = self._governed_authorization_snapshot_refusal()
        if snapshot_refusal is not None:
            result.ok = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.error = snapshot_refusal
            return result, None
        target = self.select_attended_program_transition(
            execution_workflow,
            graph_id=graph_id,
            state_id=state_id,
            params=params,
            bundle_dir=bundle_dir,
        )
        self._raise_on_workflow_snapshot_mutation(execution_workflow)
        return result, target

    def select_attended_program_transition(
        self,
        workflow: Workflow,
        *,
        graph_id: str,
        state_id: str,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
    ) -> Optional[str]:
        """Select and prove one exact successor for an attended action state."""
        if not self._execution_snapshot_required:
            self._install_execution_snapshots(workflow)
        execution_workflow = self._execution_workflow_snapshot or workflow
        graph = self._resolve_graph(execution_workflow, graph_id)
        state = graph.states.get(state_id)
        if state is None or state.kind is not StateKind.ACTION or state.step is None:
            raise StateDiverged(
                "the attended program cursor does not name an action state"
            )
        try:
            target = self._select_transition(
                state,
                workflow=execution_workflow,
                params=params,
                bundle_dir=bundle_dir,
            )
        except _ProgramHalt as exc:
            raise StateDiverged(
                "no exact outgoing program transition matched after attended completion"
            ) from exc
        if target is not None and target not in graph.states:
            raise StateDiverged(
                "the selected attended program transition targets an undefined state"
            )

        # When the exact successor is another action, prove its target is
        # uniquely available (and its identity, when armed) before committing
        # the receipt. Non-action successors carry their own deterministic
        # branch/loop/subflow semantics and need no action-target proof here.
        next_state = graph.states.get(target) if target is not None else None
        if (
            next_state is not None
            and next_state.kind is StateKind.ACTION
            and next_state.step is not None
            and next_state.step.anchor is not None
        ):
            frame = self.vision.wait_settled(self.backend)
            resolution, _region, error = self._resolve_step(
                next_state.step, frame, bundle_dir, execution_workflow
            )
            if error is not None or resolution is None:
                raise StateDiverged(
                    "the exact attended program successor target did not resolve "
                    "uniquely"
                )
            if next_state.step.identity_armed:
                identity = self._verify_identity(
                    next_state.step,
                    resolution,
                    frame,
                    params,
                    execution_workflow,
                    bundle_dir,
                )
                if identity.status != "verified":
                    raise StateDiverged(
                        "the exact attended program successor identity did not verify"
                    )
        self._raise_on_workflow_snapshot_mutation(execution_workflow)
        return target

    def revalidate_attended_completion(
        self,
        workflow: Workflow,
        *,
        step_index: int,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        run_id: str,
        transition_baseline: Any,
        transition_digest: Callable[[str, str], str],
        _preserve_execution_snapshot: bool = False,
    ) -> StepResult:
        """Verify a human-completed linear step without actuating it.

        This is intentionally not ``_run_step``: the person already completed
        the challenge/task in the live application, so invoking the action
        again could duplicate a consequential write.  The method observes only:

        * the paused step's screen postconditions;
        * its independently declared effects against the *current* system of
          record, when the contract is meaningful without a pre-action delta;
        * the next step's unique target and armed identity, proving the expected
          continuation is actually available.

        Delivery receipts are never consulted. Transition-relative
        postconditions are compared against the PHI-safe keyed baseline bound
        into the signed pause capability. Delta/collateral-loss effects still
        require a protected pre-delivery record baseline and are refused when
        one is unavailable.
        """
        from openadapt_flow.policy import effects_for_actuation

        if not _preserve_execution_snapshot:
            self._install_execution_snapshots(workflow)
            workflow = self._execution_workflow_snapshot or workflow
        snapshot_refusal = self._workflow_snapshot_refusal(workflow)
        if snapshot_refusal is None:
            snapshot_refusal = self._governed_authorization_snapshot_refusal()
        if snapshot_refusal is not None:
            raise StateDiverged(snapshot_refusal)
        if not (0 <= step_index < len(workflow.steps)):
            raise StateDiverged("the attended pause references no workflow step")
        step = workflow.steps[step_index]
        # A person completes the visible GUI path, never the optional API
        # dispatch path. Keep the path-specific effect contracts distinct so
        # an API-only verifier cannot certify a human GUI write and a failing
        # API contract cannot incorrectly refute a correctly verified GUI one.
        attended_effects = effects_for_actuation(step, "gui")
        result = StepResult(
            step_id=step.id,
            intent=step.intent,
            ok=False,
            risk=step.risk,
            risk_explanation=step.risk_explanation,
            risk_review_required=step.risk_review_required,
            actuation="human_attended",
        )
        evidence_step_id = self._new_step_evidence_key(run_dir, step.id)
        self._run_id = run_id
        (Path(run_dir) / "steps").mkdir(parents=True, exist_ok=True)
        frame = self.vision.wait_settled(self.backend)
        result.before_png = self._save_step_png(
            run_dir, evidence_step_id, "attended-before", frame
        )

        relative_kinds = {"url_changed", "title_changed", "new_tab_opened"}
        relative = [
            pc
            for pc in step.expect
            if (pc.kind.value if hasattr(pc.kind, "value") else pc.kind)
            in relative_kinds
        ]
        live_structural = self._structural_state() if relative else {}
        for condition in relative:
            kind = (
                condition.kind.value
                if hasattr(condition.kind, "value")
                else str(condition.kind)
            )
            if kind == "url_changed":
                current = live_structural.get("url")
                baseline_digest = getattr(transition_baseline, "url_digest", None)
                changed = (
                    isinstance(current, str)
                    and baseline_digest is not None
                    and transition_digest("url", current) != baseline_digest
                )
            elif kind == "title_changed":
                current = live_structural.get("page_title")
                baseline_digest = getattr(transition_baseline, "title_digest", None)
                changed = (
                    isinstance(current, str)
                    and baseline_digest is not None
                    and transition_digest("page_title", current) != baseline_digest
                )
            else:
                current = live_structural.get("page_count")
                baseline_count = getattr(transition_baseline, "page_count", None)
                changed = (
                    isinstance(current, int)
                    and isinstance(baseline_count, int)
                    and current > baseline_count
                )
            if not changed:
                result.postconditions_ok = False
                result.error = (
                    "the human-completed step's signed structural transition "
                    "was unavailable or unchanged; Continue is refused"
                )
                result.after_png = result.before_png
                return result

        nonrelative = [
            pc
            for pc in step.expect
            if (pc.kind.value if hasattr(pc.kind, "value") else pc.kind)
            not in relative_kinds
        ]
        if nonrelative:
            verification_step = step.model_copy(update={"expect": nonrelative})
            post_ok, frame, failed = self._check_postconditions(
                verification_step, frame, bundle_dir, {}, result
            )
            result.postconditions_ok = post_ok
            if not post_ok:
                # The failed descriptions can contain demonstrated text (and
                # therefore PHI). Keep them out of the browser-facing decision
                # message; protected screenshots remain in the local run dir.
                result.error = (
                    "the human-completed step's live postconditions are not "
                    f"satisfied ({len(failed)} condition(s) failed)"
                )
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result

        if attended_effects:
            if self.effect_verifier is None:
                result.effect_verified = False
                result.error = (
                    "the human-completed consequential step has no independent "
                    "effect verifier; outcome is uncertain and Continue is refused"
                )
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result
            effects = self._resolve_effects(attended_effects, params)
            profile_workflow = self._execution_workflow_snapshot or workflow
            tier_refusal = self._profile_effect_tier_refusal(
                profile_workflow,
                step,
                "gui",
                effects,
                self.effect_verifier,
            )
            if tier_refusal is not None:
                result.effect_verified = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = tier_refusal
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result
            try:
                current = self._capture_effect_pre_state(self.effect_verifier, effects)
            except Exception as exc:  # noqa: BLE001 - attended verifier boundary
                result.effect_verified = False
                result.error = (
                    "the qualified current-state verifier failed its preflight or "
                    f"readback ({type(exc).__name__}); outcome is uncertain and "
                    "Continue is refused"
                )
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result
            binding_refusal = self._candidate_binding_refusal(current, effects)
            if binding_refusal is not None:
                result.effect_verified = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = binding_refusal
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result
            tier_refusal = self._profile_effect_tier_refusal(
                profile_workflow,
                step,
                "gui",
                effects,
                self.effect_verifier,
            )
            if tier_refusal is not None:
                result.effect_verified = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = tier_refusal
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result
            snapshot_refusal = self._workflow_snapshot_refusal(profile_workflow)
            if snapshot_refusal is None:
                snapshot_refusal = self._governed_authorization_snapshot_refusal()
            if snapshot_refusal is not None:
                result.effect_verified = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = snapshot_refusal
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result
            for effect in effects:
                (
                    current_verifier,
                    effect_current,
                    tier,
                    verifier_binding_identity,
                ) = self._effect_binding_for(self.effect_verifier, current, effect)
                if effect.needs_operator_confirmation:
                    result.effect_verified = False
                    result.error = (
                        "the effect contract is still a placeholder; a human "
                        "statement cannot replace independent verification"
                    )
                    break
                if effect.requires_baseline or effect.forbid_collateral_loss:
                    result.effect_verified = False
                    result.error = (
                        "the effect requires a pre-delivery delta or collateral-"
                        "loss baseline that is unavailable after human delivery; "
                        "outcome is uncertain and Continue is refused"
                    )
                    break
                try:
                    verdict = self._verify_current_effect(
                        current_verifier, effect, effect_current
                    )
                except Exception as exc:  # noqa: BLE001 - post-input verifier boundary
                    result.effect_verified = False
                    result.error = (
                        "the qualified current-state readback failed "
                        f"({type(exc).__name__}); outcome is uncertain and "
                        "Continue is refused"
                    )
                    break
                if callable(getattr(current, "for_effect", None)) and (
                    verifier_effect_tier(current_verifier, effect) != tier
                    or effect_verifier_identity(current_verifier)
                    != verifier_binding_identity
                ):
                    result.effect_verified = False
                    result.error = (
                        "the selected verifier identity or evidence tier changed "
                        "during attended current-state readback; Continue is refused"
                    )
                    break
                result.effect_contract_hashes.append(effect.contract_hash())
                result.effect_results.append(
                    f"[attended-qualified-readback] {effect.kind.value}: "
                    f"{verdict.verdict.value} — {verdict.reason}"
                )
                if not verdict.confirmed:
                    result.effect_verified = False
                    result.error = (
                        "the qualified current-state effect readback did not "
                        f"confirm the outcome ({verdict.verdict.value})"
                    )
                    break
                result.effect_evidence.append(
                    EffectVerificationEvidence(
                        effect_contract_hash=effect.contract_hash(),
                        substrate=verdict.substrate,
                        verifier_identity=verifier_binding_identity,
                        verification_tier=(int(tier) if tier is not None else None),
                        initial_verdict=verdict.verdict.value,
                        final_verdict=verdict.verdict.value,
                        observed_effect=verdict.observed_effect,
                    )
                )
            else:
                result.effect_verified = True
            if result.error is not None:
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "attended-after", frame
                )
                return result

        if not step.expect and not attended_effects:
            result.error = (
                "the human-completed step declares neither a postcondition nor "
                "an independent effect; delivery cannot be confused with outcome "
                "verification, so Continue is refused"
            )
            result.after_png = self._save_step_png(
                run_dir, evidence_step_id, "attended-after", frame
            )
            return result

        # Prove the expected continuation's target is unique and, where the
        # workflow armed identity, still identifies the intended entity.
        next_index = step_index + 1
        if next_index < len(workflow.steps):
            next_step = workflow.steps[next_index]
            if next_step.anchor is not None:
                resolution, _region, error = self._resolve_step(
                    next_step, frame, bundle_dir, workflow
                )
                if error is not None or resolution is None:
                    result.error = (
                        "the expected next transition's target did not resolve "
                        "uniquely; live state diverged and Continue is refused"
                    )
                    result.after_png = self._save_step_png(
                        run_dir, evidence_step_id, "attended-after", frame
                    )
                    return result
                if next_step.identity_armed:
                    identity = self._verify_identity(
                        next_step,
                        resolution,
                        frame,
                        params,
                        workflow,
                        bundle_dir,
                    )
                    result.identity = identity
                    if identity.status != "verified":
                        result.error = (
                            "the expected next transition's target identity "
                            f"was {identity.status}; Continue is refused"
                        )
                        result.after_png = self._save_step_png(
                            run_dir, evidence_step_id, "attended-after", frame
                        )
                        return result

        # Screenshot persistence can invoke a deployment-provided scrubber.
        # Persist before the last semantic comparison, then return VERIFIED
        # only if that callback left the admitted workflow and authorization
        # unchanged.
        result.after_png = self._save_step_png(
            run_dir, evidence_step_id, "attended-after", frame
        )
        snapshot_refusal = self._workflow_snapshot_refusal(
            self._execution_workflow_snapshot or workflow
        )
        if snapshot_refusal is None:
            snapshot_refusal = self._governed_authorization_snapshot_refusal()
        if snapshot_refusal is not None:
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.error = snapshot_refusal
            return result
        result.ok = True
        result.postconditions_ok = True if step.expect else None
        return result

    def _resolve_graph(self, workflow: Workflow, graph_id: str) -> ProgramGraph:
        """Resolve a durable ``graph_id`` back to a :class:`ProgramGraph`
        (``TOP_GRAPH_ID`` -> ``workflow.program``, else a named subflow)."""
        if graph_id == TOP_GRAPH_ID:
            assert workflow.program is not None
            return workflow.program
        sub = workflow.subflows.get(graph_id)
        if sub is None:
            raise _ProgramHalt(
                "halt",
                f"resume references undefined graph '{graph_id}' — run aborted",
            )
        return sub

    def _resume_program_state(
        self,
        checkpoint: ProgramCheckpoint,
        *,
        workflow: Workflow,
        worklists: dict[str, list[dict[str, str]]],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
    ) -> None:
        """RESTORE the interpreter from a checkpoint's frame stack and continue.

        Re-descends the recorded frames (outer -> inner), re-entering each
        subflow / loop-body graph at the state it was in, and drives the run to
        completion from the paused state -- never from the graph entry, so an
        already-confirmed consequential write is never re-performed and a
        mid-loop pause finishes the in-progress row and runs the remaining rows.
        """
        assert workflow.program is not None
        if (
            not checkpoint.frames
            or not isinstance(checkpoint.frames[-1], GraphFrame)
            or checkpoint.frames[-1].state_id != checkpoint.verified_state_id
        ):
            raise _ProgramHalt(
                "halt",
                "the durable interpreter cursor does not match its verified state",
            )
        # Validate the complete retained control path before appending or
        # changing any live frame.  Checkpoints are normally written only after
        # an ACTION state verifies.  Treating a LOOP, SUBFLOW_CALL, BRANCH, or
        # TERMINAL as the verified leaf would skip that control state's work and
        # could continue at a later input edge.
        parent_state: Optional[State] = None
        expected_params = dict(self._governed_base_params or {})
        leaf_state: Optional[State] = None
        for index, frame in enumerate(checkpoint.frames):
            graph = self._resolve_graph(workflow, frame.graph_id)
            state = graph.states.get(frame.state_id)
            if state is None or state.id != frame.state_id:
                raise _ProgramHalt(
                    "halt",
                    "the durable interpreter cursor references an undefined "
                    "program state",
                )
            if index == 0:
                if frame.graph_id != TOP_GRAPH_ID or frame.loop is not None:
                    raise _ProgramHalt(
                        "halt",
                        "the durable interpreter cursor has an invalid root frame",
                    )
            elif frame.loop is not None:
                loop = parent_state.loop if parent_state is not None else None
                cursor = frame.loop
                if (
                    parent_state is None
                    or parent_state.kind is not StateKind.LOOP
                    or loop is None
                    or parent_state.id != cursor.loop_state_id
                    or cursor.relation != loop.relation
                    or frame.graph_id != loop.body
                ):
                    raise _ProgramHalt(
                        "halt",
                        "the durable interpreter loop cursor does not match its "
                        "parent loop state",
                    )
                source_rows = (
                    worklists[loop.relation]
                    if loop.relation in worklists
                    else (
                        workflow.data_sources[loop.relation].rows
                        if loop.relation in workflow.data_sources
                        else None
                    )
                )
                if (
                    source_rows is None
                    or len(source_rows) > loop.max_iterations
                    or cursor.rows != source_rows
                    or cursor.row_index < 0
                    or cursor.row_index >= len(source_rows)
                ):
                    raise _ProgramHalt(
                        "halt",
                        "the durable interpreter loop cursor does not match its "
                        "authorized worklist",
                    )
                expected_params.update(source_rows[cursor.row_index])
            elif (
                parent_state is None
                or parent_state.kind is not StateKind.SUBFLOW_CALL
                or parent_state.subflow != frame.graph_id
            ):
                raise _ProgramHalt(
                    "halt",
                    "the durable interpreter child frame does not match its "
                    "parent subflow call",
                )
            expected_params = self._params_with_business_decisions(
                expected_params,
                self._program_scope_from_frames(checkpoint.frames[: index + 1]),
                report.business_decision_evidence,
                run_dir=run_dir,
                workflow=workflow,
                governed_runtime_inputs_digest=(report.governed_runtime_inputs_digest),
            )
            if frame.params != expected_params:
                raise _ProgramHalt(
                    "halt",
                    "the durable interpreter frame parameters do not match the "
                    "authorized control path",
                )
            parent_state = state
            leaf_state = state
        if (
            leaf_state is None
            or leaf_state.kind is not StateKind.ACTION
            or leaf_state.step is None
            or (checkpoint.step_id and checkpoint.step_id != leaf_state.step.id)
            or checkpoint.bound_params != checkpoint.frames[-1].params
        ):
            raise _ProgramHalt(
                "halt",
                "the durable interpreter verified leaf does not match an exact "
                "action state",
            )
        receipt = checkpoint.attended_transition
        if receipt is not None:
            if (
                receipt.source_checkpoint_seq != checkpoint.seq - 1
                or receipt.control_frames_hash != control_frames_hash(checkpoint.frames)
                or not checkpoint.frames
                or checkpoint.frames[-1].graph_id != receipt.source_graph_id
                or checkpoint.frames[-1].state_id != receipt.source_state_id
                or checkpoint.verified_state_id != receipt.source_state_id
            ):
                raise _ProgramHalt(
                    "halt",
                    "the attended interpreter transition receipt does not match "
                    "its checkpoint cursor",
                )
            graph = self._resolve_graph(workflow, receipt.source_graph_id)
            source = graph.states.get(receipt.source_state_id)
            if source is None or source.kind is not StateKind.ACTION:
                raise _ProgramHalt(
                    "halt",
                    "the attended interpreter transition receipt source is not "
                    "an action state",
                )
            declared_targets = {transition.target for transition in source.transitions}
            if (
                receipt.target_state_id is None
                and source.transitions
                or receipt.target_state_id is not None
                and receipt.target_state_id not in declared_targets
            ):
                raise _ProgramHalt(
                    "halt",
                    "the attended interpreter transition receipt names an "
                    "undeclared successor",
                )
        self._resume_descend(
            checkpoint.frames,
            0,
            workflow=workflow,
            worklists=worklists,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            report=report,
            new_crops=new_crops,
            attended_transition=receipt,
        )

    def _resume_descend(
        self,
        frames: list[GraphFrame],
        depth: int,
        *,
        workflow: Workflow,
        worklists: dict[str, list[dict[str, str]]],
        bundle_dir: Path,
        run_dir: Path,
        report: RunReport,
        new_crops: dict[str, bytes],
        attended_transition: Optional[ProgramTransitionReceipt] = None,
    ) -> None:
        """Restore one frame of the interpreter stack and continue it.

        The LEAF frame (``depth == len-1``) is the last verified state: continue
        from its successor transition. An ANCESTOR frame is a ``subflow_call`` /
        ``loop`` state whose body is mid-flight: finish the child (recurse), then
        -- for a loop -- run the REMAINING rows, then continue the parent after
        the call/loop state. The live ``_frame_stack`` mirrors the descent so any
        checkpoint written during the resumed leg captures the full nesting."""
        frame_model = frames[depth]
        graph = self._resolve_graph(workflow, frame_model.graph_id)
        params = dict(frame_model.params)
        is_leaf = depth == len(frames) - 1
        live = {
            "graph_id": frame_model.graph_id,
            "state_id": frame_model.state_id,
            "params": params,
            "loop": frame_model.loop,
        }
        self._frame_stack.append(live)
        try:
            state = graph.states.get(frame_model.state_id)
            if state is None:
                raise _ProgramHalt(
                    "halt",
                    f"resume references undefined state '{frame_model.state_id}' "
                    "— run aborted",
                )

            if is_leaf:
                # The verified state: re-drive from its SUCCESSOR (never re-run
                # the verified state itself).
                self._raise_on_workflow_snapshot_mutation(workflow)
                nxt = (
                    attended_transition.target_state_id
                    if attended_transition is not None
                    else self._select_transition(
                        state,
                        workflow=workflow,
                        params=params,
                        bundle_dir=bundle_dir,
                        report=report,
                        run_dir=run_dir,
                    )
                )
                if nxt is not None:
                    self._raise_on_workflow_snapshot_mutation(workflow)
                    live["state_id"] = nxt
                    self._run_states_from(
                        graph,
                        live,
                        workflow=workflow,
                        params=params,
                        worklists=worklists,
                        bundle_dir=bundle_dir,
                        run_dir=run_dir,
                        report=report,
                        new_crops=new_crops,
                        depth=depth,
                    )
                return

            # Ancestor: finish the in-progress child, then continue this graph.
            self._resume_descend(
                frames,
                depth + 1,
                workflow=workflow,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                report=report,
                new_crops=new_crops,
                attended_transition=attended_transition,
            )
            if state.kind is StateKind.LOOP:
                loop = state.loop
                assert loop is not None
                body = workflow.subflows.get(loop.body)
                if body is None:
                    raise _ProgramHalt(
                        "halt",
                        f"resume loop body subflow '{loop.body}' is not defined "
                        "— run aborted",
                    )
                cursor = frames[depth + 1].loop
                rows = cursor.rows if cursor is not None else []
                start_i = (cursor.row_index + 1) if cursor is not None else 0
                for i in range(start_i, len(rows)):
                    iter_params = {**params, **rows[i]}
                    self._walk_graph(
                        body,
                        graph_id=loop.body,
                        workflow=workflow,
                        params=iter_params,
                        worklists=worklists,
                        bundle_dir=bundle_dir,
                        run_dir=run_dir,
                        report=report,
                        new_crops=new_crops,
                        depth=depth + 1,
                        loop_cursor=LoopCursor(
                            loop_state_id=state.id,
                            relation=loop.relation,
                            row_index=i,
                            rows=rows,
                        ),
                    )
                nxt = self._select_transition(
                    state,
                    workflow=workflow,
                    params=params,
                    bundle_dir=bundle_dir,
                    report=report,
                    run_dir=run_dir,
                )
            else:
                # subflow_call: continue after the call once the child returned.
                nxt = self._select_transition(
                    state,
                    workflow=workflow,
                    params=params,
                    bundle_dir=bundle_dir,
                    report=report,
                    run_dir=run_dir,
                )
            if nxt is not None:
                live["state_id"] = nxt
                self._run_states_from(
                    graph,
                    live,
                    workflow=workflow,
                    params=params,
                    worklists=worklists,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                    report=report,
                    new_crops=new_crops,
                    depth=depth,
                )
        finally:
            self._frame_stack.pop()

    def _build_graph_ctx(self, state: State, graph: ProgramGraph) -> _GraphStepContext:
        """Assemble the ``_GraphStepContext`` for an action state: the previously
        executed action (for the TYPE focus heuristic) and, for a SCROLL step,
        the closed-loop successors derived from the graph's transitions."""
        next_anchored: Optional[Step] = None
        following: Optional[Step] = None
        if state.step is not None and state.step.action is ActionKind.SCROLL:
            following, next_anchored = self._scroll_successors(state, graph)
        return _GraphStepContext(
            prev_action=self._prev_action,
            next_anchored=next_anchored,
            following=following,
            has_exception_handler=state.on_exception is not None,
        )

    @staticmethod
    def _scroll_successors(
        state: State, graph: ProgramGraph
    ) -> tuple[Optional[Step], Optional[Step]]:
        """(immediately-following step, next anchored step) for a SCROLL state's
        closed loop, following unconditional transitions within this graph. The
        SCROLL-in-a-graph case is rare; this is best-effort and stays inside the
        one graph (subflow boundaries end the scan)."""

        def first_target(s: State) -> Optional[str]:
            for t in s.transitions:
                if t.guard is None:
                    return t.target
            return s.transitions[0].target if s.transitions else None

        following: Optional[Step] = None
        next_anchored: Optional[Step] = None
        visited: set[str] = set()
        cur = first_target(state)
        first = True
        while cur is not None and cur not in visited:
            visited.add(cur)
            st = graph.states.get(cur)
            if st is None or st.kind is not StateKind.ACTION or st.step is None:
                break
            if first:
                following = st.step
                first = False
            if st.step.anchor is not None:
                next_anchored = st.step
                break
            cur = first_target(st)
        return following, next_anchored

    # -- per-step execution ---------------------------------------------------

    def _qualification_fault_driver_refusal(self, workflow: Workflow) -> Optional[str]:
        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_kind in {
            None,
            "representative",
        }:
            return None
        driver = self.qualification_fault_driver
        if driver is None:
            return "qualification fault driver is not configured"
        try:
            if driver.driver_id != authorization.qualification_fault_driver_id:
                return "qualification fault driver id does not match authorization"
            if (
                driver.contract_sha256
                != authorization.qualification_fault_driver_contract_sha256
            ):
                return (
                    "qualification fault driver contract does not match authorization"
                )
            if (
                driver.attestation_key_id
                != authorization.qualification_fault_driver_key_id
            ):
                return "qualification fault driver key does not match authorization"
        except Exception:
            return "qualification fault driver identity could not be observed"
        project = workflow.qualification
        assert project is not None
        trusted = project.trusted_fault_driver_keys.get(
            authorization.qualification_fault_driver_key_id or ""
        )
        if trusted is None:
            return "qualification fault driver key is not trusted by the project"
        self._qualification_fault_public_key = trusted
        return None

    @staticmethod
    def _qualification_fault_gate_for_kind(
        kind: QualificationFaultKind,
    ) -> QualificationFaultGate:
        gates: dict[QualificationFaultKind, QualificationFaultGate] = {
            "ambiguity": "target_resolution",
            "wrong_identity": "identity_verification",
            "stale_identity": "actuation_revalidation",
            "weak_effect": "effect_strength",
            "missing_effect": "effect_verifier",
        }
        return gates[kind]

    def _qualification_fault_context(
        self,
        *,
        gate: QualificationFaultGate,
        step: Step,
        actuation_path: Literal["gui", "api"],
        before_input_sha256: str,
        effect_verifier: Any,
        effects: tuple[Any, ...] = (),
    ) -> Optional[QualificationFaultContext]:
        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_kind in {
            None,
            "representative",
        }:
            return None
        kind = cast(QualificationFaultKind, authorization.qualification_case_kind)
        if self._qualification_fault_gate_for_kind(kind) != gate:
            return None
        if authorization.qualification_fault_step_id_sha256 != sha256_bytes(
            step.id.encode("utf-8")
        ):
            return None
        if self._qualification_case_action_paths.get(step.id) != actuation_path:
            return None
        required = (
            authorization.qualification_project_id,
            authorization.qualification_project_revision,
            authorization.qualification_project_contract_sha256,
            authorization.qualification_campaign_id_sha256,
            self._qualification_id_sha256(authorization.qualification_case_id),
            authorization.qualification_case_input_sha256,
            authorization.qualification_run_id_sha256,
        )
        if any(value is None for value in required):
            raise RuntimeError("qualification_fault_binding_incomplete")
        return QualificationFaultContext(
            project_id=cast(str, required[0]),
            project_revision=cast(int, required[1]),
            project_contract_sha256=cast(str, required[2]),
            campaign_id_sha256=cast(str, required[3]),
            case_id_sha256=cast(str, required[4]),
            case_input_sha256=cast(str, required[5]),
            run_id_sha256=cast(str, required[6]),
            step_id=step.id,
            actuation_path=actuation_path,
            fault_kind=kind,
            gate=gate,
            before_input_sha256=before_input_sha256,
            backend=self.backend,
            vision=self.vision,
            effect_verifier=effect_verifier,
            effects=effects,
        )

    def _require_current_fault_driver_binding(self) -> QualificationFaultDriver:
        """Snapshot the configured driver identity immediately before mutation."""

        driver = self.qualification_fault_driver
        authorization = self.governed_authorization
        if driver is None or authorization is None:
            raise RuntimeError("qualification_fault_driver_missing")
        try:
            observed = (
                driver.driver_id,
                driver.contract_sha256,
                driver.attestation_key_id,
            )
        except Exception as exc:
            raise RuntimeError(
                "qualification_fault_driver_identity_unavailable"
            ) from exc
        expected = (
            authorization.qualification_fault_driver_id,
            authorization.qualification_fault_driver_contract_sha256,
            authorization.qualification_fault_driver_key_id,
        )
        if observed != expected:
            raise RuntimeError("qualification_fault_driver_binding_changed")
        return driver

    def _validate_fault_mutation(
        self,
        context: QualificationFaultContext,
        receipt: FaultMutationReceipt,
        *,
        after_input_sha256: str,
    ) -> None:
        authorization = self.governed_authorization
        if authorization is None:
            raise RuntimeError("qualification_fault_authorization_missing")
        expected = {
            "project_id": context.project_id,
            "project_revision": context.project_revision,
            "project_contract_sha256": context.project_contract_sha256,
            "campaign_id_sha256": context.campaign_id_sha256,
            "case_id_sha256": context.case_id_sha256,
            "case_input_sha256": context.case_input_sha256,
            "run_id_sha256": context.run_id_sha256,
            "step_id_sha256": sha256_bytes(context.step_id.encode("utf-8")),
            "actuation_path": context.actuation_path,
            "fault_kind": context.fault_kind,
            "gate": context.gate,
            "driver_id": authorization.qualification_fault_driver_id,
            "driver_contract_sha256": (
                authorization.qualification_fault_driver_contract_sha256
            ),
            "attestation_key_id": (authorization.qualification_fault_driver_key_id),
            "before_input_sha256": context.before_input_sha256,
            "after_input_sha256": after_input_sha256,
        }
        actual = receipt.model_dump(mode="python")
        if any(actual.get(key) != value for key, value in expected.items()):
            raise RuntimeError("qualification_fault_receipt_binding_invalid")
        if context.before_input_sha256 == after_input_sha256:
            raise RuntimeError("qualification_fault_input_unchanged")
        trusted_key = self._qualification_fault_public_key
        if trusted_key is None or not verify_fault_mutation_receipt(
            receipt,
            trusted_public_key_base64=trusted_key,
        ):
            raise RuntimeError("qualification_fault_receipt_signature_invalid")
        if self._qualification_fault_mutations:
            raise RuntimeError("qualification_fault_mutation_repeated")
        self._qualification_fault_mutations.append(receipt)

    def _apply_screen_fault_mutation(
        self,
        *,
        gate: QualificationFaultGate,
        step: Step,
        before_png: bytes,
        effect_verifier: Any,
    ) -> bytes:
        if self._qualification_fault_mutations:
            return before_png
        context = self._qualification_fault_context(
            gate=gate,
            step=step,
            actuation_path="gui",
            before_input_sha256=sha256_bytes(before_png),
            effect_verifier=effect_verifier,
        )
        if context is None:
            return before_png
        driver = self._require_current_fault_driver_binding()
        mutation = driver.mutate(context)
        if mutation is None:
            return before_png
        if mutation.replace_effect_verifier:
            raise RuntimeError("qualification_screen_fault_replaced_verifier")
        after_png = self.backend.screenshot()
        self._validate_fault_mutation(
            context,
            mutation.receipt,
            after_input_sha256=sha256_bytes(after_png),
        )
        return after_png

    def _apply_effect_fault_mutation(
        self,
        *,
        gate: QualificationFaultGate,
        step: Step,
        actuation_path: Literal["gui", "api"],
        effect_verifier: Any,
        effects: tuple[Any, ...],
    ) -> Any:
        if self._qualification_fault_mutations:
            return effect_verifier
        before_sha256 = effect_verifier_input_sha256(effect_verifier, effects)
        context = self._qualification_fault_context(
            gate=gate,
            step=step,
            actuation_path=actuation_path,
            before_input_sha256=before_sha256,
            effect_verifier=effect_verifier,
            effects=effects,
        )
        if context is None:
            return effect_verifier
        driver = self._require_current_fault_driver_binding()
        mutation = driver.mutate(context)
        if mutation is None:
            return effect_verifier
        if not mutation.replace_effect_verifier:
            raise RuntimeError("qualification_effect_fault_did_not_replace_verifier")
        after_sha256 = effect_verifier_input_sha256(
            mutation.effect_verifier,
            effects,
        )
        self._validate_fault_mutation(
            context,
            mutation.receipt,
            after_input_sha256=after_sha256,
        )
        return mutation.effect_verifier

    def _run_step(
        self,
        step: Step,
        *,
        workflow: Workflow,
        step_index: int,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        new_crops: dict[str, bytes],
        graph_ctx: Optional["_GraphStepContext"] = None,
    ) -> StepResult:
        """Execute a single step; never raises (failures land in the result).

        ``graph_ctx`` is set ONLY by the program interpreter (Phase 2): it
        supplies the ``prev_action`` / scroll-successor context the linear loop
        derives from ``workflow.steps[step_index±1]`` (which does not exist in a
        graph). When None (the linear path) every lookup is exactly as before --
        so a linear run is byte-for-byte unchanged.
        """
        t0 = time.monotonic()
        evidence_step_id = self._new_step_evidence_key(run_dir, step.id)
        result = StepResult(
            step_id=step.id,
            intent=step.intent,
            ok=False,
            risk=step.risk,
            risk_explanation=step.risk_explanation,
            risk_review_required=step.risk_review_required,
            delivery_attempted=False,
        )
        overlay_current, overlay_total = self._control_overlay_progress(
            workflow, step_index, graph_ctx
        )
        qualification_path: Literal["gui", "api"] | None = None
        authorization = self.governed_authorization
        if (
            authorization is not None
            and authorization.qualification_case_id is not None
        ):
            from openadapt_flow.qualification import qualification_action_requirements

            required_actions, _required_identity = qualification_action_requirements(
                workflow
            )
            qualification_path = self._qualification_case_action_paths.get(step.id)
            if step.id in required_actions and qualification_path is None:
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = (
                    "qualification case reached a required write outside its exact "
                    "authorized actuation-path map"
                )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result
            if qualification_path == "api" and step.api_binding is None:
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = (
                    "qualification case requires the API actuation path, but its "
                    "binding is unavailable"
                )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result
        if (
            qualification_path != "gui"
            and step.api_binding is not None
            and self.api_actuator is None
            and (
                qualification_path == "api" or step.api_binding.on_unavailable == "halt"
            )
        ):
            self._set_api_unavailable_refusal(
                step,
                result,
                reason="no API actuator is configured for this deployment",
            )
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0
            return result
        # API/tool tier -- the TOP of the capability ladder. When the step
        # carries an api_binding and an ApiActuator is configured, PERFORM the
        # write via the API (deterministic, $0, no GUI), CONFIRM it with the
        # EffectVerifier, and SKIP the GUI resolve/act entirely. Returns True
        # when the API tier took responsibility (actuated+verified, or HALTed);
        # returns False only when the API tier is unavailable AND the binding
        # permits GUI fallback. API-only bindings return a typed pre-delivery
        # halt with NO write performed (the no-double-write contract). See
        # openadapt_flow.runtime.actuators.
        api_effect_verifier = self.effect_verifier
        api_effects: tuple[Any, ...] = ()
        api_fault_mutation_start = len(self._qualification_fault_mutations)
        if (
            qualification_path != "gui"
            and self.api_actuator is not None
            and step.api_binding is not None
            and step.api_binding.effects
        ):
            api_effects = tuple(self._resolve_effects(step.api_binding.effects, params))
            api_effect_verifier = self._apply_effect_fault_mutation(
                gate="effect_verifier",
                step=step,
                actuation_path="api",
                effect_verifier=api_effect_verifier,
                effects=api_effects,
            )
            api_effect_verifier = self._apply_effect_fault_mutation(
                gate="effect_strength",
                step=step,
                actuation_path="api",
                effect_verifier=api_effect_verifier,
                effects=api_effects,
            )
        if (
            qualification_path != "gui"
            and self.api_actuator is not None
            and step.api_binding is not None
        ):
            if self._try_api_tier(
                step,
                params,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                effect_verifier=api_effect_verifier,
            ):
                if not result.ok and result.failure_category is None:
                    result.safety_halt = True
                    result.failure_category = (
                        "governed_refusal"
                        if self.governed_authorization is not None
                        else "safety_halt"
                    )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result
            if qualification_path == "api":
                result.ok = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = (
                    "qualification case requires the API actuation path, but that "
                    "path reported unavailable; refusing GUI fallback"
                )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result
            if len(self._qualification_fault_mutations) > api_fault_mutation_start:
                result.ok = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = (
                    "the qualification fault changed the API detector input, but "
                    "the API path became unavailable without producing the required "
                    "detector refusal; refusing to reuse that mutation on the GUI "
                    "actuation path"
                )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result

        # Default behavior settles exactly as before. The opt-in readiness gate
        # owns its own single bounded settle call inside ``_apply_step_gates``;
        # capture only a raw diagnostic frame here so it is not preceded by an
        # unobservable proceed-anyway settle attempt.
        before_png = (
            self.backend.screenshot()
            if self.require_settled
            else self.vision.wait_settled(self.backend)
        )
        result.before_png = self._save_step_png(
            run_dir, evidence_step_id, "before", before_png
        )
        last_frame = before_png
        self._emit_control_overlay_phase(
            "observing",
            current_step=overlay_current,
            total_steps=overlay_total,
            observation_png=before_png,
        )
        # System-of-record pre-state, snapshotted just before the action when
        # the step declares effects (see the block guarding self._act below).
        effect_pre_state: Optional[EffectState] = None
        # The step's effect contracts with every ValueExpr bound to THIS run's
        # params (P0-3); resolved BEFORE the pre-state snapshot so verification
        # targets the record this run wrote, not the demonstration's.
        resolved_effects: Optional[list["Effect"]] = None
        # The verifier used for THIS step. Defaults to the configured one, but a
        # GUI-only recording whose mined effects are auto-derived DIFFERENT-PATH
        # on-screen read-backs is verified out of the box by a backend-bound
        # read-back verifier even with NO effect verifier configured (the
        # no-connector default); see the effect block below.
        active_verifier = self.effect_verifier
        # A backend can fail after an actuation API has begun but before it can
        # return a delivery receipt (for example, a click removes its iframe).
        # Such an action is never retried.  The runtime continues through the
        # configured postcondition and independent-effect contract, then
        # reports VERIFIED only if that complete contract proves the outcome.
        delivery_uncertain = False

        try:
            # Reject a composite remote-selection bundle/backend mismatch before
            # target resolution can acquire a remote actuation lease.  The
            # contract is intrinsic to SELECT_OPTION (old runtimes reject that
            # enum value), so there is never a safe TYPE-like fallback.
            selection_contract_error = self._selection_contract_error(step, workflow)
            if selection_contract_error is not None:
                result.error = selection_contract_error
                result.safety_halt = True
                result.failure_category = "safety_halt"
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "after", last_frame
                )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result

            # Workflow-program IR, Phase 1: evaluate the step's guard
            # (precondition) then its wait_until (readiness) BEFORE resolving /
            # acting. Both are model-free. A SCROLL step's wait_until is
            # consumed by its own closed loop (see _act_scroll), not here.
            proceed, gate_error, before_png = self._apply_step_gates(
                step,
                before_png,
                bundle_dir,
                params,
                result,
                workflow,
            )
            if before_png is not last_frame:
                result.before_png = self._save_step_png(
                    run_dir, evidence_step_id, "before", before_png
                )
                last_frame = before_png
            if not proceed:
                # gate_error None => guard skipped the step (no-op success);
                # gate_error set => guard/wait_until HALTed the run.
                result.skipped = gate_error is None
                result.ok = gate_error is None
                result.error = gate_error
                if self._governed_asset_mutation is not None:
                    result.safety_halt = True
                if gate_error is not None and result.failure_category is None:
                    result.safety_halt = True
                    result.failure_category = (
                        "governed_refusal"
                        if self.governed_authorization is not None
                        else "safety_halt"
                    )
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "after", last_frame
                )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result

            before_png = self._apply_screen_fault_mutation(
                gate="target_resolution",
                step=step,
                before_png=before_png,
                effect_verifier=active_verifier,
            )
            result.before_png = self._save_step_png(
                run_dir, evidence_step_id, "before", before_png
            )
            last_frame = before_png
            resolution, matched_region, error = self._resolve_step(
                step, before_png, bundle_dir, workflow
            )
            # Retry ladder failures with fresh settled frames until
            # ``step.timeout_s``: a remote app can present a settled-looking
            # but still-loading frame (wait_settled times out), and the
            # target only appears moments later. Structural errors (missing
            # anchor) and the risk gate (resolution is not None) never retry.
            deadline = t0 + step.timeout_s
            while (
                error is not None
                and resolution is None
                and step.anchor is not None
                and time.monotonic() < deadline
            ):
                time.sleep(self.poll_interval_s)
                before_png = self.vision.wait_settled(self.backend)
                result.before_png = self._save_step_png(
                    run_dir, evidence_step_id, "before", before_png
                )
                last_frame = before_png
                resolution, matched_region, error = self._resolve_step(
                    step, before_png, bundle_dir, workflow
                )
            if resolution is not None and matched_region is not None:
                assert step.anchor is not None
                resolution = self._retain_visual_resolution_evidence(
                    workflow=workflow,
                    anchor=step.anchor,
                    resolution=resolution,
                    matched_region=matched_region,
                    frame_png=before_png,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                )
            result.resolution = resolution
            if error is None and resolution is not None:
                before_png = self._apply_screen_fault_mutation(
                    gate="identity_verification",
                    step=step,
                    before_png=before_png,
                    effect_verifier=active_verifier,
                )
                result.before_png = self._save_step_png(
                    run_dir, evidence_step_id, "before", before_png
                )
                last_frame = before_png
                error = self._identity_gate_error(
                    step,
                    resolution,
                    before_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    evidence_run_dir=run_dir,
                )
                if error is not None and result.identity is not None:
                    code: Literal["identity_conflict", "identity_unverifiable"] = (
                        "identity_conflict"
                        if result.identity.status == "mismatch"
                        else "identity_unverifiable"
                    )
                    result.safety_refusal_evidence = SafetyRefusalEvidence(
                        stage="identity_verification",
                        code=code,
                        detector_input_sha256=sha256_bytes(before_png),
                    )
            # System-of-record effect verification -- SNAPSHOT the record just
            # before the (consequential) action, so the post-action verifier
            # can count only what THIS step wrote (delta / at-most-once /
            # collateral loss). Fail-safe, mirroring the identity gate: a step
            # that declares effects with NO verifier configured is a deployment
            # error -- refuse to perform an unverifiable consequential write
            # rather than pass it silently.
            if error is None and step.effects:
                resolved_effects = self._resolve_effects(step.effects, params)
                effect_inputs = tuple(resolved_effects)
                active_verifier = self._apply_effect_fault_mutation(
                    gate="effect_verifier",
                    step=step,
                    actuation_path="gui",
                    effect_verifier=active_verifier,
                    effects=effect_inputs,
                )
                active_verifier = self._apply_effect_fault_mutation(
                    gate="effect_strength",
                    step=step,
                    actuation_path="gui",
                    effect_verifier=active_verifier,
                    effects=effect_inputs,
                )
                missing_verifier_fault_applied = any(
                    receipt.gate == "effect_verifier"
                    and receipt.fault_kind == "missing_effect"
                    for receipt in self._qualification_fault_mutations
                )
                if (
                    error is None
                    and active_verifier is None
                    and self._all_default_readback(step.effects)
                    and not missing_verifier_fault_applied
                ):
                    # AUTO-DEFAULT (no connector): every declared effect is an
                    # auto-derived DIFFERENT-PATH on-screen read-back, whose
                    # measured false-CONFIRM rate is ~0. Wire a read-back
                    # verifier bound to the live backend for this step. A
                    # same-surface (weak) read-back or a structured effect is
                    # NOT eligible and falls through to the HALT/approval path.
                    active_verifier = self._auto_readback_verifier()
                if error is not None:
                    pass
                elif active_verifier is None:
                    authorization = self.governed_authorization
                    approved = (
                        authorization is not None
                        and authorization.approves_unverified_write(step)
                    )
                    if approved:
                        assert authorization is not None
                        result.effect_contract_hashes.extend(
                            effect.contract_hash() for effect in resolved_effects
                        )
                        result.effect_approved_unverified = True
                        result.effect_results.append(
                            "[approved-unverified] no independent EffectVerifier; "
                            "GUI actuation explicitly approved for authorization "
                            f"{authorization.authorization_id}; "
                            "screen postconditions still required"
                        )
                    else:
                        error = (
                            f"Step '{step.id}' ({step.intent}) declares "
                            f"{len(step.effects)} system-of-record effect(s) but no "
                            "EffectVerifier is configured for this run — refusing "
                            "to perform an unverifiable consequential write "
                            "(deployment/configuration error); run aborted"
                        )
                        result.effect_verified = False
                        result.effect_results.append(
                            "no EffectVerifier configured for a step that declares "
                            "effects (fail-safe HALT)"
                        )
                        result.safety_refusal_evidence = SafetyRefusalEvidence(
                            stage="effect_verifier",
                            code="effect_verifier_missing",
                            detector_input_sha256=effect_verifier_input_sha256(
                                None,
                                effect_inputs,
                            ),
                        )
                    if error is not None and self.governed_authorization is not None:
                        result.safety_halt = True
                        result.failure_category = "governed_refusal"
                else:
                    # Bind the effect contracts to this run's params BEFORE the
                    # pre-state snapshot (P0-3): match/value/idempotency_key must
                    # describe the record THIS run writes, not the demo's.
                    result.effect_contract_hashes.extend(
                        effect.contract_hash() for effect in resolved_effects
                    )
                    error = self._profile_effect_tier_refusal(
                        workflow,
                        step,
                        "gui",
                        resolved_effects,
                        active_verifier,
                    )
                    if error is None:
                        try:
                            effect_pre_state = self._capture_effect_pre_state(
                                active_verifier, resolved_effects
                            )
                        except Exception as exc:  # noqa: BLE001 - verifier boundary
                            error = (
                                "the selected system-of-record effect verifier "
                                "failed its preflight or pre-state capture before "
                                f"actuation ({type(exc).__name__}); refusing input"
                            )
                        else:
                            error = self._candidate_binding_refusal(
                                effect_pre_state, resolved_effects
                            )
                            if (
                                error is None
                                and self._required_effect_pre_state_unreadable(
                                    active_verifier,
                                    effect_pre_state,
                                    resolved_effects,
                                )
                            ):
                                error = (
                                    "the selected system-of-record effect verifier is "
                                    "unreachable before actuation; refusing input"
                                )
                        if error is None:
                            error = self._profile_effect_tier_refusal(
                                workflow,
                                step,
                                "gui",
                                resolved_effects,
                                active_verifier,
                            )
                    if error is not None:
                        result.effect_verified = False
                        result.safety_halt = True
                        result.failure_category = "governed_refusal"
                        result.effect_results.append(error)
                        result.safety_refusal_evidence = SafetyRefusalEvidence(
                            stage="effect_strength",
                            code="effect_strength_insufficient",
                            detector_input_sha256=effect_verifier_input_sha256(
                                active_verifier,
                                effect_inputs,
                            ),
                        )

            if self._governed_asset_mutation is not None:
                error = self._governed_asset_mutation
                result.safety_halt = True

            if error is None:
                before_png = self._apply_screen_fault_mutation(
                    gate="actuation_revalidation",
                    step=step,
                    before_png=before_png,
                    effect_verifier=active_verifier,
                )
                (
                    resolution,
                    matched_region,
                    before_png,
                    error,
                ) = self._revalidate_consequential_actuation(
                    step,
                    resolution,
                    matched_region,
                    before_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir=run_dir,
                    arm_keyboard=step.action in (ActionKind.KEY, ActionKind.HOTKEY),
                )
                if resolution is not None and matched_region is not None:
                    assert step.anchor is not None
                    resolution = self._retain_visual_resolution_evidence(
                        workflow=workflow,
                        anchor=step.anchor,
                        resolution=resolution,
                        matched_region=matched_region,
                        frame_png=before_png,
                        bundle_dir=bundle_dir,
                        run_dir=run_dir,
                    )
                result.resolution = resolution
                if before_png is not last_frame:
                    result.before_png = self._save_step_png(
                        run_dir, evidence_step_id, "before", before_png
                    )
                    last_frame = before_png
                if error is not None and any(
                    receipt.gate == "actuation_revalidation"
                    for receipt in self._qualification_fault_mutations
                ):
                    result.safety_refusal_evidence = SafetyRefusalEvidence(
                        stage="actuation_revalidation",
                        code="actuation_observation_changed",
                        detector_input_sha256=sha256_bytes(before_png),
                    )

            if error is None:
                if resolved_effects is not None and active_verifier is not None:
                    error = self._profile_effect_tier_refusal(
                        workflow,
                        step,
                        "gui",
                        resolved_effects,
                        active_verifier,
                    )
                    if error is not None:
                        result.effect_verified = False
                        result.safety_halt = True
                        result.failure_category = "governed_refusal"
                        result.effect_results.append(error)

            if error is not None:
                self._cancel_guarded_coordinate()
                self._cancel_guarded_keyboard()

            authorization = self.governed_authorization
            if (
                error is None
                and authorization is not None
                and authorization.qualification_fault_step_id_sha256
                == sha256_bytes(step.id.encode("utf-8"))
            ):
                error = (
                    "the bound qualification fault case did not produce its "
                    "required detector refusal; refusing to cross the input boundary"
                )
                result.safety_halt = True
                result.failure_category = "governed_refusal"

            if error is None:
                fresh_reacquisitions = 0
                while True:
                    # Structural postconditions compare against the final
                    # observed state immediately before action delivery.
                    # Readiness gates, target resolution, identity checks, and
                    # effect pre-state capture can cross backend or time
                    # boundaries; none of their state changes may be credited
                    # to the workflow action.
                    start_state = self._structural_state()
                    overlay_target = self._control_overlay_browser_target(
                        step,
                        resolution,
                        matched_region,
                        before_png,
                    )
                    self._emit_control_overlay_phase(
                        "executing",
                        current_step=overlay_current,
                        total_steps=overlay_total,
                        target_tracking=overlay_target,
                        observation_png=before_png,
                    )
                    # A retry reaches this last common point only after fresh
                    # target, context identity, and effect pre-state callbacks.
                    # Recheck exact authority after the presentation callback
                    # too, immediately before the next delivery boundary.
                    if fresh_reacquisitions:
                        error = self._fresh_actuation_authorization_refusal(
                            workflow, params, step
                        )
                        if error is not None:
                            result.safety_halt = True
                            result.failure_category = (
                                "governed_refusal"
                                if self.governed_authorization is not None
                                else "safety_halt"
                            )
                            break
                    try:
                        self._active_delivery_resolution = resolution
                        self._active_delivery_region = matched_region
                        error = self._act(
                            step,
                            resolution,
                            params,
                            workflow=workflow,
                            step_index=step_index,
                            bundle_dir=bundle_dir,
                            before_png=before_png,
                            result=result,
                            run_dir=run_dir,
                            graph_ctx=graph_ctx,
                        )
                    except FreshActuationRequired as exc:
                        retry_reset_error: Optional[str] = None
                        reacquisition_backend = (
                            cast(FreshActuationReacquisitionBackend, self.backend)
                            if isinstance(
                                self.backend, FreshActuationReacquisitionBackend
                            )
                            else None
                        )
                        can_retry = (
                            result.delivery_attempted is False
                            and fresh_reacquisitions
                            < _MAX_FRESH_ACTUATION_REACQUISITIONS
                            and self._step_needs_consequential_revalidation(
                                step, workflow
                            )
                            and isinstance(self.backend, RemoteActuationBackend)
                            and reacquisition_backend is not None
                        )
                        if can_retry:
                            assert reacquisition_backend is not None
                            try:
                                reacquisition_backend.reset_fresh_actuation_state()
                            except Exception as reset_exc:  # noqa: BLE001
                                retry_reset_error = (
                                    "the backend refused the typed zero-input "
                                    "lease reset "
                                    f"({type(reset_exc).__name__})"
                                )
                                can_retry = False
                        result.fresh_actuation_events.append(
                            self._fresh_actuation_event(
                                exc,
                                step=step,
                                workflow=workflow,
                                resolution=self._active_delivery_resolution,
                                matched_region=self._active_delivery_region,
                                retried=can_retry,
                                attempt=len(result.fresh_actuation_events) + 1,
                            )
                        )
                        self._cancel_guarded_coordinate()
                        self._cancel_guarded_keyboard()
                        if not can_retry:
                            if retry_reset_error is not None:
                                retry_detail = retry_reset_error
                            elif result.delivery_attempted is not False:
                                retry_detail = (
                                    "an earlier input edge crossed for this step, "
                                    "so replay cannot retry it"
                                )
                            elif fresh_reacquisitions >= (
                                _MAX_FRESH_ACTUATION_REACQUISITIONS
                            ):
                                retry_detail = (
                                    "the bounded fresh-frame reacquisition "
                                    "limit was exhausted"
                                )
                            elif not self._step_needs_consequential_revalidation(
                                step, workflow
                            ):
                                retry_detail = (
                                    "the step has no complete consequential "
                                    "revalidation contract"
                                )
                            else:
                                retry_detail = (
                                    "the backend cannot reset a typed invalidated "
                                    "lease for complete fresh revalidation"
                                )
                            error = f"{exc}; {retry_detail}"
                            result.safety_halt = True
                            result.failure_category = (
                                "governed_refusal"
                                if self.governed_authorization is not None
                                else "safety_halt"
                            )
                            break

                        fresh_reacquisitions += 1
                        error = self._fresh_actuation_authorization_refusal(
                            workflow, params, step
                        )
                        if error is None:
                            (
                                resolution,
                                matched_region,
                                before_png,
                                error,
                            ) = self._revalidate_consequential_actuation(
                                step,
                                resolution,
                                matched_region,
                                before_png,
                                params,
                                workflow,
                                bundle_dir,
                                result,
                                run_dir=run_dir,
                                arm_keyboard=step.action
                                in (ActionKind.KEY, ActionKind.HOTKEY),
                            )
                            if resolution is not None and matched_region is not None:
                                assert step.anchor is not None
                                resolution = self._retain_visual_resolution_evidence(
                                    workflow=workflow,
                                    anchor=step.anchor,
                                    resolution=resolution,
                                    matched_region=matched_region,
                                    frame_png=before_png,
                                    bundle_dir=bundle_dir,
                                    run_dir=run_dir,
                                )
                            result.resolution = resolution
                            if before_png is not last_frame:
                                result.before_png = self._save_step_png(
                                    run_dir, evidence_step_id, "before", before_png
                                )
                                last_frame = before_png

                        # The verifier pre-state belongs to the exact target,
                        # identity, application, session, and workflow-state
                        # observation that authorizes the next input attempt.
                        effect_refresh_error: Optional[str] = None
                        if (
                            error is None
                            and resolved_effects is not None
                            and active_verifier is not None
                        ):
                            effect_refresh_error = self._profile_effect_tier_refusal(
                                workflow,
                                step,
                                "gui",
                                resolved_effects,
                                active_verifier,
                            )
                            if effect_refresh_error is None:
                                try:
                                    effect_pre_state = self._capture_effect_pre_state(
                                        active_verifier, resolved_effects
                                    )
                                except Exception as exc:  # noqa: BLE001 - verifier boundary
                                    effect_refresh_error = (
                                        "the selected system-of-record effect verifier "
                                        "failed its preflight or pre-state capture "
                                        f"before actuation ({type(exc).__name__}); "
                                        "refusing input"
                                    )
                                else:
                                    effect_refresh_error = (
                                        self._candidate_binding_refusal(
                                            effect_pre_state, resolved_effects
                                        )
                                    )
                                    if (
                                        effect_refresh_error is None
                                        and self._required_effect_pre_state_unreadable(
                                            active_verifier,
                                            effect_pre_state,
                                            resolved_effects,
                                        )
                                    ):
                                        effect_refresh_error = (
                                            "the selected system-of-record effect "
                                            "verifier is unreachable before actuation; "
                                            "refusing input"
                                        )
                                if effect_refresh_error is None:
                                    effect_refresh_error = (
                                        self._profile_effect_tier_refusal(
                                            workflow,
                                            step,
                                            "gui",
                                            resolved_effects,
                                            active_verifier,
                                        )
                                    )
                            error = effect_refresh_error

                        if error is not None:
                            result.safety_halt = True
                            result.failure_category = (
                                "governed_refusal"
                                if self.governed_authorization is not None
                                else "safety_halt"
                            )
                            if effect_refresh_error is not None:
                                result.effect_verified = False
                                result.effect_results.append(effect_refresh_error)
                            break
                        continue
                    except ActionDeliveryUncertain as exc:
                        delivery_uncertain = True
                        result.delivery_uncertainty = ActionDeliveryUncertainty(
                            operation=exc.operation,
                            native=exc.native,
                            target_fingerprint=exc.target_fingerprint,
                            observed_at=datetime.now(timezone.utc).isoformat(),
                            cause_type=exc.cause_type,
                        )
                        # Continue to settled-state, postcondition, and
                        # independent effect verification. This is not
                        # permission to assume delivery. Success now requires
                        # the complete configured contract.
                        error = None
                    except (OcrResolutionRefused, StructuralResolutionRefused):
                        # This typed refusal proves that the current delivery
                        # boundary did not emit input. Preserve its exact safety
                        # semantics even when an earlier focus edge in the same
                        # composite step already crossed.
                        raise
                    except Exception as exc:
                        if result.delivery_attempted is not True:
                            raise
                        # An untyped backend exception after a delivery boundary
                        # is conservative uncertainty, not proof of no input.
                        # Do not retain its message because it can contain page
                        # or record data.  Continue through the same postcondition
                        # and independent-effect reconciliation as the typed path.
                        delivery_uncertain = True
                        target_fingerprint = None
                        if (
                            resolution is not None
                            and resolution.structural_handle is not None
                        ):
                            target_fingerprint = (
                                resolution.structural_handle.target_fingerprint
                            )
                        result.delivery_uncertainty = ActionDeliveryUncertainty(
                            operation=step.action.value,
                            native=result.actuation == "uia",
                            target_fingerprint=target_fingerprint,
                            observed_at=datetime.now(timezone.utc).isoformat(),
                            cause_type=type(exc).__name__,
                        )
                        error = None
                    if self._governed_asset_mutation is not None:
                        error = self._governed_asset_mutation
                        result.safety_halt = True
                    break

            if error is None:
                try:
                    after_png = self.vision.wait_settled(self.backend)
                    last_frame = after_png
                    self._emit_control_overlay_phase(
                        "verifying",
                        current_step=overlay_current,
                        total_steps=overlay_total,
                        observation_png=after_png,
                    )
                    selection_error: Optional[str] = None
                    if step.action is ActionKind.SELECT_OPTION:
                        selection_text = (
                            runtime_param_text(params[step.param])
                            if step.param is not None
                            else step.text or ""
                        )
                        selection_error = self._verify_selected_option(
                            step,
                            selection_text,
                            after_png,
                            bundle_dir,
                            workflow,
                            result,
                        )
                    if selection_error is None:
                        (
                            postconditions_ok,
                            last_frame,
                            failed,
                        ) = self._check_postconditions(
                            step, after_png, bundle_dir, start_state, result
                        )
                    else:
                        postconditions_ok = False
                        failed = [selection_error]
                    if self._governed_asset_mutation is not None:
                        postconditions_ok = False
                        failed.append(self._governed_asset_mutation)
                    # ``postconditions_ok`` records the explicit ``expect``
                    # contract only.  Input readback has its own typed field
                    # and receipt evidence.  An empty predicate list is not an
                    # implicit explicit-predicate pass.
                    result.postconditions_ok = (
                        postconditions_ok if step.expect else None
                    )
                    if result.postcondition_drift_rescues:
                        # The rescue descriptions embed OCR'd on-screen text
                        # (postcondition literals) — scrub PHI before it hits the
                        # console log (see openadapt_flow.privacy).
                        rescued = "; ".join(
                            # Rescues are non-empty descriptions, so scrub_text
                            # returns a str; ``or ""`` only satisfies its
                            # Optional[str] return.
                            _scrub_phi(r) or ""
                            for r in result.postcondition_drift_rescues
                        )
                        print(
                            "  drift-oracle: "
                            f"{len(result.postcondition_drift_rescues)}"
                            " postcondition(s) confirmed by VLM under render "
                            "drift — " + rescued
                        )
                    if not postconditions_ok:
                        if selection_error is not None:
                            error = selection_error
                        else:
                            detail = "; ".join(failed) or "unknown postcondition"
                            error = (
                                f"Postconditions failed for step '{step.id}' "
                                f"({step.intent}): expected screen state not reached "
                                f"(semantic drift) — failed: {detail} — run aborted"
                            )
                        if self.governed_authorization is not None:
                            result.safety_halt = True
                except Exception as exc:
                    if not delivery_uncertain:
                        raise
                    # A detached/navigated context can also make screen
                    # observation fail.  That does not cancel independent
                    # effect verification; record the bounded failure and keep
                    # going without exposing a backend payload.
                    result.postconditions_ok = False
                    error = (
                        "Post-delivery observation could not confirm the "
                        f"postcondition ({type(exc).__name__})"
                    )

            # Independent system-of-record verification, AFTER the screen
            # postconditions passed: the screen oracle cannot see a partial
            # save, a phantom optimistic-UI success, a duplicate write, or a
            # lost update -- the EffectVerifier reads the REAL record and HALTs
            # on any non-CONFIRMED verdict (docs/design/EFFECT_VERIFIER.md).
            if (error is None or delivery_uncertain) and effect_pre_state is not None:
                effect_error = self._verify_effects(
                    step,
                    effect_pre_state,
                    result,
                    effects=resolved_effects,
                    verifier=active_verifier,
                )
                if effect_error is not None and error is None:
                    error = effect_error
                if effect_error is not None and self.governed_authorization is not None:
                    result.safety_halt = True

            if delivery_uncertain:
                uncertainty = result.delivery_uncertainty
                assert uncertainty is not None
                uncertainty.verification_attempted = True
                uncertainty.postconditions_confirmed = result.postconditions_ok is True
                uncertainty.effects_confirmed = result.effect_verified is True
                complete_contract_confirmed = (
                    uncertainty.postconditions_confirmed
                    and uncertainty.effects_confirmed
                    and error is None
                )
                uncertainty.resolved_by_contract = complete_contract_confirmed
                if not complete_contract_confirmed:
                    detail = (
                        "Action delivery was uncertain and was not retried; "
                        "the complete postcondition and independent effect "
                        "contract did not confirm the intended outcome"
                    )
                    if error is not None:
                        detail += f". Verification detail: {error}"
                    error = detail
                    result.safety_halt = True
                    result.failure_category = (
                        "governed_refusal"
                        if self.governed_authorization is not None
                        else "safety_halt"
                    )

            result.ok = error is None
            result.error = error

            if (
                result.ok
                and result.delivery_uncertainty is None
                and resolution is not None
                and matched_region is not None
                and resolution.rung not in ("template", "structural")
                and step.anchor is not None
            ):
                # Heal from the PRE-action frame: that is the frame the
                # anchor was resolved against (the action may have navigated
                # to a different screen, where a crop at the old location
                # would be garbage).
                #
                # ``structural`` is exempt alongside ``template``: a
                # deterministic DOM/UIA locate does NOT signal that the visual
                # template is stale (structural runs FIRST regardless of visual
                # drift), so refreshing the crop every run would emit a spurious
                # reviewable anchor diff and break the zero-heal happy path. The
                # visual fallback stays as recorded -- still valid for the
                # substrate it was recorded on.
                #
                # The heal is GOVERNED: a patch that would weaken the step's
                # identity band (the reviewed context-drop bug), effect
                # coverage, or risk class is quarantined and the run HALTS
                # rather than silently applying an unverified repair.
                heal_outcome = self._heal_step(
                    step,
                    resolution,
                    matched_region,
                    before_png,
                    workflow,
                    run_dir,
                    new_crops,
                )
                if heal_outcome.promoted:
                    result.heal = heal_outcome.event
                else:
                    result.ok = False
                    error = heal_outcome.halt_reason
                    result.error = error
        except OcrResolutionRefused as exc:
            self._cancel_guarded_coordinate()
            self._cancel_guarded_keyboard()
            # Repeated target/landmark OCR is a deterministic ambiguity, not a
            # transient absence. Surface it as a typed operator-visible safety
            # halt immediately; never retry into weaker evidence or actuation.
            result.ok = False
            result.safety_halt = True
            result.failure_category = "safety_halt"
            admission = (
                "no further action was admitted after the earlier input edge"
                if result.delivery_attempted is True
                else "no action was admitted"
            )
            result.error = (
                f"OCR safety refusal for step '{step.id}' "
                f"({step.intent}): {exc} — {admission}"
            )
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="target_resolution",
                code="target_ambiguous",
                detector_input_sha256=sha256_bytes(last_frame),
            )
        except StructuralResolutionRefused as exc:
            self._cancel_guarded_coordinate()
            self._cancel_guarded_keyboard()
            # Ambiguity, disappearance, or a stale resolve->act fingerprint is
            # an intentional fail-closed refusal. Record it as a safety halt,
            # never as an incidental backend crash and never retry via pixels.
            result.ok = False
            result.safety_halt = True
            result.failure_category = "safety_halt"
            admission = (
                "no further action was admitted after the earlier input edge"
                if result.delivery_attempted is True
                else "no action was admitted"
            )
            result.error = (
                f"Structural safety refusal for step '{step.id}' "
                f"({step.intent}): {exc} — {admission}"
            )
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="target_resolution",
                code="target_ambiguous",
                detector_input_sha256=sha256_bytes(last_frame),
            )
        except Exception as exc:  # defensive: report, don't crash the run
            self._cancel_guarded_coordinate()
            self._cancel_guarded_keyboard()
            result.ok = False
            result.failure_category = "runtime_failure"
            result.error = f"Step '{step.id}' raised {type(exc).__name__}: {exc}"

        if not result.ok and result.failure_category is None:
            if graph_ctx is not None and graph_ctx.has_exception_handler:
                # An authored program exception edge may handle an ordinary
                # action failure (for example, an expected target absence).
                # Typed ambiguity/identity/effect refusals set ``safety_halt``
                # earlier and can never be caught by this branch.
                result.failure_category = "runtime_failure"
            else:
                result.safety_halt = True
                result.failure_category = (
                    "governed_refusal"
                    if self.governed_authorization is not None
                    else "safety_halt"
                )
        result.after_png = self._save_step_png(
            run_dir, evidence_step_id, "after", last_frame
        )
        result.elapsed_ms = (time.monotonic() - t0) * 1000.0
        return result

    # -- API/tool actuation tier (top of the capability ladder) -----------------

    @staticmethod
    def _template_mentions_param(value: Any, param: str) -> bool:
        """Whether an API request template contains the exact ``{param}`` token."""

        needle = "{" + param + "}"
        if isinstance(value, str):
            return needle in value
        if isinstance(value, dict):
            return any(
                Replayer._template_mentions_param(item, param)
                for item in value.values()
            )
        if isinstance(value, (list, tuple)):
            return any(Replayer._template_mentions_param(item, param) for item in value)
        return False

    @staticmethod
    def _url_path_targets_param(url_template: str, param: str) -> bool:
        """Require the identity placeholder as one whole URL path segment."""

        token = "{" + param + "}"
        return token in [
            segment for segment in urlsplit(url_template).path.split("/") if segment
        ]

    @staticmethod
    def _api_request_pointer_value(binding: ApiBinding, pointer: str) -> Any:
        """Resolve one admitted target-bearing pointer without consulting headers."""

        if pointer.startswith("/url/"):
            return binding.url_template
        root_name, *tokens = pointer.lstrip("/").split("/")
        if root_name == "body":
            value: Any = binding.body_template
        elif root_name == "query":
            value = binding.query
        else:
            return None
        for token in tokens:
            decoded = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                if decoded not in value:
                    return None
                value = value[decoded]
            elif isinstance(value, list):
                try:
                    index = int(decoded)
                except ValueError:
                    return None
                if index < 0 or index >= len(value):
                    return None
                value = value[index]
            else:
                return None
        return value

    @staticmethod
    def _api_request_pointer_field_path(pointer: str) -> tuple[str, ...]:
        """Return the fully decoded target-field path after the request root."""

        return tuple(
            token.replace("~1", "/").replace("~0", "~")
            for token in pointer.lstrip("/").split("/")[1:]
        )

    def _api_identity_refusal(
        self,
        step: Step,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        effects: list["Effect"],
        result: StepResult,
    ) -> Optional[str]:
        """Validate an exact qualified identity binding before an API write."""

        authorization_requires = (
            self.governed_authorization is not None
            and self.governed_authorization.requires_verified_identity(step.id)
        )
        project = workflow.qualification
        policy = project.identity_policies.get(step.id) if project is not None else None
        if not authorization_requires and policy is None:
            return None

        binding = step.api_binding
        assert binding is not None
        if not binding.identity:
            return (
                f"API identity verification HALTED step '{step.id}' "
                f"({step.intent}): the qualified action has no exact "
                "api_binding.identity contract — refusing to send the request"
            )

        allowed_keys: Optional[set[str]] = None
        required = 1
        if policy is not None and policy.enforcement.value == "signal_quorum":
            allowed_keys = {signal.key.value for signal in policy.signals}
            required = policy.quorum

        evidence: list[IdentitySignalEvidence] = []
        for identity in binding.identity:
            if allowed_keys is not None and identity.key not in allowed_keys:
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): semantic signal {identity.key!r} is not "
                    "part of the qualified identity policy"
                )
            raw_value = params.get(identity.param)
            value = runtime_param_text(raw_value) if raw_value is not None else None
            if value is None or value == "":
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): required identity parameter "
                    f"{identity.param!r} is missing"
                )
            pointed_templates = [
                self._api_request_pointer_value(binding, pointer)
                for pointer in identity.request_pointers
            ]
            exact_token = "{" + identity.param + "}"
            effect_target_path = tuple(identity.effect_field.split("."))
            pointers_are_bound = all(
                template is not None
                and self._api_request_pointer_field_path(pointer) == effect_target_path
                and (
                    isinstance(template, str)
                    and self._url_path_targets_param(template, identity.param)
                    if pointer.startswith("/url/")
                    else isinstance(template, str) and template == exact_token
                )
                for pointer, template in zip(
                    identity.request_pointers, pointed_templates, strict=True
                )
            )
            if not pointers_are_bound:
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): identity parameter {identity.param!r} is "
                    "not bound at every declared target-bearing request pointer"
                )
            effect_exprs = [
                effect.match.get(identity.effect_field) for effect in effects
            ]
            if not any(
                expr is not None and expr.param == identity.param
                for expr in effect_exprs
            ):
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): effect selector {identity.effect_field!r} "
                    f"is not bound to identity parameter {identity.param!r}"
                )
            evidence.append(
                IdentitySignalEvidence(
                    signal=identity.key,
                    source="api_parameter",
                    verdict="verified",
                    evidence_class="api_request_effect_binding",
                    match="exact",
                )
            )

        if len(evidence) < required:
            return (
                f"API identity verification HALTED step '{step.id}' "
                f"({step.intent}): {len(evidence)}/{required} exact qualified "
                "identity bindings verified — refusing to send the request"
            )
        result.identity = IdentityCheck(
            status="verified",
            mode="signal_quorum",
            coverage=1.0,
            signal_evidence=evidence,
            quorum_required=required,
            quorum_verified=len(evidence),
        )
        return None

    def _try_api_tier(
        self,
        step: Step,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        effect_verifier: Any,
    ) -> bool:
        """Perform ``step.api_binding``'s write via the API and confirm it.

        The TOP of the capability ladder (RFC section 4 ``api`` tier): a
        deterministic, ``$0``, model-free write, gated by the SAME
        ``EffectVerifier`` as a GUI write. For an API write the target
        "identity" is the explicit API parameter -- stronger than a resolved
        pixel band -- so no identity gate is weakened by skipping the GUI.

        Fail-safe ordering (the no-double-write contract):

        - refuse BEFORE any request when the write could not be confirmed (no
          effect contract, or no verifier) -- an unverifiable consequential
          write never proceeds;
        - snapshot the system of record, then actuate ONCE;
        - UNAVAILABLE (request never sent) -> use the binding's configured GUI
          fallback or typed pre-delivery halt; nothing was written;
        - HALT (request sent, outcome unknown / rejected) -> run the configured
          postcondition and independent-effect checks without retry or GUI
          fallback; resolve only if the complete contract proves the effect;
        - ACTUATED (2xx) -> CONFIRM with the EffectVerifier; a non-CONFIRMED
          verdict HALTs exactly as it would for a GUI write.

        Returns True when the API tier took responsibility for the step (the
        result is final -- actuated+verified, HALTed, or a config-error HALT),
        False only when the API tier is UNAVAILABLE and the binding permits the
        caller to fall through to the GUI resolution ladder.
        """
        binding = step.api_binding
        assert binding is not None  # guaranteed by the caller
        assert self.api_actuator is not None  # guaranteed by the caller (line ~1140)

        # An API write MUST be confirmable against its own exact effect
        # contract.  ``step.effects`` belongs exclusively to the GUI fallback;
        # substituting it here could confirm a different record or operation.
        effects = binding.effects
        if not effects:
            result.effect_verified = False
            result.effect_results.append(
                "API binding declares no api_binding.effects contract to "
                "confirm its write -- refusing an "
                "unverifiable API write (fail-safe HALT)"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) has an API binding but no "
                "system-of-record effect to CONFIRM the write -- an API write "
                "must be verifiable; refusing to actuate "
                "(deployment/configuration error); run aborted"
            )
            return True
        identity_refusal = self._api_identity_refusal(
            step,
            params,
            workflow,
            effects,
            result,
        )
        if identity_refusal is not None:
            result.ok = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.error = identity_refusal
            return True
        # Bind the effect contracts to this run before either the verifier
        # strength check or its detector-input digest is evaluated.
        effects = self._resolve_effects(effects, params)
        if effect_verifier is None:
            result.effect_verified = False
            result.effect_results.append(
                "API binding present but no EffectVerifier is configured to "
                "confirm the write (fail-safe HALT)"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) would actuate via the API "
                "but no EffectVerifier is configured to confirm the write -- "
                "refusing an unverifiable consequential write; run aborted"
            )
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="effect_verifier",
                code="effect_verifier_missing",
                detector_input_sha256=effect_verifier_input_sha256(None, effects),
            )
            return True

        # The same parameter substitution that the ApiActuator applies to the
        # URL/query/body now also applies to the effect checked below.
        api_hash_start = len(result.effect_contract_hashes)
        result.effect_contract_hashes.extend(
            effect.contract_hash() for effect in effects
        )
        refusal = self._profile_effect_tier_refusal(
            workflow,
            step,
            "api",
            effects,
            effect_verifier,
        )
        if refusal is not None:
            result.effect_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.effect_results.append(refusal)
            result.error = refusal
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="effect_strength",
                code="effect_strength_insufficient",
                detector_input_sha256=effect_verifier_input_sha256(
                    effect_verifier,
                    effects,
                ),
            )
            return True

        # Snapshot the system of record BEFORE the write so the verifier counts
        # only what THIS actuation wrote (delta / at-most-once / collateral
        # loss), then actuate exactly once.
        try:
            before = self._capture_effect_pre_state(effect_verifier, effects)
        except Exception as exc:  # noqa: BLE001 - deployment verifier boundary
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect pre-state capture failed before request delivery "
                f"({type(exc).__name__}); API actuation was not attempted"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) could not capture the "
                "system-of-record pre-state before API actuation; refusing to "
                "send the request — run aborted"
            )
            return True
        binding_refusal = self._candidate_binding_refusal(before, effects)
        if binding_refusal is not None:
            result.effect_verified = False
            result.effect_results.append(f"[api] {binding_refusal}")
            result.ok = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.error = binding_refusal
            return True
        if self._required_effect_pre_state_unreadable(effect_verifier, before, effects):
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect pre-state is unreachable before request delivery"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) could not read the "
                "system-of-record pre-state before API actuation; refusing to "
                "send the request — run aborted"
            )
            return True
        refusal = self._profile_effect_tier_refusal(
            workflow,
            step,
            "api",
            effects,
            effect_verifier,
        )
        if refusal is not None:
            result.effect_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.effect_results.append(refusal)
            result.error = refusal
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="effect_strength",
                code="effect_strength_insufficient",
                detector_input_sha256=effect_verifier_input_sha256(
                    effect_verifier,
                    effects,
                ),
            )
            return True
        overlay_current, overlay_total = self._control_overlay_progress(
            workflow, step_index, None
        )
        self._emit_control_overlay_phase(
            "executing",
            current_step=overlay_current,
            total_steps=overlay_total,
        )
        refusal = self._delivery_authorization_refusal(workflow, params, step, result)
        if refusal is not None:
            result.ok = False
            result.error = refusal
            return True
        # Structural postconditions compare against the last observation before
        # request delivery, not against state captured earlier during
        # authorization or verifier setup.
        start_state = self._structural_state()
        # This transition is the explicit delivery boundary. Set it BEFORE the
        # call because an exception/timeout may occur after the request left the
        # process but before the actuator can return a receipt-like outcome.
        result.delivery_attempted = True
        try:
            self._require_qualification_environment_current()
            outcome = self.api_actuator.actuate(
                binding,
                runtime_params_for_gui(params),
            )
        except Exception as exc:  # noqa: BLE001 - external actuator boundary
            # The actuator contract is no-throw, but a deployment adapter can
            # still violate it. The delivery boundary was crossed immediately
            # before the call, so fail toward an attempted/unknown write and
            # never fall through to the GUI.
            result.actuation = "api"
            result.effect_verified = False
            result.effect_results.append(
                "[api] actuator raised after delivery may have begun "
                f"({type(exc).__name__}); outcome requires reconciliation"
            )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type=type(exc).__name__,
            )

        from openadapt_flow.runtime.actuators import (
            ActuationStatus,
            ApiActuationResult,
            ApiHaltKind,
        )

        # Only the validated model can prove the pre-delivery UNAVAILABLE
        # state.  A deployment-owned actuator may return any object after its
        # call, including a duck-typed object that claims ``UNAVAILABLE``.
        # Treat that as possible delivery: collect evidence, never fall
        # through to the GUI, and never turn it into success.
        if type(outcome) is not ApiActuationResult:
            result.actuation = "api"
            result.effect_results.append(
                "[api] actuator returned an untyped result after delivery may "
                f"have begun ({type(outcome).__name__}); treating it as a "
                "protocol error that cannot resolve to success"
            )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type="ApiActuatorProtocolError",
                allow_contract_resolution=False,
            )
        try:
            # Revalidate the return value because an adapter can mutate a
            # Pydantic instance after construction.  The runtime trusts the
            # copied, validated receipt only.
            outcome = ApiActuationResult.model_validate(
                outcome.model_dump(mode="python")
            )
        except Exception as exc:  # noqa: BLE001 - deployment actuator boundary
            result.actuation = "api"
            result.effect_results.append(
                "[api] actuator returned a malformed typed result after "
                "delivery may have begun "
                f"({type(exc).__name__}); treating it as a protocol error "
                "that cannot resolve to success"
            )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type="ApiActuatorProtocolError",
                allow_contract_resolution=False,
            )

        outcome_status = outcome.status
        outcome_reason = outcome.reason

        if outcome_status is ActuationStatus.UNAVAILABLE:
            # The request was NEVER sent -- nothing was written. Remove the
            # unused API-path contracts before applying the binding's exact
            # unavailability policy. A GUI fallback result must describe only
            # the path responsible for delivery, never both alternatives.
            result.delivery_attempted = False
            del result.effect_contract_hashes[api_hash_start:]
            result.effect_results.append(f"[api] {outcome_reason}")
            if binding.on_unavailable == "halt":
                self._set_api_unavailable_refusal(
                    step,
                    result,
                    reason=outcome_reason,
                )
                return True
            return False

        # From here the request WAS attempted -- this step is API-tier and is
        # NEVER also GUI-written (the no-double-write contract).
        result.actuation = "api"

        if outcome_status is ActuationStatus.HALT:
            result.effect_results.append(f"[api] {outcome_reason}")
            halt_kind = getattr(outcome, "halt_kind", None)
            typed_halt = isinstance(halt_kind, ApiHaltKind)
            if not typed_halt:
                result.effect_results.append(
                    "[api] actuator returned an untyped HALT result; treating "
                    "the delivery as a protocol error that cannot resolve to "
                    "success"
                )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type=(
                    "ApiActuatorProtocolError"
                    if not typed_halt
                    else (
                        "ApiResponseRejected"
                        if halt_kind is ApiHaltKind.RESPONSE_REJECTED
                        else "ApiDeliveryUncertain"
                    )
                ),
                allow_contract_resolution=(
                    typed_halt and halt_kind is ApiHaltKind.DELIVERY_UNCERTAIN
                ),
            )

        # ACTUATED (2xx): confirm the write against the system of record with
        # the same EffectVerifier that gates a GUI write. A non-CONFIRMED
        # verdict HALTs. The GUI resolve/act (and screen postconditions) are
        # SKIPPED -- the record, not the screen, is the oracle for an API write.
        result.effect_results.append(f"[api] actuated {outcome_reason}")
        self._emit_control_overlay_phase(
            "verifying",
            current_step=overlay_current,
            total_steps=overlay_total,
        )
        try:
            error = self._verify_effects(
                step,
                before,
                result,
                effects=effects,
                verifier=effect_verifier,
            )
        except Exception as exc:  # noqa: BLE001 - deployment verifier boundary
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect verification raised after API delivery "
                f"({type(exc).__name__}); outcome requires reconciliation"
            )
            error = (
                f"System-of-record verification for API step '{step.id}' "
                f"({step.intent}) raised {type(exc).__name__} after delivery; "
                "the write may have landed — run aborted"
            )
        result.ok = error is None
        result.error = error
        return True

    def _verify_uncertain_api_delivery(
        self,
        step: Step,
        result: StepResult,
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        start_state: dict[str, Any],
        before: EffectState,
        effects: list["Effect"],
        effect_verifier: Any,
        cause_type: str,
        allow_contract_resolution: bool = True,
    ) -> bool:
        """Resolve one possibly dispatched API write without another actuation.

        A response loss, rejected response, or non-conforming actuator
        exception does not end evidence collection. The runtime checks the
        configured postcondition and the independent system-of-record effect.
        A response loss can resolve only when both contracts confirm. An
        explicit response outside the binding's accepted contract always
        remains halted, even when later evidence confirms a business effect.
        The API request is never retried and the GUI fallback never runs.
        """

        result.delivery_uncertainty = ActionDeliveryUncertainty(
            operation="api_request",
            native=False,
            observed_at=datetime.now(timezone.utc).isoformat(),
            cause_type=cause_type,
        )
        overlay_current, overlay_total = self._control_overlay_progress(
            workflow, step_index, None
        )
        self._emit_control_overlay_phase(
            "verifying",
            current_step=overlay_current,
            total_steps=overlay_total,
        )
        verification_errors: list[str] = []

        if step.expect:
            try:
                after_png = self.vision.wait_settled(self.backend)
                postconditions_ok, _last_frame, failed = self._check_postconditions(
                    step,
                    after_png,
                    bundle_dir,
                    start_state,
                    result,
                )
                result.postconditions_ok = postconditions_ok
                if not postconditions_ok:
                    verification_errors.append(
                        "postcondition did not confirm: "
                        + ("; ".join(failed) or "unknown postcondition")
                    )
            except Exception as exc:  # noqa: BLE001 - observation boundary
                result.postconditions_ok = False
                verification_errors.append(
                    f"postcondition observation failed ({type(exc).__name__})"
                )
        else:
            # An absent postcondition cannot resolve uncertain delivery.
            result.postconditions_ok = None
            verification_errors.append("no postcondition was declared")

        try:
            effect_error = self._verify_effects(
                step,
                before,
                result,
                effects=effects,
                verifier=effect_verifier,
            )
        except Exception as exc:  # noqa: BLE001 - deployment verifier boundary
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect verification raised after uncertain delivery "
                f"({type(exc).__name__}); outcome requires reconciliation"
            )
            effect_error = (
                f"independent effect verification failed ({type(exc).__name__})"
            )
        if effect_error is not None:
            verification_errors.append(effect_error)

        uncertainty = result.delivery_uncertainty
        assert uncertainty is not None
        uncertainty.verification_attempted = True
        uncertainty.postconditions_confirmed = result.postconditions_ok is True
        uncertainty.effects_confirmed = result.effect_verified is True
        uncertainty.resolved_by_contract = (
            allow_contract_resolution
            and uncertainty.postconditions_confirmed
            and uncertainty.effects_confirmed
            and not verification_errors
        )
        result.ok = uncertainty.resolved_by_contract
        if result.ok:
            result.error = None
        else:
            result.safety_halt = True
            result.failure_category = (
                "governed_refusal"
                if self.governed_authorization is not None
                else "safety_halt"
            )
            detail = "; ".join(verification_errors) or "verification confirmed"
            if allow_contract_resolution:
                result.error = (
                    f"API delivery for step '{step.id}' ({step.intent}) was "
                    "uncertain and was not retried; the complete postcondition "
                    "and independent effect contract did not confirm: "
                    f"{detail}"
                )
            else:
                result.error = (
                    f"API delivery for step '{step.id}' ({step.intent}) "
                    "received a response outside the binding's accepted "
                    "contract and was not retried; later verification evidence "
                    f"was collected but cannot override that rejection: {detail}"
                )
        return True

    @staticmethod
    def _set_api_unavailable_refusal(
        step: Step,
        result: StepResult,
        *,
        reason: str,
    ) -> None:
        """Record an exact pre-delivery API-only refusal.

        The caller invokes this only when no request was sent. The typed
        evidence distinguishes that state from an attempted or uncertain API
        delivery and prevents any GUI fallback.
        """

        binding = step.api_binding
        assert binding is not None
        detector_input = json.dumps(
            {
                "binding": binding.model_dump(mode="json"),
                "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        result.ok = False
        result.delivery_attempted = False
        result.safety_halt = True
        result.failure_category = "governed_refusal"
        result.effect_verified = False
        result.error = (
            f"API-only step '{step.id}' ({step.intent}) is unavailable before "
            "delivery; refusing GUI fallback — run aborted"
        )
        result.safety_refusal_evidence = SafetyRefusalEvidence(
            stage="api_admission",
            code="api_path_unavailable",
            detector_input_sha256=hashlib.sha256(detector_input).hexdigest(),
        )

    # -- system-of-record effect verification -----------------------------------

    def _resolve_effects(
        self, effects: list["Effect"], params: Mapping[str, RuntimeParamScalar]
    ) -> list["Effect"]:
        """Bind each effect's ``ValueExpr`` contract to THIS run's params (P0-3).

        Mirrors how an ``ApiBinding``'s ``{param}`` templates are filled at
        actuation time: the effect a PARAMETERIZED workflow verifies must
        describe the record IT WROTE THIS RUN, not the demonstration's. The
        reserved ``__run_id__`` param exposes the stable-per-run identity so an
        idempotency key can be bound per-run. A pure-literal (v1) effect is
        returned value-identical -- ``resolve`` is a no-op for it.
        """
        namespace = {
            **{name: runtime_param_text(value) for name, value in params.items()},
            "__run_id__": self._run_id,
        }
        run_id_sha256 = hashlib.sha256(self._run_id.encode("utf-8")).hexdigest()
        return [
            effect.resolve(
                namespace,
                opaque_param_sha256={"__run_id__": run_id_sha256},
            )
            for effect in effects
        ]

    @staticmethod
    def _capture_effect_pre_state(verifier: Any, effects: list["Effect"]) -> Any:
        """Capture the pre-action baseline for resolved effect contracts.

        Multi-candidate deployments select an adapter per resolved effect.  A
        normal verifier keeps the established single snapshot behavior.
        """
        capture_for_effects = getattr(verifier, "capture_pre_state_for_effects", None)
        if callable(capture_for_effects):
            return capture_for_effects(effects)
        if getattr(verifier, "_openadapt_requires_preflight", False):
            probe = _probe_adapter_connection(verifier)
            if not probe.ok:
                raise ValueError(
                    "the selected effect verifier failed its read-only connection "
                    f"preflight ({probe.substrate}: {probe.reason})"
                )
        return verifier.capture_pre_state()

    @staticmethod
    def _effect_pre_state_for(before: Any, effect: "Effect") -> Any:
        """Return one selected effect baseline, or a normal shared baseline."""
        selected = getattr(before, "for_effect", None)
        if callable(selected):
            return selected(effect).state
        return before

    @staticmethod
    def _effect_binding_for(
        verifier: Any, before: Any, effect: "Effect"
    ) -> tuple[Any, Any, Optional[Any], str]:
        """Return the exact adapter, state, tier, and identity bound to an effect."""

        selected = getattr(before, "for_effect", None)
        if callable(selected):
            binding = selected(effect)
            return (
                binding.verifier,
                binding.state,
                binding.tier,
                binding.verifier_identity,
            )
        return (
            verifier,
            before,
            verifier_effect_tier(verifier, effect),
            effect_verifier_identity(verifier),
        )

    @staticmethod
    def _candidate_binding_refusal(
        before: Any, effects: list["Effect"]
    ) -> Optional[str]:
        """Refuse a candidate whose identity or tier changed after selection."""

        selected = getattr(before, "for_effect", None)
        if not callable(selected):
            return None
        for effect in effects:
            binding = selected(effect)
            current_tier = verifier_effect_tier(binding.verifier, effect)
            current_identity = effect_verifier_identity(binding.verifier)
            if (
                current_tier != binding.tier
                or current_identity != binding.verifier_identity
            ):
                return (
                    "the selected effect verifier identity or evidence tier changed "
                    "after pre-action binding; refusing to use a different proof"
                )
        return None

    @staticmethod
    def _verify_current_effect(
        verifier: Any, effect: "Effect", current: EffectState
    ) -> Any:
        """Use an adapter's current-state path without inventing a new baseline."""

        candidate = effect.model_copy(deep=True)
        original = candidate.model_dump(mode="json")
        callback = getattr(verifier, "verify_current_state", None)
        if callable(callback):
            verdict = callback(candidate, current)
        else:
            from openadapt_flow.runtime.effects._common import judge_records

            # An effect whose CLAIM is a delta (``exact_new_set``) cannot be
            # judged against a synthesized empty baseline: every record in
            # scope would read as an addition, so the verdict would be
            # fabricated rather than proved. Mark the synthetic baseline
            # UNREACHABLE for those effects and let the judge return its own
            # structured INDETERMINATE refusal. Every pre-existing kind keeps
            # the readable empty baseline and is judged exactly as before.
            needs_baseline = bool(candidate.requires_baseline)
            detail: dict[str, Any] = {"current_state_readback": True}
            if needs_baseline:
                detail["baseline_unavailable_for_delta"] = True
            baseline = EffectState(
                substrate=current.substrate,
                reachable=not needs_baseline,
                records=[],
                detail=detail,
            )
            verdict = judge_records(
                candidate,
                baseline,
                current.records if current.reachable is True else None,
                substrate=current.substrate,
            )
        if candidate.model_dump(mode="json") != original:
            raise EffectContractMutationError(
                "effect verifier changed the resolved effect contract"
            )
        return verdict

    @classmethod
    def _required_effect_pre_state_unreadable(
        cls, verifier: Any, before: Any, effects: list["Effect"]
    ) -> bool:
        """Return true only when a selected effect needs, but lacks, a baseline.

        Different-path on-screen read-back proves persistence after input and
        deliberately has no readable pre-action state.  Delta, duplicate, and
        collateral checks still require their selected verifier baseline.
        """
        requirement = getattr(verifier, "requires_readable_pre_state_for", None)
        selected = getattr(before, "for_effect", None)
        missing_reachability = object()
        for effect in effects:
            if callable(selected):
                required = bool(selected(effect).requires_readable_pre_state)
            else:
                required = (
                    bool(requirement(effect))
                    if callable(requirement)
                    else bool(effect.requires_baseline or effect.forbid_collateral_loss)
                )
            state = cls._effect_pre_state_for(before, effect)
            # Legacy in-process verifiers may retain an opaque pre-state for
            # their own verify implementation. Their established verification
            # path decides whether it is usable. If a legacy state exposes
            # reachability, it must state exactly True; None, falsey values,
            # and truthy non-bools are not evidence that its baseline is
            # readable. Candidate deployments are stricter: their verifier
            # accepts only EffectState, so a missing reachability value cannot
            # bypass its pre-actuation guard.
            reachable = getattr(state, "reachable", missing_reachability)
            if (
                required
                and reachable is not missing_reachability
                and reachable is not True
            ):
                return True
        return False

    def _profile_effect_tier_refusal(
        self,
        workflow: Workflow,
        step: Step,
        actuation_path: Literal["gui", "api"],
        effects: list["Effect"],
        verifier: object,
    ) -> Optional[str]:
        """Recheck effect strength immediately before governed actuation."""

        authorization = self.governed_authorization
        if authorization is None or authorization.execution_profile is None:
            return None
        from openadapt_flow.execution_profiles import required_effect_tier
        from openadapt_flow.verification import VerificationTier

        minimum = (
            VerificationTier(authorization.minimum_effect_tier)
            if authorization.minimum_effect_tier is not None
            else required_effect_tier(workflow, authorization.execution_profile)
        )
        if minimum is None:
            return None
        requirements = authorization.effect_requirements(step.id, actuation_path)
        if requirements:
            from openadapt_flow.policy import effects_for_actuation

            declared = effects_for_actuation(
                step, "api" if actuation_path == "api" else "guarded_coordinate"
            )
            if len(requirements) != len(declared) or len(effects) != len(declared):
                return (
                    "Qualified effect requirements changed after admission — "
                    "refusing to actuate; run aborted"
                )
            for index, requirement in enumerate(requirements):
                if (
                    requirement.effect_index != index
                    or requirement.effect_contract_hash
                    != declared[index].contract_hash()
                ):
                    return (
                        "Qualified effect contract changed after admission — "
                        "refusing to actuate; run aborted"
                    )
        weak = []
        for index, effect in enumerate(effects):
            required = (
                VerificationTier(requirements[index].minimum_tier)
                if requirements
                else minimum
            )
            actual = verifier_effect_tier(verifier, effect)
            if actual is None or not actual.satisfies(required):
                weak.append("untyped" if actual is None else actual.name.lower())
        if not weak:
            return None
        observed = ", ".join(sorted(set(weak)))
        return (
            "Effect verifier strength changed after admission: "
            f"{observed} evidence cannot satisfy the required "
            "qualified effect tier — refusing to actuate; run aborted"
        )

    def _all_default_readback(self, effects: list["Effect"]) -> bool:
        """Whether EVERY declared effect is an auto-derived DIFFERENT-PATH
        on-screen read-back that may be verified out of the box with no
        connector (measured false-CONFIRM ~0). A same-surface read-back or any
        structured system-of-record effect makes this False — those still
        require a configured verifier (fail-safe)."""
        from openadapt_flow.runtime.effects.onscreen import is_default_readback_effect

        return bool(effects) and all(is_default_readback_effect(e) for e in effects)

    def _auto_readback_verifier(self) -> Any:
        """The backend-bound on-screen read-back verifier for the no-connector
        default path (built once, reused across steps)."""
        verifier = getattr(self, "_readback_verifier", None)
        if verifier is None:
            from openadapt_flow.runtime.effects.onscreen import (
                OnScreenReadbackVerifier,
            )

            verifier = OnScreenReadbackVerifier(backend=self.backend)
            self._readback_verifier = verifier
        return verifier

    def _verify_effects(
        self,
        step: Step,
        before: EffectState,
        result: StepResult,
        effects: Optional[list["Effect"]] = None,
        verifier: Any = None,
    ) -> Optional[str]:
        """Verify each declared Effect against the system of record; HALT on
        any non-CONFIRMED verdict.

        Runs AFTER the action and its screen postconditions (for a GUI write),
        or immediately after an API actuation. For each effect the configured
        verifier rules CONFIRMED / REFUTED / INDETERMINATE against the REAL
        record (an API/DB read, never the screen). A CONFIRMED effect proceeds;
        a non-CONFIRMED effect halts, except that an IRREVERSIBLE effect is
        first routed through ``reconcile_or_escalate`` -- a duplicate the
        configured compensator can undo is RECONCILED (proceed), everything else
        ESCALATEs (halt). Every verdict is recorded on ``result.effect_results``
        for the audit trail. Makes ZERO model calls (the $0 runtime guarantee).

        Args:
            step: The step being verified (for the audit/error text).
            before: The pre-action snapshot of the system of record.
            result: The step result to record verdicts on.
            effects: The effects to verify; defaults to ``step.effects`` (the
                GUI path). The API tier passes ``api_binding.effects`` only.

        Returns an error string (HALT) or None (all effects confirmed or
        reconciled).
        """
        # ``verifier`` is the configured one by default, but the caller may pass
        # the auto-wired backend-bound read-back verifier (the no-connector
        # default path) for a step whose effects are different-path read-backs.
        if verifier is None:
            verifier = self.effect_verifier
        assert verifier is not None  # guaranteed by the caller
        if effects is None:
            effects = step.effects
        for effect in effects:
            # A compiler-mined PLACEHOLDER effect (binding not derivable from
            # the demonstration — see compiler.effect_mining) carries a
            # sentinel selector, NOT a real one. Never verify it against the
            # system of record: that would either falsely refute or, worse,
            # silently pass. Fail-safe HALT until an operator completes the
            # binding and clears the flag — same posture as the identity gate.
            if effect.needs_operator_confirmation:
                result.effect_verified = False
                result.effect_results.append(
                    f"[unbound] {effect.kind.value}: NEEDS OPERATOR "
                    "CONFIRMATION — placeholder effect not bound to the system "
                    "of record (compiler could not derive it); HALT"
                )
                return (
                    f"System-of-record effect for step '{step.id}' "
                    f"({step.intent}) is an unconfirmed PLACEHOLDER — the "
                    "compiler flagged its binding as app-specific and it must "
                    "be completed by an operator before this consequential "
                    "write can run — run aborted"
                )
            try:
                verdict = verify_effect_without_mutation(verifier, effect, before)
                selected_pre_state = getattr(before, "for_effect", None)
                if callable(selected_pre_state):
                    # Candidate selection and its evidence strength are pinned
                    # before input. Do not recompute the tier after delivery:
                    # a stateful plugin must not change the receipt contract.
                    binding = selected_pre_state(effect)
                    tier = binding.tier
                    identity = binding.verifier_identity
                    if (
                        verifier_effect_tier(binding.verifier, effect) != tier
                        or effect_verifier_identity(binding.verifier) != identity
                    ):
                        result.effect_verified = False
                        result.safety_halt = True
                        result.failure_category = "governed_refusal"
                        result.effect_results.append(
                            "selected effect verifier identity or evidence tier "
                            "changed after input; HALT"
                        )
                        return (
                            f"Effect verifier binding changed after input for step "
                            f"'{step.id}' — refusing evidence from a different "
                            "adapter or tier; reconciliation is required"
                        )
                else:
                    tier = verifier_effect_tier(verifier, effect)
                    identity = effect_verifier_identity(verifier)
            except EffectContractMutationError:
                result.effect_verified = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.effect_results.append(
                    "effect verifier changed the resolved effect contract; HALT"
                )
                return (
                    f"Effect verifier changed the resolved contract for step "
                    f"'{step.id}' — refusing mutable verification evidence; "
                    "run aborted"
                )
            if verdict.confirmed:
                result.effect_evidence.append(
                    EffectVerificationEvidence(
                        effect_contract_hash=effect.contract_hash(),
                        substrate=verdict.substrate,
                        verifier_identity=identity,
                        verification_tier=(int(tier) if tier is not None else None),
                        initial_verdict=verdict.verdict.value,
                        final_verdict=verdict.verdict.value,
                        observed_effect=verdict.observed_effect,
                    )
                )
                result.effect_results.append(
                    f"[{verdict.substrate}] {effect.kind.value}: CONFIRMED"
                    + (f" — {verdict.reason}" if verdict.reason else "")
                )
                continue

            # Non-CONFIRMED: never proceed on an unverified/contradicted write.
            # An irreversible effect gets one reconcile-or-escalate pass (a
            # compensable duplicate is fixable; missing / partial / collateral
            # loss / INDETERMINATE always escalate). A reversible effect just
            # halts. Either way the run stops -- this is the same
            # refuse-rather-than-guess posture as the identity gate.
            if effect.risk == "irreversible":
                comp = reconcile_or_escalate(
                    effect,
                    verdict,
                    verifier=verifier,
                    before=before,
                    compensator=self.effect_compensator,
                )
                if comp.proceed:
                    final_verdict = comp.final_verdict or verdict
                    result.effect_evidence.append(
                        EffectVerificationEvidence(
                            effect_contract_hash=effect.contract_hash(),
                            substrate=final_verdict.substrate,
                            verifier_identity=identity,
                            verification_tier=(int(tier) if tier is not None else None),
                            initial_verdict=verdict.verdict.value,
                            final_verdict=final_verdict.verdict.value,
                            observed_effect=final_verdict.observed_effect,
                            reconciliation_completed=True,
                            reconciliation_actions=comp.actions_taken,
                        )
                    )
                    result.effect_results.append(
                        f"[{verdict.substrate}] {effect.kind.value}: "
                        f"{verdict.verdict.value.upper()} → RECONCILED "
                        f"({comp.actions_taken} action(s)) — {comp.reason}"
                    )
                    continue
                result.effect_verified = False
                final_verdict = comp.final_verdict or verdict
                result.effect_evidence.append(
                    EffectVerificationEvidence(
                        effect_contract_hash=effect.contract_hash(),
                        substrate=final_verdict.substrate,
                        verifier_identity=identity,
                        verification_tier=(int(tier) if tier is not None else None),
                        initial_verdict=verdict.verdict.value,
                        final_verdict=final_verdict.verdict.value,
                        observed_effect=final_verdict.observed_effect,
                        reconciliation_actions=comp.actions_taken,
                    )
                )
                result.effect_results.append(
                    f"[{verdict.substrate}] {effect.kind.value}: "
                    f"{verdict.verdict.value.upper()} → "
                    f"{comp.outcome.value.upper()} — {comp.reason}"
                )
                return (
                    f"System-of-record effect verification HALTED step "
                    f"'{step.id}' ({step.intent}): {effect.kind.value} "
                    f"{verdict.verdict.value} against the {verdict.substrate} "
                    f"system of record and could not be reconciled "
                    f"({comp.outcome.value}) — "
                    f"{comp.escalation or comp.reason} — run aborted"
                )

            result.effect_verified = False
            result.effect_evidence.append(
                EffectVerificationEvidence(
                    effect_contract_hash=effect.contract_hash(),
                    substrate=verdict.substrate,
                    verifier_identity=identity,
                    verification_tier=(int(tier) if tier is not None else None),
                    initial_verdict=verdict.verdict.value,
                    final_verdict=verdict.verdict.value,
                    observed_effect=verdict.observed_effect,
                )
            )
            result.effect_results.append(
                f"[{verdict.substrate}] {effect.kind.value}: "
                f"{verdict.verdict.value.upper()} — {verdict.reason}"
            )
            return (
                f"System-of-record effect verification HALTED step "
                f"'{step.id}' ({step.intent}): {effect.kind.value} "
                f"{verdict.verdict.value} against the {verdict.substrate} "
                f"system of record (the screen indicated completion but the "
                f"declared outcome is contradicted or unverifiable) — "
                f"{verdict.reason} — run aborted"
            )

        result.effect_verified = True
        return None

    def _identity_gate_error(
        self,
        step: Step,
        resolution: Resolution,
        frame_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Path,
        result: StepResult,
        *,
        evidence_run_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Run the target-identity contract on one exact observed frame."""
        qualification = workflow.qualification
        qualified_policy = (
            qualification.identity_policies.get(step.id)
            if qualification is not None
            else None
        )
        if (
            step.action
            not in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
                ActionKind.TYPE,
                ActionKind.SELECT_OPTION,
                ActionKind.KEY,
                ActionKind.HOTKEY,
            )
            or step.anchor is None
            or (
                qualified_policy is None
                and not (
                    step.anchor.context_text
                    or step.anchor.structured_identity
                    or step.anchor.identity_template
                    or step.anchor.identifier_crop
                )
            )
        ):
            return None

        check = self._verify_identity(
            step, resolution, frame_png, params, workflow, bundle_dir
        )
        if (
            check.status in {"verified", "mismatch"}
            and check.mode == "pixel"
            and workflow.qualification is not None
        ):
            try:
                if evidence_run_dir is None:
                    raise OSError("pixel evidence has no local run directory")
                recorded_png, live_png = self._identifier_crops(
                    step.anchor,
                    resolution,
                    frame_png,
                    bundle_dir,
                    workflow=workflow,
                )
                if recorded_png is None or live_png is None:
                    raise OSError("pixel evidence crops are unavailable")
                check.pixel_evidence = self._retain_pixel_identity_evidence(
                    workflow=workflow,
                    anchor=step.anchor,
                    recorded_png=recorded_png,
                    live_png=live_png,
                    run_dir=evidence_run_dir,
                )
            except OSError:
                check = IdentityCheck(
                    status="unreadable",
                    mode="pixel",
                    expected="recorded identifier crop",
                    observed="exact local pixel evidence could not be retained",
                )
        result.identity = check
        governed = (
            self.governed_authorization is not None
            and self.governed_authorization.requires_verified_identity(step.id)
        )
        signal_quorum = check.mode == "signal_quorum"
        if signal_quorum and check.status == "mismatch":
            conflicts = [
                f"{item.signal}/{item.source}"
                for item in check.signal_evidence
                if item.verdict == "conflict"
            ]
            error = (
                f"Identity signal quorum conflicted for step '{step.id}' "
                f"({step.intent}): "
                + ", ".join(conflicts)
                + " — refusing to act; run aborted"
            )
        elif signal_quorum and check.status != "verified":
            error = (
                f"Identity signal quorum was not reached for step '{step.id}' "
                f"({step.intent}): {check.quorum_verified or 0}/"
                f"{check.quorum_required or 0} independent signals verified "
                "on the fresh frame — refusing to act; run aborted"
            )
        elif check.status == "mismatch":
            error = (
                f"Identity check failed for step '{step.id}' "
                f"({step.intent}): a target was found positionally "
                f"(rung '{resolution.rung}', confidence "
                f"{resolution.confidence:.2f}) but its surrounding "
                f"text does not match the recorded target's — "
                f"expected {check.expected!r}, observed "
                f"{check.observed!r}"
                + (
                    f" (parameter '{check.param}')"
                    if check.param
                    else f" (coverage {check.coverage:.2f})"
                )
                + " — refusing to act; run aborted"
            )
        elif check.status in ("unreadable", "abstain") and (
            step.risk == "irreversible" or governed
        ):
            reason = (
                "rests on a glyph-confusable identifier OCR may have "
                "collapsed (a same-name/same-DOB homonym cannot be ruled out)"
                if check.status == "abstain"
                else "could not be read from the live screen "
                "(context band OCR found no usable text)"
            )
            scope = (
                "requires verified identity under the governed run policy"
                if governed
                else "is irreversible"
            )
            error = (
                f"Step '{step.id}' ({step.intent}) {scope} and its target "
                f"identity {reason} — needs human confirmation; refusing to act"
            )
        else:
            error = None
        if error is not None and (governed or signal_quorum):
            result.safety_halt = True
        return error

    @staticmethod
    def _retain_pixel_identity_evidence(
        *,
        workflow: Workflow,
        anchor: Anchor,
        recorded_png: bytes,
        live_png: bytes,
        run_dir: Path,
    ) -> PixelIdentityEvidence:
        """Retain exact identity crops locally and return only PHI-free bindings."""

        if anchor.identifier_crop is None:
            raise OSError("pixel evidence has no recorded anchor reference")
        recorded_sha256 = hashlib.sha256(recorded_png).hexdigest()
        live_sha256 = hashlib.sha256(live_png).hexdigest()
        if (
            workflow.manifest is None
            or workflow.manifest.file_hashes.get(anchor.identifier_crop)
            != recorded_sha256
        ):
            raise OSError("recorded identity crop does not match the bundle manifest")

        root = Path(run_dir).resolve()
        private_dir = root / "private"
        inventory_dir = private_dir / "identity-crops"
        try:
            if private_dir.is_symlink() or inventory_dir.is_symlink():
                raise OSError("pixel evidence inventory path is a symlink")
            inventory_dir.mkdir(parents=True, exist_ok=True)
            if (
                private_dir.is_symlink()
                or inventory_dir.is_symlink()
                or not inventory_dir.resolve().is_relative_to(root)
            ):
                raise OSError("pixel evidence inventory leaves the run root")
            for digest, payload in (
                (recorded_sha256, recorded_png),
                (live_sha256, live_png),
            ):
                path = inventory_dir / f"{digest}.png"
                if path.is_symlink():
                    raise OSError("pixel evidence crop path is a symlink")
                existing = path.read_bytes() if path.exists() else None
                if existing is not None and existing != payload:
                    raise OSError("pixel evidence digest path has other bytes")
                path.write_bytes(payload)
        except OSError:
            raise

        return PixelIdentityEvidence(
            recorded_crop_sha256=recorded_sha256,
            live_crop_sha256=live_sha256,
            recorded_crop_inventory_ref=(
                f"private/identity-crops/{recorded_sha256}.png"
            ),
            live_crop_inventory_ref=f"private/identity-crops/{live_sha256}.png",
            evaluator_contract_sha256=(
                identity_mod.pixel_identity_evaluator_contract_sha256()
            ),
        )

    def _retain_visual_resolution_evidence(
        self,
        *,
        workflow: Workflow,
        anchor: Anchor,
        resolution: Resolution,
        matched_region: Region,
        frame_png: bytes,
        bundle_dir: Path,
        run_dir: Path,
        allow_target_ocr: bool = True,
    ) -> Resolution:
        """Bind a visual resolution to exact, locally retained resolver inputs."""

        if self.qualification_fault_driver is None:
            return resolution
        if resolution.rung == "structural":
            return resolution
        if resolution.rung == "grounder":
            # A model response is not independently reproducible from the
            # retained frame.  Qualification rejects it as exact fault proof.
            return resolution
        template_png = self._asset_bytes(
            bundle_dir,
            anchor.template,
            workflow=workflow,
        )
        if template_png is None:
            raise OSError("visual resolution template is unavailable")
        frame_sha256 = hashlib.sha256(frame_png).hexdigest()
        template_sha256 = hashlib.sha256(template_png).hexdigest()
        if (
            workflow.manifest is None
            or workflow.manifest.file_hashes.get(anchor.template) != template_sha256
        ):
            raise OSError("visual resolution template does not match the manifest")

        root = Path(run_dir).resolve()
        inventory_dir = root / "private" / "resolution-inputs"
        if inventory_dir.is_symlink():
            raise OSError("visual resolution inventory path is a symlink")
        inventory_dir.mkdir(parents=True, exist_ok=True)
        if inventory_dir.is_symlink() or not inventory_dir.resolve().is_relative_to(
            root
        ):
            raise OSError("visual resolution inventory leaves the run root")
        for digest, payload in (
            (frame_sha256, frame_png),
            (template_sha256, template_png),
        ):
            path = inventory_dir / f"{digest}.png"
            if path.is_symlink():
                raise OSError("visual resolution input path is a symlink")
            existing = path.read_bytes() if path.exists() else None
            if existing is not None and existing != payload:
                raise OSError("visual resolution digest path has other bytes")
            path.write_bytes(payload)

        return resolution.model_copy(
            update={
                "visual_evidence": VisualResolutionEvidence(
                    frame_sha256=frame_sha256,
                    frame_inventory_ref=(
                        f"private/resolution-inputs/{frame_sha256}.png"
                    ),
                    template_sha256=template_sha256,
                    template_inventory_ref=(
                        f"private/resolution-inputs/{template_sha256}.png"
                    ),
                    evaluator_contract_sha256=(
                        visual_resolution_evaluator_contract_sha256()
                    ),
                    anchor_contract_sha256=(
                        visual_resolution_anchor_contract_sha256(
                            anchor,
                            template_sha256=template_sha256,
                            allow_target_ocr=allow_target_ocr,
                        )
                    ),
                    matched_region=matched_region,
                    allow_target_ocr=allow_target_ocr,
                )
            }
        )

    def _step_is_consequential(self, step: Step, workflow: Workflow) -> bool:
        authorization = self.governed_authorization
        return is_consequential(step, workflow) or (
            authorization is not None
            and authorization.requires_verified_identity(step.id)
        )

    def _step_requires_remote_identity_binding(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether an external opaque-remote step needs an identity lease.

        ``identity_armed`` says that the remote action must remain bound to the
        identity that was verified on the exact live frame. It does not change
        the business-risk classification of the action on browser or native
        surfaces.
        """

        return (
            step.identity_armed is True
            and workflow.surface in ("rdp", "citrix")
            and workflow.execution_mode == "external"
            and isinstance(self.backend, RemoteActuationBackend)
        )

    def _step_requires_remote_guarded_actuation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether a remote input edge must use the fresh-frame guard path."""

        return isinstance(self.backend, RemoteActuationBackend) and (
            self._step_is_consequential(step, workflow)
            or self._step_requires_remote_identity_binding(step, workflow)
            or self.qualification_fault_driver is not None
        )

    def _selection_contract_error(
        self, step: Step, workflow: Workflow
    ) -> Optional[str]:
        """Return a fail-closed error for an unusable composite selection."""

        if step.action is not ActionKind.SELECT_OPTION:
            return None
        if step.selection_region is None or step.selection_commit_key is None:
            return (
                f"Step '{step.id}' ({step.intent}) has an incomplete exact "
                "option-selection contract; refusing input"
            )
        if (
            workflow.surface not in ("rdp", "citrix")
            or workflow.execution_mode != "external"
            or not isinstance(self.backend, RemoteActuationBackend)
        ):
            return (
                f"Step '{step.id}' ({step.intent}) carries a remote "
                "option-selection contract outside its qualified external "
                "opaque remote surface; refusing input"
            )
        if not isinstance(self.backend, SelectOptionBackend):
            return (
                f"Step '{step.id}' ({step.intent}) requires atomic option "
                "selection, but this backend cannot bind type-ahead and "
                "commit under one input lease"
            )
        return None

    def _step_has_identity_contract(self, step: Step, workflow: Workflow) -> bool:
        """Whether this step carries identity evidence or an explicit mandate."""
        authorization = self.governed_authorization
        if authorization is not None and authorization.requires_verified_identity(
            step.id
        ):
            return True
        qualification = workflow.qualification
        if qualification is not None and step.id in qualification.identity_policies:
            return True
        anchor = step.anchor
        return anchor is not None and bool(
            anchor.context_text
            or anchor.structured_identity
            or anchor.identity_template
            or anchor.identifier_crop
        )

    def _step_needs_consequential_revalidation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether delivery needs a second, immediately fresh observation.

        Opaque remote surfaces always use the two-phase lease for a
        consequential action. Browser/native surfaces use the same pre-delivery
        refresh when identity is part of the action contract.
        """
        if (
            step.action is ActionKind.SELECT_OPTION
            and workflow.surface in ("rdp", "citrix")
            and workflow.execution_mode == "external"
            and isinstance(self.backend, RemoteActuationBackend)
        ):
            # A selection is a composite focus -> typeahead -> commit gesture.
            # It always needs an exact fresh-frame lease before the focus click
            # and another after focus before text+commit, independent of a
            # generic risk classifier's opinion of the TYPE step.
            return True
        if (
            self.qualification_fault_driver is not None
            and step.action
            in {
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
            }
            and isinstance(self.backend, RemoteActuationBackend)
        ):
            # Exact fault-prefix proof includes read-only prior actions. Arm a
            # fresh remote lease for those actions only during the fault run,
            # so ordinary demo replay keeps its reversible fast path.
            return True
        if self._step_requires_remote_guarded_actuation(step, workflow):
            return True
        return self._step_is_consequential(
            step, workflow
        ) and self._step_has_identity_contract(step, workflow)

    def _requires_atomic_identity_pointer(self, step: Step, workflow: Workflow) -> bool:
        """Whether a pointer edge may not cross an unleased identity boundary.

        An anchored TYPE has a focusing click before keyboard delivery.  That
        first input edge needs the same target/record lease as a consequential
        CLICK; otherwise a visually identical replacement can receive its
        handler before the later keyboard lease notices the change.
        """

        return (
            step.action
            in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
                ActionKind.TYPE,
                ActionKind.SELECT_OPTION,
            )
            and (
                self._step_is_consequential(step, workflow)
                or self._step_requires_remote_identity_binding(step, workflow)
            )
            and self._step_has_identity_contract(step, workflow)
        )

    def _requires_atomic_identity_keyboard(
        self, step: Step, workflow: Workflow
    ) -> bool:
        """Whether keyboard delivery must stay bound to the verified focus."""

        return (
            step.action
            in (
                ActionKind.KEY,
                ActionKind.HOTKEY,
                ActionKind.TYPE,
                ActionKind.SELECT_OPTION,
            )
            and (
                self._step_is_consequential(step, workflow)
                or self._step_requires_remote_identity_binding(step, workflow)
            )
            and self._step_has_identity_contract(step, workflow)
        )

    def _typed_remote_receipt_required(self) -> bool:
        """Whether a remote pointer edge must carry a typed delivery receipt.

        :class:`RemoteActuationBackend` already owns the exact-frame contract:
        after ``acquire_actuation_frame`` the backend captures once more under
        its own input lock and refuses *before the first input edge* when the
        window/session, dimensions, readiness, or exact frame content changed.
        :class:`GuardedRemotePointerActionBackend` adds an explicit
        expected-frame hash plus a typed, target-bound receipt on top of that
        contract; its absence is therefore missing *evidence*, not a missing
        safety property.

        A governed (Standard/Regulated) run and an exact qualification-fault
        proof both consume that receipt as retained evidence, so they must
        refuse before delivery rather than actuate without it. Ordinary replay
        keeps the documented lease path — refusing there over-halts every
        conforming opaque remote backend, including a pixel-only no-DOM canvas
        that can only expose the lease. Such a result stays unlabeled, which
        the outcome classifier already maps to ``COMPLETED_UNVERIFIED`` instead
        of a production-eligible success.
        """

        return (
            self.governed_authorization is not None
            or self.qualification_fault_driver is not None
        )

    def _cancel_guarded_coordinate(self) -> None:
        """Clean an unconsumed local coordinate lease, if one was armed."""

        if isinstance(self.backend, GuardedCoordinateActionBackend):
            self.backend.cancel_guarded_coordinate()

    def _cancel_guarded_keyboard(self) -> None:
        """Clean an unconsumed local focused-element lease, if one was armed."""

        if isinstance(self.backend, GuardedKeyboardActionBackend):
            self.backend.cancel_guarded_keyboard()
        if isinstance(self.backend, FocusedElementActuationLeaseBackend):
            self.backend.cancel_focused_element_lease()

    def _local_atomic_capability_error(
        self,
        step: Step,
        resolution: Optional[Resolution],
        workflow: Workflow,
        *,
        arm_keyboard: bool,
    ) -> Optional[str]:
        """Refuse before input when a local identity lease cannot be enforced.

        Consequential TYPE has two input edges: its optional focusing click and
        the subsequent keyboard delivery.  The first revalidation must prove
        both local seams are available before it focuses anything.  The second
        revalidation (``arm_keyboard=True``) only needs the keyboard seam.
        Structural focusing actions already carry a native target fingerprint;
        visual/coordinate focusing actions require the coordinate guard.
        """

        if isinstance(self.backend, RemoteActuationBackend):
            return None

        requires_keyboard = self._requires_atomic_identity_keyboard(step, workflow)
        if requires_keyboard and not isinstance(
            self.backend, GuardedKeyboardActionBackend
        ):
            return (
                f"Step '{step.id}' ({step.intent}) is a consequential "
                "identity-gated keyboard action, but this backend cannot bind "
                "the verified focus and execution context to the same input "
                "operation — refusing unguarded keyboard delivery; run aborted"
            )

        requires_pointer = self._requires_atomic_identity_pointer(step, workflow)
        pointer_edge_pending = step.action in (
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
            ActionKind.RIGHT_CLICK,
            ActionKind.DRAG,
        ) or (
            step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
            and not arm_keyboard
        )
        structural_pointer = bool(
            step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and resolution is not None
            and resolution.rung == "structural"
            and resolution.structural_handle is not None
            and resolution.structural_handle.target_fingerprint is not None
            and step.anchor is not None
            and step.anchor.structural is not None
            and callable(getattr(self.backend, "act_structural", None))
        )
        if (
            requires_pointer
            and pointer_edge_pending
            and resolution is not None
            and not structural_pointer
            and not isinstance(self.backend, GuardedCoordinateActionBackend)
        ):
            return (
                f"Step '{step.id}' ({step.intent}) is a consequential "
                "identity-gated pointer action, but this backend cannot bind "
                "target and context verification to the same actuation operation "
                "— refusing raw coordinate delivery; run aborted"
            )
        if (
            requires_pointer
            and step.action is ActionKind.DRAG
            and not isinstance(self.backend, RemoteActuationBackend)
            and not isinstance(self.backend, GuardedDragActionBackend)
        ):
            return (
                f"Step '{step.id}' ({step.intent}) is a consequential drag, "
                "but this backend cannot bind both endpoints to the fresh "
                "identity-verified frame — refusing input; run aborted"
            )
        return None

    def _revalidate_consequential_actuation(
        self,
        step: Step,
        resolution: Optional[Resolution],
        matched_region: Optional[Region],
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Path,
        result: StepResult,
        *,
        run_dir: Path,
        arm_keyboard: bool = False,
    ) -> tuple[
        Optional[Resolution],
        Optional[Region],
        bytes,
        Optional[str],
    ]:
        """Freshly re-resolve and identity-check a consequential GUI step.

        Browser/native backends provide a fresh screenshot immediately before
        delivery. ``RemoteActuationBackend`` additionally arms a one-shot
        exact-frame lease and refuses if pixels change before the first input
        edge. Reversible actions retain the ordinary replay path.
        """
        if not self._step_needs_consequential_revalidation(step, workflow):
            return resolution, matched_region, before_png, None

        capability_error = self._local_atomic_capability_error(
            step,
            resolution,
            workflow,
            arm_keyboard=arm_keyboard,
        )
        if capability_error is not None:
            result.safety_halt = True
            result.failure_category = "safety_halt"
            return resolution, matched_region, before_png, capability_error

        # The ordinary resolution pass may have armed a one-shot structural
        # handle. Consequential revalidation deliberately replaces that
        # observation with a fresh resolve immediately before delivery. Drop
        # the stale handle first: on browser backends, binding a second token
        # mutates the target node, which correctly invalidates the first
        # MutationObserver; if the old observer remains live, its cleanup
        # mutation can in turn invalidate the new guard and falsely refuse an
        # unchanged target.
        cancel_stale_structural = getattr(
            self.backend,
            "cancel_pending_structural_guards",
            None,
        )
        if callable(cancel_stale_structural):
            cancel_stale_structural()

        guarded_keyboard_backend = (
            arm_keyboard
            and self._requires_atomic_identity_keyboard(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )
        focused_element_backend = (
            arm_keyboard
            and (
                self._step_is_consequential(step, workflow)
                or self._step_requires_remote_identity_binding(step, workflow)
            )
            and isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, FocusedElementActuationLeaseBackend)
        )
        guarded_type_pointer_backend = (
            step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
            and self._requires_atomic_identity_pointer(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedCoordinateActionBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )
        guarded_click_pointer_backend = (
            step.action
            in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
                ActionKind.DRAG,
            )
            and self._requires_atomic_identity_pointer(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedCoordinateActionBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )
        prepared_pointer: Optional[Point] = None
        pointer_edge_pending = step.action in (
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
            ActionKind.RIGHT_CLICK,
            ActionKind.DRAG,
        ) or (
            step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
            and resolution is not None
            and not arm_keyboard
        )
        if (
            pointer_edge_pending
            and resolution is not None
            and isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, PreparedPointerActuationBackend)
        ):
            prepared_pointer = resolution.point
            try:
                self.backend.prepare_pointer_actuation(*prepared_pointer)
            except Exception as exc:  # noqa: BLE001 - backend boundary must halt
                if self.governed_authorization is not None:
                    result.safety_halt = True
                detail = _scrub_phi(str(exc)) or type(exc).__name__
                return (
                    resolution,
                    matched_region,
                    before_png,
                    "Pointer preflight HALTED before final target resolution for "
                    f"step '{step.id}' ({step.intent}): {detail}",
                )
        try:
            fresh_png = (
                self.backend.acquire_actuation_frame()
                if isinstance(self.backend, RemoteActuationBackend)
                else (
                    cast(
                        GuardedKeyboardActionBackend, self.backend
                    ).guarded_keyboard_frame()
                    if (
                        guarded_keyboard_backend
                        or guarded_type_pointer_backend
                        or guarded_click_pointer_backend
                    )
                    else self.backend.screenshot()
                )
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary must halt
            if self.governed_authorization is not None:
                result.safety_halt = True
            detail = _scrub_phi(str(exc)) or type(exc).__name__
            return (
                resolution,
                matched_region,
                before_png,
                "Actuation preflight HALTED before input for step "
                f"'{step.id}' ({step.intent}): {detail}",
            )

        try:
            fresh_resolution, fresh_region, error = self._resolve_step(
                step,
                fresh_png,
                bundle_dir,
                workflow,
                # Opening an opaque select control can repeat its current value
                # in the option popup. That text is valid target evidence before
                # the focusing click, but it is no longer unique afterward. The
                # second phase therefore requires the retained template or
                # independent relational landmarks.
                allow_target_ocr=not (
                    arm_keyboard and step.action is ActionKind.SELECT_OPTION
                ),
            )
        except (OcrResolutionRefused, StructuralResolutionRefused) as exc:
            result.safety_halt = True
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="actuation_revalidation",
                code="actuation_observation_changed",
                detector_input_sha256=sha256_bytes(fresh_png),
            )
            return (
                None,
                None,
                fresh_png,
                "Actuation preflight HALTED because the fresh target was "
                f"ambiguous for step '{step.id}' ({step.intent}): {exc}",
            )
        if error is not None:
            if self.governed_authorization is not None:
                result.safety_halt = True
            return fresh_resolution, fresh_region, fresh_png, error
        if (
            prepared_pointer is not None
            and fresh_resolution is not None
            and fresh_resolution.point != prepared_pointer
        ):
            result.safety_halt = True
            result.failure_category = "safety_halt"
            return (
                fresh_resolution,
                fresh_region,
                fresh_png,
                f"Step '{step.id}' ({step.intent}) resolved to "
                f"{fresh_resolution.point!r} after remote pointer hover, not "
                f"the prepared point {prepared_pointer!r}; refusing a click "
                "whose target moved before actuation",
            )
        identity_required = (
            self.governed_authorization is not None
            and self.governed_authorization.requires_verified_identity(step.id)
        ) or (
            workflow.qualification is not None
            and step.id in workflow.qualification.identity_policies
        )
        if fresh_resolution is None and identity_required:
            error = (
                f"Step '{step.id}' ({step.intent}) requires verified identity "
                "but no exact target observation was available immediately "
                "before actuation — refusing to act; run aborted"
            )
        elif fresh_resolution is not None:
            guarded_coordinate = (
                self._requires_atomic_identity_pointer(step, workflow)
                and not (
                    arm_keyboard
                    and step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
                )
                # CLICK/DOUBLE_CLICK consume the native DOM/UIA handle when
                # structural resolution wins. RIGHT_CLICK has no native
                # structural actuator, so bind its resolved point to the
                # identity-verified target instead. DRAG is armed only after
                # its independently resolved destination is known below.
                and step.action is not ActionKind.DRAG
                and (
                    fresh_resolution.rung != "structural"
                    or step.action is ActionKind.RIGHT_CLICK
                )
                and not isinstance(self.backend, RemoteActuationBackend)
                and isinstance(self.backend, GuardedCoordinateActionBackend)
            )
            if guarded_coordinate:
                coordinate_backend = cast(GuardedCoordinateActionBackend, self.backend)
                if step.action is ActionKind.RIGHT_CLICK:
                    cancel_structural = getattr(
                        self.backend,
                        "cancel_pending_structural_guards",
                        None,
                    )
                    if callable(cancel_structural):
                        cancel_structural()
                editable_arm = getattr(
                    coordinate_backend,
                    "arm_guarded_editable_coordinate",
                    None,
                )
                if step.action in (
                    ActionKind.TYPE,
                    ActionKind.SELECT_OPTION,
                ) and callable(editable_arm):
                    editable_arm(*fresh_resolution.point)
                else:
                    coordinate_backend.arm_guarded_coordinate(*fresh_resolution.point)
            guarded_keyboard = guarded_keyboard_backend
            if guarded_keyboard:
                cast(GuardedKeyboardActionBackend, self.backend).arm_guarded_keyboard(
                    *fresh_resolution.point
                )
            if focused_element_backend:
                cast(
                    FocusedElementActuationLeaseBackend,
                    self.backend,
                ).arm_focused_element_lease(*fresh_resolution.point)
            try:
                error = self._identity_gate_error(
                    step,
                    fresh_resolution,
                    fresh_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    evidence_run_dir=run_dir,
                )
                if error is not None and result.identity is not None:
                    code: Literal["identity_conflict", "identity_unverifiable"] = (
                        "identity_conflict"
                        if result.identity.status == "mismatch"
                        else "identity_unverifiable"
                    )
                    result.safety_refusal_evidence = SafetyRefusalEvidence(
                        stage="identity_verification",
                        code=code,
                        detector_input_sha256=sha256_bytes(fresh_png),
                    )
            except Exception:
                if guarded_coordinate:
                    self._cancel_guarded_coordinate()
                if guarded_keyboard:
                    self._cancel_guarded_keyboard()
                if focused_element_backend:
                    self._cancel_guarded_keyboard()
                raise
            if error is not None and guarded_coordinate:
                self._cancel_guarded_coordinate()
            if error is not None and guarded_keyboard:
                self._cancel_guarded_keyboard()
            if error is not None and focused_element_backend:
                self._cancel_guarded_keyboard()
            if error is None and isinstance(self.backend, RemoteFrameContractBackend):
                protected: list[Region] = list(
                    fresh_resolution.resolution_evidence_regions
                    if fresh_resolution is not None
                    else ()
                )
                if fresh_region is not None:
                    protected.append(fresh_region)
                elif fresh_resolution is not None:
                    x, y = fresh_resolution.point
                    protected.append((x - 1, y - 1, 3, 3))
                protected.extend(
                    pc.region for pc in step.expect if pc.region is not None
                )
                if workflow.qualification is not None:
                    policy = workflow.qualification.identity_policies.get(step.id)
                    if policy is not None:
                        protected.extend(
                            s.region for s in policy.signals if s.region is not None
                        )
                try:
                    self.backend.arm_remote_frame_contract(
                        protected_regions=tuple(protected)
                    )
                except Exception as exc:
                    return (
                        fresh_resolution,
                        fresh_region,
                        fresh_png,
                        (
                            "Actuation preflight HALTED because the remote frame mask "
                            f"overlaps protected evidence: {type(exc).__name__}"
                        ),
                    )
        # Retain the exact observation that authorizes the next input edge.
        # Composite TYPE/SELECT_OPTION and retry paths can re-resolve inside
        # ``_act`` after the outer scope captured its initial geometry. A typed
        # zero-input mismatch must describe this live edge, not the stale
        # pre-focus target.
        self._active_delivery_resolution = fresh_resolution
        self._active_delivery_region = fresh_region
        return fresh_resolution, fresh_region, fresh_png, error

    def _resolve_step(
        self,
        step: Step,
        screen_png: bytes,
        bundle_dir: Path,
        workflow: Workflow,
        *,
        allow_grounder: bool = True,
        allow_target_ocr: bool = True,
    ) -> tuple[Optional[Resolution], Optional[Region], Optional[str]]:
        """Resolve the step's anchor, applying the irreversible risk gate.

        Returns:
            (resolution, matched_region, error). ``error`` is set when the
            step needs an anchor it doesn't have, the ladder fails, or the
            risk gate blocks acting.
        """
        needs_anchor = step.action in (
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
            ActionKind.RIGHT_CLICK,
            ActionKind.DRAG,
        )
        if step.anchor is None:
            if needs_anchor:
                return (
                    None,
                    None,
                    (
                        f"Step '{step.id}' ({step.intent}) is a {step.action.value} "
                        "step but has no anchor"
                    ),
                )
            return None, None, None

        # Template-crop bytes for the visual ``template`` rung. An ENCRYPTED
        # bundle (openadapt-flow#113) has NO cleartext ``templates/*.png`` on
        # disk -- only sealed ``.enc`` ciphertext -- so its crops are pulled
        # from the in-memory plaintext that ``Workflow.load(key=...)`` already
        # decrypted, via ``decrypted_template``. A plaintext bundle reads the
        # PNG straight from disk exactly as before (the fallback also covers an
        # encrypted bundle whose crop wasn't sealed, keeping ``template_png``
        # None rather than erroring).
        rel = step.anchor.template
        template_png = self._asset_bytes(bundle_dir, rel, workflow=workflow)

        # The structural ACTION rung (top of the ladder) runs only when the
        # live backend can re-find an element (StructuralActionBackend). A
        # pixel-only backend (RDP/Citrix) lacks the method, so ``structural``
        # is None and resolution uses the visual ladder unchanged.
        structural = (
            self.backend
            if self.use_structural and hasattr(self.backend, "locate_structural")
            else None
        )
        resolved = resolve(
            step.anchor,
            screen_png,
            self.vision,
            self.grounder if allow_grounder else None,
            step.intent,
            template_png=template_png,
            viewport=_frame_viewport(screen_png),
            structural=structural,
            allow_target_ocr=allow_target_ocr,
        )
        if resolved is None:
            return (
                None,
                None,
                (
                    f"Could not resolve target for step '{step.id}' "
                    f"({step.intent}): all resolution rungs failed"
                ),
            )
        resolution, matched_region = resolved

        if step.risk == "irreversible" and is_below_ocr(resolution.rung):
            return (
                resolution,
                matched_region,
                (
                    f"Step '{step.id}' ({step.intent}) is irreversible but only "
                    f"resolved via the '{resolution.rung}' rung — needs human "
                    "confirmation; refusing to act (v0 policy)"
                ),
            )
        return resolution, matched_region, None

    def _resolve_drag_end(
        self,
        step: Step,
        screen_png: bytes,
        bundle_dir: Path,
        workflow: Workflow,
    ) -> tuple[Optional[Resolution], Optional[Region], Optional[str]]:
        """Resolve a drag destination independently on the actuation frame.

        A drag is never replayed as a stored coordinate pair. The compiler
        retains separate evidence for both endpoints; immediately before
        delivery the runtime resolves both against the same fresh frame and
        applies the same irreversible-action resolution floor to each.
        """

        if step.drag_end_anchor is None:
            return (
                None,
                None,
                (
                    f"Step '{step.id}' ({step.intent}) is a drag step but has no "
                    "destination anchor"
                ),
            )
        endpoint_step = step.model_copy(
            update={"action": ActionKind.CLICK, "anchor": step.drag_end_anchor}
        )
        resolution, region, error = self._resolve_step(
            endpoint_step,
            screen_png,
            bundle_dir,
            workflow,
        )
        return resolution, region, error

    def _deliver_backend_call(
        self,
        result: StepResult,
        call: Callable[[], _DeliveryResultT],
    ) -> _DeliveryResultT:
        """Call one backend delivery boundary with exact attempt reporting.

        A typed fresh-frame mismatch or structural refusal proves that this
        call emitted no input edge, so it preserves the step's prior delivery
        state. Every other exception is conservative: the boundary may have
        been crossed and the step must not enter the pre-delivery retry loop.
        """

        attempted_before = result.delivery_attempted
        acknowledgers = self._active_delivery_acknowledgers
        self._active_delivery_acknowledgers = ()
        try:
            delivered = call()
        except (FreshActuationRequired, StructuralResolutionRefused):
            result.delivery_attempted = attempted_before
            raise
        except Exception:
            result.delivery_attempted = True
            raise
        result.delivery_attempted = True
        for guard in acknowledgers:
            guard.acknowledge_delivery()
        return delivered

    @staticmethod
    def _regions_intersect(left: Region, right: Region) -> bool:
        left_x, left_y, left_width, left_height = left
        right_x, right_y, right_width, right_height = right
        return not (
            left_x + left_width <= right_x
            or right_x + right_width <= left_x
            or left_y + left_height <= right_y
            or right_y + right_height <= left_y
        )

    @staticmethod
    def _translate_recorded_region(
        step: Step,
        resolution: Optional[Resolution],
        region: Region,
    ) -> Region:
        """Map recorded anchor-relative geometry onto the live target point."""

        if step.anchor is None or resolution is None:
            return region
        recorded_x, recorded_y = step.anchor.click_point
        live_x, live_y = resolution.point
        x, y, width, height = region
        return (
            live_x + x - recorded_x,
            live_y + y - recorded_y,
            width,
            height,
        )

    @classmethod
    def _fresh_actuation_identity_regions(
        cls,
        step: Step,
        workflow: Workflow,
        resolution: Optional[Resolution],
    ) -> list[Region]:
        regions: list[Region] = []
        if step.anchor is not None and step.anchor.identifier_region is not None:
            regions.append(
                cls._translate_recorded_region(
                    step, resolution, step.anchor.identifier_region
                )
            )
        if workflow.qualification is not None:
            policy = workflow.qualification.identity_policies.get(step.id)
            if policy is not None:
                regions.extend(
                    cls._translate_recorded_region(step, resolution, signal.region)
                    for signal in policy.signals
                    if signal.region is not None
                )
        return regions

    def _fresh_actuation_event(
        self,
        exc: FreshActuationRequired,
        *,
        step: Step,
        workflow: Workflow,
        resolution: Optional[Resolution],
        matched_region: Optional[Region],
        retried: bool,
        attempt: int,
    ) -> FreshActuationEvent:
        target_region = matched_region
        if target_region is None and step.anchor is not None:
            target_region = self._translate_recorded_region(
                step, resolution, step.anchor.region
            )
        target_intersection = (
            self._regions_intersect(exc.changed_bbox, target_region)
            if target_region is not None
            else None
        )
        identity_regions = self._fresh_actuation_identity_regions(
            step, workflow, resolution
        )
        identity_intersection = (
            any(
                self._regions_intersect(exc.changed_bbox, region)
                for region in identity_regions
            )
            if identity_regions
            else None
        )
        return FreshActuationEvent(
            attempt=attempt,
            operation=exc.operation,
            changed_pixel_count=exc.changed_pixel_count,
            changed_bbox=exc.changed_bbox,
            frame_size=exc.frame_size,
            target_intersection=target_intersection,
            identity_intersection=identity_intersection,
            retried=retried,
        )

    def _active_program_frame_refusal(
        self,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        step: Step,
        base_params: Mapping[str, RuntimeParamScalar],
    ) -> Optional[str]:
        """Bind the live interpreter path to the sealed program before input."""

        frames = getattr(self, "_frame_stack", ())
        if not frames:
            if workflow.program is not None:
                return "governed program execution lost its active control frame"
            return None
        if workflow.program is None:
            return "governed linear execution acquired an unexpected program frame"

        expected_params = dict(base_params)
        live_scope = self._program_execution_scope()
        parent_state: Optional[State] = None
        for index, frame in enumerate(frames):
            graph_id = frame.get("graph_id")
            graph = (
                workflow.program
                if graph_id == TOP_GRAPH_ID
                else workflow.subflows.get(str(graph_id))
            )
            state_id = frame.get("state_id")
            state = graph.states.get(str(state_id)) if graph is not None else None
            if graph is None or state is None:
                return (
                    "governed program control frame no longer matches a sealed "
                    "graph and state"
                )
            if state.id != str(state_id):
                return "program state identity changed after action admission"
            cursor = frame.get("loop")
            if index == 0:
                if graph_id != TOP_GRAPH_ID or cursor is not None:
                    return (
                        "governed program root frame no longer matches the sealed "
                        "program entry path"
                    )
            elif cursor is not None:
                loop = parent_state.loop if parent_state is not None else None
                if (
                    parent_state is None
                    or parent_state.kind is not StateKind.LOOP
                    or loop is None
                    or parent_state.id != cursor.loop_state_id
                    or cursor.relation != loop.relation
                    or graph_id != loop.body
                ):
                    return (
                        "governed program loop cursor no longer matches its "
                        "sealed loop state"
                    )
                source_rows: Optional[list[dict[str, str]]]
                if (
                    self._active_runtime_worklists is not None
                    and loop.relation in self._active_runtime_worklists
                ):
                    source_rows = self._active_runtime_worklists[loop.relation]
                else:
                    relation = workflow.data_sources.get(loop.relation)
                    source_rows = relation.rows if relation is not None else None
                if source_rows is None or cursor.rows != source_rows:
                    return (
                        "governed program loop cursor no longer matches its "
                        "authorized worklist"
                    )
                if cursor.row_index < 0 or cursor.row_index >= len(source_rows):
                    return "governed program loop lost its authorized iteration cursor"
                expected_params.update(source_rows[cursor.row_index])
            elif (
                parent_state is None
                or parent_state.kind is not StateKind.SUBFLOW_CALL
                or parent_state.subflow != graph_id
            ):
                return (
                    "governed program child frame no longer matches its sealed "
                    "subflow call"
                )

            expected_params = self._params_with_business_decisions(
                expected_params,
                live_scope[: index + 1],
                self._active_business_decision_evidence,
                run_dir=self._program_run_dir,
                workflow=workflow,
                governed_runtime_inputs_digest=(
                    self._program_governed_runtime_inputs_digest
                ),
            )

            if frame.get("params") != expected_params:
                return (
                    "governed program frame parameters no longer derive from "
                    "the authorized base inputs and active worklist rows"
                )
            parent_state = state

        leaf = frames[-1]
        if (
            leaf.get("graph_id") != self._current_graph_id
            or leaf.get("state_id") != self._current_state_id
            or parent_state is None
            or parent_state.kind is not StateKind.ACTION
            or parent_state.step != step
        ):
            return (
                "governed program leaf frame no longer matches the exact sealed "
                "action being executed"
            )
        if params != expected_params:
            return (
                "governed program iteration parameters no longer derive from "
                "the authorized base inputs and active worklist row"
            )
        return None

    def _fresh_actuation_authorization_refusal(
        self,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        step: Step,
    ) -> Optional[str]:
        """Recheck exact governed authority and inputs before fresh input."""

        snapshot_refusal = self._workflow_snapshot_refusal(workflow)
        if snapshot_refusal is not None:
            return snapshot_refusal
        authorization_snapshot_refusal = self._governed_authorization_snapshot_refusal()
        if authorization_snapshot_refusal is not None:
            return authorization_snapshot_refusal
        profile_refusal = self._profile_runtime_refusal(workflow)
        if profile_refusal is not None:
            return profile_refusal
        base_params = self._governed_base_params
        if base_params is None:
            if self.governed_authorization is not None or workflow.program is not None:
                return "runtime lost its admitted parameter scope before input"
            return self._governed_asset_mutation
        authorization = self.governed_authorization
        if authorization is not None:
            authorization_refusal = authorization.validate_workflow(workflow)
            if authorization_refusal is not None:
                return authorization_refusal
            actual_inputs = runtime_inputs_digest(
                workflow,
                base_params,
                self._active_runtime_worklists,
                interstitials=list(self._interstitials),
            )
            if actual_inputs != authorization.runtime_inputs_digest:
                return (
                    "governed run authorization no longer matches the current "
                    "runtime inputs"
                )
        try:
            frame_refusal = self._active_program_frame_refusal(
                workflow, params, step, base_params
            )
        except Exception as exc:  # noqa: BLE001 - mutated interpreter boundary
            frame_refusal = (
                "program frame validation failed closed before input "
                f"({type(exc).__name__})"
            )
        if frame_refusal is not None:
            return frame_refusal
        return self._governed_asset_mutation

    def _install_execution_snapshots(self, workflow: Workflow) -> None:
        """Copy caller-owned workflow and authorization before any callback."""

        self._execution_snapshot_required = True
        self._execution_workflow_source = workflow
        try:
            self._execution_workflow_snapshot = workflow.model_copy(deep=True)
        except Exception:  # noqa: BLE001 - mutable caller-owned model boundary
            self._execution_workflow_snapshot = None
        self._governed_authorization_source = self.governed_authorization
        self._governed_authorization_snapshot_error = None
        if self.governed_authorization is not None:
            try:
                self.governed_authorization = self.governed_authorization.model_copy(
                    deep=True
                )
            except Exception as exc:  # noqa: BLE001 - caller-owned boundary
                self.governed_authorization = None
                self._governed_authorization_snapshot_error = (
                    "governed authorization could not be snapshotted before "
                    f"execution ({type(exc).__name__})"
                )

    def _workflow_snapshot_refusal(self, workflow: Workflow) -> Optional[str]:
        """Refuse a caller-owned workflow change after run admission."""

        snapshot = self._execution_workflow_snapshot
        source = self._execution_workflow_source or workflow
        if snapshot is None:
            if (
                self._execution_snapshot_required
                or self.governed_authorization is not None
                or workflow.program is not None
            ):
                return "workflow semantics could not be snapshotted before execution"
            # Preserve the private direct-_act compatibility seam for a linear
            # Demo step. Public run() always installs a snapshot first.
            return None
        try:
            current = source.model_dump(mode="json")
            admitted = snapshot.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - mutated model boundary
            return (
                "workflow semantic validation failed closed before input "
                f"({type(exc).__name__})"
            )
        if current != admitted:
            return "workflow semantics changed after admission"
        return None

    def _governed_authorization_snapshot_refusal(self) -> Optional[str]:
        """Refuse drift in the caller-owned governed run authorization."""

        if self._governed_authorization_snapshot_error is not None:
            return self._governed_authorization_snapshot_error
        source = self._governed_authorization_source
        admitted = self.governed_authorization
        if source is None:
            return None
        if admitted is None:
            return "governed authorization snapshot is unavailable"
        try:
            current = source.model_dump(mode="json")
            snapshot = admitted.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - mutable caller-owned boundary
            return (
                "governed authorization validation failed closed before input "
                f"({type(exc).__name__})"
            )
        if current != snapshot:
            return "governed authorization changed after run admission"
        return None

    def _raise_on_workflow_snapshot_mutation(self, workflow: Workflow) -> None:
        """Stop a program edge or terminal when admitted semantics changed."""

        refusal = self._workflow_snapshot_refusal(workflow)
        if refusal is None:
            refusal = self._governed_authorization_snapshot_refusal()
        if refusal is not None:
            raise _ProgramHalt(
                "halt",
                f"{refusal}; no later program state was executed",
                safety=True,
            )

    def _managed_dispatch_refusal(self) -> Optional[str]:
        """Refuse public-field drift from the managed constructor capability."""

        snapshot = self._managed_dispatch_snapshot
        if snapshot is None:
            return None
        run_id, binding_sha256, authorization_json = snapshot
        if self.durable is not True:
            return "managed dispatch binding requires durable=True"
        if self.delivery_authority_kind != "cloud_runner":
            return "managed dispatch delivery authority changed after admission"
        if self.remote_delivery_run_id != run_id:
            return "managed dispatch run identity changed after admission"
        binding = self.managed_dispatch_binding
        try:
            from openadapt_flow.runner.dispatch_envelope import ManagedDispatchBinding
            from openadapt_flow.runner.protocol import dispatch_binding_sha256 as digest

            if not isinstance(binding, ManagedDispatchBinding):
                return "managed dispatch binding disappeared after admission"
            authorization = self.governed_authorization
            if authorization is None:
                return "managed dispatch authorization disappeared after admission"
            current_authorization_json = json.dumps(
                authorization.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if (
                binding.run_id != run_id
                or binding.binding_sha256 != binding_sha256
                or binding.authorization.model_dump(mode="json")
                != authorization.model_dump(mode="json")
                or current_authorization_json != authorization_json
                or binding_sha256 != digest(run_id, binding.authorization)
            ):
                return "managed dispatch binding changed after admission"
        except Exception:  # noqa: BLE001 - managed capability boundary
            return "managed dispatch binding is invalid after admission"
        return None

    def _production_qualification_refusal(self, workflow: Workflow) -> Optional[str]:
        """Bind the current v2 admission to the durable run authorization."""

        authorization = self.governed_authorization
        if authorization is None or authorization.execution_profile not in {
            "standard",
            "regulated",
        }:
            return None
        if authorization.qualification_case_id is not None:
            return None
        guard = self.production_qualification_guard
        if guard is None:
            if self.managed_dispatch_binding is None:
                # ``openadapt flow run --profile standard|regulated`` refuses
                # before constructing this replayer when the signed admission is
                # missing. The tutorial and in-process tests still use the
                # Standard evidence gate without that CLI actuation path.
                return None
            from openadapt_flow.production_qualification import (
                managed_qualification_edge_refusal,
            )

            refusal = managed_qualification_edge_refusal(authorization, workflow)
            if refusal is not None:
                return "Production qualification could not be verified before execution"
            return None
        try:
            expected = guard.authorization_binding(workflow)
        except Exception as exc:  # noqa: BLE001 - private authority boundary
            return (
                "Production qualification could not be verified before execution "
                f"({type(exc).__name__})"
            )
        if any(
            getattr(authorization, field, None) != value
            for field, value in expected.items()
        ):
            return "Production qualification differs from the run authorization"
        return None

    def _qualification_campaign_refusal(self, workflow: Workflow) -> Optional[str]:
        """Bind a case run to its signed, isolated non-production permit."""

        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_id is None:
            return None
        guard = self.qualification_campaign_guard
        if guard is None:
            carries_permit = (
                getattr(authorization, "qualification_campaign_permit_id", None)
                is not None
            )
            if not carries_permit:
                return None
            return "qualification actuation has no signed campaign permit"
        try:
            return guard.authorization_refusal(workflow, authorization)
        except Exception as exc:  # noqa: BLE001 - private authority boundary
            return (
                "qualification campaign could not be verified before execution "
                f"({type(exc).__name__})"
            )

    def _delivery_authorization_refusal(
        self,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        step: Step,
        result: StepResult,
    ) -> Optional[str]:
        """Recheck exact authority at the last point before input delivery."""

        self._active_delivery_acknowledgers = ()
        acknowledgers: list[Any] = []
        refusal = self._managed_dispatch_refusal()
        if refusal is None and self.production_qualification_guard is not None:
            try:
                refusal = self.production_qualification_guard.refusal(workflow)
            except Exception as exc:  # noqa: BLE001 - authority boundary
                refusal = (
                    "Production qualification could not be revalidated before "
                    f"delivery ({type(exc).__name__})"
                )
        if (
            refusal is None
            and self.production_qualification_guard is None
            and self.managed_dispatch_binding is not None
        ):
            from openadapt_flow.production_qualification import (
                managed_qualification_edge_refusal,
            )

            try:
                edge_refusal = managed_qualification_edge_refusal(
                    self.governed_authorization, workflow
                )
            except Exception as exc:  # noqa: BLE001 - authority boundary
                refusal = (
                    "Production qualification could not be revalidated before "
                    f"delivery ({type(exc).__name__})"
                )
            else:
                if edge_refusal is not None:
                    refusal = (
                        "Production qualification could not be revalidated before "
                        "delivery"
                    )
        if refusal is None and self.qualification_campaign_guard is not None:
            try:
                refusal = self.qualification_campaign_guard.refusal(workflow)
            except Exception as exc:  # noqa: BLE001 - authority boundary
                refusal = (
                    "qualification campaign could not be revalidated before "
                    f"delivery ({type(exc).__name__})"
                )
        if refusal is None:
            refusal = self._fresh_actuation_authorization_refusal(
                workflow, params, step
            )
        initial_guard = getattr(self, "_durable_initial_delivery_guard", None)
        if refusal is None and initial_guard is not None:
            try:
                initial_guard.before_delivery()
                acknowledgers.append(initial_guard)
            except Exception as exc:  # noqa: BLE001 - durable fencing boundary
                refusal = (
                    f"managed initial delivery was preempted before delivery: {exc}"
                )
                result.failure_category = "continuation_preempted"
        if refusal is None and self._durable_continuation_guard is not None:
            try:
                self._durable_continuation_guard.before_delivery()
                acknowledgers.append(self._durable_continuation_guard)
            except Exception as exc:  # noqa: BLE001 - durable fencing boundary
                refusal = f"durable continuation was preempted before delivery: {exc}"
                result.failure_category = "continuation_preempted"
        if refusal is not None:
            self._cancel_guarded_coordinate()
            self._cancel_guarded_keyboard()
            result.safety_halt = True
            if result.failure_category != "continuation_preempted":
                result.failure_category = (
                    "governed_refusal"
                    if self.governed_authorization is not None
                    else "safety_halt"
                )
        else:
            self._active_delivery_acknowledgers = tuple(acknowledgers)
        return refusal

    def _act(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: Mapping[str, RuntimeParamScalar],
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        before_png: bytes,
        result: StepResult,
        run_dir: Path,
        graph_ctx: Optional["_GraphStepContext"] = None,
    ) -> Optional[str]:
        """Perform the step's action through the backend.

        Returns:
            An error string (no action performed / partial) or None.
        """
        if step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            assert resolution is not None  # guaranteed by _resolve_step
            x, y = resolution.point
            native_act = getattr(self.backend, "act_structural", None)
            requires_atomic_identity = self._requires_atomic_identity_pointer(
                step, workflow
            )
            if (
                resolution.rung == "structural"
                and resolution.structural_handle is not None
                and step.anchor is not None
                and step.anchor.structural is not None
                and callable(native_act)
            ):
                if (
                    requires_atomic_identity
                    and resolution.structural_handle.target_fingerprint is None
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "identity-gated click, but its structural target has no "
                        "fresh actuation fingerprint — refusing raw coordinate "
                        "delivery; run aborted"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                structural_locator = step.anchor.structural
                structural_handle = resolution.structural_handle
                structural_act = cast(Callable[..., Any], native_act)
                delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: structural_act(
                        structural_locator,
                        structural_handle,
                        double=step.action is ActionKind.DOUBLE_CLICK,
                    ),
                )
                result.delivery_receipt = delivery_receipt
                result.actuation = "uia" if delivery_receipt.native else "dom"
            else:
                remote_consequential = self._step_requires_remote_guarded_actuation(
                    step, workflow
                )
                typed_remote = isinstance(
                    self.backend,
                    GuardedRemotePointerActionBackend,
                )
                if (
                    remote_consequential
                    and not typed_remote
                    and self._typed_remote_receipt_required()
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "remote click, but this backend cannot bind its exact "
                        "fresh frame and target to delivery; run aborted"
                    )
                if remote_consequential:
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    if typed_remote:
                        remote_pointer = cast(
                            GuardedRemotePointerActionBackend, self.backend
                        )
                        result.delivery_receipt = self._deliver_backend_call(
                            result,
                            lambda: remote_pointer.click_guarded(
                                x,
                                y,
                                expected_frame_sha256=hashlib.sha256(
                                    before_png
                                ).hexdigest(),
                                double=step.action is ActionKind.DOUBLE_CLICK,
                            ),
                        )
                        result.actuation = "remote_guarded"
                    else:
                        # See ``_typed_remote_receipt_required``: the exact
                        # fresh frame is already bound by the backend's own
                        # one-shot lease, armed immediately above by
                        # ``_revalidate_consequential_actuation``.
                        self._deliver_backend_call(
                            result,
                            lambda: self.backend.click(
                                x,
                                y,
                                double=step.action is ActionKind.DOUBLE_CLICK,
                            ),
                        )
                elif requires_atomic_identity:
                    if isinstance(self.backend, GuardedCoordinateActionBackend):
                        refusal = self._delivery_authorization_refusal(
                            workflow, params, step, result
                        )
                        if refusal is not None:
                            return refusal
                        self._require_qualification_environment_current()
                        coordinate_backend = cast(
                            GuardedCoordinateActionBackend, self.backend
                        )
                        result.delivery_receipt = self._deliver_backend_call(
                            result,
                            lambda: coordinate_backend.act_guarded_coordinate(
                                x,
                                y,
                                expected_frame_sha256=hashlib.sha256(
                                    before_png
                                ).hexdigest(),
                                double=step.action is ActionKind.DOUBLE_CLICK,
                            ),
                        )
                        result.actuation = "guarded_coordinate"
                    else:
                        result.safety_halt = True
                        result.failure_category = "safety_halt"
                        return (
                            f"Step '{step.id}' ({step.intent}) is a consequential "
                            "identity-gated click, but this backend cannot bind "
                            "identity verification to the same actuation operation "
                            "— refusing raw coordinate delivery; run aborted"
                        )
                else:
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    self._deliver_backend_call(
                        result,
                        lambda: self.backend.click(
                            x,
                            y,
                            double=step.action is ActionKind.DOUBLE_CLICK,
                        ),
                    )
            self._last_click_point = (x, y)
            # Only a STRUCTURAL handle reports the element's own bounds. A
            # visual rung's ``matched_region`` is where the anchor TEMPLATE
            # matched, which is not the field: narrowing a following TYPE's
            # verification window to it hides the typed value and false-halts
            # a healthy run.
            self._last_click_region = (
                resolution.structural_handle.region
                if resolution.rung == "structural"
                and resolution.structural_handle is not None
                else None
            )
            return None

        if step.action is ActionKind.RIGHT_CLICK:
            assert resolution is not None  # guaranteed by _resolve_step
            x, y = resolution.point
            requires_atomic_identity = self._requires_atomic_identity_pointer(
                step, workflow
            )
            remote_consequential = self._step_requires_remote_guarded_actuation(
                step, workflow
            )
            if remote_consequential and not isinstance(
                self.backend,
                GuardedRemotePointerActionBackend,
            ):
                if self._typed_remote_receipt_required():
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "remote right click, but this backend cannot bind its "
                        "exact fresh frame and target to delivery; run aborted"
                    )
                # See ``_typed_remote_receipt_required``: the exact fresh frame
                # is already bound by the backend's own one-shot lease, and its
                # first input edge consumes it.
                if not isinstance(self.backend, RichPointerActionBackend):
                    return (
                        f"Step '{step.id}' ({step.intent}) requires a right "
                        "click, but this backend has no bounded right-click "
                        "operation"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(
                    result,
                    lambda: cast(RichPointerActionBackend, self.backend).right_click(
                        x, y
                    ),
                )
                return None
            if remote_consequential:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                remote_pointer = cast(GuardedRemotePointerActionBackend, self.backend)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: remote_pointer.right_click_guarded(
                        x,
                        y,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "remote_guarded"
            elif requires_atomic_identity:
                if isinstance(self.backend, GuardedCoordinateActionBackend):
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    coordinate_backend = cast(
                        GuardedCoordinateActionBackend, self.backend
                    )
                    result.delivery_receipt = self._deliver_backend_call(
                        result,
                        lambda: coordinate_backend.act_guarded_coordinate(
                            x,
                            y,
                            expected_frame_sha256=hashlib.sha256(
                                before_png
                            ).hexdigest(),
                            button="right",
                        ),
                    )
                    result.actuation = "guarded_coordinate"
                else:
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "identity-gated right click, but this backend cannot bind "
                        "identity verification to delivery; run aborted"
                    )
            elif isinstance(self.backend, RichPointerActionBackend):
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(
                    result,
                    lambda: cast(RichPointerActionBackend, self.backend).right_click(
                        x, y
                    ),
                )
            else:
                return (
                    f"Step '{step.id}' ({step.intent}) requires a right click, "
                    "but this backend does not implement it"
                )
            return None

        if step.action is ActionKind.DRAG:
            assert resolution is not None  # guaranteed by _resolve_step
            drag_end, drag_end_region, drag_error = self._resolve_drag_end(
                step,
                before_png,
                bundle_dir,
                workflow,
            )
            if (
                drag_end is not None
                and drag_end_region is not None
                and step.drag_end_anchor is not None
            ):
                drag_end = self._retain_visual_resolution_evidence(
                    workflow=workflow,
                    anchor=step.drag_end_anchor,
                    resolution=drag_end,
                    matched_region=drag_end_region,
                    frame_png=before_png,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                )
            result.drag_end_resolution = drag_end
            if drag_error is not None:
                self._cancel_guarded_coordinate()
                return drag_error
            assert drag_end is not None
            x, y = resolution.point
            end_x, end_y = drag_end.point
            requires_atomic_identity = self._requires_atomic_identity_pointer(
                step, workflow
            )
            structural_drag = getattr(self.backend, "drag_structural_guarded", None)
            if (
                resolution.rung == "structural"
                and drag_end.rung == "structural"
                and resolution.structural_handle is not None
                and drag_end.structural_handle is not None
                and step.anchor is not None
                and step.anchor.structural is not None
                and step.drag_end_anchor is not None
                and step.drag_end_anchor.structural is not None
                and callable(structural_drag)
            ):
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                source_locator = step.anchor.structural
                source_handle = resolution.structural_handle
                destination_locator = step.drag_end_anchor.structural
                destination_handle = drag_end.structural_handle
                structural_drag_act = cast(Callable[..., Any], structural_drag)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: structural_drag_act(
                        source_locator,
                        source_handle,
                        destination_locator,
                        destination_handle,
                    ),
                )
                result.actuation = "dom"
            elif self._step_requires_remote_guarded_actuation(step, workflow) and (
                isinstance(self.backend, GuardedRemotePointerActionBackend)
                or self._typed_remote_receipt_required()
            ):
                if not isinstance(
                    self.backend,
                    GuardedRemotePointerActionBackend,
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "remote drag, but this backend cannot bind both exact "
                        "fresh-frame endpoints to delivery; run aborted"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                remote_pointer = cast(GuardedRemotePointerActionBackend, self.backend)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: remote_pointer.drag_guarded(
                        x,
                        y,
                        end_x,
                        end_y,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "remote_guarded"
            elif requires_atomic_identity and not isinstance(
                self.backend, RemoteActuationBackend
            ):
                if not isinstance(self.backend, GuardedDragActionBackend):
                    self._cancel_guarded_coordinate()
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "drag, but this backend cannot bind both freshly "
                        "resolved endpoints to delivery; run aborted"
                    )
                # Pixel-only source and destination evidence were resolved
                # against ``before_png``. Drop any unused structural tokens,
                # then bind the source coordinate and require that exact frame
                # immediately before mouse-down. Fully structural browser
                # drags take the stronger two-element lease above.
                cancel_structural = getattr(
                    self.backend,
                    "cancel_pending_structural_guards",
                    None,
                )
                if callable(cancel_structural):
                    cancel_structural()
                if not isinstance(self.backend, GuardedCoordinateActionBackend):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) is a consequential "
                        "drag, but this backend cannot bind its freshly "
                        "resolved source to delivery; run aborted"
                    )
                try:
                    self.backend.arm_guarded_coordinate(x, y)
                except Exception as exc:  # noqa: BLE001 - backend boundary
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    detail = _scrub_phi(str(exc)) or type(exc).__name__
                    return (
                        f"Step '{step.id}' ({step.intent}) could not bind its "
                        f"freshly resolved drag source: {detail}; run aborted"
                    )
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                drag_backend = cast(GuardedDragActionBackend, self.backend)
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: drag_backend.drag_guarded(
                        x,
                        y,
                        end_x,
                        end_y,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "guarded_coordinate"
            elif isinstance(self.backend, RichPointerActionBackend):
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(
                    result,
                    lambda: cast(RichPointerActionBackend, self.backend).drag(
                        x, y, end_x, end_y
                    ),
                )
            else:
                return (
                    f"Step '{step.id}' ({step.intent}) requires a drag, but "
                    "this backend does not implement it"
                )
            return None

        if step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION):
            if step.secret:
                # Secret value is never in the bundle/params: inject it from
                # the environment, failing fast with an actionable message.
                env_var = secret_env_var(step.param or "")
                text = os.environ.get(env_var, "")
                if not step.param:
                    return (
                        f"Step '{step.id}' ({step.intent}) is marked secret "
                        "but names no parameter"
                    )
                if not text:
                    return (
                        f"Step '{step.id}' ({step.intent}) requires secret "
                        f"parameter '{step.param}', but the environment "
                        f"variable {env_var} is not set — export it with the "
                        "secret value and re-run"
                    )
            elif step.param is not None:
                if step.param not in params:
                    return (
                        f"Step '{step.id}' ({step.intent}) requires parameter "
                        f"'{step.param}' but it was not provided"
                    )
                text = runtime_param_text(params[step.param])
            elif step.text is not None:
                text = step.text
            else:
                return (
                    f"Step '{step.id}' ({step.intent}) is a TYPE step with "
                    "neither text nor param"
                )
            identity_atomic_keyboard = self._requires_atomic_identity_keyboard(
                step, workflow
            )
            requires_atomic_keyboard = (
                identity_atomic_keyboard or self.qualification_fault_driver is not None
            )
            if (
                requires_atomic_keyboard
                and not isinstance(self.backend, RemoteActuationBackend)
                and not isinstance(self.backend, GuardedKeyboardActionBackend)
            ):
                result.safety_halt = True
                result.failure_category = "safety_halt"
                return (
                    f"Step '{step.id}' ({step.intent}) is a qualification or "
                    "identity-gated keyboard action, but this backend cannot "
                    "bind the verified focus and execution context to the same "
                    "input operation — refusing unguarded keyboard delivery; "
                    "run aborted"
                )
            if step.selection_commit_key is not None:
                selection_contract_error = self._selection_contract_error(
                    step, workflow
                )
                if selection_contract_error is not None:
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return selection_contract_error
            # The field point: this step's own focusing click (anchored
            # TYPE), or the immediately preceding step's click point (the
            # recorder's click-to-focus-then-type pattern). When focus was
            # moved some other way (Tab between fields), there is no known
            # field point — verification diffs the whole frame and the
            # retry cannot re-click.
            field_point: Optional[Point] = None
            field_region: Optional[Region] = None
            if resolution is not None:
                # Anchored TYPE: click to focus the field first.
                x, y = resolution.point
                native_act = getattr(self.backend, "act_structural", None)
                requires_atomic_identity = self._requires_atomic_identity_pointer(
                    step, workflow
                )
                if (
                    resolution.rung == "structural"
                    and resolution.structural_handle is not None
                    and step.anchor is not None
                    and step.anchor.structural is not None
                    and callable(native_act)
                ):
                    refusal = self._delivery_authorization_refusal(
                        workflow, params, step, result
                    )
                    if refusal is not None:
                        return refusal
                    self._require_qualification_environment_current()
                    structural_locator = step.anchor.structural
                    structural_handle = resolution.structural_handle
                    structural_act = cast(Callable[..., Any], native_act)
                    result.delivery_receipt = self._deliver_backend_call(
                        result,
                        lambda: structural_act(
                            structural_locator,
                            structural_handle,
                        ),
                    )
                    result.actuation = "uia"
                else:
                    if (
                        requires_atomic_identity
                        and not isinstance(self.backend, RemoteActuationBackend)
                        and isinstance(self.backend, GuardedCoordinateActionBackend)
                    ):
                        refusal = self._delivery_authorization_refusal(
                            workflow, params, step, result
                        )
                        if refusal is not None:
                            return refusal
                        self._require_qualification_environment_current()
                        coordinate_backend = cast(
                            GuardedCoordinateActionBackend, self.backend
                        )
                        result.delivery_receipt = self._deliver_backend_call(
                            result,
                            lambda: coordinate_backend.act_guarded_coordinate(
                                x,
                                y,
                                expected_frame_sha256=hashlib.sha256(
                                    before_png
                                ).hexdigest(),
                            ),
                        )
                        result.actuation = "guarded_coordinate"
                    elif requires_atomic_identity and not isinstance(
                        self.backend, RemoteActuationBackend
                    ):
                        result.safety_halt = True
                        result.failure_category = "safety_halt"
                        return (
                            f"Step '{step.id}' ({step.intent}) is a consequential "
                            "identity-gated focusing click, but this backend "
                            "cannot bind target and context verification to the "
                            "same coordinate operation — refusing raw coordinate "
                            "delivery; run aborted"
                        )
                    else:
                        refusal = self._delivery_authorization_refusal(
                            workflow, params, step, result
                        )
                        if refusal is not None:
                            return refusal
                        self._require_qualification_environment_current()
                        self._deliver_backend_call(
                            result, lambda: self.backend.click(x, y)
                        )
                field_point = (x, y)
                # See ``_last_click_region``: a visual rung's matched region is
                # the anchor template's live position, not the field's bounds.
                # SELECT_OPTION does not need it here — it always revalidates
                # below and consumes that pass's fresh matched region.
                field_region = (
                    resolution.structural_handle.region
                    if resolution.rung == "structural"
                    and resolution.structural_handle is not None
                    else None
                )
                # Fresh baseline AFTER the focusing click so its own focus ring
                # never counts as "input landed". A consequential remote TYPE
                # is a second input gesture after that click, so reacquire and
                # re-run target/identity before typing; the focusing click
                # consumed the first one-shot remote lease.
                if self._step_needs_consequential_revalidation(step, workflow):
                    focused_field_point = field_point
                    (
                        refreshed,
                        refreshed_region,
                        before_png,
                        remote_error,
                    ) = self._revalidate_consequential_actuation(
                        step,
                        resolution,
                        field_region,
                        before_png,
                        params,
                        workflow,
                        bundle_dir,
                        result,
                        run_dir=run_dir,
                        arm_keyboard=True,
                    )
                    if remote_error is not None:
                        return remote_error
                    if refreshed is not None:
                        if (
                            step.action is ActionKind.SELECT_OPTION
                            and focused_field_point is not None
                            and step.anchor is not None
                            and not self._selection_target_continuous(
                                step.anchor.region,
                                focused_field_point,
                                refreshed.point,
                            )
                        ):
                            self._cancel_guarded_keyboard()
                            result.safety_halt = True
                            result.failure_category = "safety_halt"
                            return (
                                f"Step '{step.id}' ({step.intent}) re-resolved "
                                "to a different field after focus; refusing "
                                "option selection"
                            )
                        assert step.anchor is not None
                        if refreshed_region is not None:
                            refreshed = self._retain_visual_resolution_evidence(
                                workflow=workflow,
                                anchor=step.anchor,
                                resolution=refreshed,
                                matched_region=refreshed_region,
                                frame_png=before_png,
                                bundle_dir=bundle_dir,
                                run_dir=run_dir,
                                allow_target_ocr=False,
                            )
                        resolution = refreshed
                        result.resolution = refreshed
                        field_point = refreshed.point
                    # A visual match region is the retained template crop. It
                    # is not the editable control's bounds. Treating that crop
                    # as the typed-value readback region can exclude text that
                    # renders at the left edge of a wide field. Keep exact
                    # structural bounds when the backend supplies them;
                    # otherwise use the point-centred visual readback window.
                    if step.action is ActionKind.SELECT_OPTION:
                        # Composite selection binds its type-ahead and commit
                        # to the exact live resolved selection region.
                        field_region = refreshed_region
                    else:
                        field_region = (
                            refreshed.structural_handle.region
                            if (
                                refreshed is not None
                                and refreshed.rung == "structural"
                                and refreshed.structural_handle is not None
                            )
                            else None
                        )
                else:
                    before_png = self.backend.screenshot()
            elif self._prev_was_click(workflow, step_index, graph_ctx):
                field_point = self._last_click_point
                field_region = self._last_click_region
            baseline_field_value = self._text_value_at(field_point)
            if baseline_field_value is None:
                baseline_field_value = self._text_value_at(None)
            if step.selection_commit_key is not None:
                selection_commit_key = step.selection_commit_key
                assert step.selection_region is not None
                assert isinstance(self.backend, SelectOptionBackend)
                assert step.anchor is not None
                if not self._selection_region_compatible(
                    step.anchor.region, field_region
                ):
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) re-resolved to "
                        "incompatible live field geometry; refusing option "
                        "selection"
                    )
                assert field_region is not None
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                if isinstance(self.backend, GuardedSelectOptionBackend):
                    assert field_point is not None
                    guarded_select = cast(GuardedSelectOptionBackend, self.backend)
                    result.delivery_receipt = self._deliver_backend_call(
                        result,
                        lambda: guarded_select.select_option_guarded(
                            text,
                            selection_commit_key,
                            target_point=field_point,
                            expected_frame_sha256=hashlib.sha256(
                                before_png
                            ).hexdigest(),
                        ),
                    )
                    result.actuation = (
                        "remote_guarded"
                        if isinstance(self.backend, RemoteActuationBackend)
                        else "guarded_keyboard"
                    )
                else:
                    select_backend = cast(SelectOptionBackend, self.backend)
                    self._deliver_backend_call(
                        result,
                        lambda: select_backend.select_option(
                            text, selection_commit_key
                        ),
                    )
                return None
            if (
                self.qualification_fault_driver is not None
                and not identity_atomic_keyboard
                and isinstance(self.backend, GuardedKeyboardActionBackend)
            ):
                if field_point is None:
                    result.safety_halt = True
                    result.failure_category = "safety_halt"
                    return (
                        f"Step '{step.id}' ({step.intent}) has no focused field "
                        "point for exact qualification delivery; run aborted"
                    )
                self._require_qualification_environment_current()
                cast(GuardedKeyboardActionBackend, self.backend).arm_guarded_keyboard(
                    *field_point
                )
            guarded_type = (
                requires_atomic_keyboard
                and not isinstance(self.backend, RemoteActuationBackend)
                and isinstance(self.backend, GuardedKeyboardActionBackend)
            )
            if guarded_type:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: cast(
                        GuardedKeyboardActionBackend, self.backend
                    ).type_text_guarded(
                        text,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "guarded_keyboard"
            elif requires_atomic_keyboard and not isinstance(
                self.backend, RemoteActuationBackend
            ):
                result.safety_halt = True
                result.failure_category = "safety_halt"
                return (
                    f"Step '{step.id}' ({step.intent}) lost its guarded keyboard "
                    "capability before delivery — refusing unguarded text input; "
                    "run aborted"
                )
            else:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(result, lambda: self.backend.type_text(text))
            if not text:
                return None  # nothing typed, nothing to verify
            return self._verify_typed_input(
                step,
                text,
                field_point,
                before_png,
                result,
                workflow=workflow,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                resolution=resolution,
                field_region=field_region,
                baseline_field_value=baseline_field_value,
                allow_masked=step.secret,
                allow_retry=not guarded_type,
            )

        if step.action in (ActionKind.KEY, ActionKind.HOTKEY):
            if not step.key:
                return (
                    f"Step '{step.id}' ({step.intent}) is a "
                    f"{step.action.value} step with no key"
                )
            key = (
                "+".join([*step.modifiers, step.key])
                if step.action is ActionKind.HOTKEY
                else step.key
            )
            requires_atomic_keyboard = self._requires_atomic_identity_keyboard(
                step, workflow
            )
            guarded_key = (
                requires_atomic_keyboard
                and not isinstance(self.backend, RemoteActuationBackend)
                and isinstance(self.backend, GuardedKeyboardActionBackend)
            )
            if guarded_key:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                result.delivery_receipt = self._deliver_backend_call(
                    result,
                    lambda: cast(
                        GuardedKeyboardActionBackend, self.backend
                    ).press_guarded(
                        key,
                        expected_frame_sha256=hashlib.sha256(before_png).hexdigest(),
                    ),
                )
                result.actuation = "guarded_keyboard"
            elif requires_atomic_keyboard and not isinstance(
                self.backend, RemoteActuationBackend
            ):
                result.safety_halt = True
                result.failure_category = "safety_halt"
                return (
                    f"Step '{step.id}' ({step.intent}) lost its guarded keyboard "
                    "capability before delivery — refusing unguarded key input; "
                    "run aborted"
                )
            else:
                refusal = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if refusal is not None:
                    return refusal
                self._require_qualification_environment_current()
                self._deliver_backend_call(result, lambda: self.backend.press(key))
            return None

        if step.action is ActionKind.WAIT:
            # WAIT means wait_settled only; the post-action settle handles it.
            return None

        if step.action is ActionKind.SCROLL:
            return self._act_scroll(
                step,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                before_png=before_png,
                params=params,
                result=result,
                graph_ctx=graph_ctx,
            )

        return f"Step '{step.id}' has unsupported action {step.action!r}"

    @staticmethod
    def _prev_was_click(
        workflow: Workflow,
        step_index: int,
        graph_ctx: Optional["_GraphStepContext"],
    ) -> bool:
        """Whether the step executed just before this TYPE step was a click.

        Linear mode reads ``workflow.steps[step_index - 1]`` exactly as before;
        program mode reads the interpreter-supplied ``graph_ctx.prev_action``
        (the previously EXECUTED action state -- the graph has no positional
        predecessor). The click-to-focus-then-type heuristic is thereby
        identical on both paths.
        """
        if graph_ctx is not None:
            return graph_ctx.prev_action in (
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
            )
        return step_index > 0 and workflow.steps[step_index - 1].action in (
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
        )

    # -- state-dependency robustness: readiness + interstitials -----------------

    @staticmethod
    def _canonical_interstitial_snapshot(
        declarations: Any,
    ) -> tuple[tuple[Interstitial, ...], tuple[str, ...]]:
        """Deep-validate declarations and return an immutable fingerprint.

        Pydantic models are mutable.  A shallow list copy therefore does not
        bind a governed run: a backend/vision callback could edit a nested
        detection, clearance, or dismissal field after authorization validation
        but before delivery.  The returned models share no nested objects with
        the caller, and the canonical strings let the runtime detect any such
        edit immediately before an interstitial action.
        """

        validated = tuple(
            Interstitial.model_validate(declaration.model_dump(mode="python"))
            for declaration in declarations
        )
        canonical = tuple(
            json.dumps(
                declaration.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for declaration in validated
        )
        return validated, canonical

    def _interstitial_declaration_mutation(
        self, workflow: Optional[Workflow]
    ) -> Optional[str]:
        """Return a refusal when caller-owned declarations changed mid-run."""

        snapshot_error = (
            self._runtime_interstitial_snapshot_error
            or self._workflow_interstitial_snapshot_error
        )
        if snapshot_error is not None:
            return f"{snapshot_error} - refusing to emit an action; run aborted."
        try:
            _runtime, runtime_snapshot = self._canonical_interstitial_snapshot(
                self._runtime_interstitial_source
            )
            workflow_snapshot: tuple[str, ...] = ()
            if workflow is not None:
                _workflow, workflow_snapshot = self._canonical_interstitial_snapshot(
                    workflow.interstitials
                )
        except Exception as exc:
            return (
                "Interstitial declarations changed after admission and are no "
                f"longer valid ({type(exc).__name__}) - refusing to emit an "
                "action; run aborted."
            )
        if (
            runtime_snapshot != self._runtime_interstitial_snapshot
            or workflow_snapshot != self._workflow_interstitial_snapshot
        ):
            return (
                "Interstitial declarations changed after admission - refusing "
                "to emit an action under a stale authorization; run aborted."
            )
        return None

    def _wait_starting_state_settled(
        self, before_png: bytes
    ) -> tuple[bytes, Optional[str], Optional[bool]]:
        """Readiness gate: refuse to act on a frame that never settled.

        When ``require_settled`` is on, invoke exactly ONE readiness-aware settle
        operation bounded by ``settle_readiness_timeout_s``. A screen that never
        settles (slow load, perpetual spinner), a facade that cannot report
        settledness, or an invalid/erroring settle result HALTs gracefully rather
        than acting on an arbitrary mid-transition frame.

        Returns ``(frame, None)`` when ready (or when the gate is off), else
        ``(frame, halt_reason)``.
        """
        if not self.require_settled:
            return before_png, None, None
        fn = getattr(self.vision, "wait_settled_result", None)
        if not callable(fn):
            return (
                before_png,
                (
                    "The configured vision facade cannot report whether the screen "
                    "settled (wait_settled_result is unavailable) - refusing to "
                    "act while require_settled=True; run aborted (starting state "
                    "not ready)."
                ),
                False,
            )
        try:
            result = fn(
                self.backend,
                interval_s=self.poll_interval_s,
                timeout_s=self.settle_readiness_timeout_s,
            )
            frame = result.png
            settled = result.settled
            if not isinstance(frame, bytes) or not isinstance(settled, bool):
                raise TypeError("invalid settle result")
        except Exception as exc:
            return (
                before_png,
                (
                    "The screen readiness check failed "
                    f"({type(exc).__name__}) - refusing to act while "
                    "require_settled=True; run aborted (starting state not ready)."
                ),
                False,
            )
        if not settled:
            return (
                frame,
                (
                    "The screen never stopped changing (still loading or animating) "
                    f"within {self.settle_readiness_timeout_s:.1f}s - refusing to act "
                    "on a mid-transition frame; run aborted (starting state not "
                    "ready). If this UI animates continuously, qualify the workflow "
                    "with a bounded per-step wait_until readiness predicate over a "
                    "stable observation region before retrying; otherwise keep the "
                    "readiness gate enabled and halt."
                ),
                False,
            )
        return frame, None, True

    @staticmethod
    def _qualification_id_sha256(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _observe_qualification_environment(
        self,
        workflow: Workflow,
        report: RunReport,
        *,
        execution_target_kind: Optional[ExecutionTargetKind],
    ) -> Optional[str]:
        """Retain exact live environment evidence for a campaign run.

        Project labels are expected values only. They never become observed
        evidence. A qualification case refuses before workflow actuation when
        the active backend cannot independently return the application,
        application version, and session identity or when either expected
        application field differs.
        """

        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_id is None:
            return None
        project = workflow.qualification
        if project is None or execution_target_kind is None:
            return "qualification environment evidence is unavailable"
        if (
            authorization.qualification_run_id_sha256
            != hashlib.sha256(self._run_id.encode("utf-8")).hexdigest()
        ):
            return "qualification run identity does not match its authorization"
        if execution_target_kind != project.environment.target_kind:
            return "qualification target kind does not match the project"

        configured_observer = self.qualification_environment_observer
        if configured_observer is None:
            if not callable(
                getattr(self.backend, "qualification_environment_identity", None)
            ):
                return (
                    "qualification backend cannot observe the complete environment; "
                    "configure the project's environment observer"
                )
            configured_observer = BackendQualificationEnvironmentObserver(self.backend)
        expected_observer = (
            project.environment.environment_observer_id,
            project.environment.environment_observer_contract_sha256,
        )
        try:
            observed_observer = (
                configured_observer.observer_id,
                configured_observer.contract_sha256,
            )
        except Exception:
            return "qualification environment observer identity is unavailable"
        if expected_observer != observed_observer:
            return "qualification environment observer does not match the project"

        def environment_call() -> QualificationEnvironmentObservation:
            try:
                before_observer = (
                    configured_observer.observer_id,
                    configured_observer.contract_sha256,
                )
            except Exception as exc:
                raise StructuralResolutionRefused(
                    "qualification environment observer binding is unavailable"
                ) from exc
            if before_observer != observed_observer:
                raise StructuralResolutionRefused(
                    "qualification environment observer binding changed before input"
                )
            current = configured_observer.observe(
                self.backend,
                project.environment.target_kind,
            )
            try:
                after_observer = (
                    configured_observer.observer_id,
                    configured_observer.contract_sha256,
                )
            except Exception as exc:
                raise StructuralResolutionRefused(
                    "qualification environment observer binding is unavailable"
                ) from exc
            if after_observer != before_observer:
                raise StructuralResolutionRefused(
                    "qualification environment observer binding changed during observation"
                )
            return current

        try:
            observed = environment_call()
        except Exception:
            return "qualification environment observation failed"
        application = observed.application_identity
        application_version = observed.application_version
        if observed.target_kind != project.environment.target_kind:
            return "qualification environment observer returned the wrong surface"
        session = observed.session_identity_sha256
        report.observed_application_sha256 = hashlib.sha256(
            application.encode("utf-8")
        ).hexdigest()
        report.observed_application_version_sha256 = hashlib.sha256(
            application_version.encode("utf-8")
        ).hexdigest()
        report.observed_session_sha256 = session
        report.observed_environment_digest = observed.environment_digest
        report.qualification_environment_observer_id = observed_observer[0]
        report.qualification_environment_observer_contract_sha256 = observed_observer[1]
        report.observed_environment_binding_sha256 = observed.binding_sha256(
            observer_id=observed_observer[0],
            observer_contract_sha256=observed_observer[1],
        )
        if project.environment.expected_application_identity is None:
            return (
                "qualification project has no executable application identity; "
                "set application_identity separately from the display name"
            )
        if application != project.environment.expected_application_identity:
            return "observed qualification application does not match the project"
        if application_version != project.environment.application_version:
            return (
                "observed qualification application version does not match the project"
            )
        if observed.environment_digest != project.environment.environment_digest:
            return (
                "observed qualification environment digest does not match the project"
            )
        self._qualification_environment_call = environment_call
        # The observer owns ``observed`` and can legally reuse or mutate that
        # object on a later call.  Keep an independent immutable-value
        # snapshot so such mutation cannot also change the expected side of
        # the input-edge comparison.
        self._qualification_environment_bound = observed.model_copy(deep=True)
        set_guard = getattr(self.backend, "set_qualification_input_guard", None)
        if not callable(set_guard):
            self._qualification_environment_call = None
            self._qualification_environment_bound = None
            return "qualification backend cannot bind an input-edge environment guard"
        set_guard(self._require_qualification_environment_current)
        return None

    def _require_qualification_environment_current(self) -> None:
        """Recheck the bound environment immediately before an input edge."""

        call = self._qualification_environment_call
        expected = self._qualification_environment_bound
        if call is None or expected is None:
            return
        try:
            observed = call()
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "qualification environment could not be observed before input"
            ) from exc
        if observed != expected:
            raise StructuralResolutionRefused(
                "qualification environment changed before input"
            )

    def _resettle_after_interstitial(
        self, before_png: bytes
    ) -> tuple[bytes, Optional[str]]:
        """Require a settled frame after every automatic dismissal.

        Interstitial handling is itself opt-in, so unlike the legacy entry-frame
        behavior there is no backwards-compatibility reason to accept a
        proceed-anyway settle timeout here. A transient blank frame could make a
        ``TEXT_ABSENT`` clearance pass immediately before the overlay returned.
        """
        fn = getattr(self.vision, "wait_settled_result", None)
        if not callable(fn):
            return before_png, (
                "The configured vision facade cannot verify that the screen "
                "settled after an interstitial dismissal - refusing to act; "
                "run aborted."
            )
        try:
            result = fn(
                self.backend,
                interval_s=self.poll_interval_s,
                timeout_s=self.settle_readiness_timeout_s,
            )
            frame = result.png
            settled = result.settled
            if not isinstance(frame, bytes) or not isinstance(settled, bool):
                raise TypeError("invalid settle result")
        except Exception as exc:
            return before_png, (
                "The screen readiness check failed after dismissing an "
                f"interstitial ({type(exc).__name__}) - refusing to act; run "
                "aborted."
            )
        if not settled:
            return frame, (
                "The screen did not settle after the interstitial dismissal "
                f"within {self.settle_readiness_timeout_s:.1f}s - refusing to "
                "verify clearance on a mid-transition frame or act beneath it; "
                "run aborted."
            )
        return frame, None

    def _handle_interstitials(
        self,
        step: Step,
        before_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        audit_events: list[InterstitialActionResult],
        workflow: Optional[Workflow],
    ) -> tuple[bytes, Optional[str]]:
        """Detect + handle KNOWN interstitials on the entry frame (model-free).

        A detected interstitial with an explicitly reversible,
        non-consequential dismissal (``dismiss_key`` / ``dismiss_anchor``) gets
        one audited action followed by its declared visual clearance check. A
        delivery/clearance failure, persistent detection, or a detected
        interstitial with NO dismissal HALTs the run GRACEFULLY naming it. Returns
        ``(frame, None)`` when the screen is clear, else ``(frame, halt_reason)``.

        Unknown / undeclared overlays are NOT guessed at here: one that covers
        the step's target still HALTs safely downstream (the anchor does not
        resolve). This method only automates the RECURRING, declared ones so
        they do not force a babysit-the-queue halt every time they appear.
        """
        mutation_error = self._interstitial_declaration_mutation(workflow)
        if mutation_error is not None:
            return before_png, mutation_error
        interstitials = [*self._workflow_interstitials, *self._interstitials]
        if not interstitials:
            return before_png, None

        # Revalidate serialized values at the point of use. Pydantic validates
        # construction/load, but models are intentionally mutable; an in-memory
        # edit or model_construct must not bypass affirmative detection, visual
        # clearance, Escape-only, or reversible/non-consequential policy.
        validated: list[Interstitial] = []
        for candidate in interstitials:
            try:
                validated.append(
                    Interstitial.model_validate(candidate.model_dump(mode="python"))
                )
            except Exception as exc:
                return before_png, (
                    f"Interstitial '{candidate.name}' has an invalid automatic "
                    f"dismissal declaration ({type(exc).__name__}) - refusing "
                    "to emit an action; run aborted."
                )
        interstitials = validated

        handled_indices: set[int] = set()
        while True:
            mutation_error = self._interstitial_declaration_mutation(workflow)
            if mutation_error is not None:
                return before_png, mutation_error
            active_index = next(
                (
                    index
                    for index, candidate in enumerate(interstitials)
                    if self._predicate_holds(
                        candidate.detect,
                        before_png,
                        bundle_dir,
                        params,
                        workflow=workflow,
                    )
                ),
                None,
            )
            # Detection invokes vision and can therefore cross a callback
            # boundary. Recheck the caller-owned declarations before trusting
            # its result or preparing an action.
            mutation_error = self._interstitial_declaration_mutation(workflow)
            if mutation_error is not None:
                return before_png, mutation_error
            if active_index is None:
                return before_png, None
            it = interstitials[active_index]
            if active_index in handled_indices:
                return before_png, (
                    f"Interstitial '{it.name}' reappeared after a verified "
                    "dismissal in the same step - refusing an automatic retry; "
                    "run aborted."
                )
            if len(handled_indices) >= len(interstitials):
                return before_png, (
                    "The interstitial dismissal bound was exhausted before the "
                    "step entry state became clear - refusing further automatic "
                    "actions; run aborted."
                )
            if it.dismiss_key is None and it.dismiss_anchor is None:
                # A known BLOCKING interstitial with no safe auto-dismissal.
                return before_png, (
                    f"Known blocking interstitial '{it.name}' is on screen "
                    f"({self._describe_predicate(it.detect)}) and declares no "
                    "automatic dismissal - refusing to act beneath it; run "
                    "aborted (handle it, then re-run)."
                )
            if (
                it.risk != "reversible"
                or it.consequential is not False
                or it.clearance is None
                or (it.dismiss_key is not None and it.dismiss_key != "Escape")
            ):
                # Defensive runtime check in addition to Pydantic validation:
                # model_construct/in-memory mutation must never turn this into
                # an ungoverned consequential or unverifiable action.
                return before_png, (
                    f"Interstitial '{it.name}' does not declare an automatic "
                    "dismissal that is reversible, non-consequential, and "
                    "visually verifiable - refusing to emit an action; run "
                    "aborted."
                )

            resolution: Optional[Resolution] = None
            action: Literal["key", "click"]
            if it.dismiss_key is not None:
                action = "key"
            else:
                assert it.dismiss_anchor is not None
                structural_only = (
                    not it.dismiss_anchor.template.strip()
                    and it.dismiss_anchor.structural is not None
                )
                structural = (
                    self.backend
                    if self.use_structural
                    and hasattr(self.backend, "locate_structural")
                    else None
                )
                if structural_only and structural is None:
                    return before_png, (
                        f"Interstitial '{it.name}' requires a structural-only "
                        "dismissal, but this backend cannot perform structural "
                        "resolution - refusing an OCR/geometry/coordinate "
                        "downgrade; run aborted."
                    )
                template_png = self._asset_bytes(
                    bundle_dir,
                    it.dismiss_anchor.template,
                    workflow=workflow,
                )
                if self._governed_asset_mutation is not None:
                    return before_png, self._governed_asset_mutation
                res = resolve(
                    it.dismiss_anchor,
                    before_png,
                    self.vision,
                    None,  # NEVER ground a dismissal: stay model-free
                    it.name,
                    template_png=template_png,
                    viewport=_frame_viewport(before_png),
                    structural=structural,
                )
                if res is None:
                    return before_png, (
                        f"Interstitial '{it.name}' detected "
                        f"({self._describe_predicate(it.detect)}) but its dismiss "
                        "control did not resolve on screen - refusing to act; run "
                        "aborted."
                    )
                resolution, _region = res
                if structural_only and resolution.rung != "structural":
                    return before_png, (
                        f"Interstitial '{it.name}' structural dismissal did not "
                        "resolve uniquely - refusing an OCR/geometry/coordinate "
                        "downgrade; run aborted."
                    )
                action = "click"

            # Resolution can invoke vision or a structural backend.  A callback
            # must not be able to replace the declaration between authorization
            # and the audit-before-delivery boundary.
            mutation_error = self._interstitial_declaration_mutation(workflow)
            if mutation_error is not None:
                return before_png, mutation_error
            # Append BEFORE delivery: a backend exception can never create an
            # unreported key/click attempt. The event carries the exact admitted
            # policy and expected visual outcome.
            event = InterstitialActionResult(
                interstitial=it.name,
                action=action,
                key=it.dismiss_key,
                expected_clearance=it.clearance,
                resolution=resolution,
                before_frame_sha256=hashlib.sha256(before_png).hexdigest(),
            )
            audit_events.append(event)
            handled_indices.add(active_index)
            if workflow is not None:
                authorization_error = self._delivery_authorization_refusal(
                    workflow, params, step, result
                )
                if authorization_error is not None:
                    event.error = authorization_error
                    return before_png, authorization_error
            elif self._durable_continuation_guard is not None:
                # Normal replay always supplies ``workflow``. Keep the shared
                # continuation fence fail-closed for direct helper/test use as
                # well: every backend input edge must cross the same lease.
                try:
                    self._durable_continuation_guard.before_delivery()
                    self._active_delivery_acknowledgers = (
                        self._durable_continuation_guard,
                    )
                except Exception as exc:  # noqa: BLE001 - fencing boundary
                    result.failure_category = "continuation_preempted"
                    result.safety_halt = True
                    event.error = (
                        f"durable continuation was preempted before delivery: {exc}"
                    )
                    return before_png, event.error
            try:
                if it.dismiss_key is not None:
                    self._require_qualification_environment_current()
                    self._deliver_backend_call(
                        result, lambda: self.backend.press(it.dismiss_key or "Escape")
                    )
                else:
                    assert resolution is not None
                    assert it.dismiss_anchor is not None
                    if resolution.rung == "structural":
                        native_act = getattr(self.backend, "act_structural", None)
                        structural_anchor = it.dismiss_anchor.structural
                        if (
                            not callable(native_act)
                            or resolution.structural_handle is None
                            or structural_anchor is None
                        ):
                            raise StructuralResolutionRefused(
                                "structural dismissal cannot be delivered "
                                "natively without a stable element handle"
                            )
                        self._require_qualification_environment_current()
                        self._deliver_backend_call(
                            result,
                            lambda: native_act(
                                structural_anchor,
                                resolution.structural_handle,
                            ),
                        )
                    else:
                        self._require_qualification_environment_current()
                        self._deliver_backend_call(
                            result,
                            lambda: self.backend.click(
                                int(resolution.point[0]), int(resolution.point[1])
                            ),
                        )
                event.delivered = True
            except Exception as exc:
                if not isinstance(
                    exc, (FreshActuationRequired, StructuralResolutionRefused)
                ):
                    target_fingerprint = None
                    if (
                        resolution is not None
                        and resolution.structural_handle is not None
                    ):
                        target_fingerprint = (
                            resolution.structural_handle.target_fingerprint
                        )
                    result.delivery_uncertainty = ActionDeliveryUncertainty(
                        operation="interstitial_dismiss",
                        native=(
                            resolution is not None and resolution.rung == "structural"
                        ),
                        target_fingerprint=target_fingerprint,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        cause_type=type(exc).__name__,
                    )
                event.error = (
                    "dismissal delivery failed "
                    f"({type(exc).__name__}); outcome is unknown"
                )
                return before_png, (
                    f"Interstitial '{it.name}' dismissal delivery failed "
                    f"({type(exc).__name__}) - refusing to act beneath it; run "
                    "aborted."
                )

            before_png, settle_error = self._resettle_after_interstitial(before_png)
            event.after_frame_sha256 = hashlib.sha256(before_png).hexdigest()
            if settle_error is not None:
                event.error = settle_error
                return before_png, settle_error

            clearance_holds = self._predicate_holds(
                it.clearance,
                before_png,
                bundle_dir,
                params,
                workflow=workflow,
            )
            if self._governed_asset_mutation is not None:
                event.clearance_ok = False
                event.error = self._governed_asset_mutation
                return before_png, self._governed_asset_mutation
            still_detected = False
            if clearance_holds:
                still_detected = self._predicate_holds(
                    it.detect,
                    before_png,
                    bundle_dir,
                    params,
                    workflow=workflow,
                )
                if self._governed_asset_mutation is not None:
                    event.clearance_ok = False
                    event.error = self._governed_asset_mutation
                    return before_png, self._governed_asset_mutation
            event.clearance_ok = clearance_holds and not still_detected
            event.ok = event.delivered and event.clearance_ok
            if not event.ok:
                event.error = (
                    "declared visual clearance did not hold"
                    if not clearance_holds
                    else "interstitial remained detected after dismissal"
                )
                return before_png, (
                    f"Interstitial '{it.name}' did not satisfy its declared "
                    "visual clearance after one dismissal - refusing a blind "
                    "retry or action beneath it; run aborted."
                )

    # -- Workflow-program IR gates: guard + wait_until --------------------------

    def _apply_step_gates(
        self,
        step: Step,
        before_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        workflow: Optional[Workflow] = None,
    ) -> tuple[bool, Optional[str], bytes]:
        """Evaluate readiness, interstitials, guard, then wait_until before acting.

        Runs in state-dependency order, all model-free:

        1. **readiness** (``require_settled``) - refuse to act on a frame that
           never settled (still-loading / mid-transition); HALT gracefully.
        2. **interstitials** - a KNOWN reversible, non-consequential overlay on
           the entry frame gets one audited dismissal plus declared clearance
           verification, or HALTs before the workflow action.
        3. **guard** (precondition) on the entry frame - unmet HALTs (default)
           or SKIPs (``on_unmet="skip"``).
        4. **wait_until** (readiness predicate) - polled, bounded, HALT on
           timeout (never proceed-anyway).

        Returns ``(proceed, error, before_png)``:

        - ``(True, None, frame)`` - proceed to resolve/act (frame may have been
          re-settled while polling a wait_until predicate).
        - ``(False, None, frame)`` - the guard was unmet with ``on_unmet="skip"``:
          the step is a no-op success (caller marks it ``skipped``).
        - ``(False, error, frame)`` - HALT: an un-ready screen, a blocking /
          undismissable interstitial, an unmet ``halt`` guard, or a
          ``wait_until`` predicate that never held within its bound (fail-safe;
          the run NEVER proceeds-anyway on an unmet readiness predicate).

        Model-free: predicates are evaluated by :meth:`_predicate_holds`.
        """
        if workflow is not None:
            profile_refusal = self._profile_runtime_refusal(workflow)
            if profile_refusal is not None:
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                return (
                    False,
                    f"{profile_refusal} at the actuation boundary — refusing "
                    "to act; run aborted",
                    before_png,
                )
        # (1) Readiness: never act on a frame that never settled.
        before_png, readiness_error, settled = self._wait_starting_state_settled(
            before_png
        )
        result.starting_state_settled = settled
        if readiness_error is not None:
            # Like an interstitial refusal, a not-ready starting state is a
            # safety halt. A program on_exception edge must not convert the
            # explicit refusal into a successful terminal outcome.
            result.safety_halt = True
            return False, readiness_error, before_png

        # (2) Interstitials: dismiss a KNOWN overlay, or HALT gracefully.
        before_png, interstitial_error = self._handle_interstitials(
            step,
            before_png,
            bundle_dir,
            params,
            result,
            result.interstitial_actions,
            workflow,
        )
        if interstitial_error is not None:
            # An interstitial refusal is a state-safety halt, not an ordinary
            # workflow exception a program on_exception edge may turn into a
            # successful outcome after an uncertain/blocked pre-action state.
            result.safety_halt = True
            return False, interstitial_error, before_png

        # Guard (precondition) on the entry frame.
        guard_holds = step.guard is None or self._predicate_holds(
            step.guard.predicate,
            before_png,
            bundle_dir,
            params,
            workflow=workflow,
        )
        if self._governed_asset_mutation is not None:
            return False, self._governed_asset_mutation, before_png
        if not guard_holds:
            assert step.guard is not None
            if step.guard.on_unmet == "skip":
                return False, None, before_png
            return (
                False,
                (
                    f"Guard precondition for step '{step.id}' ({step.intent}) is "
                    f"unmet on the current screen "
                    f"({self._describe_predicate(step.guard.predicate)}) — "
                    "refusing to act; run aborted"
                ),
                before_png,
            )

        # wait_until (readiness). SCROLL consumes its own predicate as the stop
        # condition of its closed loop (_act_scroll), so it is skipped here.
        if step.wait_until is not None and step.action is not ActionKind.SCROLL:
            pred = step.wait_until
            deadline = time.monotonic() + pred.timeout_s
            while True:
                if self._predicate_holds(
                    pred,
                    before_png,
                    bundle_dir,
                    params,
                    workflow=workflow,
                ):
                    return True, None, before_png
                if time.monotonic() >= deadline:
                    return (
                        False,
                        (
                            f"wait_until predicate for step '{step.id}' "
                            f"({step.intent}) did not hold within "
                            f"{pred.timeout_s:.1f}s "
                            f"({self._describe_predicate(pred)}) — readiness never "
                            "reached; refusing to proceed-anyway; run aborted"
                        ),
                        before_png,
                    )
                time.sleep(self.poll_interval_s)
                before_png = self.vision.wait_settled(self.backend)

        return True, None, before_png

    def _predicate_holds(
        self,
        pred: Predicate,
        frame_png: bytes,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        *,
        workflow: Optional[Workflow] = None,
    ) -> bool:
        """Evaluate a Predicate against the current frame / run params.

        Deterministic and ZERO model calls (the $0 runtime guarantee):
        ``anchor_resolves`` runs the resolution ladder with NO grounder,
        ``text_present`` / ``text_absent`` use the tolerant OCR presence check,
        ``param_equals`` is a string compare, and ``and`` / ``or`` / ``not``
        compose. An unknown kind fails safe (does not hold).
        """
        return evaluate_program_predicate(
            pred,
            frame_png,
            runtime_params_for_gui(params),
            vision=self.vision,
            viewport=_frame_viewport(frame_png),
            asset_loader=lambda rel: self._asset_bytes(
                bundle_dir,
                rel,
                workflow=workflow,
            ),
        )

    @staticmethod
    def _describe_predicate(pred: Predicate) -> str:
        """Human-readable one-liner for a predicate (for HALT messages)."""
        kind = pred.kind.value if hasattr(pred.kind, "value") else pred.kind
        if kind == "anchor_resolves":
            label = (
                pred.intent
                or (pred.anchor.ocr_text if pred.anchor else None)
                or "target"
            )
            return f"anchor_resolves({label!r})"
        if kind in ("text_present", "text_absent"):
            return f"{kind}({pred.text!r})"
        if kind == "param_equals":
            return f"param_equals({pred.param!r}=={pred.value!r})"
        if kind in ("and", "or", "not"):
            inner = ", ".join(Replayer._describe_predicate(op) for op in pred.operands)
            return f"{kind}({inner})"
        return str(kind)

    # -- identity verification (pre-click) --------------------------------------

    @staticmethod
    def _qualified_signal_evidence(
        signal: Any,
        verdict: Literal["verified", "conflict", "unverifiable"],
    ) -> IdentitySignalEvidence:
        source = signal.source.value
        evidence_class = cast(
            Literal[
                "application_structured_text",
                "recorded_and_live_region",
                "captured_context_ocr",
                "application_identity",
                "session_identity",
                "workflow_state_identity",
            ],
            {
                "structured": "application_structured_text",
                "identifier_region": "recorded_and_live_region",
                "captured_context": "captured_context_ocr",
                "application": "application_identity",
                "session": "session_identity",
                "workflow_state": "workflow_state_identity",
            }[source],
        )
        return IdentitySignalEvidence(
            signal=signal.key.value,
            source=source,
            verdict=verdict,
            evidence_class=evidence_class,
            match=signal.match.value,
        )

    @staticmethod
    def _compare_qualified_signal_text(
        *,
        signal: Any,
        anchor: Anchor,
        live: Optional[str],
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
    ) -> Literal["verified", "conflict", "unverifiable"]:
        """Compare one live source without returning or logging identity values."""

        if live is None or not live.strip():
            return "unverifiable"
        source = signal.source.value
        recorded = (
            anchor.structured_identity
            if source == "structured"
            else anchor.context_text
        )
        extract_pattern = getattr(signal, "extract_pattern", None)
        if extract_pattern is not None:
            # Field labels are presentation text and may change case under an
            # explicitly normalized policy. Preserve the captured value's
            # original characters so the configured match mode, not the
            # extractor, decides whether values compare equal.
            compiled = re.compile(extract_pattern, re.IGNORECASE)
            live_match = compiled.search(live)
            if live_match is None:
                return "unverifiable"
            live = live_match.group("value")
            if recorded is not None:
                recorded_match = compiled.search(recorded)
                if recorded_match is None:
                    return "unverifiable"
                recorded = recorded_match.group("value")
        if recorded is not None:
            expected_form, parameter_names = parameterize_identity_text(
                recorded,
                workflow.params,
                names=signal.params,
                minimum_chars=identity_mod.MIN_PARAM_CHARS,
                case_sensitive=True,
            )
            if set(parameter_names) != set(signal.params):
                return "unverifiable"
            live_values = {**workflow.params, **runtime_params_for_gui(params)}
            live_form, used = parameterize_identity_text(
                live,
                live_values,
                names=parameter_names,
                minimum_chars=identity_mod.MIN_PARAM_CHARS,
                case_sensitive=not (
                    signal.match.value == "normalized"
                    and "casefold"
                    in {getattr(item, "value", item) for item in signal.normalizers}
                ),
            )
            if set(used) != set(parameter_names):
                return "conflict"
            if signal.match.value == "normalized":
                expected_form = normalize_signal_text(expected_form, signal.normalizers)
                live_form = normalize_signal_text(live_form, signal.normalizers)
            if expected_form != live_form:
                return "conflict"
            if source == "captured_context" and (
                identity_mod.identity_rests_on_confusable_identifier(recorded)
                or identity_mod.identity_rests_on_confusable_identifier(live)
            ):
                return "unverifiable"
            return "verified"

        from openadapt_flow.runtime import identity_template as itmpl

        result = itmpl.verify_signal_template(
            anchor.identity_template,
            source=source,
            match=signal.match.value,
            normalizers=signal.normalizers,
            live=live,
            params=runtime_params_for_gui(params),
            param_examples=workflow.params,
            parameter_names=signal.params,
            extract_pattern=extract_pattern,
        )
        if result is None:
            return "unverifiable"
        if (
            result
            and source == "captured_context"
            and anchor.identity_template is not None
            and anchor.identity_template.rests_on_confusable_identifier
        ):
            return "unverifiable"
        return "verified" if result else "conflict"

    def _verify_dedicated_identity_signal(
        self,
        *,
        source: str,
        signal: Any,
        workflow: Workflow,
    ) -> Literal["verified", "conflict", "unverifiable"]:
        """Compare a non-record identity against its dedicated live observer."""

        qualification = workflow.qualification
        if qualification is None:
            return "unverifiable"
        expected = signal.expected_value
        if not isinstance(expected, str):
            return "unverifiable"
        getter = getattr(self.backend, f"{source}_identity", None)
        live: Optional[str] = None
        if callable(getter):
            try:
                observed = getter()
                live = observed if isinstance(observed, str) else None
            except Exception:
                live = None
        if not live:
            return "unverifiable"
        if signal.match.value == "normalized":
            expected = normalize_signal_text(expected, signal.normalizers)
            live = normalize_signal_text(live, signal.normalizers)
        return "verified" if expected == live else "conflict"

    def _captured_context_observations(
        self,
        step: Step,
        resolution: Resolution,
        before_png: bytes,
        qualified_region: Region,
    ) -> list[str]:
        """Read one explicitly qualified context region at native and 2x scale."""

        assert step.anchor is not None
        anchor = step.anchor
        band = (
            resolution.point[0] + (qualified_region[0] - anchor.click_point[0]),
            resolution.point[1] + (qualified_region[1] - anchor.click_point[1]),
            qualified_region[2],
            qualified_region[3],
        )
        exclude = (
            resolution.point[0] + (anchor.region[0] - anchor.click_point[0]),
            resolution.point[1] + (anchor.region[1] - anchor.click_point[1]),
            anchor.region[2],
            anchor.region[3],
        )
        today = date.today()

        def observe(
            png: bytes,
            region: Optional[Region],
            point_y: int,
            exclude_region: Region,
        ) -> str:
            lines = [
                line
                for line in self.vision.ocr(png, region=region)
                if line.text.strip()
                and not identity_mod.regions_intersect(line.region, exclude_region)
                and not identity_mod.is_volatile_line(line.text, reference_date=today)
            ]
            lines = identity_mod.lines_near_point(lines, point_y)
            return " ".join(line.text.strip() for line in lines)

        observations = [observe(before_png, band, resolution.point[1], exclude)]
        upscaled = identity_mod.upscale_crop(before_png, band)
        if upscaled is not None:
            factor = 2
            local_exclude = (
                (exclude[0] - band[0]) * factor,
                (exclude[1] - band[1]) * factor,
                exclude[2] * factor,
                exclude[3] * factor,
            )
            observations.append(
                observe(
                    upscaled,
                    None,
                    (resolution.point[1] - band[1]) * factor,
                    local_exclude,
                )
            )
        return observations

    def _verify_signal_quorum(
        self,
        *,
        step: Step,
        resolution: Resolution,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Optional[Path],
        policy: Any,
    ) -> IdentityCheck:
        """Enforce one qualified signal quorum on the fresh pre-action frame."""

        from openadapt_flow.qualification import (
            identity_policy_independence_errors,
            identity_signal_runtime_available,
        )

        anchor = step.anchor
        assert anchor is not None
        evidence: list[IdentitySignalEvidence] = []
        if identity_policy_independence_errors(policy):
            evidence = [
                self._qualified_signal_evidence(signal, "unverifiable")
                for signal in policy.signals
            ]
            return IdentityCheck(
                status="unreadable",
                mode="signal_quorum",
                signal_evidence=evidence,
                quorum_required=policy.quorum,
                quorum_verified=0,
            )

        for signal in policy.signals:
            if not identity_signal_runtime_available(step, signal):
                evidence.append(self._qualified_signal_evidence(signal, "unverifiable"))
                continue
            source = signal.source.value
            if source == "structured":
                getter = getattr(self.backend, "structured_text_at", None)
                live = None
                if getter is not None:
                    try:
                        live = getter(
                            int(resolution.point[0]), int(resolution.point[1])
                        )
                    except Exception:
                        live = None
                verdict = self._compare_qualified_signal_text(
                    signal=signal,
                    anchor=anchor,
                    live=live,
                    params=params,
                    workflow=workflow,
                )
            elif source in {"application", "session", "workflow_state"}:
                verdict = self._verify_dedicated_identity_signal(
                    source=source,
                    signal=signal,
                    workflow=workflow,
                )
            elif source == "captured_context":
                assert signal.region is not None
                attempts = self._captured_context_observations(
                    step, resolution, before_png, signal.region
                )
                verdicts = [
                    self._compare_qualified_signal_text(
                        signal=signal,
                        anchor=anchor,
                        live=observed,
                        params=params,
                        workflow=workflow,
                    )
                    for observed in attempts
                    if observed.strip()
                ]
                if not verdicts:
                    verdict = "unverifiable"
                elif "verified" in verdicts and "conflict" in verdicts:
                    verdict = "unverifiable"
                elif "verified" in verdicts:
                    verdict = "verified"
                elif all(item == "conflict" for item in verdicts):
                    verdict = "conflict"
                else:
                    verdict = "unverifiable"
            else:
                recorded_png, live_png = self._identifier_crops(
                    anchor,
                    resolution,
                    before_png,
                    bundle_dir,
                    workflow=workflow,
                )
                recorded_text = self._ocr_identity_crop(recorded_png)
                live_text = self._ocr_identity_crop(live_png)
                text_verdict, parameterized = self._compare_direct_signal_text(
                    signal,
                    recorded_text,
                    live_text,
                    params=params,
                    workflow=workflow,
                )
                pixel = identity_mod.verify_pixel_identity(
                    recorded_png,
                    live_png,
                    enable_verify=True,
                )
                if text_verdict == "conflict":
                    verdict = "conflict"
                elif text_verdict == "unverifiable":
                    verdict = "unverifiable"
                elif parameterized:
                    # A run-bound record value is expected to render different
                    # pixels than the demonstration. Exact/explicitly normalized
                    # OCR may verify it only when the expected value does not
                    # occupy the known glyph-confusable identifier class.
                    live_values = {
                        **workflow.params,
                        **runtime_params_for_gui(params),
                    }
                    vulnerable = any(
                        identity_mod.identity_rests_on_confusable_identifier(
                            live_values.get(name)
                        )
                        for name in parameterized
                    )
                    verdict = "unverifiable" if vulnerable else "verified"
                elif pixel is not None and pixel.status == "mismatch":
                    verdict = "conflict"
                elif pixel is None or pixel.status != "verified":
                    verdict = "unverifiable"
                else:
                    verdict = "verified"
            evidence.append(self._qualified_signal_evidence(signal, verdict))

        conflicts = [item for item in evidence if item.verdict == "conflict"]
        verified = sum(item.verdict == "verified" for item in evidence)
        status: Literal["verified", "mismatch", "unreadable"] = (
            "mismatch"
            if conflicts
            else "verified"
            if verified >= policy.quorum
            else "unreadable"
        )
        return IdentityCheck(
            status=status,
            mode="signal_quorum",
            coverage=(verified / len(evidence)) if evidence else 0.0,
            signal_evidence=evidence,
            quorum_required=policy.quorum,
            quorum_verified=verified,
        )

    def _ocr_identity_crop(self, png: Optional[bytes]) -> Optional[str]:
        if not png:
            return None
        try:
            lines = [line for line in self.vision.ocr(png) if line.text.strip()]
        except Exception:
            return None
        if not lines:
            return None
        lines.sort(key=lambda line: (line.region[1], line.region[0]))
        return " ".join(line.text.strip() for line in lines)

    @staticmethod
    def _compare_direct_signal_text(
        signal: Any,
        recorded: Optional[str],
        live: Optional[str],
        *,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
    ) -> tuple[
        Literal["verified", "conflict", "unverifiable"],
        list[str],
    ]:
        if not recorded or not live:
            return "unverifiable", []
        recorded, parameter_names = parameterize_identity_text(
            recorded,
            workflow.params,
            names=signal.params,
            minimum_chars=identity_mod.MIN_PARAM_CHARS,
            case_sensitive=True,
        )
        if set(parameter_names) != set(signal.params):
            return "unverifiable", signal.params
        live, used = parameterize_identity_text(
            live,
            {**workflow.params, **runtime_params_for_gui(params)},
            names=parameter_names,
            minimum_chars=identity_mod.MIN_PARAM_CHARS,
            case_sensitive=not (
                signal.match.value == "normalized"
                and "casefold"
                in {getattr(item, "value", item) for item in signal.normalizers}
            ),
        )
        if set(used) != set(parameter_names):
            return "conflict", parameter_names
        if signal.match.value == "normalized":
            recorded = normalize_signal_text(recorded, signal.normalizers)
            live = normalize_signal_text(live, signal.normalizers)
        return (
            "verified" if recorded == live else "conflict",
            parameter_names,
        )

    def _verify_identity(
        self,
        step: Step,
        resolution: Resolution,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Optional[Path] = None,
    ) -> IdentityCheck:
        """Verify the resolved target's identity via the identity LADDER.

        Identity is checked by an ordered ladder of verifier tiers, highest
        fidelity first; the first tier that can judge this substrate wins and
        its verdict is FINAL (see
        :func:`openadapt_flow.runtime.identity.run_identity_ladder`):

        - **tier 1 -- structured text (DOM / UIA / AX).** When the bundle
          carries the target's recorded structured identity
          (``anchor.structured_identity``) AND the live backend exposes
          ``structured_text_at`` (browser DOM, or native a11y tree), identity
          is verified by an exact/normalized compare of the recorded vs live
          structured text at the resolved point -- O and 0 are distinct
          characters, so the same-name/same-DOB glyph-collapse that defeats
          OCR cannot occur, and the class closes with no OCR-availability
          cost. A mismatch here is authoritative: no lower tier overrides it.
        - **tier 2 -- pixel-compare identifier crop.** For pure-pixel
          substrates (Citrix/RDP/VDI, broken a11y) that carry a recorded
          identifier crop (``anchor.identifier_crop`` / ``identifier_region``)
          but no structured text: compare the recorded identifier crop's
          pixels to the live crop re-cut at the resolved point. OCR collapses
          O/0 and l/1, the pixels do not. VERIFIES on a matching render,
          MISMATCHES on a localized glyph change, ABSTAINS under render drift
          (see :func:`identity.verify_pixel_identity`).
        - **tier 3 -- local-VLM veto (OPTIONAL, off by default).** Only when a
          verifier is injected (``self.identity_vlm``), identity rests on a
          glyph-confusable identifier, AND the cheaper tiers abstained: a
          local open VLM answers same/different, VETO-ONLY (different/unsure
          -> halt). See :func:`identity.verify_vlm_identity`.
        - **tier N -- OCR name+DOB-primary band (#27).** The pixel-substrate
          fallback: :meth:`_verify_identity_ocr`, with its proven-irreducible
          same-name/same-DOB residual that HALTS on the sole-ambiguous-
          identifier case (docs/LIMITS.md).

        Returns the first definitive tier verdict; ``unreadable`` if no tier
        could judge identity.
        """
        anchor = step.anchor
        assert anchor is not None
        project = getattr(workflow, "qualification", None)
        if project is not None:
            identity_policy = project.identity_policies.get(step.id)
            if (
                identity_policy is not None
                and identity_policy.enforcement.value == "signal_quorum"
            ):
                return self._verify_signal_quorum(
                    step=step,
                    resolution=resolution,
                    before_png=before_png,
                    params=runtime_params_for_gui(params),
                    workflow=workflow,
                    bundle_dir=bundle_dir,
                    policy=identity_policy,
                )

        def structured_tier() -> Optional[IdentityCheck]:
            # PHI-free bundles carry a salted-hash identity_template instead of
            # the plaintext structured_identity; older bundles carry the
            # plaintext. Prefer whichever is present (audit REM-2).
            tmpl = anchor.identity_template
            has_template_structured = tmpl is not None and tmpl.structured
            recorded = anchor.structured_identity
            if not recorded and not has_template_structured:
                return None
            getter = getattr(self.backend, "structured_text_at", None)
            if getter is None:
                return None
            try:
                live = getter(int(resolution.point[0]), int(resolution.point[1]))
            except Exception:
                live = None
            if has_template_structured:
                from openadapt_flow.runtime import identity_template as itmpl

                return itmpl.verify_structured_template(
                    tmpl,
                    live,
                    params=runtime_params_for_gui(params),
                    param_examples=workflow.params,
                )
            return identity_mod.verify_structured_identity(recorded, live)

        # Recorded + live identifier crops, shared by the pixel and VLM tiers.
        # Cut lazily and cached: unavailable (no recorded crop, or the live
        # frame can't be cut) => both tiers abstain and the ladder falls to
        # OCR. Cost is paid once even though two tiers may read it.
        _crops: dict[str, Optional[bytes]] = {}

        def identifier_crops() -> tuple[Optional[bytes], Optional[bytes]]:
            if "rec" not in _crops:
                _crops["rec"], _crops["live"] = self._identifier_crops(
                    anchor, resolution, before_png, bundle_dir, workflow=workflow
                )
            return _crops["rec"], _crops["live"]

        def pixel_tier() -> Optional[IdentityCheck]:
            recorded_png, live_png = identifier_crops()
            return identity_mod.verify_pixel_identity(
                recorded_png,
                live_png,
                enable_verify=self.pixel_verify_enabled,
            )

        def vlm_tier() -> Optional[IdentityCheck]:
            recorded_png, live_png = identifier_crops()
            # Only spend the VLM where identity rests on a glyph-confusable
            # identifier -- read from whatever identity evidence the bundle has
            # (the PHI-free template precomputes this flag; older bundles read
            # it from the plaintext identity text).
            if anchor.identity_template is not None:
                confusable = anchor.identity_template.rests_on_confusable_identifier
            else:
                confusable = identity_mod.identity_rests_on_confusable_identifier(
                    anchor.structured_identity or anchor.context_text
                )
            return identity_mod.verify_vlm_identity(
                recorded_png,
                live_png,
                verifier=self.identity_vlm,
                glyph_confusable=confusable,
            )

        def ocr_tier() -> Optional[IdentityCheck]:
            tmpl = anchor.identity_template
            has_template_band = tmpl is not None and bool(tmpl.tokens)
            if not anchor.context_text and not has_template_band:
                return None
            return self._verify_identity_ocr(
                step, resolution, before_png, params, workflow
            )

        return identity_mod.run_identity_ladder(
            [structured_tier, pixel_tier, vlm_tier, ocr_tier]
        )

    def _identifier_crops(
        self,
        anchor: Anchor,
        resolution: Resolution,
        before_png: bytes,
        bundle_dir: Optional[Path],
        *,
        workflow: Optional[Workflow] = None,
    ) -> tuple[Optional[bytes], Optional[bytes]]:
        """(recorded, live) identifier-crop PNGs for the pixel/VLM tiers.

        The recorded crop is ``anchor.identifier_crop`` from the bundle (read
        through the ENCRYPTED bundle's in-memory decrypted store when the
        crop is sealed at rest -- identifier pixels are PHI and never land
        cleartext on disk for an encrypted bundle); the live crop is the
        same-sized box re-cut from ``before_png`` at
        ``anchor.identifier_region`` translated to the RESOLVED point (the
        same offset the region had from the recorded click point -- the OCR
        exclude region is translated identically). Returns (None, None) when
        the bundle carries no identifier crop/region or the frame can't be
        cut, so the tiers abstain.
        """
        if (
            bundle_dir is None
            or not anchor.identifier_crop
            or anchor.identifier_region is None
        ):
            return None, None
        recorded_png = self._asset_bytes(
            bundle_dir, anchor.identifier_crop, workflow=workflow
        )
        if recorded_png is None:
            return None, None
        rx, ry, rw, rh = anchor.identifier_region
        live_region = (
            resolution.point[0] + (rx - anchor.click_point[0]),
            resolution.point[1] + (ry - anchor.click_point[1]),
            rw,
            rh,
        )
        live_png = identity_mod.crop_region(before_png, live_region)
        return recorded_png, live_png

    def _verify_identity_ocr(
        self,
        step: Step,
        resolution: Resolution,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
    ) -> IdentityCheck:
        """OCR name+DOB-primary identity tier (the pixel-substrate fallback).

        Verify the resolved target's identity via its live OCR context band.

        OCRs the full-width band around the RESOLVED click point (the
        recorded crop's height as a coarse window), keeps only the lines of
        the point's OWN text row (the 64px crop height spans 2-3 rows of a
        dense table — a one-row-off resolution must not verify on text
        bleed from the adjacent true row; see
        :func:`openadapt_flow.runtime.identity.lines_near_point`), and
        compares them to the anchor's recorded ``context_text`` (see
        :mod:`openadapt_flow.runtime.identity` for the matching rules and
        the param-mode substitution). Dense small text is undercounted by
        OCR at native resolution, so a non-verified first pass is retried
        once at 2x resolution before the verdict.

        Returns:
            The best :class:`IdentityCheck` across the two attempts.
        """
        assert step.anchor is not None and (
            step.anchor.context_text
            or (
                step.anchor.identity_template is not None
                and step.anchor.identity_template.tokens
            )
        )
        anchor = step.anchor
        identifier_region: Optional[Region] = None
        if anchor.identifier_region is not None:
            rx, ry, rw, rh = anchor.identifier_region
            identifier_region = (
                resolution.point[0] + (rx - anchor.click_point[0]),
                resolution.point[1] + (ry - anchor.click_point[1]),
                rw,
                rh,
            )
        band = identity_mod.band_region(
            resolution.point, anchor.region[3], self.backend.viewport
        )
        # The recorded band was extracted EXCLUDING the target's own crop
        # (labels are mutable evidence the ladder heals through) and
        # excluding volatile lines. The live band must mirror both, or
        # the target's own label / a live clock cell shows up as
        # observed-side tokens the recorded band cannot explain and trips
        # the unexplained-name budget on every correct row. The exclude
        # region is the anchor crop translated to the RESOLVED point
        # (same offset it had from the recorded click point); volatility
        # is judged against the replay date — chronology near NOW is
        # volatile, a far date (a DOB) is identity evidence, exactly as
        # at record time.
        exclude = (
            resolution.point[0] + (anchor.region[0] - anchor.click_point[0]),
            resolution.point[1] + (anchor.region[1] - anchor.click_point[1]),
            anchor.region[2],
            anchor.region[3],
        )
        today = date.today()

        def attempt(
            png: bytes,
            ocr_region: Optional[Region],
            point_y: int,
            exclude_region: Region,
            *,
            scoped_identifier: bool = False,
            identifier_filter: Optional[Region] = None,
        ) -> IdentityCheck:
            lines = self.vision.ocr(png, region=ocr_region)
            if scoped_identifier:
                observed = (
                    identity_mod.identifier_text_from_lines(
                        lines,
                        region=identifier_filter,
                        reference_date=today,
                    )
                    or ""
                )
            else:
                lines = [
                    line
                    for line in lines
                    if line.text.strip()
                    and not identity_mod.regions_intersect(line.region, exclude_region)
                    and not identity_mod.is_volatile_line(
                        line.text, reference_date=today
                    )
                ]
                lines = identity_mod.lines_near_point(lines, point_y)
                observed = " ".join(line.text.strip() for line in lines)
            if anchor.identity_template is not None and anchor.identity_template.tokens:
                # PHI-free path: verify against the salted-hash template (audit
                # REM-2). Same three-way verdict, no plaintext band stored.
                from openadapt_flow.runtime import identity_template as itmpl

                return itmpl.verify_template_identity(
                    anchor.identity_template,
                    observed,
                    params=runtime_params_for_gui(params),
                    param_examples=workflow.params,
                )
            # This branch means no identity_template, so the constructor-time
            # assert (step.anchor.context_text OR identity_template) guarantees a
            # non-None context band. Use the already-narrowed local ``anchor``.
            assert anchor.context_text is not None
            return identity_mod.verify_target_identity(
                anchor.context_text,
                observed,
                params=runtime_params_for_gui(params),
                param_examples=workflow.params,
            )

        live_region = identifier_region or band
        check = attempt(
            before_png,
            # Match compilation: OCR the full frame, then retain only the
            # translated identifier region. OCRing the crop itself can change
            # segmentation/glyph recognition and false-halt a healthy screen.
            None if identifier_region is not None else live_region,
            resolution.point[1],
            exclude,
            scoped_identifier=identifier_region is not None,
            identifier_filter=identifier_region,
        )
        if check.status == "verified":
            return check
        upscaled = identity_mod.upscale_crop(before_png, live_region)
        if upscaled is None:
            return check
        # In the upscaled crop's coordinate space the point's y is its
        # offset from the band origin, times the upscale factor (and the
        # exclude region transforms the same way).
        retry = attempt(
            upscaled,
            None,
            (resolution.point[1] - live_region[1]) * 2,
            (
                (exclude[0] - live_region[0]) * 2,
                (exclude[1] - live_region[1]) * 2,
                exclude[2] * 2,
                exclude[3] * 2,
            ),
            scoped_identifier=identifier_region is not None,
        )
        # verified beats everything; between the two "cannot certify"
        # outcomes an AFFIRMATIVE mismatch outranks an abstain, and abstain
        # outranks a blank unreadable.
        rank = {"unreadable": 0, "abstain": 1, "mismatch": 2, "verified": 3}
        if (rank[retry.status], retry.coverage) > (
            rank[check.status],
            check.coverage,
        ):
            return retry
        return check

    # -- typed-input verification -------------------------------------------------

    def _field_region(
        self,
        field_point: Optional[Point],
        structural_region: Optional[Region] = None,
    ) -> Optional[Region]:
        """Region to observe for typed input, or None for the whole frame."""
        vw, vh = self.backend.viewport
        if structural_region is not None:
            sx, sy, sw, sh = structural_region
            x = max(0, sx - FIELD_REGION_PAD)
            y = max(0, sy - FIELD_REGION_PAD)
            right = min(vw, sx + sw + FIELD_REGION_PAD)
            bottom = min(vh, sy + sh + FIELD_REGION_PAD)
            if right > x and bottom > y:
                return (x, y, right - x, bottom - y)
        if field_point is None:
            return None
        w = min(FIELD_REGION_SIZE[0], vw)
        h = min(FIELD_REGION_SIZE[1], vh)
        x = min(max(0, field_point[0] - w // 2), max(0, vw - w))
        y = min(max(0, field_point[1] - h // 2), max(0, vh - h))
        return (x, y, w, h)

    def _ocr_squashed(self, png: bytes, region: Optional[Region]) -> str:
        """Squashed OCR text of a (region of a) frame."""
        lines = self.vision.ocr(png, region=region)
        return identity_mod.squash(" ".join(line.text for line in lines))

    def _readable_chars(self, png: bytes, region: Optional[Region]) -> int:
        """Confidently readable alphanumeric characters in a region.

        The masked-acceptance metric (see ``MASKED_NEW_TEXT_SLACK``):
        password dots OCR as nothing, low-confidence noise, punctuation
        runs, or — on some platform renderers — confident homogeneous
        digit runs, while a dialog adds confident words. Counting only
        alphanumeric characters of confident, non-homogeneous lines is
        also invariant to OCR merging or splitting the same text into
        different boxes between frames.
        """
        total = 0
        for line in self.vision.ocr(png, region=region):
            if getattr(line, "confidence", 1.0) < MASKED_MIN_CONFIDENCE:
                continue
            alnum = [ch for ch in line.text if ch.isalnum()]
            if not alnum:
                continue
            most_common = max(alnum.count(ch) for ch in set(alnum))
            if len(alnum) >= 4 and most_common / len(alnum) >= MASKED_REPEAT_FRACTION:
                continue  # homogeneous glyph run: masked-dot misread
            total += len(alnum)
        return total

    def _text_value_at(self, field_point: Optional[Point]) -> Optional[str]:
        """Read an exact editable value when the backend can expose one.

        ``None`` means the substrate cannot make this observation. Empty text
        is returned as ``""`` and remains distinguishable from unavailable.
        Exceptions fail closed into the visual verifier rather than escaping.
        """
        reader = getattr(
            self.backend,
            "text_value_at" if field_point is not None else "focused_text_value",
            None,
        )
        if not callable(reader):
            return None
        try:
            value = reader(*field_point) if field_point is not None else reader()
        except Exception:
            return None
        return value if isinstance(value, str) else None

    def _typed_input_landed(
        self,
        text: str,
        field_point: Optional[Point],
        baseline_png: bytes,
        *,
        field_region: Optional[Region] = None,
        baseline_field_value: Optional[str] = None,
        allow_masked: bool = False,
    ) -> tuple[bool, bool]:
        """Did the just-typed ``text`` visibly land?

        Exact structural readback is authoritative when the backend exposes
        it. Otherwise, for an OCR-able value (>=
        ``identity.MIN_PARAM_CHARS`` squashed chars), a contiguous squashed
        run of the value (retried at 2x resolution) must be readable in the
        field region. Pixel-only acceptance is restricted to steps declared
        ``secret``: masked password dots may render no readable text. A dialog
        painting over the region or an unreadable ordinary field must never
        count as "input landed". Values too short for OCR to arbitrate retain
        the legacy diff fallback.

        Returns:
            ``(landed, changed)`` — the verdict, and whether the field
            region's pixels changed at all (the caller's retry decision:
            retyping is only safe when nothing changed).
        """
        after_png = self.vision.wait_settled(self.backend)
        region = self._field_region(field_point, field_region)
        if baseline_field_value is not None:
            expected = baseline_field_value + text
            after_at_point = (
                self._text_value_at(field_point) if field_point is not None else None
            )
            after_focused = self._text_value_at(None)

            # Focus-bound readback is the strongest confirmation. A full-page
            # cover can hide the field from hit-testing while leaving that
            # exact editable element focused, so focused readback also
            # preserves the intended safe-halt-at-the-next-action behavior.
            if after_focused == expected:
                return True, expected != baseline_field_value

            # If hit-testing can read the value but focus moved elsewhere, an
            # interstitial/dialog stole focus after delivery. That is an
            # unknown state, not a success: the value could belong to an
            # obscured/replaced control.
            if after_at_point == expected:
                return False, True

            observed = [
                value for value in (after_at_point, after_focused) if value is not None
            ]
            if observed and all(value == baseline_field_value for value in observed):
                return False, False
            if observed:
                # A partial/different value is an unknown state and must halt
                # without destructive select-all.
                return False, True
            # A structurally readable field disappearing after delivery means
            # it was covered, replaced, or navigated away. Do not downgrade to
            # OCR, which could read the intended text elsewhere on the screen.
            return False, True
        changed = self.vision.pixels_changed(baseline_png, after_png, region=region)
        needle = identity_mod.squash(text)
        if len(needle) < identity_mod.MIN_PARAM_CHARS:
            return changed, changed  # too short for OCR to arbitrate
        need = identity_mod.required_run(len(needle))
        after_hay = self._ocr_squashed(after_png, region)
        if identity_mod.longest_run(needle, after_hay) >= need:
            return True, changed
        if region is not None:
            upscaled = identity_mod.upscale_crop(after_png, region)
            if upscaled is not None:
                up_hay = self._ocr_squashed(upscaled, None)
                if identity_mod.longest_run(needle, up_hay) >= need:
                    return True, changed
        if not changed:
            return False, False
        # Pixels changed but the value is unreadable: masked rendering is
        # the only acceptable explanation, and masked rendering adds no
        # confidently readable alphanumeric text (dot glyphs OCR as
        # nothing, noise, or punctuation — platform-dependent). Anything
        # else (a dialog over the field, another widget's text) must fail
        # the verdict.
        if not allow_masked:
            return False, changed
        landed = (
            self._readable_chars(after_png, region)
            <= self._readable_chars(baseline_png, region) + MASKED_NEW_TEXT_SLACK
        )
        return landed, changed

    def _verify_typed_input(
        self,
        step: Step,
        text: str,
        field_point: Optional[Point],
        baseline_png: bytes,
        result: StepResult,
        *,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        resolution: Optional[Resolution],
        field_region: Optional[Region] = None,
        baseline_field_value: Optional[str] = None,
        allow_masked: bool = False,
        allow_retry: bool = True,
    ) -> Optional[str]:
        """Verify a TYPE action landed; one guarded refocus-and-retype retry.

        The retry only fires when exact readback or pixels prove the first
        attempt changed NOTHING in the field (keystrokes fell on a
        non-rendering target): re-click
        the field (when its point is known), select-all so a false-negative
        first attempt is replaced rather than duplicated, retype. When the
        region DID change but the value cannot be read (a dialog over the
        field, an unexpected re-render), retyping is not safe — select-all
        could destroy pre-existing field content and the re-click could
        re-fire whatever now sits at that point — so the run halts
        immediately with the accurate reason. Typed input that cannot be
        confirmed must never be reported as success (VALIDATION.md 'focus
        stolen' finding).
        """
        landed, changed = self._typed_input_landed(
            text,
            field_point,
            baseline_png,
            field_region=field_region,
            baseline_field_value=baseline_field_value,
            allow_masked=allow_masked,
        )
        if landed:
            result.input_verified = True
            return None
        if changed:
            result.input_verified = False
            return (
                f"Typed input could not be verified for step '{step.id}' "
                f"({step.intent}): the field region changed but the typed "
                "value is not readable there (something else rendered over "
                "or instead of the input) — retyping is unsafe in this "
                "state; run aborted"
            )
        if not allow_retry:
            result.input_verified = False
            return (
                f"Typed input could not be verified for step '{step.id}' "
                f"({step.intent}): the identity-bound browser field showed no "
                "confirmed change; refusing an unleased refocus/retype retry"
            )
        result.input_retried = True
        retry_resolution = resolution
        retry_region = field_region
        retry_frame = baseline_png
        needs_revalidation = self._step_needs_consequential_revalidation(step, workflow)
        if field_point is not None:
            if needs_revalidation:
                (
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    retry_error,
                ) = self._revalidate_consequential_actuation(
                    step,
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir=run_dir,
                )
                if retry_error is not None:
                    result.input_verified = False
                    return retry_error
                if retry_resolution is None:
                    result.input_verified = False
                    return (
                        f"Typed input retry for step '{step.id}' ({step.intent}) "
                        "could not freshly resolve its field; run aborted"
                    )
                field_point = retry_resolution.point
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                result.input_verified = False
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.click(*field_point),
            )
            # Replace, don't append: if the first attempt DID land but was
            # not visible to the diff/OCR, retyping raw would double it.
            if needs_revalidation:
                (
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    retry_error,
                ) = self._revalidate_consequential_actuation(
                    step,
                    retry_resolution,
                    retry_region,
                    retry_frame,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir=run_dir,
                    arm_keyboard=True,
                )
                if retry_error is not None:
                    result.input_verified = False
                    return retry_error
                if retry_resolution is None or retry_resolution.point != field_point:
                    self._cancel_guarded_keyboard()
                    result.input_verified = False
                    return (
                        f"Typed input retry for step '{step.id}' ({step.intent}) "
                        "resolved to a different field after refocus; run aborted"
                    )
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                result.input_verified = False
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.press("ControlOrMeta+a"),
            )

        # The refocus click and select-all key each consume an opaque remote
        # lease.  Acquire and validate one more exact frame for the retype edge.
        if needs_revalidation:
            (
                retry_resolution,
                retry_region,
                retry_baseline,
                retry_error,
            ) = self._revalidate_consequential_actuation(
                step,
                retry_resolution,
                retry_region,
                retry_frame,
                params,
                workflow,
                bundle_dir,
                result,
                run_dir=run_dir,
                arm_keyboard=True,
            )
            if retry_error is not None:
                result.input_verified = False
                return retry_error
        else:
            retry_baseline = self.backend.screenshot()
        if (
            needs_revalidation
            and field_point is not None
            and (retry_resolution is None or retry_resolution.point != field_point)
        ):
            self._cancel_guarded_keyboard()
            result.input_verified = False
            return (
                f"Typed input retry for step '{step.id}' ({step.intent}) "
                "resolved to a different field before retyping; run aborted"
            )
        refusal = self._delivery_authorization_refusal(workflow, params, step, result)
        if refusal is not None:
            result.input_verified = False
            return refusal
        self._require_qualification_environment_current()
        self._deliver_backend_call(result, lambda: self.backend.type_text(text))
        landed, _changed = self._typed_input_landed(
            text,
            field_point,
            retry_baseline,
            field_region=retry_region,
            # Select-all means the retry's expected exact value is ``text``
            # regardless of what the field contained before replacement.
            baseline_field_value="" if baseline_field_value is not None else None,
            allow_masked=allow_masked,
        )
        if landed:
            result.input_verified = True
            return None
        result.input_verified = False
        return (
            f"Typed input could not be verified for step '{step.id}' "
            f"({step.intent}): the screen did not change where the text "
            "should have appeared and OCR could not find the typed value, "
            "after one refocus-and-retype retry — keystrokes likely fell on "
            "a non-input target (focus lost); run aborted"
        )

    def _verify_selected_option(
        self,
        step: Step,
        text: str,
        after_png: bytes,
        bundle_dir: Path,
        workflow: Workflow,
        result: StepResult,
    ) -> Optional[str]:
        """Require the option value on the final settled, re-resolved field.

        Unlike ordinary TYPE verification, selection never accepts pixel
        change alone and never retries: the commit key may already have changed
        the control. Structural readback must equal the intended option, or OCR
        must recover one complete exactly normalized line inside only the final
        live field region. The compiled region qualifies geometry but never
        supplies runtime coordinates. This method does not settle again: its
        caller passes the one final frame used by all post-action checks.
        """

        qualified_readback = step.selection_region
        qualified_target = step.anchor.region if step.anchor is not None else None
        if qualified_readback is None or qualified_target is None:
            result.input_verified = False
            return f"Option selection for step '{step.id}' has no field region"
        resolution, live_region, resolve_error = self._resolve_step(
            step,
            after_png,
            bundle_dir,
            workflow,
            # Final option verification is exact retained-evidence work. A
            # model may propose a governed repair after a halt, but it cannot
            # supply the target region that certifies the committed value.
            allow_grounder=False,
        )
        if (
            resolve_error is not None
            or resolution is None
            or not self._selection_region_compatible(qualified_target, live_region)
        ):
            result.input_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            return (
                f"Selected option field could not be re-resolved with compatible "
                f"geometry on the final settled frame for step '{step.id}' "
                f"({step.intent}); the run halted"
            )
        assert live_region is not None
        live_readback = self._mapped_selection_readback_region(
            qualified_target,
            qualified_readback,
            live_region,
            after_png,
        )
        if live_readback is None:
            result.input_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            return (
                f"Selected option readback region could not be mapped onto the "
                f"final settled frame for step '{step.id}' ({step.intent}); the "
                "run halted"
            )
        exact = self._text_value_at(resolution.point)
        if exact is not None:
            verified = identity_mod.squash(exact) == identity_mod.squash(text)
        else:
            needle = identity_mod.squash(text)
            verified = any(
                getattr(line, "confidence", 1.0) >= 0.5
                and identity_mod.squash(line.text) == needle
                for line in self.vision.ocr(after_png, region=live_readback)
            )
            if not verified:
                upscaled = identity_mod.upscale_crop(after_png, live_readback)
                if upscaled is not None:
                    verified = any(
                        getattr(line, "confidence", 1.0) >= 0.5
                        and identity_mod.squash(line.text) == needle
                        for line in self.vision.ocr(upscaled, region=None)
                    )
        result.input_verified = verified
        if verified:
            return None
        result.safety_halt = True
        result.failure_category = "governed_refusal"
        return (
            f"Selected option could not be verified for step '{step.id}' "
            f"({step.intent}) inside its exact live re-resolved field region; the "
            "commit may have selected a different value, so the run halted"
        )

    @staticmethod
    def _selection_region_compatible(
        qualified: Region,
        live: Optional[Region],
    ) -> bool:
        """Check live field shape without treating recorded coordinates as truth.

        Remote drift may translate the field, so x/y intentionally do not
        participate. A resolution rung that returns no region, flips the field
        orientation, or changes either dimension by more than 25% cannot safely
        inherit the recorded option-selection contract.
        """

        if live is None:
            return False
        _, _, qualified_w, qualified_h = qualified
        _, _, live_w, live_h = live
        if min(qualified_w, qualified_h, live_w, live_h) <= 0:
            return False
        if (qualified_w >= qualified_h) != (live_w >= live_h):
            return False
        return (
            0.75 <= live_w / qualified_w <= 1.25
            and 0.75 <= live_h / qualified_h <= 1.25
        )

    @staticmethod
    def _selection_target_continuous(
        qualified: Region,
        focused: Point,
        refreshed: Point,
    ) -> bool:
        """Require both select phases to name the same focused field.

        Reuse the selection contract's 25% size tolerance for point drift.
        This permits small OCR/template jitter but refuses a second control.
        """

        _, _, qualified_w, qualified_h = qualified
        if qualified_w <= 0 or qualified_h <= 0:
            return False
        return (
            abs(refreshed[0] - focused[0]) <= qualified_w * 0.25
            and abs(refreshed[1] - focused[1]) <= qualified_h * 0.25
        )

    @staticmethod
    def _mapped_selection_readback_region(
        recorded_target: Region,
        recorded_readback: Region,
        live_target: Region,
        frame_png: bytes,
    ) -> Optional[Region]:
        """Map the recorded readback band onto a fresh live target.

        Absolute recording coordinates never drive verification. The mapping
        preserves the band's target-relative offsets while allowing the
        already-qualified target geometry to translate or scale modestly, then
        clamps it to the exact final frame.
        """

        import cv2
        import numpy as np

        frame = cv2.imdecode(np.frombuffer(frame_png, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return None
        frame_h, frame_w = frame.shape[:2]
        target_x, target_y, target_w, target_h = recorded_target
        read_x, read_y, read_w, read_h = recorded_readback
        live_x, live_y, live_w, live_h = live_target
        if min(target_w, target_h, read_w, read_h, live_w, live_h) <= 0:
            return None
        scale_x = live_w / target_w
        scale_y = live_h / target_h
        mapped_x = round(live_x + (read_x - target_x) * scale_x)
        mapped_y = round(live_y + (read_y - target_y) * scale_y)
        mapped_w = max(1, round(read_w * scale_x))
        mapped_h = max(1, round(read_h * scale_y))
        left = max(0, mapped_x)
        top = max(0, mapped_y)
        right = min(frame_w, mapped_x + mapped_w)
        bottom = min(frame_h, mapped_y + mapped_h)
        if right <= left or bottom <= top:
            return None
        return (left, top, right - left, bottom - top)

    # -- closed-loop scroll ------------------------------------------------------

    def _implicit_scroll_target_ready(
        self,
        step: Step,
        frame_png: bytes,
        *,
        workflow: Workflow,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
    ) -> bool:
        """Resolve an implicit scroll target and honor its armed identity.

        A generic input crop can visually resolve in several places on a long
        form. Declaring the target visible from that visual match alone makes
        the preceding scroll a false no-op. Structural resolution stays
        available, model grounding stays disabled, and an identity-armed target
        is ready only when the same pre-action identity gate verifies it.
        """
        if step.anchor is None:
            return False
        template_png = self._asset_bytes(
            bundle_dir, step.anchor.template, workflow=workflow
        )
        structural = (
            self.backend
            if self.use_structural and hasattr(self.backend, "locate_structural")
            else None
        )
        try:
            resolved = resolve(
                step.anchor,
                frame_png,
                self.vision,
                None,  # scroll readiness must remain deterministic and model-free
                step.intent,
                template_png=template_png,
                viewport=_frame_viewport(frame_png),
                structural=structural,
            )
        except OcrResolutionRefused:
            # Readiness is a bounded, non-actuating probe. An ambiguous OCR
            # candidate means that the target is not ready yet, so the scroll
            # loop must continue. The later target action runs the full
            # resolver again and still halts on the same ambiguity.
            return False
        if resolved is None:
            return False
        resolution, _matched_region = resolved
        # Geometry estimates a point from surrounding landmarks; it does not
        # prove that the target itself is visible. In particular, a fixed
        # header can project the recorded point while the form row is still
        # off-screen, turning the preceding scroll into a false no-op. Scroll
        # readiness therefore requires direct deterministic target evidence.
        # Model grounding is disabled above and future weaker rungs must opt in
        # deliberately rather than silently acquiring readiness authority.
        if resolution.rung not in {
            "structural",
            "template",
            "template_global",
            "ocr",
        }:
            return False
        if not step.identity_armed:
            return True
        check = self._verify_identity(
            step, resolution, frame_png, params, workflow, bundle_dir
        )
        return check.status == "verified"

    def _act_scroll(
        self,
        step: Step,
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        run_dir: Path,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        graph_ctx: Optional["_GraphStepContext"] = None,
    ) -> Optional[str]:
        """Execute a SCROLL step as a closed loop on a wait_until predicate.

        A recorded scroll's purpose is to bring the next target into view, so
        the step scrolls by its recorded delta until a READINESS predicate holds
        on a settled frame — not a fixed number of times. That predicate is the
        step's own ``wait_until`` when set; otherwise it defaults to
        ``ANCHOR_RESOLVES`` on the NEXT anchored step's anchor — today's closed
        loop, now expressed as the first concrete ``wait_until`` predicate
        rather than a special case (RFC §6). The step probes BEFORE scrolling
        (a preceding SCROLL step may already have brought the target into view,
        making this one a no-op) and stops as soon as the predicate holds.

        The loop is bounded: this step may scroll at most
        ``SCROLL_BUDGET_FACTOR`` times its own recorded distance. On budget
        exhaustion the step fails loudly — unless the immediately following
        step is another SCROLL step, which inherits the loop (so a recorded
        run of N scrolls shares a combined ~2.5x budget).

        Falls back to the fixed recorded delta (open-loop, one gesture) when no
        readiness predicate exists (no ``wait_until`` and no later anchor) or
        the recorded delta is zero. Predicate probes never call the grounder:
        closed-loop scrolling must stay model-free.

        Returns:
            An error string on budget exhaustion (see above) or None.
        """
        dx = step.scroll_dx or 0
        dy = step.scroll_dy or 0
        # The next anchored step (default scroll stop condition): linear mode
        # scans forward in ``workflow.steps``; program mode uses the
        # interpreter-supplied successor (the target of this state's
        # unconditional transition chain).
        next_step = (
            graph_ctx.next_anchored
            if graph_ctx is not None
            else self._next_anchored_step(workflow, step_index)
        )
        # The scroll's readiness is a wait_until predicate: an explicit
        # step.wait_until wins; otherwise the next anchor's ANCHOR_RESOLVES.
        stop_pred = step.wait_until
        implicit_target_readiness = stop_pred is None and next_step is not None
        if stop_pred is None and next_step is not None:
            stop_pred = Predicate(
                kind=PredicateKind.ANCHOR_RESOLVES,
                anchor=next_step.anchor,
                intent=next_step.intent,
            )
        if stop_pred is None or (dx == 0 and dy == 0):
            remote_preflight_error = self._prepare_remote_scroll_input(step, result)
            if remote_preflight_error is not None:
                return remote_preflight_error
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.scroll(dx, dy),
            )
            return None

        def readiness_holds(frame_png: bytes) -> bool:
            if implicit_target_readiness:
                assert next_step is not None
                return self._implicit_scroll_target_ready(
                    next_step,
                    frame_png,
                    workflow=workflow,
                    bundle_dir=bundle_dir,
                    params=params,
                )
            return self._predicate_holds(
                stop_pred,
                frame_png,
                bundle_dir,
                params,
                workflow=workflow,
            )

        holds = readiness_holds(before_png)
        if self._governed_asset_mutation is not None:
            return self._governed_asset_mutation
        if holds:
            return None  # target already in view; nothing to scroll

        increment = math.hypot(dx, dy)
        budget = SCROLL_BUDGET_FACTOR * increment
        scrolled = 0.0
        while scrolled + increment <= budget:
            # Readiness callbacks run after the outer step preflight.  Bind
            # every wheel edge to a new exact remote frame and repeat the
            # context/identity checks after those callbacks.  This also avoids
            # reusing the one-shot lease consumed by the prior wheel edge.
            if self._step_needs_consequential_revalidation(step, workflow):
                (
                    _scroll_resolution,
                    _scroll_region,
                    fresh_scroll_frame,
                    scroll_error,
                ) = self._revalidate_consequential_actuation(
                    step,
                    None,
                    None,
                    before_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir=run_dir,
                )
                if scroll_error is not None:
                    return scroll_error
                if readiness_holds(fresh_scroll_frame):
                    return None
            remote_preflight_error = self._prepare_remote_scroll_input(step, result)
            if remote_preflight_error is not None:
                return remote_preflight_error
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                return refusal
            self._require_qualification_environment_current()
            self._deliver_backend_call(
                result,
                lambda: self.backend.scroll(dx, dy),
            )
            scrolled += increment
            frame = self._wait_for_scroll_transition_and_settle(before_png)
            before_png = frame
            holds = readiness_holds(frame)
            if self._governed_asset_mutation is not None:
                return self._governed_asset_mutation
            if holds:
                return None

        following = (
            graph_ctx.following
            if graph_ctx is not None
            else (
                workflow.steps[step_index + 1]
                if step_index + 1 < len(workflow.steps)
                else None
            )
        )
        if following is not None and following.action is ActionKind.SCROLL:
            # The next SCROLL step continues the loop with its own budget.
            return None
        target_desc = (
            f"the anchor of step '{next_step.id}' ({next_step.intent})"
            if next_step is not None
            else self._describe_predicate(stop_pred)
        )
        return (
            f"Step '{step.id}' ({step.intent}): closed-loop scroll exhausted "
            f"its budget ({scrolled:.0f}px of {budget:.0f}px allowed, "
            f"{SCROLL_BUDGET_FACTOR}x the recorded distance) without "
            f"{target_desc} resolving — target never came into view; run aborted"
        )

    def _wait_for_scroll_transition_and_settle(self, baseline_png: bytes) -> bytes:
        """Wait for a delivered scroll to change the frame, then settle it.

        A remote client can return two identical pre-action frames before the
        wheel packet reaches the remote application. A generic settle poll can
        therefore accept the old screen as stable. Require one visual
        transition within the configured readiness window before applying the
        normal settled-state check. At a real scroll boundary, return the last
        unchanged frame after the bounded wait so the closed loop can continue
        or refuse when its action budget is exhausted.
        """

        deadline = time.monotonic() + self.settle_readiness_timeout_s
        frame = self.backend.screenshot()
        while not self.vision.pixels_changed(baseline_png, frame):
            if time.monotonic() >= deadline:
                return frame
            time.sleep(self.poll_interval_s)
            frame = self.backend.screenshot()
        return self.vision.wait_settled(self.backend)

    def _prepare_remote_scroll_input(
        self,
        step: Step,
        result: StepResult,
    ) -> Optional[str]:
        """Bind one remote wheel edge to a fresh, one-use frame lease.

        Target-readiness probes can take longer than a remote backend's frame
        lease.  A wheel gesture has no coordinate to re-resolve, but it is
        still real input into an opaque session.  Acquire the remote backend's
        exact-content lease after readiness checks and before the final
        authorization, environment, and delivery checks.  Do not extend the
        allowed frame age or reuse the observation from the probe.
        """

        if not isinstance(self.backend, RemoteActuationBackend):
            return None
        try:
            self.backend.acquire_actuation_frame()
        except Exception as exc:  # noqa: BLE001 - backend boundary must halt
            if self.governed_authorization is not None:
                result.safety_halt = True
            detail = _scrub_phi(str(exc)) or type(exc).__name__
            return (
                "Remote scroll preflight HALTED before input for step "
                f"'{step.id}' ({step.intent}): {detail}"
            )
        return None

    @staticmethod
    def _next_anchored_step(workflow: Workflow, step_index: int) -> Optional[Step]:
        """The first step after ``step_index`` that carries an anchor."""
        for candidate in workflow.steps[step_index + 1 :]:
            if candidate.anchor is not None:
                return candidate
        return None

    # -- postconditions --------------------------------------------------------

    def _structural_state(self) -> dict[str, Any]:
        """Structural observations the backend can provide right now.

        Backends MAY expose ``url`` / ``page_title`` / ``page_count`` (see
        ``openadapt_flow.backend.StructuralBackend``). Missing observations
        are simply absent from the dict.
        """
        state: dict[str, Any] = {}
        for key in ("url", "page_title", "page_count"):
            try:
                value = getattr(self.backend, key, None)
            except Exception:
                value = None
            if value is not None:
                state[key] = value
        return state

    def _attended_transition_observation(self) -> Any:
        """Capture ephemeral browser structure for a signed pause baseline.

        Raw URL/title values live only in this return object long enough for
        :class:`AttendedActionStore` to HMAC them. They are never written into
        a checkpoint, report, pending escalation, or capability.
        """
        from openadapt_flow.runtime.durable.attended import TransitionObservation

        state = self._structural_state()
        url = state.get("url")
        title = state.get("page_title")
        page_count = state.get("page_count")
        return TransitionObservation(
            url=url if isinstance(url, str) else None,
            page_title=title if isinstance(title, str) else None,
            page_count=page_count if isinstance(page_count, int) else None,
        )

    def _structural_changed(
        self, key: str, start_state: dict[str, Any]
    ) -> Optional[bool]:
        """Whether structural observation ``key`` differs from step start.

        Returns None when the observation is unavailable on either side —
        the caller treats that as an honestly-unverifiable pass (a bundle
        recorded on a structural backend may replay on one that is not;
        see docs/LIMITS.md).
        """
        try:
            current = getattr(self.backend, key, None)
        except Exception:
            current = None
        start = start_state.get(key)
        if current is None or start is None:
            return None
        return current != start

    def _check_postconditions(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: dict[str, Any],
        result: Optional[StepResult] = None,
    ) -> tuple[bool, bytes, list[str]]:
        """Poll postconditions until each passes or times out.

        Each postcondition is polled (fresh screenshots) up to its own
        ``timeout_s``. If any fails, the screen is re-settled once and all
        postconditions are re-checked a single time. If a VLM state-verifier
        is configured, a render-drift-sensitive postcondition that still failed
        gets one last drift-oracle pass (see :meth:`_drift_oracle_rescue`).

        Returns:
            (ok, last_frame, failed) — the frame the final verdict was based
            on, plus human-readable descriptions of the postconditions that
            failed the final check (empty when ok).
        """
        ok, frame_png = self._poll_postconditions(
            step, frame_png, bundle_dir, start_state
        )
        if ok:
            return True, frame_png, []
        # One re-settle retry.
        frame_png = self.vision.wait_settled(self.backend)
        failed_pcs = [
            pc
            for pc in step.expect
            if not self._postcondition_passes(pc, frame_png, bundle_dir, start_state)
        ]
        if failed_pcs and self.state_verifier is not None:
            failed_pcs = self._drift_oracle_rescue(failed_pcs, frame_png, result)
        failed = [self._describe_postcondition(pc) for pc in failed_pcs]
        return not failed_pcs, frame_png, failed

    def _drift_oracle_rescue(
        self,
        failed_pcs: list[Any],
        frame_png: bytes,
        result: Optional[StepResult],
    ) -> list[Any]:
        """Give each deterministically-failed, render-drift-sensitive
        postcondition one confirmation pass through the VLM state-verifier.

        VETO-SAFE by construction:

        * Only ``text_present`` and ``region_stable`` are eligible — the kinds
          a theme/scale/font re-render can legitimately break. Structural
          postconditions (``url_changed`` etc.) and ``text_absent`` are NEVER
          rescued: those failures are real, not render drift.
        * A postcondition is rescued ONLY on a confident ``"yes"``; ``"no"``,
          ``"uncertain"``, and any appliance outage keep it failed (halt).
        * Every call and every rescue is recorded on ``result`` for the report
          (and counted as a model call), so a rescue is auditable, never silent.

        Returns the postconditions that remain failed after the pass.
        """
        assert self.state_verifier is not None  # guaranteed by the caller (line ~2618)
        survivors: list[Any] = []
        for pc in failed_pcs:
            expected = self._expected_state_text(pc)
            if expected is None:  # not a drift-rescuable kind
                survivors.append(pc)
                continue
            if result is not None:
                result.drift_oracle_calls += 1
            try:
                confirmed = self.state_verifier.holds(frame_png, expected)
            except Exception:
                confirmed = False  # fail-safe: any error keeps the halt
            if confirmed:
                if result is not None:
                    result.postcondition_drift_rescues.append(
                        self._describe_postcondition(pc)
                    )
            else:
                survivors.append(pc)
        return survivors

    @staticmethod
    def _expected_state_text(pc: Any) -> Optional[str]:
        """A natural-language expected-state for the drift-oracle, or None if
        the postcondition kind is not render-drift-rescuable."""
        kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
        if kind == "text_present" and pc.text:
            return f"the text {pc.text!r} is visible on the screen"
        if kind == "region_stable":
            return "the content recorded in the highlighted region is present"
        return None

    @staticmethod
    def _describe_postcondition(pc: Any) -> str:
        """Human-readable one-liner for a postcondition (for error messages)."""
        kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
        if kind in ("text_present", "text_absent"):
            return f"{kind} {pc.text!r}"
        if kind in ("url_changed", "title_changed", "new_tab_opened"):
            return f"{kind} (vs. the step's start state)"
        return f"{kind} region={tuple(pc.region) if pc.region else None}"

    def _poll_postconditions(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: dict[str, Any],
    ) -> tuple[bool, bytes]:
        """First pass: poll each postcondition until pass or timeout."""
        for pc in step.expect:
            deadline = time.monotonic() + pc.timeout_s
            while True:
                if self._postcondition_passes(pc, frame_png, bundle_dir, start_state):
                    break
                if time.monotonic() >= deadline:
                    return False, frame_png
                time.sleep(self.poll_interval_s)
                frame_png = self.backend.screenshot()
        return True, frame_png

    def _postcondition_passes(
        self,
        pc: Any,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Evaluate a single postcondition against a frame."""
        kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
        if kind in ("url_changed", "title_changed", "new_tab_opened"):
            key = {
                "url_changed": "url",
                "title_changed": "page_title",
                "new_tab_opened": "page_count",
            }[kind]
            changed = self._structural_changed(key, start_state or {})
            if changed is None:
                # Unobservable on this backend: pass, honestly unverified
                # (docs/LIMITS.md "vacuous" caveat).
                return True
            if kind == "new_tab_opened":
                try:
                    current = getattr(self.backend, "page_count", None)
                except Exception:
                    current = None
                start = (start_state or {}).get("page_count")
                return current is not None and start is not None and current > start
            return changed
        if kind == "text_present":
            # text_present (not find_text): presence must not depend on
            # whether the OCR engine merged the target into a longer box
            # or split it across boxes — see vision.ocr.text_present.
            return pc.text is not None and self.vision.text_present(frame_png, pc.text)
        if kind == "text_absent":
            return pc.text is None or not self.vision.text_present(frame_png, pc.text)
        if kind == "region_stable":
            if pc.region is None or pc.phash is None:
                return True
            region = tuple(pc.region)
            # Template check first: real apps re-layout by a few pixels
            # between runs (auto-scrolling panes, variable banner heights),
            # which the exact-position phash cannot tolerate — accept the
            # expected content anywhere near the recorded region.
            template_png = self._postcondition_template(pc, bundle_dir)
            if template_png is not None:
                search = pad_region(
                    region, PC_TEMPLATE_SEARCH_PAD, _frame_viewport(frame_png)
                )
                match = self.vision.find_template(
                    frame_png,
                    template_png,
                    search_region=search,
                    threshold=PC_TEMPLATE_THRESHOLD,
                )
                if match is not None:
                    return True
                # A region-stability check is about the recorded structure,
                # not its light/dark palette.  Use the same sealed template
                # and localized search on edge maps before falling back to the
                # exact-position structural pHash.  This preserves the
                # postcondition: a blocking modal or changed layout still
                # fails; only the representation becomes palette-invariant.
                structural_matcher = getattr(
                    self.vision, "find_structural_template", None
                )
                if callable(structural_matcher):
                    match = structural_matcher(
                        frame_png,
                        template_png,
                        search_region=search,
                        threshold=PC_STRUCTURAL_TEMPLATE_THRESHOLD,
                    )
                    if match is not None:
                        return True
            live = self.vision.phash_png(frame_png, region=region)
            distance = self.vision.phash_distance(live, pc.phash)
            return distance <= pc.phash_tolerance
        return False

    def _postcondition_template(self, pc: Any, bundle_dir: Path) -> Optional[bytes]:
        """Bytes of a REGION_STABLE postcondition's template crop, if any."""
        rel = getattr(pc, "template", None)
        if not rel:
            return None
        return self._asset_bytes(bundle_dir, rel)

    def _asset_bytes(
        self,
        bundle_dir: Path,
        rel: str,
        *,
        workflow: Optional[Workflow] = None,
    ) -> Optional[bytes]:
        """Return the verified run snapshot for governed asset consumers."""
        if self.governed_authorization is not None:
            data = self._governed_asset_snapshot.get(rel)
            expected = self._governed_asset_hashes.get(rel)
            if self._governed_plaintext_assets and expected is not None:
                path = Path(bundle_dir) / rel
                try:
                    current = path.read_bytes()
                except OSError:
                    current = b""
                if hashlib.sha256(current).hexdigest() != expected:
                    self._governed_asset_mutation = (
                        f"governed sealed asset {rel!r} changed after admission; "
                        "refusing to act; run aborted"
                    )
            return data
        if workflow is not None and workflow.encrypted:
            decrypted = workflow.decrypted_template(rel)
            if decrypted is not None:
                return decrypted
        path = Path(bundle_dir) / rel
        return path.read_bytes() if path.is_file() else None

    def _raise_on_governed_asset_mutation(self) -> None:
        """Make predicate-only mutation a non-catchable program safety halt."""
        if self._governed_asset_mutation is not None:
            raise _ProgramHalt("halt", self._governed_asset_mutation, safety=True)

    # -- healing ---------------------------------------------------------------

    def _heal_step(
        self,
        step: Step,
        resolution: Resolution,
        matched_region: Region,
        frame_png: bytes,
        workflow: Workflow,
        run_dir: Path,
        new_crops: dict[str, bytes],
    ):
        """Build, govern, and persist a repair candidate.

        A heal is a governed PATCH, not a silent bundle swap: the raw event is
        wrapped in a reviewable :class:`~openadapt_flow.runtime.healing.HealPatch`
        and run through the regression gate (identity + effect + risk) before
        it may touch the workflow. A patch that would weaken the step's
        identity band -- the reviewed context-drop bug -- is QUARANTINED
        (persisted under ``run_dir/heals/<step_id>/patch.json`` for review)
        and NOT applied; the caller halts the run. A gate-passing local repair
        can update the in-memory workflow. A gate-passing repair discovered
        under exact authorization remains a promotable candidate, because the
        running program cannot change after admission.
        """
        event, crop_png = heal_mod.build_heal_event(
            step, resolution, matched_region, frame_png, self.vision
        )
        outcome = healing_mod.govern_heal(step, event, run_dir=run_dir)
        if outcome.promoted:
            if self.governed_authorization is None:
                heal_mod.apply_heal(workflow, event)
                source = self._execution_workflow_source
                if source is not None and source is not workflow:
                    heal_mod.apply_heal(source, event)
                self._accept_healed_anchor_in_workflow_snapshot(workflow, event)
                new_crops[step.id] = crop_png
            else:
                # Exact authorization binds the admitted workflow semantics.
                # Keep a gate-passing repair as a reviewable candidate for a
                # later qualified bundle. Never change the program that is
                # currently executing under that authorization.
                outcome.patch.status = "promotable"
                healing_mod.persist_patch(outcome.patch, run_dir)
            heal_mod.persist_heal(event, crop_png, frame_png, run_dir)
        return outcome

    def _accept_healed_anchor_in_workflow_snapshot(
        self,
        workflow: Workflow,
        event: Any,
    ) -> None:
        """Update only the governed heal's anchor in the admitted snapshot."""

        snapshot = self._execution_workflow_snapshot
        if snapshot is None:
            return

        for live_step, admitted_step in zip(workflow.steps, snapshot.steps):
            if (
                live_step.id == event.step_id
                and admitted_step.id == event.step_id
                and live_step.anchor == event.new_anchor
            ):
                admitted_step.anchor = event.new_anchor.model_copy(deep=True)

        live_graphs: dict[str, ProgramGraph] = dict(workflow.subflows)
        admitted_graphs: dict[str, ProgramGraph] = dict(snapshot.subflows)
        if workflow.program is not None and snapshot.program is not None:
            live_graphs[TOP_GRAPH_ID] = workflow.program
            admitted_graphs[TOP_GRAPH_ID] = snapshot.program
        for graph_id, live_graph in live_graphs.items():
            admitted_graph = admitted_graphs.get(graph_id)
            if admitted_graph is None:
                continue
            for state_id, live_state in live_graph.states.items():
                admitted_state = admitted_graph.states.get(state_id)
                if (
                    admitted_state is not None
                    and live_state.step is not None
                    and admitted_state.step is not None
                    and live_state.step.id == event.step_id
                    and admitted_state.step.id == event.step_id
                    and live_state.step.anchor == event.new_anchor
                ):
                    admitted_state.step.anchor = event.new_anchor.model_copy(deep=True)

    # -- io ----------------------------------------------------------------------

    @staticmethod
    def _new_step_evidence_key(run_dir: Path, step_id: str) -> str:
        """Allocate one stable, collision-free file key for an action occurrence."""

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", step_id).strip("._-") or "step"
        if safe != step_id:
            safe = f"{safe}_{hashlib.sha256(step_id.encode()).hexdigest()[:8]}"
        steps_dir = Path(run_dir) / "steps"
        candidate = safe
        occurrence = 1
        while any(
            (steps_dir / f"{candidate}_{suffix}.png").exists()
            for suffix in ("before", "after", "attended-before", "attended-after")
        ):
            occurrence += 1
            candidate = f"{safe}__{occurrence:04d}"
        return candidate

    @staticmethod
    def _save_step_png(run_dir: Path, step_id: str, suffix: str, png: bytes) -> str:
        """Save a per-step screenshot; return its run-dir-relative path.

        These frames are embedded by relative path into the shareable
        ``REPORT.md``. When opt-in image redaction is enabled
        (``OPENADAPT_FLOW_SCRUB_IMAGES=1``), PII/PHI regions are burned out
        before the frame is written to disk; otherwise the raw frame is saved
        (see openadapt_flow.privacy and docs/PRIVACY.md).
        """
        rel = f"steps/{step_id}_{suffix}.png"
        (Path(run_dir) / rel).write_bytes(_scrub_png(png))
        return rel
