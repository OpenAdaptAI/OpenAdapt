"""Public synthetic task-card protocol for the headed visual benchmark."""

from __future__ import annotations

import hashlib
import json

MAGIC = b"OA1"
CARD_COLUMNS = 64


def canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_task_card(task: dict) -> list[int]:
    """Encode a task as a self-authenticating visible binary card."""
    payload = canonical_json(task)
    if len(payload) > 65535:
        raise ValueError("task-card payload is too large")
    encoded = MAGIC + len(payload).to_bytes(2, "big") + payload + hashlib.sha256(payload).digest()
    return [int(bit) for byte in encoded for bit in f"{byte:08b}"]


def decode_task_card(bits: list[int]) -> dict:
    """Decode and hash-check bits read from a captured screen frame."""
    header_size = (len(MAGIC) + 2) * 8
    if len(bits) < header_size:
        raise ValueError("task card is shorter than its header")
    header = _bits_to_bytes(bits[:header_size])
    if header[: len(MAGIC)] != MAGIC:
        raise ValueError("task-card magic is invalid")
    payload_size = int.from_bytes(header[len(MAGIC) :], "big")
    total_size = len(MAGIC) + 2 + payload_size + hashlib.sha256().digest_size
    required_bits = total_size * 8
    if len(bits) < required_bits:
        raise ValueError("task card is truncated")
    encoded = _bits_to_bytes(bits[:required_bits])
    payload_start = len(MAGIC) + 2
    payload = encoded[payload_start : payload_start + payload_size]
    digest = encoded[payload_start + payload_size : total_size]
    if hashlib.sha256(payload).digest() != digest:
        raise ValueError("task-card payload hash is invalid")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("task-card payload must be an object")
    return value


def _bits_to_bytes(bits: list[int]) -> bytes:
    if len(bits) % 8:
        raise ValueError("bit count must be divisible by eight")
    return bytes(
        int("".join(str(bit) for bit in bits[index : index + 8]), 2)
        for index in range(0, len(bits), 8)
    )
