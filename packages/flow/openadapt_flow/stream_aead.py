"""Tink's AES-256-GCM-HKDF streaming AEAD wire format, 1 MiB segments.

This is the payload format of a version 3 sealed set
(:mod:`openadapt_flow.sealed_set`). It follows Tink's ``AES256_GCM_HKDF_1MB``
key template byte for byte (https://developers.google.com/tink/streaming-aead/
aes_gcm_hkdf_streaming), built from the AES-GCM and HKDF primitives of the
``cryptography`` package; ``tests/test_stream_aead.py`` checks it against Tink
itself when Tink is installed. Nothing here is a new construction:

* header: one length byte (40), a 32-byte random salt, a 7-byte random nonce
  prefix;
* the segment key is HKDF-SHA256(ikm=key, salt=salt, info=associated data),
  32 bytes, so the associated data (the file's path in its set) is bound
  through the key;
* segment ``i`` is AES-256-GCM under the nonce ``prefix || i (4 bytes, big
  endian) || last-segment flag (1 byte)``, with no further associated data;
* ciphertext segments are 1 MiB, the first shortened by the header; the last
  segment is flagged, so a truncated stream fails to open.

It works on whole byte strings: the files Flow encrypts are read and written
in memory.
"""

from __future__ import annotations

import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_SIZE: Final[int] = 32
SEGMENT_SIZE: Final[int] = 1024 * 1024
_SALT: Final[int] = KEY_SIZE
_PREFIX: Final[int] = 7
_TAG: Final[int] = 16
HEADER_SIZE: Final[int] = 1 + _SALT + _PREFIX


class StreamError(ValueError):
    """A stream is malformed, truncated, tampered with, or under another key."""


def _segment_key(key: bytes, salt: bytes, aad: bytes) -> AESGCM:
    if len(key) != KEY_SIZE:
        raise StreamError("a streaming key must be 32 bytes")
    derived = HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=salt, info=aad)
    return AESGCM(derived.derive(key))


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    return prefix + index.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    salt = os.urandom(_SALT)
    prefix = os.urandom(_PREFIX)
    aead = _segment_key(key, salt, aad)
    out = [bytes([HEADER_SIZE]), salt, prefix]
    first = SEGMENT_SIZE - HEADER_SIZE - _TAG
    rest = SEGMENT_SIZE - _TAG
    pieces = [plaintext[:first]]
    offset = first
    while offset < len(plaintext):
        pieces.append(plaintext[offset : offset + rest])
        offset += rest
    for index, piece in enumerate(pieces):
        last = index == len(pieces) - 1
        out.append(aead.encrypt(_nonce(prefix, index, last), piece, None))
    return b"".join(out)


def decrypt(key: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    if len(ciphertext) < HEADER_SIZE + _TAG or ciphertext[0] != HEADER_SIZE:
        raise StreamError("not an AES-256-GCM-HKDF stream")
    salt = ciphertext[1 : 1 + _SALT]
    prefix = ciphertext[1 + _SALT : HEADER_SIZE]
    aead = _segment_key(key, salt, aad)
    body = ciphertext[HEADER_SIZE:]
    sizes = [SEGMENT_SIZE - HEADER_SIZE]
    total = sizes[0]
    while total < len(body):
        sizes.append(SEGMENT_SIZE)
        total += SEGMENT_SIZE
    out = []
    offset = 0
    for index, size in enumerate(sizes):
        segment = body[offset : offset + size]
        offset += size
        last = offset >= len(body)
        if len(segment) < _TAG:
            raise StreamError("the stream is truncated")
        try:
            out.append(aead.decrypt(_nonce(prefix, index, last), segment, None))
        except InvalidTag as exc:
            raise StreamError(
                "the stream does not authenticate (wrong key, tampered or truncated)"
            ) from exc
        if last:
            break
    return b"".join(out)


__all__ = [
    "HEADER_SIZE",
    "KEY_SIZE",
    "SEGMENT_SIZE",
    "StreamError",
    "decrypt",
    "encrypt",
]
