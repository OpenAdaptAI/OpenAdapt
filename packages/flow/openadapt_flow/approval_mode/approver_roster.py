"""Who may approve a save: a signed roster, rooted in a pinned key.

A person's answer counts only when a registered approver key signs it
(:mod:`.human_attestation`). The list of those keys used to be a plain map in
``principals.json``: anything that could write the file, or point a run at
another key directory, could add itself. Now:

* **A signed roster.** Approvers live in ``approvers.json`` in the key
  directory. Its root is one approver key, self-signed. Every later change
  (adding or removing an approver) is signed by an approver already on the
  roster at that point. Editing the file adds nobody: an entry without a
  valid signature from a current approver is refused, and so is everything
  after it.
* **A pinned root.** A run names the root approver key it trusts: the
  deployment config's ``approvers.root_public_key``, ``--approver-root``, or
  ``OPENADAPT_APPROVER_ROOT`` (:func:`pinned_root`). A roster whose root is a
  different key, or a key directory without a roster, approves nothing.
  Without a pin, a person's answer is refused.
* **The first approver** is set up in Desktop, behind Touch ID (Desktop
  signs the root with its approver key after a presence prompt), or, where
  there is no such app, pinned by whoever owns the deployment config.

Every check fails closed. :func:`verified_approvers` is the one place that
reads the roster; the item approvals, durable resume approvals, approver
grants and the re-arm of "only ask about problems" all use it.

Limit: this protects the runs a deployment governs. A process that can
rewrite the deployment config itself, or that starts its own run with its
own pin, is outside it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

ROSTER_FILE = "approvers.json"
ROSTER_SCHEMA = "openadapt-flow.approver-roster/v1"
#: What a roster signature covers: this domain, then the canonical entry.
ROSTER_DOMAIN = b"openadapt-flow.approver-roster/v1\x00"
#: Where a run's pin comes from when nothing else names it.
ROOT_ENV = "OPENADAPT_APPROVER_ROOT"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")

#: ``sign(message) -> "ed25519:<hex>"``: an approver key held anywhere (a
#: file key, Desktop's Keychain behind Touch ID).
Sign = Callable[[bytes], str]


class RosterError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RosterEntry(BaseModel):
    """One signed change to the roster."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Position in the roster, from 1. Signed, so an entry can't be moved,
    #: replayed or dropped from the middle.
    seq: int = Field(ge=1)
    #: The previous entry's :func:`entry_hash` (``genesis`` for the root).
    prev: str = Field(pattern=r"^(genesis|sha256:[0-9a-f]{64})$")
    action: Literal["root", "add", "remove"]
    principal: str = Field(pattern=_ID)
    public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    at: str = Field(min_length=1, max_length=64)
    #: The approver who signed this change (the root signs itself).
    signed_by: str = Field(pattern=_ID)
    signature: str = Field(min_length=1, max_length=256)

    def signed_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data.pop("signature", None)
        return data


class Roster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt-flow.approver-roster/v1"] = (
        "openadapt-flow.approver-roster/v1"
    )
    entries: list[RosterEntry] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify(public_hex: str, payload: Mapping[str, Any], signature: str) -> bool:
    from openadapt_flow.jit.authority import _verify as verify

    return verify(public_hex, ROSTER_DOMAIN, dict(payload), signature)


