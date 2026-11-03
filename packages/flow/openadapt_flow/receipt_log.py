"""The local transparency log for write receipts. See ``docs/WRITE_RECEIPTS.md``.

An append-only file of RFC 9162 leaf hashes, one per line, under a file
lock. Appending a signed statement returns a COSE Receipt
(draft-ietf-cose-merkle-tree-proofs): a COSE_Sign1 by the log key over the
tree root (detached), with ``vds`` = RFC9162_SHA256 and the inclusion proof
in ``vdp``. The log key is a separate Ed25519 key, generated here on first
use, mode 0600, and never printed.

On its own the log is the operator's: it shows order and that nothing was
edited after logging, not that the operator couldn't rebuild it. Rekor
anchoring (``write_receipt.anchor``) adds a party the operator doesn't
control.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.seal_anchor import _node, leaf_hash, root_from_inclusion

#: COSE header labels (IANA COSE Header Parameters; SCITT and COSE receipts).
ALG, KID, CTY, CWT_CLAIMS = 1, 4, 3, 15
RECEIPTS, VDS, VDP = 394, 395, 396
#: COSE algorithm EdDSA, and the RFC9162_SHA256 verifiable data structure.
EDDSA, RFC9162_SHA256 = -8, 1
#: ``vdp`` map key for inclusion proofs.
INCLUSION = -1
ENV_HOME = "OPENADAPT_RECEIPT_HOME"
_KEY = "log-ed25519.pem"
_LEAVES = "leaves"


class LogError(RuntimeError):
    """The log can't be read, written, or its key opened."""


