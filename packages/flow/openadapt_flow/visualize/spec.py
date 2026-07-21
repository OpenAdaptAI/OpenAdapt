"""The shared *program graph* spec -- the single source of truth every
visualization surface renders.

A :class:`ProgramGraphSpec` is a backend-neutral, JSON-serializable projection
of a compiled bundle (:class:`openadapt_flow.ir.Workflow`): NODES are the
compiled steps / program states (each carrying its target, resolution ladder,
identity gate, effect check, verification postconditions, and risk class),
EDGES are the sequence (with room for future branches / loops / exception
paths), and every node records its own HALT / repair points as first-class
annotations.

This module deliberately carries NO rendering and NO IR-parsing logic -- it is
the wire contract. :mod:`openadapt_flow.visualize.builder` projects a
``Workflow`` onto it; :mod:`openadapt_flow.visualize.render` (and the cloud /
desktop components) render it. A matching JSON Schema is emitted to
``schemas/program-graph-v1.json`` so non-Python surfaces validate the same
shape.

Forward-compatibility: the spec already models the Phase-2 control-flow node
kinds (``branch`` / ``loop`` / ``subflow_call`` / ``terminal``) and typed edge
kinds so richer compiled structure renders without a spec break. A linear
bundle (today's common case) projects to a straight chain of ``action`` nodes
ending in a ``success`` terminal.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

#: Spec wire version. Bump only on a BREAKING change to the node/edge shape;
#: additive optional fields do not bump it (a v1 reader ignores unknown fields).
SPEC_VERSION = 1


class NodeKind(str, Enum):
    """Kind of graph node -- mirrors :class:`openadapt_flow.ir.StateKind` so a
    Phase-2 program graph projects 1:1, while a linear bundle is all ``action``
    nodes plus a trailing ``terminal``."""

    ACTION = "action"
    BRANCH = "branch"
    LOOP = "loop"
    SUBFLOW_CALL = "subflow_call"
    TERMINAL = "terminal"


class EdgeKind(str, Enum):
    """Kind of graph edge. ``sequence`` is the unconditional next-step edge of a
    linear program; ``branch`` is a guarded transition; ``exception`` routes a
    failed action to a handler; ``loop_body`` links a loop node to its per-row
    body. Only ``sequence`` occurs in a linear bundle today; the rest leave room
    for compiled control flow."""

    SEQUENCE = "sequence"
    BRANCH = "branch"
    EXCEPTION = "exception"
    LOOP_BODY = "loop_body"


class ResolutionRung(BaseModel):
    """One rung of the target-resolution ladder for an action node.

    The ladder is consumed strongest-first at replay (structural DOM/UIA ->
    template -> ocr -> landmarks -> grounder), with an optional API tier ABOVE
    the GUI ladder. ``present`` is whether the compiler captured evidence for
    this rung; ``detail`` is a short human hint (e.g. the selector, or landmark
    count)."""

    name: str = Field(
        description="Rung id: api|structural|template|ocr|landmarks|grounder"
    )
    label: str = Field(description="Human label for the rung")
    present: bool = Field(description="Whether the compiler captured this rung")
    detail: str = Field(default="", description="Short human hint about the evidence")


class ResolutionInfo(BaseModel):
    """The full resolution ladder for an action node (ordered strongest-first).

    ``top_rung`` names the strongest rung actually present -- the one the
    runtime prefers -- so a surface can badge "resolves by DOM selector" vs
    "resolves by pixels" at a glance."""

    rungs: list[ResolutionRung] = Field(default_factory=list)
    top_rung: Optional[str] = Field(
        default=None, description="Strongest present rung (None if none captured)"
    )


class IdentityInfo(BaseModel):
    """The pre-action identity gate for a click / anchored-type node -- the
    wrong-record guard's compile-time coverage.

    ``armed`` is True when the gate will verify the target's identity band
    before acting, False when the step will act WITHOUT identity verification
    (``reason`` says why it compiled unarmed), and None when identity does not
    apply to this node. ``phi_free`` is True when identity is carried as a
    salted-hash template rather than a plaintext band."""

    applicable: bool = Field(default=False, description="Does an identity gate apply?")
    armed: Optional[bool] = Field(default=None, description="Is the gate armed?")
    reason: Optional[str] = Field(
        default=None, description="Why the step compiled unarmed (if unarmed)"
    )
    phi_free: bool = Field(
        default=False,
        description="Identity carried as salted-hash template (no plaintext)",
    )
    has_structured: bool = Field(
        default=False, description="Structured (DOM/a11y) identity present"
    )
    has_identifier_crop: bool = Field(
        default=False, description="Pixel identifier crop present"
    )


class EffectInfo(BaseModel):
    """One declared system-of-record effect on a node -- what must be true of
    the REAL record for the step to have actually succeeded (verified after the
    action; a non-confirmed verdict HALTs)."""

    kind: str = Field(description="record_written | field_equals")
    summary: str = Field(description="Short human description of the effect contract")
    risk: str = Field(default="reversible")
    needs_operator_confirmation: bool = Field(
        default=False,
        description="Placeholder effect: binding not derivable from the demo; HALTs until bound",
    )


class ParamInfo(BaseModel):
    """A typed workflow parameter (the run's inputs)."""

    name: str
    type: str = "string"
    required: bool = True
    secret: bool = False
    example: Optional[str] = None
    choices: list[str] = Field(default_factory=list)


class ProvenanceInfo(BaseModel):
    """Bundle provenance + certification, projected from the bundle manifest."""

    compiler_version: str = ""
    certified: bool = False
    certification_status: Optional[str] = None
    policy_name: Optional[str] = None
    expires_at: Optional[str] = None
    content_digest: Optional[str] = None
    source_recording_sha256: Optional[str] = None


class BundleMeta(BaseModel):
    """Bundle-level summary shown in the visualization header."""

    name: str
    schema_version: int
    created_at: Optional[str] = None
    viewport: Optional[tuple[int, int]] = None
    is_program: bool = Field(
        default=False,
        description="True when a Phase-2 ProgramGraph is present (branches/loops); "
        "False for a linear step list.",
    )
    # PHI / at-rest governance flags (surfaced so a viewer can see them at a glance)
    contains_phi: bool = False
    phi_scrubbed: bool = False
    encrypted: bool = False
    # Rollup counts (also derivable from nodes; precomputed for a compact header)
    step_count: int = 0
    action_count: int = 0
    irreversible_count: int = 0
    identity_armed_count: int = 0
    identity_unarmed_count: int = 0
    effect_count: int = 0
    api_binding_count: int = 0
    halt_point_count: int = 0
    params: list[ParamInfo] = Field(default_factory=list)
    provenance: ProvenanceInfo = Field(default_factory=ProvenanceInfo)


class GraphNode(BaseModel):
    """A node in the program graph. A compiled step projects to an ``action``
    node; a Phase-2 program state projects to its matching kind."""

    id: str
    index: int = Field(description="0-based order in the program (for layout)")
    kind: NodeKind = NodeKind.ACTION
    title: str = Field(description="Human-readable intent / purpose")
    # -- action payload (kind == action) --
    action: Optional[str] = Field(
        default=None, description="click|type|key|wait|scroll"
    )
    risk: Optional[str] = Field(default=None, description="reversible|irreversible")
    param: Optional[str] = Field(
        default=None, description="Bound run parameter, if any"
    )
    secret: bool = Field(default=False, description="Types a secret (never stored)")
    key: Optional[str] = Field(default=None, description="Key for a KEY action")
    resolution: Optional[ResolutionInfo] = None
    identity: Optional[IdentityInfo] = None
    effects: list[EffectInfo] = Field(default_factory=list)
    has_api_binding: bool = Field(default=False)
    api_summary: Optional[str] = None
    # -- verification --
    postconditions: list[str] = Field(
        default_factory=list,
        description="Vision postcondition kinds checked after the action",
    )
    # -- control flow --
    guard: Optional[str] = Field(
        default=None, description="Precondition summary, if guarded"
    )
    guard_on_unmet: Optional[str] = Field(
        default=None, description="halt|skip when guard unmet"
    )
    wait_until: Optional[str] = Field(
        default=None, description="Readiness predicate summary"
    )
    # -- terminal payload (kind == terminal) --
    outcome: Optional[str] = Field(default=None, description="success|halt|escalate")
    reason: str = ""
    # -- annotations (rendered as badges / markers) --
    #: Reasons this node can HALT the run (fail-safe stop points). Each is a
    #: short human string. Empty when the node has no distinguished halt point.
    halts: list[str] = Field(default_factory=list)
    #: Short capability/risk badges, e.g. "irreversible", "identity gate",
    #: "effect check", "unarmed", "secret", "API".
    badges: list[str] = Field(default_factory=list)


class GraphEdge(BaseModel):
    """A directed edge between two nodes."""

    source: str
    target: str
    kind: EdgeKind = EdgeKind.SEQUENCE
    label: str = ""
    guard: Optional[str] = Field(
        default=None, description="Guard summary for a branch edge"
    )


class ProgramGraphSpec(BaseModel):
    """The complete, self-describing visualization spec for one compiled bundle.

    Serialize with ``spec.model_dump_json()`` (or ``model_dump()``); every
    surface renders THIS, none re-parse the bundle. See the module docstring.
    """

    spec_version: int = SPEC_VERSION
    bundle: BundleMeta
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
