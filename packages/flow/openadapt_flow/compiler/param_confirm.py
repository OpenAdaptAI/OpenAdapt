"""One-shot operator confirm pass for field-label parameter proposals.

The compiler NEVER silently turns a demonstrated constant into a run-varying
parameter: label-derived proposals (``compiler.annotate.FieldLabelAnnotator``)
are flagged into the bundle sidecar ``param_proposals.json`` and applied only
here, after a HUMAN decides. The parameter name is the thread the effect
verifier binds to (``ValueExpr(param=...)`` in
``openadapt_flow.runtime.effects.effect``) and the name run inputs / secrets
key off, so naming is human-confirmed, never inferred behind the operator's
back.

Three ways to decide, one application point:

- **Interactive** (``openadapt-flow compile`` on a TTY): the flagged
  proposals are listed in ONE batch -- proposed name, source field label, the
  demonstrated value with its middle masked -- and the operator picks
  confirm / rename / mark-secret / keep-constant per proposal.
- **``--accept-params name1,name2``**: confirm the named proposals as-is
  (automation-friendly).
- **``--params-from decisions.json``**: full decision file --
  ``{"<proposed_name>": {"action": "confirm"|"rename"|"secret"|"constant",
  "name": "<new name, for rename>"}}``. Proposals not mentioned default to
  ``constant``.

Accepted decisions are applied by RECOMPILING the recording with
``compile_recording(param_overrides=..., secret_param_steps=...)`` so the
confirmed parameter flows through the exact recorded-``param=`` machinery
(example/default, postcondition exclusion, leakage lint, secret handling).
Unconfirmed proposals remain constants -- fail-closed, the bundle replays
exactly as demonstrated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import Workflow

PROPOSALS_FILENAME = "param_proposals.json"

_ACTIONS = ("confirm", "rename", "secret", "constant")


@dataclass(frozen=True)
class ParamProposal:
    """One flagged proposal from the bundle sidecar (nothing applied yet)."""

    step_id: str
    name: str
    field_label: Optional[str]
    masked_example: Optional[str]


@dataclass(frozen=True)
class ParamDecision:
    """One ACCEPTED operator decision (keep-constant produces no decision)."""

    step_id: str
    name: str
    secret: bool = False


def load_proposals(bundle_dir: Path | str) -> list[ParamProposal]:
    """Read the flagged proposals sidecar; empty list when there is none."""
    path = Path(bundle_dir) / PROPOSALS_FILENAME
    if not path.is_file():
        return []
    raw = json.loads(path.read_text())
    proposals: list[ParamProposal] = []
    for entry in raw.get("proposals", []) or []:
        if not isinstance(entry, dict):
            raise ValueError(f"{PROPOSALS_FILENAME}: malformed proposal {entry!r}")
        proposals.append(
            ParamProposal(
                step_id=str(entry["step_id"]),
                name=str(entry["name"]),
                field_label=entry.get("field_label"),
                masked_example=entry.get("masked_example"),
            )
        )
    return proposals


def _by_name(proposals: Iterable[ParamProposal]) -> dict[str, ParamProposal]:
    out: dict[str, ParamProposal] = {}
    for p in proposals:
        out[p.name] = p
    return out


def decisions_from_accept_list(
    proposals: list[ParamProposal], accept_names: Iterable[str]
) -> list[ParamDecision]:
    """Confirm the named proposals as-is; unknown names fail loud."""
    known = _by_name(proposals)
    decisions: list[ParamDecision] = []
    for name in accept_names:
        proposal = known.get(name)
        if proposal is None:
            raise ValueError(
                f"--accept-params: no flagged proposal named {name!r} "
                f"(available: {sorted(known) or 'none'})"
            )
        decisions.append(ParamDecision(step_id=proposal.step_id, name=name))
    return decisions


def decisions_from_file(
    proposals: list[ParamProposal], decisions_json: dict[str, Any]
) -> list[ParamDecision]:
    """Resolve a ``--params-from`` decision mapping; anything odd fails loud.

    Proposals not mentioned in the mapping default to ``constant``
    (fail-closed). A mapping entry for a non-existent proposal is an error --
    a decision that cannot be applied exactly as written must never be
    silently dropped.
    """
    known = _by_name(proposals)
    decisions: list[ParamDecision] = []
    for name, raw in decisions_json.items():
        proposal = known.get(name)
        if proposal is None:
            raise ValueError(
                f"--params-from: no flagged proposal named {name!r} "
                f"(available: {sorted(known) or 'none'})"
            )
        if not isinstance(raw, dict):
            raise ValueError(
                f"--params-from: decision for {name!r} must be an object "
                f'like {{"action": "confirm"}}, got {raw!r}'
            )
        action = raw.get("action")
        if action not in _ACTIONS:
            raise ValueError(
                f"--params-from: decision for {name!r} has invalid action "
                f"{action!r} (one of {list(_ACTIONS)})"
            )
        if action == "constant":
            continue
        final_name = name
        if action == "rename" or raw.get("name"):
            renamed = raw.get("name")
            if not isinstance(renamed, str) or not renamed:
                raise ValueError(
                    f'--params-from: rename for {name!r} needs a non-empty "name"'
                )
            final_name = renamed
        decisions.append(
            ParamDecision(
                step_id=proposal.step_id,
                name=final_name,
                secret=(action == "secret"),
            )
        )
    return decisions


def decisions_interactive(
    proposals: list[ParamProposal],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> list[ParamDecision]:
    """One-batch interactive review: confirm / rename / secret / constant.

    The default is KEEP CONSTANT (fail-closed): pressing Enter changes
    nothing about the bundle. The demonstrated value is shown middle-masked;
    the full literal is never reprinted here.
    """
    if not proposals:
        return []
    output_fn(
        f"\n{len(proposals)} typed value(s) look like parameters "
        "(named from the field labels you typed into)."
    )
    output_fn(
        "Confirming makes the value replaceable per run; unconfirmed values "
        "stay exactly as demonstrated."
    )
    decisions: list[ParamDecision] = []
    for p in proposals:
        label = f" (field label: {p.field_label!r})" if p.field_label else ""
        value = f" value: {p.masked_example}" if p.masked_example else ""
        output_fn(f"\n[{p.step_id}] proposed parameter {p.name!r}{label}{value}")
        while True:
            choice = (
                input_fn("  [c]onfirm / [r]ename / [s]ecret / [k]eep constant (k): ")
                .strip()
                .lower()
            )
            if choice in ("", "k", "keep", "constant"):
                break
            if choice in ("c", "confirm", "y", "yes"):
                decisions.append(ParamDecision(step_id=p.step_id, name=p.name))
                break
            if choice in ("r", "rename"):
                new_name = input_fn(f"  new name for {p.name!r}: ").strip()
                if new_name:
                    decisions.append(ParamDecision(step_id=p.step_id, name=new_name))
                    break
                output_fn("  empty name; try again")
                continue
            if choice in ("s", "secret"):
                decisions.append(
                    ParamDecision(step_id=p.step_id, name=p.name, secret=True)
                )
                break
            output_fn("  please answer c, r, s, or k")
    return decisions


def apply_decisions(
    recording_dir: Path | str,
    bundle_dir: Path | str,
    *,
    name: str,
    decisions: list[ParamDecision],
    **compile_kwargs: Any,
) -> Optional["Workflow"]:
    """Recompile the recording with the accepted decisions applied.

    Returns the recompiled :class:`~openadapt_flow.ir.Workflow`. With no
    accepted decisions this is a no-op returning None -- the first compile's
    bundle already IS the fail-closed outcome.
    """
    if not decisions:
        return None
    from openadapt_flow.compiler.compile import compile_recording

    seen: set[str] = set()
    for d in decisions:
        if d.name in seen:
            raise ValueError(f"duplicate confirmed parameter name {d.name!r}")
        seen.add(d.name)
    return compile_recording(
        recording_dir,
        bundle_dir,
        name=name,
        param_overrides={d.step_id: d.name for d in decisions},
        secret_param_steps=frozenset(d.step_id for d in decisions if d.secret),
        **compile_kwargs,
    )
