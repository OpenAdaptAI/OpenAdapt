#!/usr/bin/env python3
"""Native messaging host for the optional OpenAdapt Chrome extension and Co-Pilot.

Chrome talks to this process over stdin/stdout (length-prefixed JSON).
Supports three strictly validated local message envelopes against the directory
named in ``OPENADAPT_EXTENSION_RECORDING`` (or ``--recording``):

- ``EXTENSION_EVIDENCE`` -> redacted append to ``extension_evidence.jsonl``
- ``COPILOT_GET_STATE`` -> read validated ``CopilotViewState`` from ``copilot_state.json``
- ``COPILOT_SUBMIT_DECISION`` -> validate ``HumanResolutionInput`` and append to
  ``copilot_decisions.jsonl`` with ``idempotency_key`` deduplication.

Zero remote calls. Unknown or malformed envelopes fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# Allow running from a source checkout without install.
_FLOW_ROOT = Path(__file__).resolve().parents[2]
for _candidate in (
    _FLOW_ROOT,
    _FLOW_ROOT.parent / "types" / "src",
    _FLOW_ROOT.parent / "privacy" / "src",
):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from openadapt_flow.copilot_bridge import (  # noqa: E402
    COPILOT_STATE_SCHEMA,
    CopilotSessionBridge,
)
from openadapt_flow.extension_evidence import (  # noqa: E402
    ExtensionEvidenceBuffer,
    validate_native_message,
)
from openadapt_flow.form_tx.types import HumanResolutionInput  # noqa: E402


def read_message() -> Any:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None
    if len(raw_len) != 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        return None
    return json.loads(payload.decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    data = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _handle_copilot_get_state(
    msg: Mapping[str, Any], bridge: CopilotSessionBridge
) -> dict[str, Any]:
    extra = set(msg.keys()) - {"type", "schema"}
    if extra:
        raise ValueError(f"unexpected fields in COPILOT_GET_STATE: {sorted(extra)}")
    if msg.get("schema") not in (None, COPILOT_STATE_SCHEMA):
        raise ValueError(f"schema must be {COPILOT_STATE_SCHEMA}")
    state = bridge.read_state()
    return {
        "ok": True,
        "type": "COPILOT_STATE",
        "state": state.model_dump(mode="json") if state is not None else None,
    }


def _handle_copilot_submit_decision(
    msg: Mapping[str, Any], bridge: CopilotSessionBridge
) -> dict[str, Any]:
    extra = set(msg.keys()) - {"type", "decision", "schema"}
    if extra:
        raise ValueError(
            f"unexpected fields in COPILOT_SUBMIT_DECISION: {sorted(extra)}"
        )
    if msg.get("schema") not in (None, COPILOT_STATE_SCHEMA):
        raise ValueError(f"schema must be {COPILOT_STATE_SCHEMA}")
    raw_decision = msg.get("decision")
    if not isinstance(raw_decision, Mapping):
        raise ValueError("decision object required")
    decision = HumanResolutionInput.model_validate(raw_decision)
    accepted_new, total_count = bridge.append_decision(decision)
    return {
        "ok": True,
        "type": "COPILOT_DECISION_ACK",
        "accepted_new": accepted_new,
        "count": total_count,
        "halt_id": decision.halt_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--recording",
        default=os.environ.get("OPENADAPT_EXTENSION_RECORDING", ""),
        help="Recording or session directory for extension evidence and co-pilot state",
    )
    args, _unknown = parser.parse_known_args(argv)
    if not args.recording:
        # Fail closed: without a recording dir we acknowledge nothing useful.
        write_message({"ok": False, "error": "no_recording_dir"})
        return 2

    recording = Path(args.recording)
    bridge = CopilotSessionBridge(recording)
    buf = ExtensionEvidenceBuffer()
    # Reload any existing lines so reconnects append.
    existing = recording / "extension_evidence.jsonl"
    if existing.is_file():
        from openadapt_flow.extension_evidence import load_extension_evidence

        for line in load_extension_evidence(recording):
            buf.append(line)

    while True:
        try:
            msg = read_message()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            write_message({"ok": False, "error": f"invalid_json: {exc}"})
            continue
        if msg is None:
            break
        if not isinstance(msg, Mapping):
            write_message({"ok": False, "error": "native message must be an object"})
            continue

        msg_type = msg.get("type")
        if msg_type == "COPILOT_GET_STATE":
            try:
                write_message(_handle_copilot_get_state(msg, bridge))
            except (ValidationError, ValueError, OSError) as exc:
                write_message({"ok": False, "error": str(exc)})
            continue

        if msg_type == "COPILOT_SUBMIT_DECISION":
            try:
                write_message(_handle_copilot_submit_decision(msg, bridge))
            except (ValidationError, ValueError, OSError) as exc:
                write_message({"ok": False, "error": str(exc)})
            continue

        if msg_type == "EXTENSION_EVIDENCE":
            try:
                evidence = validate_native_message(msg)
            except ValueError as exc:
                write_message({"ok": False, "error": str(exc)})
                continue
            buf.append(evidence)
            buf.write(recording)
            write_message({"ok": True, "count": buf.count})
            continue

        write_message(
            {
                "ok": False,
                "error": (
                    "native message type must be EXTENSION_EVIDENCE, "
                    "COPILOT_GET_STATE, or COPILOT_SUBMIT_DECISION"
                ),
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
