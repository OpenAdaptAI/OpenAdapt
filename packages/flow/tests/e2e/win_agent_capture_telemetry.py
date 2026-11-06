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
import stat
import sys
import tempfile
import threading
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable

_SCHEMA = "openadapt.test-win-agent-capture-telemetry/v1"
_RING_LIMIT = 32
_MAX_BODY_BYTES = 1_048_576
_MAX_SIDECAR_BYTES = 65_536
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RASTER_CACHE_IMAGE_LIMIT = 2
_MAX_RASTER_PIXELS = 8_294_400
_MAX_RASTER_IMAGE_BYTES = 24_883_200
_MAX_RASTER_CACHE_BYTES = 49_766_400
_MAX_RASTER_DIMENSION = 16_384


def _unavailable_pixel_diff(
    code: str,
    expected_dimensions: list[int] | None = None,
    current_dimensions: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "code": code,
        "expected_dimensions": expected_dimensions,
        "current_dimensions": current_dimensions,
        "changed_pixel_count": None,
        "changed_bbox": None,
    }


def _valid_rgb_raster(raster: Any) -> bool:
    if (
        not isinstance(raster, tuple)
        or len(raster) != 3
        or not isinstance(raster[0], bytes)
        or type(raster[1]) is not int
        or type(raster[2]) is not int
    ):
        return False
    rgb, width, height = raster
    pixel_count = width * height
    return (
        width > 0
        and height > 0
        and width <= _MAX_RASTER_DIMENSION
        and height <= _MAX_RASTER_DIMENSION
        and pixel_count <= _MAX_RASTER_PIXELS
        and len(rgb) == pixel_count * 3
        and len(rgb) <= _MAX_RASTER_IMAGE_BYTES
    )


def compare_captured_rasters(
    expected: tuple[bytes, int, int] | None,
    current: tuple[bytes, int, int] | None,
) -> dict[str, Any]:
    """Compare two bounded RGB buffers using Pillow's C-backed operations."""

    expected_dimensions: list[int] | None = None
    current_dimensions: list[int] | None = None
    for raster, label in ((expected, "expected"), (current, "current")):
        if raster is None:
            return _unavailable_pixel_diff("raster_missing")
        if (
            not isinstance(raster, tuple)
            or len(raster) != 3
            or not isinstance(raster[0], bytes)
            or type(raster[1]) is not int
            or type(raster[2]) is not int
            or raster[1] <= 0
            or raster[2] <= 0
        ):
            return _unavailable_pixel_diff("raster_invalid")
        rgb, width, height = raster
        pixel_count = width * height
        if width > _MAX_RASTER_DIMENSION or height > _MAX_RASTER_DIMENSION:
            return _unavailable_pixel_diff("raster_size_limit")
        if pixel_count > _MAX_RASTER_PIXELS or len(rgb) > _MAX_RASTER_IMAGE_BYTES:
            return _unavailable_pixel_diff("raster_size_limit")
        if len(rgb) != pixel_count * 3:
            return _unavailable_pixel_diff("raster_invalid")
        if label == "expected":
            expected_dimensions = [width, height]
        else:
            current_dimensions = [width, height]
    assert expected is not None and current is not None
    assert expected_dimensions is not None and current_dimensions is not None
    if expected_dimensions != current_dimensions:
        return _unavailable_pixel_diff(
            "dimension_mismatch", expected_dimensions, current_dimensions
        )
    try:
        from PIL import Image, ImageChops  # noqa: PLC0415

        width, height = expected_dimensions
        expected_image = Image.frombytes("RGB", (width, height), expected[0])
        current_image = Image.frombytes("RGB", (width, height), current[0])
        difference = ImageChops.difference(expected_image, current_image)
        channels = difference.split()
        changed_mask = channels[0].point([0] + [255] * 255)
        for channel in channels[1:]:
            changed_mask = ImageChops.lighter(
                changed_mask, channel.point([0] + [255] * 255)
            )
        changed_pixel_count = changed_mask.histogram()[255]
        changed_bbox = changed_mask.getbbox()
        return {
            "status": "available",
            "code": "raster_compared",
            "expected_dimensions": expected_dimensions,
            "current_dimensions": current_dimensions,
            "changed_pixel_count": changed_pixel_count,
            "changed_bbox": list(changed_bbox) if changed_bbox else None,
        }
    except Exception:  # noqa: BLE001 - diagnostics never change refusal/admission
        return _unavailable_pixel_diff(
            "raster_invalid", expected_dimensions, current_dimensions
        )


