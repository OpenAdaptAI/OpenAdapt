#!/usr/bin/env python3
"""Native messaging host for the optional OpenAdapt Chrome extension and Co-Pilot.

Chrome talks to this process over stdin/stdout (length-prefixed JSON).
Supports five strictly validated local message envelopes against the directory
named in ``OPENADAPT_EXTENSION_RECORDING`` (or ``--recording``):

- ``EXTENSION_EVIDENCE`` -> redacted append to ``extension_evidence.jsonl``
- ``COPILOT_GET_STATE`` -> read validated ``CopilotViewState`` from ``copilot_state.json``
- ``COPILOT_SUBMIT_DECISION`` -> validate the bound run/halt envelope and
  queue one idempotent operator decision for the live runtime to claim.
- ``COPILOT_AUTH_PAUSED`` -> record structural sign-in/MFA pause state in
  ``copilot_auth_state.json`` (zero credentials) and acknowledge the pause.
- ``COPILOT_AUTH_RESUMED`` -> clear structural sign-in/MFA pause state in
  ``copilot_auth_state.json`` and acknowledge resumption.

Zero remote calls. Unknown or malformed envelopes fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
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
from openadapt_flow.copilot_protocol import CopilotBoundDecision  # noqa: E402
from openadapt_flow.extension_evidence import (  # noqa: E402
    ExtensionEvidenceBuffer,
    validate_native_message,
)


def read_message() -> Any:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None
    if len(raw_len) != 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    if length > 1_048_576:
        raise ValueError("native message exceeds size limit")
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        return None
    return json.loads(payload.decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    data = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _extract_request_id(msg: Mapping[str, Any]) -> str | None:
    req_id = msg.get("request_id")
    if req_id is None:
        return None
    if not isinstance(req_id, str) or not req_id or len(req_id) > 64:
        raise ValueError("request_id must be a short non-empty string")
    return req_id


def _with_request_id(reply: dict[str, Any], request_id: str | None) -> dict[str, Any]:
    if request_id is not None:
        reply["request_id"] = request_id
    return reply


def _handle_copilot_get_state(
    msg: Mapping[str, Any], bridge: CopilotSessionBridge
) -> dict[str, Any]:
    extra = set(msg.keys()) - {"type", "schema", "request_id"}
    if extra:
        raise ValueError(f"unexpected fields in COPILOT_GET_STATE: {sorted(extra)}")
    if msg.get("schema") not in (None, COPILOT_STATE_SCHEMA):
        raise ValueError(f"schema must be {COPILOT_STATE_SCHEMA}")
    _extract_request_id(msg)
    state = bridge.read_state()
    return {
        "ok": True,
        "type": "COPILOT_STATE",
        "state": state.model_dump(mode="json") if state is not None else None,
    }


def _handle_copilot_submit_decision(
    msg: Mapping[str, Any], bridge: CopilotSessionBridge
) -> dict[str, Any]:
    extra = set(msg.keys()) - {"type", "decision", "binding", "schema", "request_id"}
    if extra:
        raise ValueError(
            f"unexpected fields in COPILOT_SUBMIT_DECISION: {sorted(extra)}"
        )
    if msg.get("schema") not in (None, COPILOT_STATE_SCHEMA):
        raise ValueError(f"schema must be {COPILOT_STATE_SCHEMA}")
    _extract_request_id(msg)
    raw_decision = msg.get("decision")
    if not isinstance(raw_decision, Mapping):
        raise ValueError("decision object required")
    item = CopilotBoundDecision.model_validate(
        {"decision": raw_decision, "binding": msg.get("binding")}
    )
    decision = item.decision
    accepted_new, total_count = bridge.append_bound_decision(item)
    return {
        "ok": True,
        "type": "COPILOT_DECISION_ACK",
        "accepted_new": accepted_new,
        "count": total_count,
        "halt_id": decision.halt_id,
    }


_VALID_AUTH_KINDS = frozenset({"mfa", "signin", "sso"})
_SAFE_SIGNAL_RE = re.compile(r"^[a-z0-9_]{1,32}$")
COPILOT_AUTH_STATE_FILE = "copilot_auth_state.json"


def _write_auth_state(recording: Path, payload: dict[str, Any]) -> None:
    recording.mkdir(parents=True, exist_ok=True)
    target = recording / COPILOT_AUTH_STATE_FILE
    tmp = recording / f".{COPILOT_AUTH_STATE_FILE}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def _load_paused_tabs(recording: Path) -> set[int]:
    target = recording / COPILOT_AUTH_STATE_FILE
    if not target.is_file():
        return set()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if (
        isinstance(data, Mapping)
        and data.get("paused") is True
        and isinstance(data.get("tab_id"), int)
        and not isinstance(data.get("tab_id"), bool)
        and data["tab_id"] >= 0
    ):
        return {int(data["tab_id"])}
    return set()


def _evidence_tab_id(evidence: Mapping[str, Any]) -> int | None:
    frame = evidence.get("frame")
    if isinstance(frame, Mapping):
        tid = frame.get("tab_id")
        if isinstance(tid, int) and not isinstance(tid, bool) and tid >= 0:
            return tid
    tid = evidence.get("tab_id")
    if isinstance(tid, int) and not isinstance(tid, bool) and tid >= 0:
        return tid
    return None


def _validate_tab_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("tab_id must be a non-negative integer")
    return value


def _handle_copilot_auth_paused(
    msg: Mapping[str, Any], recording: Path
) -> dict[str, Any]:
    extra = set(msg.keys()) - {
        "type",
        "tab_id",
        "kind",
        "signals",
        "origin",
        "schema",
        "request_id",
    }
    if extra:
        raise ValueError(f"unexpected fields in COPILOT_AUTH_PAUSED: {sorted(extra)}")
    if msg.get("schema") not in (None, COPILOT_STATE_SCHEMA):
        raise ValueError(f"schema must be {COPILOT_STATE_SCHEMA}")
    _extract_request_id(msg)
    tab_id = _validate_tab_id(msg.get("tab_id"))
    kind = msg.get("kind")
    if kind not in _VALID_AUTH_KINDS:
        raise ValueError("kind must be one of mfa, signin, sso")
    raw_signals = msg.get("signals", [])
    if not isinstance(raw_signals, list):
        raise ValueError("signals must be a list")
    signals = [
        s for s in raw_signals if isinstance(s, str) and _SAFE_SIGNAL_RE.match(s)
    ][:8]
    origin = msg.get("origin", "")
    if not isinstance(origin, str) or len(origin) > 256:
        raise ValueError("origin must be a short string")
    payload = {
        "schema": COPILOT_STATE_SCHEMA,
        "paused": True,
        "tab_id": tab_id,
        "kind": kind,
        "signals": signals,
        "origin": origin,
        "updated_at_ms": int(time.time() * 1000),
    }
    _write_auth_state(recording, payload)
    return {
        "ok": True,
        "type": "COPILOT_AUTH_PAUSED_ACK",
        "paused": True,
        "tab_id": tab_id,
        "kind": kind,
    }


def _handle_copilot_auth_resumed(
    msg: Mapping[str, Any], recording: Path
) -> dict[str, Any]:
    extra = set(msg.keys()) - {"type", "tab_id", "kind", "schema", "request_id"}
    if extra:
        raise ValueError(f"unexpected fields in COPILOT_AUTH_RESUMED: {sorted(extra)}")
    if msg.get("schema") not in (None, COPILOT_STATE_SCHEMA):
        raise ValueError(f"schema must be {COPILOT_STATE_SCHEMA}")
    _extract_request_id(msg)
    tab_id = _validate_tab_id(msg.get("tab_id"))
    kind = msg.get("kind")
    if kind not in _VALID_AUTH_KINDS:
        raise ValueError("kind must be one of mfa, signin, sso")
    payload = {
        "schema": COPILOT_STATE_SCHEMA,
        "paused": False,
        "tab_id": tab_id,
        "kind": kind,
        "updated_at_ms": int(time.time() * 1000),
    }
    _write_auth_state(recording, payload)
    return {
        "ok": True,
        "type": "COPILOT_AUTH_RESUMED_ACK",
        "paused": False,
        "resumed": True,
        "tab_id": tab_id,
        "kind": kind,
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
    paused_tabs = _load_paused_tabs(recording)
    # Reload any existing lines so reconnects append.
    existing = recording / "extension_evidence.jsonl"
    if existing.is_file():
        from openadapt_flow.extension_evidence import load_extension_evidence

        for line in load_extension_evidence(recording):
            buf.append(line)

    while True:
        try:
            msg = read_message()
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            write_message({"ok": False, "error": "invalid_native_message"})
            return 2
        if msg is None:
            break
        if not isinstance(msg, Mapping):
            write_message({"ok": False, "error": "native message must be an object"})
            continue

        try:
            req_id = _extract_request_id(msg)
        except ValueError:
            write_message({"ok": False, "error": "invalid_request_id"})
            continue

        msg_type = msg.get("type")
        if msg_type == "COPILOT_GET_STATE":
            try:
                write_message(
                    _with_request_id(_handle_copilot_get_state(msg, bridge), req_id)
                )
            except (ValidationError, ValueError, OSError):
                write_message(
                    _with_request_id(
                        {"ok": False, "error": "copilot_message_rejected"}, req_id
                    )
                )
            continue

        if msg_type == "COPILOT_SUBMIT_DECISION":
            try:
                write_message(
                    _with_request_id(
                        _handle_copilot_submit_decision(msg, bridge), req_id
                    )
                )
            except (ValidationError, ValueError, OSError):
                write_message(
                    _with_request_id(
                        {"ok": False, "error": "copilot_message_rejected"}, req_id
                    )
                )
            continue

        if msg_type == "COPILOT_AUTH_PAUSED":
            try:
                reply = _handle_copilot_auth_paused(msg, recording)
                paused_tabs.add(int(reply["tab_id"]))
                write_message(_with_request_id(reply, req_id))
            except (ValidationError, ValueError, OSError):
                write_message(
                    _with_request_id(
                        {"ok": False, "error": "copilot_auth_pause_rejected"}, req_id
                    )
                )
            continue

        if msg_type == "COPILOT_AUTH_RESUMED":
            try:
                reply = _handle_copilot_auth_resumed(msg, recording)
                paused_tabs.discard(int(reply["tab_id"]))
                write_message(_with_request_id(reply, req_id))
            except (ValidationError, ValueError, OSError):
                write_message(
                    _with_request_id(
                        {"ok": False, "error": "copilot_auth_resume_rejected"}, req_id
                    )
                )
            continue

        if msg_type == "EXTENSION_EVIDENCE":
            try:
                evidence = validate_native_message(msg)
            except ValueError as exc:
                write_message(
                    _with_request_id({"ok": False, "error": str(exc)}, req_id)
                )
                continue
            ev_tab_id = _evidence_tab_id(evidence)
            if ev_tab_id is not None and ev_tab_id in paused_tabs:
                write_message(
                    _with_request_id(
                        {
                            "ok": True,
                            "paused": True,
                            "dropped": True,
                            "count": buf.count,
                        },
                        req_id,
                    )
                )
                continue
            buf.append(evidence)
            buf.write(recording)
            write_message(_with_request_id({"ok": True, "count": buf.count}, req_id))
            continue

        write_message(
            _with_request_id(
                {
                    "ok": False,
                    "error": (
                        "native message type must be EXTENSION_EVIDENCE, "
                        "COPILOT_GET_STATE, COPILOT_SUBMIT_DECISION, "
                        "COPILOT_AUTH_PAUSED, or COPILOT_AUTH_RESUMED"
                    ),
                },
                req_id,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
