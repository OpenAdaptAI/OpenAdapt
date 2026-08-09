"""Replay runtime: resolution ladder, postcondition verification, healing.

Public surface:

- :func:`openadapt_flow.runtime.resolver.resolve` — walk the resolution ladder
  for a single anchor against a live frame.
- :class:`openadapt_flow.runtime.replayer.Replayer` — execute a compiled
  Workflow against a Backend, verifying postconditions and healing drift.
- :mod:`openadapt_flow.runtime.grounder` — grounding rungs (protocol +
  NullGrounder + the PRIMARY OCRAnchorGrounder (openadapt-grounding) +
  FallbackGrounder + import-guarded AnthropicGrounder + build_grounder).
- :mod:`openadapt_flow.runtime.heal` — HealEvent construction/persistence and
  healed-bundle writing.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# Keep the established package-level API without importing the execution stack
# whenever a transport-only service needs one lightweight runtime submodule.
_LAZY_EXPORTS = {
    "AnthropicGrounder": "openadapt_flow.runtime.grounder",
    "FallbackGrounder": "openadapt_flow.runtime.grounder",
    "Grounder": "openadapt_flow.runtime.grounder",
    "GrounderMatch": "openadapt_flow.runtime.grounder",
    "NullGrounder": "openadapt_flow.runtime.grounder",
    "OCRAnchorGrounder": "openadapt_flow.runtime.grounder",
    "build_grounder": "openadapt_flow.runtime.grounder",
    "apply_heal": "openadapt_flow.runtime.heal",
    "build_heal_event": "openadapt_flow.runtime.heal",
    "persist_heal": "openadapt_flow.runtime.heal",
    "write_healed_bundle": "openadapt_flow.runtime.heal",
    "Replayer": "openadapt_flow.runtime.replayer",
    "RUNG_ORDER": "openadapt_flow.runtime.resolver",
    "is_below_ocr": "openadapt_flow.runtime.resolver",
    "resolve": "openadapt_flow.runtime.resolver",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "AnthropicGrounder",
    "FallbackGrounder",
    "Grounder",
    "GrounderMatch",
    "NullGrounder",
    "OCRAnchorGrounder",
    "Replayer",
    "RUNG_ORDER",
    "apply_heal",
    "build_grounder",
    "build_heal_event",
    "is_below_ocr",
    "persist_heal",
    "resolve",
    "write_healed_bundle",
]
