"""One RFC 3161 timestamp per record, stored next to it. Opt-in.

A transparency-log anchor (:mod:`openadapt_flow.seal_anchor`) records when
Sigstore Rekor saw a Seal. An RFC 3161 token from a timestamp authority
(Sigstore's, ``timestamp.sigstore.dev``, by default) is a second, independent
statement of time, signed by the TSA's certificate. The batched path
(:mod:`openadapt_flow.seal_batch`) covers many Seals with one token over a
Merkle root. This module stamps one record at a time, so each Seal or run
authorization carries its own token.

What is sent: one 32-byte SHA-256 imprint and a random nonce, nothing else.

- For a Seal, the imprint is ``seal_sha256`` (SHA-256 of the canonical Seal,
  signature included).
- For a run authorization, it is SHA-256 of
  ``canonical_json({"run_authorization_sha256": ..., "signature_sha256": ...})``,
  which covers the statement and the person's signature together.

The token is stored in its standard binary form, DER, as ``seal.tsr`` next
to ``seal.json`` (``run-authorization.tsr`` next to the statement). A small
JSON file beside it, ``seal.tsa.json`` / ``run-authorization.tsa.json``
(``openadapt.record-timestamp/v1``), names the record's digests, the imprint,
the TSA, the token file and the token's SHA-256. Before it is written, the token is
checked offline against the pinned TSA certificate: the Sigstore TSA root
shipped with OpenAdapt, unless the caller pins another.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_types.seal import canonical_json

SCHEMA = "openadapt.record-timestamp/v1"
_FIELDS = (("seal_sha256",), ("run_authorization_sha256", "signature_sha256"))


class RecordTimestampError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def timestamp_path(record_path: Path) -> Path:
    """``seal.json`` -> ``seal.tsa.json``; ``run-authorization.json`` -> ``run-authorization.tsa.json``."""
    record_path = Path(record_path)
    name = record_path.name
    stem = name[:-5] if name.endswith(".json") else name
    return record_path.with_name(stem + ".tsa.json")


def imprint(fields: Mapping[str, str]) -> bytes:
    """The 32 bytes the TSA signs over for a record named by ``fields``."""
    if tuple(sorted(fields)) not in {tuple(sorted(f)) for f in _FIELDS}:
        raise RecordTimestampError(
            "malformed", f"unknown record fields {sorted(fields)}"
        )
    if set(fields) == {"seal_sha256"}:
        return bytes.fromhex(fields["seal_sha256"])
    return hashlib.sha256(canonical_json(dict(fields)).encode("utf-8")).digest()


def default_trusted() -> list[Any]:
    from openadapt_flow.countersign.tsa import (
        load_pem_certificates,
        sigstore_tsa_root_pem,
    )

    return load_pem_certificates(sigstore_tsa_root_pem())


def token_path(timestamp_file: Path) -> Path:
    """``seal.tsa.json`` -> ``seal.tsr``: the DER TimeStampToken stored beside it."""
    timestamp_file = Path(timestamp_file)
    name = timestamp_file.name
    stem = name[: -len(".tsa.json")] if name.endswith(".tsa.json") else name
    return timestamp_file.with_name(stem + ".tsr")


def _token_file(data: Mapping[str, Any], path: Path) -> Path:
    """The token file the JSON names: a bare file name beside it, nothing else."""
    name = str(data["tsr_file"])
    if (
        not name
        or "/" in name
        or "\\" in name
        or name in (".", "..")
        or not name.endswith(".tsr")
    ):
        raise ValueError("tsr_file must be a .tsr file name beside the JSON")
    return Path(path).with_name(name)


def verify_record_timestamp(
    fields: Mapping[str, str], path: Path, trusted: Optional[Sequence[Any]] = None
) -> Any:
    """Check a stored token offline. Returns a ``TimestampVerification``."""
    from openadapt_flow.countersign.tsa import (
        TimestampVerification,
        verify_timestamp_token,
    )
    from openadapt_flow.offline_seal import loads_strict

    try:
        data = loads_strict(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return TimestampVerification(False, "timestamp_missing")
    except (OSError, ValueError):
        return TimestampVerification(False, "timestamp_malformed")
    try:
        if data["schema"] != SCHEMA:
            raise ValueError("schema")
        if any(data.get(k) != v for k, v in fields.items()):
            return TimestampVerification(False, "timestamp_mismatch")
        expected = imprint(fields)
        if data.get("imprint_sha256") != expected.hex():
            return TimestampVerification(False, "timestamp_mismatch")
        token_file = _token_file(data, Path(path))
        want_sha = str(data["tsr_sha256"])
    except (KeyError, TypeError, ValueError, AttributeError, RecordTimestampError):
        return TimestampVerification(False, "timestamp_malformed")
    try:
        token = token_file.read_bytes()
    except FileNotFoundError:
        return TimestampVerification(False, "timestamp_token_missing")
    except OSError:
        return TimestampVerification(False, "timestamp_malformed")
    if hashlib.sha256(token).hexdigest() != want_sha:
        return TimestampVerification(False, "timestamp_token_mismatch")
    pinned = list(trusted) if trusted is not None else default_trusted()
    return verify_timestamp_token(token, expected, pinned)


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def timestamp_record(
    fields: Mapping[str, str],
    out: Path,
    authority: Any,
    *,
    trusted: Optional[Sequence[Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Get one token for ``fields`` from ``authority``; write ``out`` and its ``.tsr``.

    The token is stored as-is (DER CMS ``TimeStampToken``) in ``<name>.tsr``;
    the JSON names that file and its SHA-256. Idempotent.
    """
    from openadapt_flow import no_outbound

    out = Path(out)
    tsr = token_path(out)
    if out.exists():
        existing = verify_record_timestamp(fields, out, trusted)
        if existing.ok:
            return json.loads(out.read_text(encoding="utf-8"))
        raise RecordTimestampError(
            "timestamp_exists", f"{out} does not verify ({existing.reason}); inspect it"
        )
    if no_outbound.enabled(env) and getattr(authority, "MAY_EGRESS", True) is not False:
        raise RecordTimestampError("held_no_outbound", "strict no-outbound mode is on")
    digest = imprint(fields)
    try:
        token = bytes(authority.timestamp(digest))
    except Exception as exc:  # unreachable TSA, bad answer: a closed reason
        raise RecordTimestampError("timestamp_failed", str(exc)) from exc
    pinned = list(trusted) if trusted is not None else default_trusted()
    from openadapt_flow.countersign.tsa import verify_timestamp_token

    result = verify_timestamp_token(token, digest, pinned)
    if not result.ok:
        raise RecordTimestampError("timestamp_invalid", str(result.reason))
    record = {
        "schema": SCHEMA,
        **fields,
        "imprint_sha256": digest.hex(),
        "tsa": str(getattr(authority, "name", "unknown")),
        "tsr_file": tsr.name,
        "tsr_sha256": hashlib.sha256(token).hexdigest(),
        "gen_time": result.gen_time,
    }
    _write_atomic(tsr, token)
    _write_atomic(
        out, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return record


def seal_fields(seal_path: Path) -> dict[str, str]:
    from openadapt_flow.offline_seal import LocalSealV1, loads_strict, seal_sha256

    seal = LocalSealV1.model_validate(
        loads_strict(Path(seal_path).read_text(encoding="utf-8"))
    )
    return {"seal_sha256": seal_sha256(seal.wire())}


def authorization_fields(statement_path: Path) -> dict[str, str]:
    from openadapt_flow.run_authorization import _paths

    statement, sig, _, _ = _paths(Path(statement_path))
    if not sig.is_file():
        raise RecordTimestampError("auth_unsigned", f"{sig} is missing; sign first")
    return {
        "run_authorization_sha256": hashlib.sha256(statement.read_bytes()).hexdigest(),
        "signature_sha256": hashlib.sha256(sig.read_bytes()).hexdigest(),
    }