class _RasterCache:
    """Small LRU holding only decoded RGB bytes for exact PNG hashes."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[bytes, int, int]] = OrderedDict()
        self.byte_count = 0

    def put(
        self,
        png_sha256: str,
        raster: tuple[bytes, int, int],
    ) -> bool:
        if not _valid_sha256(png_sha256):
            return False
        if not _valid_rgb_raster(raster):
            return False
        existing = self._items.get(png_sha256)
        if existing is not None:
            self._items.move_to_end(png_sha256)
            return existing == raster
        rgb, _, _ = raster
        if len(rgb) > _MAX_RASTER_CACHE_BYTES:
            return False
        while self._items and (
            len(self._items) >= _RASTER_CACHE_IMAGE_LIMIT
            or self.byte_count + len(rgb) > _MAX_RASTER_CACHE_BYTES
        ):
            _, (old_rgb, _, _) = self._items.popitem(last=False)
            self.byte_count -= len(old_rgb)
        if self.byte_count + len(rgb) > _MAX_RASTER_CACHE_BYTES:
            return False
        self._items[png_sha256] = raster
        self.byte_count += len(rgb)
        return True

    def get(self, png_sha256: str) -> tuple[bytes, int, int] | None:
        if not _valid_sha256(png_sha256):
            return None
        raster = self._items.get(png_sha256)
        if raster is not None:
            self._items.move_to_end(png_sha256)
        return raster


def _validated_pixel_diff(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "status",
        "code",
        "expected_dimensions",
        "current_dimensions",
        "changed_pixel_count",
        "changed_bbox",
    }:
        return None

    def dimensions(candidate: Any) -> list[int] | None:
        if candidate is None:
            return None
        if (
            not isinstance(candidate, list)
            or len(candidate) != 2
            or any(type(item) is not int for item in candidate)
            or not (1 <= candidate[0] <= _MAX_RASTER_DIMENSION)
            or not (1 <= candidate[1] <= _MAX_RASTER_DIMENSION)
            or candidate[0] * candidate[1] > _MAX_RASTER_PIXELS
        ):
            return None
        return candidate

    expected_dimensions = dimensions(value.get("expected_dimensions"))
    current_dimensions = dimensions(value.get("current_dimensions"))
    if value.get("expected_dimensions") is not None and expected_dimensions is None:
        return None
    if value.get("current_dimensions") is not None and current_dimensions is None:
        return None
    if value.get("status") == "unavailable":
        if (
            value.get("code")
            not in {
                "raster_missing",
                "raster_invalid",
                "dimension_mismatch",
                "raster_size_limit",
            }
            or value.get("changed_pixel_count") is not None
            or value.get("changed_bbox") is not None
        ):
            return None
    elif value.get("status") == "available":
        if (
            value.get("code") != "raster_compared"
            or expected_dimensions is None
            or current_dimensions is None
            or expected_dimensions != current_dimensions
            or type(value.get("changed_pixel_count")) is not int
            or not (
                0
                <= value["changed_pixel_count"]
                <= expected_dimensions[0] * expected_dimensions[1]
            )
        ):
            return None
        bbox = value.get("changed_bbox")
        count = value["changed_pixel_count"]
        if count == 0:
            if bbox is not None:
                return None
        elif (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(item) is not int for item in bbox)
            or bbox[0] < 0
            or bbox[1] < 0
            or bbox[2] > expected_dimensions[0]
            or bbox[3] > expected_dimensions[1]
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
            or (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < count
        ):
            return None
    else:
        return None
    return value


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
        self._raster_cache = _RasterCache()
        self._sequence = 0
        self._enabled = False
        self._owned_identity: tuple[int, int] | None = None
        self._owned_payload_sha256: str | None = None
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

    def _owned_sidecar_matches(self) -> bool:
        """Require the regular file and bytes to remain ours before update."""

        try:
            metadata = self.sidecar.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return False
            if (metadata.st_dev, metadata.st_ino) != self._owned_identity:
                return False
            if metadata.st_size > _MAX_SIDECAR_BYTES:
                return False
            content = self.sidecar.read_bytes()
            return (
                len(content) == metadata.st_size
                and self._owned_payload_sha256 is not None
                and hashlib.sha256(content).hexdigest() == self._owned_payload_sha256
            )
        except Exception:  # noqa: BLE001 - foreign sidecar state disables telemetry
            return False

    def _write_snapshot_locked(self) -> bool:
        is_initial = self._owned_identity is None
        if is_initial:
            if self.sidecar.exists() or self.sidecar.is_symlink():
                return False
        elif not self._owned_sidecar_matches():
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
            owned_fd: int | None = fd
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                stream = os.fdopen(fd, "wb")
                owned_fd = None
                with stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                if owned_fd is not None:
                    os.close(owned_fd)
                raise
            if is_initial:
                # Hard-link publication is create-only: never replace a file
                # that appeared after the initial absence check.
                os.link(temporary, self.sidecar)
                temporary.unlink()
                temporary = None
            else:
                if not self._owned_sidecar_matches():
                    return False
                os.replace(temporary, self.sidecar)
                temporary = None
            metadata = self.sidecar.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return False
            self._owned_identity = (metadata.st_dev, metadata.st_ino)
            self._owned_payload_sha256 = hashlib.sha256(payload).hexdigest()
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
            try:
                self._raster_cache.put(record["png_sha256"], (rgb, width, height))
            except Exception:  # noqa: BLE001 - preserve original capture metadata
                self._raster_cache = _RasterCache()
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
            current_record = current[0]
            expected_before = [
                row
                for row in self._captures
                if row["png_sha256"] == expected_sha256
                and row["sequence"] < current_sequence
            ]
            expected_raster_keys = {
                (row["rgb_sha256"], row["width"], row["height"])
                for row in expected_before
            }
            if not expected_before:
                pixel_diff = _unavailable_pixel_diff("raster_missing")
            elif len(expected_raster_keys) != 1:
                pixel_diff = _unavailable_pixel_diff("raster_invalid")
            else:
                try:
                    pixel_diff = compare_captured_rasters(
                        self._raster_cache.get(expected_sha256),
                        self._raster_cache.get(current_record["png_sha256"]),
                    )
                except Exception:  # noqa: BLE001 - preserve the original refusal event
                    pixel_diff = _unavailable_pixel_diff("raster_invalid")
            self._refusals.append(
                {
                    "phase": "input_guard_frame_compare",
                    "expected_png_sha256": expected_sha256,
                    "current_sequence": current_sequence,
                    "pixel_diff": pixel_diff,
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

    def make_handler_class(
        config: Any, grab_fn: Callable[..., bytes], *args: Any, **kwargs: Any
    ):
        def observed_grab() -> bytes:
            png = grab_fn()
            current = getattr(request_state, "request", None)
            if current is not None:
                # Defer hashing, decoding and file I/O until the unchanged
                # handler has completed its guard/input decision.
                if len(current["captures"]) < _RING_LIMIT:
                    current["captures"].append(png)
                else:
                    current["capture_overflow"] = True
            return png

        handler = original_factory(config, observed_grab, *args, **kwargs)

        class InstrumentedHandler(handler):
            @staticmethod
            def _finish_telemetry(state: dict[str, Any]) -> None:
                if state["telemetry_finalized"]:
                    return
                state["telemetry_finalized"] = True
                sequences: list[int] = []
                try:
                    for png in state["captures"]:
                        sequence = telemetry.capture(png)
                        if sequence is not None:
                            sequences.append(sequence)
                    if (
                        state["stale_frame"]
                        and not state["capture_overflow"]
                        and len(sequences) == 1
                    ):
                        telemetry.refused_guard(state["expected_sha256"], sequences[0])
                except Exception:  # noqa: BLE001 - telemetry never changes request outcome
                    pass

            def send_response(self, code: int, message: str | None = None) -> None:
                # The route has completed its decision (including guarded
                # input) before this hook. Flush metadata before the status
                # line/body become visible to the client.
                current = getattr(request_state, "request", None)
                if code >= 200 and current is not None:
                    self._finish_telemetry(current)
                super().send_response(code, message)

            def handle_one_request(self) -> None:  # noqa: N802 - stdlib handler contract
                state: dict[str, Any] = {
                    "captures": [],
                    "capture_overflow": False,
                    "telemetry_finalized": False,
                    "expected_sha256": None,
                    "stale_frame": False,
                }
                request_state.request = state
                try:
                    super().handle_one_request()
                finally:
                    # Only abnormal paths which never reached send_response
                    # use this bounded fallback; normal responses finalize
                    # before their first response bytes are written.
                    self._finish_telemetry(state)
                    if hasattr(request_state, "request"):
                        del request_state.request

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path != "/input/guarded":
                    super().do_POST()
                    return
                original_rfile = self.rfile
                original_send_json = self._send_json
                tap = _ReadTap(original_rfile)
                state = request_state.request
                try:
                    self.rfile = tap

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
                                state["stale_frame"] = True
                        except Exception:  # noqa: BLE001 - sidecar is diagnostic only
                            pass
                        original_send_json(status, payload)

                    self._send_json = send_json
                    super().do_POST()
                finally:
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
                or set(row)
                != {"sequence", "png_sha256", "rgb_sha256", "width", "height"}
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
                not in (
                    {"phase", "expected_png_sha256", "current_sequence"},
                    {
                        "phase",
                        "expected_png_sha256",
                        "current_sequence",
                        "pixel_diff",
                    },
                )
                or event.get("phase") != "input_guard_frame_compare"
                or not _valid_sha256(event.get("expected_png_sha256"))
                or type(event.get("current_sequence")) is not int
                or event["current_sequence"] <= 0
            ):
                return unavailable
            pixel_diff = (
                _validated_pixel_diff(event["pixel_diff"])
                if "pixel_diff" in event
                else _unavailable_pixel_diff("raster_missing")
            )
            if pixel_diff is None:
                return unavailable
            expected_matches = [
                row
                for row in captures
                if row["png_sha256"] == event["expected_png_sha256"]
            ]
            expected_before_current = [
                row
                for row in expected_matches
                if row["sequence"] < event["current_sequence"]
            ]
            expected_after_current = [
                row
                for row in expected_matches
                if row["sequence"] > event["current_sequence"]
            ]
            expected_at_current = [
                row
                for row in expected_matches
                if row["sequence"] == event["current_sequence"]
            ]
            current_matches = [
                row for row in captures if row["sequence"] == event["current_sequence"]
            ]
            current = current_matches[0] if len(current_matches) == 1 else None
            expected_rasters = {
                (row["rgb_sha256"], row["width"], row["height"])
                for row in expected_before_current
            }
            if not expected_before_current:
                expected_raster_consensus = "none"
            elif len(expected_before_current) == 1:
                expected_raster_consensus = "single"
            elif len(expected_rasters) == 1:
                expected_raster_consensus = "consistent"
            else:
                expected_raster_consensus = "inconsistent"
            if current is None:
                guard_capture_relation = "unavailable"
                current_guard_capture_is_latest = None
            else:
                current_guard_capture_is_latest = (
                    current["sequence"] == captures[-1]["sequence"]
                )
                if current["png_sha256"] == event["expected_png_sha256"]:
                    guard_capture_relation = "same_png"
                elif not expected_before_current:
                    guard_capture_relation = (
                        "future_expected_only"
                        if expected_after_current
                        else "no_prior_expected_capture"
                    )
                elif expected_raster_consensus == "inconsistent":
                    guard_capture_relation = "ambiguous_expected_raster"
                elif (
                    expected_before_current[0]["rgb_sha256"] == current["rgb_sha256"]
                    and expected_before_current[0]["width"] == current["width"]
                    and expected_before_current[0]["height"] == current["height"]
                ):
                    guard_capture_relation = "encoding_only"
                else:
                    guard_capture_relation = "raster_changed"
            diagnosis: dict[str, Any] = {
                "phase": event["phase"],
                "status": "unknown",
                "code": "ambiguous_or_missing_provenance",
                "provenance": {
                    "expected_match_count": len(expected_matches),
                    "expected_match_before_current_count": len(expected_before_current),
                    "expected_match_at_current_count": len(expected_at_current),
                    "expected_match_after_current_count": len(expected_after_current),
                    "current_sequence_match_count": len(current_matches),
                    "current_guard_capture_is_latest": current_guard_capture_is_latest,
                    "expected_raster_consensus": expected_raster_consensus,
                    "guard_capture_relation": guard_capture_relation,
                },
            }
            if "pixel_diff" in event:
                diagnosis["pixel_diff"] = pixel_diff
            if len(expected_matches) == 1 and len(current_matches) == 1:
                expected = expected_matches[0]
                assert current is not None
                if (
                    expected["sequence"] < current["sequence"]
                    and expected["png_sha256"] != current["png_sha256"]
                ):
                    diagnosis.update(
                        {
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
                            "expected_dimensions": [
                                expected["width"],
                                expected["height"],
                            ],
                            "current_dimensions": [
                                current["width"],
                                current["height"],
                            ],
                        }
                    )
            diagnoses.append(diagnosis)
        return {"status": "available", "diagnoses": diagnoses}
    except Exception:  # noqa: BLE001 - diagnostics never alter admission
        return unavailable


if __name__ == "__main__":  # pragma: no cover - exercised in subprocess test
    main()