def receipt_home(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    value = env.get(ENV_HOME)
    return (
        Path(value).expanduser() if value else Path.home() / ".openadapt" / "receipts"
    )


def key_id(public_raw: bytes) -> bytes:
    """A COSE ``kid``: the first 16 bytes of SHA-256 of the raw public key."""
    return hashlib.sha256(public_raw).digest()[:16]


def sig_structure(protected: bytes, payload: bytes) -> bytes:
    """RFC 9052 section 4.4 ``Sig_structure`` for COSE_Sign1, empty external AAD."""
    import cbor2

    return cbor2.dumps(["Signature1", protected, b"", payload])


def encode_sign1(
    protected: Mapping[int, Any],
    unprotected: Mapping[int, Any],
    payload: Optional[bytes],
    sign: Any,
    *,
    signed_payload: Optional[bytes] = None,
) -> bytes:
    """A tagged COSE_Sign1. ``payload`` None is detached; then ``signed_payload``."""
    import cbor2

    prot = cbor2.dumps(dict(protected), canonical=True)
    body = payload if payload is not None else signed_payload
    if body is None:
        raise ValueError("a detached payload still needs the bytes it signs")
    signature = sign(sig_structure(prot, body))
    return cbor2.dumps(
        cbor2.CBORTag(18, [prot, dict(unprotected), payload, signature]), canonical=True
    )


def decode_sign1(
    data: bytes,
) -> tuple[bytes, dict[int, Any], dict[Any, Any], Any, bytes]:
    """Split a tagged COSE_Sign1 strictly: raw protected bytes, both headers,
    payload, signature. Raises ValueError on any other shape."""
    import cbor2

    msg = cbor2.loads(data)
    # One byte string per receipt: a re-encoding (longer lengths, a
    # self-describe tag, trailing bytes) would verify under another digest
    # and slip past the receipt that supersedes it.
    if cbor2.dumps(msg, canonical=True) != data:
        raise ValueError("not canonical CBOR (or bytes after the message)")
    if not (isinstance(msg, cbor2.CBORTag) and msg.tag == 18):
        raise ValueError("not a tagged COSE_Sign1")
    if not (isinstance(msg.value, list) and len(msg.value) == 4):
        raise ValueError("COSE_Sign1 is not a four-element array")
    prot, unprot, payload, signature = msg.value
    if not isinstance(prot, bytes) or not isinstance(unprot, dict):
        raise ValueError("bad COSE headers")
    if payload is not None and not isinstance(payload, bytes):
        raise ValueError("bad COSE payload")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("bad COSE signature")
    headers = cbor2.loads(prot)
    # One encoding only: a repeated or reordered label would re-encode
    # differently, and two parsers could read two different headers.
    if not isinstance(headers, dict) or cbor2.dumps(headers, canonical=True) != prot:
        raise ValueError("protected header is not canonical CBOR")
    return prot, headers, unprot, payload, signature


def receipt_id(statement: bytes) -> str:
    """A receipt's identity: its leaf hash, hex. It covers only the signed
    bytes (protected header, payload, signature), so nothing in the
    unsigned header, such as the log's proof, can give a receipt another
    name. ``supersedes`` and Rekor anchors use it."""
    return leaf_of(statement).hex()


def leaf_of(statement: bytes) -> bytes:
    """The leaf a signed statement is logged under: its bytes with an empty
    unprotected header, RFC 9162 leaf hashing."""
    import cbor2

    prot, _, _, payload, signature = decode_sign1(statement)
    bare = cbor2.dumps(
        cbor2.CBORTag(18, [prot, {}, payload, signature]), canonical=True
    )
    return leaf_hash(bare)


def _largest_pow2_below(n: int) -> int:
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def tree_root(leaves: Sequence[bytes]) -> bytes:
    """RFC 9162 MTH over leaf hashes (already hashed)."""
    if not leaves:
        return hashlib.sha256(b"").digest()
    if len(leaves) == 1:
        return leaves[0]
    k = _largest_pow2_below(len(leaves))
    return _node(tree_root(leaves[:k]), tree_root(leaves[k:]))


def inclusion_path(index: int, leaves: Sequence[bytes]) -> list[bytes]:
    """RFC 9162 section 2.1.3.1 PATH(m, D[n])."""
    if len(leaves) <= 1:
        return []
    k = _largest_pow2_below(len(leaves))
    if index < k:
        return inclusion_path(index, leaves[:k]) + [tree_root(leaves[k:])]
    return inclusion_path(index - k, leaves[k:]) + [tree_root(leaves[:k])]


class LocalLog:
    """The append-only leaf file and the log key under ``home/log``."""

    def __init__(self, home: Optional[Path] = None) -> None:
        self.dir = (home or receipt_home()) / "log"

    def _private_key(self) -> Any:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        path = self.dir / _KEY
        if path.is_file():
            if sys.platform != "win32" and path.stat().st_mode & 0o077:
                raise LogError(f"{path} is open to other users; chmod 600 it")
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise LogError(f"{path} is not an Ed25519 key")
            return key
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        # Write in full under a private name, then link: the key appears
        # whole or not at all, and two first appends can't each make one.
        tmp = self.dir / f".{_KEY}.{secrets.token_hex(8)}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(pem)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                return self._private_key()
        finally:
            tmp.unlink(missing_ok=True)
        return key

    def public_raw(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return (
            self._private_key()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )

    def append(self, statement: bytes) -> bytes:
        """Log ``statement`` and return its COSE Receipt."""
        import fcntl

        import cbor2

        key = self._private_key()
        leaf = leaf_of(statement)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.dir / _LEAVES, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            leaves = []
            for line in handle.read().splitlines():
                try:
                    leaves.append(bytes.fromhex(line.decode("ascii")))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise LogError("the leaf file is corrupt") from exc
                if len(leaves[-1]) != 32:
                    raise LogError("the leaf file is corrupt")
            leaves.append(leaf)
            handle.write(leaf.hex().encode("ascii") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        index, size = len(leaves) - 1, len(leaves)
        proof = cbor2.dumps([size, index, inclusion_path(index, leaves)])
        public_raw = self.public_raw()
        return encode_sign1(
            {ALG: EDDSA, KID: key_id(public_raw), VDS: RFC9162_SHA256},
            {VDP: {INCLUSION: [proof]}},
            None,
            key.sign,
            signed_payload=tree_root(leaves),
        )


def verify_inclusion(receipt: bytes, statement: bytes, log_public_raw: bytes) -> int:
    """Check one COSE Receipt for ``statement`` against the pinned log key.
    Returns the leaf index. Raises ValueError when anything doesn't hold."""
    import cbor2
    from cwt import COSE, COSEKey

    _, headers, unprot, payload, _ = decode_sign1(receipt)
    if headers != {ALG: EDDSA, KID: key_id(log_public_raw), VDS: RFC9162_SHA256}:
        raise ValueError("the log receipt's protected header is not the pinned log's")
    if payload is not None:
        raise ValueError("a log receipt's payload must be detached")
    vdp = unprot.get(VDP)
    if set(unprot) != {VDP} or not isinstance(vdp, dict) or set(vdp) != {INCLUSION}:
        raise ValueError("the log receipt carries no single inclusion proof")
    proofs = vdp[INCLUSION]
    if not (
        isinstance(proofs, list) and len(proofs) == 1 and isinstance(proofs[0], bytes)
    ):
        raise ValueError("the log receipt carries no single inclusion proof")
    proof = cbor2.loads(proofs[0])
    if cbor2.dumps(proof, canonical=True) != proofs[0]:
        raise ValueError("the inclusion proof is not canonical CBOR")
    if not (
        isinstance(proof, list)
        and len(proof) == 3
        and all(isinstance(n, int) and not isinstance(n, bool) for n in proof[:2])
        and isinstance(proof[2], list)
        and all(isinstance(h, bytes) and len(h) == 32 for h in proof[2])
    ):
        raise ValueError("malformed inclusion proof")
    size, index, path = proof
    root = root_from_inclusion(index, size, leaf_of(statement), path)
    if root is None:
        raise ValueError("the inclusion proof doesn't fit the tree size")
    key = COSEKey.new(
        {1: 1, 2: key_id(log_public_raw), -1: 6, -2: log_public_raw, 3: EDDSA}
    )
    try:
        COSE.new().decode(receipt, key, detached_payload=root)
    except Exception as exc:  # noqa: BLE001 - cwt raises VerifyError and DecodeError
        raise ValueError("the log's signature doesn't cover this statement") from exc
    return int(index)
