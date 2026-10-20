"""Anchor an offline Seal in a public transparency log (Sigstore Rekor). Opt-in.

An offline Seal is signed by a key on the site's own machine, so on its own
it is the site's statement about itself. Anchoring adds a time and an
append-only record kept by a party the site does not control: Rekor, the
Sigstore transparency log. Anyone can later check that this exact Seal
existed when the log recorded it and has not changed since.

What becomes public, and nothing else:

* a SHA-512 digest of a short anchor statement (the Seal's SHA-256 and a
  random 32-byte nonce);
* the Seal key's public key, which links every Seal anchored with that key;
* an Ed25519ph signature over that digest, and the time Rekor recorded it.

No report content, field values, screenshots or run ids are sent. The nonce
is generated per Seal and kept only in the local anchor file, so a
low-entropy report can't be confirmed by guessing it and hashing the guess.
Anchoring is off by default; health-data deployments turn it on deliberately.

The anchor file sits next to the Seal (``seal.json`` ->
``seal.anchor.json``) and holds Rekor's full answer: the entry, its signed
entry timestamp, and its inclusion proof with the signed checkpoint. A
verifier checks all of it offline against Rekor's pinned public key;
fetching the entry again (``live=True``) is optional.

Rekor checks an Ed25519 key in a ``hashedrekord`` entry as Ed25519ph over a
SHA-512 digest, so the anchor signature uses :mod:`openadapt_flow.ed25519ph`
(or the PKCS#11 token's CKM_EDDSA with the prehash flag).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Optional, Sequence

from openadapt_types.seal import canonical_json

if TYPE_CHECKING:
    from openadapt_flow.seal_keys import SealSigner

ANCHOR_SCHEMA = "openadapt.local-seal-anchor/v1"
_STATEMENT_DOMAIN = (ANCHOR_SCHEMA + "\0").encode("utf-8")
REKOR_URL = "https://rekor.sigstore.dev"
# The public-good Rekor instance's log key, as served at
# https://rekor.sigstore.dev/api/v1/log/publicKey and distributed in the
# Sigstore trust root. Its SHA-256 (DER) is the log ID c0d23d6a...801d.
REKOR_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE2G2Y+2tabdTV5BcGiBIx0a9fAFwr
kBbmLSGtks4L3qX6yYY0zufBnhC8Ur/iy55GhWP/9A/bY2LhC30M9+RYtw==
-----END PUBLIC KEY-----
"""
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_TIMEOUT = 30

AnchorRefusal = Literal[
    "anchor_missing",
    "anchor_malformed",
    "anchor_mismatch",
    "anchor_key_mismatch",
    "anchor_bad_signature",
    "anchor_unknown_log",
    "anchor_bad_timestamp",
    "anchor_bad_inclusion",
    "anchor_bad_checkpoint",
    "anchor_live_mismatch",
]


class AnchorError(RuntimeError):
    """A closed reason a Seal was not anchored."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class AnchorVerification:
    ok: bool
    reason: Optional[AnchorRefusal] = None
    uuid: Optional[str] = None
    log_index: Optional[int] = None
    integrated_time: Optional[int] = None


def anchor_path(seal_path: Path) -> Path:
    """``seal.json`` -> ``seal.anchor.json``; ``x.seal.json`` -> ``x.seal.anchor.json``."""
    seal_path = Path(seal_path)
    stem = seal_path.name[:-5] if seal_path.name.endswith(".json") else seal_path.name
    return seal_path.with_name(stem + ".anchor.json")


def statement(seal_sha256: str, nonce: str) -> bytes:
    """The bytes whose SHA-512 goes to the log. Nothing else about the run does."""
    return _STATEMENT_DOMAIN + canonical_json(
        {"nonce": nonce, "seal_sha256": seal_sha256}
    ).encode("utf-8")


def new_nonce() -> str:
    return secrets.token_hex(32)


# --- keys --------------------------------------------------------------------


def _ed25519_pem(raw: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(raw).public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _raw_from_pem(pem: bytes) -> Optional[bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = serialization.load_pem_public_key(pem)
    except ValueError:
        return None
    if not isinstance(key, Ed25519PublicKey):
        return None
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass(frozen=True)
class LogKey:
    """A transparency log's public key, pinned by the verifier."""

    key: Any  # cryptography EllipticCurvePublicKey (or Ed25519PublicKey)
    log_id: str  # hex SHA-256 of the DER SubjectPublicKeyInfo

    @classmethod
    def from_pem(cls, pem: bytes | str) -> "LogKey":
        from cryptography.hazmat.primitives import serialization

        data = pem.encode("ascii") if isinstance(pem, str) else pem
        key = serialization.load_pem_public_key(data)
        der = key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return cls(key=key, log_id=hashlib.sha256(der).hexdigest())

    def verify(self, signature: bytes, payload: bytes) -> bool:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        try:
            if isinstance(self.key, ec.EllipticCurvePublicKey):
                self.key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
            elif isinstance(self.key, Ed25519PublicKey):
                self.key.verify(signature, payload)
            else:
                return False
        except (InvalidSignature, ValueError):
            return False
        return True


