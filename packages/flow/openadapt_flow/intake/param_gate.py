"""Refuse MCP/tool parameters that disagree with trusted intake provenance.

When an agent reads an untrusted intake document, injected text can steer it
to pass the wrong patient, amount, or other values into a taught tool (#1890).
Each :class:`~openadapt_flow.ir.ParamSpec` may declare ``intake_field``: the
intake field that may supply that tool parameter. The gate compares the
proposed tool values to a :class:`~openadapt_flow.provenance.ProvenanceFile`
produced by the local intake step (operator-trusted), and refuses on any
mismatch or missing binding. Values never appear in refusal text.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.ir import ParamSpec
from openadapt_flow.provenance import ProvenanceFile, same_value

__all__ = [
    "DEFAULT_GRANT_TTL_SECONDS",
    "IntakeGateDecision",
    "IntakeGrant",
    "IntakeGrantError",
    "IntakeGrantRegistry",
    "IntakeParamGateRefusal",
    "IntakeParamGateResult",
    "bound_param_names",
    "bundle_sha256",
    "check_tool_params",
    "load_param_specs",
    "requires_intake_provenance",
]


class IntakeParamGateRefusal(RuntimeError):
    """Proposed tool parameters failed the intake provenance gate."""

    def __init__(self, reason: str, *, mismatched: list[str], unbound: list[str]):
        super().__init__(reason)
        self.reason = reason
        self.mismatched = list(mismatched)
        self.unbound = list(unbound)


@dataclass(frozen=True)
class IntakeParamGateResult:
    """Outcome of :func:`check_tool_params` (no values, names only)."""

    ok: bool
    mismatched: tuple[str, ...] = ()
    unbound: tuple[str, ...] = ()
    reason: str = ""

    def raise_if_refused(self) -> None:
        if not self.ok:
            raise IntakeParamGateRefusal(
                self.reason,
                mismatched=list(self.mismatched),
                unbound=list(self.unbound),
            )


def _intake_field(spec: Any) -> Optional[str]:
    if isinstance(spec, ParamSpec):
        field = spec.intake_field
    elif isinstance(spec, Mapping):
        field = spec.get("intake_field")
    else:
        field = getattr(spec, "intake_field", None)
    if field is None:
        return None
    text = str(field).strip()
    return text or None


def bound_param_names(param_specs: Mapping[str, Any]) -> list[str]:
    """Tool parameter names that declare an ``intake_field`` binding."""

    return sorted(name for name, spec in param_specs.items() if _intake_field(spec))


def requires_intake_provenance(param_specs: Mapping[str, Any]) -> bool:
    """True when at least one parameter is intake-bound."""

    return bool(bound_param_names(param_specs))


def _kind_for(spec: Any, proven: Any) -> str:
    from openadapt_flow.ir import ParamKind
    from openadapt_flow.runtime.effects.readback_values import infer_value_kind

    if proven is not None and getattr(proven, "value_kind", None):
        return str(proven.value_kind)
    kind = getattr(spec, "type", None)
    if isinstance(spec, Mapping):
        kind = spec.get("type", kind)
    value = getattr(kind, "value", kind)
    if value == ParamKind.DATE or value == "date":
        return "date"
    if value == ParamKind.NUMBER or value == "number":
        return "currency"
    return infer_value_kind(str(getattr(proven, "value", "") or "x"))


def check_tool_params(
    proposed: Mapping[str, object],
    provenance: Optional[ProvenanceFile],
    param_specs: Mapping[str, Any],
) -> IntakeParamGateResult:
    """Compare proposed tool params to intake provenance for bound fields.

    Fail closed:

    * Any ``intake_field`` binding without a registered provenance file → refuse.
    * Bound field missing from provenance → refuse (``unbound``).
    * Proposed value disagrees with provenance → refuse (``mismatched``).

    Parameters without ``intake_field`` are ignored. Refusal reasons name
    parameters only, never values.
    """

    bound = bound_param_names(param_specs)
    if not bound:
        return IntakeParamGateResult(ok=True, reason="no intake-bound parameters")

    if provenance is None:
        return IntakeParamGateResult(
            ok=False,
            unbound=tuple(bound),
            reason=(
                "intake provenance is required for parameters bound to intake "
                "fields: " + ", ".join(bound)
            ),
        )

    mismatched: list[str] = []
    unbound: list[str] = []
    for name in bound:
        spec = param_specs[name]
        intake_name = _intake_field(spec)
        assert intake_name is not None
        proven = provenance.params.get(intake_name)
        if proven is None:
            unbound.append(name)
            continue
        if name not in proposed:
            unbound.append(name)
            continue
        proposed_text = "" if proposed[name] is None else str(proposed[name])
        kind = _kind_for(spec, proven)
        if not same_value(proposed_text, proven.value, kind):
            mismatched.append(name)

    if unbound or mismatched:
        parts: list[str] = []
        if mismatched:
            parts.append(
                "proposed tool values disagree with intake provenance for "
                + ", ".join(mismatched)
            )
        if unbound:
            parts.append(
                "intake provenance missing or incomplete for " + ", ".join(unbound)
            )
        return IntakeParamGateResult(
            ok=False,
            mismatched=tuple(mismatched),
            unbound=tuple(unbound),
            reason="; ".join(parts),
        )
    return IntakeParamGateResult(ok=True, reason="intake provenance matches")


# -- grants: provenance bound to one document, one bundle and one run ---------

#: How long a registered intake grant stays usable. A grant is for the run the
#: operator is about to start from one document, not a standing allow-list.
DEFAULT_GRANT_TTL_SECONDS = 15 * 60


class IntakeGrantError(ValueError):
    """Intake provenance can't be registered for this workflow."""


