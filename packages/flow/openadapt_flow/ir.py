"""Intermediate representation for compiled workflows.

A Workflow is the compiled artifact: an ordered list of Steps, each carrying
redundant evidence about its target (Anchor), the action to perform, and
assertions about what the screen should look like afterwards (Postconditions).

The canonical serialized form is a *bundle directory*:

    <bundle>/
      workflow.json        # Workflow.model_dump_json()
      templates/*.png      # anchor template crops, referenced by relative path

All coordinates are pixels in the recorded frame's coordinate space.
Regions are (x, y, w, h). Points are (x, y).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Iterator, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    model_serializer,
    model_validator,
)

if TYPE_CHECKING:
    # Type-only import for the Step.effects forward reference. The RUNTIME
    # import is at the BOTTOM of this module (see the note there) to avoid a
    # circular import through openadapt_flow.runtime's package init.
    from openadapt_flow.runtime.effects.effect import Effect

Region = tuple[int, int, int, int]
Point = tuple[int, int]

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
    TYPE = "type"
    KEY = "key"
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
    dx_px: Optional[int] = Field(
        default=None,
        description="Exact x offset landmark center -> target click point",
    )
    dy_px: Optional[int] = Field(
        default=None,
        description="Exact y offset landmark center -> target click point",
    )


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
    present with no actuator configured also falls through to the GUI ladder --
    the API tier is an OPTIMIZATION whose safe fallback is the GUI, never a gate
    that can block a runnable step.

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
    key: Optional[str] = None  # for KEY, e.g. "Enter"
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
    timeout_s: float = 10.0
    # Identity-protection audit trail (clicks and anchored TYPE steps):
    # whether this step's click is guarded by the pre-click identity check
    # (anchor.context_text present). Written by the compiler so an
    # operator can audit a bundle's protection coverage BEFORE running it;
    # None on non-click steps and on bundles compiled before this field
    # existed. An UNARMED click proceeds with NO identity verification
    # (see docs/LIMITS.md).
    identity_armed: Optional[bool] = Field(
        default=None,
        description=(
            "Clicks/anchored TYPE only: True when the pre-click identity"
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
    :class:`Step`; ``branch`` picks an edge purely by guard; ``loop`` iterates a
    worklist; ``subflow_call`` invokes a reusable subgraph; ``terminal`` ends the
    (sub)graph. ``transitions`` are the outgoing edges (empty on a terminal, a
    single unconditional edge on a degenerate linear node). ``on_exception``
    routes a FAILED action to a local handler instead of aborting the whole run.
    """

    id: str
    kind: StateKind
    # kind == ACTION: the hardened Phase-1 Step to perform (unchanged leaf --
    # anchor resolution, identity gate, effects, risk all ride along on it).
    step: Optional[Step] = None
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


class ActionDeliveryReceipt(BaseModel):
    """Proof that an action was delivered, never that its outcome happened.

    Native UIA Invoke/Focus/Toggle/Select and physical input can confirm only
    that the operating-system action API accepted the request. Business success
    remains the independent postcondition + system-of-record effect verifier's
    responsibility. ``outcome_verified`` is therefore fixed False here.
    """

    status: Literal["delivered"] = "delivered"
    receipt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    native: bool
    target_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    delivered_at: str = Field(min_length=20, max_length=64)
    outcome_verified: Literal[False] = False


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
        mode: ``structured`` compares the recorded DOM/a11y identity text
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
    mode: Literal["context", "param", "structured", "pixel", "vlm"] = "context"
    coverage: float = 0.0
    expected: str = ""
    observed: str = ""
    param: Optional[str] = None


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


class StepResult(BaseModel):
    step_id: str
    intent: str
    ok: bool
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
    identity: Optional[IdentityCheck] = None  # pre-click identity verdict
    input_verified: Optional[bool] = None  # TYPE steps: typed input landed
    input_retried: bool = False  # TYPE steps: refocus-and-retype fired
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
    # OS/UIA action-delivery evidence only. It deliberately cannot satisfy a
    # postcondition or system-of-record effect; those independent verdicts are
    # recorded in ``postconditions_ok`` / ``effect_verified``.
    delivery_receipt: Optional[ActionDeliveryReceipt] = None
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


class RunReport(BaseModel):
    workflow_name: str
    started_at: str
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
    governed_runtime_inputs_digest: Optional[str] = Field(
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
    # The structured HALT record (see HaltObservation): populated by
    # Replayer.run when the run stops on an unhandled state, so the halt->learn
    # loop can lift it into the trace corpus. None on a successful run (and on
    # any run whose halt path predates this field) — additive/back-compatible.
    halt: Optional["HaltObservation"] = None
    rung_counts: dict[str, int] = Field(default_factory=dict)
    heal_count: int = 0
    model_calls: int = 0
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

    def save(self, run_dir: Path | str) -> Path:
        run = Path(run_dir)
        run.mkdir(parents=True, exist_ok=True)
        path = run / "report.json"
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
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
from openadapt_flow.runtime.effects.effect import Effect  # noqa: E402,F401

ApiBinding.model_rebuild()
Step.model_rebuild()
# Phase-2 state-machine models: State embeds Step (whose Effect forward ref is
# resolved just above), Transition embeds Predicate, ProgramGraph embeds State,
# and Workflow embeds ProgramGraph/Relation -- rebuild in dependency order so
# every forward reference is resolved before Workflow's schema is completed.
Transition.model_rebuild()
LoopSpec.model_rebuild()
State.model_rebuild()
ProgramGraph.model_rebuild()
Workflow.model_rebuild()
