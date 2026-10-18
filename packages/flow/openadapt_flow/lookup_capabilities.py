"""Which lookups a workflow was qualified with, and which a run can offer.

Flow's resolution ladder looks a step's target up in the page or
accessibility structure (DOM, AX, UIA, AT-SPI) before it falls back to pixels.
Structure is far more reliable where it exists, but it does not exist over a
remote display viewed from outside the session: an RDP or Citrix client window
shows Flow only pixels. A workflow qualified with structure lookups could pass
qualification and then fail in production on such a surface.

This module holds the small, import-light rules that close that gap:

- the deployment surface vocabulary (``local``, ``remote_in_session``,
  ``remote_display``, ``unknown``) and how an undeclared or unknown surface
  resolves (a remote-display target defaults to pixels only);
- which lookup capabilities a surface offers (``structure``, ``pixels``);
- which capabilities a qualification actually used, read from its run
  reports;
- the qualified-lookup record kept in the qualification project, including
  the pixels-only pass every ``openadapt-flow qualify`` runs;
- the run-time refusal when a surface offers fewer capabilities than the
  workflow was qualified with.

See ``docs/SURFACES.md`` ("Lookup capabilities").
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, Workflow

#: Where the Flow runtime runs relative to the application it drives.
#:
#: - ``local``: on the same machine as the application (a browser, a native
#:   desktop app). Structure is available where the backend reads it.
#: - ``remote_in_session``: inside a remote or virtual desktop session (for
#:   example a Windows runtime installed in the Citrix or RDP session).
#:   Accessibility is genuinely available there, so structure is allowed.
#: - ``remote_display``: outside the remote session, looking at its client
#:   window or display stream. Pixels only.
DeploymentSurface = Literal["local", "remote_in_session", "remote_display"]

#: What an operator may write in ``deployment.yaml`` or ``verify.yaml``.
#: ``unknown`` resolves to ``remote_display``, the most conservative reading.
DeclaredSurface = Literal["local", "remote_in_session", "remote_display", "unknown"]

LookupCapability = Literal["pixels", "structure"]

DEPLOYMENT_SURFACES: tuple[str, ...] = (
    "local",
    "remote_in_session",
    "remote_display",
    "unknown",
)

#: Backend kinds that view a remote session from outside, through its client
#: window or the remote-display protocol. They never read the remote
#: application's structure.
REMOTE_DISPLAY_KINDS = frozenset({"rdp", "citrix", "remote-display", "remote_display"})

#: Backend kinds that can read structure when they run next to the application.
STRUCTURE_KINDS = frozenset({"web", "windows", "macos", "linux"})

QUALIFIED_LOOKUP_SCHEMA: Literal["openadapt.qualified-lookup/v1"] = (
    "openadapt.qualified-lookup/v1"
)


def _kind(kind: Optional[str]) -> str:
    return str(kind or "").strip().lower()


def resolve_deployment_surface(
    declared: Optional[str], backend_kind: Optional[str]
) -> DeploymentSurface:
    """Resolve the surface a run or qualification targets.

    An explicit ``local``, ``remote_in_session`` or ``remote_display`` wins.
    ``unknown`` is read as ``remote_display``. With nothing declared, an RDP or
    Citrix backend is ``remote_display`` and every other backend is ``local``.
    """

    value = _kind(declared)
    if value in ("local", "remote_in_session", "remote_display"):
        return value  # type: ignore[return-value]
    if value == "unknown":
        return "remote_display"
    if value:
        raise ValueError(
            f"unknown deployment surface {declared!r}; expected one of "
            f"{', '.join(DEPLOYMENT_SURFACES)}"
        )
    if _kind(backend_kind) in REMOTE_DISPLAY_KINDS:
        return "remote_display"
    return "local"


def declared_surface_problem(
    declared: Optional[str], backend_kind: Optional[str]
) -> Optional[str]:
    """Return why a declared surface contradicts the backend, or None.

    An RDP or Citrix backend drives the client window from outside the
    session, so it cannot be ``local`` or ``remote_in_session``. A runtime
    installed inside the remote session uses that session's own backend
    (``windows``, ``web``, ...) instead.
    """

    value = _kind(declared)
    if value in ("local", "remote_in_session") and (
        _kind(backend_kind) in REMOTE_DISPLAY_KINDS
    ):
        return (
            f"deployment_surface {value!r} contradicts backend kind "
            f"{_kind(backend_kind)!r}: the rdp and citrix backends view the "
            "remote session from outside (remote_display). A runtime installed "
            "inside the session uses that session's own backend, for example "
            "windows."
        )
    return None


def surface_offers_structure(
    surface: DeploymentSurface, backend_kind: Optional[str]
) -> bool:
    """True when structure lookups are possible on this surface and backend."""

    if surface == "remote_display":
        return False
    return _kind(backend_kind) in STRUCTURE_KINDS


def offered_capabilities(*, structure: bool) -> list[LookupCapability]:
    return ["pixels", "structure"] if structure else ["pixels"]


def _rungs(report: "RunReport") -> Iterable[str]:
    for result in report.results:
        for resolution in (result.resolution, result.drag_end_resolution):
            if resolution is not None:
                yield resolution.rung


def capabilities_used(
    reports: Iterable["RunReport"], *, structure_offered: bool
) -> list[LookupCapability]:
    """The lookup capabilities the given runs actually resolved targets with.

    A step resolved on the ``structural`` rung used structure; any other rung
    used pixels. When no step resolved at all, the answer is the conservative
    one: everything that was offered.
    """

    used: set[LookupCapability] = set()
    for report in reports:
        for rung in _rungs(report):
            used.add("structure" if rung == "structural" else "pixels")
    if not used:
        return offered_capabilities(structure=structure_offered)
    return sorted(used)


class PixelsOnlyPass(BaseModel):
    """The qualification pass run with structure lookups disabled."""

    model_config = ConfigDict(extra="forbid")

    #: ``gate`` when the target surface is pixels only (its result decides
    #: qualification); ``informational`` otherwise.
    role: Literal["gate", "informational"]
    cases: list[str] = Field(default_factory=list)
    trials_run: int = Field(ge=0)
    trials_passed: int = Field(ge=0)
    #: Short reasons for the failed trials, one per failure, in run order.
    failures: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _counts(self) -> "PixelsOnlyPass":
        if self.trials_passed > self.trials_run:
            raise ValueError("trials_passed cannot exceed trials_run")
        return self

    @property
    def pass_rate(self) -> Optional[float]:
        if self.trials_run == 0:
            return None
        return self.trials_passed / self.trials_run

    @property
    def passed(self) -> bool:
        return self.trials_run > 0 and self.trials_passed == self.trials_run

    def rate_text(self) -> str:
        rate = self.pass_rate
        percent = "n/a" if rate is None else f"{rate:.0%}"
        return f"{self.trials_passed}/{self.trials_run} ({percent})"

    def common_failure(self) -> Optional[str]:
        """The most frequent failure reason, without its case and trial."""

        reasons: dict[str, int] = {}
        for failure in self.failures:
            reason = failure.split(": ", 1)[1] if ": " in failure else failure
            reasons[reason] = reasons.get(reason, 0) + 1
        if not reasons:
            return None
        return max(reasons.items(), key=lambda item: item[1])[0]


class QualifiedLookup(BaseModel):
    """What a qualification's lookups ran on, kept in the qualification project.

    ``capabilities_used`` is read from the run reports of the signed cases.
    ``surface`` is the deployment surface the qualification was for.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.qualified-lookup/v1"] = QUALIFIED_LOOKUP_SCHEMA
    surface: DeploymentSurface
    target_kind: str = Field(min_length=1, max_length=32)
    structure_offered: bool
    capabilities_used: list[LookupCapability] = Field(min_length=1)
    pixels_only: PixelsOnlyPass

    @field_validator("capabilities_used")
    @classmethod
    def _sorted_unique(cls, value: list[str]) -> list[str]:
        cleaned = sorted(set(value))
        if len(cleaned) != len(value):
            raise ValueError("capabilities_used must be unique")
        return cleaned

    @model_validator(mode="after")
    def _consistent(self) -> "QualifiedLookup":
        if "structure" in self.capabilities_used and not self.structure_offered:
            raise ValueError("structure cannot be used when it was not offered")
        if self.surface == "remote_display" and self.structure_offered:
            raise ValueError("a remote_display surface offers no structure")
        if self.surface == "remote_display" and self.pixels_only.role != "gate":
            raise ValueError("the pixels-only pass gates a remote_display surface")
        return self

    @property
    def requires_structure(self) -> bool:
        return "structure" in self.capabilities_used

    def contract_sha256(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def qualified_lookup_of(workflow: "Workflow") -> Optional[QualifiedLookup]:
    project = getattr(workflow, "qualification", None)
    return getattr(project, "lookup", None) if project is not None else None


def lookup_capability_refusal(
    workflow: "Workflow",
    *,
    surface: DeploymentSurface,
    structure_available: bool,
) -> Optional[str]:
    """Return the halt reason when this run offers fewer lookups than qualified.

    None when every capability the workflow was qualified with is available
    here, or when it was never certified. A certified workflow with no
    qualified-lookup record halts where structure is missing.
    """

    record = qualified_lookup_of(workflow)
    if structure_available:
        return None
    if record is None:
        # A workflow certified without a lookup record (the manual
        # `qualification run` + `certify` path) can't show it was qualified on
        # pixels alone, so it doesn't run where structure is missing.
        project = getattr(workflow, "qualification", None)
        if project is None or getattr(project, "last_certification", None) is None:
            return None
        return (
            "HALTED_BEFORE_EFFECT: this workflow was certified without a record "
            "of which lookups it was qualified with, and this run's "
            f"{surface} surface offers pixels only. Nothing was written. "
            "Requalify it with openadapt-flow qualify (app.surface: "
            "remote_display in verify.yaml) so it is qualified on pixels alone."
        )
    if not record.requires_structure:
        return None
    if surface == "remote_display":
        why = (
            "this run's surface is remote_display (a remote session viewed "
            "from outside), which offers pixels only"
        )
    else:
        why = (
            f"structure lookups are not available on this run's "
            f"{surface} surface and backend"
        )
    return (
        "HALTED_BEFORE_EFFECT: this workflow was qualified with structure "
        f"lookups (DOM or accessibility) on a {record.surface} surface, but "
        f"{why}. Nothing was written. Requalify the workflow for this "
        "surface: run openadapt-flow qualify with app.surface: remote_display "
        "in verify.yaml so it is qualified on pixels alone, or run Flow inside "
        "the remote session (deployment_surface: remote_in_session) where "
        "accessibility is available."
    )


__all__ = [
    "DEPLOYMENT_SURFACES",
    "QUALIFIED_LOOKUP_SCHEMA",
    "DeclaredSurface",
    "DeploymentSurface",
    "LookupCapability",
    "PixelsOnlyPass",
    "QualifiedLookup",
    "capabilities_used",
    "declared_surface_problem",
    "lookup_capability_refusal",
    "offered_capabilities",
    "qualified_lookup_of",
    "resolve_deployment_surface",
    "surface_offers_structure",
]