def default_log_keys() -> list[LogKey]:
    return [LogKey.from_pem(REKOR_PUBLIC_KEY_PEM)]


# --- Merkle inclusion and checkpoints (RFC 6962 / RFC 9162) -------------------


def leaf_hash(body: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + body).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def root_from_inclusion(
    index: int, size: int, leaf: bytes, proof: Sequence[bytes]
) -> Optional[bytes]:
    """The root an inclusion proof implies (RFC 9162 section 2.1.3.2), or None."""
    if index < 0 or size <= 0 or index >= size:
        return None
    fn, sn, r = index, size - 1, leaf
    for p in proof:
        if sn == 0:
            return None
        if fn & 1 or fn == sn:
            r = _node(p, r)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = _node(r, p)
        fn >>= 1
        sn >>= 1
    return r if sn == 0 else None


@dataclass(frozen=True)
class Checkpoint:
    origin: str
    size: int
    root: bytes


def verify_checkpoint(text: str, keys: Sequence[LogKey]) -> Optional[Checkpoint]:
    """Parse a signed note and check one signature against a pinned log key."""
    body, sep, sigs = text.partition("\n\n")
    if not sep:
        return None
    signed = (body + "\n").encode("utf-8")
    lines = body.split("\n")
    if len(lines) < 3:
        return None
    try:
        size = int(lines[1])
        root = base64.b64decode(lines[2], validate=True)
    except ValueError:
        return None
    for line in sigs.splitlines():
        if not line.startswith("— "):
            continue
        parts = line[2:].rsplit(" ", 1)
        if len(parts) != 2:
            continue
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except ValueError:
            continue
        hint, signature = blob[:4], blob[4:]
        for key in keys:
            if bytes.fromhex(key.log_id)[:4] == hint and key.verify(signature, signed):
                return Checkpoint(origin=lines[0], size=size, root=root)
    return None


# --- verification ------------------------------------------------------------


def _seal_digest(seal: Mapping[str, Any]) -> str:
    from openadapt_flow.offline_seal import seal_sha256

    return seal_sha256(seal)


def verify_entry(
    entry: Mapping[str, Any],
    *,
    statement_sha512: str,
    public_raw: bytes,
    log_keys: Sequence[LogKey],
) -> AnchorVerification:
    """Check one stored Rekor entry offline: body, signature, timestamp, inclusion."""
    from openadapt_flow.ed25519ph import verify_prehashed

    try:
        uuid = str(entry["uuid"])
        body_b64 = str(entry["body"])
        body_bytes = base64.b64decode(body_b64, validate=True)
        body = json.loads(body_bytes)
        integrated_time = int(entry["integratedTime"])
        log_id = str(entry["logID"])
        log_index = int(entry["logIndex"])
        verification = entry["verification"]
        set_sig = base64.b64decode(verification["signedEntryTimestamp"], validate=True)
        proof = verification["inclusionProof"]
        spec = body["spec"]
        hashed = spec["data"]["hash"]
        sig_content = base64.b64decode(spec["signature"]["content"], validate=True)
        key_pem = base64.b64decode(
            spec["signature"]["publicKey"]["content"], validate=True
        )
    except (KeyError, TypeError, ValueError):
        return AnchorVerification(False, reason="anchor_malformed")

    def refuse(reason: AnchorRefusal) -> AnchorVerification:
        return AnchorVerification(
            False,
            reason=reason,
            uuid=uuid,
            log_index=log_index,
            integrated_time=integrated_time,
        )

    if body.get("kind") != "hashedrekord" or body.get("apiVersion") != "0.0.1":
        return refuse("anchor_malformed")
    if hashed.get("algorithm") != "sha512" or hashed.get("value") != statement_sha512:
        return refuse("anchor_mismatch")
    if _raw_from_pem(key_pem) != public_raw:
        return refuse("anchor_key_mismatch")
    if not verify_prehashed(public_raw, bytes.fromhex(statement_sha512), sig_content):
        return refuse("anchor_bad_signature")
    log = next((k for k in log_keys if k.log_id == log_id), None)
    if log is None:
        return refuse("anchor_unknown_log")
    set_payload = canonical_json(
        {
            "body": body_b64,
            "integratedTime": integrated_time,
            "logID": log_id,
            "logIndex": log_index,
        }
    ).encode("utf-8")
    if not log.verify(set_sig, set_payload):
        return refuse("anchor_bad_timestamp")
    leaf = leaf_hash(body_bytes)
    if not uuid.endswith(leaf.hex()):
        return refuse("anchor_bad_inclusion")
    try:
        root = root_from_inclusion(
            int(proof["logIndex"]),
            int(proof["treeSize"]),
            leaf,
            [bytes.fromhex(h) for h in proof["hashes"]],
        )
        claimed_root = bytes.fromhex(proof["rootHash"])
        checkpoint_text = str(proof["checkpoint"])
    except (KeyError, TypeError, ValueError):
        return refuse("anchor_malformed")
    if root is None or root != claimed_root:
        return refuse("anchor_bad_inclusion")
    checkpoint = verify_checkpoint(checkpoint_text, [log])
    if (
        checkpoint is None
        or checkpoint.root != claimed_root
        or checkpoint.size != int(proof["treeSize"])
    ):
        return refuse("anchor_bad_checkpoint")
    return AnchorVerification(
        True, uuid=uuid, log_index=log_index, integrated_time=integrated_time
    )


