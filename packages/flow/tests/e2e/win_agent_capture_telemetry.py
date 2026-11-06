"""Test-owned capture telemetry wrapper for the hosted Windows fixture.

The wrapper delegates the agent CLI and server configuration unchanged. Its
only observation is a bounded ring of hashes and dimensions for PNGs already
captured by the normal ``grab_fn``; it never stores image bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

_SCHEMA = "openadapt.test-win-agent-capture-telemetry/v1"
_RING_LIMIT = 32
_MAX_BODY_BYTES = 1_048_576
_MAX_SIDECAR_BYTES = 65_536
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class CaptureTelemetry:
    """Record existing capture calls and guarded refusal correlations only."""

    def __init__(self, sidecar: Path) -> None:
        self.sidecar = sidecar
        self._lock = threading.Lock()
        self._captures: deque[dict[str, Any]] = deque(maxlen=_RING_LIMIT)
        self._refusals: deque[dict[str, Any]] = deque(maxlen=_RING_LIMIT)
        self._sequence = 0
        self._enabled = False
        try:
            if not sidecar.is_absolute() or sidecar.name in {"", ".", ".."}:
                return
            parent = sidecar.parent.resolve(strict=True)
            if sidecar.parent.resolve(strict=True) != parent:
                return
            if sidecar.exists() or sidecar.is_symlink():
                return
            self.sidecar = parent / sidecar.name
            self._enabled = self._write_snapshot_locked()
        except Exception:  # noqa: BLE001 - telemetry must never affect capture
            self._enabled = False

    def _snapshot(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "captures": list(self._captures),
            "guarded_refusals": list(self._refusals),
        }

    def _write_snapshot_locked(self) -> bool:
        if not self._enabled and self.sidecar.exists():
            return False
        temporary: Path | None = None
        try:
            payload = json.dumps(
                self._snapshot(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            fd, name = tempfile.mkstemp(
                prefix=f".{self.sidecar.name}.",
                suffix=".tmp",
                dir=self.sidecar.parent,
            )
            temporary = Path(name)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                os.close(fd)
                raise
            if self.sidecar.exists() or self.sidecar.is_symlink():
                return False
            os.replace(temporary, self.sidecar)
            temporary = None
            try:
                os.chmod(self.sidecar, 0o600)
            except OSError:
                # Windows ACLs are inherited from the fixture's private temp
                # directory; chmod is best effort on platforms without POSIX
                # mode bits.
                pass
            return True
        except Exception:  # noqa: BLE001 - telemetry must never affect capture
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def capture(self, png: bytes) -> int | None:
        """Record metadata and return its sequence without changing ``png``."""

        if not self._enabled:
            return None
        sequence: int | None = None
        record: dict[str, Any] = {
            "sequence": 0,
            "png_sha256": hashlib.sha256(png).hexdigest(),
            "rgb_sha256": None,
            "width": None,
            "height": None,
        }
        try:
            if not png.startswith(_PNG_SIGNATURE):
                return None
            from PIL import Image  # noqa: PLC0415

            with Image.open(io.BytesIO(png)) as image:
                width, height = image.size
                if (
                    not (1 <= width <= 16384 and 1 <= height <= 16384)
                    or width * height > 100_000_000
                ):
                    return None
                rgb = image.convert("RGB").tobytes()
            record.update(
                rgb_sha256=hashlib.sha256(rgb).hexdigest(),
                width=width,
                height=height,
            )
        except Exception:  # noqa: BLE001 - telemetry must never affect capture
            return None
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            record["sequence"] = sequence
            self._captures.append(record)
            if not self._write_snapshot_locked():
                self._enabled = False
                return None
        return sequence

    def refused_guard(self, expected_sha256: Any, current_sequence: Any) -> None:
        """Record the exact capture made by this stale guarded HTTP request."""

        if not self._enabled or not _valid_sha256(expected_sha256):
            return
        if type(current_sequence) is not int or current_sequence <= 0:
            return
        with self._lock:
            current = [
                row for row in self._captures if row["sequence"] == current_sequence
            ]
            if len(current) != 1:
                return
            self._refusals.append(
                {
                    "phase": "input_guard_frame_compare",
                    "expected_png_sha256": expected_sha256,
                    "current_sequence": current_sequence,
                }
            )
            if not self._write_snapshot_locked():
                self._enabled = False


def _extract_expected_hash(raw: bytes) -> str | None:
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - never alter request handling
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("expected_frame_sha256")
    return value if _valid_sha256(value) else None


class _ReadTap:
    """Pass reads through unchanged while retaining only the bounded request."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self.parts: list[bytes] = []
        self.length = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        if chunk and self.length + len(chunk) <= _MAX_BODY_BYTES:
            self.parts.append(chunk)
            self.length += len(chunk)
        return chunk

    def bytes_read(self) -> bytes:
        return b"".join(self.parts)


