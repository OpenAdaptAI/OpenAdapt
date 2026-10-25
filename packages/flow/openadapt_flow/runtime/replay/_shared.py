"""Constants, helpers and small types the replayer's concerns share.

Moved verbatim from ``openadapt_flow.runtime.replayer`` by
scripts/extract_mixin.py; the source module re-exports every name.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any, Mapping, Optional, TypeVar

from openadapt_flow.ir import (
    ActionKind,
    LoopIterationBinding,
    RunReport,
    StateKind,
    Step,
    Workflow,
)
from openadapt_flow.network_scope import EndpointScope, endpoint_scope
from openadapt_flow.runtime.authorization import RuntimeParamScalar
from openadapt_flow.runtime.durable.program_checkpoint import (
    GraphFrame,
    ProgramCheckpoint,
)

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
