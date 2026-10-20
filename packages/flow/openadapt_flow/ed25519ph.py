"""Ed25519ph (RFC 8032, section 5.1, prehashed, empty context).

Sigstore Rekor checks an Ed25519 signature in a ``hashedrekord`` entry as
Ed25519ph over the artifact's SHA-512 digest. The ``cryptography`` library
offers pure Ed25519 only, so this module implements the prehashed variant
from the RFC's equations. It is written from the specification, not copied.

It signs only the occasional anchor digest on the operator's own machine and
is not constant time; Seals themselves are signed with ``cryptography`` or
on the customer's token. ``tests/test_seal_anchor.py`` checks it against the
RFC 8032 section 7.3 test vector and against ``cryptography`` for pure Ed25519.
"""

from __future__ import annotations

import hashlib
from typing import Optional

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_PH_DOM = b"SigEd25519 no Ed25519 collisions" + b"\x01" + b"\x00"

Point = tuple[int, int, int, int]  # extended coordinates X, Y, Z, T


def _add(a: Point, b: Point) -> Point:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    aa = (y1 - x1) * (y2 - x2) % _P
    bb = (y1 + x1) * (y2 + x2) % _P
    cc = 2 * t1 * t2 * _D % _P
    dd = 2 * z1 * z2 % _P
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: Point) -> Point:
    result: Point = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _recover_x(_BY, 0)
assert _BX is not None
_B: Point = (_BX, _BY, 1, _BX * _BY % _P)


def _encode(point: Point) -> bytes:
    x, y, z, _ = point
    zi = pow(z, _P - 2, _P)
    x, y = x * zi % _P, y * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode(data: bytes) -> Optional[Point]:
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    y, sign = value & ((1 << 255) - 1), value >> 255
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _h(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("an Ed25519 private key is 32 bytes")
    digest = hashlib.sha512(seed).digest()
    a = int.from_bytes(digest[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, digest[32:]


def _sign(seed: bytes, message: bytes, dom: bytes) -> bytes:
    a, prefix = _expand(seed)
    public = _encode(_mul(a, _B))
    r = _h(dom + prefix + message) % _L
    big_r = _encode(_mul(r, _B))
    k = _h(dom + big_r + public + message) % _L
    return big_r + int.to_bytes((r + k * a) % _L, 32, "little")


def _verify(public: bytes, message: bytes, signature: bytes, dom: bytes) -> bool:
    if len(signature) != 64:
        return False
    point_a, point_r = _decode(public), _decode(signature[:32])
    if point_a is None or point_r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = _h(dom + signature[:32] + public + message) % _L
    return _encode(_mul(s, _B)) == _encode(_add(point_r, _mul(k, point_a)))


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _encode(_mul(a, _B))


def sign_prehashed(seed: bytes, sha512_digest: bytes) -> bytes:
    """Ed25519ph signature over a 64-byte SHA-512 digest, empty context."""
    if len(sha512_digest) != 64:
        raise ValueError("Ed25519ph signs a 64-byte SHA-512 digest")
    return _sign(seed, sha512_digest, _PH_DOM)


def verify_prehashed(public: bytes, sha512_digest: bytes, signature: bytes) -> bool:
    """Check an Ed25519ph signature over a 64-byte SHA-512 digest."""
    if len(sha512_digest) != 64:
        return False
    return _verify(public, sha512_digest, signature, _PH_DOM)
