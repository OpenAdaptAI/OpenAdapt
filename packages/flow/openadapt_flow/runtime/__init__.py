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


# Annotation-only declarations keep deferred names visible to static analysis.
# No value is bound until __getattr__ loads and memoizes that exact export.
AnthropicGrounder: Any
FallbackGrounder: Any
Grounder: Any
GrounderMatch: Any
NullGrounder: Any
OCRAnchorGrounder: Any
build_grounder: Any
apply_heal: Any
build_heal_event: Any
persist_heal: Any
write_healed_bundle: Any
Replayer: Any
RUNG_ORDER: Any
is_below_ocr: Any
resolve: Any


def __getattr__(name: str) -> Any:
    global AnthropicGrounder
    global FallbackGrounder
    global Grounder
    global GrounderMatch
    global NullGrounder
    global OCRAnchorGrounder
    global build_grounder
    global apply_heal
    global build_heal_event
    global persist_heal
    global write_healed_bundle
    global Replayer
    global RUNG_ORDER
    global is_below_ocr
    global resolve

    # Literal imports retain deferred loading even when an export shadows
    # its submodule (for example durable.resume). Named globals keep
    # normal package memoization without exposing a globals() mapping.
    from importlib import import_module

    if name == "AnthropicGrounder":
        AnthropicGrounder = import_module(
            "openadapt_flow.runtime.grounder"
        ).AnthropicGrounder
        return AnthropicGrounder
    if name == "FallbackGrounder":
        FallbackGrounder = import_module(
            "openadapt_flow.runtime.grounder"
        ).FallbackGrounder
        return FallbackGrounder
    if name == "Grounder":
        Grounder = import_module("openadapt_flow.runtime.grounder").Grounder
        return Grounder
    if name == "GrounderMatch":
        GrounderMatch = import_module("openadapt_flow.runtime.grounder").GrounderMatch
        return GrounderMatch
    if name == "NullGrounder":
        NullGrounder = import_module("openadapt_flow.runtime.grounder").NullGrounder
        return NullGrounder
    if name == "OCRAnchorGrounder":
        OCRAnchorGrounder = import_module(
            "openadapt_flow.runtime.grounder"
        ).OCRAnchorGrounder
        return OCRAnchorGrounder
    if name == "build_grounder":
        build_grounder = import_module("openadapt_flow.runtime.grounder").build_grounder
        return build_grounder
    if name == "apply_heal":
        apply_heal = import_module("openadapt_flow.runtime.heal").apply_heal
        return apply_heal
    if name == "build_heal_event":
        build_heal_event = import_module("openadapt_flow.runtime.heal").build_heal_event
        return build_heal_event
    if name == "persist_heal":
        persist_heal = import_module("openadapt_flow.runtime.heal").persist_heal
        return persist_heal
    if name == "write_healed_bundle":
        write_healed_bundle = import_module(
            "openadapt_flow.runtime.heal"
        ).write_healed_bundle
        return write_healed_bundle
    if name == "Replayer":
        Replayer = import_module("openadapt_flow.runtime.replayer").Replayer
        return Replayer
    if name == "RUNG_ORDER":
        RUNG_ORDER = import_module("openadapt_flow.runtime.resolver").RUNG_ORDER
        return RUNG_ORDER
    if name == "is_below_ocr":
        is_below_ocr = import_module("openadapt_flow.runtime.resolver").is_below_ocr
        return is_below_ocr
    if name == "resolve":
        resolve = import_module("openadapt_flow.runtime.resolver").resolve
        return resolve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
