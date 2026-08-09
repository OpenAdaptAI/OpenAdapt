"""Intermediate representation for compiled workflow programs.

A :class:`Workflow` is the compiled artifact. It can carry either the
compatibility ``steps`` sequence (the degenerate straight-line program) or a
``ProgramGraph`` state machine with guarded branches, loops, subflows, and
exception paths. Every executable action retains redundant target evidence
(``Anchor``), the operation to perform, and the contracts that must hold before
and after actuation.

The canonical serialized form is a *bundle directory*:

    <bundle>/
      workflow.json[.enc]    # Workflow.model_dump_json(), optionally encrypted
      manifest.json          # PHI-free integrity and provenance sidecar
      templates/*[.enc]      # retained target/effect evidence

Pixel regions are ``(x, y, width, height)`` in recorded-frame coordinates;
structural and relational evidence remains first-class where the substrate
provides it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Iterator, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_serializer,
    model_validator,
)

from openadapt_flow.qualification_faults import FaultMutationReceipt

if TYPE_CHECKING:
    # Type-only import for the Step.effects forward reference. The RUNTIME
    # import is at the BOTTOM of this module (see the note there) to avoid a
    # circular import through openadapt_flow.runtime's package init.
    from openadapt_flow.qualification import QualificationProject
    from openadapt_flow.runtime.effects.effect import Effect

Region = tuple[int, int, int, int]
Point = tuple[int, int]
ExecutionTargetKind = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
#: How a surface is driven. ``in_session`` runs inside the session it
#: automates (accessibility/structured layers available when policy permits
#: the install); ``external`` drives a LOCAL client window of a remote
#: session via pixels/keyboard/mouse with zero install in the remote session.
#: The mode is fixed by explicit capability negotiation at qualification and
#: is never silently switched at run time (docs/SURFACES.md).
ExecutionMode = Literal["in_session", "external"]

#: Current bundle schema version. v2 adds the bundle manifest (per-asset
#: hashes, a whole-bundle content digest, and compiler/certification
#: provenance) and load-time structural + integrity validation, ON TOP of the
#: v1 semantics. v2 is a strict, ADDITIVE superset of v1: every v2-only field
#: defaults empty, so a v1 bundle migrates to v2 on read (see
#: ``openadapt_flow.bundle_validation.migrate_bundle_dict``) and replays
#: byte-for-byte. Bumped from 1 now that the IR carries ~10x the semantics it
#: did at v1 (typed params, predicates/guards, a full state-machine program,
#: system-of-record effects, API bindings, PHI-free identity templates).
SCHEMA_VERSION = 2

#: AEAD associated-data domain label for sealed ``templates/`` assets. DISTINCT
#: from :data:`openadapt_flow.crypto.BUNDLE_AAD` (which seals ``workflow.json``),
#: so a template ciphertext can never be authenticated as -- and substituted for
#: -- the workflow-json ciphertext (or vice versa) even under the SAME key. This
#: is the template-domain AAD the at-rest design calls for.
TEMPLATE_AAD: Final[bytes] = b"openadapt-flow/template"


class ActionKind(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    TYPE = "type"
    SELECT_OPTION = "select_option"
    KEY = "key"
    HOTKEY = "hotkey"
    WAIT = "wait"
    SCROLL = "scroll"


class Landmark(BaseModel):
    """A stable nearby text element, used by the geometry resolution rung.

    ``relation`` describes where the LANDMARK sits relative to the target:
    a landmark that is ``left_of`` the target implies the target is
    ``distance_px`` to the landmark's right. ``dx_px``/``dy_px``, when set,
    are the exact pixel offsets from the landmark's center to the target
    click point (target = landmark_center + (dx_px, dy_px)); the geometry
    rung prefers them over the coarser relation/distance estimate.
    """

    relation: Literal["left_of", "right_of", "above", "below"]
    ocr_text: str
    distance_px: int
    match_mode: Literal["fuzzy", "exact"] = Field(
        default="fuzzy",
        description=(
            "OCR comparison mode for this retained relation. Compiler-mined "
            "generic context remains fuzzy; a qualified opaque-field label "
            "uses exact normalized text so a near label cannot authorize input."
        ),
    )
    dx_px: Optional[int] = Field(
        default=None,
        description="Exact x offset landmark center -> target click point",
    )
    dy_px: Optional[int] = Field(
        default=None,
        description="Exact y offset landmark center -> target click point",
    )

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Keep legacy fuzzy landmarks byte-semantically unchanged.

        The package supports Pydantic 2.5, before ``Field(exclude_if=...)``.
        This v2-compatible serializer omits the additive default while keeping
        an explicit exact-label contract inside new bundle digests.
        """

        data: dict[str, Any] = handler(self)
        if self.match_mode == "fuzzy":
            data.pop("match_mode", None)
        return data


class StructuralLocator(BaseModel):
    """A stable structural (DOM / accessibility) locator for a step's target.

    Captured at record time from the recording backend's structured layer
    (:meth:`openadapt_flow.backend.StructuralActionBackend.structural_locator_at`)
    and consumed at replay by the structural ACTION rung -- the TOP of the
    resolution ladder (:mod:`openadapt_flow.runtime.resolver`). The runtime
    re-finds the SAME element by its stable identity -- a DOM id / CSS selector
    / ARIA role+name, or a Windows UIA ``AutomationId`` / ``ControlType``+
    ``Name`` -- and acts on the element's center DETERMINISTICALLY, with no
    pixel matching. This is the thesis shift from "vision-only" to
    "deterministic compiled automation with visual FALLBACK": the desktop
    benchmark measured UIA execution 21/21 vs compiled visual replay 6/21 under
    render drift.

    The visual anchor (template / ocr_text / landmarks) is ALWAYS kept too: the
    ladder falls through to it UNCHANGED when this locator is absent (pixel-only
    substrate -- RDP/Citrix/canvas) or when the element cannot be located at
    replay (see docs/LIMITS.md). Structural resolution is ADDITIVE -- it never
    removes the visual floor.

    Fields are backend-neutral; a browser backend fills ``selector`` / ``role``
    / ``name`` from the DOM, a native backend fills ``automation_id`` / ``role``
    / ``name`` from its accessibility tree (Windows UIA AutomationId or the
    Linux AT-SPI accessible ID). Each backend uses whichever fields it
    recorded; unset fields are ignored.
    """

    selector: Optional[str] = Field(
        default=None,
        description="Stable CSS/DOM selector, e.g. '#open-p1' (browser)",
    )
    frame_path: Optional[list[str]] = Field(
        default=None,
        max_length=8,
        description=(
            "Outermost-to-innermost CSS selectors for iframe/frame elements "
            "containing a browser target. Each selector must resolve uniquely "
            "in its parent frame; absent means the top-level document."
        ),
    )
    role: Optional[str] = Field(
        default=None,
        description="ARIA / UIA control role, e.g. 'button', 'link'",
    )
    name: Optional[str] = Field(
        default=None,
        description="Accessible name / label / text of the target element",
    )
    automation_id: Optional[str] = Field(
        default=None,
        description=(
            "Stable native accessibility ID: Windows UIA AutomationId or "
            "Linux AT-SPI accessible ID"
        ),
    )
    window_name: Optional[str] = Field(
        default=None,
        description=(
            "Exact top-level accessibility window name captured with the target. Native "
            "backends use it to scope candidate enumeration and refuse duplicate "
            "controls in a different application window."
        ),
    )


class TokenTemplate(BaseModel):
    """Salted-hash + shape descriptor for one recorded identity-band token.

    Carries NO plaintext (the PHI audit's REM-2): ``c``/``r`` are salted hashes
    of the token's OCR-canonical and squashed-raw forms; the rest are
    non-identifying shape flags and a length. Enough to reproduce the
    wrong-patient guard's per-token budgets at replay
    (:mod:`openadapt_flow.runtime.identity_template`) without persisting the
    identifier itself.
    """

    c: str = Field(description="salted hash of ocr_canonical(squashed token)")
    r: str = Field(description="salted hash of squashed raw token")
    n: int = Field(description="squashed length")
    alpha: bool = False
    name: bool = False
    digit: bool = False
    idsh: bool = False
    glyph: bool = False
    gen: bool = False


class ConcatTemplate(BaseModel):
    """Precomputed SPLIT-match key (consecutive recorded tokens the live OCR
    may glue into one), since hashes cannot be concatenated at replay."""

    i: int
    size: int
    c: str
    r: str
    digit: bool
    name: bool
    n: int