def verify_anchor(
    seal: Mapping[str, Any],
    anchor: Any,
    public_raw: bytes,
    *,
    log_keys: Optional[Sequence[LogKey]] = None,
    fetch: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
) -> AnchorVerification:
    """Check that ``anchor`` anchors exactly ``seal``, signed by ``public_raw``.

    ``public_raw`` is the pinned key the Seal itself verified against, so
    the anchor proves nothing about a key the verifier did not pin. With
    ``fetch``, the entry is also read back from the log and compared.
    """
    if anchor is None:
        return AnchorVerification(False, reason="anchor_missing")
    try:
        if anchor["schema"] != ANCHOR_SCHEMA:
            raise ValueError("schema")
        nonce = str(anchor["nonce"])
        seal_hex = str(anchor["seal_sha256"])
        entry = anchor["entry"]
        log_url = str(anchor.get("log_url") or REKOR_URL)
        if not (_HEX64.match(nonce) and _HEX64.match(seal_hex)):
            raise ValueError("format")
    except (KeyError, TypeError, ValueError):
        return AnchorVerification(False, reason="anchor_malformed")
    if seal_hex != _seal_digest(seal):
        return AnchorVerification(False, reason="anchor_mismatch")
    digest = hashlib.sha512(statement(seal_hex, nonce)).hexdigest()
    if anchor.get("statement_sha512") not in (None, digest):
        return AnchorVerification(False, reason="anchor_mismatch")
    result = verify_entry(
        entry,
        statement_sha512=digest,
        public_raw=public_raw,
        log_keys=log_keys if log_keys is not None else default_log_keys(),
    )
    if not result.ok or fetch is None:
        return result
    live = fetch(log_url, str(entry["uuid"]))
    if any(
        live.get(k) != entry.get(k)
        for k in ("body", "integratedTime", "logID", "logIndex")
    ):
        return AnchorVerification(
            False,
            reason="anchor_live_mismatch",
            uuid=result.uuid,
            log_index=result.log_index,
            integrated_time=result.integrated_time,
        )
    return result


def load_anchor(seal_path: Path) -> Optional[dict[str, Any]]:
    path = anchor_path(seal_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"schema": "unreadable"}
    return data if isinstance(data, dict) else {"schema": "unreadable"}


# --- the Rekor client ----------------------------------------------------------