def roster_message(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes an approver key signs for one roster change."""

    return ROSTER_DOMAIN + _canonical(dict(payload))


def _path(key_dir: Path | str) -> Path:
    return Path(key_dir) / ROSTER_FILE


def normalize_root(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if not _HEX_KEY.fullmatch(text):
        raise RosterError(
            "approver_root_invalid", "the approver root is 64 hex characters"
        )
    return text


def pinned_root(
    explicit: Optional[str] = None,
    *,
    config_root: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    config_only: bool = False,
) -> Optional[str]:
    """The root approver key this run trusts, or ``None`` when none is pinned.

    ``config_only`` (a governed run, or one an agent started): only the
    deployment config's ``approvers.root_public_key`` counts; ``--approver-root``
    and :data:`ROOT_ENV` are ignored, since whoever starts the run sets them.
    Otherwise the deployment config, ``--approver-root`` and :data:`ROOT_ENV`
    all count, and two that disagree are refused rather than one silently
    winning.
    """

    if config_only:
        return normalize_root(config_root)
    source = os.environ if env is None else env
    named = [
        normalize_root(value)
        for value in (config_root, explicit, source.get(ROOT_ENV))
        if value
    ]
    if len(set(named)) > 1:
        raise RosterError(
            "approver_root_conflict",
            "the deployment config, --approver-root and "
            f"{ROOT_ENV} name different approver roots",
        )
    return named[0] if named else None


def load(key_dir: Path | str) -> Roster:
    """The roster as stored, unverified. Missing means empty."""

    path = _path(key_dir)
    if not path.exists():
        return Roster()
    try:
        return Roster.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RosterError("approver_roster_unreadable", str(exc)) from exc


def _other_role_keys(key_dir: Path | str) -> dict[str, str]:
    """Keys that hold another role (policy signer, admitter, agent)."""

    from openadapt_flow.jit.authority import PrincipalRegistry

    try:
        registry = PrincipalRegistry.load(Path(key_dir))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for field in ("policy_signers", "admitter_keys", "agents"):
        for pid, public in getattr(registry, field).items():
            out[public] = pid
    return out


def entry_hash(entry: RosterEntry) -> str:
    """The hash the next entry names as ``prev`` (covers the signature too)."""

    return (
        "sha256:"
        + hashlib.sha256(_canonical(entry.model_dump(mode="json"))).hexdigest()
    )


#: ``(seq, entry_hash)`` of the last entry a roster's replay accepted.
Head = tuple[int, str]


def _replay(
    key_dir: Path | str, root: Optional[str]
) -> tuple[dict[str, str], list[RosterEntry]]:
    """``(active approvers, the entries that count)`` from the pinned root.

    The first entry must be the pinned root, signed by itself, at ``seq`` 1
    after ``genesis``. Each later entry must come next (``seq``), name the
    hash of the entry before it (``prev``), and be signed by an approver on
    the roster at that point. The first entry that fails stops the replay:
    nothing after it counts, so an entry can't be replayed, reordered or
    dropped from the middle.
    """

    from openadapt_flow.jit.lifecycle import is_not_a_person_name

    pinned = normalize_root(root)
    if pinned is None:
        raise RosterError(
            "approver_root_unpinned",
            "this run names no approver root, so no person's answer can be "
            "verified; set approvers.root_public_key in the deployment config",
        )
    roster = load(key_dir)
    if not roster.entries:
        raise RosterError(
            "approver_roster_missing",
            "this key directory has no approver roster; set up the first "
            "approver in Desktop, or with openadapt-flow jit approver init",
        )
    first = roster.entries[0]
    if (
        first.action != "root"
        or first.seq != 1
        or first.prev != "genesis"
        or first.public_key != pinned
        or first.signed_by != first.principal
        or not _verify(first.public_key, first.signed_payload(), first.signature)
    ):
        raise RosterError(
            "approver_root_mismatch",
            "the approver roster's root isn't the approver root this run pins",
        )
    active: dict[str, str] = {first.principal: first.public_key}
    kept = [first]
    for entry in roster.entries[1:]:
        if entry.seq != len(kept) + 1 or entry.prev != entry_hash(kept[-1]):
            break
        signer = active.get(entry.signed_by)
        if signer is None or not _verify(
            signer, entry.signed_payload(), entry.signature
        ):
            break
        if entry.action == "add":
            if (
                entry.public_key in active.values()
                or entry.principal in active
                or is_not_a_person_name(entry.principal)
            ):
                break
            active[entry.principal] = entry.public_key
        elif entry.action == "remove":
            if (
                entry.principal == first.principal
                or active.get(entry.principal) != entry.public_key
            ):
                break
            del active[entry.principal]
        else:
            break
        kept.append(entry)
    return active, kept


def head(key_dir: Path | str, root: Optional[str]) -> Head:
    """The roster's head (last accepted entry) under the pinned root."""

    _active, kept = _replay(key_dir, root)
    return kept[-1].seq, entry_hash(kept[-1])


def verified_approvers(
    key_dir: Path | str,
    root: Optional[str],
    *,
    at_least: Optional[Head] = None,
) -> dict[str, str]:
    """``{principal: public_key_hex}`` of every approver the pinned root vouches for.

    ``at_least`` is a head recorded earlier (by a run when it started, or by
    Desktop after its own last change). A roster that doesn't extend it (an
    older copy put back, or a different history) is refused, so an approver
    removed since can't come back by restoring an old file. A key that holds
    another role, or a name that names an agent or model, is never an
    approver. Raises :class:`RosterError` when nothing can be trusted.
    """

    from openadapt_flow.jit.lifecycle import is_not_a_person_name

    active, kept = _replay(key_dir, root)
    if at_least is not None:
        seq, digest = at_least
        if seq < 1 or len(kept) < seq or entry_hash(kept[seq - 1]) != digest:
            raise RosterError(
                "approver_roster_rolled_back",
                "the approver roster doesn't extend the one this run (or this "
                "computer) recorded; an older or different roster was put back",
            )
    others = _other_role_keys(key_dir)
    return {
        pid: public
        for pid, public in active.items()
        if public not in others and not is_not_a_person_name(pid)
    }


def _write(key_dir: Path | str, roster: Roster, *, new: bool) -> Path:
    path = _path(key_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = roster.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if new:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _entry(
    action: str,
    principal: str,
    public_key: str,
    signed_by: str,
    sign: Sign,
    *,
    seq: int,
    prev: str,
) -> RosterEntry:
    unsigned = {
        "seq": seq,
        "prev": prev,
        "action": action,
        "principal": principal,
        "public_key": str(public_key).lower(),
        "at": _now(),
        "signed_by": signed_by,
    }
    try:
        partial = RosterEntry.model_validate({**unsigned, "signature": "x"})
    except ValueError as exc:
        raise RosterError("approver_invalid", str(exc)) from exc
    signature = sign(roster_message(partial.signed_payload()))
    entry = partial.model_copy(update={"signature": str(signature)})
    signer_public = public_key if action == "root" else None
    if signer_public is not None and not _verify(
        signer_public, entry.signed_payload(), entry.signature
    ):
        raise RosterError(
            "approver_signature_invalid",
            "the root signature doesn't match the root key",
        )
    return entry


def init_root(
    key_dir: Path | str, *, principal: str, public_key: str, sign: Sign
) -> RosterEntry:
    """Start a roster with its root approver (the first approver).

    Refuses when a roster exists: a root is set once. Desktop calls this after
    a Touch ID prompt; elsewhere the deployment config's pin is what makes the
    root count.
    """

    from openadapt_flow.jit.lifecycle import is_not_a_person_name

    if is_not_a_person_name(principal):
        raise RosterError("approver_invalid", f"{principal!r} names an agent or model")
    if load(key_dir).entries:
        raise RosterError(
            "approver_roster_exists",
            "this key directory already has an approver roster; an approver "
            "on it adds new ones",
        )
    entry = _entry(
        "root", principal, public_key, principal, sign, seq=1, prev="genesis"
    )
    _write(key_dir, Roster(entries=[entry]), new=True)
    return entry


def _change(
    key_dir: Path | str,
    root: Optional[str],
    action: Literal["add", "remove"],
    *,
    principal: str,
    public_key: str,
    signed_by: str,
    sign: Sign,
    at_least: Optional[Head] = None,
) -> RosterEntry:
    approvers = verified_approvers(key_dir, root, at_least=at_least)
    _active, kept = _replay(key_dir, root)
    if signed_by not in approvers:
        raise RosterError(
            "approver_signer_unregistered",
            f"{signed_by!r} isn't an approver on this roster, so it can't change it",
        )
    entry = _entry(
        action,
        principal,
        public_key,
        signed_by,
        sign,
        seq=len(kept) + 1,
        prev=entry_hash(kept[-1]),
    )
    if not _verify(approvers[signed_by], entry.signed_payload(), entry.signature):
        raise RosterError(
            "approver_signature_invalid",
            f"the signature isn't {signed_by!r}'s",
        )
    roster = load(key_dir)
    # Build on the entries that count; anything after them never counted.
    candidate = Roster(entries=[*kept, entry])
    _write(key_dir, candidate, new=False)
    after = verified_approvers(key_dir, root)
    applied = (
        after.get(principal) == entry.public_key
        if action == "add"
        else principal not in after
    )
    if not applied:
        # The change didn't take (a duplicate key, an agent's name): put the
        # roster back as it was.
        _write(key_dir, roster, new=False)
        raise RosterError("approver_invalid", f"the roster refused this {action}")
    return entry


def add_approver(
    key_dir: Path | str,
    root: Optional[str],
    *,
    principal: str,
    public_key: str,
    signed_by: str,
    sign: Sign,
    at_least: Optional[Head] = None,
) -> RosterEntry:
    """Add an approver, signed by an approver already on the roster."""

    return _change(
        key_dir,
        root,
        "add",
        principal=principal,
        public_key=public_key,
        signed_by=signed_by,
        sign=sign,
        at_least=at_least,
    )


def remove_approver(
    key_dir: Path | str,
    root: Optional[str],
    *,
    principal: str,
    public_key: str,
    signed_by: str,
    sign: Sign,
    at_least: Optional[Head] = None,
) -> RosterEntry:
    """Remove an approver (not the root), signed by an approver on the roster."""

    return _change(
        key_dir,
        root,
        "remove",
        principal=principal,
        public_key=public_key,
        signed_by=signed_by,
        sign=sign,
        at_least=at_least,
    )


def approver_keys_b64(key_dir: Path | str, root: Optional[str]) -> dict[str, str]:
    """``{principal: base64 raw public key}`` for verifiers that take base64.

    The re-arm of "only ask about problems" (``switch.rearm``) verifies
    against this, so a re-arm counts only when an approver on the pinned
    roster signed it. Empty when nothing is pinned or verifiable.
    """

    import base64

    try:
        found = verified_approvers(key_dir, root)
    except RosterError:
        return {}
    return {
        pid: base64.b64encode(bytes.fromhex(public)).decode("ascii")
        for pid, public in found.items()
    }


def file_key_signer(key: Any) -> Sign:
    """``sign`` for an Ed25519 private key held in this process."""

    def sign(message: bytes) -> str:
        return "ed25519:" + key.sign(message).hex()

    return sign


__all__ = [
    "ROOT_ENV",
    "ROSTER_DOMAIN",
    "ROSTER_FILE",
    "Roster",
    "RosterEntry",
    "RosterError",
    "Head",
    "add_approver",
    "approver_keys_b64",
    "entry_hash",
    "head",
    "file_key_signer",
    "init_root",
    "load",
    "normalize_root",
    "pinned_root",
    "remove_approver",
    "roster_message",
    "verified_approvers",
]
