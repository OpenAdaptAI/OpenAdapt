"""The floor a generated record contract may never go below.

Generation reads untrusted content: page text, response bodies, field values.
Whatever a generator proposes, the contract it hands to a person must keep a
fixed set of checks that does not come from that content:

- every parameter the demonstration typed is checked in the saved record;
- the frame (nothing else in the read set changes) is on, compared by record
  identity, with nothing excused by attribution;
- no ignored field is a field a parameter is written to, and no collateral
  rule is turned off.

:func:`baseline_problems` lists every way a contract falls short. A contract
with any problem is refused, so a generated part can add checks but never
remove or weaken one. Attribution, the one rule that excuses a change, is
safety policy: a person adds it, never a generator.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from openadapt_flow.runtime.effects.effect import Effect, EffectKind

#: Verifier kinds that read through the acting screen: never a generated channel.
_SCREEN_KINDS = frozenset({"none", "onscreen"})


def _param_fields(effects: Sequence[Effect]) -> dict[str, set[str]]:
    """Parameter -> the record fields effects check it against."""

    out: dict[str, set[str]] = {}
    for effect in effects:
        selectors = (
            list(effect.new_records)
            if effect.kind is EffectKind.EXACT_NEW_SET
            else [effect.match]
        )
        for selector in selectors:
            for field, expr in selector.items():
                if expr.param is not None:
                    out.setdefault(expr.param, set()).add(field)
        if effect.field and effect.value is not None and effect.value.param:
            out.setdefault(effect.value.param, set()).add(effect.field)
    return out


def baseline_problems(cfg: Any, typed_params: Iterable[str]) -> list[str]:
    """Every way ``cfg`` (a parsed ``verify.yaml``) is weaker than the floor.

    ``typed_params`` are the parameters the demonstration typed into the app:
    each must be checked against a record field.
    """

    problems: list[str] = []
    effects: list[Effect] = list(cfg.effects)
    if cfg.verifier.kind in _SCREEN_KINDS:
        problems.append(f"read channel {cfg.verifier.kind!r} reads the acting screen")
    if not any(
        e.kind in (EffectKind.RECORD_WRITTEN, EffectKind.EXACT_NEW_SET)
        and (e.expected_count >= 1 or e.kind is EffectKind.EXACT_NEW_SET)
        for e in effects
    ):
        problems.append("no effect requires the saved record to exist")
    checked = _param_fields(effects)
    for name in sorted(set(typed_params)):
        if name not in checked:
            problems.append(f"parameter {name!r} is not checked in the saved record")
    for effect in effects:
        if "forbid_collateral_loss" in effect.model_fields_set and not (
            effect.forbid_collateral_loss
        ):
            problems.append("an effect turns off the collateral-loss rule")
        if "forbid_collateral_change" in effect.model_fields_set and not (
            effect.forbid_collateral_change
        ):
            problems.append("an effect turns off the collateral-change rule")
        if effect.needs_operator_confirmation:
            problems.append("an effect is still a placeholder")
    frame = getattr(cfg, "frame", None)
    if frame is not None:
        if frame.attribution is not None:
            problems.append(
                "the frame excuses changes by attribution, which only a person sets"
            )
        if frame.target_fields is not None:
            problems.append("the frame widens what a target record may change")
        written = {f for fields in checked.values() for f in fields}
        hidden = sorted(set(frame.ignore_fields) & written)
        if hidden:
            problems.append(
                "the frame ignores field(s) a parameter is written to: "
                + ", ".join(hidden)
            )
    drop = set(cfg.verifier.records_drop_fields)
    for sub in cfg.verifier.surfaces.values():
        drop |= set(sub.records_drop_fields)
    written = {f for fields in checked.values() for f in fields}
    dropped = sorted(drop & written)
    if dropped:
        problems.append(
            "the read drops field(s) a parameter is written to: " + ", ".join(dropped)
        )
    return problems
