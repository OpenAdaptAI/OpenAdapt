"""Assertions for synthetic plaintext markers in encrypted JSON envelopes."""

from __future__ import annotations

import base64
import json


def assert_sealed_marker_absent(raw: bytes, marker: bytes) -> None:
    assert len(marker) >= 16, "plaintext marker must have at least 16 bytes"
    assert marker not in raw, "plaintext marker is visible in sealed bytes"
    ciphertext = base64.b64decode(json.loads(raw)["ciphertext"], validate=True)
    assert marker not in ciphertext, "plaintext marker is visible in decoded ciphertext"
