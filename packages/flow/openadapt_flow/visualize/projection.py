"""Audience-bound projections of the compiled program graph.

The operator-local graph is the complete diagnostic view. Every other surface
gets a CLOSED ALLOW-LIST projection: each model that crosses the boundary
declares the exact fields permitted to leave, and the projection REBUILDS the
model from only those fields. A field that is not enumerated never leaves,
including one added to the spec after this module was written.

The allow-list is enforced, not documented. :data:`FIELD_BOUNDARY` partitions
every declared field of every crossing model into ``public`` or ``local``, and
:func:`assert_field_boundary_is_closed` -- run at import -- raises
:class:`ProjectionBoundaryError` if any declared field falls in neither. Adding
a field to ``spec.py`` therefore fails loudly at import until an author
classifies it, rather than silently shipping it to a public surface.

Values are closed too, not merely fields: a field whose vocabulary is finite
(action, risk, outcome, rung name, effect kind, postcondition kind, badge) is
checked against the closed set here and dropped when it does not match, so
widening a vocabulary upstream cannot widen this boundary by itself.

Projection does not sanitize the source bundle and never changes its
governance flags.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Final, Optional

from pydantic import BaseModel

from openadapt_flow.visualize.spec import (
    PROJECTED_BUNDLE_NAME,
    BundleMeta,
    EffectInfo,
    GraphEdge,
    GraphNode,
    IdentityInfo,
    NodeKind,
    ParamInfo,
    ProgramGraphSpec,
    ProvenanceInfo,
    ResolutionInfo,
    ResolutionRung,
)


class PresentationProfile(str, Enum):
    """Data boundary for one rendered program graph."""

    OPERATOR_LOCAL = "operator-local"
    REMOTE_SAFE = "remote-safe"
    PUBLIC_SYNTHETIC = "public-synthetic"
    SANITIZED_DERIVATIVE = "sanitized-derivative"


class ProjectionBoundaryError(RuntimeError):
    """A crossing model declares a field this module has not classified.

    Raised at import time. The fix is to add the new field to the model's
    ``public`` set (if it can never carry recorded application data or operator
    free text) or to its ``local`` set (if it can).
    """


# --------------------------------------------------------------------------
# Closed value vocabularies.
#
# Each mirrors a finite upstream enum. They are restated here rather than
# imported so that widening the upstream enum does not silently widen this
# boundary, and so this module keeps its narrow import surface.
# --------------------------------------------------------------------------

_ACTIONS: Final = frozenset(
    {
        "click",
        "double_click",
        "drag",
        "hotkey",
        "key",
        "right_click",
        "scroll",
        "select_option",
        "type",
        "wait",
    }
)
_RISKS: Final = frozenset({"reversible", "irreversible"})
_OUTCOMES: Final = frozenset({"success", "halt", "escalate"})
_GUARD_ON_UNMET: Final = frozenset({"halt", "skip"})
_POSTCONDITION_KINDS: Final = frozenset(
    {
        "text_present",
        "text_absent",
        "region_stable",
        "url_changed",
        "title_changed",
        "new_tab_opened",
    }
)
_EFFECT_KINDS: Final = frozenset({"record_written", "field_equals", "exact_new_set"})

#: Rung id -> its fixed public label. The label is DERIVED from the closed id,
#: never carried across from the source, so a free-text label upstream cannot
#: ride along. ``tests/test_visualize.py`` pins this against
#: ``builder._RUNG_LABELS`` so the two cannot drift.
_RUNG_LABELS: Final[dict[str, str]] = {
    "api": "API / tool call",
    "structural": "DOM / accessibility selector",
    "template": "Image template match",
    "ocr": "OCR text match",
    "landmarks": "Nearby-landmark geometry",
}

#: Literal badges the builder emits. Anything else is dropped.
_BADGES: Final = frozenset(
    {
        "irreversible",
        "risk review",
        "identity gate",
        "no identity gate",
        "effect check",
        "API",
        "secret",
        "optional (skippable)",
        "loop",
        "human decision",
        "halt",
        "escalate",
        "child bundle",
        "web",
        "windows",
        "macos",
        "linux",
        "rdp",
        "citrix",
    }
)
#: The builder also emits two count-templated badges. Only a bounded integer
#: plus a fixed noun is admitted; the count is structure, not recorded data.
_COUNTED_BADGE_RE: Final = re.compile(r"^\d{1,4} (?:finite answers|authorized roles)$")

#: Public edge labels, keyed by the closed edge kind.
_EDGE_LABELS: Final[dict[str, str]] = {
    "sequence": "",
    "branch": "declared branch",
    "exception": "declared exception",
    "loop_body": "declared loop",
    "handoff": "declared handoff",
}

#: Closed execution-surface vocabulary (mirrors ir.ExecutionTargetKind).
_SURFACES: Final = frozenset({"web", "windows", "macos", "linux", "rdp", "citrix"})
_COMPOSITION_SCHEMAS: Final = frozenset({"openadapt.composition/v1"})


# --------------------------------------------------------------------------
# The field boundary: every declared field of every crossing model, classified.
# --------------------------------------------------------------------------

_NODE_PUBLIC: Final = frozenset(
    {
        "id",  # synthetic node identity; topology is retained by contract
        "index",
        "kind",  # NodeKind enum
        "title",  # REPLACED by _safe_title (closed phrase set)
        "action",  # closed vocabulary
        "risk",  # closed vocabulary
        "risk_review_required",  # bool
        "secret",  # bool
        "resolution",  # rebuilt field-by-field below
        "identity",  # rebuilt field-by-field below
        "effects",  # rebuilt field-by-field below
        "has_api_binding",  # bool
        "postconditions",  # closed vocabulary
        "guard_on_unmet",  # closed vocabulary
        "outcome",  # closed vocabulary
        "halts",  # REPLACED by _safe_halts (positional, no content)
        "badges",  # closed literal set + bounded count template
        "surface",  # closed execution-surface vocabulary
    }
)
_NODE_LOCAL: Final = frozenset(
    {
        "risk_explanation",  # operator free text (ir.py: up to 512 chars)
        "param",  # recorded parameter name
        "key",  # recorded key
        "api_summary",  # method + URL template
        "guard",  # free-text predicate summary
        "wait_until",  # free-text predicate summary
        "reason",  # free-text terminal reason
    }
)

_RUNG_PUBLIC: Final = frozenset({"name", "label", "present"})
_RUNG_LOCAL: Final = frozenset({"detail"})  # selector / template path / OCR text

_RESOLUTION_PUBLIC: Final = frozenset({"rungs", "top_rung"})
_RESOLUTION_LOCAL: Final[frozenset[str]] = frozenset()

_IDENTITY_PUBLIC: Final = frozenset(
    {"applicable", "armed", "phi_free", "has_structured", "has_identifier_crop"}
)
_IDENTITY_LOCAL: Final = frozenset({"reason"})  # why the step compiled unarmed

_EFFECT_PUBLIC: Final = frozenset(
    {"kind", "summary", "risk", "needs_operator_confirmation"}
)
_EFFECT_LOCAL: Final[frozenset[str]] = frozenset()  # summary is REPLACED, not carried

_PARAM_PUBLIC: Final = frozenset({"name", "type", "required", "secret"})
_PARAM_LOCAL: Final = frozenset({"example", "choices"})

_PROVENANCE_PUBLIC: Final = frozenset(
    {"compiler_version", "certified", "certification_status", "expires_at"}
)
_PROVENANCE_LOCAL: Final = frozenset(
    {"policy_name", "content_digest", "source_recording_sha256"}
)

_BUNDLE_PUBLIC: Final = frozenset(
    {
        "name",  # REPLACED by a fixed string
        "schema_version",
        "is_program",
        "is_composition",  # bool
        "composition_schema",  # closed literal
        "contains_phi",
        "phi_scrubbed",
        "encrypted",
        "step_count",
        "action_count",
        "irreversible_count",
        "identity_armed_count",
        "identity_unarmed_count",
        "effect_count",
        "api_binding_count",
        "halt_point_count",
        "child_count",  # int
        "params",  # rebuilt field-by-field
        "provenance",  # rebuilt field-by-field
    }
)
_BUNDLE_LOCAL: Final = frozenset({"created_at", "viewport"})

_EDGE_PUBLIC: Final = frozenset({"source", "target", "kind", "label"})
_EDGE_LOCAL: Final = frozenset({"guard"})  # free-text predicate summary

_SPEC_PUBLIC: Final = frozenset({"spec_version", "bundle", "nodes", "edges"})
_SPEC_LOCAL: Final[frozenset[str]] = frozenset()

#: model -> (fields that may leave, fields that must not). Together these must
#: cover EVERY declared field of the model; see
#: :func:`assert_field_boundary_is_closed`.
FIELD_BOUNDARY: Final[dict[type[BaseModel], tuple[frozenset[str], frozenset[str]]]] = {
    ProgramGraphSpec: (_SPEC_PUBLIC, _SPEC_LOCAL),
    BundleMeta: (_BUNDLE_PUBLIC, _BUNDLE_LOCAL),
    ProvenanceInfo: (_PROVENANCE_PUBLIC, _PROVENANCE_LOCAL),
    ParamInfo: (_PARAM_PUBLIC, _PARAM_LOCAL),
    GraphNode: (_NODE_PUBLIC, _NODE_LOCAL),
    ResolutionInfo: (_RESOLUTION_PUBLIC, _RESOLUTION_LOCAL),
    ResolutionRung: (_RUNG_PUBLIC, _RUNG_LOCAL),
    IdentityInfo: (_IDENTITY_PUBLIC, _IDENTITY_LOCAL),
    EffectInfo: (_EFFECT_PUBLIC, _EFFECT_LOCAL),
    GraphEdge: (_EDGE_PUBLIC, _EDGE_LOCAL),
}


def check_model_partition(
    model: type[BaseModel],
    public: frozenset[str],
    local: frozenset[str],
) -> None:
    """Raise unless ``public`` and ``local`` exactly partition ``model``.

    This is the Python stand-in for the cloud presenter's
    ``key: keyof typeof FACT_LABELS`` type: an unenumerated field is refused
    rather than passed through.
    """

    declared = frozenset(model.model_fields)
    overlap = public & local
    if overlap:
        raise ProjectionBoundaryError(
            f"{model.__name__}: field(s) classified BOTH public and local: "
            f"{sorted(overlap)}"
        )
    unclassified = declared - (public | local)
    if unclassified:
        raise ProjectionBoundaryError(
            f"{model.__name__}: unclassified field(s) {sorted(unclassified)} would "
            "reach a non-local projection by default. Add each to the model's "
            "public set in openadapt_flow/visualize/projection.py only if it can "
            "never carry recorded application data or operator free text; "
            "otherwise add it to the local set."
        )
    stale = (public | local) - declared
    if stale:
        raise ProjectionBoundaryError(
            f"{model.__name__}: classified field(s) {sorted(stale)} no longer exist"
        )


def assert_field_boundary_is_closed() -> None:
    """Verify every crossing model is fully classified. Run at import."""

    for model, (public, local) in FIELD_BOUNDARY.items():
        check_model_partition(model, public, local)


assert_field_boundary_is_closed()


# --------------------------------------------------------------------------
# Derived public values.
# --------------------------------------------------------------------------


def _safe_title(node: GraphNode) -> str:
    if node.kind == NodeKind.TERMINAL:
        return (
            "End of declared steps"
            if (node.outcome or "").lower() == "success"
            else "Stopped for review"
        )
    if node.kind == NodeKind.BRANCH:
        return "Evaluate a declared condition"
    if node.kind == NodeKind.BUSINESS_DECISION:
        return "Request an authorized decision"
    if node.kind == NodeKind.LOOP:
        return "Repeat the bounded steps"
    if node.kind == NodeKind.SUBFLOW_CALL:
        return "Run an approved subflow"
    if node.kind == NodeKind.CHILD_BUNDLE:
        return "Run an approved child bundle"
    action = (node.action or "").lower()
    if action in {"click", "double_click"}:
        return "Select an interface target"
    if action == "type":
        if node.secret:
            return "Enter an approved secret"
        if node.param:
            return "Enter an approved input"
        return "Enter an approved value"
    if action == "key":
        return "Send an approved key"
    if action == "scroll":
        return "Move through the current view"
    if action in {"wait", "sleep"}:
        return "Wait for the declared state"
    if node.has_api_binding:
        return "Run an approved API action"
    return "Run an approved action"


def _safe_halts(node: GraphNode) -> list[str]:
    if not node.halts:
        return []
    return [f"declared stop rule {index + 1}" for index in range(len(node.halts))]


def _in_vocabulary(value: Optional[str], vocabulary: frozenset[str]) -> Optional[str]:
    """Return ``value`` when the closed vocabulary admits it, else ``None``.

    For an OPTIONAL field, dropping to ``None`` is truthful: the projection
    states nothing rather than stating something unenumerated.
    """

    return value if value in vocabulary else None


def _require_vocabulary(value: str, vocabulary: frozenset[str], field: str) -> str:
    """Return ``value``, or raise if the closed vocabulary does not admit it.

    Used for a REQUIRED governance field, where the two silent options are both
    wrong: emitting the unenumerated value risks leaking free text, and
    substituting a default misstates a system-of-record fact (substituting the
    ``risk`` default would actively understate risk). An out-of-vocabulary value
    here means the spec has drifted from this module, which is the condition
    this module exists to catch, so it fails closed and loudly.
    """

    if value not in vocabulary:
        # The rejected value is NOT echoed: it is the very thing suspected of
        # carrying recorded data, and an exception message travels into logs
        # and error reports.
        raise ProjectionBoundaryError(
            f"{field}: value is outside the closed vocabulary "
            f"{sorted(vocabulary)}. Widen the vocabulary in "
            "openadapt_flow/visualize/projection.py only after confirming the "
            "new value can never carry recorded application data."
        )
    return value


def _safe_badges(badges: list[str]) -> list[str]:
    return [
        badge
        for badge in badges
        if badge in _BADGES or _COUNTED_BADGE_RE.match(badge) is not None
    ]


# --------------------------------------------------------------------------
# Per-model allow-list rebuilds. Each constructs a NEW instance from the
# enumerated public fields only; nothing is copied wholesale.
# --------------------------------------------------------------------------


def _project_rungs(resolution: ResolutionInfo) -> ResolutionInfo:
    rungs = [
        ResolutionRung(
            name=rung.name,
            label=_RUNG_LABELS[rung.name],
            present=rung.present,
            # detail is LOCAL: selector, template path, or OCR text.
        )
        for rung in resolution.rungs
        if rung.name in _RUNG_LABELS
    ]
    return ResolutionInfo(
        rungs=rungs,
        top_rung=_in_vocabulary(resolution.top_rung, frozenset(_RUNG_LABELS)),
    )


def _project_identity(identity: IdentityInfo) -> IdentityInfo:
    return IdentityInfo(
        applicable=identity.applicable,
        armed=identity.armed,
        phi_free=identity.phi_free,
        has_structured=identity.has_structured,
        has_identifier_crop=identity.has_identifier_crop,
        # reason is LOCAL: why the step compiled unarmed.
    )


def _project_effect(effect: EffectInfo) -> EffectInfo:
    return EffectInfo(
        kind=_require_vocabulary(effect.kind, _EFFECT_KINDS, "EffectInfo.kind"),
        summary="Independent effect contract",
        risk=_require_vocabulary(effect.risk, _RISKS, "EffectInfo.risk"),
        needs_operator_confirmation=effect.needs_operator_confirmation,
    )


def _project_node(node: GraphNode) -> GraphNode:
    """Rebuild ``node`` from the allow-list. Unenumerated fields never leave."""

    return GraphNode(
        id=node.id,
        index=node.index,
        kind=node.kind,
        title=_safe_title(node),
        action=_in_vocabulary(node.action, _ACTIONS),
        risk=_in_vocabulary(node.risk, _RISKS),
        risk_review_required=node.risk_review_required,
        secret=node.secret,
        resolution=(
            None if node.resolution is None else _project_rungs(node.resolution)
        ),
        identity=(None if node.identity is None else _project_identity(node.identity)),
        effects=[_project_effect(effect) for effect in node.effects],
        has_api_binding=node.has_api_binding,
        postconditions=[
            kind for kind in node.postconditions if kind in _POSTCONDITION_KINDS
        ],
        guard_on_unmet=_in_vocabulary(node.guard_on_unmet, _GUARD_ON_UNMET),
        outcome=_in_vocabulary(node.outcome, _OUTCOMES),
        halts=_safe_halts(node),
        badges=_safe_badges(node.badges),
        surface=_in_vocabulary(node.surface, _SURFACES),
    )


def _project_edge(edge: GraphEdge) -> GraphEdge:
    return GraphEdge(
        source=edge.source,
        target=edge.target,
        kind=edge.kind,
        label=_EDGE_LABELS[edge.kind.value],
        # guard is LOCAL: a free-text predicate summary.
    )


def _project_param(param: ParamInfo, index: int) -> ParamInfo:
    return ParamInfo(
        name=f"input_{index + 1}",
        type=param.type,
        required=param.required,
        secret=param.secret,
        # example and choices are LOCAL: recorded values.
    )


def _project_provenance(provenance: ProvenanceInfo) -> ProvenanceInfo:
    return ProvenanceInfo(
        compiler_version=provenance.compiler_version,
        certified=provenance.certified,
        certification_status=provenance.certification_status,
        expires_at=provenance.expires_at,
        # policy_name, content_digest, source_recording_sha256 are LOCAL.
    )


def _project_bundle(bundle: BundleMeta) -> BundleMeta:
    return BundleMeta(
        name=PROJECTED_BUNDLE_NAME,
        schema_version=bundle.schema_version,
        is_program=bundle.is_program,
        is_composition=bundle.is_composition,
        composition_schema=_in_vocabulary(
            bundle.composition_schema, _COMPOSITION_SCHEMAS
        ),
        contains_phi=bundle.contains_phi,
        phi_scrubbed=bundle.phi_scrubbed,
        encrypted=bundle.encrypted,
        step_count=bundle.step_count,
        action_count=bundle.action_count,
        irreversible_count=bundle.irreversible_count,
        identity_armed_count=bundle.identity_armed_count,
        identity_unarmed_count=bundle.identity_unarmed_count,
        effect_count=bundle.effect_count,
        api_binding_count=bundle.api_binding_count,
        halt_point_count=bundle.halt_point_count,
        child_count=bundle.child_count,
        params=[
            _project_param(param, index) for index, param in enumerate(bundle.params)
        ],
        provenance=_project_provenance(bundle.provenance),
        # created_at and viewport are LOCAL.
    )


def project_program_graph(
    spec: ProgramGraphSpec,
    profile: PresentationProfile | str,
) -> ProgramGraphSpec:
    """Return the exact graph structure for the requested data boundary.

    A non-local projection retains node and edge identities. It is rebuilt from
    the closed allow-list in :data:`FIELD_BOUNDARY`, so it carries only the
    enumerated fields: no recorded application data, no operator free text, and
    no local provenance. The function does not assert that the source is
    PHI-free.
    """

    selected = PresentationProfile(profile)
    if selected == PresentationProfile.OPERATOR_LOCAL:
        return spec.model_copy(deep=True)

    return ProgramGraphSpec(
        spec_version=spec.spec_version,
        bundle=_project_bundle(spec.bundle),
        nodes=[_project_node(node) for node in spec.nodes],
        edges=[_project_edge(edge) for edge in spec.edges],
    )