def load_param_specs(bundle_dir: Path | str) -> Optional[dict[str, Any]]:
    """The bundle's ``param_specs``, or None when the bundle can't be loaded.

    None is not "no bindings": a caller that can't tell whether parameters are
    intake-bound must refuse when anything says they might be.
    """

    from openadapt_flow.ir import Workflow

    try:
        wf = Workflow.load(Path(bundle_dir))
    except Exception:  # noqa: BLE001 - any load failure is "unknown"
        return None
    return dict(wf.param_specs or {})


def bundle_sha256(bundle_dir: Path | str) -> str:
    """SHA-256 over the bundle's workflow and every file it references.

    Recomputed from the files, never read from a manifest, so a swapped or
    edited bundle has a different digest.
    """

    from openadapt_flow.authorization_summary import bundle_digest

    return bundle_digest(Path(bundle_dir))


def _declares_intake(param_types: Optional[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(meta, Mapping) and meta.get("intake_field")
        for meta in (param_types or {}).values()
    )


@dataclass
class IntakeGrant:
    """Operator-registered provenance for one document, bundle and run."""

    workflow: str
    provenance: ProvenanceFile
    provenance_sha256: str
    documents: tuple[str, ...]
    bundle_sha256: str
    expires_at: float
    #: The request that used this grant. A replay of that same request may
    #: pass again (it returns the same run); any other request is refused.
    consumed_by: Optional[str] = None


@dataclass(frozen=True)
class IntakeGateDecision:
    """Whether a run may start, and with which values. Names only, no values."""

    ok: bool
    reason: str = ""
    #: The values to run with: the proven intake values replace the caller's
    #: for every bound parameter, so normalization can't pick another record.
    values: dict[str, Any] = field(default_factory=dict)
    grant: Optional[IntakeGrant] = None


_LEGACY_SINGLE_USE = "\x00legacy-run"


class IntakeGrantRegistry:
    """Single-use, expiring intake grants, shared by every run entry point."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_GRANT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._grants: dict[str, IntakeGrant] = {}
        self._lock = threading.Lock()

    def register(
        self,
        names: Sequence[str],
        provenance_path: Path | str,
        bundle_dir: Path | str,
        *,
        document_sha256: Optional[str] = None,
    ) -> IntakeGrant:
        """Bind provenance to the bundle that will run, for one run.

        ``names`` are every name the workflow is reachable by (catalog name,
        legacy id); they share one grant, so it can't be used once per name.
        Registering again replaces the earlier grant.
        """

        from openadapt_flow.provenance import load_provenance

        keys = [n for n in dict.fromkeys(names) if n]
        if not keys:
            raise IntakeGrantError("no workflow name for intake provenance")
        raw = Path(provenance_path).read_bytes()
        provenance = load_provenance(provenance_path)
        documents = tuple(sorted(provenance.documents()))
        if not documents:
            raise IntakeGrantError("intake provenance must come from a source document")
        if document_sha256 is not None and document_sha256 not in documents:
            raise IntakeGrantError(
                "intake provenance was not read from the named document"
            )
        try:
            digest = bundle_sha256(bundle_dir)
        except Exception as exc:  # noqa: BLE001 - unreadable bundle
            raise IntakeGrantError("the workflow bundle could not be read") from exc
        grant = IntakeGrant(
            workflow=keys[0],
            provenance=provenance,
            provenance_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            documents=documents,
            bundle_sha256=digest,
            expires_at=self._clock() + self.ttl_seconds,
        )
        with self._lock:
            for key in keys:
                self._grants[key] = grant
        return grant

    def get(self, names: Sequence[str]) -> Optional[IntakeGrant]:
        with self._lock:
            for name in names:
                if name and name in self._grants:
                    return self._grants[name]
        return None

    def _drop(self, grant: IntakeGrant) -> None:
        for key in [k for k, g in self._grants.items() if g is grant]:
            del self._grants[key]

    def gate(
        self,
        names: Sequence[str],
        bundle_dir: Path | str,
        proposed: Mapping[str, Any],
        *,
        param_types: Optional[Mapping[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> IntakeGateDecision:
        """Check a run against its grant, and use the grant up if it passes.

        ``bundle_dir`` must be the bundle that will actually run. Fails
        closed: specs that can't be loaded when anything says the workflow is
        intake-bound, a missing, expired, used or other-bundle grant, and any
        mismatch all refuse. A legacy call with no ``request_id`` can use a
        grant once and never replay it.
        """

        values = dict(proposed)
        specs = load_param_specs(bundle_dir)
        with self._lock:
            grant = next(
                (self._grants[n] for n in names if n and n in self._grants), None
            )
            if specs is None:
                if grant is not None or _declares_intake(param_types):
                    return IntakeGateDecision(
                        ok=False,
                        reason="the workflow's parameter specs could not be "
                        "loaded, so intake-bound parameters can't be checked",
                    )
                return IntakeGateDecision(ok=True, values=values)
            bound = requires_intake_provenance(specs)
            if grant is None:
                if not bound:
                    return IntakeGateDecision(ok=True, values=values)
                result = check_tool_params(proposed, None, specs)
                return IntakeGateDecision(ok=False, reason=result.reason)
            if self._clock() > grant.expires_at:
                self._drop(grant)
                return IntakeGateDecision(
                    ok=False,
                    reason="intake provenance expired; register it again for "
                    "this document",
                )
            try:
                digest = bundle_sha256(bundle_dir)
            except Exception:  # noqa: BLE001 - unreadable bundle
                digest = None
            if digest != grant.bundle_sha256:
                return IntakeGateDecision(
                    ok=False,
                    reason="the workflow bundle changed since intake provenance "
                    "was registered",
                )
            token = request_id if request_id else _LEGACY_SINGLE_USE
            if grant.consumed_by is not None and (
                grant.consumed_by != token or token == _LEGACY_SINGLE_USE
            ):
                return IntakeGateDecision(
                    ok=False,
                    reason="intake provenance was already used for another run; "
                    "register it again for a new document",
                )
            result = check_tool_params(proposed, grant.provenance, specs)
            if not result.ok:
                return IntakeGateDecision(ok=False, reason=result.reason)
            for name in bound_param_names(specs):
                intake_name = _intake_field(specs[name])
                assert intake_name is not None
                values[name] = grant.provenance.params[intake_name].value
            grant.consumed_by = token
            return IntakeGateDecision(ok=True, values=values, grant=grant)
