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

import dataclasses
import functools
import hashlib
import hmac
import io
import json
import math
import os
import re
import struct
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    TypeVar,
    Union,
    cast,
)
from urllib.parse import urlsplit

from openadapt_flow import sealed_set as _sealed
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
    SubmitBindingBackend,
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
    CheckToClickRecheck,
    EffectVerificationEvidence,
    ExecutionTargetKind,
    FormIdentity,
    FreshActuationEvent,
    HaltObservation,
    HoverLook,
    IdentityCheck,
    IdentityLook,
    IdentitySignalEvidence,
    Interstitial,
    InterstitialActionResult,
    LoopIterationBinding,
    LoopSpec,
    ObservedSurfaceEnvironment,
    ParamKind,
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
from openadapt_flow.network_scope import EndpointScope, endpoint_scope
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
from openadapt_flow.runtime import identity_census as census_mod
from openadapt_flow.runtime import identity_looks as looks_mod
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
from openadapt_flow.runtime.known_ids import (
    lookalike_class as known_ids_lookalike_class,
)
from openadapt_flow.runtime.program_predicates import (
    evaluate_program_predicate,
    exact_png_size,
    predicate_template_refs,
    predicate_uses_frame,
    program_predicate_evaluator_contract_sha256,
)
from openadapt_flow.runtime.resolver import (
    OCR_MIN_RATIO,
    is_below_ocr,
    pad_region,
    resolve,
    visual_resolution_anchor_contract_sha256,
    visual_resolution_evaluator_contract_sha256,
)
from openadapt_flow.runtime.step_timing import timed_span
from openadapt_flow.runtime.surfaces import SurfaceRouter
from openadapt_flow.verification import (
    EffectContractMutationError,
    verifier_effect_tier,
    verify_effect_without_mutation,
)
from openadapt_flow.vision.ocr import OcrResolutionRefused

_DeliveryResultT = TypeVar("_DeliveryResultT")
_MAX_FRESH_ACTUATION_REACQUISITIONS = 2
# Check-to-click guard on the pixel path (``Replayer._pixel_recheck``): how
# often a changed target or identity region is re-observed before the click
# is refused, and how much a pixel must change to count (remote encoders
# shift colours by a few levels; a dialog or a new row changes far more).
_PIXEL_RECHECK_REOBSERVES = 2
_PIXEL_RECHECK_LEVEL = 24
_PIXEL_RECHECK_MIN_PIXELS = 4
# How long a step that raises a browser dialog waits for it to open.
_DIALOG_WAIT_S = 5.0

# How long past its step's timeout a held record check may run under an
# approval mode before the held report decides the item (a person).
READBACK_DEADLINE_MARGIN_S = 120.0
# Private module capability.  The public ``run`` API cannot mint a durable
# continuation.  ``runtime.durable.resume`` imports this object only after it
# has validated the pause, approval, bundle, checkpoint, and live state.
_DURABLE_RESUME_AUTHORITY = object()

if TYPE_CHECKING:
    from openadapt_flow.approval_mode.gate import ApprovalGate
    from openadapt_flow.production_qualification import ProductionQualificationGuard
    from openadapt_flow.qualification_campaign_authority import (
        QualificationCampaignGuard,
    )
    from openadapt_flow.runtime.control_overlay import RuntimeControlOverlayEmitter
    from openadapt_flow.runtime.readback_pipeline import (
        PipelinedReadback,
        ReadbackPipeline,
    )
    from openadapt_flow.transaction import IdempotencyLedger

# REGION_STABLE template check: how far the expected content may shift from
# the recorded region (real apps re-layout by a few pixels between runs),
# and the minimum template-match score to accept it.
PC_TEMPLATE_SEARCH_PAD = 80


_FIELD_CHECK_LIMIT = 32
_FIELD_VALUE_LIMIT = 256


def _field_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)[:_FIELD_VALUE_LIMIT]
    return None


def effect_field_checks(effect: Any, verdict: Any, tier: Any) -> list[Any]:
    """The field comparisons one effect verdict made, for the local report.

    A ``field_equals`` effect compares one field. A record effect compares
    each literal ``match`` value against the first matched record. Values
    that aren't plain text or numbers are left out rather than guessed.
    """

    from openadapt_flow.ir import EffectFieldCheck

    tier_value = int(tier) if tier is not None else None
    checks: list[EffectFieldCheck] = []
    field = getattr(effect, "field", None)
    expected_value = getattr(verdict, "expected_value", None)
    if field and expected_value is not None:
        checks.append(
            EffectFieldCheck(
                field=str(field)[:_FIELD_VALUE_LIMIT],
                expected=_field_text(expected_value),
                observed=_field_text(getattr(verdict, "observed_value", None)),
                matched=bool(getattr(verdict, "confirmed", False)),
                verification_tier=tier_value,
            )
        )
        return checks
    records = getattr(verdict, "matched_records", None) or []
    record = records[0] if records and isinstance(records[0], dict) else None
    for name, want in (getattr(effect, "match", None) or {}).items():
        expected = _field_text(want)
        if expected is None:
            continue
        observed = _field_text(record.get(name)) if record is not None else None
        checks.append(
            EffectFieldCheck(
                field=str(name)[:_FIELD_VALUE_LIMIT],
                expected=expected,
                observed=observed,
                matched=observed == expected,
                verification_tier=tier_value,
            )
        )
        if len(checks) >= _FIELD_CHECK_LIMIT:
            break
    return checks


#: A uniform page scale is inferred only from a click point at least this far
#: from both edges at the origin, only when both axes agree to within this
#: fraction, and only inside these bounds (and not within 5% of 1).
_PAGE_SCALE_MIN_COORD = 40
_PAGE_SCALE_AXIS_AGREEMENT = 0.02
_PAGE_SCALE_BOUNDS = (0.5, 2.0)
#: One pixel of click-point noise moves the estimate by a few tenths of a
#: percent, enough to drop a wide template below threshold; the check tries
#: this tight band around it.
_PAGE_SCALE_BAND = (0.99, 0.995, 1.0, 1.005, 1.01)


def _uniform_page_scale(step: Any, result: Any) -> Optional[float]:
    """The page scale this step's click showed, or ``None``.

    The recorded click point and the point the ladder resolved this run,
    divided axis by axis. A whole-page zoom multiplies both coordinates by
    the same factor; a control that merely moved almost never does, so the
    two axes must agree.
    """

    resolution = getattr(result, "resolution", None)
    anchor = getattr(step, "anchor", None)
    if resolution is None or anchor is None:
        return None
    try:
        rx, ry = (float(v) for v in anchor.click_point)
        lx, ly = (float(v) for v in resolution.point)
    except (TypeError, ValueError):
        return None
    if min(rx, ry) < _PAGE_SCALE_MIN_COORD:
        return None
    sx, sy = lx / rx, ly / ry
    if abs(sx - sy) > _PAGE_SCALE_AXIS_AGREEMENT * max(sx, sy):
        return None
    scale = (sx + sy) / 2
    low, high = _PAGE_SCALE_BOUNDS
    if not low <= scale <= high or abs(scale - 1.0) < 0.05:
        return None
    return round(scale, 3)


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


def _pipeline_safe_verifier(verifier: Any) -> bool:
    """Whether a read-back may run on a side thread while the next item runs.

    Only verifiers that read the system of record over the network and never
    touch the screen: REST, FHIR, GraphQL and SQL. Anything that binds to a
    backend (on-screen read-backs, and the redacting and candidate wrappers,
    which forward ``bind_backend``) or any other kind verifies inline.
    """

    if (
        verifier is None
        or hasattr(verifier, "bind_backend")
        or getattr(verifier, "backend", None) is not None
        or getattr(verifier, "_backend", None) is not None
    ):
        return False
    safe: list[type] = []
    for module, name in (
        ("openadapt_flow.runtime.effects.rest", "RestRecordVerifier"),
        ("openadapt_flow.runtime.effects.fhir", "FhirEffectVerifier"),
        ("openadapt_flow.runtime.effects.graphql", "GraphQLRecordVerifier"),
        ("openadapt_flow.runtime.effects.sql", "SqlRecordVerifier"),
    ):
        try:
            import importlib

            safe.append(getattr(importlib.import_module(module), name))
        except Exception:  # noqa: BLE001 - an unavailable kind is not safe
            continue
    return isinstance(verifier, tuple(safe))


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


def effect_endpoint_scope(verifier: Any) -> EndpointScope:
    """Where an effect verifier reads its system of record.

    A verifier that names a ``base_url`` (REST, FHIR) is ``loopback`` when that
    URL is on this computer and ``external`` otherwise. A verifier without one
    is ``unknown``, which keeps a network substrate counted as egress.
    """
    base_url = getattr(verifier, "base_url", None)
    return endpoint_scope(base_url) if isinstance(base_url, str) else "unknown"


def _record_loop_iteration(
    report: RunReport,
    loop_state_id: str,
    row_index: int,
    row: Mapping[str, RuntimeParamScalar],
) -> None:
    """Record the row values one loop iteration binds, before it runs.

    ``report.params`` is the run's base scope. A row field overrides it for
    that iteration, so the audit report needs each row's own values to state
    what the run typed.
    """
    report.loop_iterations.append(
        LoopIterationBinding(
            loop_state_id=loop_state_id,
            row_index=row_index,
            params=dict(row),
        )
    )


def _loop_iterations_from_checkpoints(
    checkpoints: list[ProgramCheckpoint],
) -> list[LoopIterationBinding]:
    """Rebuild the loop rows that earlier legs of a resumed run bound.

    A resumed report carries the earlier legs' results, so it also needs the
    row values those results typed. Each checkpoint's frame stack names the
    loop row it ran in. A row is listed once, at its first checkpoint, keyed by
    its position in the frame stack so a nested loop's rows under different
    outer rows stay distinct.
    """
    seen: set[tuple[tuple[str, str, int], ...]] = set()
    bindings: list[LoopIterationBinding] = []
    for checkpoint in checkpoints:
        path: tuple[tuple[str, str, int], ...] = ()
        for frame in checkpoint.frames:
            cursor = frame.loop
            if cursor is None:
                path = (*path, (frame.graph_id, "", -1))
                continue
            path = (*path, (frame.graph_id, cursor.loop_state_id, cursor.row_index))
            if path in seen or not 0 <= cursor.row_index < len(cursor.rows):
                continue
            seen.add(path)
            bindings.append(
                LoopIterationBinding(
                    loop_state_id=cursor.loop_state_id,
                    row_index=cursor.row_index,
                    params=dict(cursor.rows[cursor.row_index]),
                )
            )
    return bindings


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


def _effect_read_wording(tier: Any) -> tuple[str, str]:
    """Name an effect check in a halt message by its evidence tier.

    A separate-session record-page read (tier 2) and a screen read-back
    (tiers 3 and 4) are not system-of-record reads, so their halt messages
    must not call them that. Untyped legacy verifiers keep the old wording.
    """

    from openadapt_flow.verification import VerificationTier

    if tier is VerificationTier.INDEPENDENT_SESSION:
        return "Effect verification", "record page read in a separate session"
    if tier in (
        VerificationTier.PERSISTED_STATE_REACQUISITION,
        VerificationTier.IMMEDIATE_SCREEN,
    ):
        return "Effect verification", "screen read-back"
    return "System-of-record effect verification", "system of record"


class EgressNotPermitted(RuntimeError):
    """Raised when an egress-capable model component (a grounder / identity-VLM
    / state-verifier that could send a screenshot off the box) is wired without
    the operator's explicit ``allow_model_grounding`` opt-in (PHI audit REM-3).
    """


#: How far past its demonstrated position (as a fraction of the frame's extent
#: along the scroll axis) a scroll target may still be before the closed-loop
#: scroll counts it as ready.
SCROLL_POSITION_SLACK_FRACTION = 0.25

#: Rungs that show the target itself on screen: what closed-loop scroll
#: readiness accepts, and all a step accepts after its scroll missed it.
_DIRECT_TARGET_RUNGS = frozenset({"structural", "template", "template_global", "ocr"})

#: Steps that own the halt when the closed-loop scroll before them never found
#: their target (see ``Replayer._act_scroll``).
_SCROLL_HANDOFF_ACTIONS = frozenset(
    {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK}
)


def _scroll_target_positioned(
    recorded: tuple[int, int],
    resolved: tuple[int, int],
    scroll_delta: tuple[int, int],
    frame_png: bytes,
) -> bool:
    """Is the resolved target no further along the scroll than recorded?"""

    dx, dy = scroll_delta
    w, h = _frame_viewport(frame_png)
    if dy:
        slack = SCROLL_POSITION_SLACK_FRACTION * h
        ahead = (resolved[1] - recorded[1]) if dy > 0 else (recorded[1] - resolved[1])
        if ahead > slack:
            return False
    if dx:
        slack = SCROLL_POSITION_SLACK_FRACTION * w
        ahead = (resolved[0] - recorded[0]) if dx > 0 else (recorded[0] - resolved[0])
        if ahead > slack:
            return False
    return True


def _identity_tokens(text: Any) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z]+", str(text or "").lower()) if t}


def identity_read_contradicts(check: Any) -> bool:
    """A ``mismatch`` read that saw text the expected identity doesn't have.

    "mismatch" also covers a read that is merely incomplete (columns cut
    off). Only a read that shows a token the expected record lacks (another
    MRN, a different spelling or date) is evidence of a different record.
    """

    if getattr(check, "status", None) != "mismatch":
        return False
    expected = _identity_tokens(getattr(check, "expected", None))
    observed = _identity_tokens(getattr(check, "observed", None))
    if not expected or not observed:
        return True  # nothing to compare: keep the refusal
    return not observed <= expected


def better_identity_read(first: Any, retry: Any) -> Any:
    """The identity read to keep from a first read and its upscaled re-read.

    A read that contradicts the expected record (:func:`identity_read_contradicts`)
    is final: a second read of the same screen must never turn it into
    ``verified``. A mismatch that is only incomplete can be rescued by a
    re-read that verifies. Otherwise ``verified`` beats the "cannot certify"
    outcomes, then a mismatch, then ``abstain``, then a blank ``unreadable``;
    a tie goes to the read that covered more of the expected identity.
    """

    if identity_read_contradicts(first):
        return first
    if identity_read_contradicts(retry):
        return retry
    rank = {"unreadable": 0, "abstain": 1, "mismatch": 2, "verified": 3}
    if (rank[retry.status], retry.coverage) > (rank[first.status], first.coverage):
        return retry
    return first


#: Server answers kept per step (a save makes one or a few write requests).
_MAX_SERVER_RESPONSES = 64


@dataclass(frozen=True)
class _WebFormApprovalView:
    """An API step's prepared request, as a person is asked to approve it."""

    prepared: Any
    #: :func:`~openadapt_flow.runtime.actuators.web_form.operation_digest`.
    digest: str
    rows: list[tuple[str, str]]
    png: bytes
    ref: Any


