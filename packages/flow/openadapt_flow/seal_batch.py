"""Optional batched timestamps for offline Seals.

Offline Seals (:mod:`openadapt_flow.offline_seal`) are complete on their own.
A site that wants a third party to vouch for *when* they existed can batch
them: every queued Seal becomes a leaf of a Merkle tree, and one timestamp
request covers the root. One request covers a whole batch of runs, so only
one 32-byte digest per batch leaves the machine, and nothing at all leaves
until a person runs ``openadapt-flow local-seal submit``.

The tree follows RFC 6962: a leaf is ``sha256(0x00 || seal_sha256)`` and an
inner node is ``sha256(0x01 || left || right)``. Each Seal's entry in the
batch file carries its inclusion proof, so one Seal can be shown to an
auditor with its proof and the batch root without revealing the others.

A timestamp authority is any object with ``name`` and
``timestamp(digest: bytes) -> bytes`` (an RFC 3161 token). The countersign
module's ``HttpTsa`` (Sigstore by default) and ``LocalTsa`` fit that shape.
An authority is treated as one that sends data off the machine unless it
sets ``MAY_EGRESS = False``. In strict no-outbound mode
(:mod:`openadapt_flow.no_outbound`) such an authority is never called: the
batch is kept, marked ``held_no_outbound``, and can be submitted later from
a machine that is allowed to.

Nothing here runs during replay and nothing here calls a model.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from openadapt_flow import no_outbound
from openadapt_flow.offline_seal import (
    LocalSealV1,
    seal_home,
    seal_sha256,
    sign_subject,
)

BATCH_SCHEMA = "openadapt.local-seal-batch/v1"
MAX_BATCH = 4096


class TimestampAuthority(Protocol):
    @property
    def name(self) -> str: ...

    def timestamp(self, digest: bytes) -> bytes: ...


@dataclass(frozen=True)
class SubmitResult:
    batch: Path
    status: (
        str  # timestamped | held_no_outbound | timestamp_failed | already_timestamped
    )
    detail: str = ""


def _leaf(seal_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(seal_hex)).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def merkle_root_and_proofs(
    seal_hexes: Sequence[str],
) -> "tuple[str, list[list[dict[str, str]]]]":
    """Root (hex) and one inclusion proof per leaf, RFC 6962 shape."""
    if not seal_hexes:
        raise ValueError("a batch needs at least one Seal")
    level = [_leaf(h) for h in seal_hexes]
    proofs: list[list[dict[str, str]]] = [[] for _ in seal_hexes]
    index = list(range(len(level)))
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_node(level[i], level[i + 1]))
            else:
                nxt.append(level[i])  # odd node is promoted unchanged
        for leaf_no, at in enumerate(index):
            sibling = at ^ 1
            if sibling < len(level):
                side = "left" if sibling < at else "right"
                proofs[leaf_no].append({"side": side, "hash": level[sibling].hex()})
            index[leaf_no] = at // 2
        level = nxt
    return level[0].hex(), proofs


def verify_inclusion(
    seal_hex: str, proof: Sequence[Mapping[str, str]], root_hex: str
) -> bool:
    """True if ``proof`` places the Seal digest under ``root_hex``."""
    try:
        acc = _leaf(seal_hex)
        for step in proof:
            sibling = bytes.fromhex(step["hash"])
            if step["side"] == "left":
                acc = _node(sibling, acc)
            elif step["side"] == "right":
                acc = _node(acc, sibling)
            else:
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return acc.hex() == root_hex


def _dirs(home: Path) -> "tuple[Path, Path, Path]":
    base = home / "batch"
    return base / "pending", base / "included", base / "batches"


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def build_batch(
    home: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
    max_items: int = MAX_BATCH,
) -> Optional[Path]:
    """Fold queued Seals into one signed batch file. Local only; no network.

    Returns the batch path, or ``None`` when nothing is queued. The batch
    root is itself sealed with the local key (``subject_kind`` ``file``).
    """
    home = home or seal_home()
    pending, included, batches = _dirs(home)
    if not pending.is_dir():
        return None
    files = sorted(p for p in pending.glob("*.json"))[:max_items]
    items: list[dict[str, Any]] = []
    for path in files:
        seal = LocalSealV1.model_validate(json.loads(path.read_text(encoding="utf-8")))
        digest = seal_sha256(seal.wire())
        if digest != path.stem:
            raise ValueError(f"queued Seal {path.name} does not match its digest")
        items.append(
            {
                "seal_sha256": digest,
                "run_id": seal.run_id,
                "subject_kind": seal.subject_kind,
                "subject_sha256": seal.subject_sha256,
            }
        )
    if not items:
        return None
    root, proofs = merkle_root_and_proofs([i["seal_sha256"] for i in items])
    for item, proof in zip(items, proofs):
        item["proof"] = proof
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root_seal = sign_subject("file", root, f"batch-{root[:16]}", home=home, now=created)
    batch = {
        "schema": BATCH_SCHEMA,
        "root_sha256": root,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "items": items,
        "root_seal": root_seal.wire(),
        "timestamps": [],
        "status": "built",
    }
    batches.mkdir(parents=True, exist_ok=True)
    out = batches / f"{root}.json"
    _write(out, batch)
    done = included / root
    done.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.move(str(path), str(done / path.name))
    return out


def _may_egress(authority: Any) -> bool:
    return getattr(authority, "MAY_EGRESS", True) is not False


def submit_batch(
    batch_path: Path,
    authority: TimestampAuthority,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> SubmitResult:
    """Timestamp one batch root. Sends only the 32-byte root digest.

    In strict no-outbound mode an authority that may send data off the
    machine is not called; the batch is marked ``held_no_outbound``.
    """
    batch_path = Path(batch_path)
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    if batch.get("schema") != BATCH_SCHEMA:
        raise ValueError("not a local Seal batch")
    if batch.get("status") == "timestamped":
        return SubmitResult(batch_path, "already_timestamped")
    if no_outbound.enabled(env) and _may_egress(authority):
        batch["status"] = "held_no_outbound"
        _write(batch_path, batch)
        return SubmitResult(
            batch_path, "held_no_outbound", "strict no-outbound mode is on"
        )
    root = batch["root_sha256"]
    try:
        token = authority.timestamp(bytes.fromhex(root))
    except Exception as exc:  # a TSA failure leaves the batch as it was
        return SubmitResult(batch_path, "timestamp_failed", type(exc).__name__)
    if not isinstance(token, (bytes, bytearray)) or not token:
        return SubmitResult(batch_path, "timestamp_failed", "empty token")
    batch["timestamps"].append(
        {
            "tsa": str(getattr(authority, "name", "unknown")),
            "imprint_sha256": root,
            "token": base64.b64encode(bytes(token)).decode("ascii"),
        }
    )
    batch["status"] = "timestamped"
    _write(batch_path, batch)
    return SubmitResult(batch_path, "timestamped")


def submit_all(
    authority: TimestampAuthority,
    home: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> list[SubmitResult]:
    """Submit every batch not yet timestamped: one request per batch."""
    _, _, batches = _dirs(home or seal_home())
    results: list[SubmitResult] = []
    if not batches.is_dir():
        return results
    for path in sorted(batches.glob("*.json")):
        results.append(submit_batch(path, authority, env=env))
    return results


def proof_for(
    seal: Mapping[str, Any], home: Optional[Path] = None
) -> Optional[dict[str, Any]]:
    """Find the batch holding ``seal``: its root, proof and timestamps."""
    digest = seal_sha256(dict(seal))
    _, _, batches = _dirs(home or seal_home())
    if not batches.is_dir():
        return None
    for path in sorted(batches.glob("*.json")):
        batch = json.loads(path.read_text(encoding="utf-8"))
        for item in batch.get("items", []):
            if item.get("seal_sha256") == digest:
                return {
                    "batch": path.name,
                    "root_sha256": batch["root_sha256"],
                    "proof": item["proof"],
                    "status": batch.get("status"),
                    "timestamps": batch.get("timestamps", []),
                    "verified_inclusion": verify_inclusion(
                        digest, item["proof"], batch["root_sha256"]
                    ),
                }
    return None


__all__ = [
    "BATCH_SCHEMA",
    "SubmitResult",
    "TimestampAuthority",
    "build_batch",
    "merkle_root_and_proofs",
    "proof_for",
    "submit_all",
    "submit_batch",
    "verify_inclusion",
]