class IdentityTemplate(BaseModel):
    """PHI-free stand-in for ``Anchor.context_text`` / ``structured_identity``.

    A salted-hash, shape-preserving template of the recorded identity band. It
    lets the runtime re-run the SAME wrong-patient identity check
    (:mod:`openadapt_flow.runtime.identity_template`) with no readable name /
    DOB / MRN in the artifact. NOT a cryptographic control (a salted hash of a
    low-entropy identifier is brute-forceable by a holder of the bundle + salt);
    it removes *plaintext* PHI. The at-rest control is bundle encryption
    (docs/phi_at_rest.md, deferred). Set ``OPENADAPT_FLOW_IDENTITY_SALT`` at
    compile+replay to keep the salt out of the bundle and make the hashes
    one-way to anyone without the external secret.
    """

    schema_version: int = 1
    salt: str = Field(
        default="", description="per-bundle salt (hex); empty => env salt"
    )
    band_len: int = 0
    tokens: list[TokenTemplate] = Field(default_factory=list)
    concats: list[ConcatTemplate] = Field(default_factory=list)
    structured: Optional[str] = Field(
        default=None, description="salted hash of the structured identity string"
    )
    structured_params: list[str] = Field(
        default_factory=list,
        description=(
            "Workflow parameters embedded in the structured identity. Their "
            "demonstrated values are replaced by fixed sentinels before "
            "hashing; replay substitutes the run's value before exact compare."
        ),
    )
    signal_hashes: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Salted hashes of strict exact/explicitly-normalized identity "
            "evidence, including operator-selected extracted fields. Keys are "
            "produced by identity_signals."
        ),
    )
    context_params: list[str] = Field(
        default_factory=list,
        description=(
            "Workflow parameters replaced by fixed sentinels before the "
            "captured-context signal hashes were computed."
        ),
    )
    param_token_indices: dict[str, list[int]] = Field(default_factory=dict)
    rests_on_confusable_identifier: bool = False

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Omit empty additive metadata from legacy sealed bundle bytes.

        ``Field(exclude_if=...)`` would express this directly, but that option
        is newer than the package's declared Pydantic >=2.5 compatibility
        floor.  A wrap serializer is supported throughout Pydantic v2 and
        keeps pre-feature identity templates byte-semantically unchanged while
        still sealing non-empty run-bound structured parameter metadata.
        """
        data: dict[str, Any] = handler(self)
        if not self.structured_params:
            data.pop("structured_params", None)
        if not self.signal_hashes:
            data.pop("signal_hashes", None)
        if not self.context_params:
            data.pop("context_params", None)
        return data


class Anchor(BaseModel):
    """Redundant evidence for locating a step's target on screen.

    Resolution ladder consumes fields in order of preference (strongest, most
    drift-tolerant first): ``structural`` (DOM / UIA element, when the backend
    supports it) -> template (local, then global) -> ocr_text -> landmarks ->
    grounder. ``structural`` is the deterministic top rung; the remaining
    (visual) rungs are the FALLBACK floor for pixel-only substrates.
    """

    template: str = Field(description="Bundle-relative path to the PNG crop")
    structural: Optional[StructuralLocator] = Field(
        default=None,
        description=(
            "STRUCTURAL locator (DOM selector / role+name, or UIA"
            " AutomationId / role+name) of the clicked target, captured at"
            " record time when the recording backend exposes it"
            " (openadapt_flow.backend.StructuralActionBackend). Drives the"
            " structural ACTION rung -- the TOP of the resolution ladder --"
            " which re-finds the SAME element deterministically (no pixel"
            " match) and is far more drift-tolerant than the visual rungs"
            " (21/21 vs 6/21 on the desktop benchmark). None on pixel-only"
            " substrates or bundles compiled before this capability; the"
            " ladder then resolves via the visual rungs below."
        ),
    )
    region: Region = Field(description="Crop location in the recorded frame")
    click_point: Point = Field(description="Click point in the recorded frame")
    ocr_text: Optional[str] = Field(
        default=None, description="Text label at/near the target, if any"
    )
    context_text: Optional[str] = Field(
        default=None,
        description=(
            "Identity evidence: OCR text on the target's row (full-width"
            " band at the crop's height) EXCLUDING the target's own crop"
            " and timestamp-bearing lines. Verified before every click"
            " (see runtime.identity); None when the band had no usable"
            " text at compile time."
        ),
    )
    structured_identity: Optional[str] = Field(
        default=None,
        description=(
            "STRUCTURED identity text (DOM / accessibility tree) of the"
            " clicked target's row, captured at record time when the"
            " recording backend exposes it"
            " (openadapt_flow.backend.IdentityBackend.structured_text_at)."
            " The REAL characters (a genuine digit 0 vs a letter O), so"
            " replay verifies identity by exact/normalized string compare"
            " with NO OCR ambiguity -- the structured-text tier of the"
            " identity ladder (see runtime.identity). None on pixel-only"
            " substrates or bundles recorded before this capability; the"
            " ladder then falls back to the OCR context_text tier."
        ),
    )
    identity_template: Optional[IdentityTemplate] = Field(
        default=None,
        description=(
            "PHI-FREE identity template (salted-hash + shape) of the recorded"
            " context band and structured identity. When present, the runtime"
            " verifies target identity from THIS (no plaintext name/DOB/MRN in"
            " the artifact — the PHI audit's REM-2) and ``context_text`` /"
            " ``structured_identity`` are None. Bundles compiled before this"
            " capability carry the plaintext fields instead and still replay"
            " unchanged (backward compatible). See"
            " openadapt_flow.runtime.identity_template."
        ),
    )
    identifier_crop: Optional[str] = Field(
        default=None,
        description=(
            "Bundle-relative PNG crop of the target row's DISCRIMINATIVE"
            " IDENTIFIER cell (the MRN / name+DOB region), emitted by the"
            " COMPILER (templates/identifiers/<step>.png -- under templates/"
            " so it is sealed with the other image crops in an encrypted"
            " bundle) for identity-armed steps that captured no structured"
            " text (Citrix / RDP / remote-display pixel recordings), or for"
            " any step whose identifier region was explicitly marked at"
            " record time (--identifier). Feeds the pixel-compare and"
            " optional VLM tiers of the identity ladder (see"
            " runtime.identity): the rendered PIXELS retain the O/0 and l/1"
            " distinction OCR collapses, so a crop-vs-crop compare catches"
            " the glyph-collapse wrong-patient where the DOM/a11y tree is"
            " unavailable. None on structured (browser/UIA) recordings"
            " unless marked (the structured tier owns identity there; no"
            " identity pixels at rest) and on bundles compiled before this"
            " capability -- Step.identifier_crop_missing_reason records WHY;"
            " the ladder then falls through to the OCR band tier."
        ),
    )
    identifier_region: Optional[Region] = Field(
        default=None,
        description=(
            "Location of `identifier_crop` in the recorded frame (x, y, w, h)."
            " Replay re-crops the SAME box at the resolved point (translated"
            " by the recorded region's offset from the recorded click point,"
            " exactly as the OCR band's exclude region is) so the pixel/VLM"
            " tiers compare like-for-like. Set iff `identifier_crop` is set."
        ),
    )
    landmarks: list[Landmark] = Field(default_factory=list)
    search_pad: int = Field(
        default=80,
        description="Pixels of padding around `region` for the local search",
    )


class PostconditionKind(str, Enum):
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    REGION_STABLE = "region_stable"  # phash of `region` within tolerance
    # Structural postconditions — mined as a fallback for steps whose action
    # changed nothing visible in the single-page frame (new-tab navigation,
    # SPA route changes off-screen), so such steps are no longer vacuous.
    # They compare the step's END state against its START state on the live
    # backend; nothing instance-specific (no literal URL/title) is baked in.
    # On a backend that cannot observe the property, they pass with the step
    # honestly still unverified (see docs/LIMITS.md).
    URL_CHANGED = "url_changed"  # page URL differs from the step's start
    TITLE_CHANGED = "title_changed"  # page title differs from the step's start
    NEW_TAB_OPENED = "new_tab_opened"  # browser page count increased


class Postcondition(BaseModel):
    kind: PostconditionKind
    text: Optional[str] = None
    region: Optional[Region] = None
    phash: Optional[str] = None
    phash_tolerance: int = 8
    timeout_s: float = 5.0
    template: Optional[str] = Field(
        default=None,
        description=(
            "Bundle-relative PNG crop of the expected REGION_STABLE content;"
            " lets the check tolerate small layout shifts (content found"
            " near, not exactly at, the recorded region)"
        ),
    )


IdentitySignalKeyValue = Literal[
    "subject_name",
    "record_id",
    "secondary_identifier",
    "application",
    "session",
    "workflow_state",
]


class ApiIdentityBinding(BaseModel):
    """Exact API request/effect binding for one qualified identity signal.

    ``param`` must be referenced by the outgoing request template and by
    ``effect_field`` through ``ValueExpr(param=...)``. Runtime validates both
    sides before any request is sent, so an API optimization cannot bypass the
    qualified identity contract that guards the equivalent GUI action.
    """

    model_config = ConfigDict(extra="forbid")

    key: IdentitySignalKeyValue
    param: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    effect_field: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
    request_pointers: list[str] = Field(
        min_length=1,
        max_length=8,
        description=(
            "Explicit target-bearing JSON pointers in the request template. "
            "Supported roots are /url/<effect-field>, /body/..., and /query/...; "
            "the terminal pointer segment must name the bound effect field, and "
            "headers are never accepted as target identity."
        ),
    )

    @field_validator("request_pointers")
    @classmethod
    def _validate_request_pointers(cls, pointers: list[str]) -> list[str]:
        if len(pointers) != len(set(pointers)):
            raise ValueError("API identity request pointers must be unique")
        for pointer in pointers:
            if (
                (pointer.startswith("/body/") and len(pointer) > len("/body/"))
                or (pointer.startswith("/query/") and len(pointer) > len("/query/"))
                or (pointer.startswith("/url/") and len(pointer) > len("/url/"))
            ):
                continue
            raise ValueError(
                "API identity request pointers must target /url/<effect-field>, "
                "/body/..., or /query/...; headers and whole request objects "
                "are not valid"
            )
        return pointers


class ApiBinding(BaseModel):
    """A declarative API/tool call that performs a step's write WITHOUT the GUI.

    The TOP of the capability ladder (RFC ``docs/design/WORKFLOW_PROGRAM_IR.md``
    section 4, the ``api`` implementation of a ``TransitionContract``): where the
    target app exposes a real API, driving its GUI to make the same write is the
    wrong tool. When a step carries an ``ApiBinding`` AND the run configures an
    :class:`~openadapt_flow.runtime.actuators.ApiActuator`, the runtime performs
    the write by CALLING the API deterministically (``$0``, zero model calls),
    confirms it with the same
    :class:`~openadapt_flow.runtime.effects.EffectVerifier` that gates a GUI
    write, and SKIPS the GUI resolution/act for that step. This is the
    ``api`` leaf of the same contract the structural rung realizes as ``dom_uia``
    and the visual ladder realizes as ``vision_rdp`` -- one semantic effect,
    backend-specific implementation.

    ADDITIVE and back-compatible: the field is optional and defaults absent, so a
    bundle carrying no binding replays EXACTLY as today (GUI actuation). A binding
    also defaults to GUI fallback when the API tier is unavailable before
    delivery. A workflow can instead set ``on_unavailable="halt"`` to declare an
    API-only action. That mode refuses before GUI resolution or input rather than
    inventing a second, unqualified actuation path.

    Fields are REST/JSON-first but shaped so a FHIR / MCP / tool binding fits the
    same model (``kind`` selects the substrate; a FHIR resource POST, an MCP tool
    invocation, and a plain REST write all reduce to method + endpoint + body +
    the expected effect). Placeholders ``{param}`` in the URL / query / body are
    substituted from the run's typed params (``Workflow.params`` overlaid by the
    caller's values) at actuation time.
    """

    kind: Literal["rest", "fhir", "mcp", "tool"] = Field(
        default="rest",
        description="Substrate: 'rest'/'fhir' HTTP, or an 'mcp'/'tool' call",
    )
    on_unavailable: Literal["gui", "halt"] = Field(
        default="gui",
        description=(
            "Pre-delivery API-unavailability policy. 'gui' preserves the "
            "back-compatible GUI fallback; 'halt' declares this step API-only "
            "and refuses without GUI input when no actuator is configured or "
            "the actuator proves that no request was sent."
        ),
    )
    method: str = Field(
        default="POST",
        description="HTTP verb (REST/FHIR) or logical operation name (mcp/tool)",
    )
    url_template: str = Field(
        description=(
            "Endpoint template; absolute (http...) or relative to the"
            " actuator's base_url. `{param}` placeholders are substituted from"
            " the run's typed params."
        ),
    )
    body_template: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "JSON request body template; string leaves may carry `{param}`"
            " placeholders substituted from the run's params."
        ),
    )
    query: dict[str, str] = Field(
        default_factory=dict,
        description="Query-string params; values may carry `{param}` too",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra request headers; values may carry `{param}`",
    )
    expected_status: list[int] = Field(
        default_factory=list,
        description=(
            "Explicit acceptable HTTP status codes; empty means any 2xx is"
            " success (anything else is treated as an attempted write of"
            " unknown effect and HALTs -- never GUI-retried)."
        ),
    )
    timeout_s: float = Field(default=5.0, description="Per-request timeout in seconds")
    effects: list["Effect"] = Field(
        default_factory=list,
        description=(
            "The system-of-record effect(s) this call is expected to produce."
            " Used to CONFIRM the API write via the run's EffectVerifier when"
            " the step itself declares no `effects` (an API write must be"
            " confirmable, exactly as a GUI write with declared effects is)."
        ),
    )
    identity: list[ApiIdentityBinding] = Field(
        default_factory=list,
        description=(
            "Exact run-parameter bindings that prove qualified record identity "
            "for API actuation. Each binding must occur in both the request "
            "template and an effect selector before the request may be sent."
        ),
    )

    @model_validator(mode="after")
    def _unique_identity_bindings(self) -> "ApiBinding":
        keys = [binding.key for binding in self.identity]
        if len(keys) != len(set(keys)):
            raise ValueError("API identity semantic keys must be unique")
        return self


# -- Workflow-program IR, Phase 1 (RFC docs/design/WORKFLOW_PROGRAM_IR.md §6) --
#
# Additive, backward-compatible first step toward the parameterized workflow
# program: typed parameters, a per-step `wait_until` readiness predicate, and a
# per-step `guard` precondition. ALL optional -- a bundle that declares none of
# them loads and replays EXACTLY as a v0 linear bundle does. These fields are
# deliberately ORTHOGONAL to Step.effects / Step.risk / the Anchor identity
# rungs: they add control-flow *around* the existing hardened action leaf, they
# do not restructure it (RFC §2.1). Full branches/loops/subflows are Phase 2 --
# NOT built here.


class ParamKind(str, Enum):
    """Typed-parameter kinds (RFC §2.2 ``ParamSpec.type``).

    ``entity_ref`` names an ENTITY to be re-resolved by the identity ladder at
    run time (the "which patient" fix, docs/LIMITS.md), not a literal to blindly
    substitute; the other kinds are literal values. Phase 1 stores the type for
    typing/validation/emit -- kind-specific run-time resolution (entity_ref
    re-resolution) is Phase 2+.
    """

    STRING = "string"
    DATE = "date"
    ENUM = "enum"
    NUMBER = "number"
    ENTITY_REF = "entity_ref"


class ParamSpec(BaseModel):
    """A TYPED workflow parameter (RFC §2.2). Supersedes a bare
    ``params: dict[str, str]`` entry by carrying a type, the recorded demo
    value (``example``, which doubles as the replay default), whether it is
    required, and enum choices. Additive: ``Workflow.param_specs`` lives
    ALONGSIDE the frozen ``Workflow.params`` dict; a bundle with an empty
    ``param_specs`` behaves exactly as before.
    """

    name: str
    type: ParamKind = ParamKind.STRING
    example: Optional[str] = Field(
        default=None,
        description="Recorded demo value; also the replay default when the "
        "caller supplies no value for this parameter.",
    )
    required: bool = True
    choices: list[str] = Field(
        default_factory=list, description="Allowed values for an enum param."
    )


class PredicateKind(str, Enum):
    """Deterministic, model-free predicate kinds (RFC §2.2 ``Predicate``).

    A predicate is evaluated over the CURRENT observed frame / run parameters
    with ZERO model calls -- it is the thing a linear IR cannot express. Phase 1
    ships the concrete kinds needed to (a) subsume today's SCROLL closed loop
    (``anchor_resolves``), (b) turn the optional-modal case into a guarded
    branch (``text_present``), and (c) branch on a parameter (``param_equals``),
    plus boolean composition. ``worklist_nonempty`` (loops) is Phase 2.
    """

    #: The embedded ``anchor`` resolves on the current frame via the (model-free)
    #: resolution ladder -- today's closed-loop scroll stop condition, now a
    #: first-class predicate.
    ANCHOR_RESOLVES = "anchor_resolves"
    #: ``text`` is present on the current frame (tolerant OCR presence check).
    TEXT_PRESENT = "text_present"
    #: ``text`` is NOT present on the current frame.
    TEXT_ABSENT = "text_absent"
    #: The run's value for parameter ``param`` equals ``value`` (string compare).
    PARAM_EQUALS = "param_equals"
    AND = "and"
    OR = "or"
    NOT = "not"


class Predicate(BaseModel):
    """A deterministic condition over observed state (RFC §2.2 ``Predicate``).

    Used two ways in Phase 1: as a ``Step.wait_until`` readiness predicate (the
    replayer polls it, BOUNDED by ``timeout_s``, and HALTS on timeout -- never
    proceeds-anyway) and as the condition inside a ``Guard``. Model-free by
    construction (see ``runtime.replayer._predicate_holds``); an unknown kind
    fails safe (does not hold).
    """

    kind: PredicateKind
    anchor: Optional[Anchor] = None  # ANCHOR_RESOLVES
    text: Optional[str] = None  # TEXT_PRESENT / TEXT_ABSENT
    param: Optional[str] = None  # PARAM_EQUALS
    value: Optional[str] = None  # PARAM_EQUALS
    intent: Optional[str] = Field(
        default=None,
        description="Human-readable label (also the resolution-ladder intent "
        "for an ANCHOR_RESOLVES predicate).",
    )
    operands: list["Predicate"] = Field(
        default_factory=list, description="Sub-predicates for AND / OR / NOT."
    )
    timeout_s: float = Field(
        default=5.0,
        description="wait_until bound: how long the replayer polls this "
        "predicate before HALTing (fail-safe; never proceed-anyway).",
    )


class Guard(BaseModel):
    """A deterministic precondition on a step (RFC §2.2, Phase 1 scope).

    ``predicate`` is evaluated over the step's entry frame. When it does NOT
    hold, ``on_unmet`` decides: ``"halt"`` (the DEFAULT -- the safe direction
    for an unmet precondition, per the RFC's refuse-rather-than-guess posture)
    stops the run naming the step; ``"skip"`` makes the step a no-op success
    (the expected-but-optional case, e.g. dismissing a survey modal only when it
    appeared -- a guarded branch WITHOUT the Phase-2 state machine). Full
    multi-way branching is Phase 2.
    """

    predicate: Predicate
    on_unmet: Literal["halt", "skip"] = "halt"


Predicate.model_rebuild()  # resolve the self-referential `operands`


def predicate_contract_sha256(predicate: Optional[Predicate]) -> str:
    """Return the canonical contract digest for one program transition guard."""

    payload: dict[str, Any]
    if predicate is None:
        payload = {"kind": "unconditional"}
    else:
        payload = predicate.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


class Interstitial(BaseModel):
    """A KNOWN recurring interstitial that can appear at a step's entry frame
    and would otherwise block the run (docs/LIMITS.md "state dependency").

    Examples are a reversible "rate this" modal or a "What's New" release
    notice -- overlays that are NOT part of the recorded task but appear
    intermittently and, left unhandled, either steal the click (a silent wrong
    action) or make the target unresolvable (a babysit-the-queue halt every time
    they show). Consent, authentication, submission, and other consequential
    prompts are deliberately outside this automatic path.

    Detection is model-free: ``detect`` is a Phase-1 :class:`Predicate` (a
    ``TEXT_PRESENT`` on the overlay's signature text is typical), evaluated over
    the settled entry frame with ZERO model calls. Handling is declarative:

    - ``dismiss_key`` set -> press Escape to dismiss it, then re-settle and
      verify the declared ``clearance`` predicate.
    - ``dismiss_anchor`` set -> resolve+click a non-consequential close control
      via the SAME model-free resolution ladder, then re-settle and verify the
      declared ``clearance`` predicate.
    - NEITHER set -> a known BLOCKING interstitial with no safe automatic
      dismissal: the run HALTS gracefully NAMING it (a clear report, not a blind
      "target not found"), so an operator handles it deliberately.

    Automatic dismissal is admitted only when ``risk`` is explicitly
    ``"reversible"``, ``consequential`` is explicitly ``False``, and an
    expected visual ``clearance`` predicate is declared. Every attempted
    dismissal is recorded in the enclosing :class:`StepResult` before delivery;
    a delivery error, failed clearance, or persistent detection HALTs after one
    action (no blind retries). Interstitials are
    checked at EVERY step's entry, before the guard / wait_until gates, since an
    overlay can appear at any point in a workflow, not only at its start.
    """

    name: str = Field(description="Human-readable label for reports/HALT text.")
    detect: Predicate = Field(
        description="Model-free condition that is TRUE when this interstitial is "
        "on the current frame (typically TEXT_PRESENT on its signature text)."
    )
    dismiss_key: Optional[str] = Field(
        default=None,
        description="Key to press to dismiss it. Only 'Escape' is admitted on "
        "the automatic non-consequential path. Mutually exclusive with "
        "dismiss_anchor.",
    )
    dismiss_anchor: Optional[Anchor] = Field(
        default=None,
        description="Non-consequential close control to resolve+click. Used "
        "when Escape does not dismiss the overlay.",
    )
    risk: Optional[Literal["reversible", "irreversible"]] = Field(
        default=None,
        description="Declared dismissal risk. Automatic dismissal requires an "
        "explicit 'reversible' declaration.",
    )
    consequential: Optional[bool] = Field(
        default=None,
        description="Whether dismissal can create a consequential state change. "
        "Automatic dismissal requires an explicit false declaration.",
    )
    clearance: Optional[Predicate] = Field(
        default=None,
        description="Expected visual postcondition after dismissal. It must hold "
        "and the detection predicate must no longer hold before replay continues.",
    )

    @model_validator(mode="after")
    def _validate_safe_detection_and_dismissal(self) -> "Interstitial":
        if not self.name.strip():
            raise ValueError("interstitial name must not be empty")
        if self.dismiss_key is not None and not self.dismiss_key.strip():
            raise ValueError("interstitial dismiss_key must not be empty")
        if self.dismiss_key is not None and self.dismiss_anchor is not None:
            raise ValueError(
                "interstitial must declare at most one dismissal mechanism"
            )
        if self.dismiss_anchor is not None and not self.dismiss_anchor.template.strip():
            structural = self.dismiss_anchor.structural
            has_structural_identity = structural is not None and any(
                value and value.strip()
                for value in (
                    structural.selector,
                    structural.role,
                    structural.name,
                    structural.automation_id,
                    structural.window_name,
                )
            )
            if not has_structural_identity:
                raise ValueError(
                    "automatic interstitial click dismissal requires either a "
                    "sealed anchor template or a non-empty structural locator"
                )
        has_dismissal = self.dismiss_key is not None or self.dismiss_anchor is not None
        if self.dismiss_key is not None and self.dismiss_key.strip() != "Escape":
            raise ValueError(
                "automatic interstitial key dismissal only permits Escape; "
                "submit/confirm keys must be modeled as governed workflow steps"
            )
        if has_dismissal:
            if self.risk != "reversible" or self.consequential is not False:
                raise ValueError(
                    "automatic interstitial dismissal requires explicit "
                    "risk='reversible' and consequential=False declarations"
                )
            if self.clearance is None:
                raise ValueError(
                    "automatic interstitial dismissal requires an expected "
                    "clearance postcondition"
                )
        elif any(
            value is not None
            for value in (self.risk, self.consequential, self.clearance)
        ):
            raise ValueError(
                "blocking interstitials without a dismissal must not declare "
                "dismissal risk or clearance"
            )

        def affirmative_visual(pred: Predicate) -> bool:
            if pred.kind is PredicateKind.TEXT_PRESENT:
                return bool(pred.text and pred.text.strip())
            if pred.kind is PredicateKind.ANCHOR_RESOLVES:
                return pred.anchor is not None and bool(pred.anchor.template.strip())
            if pred.kind in (PredicateKind.AND, PredicateKind.OR):
                return bool(pred.operands) and all(
                    affirmative_visual(operand) for operand in pred.operands
                )
            return False

        def visual_postcondition(pred: Predicate) -> bool:
            if pred.kind in (PredicateKind.TEXT_PRESENT, PredicateKind.TEXT_ABSENT):
                return bool(pred.text and pred.text.strip())
            if pred.kind is PredicateKind.ANCHOR_RESOLVES:
                return pred.anchor is not None and bool(pred.anchor.template.strip())
            if pred.kind in (PredicateKind.AND, PredicateKind.OR):
                return bool(pred.operands) and all(
                    visual_postcondition(operand) for operand in pred.operands
                )
            if pred.kind is PredicateKind.NOT:
                return len(pred.operands) == 1 and visual_postcondition(
                    pred.operands[0]
                )
            return False

        if not affirmative_visual(self.detect):
            raise ValueError(
                "interstitial detection must use affirmative visual evidence "
                "(TEXT_PRESENT or ANCHOR_RESOLVES, optionally composed with "
                "AND/OR); absence, parameter, and negated predicates could "
                "trigger a blind dismissal"
            )
        if self.clearance is not None and not visual_postcondition(self.clearance):
            raise ValueError(
                "interstitial clearance must be a visual postcondition; "
                "parameter-only predicates cannot verify a UI dismissal"
            )
        return self


class Step(BaseModel):
    id: str
    intent: str = Field(description="Human-readable purpose of the step")
    action: ActionKind
    anchor: Optional[Anchor] = None  # None for pure keyboard/wait steps
    text: Optional[str] = None  # literal text for TYPE
    param: Optional[str] = None  # if set, TYPE text comes from params[param]
    secret: bool = Field(
        default=False,
        description=(
            "TYPE steps only: the parameter is a SECRET (e.g. a password)."
            " Its literal value is NEVER stored in the recording, the events"
            " log, or this bundle; at replay it is injected from the"
            " environment variable OPENADAPT_FLOW_SECRET_<PARAM> (the param"
            " name upper-cased). ``text`` is always None for a secret step,"
            " and ``param`` names the required secret. A missing secret at"
            " replay is a clear, fail-fast error (see runtime.Replayer)."
        ),
    )
    field_label: Optional[str] = Field(
        default=None,
        description=(
            "TYPE/SELECT_OPTION steps only: the receiving field's best "
            "available label,"
            " captured PASSIVELY at record time (DOM <label>/aria-label/"
            "placeholder/name for web; accessibility label for native where"
            " the seam exists; nearby-OCR text as a compile-time fallback)."
            " Pure evidence for the compile-time parameter-proposal pass and"
            " the operator confirm review -- NEVER read at replay, and never"
            " a source of silent parameterization: a label-derived parameter"
            " proposal is always gated behind operator confirmation (see"
            " compiler.annotate.FieldLabelAnnotator)."
        ),
    )
    selection_commit_key: Optional[Literal["Enter", "Tab"]] = Field(
        default=None,
        description=(
            "SELECT_OPTION only: the Enter/Tab commit key compiled from a "
            "demonstrated provisional type-ahead followed immediately by that "
            "key. Runtime delivers text+commit as one backend "
            "operation and verifies the selected value in selection_region."
        ),
    )
    selection_region: Optional[Region] = Field(
        default=None,
        description=(
            "Exact demonstrated readback band where the complete option value "
            "was readable after commit. Runtime maps this band relative to the "
            "freshly re-resolved anchor; its recorded absolute coordinates are "
            "never replayed directly."
        ),
    )
    key: Optional[str] = None  # for KEY/HOTKEY, e.g. "Enter" or "s"
    modifiers: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="HOTKEY modifiers in deterministic press order",
    )
    drag_end_anchor: Optional[Anchor] = Field(
        default=None,
        description=(
            "DRAG destination evidence, resolved independently on the same fresh "
            "pre-actuation frame as the source anchor"
        ),
    )
    scroll_dx: Optional[int] = None  # for SCROLL: wheel delta, px right
    scroll_dy: Optional[int] = None  # for SCROLL: wheel delta, px down
    expect: list[Postcondition] = Field(default_factory=list)
    # System-of-record effects (RFC docs/design/WORKFLOW_PROGRAM_IR.md 2.2):
    # typed assertions verified against the REAL system of record (an API/DB
    # read), NOT the screen — closing the transactional-write gap the vision
    # `expect` postconditions above are blind to (docs/LIMITS.md "5 of 7
    # silent"). Verified by the run's configured EffectVerifier AFTER the
    # action executes; a non-CONFIRMED verdict (REFUTED / INDETERMINATE) HALTS
    # the run (see openadapt_flow.runtime.replayer and
    # docs/design/EFFECT_VERIFIER.md). Additive and back-compatible: the
    # default is empty, so a bundle carrying no effects replays exactly as
    # before, and a declared effect with no verifier configured is a
    # deployment error that HALTS (never a silent unverifiable write).
    effects: list["Effect"] = Field(default_factory=list)
    # API/tool binding (RFC section 4, the `api` implementation of the
    # transition contract): a declarative description of the API call that
    # performs THIS step's write. When present AND the run configures an
    # ApiActuator, the runtime performs the write via the API (deterministic,
    # $0, no model), confirms it with the EffectVerifier, and SKIPS the GUI
    # resolve/act for this step (see openadapt_flow.runtime.replayer). Additive
    # and back-compatible: None (default) means the step actuates through the
    # GUI resolution ladder EXACTLY as today; a binding present with no actuator
    # configured also falls through to the GUI (the API tier's safe fallback).
    api_binding: Optional[ApiBinding] = None
    risk: Literal["reversible", "irreversible"] = "reversible"
    risk_explanation: Optional[str] = Field(
        default=None,
        max_length=512,
        description=(
            "Why the compiler or qualifying operator assigned this risk. "
            "This is provenance, not runtime evidence."
        ),
    )
    risk_review_required: bool = Field(
        default=False,
        description=(
            "True when risk is ambiguous and certification must refuse until "
            "an operator supplies an explicit reviewed override"
        ),
    )
    # Workflow-program IR, Phase 1 (RFC §6) -- both OPTIONAL and additive; a
    # step with neither replays EXACTLY as a v0 step. Orthogonal to effects /
    # risk / identity above.
    #
    # ``wait_until``: a BOUNDED readiness predicate the replayer polls BEFORE
    # acting; timeout => HALT (fail-safe, never proceed-anyway). This subsumes
    # today's SCROLL closed loop as its first concrete predicate -- a SCROLL
    # step's default readiness is "the next anchored step's anchor resolves",
    # now expressed as an ANCHOR_RESOLVES predicate (see runtime.replayer).
    wait_until: Optional[Predicate] = None
    # ``guard``: a deterministic precondition evaluated on the entry frame.
    # Unmet => HALT (default) or SKIP the step (see Guard.on_unmet).
    guard: Optional[Guard] = None

    @model_validator(mode="after")
    def _validate_rich_action_contract(self) -> "Step":
        if self.action is not ActionKind.DRAG and self.drag_end_anchor is not None:
            raise ValueError("drag_end_anchor is valid only for DRAG steps")

        if self.action is ActionKind.HOTKEY:
            if not self.key or not self.modifiers:
                raise ValueError("HOTKEY steps require a key and at least one modifier")
        elif self.modifiers:
            raise ValueError("modifiers are valid only for HOTKEY steps")
        if (self.selection_commit_key is None) != (self.selection_region is None):
            raise ValueError(
                "selection_commit_key and selection_region must be set together"
            )
        if self.selection_commit_key is not None:
            if self.action is not ActionKind.SELECT_OPTION:
                raise ValueError(
                    "option-selection contracts are valid only on SELECT_OPTION"
                )
            if self.secret:
                raise ValueError("secret TYPE steps cannot be option selections")
            if self.anchor is None:
                raise ValueError(
                    "option-selection contracts require the demonstrated field "
                    "anchor for fresh pre-commit re-resolution"
                )
        elif self.action is ActionKind.SELECT_OPTION:
            raise ValueError(
                "SELECT_OPTION requires selection_commit_key and selection_region"
            )
        return self

    timeout_s: float = 10.0
    # Identity-protection audit trail (clicks and anchored text/select steps):
    # whether this step's click is guarded by the pre-click identity check
    # (anchor.context_text present). Written by the compiler so an
    # operator can audit a bundle's protection coverage BEFORE running it;
    # None on non-click steps and on bundles compiled before this field
    # existed. An UNARMED click proceeds with NO identity verification
    # (see docs/LIMITS.md).
    identity_armed: Optional[bool] = Field(
        default=None,
        description=(
            "Clicks/anchored TYPE/SELECT_OPTION only: True when the pre-click identity"
            " check is armed (context band recorded); False when the step"
            " will click WITHOUT identity verification; None for steps"
            " the check does not apply to (or pre-metric bundles)."
        ),
    )
    identity_unarmed_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why an applicable step compiled unarmed (no readable band"
            " text, band too generic, ...); None when armed or not"
            " applicable."
        ),
    )
    identifier_crop_missing_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why this identity-applicable step compiled WITHOUT a pixel"
            " identifier crop (anchor.identifier_crop) — the explicit"
            " degrade record for the pixel identity tier, mirroring"
            " identity_unarmed_reason: e.g. structured identity owns the"
            " step, no readable identity band, a marked --identifier region"
            " was invalid. None when a crop WAS emitted, on non-applicable"
            " steps, and on bundles compiled before this field existed."
            " Without a crop the pixel-compare tier abstains on"
            " remote-display/pixel replays and identity falls to the OCR"
            " band tier (docs/LIMITS.md)."
        ),
    )


# -- Workflow-program IR, Phase 2 (RFC docs/design/WORKFLOW_PROGRAM_IR.md §2) --
#
# The parameterized STATE MACHINE: the control flow a linear action list cannot
# express -- LOOPS over a worklist, guarded BRANCHES, reusable SUBFLOWS, and
# EXCEPTION paths. Built ADDITIVELY on the Phase-1 pieces: a state's action IS a
# Phase-1 ``Step`` (the unchanged, hardened action leaf -- same anchor/identity/
# effect/risk machinery), a transition's guard IS a Phase-1 ``Predicate``, and a
# branch reuses the SAME model-free predicate evaluation. BACKWARD-COMPATIBLE:
# ``Workflow.program`` is OPTIONAL -- when it is None the runtime executes
# today's linear ``Workflow.steps`` loop byte-for-byte, and a linear bundle
# lifts mechanically to the degenerate single-path graph (``lift_to_program``,
# RFC §2.6). ZERO model calls at run time -- guards, branches, loops, and
# subflow dispatch are all deterministic ($0 replay).


class StateKind(str, Enum):
    """The kinds of node in a workflow-program graph (RFC §2.2)."""

    ACTION = "action"  # perform a Step (today's hardened action leaf)
    BRANCH = "branch"  # pick an outgoing transition by guard; performs no action
    BUSINESS_DECISION = "business_decision"  # typed, authorized human choice
    LOOP = "loop"  # iterate a worklist, running a body subflow per row
    SUBFLOW_CALL = "subflow_call"  # invoke a reusable named subflow
    TERMINAL = "terminal"  # end this (sub)graph: success | halt | escalate


class Transition(BaseModel):
    """A guarded edge to a target state (RFC §2.2) -- the thing a linear IR
    cannot express.

    ``guard`` is a Phase-1 :class:`Predicate` evaluated (model-free) over the
    current frame / run params; ``None`` means UNCONDITIONAL (the RFC's ``TRUE``
    edge -- the default fall-through, and the only edge kind a degenerate linear
    program has). A state's ``transitions`` are evaluated IN ORDER; the first
    whose guard holds wins. Multiple non-``TRUE`` transitions make a multi-way
    branch.
    """

    guard: Optional[Predicate] = None
    target: str = Field(description="Id of the state this edge leads to")
    label: str = Field(default="", description="Human-readable edge label")


class BusinessDecisionEvidenceRequirement(BaseModel):
    """One reviewed evidence item required before a business answer is valid.

    The answer carries only a digest of the retained local artifact.  It does
    not carry screenshots, record values, or free text across a trust boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    label: str = Field(min_length=1, max_length=240)


class BusinessDecisionOption(BaseModel):
    """One finite answer and its exact compiled successor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    label: str = Field(min_length=1, max_length=240)
    value: str = Field(min_length=1, max_length=512)
    target: str = Field(min_length=1, max_length=128)
    required_evidence: tuple[str, ...] = ()


class BusinessDecisionSpec(BaseModel):
    """A finite, reviewed human decision inside a workflow program.

    This is not an identity or effect verifier.  It only selects a declared
    branch and binds a declared output.  The successor action still runs the
    normal fresh-frame, identity, policy, postcondition, and effect gates.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.business-decision/v1"] = (
        "openadapt.business-decision/v1"
    )
    question: str = Field(min_length=1, max_length=500)
    authorized_roles: tuple[str, ...] = Field(min_length=1)
    output_param: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
    options: tuple[BusinessDecisionOption, ...] = Field(min_length=2)
    evidence_requirements: tuple[BusinessDecisionEvidenceRequirement, ...] = ()
    expires_after_s: int = Field(default=3600, ge=30, le=7 * 24 * 3600)
    revalidation: tuple[Predicate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_contract(self) -> "BusinessDecisionSpec":
        if not self.question.strip() or self.question.strip() != self.question:
            raise ValueError("business decision question must be trimmed and non-empty")
        roles = tuple(role.strip() for role in self.authorized_roles)
        if (
            any(not role for role in roles)
            or roles != self.authorized_roles
            or len(set(roles)) != len(roles)
            or any(len(role) > 128 for role in roles)
        ):
            raise ValueError(
                "business decision roles must be unique, non-empty, and at "
                "most 128 characters"
            )
        requirement_ids = tuple(item.id for item in self.evidence_requirements)
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("business decision evidence ids must be unique")
        option_ids = tuple(item.id for item in self.options)
        option_labels = tuple(item.label.strip() for item in self.options)
        option_values = tuple(item.value for item in self.options)
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("business decision option ids must be unique")
        if (
            any(not label for label in option_labels)
            or option_labels != tuple(item.label for item in self.options)
            or len({label.casefold() for label in option_labels}) != len(option_labels)
        ):
            raise ValueError(
                "business decision option labels must be trimmed, non-empty, "
                "and unique without case"
            )
        if any(not value.strip() or value.strip() != value for value in option_values):
            raise ValueError(
                "business decision option values must be trimmed and non-empty"
            )
        if len(set(option_values)) != len(option_values):
            raise ValueError("business decision option values must be unique")
        known = set(requirement_ids)
        for option in self.options:
            if len(set(option.required_evidence)) != len(option.required_evidence):
                raise ValueError(
                    f"business decision option {option.id!r} repeats evidence ids"
                )
            unknown = set(option.required_evidence) - known
            if unknown:
                raise ValueError(
                    f"business decision option {option.id!r} names unknown evidence"
                )
        if not any(
            (predicate.kind is PredicateKind.TEXT_PRESENT and bool(predicate.text))
            or (
                predicate.kind is PredicateKind.ANCHOR_RESOLVES
                and predicate.anchor is not None
            )
            for predicate in self.revalidation
        ):
            raise ValueError(
                "business decision revalidation requires an affirmative live "
                "frame predicate (TEXT_PRESENT or ANCHOR_RESOLVES); parameters, "
                "the retained answer, absence checks, negation, and tautological "
                "boolean expressions cannot authorize continuation"
            )
        return self

    def contract_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def business_decision_transition_guard(
    spec: BusinessDecisionSpec,
    option: BusinessDecisionOption,
) -> Predicate:
    """Return the exact guard that binds one answer to one live branch.

    The parameter equality proves which finite answer was retained.  The
    required predicates re-check the current application state immediately
    before the successor is selected.  They are not identity or effect proof;
    the successor action still runs those gates itself.
    """

    answer = Predicate(
        kind=PredicateKind.PARAM_EQUALS,
        param=spec.output_param,
        value=option.value,
        intent=f"selected business decision option {option.id}",
    )
    if not spec.revalidation:
        return answer
    return Predicate(
        kind=PredicateKind.AND,
        operands=[
            answer,
            *(predicate.model_copy(deep=True) for predicate in spec.revalidation),
        ],
        intent="business decision answer and live state still agree",
    )


def business_decision_transitions(
    spec: BusinessDecisionSpec,
) -> list[Transition]:
    """Build the only transition shape admitted for a business decision."""

    return [
        Transition(
            guard=business_decision_transition_guard(spec, option),
            target=option.target,
            label=option.label,
        )
        for option in spec.options
    ]


class Relation(BaseModel):
    """A worklist a ``loop`` state iterates over (RFC §2.3).

    Variable-length ``rows``; each row is a mapping of param name -> value that
    is bound into the run params in scope for that loop iteration. Rows may be
    INLINED here (deterministic, $0 -- the authored/compiled case) or supplied
    at run time (``Replayer.run(worklists=...)``) for a genuinely data-dependent
    queue whose length is unknown until run time. Either way iteration stays
    BOUNDED (see :class:`LoopSpec.max_iterations`).
    """

    name: str
    rows: list[dict[str, str]] = Field(
        default_factory=list,
        description="Inline worklist rows; each binds params for one iteration",
    )
    description: str = ""


class LoopSpec(BaseModel):
    """The body of a ``loop`` state (RFC §2.3; Rousillon / Helena / WebRobot).

    Binds a ``relation`` (worklist) and a ``body`` subflow that runs ONCE PER
    ROW, the row's fields merged into the run params for that iteration (so an
    ``entity_ref`` param re-resolves by the identity ladder each pass --
    iteration N acts on the RIGHT row, not a recorded pixel position). A
    zero-row worklist runs the body ZERO times. Iteration is BOUNDED by
    ``max_iterations`` -- a worklist longer than the bound HALTs (fail-safe),
    never runs unbounded.
    """

    relation: str = Field(description="Name of the Relation / worklist to loop")
    body: str = Field(description="SubflowId run once per row")
    var: str = Field(
        default="",
        description="Optional human label for the loop variable (for reports)",
    )
    max_iterations: int = Field(
        default=1000, description="Hard upper bound on iterations (fail-safe)"
    )


class State(BaseModel):
    """A node in the workflow-program graph (RFC §2.2).

    Its ``kind`` selects the payload: ``action`` carries a hardened Phase-1
    :class:`Step`; ``branch`` picks an edge purely by guard;
    ``business_decision`` binds one authorized finite human choice; ``loop``
    iterates a worklist; ``subflow_call`` invokes a reusable subgraph;
    ``terminal`` ends the (sub)graph. ``transitions`` are the outgoing edges
    (empty on a terminal, a single unconditional edge on a degenerate linear
    node). ``on_exception`` routes a FAILED action to a local handler instead
    of aborting the whole run.
    """

    id: str
    kind: StateKind
    # kind == ACTION: the hardened Phase-1 Step to perform (unchanged leaf --
    # anchor resolution, identity gate, effects, risk all ride along on it).
    step: Optional[Step] = None
    # kind == BUSINESS_DECISION: a finite authorized human answer.  The
    # decision is control input only; it never supplies identity/effect proof.
    decision: Optional[BusinessDecisionSpec] = None
    # kind == LOOP: the worklist + per-row body subflow.
    loop: Optional[LoopSpec] = None
    # kind == SUBFLOW_CALL: the reusable subflow to invoke, then continue.
    subflow: Optional[str] = None
    # Outgoing edges, evaluated IN ORDER (first matching guard wins). Empty on a
    # terminal; a single unconditional Transition on a degenerate linear node.
    transitions: list[Transition] = Field(default_factory=list)
    # Local exception handler (RFC §2.4): when this state's action FAILS (a
    # resolution / identity / postcondition / effect HALT), route to THIS state
    # instead of aborting the whole run -- the graph analog of try/except. None
    # (default) => an unhandled failure HALTs the run, exactly as today.
    on_exception: Optional[str] = None
    # kind == TERMINAL: how this (sub)graph ends. "success" completes normally
    # (returns to the caller for a subflow); "halt" / "escalate" stop the ENTIRE
    # run (success=False) -- the safe default for an underdetermined/failed path.
    outcome: Optional[Literal["success", "halt", "escalate"]] = None
    reason: str = ""

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Do not change the bytes of program states that predate decisions."""

        data: dict[str, Any] = handler(self)
        if self.decision is None:
            data.pop("decision", None)
        return data


class ProgramGraph(BaseModel):
    """A directed graph of :class:`State`s with a single ``entry`` (RFC §2.2).

    Used both as the top-level program (``Workflow.program``) and as a reusable
    subflow (``Workflow.subflows[name]``, or a ``loop`` body). Walked state by
    state from ``entry`` until a terminal (or, for a subflow, until it falls off
    / reaches a ``success`` terminal, which RETURNS to the caller).
    """

    entry: str
    states: dict[str, State] = Field(default_factory=dict)


def lift_to_program(workflow: "Workflow") -> ProgramGraph:
    """Mechanically lift a linear ``Workflow`` to the degenerate straight-line
    program (RFC §2.6): each ``Step[i]`` becomes an ``action`` State with a
    single unconditional ``Transition`` to ``Step[i+1]``, and a final ``success``
    terminal. The graph interpreter over this lift replays byte-for-byte
    identically to the linear ``Replayer`` -- the proof that "a linear bundle is
    the degenerate single-path graph".
    """
    states: dict[str, State] = {}
    steps = workflow.steps
    end_id = "__end__"
    for i, step in enumerate(steps):
        sid = f"s::{step.id}"
        target = end_id if i + 1 >= len(steps) else f"s::{steps[i + 1].id}"
        states[sid] = State(
            id=sid,
            kind=StateKind.ACTION,
            step=step,
            transitions=[Transition(target=target, label="")],
        )
    states[end_id] = State(id=end_id, kind=StateKind.TERMINAL, outcome="success")
    entry = f"s::{steps[0].id}" if steps else end_id
    return ProgramGraph(entry=entry, states=states)


class GovernedAuthorizationParameter(BaseModel):
    """One value-free parameter declaration in a governed template."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    type: ParamKind
    required: bool
    secret: bool
    has_default: bool
    choices_count: int = Field(ge=0)
    choices_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class GovernedAuthorizationTemplate(BaseModel):
    """Immutable production authorization shape for one certified bundle.

    This is not a bearer capability.  It deliberately excludes runtime input
    values, approval identity, credentials, and effect or identity recipes.
    The template lives in manifest provenance so it can commit to the final
    content digest without creating a content-digest cycle.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["openadapt.governed-authorization-template/v1"] = (
        "openadapt.governed-authorization-template/v1"
    )
    bundle_content_digest: str = Field(pattern="^[a-f0-9]{64}$")
    workflow_contract_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    qualification_project_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    qualification_project_revision: int = Field(ge=1)
    qualification_project_contract_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    qualification_environment_contract_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    qualification_report_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    qualification_case_evidence_contract_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    policy_name: str = Field(min_length=1, max_length=128)
    policy_contract_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    execution_profile: Literal["standard", "regulated"]
    minimum_effect_tier: int = Field(ge=1, le=4)
    qualified_effect_requirements: tuple["QualifiedEffectRequirement", ...] = ()
    required_identity_step_ids: tuple[str, ...] = Field(default_factory=tuple)
    identity_contract_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    parameters: tuple[GovernedAuthorizationParameter, ...] = ()
    parameter_contract_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    template_sha256: str = Field(pattern="^[a-f0-9]{64}$")

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"template_sha256"})

    def computed_sha256(self) -> str:
        raw = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @model_validator(mode="after")
    def _exact_hash_and_order(self) -> "GovernedAuthorizationTemplate":
        if self.template_sha256 != self.computed_sha256():
            raise ValueError("governed authorization template hash does not match")
        if self.required_identity_step_ids != tuple(
            sorted(self.required_identity_step_ids)
        ):
            raise ValueError(
                "governed authorization template identity steps must be ordered"
            )
        if len(self.required_identity_step_ids) != len(
            set(self.required_identity_step_ids)
        ):
            raise ValueError(
                "governed authorization template identity steps must be unique"
            )
        parameter_names = tuple(item.name for item in self.parameters)
        if parameter_names != tuple(sorted(parameter_names)):
            raise ValueError(
                "governed authorization template parameters must be ordered"
            )
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError(
                "governed authorization template parameters must be unique"
            )
        requirements = tuple(
            (item.step_id, item.actuation_path, item.effect_index)
            for item in self.qualified_effect_requirements
        )
        if requirements != tuple(sorted(requirements)) or len(requirements) != len(
            set(requirements)
        ):
            raise ValueError("governed authorization template effects must be ordered")
        return self

    @classmethod
    def create(cls, **values: Any) -> "GovernedAuthorizationTemplate":
        """Create a template with its canonical self-hash."""

        candidate = cls.model_construct(**values, template_sha256="0" * 64)
        return cls.model_validate(
            {
                **candidate.canonical_payload(),
                "template_sha256": candidate.computed_sha256(),
            }
        )


class BundleProvenance(BaseModel):
    """Who produced a bundle and, if certified, under what policy (schema v2).

    ``compiler_version`` records the ``openadapt_flow`` version that compiled /
    last saved the bundle, so an operator inventory can tell which compiler an
    artifact came from. The certification block is populated only for a bundle
    that passed a policy certification (see :meth:`Workflow.stamp_certification`
    / ``openadapt_flow.policy.evaluate_policy``): ``policy_name`` is the policy
    it was certified against, ``certification_status`` is a short label
    (``"certified"`` / ``"failed"`` / ``"expired"``), and ``expires_at`` is an
    optional ISO expiry after which a consumer should re-certify. An
    uncertified bundle leaves the block at its defaults.
    """

    compiler_version: str = Field(
        default="", description="openadapt_flow version that produced the bundle"
    )
    source_recording_sha256: Optional[str] = Field(
        default=None,
        description="Exact approved sanitized recording archive used for compilation",
        pattern="^[a-f0-9]{64}$",
    )
    compiler_config_sha256: Optional[str] = Field(
        default=None,
        description="Canonical digest of the compiler options used for this bundle",
        pattern="^[a-f0-9]{64}$",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="When this manifest/provenance was first sealed (ISO 8601)",
    )
    policy_name: Optional[str] = Field(
        default=None,
        description="Certified bundle: the policy it was certified against",
    )
    certified: bool = Field(
        default=False, description="Whether the bundle passed policy certification"
    )
    certification_status: Optional[str] = Field(
        default=None,
        description="Short label: 'certified' | 'failed' | 'expired' | None",
    )
    certified_at: Optional[str] = Field(
        default=None, description="ISO timestamp of the certification, if any"
    )
    expires_at: Optional[str] = Field(
        default=None,
        description="Optional ISO expiry; a consumer should re-certify after it",
    )
    certification_invalidated_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO timestamp when a previously persisted certification stopped "
            "matching this bundle contract"
        ),
    )
    certification_invalidation_reason: Optional[str] = Field(
        default=None,
        description=(
            "PHI-free reason the persisted certification was invalidated; "
            "the bundle must be certified again before production use"
        ),
    )
    governed_authorization_template: Optional["GovernedAuthorizationTemplate"] = Field(
        default=None,
        description=(
            "Value-free, hash-bound production authorization template for this "
            "exact certified bundle"
        ),
    )


class BundleManifest(BaseModel):
    """Integrity + provenance manifest for a compiled bundle (schema v2).

    Sealed on :meth:`Workflow.save` and re-verified on :meth:`Workflow.load`
    (``openadapt_flow.bundle_validation``): ``file_hashes`` is a SHA-256 per
    template/image asset (bundle-relative path -> hex digest), ``content_digest``
    is a whole-bundle SHA-256 over the manifest-free ``workflow.json`` content
    AND those asset hashes (so it changes if any semantic byte changes), and
    ``provenance`` carries the compiler version + certification block. ``encrypted``
    mirrors ``Workflow.encrypted``: True when the bundle is sealed at rest with
    AES-256-GCM -- both ``workflow.json`` and every ``templates/*.png`` crop.
    The ``file_hashes`` are always digests over the PLAINTEXT asset (sealed
    BEFORE encryption), so integrity re-verifies against the decrypted crops.
    Additive: a v1 bundle carries no manifest and one is computed on read.
    """

    schema_version: int = SCHEMA_VERSION
    content_digest: str = Field(
        default="", description="whole-bundle SHA-256 (content + asset hashes)"
    )
    file_hashes: dict[str, str] = Field(
        default_factory=dict,
        description="bundle-relative asset path -> SHA-256 hex digest",
    )
    provenance: BundleProvenance = Field(default_factory=BundleProvenance)
    encrypted: bool = Field(
        default=False,
        description="mirrors Workflow.encrypted (workflow.json sealed at rest)",
    )


class BackendHints(BaseModel):
    """Trusted local execution target captured with a remote-display demo.

    These hints bind a window-scoped recording to the same local client window
    at replay time. They live only inside ``workflow.json``: plaintext in an
    explicitly unencrypted local bundle, or encrypted with the rest of a sealed
    bundle. They never enter the plaintext manifest or PHI-free hosted report
    rail because a window title can contain a patient or account name.

    The schema is deliberately closed to the two pixel-window substrates that
    use ``BackendConfig.rdp_*``.  Network endpoints, credentials, arbitrary
    backend configuration, and provider-specific recipes are not recording
    metadata and must still come from the deployment config.
    """

    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    backend: Literal["rdp", "citrix"]
    rdp_window: Optional[str] = Field(default=None, min_length=1, max_length=512)
    rdp_window_title: Optional[str] = Field(default=None, min_length=1, max_length=512)
    rdp_readiness_text: Optional[str] = Field(
        default=None, min_length=1, max_length=512
    )


# -- template-asset sealing (PHI-at-rest: image crops) -----------------------
#
# When a bundle is encrypted (``Workflow.save(encrypt=True)``), the ``templates/``
# PNG crops -- pixels of the recorded (patient) screen, i.e. image PHI -- are
# sealed with the SAME AES-256-GCM AEAD as ``workflow.json``, under the distinct
# :data:`TEMPLATE_AAD` domain. Each crop ``templates/<name>.png`` is written as
# ``templates/<name>.png.enc`` and its plaintext removed, so an encrypted bundle
# leaves NO cleartext PHI-bearing screenshot on disk. Integrity digests stay over
# the PLAINTEXT crop (sealed into the manifest before encryption), so a decrypted
# load re-verifies end-to-end (see docs/phi_at_rest.md).


def _iter_plaintext_templates(bundle: Path) -> Iterator[Path]:
    """Every regular, NON-sealed file under ``<bundle>/templates`` (recursive)."""
    tdir = bundle / "templates"
    if not tdir.is_dir():
        return
    for p in sorted(tdir.rglob("*")):
        if p.is_file() and p.suffix != ".enc":
            yield p


def _seal_template_assets(
    bundle: Path, key: Optional[str], store: dict[str, bytes]
) -> None:
    """Seal every plaintext ``templates/`` crop with AES-256-GCM under
    :data:`TEMPLATE_AAD`, writing ``<crop>.enc`` and REMOVING the plaintext.

    The plaintext bytes are also cached into ``store`` (the workflow's in-memory
    template map) so the sealing workflow object still carries the crops -- a
    later plaintext re-save can recover them, mirroring an encrypted ``load``.
    A missing key raises ``crypto.MissingKeyError`` (never a silent skip)."""
    from openadapt_flow import crypto as _crypto

    for path in list(_iter_plaintext_templates(bundle)):
        rel = path.relative_to(bundle).as_posix()
        data = path.read_bytes()
        store[rel] = data
        (bundle / f"{rel}.enc").write_bytes(
            _crypto.encrypt_bytes(data, key, aad=TEMPLATE_AAD)
        )
        path.unlink()


def _decrypt_template_assets(bundle: Path, key: Optional[str]) -> dict[str, bytes]:
    """Decrypt every sealed ``templates/*.enc`` crop IN MEMORY, keyed by the
    plaintext bundle-relative path (``templates/<name>.png``).

    A wrong/missing key or a tampered ciphertext fails LOUD via
    ``crypto.DecryptionError`` / ``crypto.MissingKeyError`` (the AEAD tag),
    exactly as the ``workflow.json`` path does -- no partial materialization."""
    from openadapt_flow import crypto as _crypto

    out: dict[str, bytes] = {}
    tdir = bundle / "templates"
    if not tdir.is_dir():
        return out
    for path in sorted(tdir.rglob("*.enc")):
        if not path.is_file():
            continue
        plaintext = _crypto.decrypt_bytes(path.read_bytes(), key, aad=TEMPLATE_AAD)
        rel = path.relative_to(bundle).as_posix()[: -len(".enc")]
        out[rel] = plaintext
    return out


def _verify_sealed_template_integrity(
    workflow: "Workflow", stored: "BundleManifest", decrypted: dict[str, bytes]
) -> None:
    """Integrity check for an ENCRYPTED bundle, run against the DECRYPTED crops
    in memory (the on-disk assets are ciphertext, so the disk-based
    ``bundle_validation.verify_integrity`` cannot be used directly).

    Two checks mirroring the plaintext path: (1) the workflow content still
    hashes to the sealed ``content_digest`` over the SEALED plaintext asset
    hashes, and (2) every sealed asset's decrypted plaintext still hashes to its
    recorded digest. Raises ``bundle_validation.BundleIntegrityError`` on any
    mismatch. Skipped for a bundle with no sealed digest."""
    from openadapt_flow import bundle_validation as _bv

    if not stored.content_digest:
        return
    recomputed = _bv.compute_content_digest(workflow, stored.file_hashes)
    if recomputed != stored.content_digest:
        raise _bv.BundleIntegrityError(
            "bundle content digest mismatch on decrypt: expected "
            f"{stored.content_digest[:16]}..., recomputed {recomputed[:16]}... "
            "-- the workflow.json was modified after the manifest was sealed"
        )
    for rel, expected in stored.file_hashes.items():
        data = decrypted.get(rel)
        if data is None:
            raise _bv.BundleIntegrityError(
                f"manifest lists sealed asset {rel!r} but its ciphertext "
                f"({rel}.enc) is missing from the bundle"
            )
        if hashlib.sha256(data).hexdigest() != expected:
            raise _bv.BundleIntegrityError(
                f"sealed asset {rel!r} plaintext hash mismatch (tampered or corrupted)"
            )


class Workflow(BaseModel):
    """Compiled workflow program with linear compatibility and graph execution.

    ``steps`` preserves the original straight-line bundle format. ``program``,
    when present, is the canonical state machine; its action states reuse the
    same identity, risk, resolution, postcondition, and effect contracts.
    """

    schema_version: int = SCHEMA_VERSION
    name: str
    recording_id: Optional[str] = None
    # -- PHI governance manifest (PHI audit REM-1) --------------------------
    # A compiled bundle is a HIPAA-designated record; these fields let an
    # operator's compliance inventory classify it, and let the pre-commit / CI
    # guard (scripts/check_bundle_phi.py) block a bundle that still carries
    # plaintext identifiers from reaching git.
    #
    # ``contains_phi``: True when this bundle still carries a PLAINTEXT identity
    # band (``anchor.context_text`` / ``structured_identity``) — the flagship
    # PHI-at-rest leak (GAP-1a). PHI-free bundles store a salted-hash
    # ``identity_template`` instead and set this False. (It does NOT certify the
    # absence of every identifier in every free-text postcondition — that needs
    # the optional Presidio pass; see ``phi_scrubbed``.)
    contains_phi: bool = False
    # ``phi_scrubbed``: True when the optional openadapt-privacy (Presidio) pass
    # was ACTIVE on the compile path, so identifier-bearing TEXT_PRESENT
    # postconditions were dropped. False = the scrub was unavailable/off (the
    # bundle may retain identifier text in postconditions / labels).
    phi_scrubbed: bool = False
    # ``encrypted``: True when this bundle is sealed at rest with AES-256-GCM
    # (``save(encrypt=True)``; see openadapt_flow.crypto and docs/phi_at_rest.md).
    # BOTH the ``workflow.json`` (-> ``workflow.json.enc``, BUNDLE_AAD) AND every
    # ``templates/*.png`` image crop (-> ``templates/*.png.enc``, TEMPLATE_AAD)
    # are sealed, so an encrypted bundle leaves NO cleartext PHI -- neither the
    # identity band nor the screenshot pixels -- on disk. False (default) = the
    # plaintext path, protected by the governance guards and the operator's disk
    # encryption. Sealed INTO the integrity digest, so a decrypt at load
    # re-verifies against this value.
    encrypted: bool = False
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    viewport: Optional[tuple[int, int]] = None
    # Local-only remote-display target captured with the demonstration.
    # ``BackendHints`` can contain a PHI-bearing window title, so it is sealed
    # inside encrypted workflow.json and is never mirrored into manifest.json
    # or a hosted run summary. Empty for browser/native bundles, preserving
    # their serialized form through the compatibility serializer below.
    backend_hints: Optional[BackendHints] = None
    # -- surface binding (roadmap Section 5) --------------------------------
    # The exact execution surface this workflow was recorded/qualified on and
    # the execution mode that surface implies. A bound workflow refuses to run
    # on a different surface unless the operator passes an explicit override
    # that records itself in the run report (``RunReport.surface_override``).
    # None on every pre-Section-5 bundle (no binding, legacy behavior); both
    # fields are omitted from serialized bytes when None so legacy bundles
    # round-trip byte-for-byte.
    surface: Optional[ExecutionTargetKind] = None
    execution_mode: Optional[ExecutionMode] = None
    params: dict[str, str] = Field(
        default_factory=dict, description="param name -> example/default value"
    )
    # Workflow-program IR, Phase 1 (RFC §2.2, §6): TYPED parameter specs, ADDITIVE
    # alongside the frozen ``params`` dict above. Keyed by param name. Empty by
    # default, so a v0 bundle is unaffected; when present, the replayer folds each
    # spec's ``example`` in as a default and fails fast on a missing required one.
    param_specs: dict[str, "ParamSpec"] = Field(default_factory=dict)
    secret_params: list[str] = Field(
        default_factory=list,
        description=(
            "Names of SECRET parameters (e.g. passwords). Their values are"
            " NEVER stored here or in ``params``; each is injected at replay"
            " from OPENADAPT_FLOW_SECRET_<PARAM> (see Step.secret)."
        ),
    )
    steps: list[Step] = Field(default_factory=list)
    # KNOWN recurring reversible, non-consequential interstitials the runtime
    # detects (model-free) at EACH step's entry and either handles through an
    # audited dismissal + declared visual clearance check, or HALTs on
    # gracefully (docs/LIMITS.md "state dependency"). Empty by default, so a
    # bundle that declares none behaves exactly as before. An operator can also
    # supply extra interstitials at run time (Replayer(interstitials=...))
    # WITHOUT recompiling; governed runs bind their full declarations.
    interstitials: list["Interstitial"] = Field(default_factory=list)
    # Workflow-program IR, Phase 2 (RFC §2): the parameterized STATE MACHINE.
    # ALL optional and additive -- when ``program`` is None the runtime executes
    # the linear ``steps`` loop above byte-for-byte (today's behavior); a linear
    # bundle carries none of these. When ``program`` is present the runtime
    # interprets the graph (loops / branches / subflows / exception paths),
    # reusing the SAME per-action machinery (identity/effect/risk/heal gates) for
    # every ``action`` state. ``subflows`` are reusable named subgraphs (a loop
    # body or a shared component); ``data_sources`` are the worklists loops
    # iterate. See ``lift_to_program`` for the degenerate linear lift (RFC §2.6).
    program: Optional["ProgramGraph"] = None
    subflows: dict[str, "ProgramGraph"] = Field(default_factory=dict)
    data_sources: dict[str, "Relation"] = Field(default_factory=dict)
    # Versioned qualification intent, coverage policy, and case evidence.  The
    # executable graph/steps/effects above remain canonical; this project
    # references them rather than copying a second workflow representation.
    # None for every pre-qualification bundle and omitted from its serialized
    # bytes by ``_serialize_compatible`` below.
    qualification: Optional["QualificationProject"] = None
    # -- schema v2 integrity + provenance manifest --------------------------
    # Sealed on ``save`` (per-asset hashes, a whole-bundle content digest, the
    # compiler version, and -- for a certified bundle -- the certifying policy +
    # status + optional expiry) and re-verified on ``load``. Additive and
    # backward-compatible: a v1 bundle carries no manifest, so one is computed
    # on read (see ``openadapt_flow.bundle_validation``). Excluded from the
    # content digest itself (the digest lives INSIDE it).
    manifest: Optional["BundleManifest"] = None

    # In-memory plaintext of the bundle's sealed ``templates/`` crops, keyed by
    # bundle-relative path (``templates/<name>.png`` -> PNG bytes). Populated on
    # ``load(key=...)`` of an ENCRYPTED bundle (the crops are decrypted here, in
    # memory, never written back as cleartext) and on ``save(encrypt=True)`` (so
    # the sealing object retains the crops for a later plaintext re-save). Empty
    # for a plaintext bundle, whose crops are read from disk as before. Excluded
    # from ``model_dump`` / the content digest (a private attribute). The
    # resolver consumes a decrypted crop via :meth:`decrypted_template`.
    _decrypted_templates: dict[str, bytes] = PrivateAttr(default_factory=dict)

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Omit empty additive execution hints from legacy bundle bytes."""
        data: dict[str, Any] = handler(self)
        if self.backend_hints is None:
            data.pop("backend_hints", None)
        if self.surface is None:
            data.pop("surface", None)
        if self.execution_mode is None:
            data.pop("execution_mode", None)
        if self.qualification is None:
            data.pop("qualification", None)
        return data

    # -- bundle I/O ---------------------------------------------------------

    def decrypted_template(self, rel: str) -> Optional[bytes]:
        """Return the in-memory plaintext PNG bytes for a sealed crop at bundle-
        relative path ``rel`` (e.g. ``anchor.template``), or None when the bundle
        is not encrypted / the crop was not sealed.

        The consumption seam the resolver uses for an encrypted bundle: instead
        of reading ``<bundle>/templates/<name>.png`` from disk (which does not
        exist -- only the ``.enc`` ciphertext does), it pulls the crop that
        ``load(key=...)`` already decrypted in memory."""
        return self._decrypted_templates.get(rel)

    def decrypted_templates(self) -> dict[str, bytes]:
        """A copy of the full in-memory decrypted-crop map (bundle-relative path
        -> PNG bytes); empty for a plaintext bundle."""
        return dict(self._decrypted_templates)

    def _sync_disk_templates(self, bundle: Path) -> None:
        """Materialize any in-memory plaintext crops to disk (removing a stale
        ``.enc`` sibling) BEFORE the manifest is (re)sealed, so a re-save hashes
        the plaintext crop and a plaintext re-save recovers the PNGs a prior
        encrypted save removed from disk. A no-op for a freshly-compiled bundle
        (no in-memory crops; the compiler already wrote the plaintext PNGs)."""
        for rel, data in self._decrypted_templates.items():
            path = bundle / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            enc = bundle / f"{rel}.enc"
            if enc.exists():
                enc.unlink()

    def save(
        self,
        bundle_dir: Path | str,
        *,
        seal_manifest: bool = True,
        encrypt: bool = False,
        key: Optional[str] = None,
    ) -> Path:
        """Write workflow.json into bundle_dir (templates are written by the
        compiler / healer, which own the crop images).

        Schema v2: unless ``seal_manifest=False``, (re)computes and seals the
        integrity/provenance manifest (per-asset hashes + whole-bundle content
        digest + compiler version, carrying over any prior certification) and
        also writes it to a standalone ``manifest.json`` sidecar for external
        tooling. The ``schema_version`` is bumped to the current version.

        Encryption-at-rest (opt-in, OFF by default): when ``encrypt=True`` (or a
        ``key`` is supplied), the serialized ``workflow.json`` is sealed with
        AES-256-GCM (``openadapt_flow.crypto``) and written as
        ``workflow.json.enc`` instead of plaintext ``workflow.json``, AND every
        ``templates/*.png`` image crop -- pixels of the recorded screen, i.e.
        image PHI -- is sealed the same way (under the distinct
        :data:`TEMPLATE_AAD` domain) as ``templates/*.png.enc`` with its
        plaintext removed, so an encrypted bundle leaves NO cleartext
        PHI-bearing screenshot on disk. The passphrase comes from ``key`` or the
        ``OPENADAPT_BUNDLE_KEY`` environment variable (a missing key raises
        ``crypto.MissingKeyError`` -- an encrypt request never silently degrades
        to plaintext). The integrity manifest is sealed over the PLAINTEXT
        content (workflow AND crop digests) BEFORE encryption, so an encrypted
        bundle keeps every schema-v2 guarantee (content digest, asset hashes,
        provenance) once decrypted at load. The ``manifest.json`` sidecar stays
        plaintext (it carries only hashes + provenance, no PHI) so a compliance
        inventory can read ``encrypted: true`` without the key. When
        ``encrypt=False`` and no key is given, behavior is unchanged: a plaintext
        ``workflow.json`` and plaintext ``templates/*.png`` crops are written
        exactly as before.

        Returns the path actually written (``workflow.json`` or, when encrypted,
        ``workflow.json.enc``).
        """
        do_encrypt = encrypt or key is not None
        bundle = Path(bundle_dir)
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "templates").mkdir(exist_ok=True)
        # Restore any in-memory crops (from a prior encrypted load/save) to disk
        # as plaintext BEFORE the manifest hashes them; harmless no-op for a
        # freshly-compiled bundle whose crops are already on disk.
        self._sync_disk_templates(bundle)
        if self.schema_version < SCHEMA_VERSION:
            self.schema_version = SCHEMA_VERSION
        # Reflect the at-rest state in the workflow BEFORE the manifest is sealed,
        # so the sealed content digest (and the mirrored manifest.encrypted flag)
        # cover the true value and integrity re-verifies after a decrypt.
        self.encrypted = do_encrypt
        if seal_manifest:
            from openadapt_flow import bundle_validation as _bv

            self.manifest = _bv.build_manifest(self, bundle)
        serialized = self.model_dump_json(indent=2)
        plaintext_path = bundle / "workflow.json"
        encrypted_path = bundle / "workflow.json.enc"
        if do_encrypt:
            from openadapt_flow import crypto as _crypto

            encrypted_path.write_bytes(
                _crypto.encrypt_bytes(
                    serialized.encode("utf-8"), key, aad=_crypto.BUNDLE_AAD
                )
            )
            # Never leave a stale plaintext copy alongside the ciphertext.
            if plaintext_path.exists():
                plaintext_path.unlink()
            # Seal the image crops too (the manifest already hashed their
            # plaintext just above), so no cleartext PHI-bearing screenshot is
            # left on disk. Caches the plaintext into ``_decrypted_templates``.
            _seal_template_assets(bundle, key, self._decrypted_templates)
            path = encrypted_path
        else:
            plaintext_path.write_text(serialized)
            if encrypted_path.exists():
                encrypted_path.unlink()
            path = plaintext_path
        if self.manifest is not None:
            (bundle / "manifest.json").write_text(
                self.manifest.model_dump_json(indent=2)
            )
        return path

    @classmethod
    def load(
        cls,
        bundle_dir: Path | str,
        *,
        validate: bool = True,
        verify_integrity: bool = True,
        key: Optional[str] = None,
    ) -> "Workflow":
        """Load a bundle, migrating v1 -> v2, validating structure, and (for a
        v2 bundle carrying a sealed digest) verifying integrity.

        - ``validate`` (default True): reject a structurally MALFORMED bundle
          via ``bundle_validation.validate_workflow`` (missing entry, dangling
          transition/handler target, kind/payload mismatch, missing subflow,
          duplicate id, unreachable terminal, unsafe unconditional cycle). Only
          the *structural* category raises here; the effect-verification safety
          finding is surfaced by lint/certify, not the load path, so an existing
          uncertified-but-well-formed bundle still loads.
        - ``verify_integrity`` (default True): if the bundle carries a sealed
          manifest digest, recompute it and reject a tampered bundle. A legacy
          (pre-v2) bundle has no sealed digest, so its manifest is computed
          fresh and nothing is rejected.
        - ``key`` (default None): decryption passphrase for an ENCRYPTED bundle
          (one saved with ``save(encrypt=True)``, present on disk as
          ``workflow.json.enc`` + ``templates/*.png.enc``). Resolved from ``key``
          or the ``OPENADAPT_BUNDLE_KEY`` environment variable. It decrypts BOTH
          the ``workflow.json`` AND every sealed image crop IN MEMORY (the crops
          are exposed to the resolver via :meth:`decrypted_template`, never
          rewritten as cleartext on disk). A wrong/missing key fails LOUDLY
          (``crypto.MissingKeyError`` / ``crypto.DecryptionError``) with no
          partial load; the AEAD tag also catches a tampered ciphertext (of the
          workflow OR a crop). Ignored for a plaintext bundle. Integrity +
          structural validation then run on the decrypted content exactly as for
          a plaintext bundle.
        """
        bundle = Path(bundle_dir)
        from openadapt_flow import bundle_validation as _bv

        encrypted_path = bundle / "workflow.json.enc"
        plaintext_path = bundle / "workflow.json"
        bundle_encrypted = encrypted_path.is_file()
        if bundle_encrypted:
            from openadapt_flow import crypto as _crypto

            decrypted = _crypto.decrypt_bytes(
                encrypted_path.read_bytes(), key, aad=_crypto.BUNDLE_AAD
            )
            raw = json.loads(decrypted)
        else:
            raw = json.loads(plaintext_path.read_text())
        raw = _bv.migrate_bundle_dict(raw)
        # A manifest may be embedded in workflow.json OR sit in a sidecar; the
        # embedded one wins, else the sidecar, else it is computed fresh.
        persisted = raw.get("manifest")
        wf = cls.model_validate(raw)

        if wf.manifest is None:
            sidecar = bundle / "manifest.json"
            if sidecar.is_file():
                wf.manifest = BundleManifest.model_validate_json(sidecar.read_text())
                persisted = wf.manifest

        if bundle_encrypted:
            # Decrypt the sealed image crops IN MEMORY (fail-loud on wrong key /
            # tamper, exactly as the workflow.json above). The resolver reads
            # them via ``decrypted_template``; nothing cleartext lands on disk.
            wf._decrypted_templates = _decrypt_template_assets(bundle, key)

        if verify_integrity and persisted is not None and wf.manifest is not None:
            if bundle_encrypted:
                # On-disk crops are ciphertext, so verify the sealed asset
                # digests against the decrypted plaintext held in memory.
                _verify_sealed_template_integrity(
                    wf, wf.manifest, wf._decrypted_templates
                )
            else:
                _bv.verify_integrity(wf, bundle, wf.manifest)
            template = wf.manifest.provenance.governed_authorization_template
            if template is not None:
                # Manifest provenance is intentionally outside the content
                # digest.  Rebuild this derived authority from the sealed
                # workflow and accepted certification so a replacement
                # self-consistent template cannot weaken production policy.
                from openadapt_flow.qualification import (
                    QualificationError,
                    build_governed_authorization_template,
                )

                try:
                    expected_template = build_governed_authorization_template(wf)
                except QualificationError as exc:
                    raise _bv.BundleIntegrityError(
                        "governed authorization template cannot be reproduced "
                        "from the sealed certification"
                    ) from exc
                if template != expected_template:
                    raise _bv.BundleIntegrityError(
                        "governed authorization template does not match the "
                        "sealed workflow and certification"
                    )

        if wf.manifest is None:
            wf.manifest = _bv.build_manifest(wf, bundle)

        if validate:
            report = _bv.validate_workflow(wf)
            report.raise_if(categories=("structure",))

        return wf

    def stamp_certification(
        self,
        policy_name: str,
        passed: bool,
        *,
        expires_at: Optional[str] = None,
        status: Optional[str] = None,
    ) -> "BundleManifest":
        """Record a policy-certification result in the bundle manifest (v2).

        Ensures a manifest exists and sets its provenance certification block:
        the certifying ``policy_name``, whether it ``passed``, a short status
        label, the certification timestamp, and an optional ISO ``expires_at``.
        Persisted on the next :meth:`save`. Returns the manifest for convenience.
        """
        if self.manifest is None:
            self.manifest = BundleManifest()
        prov = self.manifest.provenance
        prov.policy_name = policy_name
        prov.certified = passed
        prov.certification_status = status or ("certified" if passed else "failed")
        prov.certified_at = datetime.now(timezone.utc).isoformat()
        prov.expires_at = expires_at
        prov.certification_invalidated_at = None
        prov.certification_invalidation_reason = None
        return self.manifest


# -- runtime results ---------------------------------------------------------

Rung = Literal[
    "structural", "template", "template_global", "ocr", "geometry", "grounder"
]


class StructuralHandle(BaseModel):
    """Result of a backend's structural locate: the resolved element's point.

    ``point`` is the element's center in the SAME coordinate space as
    :meth:`openadapt_flow.backend.Backend.click` (the pixels the resolver
    emits), so a structurally-resolved target flows through the IDENTICAL click
    path -- the pre-click identity gate and the irreversible risk gate still
    fire (structural resolution makes identity STRONGER, an exact element; it
    never bypasses it). ``confidence`` is 1.0 for a deterministic exact locate.
    """

    point: Point
    confidence: float = 1.0
    region: Optional[Region] = Field(
        default=None,
        description=(
            "Exact live element rectangle as (x, y, width, height), in the "
            "same coordinate space as point. Runtime input verification uses "
            "this instead of a fixed crop around the element center so wide "
            "text fields are observed in full."
        ),
    )
    target_fingerprint: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Opaque SHA-256 fingerprint of the unique structural candidate. "
            "A native action re-resolves the locator and requires this exact "
            "fingerprint, closing the resolve/act stale-target gap."
        ),
    )
    candidate_count: Literal[1] = 1
    supported_operations: list[str] = Field(default_factory=list, max_length=16)


class Resolution(BaseModel):
    rung: Rung
    point: Point
    confidence: float
    elapsed_ms: float
    structural_handle: Optional[StructuralHandle] = None
    visual_evidence: Optional["VisualResolutionEvidence"] = None
    resolution_evidence_regions: tuple[Region, ...] = Field(
        default_factory=tuple,
        exclude=True,
        max_length=128,
        description=(
            "Live pixel regions whose content and candidate set established this "
            "resolution. Remote comparison masks must not overlap them before input."
        ),
    )

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.visual_evidence is None:
            data.pop("visual_evidence", None)
        return data


class VisualResolutionEvidence(BaseModel):
    """Exact retained inputs for an independently reproducible visual resolve.

    A visual result in a JSON report is only a claim.  Qualification uses this
    record to load the exact frame and compiled template from the signed case
    evidence, re-run the shipped deterministic resolver, and compare its rung,
    point, confidence, and matched region.  The evidence remains local to the
    qualification boundary; the portable bundle carries only its hashes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_inventory_ref: str = Field(min_length=1, max_length=512)
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_inventory_ref: str = Field(min_length=1, max_length=512)
    evaluator_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    matched_region: Region
    allow_target_ocr: bool = True

    @field_validator("frame_inventory_ref", "template_inventory_ref")
    @classmethod
    def _relative_only(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("resolution evidence path must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("resolution evidence path must be run-relative")
        return path.as_posix()


class ActionDeliveryReceipt(BaseModel):
    """Proof that an action was delivered, never that its outcome happened.

    Native UIA Invoke/Focus/Toggle/Select and physical input can confirm only
    that the operating-system action API accepted the request. Business success
    remains the independent postcondition + system-of-record effect verifier's
    responsibility. ``outcome_verified`` is therefore fixed False here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["delivered"] = "delivered"
    receipt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    native: bool
    target_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    destination_fingerprint: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "DRAG only: fingerprint of the independently resolved destination. "
            "The source remains target_fingerprint."
        ),
    )
    selection_value_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "SELECT_OPTION only: SHA-256 of the exact option text dispatched "
            "without retaining the possibly sensitive value."
        ),
    )
    selection_commit_key: Optional[Literal["Enter", "Tab"]] = Field(
        default=None,
        description="SELECT_OPTION only: exact key dispatched after the value.",
    )
    delivered_at: str = Field(min_length=20, max_length=64)
    outcome_verified: Literal[False] = False

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for field in (
            "destination_fingerprint",
            "selection_value_sha256",
            "selection_commit_key",
        ):
            if data.get(field) is None:
                data.pop(field, None)
        return data


class ActionDeliveryUncertainty(BaseModel):
    """Evidence that an action may have landed but cannot be retried safely.

    This record never proves delivery or business success.  It makes the
    uncertainty explicit and records whether the runtime subsequently proved
    the complete postcondition + independent-effect contract.
    """

    status: Literal["delivery_uncertain"] = "delivery_uncertain"
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    native: bool
    target_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_at: str = Field(min_length=20, max_length=64)
    cause_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$",
    )
    retried: Literal[False] = False
    verification_attempted: bool = False
    postconditions_confirmed: Optional[bool] = None
    effects_confirmed: Optional[bool] = None
    resolved_by_contract: bool = False


class FreshActuationEvent(BaseModel):
    """PHI-free evidence for one pre-input actuation-frame mismatch.

    The runtime records only the number and geometry of changed pixels.  It
    does not retain the rejected frame or its pixel values.  ``retried`` is
    true only when no earlier input edge crossed for this workflow step and a
    bounded reacquisition remained available.
    """

    attempt: int = Field(ge=1)
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    changed_pixel_count: int = Field(ge=1)
    changed_bbox: Region
    frame_size: tuple[int, int]
    target_intersection: Optional[bool] = None
    identity_intersection: Optional[bool] = None
    retried: bool

    @model_validator(mode="after")
    def _valid_geometry(self) -> "FreshActuationEvent":
        frame_width, frame_height = self.frame_size
        x, y, width, height = self.changed_bbox
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("fresh-actuation frame size must be positive")
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError("fresh-actuation bounding box must be positive")
        if x + width > frame_width or y + height > frame_height:
            raise ValueError("fresh-actuation bounding box exceeds the frame")
        return self


class IdentitySignalEvidence(BaseModel):
    """PHI-free audit evidence for one qualified identity signal."""

    signal: IdentitySignalKeyValue
    source: Literal[
        "structured",
        "identifier_region",
        "captured_context",
        "application",
        "session",
        "workflow_state",
        "api_parameter",
    ]
    verdict: Literal["verified", "conflict", "unverifiable"]
    evidence_class: Literal[
        "application_structured_text",
        "recorded_and_live_region",
        "captured_context_ocr",
        "application_identity",
        "session_identity",
        "workflow_state_identity",
        "api_request_effect_binding",
    ]
    match: Literal["exact", "normalized"]


class PixelIdentityEvidence(BaseModel):
    """Content-addressed local proof behind a pixel identity verdict.

    The report retains only hashes and run-relative inventory references. The
    identifier pixels remain inside the customer-controlled run directory.
    Certification re-reads these exact bytes and runs the same evaluator again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    recorded_crop_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    live_crop_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_crop_inventory_ref: str
    live_crop_inventory_ref: str
    evaluator_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _inventory_is_content_addressed(self) -> "PixelIdentityEvidence":
        expected_recorded = f"private/identity-crops/{self.recorded_crop_sha256}.png"
        expected_live = f"private/identity-crops/{self.live_crop_sha256}.png"
        if self.recorded_crop_inventory_ref != expected_recorded:
            raise ValueError(
                "recorded pixel identity inventory reference is not content-addressed"
            )
        if self.live_crop_inventory_ref != expected_live:
            raise ValueError(
                "live pixel identity inventory reference is not content-addressed"
            )
        return self


class IdentityCheck(BaseModel):
    """Outcome of the pre-click target-identity check (runtime.identity).

    Attributes:
        status: ``verified`` (band matched), ``mismatch`` (band readable and
            AFFIRMATIVELY wrong — a different entity; the run must halt, never
            click), ``abstain`` (the band is readable and its name/DOB match,
            but it rests on a GLYPH-CONFUSABLE identifier OCR may have
            collapsed — a same-name/same-DOB homonym cannot be ruled out, so
            OCR cannot honestly certify SAME *or* assert DIFFERENT; the OCR
            tier defers and the ladder HALTs if no higher-fidelity tier
            verifies — the 8th wrong-patient reopening), or ``unreadable`` (OCR
            found no usable text in the live band; identity could not be
            judged). ``abstain`` and ``unreadable`` both mean "could not
            certify": the step proceeds flagged, and irreversible steps refuse.
        mode: ``signal_quorum`` evaluates independently named, qualified
            retained sources and records only bounded, non-value evidence;
            ``structured`` compares the recorded DOM/a11y identity text
            against the live structured text at the resolved point (the
            highest-fidelity tier -- no OCR ambiguity); ``pixel`` compares the
            recorded vs live identifier-crop PIXELS (catches the O/0 glyph
            collapse OCR discards, on stable renders); ``vlm`` is the optional
            local-VLM same/different veto for glyph-confusable identifiers
            under render drift; ``context`` compares against the recorded OCR
            band text (the pixel-substrate fallback); ``param`` re-anchors on
            the RUN's value for a parameter whose demo value was embedded in
            the recorded band.
        coverage: Matched fraction (context mode) or run/required ratio
            (param mode), diagnostic.
        expected: What the check looked for (recorded band text, or the
            run's param value on a param-mode mismatch).
        observed: Live band text the verdict was based on.
        param: The parameter that drove a param-mode check, if any.
    """

    status: Literal["verified", "mismatch", "abstain", "unreadable"]
    mode: Literal[
        "context",
        "param",
        "structured",
        "pixel",
        "vlm",
        "signal_quorum",
    ] = "context"
    coverage: float = 0.0
    expected: str = ""
    observed: str = ""
    param: Optional[str] = None
    signal_evidence: list[IdentitySignalEvidence] = Field(default_factory=list)
    quorum_required: Optional[int] = Field(default=None, ge=1)
    quorum_verified: Optional[int] = Field(default=None, ge=0)
    pixel_evidence: Optional[PixelIdentityEvidence] = Field(
        default=None,
        description=(
            "Hashes and local inventory references for the exact crops behind "
            "a pixel verdict. Crop pixels never enter the report."
        ),
    )


class HealEvent(BaseModel):
    step_id: str
    kind: Literal["anchor_refresh"] = "anchor_refresh"
    rung_used: Rung
    old_anchor: Anchor
    new_anchor: Anchor
    screenshot: Optional[str] = None  # run-dir-relative path
    applied: bool = False


class InterstitialActionResult(BaseModel):
    """Audited pre-step action for one declared interstitial dismissal.

    The runtime appends this event before backend delivery, so an exception or
    post-action refusal cannot hide the attempted key/click. ``delivered`` is
    input-delivery evidence only; ``clearance_ok`` is the independent visual
    outcome check that must be true before the workflow step may proceed.
    """

    interstitial: str
    action: Literal["key", "click"]
    key: Optional[str] = None
    risk: Literal["reversible"] = "reversible"
    consequential: Literal[False] = False
    expected_clearance: Predicate
    attempted: Literal[True] = True
    delivered: bool = False
    ok: bool = False
    clearance_ok: Optional[bool] = None
    resolution: Optional[Resolution] = None
    before_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_frame_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: Optional[str] = None


class ProgramExecutionScopeFrame(BaseModel):
    """PHI-free control scope for one action in a program run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str = Field(min_length=1, max_length=128)
    loop_state_id: Optional[str] = Field(default=None, max_length=128)
    relation: Optional[str] = Field(default=None, max_length=128)
    row_index: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _loop_scope_is_complete(self) -> "ProgramExecutionScopeFrame":
        loop_values = (self.loop_state_id, self.relation, self.row_index)
        if any(value is not None for value in loop_values) and not all(
            value is not None for value in loop_values
        ):
            raise ValueError("program loop scope is incomplete")
        return self


class ProgramGuardAssetEvidence(BaseModel):
    """One content-addressed bundle asset used by a transition predicate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory_ref: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _asset_ref_is_content_addressed(self) -> "ProgramGuardAssetEvidence":
        expected = f"private/program-transition-assets/{self.sha256}.bin"
        if self.inventory_ref != expected:
            raise ValueError(
                "transition guard asset reference is not content-addressed"
            )
        return self


class BusinessDecisionEvidence(BaseModel):
    """Authenticated control evidence for one finite human answer.

    This evidence can select a compiled branch.  It cannot satisfy an action's
    entity identity, postcondition, or business-effect contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.business-decision-evidence/v1"] = (
        "openadapt.business-decision-evidence/v1"
    )
    decision_index: int = Field(ge=0)
    graph_id: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    program_scope: list[ProgramExecutionScopeFrame] = Field(min_length=1)
    decision_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_inventory_ref: str = Field(min_length=1, max_length=512)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_inventory_ref: str = Field(min_length=1, max_length=512)
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    option_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    output_param: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
    output_value: str = Field(min_length=1, max_length=512)
    target_state_id: str = Field(min_length=1, max_length=128)
    operator_ref: str = Field(min_length=1, max_length=256)
    authorized_role: str = Field(min_length=1, max_length=128)
    authentication_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_artifact_sha256s: dict[str, str] = Field(default_factory=dict)
    idempotency_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: str
    governed_runtime_inputs_digest: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _local_inventory_is_exact(self) -> "BusinessDecisionEvidence":
        expected_request = f".business_decisions/requests/{self.request_sha256}.json"
        expected_receipt = f".business_decisions/receipts/{self.receipt_sha256}.json"
        if self.request_inventory_ref != expected_request:
            raise ValueError(
                "business decision request reference is not content-addressed"
            )
        if self.receipt_inventory_ref != expected_receipt:
            raise ValueError(
                "business decision receipt reference is not content-addressed"
            )
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in self.evidence_artifact_sha256s.values()
        ):
            raise ValueError("business decision artifact digest is invalid")
        return self


class ProgramTransitionEvidence(BaseModel):
    """Exact ordered evidence for one evaluated program transition.

    A decision emits one row for each transition evaluated before the first
    match. The final row is the selected transition. Frame-backed rows bind to
    a content-addressed private frame inside the local run directory. The
    report carries only the digest and inventory reference, not the pixels.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_index: int = Field(ge=0)
    graph_id: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    program_scope: list[ProgramExecutionScopeFrame] = Field(min_length=1)
    transition_index: int = Field(ge=0)
    guard_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    guard_verdict: bool
    selected: bool
    selected_target: Optional[str] = Field(default=None, min_length=1, max_length=128)
    guard_evidence_kind: Literal["unconditional", "parameters", "frame"]
    observed_frame_sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    observed_frame_inventory_ref: Optional[str] = None
    observed_viewport: Optional[tuple[int, int]] = None
    observed_context_sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    observed_context_inventory_ref: Optional[str] = None
    guard_evaluator_contract_sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    guard_assets: list[ProgramGuardAssetEvidence] = Field(default_factory=list)
    governed_runtime_inputs_digest: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _evidence_is_exact_and_content_addressed(self) -> "ProgramTransitionEvidence":
        if self.selected != self.guard_verdict:
            raise ValueError("a transition decision must stop at its first true guard")
        if self.selected and self.selected_target is None:
            raise ValueError("a selected transition requires its target")
        has_frame_digest = self.observed_frame_sha256 is not None
        has_frame_ref = self.observed_frame_inventory_ref is not None
        if has_frame_digest != has_frame_ref:
            raise ValueError(
                "transition frame digest and inventory reference must appear together"
            )
        if self.guard_evidence_kind == "frame" and not has_frame_digest:
            raise ValueError("frame-backed guard evidence requires an exact frame")
        if self.guard_evidence_kind != "frame" and has_frame_digest:
            raise ValueError(
                "non-visual guard evidence must not claim an observed frame"
            )
        if self.observed_frame_sha256 is not None:
            expected = f"private/program-transitions/{self.observed_frame_sha256}.png"
            if self.observed_frame_inventory_ref != expected:
                raise ValueError(
                    "transition frame inventory reference is not content-addressed"
                )
        if self.guard_evidence_kind == "frame":
            if self.observed_viewport is None or any(
                value <= 0 for value in self.observed_viewport
            ):
                raise ValueError("frame-backed guard evidence requires a viewport")
            if self.guard_evaluator_contract_sha256 is None:
                raise ValueError("frame-backed guard evidence requires an evaluator")
            if (
                self.observed_context_sha256 is None
                or self.observed_context_inventory_ref is None
            ):
                raise ValueError(
                    "frame-backed guard evidence requires exact observation context"
                )
            expected_context = (
                "private/program-transition-observations/"
                f"{self.observed_context_sha256}.json"
            )
            if self.observed_context_inventory_ref != expected_context:
                raise ValueError(
                    "transition observation context is not content-addressed"
                )
            if len({item.source_ref for item in self.guard_assets}) != len(
                self.guard_assets
            ):
                raise ValueError("transition guard asset references must be unique")
        elif (
            self.observed_viewport is not None
            or self.observed_context_sha256 is not None
            or self.observed_context_inventory_ref is not None
            or self.guard_evaluator_contract_sha256 is not None
            or self.guard_assets
        ):
            raise ValueError(
                "non-visual guard evidence must not claim visual evaluator inputs"
            )
        return self


class ProgramExceptionEvidence(BaseModel):
    """Exact typed evidence for one program exception edge.

    The classifier recomputes non-action failures from the workflow and the
    governed runtime inputs.  An action failure binds the typed runtime-failure
    category and the exact retained error digest.  Both forms disambiguate an
    exception handler from a normal transition with the same target state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_index: int = Field(ge=0)
    graph_id: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    program_scope: list[ProgramExecutionScopeFrame] = Field(min_length=1)
    target_state_id: str = Field(min_length=1, max_length=128)
    failure_kind: Literal[
        "action_failure",
        "branch_without_transition",
        "missing_subflow",
        "missing_loop_body",
        "loop_bound_exceeded",
    ]
    error_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    action_failure_category: Optional[Literal["runtime_failure"]] = None
    governed_runtime_inputs_digest: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _typed_cause_matches_edge(self) -> "ProgramExceptionEvidence":
        action = self.failure_kind == "action_failure"
        if action != bool(self.error_sha256 and self.action_failure_category):
            raise ValueError(
                "action exception evidence requires one exact typed error cause"
            )
        return self


class AttendedProgramTransitionEvidence(BaseModel):
    """A signed attended transition consumed without evaluating its guard again."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_index: int = Field(ge=0)
    graph_id: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    program_scope: list[ProgramExecutionScopeFrame] = Field(min_length=1)
    target_state_id: Optional[str] = Field(default=None, max_length=128)
    action: Literal["continue", "skip"]
    receipt_pause_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_inventory_ref: str = Field(min_length=1, max_length=512)
    control_frames_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_frames_inventory_ref: str = Field(min_length=1, max_length=512)
    governed_runtime_inputs_digest: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _receipt_ref_is_exact(self) -> "AttendedProgramTransitionEvidence":
        expected = f".attended_program_receipts/{self.receipt_pause_id}.json"
        if self.receipt_inventory_ref != expected:
            raise ValueError("attended transition receipt reference is not exact")
        expected_frames = (
            f"private/program-transition-controls/{self.control_frames_sha256}.json"
        )
        if self.control_frames_inventory_ref != expected_frames:
            raise ValueError("attended transition control reference is not exact")
        return self


class EffectVerificationEvidence(BaseModel):
    """Structured evidence behind one effect-verification decision."""

    effect_contract_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    substrate: str = Field(min_length=1)
    #: Opaque digest of the exact deployment adapter configuration that
    #: produced this evidence. Durable resume uses it with the retained tier to
    #: refuse a changed verifier after restart. ``None`` remains valid for
    #: checkpoints written before adapter binding was retained.
    verifier_identity: Optional[str] = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    verification_tier: Optional[int] = Field(default=None, ge=1, le=4)
    initial_verdict: Literal["confirmed", "refuted", "indeterminate"]
    final_verdict: Literal["confirmed", "refuted", "indeterminate"]
    reconciliation_completed: bool = False
    reconciliation_actions: int = Field(default=0, ge=0)
    #: What the verifier OBSERVED about the business effect in the system of
    #: record, independent of the pass/fail verdict. This is what lets a halted
    #: run state honestly what is known about the write: "absent" (the verifier
    #: established NO record was written -> HALTED_BEFORE_EFFECT), "conflicting"
    #: (a record WAS written but is a duplicate / wrong value -> a business
    #: effect that must be RECONCILED), "unknown" (the record could not be read,
    #: or a refutation carried no count, so absence cannot be claimed ->
    #: RECONCILIATION_REQUIRED, fail-safe), or "present" (accompanies a CONFIRMED
    #: or reconciled effect). Additive; defaults to the fail-safe "unknown" so an
    #: evidence record that predates this field never asserts "no effect".
    observed_effect: Literal["present", "absent", "conflicting", "unknown"] = "unknown"


class QualifiedEffectRequirement(BaseModel):
    """One exact qualified effect-strength requirement admitted for a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    actuation_path: Literal["gui", "api"]
    effect_index: int = Field(ge=0)
    effect_contract_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    minimum_tier: int = Field(ge=1, le=4)


class SafetyRefusalEvidence(BaseModel):
    """Typed detector output for one fail-closed pre-action refusal."""

    stage: Literal[
        "target_resolution",
        "identity_verification",
        "actuation_revalidation",
        "api_admission",
        "effect_strength",
        "effect_verifier",
    ]
    code: Literal[
        "target_ambiguous",
        "identity_conflict",
        "identity_unverifiable",
        "actuation_observation_changed",
        "api_path_unavailable",
        "effect_strength_insufficient",
        "effect_verifier_missing",
    ]
    detector_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class StepResult(BaseModel):
    step_id: str
    intent: str
    ok: bool
    risk: Literal["reversible", "irreversible"] = "reversible"
    risk_explanation: Optional[str] = None
    risk_review_required: bool = False
    # Workflow-program IR, Phase 1: True when a step was SKIPPED because its
    # ``guard`` was unmet with ``on_unmet="skip"`` (a no-op success -- the
    # step did not act). False for every executed step; additive.
    skipped: bool = False
    # Workflow-program IR, Phase 2: True when this (failed) action state was
    # routed to its ``State.on_exception`` handler instead of aborting the run
    # (the graph analog of a caught try/except). The result stays ``ok=False``
    # (the action DID fail) but the run continued via the handler; additive,
    # default False for every linear-mode and unhandled result.
    exception_handled: bool = False
    resolution: Optional[Resolution] = None
    drag_end_resolution: Optional[Resolution] = None
    identity: Optional[IdentityCheck] = None  # pre-click identity verdict
    input_verified: Optional[bool] = None  # TYPE/SELECT_OPTION input contract
    input_retried: bool = False  # TYPE only: refocus-and-retype fired
    postconditions_ok: Optional[bool] = None
    # Every pre-step interstitial key/click is appended BEFORE backend delivery,
    # so even an exception cannot produce an unreported action attempt. Failed
    # delivery or visual clearance HALTs before the workflow step acts.
    interstitial_actions: list[InterstitialActionResult] = Field(default_factory=list)
    # System-of-record effect verification (runtime.effects.EffectVerifier).
    # None when the step declared no `effects`; True when every declared
    # effect was CONFIRMED (or a duplicate was RECONCILED by compensation);
    # False when one HALTED the run (REFUTED / INDETERMINATE / escalated, or
    # effects were declared with no verifier configured). None is also used for
    # an explicitly approved but unverified GUI write; the separate
    # ``effect_approved_unverified`` flag makes that risk acceptance visible.
    # ``effect_results``
    # holds one human-readable verdict line per declared effect, for the
    # audit trail (mirrors the identity check's report surface). ZERO model
    # calls on this path — effect verification reads the system of record.
    effect_verified: Optional[bool] = None
    effect_approved_unverified: bool = False
    # A governed identity/effect/postcondition refusal is not an ordinary
    # workflow exception. Program ``on_exception`` handlers must not turn it
    # into a successful terminal outcome.
    safety_halt: bool = False
    effect_results: list[str] = Field(default_factory=list)
    effect_evidence: list[EffectVerificationEvidence] = Field(default_factory=list)
    #: Machine-readable terminal category. ``report.halt`` remains useful
    #: learning evidence but is not itself proof that a failure was a governed
    #: safety halt.
    failure_category: Optional[
        Literal[
            "governed_refusal",
            "safety_halt",
            "runtime_failure",
            "continuation_preempted",
        ]
    ] = None
    # One stable, NON-secret-bearing SHA-256 digest per verified effect, taken
    # AFTER the effect's ValueExpr contract was bound to THIS run's params
    # (P0-3). Records THAT a parameterized run verified against its own resolved
    # record/value/idempotency-key (and lets an auditor confirm two runs
    # resolved differently) without persisting the underlying value (e.g. a
    # patient identifier). Empty when the step declared no effects.
    effect_contract_hashes: list[str] = Field(default_factory=list)
    # How this step's write was PERFORMED: "api" when actuated via an
    # ApiBinding (GUI resolve/act skipped), None when it went through the GUI
    # resolution ladder (the default). Diagnostic/audit — lets an operator see
    # which steps ran on the deterministic API tier vs the visual floor.
    actuation: Optional[str] = None
    program_scope: list[ProgramExecutionScopeFrame] = Field(default_factory=list)
    # OS/UIA action-delivery evidence only. It deliberately cannot satisfy a
    # postcondition or system-of-record effect; those independent verdicts are
    # recorded in ``postconditions_ok`` / ``effect_verified``.
    delivery_receipt: Optional[ActionDeliveryReceipt] = None
    # Explicit state-machine proof of whether this workflow step crossed an
    # action-delivery boundary. ``False`` is written when a live step begins.
    # A typed fresh-frame mismatch keeps it false only when the backend proves
    # no input edge occurred. Successful delivery, uncertain delivery, and an
    # untyped backend exception all change it to True. ``None`` means a legacy
    # or synthesized result did not retain this fact; callers must treat that
    # as unknown rather than infer non-delivery from a failure category or
    # error string.
    delivery_attempted: Optional[bool] = None
    # Bounded, PHI-free diagnostics for pre-input actuation-frame mismatches.
    # No rejected screenshot or pixel value is retained. A terminal mismatch
    # is present with ``retried=False``.
    fresh_actuation_events: list[FreshActuationEvent] = Field(default_factory=list)
    # The action API raised after delivery may have begun.  This is neither a
    # receipt nor an ordinary backend failure: the runtime never retries and
    # can proceed only when the complete independent outcome contract proves
    # what happened.
    delivery_uncertainty: Optional[ActionDeliveryUncertainty] = None
    safety_refusal_evidence: Optional[SafetyRefusalEvidence] = None
    starting_state_settled: Optional[bool] = Field(
        default=None,
        description=(
            "Retained result of the bounded pre-action settling check. None "
            "means no settling observation was made; it must not be treated "
            "as proof that settled-state detection ran."
        ),
    )
    # Drift-oracle: postconditions that deterministically FAILED but were
    # confirmed by the optional on-prem VLM state-verifier under render drift
    # (recorded for audit; empty unless an appliance is configured).
    postcondition_drift_rescues: list[str] = Field(default_factory=list)
    drift_oracle_calls: int = 0  # VLM state-verifier calls this step
    heal: Optional[HealEvent] = None
    error: Optional[str] = None
    before_png: Optional[str] = None  # run-dir-relative paths
    after_png: Optional[str] = None
    elapsed_ms: float = 0.0


class UnarmedStep(BaseModel):
    """A click step that will proceed with NO identity verification."""

    step_id: str
    intent: str = ""
    reason: str = ""


class HaltObservation(BaseModel):
    """The structured record a HALT emits — the substrate the halt->learn loop
    consumes (``openadapt_flow.learning.halt_loop``).

    When ``Replayer.run`` stops on an unhandled state (a resolution failure, a
    dead-end branch, an unmet ``halt`` guard, a non-CONFIRMED effect, a ``halt``
    terminal), it records WHERE it stopped (``state_id`` / ``intent`` /
    ``reason``), WHAT unexpected state it observed there (``observed_texts`` — the
    on-screen text the compiled program had no branch for, PHI-scrubbed), and the
    PRE-context needed to learn a resolution (``completed_intents`` — the steps
    that succeeded before the halt). This is deliberately the SAME shape a
    :class:`~openadapt_flow.learning.trace.ExecutionTrace` carries (ordered
    intents + observed screen facts), so the learning bridge lifts it into the
    trace corpus with no reshaping — it is a report/audit field, NOT a parallel
    learning system.

    Additive and backward-compatible: ``RunReport.halt`` defaults to None, so a
    successful run (or a consumer that ignores it) is unaffected.
    """

    state_id: str = ""
    intent: str = ""
    reason: str = ""
    outcome: str = "halt"
    #: On-screen text observed at the halt point (PHI-scrubbed) — the unexpected
    #: UI state the program was not demonstrated to handle. Keyed later as the
    #: ``TEXT_PRESENT`` facts a learned branch guard tests.
    observed_texts: list[str] = Field(default_factory=list)
    #: Intents of the steps that completed successfully BEFORE the halt (the
    #: pre-context a resolution demonstration extends).
    completed_intents: list[str] = Field(default_factory=list)


class OutcomeContractCounts(BaseModel):
    """PHI-free counts of the contracts required and passed by one run."""

    authorization: int = Field(default=0, ge=0)
    identity: int = Field(default=0, ge=0)
    postcondition: int = Field(default=0, ge=0)
    effect: int = Field(default=0, ge=0)


OutcomeEvidenceClass = Literal[
    "authorization",
    "identity",
    "postcondition",
    "effect_tier_1",
    "effect_tier_2",
    "effect_tier_3",
    "effect_tier_4",
    "model",
    "compensation",
]


PostconditionEvidenceKind = Literal[
    "explicit_predicate",
    "intrinsic_input_readback",
]


def postcondition_step_contract_sha256(
    *,
    workflow_contract_sha256: str,
    step_index: int,
    action_kind: ActionKind | str,
) -> str:
    """Bind a step position and action to an exact workflow contract."""

    action = action_kind.value if isinstance(action_kind, ActionKind) else action_kind
    payload = {
        "domain": "openadapt.postcondition-step/v1",
        "workflow_contract_sha256": workflow_contract_sha256,
        "step_index": step_index,
        "action_kind": action,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def postcondition_contract_sha256(
    *,
    workflow_contract_sha256: str,
    step_contract_sha256: str,
    action_kind: ActionKind | str,
    contract_kind: PostconditionEvidenceKind,
    contract_index: int,
) -> str:
    """Bind one predicate/readback position without retaining its content."""

    action = action_kind.value if isinstance(action_kind, ActionKind) else action_kind
    payload = {
        "domain": "openadapt.postcondition-contract/v1",
        "workflow_contract_sha256": workflow_contract_sha256,
        "step_contract_sha256": step_contract_sha256,
        "action_kind": action,
        "actuation_path": "gui",
        "contract_kind": contract_kind,
        "contract_index": contract_index,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PostconditionContractEvidence(BaseModel):
    """PHI-free proof for one postcondition contract on one result.

    ``result_index`` binds the proof to the exact retained result occurrence,
    including repeated executions of the same program step.  The two digests
    bind it to the executable workflow and exact step contract without placing
    the step id, predicate text, typed value, or other record-bearing content in
    the control-plane envelope.

    An explicit predicate and intrinsic TYPE/SELECT_OPTION readback are
    intentionally different contract kinds.  A successful input readback can
    therefore never be counted as proof for a CLICK predicate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_index: int = Field(ge=0)
    workflow_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    step_index: int = Field(ge=0)
    step_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_kind: ActionKind
    actuation_path: Literal["gui"] = "gui"
    contract_kind: PostconditionEvidenceKind
    contract_index: int = Field(ge=0)
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verdict: Literal["passed", "refuted", "unverifiable"]

    @model_validator(mode="after")
    def _validate_contract_kind(self) -> "PostconditionContractEvidence":
        if self.contract_kind == "intrinsic_input_readback":
            if self.action_kind not in {ActionKind.TYPE, ActionKind.SELECT_OPTION}:
                raise ValueError(
                    "intrinsic input readback is valid only for TYPE or SELECT_OPTION"
                )
            if self.contract_index != 0:
                raise ValueError("intrinsic input readback uses contract index zero")
        expected_step = postcondition_step_contract_sha256(
            workflow_contract_sha256=self.workflow_contract_sha256,
            step_index=self.step_index,
            action_kind=self.action_kind,
        )
        if self.step_contract_sha256 != expected_step:
            raise ValueError(
                "postcondition evidence step digest does not match its binding"
            )
        expected_contract = postcondition_contract_sha256(
            workflow_contract_sha256=self.workflow_contract_sha256,
            step_contract_sha256=self.step_contract_sha256,
            action_kind=self.action_kind,
            contract_kind=self.contract_kind,
            contract_index=self.contract_index,
        )
        if self.contract_sha256 != expected_contract:
            raise ValueError(
                "postcondition evidence contract digest does not match its binding"
            )
        return self


class ExecutionOutcomeEnvelope(BaseModel):
    """Versioned, PHI-free execution result shared with control planes.

    The coarse ``success``/``halt``/``failed`` lifecycle remains available for
    old consumers.  This envelope states what the evidence actually proves.
    """

    version: Literal["openadapt.execution-outcome/v1"] = (
        "openadapt.execution-outcome/v1"
    )
    outcome: Literal[
        "VERIFIED",
        "COMPLETED_UNVERIFIED",
        "HALTED",
        "FAILED",
        "ROLLED_BACK",
    ]
    profile: Optional[Literal["demo", "standard", "regulated"]] = None
    production_eligible: bool = False
    qualification_evidence_only: bool = False
    execution_completed: bool = False
    required_contracts: OutcomeContractCounts = Field(
        default_factory=OutcomeContractCounts
    )
    passed_contracts: OutcomeContractCounts = Field(
        default_factory=OutcomeContractCounts
    )
    workflow_contract_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        description=(
            "Exact executable workflow contract that owns the retained "
            "postcondition evidence."
        ),
    )
    postcondition_evidence: list[PostconditionContractEvidence] = Field(
        default_factory=list,
        description=(
            "One typed, result-bound record for every required explicit or "
            "intrinsic GUI postcondition contract."
        ),
    )
    evidence_classes: list[OutcomeEvidenceClass] = Field(default_factory=list)
    model_calls: int = Field(default=0, ge=0)
    external_network_calls: Literal["none", "observed", "unknown"] = "unknown"
    compensation_actions: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_evidence_contract(self) -> "ExecutionOutcomeEnvelope":
        required = self.required_contracts.model_dump()
        passed = self.passed_contracts.model_dump()
        if any(passed[key] > required[key] for key in required):
            raise ValueError("passed contract counts cannot exceed required counts")
        if (
            self.outcome in {"VERIFIED", "COMPLETED_UNVERIFIED"}
            and not self.execution_completed
        ):
            raise ValueError(
                f"{self.outcome} requires evidence that execution completed"
            )
        if len(self.evidence_classes) != len(set(self.evidence_classes)):
            raise ValueError("evidence classes must be unique")
        postcondition_evidence = self.postcondition_evidence
        if len(postcondition_evidence) != required["postcondition"]:
            raise ValueError(
                "postcondition evidence cardinality must equal the required "
                "postcondition count"
            )
        passed_postconditions = sum(
            item.verdict == "passed" for item in postcondition_evidence
        )
        if passed_postconditions != passed["postcondition"]:
            raise ValueError(
                "passed postcondition evidence must equal the passed "
                "postcondition count"
            )
        if postcondition_evidence and self.workflow_contract_sha256 is None:
            raise ValueError(
                "postcondition evidence requires an exact workflow contract"
            )
        if any(
            item.workflow_contract_sha256 != self.workflow_contract_sha256
            for item in postcondition_evidence
        ):
            raise ValueError(
                "postcondition evidence must bind the envelope workflow contract"
            )
        evidence_keys = [
            (item.result_index, item.contract_kind, item.contract_index)
            for item in postcondition_evidence
        ]
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ValueError("postcondition evidence contract keys must be unique")
        result_contracts = [
            (item.result_index, item.contract_sha256) for item in postcondition_evidence
        ]
        if len(result_contracts) != len(set(result_contracts)):
            raise ValueError(
                "postcondition evidence contract digests must be unique per result"
            )
        has_postcondition_class = "postcondition" in self.evidence_classes
        if has_postcondition_class != bool(passed["postcondition"]):
            raise ValueError(
                "postcondition evidence class and passed contract count disagree"
            )
        if self.outcome == "VERIFIED":
            if passed != required:
                raise ValueError("VERIFIED requires every declared contract to pass")
            if self.profile not in {"standard", "regulated"}:
                raise ValueError("VERIFIED requires a Standard or Regulated profile")
            if not self.production_eligible and not self.qualification_evidence_only:
                raise ValueError(
                    "VERIFIED must be production eligible or bound to a "
                    "qualification-only run"
                )
            if required["authorization"] < 1 or passed["authorization"] < 1:
                raise ValueError(
                    "VERIFIED requires a passed governed authorization contract"
                )
        elif self.production_eligible:
            raise ValueError(
                "only VERIFIED Standard or Regulated runs are production eligible"
            )
        if self.production_eligible and self.qualification_evidence_only:
            raise ValueError("qualification-only evidence cannot authorize production")
        has_compensation = "compensation" in self.evidence_classes
        if has_compensation != (self.compensation_actions > 0):
            raise ValueError(
                "compensation evidence and completed action count are inconsistent"
            )
        if self.outcome == "ROLLED_BACK":
            if self.compensation_actions < 1:
                raise ValueError(
                    "ROLLED_BACK requires evidence of a completed compensating action"
                )
        elif self.compensation_actions:
            raise ValueError(
                "completed compensating actions require the ROLLED_BACK outcome"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Omit the additive qualification marker on ordinary run envelopes."""

        data: dict[str, Any] = handler(self)
        if not self.qualification_evidence_only:
            data.pop("qualification_evidence_only", None)
        if self.workflow_contract_sha256 is None:
            data.pop("workflow_contract_sha256", None)
        if not self.postcondition_evidence:
            data.pop("postcondition_evidence", None)
        return data


class EffectJournalEntry(BaseModel):
    """One consequential step's transaction-effect record (PHI-free).

    The effect journal is the per-step ledger the reconciliation model reads.
    For each consequential step it states the INTENDED effect (by stable
    contract hash), the ATTEMPT state (delivered / uncertain / actuated / not
    actuated), the OBSERVED effect the independent verifier read from the
    system of record, how FRESH that verification was, and any COLLATERAL delta
    (reconciling compensations applied). It carries no record values,
    parameters, or free text -- only typed evidence already present on the
    :class:`StepResult`, so it can be persisted in the run's evidence output
    without leaking PHI.
    """

    step_id: str
    intent: str
    consequential: bool = True
    #: Stable, non-secret digests of the effect contracts THIS step intended to
    #: apply (mirrors ``StepResult.effect_contract_hashes``).
    intended_effect_contract_hashes: list[str] = Field(default_factory=list)
    #: How far actuation got before verification: the write was never actuated,
    #: was delivered through the GUI/native ladder, was delivered via the API
    #: tier, or the action API raised AFTER delivery may have begun (the #250
    #: uncertain-delivery path -- never blind-retried).
    attempt_state: Literal[
        "not_actuated", "delivered", "actuated_api", "delivery_uncertain"
    ] = "not_actuated"
    #: The verifier's independent read of the business effect, taken as the worst
    #: case across this step's declared effects (unknown < conflicting < absent
    #: < present is NOT the ordering; see the classifier -- "conflicting" and
    #: "unknown" both force reconciliation, "absent" proves no effect).
    observed_effect: Literal["present", "absent", "conflicting", "unknown"] = "unknown"
    #: True when every declared effect was CONFIRMED at/above the required tier.
    effect_verified: Optional[bool] = None
    #: Whether this was an approved-but-unverified GUI write (risk accepted).
    approved_unverified: bool = False
    #: Verifier freshness: whether independent verification actually ran this
    #: run, and the ISO-8601 instant a delivery-uncertainty was observed (the
    #: tightest freshness signal we retain, present only on the #250 path).
    verification_performed: bool = False
    observed_at: Optional[str] = None
    #: Collateral delta: count of reconciling compensation actions completed for
    #: this step (0 when none). Nonzero means the system of record was mutated to
    #: undo a detected duplicate / collateral write.
    collateral_reconciliation_actions: int = Field(default=0, ge=0)


class RunReport(BaseModel):
    workflow_name: str
    started_at: str
    execution_profile: Optional[Literal["demo", "standard", "regulated"]] = Field(
        default=None,
        description=(
            "Named runtime posture applied to this run. Raw replay is Demo; "
            "governed run authorization carries Standard or Regulated."
        ),
    )
    execution_outcome: Optional[
        Literal[
            "VERIFIED",
            "COMPLETED_UNVERIFIED",
            "HALTED",
            "FAILED",
            "ROLLED_BACK",
        ]
    ] = Field(
        default=None,
        description=(
            "Evidence-qualified outcome. Standard and Regulated never map a "
            "screen-only consequential completion to VERIFIED."
        ),
    )
    production_eligible: bool = Field(
        default=False,
        description=(
            "True only for a VERIFIED result under Standard or Regulated. "
            "Demo results remain explicitly non-production."
        ),
    )
    execution_completed: Optional[bool] = Field(
        default=None,
        description=(
            "Whether execution reached a completed terminal state, independent "
            "of whether the evidence contract permitted reporting success."
        ),
    )
    outcome_envelope: Optional[ExecutionOutcomeEnvelope] = Field(
        default=None,
        description=(
            "Versioned PHI-free outcome, contract coverage, evidence classes, "
            "model calls, and external-network-call observability."
        ),
    )
    # -- Section 3: explicit transaction / reconciliation semantics ------------
    # The coarse ``execution_outcome`` (VERIFIED/HALTED/FAILED/...) is refined
    # into a first-class TERMINAL TRANSACTION outcome that states what is known
    # about the BUSINESS EFFECT. Additive and derived from the same typed
    # evidence: ``execution_outcome`` and ``outcome_envelope`` are unchanged, so
    # every existing consumer keeps working (see openadapt_flow.transaction).
    transaction_outcome: Optional[
        Literal[
            "VERIFIED",
            "HALTED_BEFORE_EFFECT",
            "RECONCILIATION_REQUIRED",
            "FAILED_PLATFORM",
            "CANCELED",
            "REJECTED_POLICY",
            "COMPLETED_UNVERIFIED",
            "ROLLED_BACK",
        ]
    ] = Field(
        default=None,
        description=(
            "Terminal transaction outcome describing what is known about the "
            "business effect. Refines the coarse execution_outcome without "
            "replacing it."
        ),
    )
    transaction_billable: Optional[bool] = Field(
        default=None,
        description=(
            "Whether this run represents a chargeable business outcome. A "
            "FAILED_PLATFORM (OpenAdapt/platform fault) and a Demo-only "
            "COMPLETED_UNVERIFIED are never billable."
        ),
    )
    transaction_platform_fault: Optional[bool] = Field(
        default=None,
        description=(
            "True only for FAILED_PLATFORM: an OpenAdapt/platform failure that "
            "occurred before any possible business effect, distinct from a "
            "customer/governed outcome."
        ),
    )
    effect_journal: list[EffectJournalEntry] = Field(
        default_factory=list,
        description=(
            "Per consequential-step transaction ledger (intended effect, "
            "attempt state, observed effect, verifier freshness, collateral "
            "delta). PHI-free; the substrate the reconciliation model reads."
        ),
    )
    idempotency_key: Optional[str] = Field(
        default=None,
        description=(
            "Caller-supplied at-most-once key for this run. When an "
            "idempotency ledger is configured, a repeat with the same key is "
            "suppressed rather than re-actuated."
        ),
    )
    idempotent_replay: bool = Field(
        default=False,
        description=(
            "True when this run was SUPPRESSED as a duplicate of a prior run "
            "that already actuated under the same idempotency key. No "
            "consequential action was re-performed."
        ),
    )
    canceled: bool = Field(
        default=False,
        description=(
            "True when the run was canceled before any business effect could "
            "occur. Drives the CANCELED terminal transaction outcome."
        ),
    )
    execution_target_kind: Optional[ExecutionTargetKind] = Field(
        default=None,
        description=(
            "Resolved backend/substrate that produced this report. The CLI "
            "sets this from the resolved backend configuration; runtime "
            "validation refuses a native/remote attestation without it."
        ),
    )
    recorded_surface: Optional[ExecutionTargetKind] = Field(
        default=None,
        description=(
            "Surface the executed workflow was recorded/qualified on "
            "(``Workflow.surface``). None for a legacy, surface-unbound "
            "bundle. Together with execution_target_kind this makes any "
            "cross-surface execution visible in the evidence."
        ),
    )
    surface_override: bool = Field(
        default=False,
        description=(
            "True when the operator explicitly overrode the workflow's bound "
            "surface (--allow-surface-override) and this run executed on a "
            "surface other than recorded_surface. The compatibility-evidence "
            "hook: a cross-surface run is never silent."
        ),
    )
    execution_origin: Optional[str] = Field(
        default=None,
        description=(
            "Actual browser origin loaded before replay. Hosted validation "
            "requires this to match its signed target boundary."
        ),
    )
    execution_entry_url: Optional[str] = Field(
        default=None,
        description=(
            "Browser entry URL requested before replay. Hosted validation "
            "binds this separately from the resulting browser origin."
        ),
    )
    bundle_content_digest: Optional[str] = Field(default=None, pattern="^[a-f0-9]{64}$")
    workflow_contract_sha256: Optional[str] = Field(
        default=None,
        pattern="^[a-f0-9]{64}$",
        description=(
            "Exact executable workflow semantics and sealed visual-asset hashes "
            "observed by the runtime before execution."
        ),
    )
    source_recording_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    parameter_schema_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    # Present only when the fail-closed ``run`` command handed an exact,
    # bundle-bound admission capability into replay.  The id/source are audit
    # references, not proof that a local CLI user's identity was authenticated.
    governed_authorization_id: Optional[str] = None
    governed_approval_source: Optional[str] = None
    governed_authorization_created_at: Optional[str] = None
    governed_policy_name: Optional[str] = None
    governed_policy_contract_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_minimum_effect_tier: Optional[int] = Field(default=None, ge=1, le=4)
    governed_qualified_effect_requirements: list[QualifiedEffectRequirement] = Field(
        default_factory=list
    )
    governed_runtime_inputs_digest: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    run_id_sha256: Optional[str] = Field(
        default=None,
        pattern="^[a-f0-9]{64}$",
        description=(
            "One-way binding to the exact runtime identity used by resolved "
            "effect contracts; the raw run identity is never retained here."
        ),
    )
    governed_qualification_project_id: Optional[str] = None
    governed_qualification_project_revision: Optional[int] = Field(default=None, ge=1)
    governed_qualification_project_contract_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_qualification_campaign_id_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_qualification_case_id_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_qualification_case_input_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_qualification_run_id_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_qualification_case_kind: Optional[
        Literal[
            "representative",
            "ambiguity",
            "wrong_identity",
            "stale_identity",
            "weak_effect",
            "missing_effect",
        ]
    ] = None
    governed_qualification_case_action_paths: dict[str, Literal["gui", "api"]] = Field(
        default_factory=dict
    )
    governed_qualification_fault_driver_id: Optional[str] = None
    governed_qualification_fault_driver_contract_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_qualification_fault_driver_key_id: Optional[str] = None
    governed_qualification_fault_step_id_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_evidence_only: bool = False
    qualification_fault_mutations: list[FaultMutationReceipt] = Field(
        default_factory=list
    )
    observed_application_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    observed_application_version_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    observed_session_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    observed_environment_digest: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    observed_environment_binding_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_environment_observer_id: Optional[str] = None
    qualification_environment_observer_contract_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    governed_authorized_effect_contracts: dict[str, list[str]] = Field(
        default_factory=dict
    )
    required_identity_step_ids: list[str] = Field(default_factory=list)
    approved_unverified_effect_step_ids: list[str] = Field(default_factory=list)
    params: dict[str, str] = Field(default_factory=dict)
    results: list[StepResult] = Field(default_factory=list)
    success: bool = False
    # Workflow-program IR, Phase 2: the outcome of the terminal state the graph
    # interpreter ended on ("success" | "halt" | "escalate"), or None for a
    # linear-mode run (no program graph) or a run that fell off the graph. The
    # ordered ``visited_states`` trace records the state ids the interpreter
    # walked (action/branch/loop/subflow/terminal), for the audit trail. Both
    # additive and empty/None on a linear run.
    terminal_outcome: Optional[str] = None
    visited_states: list[str] = Field(default_factory=list)
    program_transition_evidence: list[ProgramTransitionEvidence] = Field(
        default_factory=list,
        description=(
            "Ordered guard evaluations retained by the program runtime. "
            "Frame-backed evidence refers to private local run artifacts."
        ),
    )
    business_decision_evidence: list[BusinessDecisionEvidence] = Field(
        default_factory=list,
        description=(
            "Ordered signed finite human answers used only for program control. "
            "These records never satisfy entity identity or effect verification."
        ),
    )
    program_exception_evidence: list[ProgramExceptionEvidence] = Field(
        default_factory=list,
        description=(
            "Ordered non-action exception edges whose causes can be recomputed "
            "from the workflow and governed runtime inputs."
        ),
    )
    attended_program_transition_evidence: list[AttendedProgramTransitionEvidence] = (
        Field(
            default_factory=list,
            description=(
                "Ordered signed attended transitions consumed during durable resume."
            ),
        )
    )
    # The structured HALT record (see HaltObservation): populated by
    # Replayer.run when the run stops on an unhandled state, so the halt->learn
    # loop can lift it into the trace corpus. None on a successful run (and on
    # any run whose halt path predates this field) — additive/back-compatible.
    halt: Optional["HaltObservation"] = None
    rung_counts: dict[str, int] = Field(default_factory=dict)
    heal_count: int = 0
    model_calls: int = 0
    external_network_calls: Literal["none", "observed", "unknown"] = "unknown"
    est_model_cost_usd: float = 0.0
    total_ms: float = 0.0
    # Identity-protection coverage of the WHOLE workflow (computed at run
    # start from the bundle, not just from executed steps): how many of
    # the identity-applicable steps (clicks / anchored TYPE) carry an
    # armed pre-click identity check, and which proceed unguarded.
    identity_applicable_steps: int = 0
    identity_armed_steps: int = 0
    identity_unarmed: list[UnarmedStep] = Field(default_factory=list)
    # Egress transparency (PHI audit REM-3): True when an egress-capable model
    # component (a paid-API or on-prem-appliance grounder / identity-VLM /
    # state-verifier) was wired for this run, so a screenshot COULD leave the
    # box. False for the default local replay (which makes zero outbound calls).
    # Wiring an egress component requires the operator's explicit opt-in
    # (Replayer(allow_model_grounding=True) / CLI --allow-model-grounding).
    screenshots_may_leave_box: bool = False

    @model_validator(mode="after")
    def _validate_outcome_envelope_binding(self) -> "RunReport":
        """Keep the transported envelope bound to this exact report.

        The envelope is the PHI-free projection accepted by hosted consumers.
        When it is present, its top-level fields must not be independently
        mutable from the local report that produced it.
        """

        expected_qualification_only = bool(self.governed_qualification_case_id_sha256)
        if "qualification_evidence_only" not in self.model_fields_set:
            # Normalize reports written before the typed marker existed from
            # their already-retained qualification-case binding.
            self.qualification_evidence_only = expected_qualification_only
        elif self.qualification_evidence_only != expected_qualification_only:
            raise ValueError(
                "qualification-only status does not match the typed case binding"
            )

        envelope = self.outcome_envelope
        if envelope is None:
            return self
        if self.execution_outcome != envelope.outcome:
            raise ValueError("execution outcome does not match its evidence envelope")
        if self.execution_profile != envelope.profile:
            raise ValueError("execution profile does not match its evidence envelope")
        if self.production_eligible != envelope.production_eligible:
            raise ValueError(
                "production eligibility does not match its evidence envelope"
            )
        if (
            "qualification_evidence_only" in envelope.model_fields_set
            and envelope.qualification_evidence_only != self.qualification_evidence_only
        ):
            raise ValueError("qualification-only status does not match the run report")
        if self.execution_completed is None or (
            self.execution_completed != envelope.execution_completed
        ):
            raise ValueError(
                "execution completion does not match its evidence envelope"
            )
        if self.model_calls != envelope.model_calls:
            raise ValueError("model-call count does not match its evidence envelope")
        if "external_network_calls" not in self.model_fields_set:
            # Reports produced before this top-level binding existed carried
            # the observation only inside the envelope. Preserve read
            # compatibility while normalizing them on the next save.
            self.external_network_calls = envelope.external_network_calls
        elif self.external_network_calls != envelope.external_network_calls:
            raise ValueError(
                "external-network observation does not match its evidence envelope"
            )
        if envelope.outcome == "VERIFIED" and not self.success:
            raise ValueError("VERIFIED evidence cannot accompany a non-success report")
        if envelope.profile in {"standard", "regulated"} and self.success != (
            envelope.outcome == "VERIFIED"
        ):
            raise ValueError(
                "production report success must mean the exact VERIFIED outcome"
            )
        if envelope.outcome == "ROLLED_BACK" and self.success:
            raise ValueError("ROLLED_BACK is a non-success outcome")
        return self

    def save(
        self,
        run_dir: Path | str,
        *,
        filename: str = "report.json",
    ) -> Path:
        run = Path(run_dir)
        run.mkdir(parents=True, exist_ok=True)
        if Path(filename).name != filename:
            raise ValueError("report filename must be one local file name")
        path = run / filename
        payload = self.model_dump_json(indent=2).encode("utf-8")
        temporary = run / (f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                directory = os.open(run, os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                except OSError:
                    pass
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return path


# -- forward-reference resolution --------------------------------------------
#
# Step.effects is typed ``list[Effect]`` where Effect lives in
# ``openadapt_flow.runtime.effects.effect``. That type is imported HERE, at the
# very bottom of the module, NOT at the top: importing it eagerly triggers
# ``openadapt_flow.runtime``'s package __init__, which imports the Replayer,
# which imports THIS module — so a top-level import would recurse through a
# half-initialized ``ir`` (Step/Workflow not yet defined) and fail. By the time
# this line runs every class above is fully defined, so the (import-light — no
# OCR/cv2/model deps) runtime package loads cleanly and Step's schema can be
# completed. Effect enters this module's globals so ``model_rebuild`` resolves
# the forward reference; bundles with no effects are unaffected.
from openadapt_flow.qualification import QualificationProject  # noqa: E402,F401
from openadapt_flow.runtime.effects.effect import Effect  # noqa: E402,F401

ApiBinding.model_rebuild()
Step.model_rebuild()
GovernedAuthorizationTemplate.model_rebuild()
# Phase-2 state-machine models: State embeds Step (whose Effect forward ref is
# resolved just above), Transition embeds Predicate, ProgramGraph embeds State,
# and Workflow embeds ProgramGraph/Relation -- rebuild in dependency order so
# every forward reference is resolved before Workflow's schema is completed.
Transition.model_rebuild()
LoopSpec.model_rebuild()
State.model_rebuild()
ProgramGraph.model_rebuild()
Workflow.model_rebuild()