# The pixel census's OCR tie-break: a place the pixels do not rule out is
# distinguishable only when OCR reads both crops with every line at or above
# this confidence, neither text contains the other, and the two texts are less
# similar than this ratio. One label read two ways measured 0.82-0.95
# ("Smith, John" / "Smith, S John" / "Smith, 10 lohn"); sibling tab labels
# ("Contact" / "Stats") measure 0.5 or less.
PIXEL_CENSUS_OCR_MIN_CONFIDENCE = 0.8
PIXEL_CENSUS_OCR_MAX_SIMILARITY = 0.6


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
        known_ids: Optional complete ``runtime.known_ids.KnownIdList``. When
            set, an OCR identity check that abstained only because the band
            rests on a look-alike identifier is lifted to verified if the
            reads of three separate screenshots agree and exactly one listed
            identifier is in that look-alike class. None (default) keeps the
            abstain.
        identity_copy: Optional
            :class:`~openadapt_flow.runtime.identity_looks.IdentityCopyConfig`.
            With a ``read_clipboard`` channel, a look-alike abstain on a step
            with a recorded identifier cell takes one more look: select the
            cell, copy it, read the clipboard. The copy settles the record only
            when it equals a run parameter embedded in the band; it stops the
            run when it names another record or the screen changed outside the
            cell. Never taken in governed or qualification runs. None
            (default) takes no copy look.
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
        approval_gate: Optional per-item approval gate
            (:class:`openadapt_flow.approval_mode.gate.ApprovalGate`). When
            set, it is asked before every consequential save, at the last
            point before input delivery, and a refusal stops the item before
            the save so a person handles it. Its post-run decision is stamped
            on the report as ``approval``. It can only add refusals; it never
            changes an outcome word. None (default) leaves replay unchanged.
        surface_backends: Multi-surface workflows only
            (``Workflow.surfaces``, docs/MULTI_SURFACE.md): the live backend for
            each BACKEND surface the workflow switches to (a native app, an
            RDP/Citrix window), keyed by surface id. The primary surface is
            ``backend``; browser tabs and popups the run opens need nothing
            here. A switch to a surface with no supplied backend, or to one
            whose backend is of a different kind, halts. Ignored (and must be
            empty) for a single-surface workflow.
        attended_draft_by: Who attends this run of a JIT draft (the canary,
            :mod:`openadapt_flow.jit`): a person's name, or a registered
            ``openadapt_flow.jit.authority.Principal`` (an admitter key, or an
            agent under a valid signed admission policy). None (default): a bundle
            compiled from an agent run (``Workflow.origin``) runs only with a
            valid admission, on the origin its canary ran on and with an
            independent read-back from ``effect_verifier``; :meth:`run` raises
            ``DraftNotAdmitted`` otherwise.
        draft_key_dir: Where the JIT admission key lives, for that check.
            None uses ``$OPENADAPT_JIT_KEY_DIR`` or ``~/.openadapt/jit``.
    """

    # Display-scale state (see __init__); class defaults keep partially
    # constructed replayers, as some tests build them, on recorded geometry.
    _render_scale_attempted: bool = False
    _render_scale_inner: Optional[Backend] = None
    _render_scale_evidence: Optional[dict[str, Any]] = None
    identity_copy: Optional[looks_mod.IdentityCopyConfig] = None
    _identity_input_looks: bool = False

    def __init__(
        self,
        backend: Backend,
        *,
        vision: Optional[Any] = None,
        grounder: Optional[Any] = None,
        identity_vlm: Optional[Any] = None,
        known_ids: Optional[Any] = None,
        state_verifier: Optional[Any] = None,
        effect_verifier: Optional[EffectVerifier] = None,
        effect_compensator: Optional[Any] = None,
        api_actuator: Optional[Any] = None,
        web_form_actuator: Optional[Any] = None,
        api_steps: bool = True,
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
        approval_gate: Optional["ApprovalGate"] = None,
        surface_backends: Optional[Mapping[str, Backend]] = None,
        attended_draft_by: Optional[Any] = None,
        draft_key_dir: Optional[Path] = None,
        read_clipboard: Optional[Any] = None,
        identity_copy: Optional[looks_mod.IdentityCopyConfig] = None,
        deployment_surface: Optional[str] = None,
        layout_drift: Optional[bool] = None,
        layout_drift_tolerances: Optional[Any] = None,
        layout_drift_advisor: Optional[Callable[[Any], Optional[str]]] = None,
        secret_env_overrides: Optional[Mapping[str, str]] = None,
        readback_pipeline: Optional["ReadbackPipeline"] = None,
        render_scale: Optional[bool] = None,
    ) -> None:
        # Per-run secret sources (worklist workers): secret param name -> the
        # NAME of the env var holding its value, so each worker signs in with
        # its own account. Values are never stored here.
        self.secret_env_overrides: dict[str, str] = dict(secret_env_overrides or {})
        # Pipelined read-back (runtime/readback_pipeline.py); None = inline.
        self.readback_pipeline = readback_pipeline
        self._pipelined_readback: Optional["PipelinedReadback"] = None
        # A JIT draft (openadapt_flow.jit) runs only once admitted, or attended
        # by a named person (the canary). Checked in run().
        self._attended_draft_by = attended_draft_by
        self._draft_key_dir = draft_key_dir
        # READ steps: the clipboard of the driven session (a
        # records.read.ClipboardReader), or None for OCR-only reads. Values
        # read this run, by field name, for later steps' read_gate.
        self._read_clipboard = read_clipboard
        self._field_reads: dict[str, Any] = {}
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
        # Multi-surface routing (docs/MULTI_SURFACE.md). ``_surface_router`` is
        # set only while a multi-surface workflow runs; ``self.backend`` is then
        # the ACTIVE surface's backend, so every gate acts on that surface.
        self.surface_backends: dict[str, Backend] = dict(surface_backends or {})
        self._surface_router: Optional[SurfaceRouter] = None
        self._surface_environment_observations: dict[
            str, ObservedSurfaceEnvironment
        ] = {}
        # Font/layout drift check (runtime.layout_drift) on the frame each
        # step acts on, against the fingerprint the compiler stored in the
        # bundle. None = on for remote-desktop replays (rdp/citrix targets or
        # the FreeRDP backend), off otherwise; True/False forces it. The
        # advisor only annotates a halt; it is never called on a match.
        self.layout_drift = layout_drift
        self.layout_drift_tolerances = layout_drift_tolerances
        self.layout_drift_advisor = layout_drift_advisor
        self._layout_fingerprints: Optional[dict[str, Any]] = None
        self._layout_target_kind: Optional[str] = None
        # Display-scale normalization (runtime.render_scale). At the first
        # step with a recorded fingerprint, a frame that drifted from it gets
        # one scale estimate from the recorded labels; when the estimate
        # holds, the run continues on a view of the session resampled to the
        # recorded geometry. None = on wherever the layout-drift check is on;
        # True/False forces it.
        self.render_scale = render_scale
        self._render_scale_attempted = False
        self._render_scale_inner: Optional[Backend] = None
        self._render_scale_evidence: Optional[dict[str, Any]] = None
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
        # Where this runtime runs relative to the application (deployment.yaml
        # ``backend.deployment_surface``): ``local``, ``remote_in_session``,
        # ``remote_display`` or ``unknown``. None derives it from the run's
        # execution target (rdp / citrix => remote_display). A workflow
        # qualified with structure lookups halts before any action when the
        # resolved surface offers pixels only (see lookup_capabilities).
        self.deployment_surface = deployment_surface
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
        # API steps compiled from a recording's network capture (web_form
        # bindings, docs/API_TIER.md). An admitted one runs in place of the
        # GUI steps it stands for, through the live browser session; when a
        # precondition fails before the save is sent, those GUI steps run.
        # ``api_steps=False`` keeps every run on the GUI. ``web_form_actuator``
        # replaces the live-session actuator (tests, other transports).
        self.api_steps = api_steps
        #: Why ``api_steps`` is off, when a caller says (for the report).
        self.api_steps_off_reason: Optional[str] = None
        self.web_form_actuator = web_form_actuator
        self._web_form_span_active = False
        self._web_form_fallback_reason: Optional[str] = None
        # (step id, reason) set when a closed-loop scroll used up its budget
        # because the next step's target never resolved. That step then
        # resolves on direct deterministic rungs only, and halts on its own
        # target, so the halt names the control that changed, not the scroll.
        self._scroll_missed_target: Optional[tuple[str, str]] = None
        # Under an approval mode, the API request a person was asked about
        # (prepared before the gate), keyed by the save step's result, and
        # the operation digest the gate's last-point check re-reads.
        self._web_form_approval: dict[int, Any] = {}
        self._web_form_form_state: Optional[tuple[str, str]] = None
        self._web_form_form_state_set = False
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
        # Optional complete known-ID list (runtime.known_ids): lifts a
        # look-alike identifier abstain only when no listed twin exists.
        self.known_ids = known_ids
        # Optional select-and-copy look for look-alike identifiers
        # (runtime.identity_looks). It is input, so only the step-entry
        # identity check takes it; the actuation-time check reuses its read
        # through a pixel-matched binding.
        self.identity_copy = identity_copy
        self._identity_input_looks = False
        self._identity_look_bindings: dict[str, looks_mod.LookBinding] = {}
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
        # Settle reuse (speed, not a gate): the after-action settled frame of
        # the previous step, valid as the first frame of the next step's
        # before-settle only while no input has been delivered since. Every
        # backend input crosses ``_deliver_backend_call``, which bumps the
        # epoch and so invalidates the seed.
        self._actuation_epoch = 0
        self._settle_seed: Optional[tuple[Any, int]] = None
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
        if approval_gate is not None:
            from openadapt_flow.approval_mode.gate import ApprovalGate

            if not isinstance(approval_gate, ApprovalGate):
                raise ValueError("approval_gate must be an approval_mode ApprovalGate")
        self.approval_gate = approval_gate
        self._approval_report: Optional[RunReport] = None
        self._approval_run_dir: Optional[Path] = None
        self._approval_refusal: Optional[str] = None
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
        execution_target_origin: Optional[str] = None,
        prior_screenshots_may_leave_box: bool = False,
        prior_model_calls: int = 0,
        prior_external_network_calls: Literal[
            "none", "observed", "unknown"
        ] = "unknown",
    ) -> RunReport:
        """Execute the workflow and write a run directory.

        See :meth:`_run_unsealed` for the run itself. When ``bundle_dir`` is a
        sealed bundle (:mod:`openadapt_flow.sealed_set`), the run directory is
        a sealed set too: the report, step screenshots, identity and
        resolution crops and every other run artifact are encrypted in memory
        before they are written, and durable checkpoints use the same key.
        No plaintext of the bundle or of the live screen reaches the disk.
        """
        bundle_path = Path(bundle_dir)
        if (
            not (
                _sealed.is_sealed(bundle_path)
                or _sealed.active_writer(bundle_path) is not None
            )
            or _sealed.active_writer(Path(run_dir)) is not None
        ):
            return self._run_unsealed(
                workflow,
                params=params,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                save_healed_to=save_healed_to,
                resume_from=resume_from,
                resume_program=resume_program,
                run_id=run_id,
                idempotency_key=idempotency_key,
                execution_target_kind=execution_target_kind,
                surface_override=surface_override,
                execution_origin=execution_origin,
                execution_entry_url=execution_entry_url,
                execution_target_origin=execution_target_origin,
                prior_screenshots_may_leave_box=prior_screenshots_may_leave_box,
                prior_model_calls=prior_model_calls,
                prior_external_network_calls=prior_external_network_calls,
            )
        passphrase = _sealed.passphrase_for("bundle")
        if not passphrase:
            raise _sealed.SealedSetKeyError(
                _sealed.missing_key_message(bundle_path, "bundle")
            )
        unsupported = self._sealed_run_unsupported(workflow)
        if unsupported is not None:
            # Refuse before anything is actuated or written: these stores
            # keep their own plaintext state files in the run directory.
            raise _sealed.SealedSetError(
                f"a sealed bundle cannot run with {unsupported} yet, because "
                "that state is written to the run directory in plaintext. "
                "Nothing was executed."
            )
        if self.checkpoint_key is None:
            # A sealed bundle's run keeps its durable state under the same key.
            self.checkpoint_key = passphrase
        run_path = Path(run_dir)
        writer = _sealed.SealedSetWriter(
            run_path, "run", passphrase, resume=_sealed.is_sealed(run_path)
        )
        _sealed.register_writer(writer)
        try:
            report = self._run_unsealed(
                workflow,
                params=params,
                worklists=worklists,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                save_healed_to=save_healed_to,
                resume_from=resume_from,
                resume_program=resume_program,
                run_id=run_id,
                idempotency_key=idempotency_key,
                execution_target_kind=execution_target_kind,
                surface_override=surface_override,
                execution_origin=execution_origin,
                execution_entry_url=execution_entry_url,
                execution_target_origin=execution_target_origin,
                prior_screenshots_may_leave_box=prior_screenshots_may_leave_box,
                prior_model_calls=prior_model_calls,
                prior_external_network_calls=prior_external_network_calls,
            )
        finally:
            _sealed.unregister_writer(writer)
        writer.finalize(adopt_external=True)
        return report

    def _sealed_run_unsupported(self, workflow: Workflow) -> Optional[str]:
        """Name a run feature whose run-directory state is not sealed yet."""
        if self.approval_gate is not None:
            return "an approval gate (attended approvals)"
        if self._attended_draft_by is not None:
            return "an attended draft"
        program = workflow.program
        if program is not None:
            graphs = [program, *(getattr(workflow, "subflows", None) or {}).values()]
            for graph in graphs:
                for state in getattr(graph, "states", {}).values():
                    if getattr(state, "kind", None) == StateKind.BUSINESS_DECISION:
                        return "a business-decision state"
        return None

    def _run_unsealed(self, workflow: Workflow, **kwargs: Any) -> RunReport:
        """Run the workflow; see :meth:`_run_unsealed_steps`.

        However the run ends, a deferred record check it still holds is
        released: a durable run's check waits for its pause, and a run that
        left before writing it must not block the read-back thread forever.
        """

        try:
            return self._run_unsealed_steps(workflow, **kwargs)
        finally:
            entry = getattr(self, "_pipelined_readback", None)
            start = getattr(entry, "start", None)
            if start is not None:
                start.set()

    def _run_unsealed_steps(
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
        execution_target_origin: Optional[str] = None,
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
            execution_target_origin: For a non-web run, the app this run
                drives, as ``<kind>://<target>`` (see
                :func:`openadapt_flow.surface_selection.surface_target_origin`).
                Only an admitted JIT program's canary match reads it; it is
                not written to the report, which keeps ``execution_origin``
                for browser origins.
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
        # Layout drift: remember the target kind and reload the recorded
        # fingerprints for this run.
        self._layout_target_kind = execution_target_kind
        self._layout_fingerprints = None
        self._reset_render_scale()
        production_refusal = self._production_qualification_refusal(workflow)
        if production_refusal is not None:
            raise ValueError(production_refusal)
        campaign_refusal = self._qualification_campaign_refusal(workflow)
        if campaign_refusal is not None:
            raise ValueError(campaign_refusal)
        if bundle_dir is not None:
            # Every execution path runs through here, so a JIT draft with no
            # valid admission, or an admitted one run on another app or
            # without its independent read-back, is refused before any action.
            from openadapt_flow.jit.lifecycle import ensure_runnable

            ensure_runnable(
                bundle_dir,
                workflow=workflow,
                attended_by=self._attended_draft_by,
                key_dir=self._draft_key_dir,
                execution_origin=execution_origin or execution_target_origin,
                effect_verifier=self.effect_verifier,
                at_run=True,
                stops_before_save=bool(
                    getattr(self.approval_gate, "stops_before_first_save", False)
                ),
            )
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
        self._field_reads = {}
        self._scroll_missed_target = None
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
        prior_external_network_calls = self._initial_external_network_calls(
            prior_external_network_calls
        )
        caller_param_names = frozenset(params or {})
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
        # The run's own parameters, for postconditions that compare the screen
        # with a value (a program run also tracks them per state).
        self._run_params: dict[str, RuntimeParamScalar] = dict(params)
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
        self._begin_approval_item(
            report,
            run_dir,
            inputs_from_recording=sorted(set(params) - caller_param_names),
            durable_resume=durable_resume,
            prior_screenshots_may_leave_box=prior_screenshots_may_leave_box,
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
        lookup_refusal = self._lookup_capability_refusal(
            workflow, execution_target_kind
        )
        if lookup_refusal is not None:
            report.results.append(
                StepResult(
                    step_id="<lookup-capabilities>",
                    intent="check this surface offers the qualified lookups",
                    ok=False,
                    safety_halt=True,
                    failure_category="safety_halt",
                    error=lookup_refusal,
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

        # A typed date parameter must be a canonical ISO date before any step
        # runs: a date written another way (03/14/1990) would otherwise be
        # typed into the form and saved as whatever the app makes of it.
        malformed = self._malformed_date_params(workflow, params)
        if malformed:
            report.results.append(
                StepResult(
                    step_id="<params>",
                    intent="validate typed workflow parameters",
                    ok=False,
                    failure_category="governed_refusal",
                    error=(
                        "Typed workflow parameter(s) are not valid: "
                        + "; ".join(malformed)
                        + " — refusing to run; nothing was typed; run aborted"
                    ),
                )
            )
            report.success = False
            report.total_ms = (time.monotonic() - t_run) * 1000.0
            return self._finalize_report(report, workflow, run_dir)

        if self._approval_refusal is not None:
            # Approval modes take values only from the caller: an input the
            # run would fill from the recording is refused before any step.
            report.results.append(
                StepResult(
                    step_id="<params>",
                    intent="validate approval-mode inputs",
                    ok=False,
                    failure_category="governed_refusal",
                    error=self._approval_refusal,
                )
            )
            report.success = False
            report.total_ms = (time.monotonic() - t_run) * 1000.0
            return self._finalize_report(report, workflow, run_dir)

        surface_refusal = self._begin_surfaces(
            workflow,
            resume=resume_from is not None or resume_program is not None,
            resume_from=resume_from,
            resume_program=resume_program is not None,
            bundle_dir=bundle_dir,
        )
        if surface_refusal is not None:
            report.results.append(
                StepResult(
                    step_id="<surfaces>",
                    intent="bind the workflow's declared surfaces",
                    ok=False,
                    failure_category="governed_refusal",
                    error=surface_refusal,
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
            span_skip_to = -1
            stop_run = False
            # A durable run defers only the last step's record check: nothing
            # in the item then waits on it, and its pause is the item's end.
            self._linear_final_step_id = (
                workflow.steps[-1].id if workflow.steps else None
            )
            for step_index, step in enumerate(workflow.steps):
                # Resume: never re-execute an already-verified step (no
                # re-performed confirmed writes); its result was pre-loaded.
                if resume_from is not None and step_index < resume_from:
                    continue
                if step_index <= span_skip_to:
                    continue
                if resume_from is not None and step_index == resume_from:
                    inside = self._resume_inside_web_form_span(
                        workflow, step_index, report.results
                    )
                    if inside is not None:
                        # The folded steps are checkpointed only after the
                        # save was sent, so this save may already have
                        # landed. It is never sent again and never redone
                        # through the GUI on an empty form.
                        self._record_linear_result(
                            report,
                            workflow,
                            inside,
                            step_index=step_index,
                            step=step,
                            params=params,
                            durable_run=durable_run,
                        )
                        break
                # An admitted API step (docs/API_TIER.md) runs in place of the
                # GUI steps it stands for. When it can't, the reason lands on
                # the first of those steps and the GUI steps run as recorded.
                batch: Optional[list[StepResult]] = None
                why_not: Optional[str] = None
                span = self._web_form_span_at(
                    workflow,
                    step_index,
                    durable=durable_run is not None or resume_from is not None,
                )
                if span is not None:
                    end_index, _save_step, why_not = span
                    if why_not is None:
                        batch = self._run_web_form_span(
                            workflow,
                            step_index,
                            end_index,
                            params=params,
                            bundle_dir=bundle_dir,
                            run_dir=run_dir,
                            new_crops=new_crops,
                        )
                        if batch is None:
                            why_not = self._web_form_fallback_reason
                        else:
                            span_skip_to = end_index
                if batch is None:
                    result = self._run_step(
                        step,
                        workflow=workflow,
                        step_index=step_index,
                        params=params,
                        bundle_dir=bundle_dir,
                        run_dir=run_dir,
                        new_crops=new_crops,
                    )
                    if why_not is not None:
                        result.api_fallback = why_not
                    batch = [result]
                # An API span's folded steps are checkpointed only once its
                # save was sent: a resume then starts at the save, which can
                # only be reconciled, never redone through the GUI. A save
                # that wasn't sent leaves the resume point before the span,
                # so a resume runs the whole span again.
                save_result = batch[-1]
                checkpoint_folded = save_result.ok or save_result.delivery_attempted
                for offset, result in enumerate(batch):
                    folded = offset < len(batch) - 1
                    stop_run = self._record_linear_result(
                        report,
                        workflow,
                        result,
                        step_index=step_index + offset,
                        step=workflow.steps[step_index + offset],
                        params=params,
                        durable_run=(
                            None if folded and not checkpoint_folded else durable_run
                        ),
                    )
                    if stop_run:
                        break
                if stop_run:
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
                if _sealed.is_sealed_path(candidate):
                    # Encrypted run directory: the rename is a sealed rewrite.
                    _sealed.write_bytes(
                        run_dir / "report.json", _sealed.read_bytes(candidate)
                    )
                    _sealed.unlink(candidate)
                else:
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

    def _record_linear_result(
        self,
        report: RunReport,
        workflow: Workflow,
        result: StepResult,
        *,
        step_index: int,
        step: Step,
        params: Mapping[str, RuntimeParamScalar],
        durable_run: Optional[Any],
    ) -> bool:
        """Account one linear-loop result; True when the run must stop."""

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
                f"but the workflow changed during delivery{prior_error}"
            )
        report.results.append(result)
        self._account_result(report, result)
        if durable_run is not None:
            self._sync_durable_audit(durable_run, report)
            # Tier-3: verified step -> checkpoint; halt -> pending
            # escalation (resumable from the last checkpoint).
            if post_action_refusal is None:
                router = self._surface_router
                if router is not None and result.ok:
                    router.observe_active(step.id)
                deferred = getattr(self, "_pipelined_readback", None)
                if (
                    deferred is not None
                    and getattr(deferred, "result", None) is result
                    and result.ok
                ):
                    # The save was sent and its record check runs alongside
                    # the next item: the durable pause holds the check, so a
                    # crash resumes the check and never resends the save.
                    durable_run.record_deferred_readback(
                        step_index,
                        step,
                        result,
                        dict(params),
                        actuation=deferred.actuation,
                        before=deferred.before.model_dump(mode="json"),
                        active_surface=(
                            router.active_id if router is not None else None
                        ),
                        surface_observations=(
                            router.observations if router is not None else None
                        ),
                    )
                    deferred.durable_run = durable_run
                    deferred.step_index = step_index
                    deferred.params = dict(params)
                    deferred.workflow = workflow
                    deferred.active_surface = (
                        router.active_id if router is not None else None
                    )
                    deferred.surface_observations = (
                        router.observations if router is not None else None
                    )
                    deferred.start.set()
                    return False
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
                    active_surface=(router.active_id if router is not None else None),
                    surface_observations=(
                        router.observations if router is not None else None
                    ),
                )
                if (
                    self._durable_continuation_guard is not None
                    and result.failure_category != "continuation_preempted"
                ):
                    self._durable_continuation_guard.acknowledge_progress(
                        terminal=not result.ok
                    )
        return not result.ok

    def _web_form_span_at(
        self, workflow: Workflow, step_index: int, *, durable: bool
    ) -> Optional[tuple[int, Step, Optional[str]]]:
        """The API step whose span starts at ``step_index``, and why it can't run.

        Returns ``(save_index, save_step, reason)``; ``reason`` is None when
        the API step may run. None when no API step starts here.
        """

        from openadapt_flow.runtime.actuators.web_form import folded_checks_digest

        first = workflow.steps[step_index].id
        for end_index in range(step_index, len(workflow.steps)):
            save = workflow.steps[end_index]
            binding = save.api_binding
            if (
                binding is None
                or binding.kind != "web_form"
                or binding.web_form is None
                or binding.web_form.replaces_steps[0] != first
            ):
                continue
            ids = [s.id for s in workflow.steps[step_index : end_index + 1]]
            if not self.api_steps:
                reason = self.api_steps_off_reason or "API steps are off for this run"
            elif surface_reason := self._web_form_surface_refusal(
                workflow, step_index, end_index, binding
            ):
                reason = surface_reason
            elif self._approval_gated() and not callable(
                getattr(self._actuator_for(binding), "prepare", None)
            ):
                # Under an approval mode the person approves the exact request
                # the API step sends. An actuator that can't prepare the
                # request before sending it can't show it.
                reason = (
                    "approval mode needs the exact request before the save, and "
                    "this API actuator can't prepare it, so the GUI steps run"
                )
            elif not binding.admitted():
                reason = "the API step is not admitted (openadapt-flow api admit)"
            elif binding.web_form.other_writes_seen:
                # Proposed before such spans were refused: the steps it would
                # replace also wrote, and one request can't stand in for them.
                reason = (
                    "the steps the API step would replace also sent other writes "
                    f"({', '.join(binding.web_form.other_writes_seen)})"
                )
            elif save.identity_armed and not binding.identity:
                # The GUI save checks the right record on screen. The API step
                # must check it too, so it needs a right-record contract.
                reason = (
                    "the save checks the right record on screen, and the API "
                    "step names no right-record contract (openadapt-flow api "
                    "admit --identity)"
                )
            elif list(binding.effects) != list(save.effects):
                reason = (
                    "the save step's record check differs from the one admitted "
                    "with the API step (openadapt-flow api admit)"
                )
            elif ids != list(binding.web_form.replaces_steps):
                reason = "the program's steps no longer match the API step"
            elif (
                folded_checks_digest(workflow.steps[step_index : end_index + 1])
                != binding.web_form.folded_checks_digest
            ):
                reason = (
                    "the steps the API step replaces changed after it was "
                    "proposed (an action, value, target, check, guard or record "
                    "check), so the API step no longer stands for them "
                    "(openadapt-flow api propose)"
                )
            elif self.governed_authorization is not None and (
                governed_reason := self._governed_web_form_refusal(workflow, save)
            ):
                reason = governed_reason
            elif self._actuator_for(binding) is None:
                reason = "there is no live browser session for the API step"
            else:
                reason = None
            return end_index, save, reason
        return None

    def _web_form_surface_refusal(
        self, workflow: Workflow, start_index: int, end_index: int, binding: Any
    ) -> Optional[str]:
        """Why a multi-surface run can't use this API step, or None when it can.

        The API step is bound to its span's surface the way it is bound to the
        page in a single-surface run: every step it replaces runs on that one
        browser surface, none of them switches, opens or closes a surface, the
        surface is the active one, and the request goes to that surface's
        session on its declared origin.
        """

        router = getattr(self, "_surface_router", None)
        if router is None:
            return None
        span = workflow.steps[start_index : end_index + 1]
        surfaces = {s.surface or router.primary_id for s in span}
        if len(surfaces) != 1:
            return (
                "the steps the API step replaces run on more than one surface, "
                "so the GUI steps run"
            )
        if any(
            s.action is ActionKind.SWITCH_SURFACE or s.opens or s.closes_surface
            for s in span
        ):
            return (
                "the steps the API step replaces switch, open or close a "
                "surface, so the GUI steps run"
            )
        (surface_id,) = surfaces
        if surface_id != router.active_id:
            return "the API step's surface is not the active one, so the GUI steps run"
        spec = router.specs[surface_id]
        if spec.kind != "web":
            return "the API step's surface is not a browser, so the GUI steps run"
        actuator = self._actuator_for(binding)
        if actuator is None:
            return "there is no live browser session for the API step"
        from openadapt_flow.runtime.surfaces import url_origin

        if spec.is_page:
            expected = spec.origin
        else:
            seen = router.observations.get(surface_id)
            expected = seen.origin if seen is not None else None
        actual = url_origin(
            getattr(getattr(actuator, "transport", None), "base_url", None)
        )
        if expected is not None and actual != url_origin(expected):
            return (
                "the API step's session is on another origin than its surface, "
                "so the GUI steps run"
            )
        return None

    def _governed_web_form_refusal(
        self, workflow: Workflow, save: Step
    ) -> Optional[str]:
        """Why a governed run can't use this API step, or None when it can.

        The run's authorization must have admitted this exact API step. For a
        managed (Cloud) dispatch that is Cloud's authorization, whose
        ``api_steps`` are inside the dispatch binding digest Cloud signs.

        A qualification case runs the path its contract names for the save:
        the API step when that path is ``api``, the GUI steps otherwise. A run
        under a production qualification admission uses the API step only
        when the qualified effect contract, which the signed admission binds,
        names the save's API path; that contract exists only when the
        qualification campaign ran the API path's cases.
        """

        authorization = self.governed_authorization
        assert authorization is not None
        if authorization.qualification_case_id is not None:
            if authorization.qualification_case_action_paths.get(save.id) != "api":
                return "a qualification case runs the GUI path it names"
        elif authorization.production_qualification_admission_id is not None:
            if not authorization.effect_requirements(save.id, "api"):
                return (
                    "the production qualification admission didn't qualify this "
                    "API step's path, so the qualified GUI path runs"
                )
        return authorization.admitted_api_step(save, workflow)

    def _resume_inside_web_form_span(
        self, workflow: Workflow, step_index: int, prior: list[StepResult]
    ) -> Optional[StepResult]:
        """A halt result when a resume would start inside an API step's span.

        Returns None when ``step_index`` isn't a covered step after the first,
        or when the steps before it ran on the GUI (``prior`` holds the
        checkpointed results): then the GUI save resumes as usual.
        """

        step = workflow.steps[step_index]
        if not prior or prior[-1].actuation != "api_folded":
            return None
        for save in workflow.steps[step_index:]:
            binding = save.api_binding
            if (
                binding is None
                or binding.kind != "web_form"
                or binding.web_form is None
            ):
                continue
            covered = list(binding.web_form.replaces_steps)
            if step.id in covered[1:]:
                return StepResult(
                    step_id=step.id,
                    intent=step.intent,
                    ok=False,
                    safety_halt=True,
                    failure_category=(
                        "governed_refusal"
                        if self.governed_authorization is not None
                        else "safety_halt"
                    ),
                    risk=step.risk,
                    risk_explanation=step.risk_explanation,
                    risk_review_required=step.risk_review_required,
                    delivery_attempted=False,
                    error=(
                        f"Step '{step.id}' ({step.intent}): resume would start "
                        f"inside the API step for '{save.id}', whose save was "
                        "sent and may have landed. Resume never sends it again "
                        "and never redoes it through the GUI. Reconcile it "
                        "(the attended reconcile action reads the record back), "
                        "or start the item again"
                    ),
                )
        return None

    def _run_web_form_span(
        self,
        workflow: Workflow,
        start_index: int,
        end_index: int,
        *,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        new_crops: dict[str, bytes],
    ) -> Optional[list[StepResult]]:
        """Run the API step for steps ``start_index..end_index``.

        Returns the results (the folded GUI steps, then the save) when the API
        step took responsibility: it saved and the record check ran, or it
        stopped after sending. Returns None when the save was not sent, with
        the reason in ``self._web_form_fallback_reason``; the caller then runs
        the GUI steps.
        """

        save = workflow.steps[end_index]
        self._web_form_span_active = True
        try:
            result = self._run_step(
                save,
                workflow=workflow,
                step_index=end_index,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                new_crops=new_crops,
            )
        finally:
            self._web_form_span_active = False
            self._web_form_approval.clear()
        if result.api_fallback is not None:
            self._web_form_fallback_reason = result.api_fallback
            return None
        router = getattr(self, "_surface_router", None)
        surface = router.active_id if router is not None else None
        folded = [
            StepResult(
                step_id=s.id,
                intent=s.intent,
                ok=True,
                skipped=True,
                actuation="api_folded",
                surface=surface,
                risk=s.risk,
                risk_explanation=s.risk_explanation,
                risk_review_required=s.risk_review_required,
                delivery_attempted=False,
            )
            for s in workflow.steps[start_index:end_index]
        ]
        return [*folded, result]

    def _lookup_capability_refusal(
        self,
        workflow: Workflow,
        execution_target_kind: Optional[ExecutionTargetKind],
    ) -> Optional[str]:
        """Refuse a surface that offers fewer lookups than were qualified.

        A workflow qualified with structure lookups (DOM / accessibility)
        must not run where only pixels exist, such as an RDP or Citrix window
        viewed from outside the session: it passed qualification on evidence
        this surface cannot produce. Checked before any action.
        """

        from openadapt_flow.lookup_capabilities import (
            DeploymentSurface,
            declared_surface_problem,
            lookup_capability_refusal,
            qualified_lookup_of,
            resolve_deployment_surface,
        )

        if qualified_lookup_of(workflow) is None:
            return None
        kind = execution_target_kind or workflow.surface
        surface: DeploymentSurface
        if declared_surface_problem(self.deployment_surface, kind) is not None:
            # A declaration that contradicts the backend is read the
            # conservative way: the remote session is viewed from outside.
            surface = "remote_display"
        else:
            try:
                surface = resolve_deployment_surface(self.deployment_surface, kind)
            except ValueError:
                surface = "remote_display"
        structure_available = (
            surface != "remote_display"
            and self.use_structural
            and callable(getattr(self.backend, "locate_structural", None))
        )
        return lookup_capability_refusal(
            workflow, surface=surface, structure_available=structure_available
        )

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

        pending = getattr(self, "_pipelined_readback", None)
        handed_off = False
        if pending is not None:
            if persist and report.success and self.readback_pipeline is not None:
                handed_off = True
                # Until the read-back settles, the saved report must not say
                # success: if the process dies first, it stays unsuccessful.
                report.success = False
                report.execution_completed = False
            else:
                # The run ended some other way: settle the read-back inline
                # before the report is written.
                error = self._join_pipelined_readback(keep=True)
                error = self._settle_durable_readback(pending, error)
                self._apply_pipelined_verdict(report, error)
                self._pipelined_readback = None
        self._end_surfaces()
        if self._surface_environment_observations:
            report.observed_surface_environments = dict(
                self._surface_environment_observations
            )
        report.qualification_fault_mutations = list(self._qualification_fault_mutations)
        report.render_scale = self._render_scale_evidence
        from openadapt_flow.runtime.variant_stamp import current_variant_route

        if (variant_route := current_variant_route()) is not None:
            report.variant_route = variant_route
        self._stamp_execution_outcome(report, workflow, run_dir)
        hold = getattr(self.approval_gate, "hold_for_readback", None)
        held = False
        deadline_s = 0.0
        if handed_off and callable(hold):
            held = True
            # The item's approval decision waits for its record check: the
            # settled verdict decides it (_settle_pipelined_readback). Deciding
            # now, on the held report, would read a save still being checked
            # as an uncertain result and switch problems_only off. The wait
            # is bounded: past the deadline the held report decides it.
            assert pending is not None
            step = next((s for s in workflow.steps if s.id == pending.step_id), None)
            deadline_s = (
                float(getattr(step, "timeout_s", 0.0) or 0.0)
                + READBACK_DEADLINE_MARGIN_S
            )
            hold(deadline_s)
        else:
            self._stamp_approval(report)
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
        if handed_off:
            assert pending is not None and self.readback_pipeline is not None
            self._pipelined_readback = None
            entry = pending
            decided = threading.Lock()
            first: list[str] = []

            def _claim(by: str) -> bool:
                """Whether ``by`` is first to decide the item's approval."""

                with decided:
                    if not first:
                        first.append(by)
                    return first[0] == by

            timer: Optional[threading.Timer] = None
            if held:

                def _expire() -> None:
                    if not _claim("deadline"):
                        return
                    # The record check outlived its deadline. The held report
                    # still says unsuccessful, so deciding it now sends the
                    # item to a person, turns problems_only off and releases
                    # the hold.
                    self._stamp_approval(report)
                    report.save(run_dir)

                timer = threading.Timer(deadline_s, _expire)
                timer.daemon = True
                timer.start()

            def _settle(error: Optional[str]) -> RunReport:
                if timer is not None:
                    timer.cancel()
                return self._settle_pipelined_readback(
                    report,
                    workflow,
                    run_dir,
                    entry,
                    error,
                    decide_approval=_claim("settle"),
                )

            self.readback_pipeline.hand_off(entry, _settle)
            entry.start.set()
        return report

    # -- pipelined read-back (runtime/readback_pipeline.py) ------------------

    def _may_pipeline_readback(
        self, verifier: Any, step: Optional[Step] = None
    ) -> bool:
        """Whether this effect's read-back may run on the side thread.

        A durable run defers the record check only when the durable pause
        can hold it (docs/THROUGHPUT.md): a linear run, an encrypted
        checkpoint store, and the item's last step, so nothing else in the
        item waits on the check.
        """

        pipeline = getattr(self, "readback_pipeline", None)
        if pipeline is None:
            return False
        reason = None
        if getattr(self, "_program_durable", None) is not None:
            reason = "durable program runs verify inline"
        elif self.durable and self._durable_continuation_guard is not None:
            reason = "a resumed durable run verifies inline"
        elif self.durable and not self.checkpoint_key:
            reason = (
                "a durable run without an encrypted checkpoint store verifies "
                "inline (the deferred check keeps record rows)"
            )
        elif self.durable and (
            step is None or step.id != getattr(self, "_linear_final_step_id", None)
        ):
            reason = (
                "a durable run defers only its last step's record check; this "
                "one verifies inline"
            )
        elif getattr(self, "idempotency_ledger", None) is not None:
            reason = "idempotency-ledger runs verify inline"
        elif getattr(self, "_pipelined_readback", None) is not None:
            reason = "an earlier read-back in this item is still pending"
        else:
            target = verifier if verifier is not None else self.effect_verifier
            if not _pipeline_safe_verifier(target):
                reason = (
                    "only REST, FHIR, GraphQL and SQL read-backs run alongside "
                    "the next item; this one verifies inline"
                )
        pipeline.inline_reason = reason
        return reason is None

    def _defer_readback(
        self,
        step: Step,
        before: EffectState,
        result: StepResult,
        *,
        effects: Optional[list["Effect"]],
        verifier: Any,
        actuation: Literal["gui", "api"] = "gui",
    ) -> None:
        assert self.readback_pipeline is not None
        scratch = StepResult(step_id=step.id, intent=step.intent, ok=True)
        # A durable run's check starts only once the durable pause holds it,
        # so a crash in between can't leave a sent save without its pause.
        # Whatever waits on the check also starts it.
        start = threading.Event()
        if not self.durable:
            start.set()

        def verify() -> Optional[str]:
            start.wait()
            return self._verify_effects(
                step, before, scratch, effects=effects, verifier=verifier
            )

        entry = self.readback_pipeline.submit(step.id, verify)
        entry.start = start  # type: ignore[attr-defined]
        entry.scratch = scratch  # type: ignore[attr-defined]
        entry.result = result  # type: ignore[attr-defined]
        entry.intent = step.intent  # type: ignore[attr-defined]
        # What a durable pause needs to re-run this check after a crash.
        entry.step = step  # type: ignore[attr-defined]
        entry.before = before  # type: ignore[attr-defined]
        entry.actuation = actuation  # type: ignore[attr-defined]
        entry.durable_run = None  # type: ignore[attr-defined]
        result.effect_verified = None
        result.effect_results.append("[pipelined] system-of-record read-back pending")
        self._pipelined_readback = entry

    def _join_pipelined_readback(self, *, keep: bool = False) -> Optional[str]:
        """Wait for a pending read-back, apply it to its step, return its error."""

        entry = getattr(self, "_pipelined_readback", None)
        if entry is None:
            return None
        start = getattr(entry, "start", None)
        if start is not None:
            start.set()
        try:
            error = entry.verdict.result()
        except Exception as exc:  # noqa: BLE001 - a raising verifier fails closed
            error = f"read-back raised {type(exc).__name__}; reconciliation is required"
        self._copy_pipelined_evidence(entry, error)
        if not keep:
            self._pipelined_readback = None
        return error

    def _copy_pipelined_evidence(self, entry: Any, error: Optional[str]) -> None:
        result: StepResult = entry.result
        scratch: StepResult = entry.scratch
        result.effect_results = [
            r for r in result.effect_results if not r.startswith("[pipelined]")
        ]
        result.effect_results.extend(scratch.effect_results)
        result.effect_evidence.extend(scratch.effect_evidence)
        result.effect_field_checks.extend(scratch.effect_field_checks)
        result.effect_verified = scratch.effect_verified
        if scratch.failure_category is not None:
            result.failure_category = scratch.failure_category
        if error is not None:
            result.ok = False
            result.error = error
            result.safety_halt = True
            if result.failure_category is None:
                result.failure_category = (
                    "governed_refusal"
                    if self.governed_authorization is not None
                    else "safety_halt"
                )
        else:
            result.safety_halt = result.safety_halt or scratch.safety_halt

    def _apply_pipelined_verdict(
        self,
        report: RunReport,
        error: Optional[str],
        *,
        entry: Optional["PipelinedReadback"] = None,
    ) -> None:
        if error is None:
            return
        report.success = False
        report.execution_completed = False
        report.terminal_outcome = "halt"
        report.results.append(
            StepResult(
                step_id="<terminal>",
                intent="program halt",
                ok=False,
                error=error,
                safety_halt=True,
            )
        )
        if entry is None:
            entry = getattr(self, "_pipelined_readback", None)
        report.halt = HaltObservation(
            state_id=getattr(entry, "step_id", "") or "",
            intent=getattr(entry, "intent", "") or "",
            reason=error,
            outcome="halt",
            # The screen now belongs to the next item; nothing is probed.
            observed_texts=[],
            completed_intents=[
                r.intent for r in report.results if r.ok and not r.skipped
            ],
        )

    def _settle_pipelined_readback(
        self,
        report: RunReport,
        workflow: Workflow,
        run_dir: Path,
        entry: "PipelinedReadback",
        error: Optional[str],
        *,
        decide_approval: bool = True,
    ) -> RunReport:
        """Apply a finished deferred read-back to the saved item report.

        ``decide_approval`` is False when the read-back's deadline already
        decided the item's approval; a late verdict doesn't change it.
        """

        try:
            self._copy_pipelined_evidence(entry, error)
            error = self._settle_durable_readback(entry, error)
            if error is not None:
                self._apply_pipelined_verdict(report, error, entry=entry)
            else:
                # The execution completed; it was held unsuccessful only until
                # the read-back confirmed. Re-derive the outcome from that
                # evidence.
                report.execution_completed = True
                report.success = True
            self._stamp_execution_outcome(report, workflow, run_dir)
        finally:
            # The held approval decision is always made, even when settling
            # fails: the report then still says unsuccessful, so the item goes
            # to a person, and it never stays held open for the next item.
            if decide_approval:
                self._stamp_approval(report)
        report.save(run_dir)
        return report

    def recheck_deferred_readback(
        self,
        workflow: Workflow,
        deferred: Any,
        *,
        params: Mapping[str, RuntimeParamScalar],
        run_id: str,
    ) -> StepResult:
        """Re-run a durable pause's deferred record check; send nothing.

        The check runs against the snapshot of the system of record taken
        before the save, with this replayer's system-of-record verifier.
        Returns the save's result with the verdict applied: ``ok`` when the
        record check confirmed the save.
        """

        from openadapt_flow.policy import effects_for_actuation
        from openadapt_flow.runtime.durable.approval import StateDiverged

        step = workflow.steps[deferred.step_index]
        if step.id != deferred.step_id:
            raise StateDiverged(
                "the deferred record check names a different step than the bundle"
            )
        verifier = self.effect_verifier
        if verifier is None or not _pipeline_safe_verifier(verifier):
            raise StateDiverged(
                "re-running a deferred record check needs the run's REST, FHIR, "
                "GraphQL or SQL record checker"
            )
        self._run_id = run_id
        effects = self._resolve_effects(
            list(effects_for_actuation(step, deferred.actuation)), params
        )
        before = EffectState.model_validate(deferred.before)
        result = deferred.result.model_copy(deep=True)
        scratch = StepResult(step_id=step.id, intent=step.intent, ok=True)
        try:
            error = self._verify_effects(
                step, before, scratch, effects=effects, verifier=verifier
            )
        except Exception as exc:  # noqa: BLE001 - a raising verifier fails closed
            error = f"read-back raised {type(exc).__name__}; reconciliation is required"
        self._copy_pipelined_evidence(
            SimpleNamespace(result=result, scratch=scratch), error
        )
        return result

    def _settle_durable_readback(
        self, entry: Any, error: Optional[str]
    ) -> Optional[str]:
        """Replace a durable run's deferred-check pause with its verdict.

        Returns the error to report: the check's own, or why the durable
        record of the verdict failed. The pause then stays, so a resume
        re-runs the check; the save is never sent again either way.
        """

        durable_run = getattr(entry, "durable_run", None)
        if durable_run is None:
            return error
        result: StepResult = entry.result
        try:
            durable_run.settle_deferred_readback(
                entry.step_index,
                entry.step,
                result,
                entry.params,
                workflow=entry.workflow,
                active_surface=entry.active_surface,
                surface_observations=entry.surface_observations,
            )
        except Exception as exc:  # noqa: BLE001 - durable evidence boundary
            reason = (
                "the record check finished, but its durable record failed "
                f"({type(exc).__name__}); resume re-runs the check"
            )
            result.ok = False
            result.error = reason if error is None else f"{error}; {reason}"
            result.safety_halt = True
            return result.error
        return error

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
        self._stamp_approval(report)

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
                or step.anchor.form_identity
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

    def _initial_external_network_calls(
        self, prior: Literal["none", "observed", "unknown"]
    ) -> Literal["none", "observed", "unknown"]:
        """Seed the run's network observation before any step runs.

        A Cloud-dispatched run exchanges its dispatch binding and delivery
        permits with Cloud, so it made external network calls whatever its
        target's origin is. Without this, a Cloud run against a loopback app
        could be reported as having made none.
        """
        if self.delivery_authority_kind == "cloud_runner":
            return "observed"
        return prior

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
                report.loop_iterations.extend(
                    _loop_iterations_from_checkpoints(checkpoints)
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
        # Values read before the loop stay in scope; values a row reads do
        # not carry over to the next row's read_gate.
        reads_before_loop = dict(self._field_reads)
        for i, row in enumerate(rows):
            iter_params = {**params, **row}
            _record_loop_iteration(report, state.id, i, row)
            self._field_reads = dict(reads_before_loop)
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
                existing = (
                    _sealed.read_bytes(frame_path)
                    if _sealed.exists(frame_path)
                    else None
                )
                if existing is not None and existing != frame:
                    raise OSError("transition frame digest path has other bytes")
                _sealed.write_bytes(frame_path, frame)
                context_path = root / context_ref
                if context_path.is_symlink():
                    raise OSError("transition context path is a symlink")
                existing_context = (
                    _sealed.read_bytes(context_path)
                    if _sealed.exists(context_path)
                    else None
                )
                if existing_context is not None and existing_context != context_payload:
                    raise OSError("transition context digest path has other bytes")
                _sealed.write_bytes(context_path, context_payload)
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
                        existing = (
                            _sealed.read_bytes(path) if _sealed.exists(path) else None
                        )
                        if existing is not None and existing != payload:
                            raise OSError(
                                "transition asset digest path has other bytes"
                            )
                        _sealed.write_bytes(path, payload)
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
        workflow: Optional[Workflow] = None,
    ) -> None:
        """Prove that the live app still satisfies a linear resume cursor.

        In a multi-surface run the retained contract belongs to the surface
        that was active at the checkpoint. That surface is first bound by
        identity (the one live target whose screen shows the retained
        contract, plus its declared structured attributes); zero or several
        matches refuse the resume.
        """

        active = getattr(checkpoint, "active_surface", None)
        if (
            workflow is None
            or not workflow.surfaces
            or active is None
            or active == workflow.surfaces[0].id
        ):
            self._revalidate_retained_postconditions(
                checkpoint.expected_postconditions,
                bundle_dir=bundle_dir,
            )
            return
        backend = self._bind_checkpoint_surface(workflow, checkpoint, bundle_dir)
        primary = self.backend
        self._activate_surface_backend(backend)
        try:
            self._revalidate_retained_postconditions(
                checkpoint.expected_postconditions,
                bundle_dir=bundle_dir,
            )
        finally:
            self._activate_surface_backend(primary)

    def _bind_checkpoint_surface(
        self, workflow: Workflow, checkpoint: Any, bundle_dir: Path
    ) -> Any:
        """Bind the checkpoint's active surface by identity, or refuse."""

        active = checkpoint.active_surface
        self._surface_identity_context: tuple[Workflow, Optional[Path]] = (
            workflow,
            bundle_dir,
        )
        try:
            router = SurfaceRouter(
                workflow,
                self.backend,
                self.surface_backends,
                poll_interval_s=self.poll_interval_s,
                identity_check=self._surface_identity_check,
                resume_active=active,
                observations=dict(checkpoint.surface_observations),
            )
        except ValueError as exc:
            raise StateDiverged(f"{exc}; refusing to resume") from exc
        probe = Step(
            id=checkpoint.step_id,
            intent=f"re-bind surface '{active}' for resume",
            action=ActionKind.SWITCH_SURFACE,
            surface=active,
            expect=[
                condition.model_copy(deep=True)
                for condition in checkpoint.expected_postconditions
            ],
        )
        prefix = f"Resume after step '{checkpoint.step_id}'"
        if router.specs[active].is_page:
            page, error = router._bind_page(active, probe, prefix)
            if error is not None or page is None:
                raise StateDiverged(error or f"{prefix}: surface not bound")
            return router.backends[active]
        backend, error = router._bind_backend(active, probe, prefix)
        if error is not None or backend is None:
            raise StateDiverged(error or f"{prefix}: surface not bound")
        if self._surface_identity_check(backend, probe) is not True:
            raise StateDiverged(
                f"{prefix}: surface '{active}' does not show its retained "
                "identity; refusing to resume"
            )
        return backend

    def _revalidate_retained_postconditions(
        self,
        conditions: list[Postcondition],
        *,
        bundle_dir: Path,
    ) -> None:
        """Re-evaluate an exact retained postcondition contract or refuse."""

        # A local-edit check is relative to its step's start (focus, URL,
        # title, page count) and can't be re-run after a restart. These steps
        # had no postcondition at all before they were classified, so leaving
        # the check out of resume revalidation keeps it exactly as strict.
        conditions = [
            condition
            for condition in conditions
            if condition.kind
            not in {PostconditionKind.LOCAL_EDIT, PostconditionKind.FIELD_VALUE}
        ]
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
                verification_step, frame, bundle_dir, {}, result, workflow=workflow
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
                        endpoint_scope=effect_endpoint_scope(current_verifier),
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
                reads_before_loop = dict(self._field_reads)
                for i in range(start_i, len(rows)):
                    iter_params = {**params, **rows[i]}
                    _record_loop_iteration(report, state.id, i, rows[i])
                    self._field_reads = dict(reads_before_loop)
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
        capture_frame: Any = None,
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
            capture_frame=capture_frame,
            surface_id=(
                self._surface_router.active_id
                if self._surface_router is not None
                else None
            ),
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
        capture: Optional[Callable[[], bytes]] = None,
    ) -> bytes:
        if self._qualification_fault_mutations:
            return before_png
        # The receipt binds the frame the detector reads. ``capture`` is the
        # detector's own capture path (see _revalidation_frame_capture): two
        # paths can encode identical pixels as different PNG bytes.
        capture_frame = capture or self.backend.screenshot
        context = self._qualification_fault_context(
            gate=gate,
            step=step,
            actuation_path="gui",
            before_input_sha256=sha256_bytes(before_png),
            effect_verifier=effect_verifier,
            capture_frame=capture_frame,
        )
        if context is None:
            return before_png
        driver = self._require_current_fault_driver_binding()
        mutation = driver.mutate(context)
        if mutation is None:
            return before_png
        if mutation.replace_effect_verifier:
            raise RuntimeError("qualification_screen_fault_replaced_verifier")
        after_png = capture_frame()
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

    # -- multi-surface routing (docs/MULTI_SURFACE.md) -----------------------

    def _begin_surfaces(
        self,
        workflow: Workflow,
        *,
        resume: bool,
        resume_from: Optional[int] = None,
        resume_program: bool = False,
        bundle_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Bind the workflow's surfaces for this run, or say why not.

        A single-surface workflow binds nothing and runs exactly as before.
        A surface is a name plus an identity check, never a live handle, so a
        linear resume re-binds every surface by identity from the last
        checkpoint's surface observations (docs/MULTI_SURFACE.md).
        """

        self._surface_router = None
        if not workflow.surfaces:
            if self.surface_backends:
                return (
                    "surface_backends were supplied, but this workflow declares "
                    "no surfaces; refusing to run"
                )
            return None
        # Governed, qualified, durable, resumed, and managed (Cloud) runs are
        # admitted: the governed authorization binds the sealed bundle, whose
        # workflow.json carries the declared surface map; every step result
        # records the surface it ran on; the outcome classifier requires that
        # record to match the contract; a qualification case observes each
        # surface against its own qualified boundary; and checkpoints carry
        # each surface's last-seen identity observation, never a handle.
        if resume_program:
            return (
                "a multi-surface workflow program cannot be resumed yet: program "
                "checkpoints do not carry surface observations; refusing to run"
            )
        if self._managed_dispatch_snapshot is not None:
            # An unattended run must not start when a surface it will switch
            # to has no target: it would stop half-way after earlier writes.
            unbound = [
                spec.id
                for spec in workflow.surfaces[1:]
                if not spec.is_page and spec.id not in (self.surface_backends or {})
            ]
            if unbound:
                return (
                    "a managed dispatch of a multi-surface workflow needs a "
                    "backend for every backend surface in the deployment "
                    "profile's surfaces: map; unbound: "
                    + ", ".join(unbound)
                    + "; refusing to run"
                )
        resume_active: Optional[str] = None
        resume_observations: dict[str, Any] = {}
        if resume and resume_from:
            last = None
            for checkpoint in self._durable_linear_snapshot:
                if getattr(checkpoint, "step_index", None) == resume_from - 1:
                    last = checkpoint
            if last is None or getattr(last, "active_surface", None) is None:
                return (
                    "the checkpoint this multi-surface run resumes from carries "
                    "no surface observation, so its surfaces cannot be re-bound "
                    "by identity; refusing to run"
                )
            resume_active = last.active_surface
            resume_observations = dict(last.surface_observations)
        authorization = self.governed_authorization
        if (
            authorization is not None
            and authorization.qualification_case_id is not None
        ):
            project = workflow.qualification
            if project is None:
                return (
                    "a multi-surface qualification case needs the bundle's "
                    "qualification project; refusing to run"
                )
            for spec in workflow.surfaces[1:]:
                boundary = project.surface_environments.get(spec.id)
                if boundary is None:
                    return (
                        f"the qualification project has no environment boundary "
                        f"for surface '{spec.id}'; every surface of a "
                        "multi-surface workflow must be qualified; refusing to run"
                    )
                if boundary.target_kind != spec.kind:
                    return (
                        f"the qualification boundary for surface '{spec.id}' is "
                        f"{boundary.target_kind}, but the surface is declared "
                        f"{spec.kind}; refusing to run"
                    )
            extra = sorted(
                set(project.surface_environments)
                - {spec.id for spec in workflow.surfaces[1:]}
            )
            if extra:
                return (
                    "the qualification project bounds surfaces the workflow "
                    "does not declare: " + ", ".join(extra) + "; refusing to run"
                )
        self._surface_environment_observations = {}
        try:
            self._surface_identity_context = (workflow, bundle_dir)
            self._surface_router = SurfaceRouter(
                workflow,
                self.backend,
                self.surface_backends,
                poll_interval_s=self.poll_interval_s,
                identity_check=self._surface_identity_check,
                resume_active=resume_active,
                observations=resume_observations,
            )
        except ValueError as exc:
            return f"{exc}; refusing to run"
        return None

    @timed_span("identity")
    def _surface_identity_check(self, backend: Any, step: Step) -> Optional[bool]:
        """Vision-first identity check of one live target for ``step``.

        Pixels and screen text only, on the candidate's own screenshot: the
        step's anchor template for an action step, the recorded screen text
        (TEXT_PRESENT) of a SWITCH_SURFACE step, which was mined from the
        first frame of its target. None when the step carries neither; the
        router then relies on the declared structured attributes alone.
        """

        workflow, bundle_dir = getattr(self, "_surface_identity_context", (None, None))
        try:
            png = backend.screenshot()
        except Exception:
            return False
        evidence = False
        if step.action is ActionKind.SWITCH_SURFACE:
            for condition in step.expect:
                text = condition.text
                if (
                    condition.kind is not PostconditionKind.TEXT_PRESENT
                    or not text
                    or "{" in text
                ):
                    continue
                evidence = True
                if self.vision.find_text(png, text) is None:
                    return False
        elif step.anchor is not None and step.anchor.template and bundle_dir:
            template = self._asset_bytes(
                Path(bundle_dir), step.anchor.template, workflow=workflow
            )
            if template:
                evidence = True
                if self.vision.find_template(png, template) is None:
                    return False
        return True if evidence else None

    def _end_surfaces(self) -> None:
        """Restore the primary backend after a multi-surface run."""

        router = self._surface_router
        if router is None:
            return
        self._surface_router = None
        if self.backend is not router.primary_backend:
            self._activate_surface_backend(router.primary_backend)
        # Drop the qualification input-edge guard from every other surface
        # backend; the primary's guard follows the single-surface lifecycle.
        for backend in router.backends.values():
            if backend is router.primary_backend:
                continue
            clear_guard = getattr(backend, "set_qualification_input_guard", None)
            if callable(clear_guard):
                try:
                    clear_guard(None)
                except Exception:
                    pass

    def _activate_surface_backend(self, backend: Backend) -> None:
        """Make ``backend`` current and drop every cache bound to the old one.

        Nothing observed on one surface may be reused on another: the settle
        seed, the on-screen read-back verifier, the last click point, and any
        armed pointer/keyboard lease all belong to the surface they came from.
        """

        self._cancel_guarded_coordinate()
        self._cancel_guarded_keyboard()
        self.backend = backend
        self._settle_seed = None
        self._actuation_epoch += 1
        # The on-screen read-back verifier is bound to one backend; drop it so
        # the next step builds one for the new surface.
        vars(self).pop("_readback_verifier", None)
        self._last_click_point = None
        self._last_click_region = None
        self._control_overlay_last_observation_png = None

    def _bind_surface_qualification_environment(
        self, workflow: Workflow, surface_id: str
    ) -> Optional[str]:
        """Observe the newly active surface against its qualified boundary.

        Only a qualification case run observes environments. The primary
        surface is checked against the project's ``environment`` (as at run
        start); every other surface against ``surface_environments[id]``
        (``_begin_surfaces`` already refused a case whose project lacks one).
        On a match the input-edge guard moves to the new surface's backend,
        so every later input on that surface rechecks that environment.
        """

        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_id is None:
            return None
        router = self._surface_router
        project = workflow.qualification
        if router is None or project is None:
            return "qualification environment evidence is unavailable"
        self._qualification_environment_call = None
        self._qualification_environment_bound = None
        boundary: Any
        if surface_id == router.primary_id:
            boundary = project.environment
        else:
            boundary = project.surface_environments.get(surface_id)
            if boundary is None:
                return "the qualification project does not bound this surface"
        refusal, observed, ids = self._bind_qualification_boundary(
            boundary, surface_scoped=surface_id != router.primary_id
        )
        if surface_id != router.primary_id and observed is not None and ids is not None:
            self._surface_environment_observations[surface_id] = (
                ObservedSurfaceEnvironment(
                    target_kind=observed.target_kind,
                    application_sha256=hashlib.sha256(
                        observed.application_identity.encode("utf-8")
                    ).hexdigest(),
                    application_version_sha256=hashlib.sha256(
                        observed.application_version.encode("utf-8")
                    ).hexdigest(),
                    session_sha256=observed.session_identity_sha256,
                    environment_digest=observed.environment_digest,
                    observer_id=ids[0],
                    observer_contract_sha256=ids[1],
                    binding_sha256=observed.binding_sha256(
                        observer_id=ids[0], observer_contract_sha256=ids[1]
                    ),
                )
            )
        return refusal

    def _active_surface_binding(
        self, workflow: Workflow
    ) -> tuple[Optional[ExecutionTargetKind], Optional[str]]:
        """(kind, execution mode) of the surface the run is on right now.

        Single-surface runs use the bundle-level binding, unchanged. In a
        multi-surface run the remote-surface safety rules follow the ACTIVE
        surface, so an RDP segment of a web-first bundle keeps them.
        """

        router = self._surface_router
        if router is None:
            return workflow.surface, workflow.execution_mode
        from openadapt_flow.surface_selection import execution_mode_for_surface

        kind = cast(ExecutionTargetKind, router.active_kind())
        return kind, execution_mode_for_surface(kind)

    def _active_surface_is_external_remote(self, workflow: Workflow) -> bool:
        kind, mode = self._active_surface_binding(workflow)
        return kind in ("rdp", "citrix") and mode == "external"

    @staticmethod
    def _surface_refusal_result(step: Step, error: str) -> StepResult:
        return StepResult(
            step_id=step.id,
            intent=step.intent,
            ok=False,
            risk=step.risk,
            risk_explanation=step.risk_explanation,
            risk_review_required=step.risk_review_required,
            delivery_attempted=False,
            safety_halt=True,
            failure_category="safety_halt",
            error=error,
        )

    def _run_step(self, step: Step, **kwargs: Any) -> StepResult:
        """Execute one step and record where its time went (``timings``)."""
        from openadapt_flow.runtime import step_timing

        timer, previous = step_timing.start_step()
        result: Optional[StepResult] = None
        try:
            result = self._run_step_timed(step, **kwargs)
            return result
        finally:
            step_timing.end_step(previous)
            if result is not None:
                result.timings = timer.result_ms(result.elapsed_ms or None)

    def _run_step_timed(
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
        """Execute one step on the surface it names; never raises.

        A single-surface run goes straight to the step pipeline. In a
        multi-surface run the step is first checked against the live surface
        map (no undeclared page, the active page still open, the step on the
        active surface), a SWITCH_SURFACE step runs its identity check, and
        after the action the pages the step declares are claimed and any other
        new page halts.
        """

        pending_error = self._join_pipelined_readback()
        if pending_error is not None:
            # The previous action's deferred read-back failed: refuse to act
            # further in this run, exactly as an inline failure would.
            refused = StepResult(
                step_id=step.id,
                intent=step.intent,
                ok=False,
                error=pending_error,
                safety_halt=True,
                failure_category="safety_halt",
            )
            return refused
        # A closed-loop scroll that never found this step's target hands the
        # miss to this step (see ``_act_scroll``); only this step consumes it.
        target_missed: Optional[str] = None
        missed = self._scroll_missed_target
        if missed is not None and missed[0] == step.id:
            self._scroll_missed_target = None
            target_missed = missed[1]
        if step.target_param is not None:
            # A record-selecting click acts on the run's record: look for the
            # run's value; the identity check verifies the row (param mode).
            from openadapt_flow.runtime.target_param import bind_target_param

            sealed_step = step
            step, refusal = bind_target_param(step, params, workflow)
            # The sealed-leaf check compares the program's step with the
            # step the runtime acts on; it must see the sealed one.
            self._target_param_binding = (step, sealed_step)
            if refusal is not None:
                return StepResult(
                    step_id=step.id,
                    intent=step.intent,
                    ok=False,
                    error=refusal,
                    safety_halt=True,
                    failure_category="safety_halt",
                )
        router = self._surface_router
        if router is None:
            if step.action is ActionKind.SWITCH_SURFACE:
                return self._surface_refusal_result(
                    step,
                    f"Step '{step.id}' switches surface, but this run has no "
                    "declared surfaces; run halted",
                )
            return self._run_step_on_active_surface(
                step,
                workflow=workflow,
                step_index=step_index,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                new_crops=new_crops,
                graph_ctx=graph_ctx,
                target_missed=target_missed,
            )
        refusal = router.entry_refusal(step)
        if refusal is None and step.action is not ActionKind.SWITCH_SURFACE:
            # Route the step to its surface's live target; after a resume or
            # a closed page this re-binds it by the step's identity check.
            refusal = router.route(step)
            if refusal is None and router.active_backend is not self.backend:
                self._activate_surface_backend(router.active_backend)
                refusal = self._bind_surface_qualification_environment(
                    workflow, router.active_id
                )
        if refusal is not None:
            refused = self._surface_refusal_result(step, refusal)
            refused.surface = router.active_id
            return refused
        if step.action is ActionKind.SWITCH_SURFACE:
            switched = self._run_switch_surface_step(
                step, workflow=workflow, bundle_dir=bundle_dir, run_dir=run_dir
            )
            # The surface the run is on after the switch: the target on
            # success, the unchanged surface on a refusal.
            switched.surface = router.active_id
            return switched
        result = self._run_step_on_active_surface(
            step,
            workflow=workflow,
            step_index=step_index,
            params=params,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            new_crops=new_crops,
            graph_ctx=graph_ctx,
            target_missed=target_missed,
        )
        # Receipt: the surface this step ran on. The entry check above proved
        # it is the step's declared surface; the outcome classifier re-checks
        # the record against the contract.
        result.surface = router.active_id
        if not result.ok:
            return result
        # A guard-skipped step delivered nothing, so it cannot have opened its
        # pages; any page that did appear is still undeclared.
        checked = step.model_copy(update={"opens": []}) if result.skipped else step
        error = router.after_action_refusal(checked, timeout_s=step.timeout_s)
        if error is not None:
            result.ok = False
            result.safety_halt = True
            result.failure_category = "safety_halt"
            result.error = error
        return result

    def _run_switch_surface_step(
        self, step: Step, *, workflow: Workflow, bundle_dir: Path, run_dir: Path
    ) -> StepResult:
        """Switch to ``step.surface`` after its identity check; never raises.

        Delivers no input. The router checks the target (declared, opened on
        this run, the exact claimed page or a supplied backend of the declared
        kind, the recorded origin); then the step's postconditions, mined from
        the first frame of the new surface, must hold on it. No model may
        rescue that screen check: it is an identity check, not drift.
        """

        router = self._surface_router
        assert router is not None
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
        try:
            result.before_png = self._save_step_png(
                run_dir, evidence_step_id, "before", self.backend.screenshot()
            )
        except Exception:
            pass
        backend, refusal = router.switch(step)
        if refusal is not None or backend is None:
            result.safety_halt = True
            result.failure_category = "safety_halt"
            result.error = refusal or "surface switch refused"
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0
            return result
        self._activate_surface_backend(backend)
        env_refusal = self._bind_surface_qualification_environment(
            workflow, router.active_id
        )
        if env_refusal is not None:
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.error = (
                f"Step '{step.id}' ({step.intent}): {env_refusal} on surface "
                f"'{router.active_id}'; run halted"
            )
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0
            return result
        failed: list[str] = []
        try:
            start_state = self._structural_state()
            frame = self._settle_seeded()
            ok, frame = self._poll_postconditions(step, frame, bundle_dir, start_state)
            if not ok:
                frame = self.vision.wait_settled(self.backend)
                failed = [
                    self._describe_postcondition(pc)
                    for pc in step.expect
                    if not self._postcondition_passes(
                        pc, frame, bundle_dir, start_state
                    )
                ]
            result.after_png = self._save_step_png(
                run_dir, evidence_step_id, "after", frame
            )
        except Exception as exc:
            failed = [f"the new surface could not be observed ({type(exc).__name__})"]
        result.postconditions_ok = (not failed) if step.expect else None
        if failed:
            result.safety_halt = True
            result.failure_category = "safety_halt"
            result.error = (
                f"Surface identity check failed for step '{step.id}' "
                f"({step.intent}): surface '{step.surface}' does not show the "
                f"recorded screen state — failed: {'; '.join(failed)} — run "
                "halted"
            )
        else:
            result.ok = True
        result.elapsed_ms = (time.monotonic() - t0) * 1000.0
        return result

    def _run_step_on_active_surface(
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
        target_missed: Optional[str] = None,
    ) -> StepResult:
        """Execute a single step; never raises (failures land in the result).

        ``graph_ctx`` is set ONLY by the program interpreter (Phase 2): it
        supplies the ``prev_action`` / scroll-successor context the linear loop
        derives from ``workflow.steps[step_index±1]`` (which does not exist in a
        graph). When None (the linear path) every lookup is exactly as before --
        so a linear run is byte-for-byte unchanged.
        """
        t0 = time.monotonic()
        # Where a locating or restoring undo keeps its pre-save values.
        self._undo_prior_run_dir = run_dir
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
        if self.approval_gate is not None and self._step_is_consequential(
            step, workflow
        ):
            # Per-item approval happens before the save step resolves, so an
            # approved step then takes a fresh frame and resolves its target
            # and right-record check again. The last-point gate still re-reads
            # the form before the input is delivered.
            api_view = None
            if (
                self._web_form_span_active
                and step.api_binding is not None
                and self._approval_gated()
            ):
                # An API step fills no form: the person approves the exact
                # request it sends. If it can't be prepared, nothing was sent
                # and nobody was asked; the GUI steps run instead.
                api_view = self._web_form_approval_view(step, params)
                if isinstance(api_view, str):
                    result.api_fallback = api_view
                    result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                    return result
                self._web_form_approval[id(result)] = api_view
            approval_refusal = self._approval_step_refusal(
                workflow, step, step_index, result, api_view=api_view
            )
            if approval_refusal is not None:
                result.ok = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.error = approval_refusal
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result
        # A web_form binding (compiled from a recording's network capture)
        # acts only when the linear loop runs it in place of the GUI steps it
        # stands for. On the GUI path its save step is an ordinary click.
        step_api_binding = step.api_binding
        if (
            step_api_binding is not None
            and step_api_binding.kind == "web_form"
            and not self._web_form_span_active
        ):
            step_api_binding = None
        step_api_actuator = (
            self._actuator_for(step_api_binding)
            if step_api_binding is not None
            else None
        )
        if (
            qualification_path != "gui"
            and step_api_binding is not None
            and step_api_actuator is None
            and (
                qualification_path == "api" or step_api_binding.on_unavailable == "halt"
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
            and step_api_actuator is not None
            and step_api_binding is not None
            and step_api_binding.effects
        ):
            api_effects = tuple(self._resolve_effects(step_api_binding.effects, params))
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
        if step.read_gate is not None:
            # Checked before any actuation path: an API-bound write must not
            # skip the gate that _act applies to a GUI write.
            refusal = self._read_gate_refusal(step, result)
            if refusal is not None:
                result.ok = False
                result.error = refusal
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result
        if (
            qualification_path != "gui"
            and step_api_actuator is not None
            and step_api_binding is not None
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

        if self._web_form_span_active:
            # The API step did not send the save (a precondition failed or it
            # is unavailable). Report why and act on nothing: the linear loop
            # runs the GUI steps from the start of the span instead.
            reasons = [r for r in result.effect_results if r.startswith("[api]")]
            result.api_fallback = (
                reasons[-1][len("[api] ") :]
                if reasons
                else "the API step is unavailable"
            )
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0
            return result

        # Default behavior settles exactly as before. The opt-in readiness gate
        # owns its own single bounded settle call inside ``_apply_step_gates``;
        # capture only a raw diagnostic frame here so it is not preceded by an
        # unobservable proceed-anyway settle attempt.
        before_png = (
            self.backend.screenshot() if self.require_settled else self._settle_seeded()
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
        # Where the backend's log of server answers stood before the save, so
        # the answers to this step's write requests can be kept as evidence.
        server_mark: Optional[int] = None
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
            before_png = self._maybe_scale_render(
                step, before_png, bundle_dir, workflow
            )
            drift_error = self._layout_drift_error(
                step, before_png, bundle_dir, workflow
            )
            if drift_error is not None:
                result.error = drift_error
                result.safety_halt = True
                result.failure_category = "safety_halt"
                result.after_png = self._save_step_png(
                    run_dir, evidence_step_id, "after", last_frame
                )
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result
            resolution, matched_region, error = self._resolve_step_target(
                step, before_png, bundle_dir, workflow, target_missed
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
                resolution, matched_region, error = self._resolve_step_target(
                    step, before_png, bundle_dir, workflow, target_missed
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
                pre_identity_halt = (result.safety_halt, result.failure_category)
                error = self._entry_identity_gate_error(
                    step,
                    resolution,
                    before_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    evidence_run_dir=run_dir,
                )
                if (
                    error is not None
                    and result.identity is not None
                    and result.identity.status == "mismatch"
                    # A signal-quorum conflict or a parameter mismatch names
                    # another record outright: final, never re-observed.
                    and result.identity.mode not in ("signal_quorum", "param")
                    and not result.identity.param
                    # Qualification drills bind the refusal to the injected
                    # frame; a second look would read a different one.
                    and self.qualification_fault_driver is None
                    and time.monotonic() < deadline
                    and self._identity_band_still_changing(step, resolution, before_png)
                ):
                    # The read raced a re-render: the target's band changed
                    # right after it (a list or menu re-rendering under slow
                    # lookups). Keep the first read as evidence, then look
                    # once more on a fresh settled frame and act only if
                    # identity verifies there. A second mismatch halts.
                    result.identity_first_read = result.identity
                    result.identity_first_frame_png = self._save_step_png(
                        run_dir, evidence_step_id, "identity_first_read", before_png
                    )
                    result.identity_reobserved = True
                    time.sleep(self.poll_interval_s)
                    fresh_png = self.vision.wait_settled(self.backend)
                    fresh_resolution, fresh_region, fresh_error = self._resolve_step(
                        step, fresh_png, bundle_dir, workflow
                    )
                    fresh_drift = (
                        self._layout_drift_error(step, fresh_png, bundle_dir, workflow)
                        if fresh_error is None and fresh_resolution is not None
                        else None
                    )
                    if fresh_drift is not None:
                        # The re-render landed on a different screen: halt on
                        # the drift, as the step would have before resolving.
                        error = fresh_drift
                        result.safety_halt = True
                        result.failure_category = "safety_halt"
                    elif fresh_error is None and fresh_resolution is not None:
                        before_png = fresh_png
                        result.before_png = self._save_step_png(
                            run_dir, evidence_step_id, "before", before_png
                        )
                        last_frame = before_png
                        resolution = fresh_resolution
                        if fresh_region is not None:
                            assert step.anchor is not None
                            resolution = self._retain_visual_resolution_evidence(
                                workflow=workflow,
                                anchor=step.anchor,
                                resolution=resolution,
                                matched_region=fresh_region,
                                frame_png=before_png,
                                bundle_dir=bundle_dir,
                                run_dir=run_dir,
                            )
                        result.resolution = resolution
                        # The first read's halt flags belong to that read.
                        result.safety_halt, result.failure_category = pre_identity_halt
                        error = self._entry_identity_gate_error(
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
                if error is None:
                    error = self._unbound_submit_refusal(step, workflow, result)
                    if error is not None:
                        result.safety_refusal_evidence = SafetyRefusalEvidence(
                            stage="target_resolution",
                            code="target_ambiguous",
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
                    capture=self._revalidation_frame_capture(
                        step,
                        workflow,
                        arm_keyboard=step.action in (ActionKind.KEY, ActionKind.HOTKEY),
                    ),
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
                    if server_mark is None and effect_pre_state is not None:
                        server_mark = self._server_response_mark()
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
                    after_png = self._settle_seeded()
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
                            step,
                            after_png,
                            bundle_dir,
                            start_state,
                            result,
                            workflow=workflow,
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

            if effect_pre_state is not None:
                self._keep_undo_prior_state(
                    workflow, step, effect_pre_state, resolved_effects, params
                )
            # Independent system-of-record verification, AFTER the screen
            # postconditions passed: the screen oracle cannot see a partial
            # save, a phantom optimistic-UI success, a duplicate write, or a
            # lost update -- the EffectVerifier reads the REAL record and HALTs
            # on any non-CONFIRMED verdict (docs/design/EFFECT_VERIFIER.md).
            if server_mark is not None:
                # The server's own answer to the save: kept as evidence at a
                # lower check level. It comes from the same transaction, so it
                # never confirms an effect; the verifier below still decides.
                self._collect_server_responses(result, server_mark)
            if (
                error is None
                and not delivery_uncertain
                and effect_pre_state is not None
                and self._may_pipeline_readback(active_verifier, step)
            ):
                # Pipelined read-back (runtime/readback_pipeline.py): the same
                # check, run on the side thread. Any later step in this run
                # waits for it first; the item's report is settled with the
                # verdict when the check finishes.
                self._defer_readback(
                    step,
                    effect_pre_state,
                    result,
                    effects=resolved_effects,
                    verifier=active_verifier,
                )
            elif (error is None or delivery_uncertain) and effect_pre_state is not None:
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
                # A click rebound to the run's record (runtime.target_param)
                # found another record's row; that row must never become the
                # step's recorded anchor.
                and not (step.target_param is not None and not step.anchor.template)
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
        if binding.kind == "web_form":
            from openadapt_flow.runtime.actuators.web_form import (
                web_form_pointer_template,
            )

            return web_form_pointer_template(binding, pointer)
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
        if step.api_binding is not None and step.api_binding.kind == "web_form":
            # A form save whose GUI step checks the right record, or whose
            # admission named a right-record parameter, is checked the same
            # way in every run, governed or not (its durable checkpoint
            # needs the evidence too).
            authorization_requires = authorization_requires or bool(
                step.identity_armed or step.api_binding.identity
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
            # A form field's name is the page's, not the record's (form_lname
            # versus lname): for a form save the field is bound to the record
            # check by sending the same parameter the check selects on.
            names_must_match = binding.kind != "web_form"
            pointers_are_bound = all(
                template is not None
                and (
                    not names_must_match
                    or self._api_request_pointer_field_path(pointer)
                    == effect_target_path
                )
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
            from openadapt_flow.runtime.actuators.web_form import (
                effect_selector_rows,
            )

            effect_exprs = [
                row.get(identity.effect_field)
                for effect in effects
                for row in effect_selector_rows(effect)
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

    def _actuator_for(self, binding: Any) -> Optional[Any]:
        """The actuator that runs ``binding``, or None when none can."""

        if binding is None:
            return None
        if binding.kind != "web_form":
            return self.api_actuator
        if self.web_form_actuator is not None:
            return self.web_form_actuator
        # In a multi-surface run the request goes through the active
        # surface's session, which on a resume may not be self.backend yet.
        router = getattr(self, "_surface_router", None)
        backend = router.active_backend if router is not None else self.backend
        page = getattr(backend, "page", None)
        if page is None:
            return None
        from openadapt_flow.runtime.actuators.web_form import (
            PlaywrightSessionTransport,
            WebFormActuator,
        )

        try:
            return WebFormActuator(PlaywrightSessionTransport(page))
        except Exception:  # noqa: BLE001 - no live session: the GUI runs
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
        api_actuator = self._actuator_for(binding)
        assert api_actuator is not None  # guaranteed by the caller

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
        self._keep_undo_prior_state(workflow, step, before, effects, params)
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
        approved_view = self._web_form_approval.pop(id(result), None)
        send_prepared: Optional[Any] = None
        again: Any = None
        if approved_view is not None:
            # Under an approval mode: prepare the request again now (fresh
            # form, fresh pre-checks). The last-point gate compares its
            # operation digest with the one a person approved; a request that
            # can't be prepared again reads as an unreadable form.
            again = self._web_form_approval_view(step, params)
            self._web_form_form_state = (
                None if isinstance(again, str) else ("api_request", again.digest)
            )
            self._web_form_form_state_set = True
        try:
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
        finally:
            self._web_form_form_state = None
            self._web_form_form_state_set = False
        if refusal is None and approved_view is not None:
            if isinstance(again, str) or again.digest != approved_view.digest:
                # Also refused when the gate let it through (for example a
                # save that passed problems_only's checks without a person):
                # only the exact request prepared before the gate is sent.
                why = (
                    again
                    if isinstance(again, str)
                    else "the request, the record or the record check differs"
                )
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                refusal = (
                    f"Step '{step.id}' ({step.intent}): the API request changed "
                    f"after the approval gate ({why}); the save was not sent "
                    "and the GUI steps do not run in its place"
                )
            else:
                # Exactly the request whose digest was approved and re-checked.
                send_prepared = again.prepared
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
            if send_prepared is not None:
                # Exactly the request whose digest was approved and re-checked.
                outcome = api_actuator.send(binding, send_prepared)
            else:
                outcome = api_actuator.actuate(
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
        if self._may_pipeline_readback(effect_verifier, step):
            # Pipelined read-back (runtime/readback_pipeline.py), as for a GUI
            # save: the same record check on the side thread. The item's
            # report stays unsuccessful until it settles with the verdict.
            self._defer_readback(
                step,
                before,
                result,
                effects=effects,
                verifier=effect_verifier,
                actuation="api",
            )
            result.ok = True
            result.error = None
            return True
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
                    workflow=workflow,
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

    def _keep_undo_prior_state(
        self,
        workflow: Workflow,
        step: Step,
        before: Any,
        effects: Optional[list["Effect"]],
        params: Mapping[str, Any],
    ) -> None:
        """Keep what a locating or restoring undo of ``step`` needs.

        Reads only the pre-action baseline already captured; never raises and
        never changes the step's result (``after_stop.keep_prior_state``).
        """
        run_dir = getattr(self, "_undo_prior_run_dir", None)
        if run_dir is None or not workflow.undo_steps or not effects:
            return
        try:
            state = self._effect_pre_state_for(before, effects[0])
        except Exception:  # noqa: BLE001 - bookkeeping never stops a run
            return
        if getattr(state, "reachable", None) is not True:
            return
        from openadapt_flow.after_stop import keep_prior_state

        keep_prior_state(
            workflow, step, list(getattr(state, "records", []) or []), params, run_dir
        )

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

    def _server_response_mark(self) -> Optional[int]:
        """Where the backend's server-answer log stands, or None when the
        backend doesn't see server answers (every non-browser backend)."""

        mark = getattr(self.backend, "server_response_mark", None)
        if not callable(mark):
            return None
        try:
            return int(mark())
        except Exception:  # noqa: BLE001 - evidence only, never a failure
            return None

    def _collect_server_responses(self, result: StepResult, mark: int) -> None:
        since = getattr(self.backend, "server_responses_since", None)
        if not callable(since):
            return
        try:
            responses = list(since(mark))
        except Exception:  # noqa: BLE001 - evidence only, never a failure
            return
        result.server_responses = responses[:_MAX_SERVER_RESPONSES]

    @timed_span("readback")
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
                scope = effect_endpoint_scope(verifier)
                selected_pre_state = getattr(before, "for_effect", None)
                if callable(selected_pre_state):
                    # Candidate selection and its evidence strength are pinned
                    # before input. Do not recompute the tier after delivery:
                    # a stateful plugin must not change the receipt contract.
                    binding = selected_pre_state(effect)
                    tier = binding.tier
                    identity = binding.verifier_identity
                    scope = effect_endpoint_scope(binding.verifier)
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
            result.effect_field_checks.extend(
                effect_field_checks(effect, verdict, tier)
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
                        endpoint_scope=scope,
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
                            endpoint_scope=scope,
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
                        endpoint_scope=scope,
                        reconciliation_actions=comp.actions_taken,
                        deferred_check=verdict.deferred_check,
                    )
                )
                result.effect_results.append(
                    f"[{verdict.substrate}] {effect.kind.value}: "
                    f"{verdict.verdict.value.upper()} → "
                    f"{comp.outcome.value.upper()} — {comp.reason}"
                )
                prefix, read = _effect_read_wording(tier)
                return (
                    f"{prefix} HALTED step "
                    f"'{step.id}' ({step.intent}): {effect.kind.value} "
                    f"{verdict.verdict.value} against the {verdict.substrate} "
                    f"{read} and could not be reconciled "
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
                    endpoint_scope=scope,
                    deferred_check=verdict.deferred_check,
                )
            )
            result.effect_results.append(
                f"[{verdict.substrate}] {effect.kind.value}: "
                f"{verdict.verdict.value.upper()} — {verdict.reason}"
            )
            prefix, read = _effect_read_wording(tier)
            return (
                f"{prefix} HALTED step "
                f"'{step.id}' ({step.intent}): {effect.kind.value} "
                f"{verdict.verdict.value} against the {verdict.substrate} "
                f"{read} (the screen indicated completion but the "
                f"declared outcome is contradicted or unverifiable) — "
                f"{verdict.reason} — run aborted"
            )

        result.effect_verified = True
        return None

    def _identity_band_still_changing(
        self, step: Step, resolution: Resolution, read_png: bytes
    ) -> bool:
        """Whether the screen was still changing at an identity read.

        Takes a second frame right after the read and compares the target's
        identity band (the full-width rows around the resolved point, as tall
        as the anchor's template plus a margin). Where the backend reports
        page activity (browser DOM and network quiescence), activity counts
        too. Any doubt reads as stable: a mismatch on a stable screen is
        final.
        """

        try:
            from PIL import Image, ImageChops

            again = self.backend.screenshot()
            first = Image.open(io.BytesIO(read_png)).convert("RGB")
            second = Image.open(io.BytesIO(again)).convert("RGB")
            if first.size != second.size:
                return True
            height = step.anchor.region[3] if step.anchor is not None else 64
            half = max(int(height), 20) // 2 + 16
            y = int(resolution.point[1])
            box = (0, max(0, y - half), first.size[0], min(first.size[1], y + half))
            if ImageChops.difference(first.crop(box), second.crop(box)).getbbox():
                return True
        except Exception:
            return False
        probe = getattr(self.backend, "page_quiescent", None)
        if callable(probe):
            try:
                return probe(0.2) is False
            except Exception:
                return False
        return False

    def _entry_identity_gate_error(
        self,
        step: Step,
        *args: Any,
        **kwargs: Any,
    ) -> Optional[str]:
        """The identity gate at step entry, where an extra look may use input.

        Only this check may select and copy the identifier cell
        (:mod:`openadapt_flow.runtime.identity_looks`). The actuation-time
        check that follows takes no input and reuses that read only through a
        pixel-matched binding.
        """
        self.__dict__.setdefault("_identity_look_bindings", {}).pop(step.id, None)
        self.__dict__.setdefault("_hover_frames", {}).pop(step.id, None)
        self._identity_input_looks = True
        try:
            return self._identity_gate_error(step, *args, **kwargs)
        finally:
            self._identity_input_looks = False

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
        # A click that selects a record by a run value (Step.target_param)
        # is judged by its identity band, never by hover evidence alone.
        hover_check = (
            self._hover_check(step, resolution, frame_png, workflow, bundle_dir)
            if qualified_policy is None and step.target_param is None
            else None
        )
        if hover_check is None and (
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
                    or step.anchor.form_identity
                )
            )
        ):
            return None
        # A hover check needs an anchor too (see _hover_check).
        assert step.anchor is not None

        check = (
            hover_check
            if hover_check is not None
            else self._verify_identity(
                step, resolution, frame_png, params, workflow, bundle_dir
            )
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
        # Under a governed run, an armed step needs a verified identity before
        # it acts, whether or not the authorization lists it: the durable
        # checkpoint demands the same (step.identity_armed), and it only runs
        # after the click. An unreadable or abstained identity halts here.
        governed = self.governed_authorization is not None and (
            self.governed_authorization.requires_verified_identity(step.id)
            or step.identity_armed
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
        elif check.look_halt is not None:
            error = (
                f"Step '{step.id}' ({step.intent}) took an extra look at a "
                "look-alike identifier and the screen changed outside the "
                f"identifier ({check.look_halt}) — refusing to act"
            )
            result.safety_halt = True
        elif check.known_id_halt is not None:
            names = {
                "known_ids_unconfirmed": "has no admission confirmation",
                "known_ids_changed": "changed since it was confirmed",
                "known_ids_stale": "is older than the configured age",
            }
            error = (
                f"Step '{step.id}' ({step.intent}) needs the known-ID list to "
                f"rule out a look-alike identifier, and the list "
                f"{names[check.known_id_halt]} ({check.known_id_halt}) — "
                "confirm a current export; refusing to act"
            )
            result.safety_halt = True
        elif check.mode == "hover" and check.status == "mismatch":
            reason = check.hover.reason if check.hover is not None else "differs"
            error = (
                f"Step '{step.id}' ({step.intent}): with the pointer on the "
                "target, the screen answered differently from the recording "
                f"({reason}) — refusing to click; run aborted"
            )
        elif check.ambiguity_halt is not None:
            if check.ambiguity_halt == "identical_candidates":
                why = (
                    f"{check.candidate_count} shown rows carry the same "
                    "identity and no recorded record id tells them apart"
                )
            elif check.ambiguity_halt == "identity_uncounted":
                why = (
                    "this surface cannot count the rows that might share it, "
                    "and a consequential write follows (identity_uncounted)"
                )
            else:
                why = "the rows that could share its identity could not all be counted"
            error = (
                f"Step '{step.id}' ({step.intent}) matched the recorded "
                f"identity, but {why} (rung '{resolution.rung}') — acting "
                "could open another record; refusing to act"
            )
            result.safety_halt = True
        elif check.mode == "hover":
            # Hover evidence only adds a stop. A step that had no identity
            # check before keeps going when the response is absent or cannot
            # be compared; the verdict stays on the step result.
            error = None
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
            step.risk == "irreversible" or governed or step.target_param is not None
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
                if step.risk == "irreversible"
                else "selects a record by a run value"
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
                existing = _sealed.read_bytes(path) if _sealed.exists(path) else None
                if existing is not None and existing != payload:
                    raise OSError("pixel evidence digest path has other bytes")
                _sealed.write_bytes(path, payload)
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
            existing = _sealed.read_bytes(path) if _sealed.exists(path) else None
            if existing is not None and existing != payload:
                raise OSError("visual resolution digest path has other bytes")
            _sealed.write_bytes(path, payload)

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
            and self._active_surface_is_external_remote(workflow)
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
        if not self._active_surface_is_external_remote(workflow) or not isinstance(
            self.backend, RemoteActuationBackend
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
        # A submit binding ties an unlabeled control to the record the run
        # entered; it must be verified on the guarded path like any identity.
        if step.submit_binding is not None:
            return True
        anchor = step.anchor
        return anchor is not None and bool(
            anchor.context_text
            or anchor.structured_identity
            or anchor.identity_template
            or anchor.identifier_crop
            or anchor.form_identity
        )

    def _unbound_submit_refusal(
        self, step: Step, workflow: Workflow, result: StepResult
    ) -> Optional[str]:
        """Refuse a consequential click the compiler could not bind.

        The compiler records ``submit_binding_unbound_reason`` when the
        demonstrated click on an unlabeled control could not be tied to the
        form it submits (for example, two unlabeled controls follow the
        filled fields). Such a click has no record tie at all, so it is
        refused before input whatever identity evidence the step carries.
        """

        if (
            step.submit_binding_unbound_reason is None
            or step.action not in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            or not self._step_is_consequential(step, workflow)
        ):
            return None
        result.safety_halt = True
        result.failure_category = "safety_halt"
        return (
            f"Step '{step.id}' ({step.intent}) clicks an unlabeled control that "
            "could not be tied to the record this run entered: "
            f"{step.submit_binding_unbound_reason} — refusing to act; run aborted"
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
            and self._active_surface_is_external_remote(workflow)
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

        if (
            step.submit_binding is not None
            and step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and not isinstance(self.backend, SubmitBindingBackend)
        ):
            return (
                f"Step '{step.id}' ({step.intent}) clicks an unlabeled control "
                "whose only record tie is its recorded submit binding, but this "
                "backend cannot read the page structure to verify it — refusing "
                "input; run aborted"
            )

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

    def _guarded_keyboard_revalidation(
        self, step: Step, workflow: Workflow, *, arm_keyboard: bool
    ) -> bool:
        return bool(
            arm_keyboard
            and self._requires_atomic_identity_keyboard(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )

    def _guarded_type_pointer_revalidation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        return bool(
            step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
            and self._requires_atomic_identity_pointer(step, workflow)
            and not isinstance(self.backend, RemoteActuationBackend)
            and isinstance(self.backend, GuardedCoordinateActionBackend)
            and isinstance(self.backend, GuardedKeyboardActionBackend)
        )

    def _guarded_click_pointer_revalidation(
        self, step: Step, workflow: Workflow
    ) -> bool:
        return bool(
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

    def _revalidation_frame_capture(
        self, step: Step, workflow: Workflow, *, arm_keyboard: bool
    ) -> Optional[Callable[[], bytes]]:
        """The capture a local revalidation takes its fresh frame with.

        ``None`` means the ordinary screenshot. A remote backend's actuation
        frame arms a lease, so it is never taken outside the revalidation.
        """

        if isinstance(self.backend, RemoteActuationBackend):
            return None
        if (
            self._guarded_keyboard_revalidation(
                step, workflow, arm_keyboard=arm_keyboard
            )
            or self._guarded_type_pointer_revalidation(step, workflow)
            or self._guarded_click_pointer_revalidation(step, workflow)
        ):
            return cast(
                GuardedKeyboardActionBackend, self.backend
            ).guarded_keyboard_frame
        return None

    @timed_span("identity")
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

        guarded_keyboard_backend = self._guarded_keyboard_revalidation(
            step, workflow, arm_keyboard=arm_keyboard
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
        guarded_type_pointer_backend = self._guarded_type_pointer_revalidation(
            step, workflow
        )
        guarded_click_pointer_backend = self._guarded_click_pointer_revalidation(
            step, workflow
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
        elif (
            fresh_resolution is not None
            and step.submit_binding is not None
            and (
                fresh_resolution.rung == "structural"
                or isinstance(self.backend, RemoteActuationBackend)
                or not isinstance(self.backend, GuardedCoordinateActionBackend)
            )
        ):
            error = (
                f"Step '{step.id}' ({step.intent}) carries a submit binding, which "
                "is verified only on the guarded visual path of a DOM backend; "
                "this observation would bypass it — refusing to act; run aborted"
            )
            result.safety_halt = True
            result.failure_category = "safety_halt"
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
                elif step.submit_binding is not None:
                    # No row identity exists for an unlabeled control outside
                    # any row. Its record tie is the recorded binding to the
                    # form this run filled; the backend verifies it live and
                    # leases the exact observation until delivery, or raises.
                    try:
                        cast(
                            SubmitBindingBackend, coordinate_backend
                        ).arm_guarded_submit_coordinate(
                            *fresh_resolution.point, binding=step.submit_binding
                        )
                    except StructuralResolutionRefused:
                        result.submit_binding_verified = False
                        raise
                    result.submit_binding_verified = True
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

    def _sealed_step(self, step: Step) -> Step:
        """The program's own step for one the runtime rebound to a run value.

        ``runtime.target_param`` rebinds a record-selecting click to the
        run's record; integrity checks compare against the sealed step.
        """
        binding = getattr(self, "_target_param_binding", None)
        if binding is not None and binding[0] is step:
            return binding[1]
        return step

    def _resolve_step_target(
        self,
        step: Step,
        screen_png: bytes,
        bundle_dir: Path,
        workflow: Workflow,
        target_missed: Optional[str],
    ) -> tuple[Optional[Resolution], Optional[Region], Optional[str]]:
        """Resolve the step's target; stricter after a scroll that missed it.

        ``target_missed`` is set when the preceding closed-loop scroll used
        up its budget without this target resolving. The step then accepts
        only the rungs scroll readiness accepts (direct deterministic
        evidence, no model): a weaker rung would act where the scroll could
        not find the target. Otherwise the step halts on its own target with
        the ordinary "Could not resolve target" error and sends no input.
        """
        if target_missed is None:
            return self._resolve_step(step, screen_png, bundle_dir, workflow)
        resolution, region, error = self._resolve_step(
            step, screen_png, bundle_dir, workflow, allow_grounder=False
        )
        if resolution is not None and resolution.rung in _DIRECT_TARGET_RUNGS:
            return resolution, region, error
        if resolution is None and step.anchor is None:
            return resolution, region, error
        return (
            None,
            None,
            f"Could not resolve target for step '{step.id}' ({step.intent}): "
            f"{target_missed}",
        )

    @timed_span("resolve")
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
        resolution, matched_region = self._retarget_by_record_key(
            step, resolution, matched_region
        )

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

    @timed_span("resolve")
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

    @timed_span("settle")
    def _settle_seeded(self) -> bytes:
        """``vision.wait_settled`` that can extend the last settled run.

        Same contract as ``vision.wait_settled``: a fresh frame is always
        captured and returned, and it counts as settled only after a run of
        identical frames polled at the normal interval. When the previous
        settle on this backend ended settled and no input was delivered since
        (every input crosses ``_deliver_backend_call``, which bumps the
        epoch), an identical fresh frame extends that already-observed run
        instead of starting a new one, saving one poll interval. A changed
        frame starts over exactly as before.
        """

        seed_entry = getattr(self, "_settle_seed", None)
        self._settle_seed = None
        epoch = getattr(self, "_actuation_epoch", 0)
        seeded = getattr(self.vision, "wait_settled_seeded", None)
        seed_type = getattr(self.vision, "SettleSeed", None)
        if seeded is None or seed_type is None:
            return self.vision.wait_settled(self.backend)
        seed = None
        if seed_entry is not None and seed_entry[1] == epoch:
            seed = seed_entry[0]
        result = seeded(self.backend, seed)
        if result.settled and result.phash:
            seed_kwargs: dict[str, Any] = {}
            if getattr(result, "mode", "pixel") == "page":
                seed_kwargs["png"] = result.png
            self._settle_seed = (
                seed_type(
                    phash=result.phash,
                    stable_frames=result.stable_frames,
                    captured_at=result.captured_at,
                    **seed_kwargs,
                ),
                epoch,
            )
        return result.png

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

        self._actuation_epoch = getattr(self, "_actuation_epoch", 0) + 1
        self._settle_seed = None
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
            or parent_state.step != self._sealed_step(step)
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
        *,
        interstitial: bool = False,
    ) -> Optional[str]:
        """Recheck exact authority at the last point before input delivery.

        ``interstitial`` marks a declared, reversible dialog dismissal at step
        entry. The approval gate is consulted only for the step's own input
        edges, after its target and right-record check resolved; a dismissal
        is still audited on the result and judged by the gate's screen check.
        """

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
        if (
            refusal is None
            and not interstitial
            and self._is_qualification_fault_step(step)
        ):
            # Every path (screen or API, any approval mode, or none): a drill
            # whose detector didn't refuse its changed input still saves
            # nothing. The drill then hasn't passed: its contract requires the
            # detector's own refusal, never this one.
            refusal = (
                "the bound qualification fault case did not produce its "
                "required detector refusal; refusing to cross the input boundary"
            )
        if (
            refusal is None
            and not interstitial
            and self.approval_gate is not None
            and self._step_is_consequential(step, workflow)
        ):
            refusal = self._approval_gate_refusal(workflow, step, result)
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

    def _begin_approval_item(
        self,
        report: RunReport,
        run_dir: Path,
        *,
        inputs_from_recording: list[str],
        durable_resume: bool,
        prior_screenshots_may_leave_box: bool,
    ) -> None:
        """Start the approval gate's item before any check or step runs."""

        self._approval_report = report
        self._approval_run_dir = Path(run_dir)
        self._approval_refusal = None
        gate = self.approval_gate
        authorization = self.governed_authorization
        bound_mode = getattr(authorization, "approval_mode", None)
        if bound_mode is not None and (gate is None or gate.mode.value != bound_mode):
            # The admitted authorization names the approval mode. A run with
            # another mode, or with no gate at all, is not that authorization.
            self._approval_refusal = (
                "Approval mode: this run's authorization requires the "
                f"{bound_mode} approval mode, but the run was started "
                f"{'without an approval gate' if gate is None else 'in ' + gate.mode.value}."
                " Nothing was typed or saved."
            )
            if gate is None:
                return
        if gate is None:
            return
        try:
            begin_refusal = gate.begin_item(
                run_id=self._run_id,
                bundle_content_digest=report.bundle_content_digest,
                profile=report.execution_profile,
                inputs_from_recording=inputs_from_recording,
                durable_resume=durable_resume,
                model_egress_possible=(
                    self.allow_model_grounding
                    or self._screenshots_may_leave_box
                    or prior_screenshots_may_leave_box
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a broken gate goes to a person
            begin_refusal = (
                "Approval mode: the item could not be started "
                f"({type(exc).__name__}); nothing was typed or saved."
            )
        if self._approval_refusal is None:
            self._approval_refusal = begin_refusal

    def _approval_gate_refusal(
        self, workflow: Workflow, step: Step, result: StepResult
    ) -> Optional[str]:
        """Ask the approval gate whether this consequential save may proceed."""

        gate = self.approval_gate
        assert gate is not None
        report = self._approval_report
        run_dir = self._approval_run_dir

        def frame() -> Optional[bytes]:
            if run_dir is None or not result.before_png:
                return None
            try:
                return _sealed.read_bytes(run_dir / result.before_png)
            except OSError:
                return None

        try:
            from openadapt_flow.approval_mode.gate import SaveContext

            verdict = gate.before_save(
                SaveContext(
                    step=step,
                    workflow=workflow,
                    result=result,
                    prior_results=list(report.results) if report is not None else [],
                    effect_verifier=self.effect_verifier,
                    governed_authorization=self.governed_authorization,
                    model_calls=report.model_calls if report is not None else 0,
                    frame=frame,
                    form_state=self._approval_form_state,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a broken gate goes to a person
            return (
                "Approval mode: the pre-save gate failed "
                f"({type(exc).__name__}); this item stopped before saving."
            )
        if verdict.proceed:
            return None
        return verdict.refusal_message() or (
            "Approval mode: this item stopped before saving."
        )

    def _approval_gated(self) -> bool:
        """Whether an approval mode other than ``not_gated`` governs this run."""

        gate = self.approval_gate
        return gate is not None and (
            str(getattr(getattr(gate, "mode", None), "value", "")) != "not_gated"
        )

    def _web_form_approval_view(
        self, step: Step, params: Mapping[str, RuntimeParamScalar]
    ) -> "Any | str":
        """Prepare an API step's request for a person to approve.

        Returns the view (the prepared request, its operation digest, the
        rows and image the person sees), or the reason it can't be prepared.
        Only reads are sent.
        """

        from openadapt_flow.approval_mode.item_approval import ApiRequestRef
        from openadapt_flow.runtime.actuators import ApiActuationResult
        from openadapt_flow.runtime.actuators.web_form import (
            operation_digest,
            operation_rows,
            render_operation_png,
        )

        binding = step.api_binding
        assert binding is not None
        actuator = self._actuator_for(binding)
        if actuator is None or not callable(getattr(actuator, "prepare", None)):
            return "this API actuator can't prepare the request for approval"
        gui_params = runtime_params_for_gui(params)
        try:
            prepared = actuator.prepare(binding, gui_params)
        except Exception as exc:  # noqa: BLE001 - only reads were sent
            return f"preparing the request failed ({type(exc).__name__})"
        if isinstance(prepared, ApiActuationResult):
            return prepared.reason or "the API step is unavailable"
        effects = self._resolve_effects(list(binding.effects), params)
        # Several record checks usually select the same record (one per
        # field): the person reads, and the digest binds, each pair once.
        record = list(
            dict.fromkeys(
                (key, str(expr))
                for effect in effects
                for key, expr in sorted(effect.match.items())
            )
        )
        digest = operation_digest(
            prepared,
            record_check=[effect.contract_hash() for effect in effects],
            record=record,
        )
        rows = operation_rows(binding, prepared, gui_params, record=record)
        return _WebFormApprovalView(
            prepared=prepared,
            digest=digest,
            rows=rows,
            png=render_operation_png(
                f"Approve this request: {step.intent or step.id}", rows
            ),
            ref=ApiRequestRef(
                method=prepared.method.upper(),
                path=prepared.path,
                payload_sha256=prepared.payload_sha256(),
            ),
        )

    def _approval_form_state(
        self, frame_png: Optional[bytes]
    ) -> Optional[tuple[str, str]]:
        """The form's current field state digest, for the approval gate.

        For an API step under an approval mode it is the operation digest of
        the request about to be sent (set just before the last-point gate).
        """

        if self._web_form_form_state_set:
            return self._web_form_form_state
        from openadapt_flow.approval_mode.form_state import capture_form_state

        try:
            return capture_form_state(self.backend, self.vision, frame_png)
        except Exception:  # noqa: BLE001 - unreadable is never "unchanged"
            return None

    def _is_qualification_fault_step(self, step: Step) -> bool:
        """Whether ``step`` is the fault step of the fault case this run is.

        A readiness drill (a fault qualification case) passes only when the
        engine's own detector refuses its changed input before delivery, so
        nothing is ever delivered at its fault step.
        """

        authorization = self.governed_authorization
        return bool(
            authorization is not None
            and authorization.qualification_case_kind not in {None, "representative"}
            and authorization.qualification_fault_step_id_sha256 is not None
            and authorization.qualification_fault_step_id_sha256
            == sha256_bytes(step.id.encode("utf-8"))
        )

    def _approval_step_refusal(
        self,
        workflow: Workflow,
        step: Step,
        step_index: int,
        result: StepResult,
        *,
        api_view: Optional[Any] = None,
    ) -> Optional[str]:
        """Ask the approval gate before a save step resolves its target.

        ``api_view`` is the prepared request of an API step: the person is
        then shown that request, and its operation digest is the "form".
        """

        gate = self.approval_gate
        assert gate is not None
        authorization = self.governed_authorization
        drill_fault_step = self._is_qualification_fault_step(step)

        def frame() -> Optional[bytes]:
            if api_view is not None:
                return api_view.png
            try:
                return self.vision.wait_settled(self.backend)
            except Exception:  # noqa: BLE001 - no frame means no approval
                return None

        def form_state(frame_png: Optional[bytes]) -> Optional[tuple[str, str]]:
            if api_view is not None:
                return ("api_request", api_view.digest)
            return self._approval_form_state(frame_png)

        def save_fields() -> Optional[Sequence[Any]]:
            if api_view is None:
                return None
            from openadapt_flow.approval_mode.item_approval import (
                MAX_SAVE_FIELDS,
                SaveField,
            )

            rows = api_view.rows
            if len(rows) > MAX_SAVE_FIELDS:
                # The image lists every row; the phone shows it alone.
                return None
            return [
                SaveField(label=label[:128] or "-", after=value[:512])
                for label, value in rows
            ]

        try:
            from openadapt_flow.approval_mode.gate import StepContext

            verdict = gate.before_step(
                StepContext(
                    step=step,
                    workflow=workflow,
                    result=result,
                    step_index=step_index,
                    run_id=self._run_id or "",
                    frame=frame,
                    form_state=form_state,
                    save_fields=save_fields,
                    api_request=api_view.ref if api_view is not None else None,
                    drill_fault_step=drill_fault_step,
                    qualification_case_kind=(
                        authorization.qualification_case_kind
                        if authorization is not None
                        else None
                    ),
                    runtime_inputs_digest=(
                        authorization.runtime_inputs_digest
                        if authorization is not None
                        else None
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - a broken gate goes to a person
            return (
                "Approval mode: the approval step failed "
                f"({type(exc).__name__}); this item stopped before saving."
            )
        if verdict.proceed:
            return None
        return verdict.refusal_message() or (
            "Approval mode: this item stopped before saving."
        )

    def _stamp_approval(self, report: RunReport) -> None:
        """Stamp the approval gate's post-run decision on the report."""

        gate = self.approval_gate
        if gate is None:
            return
        try:
            report.approval = gate.finish_item(report)
        except Exception:  # noqa: BLE001 - an unknown decision goes to a person
            from openadapt_flow.approval_mode.model import (
                ApprovalModeItemEvidence,
                ItemDecisionKind,
            )

            report.approval = ApprovalModeItemEvidence(
                mode=gate.mode,
                decision=ItemDecisionKind.PROBLEM,
                decision_reason="gate_error",
                needs_person=True,
            )

    def _read_gate_refusal(self, step: Step, result: StepResult) -> Optional[str]:
        """Halt before acting when values this step needs are below its gate."""

        from openadapt_flow.records.schema import GradeTooLow, RecordRead

        gate = step.read_gate
        assert gate is not None
        reads = tuple(self._field_reads.values())
        if gate.fields:
            missing = [name for name in gate.fields if name not in self._field_reads]
            reads = tuple(r for r in reads if r.field in gate.fields)
        else:
            missing = []
        record = RecordRead.model_construct(
            schema_name="",
            schema_version=1,
            record_key="",
            run_id=self._run_id,
            read_at="",
            fields=reads,
        )
        try:
            gate.check(record)
            if missing:
                raise GradeTooLow(
                    "", {name: "unchecked" for name in missing}, gate.min_grade
                )
        except GradeTooLow as exc:
            result.safety_halt = True
            result.failure_category = "safety_halt"
            listed = ", ".join(f"{k} ({v})" for k, v in exc.below.items())
            return (
                f"Step '{step.id}' ({step.intent}) needs read values at "
                f"{gate.min_grade} or better; below it: {listed}. Halting for "
                "a person"
            )
        return None

    def _act_read(
        self,
        step: Step,
        resolution: Optional[Resolution],
        *,
        workflow: Workflow,
        params: Mapping[str, RuntimeParamScalar],
        before_png: bytes,
        result: StepResult,
    ) -> Optional[str]:
        """READ: read one field into ``result.field_read``. Zero model calls:
        OCR and the clipboard are deterministic channels."""

        from openadapt_flow.records.read import read_field
        from openadapt_flow.records.schema import SealRef

        spec = step.read
        assert spec is not None
        region = self._translate_recorded_region(step, resolution, spec.region)
        clipboard = self._read_clipboard
        if spec.proof.double_read and clipboard is None:
            result.failure_category = "runtime_failure"
            return (
                f"Step '{step.id}' ({step.intent}) asks for a double read but "
                "this run has no clipboard channel"
            )
        if clipboard is not None:
            # Focusing and copying a field is input delivery, so it passes the
            # same authorization gate as any other input.
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
            if refusal is not None:
                return refusal
            result.delivery_attempted = True
        seal = SealRef(run_id=self._run_id) if spec.proof.seal else None
        try:
            field_read = read_field(
                spec.field,
                self.backend,
                region,
                proof=spec.proof,
                clipboard=clipboard,
                copy_keys=tuple(spec.copy_keys),
                ocr=self.vision.ocr,
                step_id=step.id,
                seal=seal,
                frame_png=before_png,
            )
        except Exception as exc:  # noqa: BLE001 - a read failure is a halt
            result.failure_category = "runtime_failure"
            return f"Step '{step.id}' ({step.intent}) could not read: {exc}"
        result.field_read = field_read
        result.actuation = "read"
        self._field_reads[spec.field.name] = field_read
        if field_read.needs_review and spec.on_review == "halt":
            result.safety_halt = True
            result.failure_category = "safety_halt"
            return (
                f"Step '{step.id}' ({step.intent}) read a value that needs "
                f"review ({field_read.review_reason}). Halting for a person"
            )
        return None

    @timed_span("actuate")
    def _act(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: Mapping[str, RuntimeParamScalar],
        **kwargs: Any,
    ) -> Optional[str]:
        """Perform the step's action.

        A click recorded as closing its own popup (``Step.closes_surface``)
        arms the page backend's one-shot expectation first
        (:class:`~openadapt_flow.backend.PageCloseExpectationBackend`), so a
        "page closed" report from that click counts as delivery once the page
        is confirmed closed. Every other step leaves such a report uncertain.
        """

        guarded = step.anchor is not None and step.anchor.form_identity is not None
        backend = self.backend
        try:
            error = self._act_closing_surface(step, resolution, params, **kwargs)
        finally:
            # A create-new form's save: the identity check armed a guard that
            # cancels any other submit during the click (ir.FormIdentity).
            refusal_of = getattr(backend, "form_submit_guard_refusal", None)
            refusal = refusal_of() if guarded and callable(refusal_of) else None
        if refusal is not None and error is None:
            result = kwargs.get("result")
            if result is not None:
                result.safety_halt = True
                result.failure_category = "safety_halt"
            return (
                f"Step '{step.id}' ({step.intent}): {refusal}; the submission was "
                "cancelled before it left the page — run aborted"
            )
        return error

    def _act_closing_surface(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: Mapping[str, RuntimeParamScalar],
        **kwargs: Any,
    ) -> Optional[str]:
        expect = (
            getattr(self.backend, "expect_page_close", None)
            if step.closes_surface
            else None
        )
        if not callable(expect):
            return self._act_answering_dialog(step, resolution, params, **kwargs)
        backend = self.backend
        expect()
        try:
            return self._act_answering_dialog(step, resolution, params, **kwargs)
        finally:
            cancel = getattr(backend, "cancel_page_close", None)
            if callable(cancel):
                cancel()

    def _act_answering_dialog(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: Mapping[str, RuntimeParamScalar],
        **kwargs: Any,
    ) -> Optional[str]:
        """Perform the step's action, answering the dialog it raises, if any.

        A step with a recorded ``dialog`` arms a one-shot answer on the
        browser backend before its input edge. The dialog's message is its
        identity: a different message is dismissed, never accepted, and the
        step halts; so does a dialog that never appears. A backend that cannot
        see browser dialogs refuses the step before any input
        (docs/MULTI_SURFACE.md, "Dialogs"). Errors name a code
        (``dialog_missing``, ``dialog_mismatch``), never the dialog's text:
        the error lands in report.json and REPORT.md, and a live dialog can
        show application data.
        """

        dialog = step.dialog
        if dialog is None:
            return self._act_without_dialog(step, resolution, params, **kwargs)
        arm = getattr(self.backend, "arm_dialog", None)
        take = getattr(self.backend, "take_dialogs", None)
        if not callable(arm) or not callable(take):
            return (
                f"Step '{step.id}' ({step.intent}) answers a browser dialog, but "
                "the active backend cannot see browser dialogs; run aborted"
            )
        self._arm_dialog(arm, dialog)
        try:
            error = self._act_without_dialog(step, resolution, params, **kwargs)
        finally:
            seen = take(0.0)
        if error is not None:
            return error
        if not seen:
            seen = self._wait_for_dialog(take, dialog)
        if not seen:
            return (
                f"Step '{step.id}' ({step.intent}): the recorded browser dialog "
                "did not appear (dialog_missing); run aborted"
            )
        mismatched = [entry for entry in seen if not entry.get("matched")]
        if mismatched:
            return (
                f"Step '{step.id}' ({step.intent}): a browser dialog did not "
                "show the recorded message (dialog_mismatch); it was "
                "dismissed, not answered; run aborted"
            )
        return None

    @staticmethod
    def _arm_dialog(arm: Callable[..., Any], dialog: Any) -> None:
        if dialog.message_pattern is not None:
            arm(dialog.response, None, message_pattern=dialog.message_pattern)
        else:
            arm(dialog.response, dialog.message)

    @timed_span("postcondition")
    def _wait_for_dialog(self, take: Callable[..., Any], dialog: Any) -> list[Any]:
        """Wait briefly for an armed dialog that opens after the input edge."""

        arm = getattr(self.backend, "arm_dialog", None)
        if callable(arm):
            self._arm_dialog(arm, dialog)
        seen = take(_DIALOG_WAIT_S)
        return list(seen) if isinstance(seen, list) else []

    def _act_without_dialog(
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
        if step.read_gate is not None:
            refusal = self._read_gate_refusal(step, result)
            if refusal is not None:
                return refusal
        if step.action is ActionKind.READ:
            return self._act_read(
                step,
                resolution,
                workflow=workflow,
                params=params,
                before_png=before_png,
                result=result,
            )
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
                    rechecked = self._pixel_recheck(
                        step,
                        resolution,
                        before_png,
                        params,
                        workflow,
                        bundle_dir,
                        result,
                        run_dir,
                    )
                    if isinstance(rechecked, str):
                        return rechecked
                    resolution = rechecked
                    x, y = resolution.point
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
                rechecked = self._pixel_recheck(
                    step,
                    resolution,
                    before_png,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    run_dir,
                )
                if isinstance(rechecked, str):
                    return rechecked
                x, y = rechecked.point
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
                env_var = getattr(self, "secret_env_overrides", {}).get(
                    step.param or ""
                ) or secret_env_var(step.param or "")
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

    @timed_span("settle")
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
        application field differs. The one exception is the signed-in web
        session observer: its version is the operator's Check it confirmation,
        bound into its contract and environment digests.
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
        refusal, observed, observer_ids = self._bind_qualification_boundary(
            project.environment
        )
        if observed is not None and observer_ids is not None:
            report.observed_application_sha256 = hashlib.sha256(
                observed.application_identity.encode("utf-8")
            ).hexdigest()
            report.observed_application_version_sha256 = hashlib.sha256(
                observed.application_version.encode("utf-8")
            ).hexdigest()
            report.observed_session_sha256 = observed.session_identity_sha256
            report.observed_environment_digest = observed.environment_digest
            report.qualification_environment_observer_id = observer_ids[0]
            report.qualification_environment_observer_contract_sha256 = observer_ids[1]
            report.observed_environment_binding_sha256 = observed.binding_sha256(
                observer_id=observer_ids[0],
                observer_contract_sha256=observer_ids[1],
            )
        return refusal

    def _bind_qualification_boundary(
        self, boundary: Any, *, surface_scoped: bool = False
    ) -> tuple[
        Optional[str],
        Optional[QualificationEnvironmentObservation],
        Optional[tuple[str, str]],
    ]:
        """Observe ``self.backend`` against one qualified environment boundary.

        Returns ``(refusal, observation, (observer_id, observer_contract))``.
        The observation is returned whenever one was taken, even when it does
        not match, so the caller can retain it. On a match the input-edge
        environment guard is bound to ``self.backend``. A multi-surface
        qualification case calls this once for the primary surface (the
        project's ``environment``) and again on every surface switch, with that
        surface's boundary (``QualificationProject.surface_environments``).
        """

        configured_observer = self.qualification_environment_observer
        if (
            surface_scoped
            and configured_observer is not None
            and boundary.environment_observer_id is not None
        ):
            try:
                configured_id = configured_observer.observer_id
            except Exception:
                configured_id = None
            if configured_id != boundary.environment_observer_id:
                # A run has one configured observer. A surface whose boundary
                # names another observer uses that observer's own route below.
                configured_observer = None
        from openadapt_flow.qualification_web_session import (
            SIGNED_IN_WEB_SESSION_OBSERVER_ID,
            SignedInWebSessionObserver,
        )
        from openadapt_flow.qualify.environment import (
            WEB_OBSERVER_ID,
            WebEnvironmentObserver,
        )

        if isinstance(
            configured_observer, BackendQualificationEnvironmentObserver
        ) and boundary.environment_observer_id in (
            SIGNED_IN_WEB_SESSION_OBSERVER_ID,
            WEB_OBSERVER_ID,
        ):
            # The deployment's default is the backend's own marker observer.
            # A project bound to a page-owned web observer uses that observer
            # instead; its id, contract and environment digest below still
            # have to match the project exactly.
            configured_observer = None
        if configured_observer is None:
            if boundary.environment_observer_id == WEB_OBSERVER_ID:
                # A web app with neither OpenAdapt markers nor a session
                # cookie: its origin, browser, viewport, platform, runtime
                # and the version the operator confirmed (qualify's observer).
                configured_observer = WebEnvironmentObserver(
                    application_version=boundary.application_version,
                    headless=bool(getattr(self.backend, "headless", True)),
                )
            elif boundary.environment_observer_id == SIGNED_IN_WEB_SESSION_OBSERVER_ID:
                # Real web apps carry no OpenAdapt markers: identify them by
                # origin and signed-in session, with the version the operator
                # confirmed in Check it. The contract and environment digests
                # below still have to match the project exactly.
                try:
                    configured_observer = SignedInWebSessionObserver(
                        boundary.application_version
                    )
                except ValueError:
                    return (
                        "qualification confirmed application version is invalid",
                        None,
                        None,
                    )
        if configured_observer is None:
            if not callable(
                getattr(self.backend, "qualification_environment_identity", None)
            ):
                return (
                    "qualification backend cannot observe the complete "
                    "environment; configure the project's environment observer",
                    None,
                    None,
                )
            configured_observer = BackendQualificationEnvironmentObserver(self.backend)
        observer = configured_observer
        expected_observer = (
            boundary.environment_observer_id,
            boundary.environment_observer_contract_sha256,
        )
        try:
            observed_observer = (
                observer.observer_id,
                observer.contract_sha256,
            )
        except Exception:
            return (
                "qualification environment observer identity is unavailable",
                None,
                None,
            )
        if expected_observer != observed_observer:
            return (
                "qualification environment observer does not match the project",
                None,
                None,
            )
        backend = self.backend
        target_kind = boundary.target_kind

        def environment_call() -> QualificationEnvironmentObservation:
            try:
                before_observer = (
                    observer.observer_id,
                    observer.contract_sha256,
                )
            except Exception as exc:
                raise StructuralResolutionRefused(
                    "qualification environment observer binding is unavailable"
                ) from exc
            if before_observer != observed_observer:
                raise StructuralResolutionRefused(
                    "qualification environment observer binding changed before input"
                )
            current = observer.observe(backend, target_kind)
            try:
                after_observer = (
                    observer.observer_id,
                    observer.contract_sha256,
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
            return "qualification environment observation failed", None, None
        if observed.target_kind != boundary.target_kind:
            return (
                "qualification environment observer returned the wrong surface",
                None,
                None,
            )
        ids = (observed_observer[0], observed_observer[1])
        if boundary.expected_application_identity is None:
            return (
                "qualification project has no executable application identity; "
                "set application_identity separately from the display name",
                observed,
                ids,
            )
        if observed.application_identity != boundary.expected_application_identity:
            return (
                "observed qualification application does not match the project",
                observed,
                ids,
            )
        if observed.application_version != boundary.application_version:
            return (
                "observed qualification application version does not match the project",
                observed,
                ids,
            )
        if observed.environment_digest != boundary.environment_digest:
            return (
                "observed qualification environment digest does not match the project",
                observed,
                ids,
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
            return (
                "qualification backend cannot bind an input-edge environment guard",
                observed,
                ids,
            )
        set_guard(self._require_qualification_environment_current)
        return None, observed, ids

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

    @timed_span("postcondition")
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
                    workflow, params, step, result, interstitial=True
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

    @timed_span("identity")
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
          (see :func:`identity.verify_pixel_identity`). It also abstains when
          the identity band embeds a parameter whose run value differs from
          the demonstrated value (the recorded crop shows the demo's value;
          see :func:`identity.run_bound_identity_params`), unless identity
          rests on a glyph-confusable identifier.
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
                quorum = self._verify_signal_quorum(
                    step=step,
                    resolution=resolution,
                    before_png=before_png,
                    params=runtime_params_for_gui(params),
                    workflow=workflow,
                    bundle_dir=bundle_dir,
                    policy=identity_policy,
                )
                # An operator's quorum policy and a create-new form's identity
                # must both hold.
                if quorum.status == "verified" and anchor.form_identity is not None:
                    form = self._verify_form_identity(
                        step, resolution, anchor.form_identity
                    )
                    if form.status != "verified":
                        return form
                return quorum
        if anchor.form_identity is not None:
            # A create-new form's save: the record is what this run typed into
            # the form, so the form is checked, in the DOM (see
            # ir.FormIdentity).
            return self._verify_form_identity(step, resolution, anchor.form_identity)

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

        def pixel_reference_is_run_bound() -> bool:
            # The recorded crop pictures the DEMONSTRATION's screen. When the
            # identity band embeds a parameter whose run value differs from the
            # demonstrated one (a first name typed earlier in this run), the
            # right record renders different pixels too, so a pixel compare
            # measures the parameter change, not the record, and its verdict
            # depends only on how the two values happen to render. The OCR
            # tier's parameter check, which substitutes the run's value into
            # the recorded band and requires the whole band, decides instead.
            # A band resting on a glyph-confusable identifier keeps the pixel
            # tier: OCR cannot separate O/0 there, so its verdict stands.
            tmpl = anchor.identity_template
            if tmpl is not None and tmpl.tokens:
                embedded = list(tmpl.param_token_indices)
                confusable = tmpl.rests_on_confusable_identifier
            elif anchor.context_text:
                embedded = identity_mod.embedded_params(
                    anchor.context_text, workflow.params
                )
                confusable = identity_mod.identity_rests_on_confusable_identifier(
                    anchor.context_text
                )
            else:
                return False
            run_values = runtime_params_for_gui(params)
            bound = identity_mod.run_bound_identity_params(
                embedded, run_values, workflow.params
            )
            if not bound or confusable:
                return False
            return not any(
                identity_mod.identity_rests_on_confusable_identifier(
                    run_values.get(name)
                )
                for name in bound
            )

        def pixel_tier() -> Optional[IdentityCheck]:
            if pixel_reference_is_run_bound():
                return None  # defer to the OCR parameter check (see above)
            if self._render_scale_inner is not None:
                # The live crop is a resampled render at another display
                # scale. The tier's thresholds hold only between matching
                # renders, so it abstains; the OCR tier decides or halts.
                return None
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

        check = identity_mod.run_identity_ladder(
            [structured_tier, pixel_tier, vlm_tier, ocr_tier]
        )
        return self._apply_candidate_census(
            step, resolution, check, before_png, params, workflow, bundle_dir
        )

    def _structured_census(self, step: Step, point: Point) -> census_mod.CensusDecision:
        """The DOM/a11y candidate census at ``point`` (see identity_census)."""

        anchor = step.anchor
        assert anchor is not None
        getter = getattr(self.backend, "identity_candidates_at", None)
        if getter is None:
            return census_mod.CensusDecision("cannot_count")
        try:
            census = getter(int(point[0]), int(point[1]))
        except Exception:
            census = None
        tmpl = anchor.identity_template
        recorded = tmpl if tmpl is not None and tmpl.record_key else None
        from openadapt_flow.runtime import identity_template as itmpl

        return census_mod.decide(
            census,
            has_recorded_key=recorded is not None,
            key_matches=lambda key: (
                recorded is not None and itmpl.record_key_matches(recorded, key)
            ),
        )

    def _ocr_census(
        self,
        step: Step,
        resolution: Resolution,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
    ) -> census_mod.CensusDecision:
        """Count the frame's OCR rows that verify as the recorded identity.

        The pixel-only counterpart of :meth:`_structured_census`: every text
        row of the frame is read the way the OCR tier reads the target's row
        (the same band height, the target's own crop excluded at the same
        offset, volatile lines dropped) and checked against the recorded band.
        """
        anchor = step.anchor
        assert anchor is not None
        tmpl = anchor.identity_template
        if not (anchor.context_text or (tmpl is not None and tmpl.tokens)):
            return census_mod.CensusDecision("cannot_count")
        today = date.today()
        try:
            lines = [
                line
                for line in self.vision.ocr(before_png, region=None)
                if line.text.strip()
                and not identity_mod.is_volatile_line(line.text, reference_date=today)
            ]
        except Exception:
            return census_mod.CensusDecision("cannot_count")
        viewport = self.backend.viewport
        px, py = int(resolution.point[0]), int(resolution.point[1])
        ex, ey = (
            anchor.region[0] - anchor.click_point[0],
            anchor.region[1] - (anchor.click_point[1]),
        )
        run_params = runtime_params_for_gui(params)

        def row_text(y: int) -> str:
            band = identity_mod.band_region((px, y), anchor.region[3], viewport)
            exclude = (px + ex, y + ey, anchor.region[2], anchor.region[3])
            kept = [
                line
                for line in lines
                if identity_mod.regions_intersect(line.region, band)
                and not identity_mod.regions_intersect(line.region, exclude)
            ]
            return " ".join(
                line.text.strip() for line in identity_mod.lines_near_point(kept, y)
            )

        def verifies(text: str) -> bool:
            if tmpl is not None and tmpl.tokens:
                from openadapt_flow.runtime import identity_template as itmpl

                verdict = itmpl.verify_template_identity(
                    tmpl, text, params=run_params, param_examples=workflow.params
                )
            else:
                assert anchor.context_text is not None
                verdict = identity_mod.verify_target_identity(
                    anchor.context_text,
                    text,
                    params=run_params,
                    param_examples=workflow.params,
                )
            return verdict.status == "verified"

        target_lines = identity_mod.lines_near_point(lines, py)
        heights = sorted(line.region[3] for line in target_lines) or [16]
        same_row_px = max(4, heights[len(heights) // 2])
        centers: list[int] = []
        for line in sorted(lines, key=lambda item: item.region[1]):
            center = line.region[1] + line.region[3] // 2
            if not centers or center - centers[-1] > 3:
                centers.append(center)
        rows = [(y, row_text(y)) for y in centers]
        count = census_mod.count_matching_rows(
            rows, target_y=py, verifies=verifies, same_row_px=same_row_px
        )
        if count > 1:
            return census_mod.CensusDecision("ambiguous", count)
        return census_mod.CensusDecision("unique", count)

    def _apply_candidate_census(
        self,
        step: Step,
        resolution: Resolution,
        check: IdentityCheck,
        before_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Optional[Path] = None,
    ) -> IdentityCheck:
        """Keep ``verified`` only when the identity picks one shown record.

        A verified ladder verdict says the target reads like the recorded
        row. When another shown row reads the same, the verdict cannot tell
        them apart, whichever rung resolved the point. The census
        (:mod:`openadapt_flow.runtime.identity_census`) then decides: the
        recorded record id picks the row, or the step halts.
        """
        if check.status != "verified" or check.mode not in (
            "structured",
            "pixel",
            "vlm",
            "context",
            "param",
        ):
            return check
        teach_role = census_mod.teach_time_role(step)
        if check.mode == "structured":
            decision = self._structured_census(step, resolution.point)
        else:
            decision = self._ocr_census(step, resolution, before_png, params, workflow)
            if decision.verdict == "cannot_count" and check.mode in ("pixel", "vlm"):
                # No OCR band to read: count the places the identifier crop
                # itself verifies instead.
                decision = self._pixel_census(
                    step, resolution, before_png, workflow, bundle_dir
                )
        if decision.verdict == "cannot_count" and teach_role == "control":
            # Teach time recorded a form field or a control, and no census can
            # count here. A census that can count always decides.
            decision = census_mod.CensusDecision("not_a_row")
        if decision.verdict == "not_a_row":
            return check.model_copy(update={"census_status": "not_a_row"})
        if decision.verdict == "cannot_count":
            if not self._leads_into_consequential(step, workflow):
                return check.model_copy(update={"census_status": "cannot_count"})
            return check.model_copy(
                update={"status": "abstain", "ambiguity_halt": "identity_uncounted"}
            )
        if decision.verdict == "unique":
            return check.model_copy(
                update={"candidate_count": decision.candidate_count}
            )
        if decision.verdict in ("wrong_record", "retarget"):
            return check.model_copy(
                update={
                    "status": "mismatch",
                    "coverage": 0.0,
                    "expected": "<recorded record id>",
                    "candidate_count": decision.candidate_count,
                }
            )
        return check.model_copy(
            update={
                "status": "abstain",
                "candidate_count": decision.candidate_count,
                "ambiguity_halt": (
                    "candidates_uncounted"
                    if decision.verdict == "uncounted"
                    else "identical_candidates"
                ),
            }
        )

    def _pixel_census(
        self,
        step: Step,
        resolution: Resolution,
        before_png: bytes,
        workflow: Workflow,
        bundle_dir: Optional[Path],
    ) -> census_mod.CensusDecision:
        """Count the places on the frame that could show the identifier crop.

        For a pixel identity with no OCR band (a remote display): every place
        the recorded identifier crop matches is cut from the frame and checked
        with the ladder's pixel verifier. A clear ``mismatch`` rules a place
        out, and so does its own text when OCR reads both crops and the text
        differs (sibling tabs share their chrome, not their label). A verify,
        an abstain (drift, the uncertain band, verify turned off) or no verdict
        with the same or unreadable text counts it as a look-alike. The census is
        ``uncounted`` when the frame has too many places, when a place cannot
        be cut, or when the target's own place is not among them (the frame
        renders at another scale than the recorded crop). A featureless crop
        (a blank band, a ruled line) cannot count at all: ``cannot_count``.
        """
        from openadapt_flow.vision.match import (
            AMBIGUITY_SUSPICION_SCORE,
            crop_is_featureless,
            template_peaks,
        )

        anchor = step.anchor
        assert anchor is not None
        recorded, live = self._identifier_crops(
            anchor, resolution, before_png, bundle_dir, workflow=workflow
        )
        if recorded is None or live is None or anchor.identifier_region is None:
            return census_mod.CensusDecision("cannot_count")
        try:
            if crop_is_featureless(recorded):
                # A blank band or a ruled line matches everywhere along it: it
                # cannot count rows (cannot_count, not "ambiguous").
                return census_mod.CensusDecision("cannot_count")
            peaks = template_peaks(
                before_png, recorded, threshold=AMBIGUITY_SUSPICION_SCORE
            )
        except Exception:
            return census_mod.CensusDecision("cannot_count")
        if peaks is None:
            return census_mod.CensusDecision("uncounted")
        rx, ry, rw, rh = anchor.identifier_region
        dx, dy = self._identifier_displacement(
            anchor, resolution, before_png, bundle_dir, workflow=workflow
        )
        target = (rx + dx, ry + dy)

        def is_target(x: int, y: int) -> bool:
            return abs(x - target[0]) < max(1, rw // 2) and abs(y - target[1]) < max(
                1, rh // 2
            )

        if not any(is_target(x, y) for x, y in peaks):
            return census_mod.CensusDecision("uncounted")

        def crop_text(png: bytes) -> str:
            """The crop's own text, squashed; empty when OCR reads nothing."""
            try:
                lines = self.vision.ocr(png, region=None)
            except Exception:
                return ""
            # A low-confidence fragment ("S", "10", "lohn") is a misread, not
            # evidence: the whole crop then counts as unreadable.
            if any(
                float(getattr(line, "confidence", 0.0) or 0.0)
                < PIXEL_CENSUS_OCR_MIN_CONFIDENCE
                for line in lines
            ):
                return ""
            return identity_mod.squash(
                " ".join(str(getattr(line, "text", "") or "") for line in lines)
            )

        def texts_differ(a: str, b: str) -> bool:
            """Clearly different labels, not one label read two ways."""
            import difflib

            if a in b or b in a:
                return False  # a dropped or clipped word, not another label
            ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
            return ratio < PIXEL_CENSUS_OCR_MAX_SIMILARITY

        target_text: Optional[str] = None
        count = 1
        for x, y in peaks:
            if is_target(x, y):
                continue
            crop = identity_mod.crop_region(before_png, (x, y, rw, rh))
            if crop is None:
                return census_mod.CensusDecision("uncounted")
            verdict = identity_mod.verify_pixel_identity(
                recorded, crop, enable_verify=self.pixel_verify_enabled
            )
            if verdict is not None and verdict.status == "mismatch":
                continue
            # The pixels do not rule this place out. Sibling controls that
            # share their chrome (section tabs, toggle buttons) look alike;
            # their own text still tells them apart. Only when OCR reads both
            # crops and the text differs is the place distinguishable.
            if target_text is None:
                target_text = crop_text(live)
            other_text = crop_text(crop)
            if target_text and other_text and texts_differ(target_text, other_text):
                continue
            count += 1
        if count > 1:
            return census_mod.CensusDecision("ambiguous", count)
        return census_mod.CensusDecision("unique", count)

    def _leads_into_consequential(
        self, step: Step, workflow: Optional[Workflow]
    ) -> bool:
        """Whether this step or any step after it is a consequential write.

        A step that picks a record (opening a chart) is not itself a write, but
        the write that follows lands on whatever it opened. Program graphs and
        steps not found in the linear list count every step of the workflow.
        """

        if workflow is None:
            return is_consequential(step, None)
        steps = list(workflow.steps)
        index = next((i for i, item in enumerate(steps) if item.id == step.id), None)
        following = steps[index:] if index is not None else steps
        return self._step_is_consequential(step, workflow) or any(
            self._step_is_consequential(item, workflow) for item in following
        )

    def _retarget_by_record_key(
        self,
        step: Step,
        resolution: Resolution,
        matched_region: Region,
    ) -> tuple[Resolution, Region]:
        """Move a visual resolution onto the row with the recorded record id.

        When rows read identically, a template or text rung lands on whichever
        row now sits where the recorded one was. The recorded record id names
        the right row; move the point (and matched region) by that row's
        offset. A structural resolution names one exact element and is never
        moved: if it is the wrong record, the identity check refuses it.
        """
        anchor = step.anchor
        if (
            anchor is None
            or resolution.structural_handle is not None
            or anchor.identity_template is None
            or not anchor.identity_template.record_key
        ):
            return resolution, matched_region
        decision = self._structured_census(step, resolution.point)
        if decision.verdict != "retarget" or decision.offset is None:
            return resolution, matched_region
        dx, dy = (int(round(v)) for v in decision.offset)
        x, y = resolution.point[0] + dx, resolution.point[1] + dy
        width, height = self.backend.viewport
        if not (0 <= x < width and 0 <= y < height):
            return resolution, matched_region
        moved = resolution.model_copy(
            update={
                "point": (x, y),
                # The retained visual evidence reproduces the old point.
                "visual_evidence": None,
                "resolution_evidence_regions": tuple(
                    (rx + dx, ry + dy, rw, rh)
                    for rx, ry, rw, rh in resolution.resolution_evidence_regions
                ),
            }
        )
        rx, ry, rw, rh = matched_region
        return moved, (rx + dx, ry + dy, rw, rh)

    def _identifier_displacement(
        self,
        anchor: Anchor,
        resolution: Resolution,
        before_png: bytes,
        bundle_dir: Optional[Path],
        *,
        workflow: Optional[Workflow] = None,
    ) -> tuple[int, int]:
        """How far the target moved between the recorded and the live frame.

        Template rungs resolve to the click point, so ``resolved -
        click_point`` is the displacement. The OCR rung resolves to the CENTER
        of the matched label, which is not where the operator clicked (a click
        to the right of a placeholder, for example). Using ``resolved -
        click_point`` then shifts the live identifier crop by that gap, and the
        pixel tier reports a different identifier on an unchanged screen.

        For an OCR resolution, OCR the label in the recorded anchor template
        and in the same-sized live crop, so both label centers come from
        equivalent crops and OCR's centroid jitter cancels. Falls back to the
        click-point displacement whenever a label can't be found.
        """
        fallback = (
            int(resolution.point[0] - anchor.click_point[0]),
            int(resolution.point[1] - anchor.click_point[1]),
        )
        if (
            resolution.rung != "ocr"
            or not anchor.ocr_text
            or not anchor.template
            or bundle_dir is None
        ):
            return fallback
        find_text = getattr(self.vision, "find_text", None)
        if find_text is None:
            return fallback
        template_png = self._asset_bytes(bundle_dir, anchor.template, workflow=workflow)
        if template_png is None:
            return fallback
        try:
            recorded = find_text(template_png, anchor.ocr_text, min_ratio=OCR_MIN_RATIO)
            if recorded is None:
                return fallback
            tx, ty, tw, th = anchor.region
            rec_center = (tx + recorded.point[0], ty + recorded.point[1])
            # First estimate from the resolved label center, then re-measure
            # the label in a live crop shaped like the recorded template.
            ex = int(round(resolution.point[0] - rec_center[0]))
            ey = int(round(resolution.point[1] - rec_center[1]))
            live_crop = identity_mod.crop_region(before_png, (tx + ex, ty + ey, tw, th))
            live = (
                find_text(live_crop, anchor.ocr_text, min_ratio=OCR_MIN_RATIO)
                if live_crop
                else None
            )
        except Exception:  # noqa: BLE001 - fall back, never fail open
            return fallback
        if live is None:
            return fallback
        return (
            int(round(ex + live.point[0] - recorded.point[0])),
            int(round(ey + live.point[1] - recorded.point[1])),
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
        dx, dy = self._identifier_displacement(
            anchor, resolution, before_png, bundle_dir, workflow=workflow
        )
        live_region = (
            rx + dx,
            ry + dy,
            rw,
            rh,
        )
        live_png = identity_mod.crop_region(before_png, live_region)
        return recorded_png, live_png

    def _verify_form_identity(
        self, step: Step, resolution: Resolution, identity: FormIdentity
    ) -> IdentityCheck:
        """Verify a create-new form's save against what this run typed.

        ``verified`` only when the backend can read the form and the control
        at the resolved point belongs to the form holding exactly the fields
        this run typed into, each with the value typed. A backend that cannot
        read the page leaves identity ``unreadable``, which halts an armed
        step like any other unreadable identity.
        """

        expected = (
            f"the form holding the {identity.filled_fields} field(s) this run "
            "typed, with the values typed"
        )
        check = getattr(self.backend, "form_identity_at", None)
        if not callable(check):
            return IdentityCheck(
                status="unreadable",
                mode="form",
                expected=expected,
                observed="this backend cannot read the form's fields",
            )
        reason = check(int(resolution.point[0]), int(resolution.point[1]), identity)
        if reason is None:
            return IdentityCheck(
                status="verified", mode="form", expected=expected, observed=expected
            )
        return IdentityCheck(
            status="mismatch", mode="form", expected=expected, observed=reason
        )

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
        once at 2x resolution, and then once more on the point's own text
        row alone (:func:`openadapt_flow.runtime.identity.row_region`),
        before the verdict.

        Returns:
            The best :class:`IdentityCheck` across the attempts.
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
        if identifier_region is not None and not self._identity_input_looks:
            # The check just before the click takes no input. When the step-entry
            # check settled the record with a copy and the identifier shows the
            # same pixels now, the same record is on screen: reuse that verdict.
            binding = self.__dict__.get("_identity_look_bindings", {}).get(step.id)
            if (
                binding is not None
                and binding.verdict is not None
                and binding.same_pixels(
                    step.id, looks_mod.crop_png(before_png, identifier_region)
                )
            ):
                reused = IdentityLook(
                    kind="clipboard",
                    outcome="resolves",
                    reason="same_pixels_as_step_entry_copy",
                )
                return binding.verdict.model_copy(
                    update={"looks": [*binding.verdict.looks, reused]}
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
            row_lines: Optional[list[Any]] = None,
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
                if row_lines is not None:
                    row_lines.extend(lines)
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
        first_row_lines: list[Any] = []

        def native_attempt(
            png: bytes, row_lines: Optional[list[Any]] = None
        ) -> IdentityCheck:
            return attempt(
                png,
                # Match compilation: OCR the full frame, then retain only the
                # translated identifier region. OCRing the crop itself can
                # change segmentation/glyph recognition and false-halt a
                # healthy screen.
                None if identifier_region is not None else live_region,
                resolution.point[1],
                exclude,
                scoped_identifier=identifier_region is not None,
                identifier_filter=identifier_region,
                row_lines=row_lines,
            )

        check = native_attempt(before_png, first_row_lines)
        if check.status == "verified":
            return check
        if identity_read_contradicts(check):
            # A read that names a different record is final: a later read of
            # the same screen must never overrule it into "verified". The one
            # exception is a run parameter OCR read with look-alike glyphs
            # (C0X1234 read as COX1234): extra looks may confirm the exact
            # value; otherwise this mismatch stands.
            return self._lookalike_param_looks(
                step,
                check,
                native_attempt,
                params,
                workflow,
                identifier_region,
                before_png,
            )
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
        pending_looks: list[Any] = []
        if (
            retry.status not in ("verified", "mismatch")
            and looks_mod.is_glyph_abstain(check)
            and (
                self.known_ids is not None
                or self._copy_look_possible(identifier_region)
            )
        ):
            # The band rests on a look-alike identifier. Take extra looks that
            # change nothing (runtime.identity_looks): fresh frames, the 2x
            # re-read above, the known-ID list, and, where configured, a
            # select-and-copy of the identifier cell. Only the list or an
            # exact copy can settle it; any contradiction is final.
            matches: Optional[Callable[[str], bool]] = None
            if anchor.identity_template is not None and anchor.identity_template.tokens:
                from openadapt_flow.runtime import identity_template as itmpl

                matches = functools.partial(
                    itmpl.recorded_identifier_matches, anchor.identity_template
                )
            looked = self._look_again(
                step,
                check,
                retry,
                native_attempt,
                params,
                workflow,
                identifier_region,
                before_png,
                matches,
            )
            if (
                looked.status != "abstain"
                or looked.known_id_halt is not None
                or looked.look_halt is not None
            ):
                return looked
            pending_looks = list(looked.looks)

        def with_looks(final: IdentityCheck) -> IdentityCheck:
            if not pending_looks or final.status == "verified" or final.looks:
                return final
            return final.model_copy(update={"looks": pending_looks})

        check = better_identity_read(check, retry)
        if (
            check.status == "verified"
            or identity_read_contradicts(check)
            or identifier_region is not None
        ):
            # A read of a different record is final: the row re-read below
            # must never overrule it into "verified".
            return with_looks(check)
        # Last, re-read the point's own text row alone (see
        # identity.row_region): the same verdict rules, on a crop that holds
        # no part of the neighbouring rows.
        row = identity_mod.row_region(first_row_lines, band)
        row_png = (
            None
            if row is None
            else identity_mod.upscale_crop(
                before_png, row, identity_mod.ROW_REREAD_FACTOR
            )
        )
        if row is None or row_png is None:
            return with_looks(check)
        f = identity_mod.ROW_REREAD_FACTOR
        row_check = attempt(
            row_png,
            None,
            (resolution.point[1] - row[1]) * f,
            (
                (exclude[0] - row[0]) * f,
                (exclude[1] - row[1]) * f,
                exclude[2] * f,
                exclude[3] * f,
            ),
        )
        return with_looks(better_identity_read(check, row_check))

    def _hover_check(
        self,
        step: Step,
        resolution: Resolution,
        frame_png: bytes,
        workflow: Workflow,
        bundle_dir: Optional[Path],
    ) -> Optional[IdentityCheck]:
        """Compare the target's hover response with the recording.

        Only at the step-entry check (moving the pointer is input), only for a
        click whose recording saw a hover response and that has no other
        identity evidence,
        only on the pixel rungs, and only when the backend can hover
        (:class:`~openadapt_flow.backend.PointerHoverBackend`). None when any
        of that does not hold: the step then has no identity check, as
        before. See :mod:`openadapt_flow.runtime.hover_evidence`.
        """
        from openadapt_flow.runtime import hover_evidence as hover_mod

        evidence = step.hover
        anchor = step.anchor
        if (
            evidence is None
            or not evidence.response
            or anchor is None
            or not self._identity_input_looks
            or step.action
            not in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK)
            or resolution.rung == "structural"
            or anchor.context_text
            or anchor.structured_identity
            or anchor.identity_template
            or anchor.identifier_crop
            or anchor.form_identity
        ):
            return None
        hover = getattr(self.backend, "hover", None)
        if not callable(hover):
            return None
        point = (int(resolution.point[0]), int(resolution.point[1]))

        def check(verdict: str, reason: str, png: Optional[bytes]) -> IdentityCheck:
            status = {
                "verified": "verified",
                "mismatch": "mismatch",
                "unexpected": "mismatch",
                "absent": "abstain",
                "unverifiable": "unreadable",
            }[verdict]
            return IdentityCheck(
                status=status,  # type: ignore[arg-type]
                mode="hover",
                expected="recorded hover response",
                observed=reason,
                hover=HoverLook(
                    verdict=verdict,  # type: ignore[arg-type]
                    reason=reason,
                    frame_sha256=sha256_bytes(png) if png is not None else None,
                ),
            )

        try:
            hover(*point)
        except NotImplementedError:
            return None
        except Exception as exc:  # noqa: BLE001 - a failed hover confirms nothing
            return check(
                "unverifiable", f"hover_failed:{type(exc).__name__}"[:80], None
            )
        time.sleep(evidence.dwell_ms / 1000.0)
        hover_png = self.vision.wait_settled(self.backend)
        try:
            live = hover_mod.measure_response(frame_png, hover_png, point)
        except ValueError:
            return check("mismatch", "screen_changed_size", hover_png)
        recorded_crop = (
            self._asset_bytes(bundle_dir, evidence.crop, workflow=workflow)
            if evidence.crop and bundle_dir is not None
            else None
        )
        # The click that follows is checked against this frame, not the one
        # before the pointer moved (see _pixel_recheck).
        self.__dict__.setdefault("_hover_frames", {})[step.id] = hover_png
        verdict, reason = hover_mod.compare(
            recorded_response=evidence.response,
            recorded_region=evidence.region,
            recorded_crop=recorded_crop,
            live=live,
            point=point,
            same_render=self._render_scale_inner is None,
            ocr=self.vision.ocr,
        )
        return check(verdict, reason, hover_png)

    def _band_check(
        self,
        anchor: Anchor,
        observed: str,
        run_values: Mapping[str, str],
        workflow: Workflow,
    ) -> IdentityCheck:
        """The OCR-tier verdict for ``observed`` with the given run values."""
        tmpl = anchor.identity_template
        if tmpl is not None and tmpl.tokens:
            from openadapt_flow.runtime import identity_template as itmpl

            return itmpl.verify_template_identity(
                tmpl, observed, params=dict(run_values), param_examples=workflow.params
            )
        return identity_mod.verify_target_identity(
            anchor.context_text or "",
            observed,
            params=dict(run_values),
            param_examples=workflow.params,
        )

    def _lookalike_param_read(
        self,
        check: IdentityCheck,
        anchor: Anchor,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
    ) -> Optional[IdentityCheck]:
        """A run-parameter mismatch that is only a look-alike misread, as the
        look-alike abstain it would be, or None.

        The parameter's identifier is read with look-alike glyphs (the run
        value ``C0X1234`` read as ``COX1234``). The band is checked again with
        the value as OCR read it; when every other identity budget then passes
        and only the look-alike identifier remains, OCR cannot say which of the
        two it is. Used only to decide whether extra looks may run; the caller
        keeps the mismatch unless a look settles the record.
        """
        if check.status != "mismatch" or check.mode != "param" or not check.param:
            return None
        run_values = runtime_params_for_gui(params)
        run = run_values.get(check.param)
        if not isinstance(run, str):
            return None
        run_tokens = looks_mod.identifier_tokens(run)
        if not any(identity_mod._is_glyph_vulnerable_identifier(t) for t in run_tokens):
            return None
        seen = looks_mod.identifier_tokens(check.observed)
        as_read: list[str] = []
        for token in run_tokens:
            same = {
                o
                for o in seen
                if known_ids_lookalike_class(o) == known_ids_lookalike_class(token)
            }
            if len(same) != 1:
                return None
            as_read.append(same.pop())
        if as_read == run_tokens:
            return None
        again = self._band_check(
            anchor,
            check.observed,
            {**run_values, check.param: " ".join(as_read)},
            workflow,
        )
        if not looks_mod.is_glyph_abstain(again):
            return None
        return again

    def _lookalike_param_looks(
        self,
        step: Step,
        check: IdentityCheck,
        native_attempt: Callable[[bytes], IdentityCheck],
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        identifier_region: Optional[Region],
        frame_png: bytes,
    ) -> IdentityCheck:
        """Extra looks for a look-alike run-parameter mismatch.

        Returns the looks' ``verified`` verdict, a named halt, or ``check``
        unchanged (with the looks recorded). Never turns a mismatch into an
        abstain.
        """
        anchor = step.anchor
        assert anchor is not None
        first = self._lookalike_param_read(check, anchor, params, workflow)
        if first is None or not (
            self.known_ids is not None or self._copy_look_possible(identifier_region)
        ):
            return check

        def read(png: bytes) -> IdentityCheck:
            got = native_attempt(png)
            return self._lookalike_param_read(got, anchor, params, workflow) or got

        matches: Optional[Callable[[str], bool]] = None
        if anchor.identity_template is not None and anchor.identity_template.tokens:
            from openadapt_flow.runtime import identity_template as itmpl

            matches = functools.partial(
                itmpl.recorded_identifier_matches, anchor.identity_template
            )
        looked = self._look_again(
            step,
            first,
            None,
            read,
            params,
            workflow,
            identifier_region,
            frame_png,
            matches,
        )
        if (
            looked.status == "verified"
            or looked.known_id_halt is not None
            or looked.look_halt is not None
        ):
            return looked
        return check.model_copy(update={"looks": list(looked.looks)})

    def _check_regions(
        self, step: Step, resolution: Resolution, matched_region: Optional[Region]
    ) -> dict[str, Region]:
        """The regions a pixel step was checked on, at ``resolution``.

        ``target``: where the target matched (or a box of the recorded
        target's size at the point). ``identity``: the identifier region, or
        the identity band's row, when the step has identity evidence.
        ``hover``: the recorded hover response, when there is one.
        """
        anchor = step.anchor
        px, py = int(resolution.point[0]), int(resolution.point[1])
        regions: dict[str, Region] = {}
        if matched_region is not None:
            regions["target"] = matched_region
        elif anchor is not None:
            w, h = int(anchor.region[2]), int(anchor.region[3])
            regions["target"] = (px - w // 2, py - h // 2, w, h)
        else:
            regions["target"] = (px - 8, py - 8, 16, 16)
        if anchor is not None:
            if anchor.identifier_region is not None:
                rx, ry, rw, rh = anchor.identifier_region
                regions["identity"] = (
                    rx + px - anchor.click_point[0],
                    ry + py - anchor.click_point[1],
                    rw,
                    rh,
                )
            elif anchor.context_text or anchor.identity_template:
                h = int(anchor.region[3])
                regions["identity"] = (
                    0,
                    py - h // 2 - 4,
                    int(self.backend.viewport[0]),
                    h + 8,
                )
        if step.hover is not None and step.hover.region is not None:
            dx, dy, w, h = step.hover.region
            regions["hover"] = (px + dx, py + dy, w, h)
        return regions

    @staticmethod
    def _changed_regions(
        before_png: bytes, fresh_png: bytes, regions: Mapping[str, Region]
    ) -> list[str]:
        """Names of ``regions`` whose pixels differ between two frames."""
        import numpy as np
        from PIL import Image

        try:
            with (
                Image.open(io.BytesIO(before_png)) as a_img,
                Image.open(io.BytesIO(fresh_png)) as b_img,
            ):
                a = np.asarray(a_img.convert("RGB"), dtype=np.int16)
                b = np.asarray(b_img.convert("RGB"), dtype=np.int16)
        except Exception:  # noqa: BLE001 - an unreadable frame is a change
            return ["frame_size"]
        if a.shape != b.shape:
            return ["frame_size"]
        height, width = a.shape[:2]
        changed: list[str] = []
        for name, (x, y, w, h) in regions.items():
            x0, y0 = max(0, int(x)), max(0, int(y))
            x1, y1 = min(width, int(x + w)), min(height, int(y + h))
            if x1 <= x0 or y1 <= y0:
                continue
            diff = np.abs(a[y0:y1, x0:x1] - b[y0:y1, x0:x1]).max(axis=2)
            if int((diff > _PIXEL_RECHECK_LEVEL).sum()) >= _PIXEL_RECHECK_MIN_PIXELS:
                changed.append(name)
        return changed

    def _pixel_recheck(
        self,
        step: Step,
        resolution: Resolution,
        checked_png: bytes,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        bundle_dir: Path,
        result: StepResult,
        run_dir: Path,
    ) -> Union[Resolution, str]:
        """Guard the gap between a pixel step's check and its click.

        Right before the click, take a fresh frame and compare the target,
        identity and hover regions with the frame the step was checked on (the
        hover frame when the step took a hover look). When one changed (a
        dialog opened over the target, the list re-sorted), do not click:
        re-resolve the target and re-run the identity check on the fresh
        frame, with no input, and compare again on the next frame. After
        :data:`_PIXEL_RECHECK_REOBSERVES` re-observations that still see
        changes, or when the target or its identity cannot be confirmed again,
        refuse the click. Structured (DOM / accessibility) targets are bound
        by element tokens instead and are not rechecked here.

        Returns the resolution to click, or the refusal.
        """
        if resolution.rung == "structural":
            return resolution
        baseline = self.__dict__.get("_hover_frames", {}).get(step.id) or checked_png
        current = resolution
        region = self._active_delivery_region
        for attempt in range(_PIXEL_RECHECK_REOBSERVES + 1):
            fresh = self.backend.screenshot()
            changed = self._changed_regions(
                baseline, fresh, self._check_regions(step, current, region)
            )
            if not changed:
                if current is not resolution:
                    result.resolution = current
                return current
            what = ", ".join(changed)
            if attempt == _PIXEL_RECHECK_REOBSERVES:
                result.check_to_click.append(
                    CheckToClickRecheck(changed=changed, outcome="aborted")  # type: ignore[arg-type]
                )
                break
            fresh_resolution, fresh_region, error = self._resolve_step(
                step, fresh, bundle_dir, workflow
            )
            if error is None and fresh_resolution is not None:
                error = self._identity_gate_error(
                    step,
                    fresh_resolution,
                    fresh,
                    params,
                    workflow,
                    bundle_dir,
                    result,
                    evidence_run_dir=run_dir,
                )
            if error is not None or fresh_resolution is None:
                result.check_to_click.append(
                    CheckToClickRecheck(changed=changed, outcome="aborted")  # type: ignore[arg-type]
                )
                result.safety_halt = True
                result.failure_category = "safety_halt"
                return (
                    f"Step '{step.id}' ({step.intent}): the screen changed at the "
                    f"{what} region between the check and the click, and the "
                    "target could not be confirmed again on a fresh frame "
                    f"({error or 'target not found'}) — click not delivered"
                )
            result.check_to_click.append(
                CheckToClickRecheck(changed=changed, outcome="reobserved")  # type: ignore[arg-type]
            )
            current, region, baseline = fresh_resolution, fresh_region, fresh
        result.safety_halt = True
        result.failure_category = "safety_halt"
        return (
            f"Step '{step.id}' ({step.intent}): the screen kept changing at the "
            f"{what} region between the check and the click — click not "
            "delivered; run aborted"
        )

    def _copy_look_allowed(self) -> bool:
        """The copy look is input: never in governed or qualification runs."""
        return (
            self.identity_copy is not None
            and self._read_clipboard is not None
            and self.governed_authorization is None
            and self.managed_dispatch_binding is None
            and self.production_qualification_guard is None
            and self.qualification_campaign_guard is None
            and self.qualification_fault_driver is None
            and getattr(self, "approval_gate", None) is None
        )

    def _copy_look_possible(self, identifier_region: Optional[Region]) -> bool:
        return identifier_region is not None and self._copy_look_allowed()

    def _embedded_identity_params(
        self, anchor: Anchor, workflow: Workflow
    ) -> list[str]:
        """Workflow parameters whose demonstrated value is in the identity band."""
        tmpl = anchor.identity_template
        if tmpl is not None and tmpl.tokens:
            return list(tmpl.param_token_indices)
        if anchor.context_text:
            return identity_mod.embedded_params(anchor.context_text, workflow.params)
        return []

    def _look_again(
        self,
        step: Step,
        first: IdentityCheck,
        magnified: Optional[IdentityCheck],
        read: Callable[[bytes], IdentityCheck],
        params: Optional[Mapping[str, RuntimeParamScalar]],
        workflow: Workflow,
        identifier_region: Optional[Region],
        frame_png: bytes,
        recorded_id_matches: Optional[Callable[[str], bool]] = None,
    ) -> IdentityCheck:
        """Extra looks before stopping on a look-alike identifier.

        Returns the verdict of :func:`identity_looks.decide`: ``verified``
        when every fresh frame agrees and the known-ID list or an exact copy
        settles the record, the contradicting read when a look names another
        record, or ``first`` carrying the looks. A known-ID list that may not
        be used now, or a copy look that changed the screen, returns ``first``
        marked with a named halt.
        """
        from openadapt_flow.runtime import known_ids as known_ids_mod

        admission = (
            self.known_ids
            if isinstance(self.known_ids, known_ids_mod.KnownIdAdmission)
            else None
        )
        known: Any = self.known_ids
        if admission is not None:
            halt = admission.check()
            if halt is not None:
                return first.model_copy(update={"known_id_halt": halt.reason})
            known = admission.known_ids
        run_values = runtime_params_for_gui(params or {})

        frames: list[tuple[IdentityCheck, Optional[str]]] = []
        for _ in range(looks_mod.FRAME_LOOKS):
            time.sleep(self.poll_interval_s)
            png = self.backend.screenshot()
            frames.append((read(png), sha256_bytes(png)))
            if frames[-1][0].status == "mismatch":
                break  # final; no further looks

        frame_reads = [first, *(r for r, _ in frames)]
        known_lift: Optional[IdentityCheck] = None
        if known is not None and all(
            looks_mod.is_glyph_abstain(r) for r in frame_reads
        ):
            lifted = known_ids_mod.resolve_lookalike_abstain(
                frame_reads,
                known,
                run_values=run_values,
                recorded_id_matches=recorded_id_matches,
            )
            if lifted is not None and lifted.known_id_evidence is not None:
                update: dict[str, Any] = {"frames": len(frame_reads)}
                if admission is not None:
                    update["confirmed_at"] = admission.confirmation.confirmed_at
                    update["export_sha256"] = admission.confirmation.sha256
                known_lift = lifted.model_copy(
                    update={
                        "known_id_evidence": lifted.known_id_evidence.model_copy(
                            update=update
                        )
                    }
                )

        copied: Optional[looks_mod.ClipboardRead] = None
        classes = looks_mod.lookalike_classes(first.observed)
        bindings = self.__dict__.setdefault("_identity_look_bindings", {})
        if (
            identifier_region is not None
            and self._copy_look_allowed()
            and all(r.status != "mismatch" for r, _ in frames)
        ):
            assert self.identity_copy is not None
            if self._identity_input_looks:
                try:
                    cell_lines = self.vision.ocr(frame_png, region=identifier_region)
                except Exception:  # noqa: BLE001 - no box, no copy look
                    cell_lines = []
                cell = looks_mod.copy_region(cell_lines, identifier_region)
                copied = (
                    looks_mod.copy_identifier(
                        self.backend,
                        self._read_clipboard,
                        cell,
                        gesture=self.identity_copy.gesture,
                        copy_keys=self.identity_copy.copy_keys,
                        crop_region=identifier_region,
                    )
                    if cell is not None
                    else looks_mod.ClipboardRead(
                        text=None, reason="no_single_identifier_box"
                    )
                )
                if copied.changed_outside:
                    look = IdentityLook(
                        kind="clipboard",
                        outcome="unavailable",
                        reason="screen_changed_outside_cell",
                        frame_sha256=copied.frame_sha256,
                    )
                    return first.model_copy(
                        update={
                            "look_halt": "identity_look_changed_screen",
                            "looks": [look],
                        }
                    )
                crops = tuple(
                    c
                    for c in (
                        looks_mod.crop_png(frame_png, identifier_region),
                        copied.pre_crop,
                        copied.post_crop,
                    )
                    if c is not None
                )
                bindings[step.id] = looks_mod.LookBinding(
                    step_id=step.id, classes=classes, read=copied, crops=crops
                )
            else:
                binding = bindings.get(step.id)
                live_crop = looks_mod.crop_png(frame_png, identifier_region)
                if binding is not None and binding.matches(step.id, classes, live_crop):
                    copied = binding.read

        known_list = None
        if known is not None:
            known_list = getattr(known, "known_ids", known)
        decided = looks_mod.decide(
            first,
            frames=frames,
            magnified=magnified,
            contradicts=identity_read_contradicts,
            known_ids=known_list,
            known_ids_lift=known_lift,
            clipboard=copied,
            exact_expected=looks_mod.exact_expected_ids(
                run_values,
                self._embedded_identity_params(step.anchor, workflow)
                if step.anchor is not None
                else [],
            ),
        )
        binding = bindings.get(step.id)
        if (
            self._identity_input_looks
            and binding is not None
            and decided.status == "verified"
            and any(
                look.kind == "clipboard" and look.outcome == "resolves"
                for look in decided.looks
            )
        ):
            bindings[step.id] = dataclasses.replace(binding, verdict=decided)
        return decided

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
        after_png = self._settle_seeded()
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
        # Dropdown: typing selects the option whose label matches. When the
        # focused <select> at the field point now shows exactly the typed
        # label (whitespace-squashed, case-insensitive), the input landed.
        # Any other answer falls through to the visual check, unchanged.
        if field_point is not None and not allow_masked:
            reader = getattr(self.backend, "focused_select_label_at", None)
            label: Optional[str] = None
            if callable(reader):
                try:
                    label = reader(int(field_point[0]), int(field_point[1]))
                except Exception:
                    label = None
            if (
                isinstance(label, str)
                and identity_mod.squash(text)
                and identity_mod.squash(label).casefold()
                == identity_mod.squash(text).casefold()
            ):
                return True, True
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

    @timed_span("postcondition")
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

    @timed_span("postcondition")
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

        from openadapt_flow.vision import imgops

        frame = imgops.decode_bgr(frame_png)
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

    @timed_span("resolve")
    def _implicit_scroll_target_ready(
        self,
        step: Step,
        frame_png: bytes,
        *,
        workflow: Workflow,
        bundle_dir: Path,
        params: Mapping[str, RuntimeParamScalar],
        scroll_delta: Optional[tuple[int, int]] = None,
        misses: Optional[list[str]] = None,
    ) -> bool:
        """Resolve an implicit scroll target and honor its armed identity.

        With ``scroll_delta``, the target must also be no further along the
        scroll direction than where the demonstration had it (within
        :data:`SCROLL_POSITION_SLACK_FRACTION` of the frame). A target that
        is visible at the bottom edge but was clicked mid-screen in the
        recording still needs the scroll: the step after the click was
        recorded with that scroll position (for example, a section that
        expands below the clicked header).

        A generic input crop can visually resolve in several places on a long
        form. Declaring the target visible from that visual match alone makes
        the preceding scroll a false no-op. Structural resolution stays
        available, model grounding stays disabled, and an identity-armed target
        is ready only when the same pre-action identity gate verifies it.

        When not ready, ``misses`` (if given) gets why: ``unresolved`` (no
        direct deterministic rung found the target), ``offscreen`` (the page
        reports the recorded element outside the viewport), ``position`` or
        ``identity``.
        """

        def miss(reason: str) -> bool:
            if misses is not None:
                misses.append(reason)
            return False

        if step.anchor is None:
            return miss("unresolved")
        structural = (
            self.backend
            if self.use_structural and hasattr(self.backend, "locate_structural")
            else None
        )
        offscreen = getattr(structural, "structural_offscreen", None)
        if (
            callable(offscreen)
            and step.anchor.structural is not None
            and offscreen(step.anchor.structural) is True
        ):
            # The page itself shows the unique recorded target exists below
            # (or above) the viewport: not ready, keep scrolling. Stricter
            # than letting a visual rung find a look-alike on screen, and it
            # skips a full-frame OCR search per probe. The control exists, so
            # this miss never hands the halt to the click (#1331).
            return miss("offscreen")
        template_png = self._asset_bytes(
            bundle_dir, step.anchor.template, workflow=workflow
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
            return miss("unresolved")
        if resolved is None:
            return miss("unresolved")
        resolution, _matched_region = resolved
        # Geometry estimates a point from surrounding landmarks; it does not
        # prove that the target itself is visible. In particular, a fixed
        # header can project the recorded point while the form row is still
        # off-screen, turning the preceding scroll into a false no-op. Scroll
        # readiness therefore requires direct deterministic target evidence.
        # Model grounding is disabled above and future weaker rungs must opt in
        # deliberately rather than silently acquiring readiness authority.
        if resolution.rung not in _DIRECT_TARGET_RUNGS:
            return miss("unresolved")
        if scroll_delta is not None and not _scroll_target_positioned(
            step.anchor.click_point, resolution.point, scroll_delta, frame_png
        ):
            return miss("position")
        if not step.identity_armed:
            return True
        check = self._verify_identity(
            step, resolution, frame_png, params, workflow, bundle_dir
        )
        if check.status != "verified" and check.ambiguity_halt is not None:
            # The target is in view and its identity matched, but the census
            # refused it. Scrolling cannot change that; the scroll loop stops
            # and reports this instead of spending its budget.
            self._scroll_census_refusal = (step.id, check)
            return miss("census")
        return check.status == "verified" or miss("identity")

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

        # Until a wheel edge stops moving the page, readiness also requires
        # the target at (or past) its demonstrated position.
        position_required = [True]
        # Why each readiness probe of the next target failed; they decide who
        # owns a budget-exhaustion halt (see below).
        misses: list[str] = []

        def readiness_holds(frame_png: bytes) -> bool:
            if implicit_target_readiness:
                assert next_step is not None
                return self._implicit_scroll_target_ready(
                    next_step,
                    frame_png,
                    workflow=workflow,
                    bundle_dir=bundle_dir,
                    params=params,
                    scroll_delta=(dx, dy) if position_required[0] else None,
                    misses=misses,
                )
            return self._predicate_holds(
                stop_pred,
                frame_png,
                bundle_dir,
                params,
                workflow=workflow,
            )

        def census_refusal() -> Optional[str]:
            """The halt for a target in view whose census refused it."""

            refusal = self.__dict__.pop("_scroll_census_refusal", None)
            if not misses or misses[-1] != "census" or refusal is None:
                return None
            target_id, check = refusal
            why = {
                "identical_candidates": (
                    f"{check.candidate_count} shown rows carry the same identity "
                    "and no recorded record id tells them apart"
                ),
                "identity_uncounted": (
                    "this surface cannot count the rows that might share it, "
                    "and a consequential write follows"
                ),
            }.get(
                check.ambiguity_halt or "",
                "the rows that could share its identity could not all be counted",
            )
            result.safety_halt = True
            return (
                f"Step '{step.id}' ({step.intent}): the target of step "
                f"'{target_id}' is in view and its identity matched, but {why} "
                f"({check.ambiguity_halt}); scrolling cannot change that, so "
                "the scroll stops here and no input was sent for it"
            )

        self.__dict__.pop("_scroll_census_refusal", None)
        holds = readiness_holds(before_png)
        if self._governed_asset_mutation is not None:
            return self._governed_asset_mutation
        stop = census_refusal()
        if stop is not None:
            return stop
        if holds:
            # Target already in view; nothing to scroll and no input sent.
            # Record why, only for the next-target readiness check, so the
            # outcome classifier can tell a correctly skipped scroll from an
            # undelivered action (execution_profiles.classify_execution_outcome).
            if implicit_target_readiness and result.delivery_attempted is not True:
                result.scroll_target_ready_before = True
            return None

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
                stop = census_refusal()
                if stop is not None:
                    return stop
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
            if not self.vision.pixels_changed(before_png, frame):
                # The page did not move (its end): visibility is enough.
                position_required[0] = False
            before_png = frame
            holds = readiness_holds(frame)
            if self._governed_asset_mutation is not None:
                return self._governed_asset_mutation
            stop = census_refusal()
            if stop is not None:
                return stop
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
        if (
            graph_ctx is None
            and implicit_target_readiness
            and next_step is not None
            and following is next_step
            and next_step.action in _SCROLL_HANDOFF_ACTIONS
            and misses
            and all(miss == "unresolved" for miss in misses)
            and not step.risk_review_required
            and not self._step_is_consequential(step, workflow)
        ):
            # The scroll did its bounded work; what failed is the next click's
            # target, which never resolved on any frame (a relabelled or moved
            # control). Halting here would name the scroll and hide the
            # control that changed. Hand the miss to that click instead: it
            # resolves on direct deterministic rungs only and, when they find
            # nothing, halts on its own target before any input. A target
            # that resolved but sat out of position or failed its identity
            # check still halts here, as before.
            self._scroll_missed_target = (
                next_step.id,
                f"the preceding scroll '{step.id}' used {scrolled:.0f}px of its "
                f"{budget:.0f}px budget and the target never came into view; "
                "no deterministic rung found it, so no input was sent",
            )
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

    @timed_span("settle")
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

    @timed_span("postcondition")
    def _check_postconditions(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: dict[str, Any],
        result: Optional[StepResult] = None,
        *,
        workflow: Optional[Workflow] = None,
    ) -> tuple[bool, bytes, list[str]]:
        """Poll postconditions until each passes or times out.

        Each postcondition is polled (fresh screenshots) up to its own
        ``timeout_s``. If any fails, the screen is re-settled once and all
        postconditions are re-checked a single time. If a VLM state-verifier
        is configured, a render-drift-sensitive postcondition that still failed
        gets one last drift-oracle pass (see :meth:`_drift_oracle_rescue`),
        but never on a step that can leave a business effect behind (see
        :meth:`_postcondition_confirms_a_write`). ``workflow`` lets a qualified
        operator risk decision count; without it the classification is the
        stricter step-only one.

        Returns:
            (ok, last_frame, failed) — the frame the final verdict was based
            on, plus human-readable descriptions of the postconditions that
            failed the final check (empty when ok).
        """
        self._pc_page_scale = _uniform_page_scale(step, result)
        self._pc_scaled_match = False
        try:
            ok, frame_png = self._poll_postconditions(
                step, frame_png, bundle_dir, start_state
            )
            if ok:
                self._note_page_scale(result)
                return True, frame_png, []
            # One re-settle retry.
            frame_png = self.vision.wait_settled(self.backend)
            failed_pcs = [
                pc
                for pc in step.expect
                if not self._postcondition_passes(
                    pc, frame_png, bundle_dir, start_state
                )
            ]
            if not failed_pcs:
                self._note_page_scale(result)
        finally:
            self._pc_page_scale = None
        if (
            failed_pcs
            and self.state_verifier is not None
            and not self._postcondition_confirms_a_write(step, workflow)
        ):
            failed_pcs = self._drift_oracle_rescue(failed_pcs, frame_png, result)
        failed = [self._describe_postcondition(pc) for pc in failed_pcs]
        return not failed_pcs, frame_png, failed

    def _note_page_scale(self, result: Optional[StepResult]) -> None:
        """Record on ``result`` the page scale a passing check needed."""

        scale = getattr(self, "_pc_page_scale", None)
        if result is not None and scale is not None and self._pc_scaled_match:
            result.postcondition_page_scale = scale

    @staticmethod
    def _postcondition_confirms_a_write(
        step: Step, workflow: Optional[Workflow]
    ) -> bool:
        """Whether ``step``'s postcondition is the screen's check on a write.

        True for any step that can leave a business effect behind: the shared
        fail-closed classifier :func:`run_gate.is_consequential` (the compiled
        ``risk`` label, the write-shaped heuristic, and any declared
        system-of-record effect), plus an unresolved risk review, which the
        transaction outcome also treats as consequential.

        On such a step the drift oracle is never asked, so a failed
        postcondition stays failed. A model "yes" is not evidence that a write
        did what was intended. The step fails exactly as it would with no
        verifier configured: the run halts unless the program declares its
        own exception handler for that state. The action was already
        delivered and nothing has proven the write absent, so
        :func:`transaction.classify_transaction_outcome` reports
        ``RECONCILIATION_REQUIRED`` for the halted run.

        Read-only and navigation steps keep the drift-oracle rescue. A wrong
        "yes" there lets the run reach the next step, which still runs its own
        identity, risk, and postcondition checks before it acts.
        """
        if step.risk_review_required:
            return True
        return is_consequential(step, workflow)

    def _drift_oracle_rescue(
        self,
        failed_pcs: list[Any],
        frame_png: bytes,
        result: Optional[StepResult],
    ) -> list[Any]:
        """Give each deterministically-failed, render-drift-sensitive
        postcondition one confirmation pass through the VLM state-verifier.

        VETO-SAFE by construction:

        * The caller never invokes this on an irreversible or effect-bearing
          step (see :meth:`_postcondition_confirms_a_write`).
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

    def _local_edit_held(self, pc: Any, start_state: dict[str, Any]) -> bool:
        """A compiler-classified local edit stayed local (fail closed).

        URL, title and page count must be observable and unchanged from the
        step's start, and focus must still be in ``pc.field_selector`` when
        one is set. Anything unobservable fails the check: the classification
        rests on this evidence (docs/design/LOCAL_EDIT_CLASSIFICATION.md).
        """

        for key in ("url", "page_title", "page_count"):
            if self._structural_changed(key, start_state) is not False:
                return False
        if pc.field_selector:
            reader = getattr(self.backend, "focused_element_matches", None)
            if not callable(reader):
                return False
            try:
                return reader(pc.field_selector) is True
            except Exception:  # noqa: BLE001 - unobservable is a failure
                return False
        return True

    @staticmethod
    def _malformed_date_params(workflow: Workflow, params: dict[str, Any]) -> list[str]:
        """Why each supplied ``date`` parameter is not a canonical ISO date.

        The same check hosted dispatch applies (``runner.inputs``). An empty
        optional value is left to the required-parameter check.
        """

        from openadapt_flow.runner.inputs import AdmittedInputError, _validate_value

        problems: list[str] = []
        for name, spec in sorted(workflow.param_specs.items()):
            if spec.type is not ParamKind.DATE or name not in params:
                continue
            value = params[name]
            if value == "" and not spec.required:
                continue
            try:
                _validate_value(spec, value)
            except AdmittedInputError as exc:
                problems.append(str(exc))
        return problems

    def _no_duplicate_candidate(self, pc: Any, frame_png: bytes) -> bool:
        """The app's possible-duplicate list has loaded and names no candidate.

        A row is a candidate when its words include every word of this run's
        value of any ``pc.candidate_params`` parameter (case-insensitive). A
        value that is an ISO date is also compared as a date: every date the
        row shows, in any format intake reads (``Apr 20, 1944``,
        ``20.04.1944``, ``04/20/1944``), is normalized and compared, and a
        row date that reads either way counts when either reading matches.
        Fails closed: an unreadable list, a missing or empty parameter, or an
        empty list without its "no matches" marker all fail the check.
        """

        params = (
            getattr(self, "_current_params", None)
            or getattr(self, "_run_params", None)
            or {}
        )
        names = tuple(pc.candidate_params or ())
        if not pc.candidate_rows or not names or not pc.text:
            return False
        reader = getattr(self.backend, "element_texts", None)
        if not callable(reader):
            return False
        rows = reader(pc.candidate_rows, tuple(pc.candidate_frame_path or ()))
        if rows is None:
            return False

        def words(value: str) -> list[str]:
            return re.findall(r"[0-9a-z]+", value.casefold())

        wanted = []
        wanted_dates: set[str] = set()
        for name in names:
            text = runtime_param_text(params[name]) if name in params else ""
            value = words(text)
            if not value:
                return False
            wanted.append(value)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()):
                wanted_dates.add(text.strip())
        if not rows:
            # Nothing listed: only the list's own "no matches" marker shows
            # the search finished.
            if self._dom_text_visible(pc.text):
                return True
            return self.vision.text_present(frame_png, pc.text)
        for row in rows:
            row_words = set(words(row))
            if any(all(w in row_words for w in value) for value in wanted):
                return False
            if wanted_dates and wanted_dates & _row_dates(row):
                return False
        return True

    def _field_value_shown(self, pc: Any) -> bool:
        """The focused field shows this run's value of ``pc.param``.

        A dropdown's selected label matches up to spacing and case; a text
        field's value must match exactly. Unobservable is a failure.
        """

        params = getattr(self, "_current_params", None) or {}
        if not pc.param or pc.param not in params:
            return False
        expected = runtime_param_text(params[pc.param])

        def read(name: str) -> Optional[str]:
            reader = getattr(self.backend, name, None)
            if not callable(reader):
                return None
            try:
                value = reader()
            except Exception:  # noqa: BLE001 - unobservable is a failure
                return None
            return value if isinstance(value, str) else None

        def squash(value: str) -> str:
            return " ".join(value.split()).casefold()

        label = read("focused_select_label")
        if label is not None:
            return squash(label) == squash(expected)
        value = read("focused_text_value")
        return value is not None and value == expected

    @staticmethod
    def _describe_postcondition(pc: Any) -> str:
        """Human-readable one-liner for a postcondition (for error messages)."""
        kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
        if kind == "local_edit":
            return "local_edit (URL, title, page count and focus unchanged)"
        if kind == "field_value":
            return f"field_value (the focused field shows {{{pc.param}}})"
        if kind == "no_duplicate_candidate":
            names = ", ".join(pc.candidate_params or ())
            return f"no_duplicate_candidate (no listed row has this run's {names})"
        if kind in ("text_present", "text_absent"):
            return f"{kind} {pc.text!r}"
        if kind in ("url_changed", "title_changed", "new_tab_opened"):
            return f"{kind} (vs. the step's start state)"
        return f"{kind} region={tuple(pc.region) if pc.region else None}"

    @timed_span("postcondition")
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
            kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
            while True:
                if self._postcondition_passes(pc, frame_png, bundle_dir, start_state):
                    break
                if time.monotonic() >= deadline:
                    return False, frame_png
                wait_for_page = getattr(self.backend, "wait_for_page_count_above", None)
                start_pages = (start_state or {}).get("page_count")
                if (
                    kind == "new_tab_opened"
                    and callable(wait_for_page)
                    and isinstance(start_pages, int)
                ):
                    # Wait on the browser's page event, not a poll.
                    wait_for_page(start_pages, deadline - time.monotonic())
                else:
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
        if kind == "local_edit":
            return self._local_edit_held(pc, start_state or {})
        if kind == "field_value":
            return self._field_value_shown(pc)
        if kind == "no_duplicate_candidate":
            return self._no_duplicate_candidate(pc, frame_png)
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
            if pc.text is None:
                return False
            # Page-structure first: the DOM can PROVE the text is rendered,
            # visible and uncovered (exact normalized substring, stricter
            # than the fuzzy OCR match). It can never prove absence, so a DOM
            # miss falls through to the OCR check, unchanged.
            if self._dom_text_visible(pc.text):
                return True
            return self.vision.text_present(frame_png, pc.text)
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
                if self._scaled_region_match(frame_png, template_png, region):
                    return True
            live = self.vision.phash_png(frame_png, region=region)
            distance = self.vision.phash_distance(live, pc.phash)
            return distance <= pc.phash_tolerance
        return False

    def _scaled_region_match(
        self, frame_png: bytes, template_png: bytes, region: tuple[int, ...]
    ) -> bool:
        """The recorded content, at the page scale this step's click showed.

        A whole-page zoom (browser zoom, or an app's own scale) grows every
        element and moves it away from the top-left corner by the same
        factor, so the recorded region falls outside the search window and
        its content outside the matcher's scale band. When this step's own
        click landed at the recorded point times one uniform factor
        (:func:`_uniform_page_scale`), look for the same template at that
        factor (within one percent), at the recorded region times that
        factor. Thresholds are the unscaled check's: a modal, a missing panel or another layout still
        fails.
        """

        scale = getattr(self, "_pc_page_scale", None)
        if scale is None:
            return False
        x, y, w, h = region
        scaled = (
            int(round(x * scale)),
            int(round(y * scale)),
            int(round(w * scale)),
            int(round(h * scale)),
        )
        search = pad_region(scaled, PC_TEMPLATE_SEARCH_PAD, _frame_viewport(frame_png))
        matchers = [
            (self.vision.find_template, PC_TEMPLATE_THRESHOLD),
            (
                getattr(self.vision, "find_structural_template", None),
                PC_STRUCTURAL_TEMPLATE_THRESHOLD,
            ),
        ]
        for matcher, threshold in matchers:
            if not callable(matcher):
                continue
            try:
                match = matcher(
                    frame_png,
                    template_png,
                    search_region=search,
                    scales=tuple(round(scale * f, 4) for f in _PAGE_SCALE_BAND),
                    threshold=threshold,
                )
            except TypeError:  # a vision stand-in with no scale band
                return False
            if match is not None:
                self._pc_scaled_match = True
                return True
        return False

    def _dom_text_visible(self, text: str) -> bool:
        """True only when the backend proves ``text`` is visibly rendered.

        Optional backend seam ``visible_text_present`` (browser DOM). Any
        other answer, a missing seam, or an error is "not proven" -> False,
        so the caller keeps its visual check.
        """
        reader = getattr(self.backend, "visible_text_present", None)
        if not callable(reader):
            return False
        try:
            return reader(text) is True
        except Exception:
            return False

    def _postcondition_template(self, pc: Any, bundle_dir: Path) -> Optional[bytes]:
        """Bytes of a REGION_STABLE postcondition's template crop, if any."""
        rel = getattr(pc, "template", None)
        if not rel:
            return None
        return self._asset_bytes(bundle_dir, rel)

    def _layout_drift_on(self, workflow: Workflow) -> bool:
        if self.layout_drift is not None:
            return self.layout_drift
        kind: Optional[str]
        if self._surface_router is not None:
            # Multi-surface run: follow the surface the step acts on, so an
            # RDP segment of a web-first bundle keeps the check.
            kind, _ = self._active_surface_binding(workflow)
        else:
            kind = self._layout_target_kind or workflow.surface
        if kind in ("rdp", "citrix"):
            return True
        backend = self._render_scale_inner or self.backend
        return type(backend).__name__ == "FreeRDPBackend"

    def _live_session_profile(self) -> Optional[str]:
        """Digest of the pinned remote-session profile, when there is one."""
        transport = getattr(self.backend, "transport", None)
        profile = getattr(getattr(transport, "profile", None), "sha256", None)
        return profile if isinstance(profile, str) else None

    def _session_profile_changed(self, recorded: Any) -> bool:
        live = self._live_session_profile()
        return bool(
            recorded.profile_sha256 and live and recorded.profile_sha256 != live
        )

    def _reset_render_scale(self) -> None:
        """Return to the caller's backend and forget the last estimate."""
        if self._render_scale_inner is not None:
            release = getattr(self.backend, "release_frame_sources", None)
            if callable(release):
                release()
            self.backend = self._render_scale_inner
        self._render_scale_inner = None
        self._render_scale_attempted = False
        self._render_scale_evidence = None

    def _load_layout_fingerprints(
        self, bundle_dir: Path, workflow: Workflow
    ) -> dict[str, Any]:
        from openadapt_flow.runtime import layout_drift as drift_mod

        if self._layout_fingerprints is None:
            self._layout_fingerprints = drift_mod.load_bundle_fingerprints(
                self._asset_bytes(
                    Path(bundle_dir), drift_mod.FINGERPRINT_ASSET, workflow=workflow
                )
            )
        return self._layout_fingerprints

    def _maybe_scale_render(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Optional[Path],
        workflow: Workflow,
    ) -> bytes:
        """Estimate the session's display scale once; return the frame to use.

        Runs at the first step that has a recorded fingerprint. A frame that
        matches the fingerprint, or gives no confident estimate, is returned
        unchanged and the layout-drift check judges it as before. Otherwise
        the backend is replaced by a
        :class:`~openadapt_flow.runtime.render_scale.RenderScaledBackend` and
        the frame is returned in the recorded geometry, where the same
        layout-drift check must then pass.
        """
        if self._render_scale_attempted or bundle_dir is None:
            return frame_png
        enabled = (
            self.render_scale
            if self.render_scale is not None
            else self._layout_drift_on(workflow)
        )
        if not enabled or self._surface_router is not None:
            return frame_png
        from openadapt_flow.runtime import layout_drift as drift_mod
        from openadapt_flow.runtime import render_scale as scale_mod

        recorded = self._load_layout_fingerprints(Path(bundle_dir), workflow).get(
            step.id
        )
        if recorded is None:
            return frame_png
        self._render_scale_attempted = True
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(frame_png)) as img:
                viewport = (int(img.width), int(img.height))
        except Exception:  # noqa: BLE001 - undecodable: leave it to the drift check
            return frame_png
        if self._session_profile_changed(recorded):
            # A pinned remote-session profile (scale, DPI, font smoothing) is
            # a deployment decision; a different profile still halts below.
            self._render_scale_evidence = {
                "status": "no_estimate",
                "reason": "the pinned remote session profile changed",
            }
            return frame_png
        lines = self.vision.ocr(frame_png)
        live = drift_mod.fingerprint(lines, viewport)
        verdict = drift_mod.compare(
            recorded,
            live,
            self.layout_drift_tolerances or drift_mod.Tolerances(),
        )
        if not verdict.drifted:
            self._render_scale_evidence = {"status": "recorded_geometry"}
            return frame_png
        salt, hashed = drift_mod.load_scale_landmarks(
            self._asset_bytes(
                Path(bundle_dir), drift_mod.FINGERPRINT_ASSET, workflow=workflow
            )
        )
        landmarks = scale_mod.hashed_landmarks(
            hashed.get(step.id, [])
        ) + scale_mod.anchor_landmarks(workflow.steps)
        pairs = scale_mod.match_landmarks(landmarks, lines, salt_hex=salt)
        transform, reason = scale_mod.estimate_render_transform(
            pairs,
            recorded_viewport=recorded.viewport,
            live_viewport=viewport,
        )
        if transform is None:
            self._render_scale_evidence = {
                "status": "no_estimate",
                "reason": reason,
                "drift": list(verdict.reasons),
            }
            return frame_png
        scaled = scale_mod.render_scaled_backend(self.backend, transform)
        self._render_scale_inner = self.backend
        self.backend = cast(Backend, scaled)
        self._render_scale_evidence = {"status": "normalized", **transform.evidence()}
        # The layout-drift check below now judges the normalized frame, so a
        # font or layout change that is not a pure rescale still stops here.
        return scaled.normalize(frame_png)

    def _layout_drift_error(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Optional[Path],
        workflow: Workflow,
    ) -> Optional[str]:
        """Halt message when the frame drifted from the recorded fingerprint.

        Steps without a recorded fingerprint (older bundles, non-click steps)
        are not checked. Model-free: the advisor, when configured, is called
        only after drift is found and never changes the verdict.
        """
        if bundle_dir is None or not self._layout_drift_on(workflow):
            return None
        from openadapt_flow.runtime import layout_drift as drift_mod

        recorded = self._load_layout_fingerprints(Path(bundle_dir), workflow).get(
            step.id
        )
        if recorded is None:
            return None
        live_profile = self._live_session_profile()
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(frame_png)) as img:
                viewport = (int(img.width), int(img.height))
        except Exception:  # noqa: BLE001 - an undecodable frame is drift
            viewport = (0, 0)
        lines = self.vision.ocr(frame_png)
        tolerances = self.layout_drift_tolerances or drift_mod.Tolerances()
        if self._render_scale_inner is not None and not self._session_profile_changed(
            recorded
        ):
            # A normalized frame is judged label by label: the recorded label
            # set says where each label sat and how large it was, so OCR box
            # jitter on other lines cannot pass for a font change.
            from openadapt_flow.runtime import render_scale as scale_mod

            salt, hashed = drift_mod.load_scale_landmarks(
                self._asset_bytes(
                    Path(bundle_dir), drift_mod.FINGERPRINT_ASSET, workflow=workflow
                )
            )
            details = (
                scale_mod.check_normalized_frame(
                    hashed.get(step.id, []),
                    lines,
                    salt_hex=salt,
                    text_height_ratio=tolerances.text_height_ratio,
                    landmark_px=tolerances.landmark_px,
                )
                if salt is not None
                else None
            )
            if details:
                return (
                    f"Layout drift before step '{step.id}' ({step.intent}) on "
                    f"the display-scaled session: {'; '.join(details)} — the "
                    "screen no longer matches the recording; refusing to act."
                )
            if details is not None:
                return None
        live = drift_mod.fingerprint(
            lines,
            viewport,
            landmarks=list(recorded.landmarks),
            profile_sha256=(live_profile if isinstance(live_profile, str) else None),
        )
        try:
            drift_mod.check_layout(
                recorded,
                live,
                tolerances=tolerances,
                advisor=self.layout_drift_advisor,
            )
        except drift_mod.LayoutDriftHalt as halt:
            advice = f" Advice: {halt.verdict.advice}" if halt.verdict.advice else ""
            return (
                f"Layout drift before step '{step.id}' ({step.intent}): "
                f"{drift_mod.describe(halt.verdict)} — the screen no longer "
                f"matches the recording; refusing to act.{advice}"
            )
        return None

    def _asset_bytes(
        self,
        bundle_dir: Path,
        rel: str,
        *,
        workflow: Optional[Workflow] = None,
    ) -> Optional[bytes]:
        """Return the verified run snapshot for governed asset consumers."""
        if not rel:
            # No crop: a record-selecting click rebound to another record
            # (runtime.target_param) carries none.
            return None
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
            _sealed.exists(steps_dir / f"{candidate}_{suffix}.png")
            for suffix in ("before", "after", "attended-before", "attended-after")
        ):
            occurrence += 1
            candidate = f"{safe}__{occurrence:04d}"
        return candidate

    @staticmethod
    @timed_span("artifact_write")
    def _save_step_png(run_dir: Path, step_id: str, suffix: str, png: bytes) -> str:
        """Save a per-step screenshot; return its run-dir-relative path.

        These frames are embedded by relative path into the shareable
        ``REPORT.md``. When opt-in image redaction is enabled
        (``OPENADAPT_FLOW_SCRUB_IMAGES=1``), PII/PHI regions are burned out
        before the frame is written to disk; otherwise the raw frame is saved
        (see openadapt_flow.privacy and docs/PRIVACY.md).
        """
        rel = f"steps/{step_id}_{suffix}.png"
        _sealed.write_bytes(Path(run_dir) / rel, _scrub_png(png))
        return rel


#: Date-looking text in a listed row: ISO or Y/M/D, numeric with / . or -,
#: and a spelled-out month either side of the day.
_ROW_DATE_PATTERNS = (
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b",
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}\b",
    r"\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b",
)


def _row_dates(row: str) -> set[str]:
    """Every ISO date a listed row could be showing.

    Each date-looking substring is read the way intake reads a date
    (``intake.extract.normalize_date``). One that reads either way (03/04/1970)
    contributes both readings; one that isn't a date contributes nothing.
    """

    from datetime import date as _date

    from openadapt_flow.intake.extract import normalize_date

    found: set[str] = set()
    for pattern in _ROW_DATE_PATTERNS:
        for match in re.finditer(pattern, row):
            text = match.group(0)
            ymd = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
            if ymd:
                try:
                    found.add(
                        _date(
                            int(ymd.group(1)), int(ymd.group(2)), int(ymd.group(3))
                        ).isoformat()
                    )
                except ValueError:
                    pass
                continue
            text = re.sub(r"(\d)\.(?=\d)", r"\1/", text).replace(".", "")
            iso, error, _ = normalize_date(text)
            if iso is not None:
                found.add(iso)
            elif error and "ambiguous" in error:
                for order in ("MM/DD/YYYY", "DD/MM/YYYY"):
                    reading = normalize_date(text, order)[0]
                    if reading is not None:
                        found.add(reading)
    return found