def _http(
    method: str, url: str, payload: Optional[Mapping[str, Any]] = None
) -> tuple[int, Any, Mapping[str, str]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            return response.status, json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError:
            body = None
        return exc.code, body, dict(exc.headers or {})


def _one_entry(answer: Any) -> dict[str, Any]:
    if not (isinstance(answer, dict) and len(answer) == 1):
        raise AnchorError("log_answer_malformed", "expected one entry")
    uuid, entry = next(iter(answer.items()))
    if not isinstance(entry, dict):
        raise AnchorError("log_answer_malformed", "entry is not an object")
    return {"uuid": uuid, **entry}


def fetch_entry(log_url: str, uuid: str) -> dict[str, Any]:
    status, answer, _ = _http("GET", f"{log_url.rstrip('/')}/api/v1/log/entries/{uuid}")
    if status != 200:
        raise AnchorError("log_unavailable", f"GET entry returned {status}")
    return _one_entry(answer)


def submit_hashedrekord(
    log_url: str, statement_sha512: str, signature: bytes, public_raw: bytes
) -> dict[str, Any]:
    """POST one hashedrekord entry; on a 409 (already logged) fetch the existing one."""
    proposed = {
        "apiVersion": "0.0.1",
        "kind": "hashedrekord",
        "spec": {
            "data": {"hash": {"algorithm": "sha512", "value": statement_sha512}},
            "signature": {
                "content": base64.b64encode(signature).decode("ascii"),
                "publicKey": {
                    "content": base64.b64encode(_ed25519_pem(public_raw)).decode(
                        "ascii"
                    )
                },
            },
        },
    }
    base = log_url.rstrip("/")
    status, answer, headers = _http("POST", f"{base}/api/v1/log/entries", proposed)
    if status == 201:
        entry = _one_entry(answer)
    elif status == 409:
        location = {k.lower(): v for k, v in headers.items()}.get("location", "")
        uuid = location.rstrip("/").rsplit("/", 1)[-1]
        if not uuid:
            raise AnchorError("log_refused", "409 without a Location")
        entry = fetch_entry(base, uuid)
    else:
        detail = answer.get("message") if isinstance(answer, dict) else ""
        raise AnchorError("log_refused", f"{status} {detail or ''}".strip())
    if "inclusionProof" not in (entry.get("verification") or {}):
        entry = fetch_entry(base, entry["uuid"])
    return entry


# --- anchoring ------------------------------------------------------------------


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def anchor_seal(
    seal_path: Path,
    signer: "SealSigner",
    *,
    log_url: str = REKOR_URL,
    log_keys: Optional[Sequence[LogKey]] = None,
    submit: Callable[[str, str, bytes, bytes], Mapping[str, Any]] = submit_hashedrekord,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Anchor the Seal at ``seal_path`` and write its anchor file. Idempotent.

    Sends one SHA-512 digest, the Seal key's public key and a signature.
    Refuses in strict no-outbound mode, when ``signer`` is not the key that
    signed the Seal, and when the log's answer does not verify.
    """
    from openadapt_flow import no_outbound
    from openadapt_flow.ed25519ph import verify_prehashed
    from openadapt_flow.offline_seal import LocalSealV1
    from openadapt_flow.seal_keys import KeyUnavailable

    seal_path = Path(seal_path)
    try:
        seal = LocalSealV1.model_validate(
            json.loads(seal_path.read_text(encoding="utf-8"))
        ).wire()
    except Exception as exc:
        raise AnchorError("seal_unreadable", str(seal_path)) from exc
    keys = log_keys if log_keys is not None else default_log_keys()
    try:
        public_raw = signer.public_raw()
    except KeyUnavailable as exc:
        raise AnchorError(exc.code, str(exc)) from exc
    if signer.key_id != seal["key_id"]:
        raise AnchorError(
            "anchor_key_mismatch",
            f"the Seal was signed by {seal['key_id']}, not {signer.key_id}",
        )
    seal_hex = _seal_digest(seal)
    out = anchor_path(seal_path)
    existing = load_anchor(seal_path)
    if existing is not None and "entry" in existing:
        if verify_anchor(seal, existing, public_raw, log_keys=keys).ok:
            return existing
        raise AnchorError("anchor_exists", f"{out} does not verify; inspect it by hand")
    if existing is not None and existing.get("seal_sha256") == seal_hex:
        nonce = str(existing.get("nonce", ""))  # resume a submission that was cut off
        if not _HEX64.match(nonce):
            raise AnchorError("anchor_exists", f"{out} is unreadable")
    elif existing is not None:
        raise AnchorError("anchor_exists", f"{out} belongs to a different Seal")
    else:
        nonce = new_nonce()
    message = statement(seal_hex, nonce)
    digest = hashlib.sha512(message).hexdigest()
    pending = {
        "schema": ANCHOR_SCHEMA,
        "seal_sha256": seal_hex,
        "nonce": nonce,
        "statement_sha512": digest,
        "key_id": seal["key_id"],
        "log_url": log_url,
    }
    if no_outbound.enabled(env):
        raise AnchorError("held_no_outbound", "strict no-outbound mode is on")
    try:
        signature = signer.sign_ph(message)
    except KeyUnavailable as exc:
        raise AnchorError(exc.code, str(exc)) from exc
    if not verify_prehashed(public_raw, bytes.fromhex(digest), signature):
        raise AnchorError("anchor_bad_signature", "the key's signature does not verify")
    # Keep the nonce before anything leaves the machine, so a retry after a
    # lost answer re-submits the same digest and finds the same entry.
    _write_json(out, pending)
    entry = dict(submit(log_url, digest, signature, public_raw))
    anchor = {**pending, "entry": entry}
    result = verify_anchor(seal, anchor, public_raw, log_keys=keys)
    if not result.ok:
        raise AnchorError("log_answer_invalid", str(result.reason))
    _write_json(out, anchor)
    return anchor
