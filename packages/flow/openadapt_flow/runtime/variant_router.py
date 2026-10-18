"""Deterministic pre-run router over a variant set.

``route_variant`` picks at most one variant from the item's inputs and a
fingerprint of the first screen. It makes no model call: inputs compare as
exact strings, the theme is a luminance class of the frame, and a marker must
appear as whole words on one line of the local OCR read (not the fuzzy
``text_present`` match guards use, which would let ``Version 11`` match
``Version 12``). Zero matches, two matches, or a needed screen that could not
be captured each halt with a named reason.

``execute_variant_set`` then treats the chosen variant exactly like a process
child: the variant's own admission envelope is verified (pointer, live bundle
digest, signature, expiry, revocation) before the governed child run starts.
A refused admission halts; the router never falls back to another variant.
The run report and receipt name the variant through
:class:`~openadapt_flow.ir.VariantRouteStamp`, and ``variant-report.json`` in
the run directory records the full decision.

The replayer is not involved in routing. The chosen variant still runs every
gate it was admitted with, including its own identity and screen checks.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.admitted_composition import (
    ProcessContractError,
    live_bundle_content_digest,
    load_child_envelope,
    resolve_pointer,
)
from openadapt_flow.compiler.compose_authoring import workflow_param_names
from openadapt_flow.ir import RunReport, VariantRouteStamp, Workflow
from openadapt_flow.qualification_admission import (
    QualificationAdmissionError,
    QualificationSignerTrust,
    expected_from_payload,
    verify_qualification_admission,
)
from openadapt_flow.runtime.admitted_composition import (
    AdmittedCapability,
    AdmittedChildExecutor,
    execute,
)
from openadapt_flow.runtime.variant_stamp import variant_route_scope
from openadapt_flow.variants import VariantSet, VariantSpec, VariantWhen

ROUTER_ID: Literal["openadapt.variant-router/v0"] = "openadapt.variant-router/v0"
VARIANT_REPORT_FILENAME = "variant-report.json"

#: Mean 8-bit luminance at or below which a frame is classed ``dark``.
DARK_MAX_LUMINANCE = 96.0
#: Mean 8-bit luminance at or above which a frame is classed ``light``.
LIGHT_MIN_LUMINANCE = 160.0

RouteReason = Literal[
    "routed",
    "no_variant_matched",
    "ambiguous_variant_match",
    "first_screen_unavailable",
    "variant_admission_refused",
    "variant_bundle_unloadable",
    "run_dir_not_empty",
]

FirstScreen = Union[bytes, None, Callable[[], Optional[bytes]]]


class ScreenFingerprint(BaseModel):
    """What the router observed on the first screen. No pixels, no OCR text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    frame_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    mean_luminance: float = Field(ge=0.0, le=255.0)
    theme: Literal["light", "dark", "indeterminate"]
    #: Presence of each marker some variant names, keyed by the authored marker.
    markers: dict[str, bool] = Field(default_factory=dict)

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()