def instrument_make_handler_class(
    original_factory: Callable[..., type], telemetry: CaptureTelemetry
) -> Callable[..., type]:
    """Wrap the existing handler factory while preserving every route body."""

    request_state = threading.local()

    def make_handler_class(config: Any, grab_fn: Callable[..., bytes], *args: Any, **kwargs: Any):
        def observed_grab() -> bytes:
            png = grab_fn()
            try:
                sequence = telemetry.capture(png)
            except Exception:  # noqa: BLE001 - telemetry must never alter capture
                sequence = None
            current = getattr(request_state, "guard", None)
            if current is not None:
                current["current_sequence"] = sequence
            return png

        handler = original_factory(config, observed_grab, *args, **kwargs)

        class InstrumentedHandler(handler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path != "/input/guarded":
                    super().do_POST()
                    return
                original_rfile = self.rfile
                original_send_json = self._send_json
                tap = _ReadTap(original_rfile)
                state: dict[str, Any] = {
                    "expected_sha256": None,
                    "current_sequence": None,
                }
                try:
                    self.rfile = tap
                    request_state.guard = state

                    def send_json(status: int, payload: dict[str, Any]) -> None:
                        try:
                            if (
                                status == 409
                                and isinstance(payload, dict)
                                and payload.get("code") == "stale_frame"
                            ):
                                state["expected_sha256"] = _extract_expected_hash(
                                    tap.bytes_read()
                                )
                                telemetry.refused_guard(
                                    state["expected_sha256"],
                                    state["current_sequence"],
                                )
                        except Exception:  # noqa: BLE001 - sidecar is diagnostic only
                            pass
                        original_send_json(status, payload)

                    self._send_json = send_json
                    super().do_POST()
                finally:
                    if hasattr(request_state, "guard"):
                        del request_state.guard
                    self.rfile = original_rfile
                    self._send_json = original_send_json

        return InstrumentedHandler

    return make_handler_class


def install(server_module: Any, sidecar: Path) -> CaptureTelemetry:
    telemetry = CaptureTelemetry(sidecar)
    original_factory = server_module.make_handler_class
    server_module.make_handler_class = instrument_make_handler_class(
        original_factory, telemetry
    )
    return telemetry


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--capture-telemetry-sidecar", required=True)
    wrapper, server_args = parser.parse_known_args(args)
    server_module = importlib.import_module("openadapt_flow.backends.win_agent.server")
    original_factory = server_module.make_handler_class
    install(server_module, Path(wrapper.capture_telemetry_sidecar))
    try:
        server_module.main(server_args)
    finally:
        server_module.make_handler_class = original_factory


def safe_projection(path: Path) -> dict[str, Any]:
    """Project one sidecar to a fixed diagnosis; malformed input is unavailable."""

    unavailable = {"status": "unavailable", "code": "invalid_capture_telemetry"}
    try:
        if path.is_symlink() or not path.is_file():
            return unavailable
        if path.stat().st_size > _MAX_SIDECAR_BYTES:
            return unavailable
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "captures", "guarded_refusals"}
            or payload.get("schema") != _SCHEMA
            or not isinstance(payload["captures"], list)
            or not isinstance(payload["guarded_refusals"], list)
            or len(payload["captures"]) > _RING_LIMIT
            or len(payload["guarded_refusals"]) > _RING_LIMIT
        ):
            return unavailable
        captures: list[dict[str, Any]] = []
        seen_sequences: set[int] = set()
        previous_sequence = 0
        for row in payload["captures"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"sequence", "png_sha256", "rgb_sha256", "width", "height"}
                or type(row.get("sequence")) is not int
                or row["sequence"] <= 0
                or row["sequence"] in seen_sequences
                or row["sequence"] <= previous_sequence
                or not _valid_sha256(row.get("png_sha256"))
                or not _valid_sha256(row.get("rgb_sha256"))
                or type(row.get("width")) is not int
                or type(row.get("height")) is not int
                or not (1 <= row["width"] <= 16384)
                or not (1 <= row["height"] <= 16384)
                or row["width"] * row["height"] > 100_000_000
            ):
                return unavailable
            seen_sequences.add(row["sequence"])
            previous_sequence = row["sequence"]
            captures.append(row)
        diagnoses = []
        for event in payload["guarded_refusals"]:
            if (
                not isinstance(event, dict)
                or set(event)
                != {"phase", "expected_png_sha256", "current_sequence"}
                or event.get("phase") != "input_guard_frame_compare"
                or not _valid_sha256(event.get("expected_png_sha256"))
                or type(event.get("current_sequence")) is not int
                or event["current_sequence"] <= 0
            ):
                return unavailable
            expected_matches = [
                row
                for row in captures
                if row["png_sha256"] == event["expected_png_sha256"]
            ]
            current_matches = [
                row
                for row in captures
                if row["sequence"] == event["current_sequence"]
            ]
            diagnosis: dict[str, Any] = {
                "phase": event["phase"],
                "status": "unknown",
                "code": "ambiguous_or_missing_provenance",
            }
            if len(expected_matches) == 1 and len(current_matches) == 1:
                expected = expected_matches[0]
                current = current_matches[0]
                if (
                    expected["sequence"] < current["sequence"]
                    and expected["png_sha256"] != current["png_sha256"]
                ):
                    diagnosis = {
                        "phase": event["phase"],
                        "status": "classified",
                        "code": (
                            "encoding_only_change"
                            if expected["rgb_sha256"] == current["rgb_sha256"]
                            and expected["width"] == current["width"]
                            and expected["height"] == current["height"]
                            else "decoded_raster_changed"
                        ),
                        "expected_png_sha256": expected["png_sha256"],
                        "current_png_sha256": current["png_sha256"],
                        "expected_rgb_sha256": expected["rgb_sha256"],
                        "current_rgb_sha256": current["rgb_sha256"],
                        "expected_dimensions": [expected["width"], expected["height"]],
                        "current_dimensions": [current["width"], current["height"]],
                    }
            diagnoses.append(diagnosis)
        return {"status": "available", "diagnoses": diagnoses}
    except Exception:  # noqa: BLE001 - diagnostics never alter admission
        return unavailable
