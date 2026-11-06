"""Exact-frame pairing of an owned RDP connection and a guest witness.

The guest renderer independently obtains its logon digest before rendering.
This module only verifies the witness; constructing an expectation does not
establish pairing or advertise a qualification capability.
"""

from __future__ import annotations

import hashlib
import io
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_SCHEMA = "openadapt.rdp-pairing/v1"
_GRID = 16
_CELL = 3
_BORDER = 2
WITNESS_SIZE = _GRID * _CELL + 2 * _BORDER
Rect = tuple[int, int, int, int]


def _rect(value: Rect, viewport: tuple[int, int]) -> Rect:
    if (
        not isinstance(value, tuple)
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise ValueError("pairing region must be an integer rectangle")
    x, y, width, height = value
    if (
        min(x, y) < 0
        or min(width, height) <= 0
        or x + width > viewport[0]
        or y + height > viewport[1]
    ):
        raise ValueError("pairing region is outside the observed viewport")
    return value


def _overlaps(left: Rect, right: Rect) -> bool:
    x, y, w, h = left
    a, b, c, d = right
    return x < a + c and a < x + w and y < b + d and b < y + h


def witness_digest(nonce: str, connection_digest: str, guest_digest: str) -> bytes:
    """The guest computes this using its own observed guest digest.

    Render bits MSB-first, row-major, in a 16x16 grid of 3px cells with a
    2px black border. A set bit is RGB white; an unset bit is RGB black.
    The fixed 52x52 witness contains no application text or raw identifiers.
    """
    if not _NONCE.fullmatch(nonce) or not all(
        _HEX64.fullmatch(value) for value in (connection_digest, guest_digest)
    ):
        raise ValueError("pairing nonce or identity digest is invalid")
    return hashlib.sha256(
        (
            _SCHEMA + "\0" + nonce + "\0" + connection_digest + "\0" + guest_digest
        ).encode("ascii")
    ).digest()


def pairing_request(
    *,
    nonce: str,
    connection_digest: str,
    viewport: tuple[int, int],
    region: Rect,
    excluded_regions: tuple[Rect, ...],
    lifetime_seconds: int = 60,
) -> dict[str, Any]:
    """Prepare a bounded guest request; readiness never establishes pairing."""
    witness_digest(nonce, connection_digest, "0" * 64)
    if (
        not isinstance(viewport, tuple)
        or len(viewport) != 2
        or any(type(item) is not int or item <= 0 for item in viewport)
    ):
        raise ValueError("pairing needs observed viewport dimensions")
    region = _rect(region, viewport)
    if region[2:] != (WITNESS_SIZE, WITNESS_SIZE):
        raise ValueError("pairing witness has fixed dimensions")
    if not isinstance(excluded_regions, tuple) or not excluded_regions:
        raise ValueError("pairing requires recorded protected regions")
    for item in excluded_regions:
        if _overlaps(region, _rect(item, viewport)):
            raise ValueError("pairing witness overlaps a protected region")
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 300:
        raise ValueError("pairing witness lifetime must be between 1 and 300 seconds")
    return {
        "schema": _SCHEMA,
        "operation": "pairing_witness",
        "nonce": nonce,
        "connection_digest": connection_digest,
        "viewport": list(viewport),
        "region": list(region),
        "lifetime_seconds": lifetime_seconds,
    }


class RdpPairing:
    """Compose a live witness with an existing exact-frame environment probe.

    The session owner supplies every recorded protected/action/identity region
    as ``excluded_regions``. The guest witness remains visible for the entire
    qualification. The caller must retain the existing application/version
    probes and the independent transport session observer.
    """

    def __init__(
        self,
        transport: Any,
        *,
        viewport: tuple[int, int],
        region: Rect,
        excluded_regions: tuple[Rect, ...],
        environment_marker: str,
        environment_probe: Callable[[bytes], bool],
        lifetime_seconds: int = 60,
    ) -> None:
        # Freeze the local deadline before the owner can obtain a launch request.
        started = time.monotonic()
        nonce = secrets.token_hex(16)
        connection_digest = transport.session_identity()
        self._request = pairing_request(
            nonce=nonce,
            connection_digest=connection_digest,
            viewport=viewport,
            region=region,
            excluded_regions=excluded_regions,
            lifetime_seconds=lifetime_seconds,
        )
        if not environment_marker or not callable(environment_probe):
            raise ValueError("pairing must retain the existing environment probe")
        self._transport = transport
        self._connection_digest = connection_digest
        self._nonce = nonce
        self._viewport = viewport
        self._region = region
        self._environment_probe = environment_probe
        self._digest: bytes | None = None
        self._deadline = started + lifetime_seconds
        # Environment identity must remain stable across fresh case/session
        # witnesses. The probe binds freshness without changing this identity.
        self.environment_marker = environment_marker
        if not self.matches_transport(transport):
            raise ValueError("pairing connection is unavailable or changed")

    def matches_transport(self, transport: Any) -> bool:
        """Check the same live connection within the original local deadline."""
        if transport is not self._transport or time.monotonic() >= self._deadline:
            return False
        try:
            return (
                transport.session_identity() == self._connection_digest
                and time.monotonic() < self._deadline
            )
        except Exception:
            return False

    def guest_request(self) -> dict[str, Any]:
        """Get a request before launch; this never extends the local deadline."""
        if not self.matches_transport(self._transport):
            raise ValueError("pairing connection is unavailable or expired")
        return {
            **self._request,
            "viewport": list(self._viewport),
            "region": list(self._region),
        }

    def accept_guest_ready(self, status: str) -> bool:
        """Bind one guest readiness digest; only the PNG probe proves visibility."""
        if self._digest is not None or not self.matches_transport(self._transport):
            return False
        fields = status.split(":")
        if (
            len(fields) != 3
            or fields[0] != "ready"
            or fields[1] != self._nonce
            or not _HEX64.fullmatch(fields[2])
        ):
            return False
        self._digest = witness_digest(self._nonce, self._connection_digest, fields[2])
        return True

    def environment_probe(self, png: bytes) -> bool:
        """Verify both original environment and witness on the supplied PNG."""
        if self._digest is None or not self.matches_transport(self._transport):
            return False
        try:
            if self._environment_probe(png) is not True:
                return False
            from PIL import Image

            with Image.open(io.BytesIO(png)) as frame:
                if frame.size != self._viewport:
                    return False
                rgb = frame.convert("RGB")
                x, y, width, height = self._region
                pixels = rgb.crop((x, y, x + width, y + height))
                for row in range(height):
                    for col in range(width):
                        value = 0
                        if (
                            _BORDER <= row < height - _BORDER
                            and _BORDER <= col < width - _BORDER
                        ):
                            bit = ((row - _BORDER) // _CELL) * _GRID + (
                                col - _BORDER
                            ) // _CELL
                            value = (
                                255
                                if self._digest[bit // 8] & (128 >> (bit % 8))
                                else 0
                            )
                        if pixels.getpixel((col, row)) != (value, value, value):
                            return False
            return self.matches_transport(self._transport)
        except Exception:
            return False
