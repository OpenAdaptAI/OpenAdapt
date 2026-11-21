"""Flight recorder for a computer-use agent you already run.

``openadapt-flow flight -- CMD`` starts a loopback proxy in front of the
model API that ``CMD`` calls. The proxy meters and prices each call, scrubs
every screenshot and text block before it leaves the machine, and logs each
computer action the model asks for. A one-line executor swap
(:class:`GatedComputer` for the Anthropic computer tool,
:class:`GatedComputerCall` for an OpenAI ``computer_call`` loop) holds each
commit-class action (a click on Save, Submit, Send...) for a person.

The run directory holds ``flight.json`` (``openadapt.flight-run/v1``),
``timeline.jsonl``, scrubbed frame thumbnails and ``timeline.html``.
See ``docs/FLIGHT_RECORDER.md``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "COMMIT_VERBS",
    "FLIGHT_SCHEMA",
    "FileGate",
    "FlightProxy",
    "GateAction",
    "GatedComputer",
    "GatedComputerCall",
    "PreCommitGate",
    "RefuseGate",
    "TerminalGate",
    "Timeline",
]

_LAZY = {
    "COMMIT_VERBS": "openadapt_flow.flight.computer",
    "GateAction": "openadapt_flow.flight.gate",
    "GatedComputer": "openadapt_flow.flight.computer",
    "GatedComputerCall": "openadapt_flow.flight.computer",
    "PreCommitGate": "openadapt_flow.flight.gate",
    "TerminalGate": "openadapt_flow.flight.gate",
    "FileGate": "openadapt_flow.flight.gate",
    "RefuseGate": "openadapt_flow.flight.gate",
    "FlightProxy": "openadapt_flow.flight.proxy",
    "Timeline": "openadapt_flow.flight.timeline",
    "FLIGHT_SCHEMA": "openadapt_flow.flight.timeline",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(name)
    import importlib

    return getattr(importlib.import_module(module), name)