class VariantEvaluation(BaseModel):
    """One variant's conditions against this item."""

    model_config = ConfigDict(extra="forbid")

    variant: str
    matched: bool
    unmet: list[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    """The router's choice, or the named reason it refused to choose."""

    model_config = ConfigDict(extra="forbid")

    router: Literal["openadapt.variant-router/v0"] = ROUTER_ID
    variant_set: str
    variant_set_digest: str
    outcome: Literal["ROUTED", "HALTED"]
    reason: RouteReason
    detail: str = ""
    variant: Optional[str] = None
    variant_digest: Optional[str] = None
    evaluations: list[VariantEvaluation] = Field(default_factory=list)
    fingerprint: Optional[ScreenFingerprint] = None
    #: Names of the inputs the item supplied. Values are never recorded here.
    inputs_supplied: list[str] = Field(default_factory=list)
    model_calls: Literal[0] = 0


class VariantChildResult(BaseModel):
    """The governed run of the chosen variant."""

    model_config = ConfigDict(extra="forbid")

    outcome: str
    success: bool
    model_calls: int = 0
    run_dir: str
    report_path: Optional[str] = None
    report_sha256: Optional[str] = None
    #: True when the child's report.json carries this exact route stamp.
    report_names_variant: bool = False


class VariantRunReport(BaseModel):
    """Parent result of a routed run, written as ``variant-report.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.variant-run-report/v0"] = (
        "openadapt.variant-run-report/v0"
    )
    variant_set: str
    outcome: str
    reason: RouteReason
    detail: str = ""
    decision: RouteDecision
    stamp: Optional[VariantRouteStamp] = None
    child: Optional[VariantChildResult] = None
    started_at: str
    finished_at: str

    @property
    def success(self) -> bool:
        return self.outcome == "VERIFIED"


def classify_theme(mean_luminance: float) -> Literal["light", "dark", "indeterminate"]:
    """Luminance class of a frame. The middle band is never guessed."""

    if mean_luminance <= DARK_MAX_LUMINANCE:
        return "dark"
    if mean_luminance >= LIGHT_MIN_LUMINANCE:
        return "light"
    return "indeterminate"


def screen_fingerprint(
    frame_png: bytes, *, markers: list[str], vision: Any = None
) -> ScreenFingerprint:
    """Fingerprint one frame: size, luminance class, and marker presence."""

    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(frame_png)) as image:
        width, height = image.size
        mean = float(ImageStat.Stat(image.convert("L")).mean[0])
    found: dict[str, bool] = {}
    if markers:
        if vision is None:
            from openadapt_flow import vision as default_vision

            vision = default_vision
        lines = [str(getattr(line, "text", line)) for line in vision.ocr(frame_png)]
        for marker in markers:
            found[marker] = marker_on_lines(marker, lines)
    return ScreenFingerprint(
        frame_sha256=hashlib.sha256(frame_png).hexdigest(),
        width=width,
        height=height,
        mean_luminance=round(mean, 2),
        theme=classify_theme(mean),
        markers=found,
    )


def _words(text: str) -> str:
    return " ".join(text.split()).casefold()


def marker_on_lines(marker: str, lines: list[str]) -> bool:
    """True when ``marker`` appears as whole words on one OCR line.

    Case and runs of whitespace are ignored. A word character, or a ``.``
    followed by more text, on either side breaks the match, so ``Version 1``
    is not found in ``Version 11`` and ``Version 12`` is not found in
    ``Version 12.1``.
    """

    needle = re.escape(_words(marker))
    pattern = re.compile(rf"(?<![\w.]){needle}(?!\w|\.\w)")
    return any(pattern.search(_words(line)) for line in lines)


def _input_text(value: object) -> str:
    from openadapt_flow.runtime.authorization import runtime_param_text

    try:
        return runtime_param_text(value)  # type: ignore[arg-type]
    except Exception:
        return str(value)


def _unmet(
    when: VariantWhen,
    inputs: Mapping[str, object],
    fingerprint: Optional[ScreenFingerprint],
) -> list[str]:
    """Every condition of ``when`` that does not hold. Never echoes a value."""

    unmet: list[str] = []
    for name, allowed in when.inputs.items():
        if name not in inputs:
            unmet.append(f"input {name!r} was not supplied")
        elif _input_text(inputs[name]) not in allowed:
            unmet.append(f"input {name!r} is not one of {allowed}")
    if when.uses_screen:
        assert fingerprint is not None
        if when.theme and fingerprint.theme != when.theme:
            unmet.append(f"theme is {fingerprint.theme}, variant needs {when.theme}")
        for marker in when.text_present:
            if not fingerprint.markers.get(marker, False):
                unmet.append(f"marker {marker!r} is not on the first screen")
        for marker in when.text_absent:
            if fingerprint.markers.get(marker, True):
                unmet.append(f"marker {marker!r} is on the first screen")
    return unmet


def route_variant(
    variant_set: VariantSet,
    *,
    inputs: Mapping[str, object],
    first_screen: FirstScreen = None,
    vision: Any = None,
) -> RouteDecision:
    """Choose exactly one variant, or halt with a named reason.

    ``first_screen`` is the first frame as PNG bytes, or a callable that
    captures it. It is read only when some variant has a screen condition.
    """

    base: dict[str, Any] = {
        "variant_set": variant_set.name,
        "variant_set_digest": variant_set.digest(),
        "inputs_supplied": sorted(str(key) for key in inputs),
    }
    fingerprint: Optional[ScreenFingerprint] = None
    if variant_set.uses_screen:
        frame: Optional[bytes]
        try:
            frame = first_screen() if callable(first_screen) else first_screen
        except Exception as exc:
            return RouteDecision(
                **base,
                outcome="HALTED",
                reason="first_screen_unavailable",
                detail=f"the first screen could not be captured ({type(exc).__name__})",
            )
        if not frame:
            return RouteDecision(
                **base,
                outcome="HALTED",
                reason="first_screen_unavailable",
                detail=(
                    "a variant has a screen condition but no first screen was "
                    "supplied or captured"
                ),
            )
        try:
            fingerprint = screen_fingerprint(
                frame, markers=variant_set.markers, vision=vision
            )
        except Exception as exc:
            return RouteDecision(
                **base,
                outcome="HALTED",
                reason="first_screen_unavailable",
                detail=f"the first screen could not be read ({type(exc).__name__})",
            )
    evaluations = []
    for spec in variant_set.variants:
        unmet = _unmet(spec.when, inputs, fingerprint)
        evaluations.append(
            VariantEvaluation(variant=spec.name, matched=not unmet, unmet=unmet)
        )
    matched = [item.variant for item in evaluations if item.matched]
    base.update(evaluations=evaluations, fingerprint=fingerprint)
    if not matched:
        return RouteDecision(
            **base,
            outcome="HALTED",
            reason="no_variant_matched",
            detail=(
                f"none of the {len(evaluations)} variant(s) matches this item; "
                "record and admit a new variant, or correct the inputs"
            ),
        )
    if len(matched) > 1:
        return RouteDecision(
            **base,
            outcome="HALTED",
            reason="ambiguous_variant_match",
            detail=f"variants {matched} all match; the router does not guess",
        )
    chosen = matched[0]
    return RouteDecision(
        **base,
        outcome="ROUTED",
        reason="routed",
        variant=chosen,
        variant_digest=variant_set.variant_digest(chosen),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(run_dir: Path, report: VariantRunReport) -> VariantRunReport:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / VARIANT_REPORT_FILENAME).write_text(
        report.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    return report


def _admitted_variant(
    spec: VariantSpec,
    *,
    parent: Path,
    trusted_signers: Mapping[str, QualificationSignerTrust],
    revoked_admission_ids: set[str] | frozenset[str],
    now: datetime,
) -> tuple[Any, Workflow, Path]:
    """Verify the chosen variant's own admission. Raise with a named reason."""

    try:
        envelope = load_child_envelope(parent, spec)
    except ProcessContractError as exc:
        raise _Refusal("variant_admission_refused", str(exc)) from exc
    payload = envelope.payload
    if (
        payload.admission_id != spec.admission_id
        or payload.workflow_version_id != spec.workflow_version_id
        or payload.bundle_content_digest != spec.bundle_content_digest
    ):
        raise _Refusal(
            "variant_admission_refused",
            f"variant {spec.name!r} pointer does not match its admission envelope",
        )
    bundle = resolve_pointer(parent, spec.bundle)
    try:
        workflow = Workflow.load(bundle)
    except Exception as exc:
        raise _Refusal(
            "variant_bundle_unloadable",
            f"variant {spec.name!r} bundle could not be loaded ({type(exc).__name__})",
        ) from exc
    if live_bundle_content_digest(workflow, bundle) != payload.bundle_content_digest:
        raise _Refusal(
            "variant_admission_refused",
            f"variant {spec.name!r} live bundle digest does not match its admission",
        )
    try:
        verify_qualification_admission(
            envelope,
            trusted_signers=trusted_signers,
            expected=expected_from_payload(payload),
            revoked_admission_ids=revoked_admission_ids,
            now=now,
        )
    except QualificationAdmissionError as exc:
        raise _Refusal(
            "variant_admission_refused",
            f"variant {spec.name!r} admission refused before Execute: {exc}",
        ) from exc
    return envelope, workflow, bundle


class _Refusal(Exception):
    def __init__(self, reason: RouteReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: RouteReason = reason
        self.detail = detail


def execute_variant_set(
    variant_set: VariantSet,
    *,
    parent_dir: Path | str,
    run_dir: Path | str,
    inputs: Optional[Mapping[str, object]] = None,
    first_screen: FirstScreen = None,
    child_run: AdmittedChildExecutor,
    trusted_signers: Mapping[str, QualificationSignerTrust],
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    now: Optional[datetime] = None,
    vision: Any = None,
    dry_run: bool = False,
) -> VariantRunReport:
    """Route, verify the chosen variant's admission, then run it governed.

    With ``dry_run`` the router stops after the admission check and records
    outcome ``DRY_RUN``; nothing is executed.
    """

    parent = Path(parent_dir)
    root = Path(run_dir)
    started = _now()
    item_inputs = dict(inputs or {})
    decision = route_variant(
        variant_set, inputs=item_inputs, first_screen=first_screen, vision=vision
    )

    def halted(reason: RouteReason, detail: str) -> VariantRunReport:
        return _write(
            root,
            VariantRunReport(
                variant_set=variant_set.name,
                outcome="HALTED",
                reason=reason,
                detail=detail,
                decision=decision,
                started_at=started,
                finished_at=_now(),
            ),
        )

    if decision.outcome != "ROUTED" or decision.variant is None:
        return halted(decision.reason, decision.detail)
    spec = variant_set.variant(decision.variant)
    try:
        envelope, workflow, bundle = _admitted_variant(
            spec,
            parent=parent,
            trusted_signers=trusted_signers,
            revoked_admission_ids=revoked_admission_ids,
            now=now or datetime.now(timezone.utc),
        )
    except _Refusal as refusal:
        return halted(refusal.reason, refusal.detail)

    stamp = VariantRouteStamp(
        variant_set=variant_set.name,
        variant_set_digest=decision.variant_set_digest,
        variant=spec.name,
        variant_digest=str(decision.variant_digest),
        admission_id=spec.admission_id,
        bundle_content_digest=spec.bundle_content_digest,
        fingerprint_digest=(
            decision.fingerprint.digest() if decision.fingerprint is not None else None
        ),
    )
    child_dir = root / spec.name
    if child_dir.exists() and any(child_dir.iterdir()):
        # A child refused before it writes a report would otherwise leave an
        # earlier run's report.json to be read as this run's result.
        return halted(
            "run_dir_not_empty",
            f"{child_dir} already holds a run; use a fresh run directory",
        )
    if dry_run:
        return _write(
            root,
            VariantRunReport(
                variant_set=variant_set.name,
                outcome="DRY_RUN",
                reason="routed",
                detail=f"variant {spec.name!r} would run; its admission verified",
                decision=decision,
                started_at=started,
                finished_at=_now(),
            ),
        )
    accepted = workflow_param_names(workflow)
    child_inputs = {
        str(key): _input_text(value)
        for key, value in item_inputs.items()
        if key in accepted
    }
    with variant_route_scope(stamp):
        result = execute(
            AdmittedCapability(
                name=spec.name,
                admission_id=spec.admission_id,
                workflow_version_id=spec.workflow_version_id,
                bundle_content_digest=spec.bundle_content_digest,
            ),
            envelope,
            child_inputs,
            workflow=workflow,
            bundle_dir=bundle,
            run_dir=child_dir,
            child=spec.name,
            child_run=child_run,
        )
    report_path = Path(result.report_path) if result.report_path else None
    if report_path is None and (child_dir / "report.json").is_file():
        report_path = child_dir / "report.json"
    report_sha256: Optional[str] = None
    names_variant = False
    if report_path is not None and report_path.is_file():
        raw = report_path.read_bytes()
        report_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            names_variant = RunReport.model_validate_json(raw).variant_route == stamp
        except ValueError:
            names_variant = False
    child = VariantChildResult(
        outcome=result.outcome,
        success=bool(result.success),
        model_calls=int(result.model_calls),
        run_dir=str(child_dir),
        report_path=str(report_path) if report_path is not None else None,
        report_sha256=report_sha256,
        report_names_variant=names_variant,
    )
    return _write(
        root,
        VariantRunReport(
            variant_set=variant_set.name,
            outcome=result.outcome,
            reason="routed",
            detail=f"variant {spec.name!r} ran under its own admission",
            decision=decision,
            stamp=stamp,
            child=child,
            started_at=started,
            finished_at=_now(),
        ),
    )
