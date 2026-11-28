"""Flight recorder for a computer-use agent you already run.

``openadapt-flow flight -- CMD`` starts a loopback proxy in front of the
model API that ``CMD`` calls, and on macOS runs ``CMD`` under an OS egress
block (:mod:`openadapt_flow.flight.egress`). The proxy meters and prices each
call, scrubs the text and the PNG/JPEG/GIF/BMP/TIFF/WEBP screenshots in each
request before it leaves the machine (see FLIGHT_RECORDER.md for what is
not covered), and
logs each computer action the model asks for. A one-line executor swap
(:class:`GatedComputer` for the Anthropic computer tool,
:class:`GatedComputerCall` for an OpenAI ``computer_call`` loop) holds each
click that is not known to be safe for a person.

The run directory holds ``flight.json`` (``openadapt.flight-run/v1``),
``timeline.jsonl``, scrubbed frame thumbnails and ``timeline.html``.
See ``docs/FLIGHT_RECORDER.md``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "COMMIT_VERBS",
    "FLIGHT_SCHEMA",
    "ORD_NUM_KIND",
    "SAFE_LABELS",
    "EgressRefused",
    "FileGate",
    "FlightProxy",
    "GateAction",
    "GatedComputer",
    "GatedComputerCall",
    "PreCommitGate",
    "Pseudonyms",
    "RefuseGate",
    "ScrubbedEgress",
    "TerminalGate",
    "Timeline",
    "parse_canonical_number",
]

_LAZY = {
    "COMMIT_VERBS": "openadapt_flow.flight.computer",
    "ORD_NUM_KIND": "openadapt_flow.flight.pseudonym",
    "SAFE_LABELS": "openadapt_flow.flight.computer",
    "EgressRefused": "openadapt_flow.flight.egress",
    "ScrubbedEgress": "openadapt_flow.flight.egress",
    "GateAction": "openadapt_flow.flight.gate",
    "GatedComputer": "openadapt_flow.flight.computer",
    "GatedComputerCall": "openadapt_flow.flight.computer",
    "PreCommitGate": "openadapt_flow.flight.gate",
    "Pseudonyms": "openadapt_flow.flight.pseudonym",
    "TerminalGate": "openadapt_flow.flight.gate",
    "FileGate": "openadapt_flow.flight.gate",
    "RefuseGate": "openadapt_flow.flight.gate",
    "FlightProxy": "openadapt_flow.flight.proxy",
    "Timeline": "openadapt_flow.flight.timeline",
    "FLIGHT_SCHEMA": "openadapt_flow.flight.timeline",
    "parse_canonical_number": "openadapt_flow.flight.pseudonym",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(name)
    import importlib

    return getattr(importlib.import_module(module), name)
