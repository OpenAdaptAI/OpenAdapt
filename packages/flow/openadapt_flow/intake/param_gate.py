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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from openadapt_flow.ir import ParamSpec
from openadapt_flow.provenance import ProvenanceFile, same_value

__all__ = [
    "IntakeParamGateRefusal",
    "IntakeParamGateResult",
    "bound_param_names",
    "check_tool_params",
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
